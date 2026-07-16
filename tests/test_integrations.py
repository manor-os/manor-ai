"""E2E tests: integrations and channels CRUD, entity isolation, credential safety."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Integration

pytestmark = pytest.mark.oss_regression


async def _auth(client: AsyncClient, username: str = "intuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_create_integration(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
            "config": {"team_id": "T12345"},
            "credentials": {"bot_token": "xoxb-secret"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "slack"
    assert data["status"] == "active"
    assert data["config"] == {"team_id": "T12345"}
    assert len(data["id"]) == 26


@pytest.mark.asyncio
async def test_list_integrations(client: AsyncClient):
    headers = await _auth(client)
    await client.post("/api/v1/integrations", headers=headers, json={"provider": "slack"})
    await client.post("/api/v1/integrations", headers=headers, json={"provider": "teams"})

    resp = await client.get("/api/v1/integrations", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_update_integration(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    resp = await client.put(
        f"/api/v1/integrations/{iid}",
        headers=headers,
        json={
            "status": "disabled",
            "config": {"updated": True},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert resp.json()["config"] == {"updated": True}


@pytest.mark.asyncio
async def test_delete_integration(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    resp = await client.delete(f"/api/v1/integrations/{iid}", headers=headers)
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/integrations/{iid}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_delete_channel_integration_cleans_channel_bridge(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "int_delete_bridge")
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "email",
            "credentials": {
                "host": "smtp.example.test",
                "port": "587",
                "username": "bridge@example.test",
                "password": "secret",
            },
        },
    )
    assert create.status_code == 201
    integration = create.json()

    channel_config = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.entity_id == integration["entity_id"],
                ChannelConfig.channel_type == "email",
                ChannelConfig.config["integration_id"].astext == integration["id"],
            )
        )
    ).scalar_one()
    channel = Channel(
        id=generate_ulid(),
        entity_id=integration["entity_id"],
        workspace_id=None,
        type="email",
        name="Bridge binding",
        config={"channel_config_id": channel_config.id},
        status="active",
    )
    db_session.add(channel)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/integrations/{integration['id']}", headers=headers)
    assert resp.status_code == 204

    assert (
        await db_session.execute(select(ChannelConfig).where(ChannelConfig.id == channel_config.id))
    ).scalar_one_or_none() is None
    assert (await db_session.execute(select(Channel).where(Channel.id == channel.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_integration_isolation(client: AsyncClient):
    headers_a = await _auth(client, "int_a")
    headers_b = await _auth(client, "int_b")

    create = await client.post(
        "/api/v1/integrations",
        headers=headers_a,
        json={
            "provider": "slack",
        },
    )
    iid = create.json()["id"]

    # User B cannot see user A's integration
    resp = await client.get(f"/api/v1/integrations/{iid}", headers=headers_b)
    assert resp.status_code == 404

    # User B's list is empty
    resp2 = await client.get("/api/v1/integrations", headers=headers_b)
    assert len(resp2.json()) == 0


@pytest.mark.asyncio
async def test_create_channel(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/integrations/channels",
        headers=headers,
        json={
            "type": "slack_channel",
            "name": "#general",
            "config": {"channel_id": "C12345"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "slack_channel"
    assert data["name"] == "#general"
    assert data["status"] == "active"
    assert len(data["id"]) == 26


@pytest.mark.asyncio
async def test_channel_crud(client: AsyncClient):
    headers = await _auth(client)

    # Create
    create = await client.post(
        "/api/v1/integrations/channels",
        headers=headers,
        json={
            "type": "email",
            "name": "support@co.com",
        },
    )
    cid = create.json()["id"]

    # Read
    resp = await client.get(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "support@co.com"

    # Update
    resp = await client.put(
        f"/api/v1/integrations/channels/{cid}",
        headers=headers,
        json={
            "name": "help@co.com",
            "status": "disabled",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "help@co.com"
    assert resp.json()["status"] == "disabled"

    # Delete
    resp = await client.delete(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/integrations/channels/{cid}", headers=headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_credentials_not_exposed(client: AsyncClient, db_session):
    headers = await _auth(client)

    # Create integration with credentials
    create = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={
            "provider": "slack",
            "credentials": {"bot_token": "xoxb-super-secret"},
        },
    )
    assert create.status_code == 201
    data = create.json()
    assert "credentials" not in data
    assert data["credential_preview"].get("bot_token") in {None, "__unchanged__"}
    assert "xoxb-super-secret" not in str(data)

    # Get single integration — no credentials
    iid = data["id"]
    resp = await client.get(f"/api/v1/integrations/{iid}", headers=headers)
    data = resp.json()
    assert "credentials" not in data
    assert data["credential_preview"].get("bot_token") in {None, "__unchanged__"}
    assert "xoxb-super-secret" not in str(data)

    # List integrations — no credentials
    resp = await client.get("/api/v1/integrations", headers=headers)
    for item in resp.json():
        assert "credentials" not in item
        assert "xoxb-super-secret" not in str(item)

    # Updating with the sentinel must preserve existing encrypted creds.
    update = await client.put(
        f"/api/v1/integrations/{iid}",
        headers=headers,
        json={
            "credentials": {"bot_token": "__unchanged__", "default_channel": "C123"},
        },
    )
    assert update.status_code == 200
    integration = (await db_session.execute(select(Integration).where(Integration.id == iid))).scalar_one()
    plaintext = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="test", id="credentials_not_exposed"),
        reason="assert_update_preserved_secret",
    )
    assert plaintext["bot_token"] == "xoxb-super-secret"
    assert plaintext["default_channel"] == "C123"
