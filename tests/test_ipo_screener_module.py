"""IPO-011 screener-module tests.

Beginner note:
The screener is a thin adapter: it maps UI toggles onto the existing pipeline
and turns the dashboard snapshot into result rows. These tests therefore pin
the adapter's contract -- registry metadata, toggle wiring, progress
reporting, and above all that every emitted row satisfies the strict result
contract -- while the pipeline itself is faked, because it already has its own
suites.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from backend.ipo.dashboard import IpoDashboardRow, IpoDashboardSnapshot
from backend.ipo.models import IpoStatus
from backend.scanning.result_contract import normalize_screener_row
from screeners import ipo_screener

_UPDATED_AT = dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.UTC)


def _row(**overrides: Any) -> IpoDashboardRow:
    """Build one dashboard row; scenarios override what they exercise."""
    values: dict[str, Any] = {
        "issue_id": 7,
        "company_name": "Example Ltd",
        "issue_status": IpoStatus.OPEN,
        "score": Decimal("81.25"),
        "recommendation": "Recommended",
        "recommendation_type": "Apply confidently and consider holding if allotted",
        "confidence": "high",
        "top_positives": ("business quality (21.25/25)",),
        "top_risks": ("financial growth (5.00/20)",),
        "missing_data": (),
        "triggered_flags": (),
        "reasons": ("Financial growth: strong.",),
        "source_documents": ("https://www.sebi.gov.in/filings/example-rhp",),
        "last_updated": _UPDATED_AT,
        "has_manual_profile": True,
        "pending_proposals": 0,
        "documents_downloaded": 1,
        "documents_total": 1,
    }
    values.update(overrides)
    return IpoDashboardRow(**values)


def _install(monkeypatch, rows: list[IpoDashboardRow], **overrides: Any) -> dict[str, Any]:
    """Fake the pipeline, auto-approval, and snapshot; capture the call args.

    ``rows`` may be a list of row lists, in which case each successive dashboard
    snapshot returns the next entry. That models a run where ingestion makes new
    issues appear partway through.
    """
    captured: dict[str, Any] = {"pipeline_calls": [], "approval_calls": []}

    def _fake_pipeline(**kwargs: Any) -> Any:
        """Record one pipeline invocation and report no failed issues."""
        captured["pipeline_calls"].append(kwargs)
        return SimpleNamespace(issues=())

    snapshots = list(overrides["snapshots"]) if "snapshots" in overrides else [rows]

    def _fake_snapshot(**_kwargs: Any) -> IpoDashboardSnapshot:
        """Return the next staged snapshot, repeating the last one forever."""
        current = snapshots[0] if len(snapshots) == 1 else snapshots.pop(0)
        return IpoDashboardSnapshot(generated_at=_UPDATED_AT, rows=tuple(current))

    approver = overrides.get(
        "approver", lambda **_kwargs: SimpleNamespace(approved=(), disabled=True)
    )

    def _recording_approver(**kwargs: Any) -> Any:
        """Record the scope each auto-approval pass was given."""
        captured["approval_calls"].append(kwargs)
        return approver(**kwargs)

    monkeypatch.setattr(ipo_screener, "build_dashboard_snapshot", _fake_snapshot)
    monkeypatch.setattr(
        ipo_screener, "run_ipo_screener", overrides.get("pipeline", _fake_pipeline)
    )
    monkeypatch.setattr(
        ipo_screener, "auto_approve_ready_proposals", _recording_approver
    )
    return captured


def _ingestion_passes(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ingestion-only pipeline calls (the ones that skip scoring)."""
    return [call for call in captured["pipeline_calls"] if call.get("skip_score")]


def _work_pass(captured: dict[str, Any]) -> dict[str, Any]:
    """Return the pipeline call that does the download/enrich/extract work."""
    return next(
        call for call in captured["pipeline_calls"] if not call.get("skip_score")
    )


def test_registry_metadata_declares_an_event_driven_screener() -> None:
    """The dropdown entry is named as asked and needs no candle stack."""
    metadata = ipo_screener.SCREENER

    assert metadata["name"] == "IPO Screener"
    assert metadata["key"] == "ipo_screener"
    assert metadata["requires_candles"] is False
    # Paid AI work must be opt-in for an analyst-accessible button.
    assert metadata["default_params"]["draft_ai_extractions"] is False


def test_toggles_map_onto_the_pipeline_stages(monkeypatch) -> None:
    """Each checkbox flips exactly one documented pipeline stage."""
    captured = _install(monkeypatch, [_row()])
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {
            "run_ingestion": False,
            "download_documents": True,
            "collect_enrichment": False,
            "draft_ai_extractions": True,
            "only_active_issues": True,
            "max_issues": 0,
        },
    )

    # Ingestion is off, so no ingestion-only pass runs at all.
    assert _ingestion_passes(captured) == []
    call = _work_pass(captured)
    assert call["skip_scan"] is True
    assert call["skip_download"] is False
    assert call["skip_enrich"] is True
    assert call["extract"] is True


def test_ingestion_runs_as_its_own_pass_before_issues_are_selected(
    monkeypatch,
) -> None:
    """Filings must be inventoried before the run decides what to work on."""
    captured = _install(monkeypatch, [_row()])
    scanner = ipo_screener.IpoScreener()

    scanner.run(None, None, {"run_ingestion": True, "max_issues": 0})

    ingestion = _ingestion_passes(captured)
    assert len(ingestion) == 1
    # It inventories only: no downloading, scraping, spending, or scoring.
    assert ingestion[0]["skip_download"] is True
    assert ingestion[0]["skip_enrich"] is True
    assert ingestion[0]["skip_score"] is True
    assert "extract" not in ingestion[0] or ingestion[0]["extract"] is False
    # The working pass then does not repeat the SEBI scan.
    assert _work_pass(captured)["skip_scan"] is True


def test_a_filing_discovered_by_this_run_is_processed_by_this_run(
    monkeypatch,
) -> None:
    """A new filing must not wait for a second button press.

    Beginner note:
        Selecting issues before the SEBI scan would freeze the list to what was
        already known, so anything the scan discovered would be filtered out of
        download, enrichment and scoring -- and would only be picked up if the
        operator pressed Run again. That is the exact failure a one-button
        screener exists to prevent, so the selection is taken from the snapshot
        that exists *after* ingestion.
    """
    known = _row(issue_id=7)
    # An archived issue keeps the active-only filter narrowing, so the screener
    # sends an explicit id list rather than "every issue".
    archived = _row(issue_id=8, issue_status=IpoStatus.LISTED)
    discovered = _row(issue_id=42, company_name="Newly Filed Ltd")
    before = [known, archived]
    after = [known, archived, discovered]
    captured = _install(
        monkeypatch,
        before,
        # Before ingestion only issues 7 and 8 exist; the scan reveals 42.
        snapshots=[after, after],
    )
    scanner = ipo_screener.IpoScreener()

    frame = scanner.run(
        None, None, {"run_ingestion": True, "only_active_issues": True, "max_issues": 0}
    )

    # Selecting before the scan would have produced [7]; the post-ingestion
    # snapshot must carry the freshly discovered issue into the same run.
    assert _work_pass(captured)["issue_ids"] == [7, 42]
    assert sorted(frame["symbol"]) == ["IPO:42", "IPO:7"]


def test_every_emitted_row_satisfies_the_result_contract(monkeypatch) -> None:
    """Rows must survive the same normalizer the scan service applies.

    Beginner note:
        A row that fails the contract is silently dropped from persistence, so
        asserting the frame is not enough -- this runs the real normalizer over
        each row exactly as ``run_scan`` would.
    """
    rows = [
        _row(),
        # An unscored issue must still produce a valid, self-explaining row.
        _row(
            issue_id=9,
            company_name="Fresh Ltd",
            issue_status=IpoStatus.DRHP_FILED,
            score=None,
            recommendation=None,
            recommendation_type=None,
            confidence=None,
            reasons=(),
            last_updated=None,
            has_manual_profile=False,
            documents_downloaded=0,
            pending_proposals=2,
            missing_data=("valuation",),
        ),
    ]
    _install(monkeypatch, rows)
    scanner = ipo_screener.IpoScreener()

    frame = scanner.run(None, None, {"max_issues": 0, "only_active_issues": False})

    assert len(frame) == 2
    for record in frame.to_dict(orient="records"):
        normalized = normalize_screener_row(
            {str(key): value for key, value in record.items()},
            screener_key="ipo_screener",
        )
        assert normalized["symbol"].startswith("IPO:")
        assert normalized["provenance"]["triggered_rules"]
        assert normalized["provenance"]["indicator_values"]


def test_synthetic_symbol_stays_within_the_database_column(monkeypatch) -> None:
    """A long company name must not leak into the 50-character symbol column."""
    _install(monkeypatch, [_row(company_name="A" * 200)])
    scanner = ipo_screener.IpoScreener()

    frame = scanner.run(None, None, {"max_issues": 0, "only_active_issues": False})

    assert frame.iloc[0]["symbol"] == "IPO:7"
    assert len(frame.iloc[0]["symbol"]) <= 50
    assert frame.iloc[0]["company_name"] == "A" * 200


def test_unscored_issue_reports_why_instead_of_being_dropped(monkeypatch) -> None:
    """An issue awaiting evidence still appears, with an explaining rule."""
    _install(
        monkeypatch,
        [
            _row(
                score=None,
                recommendation=None,
                recommendation_type=None,
                reasons=(),
                has_manual_profile=False,
            )
        ],
    )
    scanner = ipo_screener.IpoScreener()

    frame = scanner.run(None, None, {"max_issues": 0, "only_active_issues": False})

    record = frame.iloc[0]
    assert record["rating"] is None
    assert "awaiting_evidence" in record["provenance"]["triggered_rules"]
    assert record["provenance"]["source"] == "deterministic"


def test_progress_is_reported_per_stage(monkeypatch) -> None:
    """The shared progress widget moves through named pipeline stages."""
    _install(monkeypatch, [_row()])
    seen: list[tuple[int, int, str]] = []
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {
            "max_issues": 0,
            "only_active_issues": False,
            "progress_callback": lambda done, total, label: seen.append(
                (done, total, label)
            ),
        },
    )

    assert [item[0] for item in seen] == [0, 1, 2]
    assert all(item[1] == len(ipo_screener._STAGES) for item in seen)
    assert seen[0][2] == "Running IPO pipeline"


def test_auto_approved_proposals_trigger_one_rescore_pass(monkeypatch) -> None:
    """A conversion inside this run is reflected without a second button press."""
    captured = _install(
        monkeypatch,
        [_row()],
        approver=lambda **_kwargs: SimpleNamespace(approved=(1,), disabled=False),
    )
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {"run_ingestion": False, "max_issues": 0, "only_active_issues": False},
    )

    assert len(captured["pipeline_calls"]) == 2
    rescore = captured["pipeline_calls"][1]
    # The second pass is scoring only: no scraping, downloading, or spending.
    assert rescore["skip_scan"] is True
    assert rescore["skip_download"] is True
    assert rescore["skip_enrich"] is True
    assert "extract" not in rescore or rescore.get("extract") is False


def test_auto_approval_is_scoped_to_the_issues_this_run_selected(
    monkeypatch,
) -> None:
    """A capped run must not convert proposals it will never rescore.

    Beginner note:
        Approval writes evidence and mutates the issue row, so it is a real
        mutation and not a read. An unscoped pass would convert every pending
        proposal in the queue -- including issues excluded by the active-only
        filter or the cap -- and the follow-up scoring pass only covers the
        selected set, leaving those issues approved but stale.
    """
    rows = [_row(issue_id=7), _row(issue_id=8, issue_status=IpoStatus.LISTED)]
    captured = _install(monkeypatch, rows)
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {"run_ingestion": False, "only_active_issues": True, "max_issues": 0},
    )

    assert captured["approval_calls"] == [{"issue_ids": [7]}]


def test_an_unnarrowed_run_lets_auto_approval_see_the_whole_queue(
    monkeypatch,
) -> None:
    """``None`` means "every issue" for both the pipeline and approval."""
    captured = _install(monkeypatch, [_row(issue_id=7)])
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {"run_ingestion": False, "only_active_issues": False, "max_issues": 0},
    )

    assert captured["approval_calls"] == [{"issue_ids": None}]
    assert _work_pass(captured)["issue_ids"] is None


@pytest.mark.parametrize(
    ("only_active", "max_issues", "expected_ids"),
    [
        (True, 0, [7]),
        (False, 0, None),
        (True, 1, [7]),
    ],
)
def test_issue_selection_narrows_the_run(
    monkeypatch, only_active: bool, max_issues: int, expected_ids: list[int] | None
) -> None:
    """Active-only and the cap both narrow which issues the pipeline touches."""
    rows = [_row(), _row(issue_id=8, issue_status=IpoStatus.LISTED)]
    captured = _install(monkeypatch, rows)
    scanner = ipo_screener.IpoScreener()

    scanner.run(
        None,
        None,
        {
            "run_ingestion": False,
            "only_active_issues": only_active,
            "max_issues": max_issues,
        },
    )

    assert _work_pass(captured)["issue_ids"] == expected_ids
