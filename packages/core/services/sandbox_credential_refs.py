"""Short-lived capability references for Sandbox credential responses."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


_REFERENCE_PREFIX = "sbxcred.v1"
_REFERENCE_TTL_SECONDS = 120


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def issue_sandbox_credential_ref(
    *,
    signing_key: str,
    sandbox_id: str,
    execution_id: str,
    event_id: str,
    provider: str,
    integration_account_id: str,
    now: int | None = None,
) -> str:
    """Issue a narrowly bound credential capability for Sandbox Service."""

    if not signing_key:
        raise ValueError("Sandbox credential references require SANDBOX_API_TOKEN")
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        "account_id": integration_account_id,
        "event_id": event_id,
        "execution_id": execution_id,
        "expires_at": issued_at + _REFERENCE_TTL_SECONDS,
        "issued_at": issued_at,
        "provider": provider,
        "sandbox_id": sandbox_id,
    }
    encoded_claims = _base64url_encode(
        json.dumps(
            claims,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signature = hmac.new(
        signing_key.encode("utf-8"),
        f"{_REFERENCE_PREFIX}.{encoded_claims}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{_REFERENCE_PREFIX}.{encoded_claims}.{_base64url_encode(signature)}"
