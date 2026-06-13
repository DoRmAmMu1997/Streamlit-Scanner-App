"""Typed, backward-compatible screener result normalization for PROV-001A.

Existing screeners return plain dictionaries collected into pandas DataFrames.
That flexibility is useful because each strategy has different indicators, but
it also means persistence previously had no standard description of *why* a row
was shortlisted. This module introduces a small domain contract without forcing
every screener to change at once.

The public dataclasses document the target shape. ``normalize_screener_row`` is
the compatibility boundary: it accepts a legacy mapping, builds a separate
JSON-safe copy, and adds canonical ``provenance_json`` fields. The original row
is never mutated, so Streamlit can continue rendering the exact DataFrame the
screener returned.

Beginner mental model:

- a *result row* says what was found, such as the symbol, rating, and price;
- *provenance* says how or why it was found, such as the rules and indicator
  values that triggered;
- ``raw_result_json`` keeps the complete normalized row for auditing, while the
  separate ``provenance_json`` database column makes the explanation envelope
  easy to find and evolve.

Security note:
Scan history is durable. A token accidentally placed in a parameter, result
field, or provider message must not become a long-lived database secret.
Normalization therefore masks credential-named mapping keys and sends every
string through the application's shared redaction helper before persistence.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from typing import Any, Literal, TypeAlias, cast

from backend.security import MASK, is_secret_key_name, redact_text

# JSON has fewer built-in value types than Python. These aliases make that
# boundary visible in type hints: a scalar cannot contain another collection,
# while a JSONValue may recursively contain lists and string-keyed objects.
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
SignalSource: TypeAlias = Literal["deterministic", "ai", "hybrid"]

# Exporting a domain-specific name keeps callers/tests independent from the
# redaction module's implementation while still using its consistent mask.
MASKED_PARAMETER = MASK

_SIGNAL_SOURCES = frozenset({"deterministic", "ai", "hybrid"})


class ResultContractError(ValueError):
    """Raised when a row cannot satisfy the minimal persisted result contract.

    This is intentionally narrower than a general serialization error. Legacy
    rows may omit optional fields, but persistence cannot safely identify a row
    without ``symbol`` and must not accept an invented provenance source.
    """


@dataclass(frozen=True)
class RuleCheck:
    """One named rule that contributed to a screener's result.

    ``passed`` and ``detail`` are optional so PROV-002 can begin with a simple
    list of rule names, then add structured evidence only where it is useful.
    """

    name: str
    passed: bool | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AIProvenance:
    """Reserved metadata for a later AI-provenance ticket.

    PROV-001A deliberately does not inspect prompts, model output, scraped
    evidence, or agent behavior. These two optional identifiers merely reserve
    a typed location that PROV-003 can expand without changing the outer result
    contract.
    """

    model_name: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class SignalProvenance:
    """Machine-readable receipts explaining how one signal was produced.

    The fields are deliberately small and strategy-neutral:

    - ``triggered_rules`` records named checks, optionally with pass/detail data;
    - ``indicator_values`` stores the market measurements behind those checks;
    - ``params_snapshot`` records the non-secret settings used for the run;
    - ``data_snapshot_date`` identifies how current the input data was;
    - ``source`` distinguishes deterministic, AI, and combined workflows.

    Most fields have empty defaults so existing screeners can adopt the
    contract gradually instead of all being rewritten in one release.
    """

    screener_key: str
    screener_version: str | None = None
    triggered_rules: list[str | RuleCheck] = field(default_factory=list)
    indicator_values: Mapping[str, JSONScalar] = field(default_factory=dict)
    params_snapshot: Mapping[str, JSONValue] = field(default_factory=dict)
    data_snapshot_date: dt.date | None = None
    source: SignalSource | None = None
    notes: str | None = None
    ai: AIProvenance | None = None


@dataclass(frozen=True)
class ScreenerResult:
    """Typed view of the common fields shared by current screener rows.

    Only ``symbol`` is required at the normalization boundary. The other common
    fields stay optional because historical and AI-assisted screeners do not
    always produce every value. ``close_price`` is the domain name used by the
    database; legacy input rows may continue using ``close``.
    """

    symbol: str
    rating: str | None = None
    signal_date: dt.date | dt.datetime | str | None = None
    close_price: Decimal | int | float | str | None = None
    reason: str | None = None
    # The composite rank score from a future RANK-* ticket. The database column
    # and repository mapping already exist; this keeps the typed contract aligned
    # with the documented PROV-001 shape. It stays ``None`` until ranking lands.
    final_score: Decimal | int | float | str | None = None
    provenance: SignalProvenance | None = None


def normalize_screener_row(
    row: Mapping[str, Any],
    *,
    screener_key: str,
    params: Mapping[str, Any] | None = None,
    data_snapshot_date: dt.date | None = None,
) -> dict[str, Any]:
    """Return a secret-safe, JSON-safe copy with canonical signal provenance.

    Args:
        row: One mapping produced by a screener. It may use legacy fields and
            may contain pandas, NumPy, date, or Decimal values.
        screener_key: Stable registry key for the screener that produced the
            row. This fills the canonical receipt even when the row itself does
            not contain provenance yet.
        params: Original scan parameters. Callables are omitted and
            credential-like values are masked before they enter the receipt.
        data_snapshot_date: Date represented by the market-data snapshot, often
            the scan's ``end_date``.

    Returns:
        A new dictionary suitable for strict JSON persistence. The input
        mapping and all nested values remain untouched.

    Raises:
        ResultContractError: If ``symbol`` is missing/blank or if an explicit
            provenance ``source`` is outside the three supported categories.

    Compatibility rules:

    - every original top-level field is retained, although secret values are
      masked and non-JSON Python values are converted;
    - ``provenance_json`` wins over legacy ``provenance`` when both are present;
    - unknown provenance keys are retained so a newer or screener-specific
      receipt is not discarded by this shared layer;
    - legacy ``rules`` is copied into canonical ``triggered_rules`` while the
      original ``rules`` key remains available;
    - absent provenance fields receive conservative defaults. In particular,
      ``source`` stays ``None`` because this layer cannot reliably infer whether
      an arbitrary legacy screener is deterministic, AI-based, or hybrid.

    ``ResultContractError`` is raised only for an unusable symbol or an explicit
    invalid provenance source. Optional common fields remain optional so this
    first contract can be adopted without updating every screener.
    """
    if not isinstance(row, Mapping):
        raise ResultContractError("Screener result row must be a mapping.")

    # Symbol is the only hard requirement because it is how a persisted result
    # is identified in history. Calling ``str`` also accepts symbol-like scalar
    # values while still rejecting None, NaN, NaT, and whitespace-only text.
    symbol = row.get("symbol")
    if _is_missing(symbol) or not str(symbol).strip():
        raise ResultContractError(
            "Screener result row requires a non-blank 'symbol'."
        )

    # Build a wholly separate JSON-safe tree before adding canonical fields.
    # This is the key immutability guarantee: editing the persistence payload
    # later cannot change the DataFrame or nested dictionaries held by the UI.
    normalized = _json_safe_mapping(row)
    normalized["symbol"] = str(symbol).strip()

    # A DataFrame with mixed rows commonly fills an absent dictionary cell with
    # NumPy NaN. Treat all missing-like values as absent so a valid legacy
    # ``provenance`` object still becomes the fallback.
    raw_provenance = row.get("provenance_json")
    if _is_missing(raw_provenance):
        raw_provenance = row.get("provenance")
    provenance = _provenance_mapping(raw_provenance)

    # Do not guess the source. A legacy row may come from a deterministic,
    # AI-assisted, or hybrid screener, and a wrong label would make the audit
    # record less trustworthy than leaving the value unknown.
    raw_source = provenance.get("source")
    source = _normalize_source(raw_source)

    # Earlier screeners used ``rules``. Copy those entries into the canonical
    # field while retaining the old key in ``canonical`` below.
    raw_rules = provenance.get("triggered_rules")
    if raw_rules is None:
        raw_rules = provenance.get("rules", [])

    # Indicator values are scalar in the v1 contract. The helper below converts
    # NumPy and pandas scalar objects without importing either dependency here.
    raw_indicators = provenance.get("indicator_values")
    # Declared as the wider JSON type: ``dict`` is invariant in its value type,
    # so a ``dict[str, JSONScalar]`` would not be assignable into the canonical
    # ``dict[str, JSONValue]`` envelope below even though every scalar fits.
    indicator_values: dict[str, JSONValue] = (
        {
            str(key): _json_safe_scalar(value)
            for key, value in raw_indicators.items()
        }
        if isinstance(raw_indicators, Mapping)
        else {}
    )

    # An existing snapshot is authoritative because the screener may have
    # recorded more precise settings. Otherwise use the run-level parameters
    # supplied by the service, dropping callbacks and masking credentials.
    raw_params = provenance.get("params_snapshot")
    params_snapshot = (
        _json_safe_mapping(raw_params, drop_callables=True)
        if isinstance(raw_params, Mapping)
        else _json_safe_mapping(params or {}, drop_callables=True)
    )

    # Likewise, keep a row-specific date when present and use the scan date only
    # as the compatibility default.
    raw_data_date = provenance.get("data_snapshot_date")
    if _is_missing(raw_data_date):
        raw_data_date = data_snapshot_date

    # Start with a safe copy of every existing provenance field, then overlay
    # the canonical contract. This preserves unknown receipts while ensuring the
    # standard keys always have one predictable representation.
    canonical = _json_safe_mapping(provenance)
    canonical.update(
        {
            "screener_key": str(screener_key),
            "screener_version": _optional_text(
                provenance.get("screener_version")
            ),
            "triggered_rules": _normalize_rules(raw_rules),
            "indicator_values": indicator_values,
            "params_snapshot": params_snapshot,
            "data_snapshot_date": _json_safe(raw_data_date),
            "source": source,
            "notes": _optional_text(provenance.get("notes")),
            "ai": _normalize_ai_placeholder(provenance.get("ai")),
        }
    )
    normalized["provenance_json"] = canonical
    return cast(dict[str, Any], normalized)


def _provenance_mapping(value: Any) -> dict[str, Any]:
    """Copy a provenance object into a mapping without rejecting legacy values.

    Dataclasses are converted with ``asdict`` so callers can already use the new
    typed models. Plain mappings continue to support old screeners, and unusual
    scalar receipts are retained under ``legacy_value`` rather than causing the
    whole scan-history write to fail.
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)

    # An old or experimental screener may have stored a plain text receipt.
    # Preserve it under an explicit compatibility key instead of crashing the
    # entire scan or pretending that it follows the new structure.
    return {"legacy_value": value}


def _normalize_rules(value: Any) -> list[JSONValue]:
    """Return rule names/checks as one JSON list while preserving useful detail.

    A single string, one ``RuleCheck``, or an iterable of either are all
    accepted. Normalizing them to a list gives readers one predictable shape in
    the database without forcing legacy producers to change immediately.
    """
    if _is_missing(value):
        return []
    if isinstance(value, str | Mapping) or (
        is_dataclass(value) and not isinstance(value, type)
    ):
        items = [value]
    elif isinstance(value, list | tuple | set):
        items = list(value)
    else:
        items = [value]
    return [_json_safe(item) for item in items]


def _normalize_source(value: Any) -> SignalSource | None:
    """Validate an explicitly supplied provenance source.

    Missing values remain ``None``. Explicit values are normalized for case and
    whitespace, but an unknown label raises instead of silently rewriting audit
    evidence into one of the supported categories.
    """
    if _is_missing(value):
        return None
    normalized = str(value).strip().lower()
    if normalized not in _SIGNAL_SOURCES:
        allowed = ", ".join(sorted(_SIGNAL_SOURCES))
        raise ResultContractError(
            f"Provenance source must be one of: {allowed}."
        )
    return cast(SignalSource, normalized)


def _normalize_ai_placeholder(value: Any) -> dict[str, JSONValue] | None:
    """Keep optional AI identifiers without implementing AI evidence handling."""
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_mapping(asdict(value))
    return {"legacy_value": _json_safe(value)}


def _json_safe_mapping(
    value: Mapping[Any, Any],
    *,
    drop_callables: bool = False,
) -> dict[str, JSONValue]:
    """Recursively convert a mapping and mask credential-named values.

    JSON object keys must be strings, so non-string keys are converted with
    ``str``. Secret-looking keys are masked before their values are inspected,
    which prevents an accidental token from surviving in a custom object.
    """
    converted: dict[str, JSONValue] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if drop_callables and callable(raw_value):
            continue
        if is_secret_key_name(key):
            converted[key] = MASKED_PARAMETER
            continue
        converted[key] = _json_safe(raw_value, drop_callables=drop_callables)
    return converted


def _json_safe(value: Any, *, drop_callables: bool = False) -> JSONValue:
    """Convert common pandas/NumPy/Python values to strict JSON-compatible data.

    ``json.dumps(..., allow_nan=False)`` rejects NaN and infinity even though
    pandas and NumPy use them frequently. Converting those values to ``None``
    keeps the stored JSON standards-compliant and gives readers a conventional
    representation for missing data.
    """
    if _is_missing(value):
        return None
    if callable(value):
        return None
    if isinstance(value, str):
        return cast(str, redact_text(value))
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_mapping(asdict(value), drop_callables=drop_callables)
    if isinstance(value, Mapping):
        return _json_safe_mapping(value, drop_callables=drop_callables)
    if isinstance(value, list | tuple):
        return [
            _json_safe(item, drop_callables=drop_callables)
            for item in value
            if not (drop_callables and callable(item))
        ]
    if isinstance(value, set | frozenset):
        items = [
            _json_safe(item, drop_callables=drop_callables)
            for item in value
            if not (drop_callables and callable(item))
        ]
        return sorted(items, key=_json_sort_key)

    # NumPy and pandas scalar objects expose ``item()``. Converting through that
    # method avoids importing NumPy into the runtime module solely for type
    # checks, while recursive handling still catches NaN and infinity.
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return _json_safe(item_method(), drop_callables=drop_callables)
        except (TypeError, ValueError):
            pass

    # Flexible raw rows occasionally contain custom scalar-like values. Keeping
    # a redacted text representation is more useful than aborting a full scan.
    return cast(str, redact_text(str(value)))


def _json_safe_scalar(value: Any) -> JSONScalar:
    """Convert an indicator value while keeping the canonical field scalar-only."""
    converted = _json_safe(value)
    if isinstance(converted, dict | list):
        # Nested indicator structures do not fit the v1 contract. Serialize them
        # as stable JSON text instead of silently dropping evidence.
        return json.dumps(converted, sort_keys=True, separators=(",", ":"))
    return converted


def _optional_text(value: Any) -> str | None:
    """Return a redacted string for an optional provenance display field."""
    if _is_missing(value):
        return None
    return cast(str, redact_text(str(value)))


def _is_missing(value: Any) -> bool:
    """Recognize scalar missing/non-finite values without importing pandas.

    Pandas/NumPy missing values do not all share one class. The helper handles
    ordinary ``None`` and non-finite numbers first, recognizes pandas sentinel
    class names, and finally uses the standard NaN property that a value is
    unequal to itself. Collection types are excluded because their comparisons
    can produce arrays instead of one Boolean answer.
    """
    if value is None:
        return True
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, float):
        return not math.isfinite(value)
    if type(value).__name__ in {"NAType", "NaTType"}:
        return True
    if isinstance(value, str | bytes | Mapping | list | tuple | set | frozenset):
        return False
    try:
        comparison = value != value
        if isinstance(comparison, bool):
            return comparison
        item_method = getattr(comparison, "item", None)
        if callable(item_method):
            return bool(item_method())
    except (TypeError, ValueError):
        return False
    return False


def _json_sort_key(value: JSONValue) -> str:
    """Provide deterministic ordering when an unordered set enters a legacy row."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
