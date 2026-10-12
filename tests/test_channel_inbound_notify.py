"""End-to-end: external channel inbound triggers a "new message" ping
to staff via their bound channel, with per-conversation throttling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.document import Channel
from packages.core.models.notification import Notification
from packages.core.models.workspace import Workspace
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter
from tests.test_document_permissions import _create_entity_user
from packages.core.governance.approval_policy_service import initialize_workspace_approval
from tests.approval_settings_test_utils import set_workspace_test_approval


# ── Fakes ───────────────────────────────────────────────────────────────────


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


@pytest.fixture
def stub_agent(monkeypatch):
    """Avoid hitting the real LLM. The inbound notify path runs *before*
    the agent in dispatch_inbound, but the agent still gets called when
    the matcher / inbound notification flow doesn't short-circuit."""
    from packages.core.services import channel_gateway

    async def _fake_run_agent(**kwargs):
        return channel_gateway.ChannelAgentRunResult(content="Thanks, looking into it.")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _fake_run_agent)


# ── Helpers ────────────────────────────────────────────────────────────────


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


async def _seed_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    inbound_recipients: list[str] | None = None,
) -> Workspace:
    settings: dict[str, Any] = {}
    if inbound_recipients is not None:
        settings = {
            "notification_policy": {
                "inbound_notify_user_ids": inbound_recipients,
            },
        }
    ws = Workspace(
        entity_id=entity_id,
        name="WS",
        status="active",
        settings=settings,
    )
    db.add(ws)
    await db.flush()
    await initialize_workspace_approval(db, ws)
    await db.commit()
    return ws


async def _seed_cc(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    owner_user_id: str,
) -> ChannelConfig:
    cc = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        channel_type="telegram",
        provider="telegram_bot",
        config={},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.flush()
    # This fixture exercises notification/history, with consent limited to
    # replies through the one channel configuration created above.
    await set_workspace_test_approval(db, workspace_id=workspace_id, entity_id=entity_id,
        actor_id=owner_user_id, user_rules=[{"effect": "allow", "selector": {
            "kind": "resource", "action_key": "external_message.send",
            "account_id": None, "resource_kind": "resource", "resource_ids": [cc.id],
        }}])
    await db.commit()
    return cc


async def _seed_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str,
    cc: ChannelConfig,
) -> Channel:
    binding = Channel(
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        type="telegram",
        name="Binding",
        status="active",
        config={"channel_config_id": cc.id},
    )
    db.add(binding)
    await db.commit()
    return binding


async def _seed_contact(
    db: AsyncSession,
    *,
    entity_id: str,
    cc: ChannelConfig,
    source_id: str,
    user_id: str | None = None,
    role: str = "external",
) -> ChannelContact:
    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id=source_id,
        display_name=source_id,
        user_id=user_id,
        role=role,
        status="active",
        last_seen_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    await db.commit()
    return contact


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_inbound_notifies_workspace_owner(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Customer writes in via Telegram → workspace owner (entity owner
    fallback) gets a "new message" notification on their bound channel."""
    owner = await _register(client, "inbound_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )

    # Owner has their own Telegram contact + opted into telegram pushes
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_1",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_1",
        user_id=None,
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Curious Customer",
        chat_id=customer_contact.source_id,
        content="Hi, do you have availability next weekend?",
    )

    # In-app notification row exists for the owner
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert "Curious Customer" in (rows[0].title or "")
    assert "next weekend" in (rows[0].content or "")
    meta = rows[0].meta or {}
    assert meta.get("channel_type") == "telegram"
    assert meta.get("channel_contact_id") == customer_contact.id

    # And a Telegram push landed on the owner's chat, not the customer's
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    customer_pushes = [s for s in fake_telegram.sent if s["to"] == customer_contact.source_id]
    assert len(owner_pushes) == 1
    assert "Curious Customer" in owner_pushes[0]["text"]
    # The customer also gets the agent's stubbed reply
    assert len(customer_pushes) == 1
    assert "looking into it" in customer_pushes[0]["text"]


@pytest.mark.asyncio
async def test_external_inbound_defaults_to_channel_owner_only(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """A user-owned channel must not alert other admins in the Entity."""
    owner = await _register(client, "inbound_private_owner")
    other_admin = await _create_entity_user(
        owner["entity_id"], "inbound_private_admin", role="admin",
    )
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_private_owner",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_private_customer",
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Private Customer",
        chat_id=customer_contact.source_id,
        content="private ping",
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.user_id for row in rows} == {owner["user_id"]}
    assert other_admin["id"] not in {row.user_id for row in rows}
    assert [s["to"] for s in fake_telegram.sent].count(owner_contact.source_id) == 1


@pytest.mark.asyncio
async def test_followup_inbound_within_window_is_throttled(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """A second inbound on the same conversation within the throttle
    window should NOT generate a second notification — only the first
    customer turn pings the operator."""
    owner = await _register(client, "throttle_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_2",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_2",
        user_id=None,
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="first ping",
    )
    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="second ping",
    )

    # Exactly one notification (throttled second time)
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1

    # Owner's Telegram got exactly one heads-up push (the customer still
    # gets their two agent replies — those are unrelated)
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert len(owner_pushes) == 1


@pytest.mark.asyncio
async def test_internal_member_inbound_does_not_notify(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """When a workspace member (role != "external") writes in via their
    own bound channel — e.g. testing the bot — no "new customer message"
    notification should fire. They're not a customer."""
    owner = await _register(client, "internal_inbound_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    # Owner has their own contact + role=admin (NOT external)
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_3",
        user_id=owner["user_id"],
        role="admin",
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=owner_contact.source_id,
        sender_name="Owner Self-Test",
        chat_id=owner_contact.source_id,
        content="hello bot",
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_workspace_inbound_recipients_override(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    stub_agent,
):
    """Explicit empty inbound_notify_user_ids suppresses all pings; the
    in-app row + bot reply still go through."""
    owner = await _register(client, "inbound_suppress_owner")
    ws = await _seed_workspace(
        db_session,
        entity_id=owner["entity_id"],
        inbound_recipients=[],
    )
    cc = await _seed_cc(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session,
        entity_id=owner["entity_id"],
        workspace_id=ws.id,
        user_id=owner["user_id"],
        cc=cc,
    )
    owner_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_owner_4",
        user_id=owner["user_id"],
        role="admin",
    )
    customer_contact = await _seed_contact(
        db_session,
        entity_id=owner["entity_id"],
        cc=cc,
        source_id="tg_customer_4",
        user_id=None,
        role="external",
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=owner["headers"],
        json={"default_channels": ["telegram"]},
    )

    from packages.core.services.channel_gateway import dispatch_inbound

    await dispatch_inbound(
        entity_id=owner["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=customer_contact.source_id,
        sender_name="Customer",
        chat_id=customer_contact.source_id,
        content="hello",
    )

    # No inbound-message notification row created for owner
    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == owner["user_id"],
                    Notification.type == "channel_inbound_message",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    # No owner-targeted telegram push (only the customer's agent reply went out)
    owner_pushes = [s for s in fake_telegram.sent if s["to"] == owner_contact.source_id]
    assert owner_pushes == []


@pytest.mark.asyncio
async def test_inbound_turn_is_not_repeated_in_agent_history(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    monkeypatch,
):
    """The agent loop appends the current message itself; history stops before it."""
    from packages.core.services import channel_gateway

    calls: list[dict[str, Any]] = []

    async def _record_agent(**kwargs):
        calls.append(kwargs)
        return channel_gateway.ChannelAgentRunResult(content=f"reply {len(calls)}")

    monkeypatch.setattr(channel_gateway, "run_channel_agent_turn", _record_agent)
    owner = await _register(client, "history_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(
        db_session, entity_id=owner["entity_id"], workspace_id=ws.id, owner_user_id=owner["user_id"],
    )
    await _seed_binding(
        db_session, entity_id=owner["entity_id"], workspace_id=ws.id, user_id=owner["user_id"], cc=cc,
    )
    customer = await _seed_contact(
        db_session, entity_id=owner["entity_id"], cc=cc, source_id="tg_history_customer",
    )

    for content in ("first ping", "second ping"):
        await channel_gateway.dispatch_inbound(
            entity_id=owner["entity_id"],
            channel_config_id=cc.id,
            channel_type="telegram",
            sender_id=customer.source_id,
            sender_name="Customer",
            chat_id=customer.source_id,
            content=content,
        )

    assert [call["current_message"] for call in calls] == ["first ping", "second ping"]
    assert calls[0]["history"] == []
    assert calls[1]["history"] == [
        {"role": "user", "content": "first ping"},
        {"role": "assistant", "content": "reply 1"},
    ]


@pytest.mark.asyncio
async def test_channel_agent_sees_its_previous_tool_output_on_the_next_inbound(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core.services import channel_agent_runtime, workspace_runtime
    from packages.core.services.channel_gateway import dispatch_inbound

    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_order", "type": "function", "function": {"name": "lookup_order", "arguments": "{\"id\": \"A7\"}"}},
    ]}
    output = {"role": "tool", "tool_call_id": "call_order", "content": "Order A7: shipped 2026-09-30 via UPS 1Z999"}
    seen: list[dict] = []

    async def resolve_runtime(*_args, **kwargs):
        return SimpleNamespace(
            tool_profile="workspace_agent", workspace_id=kwargs.get("workspace_id"), task_id=None,
            thread_ref_kind=None, thread_ref_id=None, bound_tool_names=None, is_master=False,
            mcp_allowed_names=None, extra_context="",
        )

    async def prepare_appendix(*_args, **_kwargs):
        return SimpleNamespace(tool_schemas=[], allowed_tool_names=set(), envelope=SimpleNamespace(metadata={}))

    async def execute_loop(**kwargs):
        seen.append(kwargs)
        reply = f"reply {len(seen)}"
        return SimpleNamespace(content=reply, stop_reason="end_turn", messages=[
            {"role": "system", "content": kwargs["system_prompt"]}, *(kwargs["initial_messages"] or []),
            {"role": "user", "content": kwargs["user_message"]}, call, output, {"role": "assistant", "content": reply},
        ])

    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", resolve_runtime)
    monkeypatch.setattr(channel_agent_runtime, "resolve_channel_base_prompt", AsyncMock(return_value="base"))
    monkeypatch.setattr(channel_agent_runtime, "runtime_prepare_prompt_appendix_for_turn", prepare_appendix)
    monkeypatch.setattr(channel_agent_runtime, "runtime_execute_channel_agent_loop", execute_loop)

    owner = await _register(client, "transcript_channel_owner")
    ws = await _seed_workspace(db_session, entity_id=owner["entity_id"])
    cc = await _seed_cc(db_session, entity_id=owner["entity_id"], workspace_id=ws.id, owner_user_id=owner["user_id"])
    await _seed_binding(db_session, entity_id=owner["entity_id"], workspace_id=ws.id, user_id=owner["user_id"], cc=cc)
    customer = await _seed_contact(db_session, entity_id=owner["entity_id"], cc=cc, source_id="tg_transcript_customer")

    for content in ("where is order A7?", "which carrier?"):
        result = await dispatch_inbound(
            entity_id=owner["entity_id"], channel_config_id=cc.id, channel_type="telegram",
            sender_id=customer.source_id, sender_name="Customer", chat_id=customer.source_id, content=content,
        )
        assert result["status"] == "ok", result

    assert seen[0]["initial_messages"] is None
    follow_up = seen[1]["initial_messages"]
    assert call in follow_up and output in follow_up
    assert [m["content"] for m in follow_up if m["role"] == "user"] == ["where is order A7?"]
    assert seen[1]["carry_previous_checkpoint"] is False
