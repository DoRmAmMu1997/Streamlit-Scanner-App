"""IPO Screener — run the whole IPO pipeline from the normal scan button.

Unlike every other screener in this package, this one does not scan stock
candles. It declares ``requires_candles: False`` so the UI skips the Dhan
credential check, the universe CSV, and the data loader, then runs the exact
same headless pipeline the CLI runs (``backend.jobs.run_ipo_screener``) and
turns the resulting dashboard snapshot into one result row per IPO issue.

Beginner note — why a screener at all:
The technical-analysis and 67-ka-funda agents already established the pattern
of a screener that overrides ``run()`` and does something other than a
per-symbol candle sweep. Reusing that pattern means the IPO pipeline inherits
scan history, the run-status lifecycle, provenance receipts, and the audit
trail for free, instead of growing a second dispatch path that would have to
re-implement all four.

What this module deliberately does NOT do: re-implement any pipeline stage.
Every stage below is the reviewed function the CLI calls, so the button and
the terminal can never drift apart.
"""

from __future__ import annotations

import io
import logging
from decimal import Decimal
from typing import Any, ClassVar, Literal

import pandas as pd

from backend.ipo.agents.auto_approval import auto_approve_ready_proposals
from backend.ipo.dashboard import IpoDashboardRow, build_dashboard_snapshot
from backend.jobs.run_ipo_screener import UPCOMING_ISSUE_STATUSES, run_ipo_screener
from backend.scanner_base import BaseScanner

logger = logging.getLogger(__name__)

# Offers that have not finished yet. A ``closed`` issue (SEBI's final offer
# document is filed after the issue completes) and a ``listed`` one are both
# history, and are skipped when the operator leaves ``only_active_issues`` on.
#
# Imported from the pipeline rather than restated here: the button and the
# terminal must agree on which issues are worth a run, and a second copy of the
# tuple would let them drift apart the first time either side is edited alone.
_UPCOMING_STATUSES = UPCOMING_ISSUE_STATUSES

# The pipeline stages reported through the shared progress callback, in order.
_STAGES = (
    "Running IPO pipeline",
    "Approving verified extractions",
    "Building results",
)


class IpoScreener(BaseScanner):
    """Dispatch the full IPO pipeline and report one row per IPO issue."""

    SCREENER: ClassVar[dict] = {
        "key": "ipo_screener",
        "name": "IPO Screener",
        "description": (
            "Inventory official SEBI filings, cache prospectuses, collect "
            "advisory web signals, optionally draft AI extraction proposals, "
            "and re-score every IPO issue deterministically. Produces one row "
            "per issue instead of one per stock."
        ),
        # Label only. ``requires_candles: False`` means this is never loaded;
        # it still names the run in scan history.
        "universe": "ipo_filings",
        "timeframe": "event-driven",
        "lookback_days": 0,
        "requires_candles": False,
        "default_params": {
            # Each toggle maps onto one run_ipo_screener stage.
            "run_ingestion": True,
            "download_documents": True,
            "collect_enrichment": True,
            # OFF by default: this screener is analyst-accessible and AI
            # extraction spends Claude plan credit. Opting in is per run.
            "draft_ai_extractions": False,
            # Upcoming offers only: skip issues whose IPO is already over.
            # The key is deliberately NOT renamed even though "active" is now
            # the narrower "upcoming" — it is persisted in
            # ``scan_runs.params_json``, so a rename would orphan history.
            "only_active_issues": True,
            # Bounds a Streamlit run, which blocks the tab while it works.
            "max_issues": 25,
        },
        # Keep durable parameter keys stable while making the sidebar speak in
        # domain terms. In particular, ``only_active_issues`` now means the
        # narrower upcoming lifecycle set, not every non-listed record.
        "parameter_labels": {
            "run_ingestion": "Refresh SEBI filings",
            "download_documents": "Download prospectuses",
            "collect_enrichment": "Collect web enrichment",
            "draft_ai_extractions": "Draft AI extraction proposals",
            "only_active_issues": "Only upcoming IPOs",
            "max_issues": "Maximum IPOs per run",
        },
        "parameter_help": {
            "run_ingestion": "Refresh the official SEBI filing inventory before selection.",
            "download_documents": "Cache missing DRHP/RHP prospectuses for selected IPOs.",
            "collect_enrichment": "Use optional, advisory SerpAPI evidence for selected IPOs.",
            "draft_ai_extractions": "Spend AI plan credit to draft human-review proposals.",
            "only_active_issues": "Exclude closed and listed offers; clear to include history.",
            "max_issues": "Limit the selected pipeline/result rows; 0 means no issue cap.",
        },
    }
    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "company_name",
        "issue_status",
        # The evaluation date. It is not `signal_date`, because that column
        # enrols a row in forward-return validation, which is meaningless for
        # an IPO issue -- see the comment in `_result_row`.
        "scored_on",
        "ipo_score",
        "recommendation_type",
        "confidence",
        "top_positives",
        "top_risks",
        "missing_data",
        "triggered_flags",
        "pending_proposals",
        "documents",
        "evaluation_stale",
    ]
    # IPO-012 changes which issue lifecycle states a default run selects. Bump
    # provenance so historical rows do not claim the original IPO-011 contract.
    SCREENER_VERSION = "1.1.0"

    def compute_signal(
        self, symbol: str, candles: pd.DataFrame, params: dict
    ) -> dict | None:
        """Unused: this screener is event-driven, not per-symbol.

        ``BaseScanner`` is abstract and the registry instantiates the class, so
        this must exist. Returning ``None`` mirrors how the AI screeners keep a
        working single-item path they never call.
        """
        return None

    def run(
        self,
        universe_df: pd.DataFrame | None,
        data_loader: Any,
        params: dict,
    ) -> pd.DataFrame:
        """Run the pipeline, then emit one contract-conformant row per issue.

        ``universe_df`` and ``data_loader`` arrive as ``None`` because the
        SCREENER metadata declares no candle requirement; both are accepted and
        ignored so the registry's signature check still passes.
        """
        progress = params.get("progress_callback")
        total = len(_STAGES)

        def report(step: int) -> None:
            """Advance the shared progress widget by pipeline stage."""
            if callable(progress):
                progress(step, total, _STAGES[min(step, total - 1)])

        report(0)
        run_ingestion = bool(params.get("run_ingestion", True))
        if run_ingestion:
            # Ingest first, as its own pass. Selecting issues before the SEBI
            # scan would freeze the list to what was already known, so a filing
            # discovered by this very run would be filtered out of download,
            # enrichment, extraction and scoring -- and would only be processed
            # if the operator pressed the button a second time, which is the
            # exact failure a one-button screener exists to prevent.
            run_ipo_screener(
                skip_download=True,
                skip_enrich=True,
                skip_score=True,
                output=io.StringIO(),
            )

        issue_ids = self._selected_issue_ids(params)
        outcome = run_ipo_screener(
            skip_scan=True,
            skip_download=not bool(params.get("download_documents", True)),
            skip_enrich=not bool(params.get("collect_enrichment", True)),
            extract=bool(params.get("draft_ai_extractions", False)),
            issue_ids=issue_ids,
            output=io.StringIO(),
        )

        report(1)
        # Convert freshly drafted, fully verified proposals into evidence when
        # the operator enabled it, then re-score so the button's own run shows
        # the result rather than making the user press it twice. The scope is
        # this run's own selection: approval writes evidence and mutates the
        # issue row, so a capped run must not convert proposals belonging to
        # issues it never processed and will not rescore.
        approval = auto_approve_ready_proposals(issue_ids=issue_ids)
        if approval.approved:
            run_ipo_screener(
                skip_scan=True,
                skip_download=True,
                skip_enrich=True,
                issue_ids=issue_ids,
                output=io.StringIO(),
            )

        report(2)
        rows = self._result_rows(params, failed_issue_ids=_failed_issue_ids(outcome))
        return self.build_result_frame(
            rows, compute_failure_callback=params.get("compute_failure_callback")
        )

    @staticmethod
    def _apply_selection(rows: list[IpoDashboardRow], params: dict) -> list[IpoDashboardRow]:
        """Apply the operator's active-only and cap choices to a row list.

        One implementation, used both to choose what the pipeline processes and
        to choose what the results table reports. Two copies of this rule would
        let the processed set and the reported set drift apart silently.
        """
        if bool(params.get("only_active_issues", True)):
            rows = [row for row in rows if row.issue_status in _UPCOMING_STATUSES]
        max_issues = int(params.get("max_issues", 0) or 0)
        if max_issues > 0:
            rows = rows[:max_issues]
        return rows

    def _selected_issue_ids(self, params: dict) -> list[int]:
        """Name every issue this run should process, explicitly.

        Beginner note:
            This deliberately never returns "no selection". It used to return
            ``None`` whenever the toggles happened not to narrow anything,
            meaning "let the pipeline decide" -- and the pipeline's own default
            is upcoming-only. So an operator who *unticked* ``only_active_issues``
            to widen the run silently got upcoming-only processing, while the
            results table still reported every row.

            It was worse than a plain bug because it was order-dependent: with a
            cap that happened to bite, an explicit list was sent and finished
            issues inside the cap *were* processed. Whether the toggle worked
            depended on whether the cap bit.

            Sending the list the table will report makes the button's selection
            authoritative in every combination, so the processed set and the
            reported set cannot diverge.
        """
        snapshot = build_dashboard_snapshot()
        return [
            row.issue_id for row in self._apply_selection(list(snapshot.rows), params)
        ]

    def _result_rows(
        self, params: dict, *, failed_issue_ids: set[int]
    ) -> list[dict[str, Any]]:
        """Read the post-run snapshot and shape one row per issue."""
        snapshot = build_dashboard_snapshot()
        rows = self._apply_selection(list(snapshot.rows), params)
        return [
            _result_row(row, scanner=self, failed=row.issue_id in failed_issue_ids)
            for row in rows
        ]


def _failed_issue_ids(outcome: Any) -> set[int]:
    """Collect issue ids whose scoring stage failed during this run."""
    return {
        item.issue_id
        for item in getattr(outcome, "issues", ())
        if getattr(item, "status", None) == "failed"
    }


def _result_row(
    row: IpoDashboardRow, *, scanner: BaseScanner, failed: bool
) -> dict[str, Any]:
    """Turn one dashboard row into a contract-conformant result row.

    Beginner note:
        ``scan_results.symbol`` is a NOT NULL ``String(50)``, so the symbol is
        a short synthetic key rather than the company name (which can exceed
        50 characters and would fail on PostgreSQL). The readable name travels
        in its own column.

        The result contract also requires a provenance receipt with a
        non-empty rule list and at least one scalar indicator, so an unscored
        issue still reports *why* it is unscored instead of being dropped.
    """
    triggered_rules: list[str] = []
    indicator_values: dict[str, Any] = {
        "documents_downloaded": int(row.documents_downloaded),
        "documents_total": int(row.documents_total),
        "pending_proposals": int(row.pending_proposals),
        "has_manual_profile": bool(row.has_manual_profile),
    }

    if failed:
        triggered_rules.append("scoring_failed")
    if row.recommendation_type:
        triggered_rules.append(f"verdict:{row.recommendation_type}")
    triggered_rules.extend(row.triggered_flags)
    if row.missing_data:
        triggered_rules.append("missing_data")
    if not triggered_rules:
        # The contract rejects an empty rule list; say what actually happened.
        triggered_rules.append(
            "awaiting_evidence" if row.score is None else "scored"
        )

    if row.score is not None:
        indicator_values["ipo_score"] = float(row.score)
    for item in row.breakdown:
        contribution = getattr(item, "contribution", None)
        factor = getattr(item, "factor", None)
        if factor is not None and isinstance(contribution, Decimal):
            indicator_values[f"contribution.{factor}"] = float(contribution)

    # AI extraction and advisory web signals can both inform the evidence a
    # verdict rests on, so a scored issue is hybrid rather than purely
    # deterministic; an unscored one has produced no judgement at all.
    source: Literal["deterministic", "ai", "hybrid"] = (
        "hybrid" if row.score is not None else "deterministic"
    )

    return {
        "symbol": f"IPO:{row.issue_id}",
        "rating": row.recommendation,
        # Deliberately null. A forward return is "the price N sessions after
        # the signal date", which an IPO issue has no series for: the VALID-002
        # job selects every result carrying a signal_date, could never resolve
        # the "ipo_filings" universe to instruments, and would therefore requeue
        # these rows as PENDING forever, consuming its batch budget. The
        # evaluation date is still reported, in its own column.
        "signal_date": None,
        "scored_on": row.last_updated.date() if row.last_updated else None,
        "close": row.score,
        "reason": row.reasons[0] if row.reasons else "No evaluation yet.",
        "company_name": row.company_name,
        "issue_status": row.issue_status.value,
        "ipo_score": row.score,
        "recommendation_type": row.recommendation_type,
        "confidence": row.confidence,
        "top_positives": "; ".join(row.top_positives),
        "top_risks": "; ".join((*row.triggered_flags, *row.top_risks)),
        "missing_data": "; ".join(row.missing_data),
        "triggered_flags": "; ".join(row.triggered_flags),
        "pending_proposals": row.pending_proposals,
        "documents": f"{row.documents_downloaded}/{row.documents_total}",
        "evaluation_stale": bool(row.evaluation_stale),
        "provenance": scanner.build_provenance(
            triggered_rules=triggered_rules,
            indicator_values=indicator_values,
            source=source,
            notes="IPO pipeline verdict; see the IPO dashboard for full receipts.",
        ),
    }


_screener = IpoScreener()
SCREENER = _screener.SCREENER
run = _screener.run
