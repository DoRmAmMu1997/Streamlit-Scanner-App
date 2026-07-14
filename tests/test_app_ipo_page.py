"""IPO-007 dashboard page tests.

Beginner note:
The page is a thin renderer over the Streamlit-free builder, so these tests
split the same way the history-page tests do: pure shaping helpers are called
directly, and the renderer is smoke-tested against a fake ``st`` with every
repository-touching seam monkeypatched — proving no query or network call can
hide inside a render.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from decimal import Decimal
from typing import Any

from backend.ipo.dashboard import IpoDashboardRow, IpoDashboardSnapshot
from backend.ipo.models import IpoStatus
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    INSUFFICIENT_VERIFIED_DATA,
    SKIP,
)
from backend.ipo.scoring.service import IpoRescoreOutcome
from ui import ipo_page

_SCORED_AT = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)


def _row(**overrides: Any) -> IpoDashboardRow:
    """Build one display row; scenarios override the fields they exercise."""
    values: dict[str, Any] = {
        "issue_id": 1,
        "company_name": "Example Ltd",
        "issue_status": IpoStatus.OPEN,
        "score": Decimal("81.25"),
        "recommendation": "Recommended",
        "recommendation_type": APPLY_AND_HOLD,
        "confidence": "high",
        "top_positives": ("business quality (21.25/25)",),
        "top_risks": ("financial growth (5.00/20)",),
        "missing_data": ("gmp_sentiment",),
        "triggered_flags": (),
        "reasons": ("Financial growth: strong.",),
        "source_documents": ("https://www.sebi.gov.in/filings/example-rhp",),
        "last_updated": _SCORED_AT,
        "has_manual_profile": True,
        "pending_proposals": 0,
        "documents_downloaded": 1,
        "documents_total": 1,
    }
    values.update(overrides)
    return IpoDashboardRow(**values)


def _snapshot(*rows: IpoDashboardRow) -> IpoDashboardSnapshot:
    """Wrap rows in a snapshot stamped at the fixed test instant."""
    return IpoDashboardSnapshot(generated_at=_SCORED_AT, rows=tuple(rows))


def test_label_map_covers_every_stored_recommendation_type() -> None:
    """The UI wording mapping must stay complete as verdict types evolve."""
    assert set(ipo_page._RECOMMENDATION_TYPE_LABELS) == {
        APPLY_AND_HOLD,
        APPLY_FOR_LISTING_GAINS,
        SKIP,
        INSUFFICIENT_VERIFIED_DATA,
    }
    assert (
        ipo_page._verdict_label(_row(recommendation_type=INSUFFICIENT_VERIFIED_DATA))
        == "Not Recommended - insufficient verified data"
    )
    assert ipo_page._verdict_label(_row(recommendation_type=None)) == "Not scored yet"


def test_verdict_filter_passes_unscored_rows_only_through_all() -> None:
    """Filtering is binary; unscored issues only appear in the All view."""
    scored = _row(issue_id=1)
    rejected = _row(issue_id=2, recommendation="Not Recommended", recommendation_type=SKIP)
    unscored = _row(issue_id=3, score=None, recommendation=None, recommendation_type=None)
    rows = (scored, rejected, unscored)

    assert ipo_page._apply_verdict_filter(rows, "All") == rows
    assert ipo_page._apply_verdict_filter(rows, "Recommended") == (scored,)
    assert ipo_page._apply_verdict_filter(rows, "Not Recommended") == (rejected,)


def test_rows_frame_carries_every_spec_column() -> None:
    """The table shows exactly what the sprint's card contract demands."""
    frame = ipo_page._rows_frame((_row(),))

    assert list(frame.columns) == [
        "Company",
        "Issue status",
        "Score",
        "Recommendation",
        "Confidence",
        "Top positives",
        "Top risks",
        "Missing data",
        "Pending proposals",
        "Documents",
        "Source documents",
        "Last updated",
    ]
    record = frame.iloc[0]
    assert record["Company"] == "Example Ltd"
    assert record["Score"] == "81.25"
    assert record["Recommendation"] == "Recommended - high conviction"
    assert record["Missing data"] == "gmp_sentiment"
    assert record["Documents"] == "1/1"


def test_rows_frame_marks_missing_profile_and_prepends_flags_to_risks() -> None:
    """Evidence gaps and hard flags stay visible in the flat table."""
    frame = ipo_page._rows_frame(
        (
            _row(
                score=None,
                recommendation=None,
                recommendation_type=None,
                confidence=None,
                has_manual_profile=False,
                missing_data=(),
                triggered_flags=("very_expensive_valuation",),
                top_risks=("financial growth (5.00/20)",),
                last_updated=None,
            ),
        )
    )

    record = frame.iloc[0]
    assert record["Missing data"] == "manual extraction"
    assert record["Top risks"].startswith("very_expensive_valuation")
    assert record["Recommendation"] == "Not scored yet"


class _FakeStreamlit:
    """Capture the Streamlit surface the dashboard renderer touches."""

    def __init__(self, *, rescore_clicked: bool = False) -> None:
        """Prepare capture lists and the armed button state."""
        self.rescore_clicked = rescore_clicked
        self.markdowns: list[str] = []
        self.captions: list[str] = []
        self.frames: list[Any] = []
        self.successes: list[str] = []
        self.warnings: list[str] = []
        self.radio_options: tuple[str, ...] | None = None
        self.button_keys: list[str] = []

    def subheader(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept the page heading."""

    def caption(self, text: str, **_kwargs: Any) -> None:
        """Record explanatory copy for the empty-section assertions."""
        self.captions.append(str(text))

    def markdown(self, text: str, **_kwargs: Any) -> None:
        """Record section headings."""
        self.markdowns.append(str(text))

    def dataframe(self, frame: Any, **_kwargs: Any) -> None:
        """Record each rendered section table."""
        self.frames.append(frame)

    def radio(self, _label: str, options: Any, **_kwargs: Any) -> str:
        """Record the filter options and choose the default."""
        self.radio_options = tuple(options)
        return "All"

    def button(self, _label: str, *, key: str, **_kwargs: Any) -> bool:
        """Record that the control rendered and report the armed click."""
        self.button_keys.append(key)
        return self.rescore_clicked

    def success(self, text: str, **_kwargs: Any) -> None:
        """Record the re-score confirmation."""
        self.successes.append(str(text))

    def warning(self, text: str, **_kwargs: Any) -> None:
        """Record hard-caution callouts in breakdowns."""
        self.warnings.append(str(text))

    def expander(self, *_args: Any, **_kwargs: Any) -> Any:
        """Provide the context-manager shape of a real expander."""
        return contextlib.nullcontext()


class _FakeLoader:
    """Stand-in for the cached snapshot loader with a clear() seam."""

    def __init__(self, snapshot: IpoDashboardSnapshot) -> None:
        """Serve one canned snapshot and count cache invalidations."""
        self.snapshot = snapshot
        self.cleared = 0

    def __call__(self) -> IpoDashboardSnapshot:
        """Return the canned snapshot like the cached loader."""
        return self.snapshot

    def clear(self) -> None:
        """Record one cache invalidation."""
        self.cleared += 1


def test_render_shows_all_sections_without_touching_repositories(monkeypatch) -> None:
    """A full render is pure display: sections, filter, and breakdowns only."""
    fake_st = _FakeStreamlit()
    loader = _FakeLoader(
        _snapshot(
            _row(issue_id=1),
            _row(
                issue_id=2,
                issue_status=IpoStatus.DRHP_FILED,
                score=None,
                recommendation=None,
                recommendation_type=None,
                has_manual_profile=False,
            ),
        )
    )
    monkeypatch.setattr(ipo_page, "st", fake_st)
    monkeypatch.setattr(ipo_page, "_load_snapshot", loader)
    monkeypatch.setattr(
        ipo_page,
        "rescore_issue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("render must not score")
        ),
    )

    ipo_page._render_ipo_page(can_rescore=False)

    assert fake_st.button_keys == []  # the re-score control is hidden
    assert fake_st.radio_options == ("All", "Recommended", "Not Recommended")
    section_titles = [text.split(" (")[0].strip("*") for text in fake_st.markdowns]
    for title in (
        "Available filings",
        "Open IPOs",
        "Upcoming IPOs",
        "DRHP watchlist",
        "Recommended IPOs",
        "Not Recommended IPOs",
        "Missing data queue",
    ):
        assert title in section_titles
    assert loader.cleared == 0


def test_rescore_button_runs_the_service_audits_and_refreshes(monkeypatch) -> None:
    """The admin control re-scores every issue and invalidates the cache."""
    fake_st = _FakeStreamlit(rescore_clicked=True)
    loader = _FakeLoader(_snapshot(_row(issue_id=1), _row(issue_id=2)))
    rescored: list[int] = []
    audits: list[dict[str, Any]] = []

    def _rescore(issue_id: int, **_kwargs: Any) -> IpoRescoreOutcome:
        """Record the call and report one skip so counts are visible."""
        rescored.append(issue_id)
        return IpoRescoreOutcome(
            issue_id=issue_id, company_name="Example Ltd", status="skipped_unchanged"
        )

    def _record_audit(**kwargs: Any) -> bool:
        """Capture the audit payload and report success like the real sink."""
        audits.append(kwargs)
        return True

    monkeypatch.setattr(ipo_page, "st", fake_st)
    monkeypatch.setattr(ipo_page, "_load_snapshot", loader)
    monkeypatch.setattr(ipo_page, "rescore_issue", _rescore)
    monkeypatch.setattr(ipo_page, "record_audit_event", _record_audit)

    ipo_page._render_ipo_page(can_rescore=True, user_email="admin@example.com")

    assert rescored == [1, 2]
    assert loader.cleared == 1
    assert audits[0]["user_email"] == "admin@example.com"
    assert audits[0]["metadata"]["skipped_unchanged"] == 2
    assert any("Re-score complete" in message for message in fake_st.successes)


def test_rescore_failures_are_counted_never_raised(monkeypatch) -> None:
    """One broken issue cannot abort the button for the rest."""
    fake_st = _FakeStreamlit(rescore_clicked=True)
    loader = _FakeLoader(_snapshot(_row(issue_id=1), _row(issue_id=2)))

    def _rescore(issue_id: int, **_kwargs: Any) -> IpoRescoreOutcome:
        """Crash for one issue and succeed for the other."""
        if issue_id == 1:
            raise RuntimeError("boom")
        return IpoRescoreOutcome(
            issue_id=issue_id, company_name="Example Ltd", status="evaluated"
        )

    monkeypatch.setattr(ipo_page, "st", fake_st)
    monkeypatch.setattr(ipo_page, "_load_snapshot", loader)
    monkeypatch.setattr(ipo_page, "rescore_issue", _rescore)
    monkeypatch.setattr(ipo_page, "record_audit_event", lambda **_kwargs: True)

    ipo_page._render_ipo_page(can_rescore=True, user_email="admin@example.com")

    assert any("1 failed" in message for message in fake_st.successes)
    assert any("1 evaluated" in message for message in fake_st.successes)
