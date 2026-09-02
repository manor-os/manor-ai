"""E2E tests: RBAC permissions system."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User, UserMembership
from packages.core.permissions import (
    Permission,
    _get_role_permissions,
    effective_user_has_permission,
    effective_user_role_name,
    has_permission,
    resolve_effective_user_role_name,
    user_effective_permission_keys,
)
from packages.core.services.auth_service import hash_password


async def _auth(client: AsyncClient, username: str = "permuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


async def _auth_with_ids(client: AsyncClient, username: str = "permuser") -> tuple[dict, str, str]:
    """Register and return (headers, user_id, access_token)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    return headers, data["user_id"], data["access_token"]


def _reauth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Unit-style permission checks ──


def test_owner_has_all_permissions():
    """Owner role should have every defined permission."""
    owner_perms = _get_role_permissions("owner")
    for p in Permission:
        assert p in owner_perms, f"Owner missing permission: {p.value}"


def test_member_cannot_manage_users():
    """Member role should not have users.manage."""
    assert not has_permission("member", Permission.USERS_MANAGE)
    assert not has_permission("member", Permission.ADMIN_SETTINGS)
    assert not has_permission("member", Permission.ADMIN_API_KEYS)


def test_viewer_read_only():
    """Viewer should only have read/use permissions, no create/update/delete."""
    viewer_perms = _get_role_permissions("viewer")
    for p in viewer_perms:
        # All viewer permissions should be read-oriented
        assert any(keyword in p.value for keyword in ("read", "use")), f"Viewer has non-read permission: {p.value}"
    # Verify specific create/write permissions are absent
    assert not has_permission("viewer", Permission.TASKS_CREATE)
    assert not has_permission("viewer", Permission.DOCS_UPLOAD)
    assert not has_permission("viewer", Permission.AGENTS_CREATE)


@pytest.mark.asyncio
async def test_inactive_membership_cannot_restore_legacy_admin_role(db_session):
    from packages.core.services.resource_access import resolve_user_role

    entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email="inactive-membership-owner@test.com",
        display_name="Inactive Membership Owner",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserMembership(
        user_id=user.id,
        entity_id=entity_id,
        role="owner",
        status="inactive",
    ))
    await db_session.flush()

    assert await resolve_effective_user_role_name(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        legacy_role=user.role,
    ) == ""
    assert await resolve_user_role(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) is None


@pytest.mark.asyncio
async def test_active_staff_assignment_is_authoritative_over_legacy_role(db_session):
    from packages.core.services.document_access import (
        _resolve_user_role_and_document_read,
    )
    from packages.core.services.workspace_access import resolve_workspace_read_access

    entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email="demoted-admin-permissions@test.com",
        display_name="Demoted Admin",
        password_hash=hash_password("pass123"),
        role="admin",
        status="active",
    )
    role = StaffRole(
        entity_id=entity_id,
        name="Restricted",
        permissions=[],
        status="active",
    )
    db_session.add_all([user, role])
    await db_session.flush()
    staff = Staff(
        entity_id=entity_id,
        kind="employee",
        name=user.display_name,
        email=user.email,
        user_id=user.id,
        role_id=role.id,
        status="active",
    )
    db_session.add(staff)
    await db_session.flush()

    assert await effective_user_role_name(db_session, user) == "restricted"
    assert await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    ) == set()
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )
    assert await resolve_workspace_read_access(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    ) == ("restricted", False)
    assert await _resolve_user_role_and_document_read(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) == ("restricted", False, False)

    from packages.core.services.resource_access import (
        ResourceDescriptor,
        resolve_user_role,
        user_can_access_resource,
    )
    from packages.core.services.runtime_authorization import authorize_hitl_action

    assert await resolve_user_role(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) == "restricted"
    assert not await user_can_access_resource(
        db_session,
        descriptor=ResourceDescriptor(
            resource_type="agent",
            resource_id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=generate_ulid(),
            visibility="private",
        ),
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )
    hitl_decision = await authorize_hitl_action(
        db_session,
        entity_id=entity_id,
        workspace_id=None,
        by_user_id=user.id,
        hitl_type="authorize",
    )
    assert not hitl_decision.allowed
    assert hitl_decision.matched_rule == "permission.hitl.entity_admin"

    legacy_user = User(
        entity_id=entity_id,
        email="legacy-staff-owner@test.com",
        display_name="Legacy Staff Owner",
        password_hash=hash_password("pass123"),
        role="viewer",
        status="active",
    )
    db_session.add(legacy_user)
    await db_session.flush()
    db_session.add(
        Staff(
            entity_id=entity_id,
            kind="employee",
            name=legacy_user.display_name,
            email=legacy_user.email,
            user_id=legacy_user.id,
            role_id=None,
            meta={"role": "owner"},
            status="active",
        )
    )
    await db_session.flush()

    # Pre-StaffRole rows use Staff-owned metadata during migration; they do
    # not consult the deliberately conflicting User.role above.
    assert await effective_user_role_name(db_session, legacy_user) == "owner"
    assert await effective_user_has_permission(
        db_session,
        legacy_user,
        Permission.ADMIN_SETTINGS,
    )
    staff.role_id = None
    await db_session.flush()

    assert await effective_user_role_name(db_session, user) == ""
    assert await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    ) == set()
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )
    assert await resolve_workspace_read_access(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    ) == (None, False)
    assert await _resolve_user_role_and_document_read(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) == (None, False, False)

    staff.status = "inactive"
    await db_session.flush()

    assert await effective_user_role_name(db_session, user) == ""
    assert await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    ) == set()
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )
    assert await resolve_workspace_read_access(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    ) == (None, False)

    staff.status = "active"
    staff.deleted_at = datetime.now(timezone.utc)
    await db_session.flush()

    assert await effective_user_role_name(db_session, user) == ""
    assert await user_effective_permission_keys(
        db_session,
        user.id,
        entity_id,
        user.role,
    ) == set()
    assert not await effective_user_has_permission(
        db_session,
        user,
        Permission.ADMIN_SETTINGS,
    )
    assert await _resolve_user_role_and_document_read(
        db_session,
        user_id=user.id,
        entity_id=entity_id,
        role=user.role,
    ) == (None, False, False)


@pytest.mark.asyncio
async def test_batch_resource_reads_match_individual_authorization(db_session):
    from packages.core.models.permission import (
        Capability,
        GrantStatus,
        ResourceGrant,
        SubjectType,
        Visibility,
    )
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from packages.core.services.resource_access import (
        ResourceDescriptor,
        readable_resource_ids,
        user_can_access_resource,
    )

    entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email=f"batch-resource-reader-{generate_ulid()}@test.com",
        display_name="Batch resource reader",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    workspace = Workspace(
        entity_id=entity_id,
        name="Batch resource scope",
        status="active",
        settings={"access_mode": "members_only"},
    )
    db_session.add_all([user, workspace])
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=workspace.id,
        user_id=user.id,
        role="viewer",
        status="active",
    ))

    private_granted_id = generate_ulid()
    private_role_granted_id = generate_ulid()
    db_session.add_all([
        ResourceGrant(
            entity_id=entity_id,
            resource_type="skill",
            resource_id=private_granted_id,
            subject_type=SubjectType.USER,
            subject_id=user.id,
            capabilities=[Capability.VIEW],
            granted_at=datetime.now(timezone.utc),
            status=GrantStatus.ACTIVE,
        ),
        ResourceGrant(
            entity_id=entity_id,
            resource_type="skill",
            resource_id=private_role_granted_id,
            subject_type=SubjectType.WORKSPACE_ROLE,
            # Persist the canonical ULID casing. Batch authorization used to
            # compare this against a lower-cased derived role key while the
            # single-resource path normalized both sides.
            subject_id=f"{workspace.id}:viewer",
            capabilities=[Capability.VIEW],
            granted_at=datetime.now(timezone.utc),
            status=GrantStatus.ACTIVE,
        ),
    ])
    await db_session.flush()

    descriptors = [
        ResourceDescriptor(
            resource_type="skill",
            resource_id=generate_ulid(),
            entity_id=entity_id,
            visibility=Visibility.ENTITY,
        ),
        ResourceDescriptor(
            resource_type="skill",
            resource_id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace.id,
            visibility=Visibility.WORKSPACE,
        ),
        ResourceDescriptor(
            resource_type="skill",
            resource_id=private_granted_id,
            entity_id=entity_id,
            visibility=Visibility.PRIVATE,
        ),
        ResourceDescriptor(
            resource_type="skill",
            resource_id=private_role_granted_id,
            entity_id=entity_id,
            workspace_id=workspace.id,
            visibility=Visibility.PRIVATE,
        ),
        ResourceDescriptor(
            resource_type="skill",
            resource_id=generate_ulid(),
            entity_id=entity_id,
            visibility=Visibility.PRIVATE,
        ),
    ]

    individually_readable = {
        descriptor.resource_id
        for descriptor in descriptors
        if await user_can_access_resource(
            db_session,
            descriptor=descriptor,
            entity_id=entity_id,
            user_id=user.id,
            role=user.role,
            capability=Capability.VIEW,
        )
    }
    batch_readable = await readable_resource_ids(
        db_session,
        descriptors=descriptors,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
    )

    assert batch_readable == individually_readable
    assert batch_readable == {
        descriptors[0].resource_id,
        descriptors[1].resource_id,
        descriptors[2].resource_id,
        descriptors[3].resource_id,
    }


# ── Integration tests ──


@pytest.mark.asyncio
async def test_admin_settings_access(client: AsyncClient):
    """Owner (registered user) can access admin settings."""
    headers = await _auth(client, "permuser1")
    # Owner should be able to read settings
    resp = await client.get("/api/v1/admin/settings", headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_member_denied_settings(client: AsyncClient):
    """Member role should be denied access to admin settings."""
    headers, user_id, _token = await _auth_with_ids(client, "permuser2")

    # Change own role to member via the admin endpoint (user is currently owner)
    resp = await client.put(
        f"/api/v1/auth/users/{user_id}/role",
        headers=headers,
        json={"role": "member"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "member"

    # Re-login to get a token with the updated role
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "permuser2@test.com",
            "password": "pass123",
        },
    )
    assert login_resp.status_code == 200
    member_headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    # Member should be denied admin settings
    resp = await client.get("/api/v1/admin/settings", headers=member_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_permissions_endpoint(client: AsyncClient):
    """GET /permissions returns role and permission list."""
    headers = await _auth(client, "permuser3")
    resp = await client.get("/api/v1/auth/permissions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "owner"
    assert "admin.settings" in data["permissions"]
    assert "users.manage" in data["permissions"]
    assert isinstance(data["permissions"], list)
    # Owner should have all permissions
    assert len(data["permissions"]) == len(Permission)
