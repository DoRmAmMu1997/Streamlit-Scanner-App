"""Deciding how to repair a dirty daily OHLCV frame (DATA-002).

Beginner note:
``candles.py`` answers *"is this candle data broken?"*. This module answers the
next question — *"what would fix it?"* — and nothing more. It is a **planner**:
it reads a frame plus its ``CandleQualityReport`` and returns an ordered list of
``RepairAction``s. It never touches the disk, never calls Dhan, and never
modifies the caller's DataFrame. The actual doing lives in
``backend/data_quality/cache_repair.py``.

Splitting the decision from the doing is what makes every rule below testable
with a three-row DataFrame instead of a live broker connection.

The rules exist to serve one non-negotiable principle: **never invent a price.**
There is no interpolation here, no clamping, no swapping a high and a low to
make a bar "valid", and no split adjustment. Every repair is one of exactly
three honest moves:

1. **Remove redundancy** — drop rows that carry no information (byte-identical
   duplicates, unparseable dates).
2. **Ask the vendor again** — when the cached bytes are untrustworthy, DhanHQ is
   the authority, so re-download the affected window.
3. **Drop what cannot be trusted** — and only after step 2 has been tried, so a
   day is never discarded while a good copy of it might still be available.

Anything that could plausibly be *real* vendor data — a 50% overnight move that
may be an unadjusted split, a multi-week gap in an illiquid small cap — is left
exactly as it is and reported as ``NO_ACTION_VENDOR_DATA``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, cast

import pandas as pd

from backend.data_quality.candles import (
    DEFAULT_REQUIRED_COLUMNS,
    CandleQualityReport,
)

# ---------------------------------------------------------------------------
# Stable action codes
# ---------------------------------------------------------------------------
# These strings end up in logs, the persisted repair receipt, and the Admin
# health table, so they are treated exactly like the DATA-001 finding codes:
# stable, machine-readable, and never reworded casually.

#: Drop rows that are byte-identical to another row for the same date. Removing
#: them costs zero information — two identical bars for one day cannot both be
#: real observations.
ACTION_DEDUPE_EXACT_ROWS = "DEDUPE_EXACT_ROWS"

#: Last-resort local resolution of duplicates that disagree on **volume alone**:
#: keep the bar with the greatest volume. Opt-in only, and deliberately never
#: applied to duplicates whose prices differ — see ``apply_frame_actions``.
ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME = "DEDUPE_CONFLICTING_KEEP_MAX_VOLUME"

#: Drop rows whose date cannot be parsed at all. Such a row cannot be placed on
#: the time axis, so it can never contribute to indicator math.
ACTION_DROP_UNPARSEABLE_DATES = "DROP_UNPARSEABLE_DATES"

#: Put the frame back in ascending date order. Always safe, never loses a row.
ACTION_SORT_BY_TIMESTAMP = "SORT_BY_TIMESTAMP"

#: Drop bars that are structurally impossible (high < low, open/close outside the
#: day's range, negative volume, NaN/inf values). Only ever planned *after* a
#: refetch, so the vendor gets the chance to supply a good copy first.
ACTION_DROP_IMPOSSIBLE_BARS = "DROP_IMPOSSIBLE_BARS"

#: Re-download a bounded date window from Dhan and merge it over the cache.
ACTION_REFETCH_WINDOW = "REFETCH_WINDOW"

#: Re-download the entire configured history window. Reserved for files whose
#: structure — not just a row or two — cannot be trusted.
ACTION_REFETCH_FULL = "REFETCH_FULL"

#: Recorded when a finding is deliberately left alone because it may well be
#: genuine vendor data. Carries a reason so the receipt explains the inaction.
ACTION_NO_ACTION_VENDOR_DATA = "NO_ACTION_VENDOR_DATA"

#: Actions ``apply_frame_actions`` can carry out on its own (no network needed).
FRAME_ACTION_CODES: frozenset[str] = frozenset(
    {
        ACTION_DEDUPE_EXACT_ROWS,
        ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME,
        ACTION_DROP_UNPARSEABLE_DATES,
        ACTION_SORT_BY_TIMESTAMP,
        ACTION_DROP_IMPOSSIBLE_BARS,
    }
)

#: Actions that require a Dhan round-trip.
REFETCH_ACTION_CODES: frozenset[str] = frozenset(
    {ACTION_REFETCH_WINDOW, ACTION_REFETCH_FULL}
)

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------

#: The "abort rather than mangle" guard. A repair may cost at most this fraction
#: of the symbol's *distinct trading days*. Measuring in days rather than rows is
#: deliberate: de-duplicating 1 238 redundant rows costs zero trading days and
#: must always be allowed, while dropping even a few percent of real days would
#: quietly rewrite a strategy's history and must not be.
MAX_DROPPED_DATE_RATIO = 0.02

#: A handful of discarded days is always allowed regardless of the ratio above.
#: Without this floor the ratio alone would refuse to drop a single impossible
#: bar from a short history (1 day out of 20 is 5%), which is the opposite of
#: what we want: small drops are exactly the repairs worth making.
MAX_DROPPED_DATES_FLOOR = 3

#: How long a symbol that stayed dirty after a full repair attempt is left alone
#: before the next attempt. Without this, a permanently vendor-dirty symbol would
#: re-download its whole history on every single app launch.
REPAIR_RETRY_AFTER_DAYS = 7

# The DATA-001 finding codes whose only honest answer is "the file's structure is
# wrong, download it again".
_STRUCTURAL_FATAL_CODES = frozenset(
    {"EMPTY_FRAME", "MISSING_REQUIRED_COLUMNS", "MISSING_DATE_AXIS"}
)

# The DATA-001 finding codes describing individual bars that cannot be real.
_IMPOSSIBLE_BAR_CODES = frozenset(
    {
        "INVALID_NUMERIC_VALUE",
        "HIGH_BELOW_LOW",
        "OPEN_OUTSIDE_RANGE",
        "CLOSE_OUTSIDE_RANGE",
        "NEGATIVE_VOLUME",
    }
)


@dataclass(frozen=True)
class RepairAction:
    """One step in a repair plan.

    - ``code``: a stable label from the constants above.
    - ``reason``: short human-readable justification for the receipt/logs. Only
      ever mentions symbols, codes, counts, and dates — never prices.
    - ``affected_rows``: how many rows the step expects to touch (0 when not
      row-specific).
    - ``window_start`` / ``window_end``: the inclusive date range to re-download,
      set only on the refetch actions.
    - ``drop_dates``: the specific dates a drop step targets, so the receipt can
      name exactly which trading days were discarded.
    """

    code: str
    reason: str
    affected_rows: int = 0
    window_start: date | None = None
    window_end: date | None = None
    drop_dates: tuple[date, ...] = ()


@dataclass(frozen=True)
class RepairPlan:
    """The ordered set of steps that would clean one symbol's cache file."""

    symbol: str
    actions: tuple[RepairAction, ...]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to do (the frame is already clean)."""
        return not self.actions

    @property
    def needs_refetch(self) -> bool:
        """True when carrying out this plan requires talking to Dhan."""
        return any(action.code in REFETCH_ACTION_CODES for action in self.actions)

    @property
    def wants_full_refetch(self) -> bool:
        """True when the whole history window should be re-downloaded."""
        return any(action.code == ACTION_REFETCH_FULL for action in self.actions)

    @property
    def action_codes(self) -> tuple[str, ...]:
        """Just the codes, in plan order — handy for logs and assertions."""
        return tuple(action.code for action in self.actions)

    def refetch_actions(self) -> tuple[RepairAction, ...]:
        """The steps that need a Dhan round-trip, in plan order."""
        return tuple(
            action for action in self.actions if action.code in REFETCH_ACTION_CODES
        )


def plan_repair(
    df: pd.DataFrame,
    report: CandleQualityReport,
    *,
    today: date,
    history_start: date,
    allow_stale_refetch: bool = True,
) -> RepairPlan:
    """Decide how to repair one symbol's candle frame.

    Args:
        df: the cached candle frame (never modified).
        report: the DATA-001 verdict for that frame.
        today: the newest date worth asking the vendor for.
        history_start: the oldest date the cache is meant to cover; used as the
            lower bound of a full re-download.
        allow_stale_refetch: whether a ``STALE_LATEST_CANDLE`` warning should
            plan a tail re-download. The caller sets this to ``False`` when the
            prefetch has *already* asked Dhan for that tail and got nothing back,
            which turns "stale" into vendor reality rather than a fixable defect.

    Returns:
        A ``RepairPlan``. An empty plan means the frame is clean, or that every
        remaining finding is something we refuse to auto-fix.
    """
    codes = {finding.code for finding in report.findings}
    if not codes:
        return RepairPlan(symbol=report.symbol, actions=())

    # A frame with no rows, no OHLCV columns, or no date axis gives us nothing to
    # reason about locally. Ask the vendor for the whole window and stop — every
    # other rule below needs a parseable date axis to work with.
    if codes & _STRUCTURAL_FATAL_CODES:
        return RepairPlan(
            symbol=report.symbol,
            actions=(_full_refetch(history_start, today, "frame structure is unusable"),),
        )

    parsed_dates = parsed_candle_dates(df)
    valid_dates = parsed_dates.dropna() if parsed_dates is not None else pd.Series(dtype=object)
    if parsed_dates is None or valid_dates.empty:
        # Defensive: the report said the axis exists, but nothing parsed. Treat it
        # exactly like a structural failure rather than guessing at windows.
        return RepairPlan(
            symbol=report.symbol,
            actions=(_full_refetch(history_start, today, "no parseable candle dates"),),
        )

    actions: list[RepairAction] = []
    # Track whether a full re-download is already planned. If it is, narrower
    # window refetches are redundant and would only burn extra Dhan calls.
    full_refetch_planned = False

    # --- unparseable dates -------------------------------------------------
    # Drop the NaT rows, then re-ask the vendor for the span they sat in. We
    # cannot know *which* dates they were (that is the whole problem), so the
    # window conservatively covers the frame's real extent.
    if "INVALID_DATE" in codes:
        invalid_count = int(parsed_dates.isna().sum())
        actions.append(
            RepairAction(
                code=ACTION_DROP_UNPARSEABLE_DATES,
                reason="candle rows carry a date that cannot be parsed",
                affected_rows=invalid_count,
            )
        )
        actions.append(
            RepairAction(
                code=ACTION_REFETCH_WINDOW,
                reason="restore the days lost with the unparseable rows",
                affected_rows=invalid_count,
                window_start=min(valid_dates),
                window_end=today,
            )
        )

    # --- duplicate dates ---------------------------------------------------
    # The two sub-classes need opposite treatment, so inspect the actual rows
    # rather than trusting the finding code alone.
    if "DUPLICATE_DATE" in codes:
        redundant_rows, has_conflict = _duplicate_shape(df, parsed_dates)
        if has_conflict:
            # Two genuinely different bars claim the same day. Nothing local can
            # tell us which is real, and a partial merge risks re-introducing the
            # conflict, so the whole file is re-downloaded.
            actions.append(
                _full_refetch(
                    history_start,
                    today,
                    "conflicting bars share a trading date",
                )
            )
            full_refetch_planned = True
        elif redundant_rows:
            actions.append(
                RepairAction(
                    code=ACTION_DEDUPE_EXACT_ROWS,
                    reason="identical rows repeat the same trading date",
                    affected_rows=redundant_rows,
                )
            )

    # --- structurally impossible bars --------------------------------------
    # Ask Dhan for a clean copy of just those days first; the drop is the
    # fallback that runs only if the refetched bar is still impossible.
    if codes & _IMPOSSIBLE_BAR_CODES:
        bad_dates = _impossible_bar_dates(df, parsed_dates)
        if bad_dates:
            if not full_refetch_planned:
                actions.append(
                    RepairAction(
                        code=ACTION_REFETCH_WINDOW,
                        reason="re-download bars that are structurally impossible",
                        affected_rows=len(bad_dates),
                        window_start=bad_dates[0],
                        window_end=bad_dates[-1],
                    )
                )
            actions.append(
                RepairAction(
                    code=ACTION_DROP_IMPOSSIBLE_BARS,
                    reason="discard bars still impossible after a fresh download",
                    affected_rows=len(bad_dates),
                    drop_dates=bad_dates,
                )
            )

    # --- stale tail --------------------------------------------------------
    # Only worth doing when the caller says the normal top-up did not already
    # cover this; see the ``allow_stale_refetch`` docstring.
    if "STALE_LATEST_CANDLE" in codes and allow_stale_refetch and not full_refetch_planned:
        latest = report.latest_date or max(valid_dates)
        actions.append(
            RepairAction(
                code=ACTION_REFETCH_WINDOW,
                reason="cached history stops short of today",
                affected_rows=1,
                window_start=latest + timedelta(days=1),
                window_end=today,
            )
        )

    # --- calendar gaps -----------------------------------------------------
    # A gap may be genuine (illiquid counter, pre-listing history) or dropped
    # vendor data. Asking costs one bounded request; an empty answer proves the
    # gap is real, which the executor records as NO_ACTION_VENDOR_DATA.
    if "CALENDAR_DATE_GAP" in codes and not full_refetch_planned:
        for gap_start, gap_end in _calendar_gaps(valid_dates):
            actions.append(
                RepairAction(
                    code=ACTION_REFETCH_WINDOW,
                    reason="probe a multi-day hole in the candle history",
                    window_start=gap_start,
                    window_end=gap_end,
                )
            )

    # --- suspicious overnight moves ----------------------------------------
    # Deliberately inert. A >50% overnight move is often a real corporate action
    # in unadjusted vendor data; "fixing" it would fabricate price history.
    if "SUSPICIOUS_OVERNIGHT_PRICE_GAP" in codes:
        actions.append(
            RepairAction(
                code=ACTION_NO_ACTION_VENDOR_DATA,
                reason=(
                    "large overnight move may be a genuine unadjusted corporate "
                    "action; never auto-adjusted"
                ),
            )
        )

    return RepairPlan(symbol=report.symbol, actions=tuple(actions))


def apply_frame_actions(
    df: pd.DataFrame,
    plan: RepairPlan,
    *,
    allow_conflicting_fallback: bool = False,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Carry out the offline steps of ``plan`` and report what actually changed.

    Pure: the input frame is never modified, and the returned frame is always a
    fresh object. Steps run in a fixed order regardless of plan order, because
    later steps assume earlier ones already ran (you cannot detect a duplicate
    date until the unparseable dates are gone).

    ``allow_conflicting_fallback`` opts into the last-resort
    ``DEDUPE_CONFLICTING_KEEP_MAX_VOLUME`` rule. The executor enables it only
    when a re-download is impossible or has already failed to resolve the
    conflict, so the default path always prefers the vendor's answer to a local
    guess. Even then the rule only touches duplicates that disagree on *volume
    alone*; two bars quoting different prices for one day are left untouched,
    because picking between them would stitch together a price series that never
    existed.

    Returns:
        ``(repaired_frame, applied_codes)`` where ``applied_codes`` lists only
        the steps that genuinely changed something — a planned drop that turns
        out to be a no-op (because a refetch already fixed the bar) is not
        reported.
    """
    working = df.copy(deep=True)
    applied: list[str] = []
    planned = set(plan.action_codes)

    if working.empty:
        return working, ()

    if ACTION_DROP_UNPARSEABLE_DATES in planned:
        parsed = parsed_candle_dates(working)
        if parsed is not None and parsed.isna().any():
            working = working.loc[~parsed.isna().to_numpy()].reset_index(drop=True)
            applied.append(ACTION_DROP_UNPARSEABLE_DATES)

    if ACTION_DEDUPE_EXACT_ROWS in planned:
        deduped = _drop_exact_duplicate_rows(working)
        if len(deduped.index) != len(working.index):
            working = deduped
            applied.append(ACTION_DEDUPE_EXACT_ROWS)

    if allow_conflicting_fallback:
        # Note this is driven by the *frame*, not the plan: the plan asked for a
        # re-download, and we only get here when that answer was unavailable or
        # still conflicted.
        resolved = _keep_highest_volume_per_date(working)
        if len(resolved.index) != len(working.index):
            working = resolved
            applied.append(ACTION_DEDUPE_CONFLICTING_KEEP_MAX_VOLUME)

    if ACTION_DROP_IMPOSSIBLE_BARS in planned:
        mask = _impossible_row_mask(working)
        if mask is not None and bool(mask.any()):
            working = working.loc[~mask.to_numpy()].reset_index(drop=True)
            applied.append(ACTION_DROP_IMPOSSIBLE_BARS)

    # Sorting is unconditional: it is always correct, never loses a row, and a
    # frame that is already ordered simply reports nothing.
    sorted_frame = _sorted_by_timestamp(working)
    if sorted_frame is not None:
        working = sorted_frame
        applied.append(ACTION_SORT_BY_TIMESTAMP)

    return working, tuple(applied)


def exceeds_drop_budget(*, dates_before: int, dates_after: int) -> bool:
    """True when a repair would discard too large a share of real trading days.

    The executor calls this before writing anything. Tripping it means the plan
    is treated as a failed repair and the original file is left untouched — a
    known-dirty file an operator can inspect beats a silently gutted one.
    """
    if dates_before <= 0:
        return False
    lost = max(0, dates_before - dates_after)
    if lost <= MAX_DROPPED_DATES_FLOOR:
        return False
    return (lost / dates_before) > MAX_DROPPED_DATE_RATIO


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _full_refetch(history_start: date, today: date, reason: str) -> RepairAction:
    """Build the "download the whole window again" action."""
    return RepairAction(
        code=ACTION_REFETCH_FULL,
        reason=reason,
        window_start=history_start,
        window_end=today,
    )


def parsed_candle_dates(df: pd.DataFrame) -> pd.Series | None:
    """Return each row's calendar date, or None when the frame has no date axis.

    Mirrors ``candles._extract_dates`` so the planner and the validator always
    agree on which column carries the date.
    """
    if "timestamp" in df.columns:
        raw = df["timestamp"]
    elif "date" in df.columns:
        raw = df["date"]
    elif isinstance(df.index, pd.DatetimeIndex):
        raw = pd.Series(df.index, index=df.index)
    else:
        return None
    return pd.to_datetime(raw, errors="coerce").dt.date


def _value_columns(df: pd.DataFrame) -> list[str]:
    """The OHLCV columns actually present, in canonical order."""
    return [column for column in DEFAULT_REQUIRED_COLUMNS if column in df.columns]


def _duplicate_shape(df: pd.DataFrame, parsed_dates: pd.Series) -> tuple[int, bool]:
    """Describe the duplicate-date rows: how many are redundant, any conflicts?

    Returns ``(redundant_row_count, has_conflicting_group)``. A group is
    *redundant* when every row in it agrees on all OHLCV values — those extra
    rows can be dropped for free. A group is *conflicting* when the values
    disagree, which no local rule can settle honestly.
    """
    duplicated = parsed_dates.duplicated(keep=False)
    if not bool(duplicated.any()):
        return 0, False

    columns = _value_columns(df)
    subset = df.loc[duplicated.to_numpy(), columns].copy()
    subset["__date"] = parsed_dates.loc[duplicated.to_numpy()].to_numpy()

    redundant = 0
    has_conflict = False
    for _group_date, group in subset.groupby("__date", sort=False):
        # ``drop_duplicates`` collapses byte-identical rows; whatever remains is
        # a genuinely distinct bar claiming the same day.
        distinct = len(group.drop_duplicates().index)
        if distinct > 1:
            has_conflict = True
        redundant += len(group.index) - distinct
    return redundant, has_conflict


def _is_finite(value: object) -> bool:
    """True only for an ordinary finite number (mirrors ``candles._is_finite``)."""
    try:
        return bool(pd.notna(cast(Any, value)) and math.isfinite(float(cast(Any, value))))
    except (TypeError, ValueError):
        return False


def _impossible_row_mask(df: pd.DataFrame) -> pd.Series | None:
    """Boolean mask of rows that cannot be real candles.

    Covers every DATA-001 fatal *value* check in one pass: non-finite numbers,
    high below low, open/close outside the day's range, and negative volume.
    Returns ``None`` when the frame lacks the columns to judge.
    """
    columns = _value_columns(df)
    if len(columns) < len(DEFAULT_REQUIRED_COLUMNS):
        return None

    numeric = {column: pd.to_numeric(df[column], errors="coerce") for column in columns}
    mask = pd.Series(False, index=df.index)
    for values in numeric.values():
        mask |= ~values.map(_is_finite)

    high, low = numeric["high"], numeric["low"]
    open_, close, volume = numeric["open"], numeric["close"], numeric["volume"]
    # ``fillna(False)`` keeps rows already flagged as non-finite from turning the
    # comparison results into NaN-driven surprises.
    mask |= (high < low).fillna(False)
    mask |= ((open_ < low) | (open_ > high)).fillna(False)
    mask |= ((close < low) | (close > high)).fillna(False)
    mask |= (volume < 0).fillna(False)
    return mask


def _impossible_bar_dates(df: pd.DataFrame, parsed_dates: pd.Series) -> tuple[date, ...]:
    """The sorted, distinct dates carrying an impossible bar."""
    mask = _impossible_row_mask(df)
    if mask is None or not bool(mask.any()):
        return ()
    affected = parsed_dates.loc[mask.to_numpy()].dropna()
    return tuple(sorted(set(affected)))


def _calendar_gaps(valid_dates: pd.Series) -> list[tuple[date, date]]:
    """The inclusive missing windows between consecutive cached trading days.

    Uses the same ``MAX_CALENDAR_GAP_DAYS`` threshold the validator flagged on,
    imported lazily to keep this module's import list honest about what it needs.
    """
    from backend.data_quality.candles import MAX_CALENDAR_GAP_DAYS

    ordered = sorted(set(valid_dates.dropna()))
    gaps: list[tuple[date, date]] = []
    for previous, current in pairwise(ordered):
        if (current - previous).days > MAX_CALENDAR_GAP_DAYS:
            gaps.append((previous + timedelta(days=1), current - timedelta(days=1)))
    return gaps


def _drop_exact_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep one copy of each byte-identical (date, OHLCV) row.

    Deliberately compares on the *date* plus the values rather than the raw
    timestamp, so two bars recorded at different times of the same day still
    count as duplicates of each other.
    """
    parsed = parsed_candle_dates(df)
    if parsed is None:
        return df.reset_index(drop=True)
    key = df[_value_columns(df)].copy()
    key["__date"] = parsed.to_numpy()
    keep = ~key.duplicated(keep="first")
    return df.loc[keep.to_numpy()].reset_index(drop=True)


def _keep_highest_volume_per_date(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse volume-only duplicate dates by keeping the highest-volume bar.

    Rationale: a final end-of-day bar's traded volume can never be lower than a
    partial intraday snapshot of the same session, so when two rows agree on
    every price but disagree on volume, the larger volume is the complete
    observation. This is strictly better than "keep whichever row was appended
    last", which picks the partial bar whenever a late snapshot lands after the
    real one.

    Groups whose *prices* differ are left completely alone. There is no honest
    way to choose between two different price series, and choosing anyway would
    fabricate history — those stay duplicated and keep their fatal finding.
    """
    parsed = parsed_candle_dates(df)
    if parsed is None or "volume" not in df.columns:
        return df.reset_index(drop=True)
    duplicated = parsed.duplicated(keep=False)
    if not bool(duplicated.any()):
        return df.reset_index(drop=True)

    price_columns = [column for column in _value_columns(df) if column != "volume"]
    working = df.assign(
        __date=parsed.to_numpy(),
        __volume=pd.to_numeric(df["volume"], errors="coerce").fillna(-1.0),
    )
    drop_positions: list[int] = []
    for _group_date, group in working.loc[duplicated.to_numpy()].groupby(
        "__date", sort=False
    ):
        # More than one distinct price row means a real disagreement: hands off.
        if len(group[price_columns].drop_duplicates().index) > 1:
            continue
        keeper = group["__volume"].idxmax()
        drop_positions.extend(index for index in group.index if index != keeper)

    if not drop_positions:
        return df.reset_index(drop=True)
    return df.drop(index=drop_positions).reset_index(drop=True)


def _sorted_by_timestamp(df: pd.DataFrame) -> pd.DataFrame | None:
    """Return the frame in ascending date order, or None when already ordered."""
    parsed = parsed_candle_dates(df)
    if parsed is None or parsed.isna().any():
        return None
    values = list(parsed)
    if values == sorted(values):
        return None
    ordered = df.assign(__date=parsed.to_numpy()).sort_values("__date", kind="mergesort")
    return ordered.drop(columns="__date").reset_index(drop=True)
