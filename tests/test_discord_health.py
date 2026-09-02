from __future__ import annotations

from types import SimpleNamespace

import pytest

import packages.core.services.integration_health as integration_health
import packages.core.services.oauth_account_credentials as oauth_credentials
from packages.core.config import get_settings
from packages.core.services.discord_app_config import DiscordAppConfig


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _DB:
    def __init__(self, row):
        self._row = row
        self.flush_count = 0

    async def execute(self, *_args, **_kwargs):
        return _Result(self._row)

    async def flush(self):
        self.flush_count += 1


class _Response:
    status_code = 200
    text = ""
    is_success = True

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_discord_oauth_health_uses_deployment_bot_for_connected_guild(
    monkeypatch,
) -> None:
    expected_endpoint = (
        f"{get_settings().PUBLIC_BASE_URL.rstrip('/')}"
        "/api/v1/channels/discord/interactions"
    )
    row = SimpleNamespace(
        id="discord-oauth-1",
        provider="discord",
        profile={
            "application_id": "discord-app-id",
            "guild_id": "guild-123",
            "guild_name": "Manor staging QA",
        },
    )
    db = _DB(row)
    leased_tokens = {
        "access_token": "discord-oauth-access-token",
        "refresh_token": "discord-oauth-refresh-token",
    }
    requests: list[tuple[str, dict[str, str]]] = []

    monkeypatch.setattr(
        oauth_credentials,
        "lease_oauth_account_tokens",
        lambda *_args, **_kwargs: leased_tokens,
    )

    async def _resolve_app(_db):
        return DiscordAppConfig(
            application_id="discord-app-id",
            bot_token="deployment-bot-token",
            public_key="11" * 32,
        )

    monkeypatch.setattr(
        "packages.core.services.discord_app_config.resolve_discord_app_config",
        _resolve_app,
    )

    async def _get(url: str, *, headers=None, timeout=10):
        requests.append((url, dict(headers or {})))
        if url.endswith("/users/@me"):
            return _Response({"username": "Manor staging", "discriminator": "0"})
        if url.endswith("/guilds/guild-123"):
            return _Response({"id": "guild-123", "name": "Manor staging QA"})
        if url.endswith("/applications/@me"):
            return _Response({"interactions_endpoint_url": expected_endpoint})
        raise AssertionError(f"unexpected Discord health URL: {url}")

    monkeypatch.setattr(integration_health, "_http_get", _get)

    result = await integration_health.run_and_persist_oauth(db, row.id)

    assert result["ok"] is True
    assert "Manor staging QA" in result["detail"]
    assert result["wiring"]["ok"] is True
    assert result["wiring"]["expected_url"] == expected_endpoint
    assert any(url.endswith("/guilds/guild-123") for url, _headers in requests)
    assert all(
        headers["Authorization"] == "Bot deployment-bot-token"
        for _url, headers in requests
    )
    assert "bot_token" not in leased_tokens
    assert row.profile["last_health_check"] == result
    assert db.flush_count == 1
