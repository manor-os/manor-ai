"""Database-free coverage for Manor's Meta OAuth contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from packages.core.external_api_versions import META_GRAPH
from packages.core.services.oauth_flow import (
    begin_authorization,
    complete_authorization,
)
from packages.core.services.oauth_provider_config import (
    _PROVIDER_OAUTH_META,
    OAuthProviderConfig,
    apply_authorize_param_conventions,
)


EXPECTED_SCOPES = {
    "public_profile",
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_manage_engagement",
    "pages_messaging",
    "pages_manage_metadata",
    "read_insights",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
}


def test_facebook_provider_matches_reviewed_operations() -> None:
    meta = _PROVIDER_OAUTH_META["facebook"]
    assert META_GRAPH.value in meta["authorize_url"]
    assert META_GRAPH.value in meta["token_url"]
    assert set(meta["scopes"].split(",")) == EXPECTED_SCOPES
    assert "email" not in meta["scopes"]


def test_facebook_authorize_params_omit_google_and_pkce_fields() -> None:
    params = apply_authorize_param_conventions(
        SimpleNamespace(server_key="facebook"),
        {
            "client_id": "cid",
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        },
    )
    assert params == {"client_id": "cid"}


@pytest.mark.asyncio
async def test_oauth_pending_state_is_shared_and_consumed_atomically(monkeypatch) -> None:
    from packages.core.services import oauth_flow

    class _Redis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.ttls: dict[str, int] = {}

        async def set(self, key, value, *, ex, nx):
            if nx and key in self.values:
                return False
            self.values[key] = value
            self.ttls[key] = ex
            return True

        async def get(self, key):
            return self.values.get(key)

        async def eval(self, _script, _key_count, key):
            return self.values.pop(key, None)

    redis = _Redis()

    async def _shared_redis():
        return redis

    monkeypatch.setattr(oauth_flow, "_redis_client", _shared_redis)
    oauth_flow._pending_oauth_states.clear()
    config = SimpleNamespace(
        server_key="github",
        client_id="cid",
        client_secret="secret",
        authorize_url="https://github.test/oauth/authorize",
        scopes="user:email",
    )

    started = await begin_authorization(
        config=config,
        user_id="shared-user",
        entity_id="shared-entity",
        redirect_uri="https://app.example.test/oauth/callback",
    )

    assert started.state not in oauth_flow._pending_oauth_states
    pending = await oauth_flow.get_pending_state(
        started.state,
        server_key="github",
    )
    assert pending["user_id"] == "shared-user"
    assert pending["entity_id"] == "shared-entity"
    consumed = await oauth_flow._pop_pending(started.state, server_key="github")
    assert consumed == pending
    with pytest.raises(oauth_flow.OAuthFlowError) as replay:
        await oauth_flow._pop_pending(started.state, server_key="github")
    assert replay.value.status == 400
    assert list(redis.ttls.values()) == [oauth_flow._OAUTH_STATE_TTL_SECONDS]


@pytest.mark.asyncio
async def test_multiworker_oauth_refuses_process_local_state(monkeypatch) -> None:
    from packages.core.services import oauth_flow

    async def _unavailable_redis():
        return None

    monkeypatch.setattr(oauth_flow, "_redis_client", _unavailable_redis)
    monkeypatch.setattr(oauth_flow, "_shared_state_store_required", lambda: True)
    oauth_flow._pending_oauth_states.clear()

    with pytest.raises(oauth_flow.OAuthFlowError) as unavailable:
        await begin_authorization(
            config=SimpleNamespace(
                server_key="github",
                client_id="cid",
                client_secret="secret",
                authorize_url="https://github.test/oauth/authorize",
                scopes="user:email",
            ),
            user_id="multiworker-user",
            entity_id="multiworker-entity",
            redirect_uri="https://app.example.test/oauth/callback",
        )

    assert unavailable.value.status == 503
    assert oauth_flow._pending_oauth_states == {}


@pytest.mark.asyncio
async def test_process_local_oauth_state_expires_with_shared_ttl(monkeypatch) -> None:
    from packages.core.services import oauth_flow

    async def _unavailable_redis():
        return None

    now = [100.0]
    monkeypatch.setattr(oauth_flow, "_redis_client", _unavailable_redis)
    monkeypatch.setattr(oauth_flow, "_shared_state_store_required", lambda: False)
    monkeypatch.setattr(oauth_flow.time, "monotonic", lambda: now[0])
    oauth_flow._pending_oauth_states.clear()

    started = await begin_authorization(
        config=SimpleNamespace(
            server_key="github",
            client_id="cid",
            client_secret="secret",
            authorize_url="https://github.test/oauth/authorize",
            scopes="user:email",
        ),
        user_id="local-user",
        entity_id="local-entity",
        redirect_uri="https://app.example.test/oauth/callback",
    )
    assert started.state in oauth_flow._pending_oauth_states

    now[0] += oauth_flow._OAUTH_STATE_TTL_SECONDS
    with pytest.raises(oauth_flow.OAuthFlowError) as expired:
        await oauth_flow.get_pending_state(started.state, server_key="github")
    assert expired.value.status == 400
    assert started.state not in oauth_flow._pending_oauth_states


@pytest.mark.asyncio
async def test_facebook_code_exchange_returns_long_lived_token() -> None:
    meta = _PROVIDER_OAUTH_META["facebook"]
    config = OAuthProviderConfig(
        server_key="facebook",
        client_id="cid",
        client_secret="secret",
        authorize_url=meta["authorize_url"],
        token_url=meta["token_url"],
        scopes=meta["scopes"],
        redirect_path="/api/v1/integrations/oauth/facebook/callback",
        source="test",
    )
    started = await begin_authorization(
        config=config,
        user_id="user-1",
        redirect_uri="https://app.manorai.xyz/api/v1/integrations/oauth/facebook/callback",
    )
    query = parse_qs(urlparse(started.authorize_url).query)
    assert set(query["scope"][0].split(",")) == EXPECTED_SCOPES
    assert "code_challenge" not in query

    calls: list[dict[str, str]] = []

    class _Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def json(self) -> dict[str, object]:
            return self.payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params=None, headers=None):
            calls.append(dict(params or {}))
            if (params or {}).get("grant_type") == "fb_exchange_token":
                return _Response(
                    {"access_token": "long", "expires_in": 5_184_000}
                )
            return _Response({"access_token": "short", "expires_in": 3_600})

    with patch("httpx.AsyncClient", lambda *args, **kwargs: _Client()):
        user_id, tokens = await complete_authorization(
            server_key="facebook",
            code="code",
            state=started.state,
            redirect_uri=(
                "https://app.manorai.xyz/api/v1/integrations/oauth/facebook/callback"
            ),
            config=config,
        )

    assert user_id == "user-1"
    assert tokens.access_token == "long"
    assert tokens.expires_at is not None
    assert any(call.get("fb_exchange_token") == "short" for call in calls)
