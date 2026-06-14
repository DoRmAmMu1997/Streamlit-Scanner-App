"""Authenticate durable AI verdict-cache envelopes before trusting them.

Why this module exists (beginner note)
--------------------------------------
The two AI screeners (Technical Analysis, 67 Ka Funda) cache each expensive
Claude verdict on disk so re-running the same scan is free. But anything written
to disk can also be *edited* on disk — by a buggy process, a shared volume, or a
malicious local user. Without a check, a tampered cache file could feed the app a
forged "approved" verdict it never actually produced.

This module is that check. It wraps each cache entry ("envelope") with a keyed
**HMAC-SHA-256** signature computed over the entire envelope:

- ``sign_cache_envelope``   stamps the signature when writing the cache.
- ``verify_cache_envelope`` re-derives it when reading and rejects any mismatch.

HMAC (not a plain SHA-256 hash) is used because it mixes in a secret *key*: an
attacker who can edit the file still cannot forge a valid signature without the
key. A rejected envelope is treated as a cache miss, so the agent simply
recomputes the verdict — tampering degrades to "slower", never to "wrong".

Key management (secure by default)
----------------------------------
``get_ai_cache_signing_key`` prefers an operator-provided key from the
``SCANNER_AI_CACHE_SIGNING_KEY`` environment variable. When that is unset it
falls back to a random key generated once per process, so local development stays
secure with zero setup — cached entries are simply only trusted for the lifetime
of the current process (a restart invalidates them and they recompute). Set the
environment key in deployment for restart-stable, cross-process cache reuse. The
configured key participates in the app's central secret redaction (see
``backend.config.secret_values``) so it never leaks into logs or UI errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from typing import Any

# The environment variable that holds the operator-provided signing key, and the
# envelope field the signature is stored under. Kept as named constants so the
# sign/verify paths (and tests) cannot drift on the exact strings.
AI_CACHE_SIGNING_KEY_ENV = "SCANNER_AI_CACHE_SIGNING_KEY"
AI_CACHE_SIGNATURE_FIELD = "integrity_hmac_sha256"

# Local development remains secure without extra setup: cache entries are valid
# only for this process. Generated once at import with a CSPRNG (``secrets``),
# never written to disk. Configure the environment key for restart-stable hits.
_PROCESS_SIGNING_KEY = secrets.token_bytes(32)


def get_ai_cache_signing_key() -> bytes:
    """Return the operator key or a process-random non-persistent fallback.

    The operator key (``SCANNER_AI_CACHE_SIGNING_KEY``) wins when present so
    cached verdicts survive restarts and can be shared across processes. With no
    key configured we return the per-process random key above: still
    unforgeable, just scoped to this one running process.
    """
    configured = os.getenv(AI_CACHE_SIGNING_KEY_ENV)
    if configured:
        return configured.encode("utf-8")
    return _PROCESS_SIGNING_KEY


def sign_cache_envelope(
    envelope: Mapping[str, Any],
    *,
    key: bytes,
) -> dict[str, Any]:
    """Return a copy carrying an HMAC over every trusted envelope field.

    The signature is computed over the envelope *without* any pre-existing
    signature field, then stored back under ``AI_CACHE_SIGNATURE_FIELD``. Because
    the HMAC covers the whole canonical envelope, changing any field later (for
    example flipping ``approved`` to true) invalidates the signature.
    """
    signed = dict(envelope)
    # Drop any stale signature first so we always sign the bare payload — signing
    # over a previous signature would make the result depend on signing order.
    signed.pop(AI_CACHE_SIGNATURE_FIELD, None)
    signed[AI_CACHE_SIGNATURE_FIELD] = hmac.new(
        key,
        _canonical_envelope_bytes(signed),
        hashlib.sha256,
    ).hexdigest()
    return signed


def verify_cache_envelope(envelope: Any, *, key: bytes) -> bool:
    """Return whether an envelope has a valid full HMAC-SHA-256 signature.

    Returns ``False`` (treated by callers as a cache miss → recompute) for
    anything suspicious: a non-dict, a missing/wrong-length signature, an
    envelope that is not strict-JSON canonicalizable (for example it contains
    NaN), or a signature that does not match. The comparison uses
    ``hmac.compare_digest`` so it runs in constant time and does not leak how
    many leading characters matched.
    """
    if not isinstance(envelope, dict):
        return False
    supplied = envelope.get(AI_CACHE_SIGNATURE_FIELD)
    # A valid signature is exactly 64 lowercase hex chars (SHA-256). Reject early
    # if the field is absent or the wrong shape before doing any HMAC work.
    if not isinstance(supplied, str) or len(supplied) != 64:
        return False
    # Re-derive the signature over the same bytes the signer used: the envelope
    # with the signature field removed.
    unsigned = dict(envelope)
    unsigned.pop(AI_CACHE_SIGNATURE_FIELD, None)
    try:
        canonical = _canonical_envelope_bytes(unsigned)
    except (TypeError, ValueError):
        # Non-serializable / non-finite content can never have been produced by
        # ``sign_cache_envelope`` (which also enforces strict JSON), so it cannot
        # be authentic — fail closed.
        return False
    expected = hmac.new(
        key,
        canonical,
        hashlib.sha256,
    ).hexdigest()
    # ``expected`` is already lowercase hex; lowercasing ``supplied`` makes the
    # check case-insensitive without weakening it.
    return hmac.compare_digest(supplied.lower(), expected)


def _canonical_envelope_bytes(envelope: Mapping[str, Any]) -> bytes:
    """Serialize an envelope to one deterministic byte string for HMAC.

    Both signing and verifying must hash byte-for-byte identical input, so the
    JSON is fully canonicalized: keys sorted, no insignificant whitespace, ASCII
    escaping, and ``allow_nan=False`` (NaN/Infinity are not valid JSON and would
    make the digest unreproducible). ``json.dumps`` raises ``TypeError`` /
    ``ValueError`` here for non-serializable or non-finite values, which the
    verify path catches and treats as an invalid envelope.
    """
    return json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
