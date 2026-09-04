"""Authorization contract for user-owned Integration credential sources."""

from __future__ import annotations

import importlib

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.document import Integration
from packages.core.models.channel import ChannelConfig
from packages.core.models.permission import Capability, ResourceType
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.services.integration_access import (
    grant_connection_use,
    resolve_integration_access,
)
from packages.core.services.integration_account_service import (
    list_runtime_integration_accounts,
)
from packages.core.services.agent_permission_service import can_use_integration
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_entity_owner_cannot_use_another_members_private_integration(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "integration_private_owner")
    owner = await _me(client, owner_headers)
    entity_admin = await _create_entity_user(
        owner["entity_id"], "integration_private_entity_owner", role="owner",
    )

    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="slack",
            credentials={"bot_token": "test-token"},
        )
        db.add(connection)
        await db.commit()

        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=connection.id,
            entity_id=owner["entity_id"],
            user_id=entity_admin["id"],
            action="use",
        )

    assert decision.allowed is False
    assert decision.reason == "connection_not_shared"


@pytest.mark.asyncio
async def test_named_member_can_use_but_not_manage_shared_oauth_account(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "oauth_share_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "oauth_share_member", role="member",
    )

    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=member["id"],
            entity_id=owner["entity_id"],
            role="member",
            status="active",
        ))
        account = OAuthAccount(
            user_id=owner["id"],
            provider="slack",
            provider_user_id="team-1",
            access_token="test-token",
        )
        db.add(account)
        await db.flush()
        await grant_connection_use(
            db,
            kind="oauth_account",
            connection_id=account.id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            grantee_user_id=member["id"],
        )
        await db.commit()

        use_decision = await resolve_integration_access(
            db,
            kind="oauth_account",
            connection_id=account.id,
            entity_id=owner["entity_id"],
            user_id=member["id"],
            action="use",
        )
        manage_decision = await resolve_integration_access(
            db,
            kind="oauth_account",
            connection_id=account.id,
            entity_id=owner["entity_id"],
            user_id=member["id"],
            action="manage",
        )

    assert use_decision.allowed is True
    assert use_decision.reason == "shared_use"
    assert manage_decision.allowed is False
    assert manage_decision.reason == "connection_not_owned"


@pytest.mark.asyncio
async def test_inactive_membership_does_not_fall_back_to_primary_user_row(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "inactive_connection_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        membership = (await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == owner["id"],
                UserMembership.entity_id == owner["entity_id"],
            )
        )).scalar_one()
        membership.status = "inactive"
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="gmail",
            credentials={"access_token": "test-token"},
        )
        db.add(connection)
        await db.commit()

        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=connection.id,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            action="bind_channel",
        )

    assert decision.allowed is False
    assert decision.reason == "connection_not_found"


@pytest.mark.asyncio
async def test_inactive_user_cannot_own_connection_through_active_membership(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "inactive_user_connection_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        user = await db.get(User, owner["id"])
        assert user is not None
        user.status = "inactive"
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="gmail",
            credentials={"access_token": "test-token"},
        )
        db.add(connection)
        await db.commit()

        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=connection.id,
            entity_id=owner["entity_id"],
            user_id=owner["id"],
            action="bind_channel",
        )

    assert decision.allowed is False
    assert decision.reason == "connection_not_found"




def test_user_scoped_connection_models_expose_owner_and_source_columns():
    assert "owner_user_id" in Integration.__table__.c
    assert "owner_user_id" in ChannelConfig.__table__.c
    assert "credential_source_kind" in ChannelConfig.__table__.c
    assert "credential_source_id" in ChannelConfig.__table__.c
    assert ResourceType.OAUTH_ACCOUNT == "oauth_account"
    assert Capability.USE == "use"


@pytest.mark.asyncio
async def test_runtime_resolution_requires_an_owner_or_explicit_use_grant(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "runtime_connection_owner")
    owner = await _me(client, owner_headers)
    entity_owner = await _create_entity_user(
        owner["entity_id"], "runtime_connection_entity_owner", role="owner",
    )

    async with db_module.async_session() as db:
        owner_membership = (await db.execute(
            select(UserMembership.id).where(
                UserMembership.user_id == owner["id"],
                UserMembership.entity_id == owner["entity_id"],
            )
        )).scalar_one_or_none()
        if owner_membership is None:
            db.add(UserMembership(
                user_id=owner["id"],
                entity_id=owner["entity_id"],
                role="owner",
                status="active",
                is_primary=True,
            ))
        db.add(UserMembership(
            user_id=entity_owner["id"],
            entity_id=owner["entity_id"],
            role="owner",
            status="active",
        ))
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="gmail",
            credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()

        inaccessible = await list_runtime_integration_accounts(
            db,
            user_id=entity_owner["id"],
            entity_id=owner["entity_id"],
            provider="gmail",
        )
        denied = await can_use_integration(
            db,
            user_id=entity_owner["id"],
            entity_id=owner["entity_id"],
            provider="gmail",
        )

        await grant_connection_use(
            db,
            kind="integration",
            connection_id=connection.id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            grantee_user_id=entity_owner["id"],
        )
        shared = await list_runtime_integration_accounts(
            db,
            user_id=entity_owner["id"],
            entity_id=owner["entity_id"],
            provider="gmail",
        )
        allowed = await can_use_integration(
            db,
            user_id=entity_owner["id"],
            entity_id=owner["entity_id"],
            provider="gmail",
        )

    assert inaccessible == []
    assert denied.allowed is False
    assert len(shared) == 1
    assert shared[0].id == connection.id
    assert shared[0].ownership == "shared"
    assert allowed.allowed is True
    assert allowed.account_id == connection.id


@pytest.mark.asyncio
async def test_connection_grant_api_only_allows_the_owner_to_share_and_revoke(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "connection_grant_api_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "connection_grant_api_member", role="member",
    )
    other_owner = await _create_entity_user(
        owner["entity_id"], "connection_grant_api_other_owner", role="owner",
    )

    async with db_module.async_session() as db:
        db.add_all([
            UserMembership(
                user_id=member["id"],
                entity_id=owner["entity_id"],
                role="member",
                status="active",
            ),
            UserMembership(
                user_id=other_owner["id"],
                entity_id=owner["entity_id"],
                role="owner",
                status="active",
            ),
        ])
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="telegram",
            credentials={"bot_token": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    url = f"/api/v1/integrations/connections/integration/{connection_id}/grants"
    denied = await client.post(
        url,
        headers=other_owner["headers"],
        json={"user_id": member["id"]},
    )
    assert denied.status_code == 404

    created = await client.post(
        url,
        headers=owner_headers,
        json={"user_id": member["id"]},
    )
    assert created.status_code == 201, created.text
    grant = created.json()
    assert grant["user_id"] == member["id"]
    assert grant["capabilities"] == [Capability.USE]

    listed = await client.get(url, headers=owner_headers)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [grant["id"]]

    recipient_listing = await client.get(url, headers=member["headers"])
    assert recipient_listing.status_code == 404

    revoked = await client.delete(
        f"{url}/{grant['id']}",
        headers=owner_headers,
    )
    assert revoked.status_code == 204, revoked.text

    async with db_module.async_session() as db:
        decision = await resolve_integration_access(
            db,
            kind="integration",
            connection_id=connection_id,
            entity_id=owner["entity_id"],
            user_id=member["id"],
            action="use",
        )
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_integration_crud_hides_private_connections_and_makes_shared_rows_read_only(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "integration_crud_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "integration_crud_member", role="member",
    )

    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=member["id"],
            entity_id=owner["entity_id"],
            role="member",
            status="active",
        ))
        connection = Integration(
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            provider="email",
            config={"name": "Owner inbox"},
            credentials={"username": "owner@example.test", "password": "secret"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    before_share = await client.get("/api/v1/integrations", headers=member["headers"])
    assert before_share.status_code == 200, before_share.text
    assert connection_id not in {row["id"] for row in before_share.json()}
    assert (await client.get(
        f"/api/v1/integrations/{connection_id}", headers=member["headers"],
    )).status_code == 404

    async with db_module.async_session() as db:
        await grant_connection_use(
            db,
            kind="integration",
            connection_id=connection_id,
            entity_id=owner["entity_id"],
            owner_user_id=owner["id"],
            grantee_user_id=member["id"],
        )
        await db.commit()

    after_share = await client.get("/api/v1/integrations", headers=member["headers"])
    assert after_share.status_code == 200, after_share.text
    shared = next(row for row in after_share.json() if row["id"] == connection_id)
    assert shared["ownership"] == "shared"
    assert shared["can_manage"] is False

    update = await client.put(
        f"/api/v1/integrations/{connection_id}",
        headers=member["headers"],
        json={"config": {"name": "Not allowed"}},
    )
    assert update.status_code == 404, update.text


@pytest.mark.asyncio
async def test_workspace_setup_context_hides_other_members_private_connections(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "setup_connection_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "setup_connection_member", role="member",
    )

    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=member["id"],
            entity_id=owner["entity_id"],
            role="member",
            status="active",
        ))
        db.add_all([
            Integration(
                entity_id=owner["entity_id"],
                owner_user_id=owner["id"],
                provider="telegram",
                credentials={"bot_token": "owner-token"},
            ),
            Integration(
                entity_id=owner["entity_id"],
                owner_user_id=member["id"],
                provider="discord",
                credentials={"bot_token": "member-token"},
            ),
        ])
        await db.commit()

        from packages.core.services.workspace_setup_service import _build_setup_context

        context = await _build_setup_context(
            owner["entity_id"],
            db,
            user_id=owner["id"],
        )

    providers = {
        row["provider"] for row in context["configured_integrations"]
    }
    assert "telegram" in providers
    assert "discord" not in providers
