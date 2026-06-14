"""Authenticate durable AI verdict-cache envelopes before trusting them."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

AI_CACHE_SIGNING_KEY_ENV = "SCANNER_AI_CACHE_SIGNING_KEY"
AI_CACHE_SIGNATURE_FIELD = "integrity_hmac_sha256"

# Local development remains secure without extra setup: cache entries are valid
# only for this process. Configure the environment key for restart-stable hits.
_PROCESS_SIGNING_KEY = secrets.token_bytes(32)


def get_ai_cache_signing_key() -> bytes:
    """Return the operator key or a process-random non-persistent fallback."""
    configured = os.getenv(AI_CACHE_SIGNING_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    return _PROCESS_SIGNING_KEY


def sign_cache_envelope(
    envelope: Mapping[str, Any],
    *,
    key: bytes,
) -> dict[str, Any]:
    """Return a copy carrying an HMAC over every trusted envelope field."""
    signed = dict(envelope)
    signed.pop(AI_CACHE_SIGNATURE_FIELD, None)
    signed[AI_CACHE_SIGNATURE_FIELD] = hmac.new(
        key,
        _canonical_envelope_bytes(signed),
        hashlib.sha256,
    ).hexdigest()
    return signed


def verify_cache_envelope(envelope: Any, *, key: bytes) -> bool:
    """Return whether an envelope has a valid full HMAC-SHA-256 signature."""
    if not isinstance(envelope, dict):
        return False
    supplied = envelope.get(AI_CACHE_SIGNATURE_FIELD)
    if not isinstance(supplied, str) or len(supplied) != 64:
        return False
    unsigned = dict(envelope)
    unsigned.pop(AI_CACHE_SIGNATURE_FIELD, None)
    try:
        canonical = _canonical_envelope_bytes(unsigned)
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        key,
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied.lower(), expected)


def _canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
