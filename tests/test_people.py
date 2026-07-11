"""E2E tests: clients and staff members CRUD."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "peopleuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "People Corp",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── Clients ──


@pytest.mark.asyncio
async def test_create_client(client: AsyncClient):
    headers = await _auth(client, "cc1")
    resp = await client.post(
        "/api/v1/clients",
        headers=headers,
        json={
            "name": "Acme Inc",
            "email": "contact@acme.com",
            "phone": "+1-555-0100",
            "address": "123 Main St",
            "metadata": {"industry": "tech"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Acme Inc"
    assert data["email"] == "contact@acme.com"
    assert data["metadata"] == {"industry": "tech"}
    assert data["id"]


@pytest.mark.asyncio
async def test_list_clients(client: AsyncClient):
    headers = await _auth(client, "cc2")
    for name in ["Alpha Co", "Beta Co", "Gamma Co"]:
        await client.post("/api/v1/clients", headers=headers, json={"name": name})

    resp = await client.get("/api/v1/clients", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3


@pytest.mark.asyncio
async def test_search_clients(client: AsyncClient):
    headers = await _auth(client, "cc3")
    await client.post("/api/v1/clients", headers=headers, json={"name": "Searchable Corp"})
    await client.post("/api/v1/clients", headers=headers, json={"name": "Hidden LLC"})

    resp = await client.get("/api/v1/clients", headers=headers, params={"search": "Searchable"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "Searchable Corp"


@pytest.mark.asyncio
async def test_update_client(client: AsyncClient):
    headers = await _auth(client, "cc4")
    create_resp = await client.post("/api/v1/clients", headers=headers, json={"name": "Old Name"})
    cid = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v1/clients/{cid}",
        headers=headers,
        json={
            "name": "New Name",
            "phone": "+1-555-9999",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["phone"] == "+1-555-9999"


@pytest.mark.asyncio
async def test_delete_client(client: AsyncClient):
    headers = await _auth(client, "cc5")
    create_resp = await client.post("/api/v1/clients", headers=headers, json={"name": "ToDelete"})
    cid = create_resp.json()["id"]

    # Soft delete
    resp = await client.delete(f"/api/v1/clients/{cid}", headers=headers)
    assert resp.status_code == 204

    # Not visible in list
    list_resp = await client.get("/api/v1/clients", headers=headers)
    assert list_resp.json()["total"] == 0

    # Not accessible by ID
    get_resp = await client.get(f"/api/v1/clients/{cid}", headers=headers)
    assert get_resp.status_code == 404


# ── Staff ──


@pytest.mark.asyncio
async def test_create_staff(client: AsyncClient):
    headers = await _auth(client, "ss1")
    resp = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Jane Smith",
            "email": "jane@corp.com",
            "title": "Engineer",
            "department": "Engineering",
            "skills": ["python", "sql"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Jane Smith"
    assert data["department"] == "Engineering"
    assert data["skills"] == ["python", "sql"]
    assert data["id"]


@pytest.mark.asyncio
async def test_list_staff_by_department(client: AsyncClient):
    headers = await _auth(client, "ss2")
    await client.post("/api/v1/staff", headers=headers, json={"name": "A", "department": "Eng"})
    await client.post("/api/v1/staff", headers=headers, json={"name": "B", "department": "Eng"})
    await client.post("/api/v1/staff", headers=headers, json={"name": "C", "department": "Sales"})

    resp = await client.get("/api/v1/staff", headers=headers, params={"department": "Eng"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(s["department"] == "Eng" for s in data)


@pytest.mark.asyncio
async def test_update_staff(client: AsyncClient):
    headers = await _auth(client, "ss3")
    create_resp = await client.post("/api/v1/staff", headers=headers, json={"name": "Old Staff"})
    sid = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v1/staff/{sid}",
        headers=headers,
        json={
            "name": "Updated Staff",
            "title": "Senior Engineer",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Staff"
    assert resp.json()["title"] == "Senior Engineer"


@pytest.mark.asyncio
async def test_delete_staff(client: AsyncClient):
    headers = await _auth(client, "ss4")
    create_resp = await client.post("/api/v1/staff", headers=headers, json={"name": "ToRemove"})
    sid = create_resp.json()["id"]

    resp = await client.delete(f"/api/v1/staff/{sid}", headers=headers)
    assert resp.status_code == 204
    repeat = await client.delete(f"/api/v1/staff/{sid}", headers=headers)
    assert repeat.status_code == 204

    # Hard deleted — gone
    get_resp = await client.get(f"/api/v1/staff/{sid}", headers=headers)
    assert get_resp.status_code == 404


# ── Isolation ──


@pytest.mark.asyncio
async def test_people_isolation(client: AsyncClient):
    """User A's clients and staff are invisible to User B."""
    headers_a = await _auth(client, "iso_a")
    headers_b = await _auth(client, "iso_b")

    # A creates a client and a staff member
    c_resp = await client.post("/api/v1/clients", headers=headers_a, json={"name": "A Client"})
    s_resp = await client.post("/api/v1/staff", headers=headers_a, json={"name": "A Staff"})
    cid = c_resp.json()["id"]
    sid = s_resp.json()["id"]

    # B can't see them
    assert (await client.get(f"/api/v1/clients/{cid}", headers=headers_b)).status_code == 404
    assert (await client.get(f"/api/v1/staff/{sid}", headers=headers_b)).status_code == 404

    # B's lists are empty
    cl = await client.get("/api/v1/clients", headers=headers_b)
    assert cl.json()["total"] == 0
    sl = await client.get("/api/v1/staff", headers=headers_b)
    assert len(sl.json()) == 0


@pytest.mark.asyncio
async def test_invited_existing_unlinked_staff_waits_for_invite_acceptance(client: AsyncClient):
    headers = await _auth(client, "staff_existing_user")

    invited_user = await client.post(
        "/api/v1/auth/users/invite",
        headers=headers,
        json={
            "email": "existing.staff.user@test.com",
            "role": "member",
        },
    )
    assert invited_user.status_code == 200, invited_user.text

    create_staff = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Existing User Staff",
            "email": "existing.staff.user@test.com",
        },
    )
    assert create_staff.status_code == 201, create_staff.text
    staff = create_staff.json()
    assert staff["user_id"] is None
    assert staff["has_unlinked_user_account"] is True

    create_account = await client.post(
        f"/api/v1/staff/{staff['id']}/create-account",
        headers=headers,
    )
    assert create_account.status_code == 409

    listed = await client.get("/api/v1/staff", headers=headers)
    target = next(s for s in listed.json() if s.get("email") == "existing.staff.user@test.com")
    assert target["user_id"] is None
    assert target["has_unlinked_user_account"] is True


@pytest.mark.asyncio
async def test_resend_staff_invite_generates_accept_link_without_linking(client: AsyncClient):
    headers = await _auth(client, "staff_resend_invite")

    invited_user = await client.post(
        "/api/v1/auth/users/invite",
        headers=headers,
        json={
            "email": "resend.staff.user@test.com",
            "role": "member",
        },
    )
    assert invited_user.status_code == 200, invited_user.text

    create_staff = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Resend Staff",
            "email": "resend.staff.user@test.com",
        },
    )
    assert create_staff.status_code == 201, create_staff.text
    staff_id = create_staff.json()["id"]

    resend = await client.post(
        f"/api/v1/staff/{staff_id}/resend-invite",
        headers=headers,
    )
    assert resend.status_code == 200, resend.text
    body = resend.json()
    assert body["staff_id"] == staff_id
    assert body["email"] == "resend.staff.user@test.com"
    assert body["status"] == "invited"
    assert len(body["invite_token"]) > 20
    assert body["invite_token"] in body["invite_url"]
    assert "/login?invite_token=" in body["invite_url"]
    assert body["email_sent"] is True

    before_accept = await client.get(f"/api/v1/staff/{staff_id}", headers=headers)
    assert before_accept.status_code == 200
    assert before_accept.json()["user_id"] is None
    assert before_accept.json()["has_unlinked_user_account"] is True

    listed = await client.get("/api/v1/staff", headers=headers)
    target = next(s for s in listed.json() if s["id"] == staff_id)
    assert target["user_id"] is None
    assert target["has_unlinked_user_account"] is True


@pytest.mark.asyncio
async def test_reset_staff_account_password(client: AsyncClient):
    headers = await _auth(client, "reset_staff")
    create_staff = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Reset Staff",
            "email": "reset.staff@test.com",
        },
    )
    sid = create_staff.json()["id"]

    created = await client.post(f"/api/v1/staff/{sid}/create-account", headers=headers)
    assert created.status_code == 201, created.text
    old_password = created.json()["password"]

    reset = await client.post(f"/api/v1/staff/{sid}/reset-password", headers=headers)
    assert reset.status_code == 200, reset.text
    data = reset.json()
    assert data["staff_id"] == sid
    assert data["email"] == "reset.staff@test.com"
    assert data["password"]
    assert data["password"] != old_password

    old_login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "reset.staff@test.com",
            "password": old_password,
        },
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "reset.staff@test.com",
            "password": data["password"],
        },
    )
    assert new_login.status_code == 200, new_login.text


@pytest.mark.asyncio
async def test_reset_staff_account_password_requires_account(client: AsyncClient):
    headers = await _auth(client, "reset_no_account")
    create_staff = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "No Account Staff",
            "email": "no.account.staff@test.com",
        },
    )
    sid = create_staff.json()["id"]

    reset = await client.post(f"/api/v1/staff/{sid}/reset-password", headers=headers)
    assert reset.status_code == 409


@pytest.mark.asyncio
async def test_staff_with_existing_unlinked_user_waits_for_invite_acceptance(client: AsyncClient):
    headers = await _auth(client, "unlinked_user")
    user_resp = await client.post(
        "/api/v1/auth/users/invite",
        headers=headers,
        json={"email": "existing.unlinked.staff@test.com", "role": "member"},
    )
    assert user_resp.status_code == 200, user_resp.text

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Existing Unlinked Staff",
            "email": "existing.unlinked.staff@test.com",
            "status": "invited",
        },
    )
    assert staff_resp.status_code == 201, staff_resp.text
    staff = staff_resp.json()
    assert staff["user_id"] is None
    assert staff["has_unlinked_user_account"] is True

    account = await client.post(f"/api/v1/staff/{staff['id']}/create-account", headers=headers)
    assert account.status_code == 409

    listed = await client.get("/api/v1/staff", headers=headers)
    assert listed.status_code == 200, listed.text
    target = next(s for s in listed.json() if s["id"] == staff["id"])
    assert target["user_id"] is None
    assert target["has_unlinked_user_account"] is True


@pytest.mark.asyncio
async def test_resend_existing_unlinked_staff_invite_generates_accept_link_without_linking(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    sent: list[dict] = []

    async def fake_send_staff_invite_email(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_staff_invite_email",
        fake_send_staff_invite_email,
    )

    headers = await _auth(client, "resend_staff_invite")
    user_resp = await client.post(
        "/api/v1/auth/users/invite",
        headers=headers,
        json={"email": "resend.existing.staff@test.com", "role": "member"},
    )
    assert user_resp.status_code == 200, user_resp.text

    staff_resp = await client.post(
        "/api/v1/staff",
        headers=headers,
        json={
            "name": "Resend Existing Staff",
            "email": "resend.existing.staff@test.com",
            "status": "invited",
        },
    )
    assert staff_resp.status_code == 201, staff_resp.text
    staff_id = staff_resp.json()["id"]

    resend = await client.post(f"/api/v1/staff/{staff_id}/resend-invite", headers=headers)
    assert resend.status_code == 200, resend.text
    data = resend.json()
    assert data["staff_id"] == staff_id
    assert data["email"] == "resend.existing.staff@test.com"
    assert data["status"] == "invited"
    assert data["email_sent"] is True
    assert len(data["invite_token"]) > 20
    assert data["invite_url"].startswith("http://localhost:3010/login?")
    assert f"invite_token={data['invite_token']}" in data["invite_url"]
    assert "email=resend.existing.staff%40test.com" in data["invite_url"]
    assert sent and sent[0]["to"] == "resend.existing.staff@test.com"
    assert sent[0]["invite_url"] == data["invite_url"]

    info = await client.get("/api/v1/auth/invite-info", params={"token": data["invite_token"]})
    assert info.status_code == 200, info.text
    assert info.json()["email"] == "resend.existing.staff@test.com"

    listed = await client.get("/api/v1/staff", headers=headers)
    target = next(s for s in listed.json() if s["id"] == staff_id)
    assert target["user_id"] is None
    assert target["has_unlinked_user_account"] is True


@pytest.mark.asyncio
async def test_reset_staff_account_password_is_entity_scoped(client: AsyncClient):
    headers_a = await _auth(client, "reset_iso_a")
    headers_b = await _auth(client, "reset_iso_b")
    create_staff = await client.post(
        "/api/v1/staff",
        headers=headers_a,
        json={
            "name": "Entity A Staff",
            "email": "entity.a.staff@test.com",
        },
    )
    sid = create_staff.json()["id"]
    await client.post(f"/api/v1/staff/{sid}/create-account", headers=headers_a)

    reset = await client.post(f"/api/v1/staff/{sid}/reset-password", headers=headers_b)
    assert reset.status_code == 404
