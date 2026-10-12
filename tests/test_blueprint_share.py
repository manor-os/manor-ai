"""Blueprint share tokens — lifecycle + resolve."""
import pytest
from httpx import AsyncClient

from tests.marketplace_helpers import _force_fallback_verification, _register

pytestmark = pytest.mark.asyncio


async def _make_blueprint(db_session, entity_id, slug, status="draft"):
    from packages.core.models.blueprint import WorkspaceBlueprint
    bp = WorkspaceBlueprint(
        entity_id=entity_id, slug=slug, title="Share BP",
        payload={"manifest": {}}, payload_version="1.1", status=status,
    )
    db_session.add(bp)
    await db_session.commit()
    return bp.id


async def test_share_token_lifecycle(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "sharer")
    other_headers, _ = await _register(client, "share_viewer")
    bp_id = await _make_blueprint(db_session, entity_id, "share-draft")

    # Draft is invisible to others without a token.
    r = await client.get(f"/api/v1/blueprints/{bp_id}", headers=other_headers)
    assert r.status_code == 404

    # Owner creates a token.
    r = await client.post(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers)
    assert r.status_code == 200, r.text
    token = r.json()["share_token"]
    assert len(token) >= 32

    # Any authenticated user resolves it — even for a draft.
    r = await client.get(f"/api/v1/blueprints/shared/{token}", headers=other_headers)
    assert r.status_code == 200
    assert r.json()["id"] == bp_id

    # Owner's summary now reports has_share_token without leaking the token.
    r = await client.get(f"/api/v1/blueprints/{bp_id}", headers=headers)
    assert r.json()["has_share_token"] is True
    assert "share_token" not in r.json()

    # Rotate: old token dies.
    r = await client.post(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers)
    token2 = r.json()["share_token"]
    assert token2 != token
    r = await client.get(f"/api/v1/blueprints/shared/{token}", headers=other_headers)
    assert r.status_code == 404

    # Revoke: new token dies too.
    r = await client.delete(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers)
    assert r.status_code == 204
    r = await client.get(f"/api/v1/blueprints/shared/{token2}", headers=other_headers)
    assert r.status_code == 404


async def test_share_link_limits_detail_preflight_and_install_to_verified_recipients(
    client: AsyncClient, db_session, monkeypatch,
):
    from sqlalchemy import select
    from packages.core.models.user import User
    from packages.core.services.user_email_proof import mark_email_verified

    _force_fallback_verification(monkeypatch)
    owner_headers, owner_entity = await _register(client, "share_restricted_owner")
    allowed_headers, _ = await _register(client, "share_restricted_allowed")
    other_headers, _ = await _register(client, "share_restricted_other")
    allowed_email = "share_restricted_allowed@test.com"
    allowed_user = (await db_session.execute(
        select(User).where(User.email == allowed_email)
    )).scalar_one()
    mark_email_verified(allowed_user, allowed_email)
    await db_session.commit()

    bp_id = await _make_blueprint(db_session, owner_entity, "share-restricted")
    created = await client.post(
        f"/api/v1/blueprints/{bp_id}/share-token",
        headers=owner_headers,
        json={"allowed_emails": [f"  {allowed_email.upper()}  "]},
    )
    assert created.status_code == 200, created.text
    token = created.json()["share_token"]
    owner_detail = await client.get(f"/api/v1/blueprints/{bp_id}", headers=owner_headers)
    assert owner_detail.json()["share_allowed_emails"] == [allowed_email]

    for headers, expected in ((allowed_headers, 200), (other_headers, 404)):
        assert (await client.get(
            f"/api/v1/blueprints/shared/{token}", headers=headers,
        )).status_code == expected
        assert (await client.get(
            f"/api/v1/blueprints/{bp_id}",
            params={"share_token": token}, headers=headers,
        )).status_code == expected
    assert (await client.get(
        f"/api/v1/blueprints/{bp_id}/install-preflight",
        params={"share_token": token}, headers=other_headers,
    )).status_code == 404
    assert (await client.post(
        f"/api/v1/blueprints/{bp_id}/install",
        json={"share_token": token}, headers=other_headers,
    )).status_code == 404

    rotated = await client.post(
        f"/api/v1/blueprints/{bp_id}/share-token", headers=owner_headers,
    )
    assert rotated.status_code == 200, rotated.text
    rotated_token = rotated.json()["share_token"]
    assert rotated_token != token
    assert (await client.get(
        f"/api/v1/blueprints/shared/{token}", headers=allowed_headers,
    )).status_code == 404
    assert (await client.get(
        f"/api/v1/blueprints/shared/{rotated_token}", headers=allowed_headers,
    )).status_code == 200
    assert (await client.get(
        f"/api/v1/blueprints/shared/{rotated_token}", headers=other_headers,
    )).status_code == 404

    allowed_user.verified_email = None
    allowed_user.email_verified_at = None
    await db_session.commit()
    assert (await client.get(
        f"/api/v1/blueprints/shared/{rotated_token}", headers=allowed_headers,
    )).status_code == 404

    revoked = await client.delete(
        f"/api/v1/blueprints/{bp_id}/share-token", headers=owner_headers,
    )
    assert revoked.status_code == 204
    owner_detail = await client.get(f"/api/v1/blueprints/{bp_id}", headers=owner_headers)
    assert owner_detail.json()["has_share_token"] is False
    assert owner_detail.json()["share_allowed_emails"] == []


async def test_share_token_owner_only(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers_a, entity_a = await _register(client, "share_owner2")
    headers_b, _ = await _register(client, "share_other2")
    bp_id = await _make_blueprint(db_session, entity_a, "share-pub", status="published")

    r = await client.post(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers_b)
    assert r.status_code in (403, 404)
    r = await client.delete(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers_b)
    assert r.status_code in (403, 404)


@pytest.mark.parametrize("status", ["published", "archived", "pending_review"])
async def test_share_token_resolves_any_status(
    client: AsyncClient, db_session, monkeypatch, status,
):
    """A valid token resolves the blueprint regardless of its status."""
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, f"share_st_{status}")
    viewer_headers, _ = await _register(client, f"share_stv_{status}")
    bp_id = await _make_blueprint(db_session, entity_id, f"share-{status}", status=status)

    r = await client.post(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers)
    assert r.status_code == 200, r.text
    token = r.json()["share_token"]

    r = await client.get(f"/api/v1/blueprints/shared/{token}", headers=viewer_headers)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == bp_id


async def test_has_share_token_masked_for_non_owner(
    client: AsyncClient, db_session, monkeypatch,
):
    """share-token existence is owner-only metadata — non-owners always see
    has_share_token=False, even on published blueprints that have one."""
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "mask_owner")
    other_headers, _ = await _register(client, "mask_viewer")
    bp_id = await _make_blueprint(db_session, entity_id, "mask-bp", status="published")

    r = await client.post(
        f"/api/v1/blueprints/{bp_id}/share-token", headers=headers,
        json={"allowed_emails": ["tester@example.com"]},
    )
    assert r.status_code == 200, r.text

    # Owner sees the truth.
    r = await client.get(f"/api/v1/blueprints/{bp_id}", headers=headers)
    assert r.json()["has_share_token"] is True
    assert r.json()["share_allowed_emails"] == ["tester@example.com"]

    # Non-owner detail: masked.
    r = await client.get(f"/api/v1/blueprints/{bp_id}", headers=other_headers)
    assert r.status_code == 200
    assert r.json()["has_share_token"] is False
    assert r.json()["share_allowed_emails"] == []

    # Non-owner list: masked there too.
    r = await client.get("/api/v1/blueprints", params={"status": "published"},
                         headers=other_headers)
    assert r.status_code == 200
    entry = next(b for b in r.json() if b["id"] == bp_id)
    assert entry["has_share_token"] is False


async def test_shared_resolve_requires_auth(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "share_auth")
    bp_id = await _make_blueprint(db_session, entity_id, "share-auth-bp")
    r = await client.post(f"/api/v1/blueprints/{bp_id}/share-token", headers=headers)
    token = r.json()["share_token"]
    r = await client.get(f"/api/v1/blueprints/shared/{token}")  # no auth header
    assert r.status_code in (401, 403)
