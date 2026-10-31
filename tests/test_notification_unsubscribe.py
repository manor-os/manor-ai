"""Per-kind email unsubscribe links: token binding and preference effect."""
from __future__ import annotations

import jwt
import pytest
from sqlalchemy import delete

from packages.core.config import get_settings
from packages.core.constants import notification_types
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User
from packages.core.services import notification_unsubscribe as unsub
from packages.core.services.notification_routing import select_channels
from packages.core.services.signed_email_links import email_hash

KIND = "creator_update_test"


@pytest.fixture(autouse=True)
def email_default_kind(monkeypatch):
    monkeypatch.setitem(notification_types.EVENTS_BY_KIND, KIND, {
        "kind": KIND, "category": "system", "severity": "info",
        "label": "Creator update", "description": "x", "default_channels": ["email"],
    })


@pytest.fixture
async def reader(db_session):
    entity = Entity(id=generate_ulid(), name="Reader")
    user = User(id=generate_ulid(), entity_id=entity.id, email=f"{generate_ulid()}@example.com",
                display_name="Reader", password_hash="unused", preferences={})
    db_session.add_all([entity, user])
    await db_session.commit()
    yield user
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.execute(delete(Entity).where(Entity.id == entity.id))
    await db_session.commit()


@pytest.mark.asyncio
async def test_link_turns_off_email_for_that_kind_only(client, db_session, reader):
    token = unsub.create_unsubscribe_token(reader, KIND)
    page = await client.get(f"/api/v1/notifications/unsubscribe/{token}")
    assert page.status_code == 200 and "<form" in page.text
    await db_session.refresh(reader)
    assert unsub.email_enabled_for_kind(reader.preferences, KIND)  # the GET changed nothing
    done = await client.post(f"/api/v1/notifications/unsubscribe/{token}", content="List-Unsubscribe=One-Click")
    assert done.status_code == 200
    await db_session.refresh(reader)
    assert not unsub.email_enabled_for_kind(reader.preferences, KIND)
    by_kind = reader.preferences["notifications"]["by_kind"]
    assert by_kind[KIND]["channels"] == ["inapp"]
    assert set(by_kind) == {KIND}
    again = await client.get(f"/api/v1/notifications/unsubscribe/{token}")
    assert "<form" not in again.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["signature", "type", "kind", "email_changed"])
async def test_invalid_links_change_nothing(client, db_session, reader, tamper):
    settings = get_settings()
    claims = {"typ": unsub.TOKEN_TYPE, "sub": reader.id, "kind": KIND, "email_hash": email_hash(reader.email)}
    key = settings.JWT_SECRET_KEY
    if tamper == "signature":
        key = "not-the-secret"
    if tamper == "type":
        claims["typ"] = "newsletter_unsubscribe"
    if tamper == "kind":
        claims["kind"] = "not_a_catalog_kind"
    token = jwt.encode(claims, key, algorithm=settings.JWT_ALGORITHM)
    if tamper == "email_changed":
        reader.email = f"{generate_ulid()}@example.com"
        await db_session.commit()
    response = await client.post(f"/api/v1/notifications/unsubscribe/{token}")
    assert response.status_code == 400
    await db_session.refresh(reader)
    assert unsub.email_enabled_for_kind(reader.preferences, KIND)


@pytest.fixture
def no_default_kind(monkeypatch):
    kind = "creator_update_no_default"
    monkeypatch.setitem(notification_types.EVENTS_BY_KIND, kind, {
        "kind": kind, "category": "system", "severity": "info", "label": "Plain", "description": "x",
    })
    return kind


@pytest.mark.asyncio
async def test_unsubscribe_writes_override_even_when_user_level_email_is_absent(db_session, reader, no_default_kind):
    # Email can still arrive through a workspace/entity route, which a per-kind
    # override outranks, so the override must be written although the user-level
    # channels never listed email. Existing override fields are preserved.
    reader.preferences = {"notifications": {"by_kind": {no_default_kind: {"enabled": True, "bypass_quiet_hours": True}}}}
    await db_session.commit()
    assert not unsub.email_explicitly_disabled(reader.preferences, no_default_kind)
    assert await unsub.unsubscribe_email(db_session, reader, no_default_kind) is True
    await db_session.commit()
    await db_session.refresh(reader)
    override = reader.preferences["notifications"]["by_kind"][no_default_kind]
    assert override == {"enabled": True, "bypass_quiet_hours": True, "channels": ["inapp"]}
    assert unsub.email_explicitly_disabled(reader.preferences, no_default_kind)
    workspace_policy = {"notification_policy": {"routes": {no_default_kind: ["email"]}}}
    assert "email" in select_channels(
        kind=no_default_kind, severity="info", user_prefs={}, workspace_settings=workspace_policy,
    )
    assert "email" not in select_channels(
        kind=no_default_kind, severity="info", user_prefs=reader.preferences, workspace_settings=workspace_policy,
    )
    assert await unsub.unsubscribe_email(db_session, reader, no_default_kind) is False


@pytest.mark.parametrize("override, disabled", [
    ({"enabled": False}, True),
    ({"channels": ["inapp", "telegram"]}, True),
    ({"channels": ["inapp", "email"]}, False),
    ({"channels": []}, False),
    ({"enabled": True}, False),
    (None, False),
])
def test_email_explicitly_disabled_rule(override, disabled):
    prefs = {} if override is None else {"notifications": {"by_kind": {KIND: override}}}
    assert unsub.email_explicitly_disabled(prefs, KIND) is disabled
