"""E2E tests: RBAC permissions system."""

import pytest
from httpx import AsyncClient

from packages.core.permissions import Permission, _get_role_permissions, has_permission


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
