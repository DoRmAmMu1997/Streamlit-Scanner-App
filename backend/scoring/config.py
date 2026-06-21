"""Configuration loader for the RANK-002 scoring model.

The defaults in this file mirror the RANK-001 design. Operators may override
them in ``config/scoring_model.yaml``, but a missing, malformed, or partially
filled YAML file must never stop the scanner. Invalid values quietly fall back
to defaults and are normalized before the scorer sees them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL_VERSION = "rank-1.0"
DEFAULT_WEIGHTS: dict[str, float] = {
    "technical": 0.45,
    "risk": 0.25,
    "liquidity": 0.20,
    "freshness": 0.10,
}
DEFAULT_LIQUIDITY_WINDOW = 20
DEFAULT_RISK_WINDOW = 60
DEFAULT_RISK_VOL_CAP = 0.06
DEFAULT_FRESHNESS_HALFLIFE_DAYS = 5.0

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring_model.yaml"


@dataclass(frozen=True)
class ScoringConfig:
    """Runtime knobs for the deterministic RANK-002 scorer."""

    model_version: str = DEFAULT_MODEL_VERSION
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW
    risk_window: int = DEFAULT_RISK_WINDOW
    risk_vol_cap: float = DEFAULT_RISK_VOL_CAP
    freshness_halflife_days: float = DEFAULT_FRESHNESS_HALFLIFE_DAYS


def load_scoring_config(path: Path | str | None = None) -> ScoringConfig:
    """Load scoring config from YAML, falling back to defaults safely.

    Beginner note:
    YAML ``key:`` without a value becomes Python ``None``. We intentionally
    treat that as "use the default" instead of converting it to the string
    ``"None"`` or crashing during startup.
    """
    config_path = Path(path) if path is not None else _CONFIG_PATH
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ScoringConfig()
    if not isinstance(payload, Mapping):
        return ScoringConfig()

    raw = payload.get("scoring", {})
    if not isinstance(raw, Mapping):
        return ScoringConfig()

    return ScoringConfig(
        model_version=_text_or_default(raw.get("model_version"), DEFAULT_MODEL_VERSION),
        weights=_normalize_weights(raw.get("weights")),
        liquidity_window=_int_or_default(raw.get("liquidity_window"), DEFAULT_LIQUIDITY_WINDOW),
        risk_window=_int_or_default(raw.get("risk_window"), DEFAULT_RISK_WINDOW),
        risk_vol_cap=_float_or_default(raw.get("risk_vol_cap"), DEFAULT_RISK_VOL_CAP),
        freshness_halflife_days=_float_or_default(
            raw.get("freshness_halflife_days"),
            DEFAULT_FRESHNESS_HALFLIFE_DAYS,
        ),
    )


def _normalize_weights(value: Any) -> dict[str, float]:
    """Return finite positive weights normalized to sum to one."""
    raw = value if isinstance(value, Mapping) else {}
    weights: dict[str, float] = {}
    for key, default in DEFAULT_WEIGHTS.items():
        candidate = _float_or_default(raw.get(key), default)
        weights[key] = candidate if math.isfinite(candidate) and candidate > 0 else default

    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {key: round(weight / total, 10) for key, weight in weights.items()}


def _text_or_default(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _float_or_default(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result > 0 else default


def _int_or_default(value: Any, default: int) -> int:
    result = _float_or_default(value, float(default))
    return int(result) if result >= 1 else default
