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

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


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
    # response_model contract the web client relies on
    for field in ("server_key", "name", "auth_type", "agent_can_use", "hint"):
        assert field in yt

    tk = catalog["tiktok"]
    assert tk["auth_type"] == "oauth2"
    assert tk["name"] == "TikTok"
    assert "video.publish" in (tk.get("scopes") or "")

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
async def test_mcp_catalog_requires_auth(client: AsyncClient):
    # The endpoint the frontend calls is user-scoped; no token → 401.
    resp = await client.get("/api/v1/integrations/mcp-servers")
    assert resp.status_code == 401


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
