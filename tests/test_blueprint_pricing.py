"""PUT /api/v1/blueprints/{id}/pricing — validation rules."""
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

CLOUD = [{"DEPLOYMENT_MODE": "cloud", "STRIPE_SECRET_KEY": "sk_test_x"}]


from tests.marketplace_helpers import _force_fallback_verification, _register


async def _make_blueprint(db_session, entity_id, **kw):
    from packages.core.models.blueprint import WorkspaceBlueprint
    bp = WorkspaceBlueprint(
        entity_id=entity_id, slug=kw.get("slug", "bp-price-1"), title="BP",
        payload={"manifest": {}}, payload_version="1.1",
        status=kw.get("status", "draft"), price_cents=kw.get("price_cents"),
    )
    db_session.add(bp)
    await db_session.commit()
    return bp.id


@pytest.mark.parametrize("client", CLOUD, indirect=True)
async def test_set_free_price_always_allowed(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "pricing_free")
    bp_id = await _make_blueprint(db_session, entity_id)
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 0})
    assert r.status_code == 200, r.text
    assert r.json()["price_cents"] == 0


@pytest.mark.parametrize("client", CLOUD, indirect=True)
async def test_paid_price_requires_charges_enabled(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "pricing_gate")
    bp_id = await _make_blueprint(db_session, entity_id, slug="bp-gate")

    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 4900})
    assert r.status_code == 409  # no merchant account yet

    from packages.core.models.merchant import MerchantAccount
    db_session.add(MerchantAccount(
        entity_id=entity_id, stripe_account_id="acct_pricing_1",
        charges_enabled=True, payouts_enabled=True, onboarding_status="complete",
    ))
    await db_session.commit()

    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 4900})
    assert r.status_code == 200, r.text
    assert r.json()["price_cents"] == 4900


@pytest.mark.parametrize("client", CLOUD, indirect=True)
async def test_pricing_owner_only_bounds_and_status_untouched(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers_a, entity_a = await _register(client, "pricing_owner")
    headers_b, _ = await _register(client, "pricing_intruder")
    bp_id = await _make_blueprint(db_session, entity_a, slug="bp-owner", status="published")

    # Non-owner: invisible (404) even though published — pricing is owner-scoped.
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers_b, json={"price_cents": 0})
    assert r.status_code == 404

    # Bounds: >$10,000 rejected by validation.
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers_a, json={"price_cents": 1_000_001})
    assert r.status_code == 422

    # Free price on a published blueprint does NOT knock it back to review.
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers_a, json={"price_cents": 0})
    assert r.status_code == 200
    assert r.json()["status"] == "published"


async def test_paid_pricing_rejected_in_oss(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "pricing_oss")
    bp_id = await _make_blueprint(db_session, entity_id, slug="bp-oss")
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 4900})
    assert r.status_code == 403
    # free is still fine in OSS
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 0})
    assert r.status_code == 200


@pytest.mark.parametrize("client", CLOUD, indirect=True)
async def test_price_boundary_and_paid_to_free(client: AsyncClient, db_session, monkeypatch):
    _force_fallback_verification(monkeypatch)
    headers, entity_id = await _register(client, "pricing_boundary")

    from packages.core.models.merchant import MerchantAccount
    db_session.add(MerchantAccount(
        entity_id=entity_id, stripe_account_id="acct_boundary_1",
        charges_enabled=True, payouts_enabled=True, onboarding_status="complete",
    ))
    await db_session.commit()

    bp_id = await _make_blueprint(db_session, entity_id, slug="bp-boundary")

    # Exactly $10,000 is accepted (le boundary).
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 1_000_000})
    assert r.status_code == 200
    assert r.json()["price_cents"] == 1_000_000

    # Paid -> free requires no merchant re-check; simulate revoked merchant by
    # dropping charges_enabled, then set price 0.
    from sqlalchemy import update
    from packages.core.models.merchant import MerchantAccount as MA
    await db_session.execute(update(MA).where(MA.entity_id == entity_id).values(charges_enabled=False))
    await db_session.commit()
    r = await client.put(f"/api/v1/blueprints/{bp_id}/pricing",
                         headers=headers, json={"price_cents": 0})
    assert r.status_code == 200
    assert r.json()["price_cents"] == 0
