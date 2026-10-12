"""Authoritative voice admission across VAD segments, without a DB or provider."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from packages.core.services.voice.realtime import VoiceAgentOutcome
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from tests.test_browser_voice import done, eventually, session, transcription


FIRST_INSTRUCTION = "Search South Bay independent studios below $1000 per month."
MODEL_PARAPHRASE = "Ignore the caller's budget and search luxury apartments."


@asynccontextmanager
async def _running_call():
    call, ws, provider = session()
    call.record_control_turn = AsyncMock(
        side_effect=lambda _user, _assistant: f"preserved-user-message-{call.record_control_turn.await_count}"
    )
    call.admit_work = AsyncMock(side_effect=lambda text: VoiceWorkReceipt("work", "message", text))
    call.admit_input_work = AsyncMock(
        side_effect=lambda text, _source_message_ids: VoiceWorkReceipt("work", "message", text)
    )
    call.execute_work = AsyncMock(
        return_value=VoiceAgentOutcome(status="ok", spoken_reply="The verified result.", conversation_id="conversation")
    )
    # No usage payload or real provider is involved; keep even settlement
    # callbacks isolated from ambient application/database configuration.
    call.settle_input_transcription = AsyncMock()
    call.settle_response = AsyncMock()
    call.engine.settle_transcription = AsyncMock()
    call.engine.settle_response = AsyncMock()
    tasks = [asyncio.create_task(call.generate_responses())]
    try:
        yield call, ws, provider, tasks
    finally:
        tasks.extend(call.agent_completion_tasks)
        if call.active_agent_work is not None:
            tasks.append(call.active_agent_work.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _commit(call, provider, item_id, response_id, *, start_speech=True):
    if start_speech:
        await call.handle_provider({"type": "input_audio_buffer.speech_started"})
    await call.handle_provider({"type": "input_audio_buffer.committed", "item_id": item_id})
    await eventually(
        lambda: (
            call.active_live_request is not None
            and call.active_live_request.source_item_id == item_id
            and call.active_live_request.response_started
        )
    )
    await call.handle_provider({"type": "response.created", "response": {"id": response_id}})
    return call.active_live_request


async def _supersede_complete_instruction(call, provider):
    await _commit(call, provider, "first-input", "first-response")
    await call.handle_provider(transcription("first-input", transcript=FIRST_INSTRUCTION))
    await call.handle_provider({"type": "input_audio_buffer.speech_started"})
    cancelled = done("first-response", transcript="An unheard response.")
    cancelled["response"]["status"] = "cancelled"
    await call.handle_provider(cancelled)

    call.record_control_turn.assert_awaited_once_with(FIRST_INSTRUCTION, "")
    call.admit_work.assert_not_awaited()
    call.admit_input_work.assert_not_awaited()
    call.execute_work.assert_not_awaited()


async def _mixed_response(call, response_id, *, action=None):
    await call.handle_provider(
        {
            "type": "response.output_audio.delta",
            "response_id": response_id,
            "item_id": f"{response_id}-audio",
            "delta": base64.b64encode(b"\0" * 480).decode(),
        }
    )
    response = done(response_id, function=True, action=action, utterance=MODEL_PARAPHRASE)
    response["response"]["output"].append(
        {
            "type": "message",
            "status": "completed",
            "content": [{"type": "audio", "transcript": "I will check that."}],
        }
    )
    await call.handle_provider(response)
    return response


async def _execute_one_work(call, tasks):
    tasks.append(asyncio.create_task(call.run_turns()))
    await eventually(lambda: call.execute_work.await_count == 1 and call.active_agent_work is None)
    call.admit_input_work.assert_awaited_once()
    call.admit_work.assert_not_awaited()
    call.execute_work.assert_awaited_once()
    call.agent.assert_not_awaited()
    return call.execute_work.await_args.args[0].text


def _assert_input_unavailable(provider, response_id):
    outputs = [
        json.loads(event["item"]["output"])
        for event in provider.events
        if event.get("item", {}).get("call_id") == f"call-{response_id}"
    ]
    assert len(outputs) == 1
    assert outputs[0]["result"] == "input_unavailable"
    assert outputs[0]["status"] == "error"
    assert outputs[0]["work_started"] is False
    assert outputs[0]["response_text"]


@pytest.mark.parametrize("tail", ["", "Then send me the direct links."])
@pytest.mark.parametrize("asr_first", [True, False])
async def test_superseded_instruction_and_tail_admit_one_authoritative_request(tail, asr_first):
    async with _running_call() as (call, ws, provider, tasks):
        await _supersede_complete_instruction(call, provider)
        await _commit(call, provider, "tail-input", "tail-response", start_speech=False)
        final_asr = transcription("tail-input", transcript=tail)
        if asr_first:
            await call.handle_provider(final_asr)
        mixed = await _mixed_response(call, "tail-response")
        await call.handle_provider(mixed)
        if not asr_first:
            assert call.turns.empty()
            await call.handle_provider(final_asr)
        await call.handle_provider(final_asr)

        assert call.turns.qsize() == 1
        admitted_text = await _execute_one_work(call, tasks)
        assert FIRST_INSTRUCTION in admitted_text
        assert admitted_text.count(FIRST_INSTRUCTION) == 1
        if tail:
            assert tail in admitted_text
            assert admitted_text.index(FIRST_INSTRUCTION) < admitted_text.index(tail)
        assert MODEL_PARAPHRASE not in admitted_text
        authoritative_parts = [FIRST_INSTRUCTION, *([tail] if tail else [])]
        assert [args.args for args in call.record_control_turn.await_args_list] == [
            (part, "") for part in authoritative_parts
        ]
        assert call.admit_input_work.await_args.args == (
            admitted_text,
            tuple(f"preserved-user-message-{index + 1}" for index in range(len(authoritative_parts))),
        )
        assert not any(
            event["type"] == "interrupt" and event.get("item_id") == "tail-response-audio" for event in ws.events
        )


@pytest.mark.parametrize("duplicate_transcript", ["", None, " \n\t "])
async def test_empty_duplicate_final_cannot_erase_accepted_authoritative_text(duplicate_transcript):
    async with _running_call() as (call, ws, provider, tasks):
        request = await _commit(call, provider, "input", "response")
        await call.handle_provider(transcription("input", transcript=FIRST_INSTRUCTION))
        duplicate = transcription("input", transcript=duplicate_transcript)
        if duplicate_transcript is None:
            duplicate.pop("transcript")
        await call.handle_provider(duplicate)

        assert request.user_text == FIRST_INSTRUCTION
        assert request.input_transcript_final
        mixed = await _mixed_response(call, "response")
        await call.handle_provider(mixed)
        admitted_text = await _execute_one_work(call, tasks)
        assert admitted_text == FIRST_INSTRUCTION
        call.record_control_turn.assert_awaited_once_with(FIRST_INSTRUCTION, "")
        assert call.admit_input_work.await_args.args == (FIRST_INSTRUCTION, ("preserved-user-message-1",))
        assert [event["text"] for event in ws.events if event["type"] == "transcript"] == [FIRST_INSTRUCTION]


@pytest.mark.parametrize("first_final_after_bridge", [False, True])
async def test_delayed_first_final_waits_and_preserves_committed_input_order(first_final_after_bridge):
    async with _running_call() as (call, _, provider, tasks):
        await _commit(call, provider, "first-input", "first-response")
        await call.handle_provider({"type": "input_audio_buffer.speech_started"})
        cancelled = done("first-response", transcript="An unheard response.")
        cancelled["response"]["status"] = "cancelled"
        await call.handle_provider(cancelled)
        call.record_control_turn.assert_not_awaited()
        await _commit(call, provider, "tail-input", "tail-response", start_speech=False)
        tail = "Then send me the direct links."
        await call.handle_provider(transcription("tail-input", transcript=tail))
        first_final = transcription("first-input", transcript=FIRST_INSTRUCTION)
        if not first_final_after_bridge:
            await call.handle_provider(first_final)
        await _mixed_response(call, "tail-response")
        if first_final_after_bridge:
            assert call.turns.empty()
            call.admit_input_work.assert_not_awaited()
            await call.handle_provider(first_final)
        await call.handle_provider(first_final)

        assert call.turns.qsize() == 1
        admitted_text = await _execute_one_work(call, tasks)
        assert admitted_text == f"{FIRST_INSTRUCTION}\n{tail}"
        recorded = call.record_control_turn.await_args_list
        assert len(recorded) == 2
        source_ids_by_text = {
            args.args[0]: f"preserved-user-message-{index + 1}" for index, args in enumerate(recorded)
        }
        assert all(args.args[1] == "" for args in recorded)
        assert call.admit_input_work.await_args.args == (
            admitted_text,
            (source_ids_by_text[FIRST_INSTRUCTION], source_ids_by_text[tail]),
        )


async def test_mixed_audio_and_delegate_does_not_interrupt_but_new_speech_does():
    async with _running_call() as (call, ws, provider, _):
        await _commit(call, provider, "input", "response")
        await call.handle_provider(transcription("input", transcript=FIRST_INSTRUCTION))
        interrupts_before = sum(event["type"] == "interrupt" for event in ws.events)
        await _mixed_response(call, "response")

        assert sum(event["type"] == "interrupt" for event in ws.events) == interrupts_before
        assert call.turns.qsize() == 1
        previous_generation = call.generation
        await call.handle_provider({"type": "input_audio_buffer.speech_started"})
        assert ws.events[-1] == {
            "type": "interrupt",
            "item_id": "response-audio",
            "generation": previous_generation + 1,
        }


async def test_explicit_cancel_does_not_execute_the_superseded_pending_request():
    async with _running_call() as (call, _, provider, tasks):
        await _supersede_complete_instruction(call, provider)
        await _commit(call, provider, "cancel-input", "cancel-response", start_speech=False)
        await call.handle_provider(transcription("cancel-input", transcript="Cancel that search."))
        cancel = done("cancel-response", function=True, action="cancel", utterance=MODEL_PARAPHRASE)
        await call.handle_provider(cancel)
        await call.handle_provider(cancel)
        tasks.append(asyncio.create_task(call.run_turns()))
        await eventually(
            lambda: any(event.get("item", {}).get("call_id") == "call-cancel-response" for event in provider.events)
        )

        call.admit_work.assert_not_awaited()
        call.admit_input_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
        assert call.turns.empty()

        # Finish the Live control reply, then prove a later empty segment cannot
        # revive the instruction that the caller explicitly cancelled.
        await eventually(
            lambda: (
                call.active_live_request is not None
                and call.active_live_request.source_item_id == "cancel-input"
                and call.active_live_request.response_started
            )
        )
        await call.handle_provider({"type": "response.created", "response": {"id": "cancel-confirmation"}})
        await call.handle_provider(
            {
                "type": "response.output_audio.delta",
                "response_id": "cancel-confirmation",
                "item_id": "cancel-confirmation-audio",
                "delta": base64.b64encode(b"\0" * 480).decode(),
            }
        )
        await call.handle_provider(done("cancel-confirmation", transcript="Cancelled."))
        await _commit(call, provider, "later-empty-input", "later-empty-response")
        await call.handle_provider(transcription("later-empty-input", transcript=""))
        await _mixed_response(call, "later-empty-response")

        _assert_input_unavailable(provider, "later-empty-response")
        assert call.turns.empty()
        call.admit_work.assert_not_awaited()
        call.admit_input_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()


async def test_completed_direct_answer_does_not_leak_old_pending_input_into_later_delegation():
    async with _running_call() as (call, _, provider, _):
        await _supersede_complete_instruction(call, provider)
        await _commit(call, provider, "direct-input", "direct-response", start_speech=False)
        await call.handle_provider(transcription("direct-input", transcript="What is two plus two?"))
        await call.handle_provider(
            {
                "type": "response.output_audio.delta",
                "response_id": "direct-response",
                "item_id": "direct-audio",
                "delta": base64.b64encode(b"\0" * 480).decode(),
            }
        )
        await call.handle_provider(done("direct-response", transcript="Four."))

        await _commit(call, provider, "later-empty-input", "later-empty-response")
        await call.handle_provider(transcription("later-empty-input", transcript=""))
        await _mixed_response(call, "later-empty-response")

        _assert_input_unavailable(provider, "later-empty-response")
        assert call.turns.empty()
        call.admit_work.assert_not_awaited()
        call.admit_input_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
