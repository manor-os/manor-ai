"""A role change takes effect no matter which page made it.

The Team page sends a role name and the Users page edits the membership, but
permissions come from the linked Staff row's StaffRole. Both paths must move
the Staff role, the membership and the login together.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from packages.core.permissions import user_effective_permission_keys
from tests.test_role_management import _owner_headers, _register_from_staff_invite


async def _role_id(client: AsyncClient, headers: dict, name: str) -> str:
    roles = await client.get("/api/v1/staff/roles", headers=headers)
    assert roles.status_code == 200, roles.text
    return next(role["id"] for role in roles.json() if role["name"].lower() == name)


async def _join_as_member(client: AsyncClient, owner_headers: dict, username: str) -> tuple[str, str]:
    email = f"{username}@test.com"
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": email, "name": username, "role_id": await _role_id(client, owner_headers, "member")},
    )
    assert invite.status_code == 201, invite.text
    accepted = await _register_from_staff_invite(
        client, token=invite.json()["invite_token"], email=email, username=username,
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()["user_id"], invite.json()["staff_id"]


async def _assert_role(db_session, *, user_id: str, staff_id: str, entity_id: str, role_id: str, role: str) -> None:
    db_session.expire_all()
    staff = await db_session.get(Staff, staff_id)
    assert staff.role_id == role_id
    membership = (
        await db_session.execute(
            select(UserMembership).where(
                UserMembership.user_id == user_id, UserMembership.entity_id == entity_id,
            )
        )
    ).scalar_one()
    assert membership.role == role
    assert (await db_session.get(User, user_id)).role == role


@pytest.mark.asyncio
async def test_team_page_role_edit_changes_the_effective_role(client: AsyncClient, db_session):
    # Catches: the Team page's role dropdown writing only meta.role, which the
    # assigned StaffRole outranks, so the change never took effect.
    owner_headers, owner_id, _ = await _owner_headers(client, "team_role_edit_owner")
    entity_id = (await db_session.get(User, owner_id)).entity_id
    user_id, staff_id = await _join_as_member(client, owner_headers, "team_role_edit_member")
    assert "admin.settings" not in await user_effective_permission_keys(db_session, user_id, entity_id)

    # A free-text label from an older caller is not a role assignment.
    labelled = await client.put(f"/api/v1/staff/{staff_id}", headers=owner_headers, json={"role": "Designer"})
    assert labelled.status_code == 200, labelled.text
    assert labelled.json()["role_id"] == await _role_id(client, owner_headers, "member")

    updated =await client.put(f"/api/v1/staff/{staff_id}", headers=owner_headers, json={"role": "admin"})
    assert updated.status_code == 200, updated.text
    admin_role_id = await _role_id(client, owner_headers, "admin")
    assert updated.json()["role_id"] == admin_role_id

    await _assert_role(
        db_session, user_id=user_id, staff_id=staff_id, entity_id=entity_id, role_id=admin_role_id, role="admin",
    )
    assert "admin.settings" in await user_effective_permission_keys(db_session, user_id, entity_id)


@pytest.mark.asyncio
async def test_users_page_role_change_moves_the_staff_role(client: AsyncClient, db_session):
    # Catches: PUT /auth/users/{id}/role updating only the membership while the
    # linked Staff row (the actual authority) kept the old role.
    owner_headers, owner_id, _ = await _owner_headers(client, "users_role_edit_owner")
    entity_id = (await db_session.get(User, owner_id)).entity_id
    user_id, staff_id = await _join_as_member(client, owner_headers, "users_role_edit_member")

    changed = await client.put(
        f"/api/v1/auth/users/{user_id}/role", headers=owner_headers, json={"role": "viewer"},
    )
    assert changed.status_code == 200, changed.text

    viewer_role_id = await _role_id(client, owner_headers, "viewer")
    await _assert_role(
        db_session, user_id=user_id, staff_id=staff_id, entity_id=entity_id, role_id=viewer_role_id, role="viewer",
    )
    assert "tasks.create" not in await user_effective_permission_keys(db_session, user_id, entity_id)
