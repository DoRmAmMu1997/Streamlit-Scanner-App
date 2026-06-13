"""DhanHQ client wrapper and response normalization helpers.

The rest of the app should not need to know Dhan's exact API response shape.
This module converts broker responses into ordinary pandas DataFrames with the
same six columns every screener expects:
timestamp, open, high, low, close, volume.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pandas as pd

from backend.config import DhanCredentials, get_dhan_credentials


class DhanRateLimitError(RuntimeError):
    """Raised when Dhan asks the app to slow down history requests."""


def infer_epoch_unit(values: pd.Series) -> str:
    """Infer whether numeric timestamps are seconds, milliseconds, or microseconds."""
    nums = pd.to_numeric(values, errors="coerce").dropna()
    if nums.empty:
        return "s"

    # Epoch timestamps are just "number of time units since 1970". Large values
    # mean smaller units. For example, milliseconds are roughly 1000x larger
    # than seconds for the same date.
    max_value = float(nums.max())
    if max_value > 1e14:
        return "us"
    if max_value > 1e11:
        return "ms"
    return "s"


def normalize_daily_payload(data: Any) -> pd.DataFrame:
    """
    Convert Dhan daily candle payloads into timestamp/open/high/low/close/volume.

    The Dhan SDK may return either a dictionary of arrays or a list of candle
    dictionaries. Keeping that normalization in one place means screeners can
    work with plain pandas data and ignore SDK wire-shape details.
    """
    if data is None:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    try:
        # pandas can build a table from either:
        # - [{"open": 1, ...}, {"open": 2, ...}]
        # - {"open": [1, 2], "close": [3, 4], ...}
        if isinstance(data, (list, dict)):
            df = pd.DataFrame(data)
        else:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    except Exception:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    if df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    # Dhan/source libraries can differ in capitalization or timestamp column
    # names. Lower-casing the column names lets us accept those small variants.
    normalized = {str(col).strip().lower(): col for col in df.columns}
    timestamp_col = None
    for candidate in ("start_time", "starttime", "timestamp", "time", "datetime", "date"):
        if candidate in normalized:
            timestamp_col = normalized[candidate]
            break

    open_col = normalized.get("open")
    high_col = normalized.get("high")
    low_col = normalized.get("low")
    close_col = normalized.get("close")
    volume_col = normalized.get("volume")

    if any(col is None for col in (timestamp_col, open_col, high_col, low_col, close_col)):
        # Returning an empty standard-shaped DataFrame is easier for screeners
        # to handle than a half-normalized frame with missing required columns.
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    # Coerce prices to numbers once at the boundary. After this point, screeners
    # can safely do calculations without repeatedly converting string values.
    out = pd.DataFrame(
        {
            "timestamp_raw": df[timestamp_col],
            "open": pd.to_numeric(df[open_col], errors="coerce"),
            "high": pd.to_numeric(df[high_col], errors="coerce"),
            "low": pd.to_numeric(df[low_col], errors="coerce"),
            "close": pd.to_numeric(df[close_col], errors="coerce"),
            "volume": pd.to_numeric(df[volume_col], errors="coerce") if volume_col else 0,
        }
    )

    if pd.api.types.is_numeric_dtype(out["timestamp_raw"]):
        unit = infer_epoch_unit(out["timestamp_raw"])
        timestamps = pd.to_datetime(out["timestamp_raw"], unit=unit, errors="coerce", utc=True)
    else:
        # Some APIs return numeric epochs as strings. If most values parse as
        # numbers, treat them as epochs; otherwise parse them as date strings.
        maybe_numeric = pd.to_numeric(out["timestamp_raw"], errors="coerce")
        if maybe_numeric.notna().sum() >= max(1, len(out) // 2):
            unit = infer_epoch_unit(maybe_numeric)
            timestamps = pd.to_datetime(maybe_numeric, unit=unit, errors="coerce", utc=True)
        else:
            timestamps = pd.to_datetime(out["timestamp_raw"], errors="coerce", utc=True)

    # Store timestamps as India-market naive datetimes. That matches the style
    # commonly used in local CSV files and avoids timezone surprises in tables.
    out["timestamp"] = timestamps.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = out.drop(columns=["timestamp_raw"]).dropna(subset=["timestamp", "open", "high", "low", "close"])
    out = out[["timestamp", "open", "high", "low", "close", "volume"]]
    return out.sort_values("timestamp").reset_index(drop=True)


def _iter_response_text(value: Any):
    """Yield all text-like pieces from a nested Dhan response."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_response_text(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_response_text(item)
    elif value is not None:
        yield str(value)


def _is_rate_limit_response(response: Any) -> bool:
    """Return True when a Dhan response contains a retryable rate-limit signal."""
    text = " ".join(_iter_response_text(response)).lower()
    rate_limit_tokens = (
        "dh-904",
        "rate_limit",
        "rate limit",
        "too many requests",
        "breaching rate limits",
        "try throttling",
    )
    return any(token in text for token in rate_limit_tokens)


def normalize_daily_response(response: Any) -> pd.DataFrame:
    """Validate a Dhan response dictionary and normalize the candle payload."""
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected Dhan response type: {type(response).__name__}")

    if _is_rate_limit_response(response):
        raise DhanRateLimitError(f"Dhan historical_daily_data rate limited: {response}")

    status = str(response.get("status", "")).strip().lower()
    if status and status != "success":
        remarks = response.get("remarks") or response.get("message") or response.get("data")
        remarks_text = str(remarks).strip().lower()
        # "No data" is a normal business outcome for some symbols/date ranges,
        # so it becomes an empty DataFrame instead of a hard app failure.
        if any(token in remarks_text for token in ("no data", "no records", "not found")):
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        raise RuntimeError(f"Dhan historical_daily_data failed: {remarks}")

    return normalize_daily_payload(response.get("data"))


def _format_date(value: date | datetime | str) -> str:
    """Format supported date inputs into the YYYY-MM-DD string Dhan expects."""
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


class DhanDataClient:
    """Small wrapper around the DhanHQ SDK used by scanner backends."""

    def __init__(self, credentials: DhanCredentials | None = None, raw_client: Any | None = None):
        self._raw_client_lock = threading.Lock()
        self._thread_local = threading.local()
        self._sdk_client_factory: Callable[[], Any] | None = None

        if raw_client is not None:
            # Tests pass a fake client here so they can verify our code without
            # making real network calls to Dhan.
            self.dhan = raw_client
            return

        credentials = credentials or get_dhan_credentials(required=True)
        if credentials is None:  # pragma: no cover - required=True raises first
            raise RuntimeError("Dhan credentials are not configured.")
        self._sdk_client_factory = lambda: self._build_sdk_client(credentials)
        # Preserve the long-standing public attribute for callers that inspect
        # the wrapper, while worker threads obtain their own SDK transport.
        self.dhan = self._sdk_client_factory()
        self._thread_local.dhan = self.dhan

    @staticmethod
    def _build_sdk_client(credentials: DhanCredentials) -> Any:
        """Create one authenticated SDK client and its private HTTP session."""
        try:
            from dhanhq import DhanContext, dhanhq
        except ImportError as exc:  # pragma: no cover - depends on local package state
            raise RuntimeError("Install the DhanHQ SDK with: pip install -U dhanhq") from exc

        # Modern dhanhq versions authenticate through a DhanContext object. We
        # keep that SDK detail here instead of scattering it across screeners.
        context = DhanContext(credentials.client_code, credentials.access_token)
        return dhanhq(context)

    def _client_for_current_thread(self) -> Any:
        """Return this thread's SDK client or the injected compatibility client."""
        if self._sdk_client_factory is None:
            return self.dhan

        client = getattr(self._thread_local, "dhan", None)
        if client is None:
            client = self._sdk_client_factory()
            self._thread_local.dhan = client
        return client

    @classmethod
    def from_env(cls) -> DhanDataClient:
        return cls(get_dhan_credentials(required=True))

    def fetch_daily_candles(
        self,
        security_id: str | int,
        exchange_segment: str,
        instrument_type: str,
        from_date: date | datetime | str,
        to_date: date | datetime | str,
    ) -> pd.DataFrame:
        """Fetch and normalize daily candles for one Dhan security ID."""
        # security_id is sent as a string because Dhan's SDK/docs accept string
        # identifiers, and CSV-loaded IDs naturally arrive as strings.
        client = self._client_for_current_thread()
        request = {
            "security_id": str(security_id),
            "exchange_segment": str(exchange_segment),
            "instrument_type": str(instrument_type),
            "from_date": _format_date(from_date),
            "to_date": _format_date(to_date),
        }
        if self._sdk_client_factory is None:
            # Injected clients have no documented concurrency contract. Keep
            # the compatibility seam safe by serializing only the network call.
            with self._raw_client_lock:
                response = client.historical_daily_data(**request)
        else:
            response = client.historical_daily_data(**request)
        return normalize_daily_response(response)
