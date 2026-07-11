"""Test helpers for auth flows that differ between OSS and cloud mode."""
from __future__ import annotations

import json
from typing import Any

from httpx import AsyncClient, Response


async def register_user_and_get_token(
    client: AsyncClient,
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Response:
    """Register a test user and return the normal token payload.

    OSS registration returns a token immediately. Cloud registration requires
    email verification first, so tests that only need an authenticated user
    should complete that real verification flow instead of assuming signup
    skips it.
    """
    if payload is None:
        payload = kwargs.pop("json")
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    if data.get("access_token"):
        return response

    assert data.get("requires_verification") is True, response.text
    email = str(data["email"])
    code = _verification_code_for(email)
    verify = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": email, "code": code},
    )
    assert verify.status_code == 200, verify.text
    return verify


def _verification_code_for(email: str) -> str:
    from packages.core.services import email_verification_service as verification

    redis_client = verification._redis()
    if redis_client:
        raw = redis_client.get(verification._key(email))
        assert raw, f"verification code missing for {email}"
        payload = json.loads(raw)
        return str(payload["code"])

    payload = verification._fallback.get(email)
    assert payload, f"verification code missing for {email}"
    return str(payload["code"])
