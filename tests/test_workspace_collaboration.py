"""Cross-company collaboration cannot become tenant membership or implicit execution."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from test_client_workspace_delivery import prepared, register
from packages.core.models.user import UserMembership
from packages.core.models.staff import Staff
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration, WorkspaceCollaborationEntry
from packages.core.models.workflow import WorkflowRun
from packages.core.models.chat_room import ChatRoom, ChatRoomMember


async def accepted(client, db):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db)
    response = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert response.status_code == 200, response.text
    return ph, p, rh, r, cid, source, response.json()["target_workspace_id"], receipt, token


async def invited(client, db, *, role="maintainer", share=True, accept=True):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db)
    invitation = await client.post(
        f"/api/v1/workspaces/{target}/collaboration",
        headers=rh,
        json={"email": p.email, "role": role, "days": 14, "share_configuration": share},
    )
    assert invitation.status_code == 201, invitation.text
    gid = invitation.json()["id"]
    if accept:
        joined = await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)
        assert joined.status_code == 200, joined.text
    return ph, p, rh, r, target, gid


@pytest.mark.asyncio
async def test_trial_acceptance_keeps_same_customer_owner_and_paused_workspace(client, db_session):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    state = await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)
    assert state.status_code == 200, state.text
    assert state.json()["phase"] == "trial"
    before = await db_session.scalar(select(func.count()).select_from(Workspace))
    assert (
        await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=ph, json={"action": "accept"})
    ).status_code == 404
    requested = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "request_acceptance"}
    )
    assert requested.json()["phase"] == "awaiting_acceptance"
    payload = {"action": "accept", "note": "Reviewed bilingual outputs"}
    confirmed = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
    repeated = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == repeated.json()
    assert confirmed.json()["acceptance"]["confirmed_by"] == r.id
    assert (await db_session.scalar(select(func.count()).select_from(Workspace))) == before
    await db_session.refresh(await db_session.get(Workspace, target))
    assert (await db_session.get(Workspace, target)).status == "paused"
    owners = (await db_session.scalars(select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target))).all()
    assert [(m.user_id, m.role, m.staff_id) for m in owners] == [(r.id, "owner", None)]
    provider = (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()[0]
    assert provider["phase"] == "delivered"


@pytest.mark.asyncio
async def test_invitation_requires_explicit_acceptance_and_never_creates_staff_or_company_membership(
    client, db_session
):
    ph, p, rh, r, target, gid = await invited(client, db_session, accept=False)
    portal = await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)
    assert portal.status_code == 200, portal.text
    assert portal.json()["configuration"] is None
    assert (
        await client.post(
            f"/api/v1/workspace-collaborations/{gid}/entries", headers=ph, json={"kind": "feedback", "body": "test"}
        )
    ).status_code == 404
    for _ in range(2):
        assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Staff).where(Staff.entity_id == r.entity_id, Staff.user_id == p.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UserMembership)
            .where(UserMembership.entity_id == r.entity_id, UserMembership.user_id == p.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkspaceStaff)
            .where(WorkspaceStaff.workspace_id == target, WorkspaceStaff.user_id == p.id)
        )
        == 0
    )
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    assert (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=ph)).status_code == 404
    outsider, _ = await register(client, db_session, "outsider")
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=outsider)).status_code == 404


@pytest.mark.asyncio
async def test_provider_requests_customer_authorization_without_default_access(client, db_session):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    path = f"/api/v1/clients/{cid}/deliveries/{delivery['id']}/maintenance/request"
    first = await client.post(path, headers=ph)
    assert first.status_code == 200, first.text
    second = await client.post(path, headers=ph)
    assert first.json()["id"] == second.json()["id"]
    gid = first.json()["id"]
    assert first.json()["status"] == "requested"
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 403
    state = (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()
    assert state["grants"][0]["id"] == gid
    approved = await client.post(
        f"/api/v1/workspaces/{target}/collaboration/{gid}/authorize",
        headers=rh,
        json={"email": p.email, "role": "maintainer", "days": 7, "share_configuration": False},
    )
    assert approved.status_code == 200, approved.text
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    history = (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()[0]
    assert history["maintenance"]["status"] == "active"
    assert history["provider_access"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["expired", "revoked", "deleted_workspace", "deleted_issuer", "revoked_issuer_membership", "unverified_guest"],
)
async def test_every_external_read_rechecks_live_authority(client, db_session, failure):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    grant = await db_session.get(WorkspaceCollaboration, gid)
    if failure == "expired":
        grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif failure == "revoked":
        assert (
            await client.post(f"/api/v1/workspaces/{target}/collaboration/{gid}/revoke", headers=rh)
        ).status_code == 200
    elif failure == "deleted_workspace":
        (await db_session.get(Workspace, target)).deleted_at = datetime.now(timezone.utc)
    elif failure == "deleted_issuer":
        r.deleted_at = datetime.now(timezone.utc)
    elif failure == "revoked_issuer_membership":
        membership = await db_session.scalar(
            select(UserMembership).where(UserMembership.user_id == r.id, UserMembership.entity_id == r.entity_id)
        )
        membership.status = "inactive"
    else:
        p.verified_email = None
    await db_session.commit()
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).status_code in {401, 403, 410}
    assert (
        await client.post(
            f"/api/v1/workspace-collaborations/{gid}/entries",
            headers=ph,
            json={"kind": "feedback", "body": "late write"},
        )
    ).status_code in {401, 404}
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceCollaborationEntry)) == 0


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_collaboration_migration_upgrade_downgrade_repeats(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from tests.migration_test_helpers import script_directory

    revision = script_directory().get_revision("20261002_client_collab").module

    def migrate(session):
        connection = session.connection()
        connection.execute(text("CREATE SCHEMA collaboration_migration_test"))
        connection.execute(text("SET LOCAL search_path TO collaboration_migration_test"))
        connection.execute(text("CREATE TABLE client_workspace_deliveries (id VARCHAR(26))"))
        with Operations.context(MigrationContext.configure(connection)):
            for _ in range(2):
                revision.upgrade()
                for model in (WorkspaceCollaboration, WorkspaceCollaborationEntry):
                    columns = {c["name"]: c for c in inspect(connection).get_columns(model.__tablename__)}
                    assert set(columns) == set(model.__table__.columns.keys())
                    for expected in model.__table__.columns:
                        assert columns[expected.name]["nullable"] == expected.nullable
                revision.downgrade()
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA collaboration_migration_test CASCADE"))

    await db_session.run_sync(migrate)


@pytest.mark.asyncio
async def test_issuer_membership_restore_does_not_revive_old_authorization(client, db_session):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == r.id, UserMembership.entity_id == r.entity_id)
    )
    membership.status = "inactive"
    await db_session.commit()
    membership.status = "active"
    await db_session.commit()
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).status_code == 410


@pytest.mark.asyncio
async def test_customer_can_refresh_shared_configuration_without_reinviting_active_maintainer(client, db_session):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    ws = await db_session.get(Workspace, target)
    ws.operation_revision += 1
    ws.operating_model = {
        "services": [{"service_key": "marketing", "title": "Marketing", "description": "Updated scope"}]
    }
    await db_session.commit()
    updated = await client.post(
        f"/api/v1/workspaces/{target}/collaboration/{gid}/update",
        headers=rh,
        json={"email": p.email, "role": "maintainer", "days": 7, "share_configuration": True},
    )
    assert updated.status_code == 200, updated.text
    portal = (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).json()
    assert portal["grant"]["status"] == "active" and portal["configuration"]["revision"] == ws.operation_revision
    assert portal["configuration"]["services"][0]["description"] == "Updated scope"
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    duplicate = await client.post(f"/api/v1/workspaces/{target}/collaboration", headers=rh, json={"email": p.email})
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_normal_workspace_collaboration_does_not_require_client_delivery_and_deleted_workspace_restore_keeps_access_revoked(
    client, db_session
):
    from packages.core.services.entity_service import soft_delete_workspace, restore_workspace

    owner_headers, owner = await register(client, db_session, "ordinary-owner")
    guest_headers, guest = await register(client, db_session, "ordinary-guest")
    created = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Internal prototype", "kind": "project", "primary_work": "Prototype"},
    )
    target = created.json()["id"]
    invitation = await client.post(
        f"/api/v1/workspaces/{target}/collaboration", headers=owner_headers, json={"email": guest.email}
    )
    assert invitation.status_code == 201, invitation.text
    gid = invitation.json()["id"]
    assert (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=owner_headers)).json()[
        "phase"
    ] is None
    assert (
        await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=guest_headers)
    ).status_code == 200
    await soft_delete_workspace(db_session, target, owner.entity_id)
    await db_session.commit()
    await restore_workspace(db_session, target, owner.entity_id)
    await db_session.commit()
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=guest_headers)).status_code == 410


async def group(db, owner, *members, kind="group"):
    room = ChatRoom(kind=kind, title="Customer product discussion", owner_user_id=owner.id, status="active")
    db.add(room)
    await db.flush()
    for user in (owner, *members):
        db.add(
            ChatRoomMember(
                room_id=room.id,
                user_id=user.id,
                role="owner" if user.id == owner.id else "member",
                status="active",
                visible_from=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    return room


@pytest.mark.asyncio
async def test_existing_group_link_uses_live_membership_without_granting_any_access_or_execution(client, db_session):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    room = await group(db_session, r, p)
    path = f"/api/v1/workspaces/{target}/collaboration-group"
    before_members = await db_session.scalar(select(func.count()).select_from(ChatRoomMember))
    # The provider has an active maintenance grant, but cannot alter the customer's association.
    assert (await client.put(path, headers=ph, json={"group_id": room.id})).status_code == 404
    response = await client.put(path, headers=rh, json={"group_id": room.id})
    assert response.status_code == 200, response.text
    assert response.json()["group"] == {"id": room.id, "title": room.title, "path": f"/chat?room={room.id}"}
    state = (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()
    assert state["communication"] == response.json()
    assert state["group_options"] == [response.json()["group"]]
    assert "entries" not in state and "flows" not in state
    portal = (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).json()
    assert portal["communication"] == response.json()
    assert "entries" not in portal
    assert (await client.get(f"/api/v1/chat/rooms/{room.id}", headers=ph)).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(ChatRoomMember)) == before_members
    assert (
        await db_session.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.workspace_id == target))
        == 0
    )
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceCollaborationEntry)) == 0
    # Group membership and collaboration are independent, including after revocation.
    assert (await client.post(f"/api/v1/workspaces/{target}/collaboration/{gid}/revoke", headers=rh)).status_code == 200
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).status_code == 410
    assert (await client.get(f"/api/v1/chat/rooms/{room.id}", headers=ph)).status_code == 200
    assert (await client.put(path, headers=rh, json={"group_id": None})).json() == {"linked": False, "group": None}
    ws = await db_session.get(Workspace, target)
    await db_session.refresh(ws)
    assert "collaboration_group_id" not in ws.settings and ws.status == "paused"


@pytest.mark.asyncio
async def test_nonmember_guest_never_receives_private_group_metadata_or_automatic_invitation(client, db_session):
    ph, p, rh, r, target, gid = await invited(client, db_session, accept=False)
    room = await group(db_session, r)
    assert (
        await client.put(f"/api/v1/workspaces/{target}/collaboration-group", headers=rh, json={"group_id": room.id})
    ).status_code == 200
    before_accept = (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).json()
    assert before_accept["communication"] == {"linked": False, "group": None}
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    portal = (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).json()
    assert portal["communication"] == {"linked": True, "group": None}
    assert room.id not in str(portal) and room.title not in str(portal)
    assert (await client.get(f"/api/v1/chat/rooms/{room.id}", headers=ph)).status_code == 404
    from packages.core.models.chat_room import ChatRoomInvite

    assert await db_session.scalar(select(func.count()).select_from(ChatRoomInvite)) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(ChatRoomMember).where(ChatRoomMember.user_id == p.id))
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["left", "closed", "deleted_owner", "foreign_group", "dm", "missing"])
async def test_group_link_fails_closed_and_portal_rechecks_group_lifecycle(client, db_session, failure):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    room = await group(db_session, r, p)
    path = f"/api/v1/workspaces/{target}/collaboration-group"
    assert (await client.put(path, headers=rh, json={"group_id": room.id})).status_code == 200
    if failure == "left":
        for member in (await db_session.scalars(select(ChatRoomMember).where(ChatRoomMember.room_id == room.id))).all():
            member.status = "left"
    elif failure == "closed":
        room.status = "closed"
    elif failure == "deleted_owner":
        # A third party's Group can be associated without giving them Workspace access.
        oh, other = await register(client, db_session, "group-owner")
        room.owner_user_id = other.id
        other.deleted_at = datetime.now(timezone.utc)
    elif failure == "foreign_group":
        oh, other = await register(client, db_session, "foreign-group-owner")
        room = await group(db_session, other)
    elif failure == "dm":
        room.kind = "dm"
    await db_session.commit()
    selected = "missing" if failure == "missing" else room.id
    assert (await client.put(path, headers=rh, json={"group_id": selected})).status_code == 404
    if failure in {"left", "closed", "deleted_owner", "dm"}:
        portal = (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)).json()
        assert portal["communication"] == {"linked": True, "group": None}
        assert room.id not in str(portal) and room.title not in str(portal)
        state = (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()
        assert state["group_options"] == []


@pytest.mark.asyncio
async def test_removed_submission_and_review_routes_cannot_write_or_start_work(client, db_session):
    ph, p, rh, r, target, gid = await invited(client, db_session)
    assert (
        await client.post(
            f"/api/v1/workspace-collaborations/{gid}/entries", headers=ph, json={"kind": "test_request", "body": "run"}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/workspaces/{target}/collaboration-entries/old/review", headers=rh, json={"action": "test"}
        )
    ).status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceCollaborationEntry)) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.workspace_id == target))
        == 0
    )


@pytest.mark.asyncio
async def test_group_pointer_never_crosses_blueprint_export_even_if_declared_portable(client, db_session):
    from packages.core.blueprints.exporter import export_workspace

    ph, p, rh, r, target, gid = await invited(client, db_session)
    room = await group(db_session, r, p)
    ws = await db_session.get(Workspace, target)
    ws.settings = {
        **ws.settings,
        "collaboration_group_id": room.id,
        "_blueprint": {"portable_setting_keys": ["collaboration_group_id"]},
    }
    await db_session.commit()
    payload = await export_workspace(db_session, target, title=ws.name)
    assert room.id not in str(payload) and "collaboration_group_id" not in str(payload)
