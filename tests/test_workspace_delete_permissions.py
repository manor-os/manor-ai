"""Workspace deletion uses action permission without granting global visibility."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

import packages.core.database as db_module
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.base import generate_ulid
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.permissions import Permission, _get_role_permissions
from tests.test_document_permissions import _auth, _create_entity_user
from tests.test_workspace_authz_holes import _make_members_only_workspace


async def _staff_admin(entity_id: str, username: str, *, permissions=None):
    admin = await _create_entity_user(entity_id, username, role="admin")
    async with db_module.async_session() as db:
        role = StaffRole(
            entity_id=entity_id,
            name="admin",
            permissions=sorted(permissions if permissions is not None else {
                permission.value for permission in _get_role_permissions("admin")
            }),
        )
        db.add(role)
        await db.flush()
        staff = Staff(
            entity_id=entity_id, user_id=admin["id"], name=username,
            role_id=role.id, status="active",
        )
        db.add(staff)
        await db.flush()
        db.add(UserMembership(
            entity_id=entity_id, user_id=admin["id"], role="admin",
            staff_id=staff.id, status="active",
        ))
        await db.commit()
        admin["staff_role_id"] = role.id
        admin["staff_id"] = staff.id
    return admin


@pytest.mark.asyncio
async def test_admin_can_delete_and_restore_imported_workspace_without_global_read_permission(client: AsyncClient):
    owner_headers = await _auth(client, "import_delete_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    admin = await _staff_admin(owner["entity_id"], "import_delete_admin")
    payload = {
        "manifest": {
            "blueprint_version": "1.1", "title": "Imported workspace", "kind": "project",
        },
        "contract": {"variables": [], "channels": [], "sessions": [], "requires": {}},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"operating_model": {"context": "Test", "primary_work": "Test"}},
        "policy": {"governance": {}},
    }
    installed = await client.post(
        "/api/v1/blueprints/install-payload", headers=admin["headers"],
        json={"payload": payload, "mode": "simulate"},
    )
    assert installed.status_code == 201, installed.text
    workspace_id = installed.json()["workspace_id"]
    async with db_module.async_session() as db:
        membership = (await db.execute(select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.user_id == admin["id"],
        ))).scalar_one()
        assert membership.role == "owner"
        assert membership.status == "active"
        role = await db.get(StaffRole, admin["staff_role_id"])
        assert Permission.WORKSPACES_DELETE.value in role.permissions
        assert Permission.WORKSPACES_READ_ALL.value not in role.permissions

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    listed = await client.get("/api/v1/workspaces", headers=admin["headers"])
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert deleted.status_code == 204, deleted.text
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_delete"] is True
    assert next(row for row in listed.json() if row["id"] == workspace_id)["can_delete"] is True
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace.deleted_at is not None
    hidden = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert hidden.status_code == 404
    trash = await client.get("/api/v1/workspaces/trash/list", headers=admin["headers"])
    assert trash.status_code == 200, trash.text
    assert [row["id"] for row in trash.json()] == [workspace_id]
    assert trash.json()[0]["can_delete"] is True
    restored = await client.post(f"/api/v1/workspaces/{workspace_id}/restore", headers=admin["headers"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["can_delete"] is True
    visible = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert visible.status_code == 200, visible.text
    trash = await client.get("/api/v1/workspaces/trash/list", headers=admin["headers"])
    assert trash.json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_role", ["viewer", "editor", "contributor", None])
async def test_admin_delete_requires_target_management(client: AsyncClient, workspace_role: str | None):
    headers = await _auth(client, "delete_manage_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    admin = await _staff_admin(owner["entity_id"], "delete_manage_admin")
    workspace_id = await _make_members_only_workspace(owner["entity_id"], "Not managed", owner["id"])
    async with db_module.async_session() as db:
        if workspace_role:
            db.add(WorkspaceStaff(workspace_id=workspace_id, user_id=admin["id"], role=workspace_role))
        else:
            (await db.get(Workspace, workspace_id)).settings = {"access_mode": "entity_visible"}
        await db.commit()
    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_manage"] is False
    assert detail.json()["can_delete"] is False
    listed = await client.get("/api/v1/workspaces", headers=admin["headers"])
    assert next(row for row in listed.json() if row["id"] == workspace_id)["can_delete"] is False
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert deleted.status_code == 403, deleted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_role", ["admin", "member"])
async def test_scoped_trash_and_restore_exclude_unmanaged_workspaces(client: AsyncClient, entity_role: str):
    headers = await _auth(client, "trash_scope_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    admin = (
        await _staff_admin(owner["entity_id"], "trash_scope_admin") if entity_role == "admin"
        else await _create_entity_user(owner["entity_id"], "trash_scope_member", role="member")
    )
    owned_id = await _make_members_only_workspace(owner["entity_id"], "Owned trash", admin["id"])
    denied_ids = []
    for scope in ("viewer", "editor", "inactive", "expired", "private", "entity_visible"):
        workspace_id = await _make_members_only_workspace(owner["entity_id"], scope, owner["id"])
        denied_ids.append(workspace_id)
        async with db_module.async_session() as db:
            if scope in {"viewer", "editor", "inactive", "expired"}:
                db.add(WorkspaceStaff(
                    workspace_id=workspace_id, user_id=admin["id"],
                    role=scope if scope in {"viewer", "editor"} else "owner",
                    status="inactive" if scope == "inactive" else "active",
                    expires_at=datetime.now(timezone.utc) - timedelta(hours=1) if scope == "expired" else None,
                ))
            if scope == "entity_visible":
                (await db.get(Workspace, workspace_id)).settings = {"access_mode": "entity_visible"}
            await db.commit()
    foreign_headers = await _auth(client, "trash_scope_foreign")
    foreign = (await client.get("/api/v1/auth/me", headers=foreign_headers)).json()
    foreign_id = await _make_members_only_workspace(foreign["entity_id"], "Foreign trash", foreign["id"])
    async with db_module.async_session() as db:
        await db.execute(update(Workspace).where(Workspace.id.in_([owned_id, foreign_id, *denied_ids]))
                         .values(deleted_at=datetime.now(timezone.utc)))
        await db.commit()
    trash = await client.get("/api/v1/workspaces/trash/list", headers=admin["headers"])
    assert trash.status_code == 200, trash.text
    assert [row["id"] for row in trash.json()] == [owned_id]
    for workspace_id in [*denied_ids, foreign_id, generate_ulid()]:
        restore = await client.post(f"/api/v1/workspaces/{workspace_id}/restore", headers=admin["headers"])
        assert restore.status_code == 404, restore.text
        purge = await client.delete(f"/api/v1/workspaces/trash/{workspace_id}", headers=admin["headers"])
        assert purge.status_code == 404, purge.text
    async with db_module.async_session() as db:
        for workspace_id in [owned_id, foreign_id, *denied_ids]:
            assert (await db.get(Workspace, workspace_id)).deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["create", "import"])
async def test_member_creator_can_delete_and_restore_workspace(client: AsyncClient, entry: str):
    headers = await _auth(client, "trash_member_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member = await _create_entity_user(owner["entity_id"], "trash_member", role="member")
    if entry == "create":
        created = await client.post("/api/v1/workspaces", headers=member["headers"], json={"name": "Member owner"})
    else:
        created = await client.post("/api/v1/blueprints/install-payload", headers=member["headers"], json={
            "mode": "simulate",
            "payload": {
                "manifest": {"blueprint_version": "1.1", "title": "Member import", "kind": "project"},
                "contract": {"variables": [], "channels": [], "sessions": [], "requires": {}},
                "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
                "recipe": {"operating_model": {"context": "Test", "primary_work": "Test"}},
                "policy": {"governance": {}},
            },
        })
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id" if entry == "create" else "workspace_id"]
    if entry == "create":
        assert created.json()["can_delete"] is True
    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=member["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_delete"] is True
    listing = await client.get("/api/v1/workspaces", headers=member["headers"])
    assert next(row for row in listing.json() if row["id"] == workspace_id)["can_delete"] is True
    updated = await client.put(f"/api/v1/workspaces/{workspace_id}", headers=member["headers"],
                               json={"name": "Renamed by member owner"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["can_delete"] is True
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=member["headers"])
    assert deleted.status_code == 204, deleted.text
    trash = await client.get("/api/v1/workspaces/trash/list", headers=member["headers"])
    assert trash.status_code == 200, trash.text
    assert [row["id"] for row in trash.json()] == [workspace_id]
    restore = await client.post(f"/api/v1/workspaces/{workspace_id}/restore", headers=member["headers"])
    assert restore.status_code == 200, restore.text
    assert restore.json()["can_delete"] is True
    assert restore.json()["deleted_at"] is None


@pytest.mark.asyncio
async def test_workspace_owner_does_not_need_entity_delete_permission(client: AsyncClient):
    headers = await _auth(client, "owner_permission_entity")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    admin = await _staff_admin(owner["entity_id"], "owner_permission_admin", permissions=[])
    workspace_id = await _make_members_only_workspace(owner["entity_id"], "Independent ownership", admin["id"])
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert deleted.status_code == 204, deleted.text
    restored = await client.post(f"/api/v1/workspaces/{workspace_id}/restore", headers=admin["headers"])
    assert restored.status_code == 200, restored.text


@pytest.mark.asyncio
async def test_global_read_permission_does_not_replace_delete_permission(client: AsyncClient):
    owner_headers = await _auth(client, "delete_action_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace_id = await _make_members_only_workspace(
        owner["entity_id"], "No delete authority", owner["id"],
    )
    permissions = {
        permission.value for permission in _get_role_permissions("owner")
    } - {Permission.WORKSPACES_DELETE.value}
    admin = await _staff_admin(owner["entity_id"], "delete_action_admin", permissions=permissions)
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert deleted.status_code == 403, deleted.text
    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["can_delete"] is False
    async with db_module.async_session() as db:
        assert (await db.get(Workspace, workspace_id)).deleted_at is None
    # Preserve existing global-admin recovery even when delete is withheld.
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=owner_headers)
    assert deleted.status_code == 204, deleted.text
    trash = await client.get("/api/v1/workspaces/trash/list", headers=admin["headers"])
    assert trash.status_code == 200, trash.text
    assert workspace_id in {row["id"] for row in trash.json()}
    restored = await client.post(f"/api/v1/workspaces/{workspace_id}/restore", headers=admin["headers"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["can_delete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_role", ["admin", "member"])
@pytest.mark.parametrize("membership_status", [None, "inactive", "expired"])
async def test_delete_permission_does_not_bypass_target_workspace_scope(
    client: AsyncClient, membership_status: str | None, entity_role: str,
):
    from datetime import datetime, timedelta, timezone

    owner_headers = await _auth(client, "delete_scope_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace_id = await _make_members_only_workspace(
        owner["entity_id"], "Private workspace", owner["id"],
    )
    admin = (
        await _staff_admin(owner["entity_id"], "delete_scope_admin") if entity_role == "admin"
        else await _create_entity_user(owner["entity_id"], "delete_scope_member", role="member")
    )
    if membership_status:
        async with db_module.async_session() as db:
            db.add(WorkspaceStaff(
                workspace_id=workspace_id, user_id=admin["id"], role="owner",
                status="inactive" if membership_status == "inactive" else "active",
                expires_at=(
                    datetime.now(timezone.utc) - timedelta(hours=1)
                    if membership_status == "expired" else None
                ),
            ))
            await db.commit()
    detail = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert detail.status_code == 404, detail.text
    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])
    assert deleted.status_code == 404, deleted.text
    async with db_module.async_session() as db:
        assert (await db.get(Workspace, workspace_id)).deleted_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "restore"])
@pytest.mark.parametrize("revocation", [
    "delete_permission", "membership", "workspace_membership", "workspace_role",
    "staff", "staff_role", "user", "token", "member_owner_role", "member_owner_expired",
])
async def test_lifecycle_rechecks_authority_after_lock_wait(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, revocation: str, operation: str,
):
    from packages.core.services import entity_service, reusable_resource_locks

    owner_headers = await _auth(client, "delete_wait_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    global_authority = revocation in {"delete_permission", "staff_role"}
    member_owner = revocation.startswith("member_owner_")
    permissions = {
        permission.value for permission in _get_role_permissions(
            "owner" if operation == "delete" or global_authority else "admin",
        )
    }
    admin = (
        await _create_entity_user(owner["entity_id"], "delete_wait_member", role="member") if member_owner
        else await _staff_admin(owner["entity_id"], "delete_wait_admin", permissions=permissions)
    )
    workspace_id = await _make_members_only_workspace(
        owner["entity_id"], "Revoke while waiting", owner["id"] if global_authority else admin["id"],
    )
    if operation == "restore":
        async with db_module.async_session() as db:
            (await db.get(Workspace, workspace_id)).deleted_at = datetime.now(timezone.utc)
            await db.commit()
    ready, proceed = asyncio.Event(), asyncio.Event()
    original_lock = reusable_resource_locks.lock_reusable_resource_lifecycle
    waited = False

    async def delayed_lock(*args, **kwargs):
        nonlocal waited
        if not waited:
            waited = True
            ready.set()
            await proceed.wait()
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(reusable_resource_locks, "lock_reusable_resource_lifecycle", delayed_lock)
    monkeypatch.setattr(entity_service, "lock_reusable_resource_lifecycle", delayed_lock)
    pending = asyncio.create_task(client.request(
        "DELETE" if operation == "delete" else "POST",
        f"/api/v1/workspaces/{workspace_id}" + ("/restore" if operation == "restore" else ""),
        headers=admin["headers"],
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=30)
        async with db_module.async_session() as db:
            if revocation == "delete_permission":
                role = await db.get(StaffRole, admin["staff_role_id"])
                permission = Permission.WORKSPACES_DELETE if operation == "delete" else Permission.WORKSPACES_READ_ALL
                role.permissions = [key for key in role.permissions if key != permission.value]
            elif revocation == "membership":
                membership = (await db.execute(select(UserMembership).where(
                    UserMembership.user_id == admin["id"],
                    UserMembership.entity_id == owner["entity_id"],
                ))).scalar_one()
                membership.status = "inactive"
            elif revocation in {"workspace_membership", "workspace_role"} or member_owner:
                if not member_owner:
                    role = await db.get(StaffRole, admin["staff_role_id"])
                    role.permissions = [key for key in role.permissions if key != Permission.WORKSPACES_READ_ALL.value]
                membership = (await db.execute(select(WorkspaceStaff).where(
                    WorkspaceStaff.workspace_id == workspace_id,
                    WorkspaceStaff.user_id == admin["id"],
                ))).scalar_one()
                if revocation in {"workspace_role", "member_owner_role"}:
                    membership.role = "viewer"
                elif revocation == "member_owner_expired":
                    membership.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                else:
                    membership.status = "inactive"
            elif revocation == "staff":
                (await db.get(Staff, admin["staff_id"])).status = "inactive"
            elif revocation == "staff_role":
                (await db.get(StaffRole, admin["staff_role_id"])).status = "inactive"
            elif revocation == "user":
                (await db.get(User, admin["id"])).status = "inactive"
            else:
                user = await db.get(User, admin["id"])
                user.token_version += 1
            await db.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=30)
        expected_status = 404
        if revocation in {"delete_permission", "workspace_role", "member_owner_role"} and operation == "delete":
            expected_status = 403
        assert response.status_code == expected_status, response.text
        async with db_module.async_session() as db:
            workspace = await db.get(Workspace, workspace_id)
            assert (workspace.deleted_at is None) == (operation == "delete")
    finally:
        proceed.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delete", "restore"])
@pytest.mark.parametrize("authority", ["staff_role", "workspace_membership", "user"])
async def test_lifecycle_freezes_authority_until_commit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, authority: str, operation: str,
):
    from apps.api.routers import workspaces as workspace_router

    owner_headers = await _auth(client, "delete_freeze_entity_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    admin = await _staff_admin(owner["entity_id"], "delete_freeze_admin")
    workspace_id = await _make_members_only_workspace(
        owner["entity_id"], "Freeze authority", admin["id"],
    )
    if operation == "restore":
        async with db_module.async_session() as db:
            (await db.get(Workspace, workspace_id)).deleted_at = datetime.now(timezone.utc)
            await db.commit()
    if authority == "staff_role":
        revocation = update(StaffRole).where(StaffRole.id == admin["staff_role_id"]).values(permissions=[])
    elif authority == "workspace_membership":
        revocation = update(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.user_id == admin["id"],
        ).values(status="inactive")
    else:
        revocation = update(User).where(User.id == admin["id"]).values(token_version=User.token_version + 1)
    service_name = "soft_delete_workspace" if operation == "delete" else "restore_workspace"
    original_operation = getattr(workspace_router, service_name)
    revocation_was_blocked = False

    async def operation_with_concurrent_revocation(db, target_id, entity_id):
        nonlocal revocation_was_blocked
        async with db_module.async_session() as revoking_db:
            await revoking_db.execute(text("SET LOCAL lock_timeout = '250ms'"))
            try:
                await revoking_db.execute(revocation)
            except DBAPIError as exc:
                assert "lock timeout" in str(exc).lower()
                revocation_was_blocked = True
            finally:
                await revoking_db.rollback()
        return await original_operation(db, target_id, entity_id)

    monkeypatch.setattr(workspace_router, service_name, operation_with_concurrent_revocation)
    response = await client.request(
        "DELETE" if operation == "delete" else "POST",
        f"/api/v1/workspaces/{workspace_id}" + ("/restore" if operation == "restore" else ""),
        headers=admin["headers"],
    )
    assert response.status_code == (204 if operation == "delete" else 200), response.text
    assert revocation_was_blocked, "Authority changed between authorization and lifecycle mutation"


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["workspace_role", "delete_permission", "token"])
async def test_trash_rechecks_authority_after_metadata_reads(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, revocation: str,
):
    from apps.api.routers import workspaces as workspace_router

    headers = await _auth(client, "trash_recheck_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    admin = await _staff_admin(owner["entity_id"], "trash_recheck_admin")
    workspace_id = await _make_members_only_workspace(owner["entity_id"], "Revoked trash", admin["id"])
    async with db_module.async_session() as db:
        (await db.get(Workspace, workspace_id)).deleted_at = datetime.now(timezone.utc)
        await db.commit()
    original_summaries = workspace_router._workspace_creator_summaries
    revoked = False

    async def summaries_then_revoke(db, rows):
        nonlocal revoked
        result = await original_summaries(db, rows)
        async with db_module.async_session() as revoking_db:
            if revocation == "workspace_role":
                await revoking_db.execute(update(WorkspaceStaff).where(
                    WorkspaceStaff.workspace_id == workspace_id, WorkspaceStaff.user_id == admin["id"],
                ).values(role="viewer"))
            elif revocation == "delete_permission":
                (await revoking_db.get(StaffRole, admin["staff_role_id"])).permissions = []
            else:
                user = await revoking_db.get(User, admin["id"])
                user.token_version += 1
            await revoking_db.commit()
        revoked = True
        return result

    monkeypatch.setattr(workspace_router, "_workspace_creator_summaries", summaries_then_revoke)
    response = await client.get("/api/v1/workspaces/trash/list", headers=admin["headers"])
    assert revoked
    if revocation == "workspace_role":
        assert response.status_code == 200, response.text
        assert response.json() == []
    elif revocation == "delete_permission":
        # Organization permission revocation does not revoke independent ownership.
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == [workspace_id]
    else:
        assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_delete_rejects_foreign_unknown_and_already_deleted_targets(client: AsyncClient):
    headers = await _auth(client, "delete_target_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    foreign_headers = await _auth(client, "delete_foreign_owner")
    foreign = (await client.get("/api/v1/auth/me", headers=foreign_headers)).json()
    foreign_workspace_id = await _make_members_only_workspace(
        foreign["entity_id"], "Other company", foreign["id"],
    )
    deleted_workspace_id = await _make_members_only_workspace(
        owner["entity_id"], "Already deleted", owner["id"],
    )
    async with db_module.async_session() as db:
        (await db.get(Workspace, deleted_workspace_id)).deleted_at = datetime.now(timezone.utc)
        await db.commit()
    for target_id in (foreign_workspace_id, generate_ulid(), deleted_workspace_id):
        response = await client.delete(f"/api/v1/workspaces/{target_id}", headers=headers)
        assert response.status_code == 404, response.text
    async with db_module.async_session() as db:
        assert (await db.get(Workspace, foreign_workspace_id)).deleted_at is None
