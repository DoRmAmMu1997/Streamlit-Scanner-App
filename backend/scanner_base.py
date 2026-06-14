"""Shared base class for every screener in this app.

What this module gives a new screener:

1. **A common contract** — a `BaseScanner` subclass only needs to override
   `SCREENER`, `EXTRA_RESULT_COLUMNS`, and `compute_signal(...)`. Everything
   else (data fetching, per-symbol loop, progress callback, empty result
   shape, exception capture) is inherited from this file.

2. **A normalized result schema** — every screener returns at least
   `["symbol", "rating", "signal_date", "close", "reason"]`. The UI can rely
   on these columns to render emoji BUY/SELL badges, build chart titles,
   and group rows consistently. Screeners append their own extra columns
   via `EXTRA_RESULT_COLUMNS`.

3. **Tiny utility helpers** so screeners do not repeat boilerplate:
   - `prepare_candles(candles)` — same cleaning logic as
     `backend.indicators.prepare_ohlc`, exposed as an instance method.
   - `coerce_param(params, key, cast)` — read a value from `params` with
     fallback to the screener's `default_params`, with type coercion.
   - `empty_result()` — return an empty DataFrame with the right columns so
     "no signals today" still renders cleanly.

Beginner note on Abstract Base Classes (ABC):
- `BaseScanner` declares `compute_signal(...)` as `@abstractmethod`. Python
  will refuse to instantiate a subclass that does not override it, which
  catches "I forgot to implement the strategy" mistakes at import time
  instead of mysterious "no rows ever shortlist" mistakes at runtime.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, ClassVar, get_args

import pandas as pd

from backend.indicators import prepare_ohlc
from backend.scanning.result_contract import (
    AIProvenance,
    ResultContractError,
    SignalSource,
    normalize_indicator_values,
    normalize_screener_row,
    normalize_secret_safe_json,
)
from backend.security import redact_text

logger = logging.getLogger(__name__)


# Every screener result has these columns first, in this order. Streamlit's
# emoji-badge logic, chart symbol pick, and download-CSV all expect them.
COMMON_RESULT_COLUMNS: list[str] = [
    "symbol",
    "rating",
    "signal_date",
    "close",
    "reason",
]

# PROV-002: every screener frame ends with a reserved column holding a per-row
# provenance dict ("why this stock passed"). It is appended after each
# screener's own extras so the leading/common columns the UI relies on keep
# their meaning, and so golden-snapshot diffs show provenance trailing each row.
PROVENANCE_COLUMN: str = "provenance"

# The single source of truth for the provenance ``source`` categories is the
# typed contract; deriving the runtime set from that ``Literal`` avoids a second
# vocabulary that could drift from the normalizer's own validation.
_VALID_SOURCES: frozenset[str] = frozenset(get_args(SignalSource))


class BaseScanner(ABC):
    """Abstract base for every screener.

    A subclass MUST set:
      - `SCREENER`: metadata dict picked up by `backend.screener_registry`.
      - `compute_signal(self, symbol, candles, params)`: the strategy rule.

    A subclass MAY set:
      - `EXTRA_RESULT_COLUMNS`: list of additional columns appended to the
        common schema. Order is preserved in the result DataFrame.
      - `build_chart(self, candles, params)`: returns a Lightweight Charts
        spec dict, or `None` to hide the chart section in the UI. The
        default implementation returns `None`.
    """

    # Subclasses overwrite these. The base class uses placeholder values so
    # static type checkers do not complain; the registry validates real
    # values are present at discovery time.
    SCREENER: ClassVar[dict] = {}
    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = []

    # PROV-002: bump this when a strategy's rule logic changes so historical
    # provenance stays interpretable. It is stamped onto every row the screener
    # emits via ``build_provenance`` below.
    SCREENER_VERSION: ClassVar[str] = "1.0.0"

    # ------------------------------------------------------------------
    # Public helpers used by the UI and the registry
    # ------------------------------------------------------------------

    @property
    def result_columns(self) -> list[str]:
        """Common columns first, the screener's extras, then ``provenance`` last."""
        # Deduplicate while preserving order. A screener that accidentally
        # lists "symbol" again in EXTRA_RESULT_COLUMNS should not produce a
        # duplicate column in the output DataFrame.
        seen = set()
        ordered: list[str] = []
        for column in (*COMMON_RESULT_COLUMNS, *self.EXTRA_RESULT_COLUMNS, PROVENANCE_COLUMN):
            if column in seen:
                continue
            seen.add(column)
            ordered.append(column)
        return ordered

    def empty_result(self) -> pd.DataFrame:
        """Return a properly shaped empty DataFrame for the Streamlit UI."""
        return pd.DataFrame([], columns=self.result_columns)

    def build_result_frame(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        compute_failure_callback=None,
    ) -> pd.DataFrame:
        """Validate emitted shortlist rows and return the stable result schema.

        AI screeners that currently override ``run`` can adopt this helper in a
        later task. BaseScanner itself uses it for both streaming and batch paths,
        so every row emitted through the shared template already satisfies the
        strict provenance contract.
        """
        screener_key = str(self.SCREENER.get("key", type(self).__name__))
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            candidate = dict(row)
            try:
                normalized = normalize_screener_row(
                    candidate,
                    screener_key=screener_key,
                )
            except ResultContractError as exc:
                safe_message = redact_text(
                    f"Result contract rejected emitted row: {exc}"
                )
                symbol = redact_text(str(candidate.get("symbol") or "UNKNOWN"))
                logger.warning(
                    "%s rejected emitted row for %s: %s",
                    type(self).__name__,
                    symbol,
                    safe_message,
                )
                if callable(compute_failure_callback):
                    compute_failure_callback(
                        {
                            "symbol": symbol,
                            "scanner": type(self).__name__,
                            "message": safe_message,
                            "phase": "result_contract",
                        }
                    )
                continue
            normalized_rows.append(normalized)
        return pd.DataFrame(normalized_rows, columns=self.result_columns)

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict | None:
        """Render a per-stock chart spec. Default: no chart."""
        # Returning None tells the Streamlit UI to hide the chart pane for
        # this screener. Subclasses that DO want a chart override this.
        return None

    # ------------------------------------------------------------------
    # Helpers shared with every concrete strategy
    # ------------------------------------------------------------------

    def prepare_candles(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Sort/clean/numeric-coerce candles before any indicator math.

        Delegates to `backend.indicators.prepare_ohlc` so there is exactly
        one definition of "ready for math" across the whole app.
        """
        return prepare_ohlc(candles)

    def coerce_param(self, params: dict, key: str, cast: Callable[[Any], Any] = float) -> Any:
        """Read a parameter from `params` with fallback to the screener's defaults.

        Example:
            bb_period = self.coerce_param(params, "bb_period", int)

        Why this helper exists: every screener used to repeat
        `int(params.get("bb_period", SCREENER["default_params"]["bb_period"]))`
        for each parameter. This one-liner is shorter and harder to mistype.
        """
        defaults = self.SCREENER.get("default_params", {})
        if key not in defaults and key not in params:
            raise KeyError(
                f"{type(self).__name__}: parameter '{key}' is not in params "
                f"and has no default in SCREENER['default_params']"
            )
        return cast(params.get(key, defaults.get(key)))

    def build_provenance(
        self,
        *,
        triggered_rules: Sequence[str],
        indicator_values: Mapping[str, Any],
        source: SignalSource = "deterministic",
        notes: str | None = None,
        ai: AIProvenance | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the per-row provenance dict for the reserved ``provenance`` column.

        PROV-002: a screener states *why* a row shortlisted — the rule names that
        fired and the indicator values behind them. This helper stamps the
        screener's identity (``screener_key`` from ``SCREENER`` and
        ``SCREENER_VERSION``) so each ``compute_signal`` only declares its own
        rules, and converts indicator values to plain JSON-safe scalars so the
        raw result frame stays serializable (see ``_plain_scalar``).

        ``params_snapshot`` and ``data_snapshot_date`` are intentionally left out:
        the persistence normalizer fills those from run-level context, and keeping
        them off the row keeps golden snapshots free of run-date-dependent values.
        ``source`` defaults to "deterministic" because this helper is for
        rule-based screeners; AI-assisted screeners pass "ai" or "hybrid".
        """
        if source not in _VALID_SOURCES:
            allowed = ", ".join(sorted(_VALID_SOURCES))
            raise ValueError(
                f"build_provenance source must be one of: {allowed}; got {source!r}."
            )
        provenance: dict[str, Any] = {
            "screener_key": str(self.SCREENER.get("key", type(self).__name__)),
            "screener_version": self.SCREENER_VERSION,
            "triggered_rules": [str(rule) for rule in triggered_rules],
            "indicator_values": normalize_indicator_values(indicator_values),
            "source": source,
        }
        if not provenance["triggered_rules"] or any(
            not rule.strip() for rule in provenance["triggered_rules"]
        ):
            raise ResultContractError(
                "build_provenance requires non-empty triggered_rules."
            )
        if notes is not None:
            # Defense in depth: a stray token in a hand-written note must not
            # become durable scan history. redact_text is the app-wide masker.
            provenance["notes"] = redact_text(notes)
        if ai is not None:
            raw_ai: Any = (
                asdict(ai)
                if is_dataclass(ai) and not isinstance(ai, type)
                else ai
            )
            normalized_ai = normalize_secret_safe_json(raw_ai)
            if not isinstance(normalized_ai, dict):
                raise ResultContractError(
                    "build_provenance AI receipt must normalize to a mapping."
                )
            provenance["ai"] = normalized_ai
        return provenance

    # ------------------------------------------------------------------
    # The strategy hook every subclass must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a result row for `symbol`, or `None` when the strategy says skip.

        Implementations should:
          - Call `self.prepare_candles(candles)` once.
          - Run indicator math on the prepared frame.
          - Return a dict whose keys match `self.result_columns`, OR `None`.
        """
        raise NotImplementedError

    def _compute_signal_safely(
        self,
        symbol: str,
        candles: pd.DataFrame,
        params: dict,
        compute_failure_callback,
    ) -> dict | None:
        """Run one symbol while keeping the wider scan alive.

        Beginner note: screeners scan many symbols. If indicator math fails for
        one malformed candle frame, the user still deserves results for every
        other symbol. The streaming path uses this helper to match the legacy
        batch fallback's "log, report, continue" behavior.
        """
        try:
            return self.compute_signal(symbol, candles, params)
        except Exception as exc:  # noqa: BLE001 - scan resiliency is intentional here
            # Per-symbol errors can be displayed in the UI's run details. Redact
            # once at the capture boundary, then reuse the safe text for both
            # logging and the callback payload.
            safe_message = redact_text(str(exc))
            logger.warning(
                "%s.compute_signal failed for %s: %s",
                type(self).__name__,
                symbol,
                safe_message,
            )
            if callable(compute_failure_callback):
                # The UI can show this concise row in "Run details" while the
                # logger keeps the traceback for developers. This turns "empty
                # shortlist because every compute failed" into an explainable
                # run instead of a mystery.
                compute_failure_callback(
                    {
                        "symbol": symbol,
                        "scanner": type(self).__name__,
                        "message": safe_message,
                    }
                )
            return None

    # ------------------------------------------------------------------
    # Template method: this is what the Streamlit UI calls
    # ------------------------------------------------------------------

    def run(
        self,
        universe_df: pd.DataFrame,
        data_loader,
        params: dict,
    ) -> pd.DataFrame:
        """Fetch candles, evaluate every symbol, return a result DataFrame.

        Subclasses should rarely override this. The structure (one row per
        BUY/SELL signal, empty DataFrame when no symbol matches) is fixed
        so the UI does not need per-screener special cases.
        """
        rows: list[dict] = []
        compute_failure_callback = params.get("compute_failure_callback")

        iter_history = getattr(data_loader, "iter_universe_history", None)
        if callable(iter_history):
            # Streaming is the preferred path for large universes: the loader
            # yields one symbol at a time, and the screener computes the result
            # immediately instead of first building a huge `{symbol: candles}`
            # dictionary. Older loaders still use the batch fallback below.
            for item in iter_history(
                universe_df=universe_df,
                start_date=params["start_date"],
                end_date=params["end_date"],
                max_symbols=params.get("max_symbols"),
                force_refresh=bool(params.get("force_refresh", False)),
                progress_callback=params.get("progress_callback"),
            ):
                if getattr(item, "failure", None) is not None:
                    # Loader failures are already stored on the loader for the
                    # Streamlit status panel. The scanner skips failed symbols
                    # and keeps working through the rest of the stream.
                    continue
                signal = self._compute_signal_safely(
                    getattr(item, "symbol", "UNKNOWN"),
                    getattr(item, "candles", pd.DataFrame()),
                    params,
                    compute_failure_callback,
                )
                if signal is not None:
                    rows.append(signal)
            return self.build_result_frame(
                rows,
                compute_failure_callback=compute_failure_callback,
            )

        # Centralized data fetching: every screener uses the same loader
        # contract. The loader handles caching, rate limits, and failures.
        batch = data_loader.load_universe_history(
            universe_df=universe_df,
            start_date=params["start_date"],
            end_date=params["end_date"],
            max_symbols=params.get("max_symbols"),
            force_refresh=bool(params.get("force_refresh", False)),
            progress_callback=params.get("progress_callback"),
        )

        for symbol, candles in batch.frames.items():
            try:
                signal = self.compute_signal(symbol, candles, params)
            except Exception as exc:  # noqa: BLE001 — we log and continue intentionally
                # Match the streaming path above: convert raw exception text to
                # a UI/log-safe message before it leaves this catch block.
                safe_message = redact_text(str(exc))
                # A bad candle frame for ONE symbol should not abort the
                # whole scan. The warning is enough for diagnosis in DEBUG
                # mode without breaking the user's overall workflow.
                logger.warning(
                    "%s.compute_signal failed for %s: %s",
                    type(self).__name__,
                    symbol,
                    safe_message,
                )
                if callable(compute_failure_callback):
                    # The UI can show this concise row in "Run details" while
                    # the logger keeps the traceback for developers. This turns
                    # "empty shortlist because every compute failed" into an
                    # explainable run instead of a mystery.
                    compute_failure_callback(
                        {
                            "symbol": symbol,
                            "scanner": type(self).__name__,
                            "message": safe_message,
                        }
                    )
                continue
            if signal is not None:
                rows.append(signal)

        # Returning with the fixed column order keeps the Streamlit table
        # stable even when no row matches today.
        return self.build_result_frame(
            rows,
            compute_failure_callback=compute_failure_callback,
        )


def export_module_compat(scanner: BaseScanner) -> dict[str, Any]:
    """Bundle module-level back-compat aliases for an existing screener test suite.

    Older tests import a screener as a module and call `module.run(...)`,
    `module.SCREENER`, `module.RESULT_COLUMNS`. New screeners are classes,
    so each module exposes those names via this helper:

        _scanner = MyScanner()
        SCREENER = _scanner.SCREENER
        RESULT_COLUMNS = _scanner.result_columns
        run = _scanner.run
        build_chart = _scanner.build_chart

    Returning a dict here just documents the convention; modules still
    bind the names explicitly so the names are obvious to readers.
    """
    return {
        "SCREENER": scanner.SCREENER,
        "RESULT_COLUMNS": scanner.result_columns,
        "run": scanner.run,
        "build_chart": scanner.build_chart,
    }
