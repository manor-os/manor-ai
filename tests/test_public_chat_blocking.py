"""Channel visitor blocks survive new sessions without affecting other visitors."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from apps.api.routers import public_chat
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, ChannelContact


@pytest.mark.parametrize("claim", ["user_id", "verified_customer_user_id"])
async def test_blocked_identity_cannot_reset_or_replace_its_session(db_session, monkeypatch, claim):
    cc = ChannelConfig(entity_id=generate_ulid(), channel_type="webchat", provider="webchat", config={})
    db_session.add(cc)
    await db_session.flush()
    user = SimpleNamespace(
        id=generate_ulid(), entity_id=cc.entity_id if claim == "user_id" else generate_ulid(),
        role="external", display_name="Visitor", first_name="", last_name="", email="visitor@example.test",
    )
    blocked = ChannelContact(
        entity_id=cc.entity_id, channel_config_id=cc.id, channel_type="webchat",
        source_id="blocked-session", status="blocked",
        user_id=user.id if claim == "user_id" else None,
        profile={"verified_customer_user_id": user.id} if claim != "user_id" else {},
    )
    active = ChannelContact(
        entity_id=cc.entity_id, channel_config_id=cc.id, channel_type="webchat",
        source_id="existing-active-session", status="active", profile={},
    )
    db_session.add_all([blocked, active])
    await db_session.flush()
    binding = SimpleNamespace(agent_id=None, user_id=None, workspace_id=None, agent_subscription_id=None, config={})
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(cc, binding)))
    monkeypatch.setattr(public_chat, "_optional_current_user", AsyncMock(return_value=user))
    app = FastAPI()
    app.include_router(public_chat.router)

    async def test_db():
        yield db_session

    app.dependency_overrides[get_db] = test_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for payload in (
            {"session_id": blocked.source_id}, {}, {"session_id": "new-session"},
            {"session_id": active.source_id},
        ):
            response = await client.post("/api/v1/public/chat/token/session", json=payload)
            assert response.status_code == 403, payload
            assert response.json()["detail"]["code"] == "visitor_blocked"
        count = (await db_session.execute(select(func.count()).select_from(ChannelContact).where(
            ChannelContact.channel_config_id == cc.id,
        ))).scalar_one()
        assert count == 2

        # Membership changes must not lose a block stored under either claim type.
        user.entity_id = generate_ulid() if user.entity_id == cc.entity_id else cc.entity_id
        assert (await client.post("/api/v1/public/chat/token/session", json={})).status_code == 403

        # Losing authentication must not turn a known block into a session reset.
        monkeypatch.setattr(public_chat, "_optional_current_user", AsyncMock(return_value=None))
        anonymous = await client.post("/api/v1/public/chat/token/session", json={"session_id": blocked.source_id})
        assert anonymous.status_code == 403
        assert anonymous.json()["detail"]["code"] == "visitor_blocked"

        # A different customer can replace a session left by its blocked owner.
        other = SimpleNamespace(**{**vars(user), "id": generate_ulid(), "email": "other@example.test"})
        monkeypatch.setattr(public_chat, "_optional_current_user", AsyncMock(return_value=other))
        mismatch = await client.post("/api/v1/public/chat/token/session", json={"session_id": blocked.source_id})
        assert mismatch.status_code == 403
        assert mismatch.json()["detail"]["code"] == "session_mismatch"
        assert (await client.post("/api/v1/public/chat/token/session", json={})).status_code == 200

        # Unblocking takes effect without issuing a new user identity or token.
        blocked.status = "active"
        await db_session.flush()
        monkeypatch.setattr(public_chat, "_optional_current_user", AsyncMock(return_value=user))
        assert (await client.post("/api/v1/public/chat/token/session", json={})).status_code == 200


async def test_blocks_do_not_cross_channel_or_entity_boundaries(db_session):
    from packages.core.services.channel_contacts import find_claimed_webchat_contact_for_user

    entity_id, channel_id, user_id = generate_ulid(), generate_ulid(), generate_ulid()
    user = SimpleNamespace(id=user_id, entity_id=entity_id)
    db_session.add_all([
        ChannelContact(entity_id=entity_id, channel_config_id=generate_ulid(), channel_type="webchat", source_id="other-channel", status="blocked", user_id=user_id),
        ChannelContact(entity_id=generate_ulid(), channel_config_id=channel_id, channel_type="webchat", source_id="other-entity", status="blocked", user_id=user_id),
        ChannelContact(entity_id=entity_id, channel_config_id=channel_id, channel_type="webchat", source_id="other-user", status="blocked", user_id=generate_ulid()),
    ])
    await db_session.flush()
    cc = SimpleNamespace(id=channel_id, entity_id=entity_id)
    assert await find_claimed_webchat_contact_for_user(db_session, cc=cc, user=user, status="blocked") is None
