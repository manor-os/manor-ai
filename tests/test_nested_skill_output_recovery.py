"""Two child writes stay proven after Skill completion and parent output repair."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.approvals import RuntimeApprovalAction
from packages.core.ai.runtime.nested_agent import runtime_nested_skill_scope
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolAuthorization, RuntimeToolClassification,
)
from packages.core.ai.runtime.tool_effect_intents import (
    RuntimeToolEffectPersistenceError, runtime_record_tool_effect_intent,
    runtime_record_tool_effect_result,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.service import Dispatcher, _has_ambiguous_external_effect
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationAccess
from packages.core.workers import internal
from packages.core.workers.submit_result import SUBMIT_RESULT_TERMINAL_POLICY
from test_nested_skill_durability import worker_scopes
from test_robust_worker2_partial_transcript import _seed_lease


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _classify(name, *_args, **_kwargs):
    if name == "patch_file":
        return RuntimeToolClassification.action_call(RuntimeApprovalAction(
            kind="action", action_key="workspace.file.modify", capability_id="file.write",
            operation="modify", risk_level="medium", title="Update the report",
        ))
    if name == "invoke_skill":
        return RuntimeToolClassification.orchestrator("Nested Skill", RuntimeToolAuthorization(
            action_key="test.skill.invoke", capability_id=None, access=RuntimeAuthorizationAccess.USE,
        ))
    assert name == "submit_result"
    return RuntimeToolClassification.control("Submit result", RuntimeToolAuthorization(
        action_key="test.result.submit", capability_id=None, access=RuntimeAuthorizationAccess.CONTROL,
    ))


async def _scenario(db, monkeypatch, *, fail_second=False):
    snapshot = await _seed_lease(db, execution_claim_id=generate_ulid())
    workspace = Workspace(entity_id=snapshot["entity_id"], name="Nested report recovery", operating_model={})
    db.add(workspace)
    await db.flush()
    step = await db.get(ExecutionStep, snapshot["step_id"])
    plan = await db.get(ExecutionPlan, snapshot["plan_id"])
    lease = await db.get(WorkLease, snapshot["lease_id"])
    step.workspace_id = plan.workspace_id = lease.workspace_id = workspace.id
    snapshot["workspace_id"] = workspace.id
    await db.commit()
    monkeypatch.setattr(internal, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr("packages.core.ai.runtime.approval_classifier.classify_runtime_tool", _classify)
    state = {"writes": [], "skill_invocations": 0, "submits": 0, "repair": False, "parent_barrier": None}

    async def persist_round(rounds, messages, usage, tool_calls_made, runtime_state=None):
        await internal._persist_worker_round_checkpoint(
            snapshot, rounds=rounds, messages=messages, usage=usage, tool_calls_made=tool_calls_made,
            terminal_failure=(runtime_state or {}).get("terminal_failure"),
        )

    async def model(messages, tools, **_kwargs):
        results = [message for message in messages if message.get("role") == "tool"]
        if tools[0]["function"]["name"] == "patch_file":
            if len(results) < 2:
                index = len(results) + 1
                return "", [{"id": f"write-{index}", "name": "patch_file", "arguments": {
                    "path": f"report-{index}.md", "patches": [{"op": "text.set", "text": f"revision-{index}"}],
                }}], {}
            return "Both report updates completed.", [], {}
        if not results:
            return "", [{"id": "skill-parent", "name": "invoke_skill", "arguments": {"skill_id": "report-skill"}}], {}
        if not state["repair"]:
            # The original incident: the child completed, but the parent
            # stopped without producing its mandatory submit_result payload.
            return "Updated both reports.", [], {}
        assert any(message.get("tool_call_id") == "skill-parent" for message in results)
        return "", [{"id": "submit", "name": "submit_result", "arguments": {
            "result": {"summary": "Both report updates are complete."},
        }}], {}

    async def execute(name, arguments):
        call_id = arguments["_runtime_tool_call_id_from_context"]
        public = {key: value for key, value in arguments.items() if not key.startswith("_runtime_")}
        if name == "invoke_skill":
            state["skill_invocations"] += 1
            assert not state["repair"], "output repair must consume the stored parent result"
            with runtime_nested_skill_scope("report-skill") as frame:
                child = await loop_module.agentic_loop(
                    system_prompt="Update the two reports.", user_message="Apply both changes.",
                    tools=[_tool("patch_file")], tool_executor=execute, max_rounds=5,
                    on_round_end=frame.on_round_end, on_round_end_strict=True, **frame.loop_kwargs(),
                )
                response = {"content": child.content, "stop_reason": child.stop_reason}
                await frame.complete(response, child.messages)
            result = json.dumps(response)
        elif name == "patch_file":
            assert not state["repair"], "a completed child write must never replay"
            await runtime_record_tool_effect_intent(name, public, call_id)
            state["writes"].append(public["path"])
            result = json.dumps(
                {"status": "failed", "error": "connection lost after write", "retryable": False}
                if fail_second and len(state["writes"]) == 2 else
                {"status": "completed", "patched": True, "document_id": f"document-{len(state['writes'])}", "path": public["path"]}
            )
        else:
            assert name == "submit_result"
            state["submits"] += 1
            result = '{"status":"completed","ok":true}'
        await runtime_record_tool_effect_result(name, public, result, call_id)
        if name == "invoke_skill":
            # Capture the parent result barrier before the normal round
            # checkpoint discards the nested execution frame.
            db.expire_all()
            saved = await db.get(ExecutionStep, snapshot["step_id"])
            state["parent_barrier"] = SimpleNamespace(
                id=saved.id, plan_id=saved.plan_id, entity_id=saved.entity_id,
                workspace_id=saved.workspace_id, step_status=saved.step_status, params=saved.params,
                runtime_checkpoint=copy.deepcopy(saved.runtime_checkpoint), evidence_refs=copy.deepcopy(saved.evidence_refs),
            )
        return result

    async def run(**resume):
        with worker_scopes(snapshot):
            return await loop_module.agentic_loop(
                system_prompt="Finish by submitting the report result.", user_message="Update both reports.",
                tools=[_tool("invoke_skill"), _tool("submit_result")], tool_executor=execute, max_rounds=6,
                terminal_tool_result_policy=SUBMIT_RESULT_TERMINAL_POLICY,
                on_round_end=persist_round, on_round_end_strict=True, **resume,
            )

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    return snapshot, state, run


def _effects(owner):
    return [ref for ref in owner.evidence_refs if ref.get("kind") == "tool_effect"]


@pytest.mark.asyncio
async def test_two_child_writes_survive_parent_missing_output_and_resume_same_step(db_session, monkeypatch):
    snapshot, state, run = await _scenario(db_session, monkeypatch)
    first = await run()
    assert first.stop_reason == "completed" and state["submits"] == 0
    assert state["writes"] == ["report-1.md", "report-2.md"]
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    original = copy.deepcopy(_effects(step))
    assert len(original) == 2 and all(ref["effect"] == "action" for ref in original)
    identities = {ToolEffectEvidenceFactory.effect_id(ref) for ref in original}
    assert len(identities) == 2
    assert all(ref["fields"]["tool_call_id"].startswith("nested_") for ref in original)
    for owner in (state["parent_barrier"], step):
        assert _effects(owner) == original
        assert not await _has_ambiguous_external_effect(db_session, owner.evidence_refs, owner_step=owner)
    transcript = copy.deepcopy(step.runtime_checkpoint)
    assert internal.is_worker_transcript_checkpoint(transcript)

    await Dispatcher().fail_lease(
        db_session, snapshot["lease_id"], error={"type": "MissingOutputContract", "message": "submit_result missing"},
        will_retry=False, runtime_checkpoint=transcript,
    )
    assert step.step_status == "pending" and step.current_lease_id is None
    assert step.runtime_checkpoint == transcript
    assert step.error.get("failure", {}).get("requires_human") is not True
    assert not step.human_input_prompt
    assert list((await db_session.execute(select(HitlRequest).where(HitlRequest.entity_id == snapshot["entity_id"]))).scalars()) == []
    previous = await db_session.get(WorkLease, snapshot["lease_id"])
    next_lease = WorkLease(
        id=generate_ulid(), step_id=step.id, plan_id=step.plan_id, entity_id=step.entity_id,
        workspace_id=step.workspace_id, worker_id=previous.worker_id, status="active",
        execution_claim_id=generate_ulid(), lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        evidence_refs=copy.deepcopy(step.evidence_refs),
    )
    db_session.add(next_lease)
    step.step_status, step.current_lease_id = "running", next_lease.id
    step.attempt_count += 1
    snapshot.update(lease_id=next_lease.id, execution_claim_id=next_lease.execution_claim_id, runtime_checkpoint=transcript)
    await db_session.commit()
    state["repair"] = True
    repaired = await run(
        initial_messages=internal._resume_transcript_for_retry(snapshot),
        initial_rounds=transcript["rounds"], initial_usage=transcript["usage"],
        initial_tool_calls_made=transcript["tool_calls_made"],
    )
    assert repaired.stop_reason == "submit_result" and state["submits"] == 1
    assert state["skill_invocations"] == 1 and state["writes"] == ["report-1.md", "report-2.md"]
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    assert _effects(step) == original
    assert not await _has_ambiguous_external_effect(db_session, step.evidence_refs, owner_step=step)


@pytest.mark.asyncio
async def test_second_child_terminal_error_cannot_be_hidden_by_first_receipt_or_parent_summary(db_session, monkeypatch):
    snapshot, state, run = await _scenario(db_session, monkeypatch, fail_second=True)
    with pytest.raises(RuntimeToolEffectPersistenceError):
        await run()
    db_session.expire_all()
    step = await db_session.get(ExecutionStep, snapshot["step_id"])
    effects = _effects(step)
    assert len(effects) == 2
    assert [ref["effect"] for ref in effects] == ["action", "pending"]
    assert await _has_ambiguous_external_effect(db_session, step.evidence_refs, owner_step=step)
    assert state["parent_barrier"] is None and state["submits"] == 0
    await Dispatcher().fail_lease(
        db_session, snapshot["lease_id"], error={"type": "MissingOutputContract", "message": "child outcome unknown"},
        runtime_checkpoint=step.runtime_checkpoint,
    )
    assert step.step_status == "failed"
    assert _effects(step) == effects
    assert await _has_ambiguous_external_effect(db_session, step.evidence_refs, owner_step=step)
    assert state["writes"] == ["report-1.md", "report-2.md"]
    assert list((await db_session.execute(select(HitlRequest).where(HitlRequest.entity_id == snapshot["entity_id"]))).scalars()) == []
