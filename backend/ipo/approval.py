"""Detached authority context for source-bound manual and extraction drafts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.ipo.manual_extraction import IpoManualExtractionRecord
from backend.ipo.models import IpoDocumentRecord, IpoIssueRecord, IpoValidationError


@dataclass(frozen=True)
class IpoManualEditContext:
    """Host-captured source and manual generation retained across slow work.

    Args:
        issue: Detached parent issue captured under its state lock.
        document: Owned source identity, date and cache metadata from that scope.
        manual_revision: Nonnegative integer generation, distinct from profile ID.
        latest_profile: Latest immutable profile read in that same scope, or None.

    Raises:
        IpoValidationError: For an invalid generation or mismatched source owner.

    Beginner note:
        The profile and generation are read under one state lock. A browser must
        retain this object, rather than refreshing its baseline on every rerun.
        Issue terms and source chronology remain separate approval inputs.
    """

    issue: IpoIssueRecord
    document: IpoDocumentRecord
    manual_revision: int
    latest_profile: IpoManualExtractionRecord | None

    def __post_init__(self) -> None:
        """Validate captured ownership and generation without type coercion.

        Raises:
            IpoValidationError: If ownership differs or the token is not a
                nonnegative integer. Booleans are deliberately rejected.

        Beginner note:
            Python treats bool as a subclass of int, but True cannot prove a
            captured generation. Authority metadata must preserve its exact type.
        """
        if type(self.manual_revision) is not int or self.manual_revision < 0:
            raise IpoValidationError("The manual baseline must be a nonnegative integer.")
        if self.document.issue_id != self.issue.id:
            raise IpoValidationError("The edit source must belong to the selected issue.")


def same_source(left: IpoDocumentRecord, right: IpoDocumentRecord) -> bool:
    """Compare authority-bearing source identity, chronology and cache metadata.

    Args:
        left: Originally captured source.
        right: Freshly read owned source.

    Returns:
        Whether the draft still refers to exactly the same source.

    Beginner note:
        A cache path or filing-date correction can change approval authority
        without changing scoring semantics. Both therefore belong in this check.
    """
    fields = ("id", "issue_id", "document_url", "document_type", "filing_date",
              "content_sha256", "file_path", "parse_status")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def current_document_id(documents: Sequence[IpoDocumentRecord]) -> int | None:
    """Select the one unambiguous current prospectus from the full inventory.

    Args:
        documents: All documents registered to one issue, including sources with
            no proposal or cached PDF. Callers must not filter by readiness.

    Returns:
        The latest dated RHP ID, or latest dated DRHP ID when no RHP exists.
        Returns None for no eligible sources, unknown dates, a tied latest date,
        or a DRHP dated after the latest RHP.

    Beginner note:
        A ready older proposal cannot become current just because the newest
        prospectus has not been extracted. RHP precedence reflects document
        authority; IDs and queue order cannot resolve ambiguous chronology.
        This pure helper does not approve anything. The final approval must
        repeat selection under the shared issue lock so a document write cannot
        invalidate the decision between this check and the evidence insert.
    """
    eligible = [document for document in documents if document.document_type in {"drhp", "rhp"}]
    dated = [(document, document.filing_date) for document in eligible if document.filing_date is not None]
    if not dated or len(dated) != len(eligible):
        # An undated competing filing might be newer; its database ID supplies
        # insertion order, which is not trustworthy publication chronology.
        return None
    final_prospectuses = [(document, day) for document, day in dated if document.document_type == "rhp"]
    candidates = final_prospectuses or dated
    latest_date = max(day for _document, day in candidates)
    latest = [document for document, day in candidates if day == latest_date]
    if len(latest) != 1:
        return None
    if final_prospectuses and any(
        document.document_type == "drhp" and day > latest_date for document, day in dated
    ):
        # A later draft contradicts the apparent RHP ordering. Require a human
        # to resolve the inventory instead of choosing either document by guess.
        return None
    return latest[0].id
