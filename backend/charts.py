from __future__ import annotations

# Shared Plotly chart helpers used by every screener's `build_chart(...)`.
#
# Each screener exports a small `build_chart` function that composes these
# helpers with its own indicator math. Keeping the chart primitives here means
# every chart in the app shares the same look-and-feel (colors, axis style,
# weekend gap removal, volume subplot) without each screener re-implementing
# them.

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backend.indicators import bollinger_bands, build_heikin_ashi, stochastic, supertrend


# Single source of truth for chart colors. Keeping them grouped here makes the
# whole app easy to retheme later by editing one dict.
_COLORS = {
    "bull": "#26a69a",
    "bear": "#ef5350",
    "supertrend_up": "#26a69a",
    "supertrend_down": "#ef5350",
    "bb_band": "#7e57c2",
    "bb_middle": "#9575cd",
    "volume_up": "rgba(38, 166, 154, 0.45)",
    "volume_down": "rgba(239, 83, 80, 0.45)",
    "stoch_k": "#42a5f5",
    "stoch_d": "#ffa726",
    "guide_line": "#888888",
}


def _normalize_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted, numeric, timestamped copy of the input candles.

    The screeners may pass slightly different shapes (Dhan/test/parquet), so we
    normalize once here. We keep all rows; indicators decide how to handle the
    warm-up window themselves.
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
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Shared low-level builders (used by both the 2-row and 3-row chart helpers)
# ---------------------------------------------------------------------------


def _add_candle_trace(fig: go.Figure, frame: pd.DataFrame, *, row: int, ha: bool) -> None:
    """Add a candlestick trace to `row`. `ha=True` plots Heikin Ashi candles."""
    if ha:
        # `build_heikin_ashi` keeps original OHLC plus `ha_*` columns; we plot
        # the HA values for visual smoothing. `frame` is already normalized so
        # the HA frame has the same length and row order.
        ha_frame = build_heikin_ashi(frame)
        candle_open = ha_frame["ha_open"]
        candle_high = ha_frame["ha_high"]
        candle_low = ha_frame["ha_low"]
        candle_close = ha_frame["ha_close"]
        candle_label = "Heikin Ashi"
    else:
        candle_open = frame["open"]
        candle_high = frame["high"]
        candle_low = frame["low"]
        candle_close = frame["close"]
        candle_label = "Candles"

    fig.add_trace(
        go.Candlestick(
            x=frame["timestamp"],
            open=candle_open,
            high=candle_high,
            low=candle_low,
            close=candle_close,
            increasing_line_color=_COLORS["bull"],
            decreasing_line_color=_COLORS["bear"],
            name=candle_label,
            showlegend=False,
        ),
        row=row,
        col=1,
    )


def _add_volume_trace(fig: go.Figure, frame: pd.DataFrame, *, row: int) -> None:
    """Add a volume bar trace to `row` (no-op when there is no volume column)."""
    if "volume" not in frame.columns:
        return
    # Volume bars colored by candle direction. Using the *normal* close-vs-open
    # relationship keeps the volume coloring meaningful even on HA charts.
    volume_colors = [
        _COLORS["volume_up"] if close_value >= open_value else _COLORS["volume_down"]
        for open_value, close_value in zip(frame["open"], frame["close"])
    ]
    fig.add_trace(
        go.Bar(
            x=frame["timestamp"],
            y=frame["volume"],
            marker_color=volume_colors,
            name="Volume",
            showlegend=False,
        ),
        row=row,
        col=1,
    )


def _apply_pan_layout(fig: go.Figure, title: str, *, height: int) -> None:
    """Apply the shared dark theme, pan drag-mode, and weekend gap removal."""
    fig.update_layout(
        title=title,
        template="plotly_dark",
        height=height,
        margin={"l": 40, "r": 30, "t": 60, "b": 40},
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "y": 1.06, "x": 0.0},
        # `dragmode="pan"` makes a left-mouse-button click-and-drag PAN the
        # chart instead of Plotly's default box-zoom. Mouse-wheel zoom is
        # enabled separately via `config={"scrollZoom": True}` on the
        # `st.plotly_chart(...)` call in app.py. Dragging directly on an axis
        # still zooms/scales that single axis (native Plotly behavior).
        dragmode="pan",
    )
    # `rangebreaks` removes Sat/Sun gaps from the X axis so the chart doesn't
    # render flat horizontal lines over weekends. Indian holidays still appear
    # as small gaps; configuring NSE holidays per year would be overkill here.
    fig.update_xaxes(
        rangebreaks=[{"bounds": ["sat", "mon"]}],
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
    )


# ---------------------------------------------------------------------------
# Public chart builders
# ---------------------------------------------------------------------------


def candlestick_with_volume(candles: pd.DataFrame, title: str, *, ha: bool = False) -> go.Figure:
    """Build a two-row candlestick + volume chart.

    `ha=True` renders Heikin Ashi candles (computed from the supplied normal
    OHLC via `build_heikin_ashi`). The X-axis hides weekends with a Plotly
    `rangebreak`, which matches the way TradingView draws daily charts.
    """
    frame = _normalize_candles(candles)
    if frame.empty:
        # Return a minimally valid figure so callers never crash on empty data.
        return go.Figure(layout={"title": f"{title} — no candles available"})

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.02,
    )
    _add_candle_trace(fig, frame, row=1, ha=ha)
    _add_volume_trace(fig, frame, row=2)
    _apply_pan_layout(fig, title, height=650)
    # Two-decimal y-axis matches the table formatting (NSE prices are
    # paisa-precision, so anything beyond 2 decimals is display noise).
    fig.update_yaxes(title_text="Price", tickformat=".2f", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return fig


def candlestick_volume_oscillator(candles: pd.DataFrame, title: str, *, ha: bool = False) -> go.Figure:
    """Build a three-row chart: price candles / volume / an empty oscillator panel.

    The oscillator panel (row 3) starts empty; a caller fills it with, e.g.,
    `add_stochastic_overlay(...)`. This layout is used by indicator screeners
    whose indicator lives in its own 0-100 panel rather than over the price.
    """
    frame = _normalize_candles(candles)
    if frame.empty:
        return go.Figure(layout={"title": f"{title} — no candles available"})

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.17, 0.28],
        vertical_spacing=0.03,
    )
    _add_candle_trace(fig, frame, row=1, ha=ha)
    _add_volume_trace(fig, frame, row=2)
    _apply_pan_layout(fig, title, height=780)
    fig.update_yaxes(title_text="Price", tickformat=".2f", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="Oscillator", row=3, col=1)
    return fig


# ---------------------------------------------------------------------------
# Overlays (each adds traces to an existing figure)
# ---------------------------------------------------------------------------


def add_line_overlay(
    fig: go.Figure,
    timestamps: pd.Series,
    values: pd.Series,
    name: str,
    color: str,
    *,
    row: int = 1,
    dash: str | None = None,
    width: float = 1.4,
) -> None:
    """Add a simple line trace (e.g. a moving average) to a subplot row."""
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=values,
            name=name,
            mode="lines",
            line={"color": color, "width": width, "dash": dash},
        ),
        row=row,
        col=1,
    )


def add_supertrend_overlay(
    fig: go.Figure,
    candles_for_st: pd.DataFrame,
    *,
    atr_period: int,
    multiplier: float,
    color_up: str = _COLORS["supertrend_up"],
    color_down: str = _COLORS["supertrend_down"],
) -> None:
    """Overlay the SuperTrend line on row 1 of `fig`.

    `candles_for_st` is the OHLC frame that SuperTrend should be calculated
    against. For HA-based screeners, the screener should pass HA values
    renamed to `open/high/low/close` so SuperTrend reads the smoothed candles.
    """
    frame = _normalize_candles(candles_for_st)
    if frame.empty:
        return
    st_frame = supertrend(frame, atr_period=atr_period, multiplier=multiplier)
    if st_frame.empty or "supertrend" not in st_frame.columns:
        return

    # We draw the SuperTrend line as a single series; coloring by trend
    # direction is achieved by splitting the line into "up" and "down"
    # segments via NaN gaps so a single legend entry per direction is enough.
    direction = st_frame.get("supertrend_direction")
    if direction is None:
        fig.add_trace(
            go.Scatter(
                x=st_frame["timestamp"],
                y=st_frame["supertrend"],
                name=f"SuperTrend({atr_period}, {multiplier:g})",
                line={"color": color_up, "width": 1.6},
                mode="lines",
            ),
            row=1,
            col=1,
        )
        return

    up_values = st_frame["supertrend"].where(direction == 1)
    down_values = st_frame["supertrend"].where(direction == -1)
    fig.add_trace(
        go.Scatter(
            x=st_frame["timestamp"],
            y=up_values,
            name=f"SuperTrend up ({atr_period}, {multiplier:g})",
            line={"color": color_up, "width": 1.8},
            mode="lines",
            connectgaps=False,
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=st_frame["timestamp"],
            y=down_values,
            name=f"SuperTrend down ({atr_period}, {multiplier:g})",
            line={"color": color_down, "width": 1.8},
            mode="lines",
            connectgaps=False,
        ),
        row=1,
        col=1,
    )


def add_bollinger_overlay(
    fig: go.Figure,
    candles: pd.DataFrame,
    *,
    period: int,
    std_multiplier: float,
) -> None:
    """Overlay upper/middle/lower Bollinger Bands on row 1 of `fig`."""
    frame = _normalize_candles(candles)
    if frame.empty:
        return
    bands = bollinger_bands(frame["close"], period=period, std_multiplier=std_multiplier)
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=bands["bb_upper"],
            name=f"BB Upper ({period}, {std_multiplier:g})",
            line={"color": _COLORS["bb_band"], "width": 1.2, "dash": "dot"},
            mode="lines",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=bands["bb_middle"],
            name=f"BB Middle ({period})",
            line={"color": _COLORS["bb_middle"], "width": 1.2},
            mode="lines",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=bands["bb_lower"],
            name=f"BB Lower ({period}, {std_multiplier:g})",
            line={"color": _COLORS["bb_band"], "width": 1.2, "dash": "dot"},
            mode="lines",
        ),
        row=1,
        col=1,
    )


def add_stochastic_overlay(
    fig: go.Figure,
    candles: pd.DataFrame,
    *,
    k_period: int,
    k_smoothing: int,
    d_smoothing: int,
    oversold: float = 20.0,
    overbought: float = 80.0,
    row: int = 3,
) -> None:
    """Plot the Stochastic %K and %D lines plus oversold/overbought guide lines.

    Intended for the oscillator panel (row 3) of `candlestick_volume_oscillator`.
    The panel's y-axis is fixed to the 0-100 range the oscillator lives in.
    """
    frame = _normalize_candles(candles)
    if frame.empty:
        return
    stoch = stochastic(
        frame["high"],
        frame["low"],
        frame["close"],
        k_period=k_period,
        k_smoothing=k_smoothing,
        d_smoothing=d_smoothing,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=stoch["stoch_k"],
            name="%K",
            mode="lines",
            line={"color": _COLORS["stoch_k"], "width": 1.4},
        ),
        row=row,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=stoch["stoch_d"],
            name="%D",
            mode="lines",
            line={"color": _COLORS["stoch_d"], "width": 1.4},
        ),
        row=row,
        col=1,
    )
    # Dashed guide lines mark the oversold/overbought zones the strategy cares
    # about (crossovers below 20 or above 80).
    for level in (oversold, overbought):
        fig.add_hline(
            y=level,
            line={"color": _COLORS["guide_line"], "width": 1, "dash": "dash"},
            row=row,
            col=1,
        )
    # The Stochastic oscillator is bounded 0-100, so pin the panel to that range.
    fig.update_yaxes(range=[0, 100], row=row, col=1)
