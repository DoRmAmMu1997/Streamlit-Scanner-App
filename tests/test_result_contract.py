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
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backend.scanning.result_contract import (
    AIProvenance,
    MASKED_PARAMETER,
    ResultContractError,
    RuleCheck,
    ScreenerResult,
    SignalProvenance,
    normalize_screener_row,
)


def test_contract_is_available_from_the_scanning_package_surface():
    """Callers should not need to know the result contract's file layout."""
    from backend import scanning

    assert scanning.ScreenerResult is ScreenerResult
    assert scanning.SignalProvenance is SignalProvenance
    assert scanning.RuleCheck is RuleCheck
    assert scanning.AIProvenance is AIProvenance
    assert scanning.normalize_screener_row is normalize_screener_row


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
    }


def test_typed_models_describe_the_common_result_and_provenance_contract():
    """The public dataclasses should be useful without requiring a screener rewrite."""
    rule = RuleCheck(name="close_below_band", passed=True, detail="close <= lower band")
    ai = AIProvenance(model_name="future-model", prompt_version="future-prompt-v1")
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
    }


def test_legacy_row_gains_canonical_provenance_without_losing_raw_fields():
    """A row with no provenance should remain recognizable and gain defaults."""
    normalized = normalize_screener_row(
        _legacy_row(),
        screener_key="envelope",
        params={"period": 20},
        data_snapshot_date=date(2026, 6, 11),
    )

    assert normalized["symbol"] == "RELIANCE"
    assert normalized["close"] == "1234.5678"
    assert normalized["extra_metric"] == 7
    assert normalized["provenance_json"] == {
        "screener_key": "envelope",
        "screener_version": None,
        "triggered_rules": [],
        "indicator_values": {},
        "params_snapshot": {"period": 20},
        "data_snapshot_date": "2026-06-11",
        "source": None,
        "notes": None,
        "ai": None,
    }
    json.dumps(normalized, allow_nan=False)


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
    row["provenance"] = {"rules": ["legacy_rule"], "source": "deterministic"}

    normalized = normalize_screener_row(row, screener_key="envelope")

    assert normalized["provenance_json"]["triggered_rules"] == ["legacy_rule"]
    assert normalized["provenance_json"]["source"] == "deterministic"


def test_close_price_alias_and_optional_common_fields_remain_legacy_compatible():
    """Only symbol is mandatory; a future normalized row may use close_price."""
    normalized = normalize_screener_row(
        {"symbol": "TCS", "close_price": Decimal("3890.25")},
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
        "python_datetime": datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        "numpy_int": np.int64(5),
        "numpy_float": np.float64(1.25),
        "missing": np.nan,
        "not_a_time": pd.NaT,
        "positive_infinity": float("inf"),
        "negative_infinity": np.float64("-inf"),
        "nested": [Decimal("7.25"), np.float32(2.5), np.nan],
        "set_value": {"beta", "alpha"},
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
        {"symbol": "WIPRO"},
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
