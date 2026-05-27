from __future__ import annotations

"""Technical indicator helpers for screeners.

How this module is structured (beginner note):
- Every public indicator is a small "dispatcher". It first tries a fast,
  battle-tested library (`talib` or `pandas_ta`); if that library is not
  installed, or its call fails for any reason, the dispatcher falls back to a
  pure-pandas implementation kept in this same file (named `_<name>_fallback`).
- This means the app works the same whether or not the optional libraries are
  installed — the libraries just make the math faster and more standard.

The libraries are imported inside `try/except` blocks so a missing install
never crashes the app at import time.
"""

import numpy as np
import pandas as pd

# Optional accelerated backend #1: TA-Lib (C library + Python bindings).
# Used for the standard indicators: EMA, SMA, Bollinger Bands, Stochastic.
try:
    import talib
except ImportError:  # pragma: no cover - exercised only when TA-Lib is absent
    talib = None

# Optional accelerated backend #2: pandas_ta (pure Python).
# Used for Heikin Ashi and SuperTrend, which TA-Lib does not provide.
try:
    import pandas_ta
except ImportError:  # pragma: no cover - exercised only when pandas_ta is absent
    pandas_ta = None


def _require_columns(frame: pd.DataFrame, required_columns: list[str]) -> None:
    """Raise a clear error if a candle DataFrame is missing required columns."""
    # A missing column would otherwise fail later with a harder-to-understand
    # pandas KeyError. This message tells the screener author exactly what is
    # wrong with the input candle table.
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Input candles are missing required column(s): {', '.join(missing)}")


def prepare_ohlc(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Return a clean OHLC frame sorted from oldest candle to newest candle.

    Screeners can receive data from Dhan, cache, or tests. This helper makes
    sure all indicator functions start with the same predictable shape, and
    is also what `BaseScanner.prepare_candles` calls.
    """
    # If candles is empty we still need it to be a DataFrame the screener
    # can chain `.iloc[-1]` etc. against. Returning a fresh empty frame keeps
    # the boundary tidy.
    if ohlc is None:
        return pd.DataFrame()
    if isinstance(ohlc, pd.DataFrame) and ohlc.empty:
        return pd.DataFrame(columns=list(ohlc.columns))

    _require_columns(ohlc, ["open", "high", "low", "close"])
    # Work on a copy so this helper never changes the caller's original candles.
    # That keeps one screener's indicator preparation from affecting another
    # screener that may reuse the same DataFrame later.
    frame = ohlc.copy()

    if "timestamp" in frame.columns:
        # Sort oldest-to-newest because indicators are sequential: today's value
        # depends on all candles before it. Duplicate timestamps are collapsed so
        # each day contributes only one candle.
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp")

    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            # API/CSV data can arrive as strings. Coercing bad values to NaN and
            # dropping those rows below is safer than letting string math happen.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


# Backwards-compatible alias. Some older modules still import the
# leading-underscore name; new code should use `prepare_ohlc` directly.
_prepare_ohlc = prepare_ohlc


def pivot_lows(low: pd.Series, left: int, right: int) -> pd.Series:
    """Return a boolean Series marking confirmed pivot lows.

    A pivot low is a candle whose `low` is strictly less than:
      - every low in the previous `left` candles, AND
      - every low in the next `right` candles.

    Confirmation requires `right` future candles to exist, so the LAST
    `right` rows of the input are always `False` — that is intentional, it
    prevents acting on a low that tomorrow's bar could invalidate.

    This vectorized implementation runs in O(n) regardless of `left`/`right`,
    replacing the older O(n*left*right) per-candle loop used by the Knoxville
    screener. It is reusable from any divergence-based screener that needs
    pivot detection.
    """
    left = max(1, int(left))
    right = max(1, int(right))
    series = pd.to_numeric(low, errors="coerce")

    # Rolling minimum of the `left` candles BEFORE today. `shift(1)` lines up
    # the window so today's value is compared against its left neighbors only.
    left_min = series.rolling(window=left, min_periods=left).min().shift(1)

    # For the right window we reverse the series, take a rolling min, and
    # reverse back. After `shift(1)` on the reversed view, each row sees the
    # minimum of its `right` future candles in the original ordering.
    reversed_series = series.iloc[::-1]
    right_min = (
        reversed_series.rolling(window=right, min_periods=right).min().shift(1).iloc[::-1]
    )

    # A NaN low cannot be a pivot. A pivot must be strictly less than both
    # the left and the right rolling minimum (so equal lows do not qualify).
    mask = series.notna() & (series < left_min) & (series < right_min)
    return mask.fillna(False).astype(bool)


# ---------------------------------------------------------------------------
# Moving averages (EMA / SMA): TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average: recent candles get more weight.

    Uses `talib.EMA` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            values = talib.EMA(np.asarray(series, dtype="float64"), timeperiod=int(period))
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            # Any library hiccup (bad dtype, etc.) drops to the pandas fallback.
            pass
    return _ema_fallback(series, period)


def _ema_fallback(series: pd.Series, period: int) -> pd.Series:
    """Pure-pandas EMA used when TA-Lib is unavailable."""
    return series.ewm(span=int(period), adjust=False, min_periods=int(period)).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average: every candle in the window gets equal weight.

    Uses `talib.SMA` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            values = talib.SMA(np.asarray(series, dtype="float64"), timeperiod=int(period))
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            pass
    return _sma_fallback(series, period)


def _sma_fallback(series: pd.Series, period: int) -> pd.Series:
    """Pure-pandas SMA used when TA-Lib is unavailable."""
    return series.rolling(window=int(period), min_periods=int(period)).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, a momentum oscillator between 0 and 100.

    Uses `talib.RSI` when TA-Lib is installed, otherwise a pandas fallback.
    """
    period = max(1, int(period))
    if talib is not None:
        try:
            # TA-Lib is the standard library implementation. Convert to a
            # float64 numpy array because TA-Lib expects plain numeric arrays,
            # not pandas objects or strings from CSV/API data.
            values = talib.RSI(np.asarray(series, dtype="float64"), timeperiod=period)
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            # A bad dtype or unexpected library issue should not break the app;
            # the pandas fallback below keeps the screener usable.
            pass
    return _rsi_fallback(series, period)


def _rsi_fallback(series: pd.Series, period: int = 14) -> pd.Series:
    """Pure-pandas RSI used when TA-Lib is unavailable."""
    period = max(1, int(period))
    # `diff()` tells us how much the close changed from the previous candle.
    close_numeric = pd.to_numeric(series, errors="coerce")
    delta = close_numeric.diff()
    # Positive changes are gains; negative changes are losses. The clipping
    # keeps each side separate so average gain and average loss are independent.
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    # Wilder-style RSI uses smoothed averages. ewm(alpha=1/period) gives that
    # smooth rolling behavior without needing a manual loop.
    average_gain = gains.ewm(alpha=1 / int(period), adjust=False, min_periods=int(period)).mean()
    average_loss = losses.ewm(alpha=1 / int(period), adjust=False, min_periods=int(period)).mean()
    relative_strength = average_gain / average_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + relative_strength))


def momentum(series: pd.Series, period: int = 20) -> pd.Series:
    """Momentum oscillator: today's close minus the close `period` bars ago.

    Knoxville Divergence uses this to ask whether downside momentum is weakening
    while price is still making a lower low.
    """
    period = max(1, int(period))
    if talib is not None:
        try:
            # TA-Lib MOM is simply close[today] - close[N bars ago]. We wrap it
            # so screeners can use one stable helper regardless of whether
            # TA-Lib is installed on the user's machine.
            values = talib.MOM(np.asarray(series, dtype="float64"), timeperiod=period)
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            pass
    return _momentum_fallback(series, period)


def _momentum_fallback(series: pd.Series, period: int = 20) -> pd.Series:
    """Pure-pandas Momentum used when TA-Lib is unavailable."""
    period = max(1, int(period))
    # The first `period` rows are NaN because there is no candle far enough back
    # to compare against yet. That warm-up behavior matches TA-Lib's MOM output.
    return pd.to_numeric(series, errors="coerce").diff(periods=period)


def volume_average(volume: pd.Series, period: int = 20) -> pd.Series:
    """Rolling average volume, useful for volume spike/liquidity filters."""
    return volume.rolling(window=int(period), min_periods=int(period)).mean()


# ---------------------------------------------------------------------------
# Stochastic oscillator: TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 5,
    k_smoothing: int = 4,
    d_smoothing: int = 3,
) -> pd.DataFrame:
    """
    Slow Stochastic oscillator.

    Beginner note:
    - %K measures where the close sits inside the recent high-low range.
    - %D is a smoothed (averaged) version of %K, used as the signal line.
    Returns a DataFrame with two columns: `stoch_k` and `stoch_d`, both 0-100.

    Uses `talib.STOCH` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            stoch_k, stoch_d = talib.STOCH(
                np.asarray(high, dtype="float64"),
                np.asarray(low, dtype="float64"),
                np.asarray(close, dtype="float64"),
                fastk_period=int(k_period),
                slowk_period=int(k_smoothing),
                slowk_matype=0,  # 0 = simple moving average smoothing
                slowd_period=int(d_smoothing),
                slowd_matype=0,
            )
            return pd.DataFrame(
                {"stoch_k": stoch_k, "stoch_d": stoch_d},
                index=close.index,
            )
        except Exception:
            pass
    return _stochastic_fallback(high, low, close, k_period, k_smoothing, d_smoothing)


def _stochastic_fallback(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int,
    k_smoothing: int,
    d_smoothing: int,
) -> pd.DataFrame:
    """Pure-pandas Stochastic used when TA-Lib is unavailable.

    Step 1: Fast %K = where the close sits in the recent high-low range.
    Step 2: Slow %K = smoothed Fast %K.
    Step 3: Slow %D = smoothed Slow %K.
    """
    k_period = max(1, int(k_period))
    k_smoothing = max(1, int(k_smoothing))
    d_smoothing = max(1, int(d_smoothing))

    lowest_low = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()
    # Replacing a zero range with NaN avoids a divide-by-zero on flat candles.
    range_size = (highest_high - lowest_low).replace(0, np.nan)
    fast_k = 100.0 * (close - lowest_low) / range_size
    slow_k = fast_k.rolling(window=k_smoothing, min_periods=k_smoothing).mean()
    slow_d = slow_k.rolling(window=d_smoothing, min_periods=d_smoothing).mean()
    return pd.DataFrame({"stoch_k": slow_k, "stoch_d": slow_d}, index=close.index)


# ---------------------------------------------------------------------------
# Bollinger Bands: TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def bollinger_bands(close: pd.Series, period: int = 20, std_multiplier: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands: a moving-average middle band with volatility bands above
    and below it.

    Returns a DataFrame with `bb_middle`, `bb_upper`, `bb_lower`.
    Uses `talib.BBANDS` when TA-Lib is installed, otherwise a pandas fallback.
    Both paths use population standard deviation (ddof=0).
    """
    if talib is not None:
        try:
            upper, middle, lower = talib.BBANDS(
                np.asarray(close, dtype="float64"),
                timeperiod=max(1, int(period)),
                nbdevup=float(std_multiplier),
                nbdevdn=float(std_multiplier),
                matype=0,  # 0 = simple moving average for the middle band
            )
            return pd.DataFrame(
                {"bb_middle": middle, "bb_upper": upper, "bb_lower": lower},
                index=close.index,
            )
        except Exception:
            pass
    return _bollinger_bands_fallback(close, period, std_multiplier)


def _bollinger_bands_fallback(
    close: pd.Series, period: int = 20, std_multiplier: float = 2.0
) -> pd.DataFrame:
    """Pure-pandas Bollinger Bands used when TA-Lib is unavailable."""
    period = max(1, int(period))
    multiplier = float(std_multiplier)
    # Convert to numeric because close prices can come from APIs, CSV files, or
    # tests. Invalid values become NaN and naturally produce NaN bands.
    close_numeric = pd.to_numeric(close, errors="coerce")
    # `min_periods=period` means the first period-1 rows are warm-up rows. A
    # screener should wait for a complete window before reading a Bollinger Band.
    middle = close_numeric.rolling(window=period, min_periods=period).mean()
    # Population standard deviation (ddof=0) is intentional. Pandas defaults to
    # sample standard deviation, which would give slightly wider bands.
    rolling_std = close_numeric.rolling(window=period, min_periods=period).std(ddof=0)
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": middle + multiplier * rolling_std,
            "bb_lower": middle - multiplier * rolling_std,
        },
        index=close.index,
    )


# ---------------------------------------------------------------------------
# Heikin Ashi candles: pandas_ta primary, pandas fallback
# ---------------------------------------------------------------------------


def build_heikin_ashi(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Convert normal candles into Heikin Ashi candles.

    Beginner note:
    Heikin Ashi candles are built from normal OHLC candles, but they smooth the
    open/close values so trends are visually easier to read.

    Returns the cleaned OHLC frame plus four extra columns: `ha_open`,
    `ha_high`, `ha_low`, `ha_close`. Uses `pandas_ta.ha` when pandas_ta is
    installed, otherwise a pure-pandas fallback.
    """
    if pandas_ta is not None:
        try:
            return _build_heikin_ashi_pandas_ta(ohlc)
        except Exception:
            pass
    return _build_heikin_ashi_fallback(ohlc)


def _build_heikin_ashi_pandas_ta(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Heikin Ashi via pandas_ta, normalized to this app's `ha_*` column names."""
    frame = _prepare_ohlc(ohlc)
    if frame.empty:
        return frame
    # pandas_ta returns columns named HA_open/HA_high/HA_low/HA_close. We attach
    # them to our frame under lower-case names via `.to_numpy()` so pandas does
    # not try to align on a possibly-different index.
    ha = pandas_ta.ha(frame["open"], frame["high"], frame["low"], frame["close"])
    result = frame.copy()
    result["ha_open"] = ha["HA_open"].to_numpy()
    result["ha_high"] = ha["HA_high"].to_numpy()
    result["ha_low"] = ha["HA_low"].to_numpy()
    result["ha_close"] = ha["HA_close"].to_numpy()
    return result.reset_index(drop=True)


def _build_heikin_ashi_fallback(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Pure-pandas Heikin Ashi used when pandas_ta is unavailable."""
    frame = _prepare_ohlc(ohlc)
    if frame.empty:
        return frame

    result = frame.copy()
    # HA close is the average price of the normal candle. It blends open, high,
    # low, and close into one smoother closing value.
    ha_close = (frame["open"] + frame["high"] + frame["low"] + frame["close"]) / 4.0
    ha_open = [0.0] * len(frame)
    # The first HA open has no previous HA candle, so the standard seed is the
    # midpoint of the first normal candle's open and close.
    ha_open[0] = (float(frame.iloc[0]["open"]) + float(frame.iloc[0]["close"])) / 2.0

    # HA open depends on the previous HA candle, so this part must walk forward
    # one candle at a time.
    for index in range(1, len(frame)):
        ha_open[index] = (ha_open[index - 1] + float(ha_close.iloc[index - 1])) / 2.0

    result["ha_open"] = ha_open
    result["ha_close"] = ha_close
    # HA high/low keep the full candle range visible by taking the highest/lowest
    # value among the normal candle range and the smoothed HA open/close.
    result["ha_high"] = pd.concat([frame["high"], result["ha_open"], result["ha_close"]], axis=1).max(axis=1)
    result["ha_low"] = pd.concat([frame["low"], result["ha_open"], result["ha_close"]], axis=1).min(axis=1)
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# SuperTrend: pandas_ta primary, pandas fallback
# ---------------------------------------------------------------------------


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """
    True Range measures the largest price movement available for each candle.

    It includes gaps from the previous close, which is why ATR uses True Range
    instead of only using high-low.
    """
    previous_close = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average True Range using Wilder-style smoothing."""
    period = max(1, int(period))
    true_range = _true_range(high, low, close)
    # Wilder smoothing is equivalent to an exponential moving average with
    # alpha=1/period. `min_periods=period` leaves early warm-up candles as NaN
    # until there is enough history to trust the ATR.
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def supertrend(ohlc: pd.DataFrame, atr_period: int = 10, multiplier: float = 2.0) -> pd.DataFrame:
    """
    Calculate SuperTrend on the OHLC columns passed to this function.

    Important for the Heikin Ashi screener:
    if the caller passes HA open/high/low/close renamed to open/high/low/close,
    this function calculates SuperTrend on HA candles, not normal candles.

    Returns the cleaned OHLC frame plus `atr`, `supertrend`,
    `supertrend_direction`, and `supertrend_color`. Uses `pandas_ta.supertrend`
    when pandas_ta is installed, otherwise a pure-pandas fallback.
    """
    if float(multiplier) <= 0:
        raise ValueError("SuperTrend multiplier must be positive.")
    if pandas_ta is not None:
        try:
            return _supertrend_pandas_ta(ohlc, atr_period, multiplier)
        except Exception:
            pass
    return _supertrend_fallback(ohlc, atr_period, multiplier)


def _supertrend_pandas_ta(ohlc: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    """SuperTrend via pandas_ta, normalized to this app's column schema."""
    result = _prepare_ohlc(ohlc)
    if result.empty:
        return result

    st_frame = pandas_ta.supertrend(
        result["high"], result["low"], result["close"],
        length=max(1, int(atr_period)),
        multiplier=float(multiplier),
    )
    if st_frame is None or st_frame.empty:
        raise ValueError("pandas_ta.supertrend returned no data")

    # pandas_ta names columns like `SUPERT_10_2.0` (the line) and
    # `SUPERTd_10_2.0` (the direction). We match by prefix so we do not depend
    # on the exact period/multiplier suffix.
    line_column = next(column for column in st_frame.columns if column.startswith("SUPERT_"))
    direction_column = next(column for column in st_frame.columns if column.startswith("SUPERTd_"))
    direction = st_frame[direction_column].to_numpy()

    # ATR is not part of pandas_ta's supertrend output, so we compute it with
    # the shared helper to keep the column schema identical to the fallback.
    result["atr"] = _wilder_atr(result["high"], result["low"], result["close"], atr_period).to_numpy()
    result["supertrend"] = st_frame[line_column].to_numpy()
    result["supertrend_direction"] = direction
    # A text color label is easier to inspect in Streamlit/tests than raw 1/-1.
    result["supertrend_color"] = np.where(
        direction == 1, "green", np.where(direction == -1, "red", "warmup")
    )
    return result.reset_index(drop=True)


def _supertrend_fallback(ohlc: pd.DataFrame, atr_period: int = 10, multiplier: float = 2.0) -> pd.DataFrame:
    """Pure-pandas SuperTrend used when pandas_ta is unavailable."""
    period = max(1, int(atr_period))
    factor = float(multiplier)

    # The function is candle-type agnostic. Normal candles, Heikin Ashi candles,
    # or any future transformed candle can be used as long as the columns are
    # named open/high/low/close before calling this helper.
    result = _prepare_ohlc(ohlc)
    if result.empty:
        return result

    high = result["high"]
    low = result["low"]
    close = result["close"]
    atr = _wilder_atr(high, low, close, period)

    # SuperTrend starts with two basic bands around the candle midpoint. ATR
    # decides how far the bands sit from price, and the multiplier widens or
    # tightens that distance.
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + factor * atr
    basic_lower = hl2 - factor * atr

    # These arrays are filled row-by-row because SuperTrend is stateful: today's
    # final band and direction depend on yesterday's final band and direction.
    final_upper = np.full(len(result), np.nan, dtype=float)
    final_lower = np.full(len(result), np.nan, dtype=float)
    direction = np.zeros(len(result), dtype=int)
    line = np.full(len(result), np.nan, dtype=float)

    for index in range(len(result)):
        if not np.isfinite(float(atr.iloc[index])):
            # ATR is NaN during warm-up, so the SuperTrend line is also left NaN.
            # Screeners skip these early rows instead of treating them as signals.
            continue

        if index == 0 or not np.isfinite(final_upper[index - 1]) or not np.isfinite(final_lower[index - 1]):
            # The first valid ATR candle seeds both final bands and starts in an
            # uptrend by convention. Later candles can then update that state.
            final_upper[index] = float(basic_upper.iloc[index])
            final_lower[index] = float(basic_lower.iloc[index])
            direction[index] = 1
            line[index] = final_lower[index]
            continue

        previous_close = float(close.iloc[index - 1])
        # A final upper band can move lower in a downtrend, but it should not
        # keep rising against price unless price has already closed above it.
        final_upper[index] = (
            float(basic_upper.iloc[index])
            if float(basic_upper.iloc[index]) < final_upper[index - 1] or previous_close > final_upper[index - 1]
            else final_upper[index - 1]
        )
        # A final lower band can move higher in an uptrend, but it should not
        # keep falling away from price unless price has already closed below it.
        final_lower[index] = (
            float(basic_lower.iloc[index])
            if float(basic_lower.iloc[index]) > final_lower[index - 1] or previous_close < final_lower[index - 1]
            else final_lower[index - 1]
        )

        # Direction flips only when close crosses the opposite final band. The
        # line shown to screeners is the lower band in an uptrend and the upper
        # band in a downtrend.
        if direction[index - 1] == -1 and float(close.iloc[index]) > final_upper[index - 1]:
            direction[index] = 1
        elif direction[index - 1] == 1 and float(close.iloc[index]) < final_lower[index - 1]:
            direction[index] = -1
        else:
            direction[index] = direction[index - 1]

        line[index] = final_lower[index] if direction[index] == 1 else final_upper[index]

    result["atr"] = atr
    result["supertrend"] = line
    result["supertrend_direction"] = direction
    # A text color label is easier to inspect in Streamlit/tests than raw 1/-1
    # direction numbers, while the numeric direction remains available above.
    result["supertrend_color"] = np.where(direction == 1, "green", np.where(direction == -1, "red", "warmup"))
    return result.reset_index(drop=True)
