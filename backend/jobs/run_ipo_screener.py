"""Headless IPO-008 command: the one-shot IPO screener orchestration.

Run the full deterministic pipeline with:

    python -m backend.jobs.run_ipo_screener

Stages, each isolated per unit of work: (1) inventory official SEBI filings,
(2) download missing DRHP/RHP PDFs into the verified cache, (3) collect
optional low-confidence web enrichment (skipped gracefully without a SerpAPI
key), (4) — only with ``--extract`` — draft AI extraction proposals for the
human review queue, and (5) re-score every issue through the IPO-006 scoring
service, which persists a new immutable evaluation only when its inputs
fingerprint changed.

Beginner note:
Re-running this command is idempotent end to end: filings dedup on content
hashes, downloads hit the verified cache, proposals refuse duplicates, and
scoring skips issues whose evidence is unchanged. AI extraction stays behind
an explicit flag so schedulers and CI never spend model credit by accident,
and "missing data never becomes hallucinated data" holds structurally — an
issue without verified evidence lands in the insufficient-data list instead
of being scored from guesses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TextIO

from backend.ipo.agents.financial_extractor import (
    IpoExtractionErrorReceipt,
    propose_extraction,
)
from backend.ipo.models import (
    IpoDocumentParseStatus,
    IpoStatus,
)
from backend.ipo.repository import download_document, list_documents, list_issues
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    INSUFFICIENT_VERIFIED_DATA,
    SKIP,
)
from backend.ipo.scoring.service import IpoRescoreOutcome, rescore_issue
from backend.ipo.sources.enrichment import collect_enrichment_signals
from backend.jobs.scan_ipo_filings import IpoFilingJobOutcome, run_scan_ipo_filings
from backend.observability import (
    EVENT_IPO_SCREENER_COMPLETED,
    EVENT_IPO_SCREENER_STARTED,
    configure_logging,
    log_event,
)
from backend.storage.database import ensure_database_schema, session_scope

logger = logging.getLogger(__name__)

# Stable summary tokens for the four recommendation_type strings, so the CLI
# output stays grep-friendly key=value text without free-form prose.
_TYPE_TOKENS = {
    APPLY_AND_HOLD: "high_conviction",
    APPLY_FOR_LISTING_GAINS: "listing_gains",
    SKIP: "skip",
    INSUFFICIENT_VERIFIED_DATA: "insufficient_verified_data",
}

# The issue states worth spending a run on: the offer has not finished yet, so
# fresh evidence can still change the verdict.
#
# Beginner note — why ``closed`` is NOT here:
#     SEBI's filing categories map DRHP -> drhp_filed, RHP -> rhp_filed, and
#     final offer -> ``closed`` (see backend/ipo/sources/sebi.py). A final offer
#     document is filed *after* the issue completes, so ``closed`` means "this
#     IPO is over", and scanning it spends a hard-capped SerpAPI quota on a
#     decision nobody can act on any more.
#
#     There is deliberately no date comparison here. The issue row carries no
#     listing date at all, and ``open_date``/``close_date`` are never populated
#     by ingestion, so lifecycle stage is the only signal that actually exists.
#
#     One consequence to leave alone: ``_weak_qib_demand_near_close`` judges
#     issues whose status is OPEN or CLOSED, so its evidence stops refreshing
#     for closed issues. That is correct — the vocabulary overloads ``closed``,
#     and the one produced by ingestion means "final offer filed", long past
#     the book close that flag is about.
UPCOMING_ISSUE_STATUSES = (
    IpoStatus.DRHP_FILED,
    IpoStatus.RHP_FILED,
    IpoStatus.OPEN,
)


@dataclass(frozen=True)
class IpoScreenerIssueOutcome:
    """Hold one issue's sanitized, printable scoring-stage outcome.

    Beginner note:
        The CLI deliberately carries only stable verdict fields and exception
        type names. Prospectus text, search snippets, and exception messages
        never enter terminal output where secrets or hostile Markdown could
        leak.
    """

    issue_id: int
    company_name: str
    status: str
    score: Decimal | None = None
    recommendation: str | None = None
    recommendation_type: str | None = None
    confidence: str | None = None
    triggered_flags: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    error_type: str | None = None


@dataclass(frozen=True)
class IpoScreenerJobOutcome:
    """Aggregate every stage into one CLI exit contract.

    ``enrichment_skipped_no_key`` is a configuration state, not a failure —
    the screener is fully functional without SerpAPI. Every genuine stage
    failure keeps its unit isolated but still drives the exit code nonzero so
    schedulers notice.

    Beginner note:
        Counters make partial progress visible. A job may successfully inventory
        and score most issues while one download fails; the nonzero exit code
        alerts automation without discarding the completed work.
    """

    filings: IpoFilingJobOutcome | None = None
    downloads_attempted: int = 0
    downloads_failed: int = 0
    enrichment_collected: int = 0
    enrichment_failed: int = 0
    enrichment_skipped_no_key: bool = False
    proposals_created: int = 0
    proposals_skipped: int = 0
    proposals_failed: int = 0
    issues: tuple[IpoScreenerIssueOutcome, ...] = ()
    fatal: bool = False

    @property
    def exit_code(self) -> int:
        """Return nonzero when any stage or issue genuinely failed.

        Beginner note:
            Missing optional SerpAPI configuration and insufficient verified
            IPO data are expected states, so neither is counted as a process
            failure.
        """
        return int(
            self.fatal
            or (self.filings is not None and self.filings.exit_code != 0)
            or self.downloads_failed > 0
            or self.enrichment_failed > 0
            or self.proposals_failed > 0
            or any(item.status == "failed" for item in self.issues)
        )


def _print_issue(out: TextIO, outcome: IpoScreenerIssueOutcome) -> None:
    """Write one bounded, evidence-free summary line for one issue.

    Beginner note:
        One line per issue is easy for schedulers to capture and avoids dumping
        untrusted evidence. Human-facing detail remains in the reviewed
        dashboard and immutable evaluation receipt.
    """
    if outcome.status == "failed":
        print(
            f"[ipo-screener] failed issue_id={outcome.issue_id} "
            f"error_type={outcome.error_type} company={outcome.company_name}",
            file=out,
            flush=True,
        )
        return
    if outcome.status == "insufficient_inputs" or (
        outcome.recommendation_type == INSUFFICIENT_VERIFIED_DATA
    ):
        missing = ",".join(outcome.missing) or "unknown"
        print(
            f"[ipo-screener] insufficient_data issue_id={outcome.issue_id} "
            f"missing={missing} company={outcome.company_name}",
            file=out,
            flush=True,
        )
        return
    verdict = "recommended" if outcome.recommendation == "Recommended" else "not_recommended"
    token = _TYPE_TOKENS.get(outcome.recommendation_type or "", "unknown")
    line = (
        f"[ipo-screener] {verdict} issue_id={outcome.issue_id} "
        f"score={outcome.score} type={token} confidence={outcome.confidence}"
    )
    if outcome.triggered_flags:
        line += f" flags={','.join(outcome.triggered_flags)}"
    if outcome.status == "skipped_unchanged":
        line += " unchanged=true"
    line += f" company={outcome.company_name}"
    print(line, file=out, flush=True)


def _issue_outcome_from_rescore(outcome: IpoRescoreOutcome) -> IpoScreenerIssueOutcome:
    """Flatten one scoring-service outcome into the printable job shape.

    Beginner note:
        Orchestration does not reinterpret the verdict; it copies the persisted
        result and selects only fields safe for terminal output.
    """
    evaluation = outcome.evaluation
    if evaluation is None:
        return IpoScreenerIssueOutcome(
            issue_id=outcome.issue_id,
            company_name=outcome.company_name,
            status=outcome.status,
            missing=outcome.missing,
        )
    result = evaluation.result
    return IpoScreenerIssueOutcome(
        issue_id=outcome.issue_id,
        company_name=outcome.company_name,
        status=outcome.status,
        score=result.score,
        recommendation=result.recommendation.value,
        recommendation_type=result.recommendation_type,
        confidence=result.confidence.value,
        triggered_flags=tuple(
            flag.name for flag in result.caution_flags if flag.status.value == "triggered"
        ),
        missing=result.missing_data,
    )


def run_ipo_screener(
    *,
    skip_scan: bool = False,
    skip_download: bool = False,
    skip_enrich: bool = False,
    skip_score: bool = False,
    extract: bool = False,
    force_extract: bool = False,
    issue_ids: Sequence[int] | None = None,
    to_date: dt.date | None = None,
    ensure_schema: Callable[[], object] = ensure_database_schema,
    filings_runner: Callable[..., IpoFilingJobOutcome] = run_scan_ipo_filings,
    issue_lister: Callable[..., list[Any]] = list_issues,
    document_lister: Callable[..., list[Any]] = list_documents,
    document_downloader: Callable[..., Any] = download_document,
    enricher: Callable[..., Any] = collect_enrichment_signals,
    extractor: Callable[..., Any] = propose_extraction,
    rescorer: Callable[..., IpoRescoreOutcome] = rescore_issue,
    session_factory: Any = session_scope,
    output: TextIO | None = None,
) -> IpoScreenerJobOutcome:
    """Run scan -> download -> enrich -> (optional) extract -> score once.

    Every collaborator is injectable so the command is testable without SEBI,
    SerpAPI, the Claude SDK, or a real database; production uses the defaults.

    ``skip_score`` exists for a caller that must ingest filings *before* it can
    decide which issues to work on: it runs the scan, leaves scoring alone, and
    lets that caller invoke the pipeline again with the selection it could only
    compute once the new filings existed.

    Beginner note:
        Stage isolation is per unit of work (one document, one issue, one
        query batch). A malformed PDF or one flaky search can therefore never
        abort the remaining issues — it becomes a counted, typed failure in
        the summary and a nonzero exit code at the end.
    """
    out = output or sys.stdout
    try:
        if ensure_schema() is False:
            raise RuntimeError("database schema bootstrap failed")
    except Exception as exc:  # noqa: BLE001 - command boundary becomes exit code
        error_type = type(exc).__name__
        print(f"[ipo-screener] FAILED error_type={error_type}", file=out, flush=True)
        log_event(
            logger,
            EVENT_IPO_SCREENER_COMPLETED,
            level=logging.ERROR,
            fatal=True,
            error_type=error_type,
        )
        return IpoScreenerJobOutcome(fatal=True)

    log_event(
        logger,
        EVENT_IPO_SCREENER_STARTED,
        skip_scan=skip_scan,
        skip_download=skip_download,
        skip_enrich=skip_enrich,
        skip_score=skip_score,
        extract=extract,
        force_extract=force_extract,
    )

    filings: IpoFilingJobOutcome | None = None
    if not skip_scan:
        filings = filings_runner(
            to_date=to_date, session_factory=session_factory, output=out
        )

    issues = issue_lister(session_factory=session_factory)
    if issue_ids:
        # An explicitly named set is an operator decision and wins outright, so
        # a finished issue can still be re-downloaded, re-extracted, or
        # re-scored on purpose. Without this the documented
        # ``--force-extract --issue-id N`` workflow could never reach one.
        wanted = set(issue_ids)
        issues = [issue for issue in issues if issue.id in wanted]
    else:
        # Filter once, here, so every stage below inherits it: downloads,
        # enrichment, extraction, and scoring all skip finished issues
        # together rather than each stage deciding for itself.
        issues = [
            issue for issue in issues if issue.status in UPCOMING_ISSUE_STATUSES
        ]

    downloads_attempted = 0
    downloads_failed = 0
    if not skip_download:
        for issue in issues:
            for document in document_lister(issue.id, session_factory=session_factory):
                if document.document_type not in {"drhp", "rhp"}:
                    continue
                if document.parse_status not in (
                    IpoDocumentParseStatus.NOT_DOWNLOADED,
                    IpoDocumentParseStatus.DOWNLOAD_FAILED,
                ):
                    continue
                downloads_attempted += 1
                # One document's failure must not stop the sibling downloads.
                try:
                    document_downloader(
                        issue.id, document.id, session_factory=session_factory
                    )
                except Exception as exc:  # noqa: BLE001 - per-document isolation
                    downloads_failed += 1
                    print(
                        f"[ipo-screener] download_failed issue_id={issue.id} "
                        f"document_id={document.id} error_type={type(exc).__name__}",
                        file=out,
                        flush=True,
                    )

    enrichment_collected = 0
    enrichment_failed = 0
    enrichment_skipped_no_key = False
    if not skip_enrich:
        for issue in issues:
            # No status check here: ``issues`` was already narrowed to upcoming
            # offers above. Re-checking would also override an explicitly named
            # issue, which is the one case an operator has said they want.
            # One issue's search failure must not stop the sibling batches.
            try:
                enrichment = enricher(
                    issue.id,
                    company_name=issue.company_name,
                    price_band_high=issue.price_band_high,
                    session_factory=session_factory,
                )
            except Exception as exc:  # noqa: BLE001 - per-issue isolation
                enrichment_failed += 1
                print(
                    f"[ipo-screener] enrichment_failed issue_id={issue.id} "
                    f"error_type={type(exc).__name__}",
                    file=out,
                    flush=True,
                )
                continue
            if enrichment.skipped_no_key:
                # The very first skip proves the key is absent for the whole
                # run; stop querying instead of logging one skip per issue.
                enrichment_skipped_no_key = True
                print(
                    "[ipo-screener] enrichment=skipped_no_key "
                    "(SERPAPI_API_KEY is not configured; continuing without "
                    "web signals)",
                    file=out,
                    flush=True,
                )
                break
            enrichment_collected += len(enrichment.signals)
            if enrichment.error_type is not None:
                enrichment_failed += 1
            if getattr(enrichment, "quota_exhausted", False):
                # Like the missing-key case above, this is a whole-run
                # condition rather than a per-issue one: the plan is spent, so
                # every remaining issue would fail the same way. Stopping turns
                # ~20 minutes of identical warnings into one actionable line.
                print(
                    "[ipo-screener] enrichment=quota_exhausted "
                    "(the SerpAPI plan has no searches left; continuing "
                    "without web signals)",
                    file=out,
                    flush=True,
                )
                break

    proposals_created = 0
    proposals_skipped = 0
    proposals_failed = 0
    if extract:
        for issue in issues:
            for document in document_lister(issue.id, session_factory=session_factory):
                if document.document_type not in {"drhp", "rhp"}:
                    continue
                if (
                    document.parse_status is not IpoDocumentParseStatus.PENDING
                    or not document.content_sha256
                ):
                    continue
                result = extractor(
                    issue.id,
                    document.id,
                    force_extract=force_extract,
                    session_factory=session_factory,
                )
                if isinstance(result, IpoExtractionErrorReceipt):
                    if result.code in {
                        "pending_proposal_exists",
                        "unchanged_extraction_history",
                        "identical_proposal",
                    }:
                        proposals_skipped += 1
                    else:
                        proposals_failed += 1
                        print(
                            f"[ipo-screener] extraction_failed issue_id={issue.id} "
                            f"document_id={document.id} code={result.code} "
                            f"error_type={result.error_type}",
                            file=out,
                            flush=True,
                        )
                else:
                    proposals_created += 1
                    print(
                        f"[ipo-screener] proposal_created issue_id={issue.id} "
                        f"document_id={document.id} proposal_id={result.id} "
                        f"confidence={result.confidence.value}",
                        file=out,
                        flush=True,
                    )

    issue_outcomes: list[IpoScreenerIssueOutcome] = []
    for issue in issues if not skip_score else ():
        # One issue's scoring failure must not stop the sibling issues.
        try:
            outcome = _issue_outcome_from_rescore(
                rescorer(issue.id, session_factory=session_factory)
            )
        except Exception as exc:  # noqa: BLE001 - per-issue isolation
            outcome = IpoScreenerIssueOutcome(
                issue_id=issue.id,
                company_name=issue.company_name,
                status="failed",
                error_type=type(exc).__name__,
            )
        issue_outcomes.append(outcome)
        _print_issue(out, outcome)

    result = IpoScreenerJobOutcome(
        filings=filings,
        downloads_attempted=downloads_attempted,
        downloads_failed=downloads_failed,
        enrichment_collected=enrichment_collected,
        enrichment_failed=enrichment_failed,
        enrichment_skipped_no_key=enrichment_skipped_no_key,
        proposals_created=proposals_created,
        proposals_skipped=proposals_skipped,
        proposals_failed=proposals_failed,
        issues=tuple(issue_outcomes),
    )
    totals = {
        "evaluated": sum(item.status == "evaluated" for item in issue_outcomes),
        "skipped_unchanged": sum(
            item.status == "skipped_unchanged" for item in issue_outcomes
        ),
        "insufficient": sum(
            item.status == "insufficient_inputs" for item in issue_outcomes
        ),
        "failed": sum(item.status == "failed" for item in issue_outcomes),
    }
    print(
        f"[ipo-screener] totals evaluated={totals['evaluated']} "
        f"skipped_unchanged={totals['skipped_unchanged']} "
        f"insufficient={totals['insufficient']} failed={totals['failed']} "
        f"downloads_failed={downloads_failed} proposals={proposals_created} "
        f"exit_code={result.exit_code}",
        file=out,
        flush=True,
    )
    log_event(
        logger,
        EVENT_IPO_SCREENER_COMPLETED,
        evaluated=totals["evaluated"],
        skipped_unchanged=totals["skipped_unchanged"],
        insufficient=totals["insufficient"],
        failed=totals["failed"],
        downloads_attempted=downloads_attempted,
        downloads_failed=downloads_failed,
        enrichment_collected=enrichment_collected,
        enrichment_failed=enrichment_failed,
        enrichment_skipped_no_key=enrichment_skipped_no_key,
        proposals_created=proposals_created,
        proposals_failed=proposals_failed,
        exit_code=result.exit_code,
    )
    return result


def _parse_iso_date(value: str) -> dt.date:
    """Convert one CLI YYYY-MM-DD value into a date with argparse-safe errors."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date YYYY-MM-DD") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    job_runner: Callable[..., IpoScreenerJobOutcome] = run_ipo_screener,
) -> int:
    """Parse command options, configure logs, and return the job's process code.

    Dependency injection keeps argument parsing testable without SEBI,
    SerpAPI, the Claude SDK, or a database; the production module entry point
    supplies the real runner.

    Beginner note:
        ``--extract`` is opt-in because it can spend model credit.
        ``--force-extract`` broadens only reviewed-history processing; pending
        and semantically identical proposals remain protected by repository
        and database idempotency rules.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Run the IPO screener end to end: inventory SEBI filings, download "
            "prospectuses, collect optional web enrichment, optionally draft AI "
            "extraction proposals, and re-score every issue."
        )
    )
    parser.add_argument("--skip-scan", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument(
        "--extract",
        action="store_true",
        help=(
            "Also draft AI extraction proposals for cached documents without "
            "one (spends Claude plan credit; off by default)."
        ),
    )
    parser.add_argument(
        "--force-extract",
        action="store_true",
        help=(
            "Re-run reviewed extraction history, while still skipping pending "
            "or semantically identical proposals. Implies --extract."
        ),
    )
    parser.add_argument(
        "--issue-id",
        type=int,
        action="append",
        dest="issue_ids",
        default=None,
        help="Limit downloads/enrichment/extraction/scoring to this issue id "
        "(repeatable).",
    )
    parser.add_argument("--to-date", type=_parse_iso_date, default=None)
    args = parser.parse_args(argv)

    configure_logging()
    outcome = job_runner(
        skip_scan=args.skip_scan,
        skip_download=args.skip_download,
        skip_enrich=args.skip_enrich,
        extract=args.extract or args.force_extract,
        force_extract=args.force_extract,
        issue_ids=args.issue_ids,
        to_date=args.to_date,
    )
    return int(outcome.exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests
    raise SystemExit(main())
