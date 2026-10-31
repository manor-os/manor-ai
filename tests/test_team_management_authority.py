"""Team management authority: nobody can take over or outrank the owner.

Owner and admin are protected roles: only an owner can grant them or act on a
person who holds one. Plain field edits need user management, a pending
invite cannot be redirected to another address, password resets never reach
accounts that belong to other companies, and deactivation stays in this
company.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.staff import Staff
from packages.core.models.user import User, UserMembership
from tests.test_role_management import _owner_headers, _register_from_staff_invite


async def _role_id(client: AsyncClient, headers: dict, name: str) -> str:
    roles = await client.get("/api/v1/staff/roles", headers=headers)
    assert roles.status_code == 200, roles.text
    return next(role["id"] for role in roles.json() if role["name"].lower() == name)


async def _join(client: AsyncClient, owner_headers: dict, *, role: str, username: str) -> tuple[dict, str, str]:
    """Invite + accept a new account with a system role. Returns (headers, user_id, staff_id)."""
    email = f"{username}@test.com"
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": email, "name": username, "role_id": await _role_id(client, owner_headers, role)},
    )
    assert invite.status_code == 201, invite.text
    accepted = await _register_from_staff_invite(
        client, token=invite.json()["invite_token"], email=email, username=username,
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user_id"], invite.json()["staff_id"]


@pytest.mark.asyncio
async def test_pending_invite_cannot_be_redirected_to_another_address(client: AsyncClient, db_session):
    # Catches: any member (even a viewer) rewriting a pending admin invite's
    # email to their own address and accepting it with the admin role.
    owner_headers, _, _ = await _owner_headers(client, "redirect_invite_owner")
    viewer_headers, _, _ = await _join(client, owner_headers, role="viewer", username="redirect_invite_viewer")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "intended.admin@test.com", "role_id": await _role_id(client, owner_headers, "admin")},
    )
    assert invite.status_code == 201, invite.text
    staff_id, token = invite.json()["staff_id"], invite.json()["invite_token"]

    hijack = await client.put(
        f"/api/v1/staff/{staff_id}", headers=viewer_headers, json={"email": "attacker@test.com"},
    )
    assert hijack.status_code == 403

    # A manager correcting the address voids the old link; it must be re-sent.
    corrected = await client.put(
        f"/api/v1/staff/{staff_id}", headers=owner_headers, json={"email": "corrected.admin@test.com"},
    )
    assert corrected.status_code == 200, corrected.text
    staff = await db_session.get(Staff, staff_id)
    await db_session.refresh(staff)
    assert (staff.meta or {}).get("invite_token") is None
    stale = await _register_from_staff_invite(client, token=token, email="corrected.admin@test.com")
    assert stale.status_code >= 400


@pytest.mark.asyncio
async def test_only_an_owner_can_grant_owner_or_admin(client: AsyncClient):
    # Catches: an admin inviting, assigning or self-assigning a protected role
    # (owner/admin) through any of the role-writing endpoints.
    owner_headers, _, _ = await _owner_headers(client, "grant_owner")
    admin_headers, admin_id, _ = await _join(client, owner_headers, role="admin", username="grant_admin")
    _, member_id, member_staff_id = await _join(client, owner_headers, role="member", username="grant_member")
    owner_role_id = await _role_id(client, owner_headers, "owner")
    admin_role_id = await _role_id(client, owner_headers, "admin")

    for role_id in (owner_role_id, admin_role_id):
        invited = await client.post(
            "/api/v1/staff/invite", headers=admin_headers,
            json={"email": f"escalate.{role_id[-6:]}@test.com", "role_id": role_id},
        )
        assert invited.status_code == 403, invited.text
    for body in ({"role_id": owner_role_id}, {"role": "owner"}, {"role": "admin"}):
        promoted = await client.put(f"/api/v1/staff/{member_staff_id}", headers=admin_headers, json=body)
        assert promoted.status_code == 403, (body, promoted.text)
    for user_id, role in ((admin_id, "owner"), (member_id, "admin")):
        changed = await client.put(
            f"/api/v1/auth/users/{user_id}/role", headers=admin_headers, json={"role": role},
        )
        assert changed.status_code == 403, (user_id, role, changed.text)

    # The owner can still grant both.
    granted = await client.put(
        f"/api/v1/auth/users/{member_id}/role", headers=owner_headers, json={"role": "admin"},
    )
    assert granted.status_code == 200, granted.text


@pytest.mark.asyncio
async def test_admin_cannot_act_on_the_owner_or_another_admin(client: AsyncClient, db_session):
    # Catches: an admin demoting or deactivating the owner, or managing a
    # fellow admin (resetting their password, removing them).
    owner_headers, owner_id, _ = await _owner_headers(client, "act_owner")
    admin_headers, _, _ = await _join(client, owner_headers, role="admin", username="act_admin")
    _, _, other_admin_staff_id = await _join(client, owner_headers, role="admin", username="act_admin2")

    demote = await client.put(f"/api/v1/auth/users/{owner_id}/role", headers=admin_headers, json={"role": "member"})
    assert demote.status_code == 403
    deactivate = await client.delete(f"/api/v1/auth/users/{owner_id}", headers=admin_headers)
    assert deactivate.status_code == 403
    for method, path in (
        ("post", f"/api/v1/staff/{other_admin_staff_id}/reset-password"),
        ("put", f"/api/v1/staff/{other_admin_staff_id}"),
        ("delete", f"/api/v1/staff/{other_admin_staff_id}"),
    ):
        kwargs = {"json": {"name": "Renamed"}} if method == "put" else {}
        response = await getattr(client, method)(path, headers=admin_headers, **kwargs)
        assert response.status_code == 403, (method, path, response.text)

    owner = await db_session.get(User, owner_id)
    await db_session.refresh(owner)
    assert (owner.role, owner.status) == ("owner", "active")


@pytest.mark.asyncio
async def test_password_reset_never_reaches_an_account_of_another_company(client: AsyncClient):
    # Catches: an admin here resetting the password of someone who also owns
    # another company and logging in as them there; and a reset leaving the
    # person's existing sessions alive.
    owner_a_headers, _, _ = await _owner_headers(client, "reset_company_a")
    _, _, owner_b_token = await _owner_headers(client, "reset_company_b")
    invite = await client.post(
        "/api/v1/staff/invite", headers=owner_a_headers, json={"email": "reset_company_b@test.com"},
    )
    assert invite.status_code == 201, invite.text
    owner_b_headers = {"Authorization": f"Bearer {owner_b_token}"}
    pending = (await client.get("/api/v1/people/me", headers=owner_b_headers)).json()["pending_invites"][0]
    accepted = await client.post(f"/api/v1/people/invites/{pending['invite_id']}/accept", headers=owner_b_headers)
    assert accepted.status_code == 200, accepted.text

    blocked = await client.post(
        f"/api/v1/staff/{invite.json()['staff_id']}/reset-password", headers=owner_a_headers,
    )
    assert blocked.status_code == 409, blocked.text

    member_headers, _, member_staff_id = await _join(
        client, owner_a_headers, role="member", username="reset_only_here",
    )
    reset = await client.post(f"/api/v1/staff/{member_staff_id}/reset-password", headers=owner_a_headers)
    assert reset.status_code == 200, reset.text
    assert (await client.get("/api/v1/auth/me", headers=member_headers)).status_code == 401


@pytest.mark.asyncio
async def test_deactivating_a_user_only_removes_them_from_this_company(client: AsyncClient, db_session):
    # Catches: the legacy deactivate endpoint disabling the account globally
    # (locking the person out of their own company) while the Team page still
    # showed them active.
    owner_a_headers, _, _ = await _owner_headers(client, "scope_company_a")
    _, owner_b_id, owner_b_token = await _owner_headers(client, "scope_company_b")
    invite = await client.post(
        "/api/v1/staff/invite", headers=owner_a_headers, json={"email": "scope_company_b@test.com"},
    )
    owner_b_headers = {"Authorization": f"Bearer {owner_b_token}"}
    pending = (await client.get("/api/v1/people/me", headers=owner_b_headers)).json()["pending_invites"][0]
    joined = await client.post(f"/api/v1/people/invites/{pending['invite_id']}/accept", headers=owner_b_headers)
    assert joined.status_code == 200, joined.text
    entity_a = pending["entity_id"]

    removed = await client.delete(f"/api/v1/auth/users/{owner_b_id}", headers=owner_a_headers)
    assert removed.status_code == 204, removed.text

    membership_a = (await db_session.execute(select(UserMembership.status).where(
        UserMembership.user_id == owner_b_id, UserMembership.entity_id == entity_a,
    ))).scalar_one()
    staff = await db_session.get(Staff, invite.json()["staff_id"])
    await db_session.refresh(staff)
    user = await db_session.get(User, owner_b_id)
    await db_session.refresh(user)
    assert (membership_a, staff.status, user.status) == ("inactive", "inactive", "active")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "scope_company_b@test.com", "password": "pass123"},
    )
    assert login.status_code == 200, login.text
