"""Configuration-loader tests for RANK-002 scoring defaults.

The scoring config is operator-editable YAML, so nulls, malformed files, and bad
numeric values must fall back to safe defaults instead of breaking scanner
startup or producing non-finite weights.
"""

from __future__ import annotations

import math

from backend.scoring.config import DEFAULT_WEIGHTS, ScoringConfig, load_scoring_config


def test_load_scoring_config_uses_defaults_when_file_is_missing(tmp_path):
    config = load_scoring_config(tmp_path / "missing.yaml")

    assert config == ScoringConfig()


def test_load_scoring_config_malformed_file_is_graceful(tmp_path):
    path = tmp_path / "scoring_model.yaml"
    path.write_text("scoring: [not: valid: yaml", encoding="utf-8")

    assert load_scoring_config(path) == ScoringConfig()


def test_load_scoring_config_treats_explicit_nulls_as_defaults(tmp_path):
    path = tmp_path / "scoring_model.yaml"
    path.write_text(
        """
        scoring:
          model_version:
          weights:
            technical:
            risk:
            liquidity:
            freshness:
          liquidity_window:
          risk_window:
          risk_vol_cap:
          freshness_halflife_days:
        """,
        encoding="utf-8",
    )

    assert load_scoring_config(path) == ScoringConfig()


def test_load_scoring_config_normalizes_custom_weights(tmp_path):
    path = tmp_path / "scoring_model.yaml"
    path.write_text(
        """
        scoring:
          weights:
            technical: 4
            risk: 2
            liquidity: 2
            freshness: 2
        """,
        encoding="utf-8",
    )

    config = load_scoring_config(path)

    assert config.weights == {
        "technical": 0.4,
        "risk": 0.2,
        "liquidity": 0.2,
        "freshness": 0.2,
    }
    assert math.isclose(sum(config.weights.values()), 1.0)


def test_load_scoring_config_defaults_invalid_weight_values(tmp_path):
    path = tmp_path / "scoring_model.yaml"
    path.write_text(
        """
        scoring:
          weights:
            technical: -1
            risk: .inf
            liquidity: not-a-number
            freshness: 0.5
        """,
        encoding="utf-8",
    )

    config = load_scoring_config(path)

    assert all(math.isfinite(weight) and weight > 0 for weight in config.weights.values())
    assert math.isclose(sum(config.weights.values()), 1.0)
    assert config.weights["technical"] != -1
    assert config.weights["risk"] != float("inf")
    expected_total = (
        DEFAULT_WEIGHTS["technical"]
        + DEFAULT_WEIGHTS["risk"]
        + DEFAULT_WEIGHTS["liquidity"]
        + 0.5
    )
    assert config.weights["liquidity"] == round(
        DEFAULT_WEIGHTS["liquidity"] / expected_total,
        10,
    )
