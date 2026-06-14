from __future__ import annotations

import pytest

from backend.ai_cache_integrity import sign_cache_envelope, verify_cache_envelope


def test_signed_cache_envelope_detects_valid_shaped_verdict_tampering():
    key = b"k" * 32
    signed = sign_cache_envelope(
        {
            "schema_version": 1,
            "verdict": {"approved": False, "confidence": 2},
        },
        key=key,
    )

    assert verify_cache_envelope(signed, key=key) is True

    signed["verdict"] = {"approved": True, "confidence": 9}

    assert verify_cache_envelope(signed, key=key) is False


def test_cache_signature_is_bound_to_the_selected_key():
    signed = sign_cache_envelope(
        {"schema_version": 1, "verdict": {"approved": True}},
        key=b"a" * 32,
    )

    assert verify_cache_envelope(signed, key=b"b" * 32) is False


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cache_envelope_is_an_invalid_miss(non_finite):
    envelope = {
        "schema_version": 2,
        "verdict": {"confidence": non_finite},
        "integrity_hmac_sha256": "0" * 64,
    }

    assert verify_cache_envelope(envelope, key=b"k" * 32) is False
