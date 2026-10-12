"""Voice carries Main's approval data without replacing its human transcript."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from apps.api.routers import chat, public_chat
from packages.core.schemas.chat import ChatMessageResponse
from packages.core.services import conversation_messages
from packages.core.services.voice.approvals import (
    VoiceApprovalReplyError,
    voice_pending_actions,
)


@pytest.fixture
def voice_api(monkeypatch):
    db = AsyncMock()

    @asynccontextmanager
    async def database():
        yield db

    scope = ChatAudioScope("entity", "owner", "workspace", "conversation", "agent")
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.voice_session_mode = "chat_gateway"
    dispatch = AsyncMock(return_value=ChatMessageResponse(conversation_id="conversation", content="Created."))
    monkeypatch.setattr(chat_voice, "async_session", database)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(chat_voice, "get_current_user", AsyncMock(return_value=SimpleNamespace(id="owner", entity_id="entity")))
    monkeypatch.setattr(chat_voice, "security", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_voice, "load_voice_pending_actions", AsyncMock(return_value=()))
    monkeypatch.setattr(chat, "chat_message", dispatch)
    return SimpleNamespace(db=db, scope=scope, request=request, start=chat_voice.CallStart(token="test"), dispatch=dispatch)


def card():
    return {
        "id": "approval-1", "type": "approval", "prompt": "Create the daily Initiative?",
        "content": "Check the workspace every morning.", "options": ["approve", "reject"],
        "tool": "create_agent_initiative", "action": "workspace.automation.agent_initiative.create",
        "arguments": {"private": "never include execution arguments in Live payload"},
    }


async def invoke(api, text, **kwargs):
    return await chat_voice.call_agent(api.request, api.start, api.scope, text, **kwargs)


async def test_main_pending_is_not_reported_as_creation_success(voice_api):
    voice_api.dispatch.return_value = ChatMessageResponse(
        conversation_id="conversation", content="Please approve this creation.", hitl_requests=[card()],
    )
    result = await invoke(voice_api, "Create a daily Initiative")
    assert result.status == "approval_required"
    assert result.pending_actions[0].id == "approval-1"
    assert result.pending_actions[0].choices == ("approve", "reject")
    assert "arguments" not in json.dumps(result.pending_actions[0].to_payload())
    assert "_fingerprint" not in result.pending_actions[0].to_payload()


async def test_exact_approval_is_internal_while_main_receives_original_asr(voice_api, monkeypatch):
    pending = voice_pending_actions([card()])
    prepare = AsyncMock(return_value='{"hitl_id":"approval-1","action":"approve"}')
    monkeypatch.setattr(chat_voice, "prepare_voice_approval_reply", prepare)
    voice_api.request.state.voice_origin_message_id = "previous-origin"
    voice_api.request.state.voice_approval_action = "previous-state"

    async def dispatch(**kwargs):
        assert kwargs["message"] == "同意"
        assert kwargs["conversation_id"] == "conversation"
        assert kwargs["request"].state.voice_origin_message_id == "voice-source"
        assert json.loads(kwargs["request"].state.voice_approval_action) == {
            "hitl_id": "approval-1", "action": "approve",
        }
        return ChatMessageResponse(conversation_id="conversation", content="Created.")

    voice_api.dispatch.side_effect = dispatch
    result = await invoke(
        voice_api, "同意", origin_message_id="voice-source",
        pending_action_id="approval-1", presented_actions=pending,
    )
    assert result.status == "ok"
    assert prepare.await_args.kwargs["presented_actions"] == pending
    assert prepare.await_args.kwargs["user_id"] == "owner"
    assert voice_api.request.state.voice_origin_message_id == "previous-origin"
    assert voice_api.request.state.voice_approval_action == "previous-state"


async def test_question_preserves_pending_without_inventing_approval(voice_api, monkeypatch):
    pending = voice_pending_actions([card()])
    monkeypatch.setattr(chat_voice, "prepare_voice_approval_reply", AsyncMock(return_value=None))
    chat_voice.load_voice_pending_actions.return_value = pending

    async def dispatch(**kwargs):
        assert kwargs["message"] == "为什么需要确认？"
        assert kwargs["request"].state.voice_approval_action is None
        return ChatMessageResponse(conversation_id="conversation", content="It changes the schedule.")

    voice_api.dispatch.side_effect = dispatch
    result = await invoke(voice_api, "为什么需要确认？", presented_actions=pending)
    assert result.status == "approval_required"
    assert result.pending_actions == pending


@pytest.mark.parametrize("code", ["approval_stale", "approval_changed", "approval_target_ambiguous", "approval_not_presented", "approval_requires_ui"])
async def test_blocked_confirmation_never_falls_back_to_main(voice_api, monkeypatch, code):
    monkeypatch.setattr(chat_voice, "prepare_voice_approval_reply", AsyncMock(side_effect=VoiceApprovalReplyError(code, "No action was approved.")))
    save = AsyncMock()
    monkeypatch.setattr(conversation_messages, "add_message", save)
    result = await invoke(voice_api, "确认", pending_action_id="approval-1", presented_actions=voice_pending_actions([card()]))
    assert result.status == "approval_required"
    assert result.spoken_reply == "No action was approved."
    voice_api.dispatch.assert_not_awaited()
    assert save.await_args.kwargs["meta"] == {"voice_approval_error": code}
    voice_api.db.commit.assert_awaited_once()


async def test_public_call_cannot_submit_an_internal_voice_approval(voice_api, monkeypatch):
    monkeypatch.setattr(chat_voice, "enforce_public_audio_budget", AsyncMock())
    dispatch = AsyncMock()
    monkeypatch.setattr(public_chat, "send_message", dispatch)
    voice_api.start = chat_voice.CallStart(public_token="public", session_id="visitor")
    result = await invoke(voice_api, "approve", pending_action_id="approval-1")
    assert result.status == "approval_required"
    dispatch.assert_not_awaited()
    voice_api.dispatch.assert_not_awaited()


async def test_internal_approval_state_is_restored_when_main_fails(voice_api, monkeypatch):
    monkeypatch.setattr(chat_voice, "prepare_voice_approval_reply", AsyncMock(return_value='{"hitl_id":"approval-1","action":"approve"}'))
    voice_api.request.state.voice_approval_action = "previous-state"
    voice_api.dispatch.side_effect = RuntimeError("Main unavailable")
    with pytest.raises(RuntimeError, match="Main unavailable"):
        await invoke(voice_api, "approve", presented_actions=voice_pending_actions([card()]))
    assert voice_api.request.state.voice_approval_action == "previous-state"


class ApprovalBoundaryReached(Exception):
    pass


@pytest.fixture
def main_boundary(monkeypatch):
    """Exercise the real non-streaming entrypoint up to its approval boundary."""

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.voice_session_mode = "chat_gateway"
    user = SimpleNamespace(id="owner", entity_id="entity", role="owner")
    db = AsyncMock()
    conversation = SimpleNamespace(id="conversation", workspace_id=None, thread_ref_kind=None, thread_ref_id=None)
    budget = AsyncMock(return_value=False)
    resolver = AsyncMock(side_effect=ApprovalBoundaryReached)
    monkeypatch.setattr(chat, "_require_chat_budget_unless_pending_approval", budget)
    monkeypatch.setattr(chat, "_resolve_chat_workspace_scope", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(chat, "_resolve_task_session_request_agent", AsyncMock(return_value="agent"))
    monkeypatch.setattr(chat, "_conversation_video_edit_route_state", AsyncMock())

    async def attachments(message, *_args, **_kwargs):
        return SimpleNamespace(cleaned_message=message, attachments=SimpleNamespace(attachment_refs=[]))

    async def skill_turn(_db, **kwargs):
        return SimpleNamespace(manual_skill_refs=None, llm_base_message=kwargs["message"], saved_user_base=kwargs["message"])

    monkeypatch.setattr(chat, "_build_attachments", attachments)
    monkeypatch.setattr(chat, "_prepare_manual_skill_turn", skill_turn)
    monkeypatch.setattr(chat, "_chat_mode_runtime_prompt", lambda *_: "")
    monkeypatch.setattr(chat, "_chat_mode_direct_tool_calls", lambda **_: [])
    monkeypatch.setattr(chat, "_stream_llm_message_with_attachments", lambda message, *_: message)
    monkeypatch.setattr(chat, "_message_with_chat_mode_marker", lambda message, *_: message)
    monkeypatch.setattr(chat, "get_or_create_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(chat, "resolve_chat_approval_turn", resolver)

    async def invoke_main(message):
        return await chat.chat_message(
            request=request, message=message, conversation_id="conversation", agent_id="agent",
            workspace_id=None, workspace_context=False, thread_ref_kind=None, thread_ref_id=None,
            document_ids=None, manual_skill_ids=None, manual_skill_refs=None, chat_mode=None,
            chat_mode_payload=None, blocked_tools=None, editor_context=None, files=[], user=user, db=db,
        )

    return SimpleNamespace(request=request, db=db, budget=budget, resolver=resolver, invoke=invoke_main)


@pytest.mark.parametrize("utterance", ["确认", "为什么要确认？", '{"hitl_id":"invented","action":"approve"}'])
async def test_voice_main_never_implicitly_resolves_raw_speech(main_boundary, utterance):
    with pytest.raises(ApprovalBoundaryReached):
        await main_boundary.invoke(utterance)
    assert main_boundary.resolver.await_args.kwargs["message"] == ""


async def test_voice_and_card_share_exact_pending_budget_preflight(main_boundary):
    action = '{"hitl_id":"approval-1","action":"reject"}'
    main_boundary.request.state.voice_approval_action = action
    with pytest.raises(ApprovalBoundaryReached):
        await main_boundary.invoke("拒绝")
    assert main_boundary.budget.await_args.kwargs["message"] == action
    assert main_boundary.resolver.await_args.kwargs["message"] == action


async def test_text_main_keeps_its_existing_confirmation_contract(main_boundary):
    main_boundary.request.state.voice_session_mode = None
    with pytest.raises(ApprovalBoundaryReached):
        await main_boundary.invoke("确认")
    assert main_boundary.resolver.await_args.kwargs["message"] == "确认"


async def test_disappeared_voice_approval_stops_before_model_or_tools(main_boundary, monkeypatch):
    main_boundary.request.state.voice_approval_action = '{"hitl_id":"approval-1","action":"approve"}'
    main_boundary.resolver.side_effect = None
    main_boundary.resolver.return_value = (None, None, True, None)
    save = AsyncMock(side_effect=lambda *_args, **kwargs: SimpleNamespace(id="saved", content=kwargs["content"]))
    runtime = AsyncMock()
    monkeypatch.setattr(chat, "add_message", save)
    monkeypatch.setattr(chat, "runtime_run_chat_turn", runtime)
    result = await main_boundary.invoke("确认")
    assert result.stop_reason == "approval_unavailable"
    assert save.await_args_list[0].kwargs["content"] == "确认"
    assert result.message_id == "saved"
    runtime.assert_not_awaited()
