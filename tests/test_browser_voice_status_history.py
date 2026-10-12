"""Completed durable voice work remains queryable without re-execution."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from packages.core.services.voice.live_agent import LiveVoiceTool
from packages.core.services.voice.realtime import BridgeCall
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from packages.core.services.voice.work_router import classify_voice_work_followup
from packages.core.services.voice.work_types import VoiceWorkAction, VoiceWorkContext, VoiceWorkState
from tests.test_browser_voice import eventually, session

pytestmark = pytest.mark.unit


def _status_call():
    return BridgeCall(
        "status-call",
        "What did you find?",
        "status-response",
        action=VoiceWorkAction.STATUS,
        tool=LiveVoiceTool.ACTIVE_WORK,
        transcript_sent=True,
    )


def _function_outputs(provider):
    return [
        json.loads(event["item"]["output"])
        for event in provider.events
        if event.get("item", {}).get("type") == "function_call_output"
    ]


@pytest.mark.parametrize("age_seconds", [31, 1800, None])
@pytest.mark.parametrize(
    ("state", "outcome_status", "reply_snippet"),
    [
        (VoiceWorkState.COMPLETED, "ok", "The result is: Latest result"),
        (VoiceWorkState.FAILED, "error", "That task did not finish"),
        (VoiceWorkState.INTERRUPTED, "cancelled", "The stop request was sent"),
    ],
)
async def test_status_reads_latest_durable_receipt_after_recent_window(
    age_seconds, state, outcome_status, reply_snippet,
):
    call, _, provider = session()
    receipt = VoiceWorkReceipt("latest-work", "latest-message", "Find current listings")
    call.last_work_receipt = receipt
    call.last_work_completed_at = (
        time.monotonic() - age_seconds if age_seconds is not None else None
    )
    call.last_work_status = outcome_status
    context = VoiceWorkContext(
        request=receipt.text,
        state=state,
        assistant_output="Latest result",
    )
    decision = classify_voice_work_followup(
        "What did you find?", context, requested_action=VoiceWorkAction.STATUS,
    )
    call.route_work_action = AsyncMock(return_value=decision)
    call.route_followup = AsyncMock()
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock()
    worker = asyncio.create_task(call.run_turns())
    try:
        await call.turns.put((0, _status_call()))
        await eventually(lambda: not call.requests.empty())

        call.engine.check_access.assert_awaited_once()
        call.route_work_action.assert_awaited_once_with(
            "What did you find?", receipt, VoiceWorkAction.STATUS,
        )
        call.route_followup.assert_not_awaited()
        output, = _function_outputs(provider)
        assert output["status"] == "action_handled"
        assert output["response_text"] == decision.reply
        assert reply_snippet in output["response_text"]
        call.admit_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_status_uses_legacy_scoped_lookup_for_old_receipt():
    call, _, provider = session()
    receipt = VoiceWorkReceipt("latest-work", "latest-message", "Run the report")
    call.last_work_receipt = receipt
    call.last_work_completed_at = time.monotonic() - 1800
    call.last_work_status = "ok"
    decision = classify_voice_work_followup(
        "What did you find?",
        VoiceWorkContext(request=receipt.text, state=VoiceWorkState.COMPLETED),
        requested_action=VoiceWorkAction.STATUS,
    )
    call.route_followup = AsyncMock(return_value=decision)
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock()
    worker = asyncio.create_task(call.run_turns())
    try:
        await call.turns.put((0, _status_call()))
        await eventually(lambda: not call.requests.empty())

        call.route_followup.assert_awaited_once_with("What did you find?", receipt)
        output, = _function_outputs(provider)
        assert output["response_text"] == decision.reply
        call.admit_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_each_status_lookup_uses_the_latest_receipt():
    call, _, provider = session()
    older = VoiceWorkReceipt("older-work", "older-message", "Older report")
    latest = VoiceWorkReceipt("latest-work", "latest-message", "Latest report")
    call.last_work_completed_at = time.monotonic() - 1800
    call.last_work_status = "ok"

    async def lookup(text, receipt, action):
        return classify_voice_work_followup(
            text,
            VoiceWorkContext(
                request=receipt.text,
                state=VoiceWorkState.COMPLETED,
                assistant_output=receipt.text,
            ),
            requested_action=action,
        )

    call.route_work_action = AsyncMock(side_effect=lookup)
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock()
    worker = asyncio.create_task(call.run_turns())
    try:
        for count, receipt in enumerate((older, latest), 1):
            call.last_work_receipt = receipt
            await call.turns.put((0, _status_call()))
            await eventually(lambda: len(_function_outputs(provider)) == count)
            call.route_work_action.assert_awaited_with(
                "What did you find?", receipt, VoiceWorkAction.STATUS,
            )
            assert receipt.text in _function_outputs(provider)[-1]["response_text"]

        assert call.route_work_action.await_count == 2
        call.admit_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("denied_at", ["preflight", "scoped_lookup"])
async def test_old_receipt_status_fails_closed_when_access_is_revoked(denied_at):
    call, _, provider = session()
    call.last_work_receipt = VoiceWorkReceipt("work", "message", "Run the report")
    call.last_work_completed_at = time.monotonic() - 1800
    call.last_work_status = "ok"
    call.route_work_action = AsyncMock()
    denied = (
        call.engine.check_access if denied_at == "preflight" else call.route_work_action
    )
    denied.side_effect = PermissionError("Access revoked")
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock()
    worker = asyncio.create_task(call.run_turns())
    try:
        await call.turns.put((0, _status_call()))
        with pytest.raises(PermissionError, match="Access revoked"):
            await asyncio.wait_for(worker, 2)

        call.engine.check_access.assert_awaited_once()
        if denied_at == "preflight":
            call.route_work_action.assert_not_awaited()
        else:
            call.route_work_action.assert_awaited_once()
        assert not _function_outputs(provider)
        assert call.requests.empty()
        call.admit_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_status_without_durable_receipt_keeps_recent_window():
    call, _, provider = session()
    call.last_work_completed_at = time.monotonic() - 1800
    call.last_work_status = "ok"
    call.route_work_action = AsyncMock()
    call.admit_work = AsyncMock()
    call.execute_work = AsyncMock()
    worker = asyncio.create_task(call.run_turns())
    try:
        await call.turns.put((0, _status_call()))
        await eventually(lambda: not call.requests.empty())

        output, = _function_outputs(provider)
        assert output["result"] == "idle"
        call.route_work_action.assert_not_awaited()
        call.admit_work.assert_not_awaited()
        call.execute_work.assert_not_awaited()
        call.agent.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
