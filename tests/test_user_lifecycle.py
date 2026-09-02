"""E2E tests: user account soft-delete / restore / sole-admin cascade /
hard-purge anonymization.

Stripe + OAuth revocation are integration concerns — covered by mocks
in unit tests at the service layer rather than re-asserting them here.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update as sa_update


async def _register(
    client: AsyncClient,
    *,
    username: str,
    entity_name: str = "Lifecycle Co",
) -> tuple[str, dict, dict]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": entity_name,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["access_token"], {"Authorization": f"Bearer {body['access_token']}"}, body


@pytest.mark.asyncio
async def test_self_delete_soft_deletes_user(client: AsyncClient):
    """DELETE /auth/me marks deleted_at; subsequent /auth/me with the
    old JWT returns 401 (user_not_found)."""
    _, headers, body = await _register(client, username="lifecycle_self")

    delete = await client.delete("/api/v1/auth/me", headers=headers)
    assert delete.status_code == 200, delete.text
    summary = delete.json()
    assert summary["user_id"] == body["user_id"]
    assert summary["grace_days"] >= 1

    # Old JWT should now be rejected (user_not_found because get_user_by_id
    # filters deleted_at)
    me_after = await client.get("/api/v1/auth/me", headers=headers)
    assert me_after.status_code == 401


@pytest.mark.asyncio
async def test_login_offers_restore_for_soft_deleted(client: AsyncClient):
    """A correct password on a soft-deleted account returns
    ``{requires_restore: true}`` instead of a JWT."""
    _, headers, _ = await _register(client, username="lifecycle_login_restore")
    await client.delete("/api/v1/auth/me", headers=headers)

    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "lifecycle_login_restore@test.com",
            "password": "pass123",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("requires_restore") is True
    assert body["email"] == "lifecycle_login_restore@test.com"
    assert body["grace_days"] >= 1


@pytest.mark.asyncio
async def test_restore_endpoint_brings_account_back(client: AsyncClient):
    """POST /auth/me/restore with email + password un-deletes and
    issues a fresh JWT."""
    _, headers, _ = await _register(client, username="lifecycle_restore_user")
    await client.delete("/api/v1/auth/me", headers=headers)

    restore = await client.post(
        "/api/v1/auth/me/restore",
        json={
            "email": "lifecycle_restore_user@test.com",
            "password": "pass123",
        },
    )
    assert restore.status_code == 200, restore.text
    new_token = restore.json()["access_token"]
    assert new_token

    # /auth/me with the new token works
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_restore_404_when_not_deleted(client: AsyncClient):
    """Restoring an account that isn't in trash returns 404."""
    await _register(client, username="lifecycle_not_deleted")
    resp = await client.post(
        "/api/v1/auth/me/restore",
        json={
            "email": "lifecycle_not_deleted@test.com",
            "password": "pass123",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_restore_wrong_password_401(client: AsyncClient):
    _, headers, _ = await _register(client, username="lifecycle_wrong_pw")
    await client.delete("/api/v1/auth/me", headers=headers)

    resp = await client.post(
        "/api/v1/auth/me/restore",
        json={
            "email": "lifecycle_wrong_pw@test.com",
            "password": "wrongpassword",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_sole_admin_cascades_to_entity(client: AsyncClient, db_session):
    """When the deleted user is the sole owner/admin of their entity,
    soft-delete cascades to the entity + its workspaces."""
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace

    _, headers, body = await _register(client, username="lifecycle_sole_admin")

    # Create a workspace under this entity
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "OnlyOne"},
    )
    ws_id = ws_resp.json()["id"]

    delete = await client.delete("/api/v1/auth/me", headers=headers)
    assert delete.status_code == 200
    assert delete.json()["entity_cascaded"] is True

    # Entity + workspace should both have deleted_at set
    entity = (await db_session.execute(select(Entity).where(Entity.id == body["entity_id"]))).scalar_one()
    assert entity.deleted_at is not None

    ws = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert ws.deleted_at is not None


@pytest.mark.asyncio
async def test_non_sole_admin_does_not_cascade(client: AsyncClient, db_session):
    """If the deleted user has an admin peer, the entity stays
    active."""
    from packages.core.models.user import Entity, User
    from packages.core.services.auth_service import hash_password

    _, headers, body = await _register(
        client,
        username="lifecycle_admin_a",
        entity_name="Multi Admin Co",
    )
    entity_id = body["entity_id"]

    # Add a second admin directly via DB (no admin invite flow set up
    # in the test harness).
    peer = User(
        entity_id=entity_id,
        email="lifecycle_peer@test.com",
        display_name="Peer Admin",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    db_session.add(peer)
    # Commit so the API request below (which runs in a separate
    # session) sees the peer when computing sole-admin status.
    await db_session.commit()

    delete = await client.delete("/api/v1/auth/me", headers=headers)
    assert delete.status_code == 200
    assert delete.json()["entity_cascaded"] is False

    entity = (await db_session.execute(select(Entity).where(Entity.id == entity_id))).scalar_one()
    assert entity.deleted_at is None


@pytest.mark.asyncio
async def test_admin_in_another_active_entity_still_prevents_cascade(db_session):
    """The active-entity pointer must not hide an admin membership elsewhere."""
    from packages.core.models.base import generate_ulid
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.services.auth_service import hash_password
    from packages.core.services.user_lifecycle import soft_delete_user

    target_entity = Entity(id=generate_ulid(), name="Target Entity")
    active_entity = Entity(id=generate_ulid(), name="Active Entity")
    deleting_owner = User(
        entity_id=target_entity.id,
        email="lifecycle-target-owner@test.com",
        display_name="Deleting Owner",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
    )
    peer = User(
        entity_id=active_entity.id,
        email="lifecycle-switched-admin@test.com",
        display_name="Switched Admin",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    admin_role = StaffRole(
        entity_id=target_entity.id,
        name="admin",
        permissions=[],
        status="active",
    )
    db_session.add_all(
        [target_entity, active_entity, deleting_owner, peer, admin_role]
    )
    await db_session.flush()
    peer_staff = Staff(
        entity_id=target_entity.id,
        kind="employee",
        name=peer.display_name,
        email=peer.email,
        user_id=peer.id,
        role_id=admin_role.id,
        status="active",
    )
    db_session.add(peer_staff)
    await db_session.flush()
    db_session.add(
        UserMembership(
            id=generate_ulid(),
            user_id=peer.id,
            entity_id=target_entity.id,
            role="admin",
            status="active",
            staff_id=peer_staff.id,
        )
    )
    await db_session.flush()

    result = await soft_delete_user(db_session, deleting_owner.id)

    assert result["entity_cascaded"] is False
    assert target_entity.deleted_at is None


@pytest.mark.asyncio
async def test_purge_after_grace_window_anonymizes(client: AsyncClient, db_session):
    """Hard-purge: the user row is gone, but their tasks remain
    with `created_by` rewritten to the deleted-user sentinel."""
    from packages.core.models.base import generate_ulid
    from packages.core.models.chat_feedback import ChatMessageFeedback
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace
    from packages.core.services.user_lifecycle import (
        list_users_due_for_purge,
        purge_user,
        soft_delete_user,
    )

    _, headers, body = await _register(client, username="lifecycle_purge")
    user_id = body["user_id"]
    entity_id = body["entity_id"]

    # Create a task attributed to this user via creator_id (the
    # actual FK-shaped column on Task; TaskLog has the String(100)
    # ``created_by`` field that the sentinel rewrite covers).
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Feedback workspace",
    )
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Audit task",
        workspace_id=workspace.id,
        status="completed",
        creator_id=user_id,
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace.id,
        channel="web",
        scope="workspace_main",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Private feedback preview",
        author_kind="agent",
        message_kind="agent_update",
        refs=[{"type": "task", "id": task.id}],
        meta={"feedback_target_kind": "task_completion"},
    )
    db_session.add_all([workspace, task, conversation, message])
    await db_session.flush()
    feedback = ChatMessageFeedback(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation.id,
        message_id=message.id,
        target_kind="task_completion",
        target_id=task.id,
        task_id=task.id,
        rating="down",
        content_preview="Private feedback preview",
        mutation_sequence=1,
    )
    feedback_evidence = RuntimeEvidence(
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=user_id,
        conversation_id=conversation.id,
        message_id=message.id,
        task_id=task.id,
        evidence_type="task_completion_feedback",
        source="workspace_chat",
        status="succeeded",
        summary="Private task feedback evidence",
        details={
            "rating": "down",
            "target_kind": "task_completion",
            "target_id": task.id,
            "task_id": task.id,
            "plan_id": None,
        },
        metrics={"helpful": 0},
    )
    db_session.add_all([feedback, feedback_evidence])
    await db_session.commit()
    task_id = task.id
    feedback_id = feedback.id
    feedback_evidence_id = feedback_evidence.id
    message_id = message.id

    await soft_delete_user(db_session, user_id)
    # Backdate deleted_at past grace window
    await db_session.execute(
        sa_update(User).where(User.id == user_id).values(deleted_at=datetime.now(timezone.utc) - timedelta(days=45))
    )
    await db_session.flush()

    due = await list_users_due_for_purge(db_session, grace_days=30)
    assert any(u.id == user_id for u in due)

    purged = await purge_user(db_session, user_id)
    assert purged is True

    # User row gone
    after = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    assert after is None

    # Task survives, FK-shaped attribution NULLed (creator_id is a
    # String(26) FK, so the anonymization path NULLs it rather than
    # rewriting to the sentinel).
    surviving = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert surviving.creator_id is None
    assert await db_session.get(ChatMessageFeedback, feedback_id) is None
    assert await db_session.get(RuntimeEvidence, feedback_evidence_id) is None
    surviving_message = await db_session.get(Message, message_id)
    assert surviving_message is not None
    assert surviving_message.meta["feedback_target_kind"] == "task_completion"


@pytest.mark.asyncio
async def test_purge_detaches_live_automation_owner_and_prevents_regrowth(
    client: AsyncClient,
    db_session,
):
    from packages.core.ledger.adapters import record_automation_run_finished
    from packages.core.models.base import generate_ulid
    from packages.core.models.product_growth import ProductGrowthEvent
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace, WorkspaceActivity
    from packages.core.services.auth_service import hash_password
    from packages.core.services.scheduler_service import create_scheduled_job
    from packages.core.services.user_lifecycle import purge_user, soft_delete_user

    _, _, body = await _register(
        client,
        username="lifecycle_automation_owner",
        entity_name="Automation Privacy Co",
    )
    user_id = body["user_id"]
    entity_id = body["entity_id"]
    peer = User(
        entity_id=entity_id,
        email=f"automation-peer-{generate_ulid()}@test.com",
        display_name="Automation Peer",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Automation privacy workspace",
    )
    db_session.add_all([peer, workspace])
    await db_session.flush()
    activity = WorkspaceActivity(
        workspace_id=workspace.id,
        entity_id=entity_id,
        event_type="automation_created",
        summary="Created by a soon-to-be deleted user",
        user_id=user_id,
    )
    db_session.add(activity)
    job = await create_scheduled_job(
        db_session,
        entity_id,
        f"privacy-owner:{generate_ulid()}",
        "Privacy owner",
        workspace_id=workspace.id,
        user_id=user_id,
    )
    await db_session.commit()
    job_id = job.id
    activity_id = activity.id
    assert (await db_session.execute(
        select(ProductGrowthEvent).where(ProductGrowthEvent.user_id == user_id)
    )).scalars().all()

    await soft_delete_user(db_session, user_id)
    await db_session.execute(
        sa_update(User)
        .where(User.id == user_id)
        .values(
            deleted_at=datetime.now(timezone.utc) - timedelta(days=45)
        )
    )
    await db_session.flush()
    assert await purge_user(db_session, user_id) is True
    await db_session.commit()

    db_session.expire_all()
    surviving_job = await db_session.get(ScheduledJob, job_id)
    surviving_activity = await db_session.get(WorkspaceActivity, activity_id)
    assert surviving_job is not None and surviving_job.user_id is None
    assert surviving_activity is not None and surviving_activity.user_id is None
    assert (await db_session.execute(
        select(ProductGrowthEvent).where(ProductGrowthEvent.user_id == user_id)
    )).scalars().all() == []

    await record_automation_run_finished(
        db_session,
        surviving_job,
        run_id=generate_ulid(),
        status="completed",
    )
    await db_session.commit()
    assert (await db_session.execute(
        select(ProductGrowthEvent).where(ProductGrowthEvent.user_id == user_id)
    )).scalars().all() == []


@pytest.mark.asyncio
async def test_restore_uncascades_entity_workspaces(client: AsyncClient, db_session):
    """Restoring a sole-admin's account also un-soft-deletes their
    cascaded entity + workspaces."""
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace

    _, headers, body = await _register(client, username="lifecycle_restore_cascade")
    entity_id = body["entity_id"]
    ws_resp = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Coming back"},
    )
    ws_id = ws_resp.json()["id"]

    await client.delete("/api/v1/auth/me", headers=headers)

    restore = await client.post(
        "/api/v1/auth/me/restore",
        json={
            "email": "lifecycle_restore_cascade@test.com",
            "password": "pass123",
        },
    )
    assert restore.status_code == 200

    entity = (await db_session.execute(select(Entity).where(Entity.id == entity_id))).scalar_one()
    ws = (await db_session.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
    assert entity.deleted_at is None
    assert ws.deleted_at is None


@pytest.mark.asyncio
async def test_purge_user_deletes_only_owned_twilio_voice_sessions(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import TwilioVoiceCallSession
    from packages.core.models.user import Entity, User
    from packages.core.services.auth_service import hash_password
    from packages.core.services.user_lifecycle import purge_user
    from packages.core.services.voice.call_sessions import create_call_session

    entity = Entity(id=generate_ulid(), name="Voice user purge")
    deleting_user = User(
        entity_id=entity.id,
        email="voice-user-purge@test.com",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
        deleted_at=datetime.now(timezone.utc),
    )
    surviving_user = User(
        entity_id=entity.id,
        email="voice-user-survives@test.com",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add_all([entity, deleting_user, surviving_user])
    await db_session.flush()
    deleted_session, _ = await create_call_session(
        db_session,
        entity_id=entity.id,
        owner_user_id=deleting_user.id,
        channel_config_id=generate_ulid(),
        direction="outbound",
        call_sid="CA-user-purge-deleted",
        from_number="+14155550110",
        to_number="+14155550111",
    )
    surviving_session, _ = await create_call_session(
        db_session,
        entity_id=entity.id,
        owner_user_id=surviving_user.id,
        channel_config_id=generate_ulid(),
        direction="outbound",
        call_sid="CA-user-purge-survives",
        from_number="+14155550110",
        to_number="+14155550112",
    )
    await db_session.commit()

    assert await purge_user(db_session, deleting_user.id) is True

    assert await db_session.get(TwilioVoiceCallSession, deleted_session.id) is None
    assert await db_session.get(TwilioVoiceCallSession, surviving_session.id) is not None


@pytest.mark.asyncio
async def test_purge_last_user_deletes_unowned_entity_voice_sessions(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import TwilioVoiceCallSession
    from packages.core.models.user import Entity, User
    from packages.core.services.auth_service import hash_password
    from packages.core.services.user_lifecycle import purge_user
    from packages.core.services.voice.call_sessions import create_call_session

    deleted_at = datetime.now(timezone.utc)
    entity = Entity(
        id=generate_ulid(),
        name="Voice entity purge",
        deleted_at=deleted_at,
    )
    user = User(
        entity_id=entity.id,
        email="voice-entity-purge@test.com",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
        deleted_at=deleted_at,
    )
    db_session.add_all([entity, user])
    await db_session.flush()
    legacy_session, _ = await create_call_session(
        db_session,
        entity_id=entity.id,
        owner_user_id=None,
        channel_config_id=generate_ulid(),
        direction="inbound",
        call_sid="CA-entity-purge-legacy",
        from_number="+14155550111",
        to_number="+14155550110",
    )
    await db_session.commit()

    assert await purge_user(db_session, user.id) is True

    assert await db_session.get(TwilioVoiceCallSession, legacy_session.id) is None
    assert await db_session.get(Entity, entity.id) is None
