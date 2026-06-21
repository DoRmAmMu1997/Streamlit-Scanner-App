"""Tests for the PROV-001A typed screener-result normalization boundary.

Screeners still return flexible dictionaries and pandas DataFrames. These tests
exercise the small compatibility layer that turns one legacy row into a
JSON-safe persistence row without changing the caller's original data.

Many values below look more complicated than a normal JSON example on purpose.
Real DataFrame rows contain pandas timestamps, NumPy numbers, missing sentinels,
and Decimal prices. Testing those concrete objects documents the boundary much
more clearly than testing only strings and integers would.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backend.scanning.result_contract import (
    MASKED_PARAMETER,
    AIEvaluationRecord,
    AIProvenance,
    EvidenceReference,
    ResultContractError,
    RuleCheck,
    ScreenerResult,
    SignalProvenance,
    normalize_screener_row,
    normalize_secret_safe_json,
)


def test_contract_is_available_from_the_scanning_package_surface():
    """Callers should not need to know the result contract's file layout."""
    from backend import scanning

    assert scanning.ScreenerResult is ScreenerResult
    assert scanning.SignalProvenance is SignalProvenance
    assert scanning.RuleCheck is RuleCheck
    assert scanning.AIProvenance is AIProvenance
    assert scanning.normalize_screener_row is normalize_screener_row


def _valid_provenance(**overrides) -> dict[str, object]:
    provenance: dict[str, object] = {
        "triggered_rules": ["test_rule"],
        "indicator_values": {"rsi_14": Decimal("31.80")},
        "source": "deterministic",
    }
    provenance.update(overrides)
    return provenance


def _legacy_row() -> dict[str, object]:
    """Return a realistic pre-contract row shared by compatibility tests.

    ``close`` is intentionally used instead of the newer ``close_price`` name,
    and the NumPy metric demonstrates that legacy strategy-specific columns are
    retained rather than limited to the five common result fields.
    """
    return {
        "symbol": "RELIANCE",
        "rating": "BUY",
        "signal_date": date(2026, 6, 11),
        "close": Decimal("1234.5678"),
        "reason": "oversold reversal",
        "extra_metric": np.int64(7),
        "provenance": _valid_provenance(),
    }


def test_typed_models_describe_the_common_result_and_provenance_contract():
    """The public dataclasses should be useful without requiring a screener rewrite."""
    rule = RuleCheck(name="close_below_band", passed=True, detail="close <= lower band")
    evidence = EvidenceReference(
        source_label="annual report",
        sanitized_url="https://example.com/report",
        sha256="a" * 64,
    )
    ai = AIProvenance(
        model_name="future-model",
        prompt_version="future-prompt-v1",
        prompt_sha256="b" * 64,
        generated_at=datetime(2026, 6, 13, 8, 0, tzinfo=UTC),
        cache_hit=False,
        evidence_references=[evidence],
        input_context_hash="c" * 64,
    )
    provenance = SignalProvenance(
        screener_key="envelope",
        screener_version="1.2.3",
        triggered_rules=[rule],
        indicator_values={"rsi_14": 31.8},
        params_snapshot={"period": 20},
        data_snapshot_date=date(2026, 6, 11),
        source="hybrid",
        notes="AI fields are only a placeholder in PROV-001A.",
        ai=ai,
    )
    result = ScreenerResult(
        symbol="RELIANCE",
        rating="BUY",
        signal_date=date(2026, 6, 11),
        close_price=Decimal("1234.5678"),
        reason="oversold reversal",
        provenance=provenance,
    )

    assert result.symbol == "RELIANCE"
    assert result.close_price == Decimal("1234.5678")
    assert provenance.triggered_rules == [rule]
    assert asdict(provenance)["ai"] == {
        "model_name": "future-model",
        "prompt_version": "future-prompt-v1",
        "prompt_sha256": "b" * 64,
        "generated_at": datetime(2026, 6, 13, 8, 0, tzinfo=UTC),
        "cache_hit": False,
        "verdict": None,
        "confidence": None,
        "decision_reason": None,
        "evidence_references": [
            {
                "source_label": "annual report",
                "sanitized_url": "https://example.com/report",
                "sha256": "a" * 64,
            }
        ],
        "input_context_hash": "c" * 64,
    }


def test_signal_provenance_accepts_optional_score_breakdown_receipt():
    """RANK-002 receipts belong in provenance without breaking old producers."""
    provenance = SignalProvenance(
        screener_key="envelope",
        triggered_rules=["close_below_band"],
        indicator_values={"close": Decimal("80.0")},
        source="deterministic",
        score_breakdown={
            "model_version": "rank-1.0",
            "components": {"freshness": 87.06},
        },
    )

    assert asdict(provenance)["score_breakdown"] == {
        "model_version": "rank-1.0",
        "components": {"freshness": 87.06},
    }


def test_screener_result_carries_the_reserved_final_score_field():
    """PROV-001 lists final_score in the contract; RANK-002 populates it.

    The DB column and repository mapping already exist; this closes the typed
    contract so the dataclass matches the documented shape. It stays optional
    and defaults to None for legacy rows and graceful-null scoring failures.
    """
    assert ScreenerResult(symbol="TCS").final_score is None
    scored = ScreenerResult(symbol="TCS", final_score=Decimal("82.50"))
    assert scored.final_score == Decimal("82.50")
    assert asdict(scored)["final_score"] == Decimal("82.50")


def test_normalize_keeps_final_score_as_a_json_safe_top_level_field():
    """A row's final_score must survive normalization for the repository to store."""
    normalized = normalize_screener_row(
        {
            "symbol": "TCS",
            "final_score": Decimal("82.50"),
            "provenance": _valid_provenance(),
        },
        screener_key="demo",
    )

    # Decimal becomes a lossless string in the JSON copy; the repository parses it
    # back through _as_decimal into the typed numeric column.
    assert normalized["final_score"] == "82.50"
    json.dumps(normalized, allow_nan=False)


def test_row_without_mapping_provenance_is_rejected():
    """Every shortlisted row must carry an auditable mapping receipt."""
    row = _legacy_row()
    row.pop("provenance")

    with pytest.raises(ResultContractError, match="mapping provenance"):
        normalize_screener_row(row, screener_key="envelope")


def test_existing_provenance_is_enriched_and_legacy_rules_are_normalized():
    """Legacy ``rules`` stay present while canonical ``triggered_rules`` is added."""
    row = _legacy_row()
    row["provenance"] = {
        "rules": ["close_below_band"],
        "indicator_values": {"rsi_14": np.float64(31.8)},
        "source": "deterministic",
        "custom_receipt": {"observed_at": pd.Timestamp("2026-06-11")},
    }

    normalized = normalize_screener_row(row, screener_key="envelope")
    provenance = normalized["provenance_json"]

    assert normalized["provenance"]["rules"] == ["close_below_band"]
    assert provenance["rules"] == ["close_below_band"]
    assert provenance["triggered_rules"] == ["close_below_band"]
    assert provenance["indicator_values"] == {"rsi_14": 31.8}
    assert provenance["custom_receipt"] == {"observed_at": "2026-06-11T00:00:00"}


def test_provenance_json_takes_precedence_when_both_legacy_keys_exist():
    """The database-oriented key is authoritative, but both raw fields survive."""
    row = _legacy_row()
    row["provenance"] = {"triggered_rules": ["legacy_rule"]}
    row["provenance_json"] = {
        "triggered_rules": [
            RuleCheck(name="canonical_rule", passed=True, detail="confirmed")
        ],
        "indicator_values": {"score": 1},
        "source": "deterministic",
        "screener_version": "2",
    }

    # Some transitional screeners may emit both names. Choosing one canonical
    # source prevents two contradictory receipts from being merged silently,
    # while raw-field preservation still keeps the legacy evidence auditable.
    normalized = normalize_screener_row(row, screener_key="envelope")

    assert normalized["provenance"] == {"triggered_rules": ["legacy_rule"]}
    assert normalized["provenance_json"]["triggered_rules"] == [
        {"name": "canonical_rule", "passed": True, "detail": "confirmed"}
    ]
    assert normalized["provenance_json"]["screener_version"] == "2"


def test_missing_dataframe_provenance_json_falls_back_to_legacy_provenance():
    """A mixed DataFrame commonly represents an absent dict cell as NumPy NaN."""
    row = _legacy_row()
    row["provenance_json"] = np.nan
    row["provenance"] = {
        "rules": ["legacy_rule"],
        "indicator_values": {"score": 1},
        "source": "deterministic",
    }

    normalized = normalize_screener_row(row, screener_key="envelope")

    assert normalized["provenance_json"]["triggered_rules"] == ["legacy_rule"]
    assert normalized["provenance_json"]["source"] == "deterministic"


def test_close_price_alias_and_optional_common_fields_remain_legacy_compatible():
    """Only symbol is mandatory; a future normalized row may use close_price."""
    normalized = normalize_screener_row(
        {
            "symbol": "TCS",
            "close_price": Decimal("3890.25"),
            "provenance": _valid_provenance(),
        },
        screener_key="minimal",
    )

    assert normalized["symbol"] == "TCS"
    assert normalized["close_price"] == "3890.25"
    assert "close" not in normalized


def test_dates_numpy_values_and_missing_values_become_strict_json():
    """Pandas/NumPy audit values must serialize without non-standard NaN tokens."""
    row = {
        "symbol": "INFY",
        "signal_date": pd.Timestamp("2026-06-10 15:30:00", tz="Asia/Kolkata"),
        "python_datetime": datetime(2026, 6, 10, 10, 0, tzinfo=UTC),
        "numpy_int": np.int64(5),
        "numpy_float": np.float64(1.25),
        "missing": np.nan,
        "not_a_time": pd.NaT,
        "positive_infinity": float("inf"),
        "negative_infinity": np.float64("-inf"),
        "nested": [Decimal("7.25"), np.float32(2.5), np.nan],
        "set_value": {"beta", "alpha"},
        "provenance": _valid_provenance(),
    }

    normalized = normalize_screener_row(row, screener_key="json_types")

    assert normalized["signal_date"] == "2026-06-10T15:30:00+05:30"
    assert normalized["python_datetime"] == "2026-06-10T10:00:00+00:00"
    assert normalized["numpy_int"] == 5
    assert normalized["numpy_float"] == 1.25
    assert normalized["missing"] is None
    assert normalized["not_a_time"] is None
    assert normalized["positive_infinity"] is None
    assert normalized["negative_infinity"] is None
    assert normalized["nested"] == ["7.25", 2.5, None]
    assert normalized["set_value"] == ["alpha", "beta"]
    json.dumps(normalized, allow_nan=False)


@pytest.mark.parametrize("row", [{}, {"symbol": None}, {"symbol": ""}, {"symbol": "   "}])
def test_missing_or_blank_symbol_fails_clearly(row):
    """A persisted result without a symbol cannot be audited or queried safely."""
    with pytest.raises(ResultContractError, match="non-blank 'symbol'"):
        normalize_screener_row(row, screener_key="envelope")


def test_normalization_does_not_mutate_the_input_or_nested_values():
    """The UI still owns the original row, including its nested provenance objects."""
    row = _legacy_row()
    nested_rules = ["rule_one"]
    nested_indicators = {"rsi": Decimal("30.5")}
    row["provenance"] = {
        "rules": nested_rules,
        "indicator_values": nested_indicators,
        "source": "deterministic",
    }

    normalized = normalize_screener_row(row, screener_key="envelope")

    # Mutate the returned persistence tree deliberately. If normalization had
    # made only a shallow copy, these changes would leak back into the nested
    # lists/dictionaries still owned by the original DataFrame row.
    normalized["provenance_json"]["triggered_rules"].append("rule_two")
    normalized["provenance_json"]["indicator_values"]["rsi"] = 99

    assert row["close"] == Decimal("1234.5678")
    assert nested_rules == ["rule_one"]
    assert nested_indicators == {"rsi": Decimal("30.5")}
    assert "provenance_json" not in row


def test_invalid_source_is_rejected_instead_of_silently_relabeling_evidence():
    """Only the three ticket-defined source categories belong in the contract."""
    row = _legacy_row()
    row["provenance"] = {"source": "internet"}

    with pytest.raises(ResultContractError, match="source"):
        normalize_screener_row(row, screener_key="envelope")


def test_parameter_snapshot_drops_callables_and_masks_credentials(monkeypatch):
    """Configuration values must not become durable secrets inside provenance."""
    monkeypatch.setenv("SERPAPI_API_KEY", "configured-serp-secret")
    params = {
        "period": np.int64(20),
        "progress_callback": lambda *_args: None,
        "api_key": "direct-api-key-secret",
        "nested": {
            "authorization": "Bearer nested-bearer-secret",
            "safe_note": "token=inline-token-secret",
            "database_url": "postgresql://user:db-password@db/scanner",
        },
        "configured_value": "configured-serp-secret",
    }

    normalized = normalize_screener_row(
        {"symbol": "WIPRO", "provenance": _valid_provenance()},
        screener_key="secret_safe",
        params=params,
    )
    snapshot = normalized["provenance_json"]["params_snapshot"]
    rendered = json.dumps(snapshot)

    assert snapshot["period"] == 20
    assert "progress_callback" not in snapshot
    assert snapshot["api_key"] == MASKED_PARAMETER
    assert snapshot["nested"]["authorization"] == MASKED_PARAMETER
    assert snapshot["nested"]["database_url"] == MASKED_PARAMETER
    assert "inline-token-secret" not in rendered
    assert "configured-serp-secret" not in rendered
    assert "direct-api-key-secret" not in rendered
    assert "nested-bearer-secret" not in rendered
    assert "db-password" not in rendered


def test_secret_shaped_text_in_raw_rows_and_provenance_is_redacted(monkeypatch):
    """The raw audit copy is flexible, but it must not become a credential vault."""
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "configured-broker-secret")
    row = {
        "symbol": "SBIN",
        "reason": "provider failed with token=raw-row-secret",
        "provider_message": "configured-broker-secret",
        "provenance": {
            "notes": "Authorization: Bearer provenance-bearer-secret",
            "triggered_rules": ["provider_checked"],
            "indicator_values": {"score": 1},
            "source": "ai",
        },
    }

    normalized = normalize_screener_row(row, screener_key="secret_safe")
    rendered = json.dumps(normalized)

    for secret in (
        "raw-row-secret",
        "configured-broker-secret",
        "provenance-bearer-secret",
    ):
        assert secret not in rendered


def test_normalize_screener_row_rejects_non_mapping_rows():
    """A row must be a mapping; anything else is a programming error.

    ``to_dict("records")`` always produces dictionaries, so a list or scalar
    here means a caller bypassed the service path. Failing fast with the
    contract error keeps that mistake visible instead of persisting garbage.
    """
    with pytest.raises(ResultContractError):
        normalize_screener_row(["symbol", "TCS"], screener_key="demo")


def test_normalize_screener_row_rejects_scalar_provenance():
    """Free-text provenance cannot satisfy the structured audit contract."""
    with pytest.raises(ResultContractError, match="mapping provenance"):
        normalize_screener_row(
            {"symbol": "TCS", "provenance": "manual note from an old screener"},
            screener_key="demo",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("triggered_rules", [], "triggered_rules"),
        ("indicator_values", {}, "indicator_values"),
        ("indicator_values", {"nested": {"value": 1}}, "scalar"),
        ("indicator_values", {"array": np.array([1, 2])}, "strict JSON"),
        ("indicator_values", {"custom": object()}, "strict JSON"),
    ],
)
def test_strict_provenance_rejects_missing_or_non_scalar_evidence(field, value, message):
    provenance = _valid_provenance()
    provenance[field] = value

    with pytest.raises(ResultContractError, match=message):
        normalize_screener_row(
            {"symbol": "TCS", "provenance": provenance},
            screener_key="demo",
        )


def test_indicator_values_preserve_decimal_and_normalize_supported_scalars():
    normalized = normalize_screener_row(
        {
            "symbol": "TCS",
            "provenance": _valid_provenance(
                indicator_values={
                    "decimal": Decimal("0.1000000000000000001"),
                    "date": date(2026, 6, 13),
                    "numpy": np.int64(7),
                }
            ),
        },
        screener_key="demo",
    )

    assert normalized["provenance_json"]["indicator_values"] == {
        "decimal": "0.1000000000000000001",
        "date": "2026-06-13",
        "numpy": 7,
    }
    json.dumps(normalized, allow_nan=False)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), np.inf])
def test_indicator_values_reject_non_finite_numbers(value):
    with pytest.raises(ResultContractError, match="finite"):
        normalize_screener_row(
            {
                "symbol": "TCS",
                "provenance": _valid_provenance(
                    indicator_values={"invalid_number": value}
                ),
            },
            screener_key="demo",
        )


def test_public_json_normalizer_recurses_drops_callables_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "configured-secret")
    normalized = normalize_secret_safe_json(
        {
            "as_of": date(2026, 6, 13),
            "amount": Decimal("1.2300"),
            "callback": lambda: None,
            "nested": {
                "api_key": "raw-secret",
                "message": "token=inline-secret",
                "configured": "configured-secret",
                "callbacks": [1, lambda: None, 2],
            },
        }
    )

    assert normalized == {
        "as_of": "2026-06-13",
        "amount": "1.2300",
        "nested": {
            "api_key": MASKED_PARAMETER,
            "message": "token=***REDACTED***",
            "configured": "***REDACTED***",
            "callbacks": [1, 2],
        },
    }
    json.dumps(normalized, allow_nan=False)


def test_public_json_normalizer_rejects_custom_objects_and_arrays():
    with pytest.raises(TypeError, match="Unsupported JSON value type"):
        normalize_secret_safe_json({"custom": object()})

    with pytest.raises(TypeError, match="Unsupported JSON value type"):
        normalize_secret_safe_json({"array": np.array([1, 2])})


def test_normalize_screener_row_wraps_unsupported_values_as_contract_errors():
    with pytest.raises(ResultContractError, match="strict JSON"):
        normalize_screener_row(
            {
                "symbol": "TCS",
                "custom": object(),
                "provenance": _valid_provenance(),
            },
            screener_key="demo",
        )


def test_ai_evaluation_domain_types_are_json_normalizable():
    provenance = AIProvenance(
        model_name="gpt-test",
        prompt_version="v3",
        prompt_sha256="b" * 64,
        generated_at=datetime(2026, 6, 13, 8, 0, tzinfo=UTC),
        cache_hit=True,
        verdict="BUY",
        confidence=Decimal("8.75"),
        decision_reason="Evidence confirms the setup.",
        evidence_references=[
            EvidenceReference(
                source_label="exchange filing",
                sanitized_url="https://example.com/filing",
                sha256="a" * 64,
            )
        ],
    )
    record = AIEvaluationRecord(
        symbol="TCS",
        signal_date=date(2026, 6, 13),
        outcome="approved",
        verdict="BUY",
        confidence=Decimal("8.75"),
        decision_reason="Evidence confirms the setup.",
        provenance=provenance,
        validated_verdict_json={"risk": "medium"},
    )

    normalized = normalize_secret_safe_json(asdict(record))
    assert normalized["confidence"] == "8.75"
    assert normalized["provenance"]["verdict"] == "BUY"
    assert normalized["provenance"]["confidence"] == "8.75"
    assert normalized["provenance"]["decision_reason"] == "Evidence confirms the setup."
    assert normalized["provenance"]["generated_at"] == "2026-06-13T08:00:00+00:00"
    json.dumps(normalized, allow_nan=False)
