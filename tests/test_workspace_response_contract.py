from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.final_response import FinalResponseAction
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.workspace_response import (
    WorkspaceChatResponseContract, workspace_chat_response_contract,
)
from packages.core.services.workspace_execution_evidence import (
    ExecutionEvidenceScope, ExecutionObservationState, task_execution_state,
)

pytestmark = pytest.mark.unit


def scope(workspace="workspace"):
    return ExecutionEvidenceScope("entity", workspace, "user", "conversation", "message")


def fact(*, revision="a" * 64, origin="current_turn", task_id="task"):
    return {"kind": "task", "id": task_id, "revision": revision, "title": "Real task",
            "state": "accepted", "url": f"/tasks/{task_id}", "origin": origin}


def summary(*, revision=None, origin="current_turn", task_id="task"):
    ref = {"kind": "task", "id": task_id, "scope": origin}
    if revision is not None:
        ref["revision"] = revision
    return json.dumps({"kind": "execution_summary", "references": [ref]})


@pytest.mark.asyncio
async def test_ordinary_zero_tool_answer_needs_no_execution_read():
    async def read(*args):
        raise AssertionError("Ordinary answers should not query execution state")
    decision = await WorkspaceChatResponseContract(scope(), reader=read).validate(
        '{"kind":"answer","text":"DAG means directed acyclic graph."}'
    )
    assert decision.action is FinalResponseAction.ACCEPT
    assert decision.content == "DAG means directed acyclic graph."


@pytest.mark.parametrize("content", [
    "Task has been dispatched", '{"kind":"answer","text":""}',
    '{"kind":"answer","kind":"execution_summary","text":"ok"}',
    '{"kind":"execution_summary","references":[],"status":"running"}',
])
@pytest.mark.asyncio
async def test_invalid_reply_contract_returns_shared_diagnostic(content):
    decision = await WorkspaceChatResponseContract(scope()).validate(content)
    assert decision.action is FinalResponseAction.REVISE_REPLY
    assert decision.diagnostic["contract"] == "tool_error"
    assert decision.diagnostic["tool"] == "final_response"
    assert decision.content == ""


@pytest.mark.asyncio
async def test_execution_revision_read_then_revalidation_and_server_rendering():
    current = [fact()]
    async def read(*args):
        return current
    contract = WorkspaceChatResponseContract(scope(), locale="zh", reader=read)
    first = await contract.validate(summary())
    assert first.action is FinalResponseAction.REVISE_REPLY
    assert first.diagnostic["details"]["facts"] == current
    accepted = await contract.validate(summary(revision="a" * 64))
    assert accepted.action is FinalResponseAction.ACCEPT
    assert "等待派发" in accepted.content
    assert "正在执行" not in accepted.content
    current[:] = [fact(revision="b" * 64)]
    stale = await contract.validate(summary(revision="a" * 64))
    assert stale.action is FinalResponseAction.REVISE_REPLY
    assert stale.diagnostic["error"] == "execution_revision_required"


@pytest.mark.asyncio
async def test_old_record_does_not_prove_current_turn_dispatch():
    async def read(*args):
        return [fact(origin="existing_record")]
    contract = WorkspaceChatResponseContract(scope(), reader=read)
    result = await contract.validate(summary(revision="a" * 64))
    assert result.action is FinalResponseAction.CONTINUE_EXECUTION
    assert result.diagnostic["error"] == "execution_origin_mismatch"
    result = await contract.validate(summary(revision="a" * 64, origin="existing_record"))
    assert result.action is FinalResponseAction.ACCEPT
    assert "Existing task" in result.content


@pytest.mark.asyncio
async def test_unknown_reference_is_not_evidence_of_no_effect():
    async def read(*args):
        return []
    result = await WorkspaceChatResponseContract(scope(), reader=read).validate(summary())
    assert result.action is FinalResponseAction.REVISE_REPLY
    assert "Do not infer" in result.diagnostic["message"]


@pytest.mark.asyncio
async def test_concurrent_workspaces_keep_scope_and_revision_isolated():
    async def read(current_scope, references):
        await asyncio.sleep(0)
        assert references[0]["id"] == current_scope.workspace_id
        return [fact(task_id=current_scope.workspace_id, revision=current_scope.workspace_id)]
    async def run(index):
        task_id = f"workspace-{index}"
        contract = WorkspaceChatResponseContract(scope(task_id), reader=read)
        accepted = await contract.validate(summary(task_id=task_id, revision=task_id))
        assert accepted.action is FinalResponseAction.ACCEPT
        assert [item["id"] for item in accepted.evidence] == [task_id]
    await asyncio.gather(*(run(index) for index in range(64)))


def lease(**changes):
    now = datetime.now(timezone.utc)
    values = dict(status="active", lease_until=now + timedelta(minutes=2),
                  execution_claim_id="preallocated", execution_claimed_at=None, heartbeat_count=0)
    return NS(**(values | changes))


def test_lease_allocation_is_not_execution_and_expired_claim_is_not_live():
    task = NS(status="in_progress")
    assert task_execution_state(task, leases=(lease(),)) is ExecutionObservationState.ASSIGNED
    started = lease(execution_claimed_at=datetime.now(timezone.utc))
    assert task_execution_state(task, leases=(started,)) is ExecutionObservationState.RUNNING
    assert task_execution_state(task, leases=(lease(heartbeat_count=1),)) is ExecutionObservationState.RUNNING
    expired = lease(execution_claimed_at=datetime.now(timezone.utc), lease_until=datetime.now(timezone.utc)-timedelta(seconds=1))
    assert task_execution_state(task, leases=(expired,), admitted=True) is ExecutionObservationState.ACCEPTED


def test_mechanical_completion_does_not_prove_accepted_delivery():
    from packages.core.contracts.artifact_delivery import artifact_delivery_step_binding
    task = NS(status="completed", actual_output={"plan_id": "plan", "supervisor_verdict": "completed"})
    plan = NS(id="plan", status="completed", dispatcher_state={})
    assert task_execution_state(task, plan=plan) is ExecutionObservationState.COMPLETED_UNVERIFIED
    plan.dispatcher_state = {"task_content_acceptance": {"verdict": "accepted", "snapshot_digest": "digest", "checks": [{}]}}
    assert task_execution_state(task, plan=plan) is ExecutionObservationState.COMPLETED_UNVERIFIED
    step = NS(id="step", step_key="deliver", step_status="done", attempt_count=1, params={}, result={"text": "delivered"})
    plan.dispatcher_state["task_content_acceptance"]["artifact_manifest"] = {
        "plan_id": plan.id, "steps": [{**artifact_delivery_step_binding(step), "refs": []}],
    }
    assert task_execution_state(task, plan=plan, steps=(step,)) is ExecutionObservationState.DELIVERED
    step.result = {"text": "changed after acceptance"}
    assert task_execution_state(task, plan=plan, steps=(step,)) is ExecutionObservationState.COMPLETED_UNVERIFIED
    task.actual_output["plan_id"] = "different"
    assert task_execution_state(task, plan=plan) is ExecutionObservationState.COMPLETED_UNVERIFIED


def test_task_title_cannot_inject_a_second_status_or_link():
    from packages.core.ai.runtime.workspace_response import _safe_label
    title = "done](https://evil.test)\n- **Accepted** <img src=x>"
    escaped = _safe_label(title)
    assert "\n" not in escaped and "<img" not in escaped
    assert "\\]\\(" in escaped and "\\*\\*Accepted" in escaped


def test_pending_input_requires_current_plan_step_and_lease():
    from packages.core.services.workspace_execution_evidence import _current_pending_input
    plan = NS(id="plan", status="running")
    step = NS(id="step", plan_id="plan", step_status="waiting_human", current_lease_id="lease")
    current_lease = NS(id="lease", plan_id="plan", step_id="step", status="needs_human", lease_until=datetime.now(timezone.utc)+timedelta(minutes=1))
    request = NS(origin_plan_id="plan", origin_kind="lease", context={"pending_kind": "needs_input"}, dedup_key="lease:lease:needs_input")
    assert _current_pending_input(request, plan, step, current_lease)
    for target, field, invalid in [(request, "origin_plan_id", "old"), (step, "current_lease_id", "new"), (step, "step_status", "done"), (current_lease, "status", "expired"), (current_lease, "lease_until", datetime.now(timezone.utc)-timedelta(seconds=1))]:
        original = getattr(target, field)
        setattr(target, field, invalid)
        assert not _current_pending_input(request, plan, step, current_lease)
        setattr(target, field, original)


def test_only_explicit_workspace_chat_enables_contract():
    values = dict(entity_id="entity", workspace_id="workspace", user_id="user", conversation_id="conversation", metadata={})
    for surface in ChatSurface:
        contract = workspace_chat_response_contract(NS(runtime_envelope=NS(**values, surface=surface)))
        assert bool(contract) is (surface is ChatSurface.WORKSPACE_CHAT)


def tool_schema():
    return {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}


@pytest.mark.asyncio
async def test_loop_buffers_unvalidated_opening_progress_final_and_repairs_same_loop(monkeypatch):
    events = []
    calls = []
    rounds = []
    async def stream(event, payload):
        events.append((event, payload))
    async def model(messages, tools, **kwargs):
        rounds.append("tools")
        await kwargs["stream_handler"]("text_delta", {"content": "I dispatched imaginary work"})
        await kwargs["stream_handler"]("final_response_start", {})
        if len(rounds) == 1:
            return '{"kind":"execution_summary","references":[]}', None, {}
        if len(rounds) == 2:
            assert "continue_execution" in messages[-1]["content"]
            return "Unverified tool preamble", [{"id": "search", "name": "web_search", "arguments": {}}], {}
        return '{"kind":"answer","text":"The search returned no matching information."}', None, {}
    async def execute(name, args):
        calls.append(name)
        return '{"results":[]}'
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="research", tools=[tool_schema()],
        tool_executor=execute, stream_handler=stream, max_rounds=5,
        final_response_contract=WorkspaceChatResponseContract(scope()),
    )
    assert result.stop_reason == "completed"
    assert result.content == "The search returned no matching information."
    assert calls == ["web_search"]
    assert not any(event == "text_delta" for event, _ in events)


@pytest.mark.asyncio
async def test_loop_revision_repair_uses_text_only_without_repeating_tools(monkeypatch):
    invocations = []
    async def read(*args):
        return [fact()]
    async def model(messages, tools, **kwargs):
        invocations.append("tools")
        return summary(), None, {}
    async def revise(messages, **kwargs):
        invocations.append("text")
        assert "execution_revision_required" in messages[-1]["content"]
        return summary(revision="a" * 64), {}
    async def execute(*args):
        raise AssertionError("Reply repair must not replay effects")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", revise)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="status", tools=[tool_schema()], tool_executor=execute,
        final_response_contract=WorkspaceChatResponseContract(scope(), reader=read), max_rounds=5,
    )
    assert invocations == ["tools", "text"]
    assert result.control["execution_evidence"] == [fact()]
    assert "waiting for dispatch" in result.content


@pytest.mark.asyncio
async def test_reply_repair_after_tools_uses_json_mode_without_conflicting_stream_marker(monkeypatch):
    from packages.core.ai.runtime import agentic_llm
    from packages.core.ai.runtime.workspace_response import WORKSPACE_RESPONSE_INSTRUCTIONS

    tool_rounds = []
    executions = []
    async def model(messages, tools, **kwargs):
        tool_rounds.append(messages)
        assert "## Final Response Stream Marker" not in messages[0]["content"]
        if len(tool_rounds) == 1:
            return "", [{"id": "search", "name": "web_search", "arguments": {}}], {}
        return "A plain reply that does not match the contract", None, {}

    async def provider(messages, **kwargs):
        assert kwargs["response_format"] == WorkspaceChatResponseContract(scope()).response_format
        assert kwargs["response_format"]["type"] == "json_schema"
        assert "## Final Response Stream Marker" not in messages[0]["content"]
        diagnostic = json.loads(messages[-1]["content"])
        assert diagnostic["details"]["response_contract"] == WORKSPACE_RESPONSE_INSTRUCTIONS
        assert "do not call tools" in diagnostic["details"]["repair_instruction"]
        return '{"kind":"answer","text":"No matching result was found."}', {}

    async def execute(name, args):
        executions.append(name)
        return '{"results":[]}'

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="read only search", tools=[tool_schema()],
        tool_executor=execute, max_rounds=4,
        final_response_contract=WorkspaceChatResponseContract(scope()),
    )
    assert result.stop_reason == "completed"
    assert result.content == "No matching result was found."
    assert executions == ["web_search"]


@pytest.mark.asyncio
async def test_ordinary_text_completion_does_not_opt_into_json_mode(monkeypatch):
    from packages.core.ai.runtime import agentic_llm

    async def provider(messages, **kwargs):
        assert "response_format" not in kwargs
        return "Plain answer", {}

    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    content, _ = await agentic_llm.runtime_execute_agentic_round_text_completion(
        [{"role": "user", "content": "hello"}], temperature=0,
    )
    assert content == "Plain answer"


@pytest.mark.parametrize("leading_marker", [False, True])
@pytest.mark.asyncio
async def test_typed_reply_is_not_rewritten_by_legacy_marker_parser(monkeypatch, leading_marker):
    text = f"Literal marker: {loop_module.FINAL_RESPONSE_SENTINEL}"
    candidate = json.dumps({"kind": "answer", "text": text})
    if leading_marker:
        candidate = loop_module.FINAL_RESPONSE_SENTINEL + candidate

    async def model(*args, **kwargs):
        return candidate, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="explain markers", tools=[],
        tool_executor=None, max_rounds=1,
        final_response_contract=WorkspaceChatResponseContract(scope()),
    )
    if leading_marker:
        assert result.error == "final_response_validation_failed"
        assert result.content == ""
    else:
        assert result.content == text
        assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_continue_execution_cannot_restore_tools_removed_by_approval(monkeypatch):
    calls = []
    summaries = []
    async def execute(name, args):
        calls.append(name)
        return '{"results":[]}'
    async def tool_model(*args, **kwargs):
        raise AssertionError("Reply correction cannot restore disabled tools")
    async def text_model(messages, **kwargs):
        summaries.append(messages)
        return ('{"kind":"execution_summary","references":[]}' if len(summaries) == 1 else '{"kind":"answer","text":"No additional work was started."}'), {}
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", tool_model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", text_model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="approved", tools=[tool_schema()], tool_executor=execute,
        forced_tool_calls=[{"id": "approved", "name": "web_search", "arguments": {}, "disable_followup_tools": True}],
        final_response_contract=WorkspaceChatResponseContract(scope()), max_rounds=5,
    )
    assert result.content == "No additional work was started."
    assert calls == ["web_search"]


@pytest.mark.asyncio
async def test_non_workspace_loop_preserves_plain_text_and_streaming(monkeypatch):
    events = []
    async def stream(event, payload):
        events.append(event)
    async def model(messages, **kwargs):
        await kwargs["stream_handler"]("text_delta", {"content": "hello"})
        return "hello", {}
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="hello", tools=[], tool_executor=None,
        stream_handler=stream,
    )
    assert result.content == "hello"
    assert events == ["text_delta"]


@pytest.mark.asyncio
async def test_invalid_max_rounds_summary_is_not_published(monkeypatch):
    async def final(*args, **kwargs):
        return "Fabricated completion", {}
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="start", tools=[], tool_executor=None,
        max_rounds=0, final_response_contract=WorkspaceChatResponseContract(scope()),
    )
    assert result.error == "final_response_validation_failed"
    assert result.content == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ChatSurface.WORKSPACE_CHAT, ChatSurface.GLOBAL_OWNER_CHAT])
async def test_chat_transport_persists_accepted_reply_after_withdrawing_candidate(monkeypatch, surface):
    from copy import deepcopy
    from unittest.mock import AsyncMock
    from packages.core import database
    from packages.core.services import chat_service

    unpublished = "UNVERIFIED_EXECUTION_CANDIDATE has dispatched and finished all work."
    final_text = "Saved deck.pptx."
    workspace_id = "workspace" if surface == ChatSurface.WORKSPACE_CHAT else None
    saved, snapshots, rounds = [], [], []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def commit(self):
            pass
    async def context(*args, **kwargs):
        envelope = NS(surface=surface, entity_id="entity", workspace_id=workspace_id,
                      user_id="user", conversation_id="conversation", metadata={"origin_user_message_id": "message"})
        tools = [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in ("invoke_skill", "sandbox_save_result")
        ]
        return "system", tools, [], NS(
            workspace_id=workspace_id, task_id=None, runtime_envelope=envelope,
            tool_profile=None, allowed_tool_names=None, user=None, entity=None,
        )
    async def provider(messages, *args, **kwargs):
        rounds.append(messages)
        if len(rounds) == 1:
            return "", [{"id": "open", "name": "invoke_skill", "arguments": {"skill": "pptx"}}], {}
        if len(rounds) == 2:
            await kwargs["stream_handler"]("text_delta", {"content": unpublished})
            # Cross the transport's small-delta/sentinel buffering threshold.
            detail = " Every quarter has slides with charts and speaker notes." * 5
            await kwargs["stream_handler"]("text_delta", {"content": detail})
            return unpublished + detail, None, {}
        if len(rounds) == 3:
            assert "sandbox_handoff_undelivered" in json.dumps(messages)
            return "", [{"id": "save", "name": "sandbox_save_result", "arguments": {}}], {}
        await kwargs["stream_handler"]("text_delta", {"content": final_text})
        return final_text, None, {}
    async def execute(name, args):
        if name == "invoke_skill":
            return json.dumps({"status": "sandbox_ready", "skill": "pptx", "sandbox_id": "sbx-1"})
        assert name == "sandbox_save_result"
        return json.dumps({"saved": True, "document_id": "doc-1", "fs_path": "/k/deck.pptx",
                           "artifact_role": "final", "saved_to_knowledge": True})
    async def real_loop(**kwargs):
        assert kwargs["final_response_contract"] is not None
        return await loop_module.agentic_loop(
            system_prompt=kwargs["system_prompt"], user_message=kwargs["user_message"], tools=kwargs["tools"],
            tool_executor=execute, on_tool_start=kwargs["on_tool_start"], on_tool_end=kwargs["on_tool_end"],
            stream_handler=kwargs["stream_handler"], final_response_contract=kwargs["final_response_contract"], max_rounds=5,
        )
    async def save(**kwargs):
        saved.append(deepcopy(kwargs))
        return "assistant"
    async def snapshot(*args, **kwargs):
        snapshots.append(deepcopy((args, kwargs)))
    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", context)
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", real_loop)
    monkeypatch.setattr(chat_service, "save_or_update_assistant_stream_message", save)
    monkeypatch.setattr(chat_service, "get_chat_turn_cancel_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(chat_service, "chat_turn_cancellation_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_service, "runtime_persist_chat_stream_runtime_events", AsyncMock())
    monkeypatch.setattr(chat_service, "record_chat_runtime_learning", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_service, "schedule_learning_candidate_applies", AsyncMock())
    monkeypatch.setattr(chat_service, "_publish_chat_stream_snapshot", snapshot)
    monkeypatch.setattr(chat_service, "_schedule_tool_path_recording", lambda **kwargs: None)
    monkeypatch.setattr(loop_module, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", provider)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", provider)
    wire = []
    async for raw in chat_service.stream_chat_response(
        "Create a deck", "conversation", entity_id="entity", user_id="user", workspace_id=workspace_id,
        assistant_message_id="assistant", persist_messages=True, runtime_surface=surface,
    ):
        wire.append(raw)
    if chat_service._SNAPSHOT_PUBLISH_TASKS:
        await asyncio.gather(*list(chat_service._SNAPSHOT_PUBLISH_TASKS))
    assert saved[-1]["content"] == final_text
    assert final_text in "".join(wire)
    assert "sandbox_save_result" in "".join(wire)  # Real tool activity remains live.
    # Plain-text candidates stream live, but a failed delivery gate withdraws
    # the candidate before the accepted reply is persisted or reconnected.
    events = [(raw.split("\n", 1)[0].removeprefix("event: "),
               json.loads(raw.split("\ndata: ", 1)[1])) for raw in wire if "\ndata: " in raw]
    deltas = "".join(payload.get("content", "") for event, payload in events if event == "text_delta")
    assert unpublished in deltas
    assert any(event == "text_reset" for event, _ in events)
    terminal = next(payload for event, payload in events if event == "stream_end")
    assert unpublished not in json.dumps([terminal, saved[-1]])
    if surface == ChatSurface.GLOBAL_OWNER_CHAT:
        assert snapshots
        assert unpublished not in json.dumps(snapshots[-1])
        assert final_text in json.dumps(snapshots[-1])
    else:
        assert not snapshots  # Workspace uses its own realtime channel.
    process = next(block for block in terminal["assistant_blocks"] if block["type"] == "process")
    assert process["outcome_status"] == "completed"
    assert len(rounds) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(("waiting", "detail"), [
    ({"kind": "waiting_timer", "wake_at": "2026-10-12T09:00:00+00:00"}, "until 2026-10-12T09:00:00+00:00"),
    ({"kind": "waiting_job", "job_ids": ["a", "b"]}, "for 2 media job(s)"),
])
async def test_parked_task_renders_what_it_waits_for(waiting, detail):
    from packages.core.ai.runtime.workspace_response import _safe_label

    parked = {**fact(), "state": "waiting", "waiting": waiting}

    async def read(*args):
        return [parked]

    decision = await WorkspaceChatResponseContract(scope(), locale="en", reader=read).validate(
        summary(revision="a" * 64)
    )
    assert decision.action is FinalResponseAction.ACCEPT
    assert f"Paused; waiting {_safe_label(detail)}" in decision.content


def test_parked_agent_loop_state_without_and_with_a_live_claim():
    from packages.core.services.task_execution import task_execution_intent_generation

    task = NS(id="t", entity_id="e", workspace_id="w", status="in_progress", status_changed_at=None,
              task_type="general", agent_id=None, agent_type=None, owner_service_key=None,
              owner_subscription_id=None, delegate_service_keys=[])
    run = NS(phase="waiting_job", claim_token=None, claim_generation=None, wake_generation="g",
             claim_expires_at=None, lifecycle_generation=task_execution_intent_generation(task),
             current_plan_id=None)
    assert task_execution_state(task, coordinator=run) is ExecutionObservationState.WAITING
    run.claim_token, run.claim_generation = "claim", "g"
    run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert task_execution_state(task, coordinator=run) is ExecutionObservationState.RUNNING
