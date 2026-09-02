"""Integrations record provenance and remain private to their connector.

``created_by_user_id`` is an audit field. Access is controlled separately by
``owner_user_id`` and explicit use grants.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.document import Integration
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_records_the_connecting_user(client: AsyncClient):
    headers = await _auth(client, "intprov_creator")
    me = await _me(client, headers)

    r = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "slack", "config": {"name": "Team Slack"}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created_by_user_id"] == me["id"]

    async with db_module.async_session() as db:
        row = await db.get(Integration, body["id"])
        assert row.created_by_user_id == me["id"]


@pytest.mark.asyncio
async def test_creator_name_is_resolved_for_list_and_detail(client: AsyncClient):
    headers = await _auth(client, "intprov_named")
    me = await _me(client, headers)

    created = await client.post(
        "/api/v1/integrations",
        headers=headers,
        json={"provider": "github", "config": {}},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]

    listed = await client.get("/api/v1/integrations", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(i for i in listed.json() if i["id"] == integration_id)
    assert row["created_by_user_id"] == me["id"]
    assert row["created_by_name"]

    detail = await client.get(f"/api/v1/integrations/{integration_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["created_by_name"] == row["created_by_name"]


@pytest.mark.asyncio
async def test_integration_is_private_to_owner_by_default(client: AsyncClient):
    """A same-Entity member cannot see an owner's connection without a grant."""
    owner_headers = await _auth(client, "intprov_owner")
    owner = await _me(client, owner_headers)

    created = await client.post(
        "/api/v1/integrations",
        headers=owner_headers,
        json={"provider": "notion", "config": {"name": "Shared Notion"}},
    )
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]

    # A different member of the same entity cannot see or manage it.
    colleague = await _create_entity_user(owner["entity_id"], "intprov_colleague", "member")

    listed = await client.get("/api/v1/integrations", headers=colleague["headers"])
    assert listed.status_code == 200, listed.text
    assert integration_id not in [i["id"] for i in listed.json()]

    detail = await client.get(
        f"/api/v1/integrations/{integration_id}", headers=colleague["headers"]
    )
    assert detail.status_code == 404, detail.text


@pytest.mark.asyncio
async def test_row_without_a_recorded_creator_is_hidden_until_repaired(client: AsyncClient):
    """Unknown historical ownership must not become an Entity-wide fallback."""
    headers = await _auth(client, "intprov_legacy")
    me = await _me(client, headers)

    async with db_module.async_session() as db:
        row = Integration(
            entity_id=me["entity_id"],
            provider="stripe",
            config={},
            credentials={},
        )
        db.add(row)
        await db.flush()
        integration_id = row.id
        await db.commit()

    r = await client.get(f"/api/v1/integrations/{integration_id}", headers=headers)
    assert r.status_code == 404, r.text
