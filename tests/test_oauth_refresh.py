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
from packages.core.credentials import CredentialDecryptError
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount
from packages.core.services.oauth_account_credentials import (
    lease_oauth_account_tokens,
    oauth_account_is_runtime_usable_clause,
    store_oauth_account_tokens,
)
from packages.core.services.integration_account_service import (
    list_runtime_integration_accounts,
)
from packages.core.services.integration_resolution import (
    connected_integration_provider_keys,
)
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


def _leased_tokens(account) -> dict[str, str]:
    return lease_oauth_account_tokens(
        account,
        requester_id="test_oauth_refresh",
        requester_kind="test",
        reason="verify encrypted OAuth token rotation",
    )


def test_reconnect_overwrites_unreadable_credentials_without_preserving_refresh(
    monkeypatch,
):
    import packages.core.services.oauth_account_credentials as credential_helpers

    account = OAuthAccount(
        id=generate_ulid(),
        user_id=generate_ulid(),
        provider="facebook",
        provider_user_id="fb-reconnect",
        credential_ref="vault:v1:unreadable",
        credential_scheme="vault_transit",
        profile={
            "oauth_refresh": {
                "reauth_required": True,
                "error": "credential_decrypt_failed",
            },
            "last_health_check": {"ok": False},
        },
    )
    stored: dict[str, str] = {}

    class FakeCredentialService:
        def store_oauth_account(self, row, payload):
            stored.update(payload)
            row.credential_ref = "vault:v1:replacement"

    def fail_if_leased(*args, **kwargs):
        raise AssertionError("reconnect must not decrypt unreadable credentials")

    monkeypatch.setattr(
        credential_helpers,
        "lease_oauth_account_tokens",
        fail_if_leased,
    )
    monkeypatch.setattr(
        credential_helpers,
        "get_credential_service",
        lambda: FakeCredentialService(),
    )

    store_oauth_account_tokens(
        account,
        access_token="fresh-access-token",
        refresh_token=None,
        requester_id=account.user_id,
    )

    assert stored == {"access_token": "fresh-access-token"}
    assert account.credential_ref == "vault:v1:replacement"
    assert "oauth_refresh" not in account.profile
    assert "last_health_check" not in account.profile


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
    assert row.access_token is None
    assert _leased_tokens(row)["access_token"] == "NEW_TOKEN"
    assert row.token_expires_at > now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_youtube_user_token_near_expiry_is_refreshed(client: AsyncClient):
    """YouTube OAuth accounts use the Google token endpoint for refresh."""
    _, user_id, _ = await _register(client, "oauth_youtube_usr")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="youtube",
                provider_user_id="yt-1",
                access_token="OLD_YOUTUBE_TOKEN",
                refresh_token="yt-rt-1",
                token_expires_at=now + timedelta(minutes=2),
            )
        )
        await db.commit()

    calls: list[dict] = []

    class _YouTubeRefreshClient(_MockHttpxClient):
        async def post(self, url, data=None, headers=None):
            calls.append({"url": url, "data": data, "headers": headers})
            return await super().post(url, data=data, headers=headers)

    def fake_client(*a, **kw):
        return _YouTubeRefreshClient(
            {
                "access_token": "NEW_YOUTUBE_TOKEN",
                "expires_in": 3600,
            }
        )

    with patch.dict(
        os.environ,
        {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csec"},
    ):
        with patch("httpx.AsyncClient", fake_client):
            async with dbmod.async_session() as db:
                n = await _refresh_oauth_accounts(db)
                await db.commit()

    assert n == 1
    assert calls[0]["url"] == "https://oauth2.googleapis.com/token"
    assert calls[0]["data"]["refresh_token"] == "yt-rt-1"
    async with dbmod.async_session() as db:
        row = (
            await db.execute(
                select(OAuthAccount).where(OAuthAccount.user_id == user_id)
            )
        ).scalar_one()
    assert _leased_tokens(row)["access_token"] == "NEW_YOUTUBE_TOKEN"
    assert row.token_expires_at > now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_twitter_user_refresh_uses_basic_auth_without_client_credentials(
    client: AsyncClient,
):
    """X refresh requests authenticate credentials only through Basic Auth."""
    _, user_id, _ = await _register(client, "oauth_twitter_refresh")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=generate_ulid(),
                user_id=user_id,
                provider="twitter_x",
                provider_user_id="x-1",
                access_token="OLD_X_TOKEN",
                refresh_token="x-rt-1",
                token_expires_at=now + timedelta(minutes=2),
            )
        )
        await db.commit()

    calls: list[dict] = []

    class _TwitterRefreshClient(_MockHttpxClient):
        async def post(self, url, data=None, headers=None):
            calls.append({"url": url, "data": data, "headers": headers})
            return await super().post(url, data=data, headers=headers)

    def fake_client(*args, **kwargs):
        return _TwitterRefreshClient(
            {"access_token": "NEW_X_TOKEN", "expires_in": 7200}
        )

    with patch.dict(
        os.environ,
        {"X_CLIENT_ID": "x-cid", "X_CLIENT_SECRET": "x-csec"},
    ):
        with patch("httpx.AsyncClient", fake_client):
            async with dbmod.async_session() as db:
                n = await _refresh_oauth_accounts(db)
                await db.commit()

    assert n == 1
    assert calls[0]["url"] == "https://api.x.com/2/oauth2/token"
    assert calls[0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "x-rt-1",
    }
    assert calls[0]["headers"]["Authorization"].startswith("Basic ")


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
    assert row.access_token is None
    assert row.refresh_token is None
    assert _leased_tokens(row) == {
        "access_token": "NEW",
        "refresh_token": "rt_NEW",
    }


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


@pytest.mark.asyncio
async def test_credential_decrypt_failure_marks_only_bad_account_for_reconnect(
    client: AsyncClient,
    monkeypatch,
    caplog,
):
    """One stale Vault ciphertext must not abort other OAuth refreshes."""
    _, bad_user_id, bad_entity_id = await _register(client, "oauth_usr_bad_cipher")
    _, good_user_id, good_entity_id = await _register(client, "oauth_usr_good_cipher")

    import packages.core.database as dbmod

    now = datetime.now(timezone.utc)
    bad_id = generate_ulid()
    good_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all(
            [
                OAuthAccount(
                    id=bad_id,
                    user_id=bad_user_id,
                    provider="gmail",
                    provider_user_id="g-bad-cipher",
                    credential_ref="vault:v1:test-unrecoverable",
                    credential_scheme="vault_transit",
                    token_expires_at=now + timedelta(minutes=1),
                ),
                OAuthAccount(
                    id=good_id,
                    user_id=good_user_id,
                    provider="gmail",
                    provider_user_id="g-good-cipher",
                    access_token="GOOD_OLD",
                    refresh_token="rt-good-cipher",
                    token_expires_at=now + timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

    original_lease = oauth_refresh_task.lease_oauth_account_tokens

    def fake_lease(row, **kwargs):
        if row.id == bad_id:
            raise CredentialDecryptError("cipher: message authentication failed")
        return original_lease(row, **kwargs)

    async def fake_refresh(*args, **kwargs):
        return {"access_token": "GOOD_NEW", "expires_in": 3600}

    monkeypatch.setattr(oauth_refresh_task, "lease_oauth_account_tokens", fake_lease)
    monkeypatch.setattr(oauth_refresh_task, "refresh_token_via_provider", fake_refresh)

    with caplog.at_level("ERROR"):
        async with dbmod.async_session() as db:
            n = await _refresh_oauth_accounts(db)
            await db.commit()

    assert n == 1
    assert not [record for record in caplog.records if record.levelno >= 40]
    async with dbmod.async_session() as db:
        bad = await db.get(OAuthAccount, bad_id)
        good = await db.get(OAuthAccount, good_id)
        runtime_usable_ids = set((await db.execute(
            select(OAuthAccount.id).where(oauth_account_is_runtime_usable_clause())
        )).scalars().all())
        bad_accounts = await list_runtime_integration_accounts(
            db,
            user_id=bad_user_id,
            entity_id=bad_entity_id,
            provider="gmail",
        )
        good_accounts = await list_runtime_integration_accounts(
            db,
            user_id=good_user_id,
            entity_id=good_entity_id,
            provider="gmail",
        )
        bad_connected = await connected_integration_provider_keys(
            db,
            entity_id=bad_entity_id,
            user_id=bad_user_id,
        )
        good_connected = await connected_integration_provider_keys(
            db,
            entity_id=good_entity_id,
            user_id=good_user_id,
        )
    assert bad.credential_ref == "vault:v1:test-unrecoverable"
    assert bad.token_expires_at is None
    assert bad.profile["oauth_refresh"]["error"] == "credential_decrypt_failed"
    assert bad.profile["last_health_check"]["ok"] is False
    assert _leased_tokens(good)["access_token"] == "GOOD_NEW"
    assert bad_id not in runtime_usable_ids
    assert good_id in runtime_usable_ids
    assert [account.id for account in bad_accounts] == []
    assert [account.id for account in good_accounts] == [good_id]
    assert "gmail" not in bad_connected
    assert "gmail" in good_connected


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
