"""Model roundtrips for marketplace tables. DB schema comes from
Base.metadata.create_all in conftest, so this also proves the models load."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.models.base import generate_ulid


@pytest.mark.asyncio
async def test_merchant_account_roundtrip(db_session):
    from packages.core.models.merchant import MerchantAccount

    entity_id = generate_ulid()
    row = MerchantAccount(
        entity_id=entity_id,
        stripe_account_id="acct_test_123",
        onboarding_status="pending",
    )
    db_session.add(row)
    await db_session.flush()

    got = (await db_session.execute(
        select(MerchantAccount).where(MerchantAccount.entity_id == entity_id)
    )).scalar_one()
    assert got.stripe_account_id == "acct_test_123"
    assert got.charges_enabled is False
    assert got.payouts_enabled is False
    assert got.onboarding_status == "pending"


@pytest.mark.asyncio
async def test_blueprint_purchase_roundtrip_and_pricing_columns(db_session):
    from packages.core.models.blueprint import WorkspaceBlueprint
    from packages.core.models.blueprint_purchase import BlueprintPurchase

    seller = generate_ulid()
    buyer = generate_ulid()
    bp = WorkspaceBlueprint(
        entity_id=seller, slug="paid-bp", title="Paid BP",
        payload={"manifest": {}}, payload_version="1.1",
        price_cents=4900, currency="usd",
    )
    db_session.add(bp)
    await db_session.flush()
    assert bp.purchase_count == 0
    assert bp.share_token is None

    purchase = BlueprintPurchase(
        blueprint_id=bp.id,
        buyer_entity_id=buyer,
        buyer_user_id=generate_ulid(),
        amount_cents=4900,
        currency="usd",
        platform_fee_cents=0,
        seller_amount_cents=4900,
        payload_snapshot=bp.payload,
        blueprint_title=bp.title,
        stripe_checkout_session_id="cs_test_abc",
    )
    db_session.add(purchase)
    await db_session.flush()

    got = (await db_session.execute(
        select(BlueprintPurchase).where(BlueprintPurchase.blueprint_id == bp.id)
    )).scalar_one()
    assert got.status == "pending"
    assert got.payload_snapshot == {"manifest": {}}


@pytest.mark.asyncio
async def test_order_blueprint_column(db_session):
    from packages.core.models.billing import Order

    order = Order(
        entity_id=generate_ulid(),
        order_type="blueprint",
        blueprint_id=generate_ulid(),
        amount=4900,
        status="paid",
    )
    db_session.add(order)
    await db_session.flush()
    assert order.blueprint_id is not None


def _make_blueprint(WorkspaceBlueprint, **overrides):
    defaults = dict(
        entity_id=generate_ulid(),
        slug=f"bp-{generate_ulid().lower()}",
        title="Constraint BP",
        payload={"manifest": {}},
        payload_version="1.1",
    )
    defaults.update(overrides)
    return WorkspaceBlueprint(**defaults)


@pytest.mark.asyncio
async def test_live_entitlement_partial_unique_index(db_session):
    """ux_blueprint_purchases_live_entitlement: one non-refunded purchase per
    (blueprint, buyer entity); a refunded row frees the slot."""
    from packages.core.models.blueprint import WorkspaceBlueprint
    from packages.core.models.blueprint_purchase import BlueprintPurchase

    bp = _make_blueprint(WorkspaceBlueprint)
    db_session.add(bp)
    await db_session.flush()

    buyer = generate_ulid()

    def make_purchase():
        return BlueprintPurchase(
            blueprint_id=bp.id,
            buyer_entity_id=buyer,
            buyer_user_id=generate_ulid(),
            amount_cents=4900,
            seller_amount_cents=4900,
            payload_snapshot={"manifest": {}},
            blueprint_title=bp.title,
        )

    first = make_purchase()  # status defaults to 'pending'
    db_session.add(first)
    await db_session.flush()

    # Duplicate while first is 'pending' → blocked.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_purchase())
            await db_session.flush()

    # Duplicate while first is 'completed' → blocked.
    first.status = "completed"
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_purchase())
            await db_session.flush()

    # Refund the first purchase (commit so the state is durable), then a
    # new purchase for the same (blueprint, buyer) must succeed.
    first.status = "refunded"
    await db_session.commit()

    second = make_purchase()
    db_session.add(second)
    await db_session.flush()
    assert second.id != first.id
    assert second.status == "pending"


@pytest.mark.asyncio
async def test_share_token_partial_unique_index(db_session):
    """ux_workspace_blueprints_share_token: tokens are unique, but any number
    of blueprints may have share_token=None."""
    from packages.core.models.blueprint import WorkspaceBlueprint

    # Two blueprints without a share token coexist fine.
    bp_a = _make_blueprint(WorkspaceBlueprint)
    bp_b = _make_blueprint(WorkspaceBlueprint)
    assert bp_a.share_token is None and bp_b.share_token is None
    db_session.add_all([bp_a, bp_b])
    await db_session.flush()

    token = f"tok_{generate_ulid().lower()}"
    bp_c = _make_blueprint(WorkspaceBlueprint, share_token=token)
    db_session.add(bp_c)
    await db_session.flush()

    # Same token on a second blueprint → blocked.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_make_blueprint(WorkspaceBlueprint, share_token=token))
            await db_session.flush()
