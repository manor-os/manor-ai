"""Progressive catalog reads keep the existing actor/account projection."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from tests.test_integrations_mcp_catalog import _register, _register_with_ids


async def test_snapshot_does_not_scan_skills_or_wait_for_external_config(client, monkeypatch):
    from apps.api.routers import integrations as router
    from packages.core.services import builtin_skill_loader

    token = await _register(client, "snapshot_first_paint")
    seed = AsyncMock(side_effect=AssertionError("snapshot must not scan skill bundles"))
    remote = AsyncMock(side_effect=AssertionError("snapshot must not wait for Nango"))
    monkeypatch.setattr(builtin_skill_loader, "seed_builtin_skills", seed)
    monkeypatch.setattr(router, "_collect_nango_provider_keys", remote)
    monkeypatch.setattr(router, "_read_nango_provider_snapshot", AsyncMock(return_value=({}, True)))
    response = await client.get(
        "/api/v1/integrations/mcp-servers?snapshot=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    rows = {row["server_key"]: row for row in response.json()}
    assert "gmail" in rows
    assert "nango" not in rows
    assert rows["remotion"]["setup_pending"] is True
    assert rows["remotion"]["agent_can_use"] is False
    # Nango can also override credential-based cards such as Shopify.
    assert rows["shopify"]["setup_pending"] is True
    seed.assert_not_awaited()
    remote.assert_not_awaited()


async def test_full_catalog_repairs_existing_skills_and_snapshot_preserves_accounts(client):
    from sqlalchemy import select
    import packages.core.database as dbmod
    from packages.core.models.skill import Skill
    from packages.core.services.builtin_plugin_service import BUILTIN_PLUGINS
    from packages.core.services.skill_service import reset_builtin_skill_seed_cache

    token = await _register(client, "snapshot_known_accounts")
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/v1/integrations", headers=headers,
        json={"provider": "webhook", "config": {"name": "Example", "url": "https://example.test"},
              "credentials": {"bearer_token": "test-only"}},
    )
    assert created.status_code in (200, 201), created.text
    initial = await client.get("/api/v1/integrations/mcp-servers", headers=headers)
    assert initial.status_code == 200, initial.text
    slug = BUILTIN_PLUGINS["remotion"].skills[0].slug
    async with dbmod.async_session() as db:
        skill = (await db.execute(select(Skill).where(Skill.entity_id.is_(None), Skill.slug == slug))).scalar_one()
        original_prompt = skill.system_prompt
        skill.system_prompt = "stale packaged instructions"
        await db.commit()
    # The repair runs once per process until a pass finds nothing to write.
    reset_builtin_skill_seed_cache()
    for suffix in ("?snapshot=true", ""):
        response = await client.get(f"/api/v1/integrations/mcp-servers{suffix}", headers=headers)
        assert response.status_code == 200, response.text
        before = {row["server_key"]: row for row in initial.json()}
        after = {row["server_key"]: row for row in response.json()}
        assert after.keys() == before.keys()
        for key in before:
            for field in ("connections", "entity_accounts", "agent_can_use", "oauth_configured",
                          "user_has_required_permission", "nango_provider_config_key", "plugin_enabled"):
                assert after[key][field] == before[key][field], (key, field)
        async with dbmod.async_session() as db:
            prompt = (await db.execute(select(Skill.system_prompt).where(Skill.entity_id.is_(None), Skill.slug == slug))).scalar_one()
        assert prompt == ("stale packaged instructions" if suffix else original_prompt)


async def test_snapshot_still_requires_auth(client):
    response = await client.get("/api/v1/integrations/mcp-servers?snapshot=true")
    assert response.status_code == 401


async def test_nango_snapshot_is_scoped_and_failed_refresh_keeps_last_success(monkeypatch):
    from apps.api.routers import integrations as router
    from packages.core.ai.mcp import nango
    from packages.core.cache import cache

    stored = {}
    async def read(key):
        return stored.get(key)
    async def write(key, value, ttl):
        assert ttl == 300
        stored[key] = value
        return True

    monkeypatch.setattr(cache, "get", read)
    monkeypatch.setattr(cache, "set", write)
    monkeypatch.setattr(nango, "get_nango_secret", AsyncMock(return_value="first-secret"))
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"configs": [{"unique_key": "linear", "provider": "linear"}]},
    ))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))

    assert await router._read_nango_provider_snapshot(None, "entity-one") == ({}, True)
    assert await router._collect_nango_provider_keys(None, "entity-one") == {"linear": "linear"}
    assert await router._read_nango_provider_snapshot(None, "entity-one") == ({"linear": "linear"}, True)
    assert await router._read_nango_provider_snapshot(None, "entity-two") == ({}, True)
    assert all("first-secret" not in key for key in stored)

    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(HTTPException) as failed:
        await router._collect_nango_provider_keys(None, "entity-one")
    assert failed.value.status_code == 503
    assert await router._read_nango_provider_snapshot(None, "entity-one") == ({"linear": "linear"}, True)

    monkeypatch.setattr(nango, "get_nango_secret", AsyncMock(return_value="rotated-secret"))
    assert await router._read_nango_provider_snapshot(None, "entity-one") == ({}, True)
    monkeypatch.setattr(nango, "get_nango_secret", AsyncMock(return_value=None))
    assert await router._read_nango_provider_snapshot(None, "entity-one") == ({}, False)


async def test_snapshot_cache_failure_is_unknown_not_unconfigured(monkeypatch):
    from apps.api.routers import integrations as router
    from packages.core.ai.mcp import nango
    from packages.core.cache import cache

    monkeypatch.setattr(nango, "get_nango_secret", AsyncMock(return_value="configured"))
    monkeypatch.setattr(cache, "get", AsyncMock(side_effect=ConnectionError("cache unavailable")))
    assert await router._read_nango_provider_snapshot(None, "entity") == ({}, True)


async def test_external_outage_keeps_cards_and_locally_configured_oauth_usable(client, monkeypatch):
    from apps.api.routers import integrations as router
    from packages.core.services import oauth_provider_config

    token = await _register(client, "snapshot_external_outage")
    monkeypatch.setattr(router, "_collect_nango_provider_keys", AsyncMock(side_effect=HTTPException(503)))
    monkeypatch.setattr(router, "_read_nango_provider_snapshot", AsyncMock(return_value=({"linear": "linear", "shopify": "shopify"}, True)))
    monkeypatch.setattr(oauth_provider_config, "oauth_client_configured", lambda key, server: key == "gmail")
    response = await client.get("/api/v1/integrations/mcp-servers", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    rows = {row["server_key"]: row for row in response.json()}
    assert rows["gmail"]["oauth_configured"] is True
    assert rows["gmail"]["setup_pending"] is False
    assert rows["webhook"]["setup_pending"] is False
    for key in ("manor_pms", "manor_mcp_minutes"):
        assert rows[key]["setup_pending"] is False
    assert rows["linear"]["nango_provider_config_key"] == "linear"
    assert rows["linear"]["setup_unavailable"] is True
    assert rows["linear"]["setup_pending"] is True
    assert rows["shopify"]["nango_provider_config_key"] == "shopify"
    assert rows["shopify"]["setup_unavailable"] is True
    assert rows["whatsapp"]["setup_unavailable"] is True


async def test_external_outage_without_cached_config_cannot_replace_full_catalog(client, monkeypatch):
    from apps.api.routers import integrations as router

    token = await _register(client, "snapshot_unknown_routing")
    monkeypatch.setattr(router, "_collect_nango_provider_keys", AsyncMock(side_effect=HTTPException(503)))
    monkeypatch.setattr(router, "_read_nango_provider_snapshot", AsyncMock(return_value=({}, True)))
    headers = {"Authorization": f"Bearer {token}"}
    full = await client.get("/api/v1/integrations/mcp-servers", headers=headers)
    assert full.status_code == 503
    snapshot = await client.get("/api/v1/integrations/mcp-servers?snapshot=true", headers=headers)
    assert snapshot.status_code == 200
    rows = {row["server_key"]: row for row in snapshot.json()}
    for key in ("shopify", "gmail", "whatsapp"):
        assert rows[key]["setup_pending"] is True
    for key in ("manor_pms", "manor_mcp_minutes"):
        assert rows[key]["setup_pending"] is False


@pytest.mark.parametrize("full_outage", [False, True])
async def test_catalog_preserves_nango_routing_only_for_authorized_accounts(client, monkeypatch, full_outage):
    from apps.api.routers import integrations as router
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Integration
    from packages.core.models.user import User, UserMembership
    from packages.core.services.auth_service import create_access_token, hash_password
    from packages.core.services.integration_access import grant_connection_use

    owner = await _register_with_ids(client, "snapshot_private_nango")
    other_id = generate_ulid()
    account_ids = {provider: generate_ulid() for provider in ("shopify", "linear_cloud")}
    monkeypatch.setattr(router, "_collect_nango_provider_keys", AsyncMock(side_effect=HTTPException(503)))
    monkeypatch.setattr(router, "_read_nango_provider_snapshot", AsyncMock(return_value=({"unrelated": "unrelated"} if full_outage else {}, True)))
    async with dbmod.async_session() as db:
        db.add_all([
            User(id=other_id, entity_id=owner["entity_id"], email="snapshot_other@example.test",
                 password_hash=hash_password("securepass123"), role="owner", status="active"),
            UserMembership(user_id=other_id, entity_id=owner["entity_id"], role="owner", status="active", is_primary=True),
            *[Integration(
                id=account_id, entity_id=owner["entity_id"], owner_user_id=owner["user_id"],
                provider=provider, status="active", credentials={},
                config={"name": f"Private {provider}", "nango": {
                    "connection_id": f"private-{provider}", "provider_config_key": provider.replace("_", "-"),
                }},
            ) for provider, account_id in account_ids.items()],
        ])
        await db.commit()

    async def snapshot(token):
        suffix = "" if full_outage else "?snapshot=true"
        response = await client.get("/api/v1/integrations/mcp-servers" + suffix, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        return {row["server_key"]: row for row in response.json()}

    owner_rows = await snapshot(owner["access_token"])
    for provider, account_id in account_ids.items():
        key = provider.replace("_", "-")
        assert owner_rows[key]["nango_provider_config_key"] == key
        assert owner_rows[key]["setup_pending"] is True
        assert [row["id"] for row in owner_rows[key]["entity_accounts"]] == [account_id]
    other_token = create_access_token(other_id, owner["entity_id"], "owner")
    private_rows = await snapshot(other_token)
    assert "linear-cloud" not in private_rows
    assert private_rows["shopify"]["nango_provider_config_key"] is None
    async with dbmod.async_session() as db:
        await grant_connection_use(db, kind="integration", connection_id=account_ids["linear_cloud"],
                                   entity_id=owner["entity_id"], owner_user_id=owner["user_id"], grantee_user_id=other_id)
        await db.commit()
    shared_rows = await snapshot(other_token)
    assert shared_rows["linear-cloud"]["nango_provider_config_key"] == "linear-cloud"
    assert [row["id"] for row in shared_rows["linear-cloud"]["entity_accounts"]] == [account_ids["linear_cloud"]]
