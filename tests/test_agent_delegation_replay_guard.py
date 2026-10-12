"""A delegated run that ended with an unconfirmed effect parks its session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid


@pytest.mark.asyncio
async def test_unconfirmed_child_result_requires_explicit_continue(db_session):
    from packages.core.services.agent_delegation_session import (
        DelegationSessionInterrupted,
        claim_delegation_session,
        finish_delegation_session,
    )

    scope = dict(
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        conversation_id=generate_ulid(), user_id=generate_ulid(),
        subscription=SimpleNamespace(id=generate_ulid(), agent_id=generate_ulid(), service_key="svc"),
    )
    session, run_id, _messages = await claim_delegation_session(db_session, **scope)
    await finish_delegation_session(
        db_session, session_id=session.id, run_id=run_id, messages=[], context_usage={},
        run_result={"status": "failed", "content": "unknown", "do_not_replay": True},
    )

    with pytest.raises(DelegationSessionInterrupted):
        await claim_delegation_session(db_session, **scope)
    again, _run_id, _ = await claim_delegation_session(db_session, **scope, continue_interrupted=True)
    assert again.id == session.id


@pytest.mark.asyncio
async def test_confirmed_child_result_leaves_the_session_idle(db_session):
    from packages.core.services.agent_delegation_session import (
        claim_delegation_session,
        finish_delegation_session,
    )

    scope = dict(
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        conversation_id=generate_ulid(), user_id=generate_ulid(),
        subscription=SimpleNamespace(id=generate_ulid(), agent_id=generate_ulid(), service_key="svc"),
    )
    session, run_id, _messages = await claim_delegation_session(db_session, **scope)
    await finish_delegation_session(
        db_session, session_id=session.id, run_id=run_id, messages=[], context_usage={},
        run_result={"status": "completed", "content": "done", "do_not_replay": False},
    )

    again, _run_id, _ = await claim_delegation_session(db_session, **scope)
    assert again.id == session.id


_UNCONFIRMED_LEDGER = {"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1", "tool": "publish_post"}]}
_NEXT_STEP = (
    "Check whether the child's last action took effect before delegating again; "
    "then pass continue_interrupted=true."
)


async def _delegate(monkeypatch, *, loop_result, receipts=(), claim_error=None):
    import json
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from packages.core import database
    from packages.core.ai import context
    from packages.core.ai.runtime import harness
    from packages.core.ai.runtime import workspace_delegation_actions as delegation
    from packages.core.services import agent_delegation_session as sessions

    @asynccontextmanager
    async def session():
        yield AsyncMock()

    monkeypatch.setattr(database, "async_session", session)
    sub = SimpleNamespace(id="subscription", agent_id="publisher", service_key="publish", custom_prompt=None)
    monkeypatch.setattr(delegation, "_resolve_workspace_service_agent", AsyncMock(
        return_value=(sub, SimpleNamespace(name="Publisher"), [], None),
    ))
    monkeypatch.setattr(sessions, "claim_delegation_session", AsyncMock(
        return_value=(SimpleNamespace(id="same-session"), "new-run", []),
        side_effect=claim_error,
    ))
    finish = AsyncMock()
    monkeypatch.setattr(sessions, "finish_delegation_session", finish)
    monkeypatch.setattr(context, "build_agent_context", AsyncMock(return_value=SimpleNamespace(
        runtime_envelope=None, system_prompt="Publish", tools=[], task_id=None,
        tool_profile="workspace_agent", allowed_tool_names=set(), model="test", llm_metadata=None,
    )))

    async def loop(**kwargs):
        for receipt in receipts:
            kwargs["on_tool_end"]("publish_post", json.dumps(receipt), 1.0, {})
        return SimpleNamespace(
            messages=[{"role": "assistant", "content": "Done"}], usage={},
            **{"rounds": 1, "tool_calls_made": ["publish_post"], "error": None, "control": None,
               "tool_evidence": {}, **loop_result},
        )

    monkeypatch.setattr(harness, "runtime_execute_chat_agent_loop", loop)
    raw = await delegation.runtime_workspace_delegate_service_action(
        entity_id="entity", workspace_id="workspace", user_id="user", conversation_id="conversation",
        params={"service_key": "publish", "prompt": "Publish the post"},
    )
    return json.loads(raw), finish


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loop_result", "receipts", "parked"),
    [
        # A terminal unconfirmed tool error carries do_not_replay on its control.
        ({"content": "The publish call timed out; its outcome is unknown.", "stop_reason": "tool_error",
          "control": {"kind": "tool_error", "terminal_failure": True, "retryable": False,
                      "do_not_replay": True, "effect_state": "unconfirmed"}}, (), True),
        # A max-rounds child after an unconfirmed call: only the ledger knows.
        ({"content": "Ran out of rounds.", "stop_reason": "max_rounds",
          "tool_evidence": _UNCONFIRMED_LEDGER}, (), True),
        # A failed child after an unconfirmed call: only the ledger knows.
        ({"content": "Gave up.", "stop_reason": "error", "error": "provider_error",
          "tool_evidence": _UNCONFIRMED_LEDGER}, (), True),
        # A child that hit an unconfirmed call, checked it and then succeeded:
        # the sticky ledger floor alone must not park a recovered success.
        ({"content": "Published and verified.", "stop_reason": "completed",
          "tool_evidence": _UNCONFIRMED_LEDGER}, ({"status": "completed"},), False),
        ({"content": "Could not publish.", "stop_reason": "tool_error",
          "control": {"kind": "tool_error", "retryable": True}}, (), False),
    ],
)
async def test_delegation_hands_the_child_replay_fields_to_its_session(monkeypatch, loop_result, receipts, parked):
    payload, finish = await _delegate(monkeypatch, loop_result=loop_result, receipts=receipts)

    assert payload["session_id"] == "same-session"
    finish.assert_awaited_once()
    run_result = finish.await_args.kwargs["run_result"]
    assert (run_result.get("do_not_replay") is True) is parked
    if parked:
        assert run_result["effect_state"] == "unconfirmed"
        # The parent learns about the parked child in the same tool result.
        assert payload["do_not_replay"] is True
        assert payload["effect_state"] == "unconfirmed"
        assert payload["session_status"] == "interrupted"
        assert payload["next_step"] == _NEXT_STEP
    else:
        assert payload["status"] == ("completed" if receipts else payload["status"])
        assert "do_not_replay" not in payload
        assert "session_status" not in payload
        assert "next_step" not in payload


@pytest.mark.asyncio
async def test_interrupted_session_has_its_own_error_code(monkeypatch):
    from packages.core.services.agent_delegation_session import DelegationSessionInterrupted

    payload, finish = await _delegate(
        monkeypatch,
        loop_result={"content": "unused", "stop_reason": "completed"},
        claim_error=DelegationSessionInterrupted("check the previous turn first"),
    )

    assert payload == {"error": "delegation_session_interrupted", "message": "check the previous turn first"}
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_session_keeps_the_unavailable_code(monkeypatch):
    from packages.core.services.agent_delegation_session import DelegationSessionBusy

    payload, _finish = await _delegate(
        monkeypatch,
        loop_result={"content": "unused", "stop_reason": "completed"},
        claim_error=DelegationSessionBusy("still working"),
    )

    assert payload["error"] == "delegation_session_unavailable"


def test_continue_interrupted_is_described_as_continue_not_repeat():
    import json

    from packages.core.ai.runtime.workspace_delegation_actions import delegate_to_agent_schema
    from packages.core.ai.tools.workspace_agent_tools import WORKSPACE_AGENT_SCHEMA

    wording = "Continue after you have checked the previous turn's effects; this does not repeat it."
    item = delegate_to_agent_schema()["function"]["parameters"]["properties"]["tasks"]["items"]
    assert item["properties"]["continue_interrupted"]["description"] == wording
    assert wording in json.dumps(WORKSPACE_AGENT_SCHEMA)
