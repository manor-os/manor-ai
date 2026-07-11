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
async def test_purge_after_grace_window_anonymizes(client: AsyncClient, db_session):
    """Hard-purge: the user row is gone, but their tasks remain
    with `created_by` rewritten to the deleted-user sentinel."""
    from packages.core.models.task import Task
    from packages.core.models.user import User
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
    task = Task(
        entity_id=entity_id,
        title="Audit task",
        status="pending",
        creator_id=user_id,
    )
    db_session.add(task)
    await db_session.commit()
    task_id = task.id

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
