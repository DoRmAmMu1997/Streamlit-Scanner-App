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
from urllib.parse import urlsplit, urlunsplit

from backend.security import MASK, is_secret_key_name, redact_text
from backend.url_safety import is_safe_http_url

# JSON has fewer built-in value types than Python. These aliases make that
# boundary visible in type hints: a scalar cannot contain another collection,
# while a JSONValue may recursively contain lists and string-keyed objects.
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
SignalSource: TypeAlias = Literal["deterministic", "ai", "hybrid"]
AIEvaluationOutcome: TypeAlias = Literal["approved", "rejected", "error"]

# Exporting a domain-specific name keeps callers/tests independent from the
# redaction module's implementation while still using its consistent mask.
MASKED_PARAMETER = MASK

_SIGNAL_SOURCES = frozenset({"deterministic", "ai", "hybrid"})
_AI_EVALUATION_OUTCOMES = frozenset({"approved", "rejected", "error"})
_DROP = object()


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
class EvidenceReference:
    """One durable evidence pointer without storing fetched document contents."""

    source_label: str
    sanitized_url: str | None
    sha256: str


@dataclass(frozen=True)
class AIProvenance:
    """Reproducible metadata for one AI verdict."""

    model_name: str
    prompt_version: str
    prompt_sha256: str
    generated_at: dt.datetime
    cache_hit: bool
    evidence_references: list[EvidenceReference] = field(default_factory=list)
    input_context_hash: str | None = None
    verdict: str | None = None
    confidence: Decimal | int | float | str | None = None
    decision_reason: str | None = None


@dataclass(frozen=True)
class AIEvaluationRecord:
    """Callback payload captured for later durable AI-evaluation persistence."""

    symbol: str
    signal_date: dt.date | None
    outcome: AIEvaluationOutcome
    verdict: str | None
    confidence: Decimal | int | float | str | None
    decision_reason: str | None
    provenance: AIProvenance
    validated_verdict_json: Mapping[str, JSONValue] = field(default_factory=dict)
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


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
    indicator_values = normalize_indicator_values(raw_indicators)

    # An existing snapshot is authoritative because the screener may have
    # recorded more precise settings. Otherwise use the run-level parameters
    # supplied by the service, dropping callbacks and masking credentials.
    raw_params = provenance.get("params_snapshot")
    params_snapshot = (
        _json_safe_mapping(raw_params)
        if isinstance(raw_params, Mapping)
        else _json_safe_mapping(params or {})
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
            "indicator_values": cast(dict[str, JSONValue], indicator_values),
            "params_snapshot": params_snapshot,
            "data_snapshot_date": _json_safe(raw_data_date),
            "source": source,
            "notes": _optional_text(provenance.get("notes")),
            "ai": _normalize_ai_placeholder(provenance.get("ai")),
        }
    )
    normalized["provenance_json"] = canonical
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResultContractError(
            "Screener result row is not strict JSON serializable."
        ) from exc
    return cast(dict[str, Any], normalized)


def _provenance_mapping(value: Any) -> dict[str, Any]:
    """Return the required provenance mapping or raise a contract error."""
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise ResultContractError(
        "Screener result row requires mapping provenance."
    )


def _normalize_rules(value: Any) -> list[JSONValue]:
    """Return rule names/checks as one JSON list while preserving useful detail.

    A single string, one ``RuleCheck``, or an iterable of either are all
    accepted. Normalizing them to a list gives readers one predictable shape in
    the database without forcing legacy producers to change immediately.
    """
    if _is_missing(value):
        raise ResultContractError("Provenance requires non-empty triggered_rules.")
    if isinstance(value, str | Mapping) or (
        is_dataclass(value) and not isinstance(value, type)
    ):
        items = [value]
    elif isinstance(value, list | tuple | set):
        items = list(value)
    else:
        items = [value]
    normalized = [_json_safe(item) for item in items]
    if not normalized:
        raise ResultContractError("Provenance requires non-empty triggered_rules.")
    for rule in normalized:
        if isinstance(rule, str):
            if not rule.strip():
                raise ResultContractError(
                    "Provenance triggered_rules cannot contain blank names."
                )
        elif isinstance(rule, dict):
            if not str(rule.get("name", "")).strip():
                raise ResultContractError(
                    "Structured triggered_rules require a non-blank name."
                )
        else:
            raise ResultContractError(
                "Provenance triggered_rules must be names or RuleCheck mappings."
            )
    return normalized


def _normalize_source(value: Any) -> SignalSource:
    """Validate an explicitly supplied provenance source.

    Missing values remain ``None``. Explicit values are normalized for case and
    whitespace, but an unknown label raises instead of silently rewriting audit
    evidence into one of the supported categories.
    """
    if _is_missing(value):
        raise ResultContractError("Provenance requires a valid source.")
    normalized = str(value).strip().lower()
    if normalized not in _SIGNAL_SOURCES:
        allowed = ", ".join(sorted(_SIGNAL_SOURCES))
        raise ResultContractError(
            f"Provenance source must be one of: {allowed}."
        )
    return cast(SignalSource, normalized)


def _normalize_ai_placeholder(value: Any) -> dict[str, JSONValue] | None:
    """Normalize optional typed or mapping AI provenance."""
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_mapping(asdict(value))
    raise ResultContractError("AI provenance must be a mapping or AIProvenance.")


def normalize_secret_safe_json(value: Any) -> JSONValue:
    """Return recursively JSON-compatible, callable-free, secret-safe data.

    This is the public persistence boundary for both run parameters and durable
    result/evaluation payloads. Mapping keys that look credential-related are
    masked, strings use the application redactor, Decimal remains lossless text,
    dates use ISO-8601, NumPy/pandas scalars unwrap through ``item()``, and
    non-finite numbers become JSON null.
    """
    normalized = _normalize_json(value)
    return None if normalized is _DROP else cast(JSONValue, normalized)


def normalize_indicator_values(value: Any) -> dict[str, JSONScalar]:
    """Validate and normalize the required scalar indicator evidence mapping."""
    if not isinstance(value, Mapping) or not value:
        raise ResultContractError(
            "Provenance requires non-empty scalar indicator_values."
        )
    return {
        str(key): _json_safe_scalar(item)
        for key, item in value.items()
    }


def sanitize_evidence_url(value: Any) -> str | None:
    """Return a redacted HTTP(S) URL without credentials, query, or fragment."""
    if _is_missing(value):
        return None
    safe_text = cast(str, redact_text(str(value).strip()))
    if not is_safe_http_url(safe_text):
        return None
    parsed = urlsplit(safe_text)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


def _json_safe_mapping(value: Mapping[Any, Any]) -> dict[str, JSONValue]:
    """Recursively convert a mapping and mask credential-named values.

    JSON object keys must be strings, so non-string keys are converted with
    ``str``. Secret-looking keys are masked before their values are inspected,
    which prevents an accidental token from surviving in a custom object.
    """
    try:
        normalized = normalize_secret_safe_json(value)
    except TypeError as exc:
        raise ResultContractError(
            "Screener result row is not strict JSON serializable."
        ) from exc
    if not isinstance(normalized, dict):
        raise TypeError("mapping normalization did not produce a JSON object")
    return normalized


def _json_safe(value: Any) -> JSONValue:
    """Convert common pandas/NumPy/Python values to strict JSON-compatible data.

    ``json.dumps(..., allow_nan=False)`` rejects NaN and infinity even though
    pandas and NumPy use them frequently. Converting those values to ``None``
    keeps the stored JSON standards-compliant and gives readers a conventional
    representation for missing data.
    """
    normalized = _normalize_json(value)
    return None if normalized is _DROP else cast(JSONValue, normalized)


def _normalize_json(value: Any) -> JSONValue | object:
    """Internal recursive implementation with a sentinel for dropped callables."""
    if _is_missing(value):
        return None
    if callable(value):
        return _DROP
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
        return _normalize_json(asdict(value))
    if isinstance(value, Mapping):
        converted: dict[str, JSONValue] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if is_secret_key_name(key):
                converted[key] = MASKED_PARAMETER
                continue
            item = _normalize_json(raw_value)
            if item is _DROP:
                continue
            converted[key] = cast(JSONValue, item)
        return converted
    if isinstance(value, list | tuple):
        items: list[JSONValue] = []
        for raw_item in value:
            item = _normalize_json(raw_item)
            if item is not _DROP:
                items.append(cast(JSONValue, item))
        return items
    if isinstance(value, set | frozenset):
        items = []
        for raw_item in value:
            item = _normalize_json(raw_item)
            if item is not _DROP:
                items.append(cast(JSONValue, item))
        return sorted(items, key=_json_sort_key)

    # NumPy and pandas scalar objects expose ``item()``. Converting through that
    # method avoids importing NumPy into the runtime module solely for type
    # checks, while recursive handling still catches NaN and infinity.
    if type(value).__name__ == "ndarray":
        raise TypeError("Unsupported JSON value type: ndarray")
    item_method = getattr(value, "item", None)
    if _is_numpy_or_pandas_value(value) and callable(item_method):
        try:
            return _normalize_json(item_method())
        except (TypeError, ValueError):
            pass

    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _json_safe_scalar(value: Any) -> JSONScalar:
    """Convert one supported indicator scalar or reject richer objects."""
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        raise ResultContractError(
            "Provenance indicator_values must contain scalar values."
        )
    if type(value).__name__ == "ndarray":
        raise ResultContractError(
            "Provenance indicator_values must contain scalar values."
        )
    if isinstance(value, Decimal) and not value.is_finite():
        raise ResultContractError(
            "Provenance indicator_values must contain finite numbers."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ResultContractError(
            "Provenance indicator_values must contain finite numbers."
        )
    if _is_missing(value):
        return None
    if isinstance(
        value,
        str | bool | int | float | Decimal | dt.datetime | dt.date,
    ):
        return cast(JSONScalar, _json_safe(value))

    item_method = getattr(value, "item", None)
    if _is_numpy_or_pandas_value(value) and callable(item_method):
        try:
            unwrapped = item_method()
        except (TypeError, ValueError) as exc:
            raise ResultContractError(
                "Provenance indicator_values must contain scalar values."
            ) from exc
        if unwrapped is value:
            raise ResultContractError(
                "Provenance indicator_values must contain scalar values."
            )
        return _json_safe_scalar(unwrapped)

    raise ResultContractError(
        "Provenance indicator_values must contain scalar values."
    )


def _is_numpy_or_pandas_value(value: Any) -> bool:
    """Recognize supported third-party scalar families without importing them."""
    return type(value).__module__.partition(".")[0] in {"numpy", "pandas"}


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
