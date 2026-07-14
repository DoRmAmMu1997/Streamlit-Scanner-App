"""IPO-008 screener-orchestration job tests.

Beginner note:
Every collaborator is injected as a fake, so these tests pin the *contract*
of the command: which stages run under which flags, how one unit's failure
stays isolated while still driving the exit code, and the exact grep-friendly
summary grammar operators and schedulers rely on.
"""

from __future__ import annotations

import io
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from backend.ipo.agents.financial_extractor import IpoExtractionErrorReceipt
from backend.ipo.models import Confidence, IpoDocumentParseStatus, IpoStatus
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    INSUFFICIENT_VERIFIED_DATA,
    SKIP,
)
from backend.ipo.scoring.service import IpoRescoreOutcome
from backend.jobs.run_ipo_screener import (
    IpoScreenerJobOutcome,
    main,
    run_ipo_screener,
)
from backend.jobs.scan_ipo_filings import IpoFilingJobOutcome


def _issue(issue_id: int, company: str, status: IpoStatus = IpoStatus.OPEN) -> Any:
    """Build one detached-issue stand-in with the fields the job reads."""
    return SimpleNamespace(
        id=issue_id,
        company_name=company,
        status=status,
        price_band_high=Decimal("100"),
    )


def _document(
    document_id: int,
    *,
    parse_status: IpoDocumentParseStatus,
    document_type: str = "rhp",
    content_sha256: str | None = "a" * 64,
) -> Any:
    """Build one detached-document stand-in with the fields the job reads."""
    return SimpleNamespace(
        id=document_id,
        document_type=document_type,
        parse_status=parse_status,
        content_sha256=content_sha256,
    )


def _flag(name: str, status: str) -> Any:
    """Build one caution-flag stand-in exposing name and status.value."""
    return SimpleNamespace(name=name, status=SimpleNamespace(value=status))


def _evaluation(
    *,
    score: str,
    recommendation: str,
    recommendation_type: str,
    confidence: Confidence = Confidence.HIGH,
    flags: tuple[Any, ...] = (),
    missing: tuple[str, ...] = (),
) -> Any:
    """Build one evaluation stand-in shaped like IpoEvaluationRecord.result."""
    return SimpleNamespace(
        result=SimpleNamespace(
            score=Decimal(score),
            recommendation=SimpleNamespace(value=recommendation),
            recommendation_type=recommendation_type,
            confidence=confidence,
            caution_flags=flags,
            missing_data=missing,
        )
    )


def _rescore(issue: Any, status: str, evaluation: Any = None, **kwargs: Any) -> IpoRescoreOutcome:
    """Build one scoring-service outcome for the injected fake rescorer."""
    return IpoRescoreOutcome(
        issue_id=issue.id,
        company_name=issue.company_name,
        status=status,  # type: ignore[arg-type]
        evaluation=evaluation,
        **kwargs,
    )


def _quiet_filings(**_kwargs: Any) -> IpoFilingJobOutcome:
    """Stand-in filings run that succeeded with nothing to report."""
    return IpoFilingJobOutcome()


def test_happy_path_prints_verdict_lines_totals_and_exits_zero() -> None:
    """One evaluated, one insufficient issue produce the documented summary."""
    issues = [_issue(1, "Acme Ltd"), _issue(2, "Beta Ltd")]
    outcomes = {
        1: _rescore(
            issues[0],
            "evaluated",
            _evaluation(
                score="81.25",
                recommendation="Recommended",
                recommendation_type=APPLY_AND_HOLD,
                flags=(_flag("very_expensive_valuation", "not_triggered"),),
            ),
        ),
        2: _rescore(issues[1], "insufficient_inputs", missing=("manual_extraction",)),
    }
    out = io.StringIO()

    result = run_ipo_screener(
        ensure_schema=lambda: True,
        filings_runner=_quiet_filings,
        issue_lister=lambda **_kwargs: issues,
        document_lister=lambda *_args, **_kwargs: [],
        enricher=lambda issue_id, **_kwargs: SimpleNamespace(
            skipped_no_key=False, signals=(1, 2), error_type=None
        ),
        rescorer=lambda issue_id, **_kwargs: outcomes[issue_id],
        session_factory=object,
        output=out,
    )

    text = out.getvalue()
    assert (
        "[ipo-screener] recommended issue_id=1 score=81.25 type=high_conviction "
        "confidence=high company=Acme Ltd" in text
    )
    assert (
        "[ipo-screener] insufficient_data issue_id=2 missing=manual_extraction "
        "company=Beta Ltd" in text
    )
    assert "totals evaluated=1 skipped_unchanged=0 insufficient=1 failed=0" in text
    assert result.enrichment_collected == 4
    assert result.exit_code == 0


def test_flags_and_insufficient_verdicts_render_their_own_grammar() -> None:
    """Triggered flags and insufficient-data verdicts are visible at a glance."""
    issues = [_issue(1, "Flagged Ltd"), _issue(2, "DataGap Ltd")]
    outcomes = {
        1: _rescore(
            issues[0],
            "evaluated",
            _evaluation(
                score="44.00",
                recommendation="Not Recommended",
                recommendation_type=SKIP,
                flags=(_flag("very_expensive_valuation", "triggered"),),
            ),
        ),
        2: _rescore(
            issues[1],
            "evaluated",
            _evaluation(
                score="70.00",
                recommendation="Not Recommended",
                recommendation_type=INSUFFICIENT_VERIFIED_DATA,
                confidence=Confidence.LOW,
                missing=("valuation",),
            ),
        ),
    }
    out = io.StringIO()

    result = run_ipo_screener(
        skip_scan=True,
        skip_download=True,
        skip_enrich=True,
        ensure_schema=lambda: True,
        issue_lister=lambda **_kwargs: issues,
        document_lister=lambda *_args, **_kwargs: [],
        rescorer=lambda issue_id, **_kwargs: outcomes[issue_id],
        session_factory=object,
        output=out,
    )

    text = out.getvalue()
    assert "not_recommended issue_id=1" in text
    assert "flags=very_expensive_valuation" in text
    assert "insufficient_data issue_id=2 missing=valuation" in text
    assert result.exit_code == 0


def test_skip_flags_gate_their_stages_and_extract_defaults_off() -> None:
    """--skip-* suppress stages; AI extraction never runs without --extract."""
    calls: dict[str, int] = {"filings": 0, "download": 0, "enrich": 0, "extract": 0}

    def _count(name: str) -> Any:
        """Build one counting fake for the named stage."""

        def _fake(*_args: Any, **_kwargs: Any) -> Any:
            """Fail loudly if a gated stage is invoked despite its skip flag."""
            calls[name] += 1
            raise AssertionError(f"stage {name} must not run")

        return _fake

    out = io.StringIO()
    result = run_ipo_screener(
        skip_scan=True,
        skip_download=True,
        skip_enrich=True,
        extract=False,
        ensure_schema=lambda: True,
        filings_runner=_count("filings"),
        issue_lister=lambda **_kwargs: [_issue(1, "Acme Ltd")],
        document_lister=lambda *_args, **_kwargs: [
            _document(5, parse_status=IpoDocumentParseStatus.PENDING)
        ],
        document_downloader=_count("download"),
        enricher=_count("enrich"),
        extractor=_count("extract"),
        rescorer=lambda issue_id, **_kwargs: _rescore(
            _issue(1, "Acme Ltd"), "insufficient_inputs", missing=("manual_extraction",)
        ),
        session_factory=object,
        output=out,
    )

    assert calls == {"filings": 0, "download": 0, "enrich": 0, "extract": 0}
    assert result.exit_code == 0


def test_extract_flag_targets_cached_documents_and_counts_outcomes() -> None:
    """--extract drafts proposals for cached PDFs; duplicates count as skips."""
    issue = _issue(1, "Acme Ltd")
    documents = [
        _document(5, parse_status=IpoDocumentParseStatus.PENDING),
        _document(6, parse_status=IpoDocumentParseStatus.NOT_DOWNLOADED),
        _document(7, parse_status=IpoDocumentParseStatus.PENDING),
    ]
    results = {
        5: SimpleNamespace(id=11, confidence=Confidence.HIGH),
        7: IpoExtractionErrorReceipt(
            issue_id=1, document_id=7, error_type="IpoExtractionError",
            code="pending_proposal_exists",
        ),
    }
    extracted: list[int] = []

    def _extractor(_issue_id: int, document_id: int, **_kwargs: Any) -> Any:
        """Record which documents were sent to the agent."""
        extracted.append(document_id)
        return results[document_id]

    out = io.StringIO()
    result = run_ipo_screener(
        skip_scan=True,
        skip_download=True,
        skip_enrich=True,
        extract=True,
        ensure_schema=lambda: True,
        issue_lister=lambda **_kwargs: [issue],
        document_lister=lambda *_args, **_kwargs: documents,
        extractor=_extractor,
        rescorer=lambda issue_id, **_kwargs: _rescore(
            issue, "insufficient_inputs", missing=("manual_extraction",)
        ),
        session_factory=object,
        output=out,
    )

    assert extracted == [5, 7]  # only verified cached PDFs reach the agent
    assert result.proposals_created == 1
    assert result.proposals_skipped == 1
    assert result.proposals_failed == 0
    assert "proposal_created issue_id=1 document_id=5 proposal_id=11" in out.getvalue()
    assert result.exit_code == 0


def test_failures_stay_isolated_but_drive_the_exit_code() -> None:
    """A download error and a scoring crash never stop the sibling issues."""
    issues = [_issue(1, "Acme Ltd"), _issue(2, "Beta Ltd")]
    rescored: list[int] = []

    def _rescorer(issue_id: int, **_kwargs: Any) -> IpoRescoreOutcome:
        """Crash for the first issue and succeed for the second."""
        rescored.append(issue_id)
        if issue_id == 1:
            raise RuntimeError("scoring exploded")
        return _rescore(issues[1], "skipped_unchanged", _evaluation(
            score="70.00",
            recommendation="Recommended",
            recommendation_type=APPLY_AND_HOLD,
        ))

    def _downloader(*_args: Any, **_kwargs: Any) -> None:
        """Fail every download attempt."""
        raise TimeoutError("network down")

    out = io.StringIO()
    result = run_ipo_screener(
        skip_scan=True,
        skip_enrich=True,
        ensure_schema=lambda: True,
        issue_lister=lambda **_kwargs: issues,
        document_lister=lambda *_args, **_kwargs: [
            _document(5, parse_status=IpoDocumentParseStatus.NOT_DOWNLOADED)
        ],
        document_downloader=_downloader,
        rescorer=_rescorer,
        session_factory=object,
        output=out,
    )

    text = out.getvalue()
    assert rescored == [1, 2]
    assert result.downloads_failed == 2
    assert "download_failed issue_id=1 document_id=5 error_type=TimeoutError" in text
    assert "[ipo-screener] failed issue_id=1 error_type=RuntimeError" in text
    assert "unchanged=true" in text
    assert result.exit_code == 1


def test_missing_serpapi_key_is_a_graceful_skip_not_a_failure() -> None:
    """The first no-key outcome stops further queries and stays exit 0."""
    issues = [_issue(1, "Acme Ltd"), _issue(2, "Beta Ltd")]
    enrich_calls: list[int] = []

    def _enricher(issue_id: int, **_kwargs: Any) -> Any:
        """Report the missing key exactly like the real collector."""
        enrich_calls.append(issue_id)
        return SimpleNamespace(skipped_no_key=True, signals=(), error_type=None)

    out = io.StringIO()
    result = run_ipo_screener(
        skip_scan=True,
        skip_download=True,
        ensure_schema=lambda: True,
        issue_lister=lambda **_kwargs: issues,
        document_lister=lambda *_args, **_kwargs: [],
        enricher=_enricher,
        rescorer=lambda issue_id, **_kwargs: _rescore(
            next(issue for issue in issues if issue.id == issue_id),
            "insufficient_inputs",
            missing=("manual_extraction",),
        ),
        session_factory=object,
        output=out,
    )

    assert enrich_calls == [1]  # one probe proves the key is absent
    assert result.enrichment_skipped_no_key is True
    assert "enrichment=skipped_no_key" in out.getvalue()
    assert result.exit_code == 0


def test_fatal_schema_bootstrap_prints_and_exits_one() -> None:
    """A dead database aborts before any stage with the fatal grammar."""
    out = io.StringIO()

    result = run_ipo_screener(ensure_schema=lambda: False, output=out)

    assert result.fatal is True
    assert result.exit_code == 1
    assert "[ipo-screener] FAILED error_type=RuntimeError" in out.getvalue()


def test_issue_id_filter_narrows_every_stage() -> None:
    """--issue-id limits scoring to the named issues only."""
    issues = [_issue(1, "Acme Ltd"), _issue(2, "Beta Ltd")]
    rescored: list[int] = []

    out = io.StringIO()
    run_ipo_screener(
        skip_scan=True,
        skip_download=True,
        skip_enrich=True,
        issue_ids=[2],
        ensure_schema=lambda: True,
        issue_lister=lambda **_kwargs: issues,
        document_lister=lambda *_args, **_kwargs: [],
        rescorer=lambda issue_id, **_kwargs: (
            rescored.append(issue_id)  # type: ignore[func-returns-value]
            or _rescore(issues[1], "insufficient_inputs", missing=("manual_extraction",))
        ),
        session_factory=object,
        output=out,
    )

    assert rescored == [2]


def test_main_wires_cli_flags_into_the_runner() -> None:
    """The CLI surface maps one-to-one onto the runner's keyword options."""
    received: dict[str, Any] = {}

    def _runner(**kwargs: Any) -> IpoScreenerJobOutcome:
        """Capture the parsed options and succeed."""
        received.update(kwargs)
        return IpoScreenerJobOutcome()

    code = main(
        [
            "--skip-scan",
            "--skip-enrich",
            "--extract",
            "--issue-id",
            "7",
            "--issue-id",
            "9",
            "--to-date",
            "2026-07-13",
        ],
        job_runner=_runner,
    )

    assert code == 0
    assert received["skip_scan"] is True
    assert received["skip_download"] is False
    assert received["skip_enrich"] is True
    assert received["extract"] is True
    assert received["issue_ids"] == [7, 9]
    assert str(received["to_date"]) == "2026-07-13"
