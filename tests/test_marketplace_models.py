"""Model roundtrips for marketplace tables. DB schema comes from
Base.metadata.create_all in conftest, so this also proves the models load."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from packages.core.models.base import generate_ulid


def test_blueprint_delivery_source_uses_current_published_release():
    from packages.core.services.marketplace_billing import blueprint_delivery_source

    blueprint = SimpleNamespace(
        status="published",
        payload={"release": "current"},
        content_version="2.0.0",
    )
    purchase = SimpleNamespace(
        payload_snapshot={"release": "purchased"},
        blueprint_content_version="1.0.0",
    )

    assert blueprint_delivery_source(blueprint, purchase) == (
        {"release": "current"},
        "2.0.0",
    )


def test_blueprint_delivery_source_falls_back_when_release_is_archived():
    from packages.core.services.marketplace_billing import blueprint_delivery_source

    blueprint = SimpleNamespace(
        status="archived",
        payload={"release": "unpublished-edit"},
        content_version="2.0.0",
    )
    purchase = SimpleNamespace(
        payload_snapshot={"release": "purchased"},
        blueprint_content_version="1.0.0",
    )

    assert blueprint_delivery_source(blueprint, purchase) == (
        {"release": "purchased"},
        "1.0.0",
    )


def test_blueprint_delivery_paid_status_follows_the_delivered_release(monkeypatch):
    from packages.core.services.marketplace_billing import (
        blueprint_delivery_is_paid,
    )

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    purchase = SimpleNamespace(amount_cents=4900)
    free_published_release = SimpleNamespace(status="published", price_cents=0)
    archived_paid_release = SimpleNamespace(status="archived", price_cents=0)

    assert not blueprint_delivery_is_paid(
        free_published_release,
        purchase,
    )
    assert blueprint_delivery_is_paid(
        archived_paid_release,
        purchase,
    )


def test_blueprint_delivery_paid_status_stays_disabled_in_oss(monkeypatch):
    from packages.core.services.marketplace_billing import (
        blueprint_delivery_is_paid,
    )

    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    paid_release = SimpleNamespace(status="published", price_cents=4900)

    assert not blueprint_delivery_is_paid(paid_release, None)




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
