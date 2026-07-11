"""End-to-end tests for the agent-callable notification tools.

Exercises the tools the way ``tool_pool.execute`` does: handlers receive
``entity_id`` + extra context kwargs straight from positional/keyword
injection, and return a JSON string. We assert side effects (Notification
rows + channel pushes via the recorded adapter) rather than parsing the
return strings — that's the contract the agent's downstream tool-loop
relies on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.tools.notification_tools import (
    _find_team_members,
    _notify_user,
)
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.notification import Notification
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


class _RecordingAdapter(ChannelAdapter):
    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_telegram():
    fake = _RecordingAdapter()
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = fake  # type: ignore[assignment]
    yield fake
    if original is None:
        ADAPTERS.pop("telegram", None)
    else:
        ADAPTERS["telegram"] = original


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"Org {username}",
        },
    )
    body = resp.json()
    return {
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "user_id": body["user_id"],
        "entity_id": body["entity_id"],
    }


async def _link_telegram(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    source_id: str = "tg_target_1",
) -> tuple[ChannelConfig, ChannelContact]:
    cc = ChannelConfig(
        entity_id=entity_id,
        channel_type="telegram",
        provider="telegram_bot",
        config={},
        credentials={"bot_token": "t"},
        status="active",
    )
    db.add(cc)
    await db.flush()
    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id=source_id,
        display_name=source_id,
        user_id=user_id,
        role="member",
        status="active",
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    await db.commit()
    return cc, contact


# ── notify_user ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_user_writes_in_app_row(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    """Basic happy path — agent calls notify_user, recipient gets in-app
    row + telegram push (when they opted into telegram)."""
    sender = await _register(client, "agent_sender")
    target = await _register(client, "agent_target")
    # Force target into sender's entity (auth.register makes a new entity
    # per user) — for the test we manually move the target row over.
    from packages.core.models.user import User

    target_user = (await db_session.execute(select(User).where(User.id == target["user_id"]))).scalar_one()
    target_user.entity_id = sender["entity_id"]
    await db_session.commit()

    cc, contact = await _link_telegram(
        db_session,
        entity_id=sender["entity_id"],
        user_id=target["user_id"],
    )
    # Target user opts into telegram via the API
    await client.put(
        "/api/v1/notifications/preferences",
        headers=target["headers"],
        json={"default_channels": ["telegram"]},
    )

    raw = await _notify_user(
        entity_id=sender["entity_id"],
        user_id=target["user_id"],
        title="Your report is ready",
        body="Open the dashboard to see the numbers.",
        link="/dashboards/weekly",
        severity="info",
        kind="task_succeeded",
        _agent_id_from_context="agent-123",
    )
    result = json.loads(raw)
    assert result["ok"] is True, result
    assert result["user_id"] == target["user_id"]

    # In-app row
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == target["user_id"],
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].title == "Your report is ready"
    assert rows[0].type == "task_succeeded"
    # agent_id surfaced into meta for audit
    assert (rows[0].meta or {}).get("sent_by_agent") == "agent-123"

    # Telegram push reached the target's bound contact
    assert [s["to"] for s in fake_telegram.sent] == [contact.source_id]
    text = fake_telegram.sent[0]["text"]
    assert "Your report is ready" in text
    assert "Open the dashboard" in text
    assert "/dashboards/weekly" in text


@pytest.mark.asyncio
async def test_notify_user_refuses_cross_entity_recipient(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    """The recipient must share an entity with the calling agent —
    otherwise the tool returns ok=False without touching the DB or
    channel adapters."""
    sender = await _register(client, "agent_sender_b")
    other = await _register(client, "agent_other_entity")
    # other lives in a different entity; do NOT move them

    raw = await _notify_user(
        entity_id=sender["entity_id"],
        user_id=other["user_id"],
        title="Hi",
    )
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error"] == "user_not_found_or_not_in_entity"

    # No fan-out happened
    rows = (
        (await db_session.execute(select(Notification).where(Notification.user_id == other["user_id"]))).scalars().all()
    )
    assert rows == []
    assert fake_telegram.sent == []


@pytest.mark.asyncio
async def test_notify_user_validates_inputs(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    sender = await _register(client, "agent_sender_c")
    raw = await _notify_user(
        entity_id=sender["entity_id"],
        user_id="",
        title="",
    )
    result = json.loads(raw)
    assert result["ok"] is False
    assert "user_id" in result["error"]

    raw2 = await _notify_user(
        entity_id=sender["entity_id"],
        user_id=sender["user_id"],
        title="",
    )
    result2 = json.loads(raw2)
    assert result2["ok"] is False
    assert "title" in result2["error"]


@pytest.mark.asyncio
async def test_notify_user_clamps_unknown_severity(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    sender = await _register(client, "agent_sender_d")
    raw = await _notify_user(
        entity_id=sender["entity_id"],
        user_id=sender["user_id"],
        title="hello self",
        severity="bogus",
    )
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["severity"] == "info"


# ── find_team_members ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_team_members_returns_active_users_in_entity(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Without a query, returns the entity's active members capped at
    limit. Excludes users in other entities."""
    owner = await _register(client, "team_owner")
    # Manually create a second user in the same entity
    from packages.core.models.user import User
    from packages.core.models.base import generate_ulid

    teammate = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="teammate@test.com",
        password_hash="x",
        role="member",
        status="active",
        display_name="Team Mate",
    )
    db_session.add(teammate)
    # Also a user in a different entity — should NOT appear in results
    outsider = User(
        id=generate_ulid(),
        entity_id="01OUTSIDER000000000000000",
        email="outsider@test.com",
        password_hash="x",
        role="member",
        status="active",
    )
    db_session.add(outsider)
    await db_session.commit()

    raw = await _find_team_members(entity_id=owner["entity_id"])
    result = json.loads(raw)
    assert result["ok"] is True
    ids = [m["user_id"] for m in result["members"]]
    assert owner["user_id"] in ids
    assert teammate.id in ids
    assert outsider.id not in ids


@pytest.mark.asyncio
async def test_find_team_members_query_filters_by_substring(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Case-insensitive substring match across display_name / username
    / email."""
    owner = await _register(client, "search_owner")
    from packages.core.models.user import User
    from packages.core.models.base import generate_ulid

    alice = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="alice@biz.com",
        password_hash="x",
        role="member",
        status="active",
        display_name="Alice Wonderland",
        first_name="Alice",
    )
    bob = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="robert@biz.com",
        password_hash="x",
        role="member",
        status="active",
        display_name="Bob Builder",
        first_name="Bob",
    )
    db_session.add_all([alice, bob])
    await db_session.commit()

    raw = await _find_team_members(
        entity_id=owner["entity_id"],
        query="alice",
    )
    result = json.loads(raw)
    ids = {m["user_id"] for m in result["members"]}
    assert alice.id in ids
    assert bob.id not in ids

    # Email match
    raw2 = await _find_team_members(
        entity_id=owner["entity_id"],
        query="robert",
    )
    result2 = json.loads(raw2)
    ids2 = {m["user_id"] for m in result2["members"]}
    assert bob.id in ids2
    assert alice.id not in ids2


# ── Agent loop integration smoke ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_registered_in_pool(client: AsyncClient):
    """tool_pool.initialize() should pick up the new module so the
    agent's discoverable tool list actually contains them."""
    from packages.core.ai.tool_pool import tool_pool

    tool_pool.initialize()
    assert tool_pool.get_schema("notify_user") is not None
    assert tool_pool.get_schema("find_team_members") is not None
