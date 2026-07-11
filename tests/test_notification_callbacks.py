"""End-to-end tests for actionable notifications + channel reply callbacks.

The flow under test:

  notify(actions=[approve|reject], callback_kind="test.hitl")
       │
       ▼  external channel fan-out + NotificationDelivery row written
  channel_gateway.dispatch_inbound  ← user replies "approve" on Telegram
       │  matches action key via notification_callbacks.match_action
       ▼  fires registered callback + acks the user
  Delivery row marked resolved
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import (
    ChannelConfig,
    ChannelContact,
)
from packages.core.models.notification import (
    NotificationDelivery,
)
from packages.core.services import notify as notify_module
from packages.core.services import notification_callbacks
from packages.core.services.channel_gateway import dispatch_inbound
from packages.core.services.channels import ADAPTERS
from packages.core.services.channels.base import ChannelAdapter


# ── Test fixtures ───────────────────────────────────────────────────────────


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
    source_id: str = "tg_test_user",
) -> tuple[ChannelConfig, ChannelContact]:
    cc = ChannelConfig(
        entity_id=entity_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Test Telegram",
        config={},
        credentials={"bot_token": "test:token"},
        status="active",
    )
    db.add(cc)
    await db.flush()

    contact = ChannelContact(
        entity_id=entity_id,
        channel_config_id=cc.id,
        channel_type="telegram",
        source_id=source_id,
        display_name="Tester",
        user_id=user_id,
        role="member",
        status="active",
    )
    db.add(contact)
    await db.commit()
    return cc, contact


async def _create_telegram_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    cc: ChannelConfig,
) -> None:
    """The channel gateway requires an active Channel binding so
    dispatch_inbound knows where to route messages. The binding for this
    test is a no-op pointer; we'll short-circuit before agent run anyway."""
    from packages.core.models.document import Channel

    db.add(
        Channel(
            entity_id=entity_id,
            workspace_id=None,
            user_id=user_id,
            type="telegram",
            name="Test binding",
            status="active",
            config={"channel_config_id": cc.id},
        )
    )
    await db.commit()


class _RecordingAdapter(ChannelAdapter):
    """ChannelAdapter that records sends; inherits the base class so we
    pick up the default ``send_actionable_message`` (text + footer)."""

    channel_type = "telegram"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, cc, to, text, **kwargs):
        self.sent.append({"cc_id": cc.id, "to": to, "text": text})
        return {"status": "sent", "external_id": f"ext-{len(self.sent)}"}

    async def parse_inbound(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_telegram(monkeypatch):
    fake = _RecordingAdapter()
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = fake  # type: ignore[assignment]
    yield fake
    if original is None:
        ADAPTERS.pop("telegram", None)
    else:
        ADAPTERS["telegram"] = original


@pytest.fixture
def hitl_callback():
    """Register a recording handler for the 'test.hitl' callback kind so
    each test gets a clean slate to assert against."""
    calls: list[dict[str, Any]] = []

    async def handler(payload, action_key, context):
        calls.append(
            {
                "payload": payload,
                "action_key": action_key,
                "context": context,
            }
        )
        return {"ok": True, "message": f"Recorded {action_key} for task {payload.get('task_id')}"}

    original = notification_callbacks.get_callback("test.hitl")
    notification_callbacks.register_callback("test.hitl", handler)
    yield calls
    if original is None:
        notification_callbacks._REGISTRY.pop("test.hitl", None)
    else:
        notification_callbacks._REGISTRY["test.hitl"] = original


@pytest.fixture
def failing_callback():
    """Variant of hitl_callback whose handler returns ok=False."""
    calls: list[dict[str, Any]] = []

    async def handler(payload, action_key, context):
        calls.append({"payload": payload, "action_key": action_key})
        return {"ok": False, "error": "task already resolved"}

    original = notification_callbacks.get_callback("test.hitl")
    notification_callbacks.register_callback("test.hitl", handler)
    yield calls
    if original is None:
        notification_callbacks._REGISTRY.pop("test.hitl", None)
    else:
        notification_callbacks._REGISTRY["test.hitl"] = original


# ── Action matcher unit checks ─────────────────────────────────────────────


def test_match_action_exact_key():
    actions = [{"key": "approve", "label": "Approve"}, {"key": "reject", "label": "Reject"}]
    assert notification_callbacks.match_action("approve", actions) == "approve"
    assert notification_callbacks.match_action("REJECT", actions) == "reject"
    assert notification_callbacks.match_action("  approve  ", actions) == "approve"


def test_match_action_label_or_synonym():
    actions = [{"key": "approve", "label": "Yes please", "synonyms": ["yep", "ok"]}]
    assert notification_callbacks.match_action("yes please", actions) == "approve"
    assert notification_callbacks.match_action("OK", actions) == "approve"
    assert notification_callbacks.match_action("nope", actions) is None


def test_match_action_numeric_pick():
    actions = [{"key": "approve"}, {"key": "reject"}]
    assert notification_callbacks.match_action("1", actions) == "approve"
    assert notification_callbacks.match_action("2", actions) == "reject"
    assert notification_callbacks.match_action("3", actions) is None
    assert notification_callbacks.match_action("0", actions) is None


def test_match_action_empty_or_no_match():
    actions = [{"key": "approve"}]
    assert notification_callbacks.match_action("", actions) is None
    assert notification_callbacks.match_action("    ", actions) is None
    assert notification_callbacks.match_action("anything", []) is None
    assert notification_callbacks.match_action("anything", None) is None


# ── Notify writes a delivery row when actions are set ───────────────────────


@pytest.mark.asyncio
async def test_notify_with_actions_writes_delivery(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    ctx = await _register(client, "actions_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve external reply?",
        body="Customer asked X. Draft: Y.",
        actions=[
            {"key": "approve", "label": "Approve"},
            {"key": "reject", "label": "Reject"},
        ],
        callback_kind="test.hitl",
        callback_payload={"task_id": "TASK-1"},
    )

    # Delivery row should exist, status=sent (adapter succeeded)
    rows = (
        (
            await db_session.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.user_id == ctx["user_id"],
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    delivery = rows[0]
    assert delivery.channel_type == "telegram"
    assert delivery.channel_contact_id == contact.id
    assert delivery.status == "sent"
    assert delivery.callback_kind == "test.hitl"
    assert delivery.callback_payload == {"task_id": "TASK-1"}
    assert delivery.actions == [
        {"key": "approve", "label": "Approve"},
        {"key": "reject", "label": "Reject"},
    ]

    # Outbound text should include the "Reply with…" footer
    assert len(fake_telegram.sent) == 1
    rendered = fake_telegram.sent[0]["text"]
    assert "Reply with:" in rendered
    assert "1. Approve" in rendered
    assert "2. Reject" in rendered


# ── Reply on the channel fires the callback ────────────────────────────────


@pytest.mark.asyncio
async def test_channel_reply_resolves_delivery_via_callback(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    hitl_callback,
):
    ctx = await _register(client, "reply_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _create_telegram_binding(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        cc=cc,
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    # 1) Send the actionable notification
    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve refund?",
        body="$50 refund request",
        actions=[
            {"key": "approve", "label": "Approve"},
            {"key": "reject", "label": "Reject"},
        ],
        callback_kind="test.hitl",
        callback_payload={"task_id": "TASK-42", "amount": 50},
    )
    fake_telegram.sent.clear()  # drop the outbound prompt — assert on the ack only

    # 2) Simulate Telegram webhook delivering the user's "approve" reply
    result = await dispatch_inbound(
        entity_id=ctx["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=contact.source_id,
        sender_name="Tester",
        chat_id=contact.source_id,
        content="approve",
    )

    # The gateway should report the delivery was resolved + not run the agent
    assert result["status"] == "delivery_resolved"
    assert result["action_key"] == "approve"
    assert result["callback_kind"] == "test.hitl"

    # The callback handler should have been invoked exactly once with
    # the producer's payload + the dispatch context.
    assert len(hitl_callback) == 1
    call = hitl_callback[0]
    assert call["action_key"] == "approve"
    assert call["payload"] == {"task_id": "TASK-42", "amount": 50}
    assert call["context"]["responder"]["source_id"] == contact.source_id
    assert call["context"]["responder"]["user_id"] == ctx["user_id"]

    # The delivery row should be marked resolved
    refresh = (
        await db_session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == ctx["user_id"],
            )
        )
    ).scalar_one()
    assert refresh.status == "resolved"
    assert refresh.resolved_action_key == "approve"
    assert refresh.resolved_at is not None

    # An ack reply should have been sent through the adapter, carrying
    # the handler's custom message.
    assert len(fake_telegram.sent) == 1
    ack_text = fake_telegram.sent[0]["text"]
    assert "Recorded approve" in ack_text
    assert "TASK-42" in ack_text


@pytest.mark.asyncio
async def test_numeric_reply_matches_action_by_position(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    hitl_callback,
):
    """A phone user typing "2" should resolve to the second action."""
    ctx = await _register(client, "numeric_reply_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _create_telegram_binding(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        cc=cc,
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve?",
        actions=[{"key": "approve"}, {"key": "reject"}],
        callback_kind="test.hitl",
        callback_payload={"task_id": "T1"},
    )
    fake_telegram.sent.clear()

    result = await dispatch_inbound(
        entity_id=ctx["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=contact.source_id,
        sender_name="Tester",
        chat_id=contact.source_id,
        content="2",
    )
    assert result["status"] == "delivery_resolved"
    assert result["action_key"] == "reject"
    assert hitl_callback[0]["action_key"] == "reject"


@pytest.mark.asyncio
async def test_unmatched_reply_falls_through_to_agent_path(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    hitl_callback,
):
    """A free-form reply that isn't a recognised action key should NOT
    resolve the delivery — the message goes to the agent as usual."""
    ctx = await _register(client, "fallthrough_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _create_telegram_binding(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        cc=cc,
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve?",
        actions=[{"key": "approve"}, {"key": "reject"}],
        callback_kind="test.hitl",
        callback_payload={"task_id": "T2"},
    )

    # User types something not matching any action
    result = await dispatch_inbound(
        entity_id=ctx["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=contact.source_id,
        sender_name="Tester",
        chat_id=contact.source_id,
        content="hmm let me think about it",
    )

    # Result should NOT be a delivery_resolved — we want the agent run
    # to take over (or fail downstream; here we just assert no callback).
    assert result.get("status") != "delivery_resolved"
    assert hitl_callback == []

    # Delivery still pending
    refresh = (
        await db_session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == ctx["user_id"],
            )
        )
    ).scalar_one()
    assert refresh.status == "sent"
    assert refresh.resolved_action_key is None


@pytest.mark.asyncio
async def test_callback_failure_marks_delivery_failed(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
    failing_callback,
):
    """A callback that returns ok=False should leave the delivery in
    state=failed with the error message, and still ack the user."""
    ctx = await _register(client, "callback_fail_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _create_telegram_binding(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        cc=cc,
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve?",
        actions=[{"key": "approve"}, {"key": "reject"}],
        callback_kind="test.hitl",
        callback_payload={"task_id": "T3"},
    )
    fake_telegram.sent.clear()

    result = await dispatch_inbound(
        entity_id=ctx["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=contact.source_id,
        sender_name="Tester",
        chat_id=contact.source_id,
        content="approve",
    )
    assert result["status"] == "delivery_callback_failed"

    refresh = (
        await db_session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == ctx["user_id"],
            )
        )
    ).scalar_one()
    assert refresh.status == "failed"
    assert refresh.error_message and "already resolved" in refresh.error_message

    # Still sends a (default) ack so the user knows their reply was seen
    assert len(fake_telegram.sent) == 1


@pytest.mark.asyncio
async def test_unknown_callback_kind_does_not_crash(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    """Producer ships an unregistered callback_kind (typo, missing import).
    The matcher should still resolve the action, dispatch_callback returns
    a polite error, and the delivery is marked failed."""
    ctx = await _register(client, "unknown_cb_user")
    cc, contact = await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await _create_telegram_binding(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        cc=cc,
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_hitl_requested",
        title="Approve?",
        actions=[{"key": "approve"}, {"key": "reject"}],
        callback_kind="nonexistent.kind",
        callback_payload={"task_id": "T4"},
    )

    result = await dispatch_inbound(
        entity_id=ctx["entity_id"],
        channel_config_id=cc.id,
        channel_type="telegram",
        sender_id=contact.source_id,
        sender_name="Tester",
        chat_id=contact.source_id,
        content="approve",
    )
    assert result["status"] == "delivery_callback_failed"
    assert result["callback_result"]["error"] == "unknown_callback_kind"

    refresh = (
        await db_session.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.user_id == ctx["user_id"],
            )
        )
    ).scalar_one()
    assert refresh.status == "failed"


@pytest.mark.asyncio
async def test_native_actionable_adapter_skips_text_footer(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """An adapter overriding send_actionable_message (Telegram inline
    keyboard, WhatsApp quick replies) should NOT see a text "Reply with…"
    footer — it gets the raw body + a structured ``actions`` list to
    render natively. The default-impl fallback is only for adapters that
    don't override."""

    class _NativeActionable(ChannelAdapter):
        channel_type = "telegram"
        sent: list = []
        actionable_sent: list = []

        async def send_text(self, cc, to, text, **kwargs):
            self.sent.append({"to": to, "text": text})
            return {"status": "sent", "external_id": "ext-text"}

        async def send_actionable_message(self, cc, to, text, *, actions):
            self.actionable_sent.append({"to": to, "text": text, "actions": actions})
            return {"status": "sent", "external_id": "ext-action"}

        async def parse_inbound(self, *args, **kwargs):
            return None

    native = _NativeActionable()
    original = ADAPTERS.get("telegram")
    ADAPTERS["telegram"] = native  # type: ignore[assignment]
    try:
        ctx = await _register(client, "native_action_user")
        cc, _ = await _link_telegram(
            db_session,
            entity_id=ctx["entity_id"],
            user_id=ctx["user_id"],
        )
        await client.put(
            "/api/v1/notifications/preferences",
            headers=ctx["headers"],
            json={"default_channels": ["telegram"]},
        )

        await notify_module.notify(
            entity_id=ctx["entity_id"],
            user_id=ctx["user_id"],
            type="task_hitl_requested",
            title="Approve?",
            body="Please decide",
            actions=[
                {"key": "approve", "label": "Approve"},
                {"key": "reject", "label": "Reject"},
            ],
            callback_kind="test.unused",
            callback_payload={},
        )

        # Native path was used, text path was not
        assert native.sent == []
        assert len(native.actionable_sent) == 1
        payload = native.actionable_sent[0]
        # No "Reply with:" footer — adapter is expected to render the
        # actions natively.
        assert "Reply with:" not in payload["text"]
        assert "Approve?" in payload["text"]
        # Structured actions were forwarded so the adapter can build a UI
        assert [a["key"] for a in payload["actions"]] == ["approve", "reject"]
    finally:
        if original is None:
            ADAPTERS.pop("telegram", None)
        else:
            ADAPTERS["telegram"] = original


# ── Notify without actions stays back-compatible ───────────────────────────


@pytest.mark.asyncio
async def test_notify_without_actions_writes_no_delivery_row(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_telegram: _RecordingAdapter,
):
    """Notifications without actions shouldn't write delivery rows — the
    HITL machinery only kicks in for actionable events."""
    ctx = await _register(client, "no_actions_user")
    await _link_telegram(
        db_session,
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
    )
    await client.put(
        "/api/v1/notifications/preferences",
        headers=ctx["headers"],
        json={"default_channels": ["telegram"]},
    )

    await notify_module.notify(
        entity_id=ctx["entity_id"],
        user_id=ctx["user_id"],
        type="task_succeeded",
        title="Done",
        body="task complete",
    )

    rows = (
        (
            await db_session.execute(
                select(NotificationDelivery).where(
                    NotificationDelivery.user_id == ctx["user_id"],
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    # Outbound text should NOT have the "Reply with:" footer
    assert "Reply with:" not in fake_telegram.sent[0]["text"]
