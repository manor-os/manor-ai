"""E2E tests for the new role management + staff invite endpoints.

Covers:
  - Permission catalog (GET /permissions)
  - Role CRUD (GET/POST/PUT/DELETE /staff/roles)
  - System role protections (can't rename/delete)
  - Permission gating — admin can mutate, member cannot
  - Staff invite creation + token issuance
  - Accept invite flow (token → User row + JWT)
  - Integration.required_permission gating for agent MCP access
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from packages.core.models.base import generate_ulid
from packages.core.models.notification import Notification
from packages.core.models.user import Entity, User

from packages.core.services.auth_service import hash_password
from packages.core.services.oauth_provider_config import OAuthProviderConfig


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _owner_headers(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    """Register a new entity (caller becomes owner). Returns (headers, user_id, token)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["user_id"], data["access_token"]


async def _downgrade_to(client: AsyncClient, owner_headers: dict, user_id: str, role: str, username: str) -> dict:
    """Downgrade the caller's role, re-login, return new headers with the downgraded JWT."""
    resp = await client.put(
        f"/api/v1/auth/users/{user_id}/role",
        headers=owner_headers,
        json={"role": role},
    )
    assert resp.status_code == 200, f"role update failed: {resp.status_code} {resp.text}"
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _register_from_staff_invite(
    client: AsyncClient,
    *,
    token: str,
    email: str,
    username: str = "Invited User",
    password: str = "pass123",
):
    return await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
            "invite_token": token,
        },
    )


def _mock_google_oauth(monkeypatch: pytest.MonkeyPatch, profile: dict) -> None:
    async def fake_resolve_oauth_config(_db, server_key: str):
        assert server_key == "gmail"
        return OAuthProviderConfig(
            server_key="gmail",
            client_id="google-client-id",
            client_secret="google-client-secret",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes="openid email profile",
            redirect_path="/oauth/callback",
            source="test",
        )

    class FakeGoogleResponse:
        def __init__(self, payload: dict):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    class FakeGoogleClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeGoogleResponse(
                {
                    "access_token": "google-access-token",
                    "refresh_token": "google-refresh-token",
                }
            )

        async def get(self, *_args, **_kwargs):
            return FakeGoogleResponse(profile)

    monkeypatch.setattr(
        "packages.core.services.oauth_provider_config.resolve_oauth_config",
        fake_resolve_oauth_config,
    )
    monkeypatch.setattr("apps.api.routers.auth.httpx.AsyncClient", FakeGoogleClient)


# ── Permission catalog ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permissions_catalog_groups(client: AsyncClient):
    """GET /permissions returns enum grouped by module with labels."""
    headers, _, _ = await _owner_headers(client, "cat_owner")
    resp = await client.get("/api/v1/permissions", headers=headers)
    assert resp.status_code == 200

    groups = resp.json()
    assert isinstance(groups, list)
    group_names = {g["name"] for g in groups}
    # Core groups must exist
    for required in ("Entity", "Users", "Tasks", "Agents", "Integrations", "MCP", "Admin"):
        assert required in group_names, f"missing group: {required}"

    # Every permission has key + label
    for g in groups:
        for p in g["permissions"]:
            assert "key" in p and "label" in p
            assert "." in p["key"]  # e.g. "users.manage"


# ── Role CRUD ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_roles_seeded_on_first_list(client: AsyncClient):
    """First GET /staff/roles for a fresh entity seeds the 4 system roles."""
    headers, _, _ = await _owner_headers(client, "roles_seed")
    resp = await client.get("/api/v1/staff/roles", headers=headers)
    assert resp.status_code == 200

    roles = resp.json()
    names = {r["name"].lower() for r in roles}
    assert {"viewer", "member", "admin", "owner"} <= names

    # System flag set correctly, at least one marked as default
    for r in roles:
        if r["name"].lower() in ("viewer", "member", "admin", "owner"):
            assert r["is_system"] is True
    assert any(r["is_default"] for r in roles)


@pytest.mark.asyncio
async def test_create_custom_role(client: AsyncClient):
    """Owner creates a custom role with a subset of permissions."""
    headers, _, _ = await _owner_headers(client, "roles_create")

    resp = await client.post(
        "/api/v1/staff/roles",
        headers=headers,
        json={"name": "Finance Lead", "permissions": ["tasks.read", "docs.read", "admin.billing"]},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Finance Lead"
    assert data["is_system"] is False
    assert set(data["permissions"]) == {"tasks.read", "docs.read", "admin.billing"}
    assert data["staff_count"] == 0


@pytest.mark.asyncio
async def test_cannot_reuse_system_role_name(client: AsyncClient):
    """System role names (admin/owner/member/viewer) are reserved."""
    headers, _, _ = await _owner_headers(client, "roles_collide")
    resp = await client.post(
        "/api/v1/staff/roles",
        headers=headers,
        json={"name": "Admin", "permissions": []},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_role_rejects_unknown_permission(client: AsyncClient):
    """Invalid permission keys are rejected up front."""
    headers, _, _ = await _owner_headers(client, "roles_badperm")
    resp = await client.post(
        "/api/v1/staff/roles",
        headers=headers,
        json={"name": "Bogus", "permissions": ["not.a.real.permission"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_update_role_permissions(client: AsyncClient):
    """PUT updates permission array; name stays."""
    headers, _, _ = await _owner_headers(client, "roles_update")

    create = await client.post(
        "/api/v1/staff/roles",
        headers=headers,
        json={"name": "Support", "permissions": ["tasks.read"]},
    )
    role_id = create.json()["id"]

    update = await client.put(
        f"/api/v1/staff/roles/{role_id}",
        headers=headers,
        json={"permissions": ["tasks.read", "tasks.create"]},
    )
    assert update.status_code == 200
    assert set(update.json()["permissions"]) == {"tasks.read", "tasks.create"}
    assert update.json()["name"] == "Support"


@pytest.mark.asyncio
async def test_system_role_permissions_editable(client: AsyncClient):
    """System roles (viewer/member/admin/owner) can have their permissions edited."""
    headers, _, _ = await _owner_headers(client, "sys_edit")
    roles = (await client.get("/api/v1/staff/roles", headers=headers)).json()
    viewer = next(r for r in roles if r["name"].lower() == "viewer")

    # Add a new permission to the viewer role
    new_perms = list(viewer["permissions"]) + ["tasks.create"]
    update = await client.put(
        f"/api/v1/staff/roles/{viewer['id']}",
        headers=headers,
        json={"permissions": new_perms},
    )
    assert update.status_code == 200
    assert "tasks.create" in update.json()["permissions"]
    assert update.json()["is_system"] is True
    assert update.json()["name"].lower() == "viewer"


@pytest.mark.asyncio
async def test_system_role_cannot_be_renamed(client: AsyncClient):
    """Backend rejects PUT name: for a system role."""
    headers, _, _ = await _owner_headers(client, "sys_rename")
    roles = (await client.get("/api/v1/staff/roles", headers=headers)).json()
    admin = next(r for r in roles if r["name"].lower() == "admin")

    resp = await client.put(
        f"/api/v1/staff/roles/{admin['id']}",
        headers=headers,
        json={"name": "SuperAdmin"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_delete_system_role(client: AsyncClient):
    """System roles are protected from deletion."""
    headers, _, _ = await _owner_headers(client, "roles_del_sys")
    roles = (await client.get("/api/v1/staff/roles", headers=headers)).json()
    admin_role = next(r for r in roles if r["name"].lower() == "admin")

    resp = await client.delete(
        f"/api/v1/staff/roles/{admin_role['id']}",
        headers=headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_custom_role(client: AsyncClient):
    """Custom roles delete cleanly; 204 No Content."""
    headers, _, _ = await _owner_headers(client, "roles_delcustom")
    create = await client.post(
        "/api/v1/staff/roles",
        headers=headers,
        json={"name": "Temp", "permissions": ["tasks.read"]},
    )
    role_id = create.json()["id"]

    resp = await client.delete(
        f"/api/v1/staff/roles/{role_id}",
        headers=headers,
    )
    assert resp.status_code == 204


# ── Permission gating ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_cannot_create_role(client: AsyncClient):
    """Downgraded-to-member user gets 403 on POST /staff/roles."""
    owner_headers, user_id, _ = await _owner_headers(client, "gate_member")
    member_headers = await _downgrade_to(client, owner_headers, user_id, "member", "gate_member")

    resp = await client.post(
        "/api/v1/staff/roles",
        headers=member_headers,
        json={"name": "X", "permissions": []},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_update_role(client: AsyncClient):
    """Member gets 403 on PUT /staff/roles/{id}."""
    owner_headers, user_id, _ = await _owner_headers(client, "gate_update")
    # Create a custom role while still owner
    create = await client.post(
        "/api/v1/staff/roles",
        headers=owner_headers,
        json={"name": "Helper", "permissions": ["tasks.read"]},
    )
    role_id = create.json()["id"]
    member_headers = await _downgrade_to(client, owner_headers, user_id, "member", "gate_update")

    resp = await client.put(
        f"/api/v1/staff/roles/{role_id}",
        headers=member_headers,
        json={"permissions": []},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_delete_role(client: AsyncClient):
    """Member gets 403 on DELETE /staff/roles/{id}."""
    owner_headers, user_id, _ = await _owner_headers(client, "gate_delete")
    create = await client.post(
        "/api/v1/staff/roles",
        headers=owner_headers,
        json={"name": "Doomed", "permissions": []},
    )
    role_id = create.json()["id"]
    member_headers = await _downgrade_to(client, owner_headers, user_id, "member", "gate_delete")

    resp = await client.delete(
        f"/api/v1/staff/roles/{role_id}",
        headers=member_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_can_list_roles(client: AsyncClient):
    """Read access stays open so the UI can render the role list."""
    owner_headers, user_id, _ = await _owner_headers(client, "gate_list")
    member_headers = await _downgrade_to(client, owner_headers, user_id, "member", "gate_list")

    resp = await client.get("/api/v1/staff/roles", headers=member_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Staff invite ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_creates_pending_staff(client: AsyncClient):
    """Owner can invite. Invite returns a token and creates a pending Staff row."""
    headers, _, _ = await _owner_headers(client, "inv_owner")

    roles = (await client.get("/api/v1/staff/roles", headers=headers)).json()
    member_role = next(r for r in roles if r["name"].lower() == "member")

    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "newbie@test.com", "role_id": member_role["id"]},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "invited"
    assert data["email"] == "newbie@test.com"
    assert len(data["invite_token"]) > 20

    # The staff row should show in the list as 'invited'
    staff_list = (await client.get("/api/v1/staff", headers=headers)).json()
    target = next((s for s in staff_list if s.get("email") == "newbie@test.com"), None)
    assert target is not None
    assert target["status"] == "invited"


@pytest.mark.asyncio
async def test_staff_invite_respects_users_plan_limit(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Team invite must use the same users plan gate as /auth/users/invite."""
    from packages.core.services import plan_gate

    async def deny_users_gate(_db, _entity_id, resource):
        assert resource == "users"
        return plan_gate.GateResult(
            allowed=False,
            message="You've reached the Free plan limit of 1 team members. Upgrade for more.",
            limit=1,
            current=1,
            plan="Free",
        )

    plan_gate.invalidate_gate_cache()
    monkeypatch.setattr(plan_gate, "check", deny_users_gate)

    headers, _, _ = await _owner_headers(client, "inv_user_limit")
    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "blocked-invite@test.com"},
    )

    assert resp.status_code == 402
    assert resp.json()["detail"]["kind"] == "users"
    assert resp.json()["detail"]["limit"] == 1
    assert resp.json()["detail"]["current"] == 1


@pytest.mark.asyncio
async def test_invite_sends_email_and_notifies_inviter(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    """Team invite sends a mail link and writes an in-app audit notification."""
    sent: list[dict] = []

    async def fake_send_staff_invite_email(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_staff_invite_email",
        fake_send_staff_invite_email,
    )

    headers, user_id, _ = await _owner_headers(client, "inv_notify")
    roles = (await client.get("/api/v1/staff/roles", headers=headers)).json()
    member_role = next(r for r in roles if r["name"].lower() == "member")

    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "mailme@test.com", "role_id": member_role["id"]},
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["email_sent"] is True
    assert data["invite_url"].startswith("http://localhost:3010/login?")
    assert f"invite_token={data['invite_token']}" in data["invite_url"]
    assert "email=mailme%40test.com" in data["invite_url"]
    assert sent == [
        {
            "to": "mailme@test.com",
            "entity_name": "inv_notify Corp",
            "inviter_name": "inv_notify",
            "invite_url": data["invite_url"],
        }
    ]

    result = await db_session.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.type == "team_invite_sent",
        )
    )
    notification = result.scalar_one()
    assert notification.title == "Team invite created"
    assert notification.meta["invitee_email"] == "mailme@test.com"
    assert notification.meta["invite_url"] == data["invite_url"]
    assert notification.meta["email_sent"] is True


@pytest.mark.asyncio
async def test_invite_existing_user_creates_recipient_notification(
    client: AsyncClient,
):
    """Existing Manor users get an in-app invite without being auto-linked."""
    inviter_headers, _, _ = await _owner_headers(client, "recipient_notify_owner")
    invitee_headers, _, _ = await _owner_headers(client, "recipient_notify_user")
    invitee_email = "recipient_notify_user@test.com"

    roles = (await client.get("/api/v1/staff/roles", headers=inviter_headers)).json()
    member_role = next(r for r in roles if r["name"].lower() == "member")
    resp = await client.post(
        "/api/v1/staff/invite",
        headers=inviter_headers,
        json={"email": invitee_email, "role_id": member_role["id"]},
    )
    assert resp.status_code == 201, resp.text

    notifications = (await client.get("/api/v1/notifications", headers=invitee_headers)).json()
    received = [n for n in notifications["items"] if n["type"] == "team_invite_received"]
    assert len(received) == 1
    assert received[0]["metadata"]["invite_token"] == resp.json()["invite_token"]
    assert received[0]["metadata"]["entity_name"] == "recipient_notify_owner Corp"
    assert received[0]["metadata"]["invitee_email"] == invitee_email

    me = (await client.get("/api/v1/auth/me", headers=invitee_headers)).json()
    names = {m["entity_name"] for m in me["memberships"]}
    assert "recipient_notify_owner Corp" not in names


@pytest.mark.asyncio
async def test_member_cannot_invite(client: AsyncClient):
    """Members don't have users.invite — 403."""
    owner_headers, user_id, _ = await _owner_headers(client, "inv_gate")
    member_headers = await _downgrade_to(client, owner_headers, user_id, "member", "inv_gate")

    resp = await client.post(
        "/api/v1/staff/invite",
        headers=member_headers,
        json={"email": "nope@test.com"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_invite_rejects_duplicate_email(client: AsyncClient):
    """Can't invite the same email twice within the same entity, regardless of casing."""
    headers, _, _ = await _owner_headers(client, "inv_dup")
    first = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "  Twice@Test.com  "},
    )
    assert first.status_code == 201, first.text
    assert first.json()["email"] == "twice@test.com"
    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "twice@test.com"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invite_rejects_blank_email(client: AsyncClient):
    """Staff invites must have a usable email address."""
    headers, _, _ = await _owner_headers(client, "inv_blank")
    resp = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "   "},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_custom_staff_role_can_invite_after_account_creation(client: AsyncClient):
    """StaffRole permissions are honored even when legacy User.role is member."""
    owner_headers, _, _ = await _owner_headers(client, "custom_inviter_owner")

    role = await client.post(
        "/api/v1/staff/roles",
        headers=owner_headers,
        json={"name": "Invite Coordinator", "permissions": ["users.invite"]},
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=owner_headers,
        json={
            "name": "Invite Coordinator",
            "email": "custom-inviter@test.com",
            "role_id": role_id,
        },
    )
    assert staff_resp.status_code == 201, staff_resp.text
    assert staff_resp.json()["role_id"] == role_id

    account = await client.post(
        f"/api/v1/staff/{staff_resp.json()['id']}/create-account",
        headers=owner_headers,
    )
    assert account.status_code == 201, account.text

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "custom-inviter@test.com",
            "password": account.json()["password"],
        },
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "member"
    assert me.json()["staff_role_id"] == role_id
    assert "users.invite" in me.json()["permissions"]

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "invited-by-custom-role@test.com"},
    )
    assert invite.status_code == 201, invite.text


@pytest.mark.asyncio
async def test_create_staff_account_maps_system_staff_role(client: AsyncClient):
    """Manual account creation derives legacy User.role from Staff.role_id."""
    owner_headers, _, _ = await _owner_headers(client, "create_account_role")
    roles = (await client.get("/api/v1/staff/roles", headers=owner_headers)).json()
    admin_role = next(r for r in roles if r["name"].lower() == "admin")

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=owner_headers,
        json={
            "name": "Admin Staff",
            "email": "admin-staff-role@test.com",
            "role_id": admin_role["id"],
        },
    )
    assert staff_resp.status_code == 201, staff_resp.text

    account = await client.post(
        f"/api/v1/staff/{staff_resp.json()['id']}/create-account",
        headers=owner_headers,
    )
    assert account.status_code == 201, account.text

    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin-staff-role@test.com",
            "password": account.json()["password"],
        },
    )
    assert login.status_code == 200, login.text
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["role"] == "admin"
    assert me.json()["staff_role_id"] == admin_role["id"]


# ── Accept invite ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_invite_creates_user(client: AsyncClient):
    """Valid token → standard registration creates user + activates staff."""
    owner_headers, _, _ = await _owner_headers(client, "accept_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "join@test.com", "name": "New Joiner"},
    )
    token = invite.json()["invite_token"]

    info = await client.get("/api/v1/auth/invite-info", params={"token": token})
    assert info.status_code == 200, info.text
    assert info.json()["email"] == "join@test.com"

    resp = await _register_from_staff_invite(
        client,
        token=token,
        email="join@test.com",
        username="New Joiner",
        password="joinpass123",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["access_token"]
    assert data["role"] in ("member", "viewer", "admin", "owner")
    assert data["entity_id"]
    assert data["user_id"]
    assert invite.json()["staff_id"]

    # Staff row should now be active
    new_user_headers = {"Authorization": f"Bearer {data['access_token']}"}
    staff_list = (await client.get("/api/v1/staff", headers=new_user_headers)).json()
    target = next((s for s in staff_list if s.get("email") == "join@test.com"), None)
    assert target is not None
    assert target["status"] == "active"


@pytest.mark.asyncio
async def test_accept_invite_exposes_custom_role_permissions(client: AsyncClient):
    """Accepted custom StaffRole users get effective permissions from /auth/me."""
    owner_headers, _, _ = await _owner_headers(client, "accept_custom_role")
    role = await client.post(
        "/api/v1/staff/roles",
        headers=owner_headers,
        json={"name": "Guest Inviter", "permissions": ["users.invite"]},
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={
            "email": "custom-joiner@test.com",
            "name": "Custom Joiner",
            "role_id": role_id,
        },
    )
    assert invite.status_code == 201, invite.text

    accepted = await _register_from_staff_invite(
        client,
        token=invite.json()["invite_token"],
        email="custom-joiner@test.com",
        username="Custom Joiner",
        password="custompass123",
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "member"

    headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["staff_role_id"] == role_id
    assert "users.invite" in me.json()["permissions"]

    second_invite = await client.post(
        "/api/v1/staff/invite",
        headers=headers,
        json={"email": "invited-after-accept@test.com"},
    )
    assert second_invite.status_code == 201, second_invite.text


@pytest.mark.asyncio
async def test_existing_user_accepts_invite_as_membership(client: AsyncClient):
    """An existing account can join another company without losing its own entity."""
    org_a_headers, _, _ = await _owner_headers(client, "cross_invite_a")
    _, _, org_b_token = await _owner_headers(client, "cross_invite_b")

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=org_a_headers,
        json={"email": "cross_invite_b@test.com"},
    )
    assert invite.status_code == 201, invite.text

    registered = await _register_from_staff_invite(
        client,
        token=invite.json()["invite_token"],
        email="cross_invite_b@test.com",
        password="wrong-entity-pass",
    )
    assert registered.status_code == 409

    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        headers={"Authorization": f"Bearer {org_b_token}"},
        json={"token": invite.json()["invite_token"]},
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["access_token"]

    company_headers = {"Authorization": f"Bearer {accepted_body['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=company_headers)
    assert me.status_code == 200, me.text
    memberships = me.json()["memberships"]
    assert len(memberships) == 2
    assert any(m["is_current"] and m["entity_id"] == me.json()["entity_id"] for m in memberships)

    staff_list = (await client.get("/api/v1/staff", headers=company_headers)).json()
    target = next((s for s in staff_list if s.get("email") == "cross_invite_b@test.com"), None)
    assert target is not None
    assert target["status"] == "active"
    assert target["role"] != "staff"

    personal = next(m for m in memberships if not m["is_current"])
    switched = await client.post(
        "/api/v1/auth/entities/switch",
        headers=company_headers,
        json={"entity_id": personal["entity_id"]},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["entity_id"] == personal["entity_id"]


@pytest.mark.asyncio
async def test_invite_register_rejects_existing_email_case_insensitively(
    client: AsyncClient,
    db_session,
):
    """Invite registration must not create a duplicate account for email casing differences."""
    personal_entity = Entity(id=generate_ulid(), name="Case Register Personal")
    existing_user = User(
        id=generate_ulid(),
        entity_id=personal_entity.id,
        email="Case.Register@Test.com",
        display_name="Case Register",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
    )
    db_session.add_all([personal_entity, existing_user])
    await db_session.commit()

    owner_headers, _, _ = await _owner_headers(client, "case_register_invite_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "case.register@test.com"},
    )
    assert invite.status_code == 201, invite.text

    registered = await _register_from_staff_invite(
        client,
        token=invite.json()["invite_token"],
        email="case.register@test.com",
        password="new-pass-should-not-create",
    )
    assert registered.status_code == 409

    matching_users = (
        (await db_session.execute(select(User).where(func.lower(User.email) == "case.register@test.com")))
        .scalars()
        .all()
    )
    assert len(matching_users) == 1


@pytest.mark.asyncio
async def test_people_gateway_lists_and_accepts_pending_team_invite(client: AsyncClient):
    """People gateway is the logged-in invite gateway for existing accounts."""
    org_a_headers, _, _ = await _owner_headers(client, "gateway_invite_a")
    _, _, org_b_token = await _owner_headers(client, "gateway_invite_b")

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=org_a_headers,
        json={"email": "gateway_invite_b@test.com"},
    )
    assert invite.status_code == 201, invite.text

    personal_headers = {"Authorization": f"Bearer {org_b_token}"}
    before = await client.get("/api/v1/people/me", headers=personal_headers)
    assert before.status_code == 200, before.text
    before_body = before.json()
    assert len(before_body["pending_invites"]) == 1
    pending = before_body["pending_invites"][0]
    assert pending["email"] == "gateway_invite_b@test.com"
    assert pending["can_accept"] is True
    assert pending["can_decline"] is True
    assert before_body["billing"]["scope"] in {"member", "company"}

    accepted = await client.post(
        f"/api/v1/people/invites/{pending['invite_id']}/accept",
        headers=personal_headers,
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["access_token"]
    context = accepted_body["context"]
    assert context["pending_invites"] == []
    assert context["active_entity"]["id"] == pending["entity_id"]
    assert context["active_membership"]["staff_id"] == pending["invite_id"]
    assert context["active_membership"]["can_leave"] is True
    assert context["billing"]["scope"] == "company"
    assert context["billing"]["total_credits"] is not None
    assert context["billing"]["remaining_credits"] is not None

    company_headers = {"Authorization": f"Bearer {accepted_body['access_token']}"}
    directory = await client.get("/api/v1/people/directory", headers=company_headers)
    assert directory.status_code == 200, directory.text
    assert any(row["email"] == "gateway_invite_b@test.com" for row in directory.json())


@pytest.mark.asyncio
async def test_people_gateway_declines_pending_team_invite(client: AsyncClient):
    """Declining from the gateway does not link the user and records declined state."""
    org_a_headers, _, _ = await _owner_headers(client, "gateway_decline_a")
    _, _, org_b_token = await _owner_headers(client, "gateway_decline_b")

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=org_a_headers,
        json={"email": "gateway_decline_b@test.com"},
    )
    assert invite.status_code == 201, invite.text

    personal_headers = {"Authorization": f"Bearer {org_b_token}"}
    before = (await client.get("/api/v1/people/me", headers=personal_headers)).json()
    invite_id = before["pending_invites"][0]["invite_id"]

    declined = await client.post(
        f"/api/v1/people/invites/{invite_id}/decline",
        headers=personal_headers,
    )
    assert declined.status_code == 200, declined.text
    context = declined.json()["context"]
    assert context["pending_invites"] == []
    assert len(context["declined_invites"]) == 1
    assert context["declined_invites"][0]["invite_id"] == invite_id
    assert context["declined_invites"][0]["status"] == "declined"

    replay = await client.post(
        "/api/v1/auth/accept-invite",
        headers=personal_headers,
        json={"token": invite.json()["invite_token"]},
    )
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_people_gateway_switches_and_leaves_membership(client: AsyncClient):
    """Switch and leave are membership actions that return a fresh context."""
    org_a_headers, _, _ = await _owner_headers(client, "gateway_leave_a")
    _, _, org_b_token = await _owner_headers(client, "gateway_leave_b")

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=org_a_headers,
        json={"email": "gateway_leave_b@test.com"},
    )
    assert invite.status_code == 201, invite.text
    personal_headers = {"Authorization": f"Bearer {org_b_token}"}
    invite_id = (await client.get("/api/v1/people/me", headers=personal_headers)).json()["pending_invites"][0][
        "invite_id"
    ]
    accepted = await client.post(
        f"/api/v1/people/invites/{invite_id}/accept",
        headers=personal_headers,
    )
    assert accepted.status_code == 200, accepted.text

    company_headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}
    company_context = accepted.json()["context"]
    personal_membership = next(m for m in company_context["memberships"] if not m["is_current"])
    switched = await client.post(
        f"/api/v1/people/memberships/{personal_membership['entity_id']}/switch",
        headers=company_headers,
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["context"]["active_entity"]["id"] == personal_membership["entity_id"]

    personal_again_headers = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    company_membership = next(
        m for m in switched.json()["context"]["memberships"] if m["entity_id"] != personal_membership["entity_id"]
    )
    switched_back = await client.post(
        f"/api/v1/people/memberships/{company_membership['entity_id']}/switch",
        headers=personal_again_headers,
    )
    assert switched_back.status_code == 200, switched_back.text

    leave_headers = {"Authorization": f"Bearer {switched_back.json()['access_token']}"}
    left = await client.post(
        f"/api/v1/people/memberships/{company_membership['entity_id']}/leave",
        headers=leave_headers,
    )
    assert left.status_code == 200, left.text
    left_body = left.json()
    assert left_body["access_token"]
    assert left_body["context"]["active_entity"]["id"] == personal_membership["entity_id"]
    assert all(m["entity_id"] != company_membership["entity_id"] for m in left_body["context"]["memberships"])


@pytest.mark.asyncio
async def test_accept_invite_rejects_bad_token(client: AsyncClient):
    """Garbage token → 400."""
    headers, _, _ = await _owner_headers(client, "bad_invite_token")
    info = await client.get("/api/v1/auth/invite-info", params={"token": "obviously-not-real"})
    assert info.status_code == 400

    resp = await client.post(
        "/api/v1/auth/accept-invite",
        headers=headers,
        json={"token": "obviously-not-real"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_accept_invite_single_use(client: AsyncClient):
    """Token can only be used once — second attempt fails."""
    owner_headers, _, _ = await _owner_headers(client, "once_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "single@test.com"},
    )
    token = invite.json()["invite_token"]

    first = await _register_from_staff_invite(
        client,
        token=token,
        email="single@test.com",
        password="pw",
    )
    assert first.status_code == 200

    # Replay
    second = await client.post(
        "/api/v1/auth/accept-invite",
        headers={"Authorization": f"Bearer {first.json()['access_token']}"},
        json={"token": token},
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_oauth_google_accepts_team_invite(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """Google OAuth can consume a team invite when the Google email matches."""
    _mock_google_oauth(
        monkeypatch,
        {
            "sub": "google-user-joiner",
            "email": "google.join@test.com",
            "email_verified": True,
            "name": "Google Joiner",
            "given_name": "Google",
            "family_name": "Joiner",
            "picture": "https://example.com/avatar.png",
        },
    )

    owner_headers, _, _ = await _owner_headers(client, "google_invite_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "google.join@test.com", "name": "Pending Google"},
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["invite_token"]

    accepted = await client.post(
        "/api/v1/auth/oauth/google",
        json={
            "code": "fake-google-code",
            "redirect_uri": "http://test/oauth/callback",
            "team_invite_token": token,
        },
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["access_token"]
    assert accepted_body["user_id"]

    invited_headers = {"Authorization": f"Bearer {accepted_body['access_token']}"}
    staff_list = (await client.get("/api/v1/staff", headers=invited_headers)).json()
    target = next((s for s in staff_list if s.get("email") == "google.join@test.com"), None)
    assert target is not None
    assert target["status"] == "active"
    assert target["user_id"] == accepted_body["user_id"]

    replay = await client.post(
        "/api/v1/auth/oauth/google",
        json={
            "code": "fake-google-code",
            "redirect_uri": "http://test/oauth/callback",
            "team_invite_token": token,
        },
    )
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_oauth_team_invite_reuses_existing_user_case_insensitively(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    """Existing Google users sign in first, then accept the pending team invite manually."""
    _mock_google_oauth(
        monkeypatch,
        {
            "sub": "google-existing-case-user",
            "email": "case.google@test.com",
            "email_verified": True,
            "name": "Case Google",
            "given_name": "Case",
            "family_name": "Google",
            "picture": "https://example.com/case-avatar.png",
        },
    )

    personal_entity = Entity(id=generate_ulid(), name="Case Existing Personal")
    existing_user = User(
        id=generate_ulid(),
        entity_id=personal_entity.id,
        email="Case.Google@Test.com",
        display_name="Existing Case User",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
    )
    db_session.add_all([personal_entity, existing_user])
    await db_session.commit()

    owner_headers, _, _ = await _owner_headers(client, "google_case_invite_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "case.google@test.com", "name": "Case Google"},
    )
    assert invite.status_code == 201, invite.text

    signed_in = await client.post(
        "/api/v1/auth/oauth/google",
        json={
            "code": "fake-google-code",
            "redirect_uri": "http://test/oauth/callback",
            "team_invite_token": invite.json()["invite_token"],
        },
    )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user_id"] == existing_user.id

    existing_headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}
    staff_list = (await client.get("/api/v1/staff", headers=owner_headers)).json()
    active_staff = next((s for s in staff_list if s.get("email") == "case.google@test.com"), None)
    assert active_staff is not None
    assert active_staff["status"] == "active"
    assert active_staff["user_id"] == existing_user.id

    gateway = await client.get("/api/v1/people/me", headers=existing_headers)
    assert gateway.status_code == 200, gateway.text
    gateway_body = gateway.json()
    assert gateway_body["pending_invites"] == []
    assert gateway_body["active_entity"]["id"] == signed_in.json()["entity_id"]

    matching_users = (
        (await db_session.execute(select(User).where(func.lower(User.email) == "case.google@test.com"))).scalars().all()
    )
    assert len(matching_users) == 1


@pytest.mark.asyncio
async def test_member_can_leave_team_and_access_is_disabled(client: AsyncClient):
    """A linked non-admin staff user can leave the team without deleting history."""
    owner_headers, _, _ = await _owner_headers(client, "leave_owner")
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": "self.leave@test.com", "name": "Self Leave"},
    )
    assert invite.status_code == 201, invite.text

    accepted = await _register_from_staff_invite(
        client,
        token=invite.json()["invite_token"],
        email="self.leave@test.com",
        username="Self Leave",
        password="leavepass123",
    )
    assert accepted.status_code == 200, accepted.text
    member_headers = {"Authorization": f"Bearer {accepted.json()['access_token']}"}

    left = await client.post("/api/v1/staff/me/leave", headers=member_headers)
    assert left.status_code == 200, left.text
    assert left.json()["status"] == "inactive"

    disabled = await client.get("/api/v1/auth/me", headers=member_headers)
    assert disabled.status_code == 403

    staff_list = (await client.get("/api/v1/staff", headers=owner_headers)).json()
    target = next((s for s in staff_list if s.get("email") == "self.leave@test.com"), None)
    assert target is not None
    assert target["status"] == "inactive"
    assert target["user_id"] == accepted.json()["user_id"]


@pytest.mark.asyncio
async def test_owner_cannot_self_leave_team(client: AsyncClient):
    """Owners must transfer ownership or change role before leaving."""
    owner_headers, _, _ = await _owner_headers(client, "leave_owner_blocked")
    resp = await client.post("/api/v1/staff/me/leave", headers=owner_headers)
    assert resp.status_code == 403
