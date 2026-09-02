"""Integration test: the new video-platform MCP servers surface through the
real ``GET /api/v1/integrations/mcp-servers`` endpoint — the same API the web
Integrations page consumes.

Boots the actual FastAPI app + a real Postgres (via the conftest ``client``
fixture, which runs ``seed_mcp_catalog``), registers a user, and asserts the
catalog the frontend renders includes youtube + tiktok with the expected
shape (server_key, oauth2 auth, scopes), and that Instagram Reels remains
reachable via the facebook card.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event


async def _register(client: AsyncClient, username: str) -> str:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": "Catalog Test Co",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _register_with_ids(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": "Catalog Test Co",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_mcp_catalog_exposes_youtube_and_tiktok(client: AsyncClient):
    token = await _register(client, "catalog_user")
    resp = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    catalog = {row["server_key"]: row for row in resp.json()}

    # The two new servers must be in the catalog the frontend renders.
    assert "youtube" in catalog, "youtube missing from MCP catalog endpoint"
    assert "tiktok" in catalog, "tiktok missing from MCP catalog endpoint"

    yt = catalog["youtube"]
    assert yt["auth_type"] == "oauth2"
    assert yt["name"] == "YouTube"
    assert "youtube.force-ssl" in (yt.get("scopes") or "")
    assert "youtube.readonly" not in (yt.get("scopes") or "")
    # response_model contract the web client relies on
    for field in (
        "server_key",
        "server_kind",
        "name",
        "auth_type",
        "agent_can_use",
        "hint",
    ):
        assert field in yt
    assert yt["server_kind"] == "managed"

    tk = catalog["tiktok"]
    assert tk["auth_type"] == "oauth2"
    assert tk["name"] == "TikTok"
    assert "video.publish" in (tk.get("scopes") or "")

    for remote_key in ("stripe", "paypal"):
        assert catalog[remote_key]["server_kind"] == "managed"
        assert catalog[remote_key]["supports_multi_account"] is True

    # Instagram Reels publishing rides on the facebook card — confirm it's
    # still in the catalog so the capability is reachable from the UI.
    assert "facebook" in catalog


@pytest.mark.asyncio
async def test_mcp_catalog_exposes_ecommerce_platforms(client: AsyncClient):
    token = await _register(client, "shop_user")
    resp = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    catalog = {row["server_key"]: row for row in resp.json()}

    for key, name in (
        ("shopify", "Shopify"),
        ("woocommerce", "WooCommerce"),
        ("square", "Square"),
        ("tiktok_shop", "TikTok Shop"),
        ("amazon", "Amazon (Selling Partner)"),
    ):
        assert key in catalog, f"{key} missing from MCP catalog endpoint"
        row = catalog[key]
        # Store credentials (domain + token / key+secret), not OAuth.
        assert row["auth_type"] == "credentials"
        assert row["name"] == name
        for field in ("server_key", "name", "auth_type", "agent_can_use", "hint"):
            assert field in row


@pytest.mark.asyncio
async def test_mcp_catalog_exposes_truthful_nango_capability_metadata(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router

    async def _nango_provider_configs(*args, **kwargs):
        return {
            "gmail": "gmail",
            "outlook": "outlook",
            "twitter": "twitter",
            "linear": "linear",
        }

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _nango_provider_configs,
    )
    token = await _register(client, "catalog_nango_capability_metadata")
    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    catalog = {row["server_key"]: row for row in response.json()}
    assert catalog["gmail"]["nango_mode"] == "oauth_saas"
    assert catalog["gmail"]["nango_operations"] == {
        "read": True,
        "write": True,
        "inbound": False,
    }
    assert catalog["outlook"]["nango_mode"] == "bidirectional_chat"
    assert catalog["outlook"]["nango_operations"]["inbound"] is True
    assert catalog["twitter_x"]["nango_provider_config_key"] == "twitter"
    assert catalog["twitter_x"]["nango_mode"] == "oauth_saas"
    assert catalog["twitter_x"]["nango_operations"] == {
        "read": True,
        "write": True,
        "inbound": False,
    }
    # Unknown Nango providers remain visible as OAuth cards, but do not claim
    # operations Manor has not explicitly implemented.
    assert catalog["linear"]["nango_mode"] is None
    assert catalog["linear"]["nango_operations"] == {}


@pytest.mark.asyncio
async def test_mcp_catalog_requires_auth(client: AsyncClient):
    # The endpoint the frontend calls is user-scoped; no token → 401.
    resp = await client.get("/api/v1/integrations/mcp-servers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_catalog_lists_every_authorized_entity_account_default_first(
    client: AsyncClient,
    monkeypatch,
):
    """The UI catalog must preserve the registry's multi-account ordering."""
    from apps.api.routers import integrations as integration_router

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    token = await _register(client, "catalog_multi_entity_accounts")
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Primary webhook", "url": "https://one.example.test"},
            "credentials": {"bearer_token": "primary-secret"},
        },
    )
    second = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "webhook",
            "config": {"name": "Secondary webhook", "url": "https://two.example.test"},
            "credentials": {"bearer_token": "secondary-secret"},
        },
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    switched = await client.post(
        "/api/v1/integrations/mcp-servers/webhook/"
        f"entity-accounts/{second.json()['id']}/set-default",
        headers=headers,
    )
    assert switched.status_code == 204, switched.text

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    accounts = webhook["entity_accounts"]
    assert [account["id"] for account in accounts] == [
        second.json()["id"],
        first.json()["id"],
    ]
    assert [account["is_default"] for account in accounts] == [True, False]
    assert all(account["ownership"] == "mine" for account in accounts)
    assert all(account["can_manage"] is True for account in accounts)
    display_names = {account["id"]: account["display_name"] for account in accounts}
    assert display_names == {
        first.json()["id"]: "Primary webhook",
        second.json()["id"]: "Secondary webhook",
    }
    assert "https://one.example.test" not in response.text
    assert "https://two.example.test" not in response.text
    assert "primary-secret" not in response.text
    assert "secondary-secret" not in response.text


@pytest.mark.asyncio
async def test_mcp_catalog_bulk_loads_account_registry_once(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router

    original = integration_router.load_integration_catalog_accounts
    calls = 0

    async def _counted_catalog_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        integration_router,
        "load_integration_catalog_accounts",
        _counted_catalog_load,
    )
    token = await _register(client, "catalog_single_registry_load")

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert calls == 1


@pytest.mark.asyncio
async def test_mcp_catalog_serializes_user_oauth_and_nango_entity_accounts(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_nango_entity_account")
    user_oauth_id = generate_ulid()
    secondary_oauth_id = generate_ulid()
    entity_account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            OAuthAccount(
                id=user_oauth_id,
                user_id=owner["user_id"],
                provider="linkedin",
                provider_user_id="linkedin-user",
                access_token="oauth-token",
                profile={"email": "member@example.test", "is_default": True},
            ),
            OAuthAccount(
                id=secondary_oauth_id,
                user_id=owner["user_id"],
                provider="linkedin",
                provider_user_id="linkedin-secondary-user",
                access_token="secondary-oauth-token",
                profile={
                    "email": "secondary@example.test",
                    "is_default": False,
                },
            ),
            Integration(
                id=entity_account_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                provider="linkedin",
                status="active",
                config={
                    "nango": {
                        "connection_id": "nango-linkedin-connection",
                        "provider_config_key": "linkedin",
                    }
                },
                credentials={},
            ),
        ])
        await db.commit()

    switched = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"connections/{secondary_oauth_id}/set-default",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert switched.status_code == 204, switched.text

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    assert [connection["id"] for connection in linkedin["connections"]] == [
        secondary_oauth_id,
        user_oauth_id,
    ]
    assert [connection["is_default"] for connection in linkedin["connections"]] == [
        True,
        False,
    ]
    entity_account = next(
        account
        for account in linkedin["entity_accounts"]
        if account["id"] == entity_account_id
    )
    assert entity_account["nango_backed"] is True


@pytest.mark.asyncio
async def test_provider_default_switches_across_oauth_and_entity_accounts(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount, User
    from packages.core.services.integration_account_service import (
        load_runtime_integration_registry,
    )

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_cross_kind_default")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    entity_created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "linkedin",
            "config": {"name": "Entity LinkedIn"},
            "credentials": {"access_token": "entity-linkedin-token"},
        },
    )
    assert entity_created.status_code == 201, entity_created.text
    entity_account_id = entity_created.json()["id"]
    oauth_account_id = generate_ulid()
    async with dbmod.async_session() as db:
        user_row = await db.get(User, owner["user_id"])
        assert user_row is not None
        user_row.preferences = {"unrelated_preference": "preserved"}
        db.add(OAuthAccount(
            id=oauth_account_id,
            user_id=owner["user_id"],
            provider="linkedin",
            provider_user_id="cross-kind-linkedin-user",
            access_token="oauth-linkedin-token",
            token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            profile={"email": "oauth@example.test", "is_default": True},
        ))
        await db.commit()

    oauth_default = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"connections/{oauth_account_id}/set-default",
        headers=headers,
    )
    assert oauth_default.status_code == 404, oauth_default.text
    entity_default = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"entity-accounts/{entity_account_id}/set-default",
        headers=headers,
    )
    assert entity_default.status_code == 204, entity_default.text

    async with dbmod.async_session() as db:
        registry = await load_runtime_integration_registry(
            db,
            user_id=owner["user_id"],
            entity_id=owner["entity_id"],
            provider_keys=["linkedin"],
        )
        user_row = await db.get(User, owner["user_id"])
        assert user_row is not None
        preferences = user_row.preferences
    binding = registry.integration("linkedin")
    assert binding is not None
    assert binding.default_account_id == entity_account_id
    assert sum(account.is_default for account in binding.accounts) == 1
    assert preferences["unrelated_preference"] == "preserved"
    assert preferences["integration_account_defaults"][owner["entity_id"]][
        "linkedin"
    ] == {
        "kind": "integration",
        "account_id": entity_account_id,
    }

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    combined = [*linkedin["connections"], *linkedin["entity_accounts"]]
    assert [row["id"] for row in combined if row["is_default"]] == [
        entity_account_id,
    ]
    assert linkedin["agent_can_use"] is True

    # A stale persisted preference must not let an expired account hide a
    # healthy sibling. This simulates an account expiring after it was chosen.
    async with dbmod.async_session() as db:
        user_row = await db.get(User, owner["user_id"])
        assert user_row is not None
        preferences = dict(user_row.preferences or {})
        defaults = dict(preferences.get("integration_account_defaults") or {})
        entity_defaults = dict(defaults.get(owner["entity_id"]) or {})
        entity_defaults["linkedin"] = {
            "kind": "oauth_account",
            "account_id": oauth_account_id,
        }
        defaults[owner["entity_id"]] = entity_defaults
        preferences["integration_account_defaults"] = defaults
        user_row.preferences = preferences
        await db.commit()

    async with dbmod.async_session() as db:
        registry = await load_runtime_integration_registry(
            db,
            user_id=owner["user_id"],
            entity_id=owner["entity_id"],
            provider_keys=["linkedin"],
        )
    binding = registry.integration("linkedin")
    assert binding is not None
    assert binding.default_account_id == entity_account_id
    assert sum(account.is_default for account in binding.accounts) == 1

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    by_id = {
        row["id"]: row
        for row in [*linkedin["connections"], *linkedin["entity_accounts"]]
    }
    assert by_id[oauth_account_id]["runtime_callable"] is False
    assert by_id[oauth_account_id]["availability"] == "reconnect_required"
    assert by_id[entity_account_id]["is_default"] is True
    assert linkedin["agent_can_use"] is True


@pytest.mark.asyncio
async def test_general_preferences_cannot_overwrite_integration_account_default(
    client: AsyncClient,
):
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount, User
    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        set_default_runtime_integration_account,
    )
    from packages.core.services.settings_service import (
        get_user_preferences,
        update_user_preferences,
    )

    owner = await _register_with_ids(client, "catalog_preference_concurrency")
    oauth_id = generate_ulid()
    async with dbmod.async_session() as db:
        user = await db.get(User, owner["user_id"])
        assert user is not None
        user.preferences = {"unrelated_preference": "preserved"}
        db.add(OAuthAccount(
            id=oauth_id,
            user_id=owner["user_id"],
            provider="gmail",
            provider_user_id="preference-race@example.test",
            access_token="preference-race-token",
            profile={"email": "preference-race@example.test"},
        ))
        await db.commit()
    # Keep a stale ORM instance alive to reproduce the old whole-document
    # JSONB overwrite after the account-default transaction commits.
    async with dbmod.async_session() as stale_db:
        stale_user = await stale_db.get(User, owner["user_id"])
        assert stale_user is not None
        assert await get_user_preferences(stale_db, owner["user_id"]) == {
            "unrelated_preference": "preserved",
        }

        async with dbmod.async_session() as default_db:
            assert await set_default_runtime_integration_account(
                default_db,
                kind=IntegrationAccountKind.OAUTH_ACCOUNT,
                user_id=owner["user_id"],
                entity_id=owner["entity_id"],
                provider="gmail",
                account_id=oauth_id,
            )
            await default_db.commit()

        # Preferences are transaction-local, so a reader after commit cannot
        # observe a stale cache entry repopulated before commit.
        async with dbmod.async_session() as read_db:
            after_default = await get_user_preferences(read_db, owner["user_id"])
        assert after_default["integration_account_defaults"][owner["entity_id"]][
            "gmail"
        ]["account_id"] == oauth_id

        await update_user_preferences(
            stale_db,
            owner["user_id"],
            {
                "dashboard_layout": {"mode": "grid"},
                # Public preference writes cannot forge the reserved pointer.
                "integration_account_defaults": {"forged": True},
            },
        )
        await stale_db.commit()

    async with dbmod.async_session() as db:
        preferences = await get_user_preferences(db, owner["user_id"])
    assert preferences["unrelated_preference"] == "preserved"
    assert preferences["dashboard_layout"] == {"mode": "grid"}
    assert preferences["integration_account_defaults"][owner["entity_id"]][
        "gmail"
    ] == {"kind": "oauth_account", "account_id": oauth_id}


@pytest.mark.asyncio
async def test_user_preferences_are_read_from_the_transaction_not_cache(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as dbmod
    from packages.core.models.user import User
    from packages.core.services import settings_service

    owner = await _register_with_ids(client, "catalog_preferences_no_cache")
    async with dbmod.async_session() as db:
        user = await db.get(User, owner["user_id"])
        assert user is not None
        user.preferences = {"fresh": "database"}
        await db.commit()

    async def _cache_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("user preferences must not read the shared cache")

    async def _cache_must_not_be_written(*_args, **_kwargs):
        raise AssertionError("user preferences must not write the shared cache")

    monkeypatch.setattr(settings_service.cache, "get", _cache_must_not_be_read)
    monkeypatch.setattr(settings_service.cache, "set", _cache_must_not_be_written)

    async with dbmod.async_session() as db:
        preferences = await settings_service.get_user_preferences(
            db,
            owner["user_id"],
        )

    assert preferences == {"fresh": "database"}


@pytest.mark.asyncio
async def test_oauth_default_ignores_rejected_non_default_entity_health(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_oauth_default_health")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    entity_id = generate_ulid()
    oauth_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            Integration(
                id=entity_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                provider="linkedin",
                status="active",
                config={
                    "name": "Rejected entity account",
                    "last_health_check": {
                        "ok": False,
                        "detail": "Provider rejected the stored credentials",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                credentials={"access_token": "rejected-entity-token"},
            ),
            OAuthAccount(
                id=oauth_id,
                user_id=owner["user_id"],
                provider="linkedin",
                provider_user_id="healthy-oauth-default",
                access_token="healthy-oauth-token",
                profile={"email": "healthy@example.test", "is_default": True},
            ),
        ])
        await db.commit()

    switched = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"connections/{oauth_id}/set-default",
        headers=headers,
    )
    assert switched.status_code == 204, switched.text

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    assert linkedin["agent_can_use"] is True
    assert next(
        account
        for account in linkedin["entity_accounts"]
        if account["id"] == entity_id
    )["health"]["ok"] is False


@pytest.mark.asyncio
async def test_mcp_catalog_bulk_authorization_has_constant_grant_queries(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import User, UserMembership
    from packages.core.services.auth_service import create_access_token, hash_password
    from packages.core.services.integration_access import grant_connection_use

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_bulk_share_owner")
    grantee_id = generate_ulid()
    account_ids = [generate_ulid() for _ in range(4)]
    async with dbmod.async_session() as db:
        db.add_all([
            User(
                id=grantee_id,
                entity_id=owner["entity_id"],
                email="catalog_bulk_share_grantee@example.test",
                display_name="Catalog Bulk Grantee",
                password_hash=hash_password("securepass123"),
                role="member",
                status="active",
            ),
            UserMembership(
                id=generate_ulid(),
                user_id=grantee_id,
                entity_id=owner["entity_id"],
                role="member",
                status="active",
                is_primary=True,
            ),
            *[
                Integration(
                    id=account_id,
                    entity_id=owner["entity_id"],
                    owner_user_id=owner["user_id"],
                    provider="linkedin",
                    status="active",
                    config={"name": f"Shared LinkedIn {index}"},
                    credentials={"access_token": f"shared-token-{index}"},
                )
                for index, account_id in enumerate(account_ids)
            ],
        ])
        await db.flush()
        for account_id in account_ids:
            await grant_connection_use(
                db,
                kind="integration",
                connection_id=account_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                grantee_user_id=grantee_id,
            )
        await db.commit()

    statements: list[str] = []

    def _record_statement(_conn, _cursor, statement, *_args):
        if "resource_grants" in statement.lower():
            statements.append(statement)

    event.listen(dbmod.engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        response = await client.get(
            "/api/v1/integrations/mcp-servers",
            headers={
                "Authorization": "Bearer "
                + create_access_token(grantee_id, owner["entity_id"], "member")
            },
        )
    finally:
        event.remove(
            dbmod.engine.sync_engine,
            "before_cursor_execute",
            _record_statement,
        )

    assert response.status_code == 200, response.text
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    assert {row["id"] for row in linkedin["entity_accounts"]} >= set(account_ids)
    assert len(statements) == 3


@pytest.mark.asyncio
async def test_mcp_catalog_requires_reconnect_when_nango_connection_is_missing(
    client: AsyncClient,
):
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    owner = await _register_with_ids(client, "catalog_missing_nango_connection")
    entity_account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(
            Integration(
                id=entity_account_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                provider="linkedin",
                status="active",
                config={
                    "nango": {
                        "connection_id": "missing-nango-linkedin-connection",
                        "provider_config_key": "linkedin",
                    }
                },
                credentials={},
            )
        )
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    linkedin = next(row for row in response.json() if row["server_key"] == "linkedin")
    assert linkedin["entity_connected"] is False
    assert linkedin["agent_can_use"] is False
    assert "Nango connection is no longer available" in linkedin["hint"]
    assert len(linkedin["entity_accounts"]) == 1
    account = linkedin["entity_accounts"][0]
    assert account["id"] == entity_account_id
    assert account["runtime_callable"] is False
    assert account["availability"] == "reconnect_required"

    set_default = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"entity-accounts/{entity_account_id}/set-default",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert set_default.status_code == 404, set_default.text


@pytest.mark.asyncio
async def test_mcp_catalog_hides_private_accounts_until_the_owner_shares_them(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount, User, UserMembership
    from packages.core.services.auth_service import create_access_token, hash_password
    from packages.core.services.integration_access import grant_connection_use

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_private_connection_owner")
    other_id = generate_ulid()
    connection_id = generate_ulid()
    oauth_id = generate_ulid()
    nango_connection_id = (
        f"{owner['entity_id']}--{owner['user_id']}--linkedin--{generate_ulid()}"
    )
    async with dbmod.async_session() as db:
        db.add_all([
            User(
                id=other_id,
                entity_id=owner["entity_id"],
                email="catalog_private_connection_other@example.test",
                display_name="Catalog Other",
                password_hash=hash_password("securepass123"),
                role="owner",
                status="active",
            ),
            UserMembership(
                user_id=other_id,
                entity_id=owner["entity_id"],
                role="owner",
                status="active",
                is_primary=True,
            ),
            Integration(
                id=connection_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                provider="linkedin",
                status="active",
                config={
                    "name": "Owner LinkedIn",
                    "nango": {
                        "connection_id": nango_connection_id,
                        "provider_config_key": "linkedin",
                    },
                },
                credentials={},
            ),
            OAuthAccount(
                id=oauth_id,
                user_id=owner["user_id"],
                provider="linkedin",
                provider_user_id="shared-linkedin-oauth",
                access_token="shared-linkedin-token",
                profile={"name": "Owner LinkedIn OAuth"},
            ),
        ])
        await db.commit()

        other_token = create_access_token(other_id, owner["entity_id"], "owner")
        before_share = await client.get(
            "/api/v1/integrations/mcp-servers",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert before_share.status_code == 200, before_share.text
        before_linkedin = next(
            row for row in before_share.json() if row["server_key"] == "linkedin"
        )
        assert connection_id not in {
            account["id"] for account in before_linkedin["entity_accounts"]
        }
        assert oauth_id not in {
            account["id"] for account in before_linkedin["connections"]
        }

        await grant_connection_use(
            db,
            kind="integration",
            connection_id=connection_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            grantee_user_id=other_id,
        )
        await grant_connection_use(
            db,
            kind="oauth_account",
            connection_id=oauth_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            grantee_user_id=other_id,
        )
        await db.commit()

        after_share = await client.get(
            "/api/v1/integrations/mcp-servers",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        shared_integrations = await client.get(
            "/api/v1/integrations",
            headers={"Authorization": f"Bearer {other_token}"},
        )

    assert after_share.status_code == 200, after_share.text
    after_linkedin = next(
        row for row in after_share.json() if row["server_key"] == "linkedin"
    )
    shared = next(
        account
        for account in after_linkedin["entity_accounts"]
        if account["id"] == connection_id
    )
    assert shared["ownership"] == "shared"
    shared_oauth = next(
        account
        for account in after_linkedin["connections"]
        if account["id"] == oauth_id
    )
    assert shared_oauth["ownership"] == "shared"
    assert shared_oauth["can_manage"] is False
    assert shared_integrations.status_code == 200, shared_integrations.text
    shared_row = next(
        row for row in shared_integrations.json() if row["id"] == connection_id
    )
    assert shared_row["ownership"] == "shared"
    assert shared_row["can_manage"] is False
    assert shared_row["config"]["name"] == "Owner LinkedIn"
    assert "nango" not in shared_row["config"]

    selected = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"entity-accounts/{connection_id}/set-default",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert selected.status_code == 204, selected.text

    selected_catalog = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    selected_linkedin = next(
        row
        for row in selected_catalog.json()
        if row["server_key"] == "linkedin"
    )
    selected_shared = next(
        account
        for account in selected_linkedin["entity_accounts"]
        if account["id"] == connection_id
    )
    assert selected_shared["ownership"] == "shared"
    assert selected_shared["is_default"] is True
    assert selected_shared["can_manage"] is False

    async with dbmod.async_session() as db:
        persisted = await db.get(Integration, connection_id)
        recipient = await db.get(User, other_id)
    assert persisted is not None
    assert not bool((persisted.config or {}).get("is_default"))
    assert recipient is not None
    assert recipient.preferences["integration_account_defaults"][
        owner["entity_id"]
    ]["linkedin"] == {
        "kind": "integration",
        "account_id": connection_id,
    }

    from packages.core.services.integration_account_service import (
        IntegrationAccountKind,
        normalize_runtime_integration_account_defaults,
    )

    async with dbmod.async_session() as db:
        db.add(Integration(
            id=generate_ulid(),
            entity_id=owner["entity_id"],
            owner_user_id=other_id,
            provider="linkedin",
            status="active",
            config={"name": "Recipient LinkedIn"},
            credentials={"access_token": "recipient-linkedin-entity-token"},
        ))
        await db.flush()
        await normalize_runtime_integration_account_defaults(
            db,
            kind=IntegrationAccountKind.INTEGRATION,
            user_id=other_id,
            entity_id=owner["entity_id"],
            provider="linkedin",
        )
        await db.commit()

    normalized_entity_catalog = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    normalized_entity_linkedin = next(
        row
        for row in normalized_entity_catalog.json()
        if row["server_key"] == "linkedin"
    )
    normalized_shared_entity = next(
        account
        for account in normalized_entity_linkedin["entity_accounts"]
        if account["id"] == connection_id
    )
    assert normalized_shared_entity["is_default"] is True

    selected_oauth_response = await client.post(
        "/api/v1/integrations/mcp-servers/linkedin/"
        f"connections/{oauth_id}/set-default",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert selected_oauth_response.status_code == 204, selected_oauth_response.text

    async with dbmod.async_session() as db:
        persisted_oauth = await db.get(OAuthAccount, oauth_id)
        recipient = await db.get(User, other_id)
    assert persisted_oauth is not None
    assert not bool((persisted_oauth.profile or {}).get("is_default"))
    assert recipient is not None
    assert recipient.preferences["integration_account_defaults"][
        owner["entity_id"]
    ]["linkedin"] == {
        "kind": "oauth_account",
        "account_id": oauth_id,
    }

    # Creating an unrelated owned account runs normalization. It must not
    # overwrite the actor's explicit preference for an authorized shared row.
    async with dbmod.async_session() as db:
        db.add(OAuthAccount(
            id=generate_ulid(),
            user_id=other_id,
            provider="linkedin",
            provider_user_id="recipient-linkedin-oauth",
            access_token="recipient-linkedin-token",
            profile={"name": "Recipient LinkedIn OAuth"},
        ))
        await db.flush()
        await normalize_runtime_integration_account_defaults(
            db,
            kind=IntegrationAccountKind.OAUTH_ACCOUNT,
            user_id=other_id,
            entity_id=owner["entity_id"],
            provider="linkedin",
        )
        await db.commit()

    normalized_catalog = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    normalized_linkedin = next(
        row
        for row in normalized_catalog.json()
        if row["server_key"] == "linkedin"
    )
    normalized_shared = next(
        account
        for account in normalized_linkedin["connections"]
        if account["id"] == oauth_id
    )
    assert normalized_shared["is_default"] is True

    async with dbmod.async_session() as db:
        recipient = await db.get(User, other_id)
    assert recipient is not None
    assert recipient.preferences["integration_account_defaults"][
        owner["entity_id"]
    ]["linkedin"] == {
        "kind": "oauth_account",
        "account_id": oauth_id,
    }


@pytest.mark.asyncio
async def test_mcp_catalog_nango_only_provider_supports_multiple_accounts(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router

    async def _nango_only_provider(*args, **kwargs):
        return {"linear": "linear"}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _nango_only_provider,
    )
    token = await _register(client, "catalog_nango_multi_account")
    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    linear = next(
        row for row in response.json() if row["server_key"] == "linear"
    )
    assert linear["server_kind"] == "managed"
    assert linear["nango_provider_config_key"] == "linear"
    assert linear["supports_multi_account"] is True


@pytest.mark.asyncio
async def test_mcp_catalog_lists_reconnect_account_without_routing_to_it(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_reconnect_account")
    oauth_id = generate_ulid()
    checked_at = datetime.now(timezone.utc).isoformat()
    async with dbmod.async_session() as db:
        db.add(
            OAuthAccount(
                id=oauth_id,
                user_id=owner["user_id"],
                provider="gmail",
                provider_user_id="gmail-reconnect-user",
                credential_ref="vault:v1:unreadable",
                credential_scheme="vault_transit",
                profile={
                    "email": "reconnect@example.test",
                    "oauth_refresh": {"reauth_required": True},
                    "last_health_check": {
                        "ok": False,
                        "detail": "Stored OAuth credentials could not be decrypted; reconnect.",
                        "latency_ms": 0.0,
                        "checked_at": checked_at,
                    },
                },
            )
        )
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    gmail = next(row for row in response.json() if row["server_key"] == "gmail")
    assert [connection["id"] for connection in gmail["connections"]] == [oauth_id]
    assert gmail["connections"][0]["health"]["ok"] is False
    assert gmail["connections"][0]["runtime_callable"] is False
    assert gmail["connections"][0]["availability"] == "reconnect_required"
    assert gmail["user_connected"] is False
    assert gmail["agent_can_use"] is False


@pytest.mark.asyncio
async def test_mcp_catalog_lists_incomplete_entity_account_for_repair(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_incomplete_entity_account")
    account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(Integration(
            id=account_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            created_by_user_id=owner["user_id"],
            provider="webhook",
            status="active",
            config={"name": "Webhook needing credentials"},
            credentials={},
        ))
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    assert [account["id"] for account in webhook["entity_accounts"]] == [
        account_id,
    ]
    account = webhook["entity_accounts"][0]
    assert account["display_name"] == "Webhook needing credentials"
    assert account["runtime_callable"] is False
    assert account["availability"] == "reconnect_required"
    assert webhook["entity_connected"] is False
    assert webhook["agent_can_use"] is False
    assert webhook["requires_explicit_account"] is False


@pytest.mark.asyncio
async def test_mcp_catalog_lists_failed_entity_projection_for_management(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationAccountFactory,
    )

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_failed_entity_projection")
    account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(Integration(
            id=account_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            created_by_user_id=owner["user_id"],
            provider="webhook",
            status="active",
            config={"name": "Malformed webhook"},
            credentials={"bearer_token": "secret"},
        ))
        await db.commit()

    def _fail_entity_projection(*_args, **_kwargs):
        raise ValueError("malformed entity account metadata")

    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_entity",
        _fail_entity_projection,
    )
    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    assert [account["id"] for account in webhook["entity_accounts"]] == [
        account_id,
    ]
    account = webhook["entity_accounts"][0]
    assert account["runtime_callable"] is False
    assert account["availability"] == "load_failed"
    assert webhook["requires_explicit_account"] is True
    assert webhook["agent_can_use"] is False


@pytest.mark.asyncio
async def test_mcp_catalog_lists_permission_blocked_entity_account(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.services import integration_account_service

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    async def _deny_required_permission(
        _db,
        _user_id,
        _entity_id,
        permission,
    ):
        return permission != "mcp.webhook.use"

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    monkeypatch.setattr(
        integration_account_service,
        "user_has_permission",
        _deny_required_permission,
    )
    owner = await _register_with_ids(client, "catalog_permission_blocked_entity")
    account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(Integration(
            id=account_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["user_id"],
            created_by_user_id=owner["user_id"],
            provider="webhook",
            status="active",
            config={"name": "Restricted webhook"},
            credentials={"bearer_token": "secret"},
            required_permission="mcp.webhook.use",
        ))
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    assert [account["id"] for account in webhook["entity_accounts"]] == [
        account_id,
    ]
    account = webhook["entity_accounts"][0]
    assert account["runtime_callable"] is False
    assert account["availability"] == "permission_denied"
    assert webhook["requires_explicit_account"] is False
    assert webhook["user_has_required_permission"] is False
    assert webhook["agent_can_use"] is False
    assert "lacks" in webhook["hint"].lower()

    set_default = await client.post(
        "/api/v1/integrations/mcp-servers/webhook/"
        f"entity-accounts/{account_id}/set-default",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert set_default.status_code == 404, set_default.text


@pytest.mark.asyncio
async def test_mcp_catalog_keeps_provider_callable_with_a_blocked_sibling(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.services import integration_account_service

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    async def _deny_required_permission(
        _db,
        _user_id,
        _entity_id,
        permission,
    ):
        return permission != "mcp.webhook.restricted"

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    monkeypatch.setattr(
        integration_account_service,
        "user_has_permission",
        _deny_required_permission,
    )
    owner = await _register_with_ids(client, "catalog_mixed_permission_entity")
    healthy_id = generate_ulid()
    blocked_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            Integration(
                id=healthy_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                created_by_user_id=owner["user_id"],
                provider="webhook",
                status="active",
                config={"name": "Healthy webhook"},
                credentials={"bearer_token": "healthy-secret"},
            ),
            Integration(
                id=blocked_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                created_by_user_id=owner["user_id"],
                provider="webhook",
                status="active",
                config={"name": "Restricted webhook"},
                credentials={"bearer_token": "restricted-secret"},
                required_permission="mcp.webhook.restricted",
            ),
        ])
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    by_id = {account["id"]: account for account in webhook["entity_accounts"]}
    assert by_id[healthy_id]["runtime_callable"] is True
    assert by_id[blocked_id]["availability"] == "permission_denied"
    assert webhook["entity_connected"] is True
    assert webhook["user_has_required_permission"] is True
    assert webhook["agent_can_use"] is True
    assert "lacks 'None'" not in webhook["hint"]


@pytest.mark.asyncio
async def test_mcp_catalog_uses_healthy_sibling_when_default_credentials_rejected(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_rejected_default_sibling")
    rejected_id = generate_ulid()
    healthy_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            Integration(
                id=rejected_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                created_by_user_id=owner["user_id"],
                provider="webhook",
                status="active",
                config={
                    "name": "Rejected default",
                    "is_default": True,
                    "last_health_check": {
                        "ok": False,
                        "detail": "Bearer token rejected by provider",
                    },
                },
                credentials={"bearer_token": "rejected-secret"},
            ),
            Integration(
                id=healthy_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                created_by_user_id=owner["user_id"],
                provider="webhook",
                status="active",
                config={"name": "Healthy sibling"},
                credentials={"bearer_token": "healthy-secret"},
            ),
        ])
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    webhook = next(row for row in response.json() if row["server_key"] == "webhook")
    by_id = {account["id"]: account for account in webhook["entity_accounts"]}
    assert by_id[rejected_id]["runtime_callable"] is False
    assert by_id[rejected_id]["availability"] == "reconnect_required"
    assert by_id[healthy_id]["runtime_callable"] is True
    assert by_id[healthy_id]["is_default"] is True
    assert webhook["agent_can_use"] is True


@pytest.mark.asyncio
async def test_mcp_catalog_tolerates_non_object_oauth_profile(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_non_object_oauth_profile")
    account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(OAuthAccount(
            id=account_id,
            user_id=owner["user_id"],
            provider="gmail",
            provider_user_id="legacy-profile@example.test",
            access_token="legacy-profile-token",
            profile=["legacy"],
        ))
        await db.commit()

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    gmail = next(row for row in response.json() if row["server_key"] == "gmail")
    account = next(row for row in gmail["connections"] if row["id"] == account_id)
    assert account["runtime_callable"] is True
    assert account["availability"] == "callable"


@pytest.mark.asyncio
async def test_unavailable_accounts_cannot_be_saved_as_defaults(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_unavailable_defaults")
    oauth_id = generate_ulid()
    entity_account_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            OAuthAccount(
                id=oauth_id,
                user_id=owner["user_id"],
                provider="gmail",
                provider_user_id="reconnect-default@example.test",
                credential_ref="vault:v1:unreadable",
                credential_scheme="vault_transit",
                profile={"oauth_refresh": {"reauth_required": True}},
            ),
            Integration(
                id=entity_account_id,
                entity_id=owner["entity_id"],
                owner_user_id=owner["user_id"],
                created_by_user_id=owner["user_id"],
                provider="webhook",
                status="active",
                config={"name": "Incomplete webhook"},
                credentials={},
            ),
        ])
        await db.commit()

    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    oauth_response = await client.post(
        "/api/v1/integrations/mcp-servers/gmail/"
        f"connections/{oauth_id}/set-default",
        headers=headers,
    )
    entity_response = await client.post(
        "/api/v1/integrations/mcp-servers/webhook/"
        f"entity-accounts/{entity_account_id}/set-default",
        headers=headers,
    )

    assert oauth_response.status_code == 404, oauth_response.text
    assert entity_response.status_code == 404, entity_response.text


@pytest.mark.asyncio
async def test_mcp_catalog_preserves_partial_registry_fail_closed(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationAccountFactory,
    )

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_partial_registry")
    malformed_id = generate_ulid()
    healthy_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add_all([
            OAuthAccount(
                id=malformed_id,
                user_id=owner["user_id"],
                provider="gmail",
                provider_user_id="malformed@example.test",
                access_token="malformed-token",
                profile={"email": "malformed@example.test", "is_default": True},
            ),
            OAuthAccount(
                id=healthy_id,
                user_id=owner["user_id"],
                provider="gmail",
                provider_user_id="healthy@example.test",
                access_token="healthy-token",
                profile={"email": "healthy@example.test"},
            ),
        ])
        await db.commit()

    original_from_oauth = RuntimeIntegrationAccountFactory.from_oauth

    def _partially_load_oauth(row, *, actor_user_id=None, provider=None):
        if row.id == malformed_id:
            raise ValueError("malformed OAuth account metadata")
        return original_from_oauth(
            row,
            actor_user_id=actor_user_id,
            provider=provider,
        )

    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_oauth",
        _partially_load_oauth,
    )

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    gmail = next(row for row in response.json() if row["server_key"] == "gmail")
    assert {connection["id"] for connection in gmail["connections"]} == {
        healthy_id,
        malformed_id,
    }
    by_id = {connection["id"]: connection for connection in gmail["connections"]}
    assert by_id[healthy_id]["runtime_callable"] is True
    assert by_id[healthy_id]["availability"] == "callable"
    assert by_id[healthy_id]["is_default"] is False
    assert by_id[malformed_id]["runtime_callable"] is False
    assert by_id[malformed_id]["availability"] == "load_failed"
    assert gmail["requires_explicit_account"] is True
    assert gmail["agent_can_use"] is True
    assert "incomplete" in gmail["hint"].lower()


@pytest.mark.asyncio
async def test_mcp_catalog_preserves_partial_status_when_every_account_fails(
    client: AsyncClient,
    monkeypatch,
):
    from apps.api.routers import integrations as integration_router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import OAuthAccount
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationAccountFactory,
    )

    async def _no_nango_provider_configs(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        integration_router,
        "_collect_nango_provider_keys",
        _no_nango_provider_configs,
    )
    owner = await _register_with_ids(client, "catalog_partial_all_failed")
    malformed_id = generate_ulid()
    async with dbmod.async_session() as db:
        db.add(OAuthAccount(
            id=malformed_id,
            user_id=owner["user_id"],
            provider="gmail",
            provider_user_id="malformed-only@example.test",
            access_token="malformed-token",
            profile={"email": "malformed-only@example.test", "is_default": True},
        ))
        await db.commit()

    def _fail_oauth_projection(*_args, **_kwargs):
        raise ValueError("malformed OAuth account metadata")

    monkeypatch.setattr(
        RuntimeIntegrationAccountFactory,
        "from_oauth",
        _fail_oauth_projection,
    )

    response = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )

    assert response.status_code == 200, response.text
    gmail = next(row for row in response.json() if row["server_key"] == "gmail")
    assert [connection["id"] for connection in gmail["connections"]] == [
        malformed_id,
    ]
    assert gmail["connections"][0]["runtime_callable"] is False
    assert gmail["connections"][0]["availability"] == "load_failed"
    assert gmail["requires_explicit_account"] is True
    assert gmail["agent_can_use"] is False
    assert "repair" in gmail["hint"].lower()


@pytest.mark.asyncio
async def test_mcp_catalog_cli_worker_state_is_scoped_to_current_user(client: AsyncClient):
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User, UserMembership
    from packages.core.models.worker import Worker
    from packages.core.services.auth_service import create_access_token, hash_password

    owner = await _register_with_ids(client, "catalog_cli_owner")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        other_user = User(
            id=generate_ulid(),
            entity_id=owner["entity_id"],
            email="catalog_cli_other@example.com",
            display_name="Catalog Other",
            password_hash=hash_password("securepass123"),
            role="member",
            status="active",
        )
        db.add(other_user)
        db.add(
            UserMembership(
                id=generate_ulid(),
                user_id=other_user.id,
                entity_id=owner["entity_id"],
                role="member",
                status="active",
                is_primary=True,
            )
        )
        owner_worker_display_name = "Owner Local Worker"
        db.add(
            Worker(
                id="worker_catalog_owner_cli",
                entity_id=owner["entity_id"],
                kind="custom_http",
                display_name=owner_worker_display_name,
                version="test",
                status="active",
                created_by_user_id=owner["user_id"],
                last_heartbeat_at=datetime.now(timezone.utc),
                capabilities={
                    "supported_kinds": ["code", "action"],
                    "supported_providers": ["chrome"],
                    "code": {"tools": {"codex_cli": {"status": "ready"}}},
                    "browser": {
                        "native_host_connected": True,
                        "extension_connected": True,
                    },
                },
            )
        )
        await db.commit()
        other_token = create_access_token(other_user.id, owner["entity_id"], "member")

    owner_resp = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert owner_resp.status_code == 200, owner_resp.text
    owner_catalog = {row["server_key"]: row for row in owner_resp.json()}
    assert owner_catalog["chrome"]["agent_can_use"] is True

    other_resp = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_resp.status_code == 200, other_resp.text
    other_catalog = {row["server_key"]: row for row in other_resp.json()}
    assert other_catalog["chrome"]["agent_can_use"] is False


@pytest.mark.asyncio
async def test_workers_list_only_returns_current_users_cli_workers(client: AsyncClient):
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User, UserMembership
    from packages.core.models.worker import Worker
    from packages.core.services.auth_service import create_access_token, hash_password

    owner = await _register_with_ids(client, "workers_cli_owner")

    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        other_user = User(
            id=generate_ulid(),
            entity_id=owner["entity_id"],
            email="workers_cli_other@example.com",
            display_name="Workers Other",
            password_hash=hash_password("securepass123"),
            role="member",
            status="active",
        )
        db.add(other_user)
        db.add(
            UserMembership(
                id=generate_ulid(),
                user_id=other_user.id,
                entity_id=owner["entity_id"],
                role="member",
                status="active",
                is_primary=True,
            )
        )
        owner_worker_display_name = "Owner Local Worker"
        db.add(
            Worker(
                id="worker_list_owner_cli",
                entity_id=owner["entity_id"],
                kind="custom_http",
                display_name=owner_worker_display_name,
                version="test",
                status="active",
                created_by_user_id=owner["user_id"],
                last_heartbeat_at=datetime.now(timezone.utc),
                capabilities={"supported_kinds": ["action"], "supported_providers": ["chrome"]},
            )
        )
        await db.commit()
        other_token = create_access_token(other_user.id, owner["entity_id"], "member")

    owner_resp = await client.get(
        "/api/v1/workers?kind=custom_http",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    assert owner_resp.status_code == 200, owner_resp.text
    assert [row["id"] for row in owner_resp.json()] == ["worker_list_owner_cli"]

    other_resp = await client.get(
        "/api/v1/workers?kind=custom_http",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_resp.status_code == 200, other_resp.text
    assert other_resp.json() == []
