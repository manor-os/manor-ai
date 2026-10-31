"""Authorization contract for user-owned Integration credential sources."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.document import Integration
from packages.core.models.channel import ChannelConfig
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
)
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.services.integration_access import (
    connection_share_summaries,
    grant_connection_use,
    resolve_integration_access,
    set_connection_entity_share,
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
async def test_share_people_show_the_same_avatar_as_the_team_page(client: AsyncClient):
    # Catches: the share dialog showing a stored random-letter placeholder (or
    # bare initials) while the team page shows the member's real staff photo.
    from packages.core.models.staff import Staff

    owner_headers = await _auth(client, "avatar_share_owner")
    owner = await _me(client, owner_headers)
    placeholder = (
        "https://api.dicebear.com/9.x/initials/svg?seed=1a2b3c4d"
        "&backgroundColor=0f766e,14b8a6,0d9488,0ea5e9,6366f1,8b5cf6"
        "&backgroundType=gradientLinear&fontSize=40"
    )
    real_photo = "/api/v1/fs/entity/avatars/avatar-share-member.png"
    member = await _create_entity_user(
        owner["entity_id"], "avatar_share_member", avatar_url=placeholder,
    )
    await _add_membership(member["id"], owner["entity_id"])
    async with db_module.async_session() as db:
        db.add(Staff(
            entity_id=owner["entity_id"], name="Avatar Share Member",
            user_id=member["id"], avatar_url=real_photo,
        ))
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    url = f"/api/v1/integrations/connections/integration/{connection_id}/grants"
    state = (await client.get(url, headers=owner_headers)).json()
    candidate = next(row for row in state["candidates"] if row["user_id"] == member["id"])
    assert candidate["avatar_url"] == real_photo

    shared = await client.post(url, headers=owner_headers, json={"user_ids": [member["id"]]})
    assert shared.json()["users"][0]["avatar_url"] == real_photo
    async with db_module.async_session() as db:
        summaries = await connection_share_summaries(
            db, entity_id=owner["entity_id"], oauth_account_ids=[], integration_ids=[connection_id],
        )
    assert [p.avatar_url for p in summaries[("integration", connection_id)].people] == [real_photo]


@pytest.mark.asyncio
async def test_connection_grant_api_only_allows_the_owner_to_share_and_revoke(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "connection_grant_api_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(
        owner["entity_id"], "connection_grant_api_member", role="member",
        avatar_url="/avatars/member.png",
    )
    other_owner = await _create_entity_user(
        owner["entity_id"], "connection_grant_api_other_owner", role="owner",
    )
    viewer = await _create_entity_user(
        owner["entity_id"], "connection_grant_api_viewer", role="viewer",
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
            UserMembership(
                user_id=viewer["id"],
                entity_id=owner["entity_id"],
                role="viewer",
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
    denied = await client.post(url, headers=other_owner["headers"], json={"user_ids": [member["id"]]})
    assert denied.status_code == 404

    created = await client.post(url, headers=owner_headers, json={"user_ids": [member["id"]]})
    assert created.status_code == 201, created.text
    state = created.json()
    assert [row["user_id"] for row in state["users"]] == [member["id"]]
    assert state["users"][0]["avatar_url"] == "/avatars/member.png"
    async with db_module.async_session() as db:
        summaries = await connection_share_summaries(
            db, entity_id=owner["entity_id"], oauth_account_ids=[], integration_ids=[connection_id],
        )
    summary = summaries[("integration", connection_id)]
    assert (summary.user_count, [(p.user_id, p.avatar_url) for p in summary.people]) == (
        1, [(member["id"], "/avatars/member.png")],
    )
    candidate_ids = [row["user_id"] for row in state["candidates"]]
    assert member["id"] not in candidate_ids
    assert other_owner["id"] in candidate_ids
    # Viewers cannot run personal integrations through agents, so they are
    # neither offered nor accepted as named recipients.
    assert viewer["id"] not in candidate_ids
    viewer_share = await client.post(url, headers=owner_headers, json={"user_ids": [viewer["id"]]})
    assert viewer_share.status_code == 400
    grant_id = state["users"][0]["grant_id"]

    listed = await client.get(url, headers=owner_headers)
    assert listed.status_code == 200, listed.text
    assert [row["grant_id"] for row in listed.json()["users"]] == [grant_id]

    recipient_listing = await client.get(url, headers=member["headers"])
    assert recipient_listing.status_code == 404

    revoked = await client.delete(f"{url}/{grant_id}", headers=owner_headers)
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


async def _add_membership(user_id: str, entity_id: str, role: str = "member") -> None:
    async with db_module.async_session() as db:
        db.add(UserMembership(user_id=user_id, entity_id=entity_id, role=role, status="active"))
        await db.commit()


@pytest.mark.asyncio
async def test_entity_share_reaches_current_and_future_members_but_not_clients(
    client: AsyncClient,
):
    # Catches: entity grants ignored, clients treated as members, or a
    # snapshot of members taken at share time instead of read-time membership.
    owner_headers = await _auth(client, "entity_share_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "entity_share_member")
    client_user = await _create_entity_user(owner["entity_id"], "entity_share_client", role="client")
    await _add_membership(member["id"], owner["entity_id"])
    await _add_membership(client_user["id"], owner["entity_id"], role="client")

    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.flush()
        await set_connection_entity_share(
            db, kind="integration", connection_id=connection.id,
            entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=True,
        )
        await db.commit()
        connection_id = connection.id

    late_member = await _create_entity_user(owner["entity_id"], "entity_share_late_member")
    await _add_membership(late_member["id"], owner["entity_id"])

    async def use(user_id: str):
        async with db_module.async_session() as db:
            return await resolve_integration_access(
                db, kind="integration", connection_id=connection_id,
                entity_id=owner["entity_id"], user_id=user_id, action="use",
            )

    assert (await use(member["id"])).reason == "shared_use"
    assert (await use(late_member["id"])).allowed is True
    assert (await use(client_user["id"])).allowed is False

    # Membership is read at access time: deactivated members and members of
    # another Entity do not resolve through the Entity grant.
    outsider = await _me(client, await _auth(client, "entity_share_outsider"))
    assert outsider["entity_id"] != owner["entity_id"]
    assert (await use(outsider["id"])).allowed is False
    async with db_module.async_session() as db:
        late = (await db.execute(select(UserMembership).where(
            UserMembership.user_id == late_member["id"],
            UserMembership.entity_id == owner["entity_id"],
        ))).scalar_one()
        late.status = "inactive"
        await db.commit()
    assert (await use(late_member["id"])).allowed is False

    async with db_module.async_session() as db:
        await set_connection_entity_share(
            db, kind="integration", connection_id=connection_id,
            entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=False,
        )
        # A USER grant created for a client before the rules tightened must
        # not resolve either.
        db.add(ResourceGrant(
            entity_id=owner["entity_id"], resource_type="integration",
            resource_id=connection_id, subject_type="user", subject_id=client_user["id"],
            capabilities=["use"], granted_by=owner["id"],
            granted_at=datetime.now(UTC), status="active",
        ))
        await db.commit()
    assert (await use(member["id"])).allowed is False
    assert (await use(client_user["id"])).allowed is False


@pytest.mark.asyncio
async def test_disabling_entity_share_revokes_duplicate_grants(client: AsyncClient):
    # Catches: a double-click leaving two active entity grants, with disable
    # revoking only one so the connection stays shared.
    owner = await _me(client, await _auth(client, "dup_share_owner"))
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.flush()
        for _ in range(2):
            db.add(ResourceGrant(
                entity_id=owner["entity_id"], resource_type="integration",
                resource_id=connection.id, subject_type="entity",
                subject_id=owner["entity_id"], capabilities=["use"],
                granted_by=owner["id"], granted_at=datetime.now(UTC), status="active",
            ))
        await db.flush()
        await set_connection_entity_share(
            db, kind="integration", connection_id=connection.id,
            entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=False,
        )
        await db.commit()
        remaining = (await db.execute(select(ResourceGrant).where(
            ResourceGrant.resource_id == connection.id,
            ResourceGrant.subject_type == "entity",
            ResourceGrant.status == "active",
        ))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_share_rejects_chrome_connections(
    client: AsyncClient,
):
    # Catches: sharing a device-bound Chrome connection.
    owner_headers = await _auth(client, "share_reject_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        chrome = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="chrome", credentials={"token": "test-token"},
        )
        db.add(chrome)
        await db.flush()
        with pytest.raises(ValueError, match="cannot be shared"):
            await set_connection_entity_share(
                db, kind="integration", connection_id=chrome.id,
                entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=True,
            )


@pytest.mark.asyncio
async def test_runtime_registry_lists_entity_shared_accounts_after_own_accounts(
    client: AsyncClient,
):
    # Catches: discovery/dispatch ignoring Entity shares, or a company-wide
    # shared account displacing the member's own default.
    owner_headers = await _auth(client, "registry_entity_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "registry_entity_member")
    await _add_membership(member["id"], owner["entity_id"])

    async with db_module.async_session() as db:
        shared = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "shared-token"},
        )
        own = Integration(
            entity_id=owner["entity_id"], owner_user_id=member["id"],
            provider="gmail", credentials={"api_key": "own-token"},
        )
        db.add_all([shared, own])
        await db.flush()
        await set_connection_entity_share(
            db, kind="integration", connection_id=shared.id,
            entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=True,
        )
        await db.commit()

        accounts = await list_runtime_integration_accounts(
            db, user_id=member["id"], entity_id=owner["entity_id"], provider="gmail",
        )

    assert [account.id for account in accounts] == [own.id, shared.id]
    assert [account.ownership for account in accounts] == ["mine", "shared"]


@pytest.mark.asyncio
async def test_owner_demoted_to_client_ends_their_shares(client: AsyncClient):
    # Catches: a former teammate's credentials staying usable by the company
    # after they become an external client, through either grant subject or
    # through the runtime registry.
    admin_headers = await _auth(client, "demoted_owner_admin")
    admin = await _me(client, admin_headers)
    entity_id = admin["entity_id"]
    owner = await _create_entity_user(entity_id, "demoted_owner")
    teammate = await _create_entity_user(entity_id, "demoted_owner_teammate")
    await _add_membership(owner["id"], entity_id)
    await _add_membership(teammate["id"], entity_id)

    async with db_module.async_session() as db:
        everyone = Integration(
            entity_id=entity_id, owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        named = OAuthAccount(
            user_id=owner["id"], provider="gmail",
            provider_user_id="demoted-owner-box", access_token="t",
        )
        db.add_all([everyone, named])
        await db.flush()
        await set_connection_entity_share(
            db, kind="integration", connection_id=everyone.id,
            entity_id=entity_id, owner_user_id=owner["id"], enabled=True,
        )
        await grant_connection_use(
            db, kind="oauth_account", connection_id=named.id, entity_id=entity_id,
            owner_user_id=owner["id"], grantee_user_id=teammate["id"],
        )
        await db.commit()
        everyone_id, named_id = everyone.id, named.id

    async def set_owner_role(role: str) -> None:
        async with db_module.async_session() as db:
            membership = (await db.execute(
                select(UserMembership).where(
                    UserMembership.user_id == owner["id"],
                    UserMembership.entity_id == entity_id,
                )
            )).scalar_one()
            membership.role = role
            await db.commit()

    async def teammate_view():
        async with db_module.async_session() as db:
            decisions = [
                (await resolve_integration_access(
                    db, kind=kind, connection_id=connection_id,
                    entity_id=entity_id, user_id=teammate["id"], action="use",
                )).allowed
                for kind, connection_id in (("integration", everyone_id), ("oauth_account", named_id))
            ]
            accounts = await list_runtime_integration_accounts(
                db, user_id=teammate["id"], entity_id=entity_id, provider="gmail",
            )
        return decisions, sorted(account.id for account in accounts)

    assert await teammate_view() == ([True, True], sorted([everyone_id, named_id]))

    await set_owner_role("client")
    assert await teammate_view() == ([False, False], [])
    async with db_module.async_session() as db:
        own_use = await resolve_integration_access(
            db, kind="integration", connection_id=everyone_id,
            entity_id=entity_id, user_id=owner["id"], action="use",
        )
        assert own_use.allowed is True
        with pytest.raises(ValueError, match="can no longer share"):
            await grant_connection_use(
                db, kind="integration", connection_id=everyone_id, entity_id=entity_id,
                owner_user_id=owner["id"], grantee_user_id=teammate["id"],
            )
    listed = await client.get("/api/v1/integrations/shares", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert {
        row["owner_active"] for row in listed.json() if row["owner_user_id"] == owner["id"]
    } == {False}

    await set_owner_role("member")
    assert await teammate_view() == ([True, True], sorted([everyone_id, named_id]))


@pytest.mark.asyncio
async def test_member_owner_shares_with_everyone_and_removal_revokes_shares(
    client: AsyncClient,
):
    # Catches: members still blocked from sharing their own account; deleting a
    # connection leaving live grants behind.
    entity_owner_headers = await _auth(client, "member_share_entity_owner")
    entity_owner = await _me(client, entity_owner_headers)
    member_owner = await _create_entity_user(entity_owner["entity_id"], "member_share_owner")
    teammate = await _create_entity_user(entity_owner["entity_id"], "member_share_teammate")
    await _add_membership(member_owner["id"], entity_owner["entity_id"])
    await _add_membership(teammate["id"], entity_owner["entity_id"])

    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=entity_owner["entity_id"], owner_user_id=member_owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    base = f"/api/v1/integrations/connections/integration/{connection_id}"
    enabled = await client.put(
        f"{base}/entity-share", headers=member_owner["headers"], json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["entity_grant_id"]

    async with db_module.async_session() as db:
        decision = await resolve_integration_access(
            db, kind="integration", connection_id=connection_id,
            entity_id=entity_owner["entity_id"], user_id=teammate["id"], action="use",
        )
    assert decision.allowed is True

    removed = await client.delete(
        f"/api/v1/integrations/{connection_id}", headers=member_owner["headers"],
    )
    assert removed.status_code == 204, removed.text
    async with db_module.async_session() as db:
        live = (await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_id == connection_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
        )).scalars().all()
    assert live == []


class _NullSession:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id, recorded", [("owner-1", True), ("actor-1", False)])
async def test_dispatch_records_only_non_owner_credential_use(monkeypatch, owner_id, recorded):
    # Catches: shared use going unrecorded, or the owner's own calls flooding
    # the owner's usage view.
    import packages.core.database as database
    from packages.core.ai.mcp import file_registrar
    from packages.core.ai.tools import mcp_builtin
    from packages.core.services import agent_permission_service, connection_usage
    from packages.core.services.agent_permission_service import ToolAccessDecision

    monkeypatch.setattr(database, "async_session", lambda: _NullSession())
    monkeypatch.setattr(agent_permission_service, "can_use_integration", AsyncMock(
        return_value=ToolAccessDecision(
            allowed=True, reason="shared", scope="user", account_id="acct-1",
            account_kind="oauth_account", account_owner_user_id=owner_id,
        ),
    ))
    monkeypatch.setattr(mcp_builtin, "_resolve_bearer_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(file_registrar, "register_generated_files", AsyncMock(return_value=0))
    record = Mock()
    monkeypatch.setattr(connection_usage, "record_shared_connection_use", record)
    call_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    module = SimpleNamespace(call_tool=call_tool)
    context = mcp_builtin._MCPResolvedDispatchContext(
        server_row=SimpleNamespace(server_key="notion"), transport="builtin", module=module,
    )

    await mcp_builtin._build_handler("notion", "search")(
        entity_id="entity-1", user_id="actor-1", query="q",
        _runtime_mcp_dispatch_context=context,
    )

    call_tool.assert_awaited_once()
    assert record.call_count == (1 if recorded else 0)
    if recorded:
        kwargs = record.call_args.kwargs
        assert (kwargs["connection_id"], kwargs["actor_user_id"], kwargs["owner_user_id"]) == (
            "acct-1", "actor-1", "owner-1",
        )
        assert (kwargs["server_key"], kwargs["tool_name"]) == ("notion", "search")


@pytest.mark.asyncio
async def test_connection_usage_is_visible_to_the_owner_only(client: AsyncClient):
    # Catches: recipients reading the owner's usage log, or the owner's own
    # use being stored.
    from packages.core.services.connection_usage import (
        flush_shared_connection_uses,
        record_shared_connection_use,
    )

    owner_headers = await _auth(client, "usage_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "usage_member", display_name="Usage Member")
    await _add_membership(member["id"], owner["entity_id"])
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    for actor in (member["id"], owner["id"]):
        record_shared_connection_use(
            entity_id=owner["entity_id"], connection_kind="integration",
            connection_id=connection_id, owner_user_id=owner["id"], actor_user_id=actor,
            server_key="gmail", tool_name="send_email",
        )
    await flush_shared_connection_uses()

    url = f"/api/v1/integrations/connections/integration/{connection_id}/usage"
    owner_view = await client.get(url, headers=owner_headers)
    assert owner_view.status_code == 200, owner_view.text
    assert [(row["actor_user_id"], row["actor_display_name"], row["tool_name"]) for row in owner_view.json()] == [
        (member["id"], "Usage Member", "send_email"),
    ]
    assert (await client.get(url, headers=member["headers"])).status_code == 404


@pytest.mark.asyncio
async def test_shared_use_burst_coalesces_into_few_transactions(client: AsyncClient, monkeypatch):
    # Catches: one DB transaction (one admission permit) per shared tool call,
    # which starves chat pods when a company-wide account is busy.
    from packages.core.services import connection_usage

    owner_headers = await _auth(client, "usage_burst_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "usage_burst_member")
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    real_session = db_module.async_session
    opened = 0

    def counting_session():
        nonlocal opened
        opened += 1
        return real_session()

    monkeypatch.setattr(db_module, "async_session", counting_session)
    for index in range(50):
        connection_usage.record_shared_connection_use(
            entity_id=owner["entity_id"], connection_kind="integration",
            connection_id=connection_id, owner_user_id=owner["id"], actor_user_id=member["id"],
            server_key="gmail", tool_name=f"tool_{index}",
        )
    await connection_usage.flush_shared_connection_uses()
    monkeypatch.setattr(db_module, "async_session", real_session)

    assert opened <= 2
    async with db_module.async_session() as db:
        uses = await connection_usage.list_connection_uses(
            db, entity_id=owner["entity_id"], connection_kind="integration",
            connection_id=connection_id, limit=100,
        )
    assert len(uses) == 50


@pytest.mark.asyncio
async def test_shared_use_writer_waits_for_its_own_admission_permit(monkeypatch):
    # Catches: the background writer inheriting the enqueuing turn's admission
    # lineage, so its inserts ride the turn's permit and bypass backpressure.
    from packages.core import database_runtime
    from packages.core.services import connection_usage

    seen_lineage: list[tuple] = []

    def observing_session():
        seen_lineage.append(database_runtime._admission_lineage.get())
        return _NullSession()

    monkeypatch.setattr(db_module, "async_session", observing_session)
    token = database_runtime._admission_lineage.set(("turn-holding-a-permit",))
    try:
        connection_usage.record_shared_connection_use(
            entity_id="entity-1", connection_kind="integration", connection_id="conn-1",
            owner_user_id="owner-1", actor_user_id="actor-1",
            server_key="gmail", tool_name="send_email",
        )
    finally:
        database_runtime._admission_lineage.reset(token)
    await connection_usage.flush_shared_connection_uses()

    assert seen_lineage == [()]


@pytest.mark.asyncio
async def test_concurrent_shares_leave_exactly_one_grant_per_subject(client: AsyncClient):
    # Catches: check-then-insert races (double clicks, retries) creating
    # duplicate grants, after which "Remove" left the recipient with access.
    owner_headers = await _auth(client, "concurrent_share_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "concurrent_share_member")
    await _add_membership(member["id"], owner["entity_id"])
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    async def share_with_member():
        async with db_module.async_session() as db:
            await grant_connection_use(
                db, kind="integration", connection_id=connection_id, entity_id=owner["entity_id"],
                owner_user_id=owner["id"], grantee_user_id=member["id"],
            )
            await db.commit()

    async def share_with_everyone():
        async with db_module.async_session() as db:
            await set_connection_entity_share(
                db, kind="integration", connection_id=connection_id,
                entity_id=owner["entity_id"], owner_user_id=owner["id"], enabled=True,
            )
            await db.commit()

    await asyncio.gather(
        *(share_with_member() for _ in range(8)),
        *(share_with_everyone() for _ in range(8)),
    )

    async with db_module.async_session() as db:
        active = (await db.execute(
            select(ResourceGrant.subject_type).where(
                ResourceGrant.resource_id == connection_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
        )).scalars().all()
    assert sorted(active) == ["entity", "user"]


@pytest.mark.asyncio
async def test_removing_a_person_revokes_their_duplicate_grants(client: AsyncClient):
    # Catches: duplicate active grants written before the share lock existed
    # keeping access alive after the owner removes the person once.
    owner_headers = await _auth(client, "duplicate_grant_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "duplicate_grant_member")
    await _add_membership(member["id"], owner["entity_id"])
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.flush()
        db.add_all([
            ResourceGrant(
                entity_id=owner["entity_id"], resource_type="integration",
                resource_id=connection.id, subject_type="user", subject_id=member["id"],
                capabilities=["use"], granted_by=owner["id"],
                granted_at=datetime.now(UTC), status="active",
            )
            for _ in range(2)
        ])
        await db.commit()
        connection_id = connection.id

    url = f"/api/v1/integrations/connections/integration/{connection_id}/grants"
    state = (await client.get(url, headers=owner_headers)).json()
    assert [row["user_id"] for row in state["users"]] == [member["id"]]
    removed = await client.delete(f"{url}/{state['users'][0]['grant_id']}", headers=owner_headers)
    assert removed.status_code == 204, removed.text

    async with db_module.async_session() as db:
        decision = await resolve_integration_access(
            db, kind="integration", connection_id=connection_id,
            entity_id=owner["entity_id"], user_id=member["id"], action="use",
        )
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_batch_share_with_a_client_recipient_is_rejected_atomically(
    client: AsyncClient,
):
    # Catches: a partially applied batch (the valid recipient keeps a grant)
    # when a later recipient is not allowed to receive shares.
    owner_headers = await _auth(client, "batch_share_atomic_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "batch_share_atomic_member")
    client_user = await _create_entity_user(
        owner["entity_id"], "batch_share_atomic_client", role="client",
    )
    await _add_membership(member["id"], owner["entity_id"])
    await _add_membership(client_user["id"], owner["entity_id"], role="client")

    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=owner["entity_id"], owner_user_id=owner["id"],
            provider="telegram", credentials={"bot_token": "test-token"},
        )
        db.add(connection)
        await db.commit()
        connection_id = connection.id

    response = await client.post(
        f"/api/v1/integrations/connections/integration/{connection_id}/grants",
        headers=owner_headers,
        json={"user_ids": [member["id"], client_user["id"]]},
    )
    assert response.status_code == 400, response.text

    async with db_module.async_session() as db:
        live = (await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.resource_id == connection_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
        )).scalars().all()
    assert live == []


@pytest.mark.asyncio
async def test_admin_lists_and_revokes_shares_without_gaining_use(client: AsyncClient):
    # Catches: members reading the Entity-wide share list; admin revoke not
    # taking effect; admin visibility leaking into use rights.
    admin_headers = await _auth(client, "share_admin")
    admin = await _me(client, admin_headers)
    owner = await _create_entity_user(admin["entity_id"], "share_admin_owner")
    teammate = await _create_entity_user(admin["entity_id"], "share_admin_teammate")
    await _add_membership(owner["id"], admin["entity_id"])
    await _add_membership(teammate["id"], admin["entity_id"])
    async with db_module.async_session() as db:
        connection = Integration(
            entity_id=admin["entity_id"], owner_user_id=owner["id"],
            provider="gmail", credentials={"api_key": "test-token"},
        )
        db.add(connection)
        await db.flush()
        grant = await grant_connection_use(
            db, kind="integration", connection_id=connection.id, entity_id=admin["entity_id"],
            owner_user_id=owner["id"], grantee_user_id=teammate["id"],
        )
        await db.commit()
        connection_id, grant_id = connection.id, grant.id

    # An admin of a different Entity sees and can revoke nothing here.
    other_headers = await _auth(client, "share_admin_other_entity")
    other_listed = await client.get("/api/v1/integrations/shares", headers=other_headers)
    assert other_listed.status_code == 200, other_listed.text
    assert grant_id not in {item["grant_id"] for item in other_listed.json()}
    other_revoke = await client.delete(f"/api/v1/integrations/shares/{grant_id}", headers=other_headers)
    assert other_revoke.status_code == 404, other_revoke.text

    # OAuth-account shares are listed too, and a deactivated owner is flagged.
    async with db_module.async_session() as db:
        oauth = OAuthAccount(
            user_id=owner["id"], provider="gmail",
            provider_user_id="admin-list-oauth", access_token="t",
        )
        db.add(oauth)
        await db.flush()
        oauth_grant = await grant_connection_use(
            db, kind="oauth_account", connection_id=oauth.id, entity_id=admin["entity_id"],
            owner_user_id=owner["id"], grantee_user_id=teammate["id"],
        )
        await db.commit()
        oauth_id, oauth_grant_id = oauth.id, oauth_grant.id

    assert (await client.get("/api/v1/integrations/shares", headers=teammate["headers"])).status_code == 403
    listed = await client.get("/api/v1/integrations/shares", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    oauth_row = next(item for item in listed.json() if item["grant_id"] == oauth_grant_id)
    assert (oauth_row["connection_kind"], oauth_row["connection_id"]) == ("oauth_account", oauth_id)
    row = next(item for item in listed.json() if item["grant_id"] == grant_id)
    assert (row["connection_id"], row["owner_user_id"], row["recipient_user_id"], row["owner_active"]) == (
        connection_id, owner["id"], teammate["id"], True,
    )

    async with db_module.async_session() as db:
        admin_use = await resolve_integration_access(
            db, kind="integration", connection_id=connection_id,
            entity_id=admin["entity_id"], user_id=admin["id"], action="use",
        )
    assert admin_use.allowed is False

    revoked = await client.delete(f"/api/v1/integrations/shares/{grant_id}", headers=admin_headers)
    assert revoked.status_code == 204, revoked.text
    async with db_module.async_session() as db:
        teammate_use = await resolve_integration_access(
            db, kind="integration", connection_id=connection_id,
            entity_id=admin["entity_id"], user_id=teammate["id"], action="use",
        )
    assert teammate_use.allowed is False

    async with db_module.async_session() as db:
        membership = (await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == owner["id"],
                UserMembership.entity_id == admin["entity_id"],
            )
        )).scalar_one()
        membership.status = "inactive"
        await db.commit()
    listed = await client.get("/api/v1/integrations/shares", headers=admin_headers)
    oauth_row = next(item for item in listed.json() if item["grant_id"] == oauth_grant_id)
    assert oauth_row["owner_active"] is False


@pytest.mark.asyncio
async def test_shared_mailbox_is_a_sender_but_unshared_one_is_not(client: AsyncClient, monkeypatch):
    # Catches: shared mailboxes filtered out of senders, or the token lookup
    # leasing a mailbox the sender was never given.
    from packages.core.services import connected_email_accounts, email_service

    owner_headers = await _auth(client, "mailbox_owner")
    owner = await _me(client, owner_headers)
    member = await _create_entity_user(owner["entity_id"], "mailbox_member")
    await _add_membership(member["id"], owner["entity_id"])
    async with db_module.async_session() as db:
        shared = OAuthAccount(user_id=owner["id"], provider="outlook", provider_user_id="shared-box", access_token="t1")
        private = OAuthAccount(user_id=owner["id"], provider="gmail", provider_user_id="private-box", access_token="t2")
        # The member's own Gmail mailbox sorts ahead of the shared Outlook one,
        # so only the stored sender preference can make the shared one default.
        own = OAuthAccount(user_id=member["id"], provider="gmail", provider_user_id="own-box", access_token="t3")
        db.add_all([shared, private, own])
        await db.flush()
        await grant_connection_use(
            db, kind="oauth_account", connection_id=shared.id, entity_id=owner["entity_id"],
            owner_user_id=owner["id"], grantee_user_id=member["id"],
        )
        await db.commit()
        shared_id, private_id = shared.id, private.id

    monkeypatch.setattr(email_service, "connected_oauth_sender", AsyncMock(return_value="team@example.com"))
    async with db_module.async_session() as db:
        senders = await connected_email_accounts.available_email_senders(
            db, entity_id=owner["entity_id"], user_id=member["id"],
        )
        denied = await connected_email_accounts.sender_oauth_token(
            db, sender_id=f"oauth:{private_id}", provider="gmail",
            entity_id=owner["entity_id"], user_id=member["id"], reason="test",
        )
    assert (f"oauth:{shared_id}", "team@example.com", "outlook") in senders
    assert all(sender[0] != f"oauth:{private_id}" for sender in senders)
    assert denied is None

    # A shared mailbox can also be the member's own default sender; this only
    # writes the member's preference, never the owner's row.
    async with db_module.async_session() as db:
        before = await connected_email_accounts.email_account_settings(
            db, entity_id=owner["entity_id"], user_id=member["id"],
        )
        assert before["default_sender_id"] != f"oauth:{shared_id}"
        assert await connected_email_accounts.set_default_email_sender(
            db, entity_id=owner["entity_id"], user_id=member["id"], sender_id=f"oauth:{shared_id}",
        )
        await db.commit()
    async with db_module.async_session() as db:
        settings = await connected_email_accounts.email_account_settings(
            db, entity_id=owner["entity_id"], user_id=member["id"],
        )
    assert settings["default_sender_id"] == f"oauth:{shared_id}"
