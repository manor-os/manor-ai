"""Live speaks Main HITL and forwards exact user replies without granting it."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.services.voice.approvals import VoicePendingAction
from packages.core.services.voice.browser import BrowserAgentWork, BrowserLiveRequest
from packages.core.services.voice.live_agent import (
    LiveVoiceTextDecision,
    LiveVoiceTool,
    LiveVoiceTurnAction,
    live_voice_session_factory,
)
from packages.core.services.voice.realtime import (
    BridgeCall,
    VoiceAgentOutcome,
    build_function_output_event,
    parse_completed_bridge_call,
)
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from tests.test_browser_voice import done, eventually, session, transcription
from tests.test_browser_voice_admission import (
    _commit,
    _execute_one_work,
    _running_call,
    _supersede_complete_instruction,
)
from tests.test_gateway_voice import gateway_session, pcm

pytestmark = pytest.mark.unit


def pending(action_id="approval-1"):
    return VoicePendingAction(
        id=action_id,
        title="Create daily check",
        description="Check every day at 09:00 and notify me.",
        choices=("approve", "reject"),
        requires_ui=False,
        _fingerprint="server-only-fingerprint",
    )


def bridge_event(*, action_id, response_id="reply", tool=LiveVoiceTool.DELEGATE):
    event = done(response_id, function=True, tool_name=tool.value, utterance="model paraphrase")
    event["response"]["output"][0]["arguments"] = json.dumps({
        "utterance": "model paraphrase", "pending_action_id": action_id,
    })
    return event


def test_live_delegation_preserves_target_without_adding_an_approval_tool():
    tools = live_voice_session_factory.tools()
    assert len(tools) == 3
    delegate = next(tool for tool in tools if tool["name"] == LiveVoiceTool.DELEGATE.value)
    assert delegate["parameters"]["required"] == ["utterance"]
    assert "pending_action_id" in delegate["parameters"]["properties"]
    decision = live_voice_session_factory.text_decision(None, [{
        "name": LiveVoiceTool.DELEGATE.value,
        "arguments": {"utterance": "确认", "pending_action_id": "approval-1"},
    }])
    assert decision.pending_action_id == "approval-1"
    call = parse_completed_bridge_call(
        bridge_event(action_id="approval-1"), seen_event_ids=set(), seen_call_ids=set(),
    )
    assert call.pending_action_id == "approval-1"
    assert call.presented_actions == ()
    instructions = live_voice_session_factory.instructions()
    assert "never treat it as a backchannel" in instructions
    assert "Login, MFA, CAPTCHA" in instructions
    assert "not successful execution" in live_voice_session_factory.result_presentation_instructions()


@pytest.mark.parametrize("action_id", [None, "", "  ", 7, [], "a" * 129])
def test_native_parser_rejects_invalid_target_values(action_id):
    assert parse_completed_bridge_call(
        bridge_event(action_id=action_id), seen_event_ids=set(), seen_call_ids=set(),
    ) is None
    with pytest.raises(RuntimeError, match="invalid pending action"):
        live_voice_session_factory.text_decision(None, [{
            "name": LiveVoiceTool.DELEGATE.value,
            "arguments": {"utterance": "yes", "pending_action_id": action_id},
        }])


def test_structured_outcome_exposes_display_data_not_approval_fingerprint():
    event = build_function_output_event(
        call_id="call", status="approval_required", spoken_reply="Please approve.",
        pending_actions=(pending(),),
    )
    payload = json.loads(event["item"]["output"])
    assert payload["pending_actions"] == [pending().to_payload(item_number=1)]
    assert "fingerprint" not in event["item"]["output"]


@pytest.mark.parametrize("asr_first", [True, False])
@pytest.mark.parametrize("reply", ["确认", "拒绝"])
async def test_native_wait_reply_uses_only_its_final_asr_not_old_input(asr_first, reply):
    async with _running_call() as (call, _, provider, tasks):
        await _supersede_complete_instruction(call, provider)
        earlier = call.pending_live_inputs["first-input"]
        actions = (pending(),)
        call.presented_pending_actions = actions
        call.admit_input_work = AsyncMock(
            side_effect=lambda text, _ids, **_context: VoiceWorkReceipt("reply-work", "reply-message", text)
        )
        current = await _commit(call, provider, "reply-input", "reply-response", start_speech=False)
        if asr_first:
            await call.handle_provider(transcription("reply-input", transcript=reply))
        await call.handle_provider(done(
            "reply-response", function=True, tool_name=LiveVoiceTool.WAIT.value,
            utterance="Do a different operation.",
        ))
        if not asr_first:
            assert call.turns.empty()
            await call.handle_provider(transcription("reply-input", transcript=reply))
        assert await _execute_one_work(call, tasks) == reply
        call.admit_input_work.assert_awaited_once_with(
            reply, (current.user_message_id,), pending_action_id="approval-1", presented_actions=actions,
        )
        assert not earlier.input_consumed
        assert call.presented_pending_actions == ()


async def test_native_exact_target_and_snapshot_survive_admission():
    async with _running_call() as (call, _, provider, tasks):
        actions = (pending(), pending("approval-2"))
        call.presented_pending_actions = actions
        call.admit_input_work = AsyncMock(
            side_effect=lambda text, _ids, **_context: VoiceWorkReceipt("reply-work", "reply-message", text)
        )
        current = await _commit(call, provider, "reply-input", "reply-response")
        await call.handle_provider(bridge_event(action_id="approval-2", response_id="reply-response"))
        assert call.turns.empty()
        await call.handle_provider(transcription("reply-input", transcript="批准第二项"))
        assert await _execute_one_work(call, tasks) == "批准第二项"
        call.admit_input_work.assert_awaited_once_with(
            "批准第二项", (current.user_message_id,),
            pending_action_id="approval-2", presented_actions=actions,
        )


@pytest.mark.parametrize("visible_before_input,reply", [(False, "确认"), (True, "嗯……")])
async def test_native_wait_never_approves_unseen_or_unrelated_input(visible_before_input, reply):
    async with _running_call() as (call, _, provider, tasks):
        if visible_before_input:
            call.presented_pending_actions = (pending(),)
        await _commit(call, provider, "input", "response")
        call.presented_pending_actions = (pending(),)
        await call.handle_provider(done("response", function=True, tool_name=LiveVoiceTool.WAIT.value))
        await call.handle_provider(transcription("input", transcript=reply))
        tasks.append(asyncio.create_task(call.run_turns()))
        await eventually(lambda: call.turns.empty())
        call.admit_input_work.assert_not_awaited()
        call.admit_work.assert_not_awaited()


@pytest.mark.parametrize("reordered", [False, True])
async def test_native_registers_pending_only_when_result_audio_is_sent(reordered):
    call, _, provider = session()
    previous = (pending(), pending("approval-2")) if reordered else ()
    actions = tuple(reversed(previous)) if reordered else (pending(),)
    call.presented_pending_actions = previous
    outcome = VoiceAgentOutcome("approval_required", "May I create it?", pending_actions=actions)
    task = asyncio.create_task(AsyncMock(return_value=outcome)())
    try:
        work = BrowserAgentWork(0, BridgeCall("call", "Create it", "response"), task, 0)
        request = BrowserLiveRequest(0, "", [], result_work=work, result_outcome=outcome)
        await call._close_pending_work_result(request)
        payload = json.loads(provider.events[-1]["item"]["output"])
        assert payload["pending_actions"] == [
            action.to_payload(item_number=index) for index, action in enumerate(actions, 1)
        ]
        assert call.presented_pending_actions == previous
        call.active_live_request = request
        await call.handle_provider({
            "type": "response.output_audio.delta", "item_id": "spoken-approval",
            "delta": base64.b64encode(b"\0" * 480).decode(),
        })
        assert call.presented_pending_actions == actions
    finally:
        await task


@pytest.mark.parametrize("count,reply", [(1, "确认"), (1, "拒绝"), (2, "确认"), (2, "批准第二项")])
async def test_gateway_wait_for_pending_reply_delegates_original_text_and_snapshot(count, reply):
    call, _ = gateway_session()
    actions = tuple(pending(f"approval-{index}") for index in range(count))
    call.presented_pending_actions = actions
    call.transcribe = AsyncMock(return_value=reply)
    call.foreground_agent = AsyncMock(return_value=LiveVoiceTextDecision(wait_for_user=True))
    call.admit_work = AsyncMock(return_value=VoiceWorkReceipt("work", "message", reply))
    await call.queue_input((1, pcm()))
    call.presented_pending_actions = (pending("newer-not-heard"),)
    runner = asyncio.create_task(call.transcribe_inputs())
    try:
        await eventually(lambda: call.admit_work.await_count == 1)
        expected = {"presented_actions": actions}
        if count == 1:
            expected["pending_action_id"] = actions[0].id
        call.admit_work.assert_awaited_once_with(reply, **expected)
        call.foreground_agent.assert_awaited_once_with(reply, None, presented_actions=actions)
        call.agent.assert_not_awaited()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_gateway_wait_without_presented_approval_remains_silent():
    call, _ = gateway_session()
    call.transcribe = AsyncMock(return_value="确认")
    call.foreground_agent = AsyncMock(return_value=LiveVoiceTextDecision(wait_for_user=True))
    call.admit_work = AsyncMock()
    await call.queue_input((1, pcm()))
    call.presented_pending_actions = (pending(),)
    runner = asyncio.create_task(call.transcribe_inputs())
    try:
        await eventually(lambda: call.foreground_agent.await_count == 1)
        call.foreground_agent.assert_awaited_once_with("确认", None)
        call.admit_work.assert_not_awaited()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize("rendered", ["May I create the daily check?", ""])
@pytest.mark.parametrize("reordered", [False, True])
async def test_gateway_renders_structured_hitl_and_only_registers_spoken_result(rendered, reordered):
    call, ws = gateway_session()
    previous = (pending(), pending("approval-2")) if reordered else ()
    actions = tuple(reversed(previous)) if reordered else (pending(),)
    call.presented_pending_actions = previous
    receipt = VoiceWorkReceipt("work", "message", "Create the daily check")
    outcome = VoiceAgentOutcome(
        "approval_required", "", conversation_id="conversation", pending_actions=actions,
    )
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock(return_value=outcome)
    call.render_result = AsyncMock(return_value=rendered)
    call.generation = 1
    call.queue_turn((1, receipt.text, receipt))
    worker = asyncio.create_task(call.run_turns())
    try:
        if rendered:
            await eventually(lambda: any(event["type"] == "audio_clip" for event in ws.events))
            assert call.presented_pending_actions == actions
            call.speak.assert_awaited_once_with(rendered, "warm")
        else:
            await eventually(lambda: 1 not in call.turn_timings and call.execute_work.await_count == 1)
            assert call.presented_pending_actions == previous
            call.speak.assert_not_awaited()
        call.render_result.assert_awaited_once_with(receipt.text, outcome)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_answering_one_of_multiple_cards_requires_fresh_presentation_before_another_yes():
    call, _ = gateway_session()
    actions = (pending(), pending("approval-2"))
    call.presented_pending_actions = actions
    call.admit_work = AsyncMock(return_value=VoiceWorkReceipt("work", "message", "yes"))
    await call._admit_agent_work(
        "yes", LiveVoiceTextDecision(action=LiveVoiceTurnAction.DELEGATE, pending_action_id="approval-1"), actions,
    )
    assert call.presented_pending_actions == ()


@pytest.mark.parametrize("cancel_at", [None, "before_delivery", "after_queue"])
async def test_recovered_pending_result_uses_live_renderer_and_respects_cancel(cancel_at):
    call, _, provider = session()
    call.live_instructions = "Fixture Agent identity and ordinary Live safety contract."
    call._send_spoken_turn = AsyncMock()
    outcome = VoiceAgentOutcome(
        "approval_required", "RAW MAIN REQUEST", conversation_id="conversation", pending_actions=(pending(),),
    )
    task = asyncio.create_task(AsyncMock(return_value=outcome)())
    receipt = VoiceWorkReceipt("recovered-work", "message", "Create it", recovered=True)
    work = BrowserAgentWork(0, BridgeCall(None, receipt.text, ""), task, 0, receipt=receipt)
    work.suppress_delivery = cancel_at == "before_delivery"
    call.active_agent_work = work
    runner = None
    try:
        await call._deliver_agent_work(work)
        call._send_spoken_turn.assert_not_awaited()
        assert call.presented_pending_actions == ()
        if cancel_at == "before_delivery":
            assert call.requests.empty()
            return
        assert "work:recovered-work" in call.pending_work_results
        work.suppress_delivery = cancel_at == "after_queue"
        runner = asyncio.create_task(call.generate_responses())
        if cancel_at:
            await eventually(lambda: not call.pending_work_results)
            assert not any(event["type"] == "response.create" for event in provider.events)
        else:
            await eventually(lambda: any(event["type"] == "response.create" for event in provider.events))
            response = next(event["response"] for event in provider.events if event["type"] == "response.create")
            assert response["instructions"].startswith(call.live_instructions)
            assert '"pending_actions":[{"id":"approval-1"' in response["instructions"]
            assert "server-only-fingerprint" not in response["instructions"]
            assert "Speak exactly" not in response["instructions"]
            assert "not successful execution" in response["instructions"]
            assert [tool["name"] for tool in response["tools"]] == [LiveVoiceTool.WAIT.value]
        call.agent.assert_not_awaited()
    finally:
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await task


def test_native_next_foreground_input_receives_exact_presented_targets():
    call, _, _ = session()
    call.presented_pending_actions = (pending(), pending("approval-2"))
    request = call._queue_live_response(1, "批准第二项", instructions=None)
    _, event = call.requests.get_nowait()
    assert request.presented_actions == call.presented_pending_actions
    assert event["response"]["instructions"].startswith(call.live_instructions)
    assert '"id":"approval-2"' in event["response"]["instructions"]
    assert "server-only-fingerprint" not in event["response"]["instructions"]
