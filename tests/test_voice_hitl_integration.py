"""Voice decisions use Chat's durable approval/continuation boundary.

Only provider account discovery and provider I/O are replaced. The approval
guard, generated cards, authorization, resolver, Runtime executor, and voice
receipts use the real database and production implementations.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.approval_settings_test_utils import (
    action_rule, initialize_test_approval_owners, set_workspace_test_approval,
)
from sqlalchemy import select

from packages.core import database
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
from packages.core.constants.agents import MANOR_AGENT_ID
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.chat_approvals import resolve_chat_approval_turn
from packages.core.services.voice.approvals import (
    VoiceApprovalReplyError,
    load_voice_pending_actions,
    prepare_voice_approval_reply,
)
from packages.core.services.voice.work_queue import (
    VOICE_WORK_META_KEY,
    VoiceWorkNotPendingError,
    admit_voice_work,
    claim_voice_work,
    recover_voice_work,
)


pytestmark = pytest.mark.asyncio
_TOOL = "mcp__twitter_x__create_tweet"


class _SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_exc):
        return False


async def _setup(db, monkeypatch):
    # Keep every production nested session on this test's isolated engine/loop.
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db))

    async def provider_account_preflight(**_kwargs):
        return SimpleNamespace(blocked_result=None, integration_account_id=None)

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution.runtime_preflight_mcp_call",
        provider_account_preflight,
    )
    ids = {key: generate_ulid() for key in (
        "entity_id", "user_id", "workspace_id", "conversation_id",
    )}
    db.add(Entity(id=ids["entity_id"], name="Voice HITL integration", settings={"approval_defaults": account_defaults()}))
    db.add(User(
        id=ids["user_id"], entity_id=ids["entity_id"],
        email=f"{ids['user_id']}@example.test", password_hash="x",
        role="owner", status="active",
    ))
    db.add(UserMembership(
        user_id=ids["user_id"], entity_id=ids["entity_id"],
        role="owner", status="active", is_primary=True,
    ))
    db.add(Workspace(
        id=ids["workspace_id"], entity_id=ids["entity_id"],
        name="Voice approval workspace", operating_model={},
    ))
    db.add(Conversation(
        id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], workspace_id=ids["workspace_id"],
        title="Voice to Main", channel="web",
    ))
    await db.flush()
    workspace = await db.get(Workspace, ids["workspace_id"])
    await initialize_test_approval_owners(db, workspace, user_id=ids["user_id"])
    await set_workspace_test_approval(
        db, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        actor_id=ids["user_id"], user_rules=[action_rule("social_post.publish", effect="ask")],
    )
    await db.commit()
    return ids


def _envelope(ids):
    return RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER,
            entity_id=ids["entity_id"], actor_user_id=ids["user_id"],
            execution_user_id=ids["user_id"], agent_id=MANOR_AGENT_ID,
            workspace_id=ids["workspace_id"],
        ),
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        entity_id=ids["entity_id"], user_id=ids["user_id"],
        agent_id=MANOR_AGENT_ID, workspace_id=ids["workspace_id"],
        conversation_id=ids["conversation_id"], allowed_tool_names=(_TOOL,),
    )


async def _pending(db, ids, *texts):
    cards = []
    for text in texts:
        blocked = await guard_runtime_tool_action(
            name=_TOOL, arguments={"text": text}, **ids,
            operation_id=f"test:voice:{generate_ulid()}",
            runtime_envelope=_envelope(ids),
        )
        payload = json.loads(blocked or "{}")
        assert payload["__hitl__"] is True
        cards.append(payload["hitl"])
    message = Message(
        conversation_id=ids["conversation_id"], role="assistant",
        content="Please review the requested posts.",
        pending_action=cards[0], meta={"hitl_requests": cards},
    )
    db.add(message)
    await db.commit()
    actions = await load_voice_pending_actions(
        db, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
    )
    assert len(actions) == len(cards)
    assert all(not action.requires_ui for action in actions)
    return message, actions


async def _prepare(db, ids, actions, *, text="同意", target_id=None):
    return await prepare_voice_approval_reply(
        db, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], text=text, presented_actions=actions,
        target_id=target_id,
    )


async def _resolve(db, ids, action):
    result = await resolve_chat_approval_turn(
        db, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], message=action,
    )
    await db.commit()
    return result


async def _execute(db, ids, forced_call):
    async def provider_post(*, text, entity_id, user_id, **kwargs):
        # This durable row is the fake external provider's effect, not a mock
        # call count. A duplicate handler invocation would create a second row.
        assert entity_id == ids["entity_id"]
        assert user_id == ids["user_id"]
        assert "approval_token" not in kwargs
        effect = Message(
            conversation_id=ids["conversation_id"], role="tool", content=text,
            meta={"voice_test_provider_effect": True},
        )
        db.add(effect)
        await db.commit()
        return json.dumps({"ok": True, "post_id": effect.id})

    result = await runtime_execute_registered_tool(
        tool_name=forced_call["name"], arguments=dict(forced_call["arguments"]),
        handler_resolver=lambda name: provider_post if name == _TOOL else None,
        runtime_envelope=_envelope(ids),
    )
    return json.loads(result) if isinstance(result, str) else result


async def _effects(db, ids):
    return list((await db.execute(select(Message).where(
        Message.conversation_id == ids["conversation_id"],
        Message.meta.contains({"voice_test_provider_effect": True}),
    ))).scalars().all())


async def test_voice_exact_approve_runs_frozen_call_once_and_leaves_other_card_pending(
    db_session, monkeypatch,
):
    ids = await _setup(db_session, monkeypatch)
    message, actions = await _pending(db_session, ids, "First post", "Exact second post")
    with pytest.raises(VoiceApprovalReplyError, match="More than one request"):
        await _prepare(db_session, ids, actions)
    with pytest.raises(VoiceApprovalReplyError, match="More than one request"):
        await _prepare(db_session, ids, actions, target_id=actions[1].id)
    action = await _prepare(db_session, ids, actions, text="批准第二项", target_id=actions[1].id)
    assert json.loads(action) == {"hitl_id": actions[1].id, "action": "approve"}
    assert (await db_session.get(HitlRequest, actions[1].id)).status == "pending"

    _replacement, _saved, _save, metadata = await _resolve(db_session, ids, action)
    forced = metadata["forced_tool_calls"][0]
    assert forced["arguments"] == {
        "text": "Exact second post", "approval_token": actions[1].id,
    }
    assert forced["disable_followup_tools"] is True
    assert (await db_session.get(HitlRequest, actions[1].id)).status == "granted"
    assert (await _execute(db_session, ids, forced))["ok"] is True
    decided = await db_session.get(HitlRequest, actions[1].id)
    assert decided.status == "consumed"
    assert decided.consumed_at is not None
    assert decided.decided_by_user_id == ids["user_id"]
    assert (await db_session.get(HitlRequest, actions[0].id)).status == "pending"
    await db_session.refresh(message)
    cards = {card["id"]: card for card in message.meta["hitl_requests"]}
    assert cards[actions[1].id]["resolved"] is True
    assert not cards[actions[0].id].get("resolved")

    # Both the helper and Main's resolver reject the already-decided input;
    # replaying the previously obtained forced call also cannot repeat I/O.
    with pytest.raises(VoiceApprovalReplyError):
        await _prepare(db_session, ids, actions, target_id=actions[1].id)
    replay = await _resolve(db_session, ids, action)
    assert "forced_tool_calls" not in replay[3]
    assert (await _execute(db_session, ids, forced)).get("ok") is not True
    effects = await _effects(db_session, ids)
    assert [row.content for row in effects] == ["Exact second post"]


async def test_voice_reject_is_terminal_and_cannot_be_reversed_or_executed(
    db_session, monkeypatch,
):
    ids = await _setup(db_session, monkeypatch)
    message, actions = await _pending(db_session, ids, "Do not publish")
    action = await _prepare(db_session, ids, actions, text="拒绝")
    result = await _resolve(db_session, ids, action)
    assert "forced_tool_calls" not in result[3]
    request = await db_session.get(HitlRequest, actions[0].id)
    assert request.status == "denied"
    assert request.consumed_at is None
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolution"] == "reject"

    with pytest.raises(VoiceApprovalReplyError):
        await _prepare(db_session, ids, actions, text="同意")
    for choice in ("reject", "approve"):
        repeated = await _resolve(db_session, ids, json.dumps({
            "hitl_id": actions[0].id, "action": choice,
        }))
        assert "forced_tool_calls" not in repeated[3]
        assert request.status == "denied"
    assert (await _execute(db_session, ids, {
        "name": _TOOL,
        "arguments": {"text": "Do not publish", "approval_token": actions[0].id},
    })).get("ok") is not True
    assert await _effects(db_session, ids) == []


@pytest.mark.parametrize("spoken", ["同意", "拒绝", "以后都允许"])
async def test_voice_shared_workspace_does_not_bypass_governance_authority(
    db_session, monkeypatch, spoken,
):
    ids = await _setup(db_session, monkeypatch)
    message, actions = await _pending(db_session, ids, "Owner approval required")
    editor_id = generate_ulid()
    db_session.add(User(
        id=editor_id, entity_id=ids["entity_id"],
        email=f"{editor_id}@example.test", password_hash="x",
        role="member", status="active",
    ))
    db_session.add(UserMembership(
        user_id=editor_id, entity_id=ids["entity_id"],
        role="member", status="active", is_primary=True,
    ))
    db_session.add(WorkspaceStaff(
        workspace_id=ids["workspace_id"], user_id=editor_id,
        role="editor", status="active",
    ))
    await db_session.commit()
    editor_ids = {**ids, "user_id": editor_id}
    # Shared conversation access does not grant approval authority. Persistent
    # policy changes must use Chat controls rather than a spoken confirmation.
    if spoken == "以后都允许":
        with pytest.raises(VoiceApprovalReplyError, match="Chat controls"):
            await _prepare(db_session, editor_ids, actions, text=spoken)
    else:
        action = await _prepare(db_session, editor_ids, actions, text=spoken)
        replacement, _saved, _save, metadata = await _resolve(db_session, editor_ids, action)
        assert "authority" in replacement
        assert "forced_tool_calls" not in metadata
    request = await db_session.get(HitlRequest, actions[0].id)
    assert request.status == "pending"
    assert request.decided_by_user_id is None
    assert request.consumed_at is None
    await db_session.refresh(message)
    assert not message.meta["hitl_requests"][0].get("resolved")
    assert await _effects(db_session, ids) == []

    # A denied spoken decision must not poison the legitimate owner's card.
    owner_action = await _prepare(db_session, ids, actions)
    owner_result = await _resolve(db_session, ids, owner_action)
    assert (await _execute(db_session, ids, owner_result[3]["forced_tool_calls"][0]))["ok"] is True
    assert len(await _effects(db_session, ids)) == 1


async def test_voice_recovery_preserves_presented_card_and_refuses_changed_payload(
    db_session, monkeypatch,
):
    ids = await _setup(db_session, monkeypatch)
    message, actions = await _pending(db_session, ids, "Frozen original post")
    receipt = await admit_voice_work(
        db_session, conversation_id=ids["conversation_id"], text="同意",
        user_id=ids["user_id"], public_channel=False,
        pending_action_id=actions[0].id, presented_actions=actions,
    )
    recovered = await recover_voice_work(db_session, conversation_id=ids["conversation_id"])
    assert len(recovered) == 1
    assert recovered[0].recovered is True
    assert recovered[0].pending_action_id == actions[0].id
    assert recovered[0].presented_actions == actions
    queued = await db_session.get(Message, receipt.message_id)
    assert queued.content == "同意"
    assert queued.meta[VOICE_WORK_META_KEY]["presented_actions"] == [
        action.to_record() for action in actions
    ]

    cards = [dict(card) for card in message.meta["hitl_requests"]]
    cards[0]["content"] = "A different post appeared while this work was queued"
    message.meta = {**message.meta, "hitl_requests": cards}
    await db_session.commit()
    with pytest.raises(VoiceApprovalReplyError) as error:
        await _prepare(
            db_session, ids, recovered[0].presented_actions,
            text=recovered[0].text, target_id=recovered[0].pending_action_id,
        )
    assert error.value.code == "approval_changed"
    assert (await db_session.get(HitlRequest, actions[0].id)).status == "pending"
    assert await _effects(db_session, ids) == []


async def test_voice_running_receipt_recovery_never_replays_consumed_approval(
    db_session, monkeypatch,
):
    ids = await _setup(db_session, monkeypatch)
    _message, actions = await _pending(db_session, ids, "Publish exactly once")
    receipt = await admit_voice_work(
        db_session, conversation_id=ids["conversation_id"], text="同意",
        user_id=ids["user_id"], public_channel=False,
        pending_action_id=actions[0].id, presented_actions=actions,
    )
    await claim_voice_work(db_session, receipt, conversation_id=ids["conversation_id"])
    action = await _prepare(
        db_session, ids, receipt.presented_actions,
        text=receipt.text, target_id=receipt.pending_action_id,
    )
    resolution = await _resolve(db_session, ids, action)
    forced = resolution[3]["forced_tool_calls"][0]
    assert (await _execute(db_session, ids, forced))["ok"] is True

    # Simulate process loss after provider I/O but before voice completion.
    assert await recover_voice_work(db_session, conversation_id=ids["conversation_id"]) == []
    queued = await db_session.get(Message, receipt.message_id)
    await db_session.refresh(queued)
    assert queued.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"
    with pytest.raises(VoiceWorkNotPendingError):
        await claim_voice_work(db_session, receipt, conversation_id=ids["conversation_id"])
    assert (await _execute(db_session, ids, forced)).get("ok") is not True
    assert (await db_session.get(HitlRequest, actions[0].id)).status == "consumed"
    assert len(await _effects(db_session, ids)) == 1


@pytest.mark.parametrize("malformed", ["collection", "record", "fingerprint", "target"])
async def test_voice_recovery_interrupts_malformed_approval_but_recovers_other_work(
    db_session, monkeypatch, malformed,
):
    ids = await _setup(db_session, monkeypatch)
    _message, actions = await _pending(db_session, ids, "Still needs approval")
    damaged = await admit_voice_work(
        db_session, conversation_id=ids["conversation_id"], text="同意",
        user_id=ids["user_id"], public_channel=False,
        pending_action_id=actions[0].id, presented_actions=actions,
    )
    ordinary = await admit_voice_work(
        db_session, conversation_id=ids["conversation_id"], text="Explain the report",
        user_id=ids["user_id"], public_channel=False,
    )
    queued = await db_session.get(Message, damaged.message_id)
    work = dict(queued.meta[VOICE_WORK_META_KEY])
    if malformed == "collection":
        work["presented_actions"] = {"not": "a bounded action list"}
    elif malformed == "record":
        work["presented_actions"] = ["not an action record"]
    elif malformed == "fingerprint":
        work["presented_actions"] = [{**actions[0].to_record(), "_fingerprint": ""}]
    else:
        work["pending_action_id"] = {"id": actions[0].id}
    queued.meta = {**queued.meta, VOICE_WORK_META_KEY: work}
    await db_session.commit()

    recovered = await recover_voice_work(db_session, conversation_id=ids["conversation_id"])
    assert recovered == [replace(ordinary, recovered=True)]
    await db_session.refresh(queued)
    assert queued.meta[VOICE_WORK_META_KEY]["state"] == "interrupted"
    assert "approval context is invalid" in queued.meta[VOICE_WORK_META_KEY]["error"]
    with pytest.raises(VoiceWorkNotPendingError):
        await claim_voice_work(db_session, damaged, conversation_id=ids["conversation_id"])
    await claim_voice_work(db_session, recovered[0], conversation_id=ids["conversation_id"])
    assert (await db_session.get(HitlRequest, actions[0].id)).status == "pending"
    assert await _effects(db_session, ids) == []


@pytest.mark.parametrize("changed", ["target", "snapshot"])
async def test_voice_same_source_words_cannot_retarget_or_replace_frozen_approval(
    db_session, monkeypatch, changed,
):
    ids = await _setup(db_session, monkeypatch)
    _message, actions = await _pending(db_session, ids, "Original target", "Another target")
    source = Message(
        conversation_id=ids["conversation_id"], role="user", content="同意",
        meta={"voice_control": True, "author_user_id": ids["user_id"]},
    )
    db_session.add(source)
    await db_session.commit()
    admission = {
        "conversation_id": ids["conversation_id"], "text": "同意",
        "user_id": ids["user_id"], "public_channel": False,
        "source_message_ids": (source.id,),
        "pending_action_id": actions[0].id, "presented_actions": actions,
    }
    receipt = await admit_voice_work(db_session, **admission)
    assert await admit_voice_work(db_session, **admission) == receipt
    changed_fields = (
        {"pending_action_id": actions[1].id}
        if changed == "target" else
        {"presented_actions": (replace(actions[0], title="A changed presentation"), actions[1])}
    )
    with pytest.raises(ValueError, match="different work"):
        await admit_voice_work(db_session, **{**admission, **changed_fields})
    with pytest.raises(VoiceWorkNotPendingError):
        await claim_voice_work(
            db_session, replace(receipt, **changed_fields),
            conversation_id=ids["conversation_id"],
        )
    await db_session.refresh(source)
    assert source.content == "同意"
    assert source.meta[VOICE_WORK_META_KEY]["pending_action_id"] == actions[0].id
    assert source.meta[VOICE_WORK_META_KEY]["presented_actions"] == [
        action.to_record() for action in actions
    ]
    assert source.meta[VOICE_WORK_META_KEY]["state"] == "pending"
    assert await recover_voice_work(db_session, conversation_id=ids["conversation_id"]) == [
        replace(receipt, recovered=True),
    ]
    await claim_voice_work(db_session, receipt, conversation_id=ids["conversation_id"])
    for action in actions:
        assert (await db_session.get(HitlRequest, action.id)).status == "pending"
    assert await _effects(db_session, ids) == []
