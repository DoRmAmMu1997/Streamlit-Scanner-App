"""Tests for the pure candle-repair planner (DATA-002).

Beginner note:
The planner never touches the disk or the network. It looks at one symbol's
candle frame plus the DATA-001 quality report and decides *what would fix this*
— dedupe these rows, re-download that window, or leave it alone because the
vendor's data is simply like that. Keeping the decision separate from the doing
means every repair rule below is testable with a three-row DataFrame.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from backend.data_quality.candles import validate_candles
from backend.data_quality.repair import (
    ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME,
    ACTION_DEDUPE_EXACT_ROWS,
    ACTION_DROP_IMPOSSIBLE_BARS,
    ACTION_DROP_UNPARSEABLE_DATES,
    ACTION_NO_ACTION_VENDOR_DATA,
    ACTION_REFETCH_FULL,
    ACTION_REFETCH_WINDOW,
    ACTION_SORT_BY_TIMESTAMP,
    MAX_DROPPED_DATE_RATIO,
    apply_frame_actions,
    exceeds_drop_budget,
    plan_repair,
)

TODAY = date(2026, 6, 10)
HISTORY_START = date(2016, 6, 10)


def _valid_candles() -> pd.DataFrame:
    """Three clean consecutive daily bars — the baseline every case mutates."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-08", "2026-06-09", "2026-06-10"]),
            "open": [100.0, 102.0, 103.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 101.0, 102.0],
            "close": [104.0, 103.5, 106.0],
            "volume": [1_000.0, 1_100.0, 1_200.0],
        }
    )


def _plan(frame: pd.DataFrame, *, symbol: str = "TEST", allow_stale_refetch: bool = True):
    """Validate then plan, the way the executor pairs the two calls."""
    report = validate_candles(frame, symbol=symbol, expected_latest_date=TODAY)
    return plan_repair(
        frame,
        report,
        today=TODAY,
        history_start=HISTORY_START,
        allow_stale_refetch=allow_stale_refetch,
    )


def _codes(plan) -> list[str]:
    return [action.code for action in plan.actions]


# ---------------------------------------------------------------------------
# Purity — the planner is a decision, never a mutation
# ---------------------------------------------------------------------------


def test_clean_frame_produces_an_empty_plan():
    plan = _plan(_valid_candles())

    assert plan.is_empty
    assert plan.actions == ()
    assert not plan.needs_refetch


def test_planner_never_mutates_the_caller_frame():
    frame = _valid_candles()
    frame.loc[1, "high"] = 1.0  # high < low -> fatal, guarantees a real plan
    before = frame.copy(deep=True)

    plan = _plan(frame)

    assert not plan.is_empty
    pd.testing.assert_frame_equal(frame, before)


def test_apply_frame_actions_never_mutates_the_caller_frame():
    frame = pd.concat([_valid_candles(), _valid_candles().iloc[[0]]], ignore_index=True)
    before = frame.copy(deep=True)

    repaired, applied = apply_frame_actions(frame, _plan(frame))

    assert ACTION_DEDUPE_EXACT_ROWS in applied
    assert len(repaired.index) == 3
    pd.testing.assert_frame_equal(frame, before)


# ---------------------------------------------------------------------------
# Structural defects — only the vendor can answer, so refetch the whole window
# ---------------------------------------------------------------------------


def test_empty_frame_plans_a_full_refetch():
    plan = _plan(pd.DataFrame())

    assert _codes(plan) == [ACTION_REFETCH_FULL]
    assert plan.needs_refetch
    assert plan.actions[0].window_start == HISTORY_START
    assert plan.actions[0].window_end == TODAY


def test_missing_columns_plan_a_full_refetch():
    plan = _plan(_valid_candles().drop(columns=["close"]))

    assert _codes(plan) == [ACTION_REFETCH_FULL]


def test_missing_date_axis_plans_a_full_refetch():
    plan = _plan(_valid_candles().drop(columns=["timestamp"]))

    assert _codes(plan) == [ACTION_REFETCH_FULL]


def test_unparseable_dates_are_dropped_then_the_hole_is_refetched():
    frame = _valid_candles()
    frame["timestamp"] = frame["timestamp"].astype(object)
    frame.loc[1, "timestamp"] = "not-a-date"

    plan = _plan(frame)

    assert _codes(plan) == [ACTION_DROP_UNPARSEABLE_DATES, ACTION_REFETCH_WINDOW]
    assert plan.actions[0].affected_rows == 1

    repaired, applied = apply_frame_actions(frame, plan)
    assert applied == (ACTION_DROP_UNPARSEABLE_DATES,)
    assert len(repaired.index) == 2


# ---------------------------------------------------------------------------
# Duplicate dates — the two sub-classes need opposite fixes
# ---------------------------------------------------------------------------


def test_exact_duplicate_rows_are_deduped_without_any_refetch():
    frame = pd.concat([_valid_candles(), _valid_candles().iloc[[1]]], ignore_index=True)

    plan = _plan(frame)

    assert _codes(plan) == [ACTION_DEDUPE_EXACT_ROWS]
    assert not plan.needs_refetch
    assert plan.actions[0].affected_rows == 1

    repaired, _applied = apply_frame_actions(frame, plan)
    assert len(repaired.index) == 3
    # Dedupe must never cost a trading day.
    assert set(pd.to_datetime(repaired["timestamp"]).dt.date) == {
        date(2026, 6, 8),
        date(2026, 6, 9),
        date(2026, 6, 10),
    }
    assert validate_candles(repaired, symbol="TEST").findings == ()


def test_conflicting_duplicate_rows_plan_a_full_refetch():
    """Two different bars for one date cannot be resolved locally."""
    # Differ by a plausible amount: a wild difference would also trip the
    # overnight-price-gap warning and stop this test isolating the duplicate rule.
    conflicting = _valid_candles().iloc[[1]].copy()
    conflicting["close"] = 103.9
    conflicting["high"] = 106.5
    frame = pd.concat([_valid_candles(), conflicting], ignore_index=True)

    plan = _plan(frame)

    assert _codes(plan) == [ACTION_REFETCH_FULL]
    assert plan.needs_refetch


def test_conflicting_duplicate_fallback_keeps_the_highest_volume_bar():
    """The ABREL case: same OHLC twice, one a partial intraday snapshot.

    A final end-of-day bar's volume can never be below a partial snapshot's, so
    the greatest-volume row is the real one. Plain ``keep="last"`` would keep the
    partial bar, which is exactly the bug this rule exists to avoid.
    """
    partial = _valid_candles().iloc[[1]].copy()
    partial["volume"] = 5.0  # partial snapshot arrives *after* the full bar
    frame = pd.concat([_valid_candles(), partial], ignore_index=True)

    repaired, applied = apply_frame_actions(
        frame, _plan(frame), allow_conflicting_fallback=True
    )

    assert ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME in applied
    assert len(repaired.index) == 3
    kept = repaired.loc[
        pd.to_datetime(repaired["timestamp"]).dt.date == date(2026, 6, 9), "volume"
    ]
    assert kept.tolist() == [1_100.0]


def test_conflicting_fallback_is_off_by_default():
    """Without the opt-in the executor must go to the vendor, not guess."""
    partial = _valid_candles().iloc[[1]].copy()
    partial["volume"] = 5.0
    frame = pd.concat([_valid_candles(), partial], ignore_index=True)

    _repaired, applied = apply_frame_actions(frame, _plan(frame))

    assert ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME not in applied


# ---------------------------------------------------------------------------
# Impossible bars — ask the vendor first, drop only what survives a refetch
# ---------------------------------------------------------------------------


def test_impossible_bar_plans_a_window_refetch_before_dropping():
    frame = _valid_candles()
    frame.loc[1, "low"] = frame.loc[1, "high"]  # low == high but open sits below

    plan = _plan(frame)

    assert _codes(plan) == [ACTION_REFETCH_WINDOW, ACTION_DROP_IMPOSSIBLE_BARS]
    # The refetch window is bounded by the affected dates, not the whole history.
    assert plan.actions[0].window_start == date(2026, 6, 9)
    assert plan.actions[0].window_end == date(2026, 6, 9)
    assert plan.actions[1].drop_dates == (date(2026, 6, 9),)


def test_dropping_an_impossible_bar_leaves_the_rest_scannable():
    frame = _valid_candles()
    frame.loc[1, "low"] = frame.loc[1, "high"]

    repaired, applied = apply_frame_actions(frame, _plan(frame))

    assert applied == (ACTION_DROP_IMPOSSIBLE_BARS,)
    assert len(repaired.index) == 2
    assert not validate_candles(repaired, symbol="TEST").has_fatal_findings


def test_negative_volume_is_planned_like_any_other_impossible_bar():
    frame = _valid_candles()
    frame.loc[2, "volume"] = -5.0

    plan = _plan(frame)

    assert _codes(plan) == [ACTION_REFETCH_WINDOW, ACTION_DROP_IMPOSSIBLE_BARS]


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_stale_latest_candle_plans_a_tail_refetch_when_allowed():
    frame = _valid_candles()
    frame["timestamp"] = pd.to_datetime(["2026-05-01", "2026-05-04", "2026-05-05"])

    plan = _plan(frame)

    assert ACTION_REFETCH_WINDOW in _codes(plan)
    tail = next(a for a in plan.actions if a.code == ACTION_REFETCH_WINDOW)
    assert tail.window_start == date(2026, 5, 6)  # latest cached date + 1
    assert tail.window_end == TODAY


def test_stale_latest_candle_is_skipped_when_the_top_up_already_succeeded():
    """The prefetch just asked Dhan and got nothing; re-asking burns quota."""
    frame = _valid_candles()
    frame["timestamp"] = pd.to_datetime(["2026-05-01", "2026-05-04", "2026-05-05"])

    plan = _plan(frame, allow_stale_refetch=False)

    assert ACTION_REFETCH_WINDOW not in _codes(plan)


def test_calendar_gap_plans_a_refetch_of_the_missing_window():
    frame = _valid_candles()
    frame["timestamp"] = pd.to_datetime(["2026-05-04", "2026-06-09", "2026-06-10"])

    plan = _plan(frame)

    windows = [
        (a.window_start, a.window_end)
        for a in plan.actions
        if a.code == ACTION_REFETCH_WINDOW
    ]
    assert (date(2026, 5, 5), date(2026, 6, 8)) in windows


def test_suspicious_price_gap_is_never_auto_adjusted():
    """A 50%+ overnight move may be a real unadjusted split. Never rewrite it."""
    frame = _valid_candles()
    frame.loc[2, "open"] = 10.0
    frame.loc[2, "low"] = 9.0
    frame.loc[2, "high"] = 12.0
    frame.loc[2, "close"] = 11.0

    plan = _plan(frame)

    assert ACTION_NO_ACTION_VENDOR_DATA in _codes(plan)
    assert not plan.needs_refetch
    frame_before = frame.copy(deep=True)
    repaired, applied = apply_frame_actions(frame, plan)
    assert applied == ()
    pd.testing.assert_frame_equal(repaired, frame_before)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_drop_budget_is_measured_in_lost_trading_days():
    # Removing duplicate rows costs no dates, so it is always within budget.
    assert not exceeds_drop_budget(dates_before=100, dates_after=100)
    # Losing a single day out of 2 500 is the TVSHLTD case — allowed.
    assert not exceeds_drop_budget(dates_before=2_500, dates_after=2_499)
    # Losing a tenth of the history is never an acceptable "repair".
    assert exceeds_drop_budget(dates_before=100, dates_after=90)
    assert MAX_DROPPED_DATE_RATIO < 0.1


def test_drop_budget_tolerates_an_empty_starting_frame():
    assert not exceeds_drop_budget(dates_before=0, dates_after=0)


def test_unsorted_rows_are_sorted_without_losing_any():
    frame = _valid_candles().iloc[::-1].reset_index(drop=True)
    frame.loc[0, "low"] = frame.loc[0, "high"]  # force a plan to exist

    repaired, applied = apply_frame_actions(frame, _plan(frame))

    assert ACTION_SORT_BY_TIMESTAMP in applied
    timestamps = pd.to_datetime(repaired["timestamp"]).tolist()
    assert timestamps == sorted(timestamps)


def test_apply_frame_actions_reports_only_the_actions_that_changed_something():
    """A planned action that turns out to be a no-op must not be reported."""
    frame = _valid_candles()
    frame.loc[1, "low"] = frame.loc[1, "high"]
    plan = _plan(frame)

    # Drop the offending row up front; the planned drop is now a no-op.
    pre_cleaned = frame.drop(index=[1]).reset_index(drop=True)
    _repaired, applied = apply_frame_actions(pre_cleaned, plan)

    assert ACTION_DROP_IMPOSSIBLE_BARS not in applied
