"""Tests for the OAuth refresh task.

Covers:
  * User-scope tokens near expiry get refreshed
  * Far-future tokens stay untouched
  * Missing refresh_token → row skipped, no error
  * Provider returns HTTP error → row skipped, logs warning
  * Rotated refresh_token is persisted
  * Entity-scope tokens in integrations.credentials refreshed
  * Stripe (api_key, not OAuth) never scanned
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.tasks import oauth_refresh as oauth_refresh_task
from packages.core.tasks.oauth_refresh import (
    _refresh_integrations,
    _refresh_oauth_accounts,
    refresh_token_via_provider,
)


async def _register(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    return headers, data["user_id"], me.json()["entity_id"]


def _mock_response(status_code=200, json_body=None):
    class _R:
        def __init__(self):
            self.status_code = status_code
            self.text = ""

        def json(self):
            return json_body or {}

    return _R()


class _MockHttpxClient:
    """Replaces httpx.AsyncClient for token-endpoint calls."""

    def __init__(self, response_data):
        self._response_data = response_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, data=None, headers=None):
        return _mock_response(200, self._response_data)


# ── User-scope refresh ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_token_near_expiry_is_refreshed(client: AsyncClient):
    """A token expiring in <5 minutes gets a fresh access_token."""
    _, user_id, _ = await _register(client, "oauth_usr_1")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="gmail",
                provider_user_id="g-1",
                access_token="OLD_TOKEN",
                refresh_token="rt-1",
                token_expires_at=now + timedelta(minutes=2),  # expiring soon
            )
        )
        await db.commit()

    def fake_client(*a, **kw):
        return _MockHttpxClient(
            {
                "access_token": "NEW_TOKEN",
                "expires_in": 3600,
            }
        )

    with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec"}):
        with patch("httpx.AsyncClient", fake_client):
            async with dbmod.async_session() as db:
                n = await _refresh_oauth_accounts(db)
                await db.commit()

    assert n == 1
    async with dbmod.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
    assert row.access_token == "NEW_TOKEN"
    assert row.token_expires_at > now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_user_token_far_future_untouched(client: AsyncClient):
    """Token expiring in 10 hours is not refreshed."""
    _, user_id, _ = await _register(client, "oauth_usr_2")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="gmail",
                provider_user_id="g-2",
                access_token="KEEP",
                refresh_token="rt-2",
                token_expires_at=datetime.now(timezone.utc) + timedelta(hours=10),
            )
        )
        await db.commit()

    with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec"}):
        async with dbmod.async_session() as db:
            n = await _refresh_oauth_accounts(db)
    assert n == 0


@pytest.mark.asyncio
async def test_user_token_without_refresh_token_skipped(client: AsyncClient):
    """No refresh_token in the row → can't refresh, row skipped silently."""
    _, user_id, _ = await _register(client, "oauth_usr_3")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="gmail",
                provider_user_id="g-3",
                access_token="only-access",
                refresh_token=None,
                token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        await db.commit()

    async with dbmod.async_session() as db:
        n = await _refresh_oauth_accounts(db)
    assert n == 0


@pytest.mark.asyncio
async def test_rotated_refresh_token_persisted(client: AsyncClient):
    """Provider returning a new refresh_token updates the stored one."""
    _, user_id, _ = await _register(client, "oauth_usr_rot")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="linkedin",
                provider_user_id="li-1",
                access_token="old",
                refresh_token="rt_OLD",
                token_expires_at=now + timedelta(minutes=1),
            )
        )
        await db.commit()

    def fake_client(*a, **kw):
        return _MockHttpxClient(
            {
                "access_token": "NEW",
                "refresh_token": "rt_NEW",
                "expires_in": 7200,
            }
        )

    with patch.dict(os.environ, {"LINKEDIN_CLIENT_ID": "cid", "LINKEDIN_CLIENT_SECRET": "csec"}):
        with patch("httpx.AsyncClient", fake_client):
            async with dbmod.async_session() as db:
                await _refresh_oauth_accounts(db)
                await db.commit()

    async with dbmod.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
    assert row.access_token == "NEW"
    assert row.refresh_token == "rt_NEW"


@pytest.mark.asyncio
async def test_permanent_user_refresh_error_marks_reauth_required(
    client: AsyncClient,
    monkeypatch,
):
    """Invalid refresh tokens are marked for reconnect and not retried."""
    _, user_id, _ = await _register(client, "oauth_usr_reauth")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="twitter_x",
                provider_user_id="x-1",
                access_token="OLD",
                refresh_token="rt_bad",
                token_expires_at=now + timedelta(minutes=1),
            )
        )
        await db.commit()

    calls = 0

    async def fake_refresh(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {
            oauth_refresh_task._PERMANENT_REFRESH_ERROR_KEY: {
                "error": "invalid_request",
                "description": "Value passed for the token was invalid.",
                "status_code": 400,
            }
        }

    monkeypatch.setattr(oauth_refresh_task, "refresh_token_via_provider", fake_refresh)

    async with dbmod.async_session() as db:
        n = await _refresh_oauth_accounts(db)
        await db.commit()

    assert n == 0
    assert calls == 1
    async with dbmod.async_session() as db:
        row = (await db.execute(select(OAuthAccount).where(OAuthAccount.user_id == user_id))).scalar_one()
    assert row.access_token is None
    assert row.refresh_token is None
    assert row.token_expires_at is None
    assert row.profile["oauth_refresh"]["reauth_required"] is True
    assert row.profile["last_health_check"]["ok"] is False

    async with dbmod.async_session() as db:
        n = await _refresh_oauth_accounts(db)
    assert n == 0
    assert calls == 1


# ── Entity-scope refresh ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entity_integration_refreshed(client: AsyncClient):
    """Integration row with expiring creds.refresh_token gets refreshed."""
    _, _, entity_id = await _register(client, "oauth_ent_1")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            Integration(
                id=generate_ulid(),
                entity_id=entity_id,
                provider="quickbooks",
                status="active",
                config={},
                credentials={
                    "access_token": "old_qb",
                    "refresh_token": "rt_qb",
                    "expires_at": (now + timedelta(minutes=2)).isoformat(),
                },
            )
        )
        await db.commit()

    def fake_client(*a, **kw):
        return _MockHttpxClient(
            {
                "access_token": "NEW_QB",
                "refresh_token": "rt_qb_new",
                "expires_in": 3600,
            }
        )

    with patch.dict(
        os.environ,
        {
            "QUICKBOOKS_CLIENT_ID": "qb_id",
            "QUICKBOOKS_CLIENT_SECRET": "qb_sec",
        },
    ):
        with patch("httpx.AsyncClient", fake_client):
            async with dbmod.async_session() as db:
                n = await _refresh_integrations(db)
                await db.commit()

    assert n == 1
    async with dbmod.async_session() as db:
        row = (await db.execute(select(Integration).where(Integration.entity_id == entity_id))).scalar_one()
    assert row.credentials["access_token"] == "NEW_QB"
    assert row.credentials["refresh_token"] == "rt_qb_new"
    # expires_at should now be far future
    from datetime import datetime as _dt

    new_exp = _dt.fromisoformat(row.credentials["expires_at"].replace("Z", "+00:00"))
    assert new_exp > now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_stripe_never_refreshed(client: AsyncClient):
    """Stripe uses static api_key — no refresh logic runs for it."""
    _, _, entity_id = await _register(client, "oauth_ent_2")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        db.add(
            Integration(
                id=generate_ulid(),
                entity_id=entity_id,
                provider="stripe",
                status="active",
                config={},
                credentials={"secret_key": "sk_live_x"},  # no refresh_token
            )
        )
        await db.commit()

    async with dbmod.async_session() as db:
        n = await _refresh_integrations(db)
    assert n == 0


@pytest.mark.asyncio
async def test_permanent_integration_refresh_error_marks_reauth_required(
    client: AsyncClient,
    monkeypatch,
):
    """Entity-scope invalid refresh tokens are cleared after a permanent error."""
    _, _, entity_id = await _register(client, "oauth_ent_reauth")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            Integration(
                id=generate_ulid(),
                entity_id=entity_id,
                provider="quickbooks",
                status="active",
                config={},
                credentials={
                    "access_token": "old_qb",
                    "refresh_token": "rt_bad",
                    "expires_at": (now + timedelta(minutes=2)).isoformat(),
                    "realm_id": "realm_1",
                },
            )
        )
        await db.commit()

    calls = 0

    async def fake_refresh(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {
            oauth_refresh_task._PERMANENT_REFRESH_ERROR_KEY: {
                "error": "invalid_grant",
                "description": "Refresh token revoked.",
                "status_code": 400,
            }
        }

    monkeypatch.setattr(oauth_refresh_task, "refresh_token_via_provider", fake_refresh)

    async with dbmod.async_session() as db:
        n = await _refresh_integrations(db)
        await db.commit()

    assert n == 0
    assert calls == 1
    async with dbmod.async_session() as db:
        row = (await db.execute(select(Integration).where(Integration.entity_id == entity_id))).scalar_one()
    assert row.credentials == {"realm_id": "realm_1"}
    assert row.config["oauth_refresh"]["reauth_required"] is True
    assert row.config["last_health_check"]["ok"] is False

    async with dbmod.async_session() as db:
        n = await _refresh_integrations(db)
    assert n == 0
    assert calls == 1


# ── Provider call path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_skips_unknown_provider():
    """Unknown provider → None, no HTTP call."""
    out = await refresh_token_via_provider("obscure_provider", "rt")
    assert out is None


@pytest.mark.asyncio
async def test_refresh_skips_when_env_not_set(monkeypatch):
    """Provider configured but client_id/secret env missing → skip without error."""
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    out = await refresh_token_via_provider("gmail", "rt")
    assert out is None
