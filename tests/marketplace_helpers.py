"""Shared helpers for the marketplace test files (merchant, pricing, share,
checkout). Deliberately a plain importable module — NOT conftest.py — so the
helpers never surprise unrelated tests."""


def _force_fallback_verification(monkeypatch):
    """Route verification codes to the in-memory fallback store even when a
    real Redis is reachable on this machine — otherwise codes land in Redis
    where ``_register`` can't read them and cloud-mode tests flake."""
    import packages.core.services.email_verification_service as evs
    monkeypatch.setattr(evs, "_redis", lambda: None)


async def _register(client, name, *, plan_id="plan_personal"):
    email = f"{name}@test.com"
    r = await client.post("/api/v1/auth/register", json={
        "username": name, "email": email,
        "password": "pass123456789", "entity_name": name,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    if body.get("requires_verification"):
        # Cloud mode gates registration behind email verification. The
        # caller patched _redis to None, so the code sits in the service's
        # in-memory fallback.
        from packages.core.services.email_verification_service import _fallback
        code = _fallback[email]["code"]
        r = await client.post("/api/v1/auth/verify-email", json={"email": email, "code": code})
        assert r.status_code == 200, r.text
        body = r.json()
    entity_id = body["entity_id"]
    if plan_id:
        import asyncio
        from sqlalchemy import update
        import packages.core.database as db_module
        from packages.core.models.user import Entity

        # FastAPI finalises yield dependencies after building the response.
        # ASGITransport normally waits for that finaliser, but under the full
        # async suite a fresh connection can very briefly miss the committed
        # registration row. Use the app fixture's current engine and retry the
        # idempotent plan assignment rather than leaving marketplace tests
        # dependent on connection scheduling.
        updated = False
        for _ in range(20):
            async with db_module.engine.begin() as conn:
                result = await conn.execute(
                    update(Entity)
                    .where(Entity.id == entity_id)
                    .values(plan_id=plan_id, settings={"plan": plan_id})
                )
            if result.rowcount == 1:
                updated = True
                break
            await asyncio.sleep(0.01)
        assert updated, f"registered entity {entity_id} was not persisted"
    return {"Authorization": f"Bearer {body['access_token']}"}, entity_id


async def _restricted_staff_headers(client, owner_headers, name):
    """Create a same-entity staff account with no effective permissions."""
    role = await client.post(
        "/api/v1/staff/roles",
        headers=owner_headers,
        json={"name": f"{name} restricted", "permissions": []},
    )
    assert role.status_code == 201, role.text
    staff = await client.post(
        "/api/v1/staff",
        headers=owner_headers,
        json={
            "name": name,
            "email": f"{name}@test.com",
            "role_id": role.json()["id"],
        },
    )
    assert staff.status_code == 201, staff.text
    account = await client.post(
        f"/api/v1/staff/{staff.json()['id']}/create-account",
        headers=owner_headers,
    )
    assert account.status_code == 201, account.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": f"{name}@test.com", "password": account.json()["password"]},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}
