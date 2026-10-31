"""Tests for email service and password reset flow."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from redis.asyncio import Redis
from sqlalchemy import event, select

from packages.core import database
from packages.core.models.user import User
from packages.core.services import password_reset_service as password_reset
from packages.core.services.auth_service import verify_password


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


def test_smtp_config_accepts_legacy_username_alias(monkeypatch):
    """Older runtime files used SMTP_USERNAME instead of SMTP_USER."""
    from packages.core.services.email_service import _get_smtp_config

    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.setenv("SMTP_USERNAME", "legacy-smtp-user")

    assert _get_smtp_config()["username"] == "legacy-smtp-user"


@pytest.mark.asyncio
async def test_send_email_blocks_local_fixture_domains():
    """Synthetic ``.local`` accounts must never reach a configured SMTP relay."""
    import packages.core.services.email_service as email_mod
    from packages.core.services import smtp_transport

    mock_send = AsyncMock()
    original = getattr(smtp_transport, "send_message_async", None)
    smtp_transport.send_message_async = mock_send  # type: ignore[attr-defined]
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            mp.delenv("PYTEST_CURRENT_TEST", raising=False)
            result = await email_mod.send_email(
                "newsletter-e2e@manor.local", "Test", "<p>Hello</p>",
            )
        assert result is True
        mock_send.assert_not_awaited()
    finally:
        if original is None:
            delattr(smtp_transport, "send_message_async")
        else:
            smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_notification_email_escapes_plain_text_body(monkeypatch):
    """Notification text is plain text: markup in it must render literally."""
    import packages.core.services.email_service as email_mod

    sent: dict[str, str] = {}

    async def fake_send_email(to, subject, html_body, *args, **kwargs):
        sent.update(to=to, subject=subject, html=html_body)
        return True

    monkeypatch.setattr(email_mod, "send_email", fake_send_email)

    ok = await email_mod.send_notification_email(
        "owner@manor-test.io",
        "Review <script>alert(1)</script>",
        "<b>Q&A</b> needs you\nSecond line",
    )

    assert ok is True
    html = sent["html"]
    assert "&lt;b&gt;Q&amp;A&lt;/b&gt; needs you\nSecond line" in html
    assert "<b>Q&A</b>" not in html
    assert "<script>" not in html
    # A subject is a header, not HTML: it stays as written.
    assert sent["subject"] == "Manor AI: Review <script>alert(1)</script>"


@pytest.mark.asyncio
async def test_invite_email_template():
    """Invite email delegates the MIME message to the shared transport."""
    import packages.core.services.email_service as email_mod
    from packages.core.services import smtp_transport

    mock_send = AsyncMock()
    original_transport = getattr(smtp_transport, "send_message_async", None)
    smtp_transport.send_message_async = mock_send  # type: ignore[attr-defined]
    try:
        with patch.dict(os.environ, {"EMAIL_ENABLED": "true"}, clear=False):
            result = await email_mod.send_invite_email(
                to="newuser@example.com",
                entity_name="Acme Corp",
                inviter_name="Alice",
                temp_password="tmp123",
            )

            assert result is True
            mock_send.assert_awaited_once()

            call_args = mock_send.call_args
            msg = call_args.args[0] if call_args.args else call_args.kwargs.get("message")
            assert msg["To"] == "newuser@example.com"
            assert "Acme Corp" in msg["Subject"]
    finally:
        if original_transport is None:
            delattr(smtp_transport, "send_message_async")
        else:
            smtp_transport.send_message_async = original_transport


# ── Password reset integration tests (real PostgreSQL and isolated Redis) ──

_EMAIL = "emailuser@example.com"
_OLD_PASSWORD = "Original-reset-password-123"
_NEW_PASSWORD = "Updated-reset-password-456"
_INVALID_TOKEN = {
    "code": "auth.password_reset.invalid_token",
    "message": "Invalid or expired reset token",
}


@pytest.fixture
async def reset_deliveries(isolated_test_redis, monkeypatch):
    """Capture the mail boundary; token persistence still uses real Redis."""
    tokens = []
    store = Redis.from_url(isolated_test_redis, decode_responses=True)

    async def deliver(email, token):
        assert email == _EMAIL
        tokens.append(token)
        return True

    monkeypatch.setattr("packages.core.services.email_service.send_password_reset_email", deliver)
    try:
        yield store, tokens
    finally:
        if tokens:
            await store.delete(*[password_reset._token_key(token) for token in tokens])
        await store.aclose()


async def _register_email_user(client):
    """Helper: register a user for password reset tests."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "emailuser",
            "email": _EMAIL,
            "password": _OLD_PASSWORD,
            "entity_name": "EmailTestOrg",
        },
    )
    assert resp.status_code == 200
    return resp.json()


async def _request_reset(client, reset_deliveries):
    response = await client.post("/api/v1/auth/forgot-password", json={"email": _EMAIL})
    assert response.status_code == 200, response.text
    return reset_deliveries[1][-1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_password_reset_flow(client, reset_deliveries):
    """A committed reset revokes old passwords, sessions, and all old links."""
    registration = await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    second_token = await _request_reset(client, reset_deliveries)
    store, _ = reset_deliveries
    key = password_reset._token_key(token)
    assert token not in key
    assert key.endswith(hashlib.sha256(token.encode()).hexdigest())
    assert 0 < await store.ttl(key) <= 3600
    assert token not in await store.get(key)

    resp = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": _NEW_PASSWORD},
    )
    assert resp.status_code == 200
    for old_token in (token, second_token):
        repeated = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": old_token, "new_password": "Another-reset-password-789"},
        )
        assert repeated.status_code == 400
        assert repeated.json()["detail"] == _INVALID_TOKEN

    old_session = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {registration['access_token']}"},
    )
    assert old_session.status_code == 401
    resp = await client.post(
        "/api/v1/auth/login", json={"email": _EMAIL, "password": _OLD_PASSWORD},
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/api/v1/auth/login", json={"email": _EMAIL, "password": _NEW_PASSWORD},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_password_reset_shared_across_service_instances(client, reset_deliveries):
    """A fresh module with no issuer globals can redeem the shared link."""
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    spec = importlib.util.spec_from_file_location("password_reset_other_worker", password_reset.__file__)
    other_worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other_worker)
    async with database.async_session() as db:
        assert await other_worker.reset_password(db, token, _NEW_PASSWORD)
        await db.commit()
    async with database.async_session() as db:
        assert not await password_reset.reset_password(db, token, _NEW_PASSWORD)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_password_reset_concurrent_consumption(client, reset_deliveries, monkeypatch):
    """Both callers read the link before contending for the real User row lock."""
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    original_verify = password_reset.verify_reset_token
    both_read = asyncio.Event()
    reads = 0

    async def coordinated_verify(value):
        nonlocal reads
        data = await original_verify(value)
        reads += 1
        if reads == 2:
            both_read.set()
        await asyncio.wait_for(both_read.wait(), 5)
        return data

    monkeypatch.setattr(password_reset, "verify_reset_token", coordinated_verify)
    responses = await asyncio.wait_for(asyncio.gather(*[
        client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": value})
        for value in (_NEW_PASSWORD, "Other-concurrent-password-789")
    ]), 10)
    assert sorted(response.status_code for response in responses) == [200, 400]
    async with database.async_session() as db:
        user = await db.scalar(select(User).where(User.email == _EMAIL))
        assert user.token_version == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_password_reset_commit_failure_keeps_link_retryable(client, reset_deliveries):
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    async with database.async_session() as db:
        assert await password_reset.reset_password(db, token, _NEW_PASSWORD)

        def fail_commit(session):
            raise RuntimeError("simulated commit failure")

        event.listen(db.sync_session, "before_commit", fail_commit, once=True)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await db.commit()
        await db.rollback()
    async with database.async_session() as db:
        user = await db.scalar(select(User).where(User.email == _EMAIL))
        assert verify_password(_OLD_PASSWORD, user.password_hash)
        assert user.token_version == 0
    retried = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": _NEW_PASSWORD},
    )
    assert retried.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("record", ["expired", "missing", "malformed"])
async def test_password_reset_invalid_token(client, reset_deliveries, record):
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    store, _ = reset_deliveries
    key = password_reset._token_key(token)
    if record == "expired":
        payload = json.loads(await store.get(key))
        payload["expires_at"] = time.time() - 1
        await store.setex(key, 3600, json.dumps(payload))
    elif record == "missing":
        await store.delete(key)
    else:
        await store.setex(key, 3600, "not-json")
    response = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": _NEW_PASSWORD},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_TOKEN


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["email", "password", "version", "deleted"])
async def test_password_reset_bound_to_current_identity(client, reset_deliveries, change):
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    async with database.async_session.begin() as db:
        user = await db.scalar(select(User).where(User.email == _EMAIL))
        if change == "email":
            user.email = "changed@example.com"
        elif change == "password":
            user.password_hash = "changed-password-fingerprint"
        elif change == "version":
            user.token_version += 1
        else:
            user.deleted_at = datetime.now(timezone.utc)
    response = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": _NEW_PASSWORD},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _INVALID_TOKEN


@pytest.mark.integration
@pytest.mark.asyncio
async def test_password_reset_policy_error_preserves_link(client, reset_deliveries, monkeypatch):
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    monkeypatch.setenv("PASSWORD_POLICY_ENFORCED", "true")
    monkeypatch.setenv("PASSWORD_MIN_LENGTH", "12")
    response = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "short"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Password must be at least 12 characters"
    response = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": _NEW_PASSWORD},
    )
    assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "write", "read", "timeout"])
async def test_password_reset_storage_failure_is_503(client, reset_deliveries, monkeypatch, failure):
    await _register_email_user(client)
    token = await _request_reset(client, reset_deliveries)
    store = await password_reset._get_redis()

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive-storage-detail")

    if failure == "unavailable":
        monkeypatch.setattr(password_reset, "_get_redis", AsyncMock(return_value=None))
    elif failure == "timeout":
        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(store, "get", hanging)
        monkeypatch.setattr(password_reset, "_STORE_TIMEOUT", 0.01)
    else:
        monkeypatch.setattr(store, {"write": "setex", "read": "get"}[failure], broken)

    if failure in {"unavailable", "write"}:
        response = await client.post("/api/v1/auth/forgot-password", json={"email": _EMAIL})
        unknown = await client.post(
            "/api/v1/auth/forgot-password", json={"email": "not-registered@example.com"},
        )
        assert (unknown.status_code, unknown.json()["detail"]) == (
            response.status_code, response.json()["detail"],
        )
    else:
        response = await client.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": _NEW_PASSWORD},
        )
    assert response.status_code == 503
    assert "sensitive-storage-detail" not in response.text
    assert "invalid_token" not in response.text
    async with database.async_session() as db:
        user = await db.scalar(select(User).where(User.email == _EMAIL))
        assert user.token_version == 0
        assert verify_password(_OLD_PASSWORD, user.password_hash)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forgot_password_mail_failure_preserves_generic_response(client, reset_deliveries, monkeypatch):
    await _register_email_user(client)
    monkeypatch.setattr(
        "packages.core.services.email_service.send_password_reset_email", AsyncMock(return_value=False),
    )
    known = await client.post("/api/v1/auth/forgot-password", json={"email": _EMAIL})
    unknown = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "not-registered@example.com"},
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
