"""Voice approves a presented exact Chat request, never a guessed next item."""

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from packages.core.services.chat_approvals import parse_hitl_action
from packages.core.services.voice.approvals import (
    VoiceApprovalReplyError,
    VoicePendingAction,
    load_voice_pending_actions,
    prepare_voice_approval_reply,
    voice_approval_reply_choice,
    voice_pending_actions,
)


def card(target="initiative-approval", **changes):
    return {
        "id": target, "type": "approval", "prompt": "Create this Initiative?",
        "content": "Check the project every morning.", "tool": "agent_initiative",
        "action": "agent.initiative.create", "options": ["approve", "always_approve", "reject"],
        **changes,
    }


def message(*cards, resolved_at=None):
    return SimpleNamespace(meta={"hitl_requests": list(cards)}, resolved_at=resolved_at)


def runtime_request(raw, **changes):
    return SimpleNamespace(
        status="pending", origin_kind="tool_call", hitl_type="authorize",
        action_key=raw["action"], context={"tool": raw["tool"]}, **changes,
    )


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class DB:
    def __init__(self, raw=None, *, messages=None, request=None, files=None):
        raw = raw or card()
        self.conversation = SimpleNamespace(user_id="user", workspace_id=None, meta={"file_approvals": files or {}})
        self.messages = [message(raw)] if messages is None else messages
        self.request = runtime_request(raw) if request is None else request
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        table = statement.column_descriptions[0]["entity"].__tablename__
        return Result({"conversations": self.conversation, "messages": self.messages, "hitl_requests": self.request}[table])


async def reply(db, text="确认", *, presented=None, target_id=None):
    return await prepare_voice_approval_reply(
        db, conversation_id="conversation", entity_id="entity", user_id="user", text=text,
        presented_actions=voice_pending_actions([card()]) if presented is None else presented,
        target_id=target_id,
    )


@pytest.mark.parametrize("text,expected", [
    ("确认。", "approve"), ("同意", "approve"), ("approve", "approve"),
    ("Go ahead!", "approve"), ("拒绝", "reject"), ("No.", "reject"),
    ("以后都允许", "always_approve"), ("always_approve", "always_approve"),
    ("批准第二项", "approve"), ("拒绝第2项", "reject"), ("approve item 2", "approve"),
    ("总是批准第二项", "always_approve"), ("always approve item 2", "always_approve"),
    ("为什么需要确认", None), ("确认吗？", None), ("approve?", None),
    ("同意明天讨论", None), ("I don't approve", None), ("not approved", None),
    ("帮我创建一个任务", None), ("批准第二项吗", None), ("是否批准第二项？", None),
    ("不要批准第二项", None), ("批准第二项但先别执行", None),
    ("approve item 2 and create another task", None),
])
def test_reply_requires_a_complete_explicit_verdict(text, expected):
    assert voice_approval_reply_choice(text) == expected


def test_display_is_frozen_bounded_and_does_not_expose_execution_material():
    raw = card(approval_token="secret", arguments={"password": "secret"}, args_preview={"api_key": "secret"})
    action, = voice_pending_actions([raw])
    assert set(action.to_payload()) == {"id", "title", "description", "choices", "requires_ui"}
    assert "secret" not in json.dumps(action.to_payload())
    assert action.requires_ui is False
    assert VoicePendingAction.from_record(json.loads(json.dumps(action.to_record()))) == action
    with pytest.raises(FrozenInstanceError):
        action.title = "Changed"
    bounded = voice_pending_actions([card(str(index)) for index in range(100)])
    assert len(bounded) == 8
    assert all(item.requires_ui for item in bounded)


@pytest.mark.parametrize("changes", [
    {"id": []}, {"id": "x" * 129}, {"id": " "}, {"id": " spaced "},
    {"title": "x" * 501}, {"description": "x" * 2001},
    {"choices": "approve"}, {"choices": ["approve", "approve"]},
    {"choices": ["approve", {}]}, {"choices": ["sign_in"]}, {"choices": []},
    {"requires_ui": "false"}, {"requires_ui": 0}, {"_fingerprint": "forged"},
    {"_fingerprint": ""}, {"unknown_future_field": "value"},
])
def test_malformed_restored_records_fail_closed(changes):
    record = voice_pending_actions([card()])[0].to_record()
    with pytest.raises(ValueError):
        VoicePendingAction.from_record({**record, **changes})


@pytest.mark.parametrize("record", [None, [], {}, {"id": "only-id"}])
def test_missing_or_non_object_restored_record_fails_closed(record):
    with pytest.raises(ValueError):
        VoicePendingAction.from_record(record)


@pytest.mark.parametrize("changes", [
    {"hitl_type": "input"}, {"hitl_type": "review"}, {"type": "login"},
    {"type": "unknown"}, {"confirmation_mode": "handoff_required"},
    {"policy_category": "captcha"}, {"options": ["retry", "cancel"]},
])
def test_non_authorization_cards_require_original_controls(changes):
    assert voice_pending_actions([card(**changes)])[0].requires_ui


def test_resolved_malformed_and_duplicate_cards_do_not_become_new_requests():
    assert voice_pending_actions(None) == ()
    assert voice_pending_actions([{}, {"id": []}, card(resolved=True), card(status="consumed")]) == ()
    assert len(voice_pending_actions([card(), card()])) == 1
    action, = voice_pending_actions([card(prompt="x" * 1000, content={"secret": "value"})])
    assert len(action.title) == 500
    assert action.description == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("text,choice", [("确认", "approve"), ("拒绝", "reject"), ("以后都允许", "always_approve")])
async def test_initiative_approval_becomes_the_existing_exact_chat_action(text, choice):
    db = DB(files={"different-file": {"status": "pending"}})
    result = await reply(db, text)
    assert parse_hitl_action(result) == ("initiative-approval", choice, None)
    assert db.request.status == "pending"  # preparing never grants or consumes
    assert db.conversation.meta["file_approvals"]["different-file"]["status"] == "pending"
    for statement in db.statements:
        assert statement.get_execution_options()["populate_existing"] is True
    compiled = [statement.compile(dialect=postgresql.dialect()) for statement in db.statements]
    assert compiled[0].params["id_1"] == "conversation"
    assert compiled[0].params["entity_id_1"] == "entity"
    assert compiled[-1].params["id_1"] == "initiative-approval"
    assert compiled[-1].params["entity_id_1"] == "entity"
    assert compiled[-1].params["origin_conversation_id_1"] == "conversation"


@pytest.mark.asyncio
async def test_real_initiative_runtime_producer_card_round_trips_to_main_action():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
    from packages.core.ai.runtime.approval_service import _runtime_hitl_payload
    from packages.core.services.hitl_requests import hitl_requests_from_data

    tool = "create_agent_initiative"
    arguments = {"title": "Morning check", "prompt": "Check my project", "schedule": {"type": "daily"}}
    action = classify_runtime_tool_action(tool, arguments)
    assert action.action_key == "workspace.automation.agent_initiative.create"
    render = {"tool": tool, "args_preview": arguments, "content": "Check my project each morning"}
    envelope = json.loads(_runtime_hitl_payload(
        request_id="initiative-approval", action=action, tool_name=tool,
        arguments=arguments, matched_rule="test_rule", render=render,
    ))
    cards = hitl_requests_from_data(envelope)
    assert "operation" in cards[0]
    db = DB(cards[0])
    db.request.context["args_preview"] = arguments
    actions = voice_pending_actions(cards)
    assert actions[0].requires_ui is False
    assert "operation" not in actions[0].to_payload()
    assert parse_hitl_action(await reply(db, presented=actions)) == ("initiative-approval", "approve", None)


@pytest.mark.asyncio
async def test_ordinary_question_does_not_select_any_approval_or_query_database():
    db = DB()
    assert await reply(db, "为什么需要确认？") is None
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("presented,target,code", [
    ((), None, "approval_not_presented"),
    (voice_pending_actions([card(), card("file-approval")]), None, "approval_target_ambiguous"),
    (voice_pending_actions([card()]), "foreign-id", "approval_target_mismatch"),
    (voice_pending_actions([card(hitl_type="input")]), None, "approval_requires_ui"),
])
async def test_no_presented_unique_supported_target_never_falls_back(presented, target, code):
    db = DB()
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db, presented=presented, target_id=target)
    assert error.value.code == code
    assert error.value.message
    assert db.statements == []


@pytest.mark.asyncio
async def test_model_selected_target_does_not_disambiguate_a_bare_yes():
    actions = voice_pending_actions([card("file-approval"), card()])
    db = DB()
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db, presented=actions, target_id="initiative-approval")
    assert error.value.code == "approval_target_ambiguous"
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text,choice", [
    ("批准第二项", "approve"), ("确认第2项", "approve"), ("approve item 2", "approve"),
    ("拒绝第2项", "reject"), ("reject item 2", "reject"),
    ("总是批准第二项", "always_approve"), ("always approve item 2", "always_approve"),
])
@pytest.mark.parametrize("target_id", [None, "initiative-approval"])
async def test_explicit_ordinal_selects_snapshot_order_not_model_order(text, choice, target_id):
    actions = voice_pending_actions([card("file-approval"), card()])
    result = await reply(DB(), text, presented=actions, target_id=target_id)
    assert parse_hitl_action(result) == ("initiative-approval", choice, None)


@pytest.mark.asyncio
async def test_model_target_must_match_the_human_ordinal():
    actions = voice_pending_actions([card("file-approval"), card()])
    db = DB()
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db, "批准第二项", presented=actions, target_id="file-approval")
    assert error.value.code == "approval_target_mismatch"
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["批准第零项", "批准第3项", "批准第九项", "approve item 0", "approve item 9", "approve item 99"])
async def test_ordinal_outside_presented_bounded_snapshot_never_falls_back(text):
    db = DB()
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db, text, presented=voice_pending_actions([card("first"), card()]))
    assert error.value.code == "approval_ordinal_out_of_range"
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["granted", "consumed", "denied", "expired"])
async def test_terminal_durable_request_cannot_resolve_another_pending_item(status):
    db = DB(files={"next-file": {"status": "pending"}})
    db.request.status = status
    with pytest.raises(VoiceApprovalReplyError, match="expired or was already answered"):
        await reply(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [[], [message(card(resolved=True))], [message(card(prompt="Different operation?"))]])
async def test_missing_resolved_or_changed_chat_projection_blocks(stored):
    with pytest.raises(VoiceApprovalReplyError):
        await reply(DB(messages=stored))


@pytest.mark.asyncio
async def test_sibling_resolution_does_not_change_the_selected_card_snapshot():
    from datetime import datetime, timezone

    # resolved_at belongs to pending_action, not every Runtime card in meta.
    db = DB(messages=[message(card(), card("sibling", resolved=True, resolution="reject"), resolved_at=datetime.now(timezone.utc))])
    assert parse_hitl_action(await reply(db)) == ("initiative-approval", "approve", None)


@pytest.mark.asyncio
async def test_conflicting_duplicate_id_in_one_message_fails_closed():
    db = DB(messages=[message(card(), card(prompt="A different operation"))])
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db)
    assert error.value.code == "approval_target_ambiguous"


@pytest.mark.asyncio
async def test_expired_provider_and_human_only_provider_cannot_be_voice_approved():
    db = DB()
    db.request.context["continuation"] = {"expires_at": "2000-01-01T00:00:00Z"}
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db)
    assert error.value.code == "approval_stale"
    db.request.context["continuation"] = {"confirmation_mode": "handoff_required"}
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db)
    assert error.value.code == "approval_requires_ui"


@pytest.mark.asyncio
async def test_actor_conversation_boundary_and_changed_durable_action_fail_closed():
    db = DB()
    db.conversation.user_id = "another-user"
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db)
    assert error.value.code == "approval_scope_unavailable"
    db.conversation.user_id = "user"
    db.request.action_key = "file.delete"
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db)
    assert error.value.code == "approval_changed"


@pytest.mark.asyncio
async def test_exact_legacy_file_request_and_id_collision():
    raw = card(tool="save_file", action="write", paths=["report.txt"])
    file_request = {"status": "pending", "tool": "save_file", "action": "write", "paths": ["report.txt"]}
    db = DB(raw, files={raw["id"]: file_request})
    db.request = None
    result = await reply(db, presented=voice_pending_actions([raw]))
    assert json.loads(result)["hitl_id"] == raw["id"]
    db.request = runtime_request(raw)
    with pytest.raises(VoiceApprovalReplyError) as error:
        await reply(db, presented=voice_pending_actions([raw]))
    assert error.value.code == "approval_target_ambiguous"


@pytest.mark.asyncio
async def test_load_current_pending_keeps_newest_terminal_projection_and_bounds_overflow():
    db = DB(messages=[message(card(resolved=True)), message(card()), message(card("still-pending"))])
    actions = await load_voice_pending_actions(db, conversation_id="conversation", entity_id="entity")
    assert [item.id for item in actions] == ["still-pending"]
    compiled = db.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.params["id_1"] == "conversation"
    assert compiled.params["entity_id_1"] == "entity"
    # SQL excludes historical resolved metadata cards before LIMIT, not merely
    # rows whose independent pending_action channel has no resolved_at stamp.
    assert "jsonb_path_exists" in str(compiled)
    assert "resolved_at IS NULL" not in str(compiled)
    path = next(value for value in compiled.params.values() if isinstance(value, str) and value.startswith("$.hitl_requests"))
    assert "@.resolved == false" in path
    assert '@.status == "pending"' in path
    db.messages = [message(card(str(index), resolved=index > 0)) for index in range(9)]
    actions = await load_voice_pending_actions(db, conversation_id="conversation", entity_id="entity")
    assert len(actions) == 1
    assert actions[0].requires_ui
