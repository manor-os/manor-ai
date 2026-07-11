"""E2E tests: two-factor authentication (TOTP)."""

import pytest
from httpx import AsyncClient

from packages.core.services.totp_service import generate_totp_code


async def _register_and_token(client: AsyncClient) -> str:
    """Register tfauser and return JWT token."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "tfauser",
            "email": "tfa@example.com",
            "password": "securepass123",
            "entity_name": "TFA Corp",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_setup_2fa(client: AsyncClient):
    """Setup 2FA returns secret and URI."""
    token = await _register_and_token(client)

    resp = await client.post("/api/v1/auth/2fa/setup", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "secret" in data
    assert "uri" in data
    assert data["uri"].startswith("otpauth://totp/")
    assert data["username"] == "tfauser"


@pytest.mark.asyncio
async def test_verify_and_enable(client: AsyncClient):
    """Generate code from secret, verify, get backup codes."""
    token = await _register_and_token(client)

    # Setup
    setup_resp = await client.post("/api/v1/auth/2fa/setup", headers=_auth(token))
    secret = setup_resp.json()["secret"]

    # Generate a valid TOTP code and verify
    code = generate_totp_code(secret)
    resp = await client.post(
        "/api/v1/auth/2fa/verify",
        json={"code": code},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enabled"] is True
    assert "backup_codes" in data
    assert len(data["backup_codes"]) == 8


@pytest.mark.asyncio
async def test_login_requires_2fa(client: AsyncClient):
    """Enable 2FA, login without code -> requires_2fa response."""
    token = await _register_and_token(client)

    # Setup and enable
    setup_resp = await client.post("/api/v1/auth/2fa/setup", headers=_auth(token))
    secret = setup_resp.json()["secret"]
    code = generate_totp_code(secret)
    await client.post(
        "/api/v1/auth/2fa/verify",
        json={"code": code},
        headers=_auth(token),
    )

    # Login without totp_code
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "tfauser",
            "password": "securepass123",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["requires_2fa"] is True
    assert "user_id" in data
    # No access_token should be present
    assert "access_token" not in data


@pytest.mark.asyncio
async def test_login_with_2fa(client: AsyncClient):
    """Enable 2FA, login with valid code -> get token."""
    token = await _register_and_token(client)

    # Setup and enable
    setup_resp = await client.post("/api/v1/auth/2fa/setup", headers=_auth(token))
    secret = setup_resp.json()["secret"]
    code = generate_totp_code(secret)
    await client.post(
        "/api/v1/auth/2fa/verify",
        json={"code": code},
        headers=_auth(token),
    )

    # Login with totp_code
    login_code = generate_totp_code(secret)
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "tfauser",
            "password": "securepass123",
            "totp_code": login_code,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "access_token" in data
    assert data["role"] == "owner"


@pytest.mark.asyncio
async def test_disable_2fa(client: AsyncClient):
    """Enable then disable 2FA with valid code."""
    token = await _register_and_token(client)

    # Setup and enable
    setup_resp = await client.post("/api/v1/auth/2fa/setup", headers=_auth(token))
    secret = setup_resp.json()["secret"]
    code = generate_totp_code(secret)
    await client.post(
        "/api/v1/auth/2fa/verify",
        json={"code": code},
        headers=_auth(token),
    )

    # Verify 2FA is enabled
    status_resp = await client.get("/api/v1/auth/2fa/status", headers=_auth(token))
    assert status_resp.json()["totp_enabled"] is True

    # Disable with valid code
    disable_code = generate_totp_code(secret)
    resp = await client.post(
        "/api/v1/auth/2fa/disable",
        json={"code": disable_code},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["disabled"] is True

    # Verify 2FA is now disabled
    status_resp = await client.get("/api/v1/auth/2fa/status", headers=_auth(token))
    assert status_resp.json()["totp_enabled"] is False

    # Login should work without totp_code now
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "tfauser",
            "password": "securepass123",
        },
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()
