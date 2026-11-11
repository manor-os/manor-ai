"""Cross-company collaboration cannot become tenant membership or implicit execution."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from test_client_workspace_delivery import prepared, register
from packages.core.models.base import generate_ulid
from packages.core.models.client_delivery import ClientContact, ClientWorkspaceDelivery
from packages.core.models.user import User, UserMembership
from packages.core.models.staff import Staff
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration, WorkspaceCollaborationEntry
from packages.core.models.workflow import WorkflowRun
from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.services.auth_service import create_access_token


async def accepted(client, db, *, phase="trial"):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db, phase=phase)
    response = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert response.status_code == 200, response.text
    return ph, p, rh, r, cid, source, response.json()["target_workspace_id"], receipt, token


async def minted(client, provider_headers, source, delivery_id):
    """A scoped session into the customer Workspace, minted from the provider's own sign-in."""
    response = await client.post(
        f"/api/v1/workspaces/{source}/client-trials/{delivery_id}/session", headers=provider_headers
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def invited(client, db, *, role="maintainer", share=True, accept=True):
    # A direct delivery: no trial grant, so the customer's invitation is the only access.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db, phase="delivered")
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


async def trial_grant(db, target):
    return await db.scalar(
        select(WorkspaceCollaboration)
        .where(WorkspaceCollaboration.workspace_id == target, WorkspaceCollaboration.delivery_id.is_not(None))
        .execution_options(populate_existing=True)
    )


async def provider_access(client, headers, cid):
    # What the provider's own delivery list reports, derived from live grants.
    return (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=headers)).json()[0]["provider_access"]


@pytest.mark.asyncio
async def test_trial_provider_stays_until_acceptance_removes_them(client, db_session):
    # Catches: the customer removing the provider mid-trial through any grant
    # action; acceptance proceeding without a keep/remove decision; requesting
    # acceptance ending access early; "remove" leaving the seat, the membership
    # or the provider's company switch behind, or not being recorded.
    from packages.core.services.external_collaboration_access import collaborator_workspace_ids

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    grant = await trial_grant(db_session, target)
    # The grant issued at acceptance passes the per-request gate as issued.
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == {target}
    state = await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)
    assert state.status_code == 200, state.text
    assert state.json()["phase"] == "trial"
    before = await db_session.scalar(select(func.count()).select_from(Workspace))
    assert (
        await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=ph, json={"action": "accept"})
    ).status_code == 404
    base = f"/api/v1/workspaces/{target}/collaboration/{grant.id}"
    body = {"email": p.email, "role": "maintainer", "days": 7}
    for action in ("revoke", "update", "authorize"):
        refused = await client.post(f"{base}/{action}", headers=rh, json=None if action == "revoke" else body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"].startswith("The provider stays during the trial")
    requested = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "request_acceptance"}
    )
    assert requested.json()["phase"] == "awaiting_acceptance"
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.collaboration_id for s in seats] == [grant.id]
    payload = {"action": "accept", "note": "Reviewed bilingual outputs"}
    undecided = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
    assert undecided.status_code == 422 and "keep the provider" in undecided.json()["detail"]
    # The provider had switched into the customer company; removal sends them home.
    p.entity_id, p.role = customer, "collaborator"
    await db_session.commit()
    payload["provider"] = "remove"
    confirmed = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
    repeated = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == repeated.json()
    assert confirmed.json()["acceptance"]["confirmed_by"] == r.id
    assert confirmed.json()["acceptance"]["provider"] == "remove"
    assert (await db_session.scalar(select(func.count()).select_from(Workspace))) == before
    ws = await db_session.get(Workspace, target)
    await db_session.refresh(ws)
    assert ws.status == "paused"
    assert (await trial_grant(db_session, target)).status == "revoked"
    memberships, seats = await derived(db_session, p.id, customer)
    assert [(m.role, m.status) for m in memberships] == [("collaborator", "inactive")] and seats == []
    await db_session.refresh(p)
    assert p.entity_id != customer
    owners = (await db_session.scalars(select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target))).all()
    assert [(m.user_id, m.role, m.staff_id) for m in owners] == [(r.id, "owner", None)]
    provider = (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()[0]
    assert (provider["phase"], provider["provider_access"]) == ("delivered", "none")
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == frozenset()


@pytest.mark.asyncio
async def test_kept_provider_becomes_a_never_expiring_maintainer_the_customer_can_later_remove(client, db_session):
    # Catches: "keep" leaving the trial role, a finite expiry or the provider's
    # stale issuer authority; an update moving the grant's expiry without the
    # seat following; the maintainer becoming irrevocable; the customer's member
    # list hiding the provider's seat or not marking it as grant-derived.
    from packages.core.services.external_collaboration_access import collaborator_workspace_ids

    async def member_roles():
        staff = await client.get(f"/api/v1/workspaces/{target}/staff", headers=rh)
        assert staff.status_code == 200, staff.text
        return {row["user_id"]: row["collaboration_role"] for row in staff.json()}

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    grant = await trial_grant(db_session, target)
    assert await member_roles() == {r.id: None, p.id: "provider_trial"}
    kept = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "accept", "provider": "keep"}
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["acceptance"]["provider"] == "keep"
    grant = await trial_grant(db_session, target)
    assert (grant.role, grant.status, grant.expires_at, grant.authorized_by) == ("maintainer", "active", None, r.id)
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"]
    assert [(s.role, s.expires_at, s.collaboration_id) for s in seats] == [("owner", None, grant.id)]
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == {target}
    assert await provider_access(client, ph, cid) == "maintainer"
    assert await member_roles() == {r.id: None, p.id: "maintainer"}

    base = f"/api/v1/workspaces/{target}/collaboration/{grant.id}"
    # Updating the shared configuration can keep "No expiry"; a new invitation cannot have none.
    unchanged = await client.post(
        f"{base}/update", headers=rh, json={"email": p.email, "role": "maintainer", "days": None, "share_configuration": True}
    )
    assert unchanged.status_code == 200, unchanged.text
    grant = await trial_grant(db_session, target)
    memberships, seats = await derived(db_session, p.id, customer)
    assert (grant.expires_at, grant.share_configuration, [s.expires_at for s in seats]) == (None, True, [None])
    endless = await client.post(
        f"/api/v1/workspaces/{target}/collaboration", headers=rh, json={"email": "guest@example.com", "days": None}
    )
    assert endless.status_code == 422, endless.text
    updated = await client.post(
        f"{base}/update", headers=rh, json={"email": p.email, "role": "maintainer", "days": 30}
    )
    assert updated.status_code == 200, updated.text
    grant = await trial_grant(db_session, target)
    memberships, seats = await derived(db_session, p.id, customer)
    assert grant.expires_at is not None and [s.expires_at for s in seats] == [grant.expires_at]
    # Once it expires, "No expiry" is not a way to extend it indefinitely.
    extended = await client.post(f"{base}/update", headers=rh, json={"email": p.email, "role": "maintainer", "days": None})
    assert extended.status_code == 422, extended.text

    assert (await client.post(f"{base}/revoke", headers=rh)).status_code == 200
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["inactive"] and seats == []
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == frozenset()
    assert await provider_access(client, ph, cid) == "none"


@pytest.mark.asyncio
async def test_every_source_manager_is_offered_the_customer_trial(client, db_session):
    # Catches: the delivery list offering the trial only to the person whose
    # grant it is, offering it to someone who cannot manage the source
    # Workspace, or still offering it after the customer removed the team;
    # the customer's member list not marking the provider team's seats.
    from types import SimpleNamespace

    from packages.core.services.client_delivery import deliveries

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    owner = SimpleNamespace(id="01TEAMMATE0000000000000000", entity_id=p.entity_id, role="owner")
    outsider = SimpleNamespace(id="01OUTSIDER0000000000000000", entity_id=p.entity_id, role="member")

    async def offered(viewer):
        [row] = await deliveries(cid, user=viewer, db=db_session)
        return row["provider_access"], row["can_open_trial"], row["can_manage"]

    assert await offered(p) == await offered(owner) == ("trial", True, True)
    assert await offered(outsider) == ("none", False, False)
    staff = (await client.get(f"/api/v1/workspaces/{target}/staff", headers=rh)).json()
    assert {row["user_id"]: row["collaboration_team"] for row in staff} == {r.id: False, p.id: True}
    kept = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "accept", "provider": "keep"}
    )
    assert kept.status_code == 200, kept.text
    assert await offered(owner) == ("maintainer", True, True)
    grant = await trial_grant(db_session, target)
    assert (
        await client.post(f"/api/v1/workspaces/{target}/collaboration/{grant.id}/revoke", headers=rh)
    ).status_code == 200
    assert await offered(owner) == ("none", False, True)


@pytest.mark.asyncio
async def test_collaborator_inside_the_customer_company_cannot_manage_its_collaboration(client, db_session):
    # Catches: the provider's derived owner seat letting them, working inside
    # the customer Workspace, decide their own trial, revoke others, invite
    # people into the customer's Workspace or be offered its deletion.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    grant = await trial_grant(db_session, target)
    inside = await minted(client, ph, source, delivery["id"])
    # The derived owner seat manages the Workspace but never owns its lifecycle.
    detail = await client.get(f"/api/v1/workspaces/{target}", headers=inside)
    assert detail.status_code == 200, detail.text
    assert (detail.json()["can_manage"], detail.json()["can_delete"]) == (True, False)
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()["can_delete"] is True
    refused = [
        await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=inside),
        await client.post(
            f"/api/v1/workspaces/{target}/collaboration", headers=inside, json={"email": "friend@delivery.test"}
        ),
        await client.post(f"/api/v1/workspaces/{target}/collaboration/{grant.id}/revoke", headers=inside),
        await client.post(
            f"/api/v1/workspaces/{target}/delivery-phase", headers=inside, json={"action": "accept", "provider": "keep"}
        ),
        await client.put(f"/api/v1/workspaces/{target}/collaboration-group", headers=inside, json={"group_id": None}),
    ]
    for response in refused:
        assert response.status_code == 403, response.text
        # The route boundary refuses before the handler's own collaborator check.
        assert response.json()["detail"] == "This page is not available to external collaborators"
    grant = await trial_grant(db_session, target)
    assert (grant.role, grant.status) == ("provider_trial", "active")
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceCollaboration)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["tester", "maintainer"])
async def test_invitation_requires_explicit_acceptance_and_never_creates_staff(client, db_session, role):
    # Catches: an invitation granting access before the guest accepts; a
    # tester gaining any membership or seat; an accepted maintainer not getting
    # exactly one derived collaborator membership and seat, or getting Staff.
    ph, p, rh, r, target, gid = await invited(client, db_session, role=role, accept=False)
    portal = await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=ph)
    assert portal.status_code == 200, portal.text
    assert portal.json()["configuration"] is None
    assert (
        await client.post(
            f"/api/v1/workspace-collaborations/{gid}/entries", headers=ph, json={"kind": "feedback", "body": "test"}
        )
    ).status_code == 404
    assert await derived(db_session, p.id, r.entity_id) == ([], [])
    for _ in range(2):
        assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Staff).where(Staff.entity_id == r.entity_id, Staff.user_id == p.id)
        )
        == 0
    )
    memberships, seats = await derived(db_session, p.id, r.entity_id)
    if role == "tester":
        assert (memberships, seats) == ([], [])
    else:
        assert [(m.role, m.status) for m in memberships] == [("collaborator", "active")]
        assert [(s.workspace_id, s.role, s.collaboration_id) for s in seats] == [(target, "owner", gid)]
    # The provider's home-company sign-in never reaches the customer's Workspace.
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    assert (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=ph)).status_code == 404
    outsider, _ = await register(client, db_session, "outsider")
    assert (await client.get(f"/api/v1/workspace-collaborations/{gid}", headers=outsider)).status_code == 404


@pytest.mark.asyncio
async def test_provider_requests_customer_authorization_without_default_access(client, db_session):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session, phase="delivered")
    # A direct delivery gives the provider no trial access to start from.
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceCollaboration)) == 0
    assert await derived(db_session, p.id, r.entity_id) == ([], [])
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
    # The accepted maintenance request is live access, reported as such.
    assert history["provider_access"] == "maintainer"


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
                        # Grants created by this revision always expire; the
                        # never-expiring relaxation is 20261015_01.
                        nullable = expected.nullable and expected.name != "expires_at"
                        assert columns[expected.name]["nullable"] == nullable
                revision.downgrade()
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA collaboration_migration_test CASCADE"))

    await db_session.run_sync(migrate)


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_provider_trial_migration_relaxes_expiry_and_backfills_idempotently(db_session):
    # Catches: expires_at staying NOT NULL (a never-expiring grant cannot be
    # stored), trial deliveries missing their provider grant, a re-run or
    # upgrade->downgrade->upgrade duplicating it, a removed sender, deleted target
    # or delivery with no accepting customer (no issuer to authorize) getting one,
    # or downgrade leaving it (or its entries) behind, or leaving a derived seat
    # that older code would read as customer-managed.
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from tests.migration_test_helpers import script_directory

    scripts = script_directory()
    base = scripts.get_revision("20261002_client_collab").module
    trial = scripts.get_revision("20261015_01").module

    def count(connection):
        return connection.execute(
            text("SELECT count(*) FROM workspace_collaborations WHERE role = 'provider_trial'")
        ).scalar()

    def expires_nullable(connection):
        columns = {c["name"]: c for c in inspect(connection).get_columns("workspace_collaborations")}
        return columns["expires_at"]["nullable"]

    def migrate(session):
        connection = session.connection()
        connection.execute(text("CREATE SCHEMA provider_trial_migration_test"))
        connection.execute(text("SET LOCAL search_path TO provider_trial_migration_test"))
        connection.execute(text(
            "CREATE TABLE users (id VARCHAR(26) PRIMARY KEY, email VARCHAR(255), status VARCHAR(20),"
            " deleted_at TIMESTAMPTZ)"
        ))
        connection.execute(text("CREATE TABLE workspaces (id VARCHAR(26) PRIMARY KEY, deleted_at TIMESTAMPTZ)"))
        connection.execute(text(
            "CREATE TABLE client_workspace_deliveries (id VARCHAR(26) PRIMARY KEY, target_entity_id VARCHAR(26),"
            " target_workspace_id VARCHAR(26), created_by VARCHAR(26), accepted_by VARCHAR(26),"
            " accepted_at TIMESTAMPTZ)"
        ))
        connection.execute(text(
            "CREATE TABLE workspace_staff (id VARCHAR(26) PRIMARY KEY, workspace_id VARCHAR(26), user_id VARCHAR(26))"
        ))
        connection.execute(text(
            "CREATE TABLE user_memberships (id VARCHAR(26) PRIMARY KEY, user_id VARCHAR(26), entity_id VARCHAR(26),"
            " role VARCHAR(20), status VARCHAR(20))"
        ))
        connection.execute(text("INSERT INTO workspace_staff VALUES ('OWNSEAT', 'WORKSPACE', 'OWNER')"))
        connection.execute(text(
            "INSERT INTO user_memberships VALUES ('EXT', 'PROVIDER', 'CUSTOMER', 'collaborator', 'active'),"
            " ('REAL', 'OWNER', 'CUSTOMER', 'owner', 'active')"
        ))
        with Operations.context(MigrationContext.configure(connection)):
            base.upgrade()
            assert not expires_nullable(connection)
            connection.execute(text(
                "INSERT INTO users (id, email, status, deleted_at) VALUES"
                " ('PROVIDER', 'Provider@Example.com', 'active', NULL),"
                " ('REMOVED', 'gone@example.com', 'active', now()),"
                " ('SUSPENDED', 'paused@example.com', 'suspended', NULL)"
            ))
            connection.execute(text(
                "INSERT INTO workspaces (id, deleted_at) VALUES ('WORKSPACE', NULL), ('WORKSPACE2', NULL),"
                " ('WORKSPACE3', NULL), ('DEADWS', now())"
            ))
            connection.execute(text(
                "INSERT INTO client_workspace_deliveries (id, target_entity_id, target_workspace_id, created_by,"
                " accepted_by, phase) VALUES"
                " ('TRIAL', 'CUSTOMER', 'WORKSPACE', 'PROVIDER', 'OWNER', 'trial'),"
                " ('AWAITING', 'CUSTOMER', 'WORKSPACE2', 'PROVIDER', 'OWNER', 'awaiting_acceptance'),"
                " ('DELIVERED', 'CUSTOMER', 'WORKSPACE3', 'PROVIDER', 'OWNER', 'delivered'),"
                " ('PENDING', NULL, NULL, 'PROVIDER', NULL, 'trial'),"
                " ('REMOVEDSENDER', 'CUSTOMER', 'WORKSPACE', 'REMOVED', 'OWNER', 'trial'),"
                " ('SUSPENDEDSENDER', 'CUSTOMER', 'WORKSPACE', 'SUSPENDED', 'OWNER', 'trial'),"
                " ('DEADTARGET', 'CUSTOMER', 'DEADWS', 'PROVIDER', 'OWNER', 'trial'),"
                " ('UNACCEPTED', 'CUSTOMER', 'WORKSPACE', 'PROVIDER', NULL, 'trial')"
            ))
            # The grant keeps the delivery's acceptance time; AWAITING has none recorded.
            connection.execute(text(
                "UPDATE client_workspace_deliveries SET accepted_at = '2026-10-01T09:00:00Z' WHERE id = 'TRIAL'"
            ))
            for _ in range(2):
                trial.upgrade()
                assert expires_nullable(connection)
                assert count(connection) == 2
                trial.upgrade()  # re-running never duplicates the grant
                assert count(connection) == 2
                assert "collaboration_id" in {c["name"] for c in inspect(connection).get_columns("workspace_staff")}
                connection.execute(text(
                    "INSERT INTO workspace_staff (id, workspace_id, user_id, collaboration_id)"
                    " SELECT 'DERIVED', 'WORKSPACE', 'PROVIDER', id FROM workspace_collaborations"
                    " WHERE delivery_id = 'TRIAL'"
                ))
                connection.execute(text("UPDATE user_memberships SET status = 'active'"))
                granted = {
                    row.delivery_id: row
                    for row in connection.execute(text(
                        "SELECT delivery_id, entity_id, workspace_id, email, status, authorized_by, user_id,"
                        " expires_at, accepted_at, share_configuration, authority_fingerprint,"
                        " id FROM workspace_collaborations WHERE role = 'provider_trial'"
                    ))
                }
                # Only live senders into live target Workspaces, in a trial phase.
                assert set(granted) == {"TRIAL", "AWAITING"}
                grant = granted["TRIAL"]
                assert (grant.entity_id, grant.workspace_id) == ("CUSTOMER", "WORKSPACE")
                assert grant.email == "provider@example.com" and grant.status == "active"
                assert (grant.authorized_by, grant.user_id) == ("OWNER", "PROVIDER")
                assert grant.expires_at is None
                assert grant.accepted_at == datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
                assert granted["AWAITING"].accepted_at is not None
                assert grant.share_configuration is False
                assert all(re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", row.id) for row in granted.values())
                assert grant.authority_fingerprint is None
                assert granted["AWAITING"].workspace_id == "WORKSPACE2"
                # One trial grant per delivery is a database invariant, not a convention.
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text(
                        "INSERT INTO workspace_collaborations (id, entity_id, workspace_id, delivery_id, email,"
                        " role, status) VALUES ('DUP', 'CUSTOMER', 'WORKSPACE', 'TRIAL', 'a@b.co',"
                        " 'provider_trial', 'active')"
                    ))
                connection.execute(text(
                    "INSERT INTO workspace_collaboration_entries (id, entity_id, workspace_id, collaboration_id,"
                    " created_by, kind, body, status) SELECT 'ENTRY', 'CUSTOMER', 'WORKSPACE', id, 'PROVIDER',"
                    " 'note', 'hello', 'submitted' FROM workspace_collaborations WHERE delivery_id = 'TRIAL'"
                ))
                trial.downgrade()
                assert not expires_nullable(connection)
                assert count(connection) == 0
                assert connection.execute(text("SELECT count(*) FROM workspace_collaboration_entries")).scalar() == 0
                assert "collaboration_id" not in {
                    c["name"] for c in inspect(connection).get_columns("workspace_staff")
                }
                assert connection.execute(text("SELECT array_agg(id) FROM workspace_staff")).scalar() == ["OWNSEAT"]
                assert dict(connection.execute(text("SELECT id, status FROM user_memberships")).all()) == {
                    "EXT": "inactive", "REAL": "active"
                }
            # Other never-expiring grants survive a downgrade with a concrete expiry.
            trial.upgrade()
            connection.execute(text(
                "INSERT INTO workspace_collaborations (id, entity_id, workspace_id, email, role, status)"
                " VALUES ('KEPT', 'CUSTOMER', 'WORKSPACE', 'a@b.co', 'maintainer', 'active')"
            ))
            trial.downgrade()
            assert connection.execute(
                text("SELECT expires_at IS NOT NULL FROM workspace_collaborations WHERE id = 'KEPT'")
            ).scalar()
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA provider_trial_migration_test CASCADE"))

    await db_session.run_sync(migrate)


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_team_grant_migration_allows_one_live_grant_per_person_and_downgrades_cleanly(db_session):
    # Catches: two managers of one delivery not both getting a grant; one person
    # holding two live grants; a revoked grant blocking a new one; a duplicate
    # surviving upgrade or taking the person's seat with it; a provider left
    # parked in the customer company; a downgrade leaving a second trial grant
    # (the old index cannot be restored), its seat or its membership behind.
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from tests.migration_test_helpers import script_directory

    team = script_directory().get_revision("20261020_01").module

    def grant(connection, gid, user, role="provider_trial", status="active", day=2):
        at = datetime(2026, 10, day, tzinfo=timezone.utc)
        connection.execute(text(
            "INSERT INTO workspace_collaborations (id, entity_id, workspace_id, delivery_id, email, role, status,"
            " user_id, created_at) VALUES (:id, 'CUSTOMER', 'WS', 'D', 'p@x.co', :role, :status, :user, :at)"
        ), {"id": gid, "role": role, "status": status, "user": user, "at": at})

    def rows(connection, sql):
        return dict(connection.execute(text(sql)).all())

    def migrate(session):
        connection = session.connection()
        connection.execute(text("CREATE SCHEMA team_grant_migration_test"))
        connection.execute(text("SET LOCAL search_path TO team_grant_migration_test"))
        connection.execute(text("CREATE TABLE users (id VARCHAR(26) PRIMARY KEY, entity_id VARCHAR(26), role VARCHAR(20))"))
        connection.execute(text(
            "CREATE TABLE user_memberships (id VARCHAR(26) PRIMARY KEY, user_id VARCHAR(26), entity_id VARCHAR(26),"
            " role VARCHAR(20), status VARCHAR(20), deleted_at TIMESTAMPTZ, is_primary BOOLEAN DEFAULT false,"
            " created_at TIMESTAMPTZ DEFAULT now())"
        ))
        connection.execute(text(
            "CREATE TABLE workspace_collaborations (id VARCHAR(26) PRIMARY KEY, entity_id VARCHAR(26),"
            " workspace_id VARCHAR(26), delivery_id VARCHAR(26), email VARCHAR(255), role VARCHAR(20),"
            " status VARCHAR(20), user_id VARCHAR(26), expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(),"
            " snapshot JSONB DEFAULT '{}'::jsonb)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX ux_workspace_collaboration_provider_trial_delivery"
            " ON workspace_collaborations (delivery_id) WHERE role = 'provider_trial'"
        ))
        connection.execute(text(
            "CREATE TABLE workspace_staff (id VARCHAR(26) PRIMARY KEY, workspace_id VARCHAR(26), user_id VARCHAR(26),"
            " collaboration_id VARCHAR(26), expires_at TIMESTAMPTZ)"
        ))
        connection.execute(text(
            "CREATE TABLE workspace_collaboration_entries (id VARCHAR(26) PRIMARY KEY, collaboration_id VARCHAR(26))"
        ))
        # P1 kept as maintainer and also accepted a maintenance request: two live grants.
        # Their seat follows the later one; they were switched into the customer company.
        grant(connection, "G1", "P1", day=1)
        grant(connection, "G1DUP", "P1", role="maintainer")
        connection.execute(text("INSERT INTO workspace_staff VALUES ('SEAT1', 'WS', 'P1', 'G1DUP', NULL)"))
        connection.execute(text(
            "INSERT INTO users VALUES ('P1', 'CUSTOMER', 'collaborator'), ('P2', 'HOME', 'owner')"
        ))
        connection.execute(text(
            "INSERT INTO user_memberships (id, user_id, entity_id, role, status, is_primary) VALUES"
            " ('M1', 'P1', 'CUSTOMER', 'collaborator', 'active', false),"
            " ('H1', 'P1', 'HOME', 'Admin', 'active', true),"
            " ('M2', 'P2', 'CUSTOMER', 'collaborator', 'active', false)"
        ))
        with Operations.context(MigrationContext.configure(connection)):
            for _ in range(2):
                team.upgrade()
                assert rows(connection, "SELECT id, status FROM workspace_collaborations") == {
                    "G1": "active", "G1DUP": "revoked"
                }
                assert rows(connection, "SELECT id, collaboration_id FROM workspace_staff") == {"SEAT1": "G1"}
                assert connection.execute(text("SELECT entity_id, role FROM users WHERE id = 'P1'")).one() == (
                    "HOME", "admin"
                )
                # A second manager joins; the same person cannot hold two live grants.
                grant(connection, "G2", "P2", day=3)
                with pytest.raises(IntegrityError), connection.begin_nested():
                    grant(connection, "G1AGAIN", "P1", role="maintainer")
                # A revoked grant never blocks the next one.
                connection.execute(text("UPDATE workspace_collaborations SET status = 'revoked' WHERE id = 'G2'"))
                grant(connection, "G2NEW", "P2", day=4)
                connection.execute(text("INSERT INTO workspace_staff VALUES ('SEAT2', 'WS', 'P2', 'G2NEW', NULL)"))
                connection.execute(text("INSERT INTO workspace_collaboration_entries VALUES ('E2', 'G2NEW')"))
                # An older revoked trial grant is not the one the downgrade keeps.
                connection.execute(text(
                    "INSERT INTO workspace_collaborations (id, entity_id, workspace_id, delivery_id, email, role,"
                    " status, user_id, created_at) VALUES ('G0', 'CUSTOMER', 'WS', 'D', 'p@x.co', 'provider_trial',"
                    " 'revoked', 'P0', '2026-09-01T00:00:00Z')"
                ))

                team.downgrade()
                assert rows(connection, "SELECT id, role FROM workspace_collaborations") == {
                    "G1": "provider_trial", "G1DUP": "maintainer"
                }
                assert rows(connection, "SELECT id, collaboration_id FROM workspace_staff") == {"SEAT1": "G1"}
                assert connection.execute(text("SELECT count(*) FROM workspace_collaboration_entries")).scalar() == 0
                assert rows(connection, "SELECT id, status FROM user_memberships") == {
                    "M1": "active", "H1": "active", "M2": "inactive"
                }
                with pytest.raises(IntegrityError), connection.begin_nested():
                    grant(connection, "G3", "P3")
                connection.execute(text("UPDATE user_memberships SET status = 'active'"))
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA team_grant_migration_test CASCADE"))

    await db_session.run_sync(migrate)


def test_never_expiring_grant_reports_its_real_status():
    # Catches: a NULL expiry crashing public_grant or being reported as expired.
    from packages.core.services.external_collaboration_access import grant_expired
    from packages.core.services.workspace_collaboration import public_grant

    now = datetime.now(timezone.utc)
    grant = WorkspaceCollaboration(
        id="GRANT", entity_id="E", workspace_id="W", email="a@b.co", role="provider_trial", status="active",
        expires_at=None,
    )
    assert not grant_expired(grant) and public_grant(grant)["status"] == "active"
    grant.expires_at = now - timedelta(seconds=1)
    assert grant_expired(grant) and public_grant(grant)["status"] == "expired"
    grant.status = "revoked"
    assert public_grant(grant)["status"] == "revoked"
    grant.status, grant.expires_at = "active", now + timedelta(days=1)
    assert not grant_expired(grant)
    assert grant_expired(grant, at=now + timedelta(days=2))


@pytest.mark.asyncio
async def test_one_guests_concurrent_maintainer_acceptances_both_derive_access(client, db_session):
    # Catches: a guest accepting two invitations at once deadlocking, as each
    # accept held SHARE on the guest's User row and upgraded it to sync access.
    import asyncio

    ph, p, rh, r, first, gid = await invited(client, db_session, accept=False)
    created = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Second", "kind": "project", "primary_work": "More"}
    )
    assert created.status_code == 201, created.text
    second = created.json()["id"]
    invitation = await client.post(
        f"/api/v1/workspaces/{second}/collaboration", headers=rh, json={"email": p.email, "role": "maintainer"}
    )
    assert invitation.status_code == 201, invitation.text
    results = await asyncio.gather(*[
        client.post(f"/api/v1/workspace-collaborations/{grant_id}/accept", headers=ph)
        for grant_id in (gid, invitation.json()["id"])
    ])
    assert [response.status_code for response in results] == [200, 200], [response.text for response in results]
    memberships, seats = await derived(db_session, p.id, r.entity_id)
    assert [m.status for m in memberships] == ["active"]
    assert sorted(s.workspace_id for s in seats) == sorted([first, second])


@pytest.mark.asyncio
async def test_issuer_creating_another_workspace_keeps_the_trial_grant_live(client, db_session):
    # Catches: an incidental write to the issuer's membership or seat (the
    # Staff link backfilled on their first Workspace creation) changing the
    # issuer fingerprint and silently voiding every grant they issued.
    from packages.core.services.external_collaboration_access import collaborator_workspace_ids

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == {target}
    created = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Unrelated", "kind": "project", "primary_work": "Other"}
    )
    assert created.status_code == 201, created.text
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == {target}


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


async def access_grant(db, user, issuer, target):
    from packages.core.services.workspace_collaboration import issuer_fingerprint

    ws = await db.get(Workspace, target)
    row = WorkspaceCollaboration(
        entity_id=ws.entity_id, workspace_id=target, email=user.email.lower(), role="maintainer",
        status="active", authorized_by=issuer.id, user_id=user.id, expires_at=None,
        accepted_at=datetime.now(timezone.utc),
        authority_fingerprint=await issuer_fingerprint(db, ws, issuer.id),
    )
    db.add(row)
    await db.commit()
    return row


async def derived(db, user_id, entity_id):
    memberships = (
        await db.scalars(
            select(UserMembership)
            .where(UserMembership.user_id == user_id, UserMembership.entity_id == entity_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    seats = (
        await db.scalars(
            select(WorkspaceStaff)
            .join(Workspace, Workspace.id == WorkspaceStaff.workspace_id)
            .where(WorkspaceStaff.user_id == user_id, Workspace.entity_id == entity_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return memberships, seats


@pytest.mark.asyncio
async def test_provider_trial_grant_derives_one_collaborator_membership_and_owner_seat_until_revoked(
    client, db_session
):
    # Catches: the post-deploy reconcile script's dry run writing, or reconcile
    # leaving a backfilled grant unusable or duplicating derived rows; a seat on any other customer Workspace, a non-owner seat or
    # a primary membership; the provider shown as the Workspace creator; a revoke
    # leaving the seat/membership live or the provider parked in the customer
    # company (or skipping a public-signup "external" home); the customer's
    # removal route not re-deriving; re-grant not reviving access; the seat not
    # passing to another live grant; the request-time lookup ignoring a revoke
    # or the issuing owner losing authority.
    from packages.core.services.external_collaboration_access import (
        collaborator_workspace_ids,
        sync_external_access,
    )
    from packages.core.services.workspace_collaboration import issuer_fingerprint
    from scripts.reconcile_provider_trial_access import run as reconcile_script

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    ws = await db_session.get(Workspace, target)
    customer, home = ws.entity_id, p.entity_id
    other = Workspace(entity_id=customer, name="Unrelated customer Workspace")
    db_session.add(other)
    await db_session.commit()
    grant = await trial_grant(db_session, target)
    grant.authority_fingerprint = None  # as the backfill migration leaves it
    await db_session.commit()
    customer_seat = await db_session.scalar(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target, WorkspaceStaff.user_id == r.id)
    )
    before = (customer_seat.id, customer_seat.role, customer_seat.status, customer_seat.collaboration_id)

    dry = await reconcile_script(db_session, dry_run=True)
    assert dry["missing_fingerprint"] >= 1 and dry["reconciled"] == 0
    await db_session.refresh(grant)
    assert grant.authority_fingerprint is None
    for _ in range(2):
        assert (await reconcile_script(db_session, dry_run=False))["reconciled"] >= 1
        await sync_external_access(db_session, grant)
        await db_session.commit()
        memberships, seats = await derived(db_session, p.id, customer)
        assert [(m.role, m.status, m.is_primary) for m in memberships] == [("collaborator", "active", False)]
        assert [(s.workspace_id, s.role, s.status, s.staff_id, s.expires_at, s.collaboration_id) for s in seats] == [
            (target, "owner", "active", None, None, grant.id)
        ]
    assert grant.authority_fingerprint == await issuer_fingerprint(db_session, ws, r.id)
    await db_session.refresh(customer_seat)
    assert (customer_seat.id, customer_seat.role, customer_seat.status, customer_seat.collaboration_id) == before
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == {target}

    # With no recorded creator and no customer owner seat, the derived owner
    # seat must still never be presented as the Workspace's creator.
    ws.settings = {k: v for k, v in (ws.settings or {}).items() if k != "created_by_user_id"}
    customer_seat.role = "editor"
    await db_session.commit()
    detail = await client.get(f"/api/v1/workspaces/{target}", headers=rh)
    assert detail.status_code == 200, detail.text
    assert detail.json()["created_by_user_id"] == r.id

    # The provider had switched into the customer company; removal at acceptance
    # sends them home, even when that home is a public-signup "external" membership.
    home_membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == home)
    )
    home_membership.role = "external"
    p.entity_id, p.role = customer, "collaborator"
    await db_session.commit()
    removed = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "accept", "provider": "remove"}
    )
    assert removed.status_code == 200, removed.text
    memberships, seats = await derived(db_session, p.id, customer)
    assert [(m.role, m.status) for m in memberships] == [("collaborator", "inactive")] and seats == []
    await db_session.refresh(p)
    assert (p.entity_id, p.role) == (home, "external")
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == frozenset()

    await db_session.refresh(grant)
    grant.status = "active"
    await sync_external_access(db_session, grant)
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.collaboration_id for s in seats] == [grant.id]
    successor = await access_grant(db_session, p, r, target)
    grant.status = "revoked"
    await sync_external_access(db_session, grant)
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.collaboration_id for s in seats] == [successor.id]
    issuer = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == r.id, UserMembership.entity_id == customer)
    )
    issuer.status = "inactive"
    await db_session.commit()
    assert await collaborator_workspace_ids(db_session, user_id=p.id, entity_id=customer) == frozenset()


@pytest.mark.asyncio
async def test_collaboration_grant_never_rewrites_a_real_members_membership_or_seat(client, db_session):
    # Catches: a grant downgrading a genuine employee to `external`, replacing
    # their customer-managed seat with an owner seat, deleting it on revoke, or
    # moving their company switch away from the customer.
    from packages.core.services.external_collaboration_access import sync_external_access

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    mh, m = await register(client, db_session, "employee")
    db_session.add(UserMembership(user_id=m.id, entity_id=customer, role="member", status="active"))
    db_session.add(WorkspaceStaff(workspace_id=target, user_id=m.id, role="editor", added_by=r.id, status="active"))
    m.entity_id, m.role = customer, "member"
    await db_session.commit()
    grant = await access_grant(db_session, m, r, target)

    for status in ("active", "revoked"):
        grant.status = status
        await sync_external_access(db_session, grant)
        await db_session.commit()
        memberships, seats = await derived(db_session, m.id, customer)
        assert [(x.role, x.status) for x in memberships] == [("member", "active")]
        assert [(s.role, s.status, s.collaboration_id) for s in seats] == [("editor", "active", None)]
        await db_session.refresh(m)
        assert m.entity_id == customer


async def trialing(client, db):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db)
    grant = await trial_grant(db, target)
    return p, r, target, (await db.get(Workspace, target)).entity_id, grant


@pytest.mark.asyncio
async def test_customer_team_invite_takes_over_a_derived_seat(client, db_session):
    # Catches: a team invite inheriting the derived owner role, or leaving the
    # seat marked as derived so the trial's revoke deletes a customer decision.
    from packages.core.services.external_collaboration_access import sync_external_access
    from packages.core.services.team_invite_service import accept_team_invite_for_user

    p, r, target, customer, grant = await trialing(client, db_session)
    db_session.add(Staff(
        entity_id=customer, name="Provider", email=p.email, status="invited",
        meta={"invite_token": "team-invite-takeover", "workspace_ids": [target]},
    ))
    await db_session.commit()
    await accept_team_invite_for_user(db_session, token="team-invite-takeover", user=p)
    await db_session.commit()
    grant.status = "revoked"
    await sync_external_access(db_session, grant)
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [(m.role, m.status) for m in memberships] == [("member", "active")]
    assert [(s.role, s.status, s.collaboration_id) for s in seats] == [("viewer", "active", None)]


@pytest.mark.asyncio
async def test_deleting_the_provider_clears_derived_access(client, db_session):
    # Catches: account deletion revoking the grant but leaving the owner seat
    # and an active collaborator membership behind, or the delivery list still
    # reporting the deleted provider's trial access.
    from types import SimpleNamespace

    from packages.core.services.client_delivery import deliveries
    from packages.core.services.user_lifecycle import soft_delete_user

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home = (await db_session.get(Workspace, target)).entity_id, p.entity_id
    await soft_delete_user(db_session, p.id)
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [(m.role, m.status) for m in memberships] == [("collaborator", "inactive")] and seats == []
    # The deleted sender can no longer sign in; read the list as their former company.
    viewer = SimpleNamespace(id=p.id, entity_id=home, role="owner")
    assert [row["provider_access"] for row in await deliveries(cid, user=viewer, db=db_session)] == ["none"]


@pytest.mark.asyncio
async def test_provider_pays_only_for_its_own_trial_workspace_until_delivered(client, db_session, monkeypatch):
    # Catches: the payer following anything but the accepted delivery of this
    # exact (customer, Workspace); a trial sponsoring the customer's other
    # Workspaces or a foreign entity naming the trial Workspace; the switch
    # being ignored; delivered still billing the provider; a pin made during the
    # trial failing validation after the phase moves on; an unrelated payer
    # passing validation.
    from packages.core.models.client_delivery import ClientWorkspaceDelivery
    from packages.core.models.user import Entity
    from packages.core.services.billing_payer import (
        TRIAL_PAYER_ENV,
        resolve_billing_payer,
        sponsored_trial_workspaces,
        validate_payer,
    )

    monkeypatch.setenv(TRIAL_PAYER_ENV, "1")
    ph, p, rh, r, cid, source, target, receipt, token = await accepted(client, db_session)
    ws = await db_session.get(Workspace, target)
    customer, provider = ws.entity_id, p.entity_id
    delivery = await db_session.scalar(
        select(ClientWorkspaceDelivery).where(ClientWorkspaceDelivery.target_workspace_id == target)
    )
    other = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Own work", "kind": "campaign", "primary_work": "Own"}
    )
    assert other.status_code == 201, other.text
    foreign = "0" * 26

    async def payer(entity_id=customer, workspace_id=target):
        found = await resolve_billing_payer(db_session, entity_id=entity_id, workspace_id=workspace_id)
        return found.entity_id, found.sponsored, found.delivery_id

    label = f"{(await db_session.get(Entity, customer)).name} / {ws.name}"
    assert await payer() == (provider, True, delivery.id)
    assert await sponsored_trial_workspaces(db_session, provider) == {target: label}
    assert await payer(workspace_id=other.json()["id"]) == (customer, False, None)
    assert await payer(workspace_id=None) == (customer, False, None)
    assert await payer(entity_id=foreign) == (foreign, False, None)
    # An unrelated payer, and the provider paying for an entity it never delivered to.
    for entity_id, payer_id in ((customer, foreign), (foreign, provider)):
        with pytest.raises(ValueError):
            await validate_payer(db_session, entity_id=entity_id, workspace_id=target, payer_entity_id=payer_id)
    monkeypatch.setenv(TRIAL_PAYER_ENV, "0")
    assert await payer() == (customer, False, None)
    monkeypatch.setenv(TRIAL_PAYER_ENV, "1")

    requested = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "request_acceptance"}
    )
    assert requested.json()["phase"] == "awaiting_acceptance"
    db_session.expire_all()
    assert await payer() == (provider, True, delivery.id)
    kept = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "accept", "provider": "keep"}
    )
    assert kept.status_code == 200, kept.text
    db_session.expire_all()
    assert await payer() == (customer, False, None)
    assert await sponsored_trial_workspaces(db_session, provider) == {}
    await validate_payer(db_session, entity_id=customer, workspace_id=target, payer_entity_id=provider)
    await validate_payer(db_session, entity_id=customer, workspace_id=target, payer_entity_id=customer)


@pytest.mark.asyncio
async def test_deleted_trial_provider_is_not_replaced_by_the_customer_as_payer(client, db_session, monkeypatch):
    # Catches: a trial whose provider company was deleted silently billing the
    # customer, who was told the provider pays.
    from packages.core.models.user import Entity
    from packages.core.services.billing_payer import TRIAL_PAYER_ENV, TrialSponsorUnavailable, resolve_billing_payer

    monkeypatch.setenv(TRIAL_PAYER_ENV, "1")
    p, r, target, customer, grant = await trialing(client, db_session)
    (await db_session.get(Entity, p.entity_id)).deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    with pytest.raises(TrialSponsorUnavailable):
        await resolve_billing_payer(db_session, entity_id=customer, workspace_id=target)


async def test_listing_all_deliveries_stays_inside_the_company_and_matches_the_client_list(client, db_session):
    # Catches: the cross-client list leaking another company's deliveries, or
    # returning a row the per-client list would not (the same fields, nothing more).
    ph, _, _, _, cid, _, receipt, _ = await prepared(client, db_session)
    other_ph, _, _, _, _, _, other_receipt, _ = await prepared(client, db_session, suffix="-other")

    rows = (await client.get("/api/v1/client-deliveries", headers=ph)).json()
    assert [row["id"] for row in rows] == [receipt["id"]]
    per_client = (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()
    assert rows == [{**per_client[0], "client_name": "Customer"}]
    other_rows = (await client.get("/api/v1/client-deliveries", headers=other_ph)).json()
    assert [row["id"] for row in other_rows] == [other_receipt["id"]]


async def test_listing_all_deliveries_covers_only_workspaces_the_person_manages(client, db_session):
    # Catches: an ordinary member reading the company's client list here, and a
    # Workspace owner seeing deliveries of Workspaces they don't manage.
    ph, p, _, _, cid, source, receipt, _ = await prepared(client, db_session)
    member = User(id=generate_ulid(), entity_id=p.entity_id, email=f"u-{generate_ulid()}@example.com",
                  password_hash="x", role="member", status="active")
    owner = User(id=generate_ulid(), entity_id=p.entity_id, email=f"u-{generate_ulid()}@example.com",
                 password_hash="x", role="member", status="active")
    db_session.add_all([
        member, owner,
        UserMembership(user_id=member.id, entity_id=p.entity_id, role="member", status="active"),
        UserMembership(user_id=owner.id, entity_id=p.entity_id, role="member", status="active"),
    ])
    other_ws = await client.post(
        "/api/v1/workspaces", headers=ph, json={"name": "Second", "kind": "campaign", "primary_work": "More"},
    )
    contact = (await client.get(f"/api/v1/clients/{cid}/contacts", headers=ph)).json()[0]
    second = (await client.post(
        f"/api/v1/clients/{cid}/deliveries", headers=ph,
        json={"workspace_id": other_ws.json()["id"], "contact_id": contact["id"]},
    )).json()
    db_session.add(WorkspaceStaff(workspace_id=other_ws.json()["id"], user_id=owner.id, role="owner", status="active"))
    await db_session.commit()

    async def listed(user):
        headers = {"Authorization": f"Bearer {create_access_token(user.id, user.entity_id, 'member')}"}
        response = await client.get("/api/v1/client-deliveries", headers=headers)
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()]

    assert await listed(member) == []
    assert await listed(owner) == [second["id"]]
    both = (await client.get("/api/v1/client-deliveries", headers=ph)).json()
    assert [row["id"] for row in both] == [second["id"], receipt["id"]]
    assert {row["source_workspace_id"] for row in both} == {source, other_ws.json()["id"]}


@pytest.mark.parametrize("change", ["revoked", "contact_removed", "contact_email", "client_inactive", "client_deleted"])
async def test_listing_all_deliveries_leaves_out_invitations_that_read_as_revoked(client, db_session, change):
    # Catches: this list disagreeing with the per-client one, which shows a
    # pending invitation as revoked once its client or contact changed.
    ph, _, _, _, cid, _, receipt, _ = await prepared(client, db_session)
    row = await db_session.get(ClientWorkspaceDelivery, receipt["id"])
    contact = await db_session.get(ClientContact, row.contact_id)
    from packages.core.models.people import Client

    customer = await db_session.get(Client, cid)
    if change == "revoked":
        row.status = "revoked"
    elif change == "contact_removed":
        contact.deleted_at = datetime.now(timezone.utc)
    elif change == "contact_email":
        contact.email = "someone-else@delivery.test"
    elif change == "client_inactive":
        customer.status = "inactive"
    else:
        customer.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert (await client.get("/api/v1/client-deliveries", headers=ph)).json() == []


async def test_listing_all_deliveries_keeps_an_expired_invitation_and_an_accepted_delivery(client, db_session):
    # Catches: only pending invitations surviving the live filter, or an accepted
    # delivery dropping out when its contact later changes.
    ph, _, rh, _, _, _, target, delivery, _ = await accepted(client, db_session)
    expired_ph, _, _, _, _, _, invitation, _ = await prepared(client, db_session, suffix="-late")
    late = await db_session.get(ClientWorkspaceDelivery, invitation["id"])
    late.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    held = await db_session.get(ClientWorkspaceDelivery, delivery["id"])
    (await db_session.get(ClientContact, held.contact_id)).email = "someone-else@delivery.test"
    await db_session.commit()

    assert [row["status"] for row in (await client.get("/api/v1/client-deliveries", headers=expired_ph)).json()] == ["expired"]
    rows = (await client.get("/api/v1/client-deliveries", headers=ph)).json()
    assert [(row["id"], row["status"], row["provider_access"], row["can_open_trial"]) for row in rows] == [
        (delivery["id"], "delivered", "trial", True),
    ]
    assert rows[0]["target_workspace_id"] == target
