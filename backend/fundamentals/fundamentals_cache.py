from __future__ import annotations

"""On-disk JSON cache for the Check Fundamentals feature.

Two caches live under `data/cache/fundamentals/`:

1. **Data cache** — the parsed screener.in payload for one stock. Stocks
   refresh quarterly at most, so a 30-day TTL is plenty. File name:
   ``<SYMBOL>_data.json``.

2. **Verdict cache** — the Check Fundamentals agent's JSON verdict for that stock.
   Keyed by ``(symbol, model, data_fetch_date)`` so re-clicks for the same
   day and model are free, but a fresh data fetch (e.g. a new quarter
   landed) or model swap invalidates automatically. File name:
   ``<SYMBOL>_verdict_<MODEL_HASH>_<DATA_DATE>.json``.

Both files store JSON with an ISO timestamp; loading old or expired files
returns ``None`` so the caller fetches afresh.
"""

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.config import FUNDAMENTALS_CACHE_DIR


logger = logging.getLogger(__name__)


def _safe_symbol(symbol: str) -> str:
    """Normalize a symbol into something safe to use as a file stem.

    Mirrors the convention used by `backend.daily_data_loader.safe_file_stem`
    — uppercase letters, digits, dashes only. Anything else becomes ``_``.
    """
    cleaned = re.sub(r"[^A-Z0-9-]+", "_", str(symbol).strip().upper())
    return cleaned or "UNKNOWN"


def _hash_model(model: str) -> str:
    """Stable 8-char tag for a model string (used in the verdict filename).

    Keeps filenames short and predictable while still being deterministic
    for the same model name.
    """
    return hashlib.sha1(model.encode("utf-8")).hexdigest()[:8]


def _default_ttl_days() -> int:
    """Read TTL from env, falling back to 30 days."""
    raw = (os.getenv("SCANNER_FUNDAMENTALS_TTL_DAYS") or "").strip()
    if not raw:
        return 30
    try:
        value = int(raw)
        return value if value > 0 else 30
    except (TypeError, ValueError):
        return 30


class FundamentalsCache:
    """JSON file cache with TTLs for screener.in data + agent verdicts."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        data_ttl_days: int | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else FUNDAMENTALS_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_ttl_days = data_ttl_days if data_ttl_days is not None else _default_ttl_days()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def data_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{_safe_symbol(symbol)}_data.json"

    def verdict_path(self, symbol: str, model: str, data_date: str) -> Path:
        # `data_date` is a YYYY-MM-DD string: the date when the fetched
        # screener.in data was produced. Embedding it in the filename means
        # a new data fetch automatically invalidates the prior verdict.
        return (
            self.cache_dir
            / f"{_safe_symbol(symbol)}_verdict_{_hash_model(model)}_{data_date}.json"
        )

    # ------------------------------------------------------------------
    # Data cache
    # ------------------------------------------------------------------

    def get_data(self, symbol: str) -> dict[str, Any] | None:
        """Return cached screener.in data for `symbol`, or `None` if missing/expired."""
        path = self.data_path(symbol)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read cached fundamentals at %s; ignoring", path)
            return None

        fetched_at = self._parse_iso(payload.get("fetched_at"))
        if fetched_at is None:
            return None

        if datetime.now(UTC) - fetched_at > timedelta(days=self.data_ttl_days):
            # Expired — caller should refetch.
            return None
        return payload

    def set_data(self, symbol: str, data: dict[str, Any]) -> None:
        """Write screener.in data for `symbol` to cache. Overwrites previous."""
        path = self.data_path(symbol)
        # Defensive copy so callers can keep mutating their dict afterwards.
        payload = dict(data)
        # Stamp / overwrite the timestamp so cache TTL is honored even when
        # the caller forgot to set it.
        payload["fetched_at"] = payload.get("fetched_at") or datetime.now(UTC).isoformat()
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    # Verdict cache
    # ------------------------------------------------------------------

    def get_verdict(self, symbol: str, model: str, data_date: str) -> dict[str, Any] | None:
        """Return the cached agent verdict for this (symbol, model, data_date)."""
        path = self.verdict_path(symbol, model, data_date)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read cached verdict at %s; ignoring", path)
            return None

    def set_verdict(
        self,
        symbol: str,
        model: str,
        data_date: str,
        verdict: dict[str, Any],
    ) -> None:
        """Persist an agent verdict alongside the (symbol, model, data_date) key."""
        path = self.verdict_path(symbol, model, data_date)
        path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate(self, symbol: str) -> int:
        """Delete every cached file (data + verdicts) for one symbol.

        Returns the number of files deleted. Used by the "Re-run analysis"
        button to force fresh data + a fresh agent call.
        """
        removed = 0
        stem_prefix = _safe_symbol(symbol)
        for path in self.cache_dir.glob(f"{stem_prefix}_*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not delete cache file %s", path, exc_info=True)
        return removed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        # Ensure tz-aware so the TTL math works without surprises.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
