"""Shared helpers for the marketplace test files (merchant, pricing, share,
checkout). Deliberately a plain importable module — NOT conftest.py — so the
helpers never surprise unrelated tests."""


def _force_fallback_verification(monkeypatch):
    """Route verification codes to the in-memory fallback store even when a
    real Redis is reachable on this machine — otherwise codes land in Redis
    where ``_register`` can't read them and cloud-mode tests flake."""
    import packages.core.services.email_verification_service as evs
    monkeypatch.setattr(evs, "_redis", lambda: None)


async def _register(client, name):
    email = f"{name}@test.com"
    r = await client.post("/api/v1/auth/register", json={
        "username": name, "email": email,
        "password": "pass123", "entity_name": name,
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
    return {"Authorization": f"Bearer {body['access_token']}"}, body["entity_id"]
