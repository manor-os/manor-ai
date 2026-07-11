"""Tests for email service and password reset flow."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── Email service unit tests ──

ROOT_DIR = Path(__file__).resolve().parents[1]
EMAIL_TEMPLATE_DIR = ROOT_DIR / "packages/core/templates/email"
EMAIL_LOGO_ASSET = ROOT_DIR / "apps/web/public/email-logo.png"
EXPECTED_EMAIL_LOGO = (
    '<img src="{{ app_url }}/email-logo.png" width="32" height="32" '
    'alt="Manor AI" class="logo-icon" '
    'style="display:block;border:0;outline:none;text-decoration:none;border-radius:8px;">'
)


def test_all_email_templates_use_shared_logo():
    """Every HTML email must use the same hosted Manor logo image."""
    assert EMAIL_LOGO_ASSET.is_file()

    templates = sorted(EMAIL_TEMPLATE_DIR.glob("*.html"))
    assert templates

    for template in templates:
        html = template.read_text(encoding="utf-8")
        assert html.count(EXPECTED_EMAIL_LOGO) == 1, template.name
        assert "logo-text" not in html, template.name
        assert '<div class="logo-icon">' not in html, template.name
        assert "favicon.svg" not in html, template.name


@pytest.mark.asyncio
async def test_send_email_disabled():
    """When EMAIL_ENABLED=false, send_email succeeds without actually sending."""
    with patch.dict(os.environ, {"EMAIL_ENABLED": "false"}, clear=False):
        from packages.core.services.email_service import send_email

        result = await send_email("user@example.com", "Test", "<p>Hello</p>")
        assert result is True


@pytest.mark.asyncio
async def test_invite_email_template():
    """Invite email calls aiosmtplib.send with correct content when enabled."""
    from types import SimpleNamespace
    import packages.core.services.email_service as email_mod

    mock_send = AsyncMock()
    fake_smtp = SimpleNamespace(send=mock_send)
    original = email_mod.aiosmtplib

    email_mod.aiosmtplib = fake_smtp  # type: ignore[assignment]
    try:
        with patch.dict(os.environ, {"EMAIL_ENABLED": "true"}, clear=False):
            result = await email_mod.send_invite_email(
                to="newuser@example.com",
                entity_name="Acme Corp",
                inviter_name="Alice",
                temp_password="tmp123",
            )

            assert result is True
            mock_send.assert_called_once()

            call_args = mock_send.call_args
            msg = call_args.args[0] if call_args.args else call_args.kwargs.get("message")
            assert msg["To"] == "newuser@example.com"
            assert "Acme Corp" in msg["Subject"]
    finally:
        email_mod.aiosmtplib = original


# ── Password reset e2e tests (use real DB via client fixture) ──


async def _register_email_user(client):
    """Helper: register a user for password reset tests."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "emailuser",
            "email": "emailuser@example.com",
            "password": "oldpass123",
            "entity_name": "EmailTestOrg",
        },
    )
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_password_reset_flow(client):
    """Full flow: register -> forgot-password -> reset -> login with new password."""
    await _register_email_user(client)

    # Request reset (should always return 200)
    with patch("packages.core.services.email_service.send_email", new_callable=AsyncMock):
        resp = await client.post(
            "/api/v1/auth/forgot-password",
            json={
                "email": "emailuser@example.com",
            },
        )
    assert resp.status_code == 200
    assert "reset link" in resp.json()["detail"].lower()

    # Grab the token from the in-memory store
    from packages.core.services.password_reset_service import _reset_tokens

    assert len(_reset_tokens) == 1
    token = list(_reset_tokens.keys())[0]

    # Reset password
    resp = await client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "new_password": "newpass456",
        },
    )
    assert resp.status_code == 200
    assert "reset" in resp.json()["detail"].lower()

    # Token should be consumed
    assert len(_reset_tokens) == 0

    # Old password should fail
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "emailuser",
            "password": "oldpass123",
        },
    )
    assert resp.status_code == 401

    # New password should work
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "emailuser",
            "password": "newpass456",
        },
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_password_reset_expired_token(client):
    """Expired tokens are rejected."""
    await _register_email_user(client)

    # Request reset
    with patch("packages.core.services.email_service.send_email", new_callable=AsyncMock):
        await client.post(
            "/api/v1/auth/forgot-password",
            json={
                "email": "emailuser@example.com",
            },
        )

    from packages.core.services.password_reset_service import _reset_tokens

    token = list(_reset_tokens.keys())[0]

    # Expire the token by setting expires_at to the past
    _reset_tokens[token]["expires_at"] = time.time() - 1

    # Attempt reset — should fail
    resp = await client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": token,
            "new_password": "newpass456",
        },
    )
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower() or "expired" in resp.json()["detail"].lower()
