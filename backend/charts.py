from __future__ import annotations

# Chart helpers for every screener's `build_chart(...)`.
#
# How charting works in this app (beginner note):
# - Each screener's `build_chart` composes the helpers below into a plain
#   "chart spec" dict — a JSON-serializable description of panes and series.
# - `render_chart_html(spec)` turns that spec into a small HTML document that
#   embeds TradingView's Lightweight Charts v5 library (loaded from a CDN).
# - `app.py` shows that HTML with `st.components.v1.html(...)`.
#
# Why Lightweight Charts instead of Plotly: its price scale is natively
# drag-to-scale (grab the right-edge price axis and drag to zoom the Y-axis),
# which is the TradingView-style interaction the app needs and Plotly cannot do.

import html as _html
import json
import math

import pandas as pd

from backend.indicators import bollinger_bands, build_heikin_ashi, stochastic, supertrend


# Pinned Lightweight Charts v5 version. Pinning an exact version keeps the
# embedded JavaScript API stable; bump deliberately when upgrading.
_LWC_VERSION = "5.2.0"
_LWC_CDN_URL = (
    f"https://unpkg.com/lightweight-charts@{_LWC_VERSION}"
    "/dist/lightweight-charts.standalone.production.js"
)

# Single source of truth for chart colors. Editing this dict re-themes the app.
_COLORS = {
    "bull": "#26a69a",
    "bear": "#ef5350",
    "supertrend_up": "#26a69a",
    "supertrend_down": "#ef5350",
    "bb_band": "#7e57c2",
    "bb_middle": "#9575cd",
    "volume_up": "rgba(38, 166, 154, 0.55)",
    "volume_down": "rgba(239, 83, 80, 0.55)",
    "stoch_k": "#42a5f5",
    "stoch_d": "#ffa726",
    "guide_line": "#888888",
}

# A price column shows two decimals (NSE prices are paisa-precision).
_PRICE_FORMAT = {"type": "price", "precision": 2, "minMove": 0.01}

# Maps a human dash name to a Lightweight Charts LineStyle integer
# (0 = solid, 1 = dotted, 2 = dashed).
_DASH_TO_LINESTYLE = {None: 0, "solid": 0, "dot": 1, "dotted": 1, "dash": 2, "dashed": 2}


# ---------------------------------------------------------------------------
# Small data helpers
# ---------------------------------------------------------------------------


def _normalize_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Return sorted, numeric, de-duplicated candles.

    Lightweight Charts requires data sorted ascending by time with one point
    per timestamp, so we sort and drop duplicate days here.
    """
    required = ["timestamp", "open", "high", "low", "close"]
    missing = [column for column in required if column not in candles.columns]
    if missing:
        raise ValueError(f"build_chart received candles missing columns: {missing}")

    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )


def _num(value: object) -> float | None:
    """Return a finite float, or None for NaN/inf/non-numeric values.

    None becomes a "whitespace" gap in a Lightweight Charts series.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_times(timestamps) -> list[str]:
    """Format any timestamp-like sequence into 'YYYY-MM-DD' strings.

    Lightweight Charts treats 'YYYY-MM-DD' values as business days and draws
    them consecutively, so weekend gaps disappear automatically.
    """
    return list(pd.to_datetime(pd.Index(timestamps)).strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Series builders (each returns one Lightweight Charts series spec)
# ---------------------------------------------------------------------------


def _candle_series(frame: pd.DataFrame, ha: bool) -> dict:
    """Build a candlestick series spec. `ha=True` plots Heikin Ashi candles."""
    if ha:
        # `build_heikin_ashi` re-cleans the frame; since `frame` is already
        # normalized the row order/length is preserved.
        ha_frame = build_heikin_ashi(frame)
        times = _iso_times(ha_frame["timestamp"])
        opens, highs = ha_frame["ha_open"], ha_frame["ha_high"]
        lows, closes = ha_frame["ha_low"], ha_frame["ha_close"]
    else:
        times = _iso_times(frame["timestamp"])
        opens, highs = frame["open"], frame["high"]
        lows, closes = frame["low"], frame["close"]

    data = []
    for time, open_, high, low, close in zip(times, opens, highs, lows, closes):
        values = (_num(open_), _num(high), _num(low), _num(close))
        if None in values:
            continue  # a candle needs all four values; skip incomplete rows
        data.append({"time": time, "open": values[0], "high": values[1],
                     "low": values[2], "close": values[3]})

    return {
        "kind": "candlestick",
        "data": data,
        "options": {
            "upColor": _COLORS["bull"],
            "downColor": _COLORS["bear"],
            "wickUpColor": _COLORS["bull"],
            "wickDownColor": _COLORS["bear"],
            "borderVisible": False,
            "priceFormat": _PRICE_FORMAT,
        },
    }


def _volume_series(frame: pd.DataFrame) -> dict:
    """Build a volume histogram series spec, colored by candle direction."""
    times = _iso_times(frame["timestamp"])
    data = []
    for time, open_, close, volume in zip(
        times, frame["open"], frame["close"], frame["volume"]
    ):
        value = _num(volume)
        if value is None:
            data.append({"time": time})  # whitespace gap
            continue
        # Color uses the normal close-vs-open relationship even on HA charts.
        color = _COLORS["volume_up"] if close >= open_ else _COLORS["volume_down"]
        data.append({"time": time, "value": value, "color": color})

    return {
        "kind": "histogram",
        "data": data,
        "options": {
            "priceFormat": {"type": "volume"},
            "priceLineVisible": False,
            "lastValueVisible": False,
        },
    }


def _line_series(timestamps, values, title: str, color: str, *, dash=None, width: int = 2) -> dict:
    """Build a line series spec. NaN values become whitespace gaps."""
    times = _iso_times(timestamps)
    data = []
    for time, value in zip(times, values):
        numeric = _num(value)
        if numeric is None:
            data.append({"time": time})  # whitespace gap (e.g. indicator warm-up)
        else:
            data.append({"time": time, "value": numeric})

    return {
        "kind": "line",
        "data": data,
        "options": {
            "color": color,
            "lineWidth": int(width),
            "lineStyle": _DASH_TO_LINESTYLE.get(dash, 0),
            "title": title,
            "priceLineVisible": False,
            "lastValueVisible": False,
            "priceFormat": _PRICE_FORMAT,
        },
    }


def _empty_spec(title: str) -> dict:
    """Return a minimal but renderable spec for empty/missing candle data."""
    return {
        "title": f"{title} — no candles available",
        "height": 220,
        "panes": [{"height": 180, "series": [], "price_lines": []}],
    }


# ---------------------------------------------------------------------------
# Public spec builders
# ---------------------------------------------------------------------------


def candlestick_with_volume(candles: pd.DataFrame, title: str, *, ha: bool = False) -> dict:
    """Build a two-pane chart spec: price candles on top, volume below.

    `ha=True` renders Heikin Ashi candles instead of normal OHLC candles.
    """
    if candles is None or candles.empty:
        return _empty_spec(title)
    frame = _normalize_candles(candles)
    if frame.empty:
        return _empty_spec(title)

    price_pane = {"height": 430, "series": [_candle_series(frame, ha)], "price_lines": []}
    volume_pane = {"height": 130, "series": [], "price_lines": []}
    if "volume" in frame.columns:
        volume_pane["series"].append(_volume_series(frame))
    return {"title": title, "height": 620, "panes": [price_pane, volume_pane]}


def candlestick_volume_oscillator(candles: pd.DataFrame, title: str, *, ha: bool = False) -> dict:
    """Build a three-pane chart spec: price / volume / an empty oscillator pane.

    The oscillator pane (index 2) starts empty; a caller fills it with, e.g.,
    `add_stochastic_overlay(...)`.
    """
    if candles is None or candles.empty:
        return _empty_spec(title)
    frame = _normalize_candles(candles)
    if frame.empty:
        return _empty_spec(title)

    price_pane = {"height": 350, "series": [_candle_series(frame, ha)], "price_lines": []}
    volume_pane = {"height": 110, "series": [], "price_lines": []}
    if "volume" in frame.columns:
        volume_pane["series"].append(_volume_series(frame))
    oscillator_pane = {"height": 170, "series": [], "price_lines": []}
    return {"title": title, "height": 700, "panes": [price_pane, volume_pane, oscillator_pane]}


# ---------------------------------------------------------------------------
# Overlay mutators (each appends series/guide-lines to a pane of an existing spec)
# ---------------------------------------------------------------------------


def add_line_overlay(spec: dict, timestamps, values, name: str, color: str,
                     *, pane: int = 0, dash=None) -> None:
    """Append a simple line series (e.g. a moving average) to a pane."""
    panes = spec.get("panes", [])
    if 0 <= pane < len(panes):
        panes[pane]["series"].append(_line_series(timestamps, values, name, color, dash=dash))


def add_supertrend_overlay(spec: dict, candles_for_st: pd.DataFrame, *,
                           atr_period: int, multiplier: float) -> None:
    """Append the SuperTrend line to the price pane (pane 0).

    `candles_for_st` is the OHLC frame SuperTrend is calculated against — for
    HA-based screeners the caller passes HA values renamed to open/high/low/close.

    Beginner note — why this splits into "runs":
    SuperTrend is ONE line. At each bar it sits on one side of price (below in
    an uptrend, above in a downtrend) and "flips" sides when the trend changes.
    Lightweight Charts draws a straight connector across any gap *inside* a
    single line series, so we cannot make one series "break" at the flips, and
    two overlapping series would look like a channel. Instead we cut the line
    into contiguous same-direction **runs** and draw each run as its own
    gap-free series (green for uptrend runs, red for downtrend runs). The runs
    do not overlap in time, so together they read as one bicolor line.
    """
    frame = _normalize_candles(candles_for_st)
    if frame.empty:
        return
    st_frame = supertrend(frame, atr_period=atr_period, multiplier=multiplier)
    if st_frame.empty or "supertrend" not in st_frame.columns:
        return

    direction_series = st_frame.get("supertrend_direction")
    if direction_series is None:
        # No trend-direction info: draw the raw line as a single series.
        add_line_overlay(spec, st_frame["timestamp"], st_frame["supertrend"],
                         f"SuperTrend({atr_period}, {multiplier:g})",
                         _COLORS["supertrend_up"], pane=0)
        return

    # Walk the bars and group them into contiguous runs of the same direction.
    # A run ends when the direction flips or the SuperTrend value is missing
    # (the ATR warm-up bars have no value and belong to no run).
    runs: list[tuple[float, list]] = []
    run_direction: float | None = None
    run_points: list = []
    for timestamp, value, direction in zip(
        st_frame["timestamp"], st_frame["supertrend"], direction_series
    ):
        numeric = _num(value)
        sign = _num(direction)
        if numeric is None or sign not in (1.0, -1.0):
            if run_points:
                runs.append((run_direction, run_points))
            run_points, run_direction = [], None
            continue
        if run_points and sign != run_direction:
            runs.append((run_direction, run_points))
            run_points = []
        run_direction = sign
        run_points.append((timestamp, numeric))
    if run_points:
        runs.append((run_direction, run_points))

    # Each run is a gap-free line series. `name=""` keeps the price scale clean
    # (no per-run label); the chart title already names the indicator.
    for direction, points in runs:
        color = _COLORS["supertrend_up"] if direction == 1.0 else _COLORS["supertrend_down"]
        add_line_overlay(spec, [t for t, _ in points], [v for _, v in points],
                         "", color, pane=0)


def add_bollinger_overlay(spec: dict, candles: pd.DataFrame, *,
                          period: int, std_multiplier: float) -> None:
    """Append upper/middle/lower Bollinger Bands to the price pane (pane 0)."""
    frame = _normalize_candles(candles)
    if frame.empty:
        return
    bands = bollinger_bands(frame["close"], period=period, std_multiplier=std_multiplier)
    times = frame["timestamp"]
    add_line_overlay(spec, times, bands["bb_upper"],
                     f"BB Upper ({period}, {std_multiplier:g})",
                     _COLORS["bb_band"], pane=0, dash="dot")
    add_line_overlay(spec, times, bands["bb_middle"], f"BB Middle ({period})",
                     _COLORS["bb_middle"], pane=0)
    add_line_overlay(spec, times, bands["bb_lower"],
                     f"BB Lower ({period}, {std_multiplier:g})",
                     _COLORS["bb_band"], pane=0, dash="dot")


def add_stochastic_overlay(spec: dict, candles: pd.DataFrame, *,
                           k_period: int, k_smoothing: int, d_smoothing: int,
                           oversold: float = 20.0, overbought: float = 80.0,
                           pane: int = 2) -> None:
    """Append %K/%D lines plus oversold/overbought guide lines to a pane."""
    panes = spec.get("panes", [])
    if not (0 <= pane < len(panes)):
        return
    frame = _normalize_candles(candles)
    if frame.empty:
        return
    stoch = stochastic(frame["high"], frame["low"], frame["close"],
                       k_period=k_period, k_smoothing=k_smoothing, d_smoothing=d_smoothing)
    add_line_overlay(spec, frame["timestamp"], stoch["stoch_k"], "%K", _COLORS["stoch_k"], pane=pane)
    add_line_overlay(spec, frame["timestamp"], stoch["stoch_d"], "%D", _COLORS["stoch_d"], pane=pane)
    # Dashed reference lines mark the oversold/overbought zones.
    panes[pane]["price_lines"].extend([
        {"price": float(oversold), "color": _COLORS["guide_line"], "title": f"{oversold:g}"},
        {"price": float(overbought), "color": _COLORS["guide_line"], "title": f"{overbought:g}"},
    ])


# ---------------------------------------------------------------------------
# Spec -> HTML renderer
# ---------------------------------------------------------------------------

# The chart HTML. Sentinel tokens (__CDN__, __TITLE__, __SPEC_JSON__) are
# substituted by `render_chart_html`. The JavaScript is generic: it reads any
# spec and builds the panes/series, so there is no per-screener JavaScript.
_CHART_HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: #0e1117; }
  body { display: flex; flex-direction: column; height: 100vh;
         font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
  .chart-title { flex: 0 0 auto; color: #d0d4dc; font-size: 13px; padding: 6px 10px; }
  #chart { flex: 1 1 auto; min-height: 0; }
</style>
</head>
<body>
<div class="chart-title">__TITLE__</div>
<div id="chart"></div>
<script src="__CDN__"></script>
<script>
const SPEC = __SPEC_JSON__;
(function () {
  const LWC = window.LightweightCharts;
  const container = document.getElementById("chart");
  if (!LWC) {
    container.textContent = "Chart library failed to load (check your connection).";
    return;
  }
  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: { background: { color: "#0e1117" }, textColor: "#d0d4dc" },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.06)" },
      horzLines: { color: "rgba(255,255,255,0.06)" },
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.15)" },
    timeScale: { borderColor: "rgba(255,255,255,0.15)" },
  });
  const CTOR = {
    candlestick: LWC.CandlestickSeries,
    line: LWC.LineSeries,
    histogram: LWC.HistogramSeries,
  };
  (SPEC.panes || []).forEach(function (pane, paneIndex) {
    let firstSeries = null;
    (pane.series || []).forEach(function (s) {
      const ctor = CTOR[s.kind];
      if (!ctor) { return; }
      const series = chart.addSeries(ctor, s.options || {}, paneIndex);
      series.setData(s.data || []);
      if (!firstSeries) { firstSeries = series; }
    });
    if (firstSeries) {
      (pane.price_lines || []).forEach(function (pl) {
        firstSeries.createPriceLine({
          price: pl.price, color: pl.color, lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: pl.title || "",
        });
      });
    }
  });
  const panes = chart.panes();
  (SPEC.panes || []).forEach(function (pane, i) {
    if (panes[i] && pane.height) { panes[i].setHeight(pane.height); }
  });
  chart.timeScale().fitContent();
})();
</script>
</body>
</html>
"""


def render_chart_html(spec: dict) -> str:
    """Render a chart spec into a self-contained HTML document.

    Embed it in Streamlit with `st.components.v1.html(html, height=spec["height"])`.
    """
    # `allow_nan=False` makes a stray NaN fail loudly rather than emit invalid
    # data; the series builders already convert NaN to whitespace points.
    payload = json.dumps(spec, separators=(",", ":"), allow_nan=False)
    # Neutralize any "</script>" sequence inside the data so embedded JSON can
    # never break out of the <script> tag (defense against HTML injection).
    payload = payload.replace("</", "<\\/")
    title = _html.escape(str(spec.get("title", "")))
    return (
        _CHART_HTML_TEMPLATE
        .replace("__CDN__", _LWC_CDN_URL)
        .replace("__TITLE__", title)
        .replace("__SPEC_JSON__", payload)
    )
