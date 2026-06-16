# Adding a screener: the full walkthrough

The README shows the minimal skeleton; this page is the complete path from
idea to merged screener, including the parts that are easy to miss (golden
tests, chart hooks, and what the registry validates).

A screener is one Python file in `screeners/` containing a subclass of
[`BaseScanner`](../backend/scanner_base.py). Discovery is automatic - no
registration list to edit, no imports to add anywhere else.

---

## 1. The contract

```python
from __future__ import annotations

from typing import ClassVar

import pandas as pd

from backend.scanner_base import BaseScanner


class MyScanner(BaseScanner):
    """One sentence on what the strategy looks for."""

    SCREENER: ClassVar[dict] = {
        "key": "my_screener",              # stable id; used in scan history
        "name": "My Screener",             # UI display name
        "description": "What a user sees in the sidebar.",
        "universe": "nifty_500",           # a key from UNIVERSE_CONFIG
        "timeframe": "daily",
        "lookback_days": 80,               # candles your math needs
        "default_params": {"period": 20},  # user-tunable via the sidebar
    }

    # Extra columns appended after the common ones (symbol, rating,
    # signal_date, close, reason). Order is preserved in the results table.
    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = ["my_metric"]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a result row dict for a hit, or None for no signal."""
        period = self.coerce_param(params, "period", int)
        frame = self.prepare_candles(candles)
        if len(frame) < period:
            return None
        # ... strategy math on frame ...
        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": frame["timestamp"].iloc[-1].date(),
            "close": float(frame["close"].iloc[-1]),
            "reason": "Plain-English why, shown in the UI and stored in history.",
            "my_metric": 1.23,
            # PROV-002: record *why* the stock shortlisted. `build_provenance`
            # stamps the screener key/version and is folded into the persisted
            # `provenance_json`. Every built-in screener provides this; the
            # reserved `provenance` column is appended automatically (do not list
            # it in EXTRA_RESULT_COLUMNS).
            "provenance": self.build_provenance(
                triggered_rules=["my_rule_fired"],
                indicator_values={
                    "my_metric": 1.23,
                    "close": float(frame["close"].iloc[-1]),
                },
                # source defaults to "deterministic"; AI/hybrid screeners pass "ai"/"hybrid".
            ),
        }
```

Provenance is technically optional at the persistence boundary (a row without it
gets conservative empty defaults), but **PROV-002 expects every screener to
supply it** so the history page can answer "why was this shortlisted?" —
`build_provenance` requires non-empty `triggered_rules`, so it fails loudly if you
forget the rule names.

What the base class gives you for free: the per-symbol candle loop, progress
reporting, per-symbol failure isolation (one bad stock cannot kill the scan -
failures are reported via the SCAN-003 compute-failure callback and the run is
marked PARTIAL), column normalization, and an empty-but-correctly-shaped
DataFrame when nothing matches.

What the registry validates at discovery time (`backend/screener_registry.py`):
`SCREENER` has the required keys, `key` is unique, and `run`'s signature
matches. The registry does not validate the universe key; choose a key from
`backend.universe_builder.UNIVERSE_CONFIG` so the loader can resolve it. A
broken screener file produces a clear sidebar error instead of taking down the
app.

## 2. Annotate class attributes with ClassVar

`SCREENER` and `EXTRA_RESULT_COLUMNS` are class-level constants; annotate them
`ClassVar[...]` as above or ruff (RUF012) will flag them as mutable class
defaults.

## 3. Optional: a chart

Implement `build_chart` only if the strategy benefits from a visual; return
`None`-never-implemented hides the chart pane entirely:

```python
    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict | None:
        from backend.charts import add_bollinger_overlay, candlestick_with_volume

        spec = candlestick_with_volume(candles, title="My Screener")
        add_bollinger_overlay(
            spec,
            candles,
            period=int(params.get("period", 20)),
            std_multiplier=float(params.get("std_multiplier", 2.0)),
        )
        return spec
```

Chart specs are plain dicts rendered by `backend/charts.py` (TradingView
Lightweight Charts). Browse the existing screeners for overlay examples:
envelope, Bollinger, SuperTrend, stochastic.

## 4. Tests: unit + golden

Two layers, both expected for a merged screener:

1. **Unit test** (`tests/test_real_screeners.py` has many examples): build a
   small candle frame that should and should not trigger, run the screener
   through a fake loader, assert on the rows.
2. **Golden snapshot** (`tests/test_screener_golden_outputs.py`, TEST-001):
   import the screener module, build deterministic candle fixtures and params,
   and append a `GoldenCase` to `_golden_cases()`. The first run with
   `UPDATE_GOLDEN=1` writes its reference JSON under `tests/golden/screeners/`;
   review that generated diff before committing it. Afterwards CI fails if the
   screener's output drifts unexpectedly. This is what lets refactors touch
   shared indicator code with confidence.

```bash
UPDATE_GOLDEN=1 python -m pytest tests/test_screener_golden_outputs.py   # once
python -m pytest -q                                                      # always
```

## 5. The pre-merge checklist

```bash
python -m pre_commit validate-config .pre-commit-config.yaml
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
docker build --tag streamlit-scanner-app:ci .
docker compose config
docker compose up --build --wait --wait-timeout 180
docker compose down --volumes --remove-orphans
```

These are the exact CI gates. If you forked your branch a while ago, merge
current `main` first - gates added after your fork still apply to your new
code (see docs/operations.md, "CI gates and the multi-branch workflow").

## 6. Things that bite people

- **Decimal vs float**: result rows may carry `Decimal` for money fields; the
  storage layer serializes them losslessly. Do indicator math in float, format
  at the edges.
- **NaN warm-up rows**: indicators normally contain NaNs in their warm-up
  prefix. First enforce the strategy's minimum history length, then check only
  the latest indicator values used by the decision, for example
  `if bands[["upper", "lower"]].iloc[-1].isna().any(): return None`.
- **Don't fetch data yourself**: the loader injected into `run` is the only
  sanctioned data path - it owns caching, rate limits, and failure
  bookkeeping. A screener that opens its own HTTP connections will bypass the
  circuit breaker and the redaction layer.
- **Keys are forever**: `SCREENER["key"]` is stored in every scan-history row.
  Renaming it orphans history, so pick a good one the first time.
