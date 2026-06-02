"""Tests for the Lightweight Charts chart-spec builders and HTML renderer.

These verify the pure-Python layer: the spec dictionaries each `build_chart`
produces and the HTML that `render_chart_html` emits. They do not exercise a
browser — the actual chart drawing is the (trusted) Lightweight Charts library.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from backend import charts
from screeners import (
    bollinger_band_reversal,
    bollinger_lower_band,
    envelope,
    envelope_knoxville_buy,
    green_candles_20pct_up,
    heikin_ashi_supertrend,
    stochastic_swing,
    technical_analysis,
)


def _candles(periods: int = 260) -> pd.DataFrame:
    """Return a well-formed daily OHLCV frame for chart-spec tests."""
    close = np.linspace(100.0, 130.0, periods) + np.sin(np.arange(periods))
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D"),
            "open": close - 0.5,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": [1000.0] * periods,
        }
    )


def test_candlestick_with_volume_builds_two_pane_spec():
    spec = charts.candlestick_with_volume(_candles(), "Title")

    assert spec["title"] == "Title"
    assert isinstance(spec["height"], int)
    assert len(spec["panes"]) == 2
    # Pane 0 is the price pane (candlesticks); pane 1 is the volume histogram.
    assert spec["panes"][0]["series"][0]["kind"] == "candlestick"
    assert spec["panes"][1]["series"][0]["kind"] == "histogram"


def test_candlestick_volume_oscillator_builds_three_panes():
    spec = charts.candlestick_volume_oscillator(_candles(), "Title")

    assert len(spec["panes"]) == 3
    # The oscillator pane (index 2) starts empty until an overlay fills it.
    assert spec["panes"][2]["series"] == []


def test_overlays_append_series_to_the_correct_pane():
    spec = charts.candlestick_with_volume(_candles(), "Title")
    charts.add_bollinger_overlay(spec, _candles(), period=20, std_multiplier=2.0)

    price_kinds = [series["kind"] for series in spec["panes"][0]["series"]]
    # One candlestick plus three Bollinger lines on the price pane.
    assert price_kinds == ["candlestick", "line", "line", "line"]


def test_stochastic_overlay_adds_lines_and_guide_lines():
    spec = charts.candlestick_volume_oscillator(_candles(), "Title")
    charts.add_stochastic_overlay(
        spec, _candles(), k_period=5, k_smoothing=4, d_smoothing=3,
        oversold=20.0, overbought=80.0, pane=2,
    )
    oscillator = spec["panes"][2]
    assert [s["kind"] for s in oscillator["series"]] == ["line", "line"]
    # 20 and 80 reference lines.
    assert sorted(pl["price"] for pl in oscillator["price_lines"]) == [20.0, 80.0]


def test_line_series_converts_nan_to_whitespace_points():
    # A line series point with a NaN value must be a whitespace point — a dict
    # with a "time" key but no "value" — so Lightweight Charts draws a gap.
    values = pd.Series([np.nan, 10.0, np.nan])
    times = pd.date_range("2024-01-01", periods=3, freq="D")
    series = charts._line_series(times, values, "x", "#fff")
    assert "value" not in series["data"][0]
    assert series["data"][1]["value"] == 10.0
    assert "value" not in series["data"][2]


def test_render_chart_html_embeds_cdn_and_spec():
    spec = charts.candlestick_with_volume(_candles(), "Title")
    html = charts.render_chart_html(spec)

    assert "lightweight-charts@" in html  # the pinned CDN script
    assert "createChart" in html
    assert "Title" in html


def test_render_chart_html_neutralizes_script_breakout():
    # A title containing "</script>" must not be able to close the embedded
    # <script> block. The template itself has exactly two </script> tags (the
    # CDN tag and the inline tag); injected data must add zero more.
    baseline = charts.render_chart_html(charts.candlestick_with_volume(_candles(), "ok"))
    assert baseline.count("</script>") == 2

    evil = charts.render_chart_html(
        charts.candlestick_with_volume(_candles(), "</script><script>alert(1)</script>")
    )
    assert evil.count("</script>") == 2  # the data injected no extra closing tags

    # The visible chart title is HTML-escaped, so "<" never reaches the page
    # as markup.
    titled = charts.render_chart_html(charts.candlestick_with_volume(_candles(), "<b>hi</b>"))
    assert "&lt;b&gt;hi&lt;/b&gt;" in titled


def _trending_candles() -> pd.DataFrame:
    """Candles that fall then rise — forcing at least one SuperTrend flip."""
    close = np.concatenate([np.linspace(200.0, 130.0, 50), np.linspace(130.0, 210.0, 50)])
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": [1000.0] * len(close),
        }
    )


def test_supertrend_overlay_draws_one_clean_line_not_a_channel():
    # SuperTrend is a single line that flips sides; it must NOT render as two
    # overlapping full-width lines (a "channel"). Lightweight Charts connects
    # its line straight across any whitespace gap, so every SuperTrend line
    # series must be gap-free — i.e. each contiguous trend run is its own
    # series with no whitespace points.
    candles = _trending_candles()
    spec = charts.candlestick_with_volume(candles, "Title")
    charts.add_supertrend_overlay(spec, candles, atr_period=10, multiplier=2.0)

    line_series = [s for s in spec["panes"][0]["series"] if s["kind"] == "line"]
    assert line_series, "expected at least one SuperTrend line series"
    for series in line_series:
        assert all("value" in point for point in series["data"]), (
            "a SuperTrend series contains whitespace points; Lightweight Charts "
            "would draw a full-width connector across them (the channel bug)"
        )
        # Each run-series is drawn in a single trend color.
        assert series["options"]["color"] in (
            charts._COLORS["supertrend_up"],
            charts._COLORS["supertrend_down"],
        )


def test_each_screener_build_chart_returns_serializable_spec():
    candles = _candles(260)
    expectations = [
        (heikin_ashi_supertrend, 2),
        (bollinger_band_reversal, 2),
        (bollinger_lower_band, 2),
        (envelope, 2),
        (envelope_knoxville_buy, 2),
        (green_candles_20pct_up, 2),
        (technical_analysis, 2),
        (stochastic_swing, 3),
    ]
    for module, expected_panes in expectations:
        spec = module.build_chart(candles, dict(module.SCREENER["default_params"]))
        # The spec must be plain JSON-serializable data (no NaN, no objects).
        json.dumps(spec, allow_nan=False)
        assert len(spec["panes"]) == expected_panes
        # And it must render to HTML without error.
        assert "createChart" in charts.render_chart_html(spec)


def test_empty_candles_return_minimal_renderable_spec():
    # An empty DataFrame (no columns) must not raise; it yields a minimal spec.
    spec = charts.candlestick_with_volume(pd.DataFrame(), "Title")
    assert "panes" in spec
    # And that minimal spec still renders to HTML.
    assert "createChart" in charts.render_chart_html(spec)
