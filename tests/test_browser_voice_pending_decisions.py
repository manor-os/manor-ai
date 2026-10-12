"""Late ASR reconciles pending decisions without duplicate or lost work."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.services.voice.live_agent import LiveVoiceTool
from packages.core.services.voice.realtime import BridgeCall
from tests.test_browser_voice import done, eventually, session, transcription
from tests.test_browser_voice_admission import (
    FIRST_INSTRUCTION,
    _commit,
    _execute_one_work,
    _mixed_response,
    _running_call,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("first_asr_first", [False, True])
async def test_newest_pending_delegate_combines_inputs_before_older_decision_runs(first_asr_first):
    async with _running_call() as (call, _, provider, tasks):
        first = await _commit(call, provider, "first-input", "first-response")
        await _mixed_response(call, "first-response")
        assert first.pending_bridge_call is not None

        await _commit(call, provider, "tail-input", "tail-response")
        tail = "Then send me the direct links."
        await _mixed_response(call, "tail-response")
        assert call.turns.empty()

        finals = [
            transcription("first-input", transcript=FIRST_INSTRUCTION),
            transcription("tail-input", transcript=tail),
        ]
        if not first_asr_first:
            finals.reverse()
        await call.handle_provider(finals[0])
        assert call.turns.empty()
        await call.handle_provider(finals[1])
        assert call.turns.qsize() == 1
        assert await _execute_one_work(call, tasks) == f"{FIRST_INSTRUCTION}\n{tail}"
        assert first.input_consumed
        closed_first = [
            json.loads(event["item"]["output"])
            for event in provider.events
            if event.get("item", {}).get("call_id") == "call-first-response"
        ]
        assert len(closed_first) == 1
        assert closed_first[0]["status"] == "no_reply"


async def _finish_control_reply(call, *, item_id, response_id):
    await eventually(
        lambda: (
            call.active_live_request is not None
            and call.active_live_request.source_item_id == item_id
            and call.active_live_request.response_started
        )
    )
    await call.handle_provider({"type": "response.created", "response": {"id": response_id}})
    await call.handle_provider({
        "type": "response.output_audio.delta",
        "response_id": response_id,
        "item_id": f"{response_id}-audio",
        "delta": base64.b64encode(b"\0" * 480).decode(),
    })
    await call.handle_provider(done(response_id, transcript="There is no running task."))


@pytest.mark.parametrize("action", ["status", "cancel"])
async def test_control_reply_preserves_pending_delegate_unless_explicitly_cancelled(action):
    async with _running_call() as (call, _, provider, tasks):
        first = await _commit(call, provider, "first-input", "first-response")
        await _mixed_response(call, "first-response")
        assert first.pending_bridge_call is not None

        await _commit(call, provider, "control-input", "control-response")
        control_text = "What did you find?" if action == "status" else "Cancel that search."
        await call.handle_provider(transcription("control-input", transcript=control_text))
        await call.handle_provider(done("control-response", function=True, action=action))
        assert first.input_consumed is (action == "cancel")
        tasks.append(asyncio.create_task(call.run_turns()))
        await _finish_control_reply(
            call, item_id="control-input", response_id="control-confirmation",
        )
        # Finishing a spoken status reply is not a direct answer to the older
        # request, and must not consume its already-chosen execution decision.
        assert first.input_consumed is (action == "cancel")
        call.admit_input_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()

        await call.handle_provider(transcription("first-input", transcript=FIRST_INSTRUCTION))
        if action == "status":
            await eventually(lambda: call.execute_work.await_count == 1)
            call.admit_input_work.assert_awaited_once_with(
                FIRST_INSTRUCTION, (first.user_message_id,),
            )
            assert call.execute_work.await_args.args[0].text == FIRST_INSTRUCTION
        else:
            assert call.turns.empty()
            call.admit_input_work.assert_not_awaited()
            call.execute_work.assert_not_awaited()
        call.admit_work.assert_not_awaited()
        call.agent.assert_not_awaited()


async def test_finalized_empty_groups_do_not_fill_unhandled_input_limit():
    call, _, provider = session()
    call.settle_input_transcription = AsyncMock()
    call.engine.settle_transcription = AsyncMock()
    call.record_control_turn = AsyncMock()
    call.admit_input_work = AsyncMock()
    call.execute_work = AsyncMock()

    for index in range(33):
        item_id = f"empty-input-{index}"
        await call.handle_provider({"type": "input_audio_buffer.speech_started"})
        await call.handle_provider({"type": "input_audio_buffer.committed", "item_id": item_id})
        _, event = call.requests.get_nowait()
        request = event["_manor_live_request"]
        request.persist_exchange = False
        request.pending_bridge_call = BridgeCall(
            f"call-{index}", "Not authoritative", f"response-{index}",
            tool=LiveVoiceTool.DELEGATE,
        )
        await call.handle_provider(transcription(item_id, transcript=""))
        assert request.input_consumed
        assert not call.pending_live_inputs
        assert item_id not in call.live_requests_by_source
        # The caller starts speaking before the recovery reply plays. Cleanup
        # must not depend on that reply eventually reaching response.done.
        call.requests.get_nowait()

    outputs = [
        json.loads(event["item"]["output"])
        for event in provider.events
        if event.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(outputs) == 33
    assert all(output["result"] == "input_unavailable" for output in outputs)
    assert call.turns.empty()
    call.record_control_turn.assert_not_awaited()
    call.admit_input_work.assert_not_awaited()
    call.execute_work.assert_not_awaited()
    call.agent.assert_not_awaited()


async def test_empty_status_group_does_not_consume_older_pending_delegate():
    async with _running_call() as (call, _, provider, tasks):
        first = await _commit(call, provider, "first-input", "first-response")
        await _mixed_response(call, "first-response")
        control = await _commit(call, provider, "status-input", "status-response")
        await call.handle_provider(transcription("status-input", transcript=""))
        await call.handle_provider(done("status-response", function=True, action="status"))

        assert control.input_consumed
        assert not first.input_consumed
        assert call.pending_live_inputs == {"first-input": first}
        await call.handle_provider(transcription("first-input", transcript=FIRST_INSTRUCTION))
        assert await _execute_one_work(call, tasks) == FIRST_INSTRUCTION


async def test_empty_combined_group_closes_each_pending_bridge_once():
    async with _running_call() as (call, _, provider, _):
        first = await _commit(call, provider, "first-input", "first-response")
        await _mixed_response(call, "first-response")
        tail = await _commit(call, provider, "tail-input", "tail-response")
        await _mixed_response(call, "tail-response")
        await call.handle_provider(transcription("tail-input", transcript=""))
        await call.handle_provider(transcription("first-input", transcript=""))

        assert first.input_consumed and tail.input_consumed
        assert not call.pending_live_inputs
        assert not call.live_requests_by_source
        function_outputs = [
            event for event in provider.events
            if event.get("item", {}).get("type") == "function_call_output"
        ]
        assert len(function_outputs) == 2
        outputs = {
            event["item"]["call_id"]: json.loads(event["item"]["output"])
            for event in function_outputs
        }
        assert outputs["call-first-response"]["status"] == "no_reply"
        assert outputs["call-tail-response"]["result"] == "input_unavailable"
        assert len(outputs) == 2
        assert call.turns.empty()
        call.record_control_turn.assert_not_awaited()
        call.admit_input_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
