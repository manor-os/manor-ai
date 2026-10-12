"""Workflow adapters preserve human gates and resume the exact Agent checkpoint."""
import copy
import json
from types import SimpleNamespace

import pytest

from packages.core.ai import agentic_loop as loops
from packages.core.ai import workflow_runner as module
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services import workflow_runtime_approvals as approvals


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def commit(self):
        pass


def gate():
    return {"__hitl__": True, "error": "approval_required", "approval_token": "request-1",
            "hitl": {"id": "request-1", "type": "approval", "prompt": "Approve?", "tool": "write_file"}}


def paused():
    return {"status": "paused", "code": "runtime_approval_required", "resume_strategy": "blocked",
            "runtime_approval": {"version": 1, "request_id": "request-1", "workflow_run_id": "run-1",
                                 "step_id": "produce", "execution_node_id": "produce", "checkpoint_digest": "digest"},
            "output": "Approval is required before this step can continue."}


async def attach(result, _envelope):
    return result


async def appendix(*_args, **_kwargs):
    return SimpleNamespace(tool_schemas=[], allowed_tool_names=frozenset(), envelope=None,
                           context_section="", availability_section="")


@pytest.mark.parametrize("structured", [False, True])
async def test_tool_gate_precedes_schema_and_never_populates_variables(monkeypatch, structured):
    captured = {}

    async def execute(**_kwargs):
        data = gate()
        return SimpleNamespace(output=json.dumps(data), structured_output=data if structured else None, envelope=None)

    async def persist(_db, **kwargs):
        captured.update(kwargs)
        return paused()

    monkeypatch.setattr(module, "runtime_execute_workflow_tool_step", execute)
    monkeypatch.setattr(approvals, "persist_workflow_runtime_approval_gate", persist)
    step = {"id": "produce", "type": "tool", "config": {"tool": "write_file", "args": {"path": "draft.md"},
            "output_var": "document", "output_schema": {"type": "object", "required": ["document_id"]}}}
    runner = WorkflowRunner(session_factory=Session)
    result = await runner._execute_tool_step(step, {}, "entity-1", "user-1", {"workflow_run_id": "run-1"})
    assert result == paused()
    assert captured["control"]["blocking_result"] == json.dumps(gate())
    assert captured["tool_arguments"] == {"path": "draft.md"}
    run = WorkflowRun(id="run-1", workflow_id="flow", entity_id="entity-1", variables={}, step_results={})
    runner._record_step_result(step, result, run)
    assert run.variables == {}
    assert "approval_token" not in json.dumps(run.step_results)


@pytest.mark.parametrize("kind", ["agent", "skill"])
async def test_agent_native_gate_is_not_parsed_as_empty_json(monkeypatch, kind):
    captured = {}
    control = {"kind": "approval_required", "tool_name": "write_file", "tool_call_id": "call-1",
               "blocking_result": json.dumps(gate()), "checkpoint": {"private": "model transcript"}}

    async def execute(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="", stop_reason="hitl_required", control=control)

    async def skill(*_args, **kwargs):
        captured.update(kwargs)
        return {"content": "", "stop_reason": "hitl_required", "control": control}

    async def persist(_db, **kwargs):
        assert kwargs["control"] == control
        return paused()

    async def definition(*_args, **_kwargs):
        return SimpleNamespace(config={"type": "prompt"})

    def no_parse(*_args):
        pytest.fail("A pending approval is not final business JSON")

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_prepare_trace_envelope_for_turn", lambda _request: None)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", execute)
    monkeypatch.setattr(module, "runtime_invoke_skill", skill)
    monkeypatch.setattr("packages.core.services.skill_service.get_skill", definition)
    monkeypatch.setattr(module, "_parse_agent_output", no_parse)
    monkeypatch.setattr(approvals, "persist_workflow_runtime_approval_gate", persist)
    config = {"prompt": "Save deliverables", "output_format": "json", "output_schema": {"type": "object"}}
    if kind == "skill":
        config["skill"] = "writer"
    result = await WorkflowRunner(session_factory=Session)._execute_agent_step(
        {"id": "produce", "type": "agent", "config": config}, {}, "entity-1", "user-1", {"workflow_run_id": "run-1"})
    assert result == paused()
    assert captured["hitl_resume_checkpoint"] is True
    assert "checkpoint" not in result
    assert "model transcript" not in json.dumps(result)


async def test_genuine_empty_final_response_remains_failure(monkeypatch):
    async def execute(**_kwargs):
        return SimpleNamespace(content="", stop_reason="completed", control=None, error=None)

    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", execute)
    with pytest.raises(ValueError, match="LLM returned an empty response"):
        await WorkflowRunner()._execute_agent_step(
            {"id": "produce", "type": "agent", "config": {"prompt": "Work", "output_format": "json"}},
            {}, "entity-1", "user-1", {})


@pytest.mark.parametrize("failure", [approvals.WorkflowRuntimeApprovalError("No exact checkpoint; do not replay the Agent"), RuntimeError("storage write failed")])
async def test_checkpoint_failure_is_explicit_and_forbids_step_replay(monkeypatch, failure):
    async def persist(_db, **_kwargs):
        raise failure

    monkeypatch.setattr(approvals, "persist_workflow_runtime_approval_gate", persist)
    result = await WorkflowRunner(session_factory=Session)._native_approval_result(
        step={"id": "produce"}, runtime_context={}, entity_id="entity-1", user_id="user-1", control={})
    assert result["status"] == "failed"
    assert result["code"] == "runtime_approval_continuation_unavailable"
    assert result["do_not_replay"] is True
    assert result["retry_blocked"] is True


async def test_real_agent_loop_resume_does_not_replay_effect_before_gate(monkeypatch):
    calls, snapshots = [], []
    approved = False
    schemas = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
               for name in ("write_note", "write_file")]
    batch = [{"id": "before", "name": "write_note", "arguments": {"text": "before"}},
             {"id": "pending", "name": "write_file", "arguments": {"text": "gated"}},
             {"id": "after", "name": "write_note", "arguments": {"text": "after"}}]

    async def completion(*_args, **_kwargs):
        return ('{"saved":true}', [], {}) if approved else ("", copy.deepcopy(batch), {})

    async def executor(name, args):
        if name == "write_file" and not approved:
            return json.dumps(gate())
        calls.append(args["text"])
        return json.dumps({"ok": True})

    async def execute(**kwargs):
        if approved:
            assert kwargs["forced_tool_calls"] is None
        return await loops.agentic_loop(
            system_prompt=kwargs["system_prompt"], user_message=kwargs["user_message"], tools=schemas,
            tool_executor=executor, max_rounds=3, hitl_resume_checkpoint=kwargs["hitl_resume_checkpoint"],
            resume_checkpoint=kwargs.get("runtime_checkpoint"), output_schema={"type": "object"},
            forced_tool_calls=kwargs.get("forced_tool_calls"))

    async def persist(_db, **kwargs):
        snapshots.append(copy.deepcopy(kwargs["control"]["checkpoint"]))
        return paused()

    async def resume(_db, **_kwargs):
        return {"checkpoint": copy.deepcopy(snapshots[0])}

    monkeypatch.setattr(loops, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", execute)
    monkeypatch.setattr(module, "runtime_attach_and_persist_workflow_runner_result", attach)
    monkeypatch.setattr(approvals, "persist_workflow_runtime_approval_gate", persist)
    monkeypatch.setattr(approvals, "load_workflow_approval_resume", resume)
    runner = WorkflowRunner(session_factory=Session)
    step = {"id": "produce", "type": "agent", "config": {"prompt": "Work", "output_format": "json",
            "forced_tool_calls": [{"name": "write_note", "arguments": {"text": "initial setup"}}]}}
    first = await runner._execute_agent_step(step, {}, "entity-1", "user-1", {"workflow_run_id": "run-1"})
    assert first["status"] == "paused"
    assert calls == ["initial setup", "before"]
    assert snapshots[0]["pending_tool_call"]["id"] == "pending"
    approved = True
    result = await runner._execute_agent_step(step, {}, "entity-1", "user-1", {"workflow_run_id": "run-1"}, previous_result=first)
    assert result["status"] == "completed"
    assert result["output"] == {"saved": True}
    assert calls == ["initial setup", "before", "gated", "after"]


async def test_tool_without_conversation_fails_without_fabricating_approval(monkeypatch):
    async def execute(**_kwargs):
        return SimpleNamespace(output=json.dumps({"error": "approval_required", "message": "No conversation context to request approval."}), structured_output=None, envelope=None)

    monkeypatch.setattr(module, "runtime_execute_workflow_tool_step", execute)
    monkeypatch.setattr(module, "runtime_attach_and_persist_workflow_runner_result", attach)
    result = await WorkflowRunner()._execute_tool_step(
        {"id": "read", "config": {"tool": "read_file", "args": {"document_id": "doc"}}},
        {}, "entity-1", "user-1", {})
    assert result["status"] == "failed"
    assert result["error"] == "approval_required"
    assert "runtime_approval" not in result


async def test_native_gate_stops_later_frontier_siblings(monkeypatch):
    runner = WorkflowRunner()
    run = WorkflowRun(id="run-1", workflow_id="flow", entity_id="entity-1", status="running",
                      variables={}, step_results={}, trigger_data={}, execution_trace=[])
    flow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["gate", "later"]},
        {"id": "gate", "type": "tool", "next": []},
        {"id": "later", "type": "tool", "next": []},
    ])
    calls = []

    async def execute(step, *_args, **_kwargs):
        calls.append(step["id"])
        return paused() if step["id"] == "gate" else {"status": "completed"}

    async def active(*_args, **_kwargs):
        return False

    async def project(*_args, **_kwargs):
        pass

    monkeypatch.setattr(runner, "_execute_step_safe", execute)
    monkeypatch.setattr(runner, "_externally_paused_or_cancelled", active)
    monkeypatch.setattr("packages.core.services.workflow_chat_projection.project_workflow_run_status", project)
    await runner._run_loop(flow, run, Session())
    assert calls == ["start", "gate"]
    assert run.status == "paused"
    assert run.current_step_id == "gate"
    assert "later" not in run.step_results


@pytest.mark.parametrize("kind", ["loop", "parallel"])
async def test_unsupported_nested_approval_preserves_non_replay_failure(monkeypatch, kind):
    runner = WorkflowRunner()
    calls = []

    async def execute(step, *_args, **_kwargs):
        calls.append(step["id"])
        return {"status": "failed", "code": "runtime_approval_continuation_unavailable",
                "error": "No parent checkpoint", "do_not_replay": True, "retry_blocked": True}

    monkeypatch.setattr(runner, "_execute_step_safe", execute)
    step = {"id": "group", "type": kind, "config": {"items": [1, 2], "steps": [{"id": "gated"}, {"id": "later"}]}}
    run = WorkflowRun(id="run-1", entity_id="entity-1", variables={})
    execute_group = runner._execute_loop_step if kind == "loop" else runner._execute_parallel_step
    result = await execute_group(step, {}, "entity-1", run, Session())
    assert calls == ["gated"]
    assert result["do_not_replay"] is True
    assert result["retry_blocked"] is True


async def test_lost_resume_is_not_automatically_retried_or_a_new_attempt(monkeypatch):
    runner = WorkflowRunner()
    attempts = []

    async def execute(*_args, **_kwargs):
        attempts.append(True)
        raise approvals.WorkflowRuntimeApprovalError("Saved continuation is stale")

    async def no_transaction(*_args, **_kwargs):
        pass

    monkeypatch.setattr(runner, "_execute_step", execute)
    monkeypatch.setattr(module, "_commit_step_pointer_only", no_transaction)
    monkeypatch.setattr(module, "release_read_transaction", no_transaction)
    run = WorkflowRun(id="run-1", entity_id="entity-1", variables={}, step_results={})
    result = await runner._execute_step_safe(
        {"id": "produce", "type": "agent", "config": {"retry_on_fail": True, "max_tries": 3}}, run, Session())
    assert attempts == [True]
    assert result["retry_blocked"] is True
    assert result["do_not_replay"] is True


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_native_wake_waits_for_outer_transaction(monkeypatch, finish):
    from sqlalchemy.orm import Session as SyncSession

    wakes = []
    monkeypatch.setattr(WorkflowRunner, "enqueue_resume", staticmethod(
        lambda run_id, delay, *, continuation_token: wakes.append((run_id, continuation_token))))
    with SyncSession() as session:
        session.begin()
        approvals._queue_wake(session, "run", "token")
        with session.begin_nested():
            pass
        assert wakes == [], "a message SAVEPOINT must not publish an uncommitted grant"
        getattr(session, finish)()
        assert wakes == ([("run", "token")] if finish == "commit" else [])
        with session.begin():
            pass
        assert len(wakes) == (1 if finish == "commit" else 0)


def test_native_wake_nested_rollback_discards_only_its_own_grant(monkeypatch):
    from sqlalchemy.orm import Session as SyncSession

    wakes = []
    monkeypatch.setattr(WorkflowRunner, "enqueue_resume", staticmethod(
        lambda run_id, delay, *, continuation_token: wakes.append((run_id, continuation_token))))
    with SyncSession() as session:
        session.begin()
        approvals._queue_wake(session, "outer", "outer-token")
        nested = session.begin_nested()
        approvals._queue_wake(session, "rolled-back", "inner-token")
        nested.rollback()
        assert wakes == []
        session.commit()
    assert wakes == [("outer", "outer-token")]
