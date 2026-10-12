"""Validation for Core-issued Sandbox credential capability references."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time


_REFERENCE_PREFIX = "sbxcred.v1"
_REFERENCE_TTL_SECONDS = 120
_REFERENCE_CLOCK_SKEW_SECONDS = 5


def _base64url_decode(value: str) -> bytes:
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )


def validate_sandbox_credential_ref(
    reference: str,
    *,
    signing_key: str,
    sandbox_id: str,
    execution_id: str,
    event_id: str,
    provider: str,
    integration_account_id: str,
    now: int | None = None,
) -> None:
    """Reject references that are forged, expired, or replayed out of scope."""

    if not signing_key:
        raise ValueError("Sandbox credential references are not configured")
    parts = reference.split(".")
    if len(parts) != 4 or ".".join(parts[:2]) != _REFERENCE_PREFIX:
        raise ValueError("Invalid Sandbox credential reference")
    encoded_claims, encoded_signature = parts[2], parts[3]
    expected_signature = hmac.new(
        signing_key.encode("utf-8"),
        f"{_REFERENCE_PREFIX}.{encoded_claims}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        supplied_signature = _base64url_decode(encoded_signature)
    except (binascii.Error, ValueError):
        raise ValueError("Invalid Sandbox credential reference") from None
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("Invalid Sandbox credential reference")
    try:
        claims = json.loads(_base64url_decode(encoded_claims))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise ValueError("Invalid Sandbox credential reference") from None
    if not isinstance(claims, dict):
        raise ValueError("Invalid Sandbox credential reference")

    expected = {
        "account_id": integration_account_id,
        "event_id": event_id,
        "execution_id": execution_id,
        "provider": provider,
        "sandbox_id": sandbox_id,
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise ValueError("Sandbox credential reference scope does not match")
    try:
        issued_at = int(claims["issued_at"])
        expires_at = int(claims["expires_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid Sandbox credential reference") from None
    current_time = int(time.time()) if now is None else int(now)
    if (
        issued_at > current_time + _REFERENCE_CLOCK_SKEW_SECONDS
        or expires_at <= current_time
        or expires_at - issued_at != _REFERENCE_TTL_SECONDS
    ):
        raise ValueError("Sandbox credential reference has expired")
