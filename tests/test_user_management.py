"""E2E tests: OAuth + user management endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.user import User, UserMembership


async def _auth(client: AsyncClient, username: str = "mgmtuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_list_users(client: AsyncClient):
    """Register user, list users, verify 1 user returned."""
    headers = await _auth(client)
    resp = await client.get("/api/v1/auth/users", headers=headers)
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) == 1
    assert users[0]["username"] == "mgmtuser"


@pytest.mark.asyncio
async def test_member_can_use_directory_but_not_admin_user_list(client: AsyncClient):
    """Members need a minimal directory for mentions/assignees, but not
    the richer owner/admin user-management list."""
    headers = await _auth(client, username="memberdirectory")
    async with db_module.async_session() as session:
        user = (await session.execute(select(User).where(User.email == "memberdirectory@test.com"))).scalar_one()
        membership = (
            await session.execute(
                select(UserMembership).where(
                    UserMembership.user_id == user.id,
                    UserMembership.entity_id == user.entity_id,
                    UserMembership.status == "active",
                )
            )
        ).scalar_one()
        membership.role = "member"
        await session.commit()

    admin_resp = await client.get("/api/v1/auth/users", headers=headers)
    assert admin_resp.status_code == 403

    directory_resp = await client.get("/api/v1/auth/users/directory", headers=headers)
    assert directory_resp.status_code == 200
    rows = directory_resp.json()
    assert rows == [
        {
            "id": user.id,
            "email": "memberdirectory@test.com",
            "display_name": "memberdirectory",
            "avatar_url": user.avatar_url,
        }
    ]
    assert "role" not in rows[0]


@pytest.mark.asyncio
async def test_invite_user(client: AsyncClient):
    """Invite a user by email, verify created with 'invited' status."""
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/auth/users/invite",
        json={
            "email": "invited@test.com",
            "role": "member",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "invited@test.com"
    assert data["role"] == "member"

    # Verify shows up in user list
    list_resp = await client.get("/api/v1/auth/users", headers=headers)
    assert len(list_resp.json()) == 2


@pytest.mark.asyncio
async def test_change_user_role(client: AsyncClient):
    """Register user, invite another, change their role to admin."""
    headers = await _auth(client)

    # Invite a second user
    invite_resp = await client.post(
        "/api/v1/auth/users/invite",
        json={
            "email": "rolechange@test.com",
            "role": "member",
        },
        headers=headers,
    )
    user_id = invite_resp.json()["id"]

    # Change role
    resp = await client.put(
        f"/api/v1/auth/users/{user_id}/role",
        json={
            "role": "admin",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_deactivate_user(client: AsyncClient):
    """Register owner, invite a second user, deactivate them."""
    headers = await _auth(client)

    # Invite a second user
    invite_resp = await client.post(
        "/api/v1/auth/users/invite",
        json={
            "email": "deactivate@test.com",
            "role": "member",
        },
        headers=headers,
    )
    user_id = invite_resp.json()["id"]

    # Deactivate
    resp = await client.delete(f"/api/v1/auth/users/{user_id}", headers=headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_change_password(client: AsyncClient):
    """Register, change password, login with new password."""
    headers = await _auth(client, username="pwuser")

    # Change password
    resp = await client.put(
        "/api/v1/auth/password",
        json={
            "old_password": "pass123",
            "new_password": "newpass456",
        },
        headers=headers,
    )
    assert resp.status_code == 200

    # Login with new password
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "pwuser",
            "password": "newpass456",
        },
    )
    assert login_resp.status_code == 200
    assert login_resp.json()["access_token"]


@pytest.mark.asyncio
async def test_oauth_google_no_config(client: AsyncClient):
    """Call oauth/google without config, get 500 error."""
    resp = await client.post(
        "/api/v1/auth/oauth/google",
        json={
            "code": "fake-code",
            "redirect_uri": "http://localhost:3000/callback",
        },
    )
    assert resp.status_code == 500
    assert "not configured" in resp.json()["detail"].lower()
