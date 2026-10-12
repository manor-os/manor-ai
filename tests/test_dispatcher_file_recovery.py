"""Internal file recovery resumes exact state and never asks users to guess."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.service import Dispatcher, INTERNAL_FILE_RECOVERY_FAILED, LeaseNotActive, _failure_checkpoint, _internal_file_recovery_error, _is_system_file_effect
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.worker import WorkLease, WorkerActivityLog
from packages.core.models.workspace import Workspace
from packages.core.plans.executor import PlanExecutor, evaluate_task_completion_gates
from packages.core.services.runtime_file_effect_recovery import RuntimeFileEffectRecovery
from tests.test_dispatcher_envelope_status import _setup_step

_FILE_RESULT = '{"document_id":"doc-1","path":"documents/report.md"}'
_FILE_RESULT_SHA256 = hashlib.sha256(_FILE_RESULT.encode("utf-8")).hexdigest()


async def _file_step(db, *, returned=False, observed=False, attempts=1, auto_human=False):
    step_id, lease_id = await _setup_step(db, kind="subagent", attempt_count=attempts)
    step = await db.get(ExecutionStep, step_id)
    lease = await db.get(WorkLease, lease_id)
    step.params = {**step.params, "retry_policy": {"max_attempts": 3, "auto_human_on_exhausted": auto_human}}
    arguments = {"path": "documents/report.md", "operations": [{"op": "text.set", "text": "saved"}]}
    pending = {"id": "write-1", "name": "patch_file", "arguments": arguments}
    step.runtime_checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "Update the report"}, {
            "role": "assistant", "tool_calls": [{"id": "write-1", "function": {
                "name": "patch_file", "arguments": json.dumps(arguments),
            }}],
        }], usage={}, rounds=1, tool_calls_made=[], pending_tool_call=pending,
        pending_tool_result={"id": "write-1", "content": _FILE_RESULT} if observed else None,
    ).to_dict()
    effect = ToolEffectEvidenceFactory.bind_effect_id({
        "kind": "tool_effect", "effect": "action" if returned else "pending", "fields": {
            "tool": "patch_file", "tool_call_id": "write-1", "arguments_hash": _tool_arguments_hash(arguments),
            "capability_id": "file.write", "action_key": "workspace.file.modify", "classification": "action",
            "owner_plan_id": step.plan_id, "owner_step_id": step.id, "effect_generation": "1",
            "intent_state": "returned" if returned else "started", "path": "documents/report.md",
            "result_sha256": _FILE_RESULT_SHA256,
        },
    })
    step.evidence_refs = [effect]
    lease.evidence_refs = [effect]
    await db.flush()
    return step, lease, effect


def _receipt():
    return RuntimeFileEffectRecovery("committed", {
        "kind": "runtime_file_reconciliation", "operation_status": "committed", "status": "completed",
        "original_tool_result_available": False,
        "files": [{"path": "documents/report.md", "document_id": "doc-1", "content_sha256": "a" * 64}],
    }, ("journal-1",))


def test_file_recovery_error_describes_exact_pending_patch_without_contents():
    arguments = {"path": "documents/report.md", "operations": [
        {"op": "text.replace", "old_text": "PRIVATE BEFORE", "new_text": "PRIVATE AFTER"},
        {"op": "text.set", "text": "PRIVATE CONTENT"},
    ], "approval_token": "PRIVATE TOKEN"}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "write-1", "name": "patch_file", "arguments": arguments},
    ).to_dict()
    effect = {"kind": "tool_effect", "effect": "pending", "fields": {
        "tool": "patch_file", "tool_call_id": "write-1", "arguments_hash": _tool_arguments_hash(arguments),
    }}
    error = _internal_file_recovery_error({}, [effect], runtime_checkpoint=checkpoint)
    context = error["recovery"]["file_operations"][0]["display_context"]
    assert context == {
        "operation": "patch", "source": "host_pending_checkpoint", "targets": ["documents/report.md"],
        "patch_operation_count": 2, "patch_operation_types": ["text.replace", "text.set"],
    }
    assert "patch (2 operations: text.replace, text.set) for documents/report.md" in error["message"]
    assert "could not verify" in error["message"]
    assert "PRIVATE" not in json.dumps(error)
    assert effect["fields"].get("path") is None


@pytest.mark.parametrize("mismatch", ["tool", "call", "arguments"])
def test_file_recovery_error_never_borrows_different_pending_target(mismatch):
    arguments = {"path": "documents/private.md", "operations": [{"op": "text.append", "text": "private"}]}
    fields = {"tool": "patch_file", "tool_call_id": "write-1", "arguments_hash": _tool_arguments_hash(arguments)}
    pending = {"id": "write-1", "name": "patch_file", "arguments": arguments}
    if mismatch == "tool":
        pending["name"] = "delete_file"
    elif mismatch == "call":
        pending["id"] = "other"
    else:
        pending["arguments"] = {**arguments, "path": "documents/other.md"}
    error = _internal_file_recovery_error({}, [{"fields": fields}], runtime_checkpoint={"pending_tool_call": pending})
    context = error["recovery"]["file_operations"][0]["display_context"]
    assert context == {"operation": "patch", "targets": []}
    assert "documents/" not in json.dumps(error)


def test_file_recovery_error_uses_project_target_and_known_video_operation():
    arguments = {"project_path": "Videos/launch/source", "operation": "write_files", "files": {"index.html": "PRIVATE HTML"}}
    effect = {"fields": {"tool": "video_edit", "tool_call_id": "video-1", "arguments_hash": _tool_arguments_hash(arguments)}}
    checkpoint = {"pending_tool_call": {"id": "video-1", "name": "video_edit", "arguments": arguments}}
    error = _internal_file_recovery_error({}, [effect], runtime_checkpoint=checkpoint)
    assert "video edit: write_files for Videos/launch/source" in error["message"]
    assert "PRIVATE HTML" not in json.dumps(error)


def test_file_recovery_error_preserves_verified_journal_target_without_pending_frame():
    observation = _receipt().observation
    fields = {"tool": "patch_file", "runtime_file_observation": observation, "runtime_file_journal_ids": ["journal-1"]}
    error = _internal_file_recovery_error({}, [{"fields": fields}], output_repair_exhausted=True)
    assert "patch for documents/report.md" in error["message"]
    assert "has a verified result" in error["message"]
    operation = error["recovery"]["file_operations"][0]
    assert operation["runtime_file_observation"] == observation
    assert operation["display_context"] == {"operation": "patch", "targets": ["documents/report.md"]}


@pytest.mark.parametrize("key,value", [
    ("action_key", []), ("action_key", {}), ("classification", []), ("classification", {}),
    ("tool", ["patch_file"]), ("provider", {}),
])
def test_system_file_effect_rejects_malformed_metadata(key, value):
    fields = {"tool": "patch_file", "capability_id": "file.write", "action_key": "workspace.file.modify", "classification": "action"}
    fields[key] = value
    assert not _is_system_file_effect({"kind": "tool_effect", "fields": fields})


def test_failure_checkpoint_rejects_malformed_effect_fields():
    current = {"pending_tool_call": {"id": "write-1", "name": "patch_file", "arguments": {}}}
    step = SimpleNamespace(runtime_checkpoint=current, evidence_refs=[{"kind": "tool_effect", "fields": ["invalid"]}])
    assert _failure_checkpoint(step, {"messages": []}) == current


def test_failure_transcript_does_not_discard_exact_pending_frame():
    current = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "write"}], usage={}, rounds=1,
        tool_calls_made=[], pending_tool_call={"id": "write", "name": "patch_file", "arguments": {}},
    ).to_dict()
    step = SimpleNamespace(runtime_checkpoint=current)
    supplied = {"schema_version": "worker_transcript.v1", "messages": [{"role": "assistant", "content": "failed"}]}
    assert _failure_checkpoint(step, supplied) == current
    assert _failure_checkpoint(step, supplied) is not current


@pytest.mark.parametrize("attack", ["incoming_receipt", "changed_arguments", "error_result", "historical_result"])
def test_failure_checkpoint_requires_persisted_exact_terminal_proof(attack):
    arguments = {"path": "documents/report.md", "operations": [{"op": "text.set", "text": "saved"}]}
    current = RuntimeAgentCheckpoint(
        messages=[{"role": "user", "content": "write"}], usage={}, rounds=1,
        tool_calls_made=[], pending_tool_call={"id": "write-1", "name": "patch_file", "arguments": arguments},
    ).to_dict()
    terminal = ToolEffectEvidenceFactory.bind_effect_id({"kind": "tool_effect", "effect": "action", "fields": {
        "tool": "patch_file", "tool_call_id": "write-1", "arguments_hash": _tool_arguments_hash(arguments),
        "intent_state": "returned", "result_sha256": _FILE_RESULT_SHA256,
    }})
    pending = copy.deepcopy(terminal)
    pending["effect"] = "pending"
    pending["fields"]["intent_state"] = "started"
    candidate = copy.deepcopy(current)
    candidate["pending_tool_result"] = {"id": "write-1", "content": _FILE_RESULT}
    persisted = [terminal]
    if attack == "incoming_receipt":
        persisted = [pending]
    elif attack == "changed_arguments":
        candidate["pending_tool_call"]["arguments"] = {"path": "other.md"}
    elif attack == "error_result":
        content = '{"error":"connection lost","retryable":false}'
        candidate["pending_tool_result"]["content"] = content
        terminal["fields"]["result_sha256"] = hashlib.sha256(content.encode()).hexdigest()
    else:
        candidate["messages"].extend([
            {"role": "assistant", "tool_calls": [{"id": "write-1", "function": {"name": "patch_file", "arguments": json.dumps(arguments)}}]},
            {"role": "tool", "tool_call_id": "write-1", "content": _FILE_RESULT},
        ])
        candidate["pending_tool_result"] = None
    step = SimpleNamespace(runtime_checkpoint=current, evidence_refs=[terminal])
    assert _failure_checkpoint(step, candidate, persisted_evidence=persisted) == current


@pytest.mark.asyncio
async def test_supplied_exact_result_precedes_ambiguity_check(db_session):
    step, lease, _ = await _file_step(db_session, returned=True)
    report = copy.deepcopy(step.runtime_checkpoint)
    report["pending_tool_result"] = {"id": "write-1", "content": _FILE_RESULT}
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"},
                                  will_retry=False, runtime_checkpoint=report)
    assert step.step_status == "pending"
    assert step.runtime_checkpoint == report
    assert step.attempt_count == 1
    assert step.error.get("failure", {}).get("requires_human") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [False, True])
async def test_journal_recovery_resumes_same_step_without_reexecuting_file(db_session, monkeypatch, returned):
    from packages.core.services import runtime_file_effect_recovery

    step, lease, effect = await _file_step(db_session, returned=returned)
    recover = AsyncMock(return_value=_receipt())
    monkeypatch.setattr(runtime_file_effect_recovery, "read_runtime_file_effect_receipt", recover)
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"}, will_retry=False)
    assert step.step_status == "pending"
    assert lease.status == "failed"
    assert step.attempt_count == 1
    assert step.current_lease_id is None
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "write-1"
    observation = json.loads(step.runtime_checkpoint["pending_tool_result"]["content"])
    assert observation["kind"] == "runtime_file_reconciliation"
    assert observation["original_tool_result_available"] is False
    assert step.evidence_refs[0]["effect"] == "action"
    assert ToolEffectEvidenceFactory.identity(step.evidence_refs[0]) == ToolEffectEvidenceFactory.identity(effect)
    assert step.evidence_refs[0]["fields"]["runtime_file_original_effect"] == effect
    assert len(step.evidence_refs) == 1
    assert recover.await_args.kwargs["step_id"] == step.id
    assert (await db_session.execute(select(HitlRequest))).scalars().all() == []


@pytest.mark.asyncio
async def test_recovered_file_never_refunds_attempt_or_restores_approval_as_transient(db_session, monkeypatch):
    from packages.core.services import runtime_file_effect_recovery
    from packages.core.governance import error_signatures

    step, lease, _ = await _file_step(db_session)
    monkeypatch.setattr(runtime_file_effect_recovery, "read_runtime_file_effect_receipt", AsyncMock(return_value=_receipt()))
    monkeypatch.setattr(error_signatures, "classify_execution_error", lambda *_args: error_signatures.ExecutionErrorHint(
        what_happened="Transient provider error", action_to_take="Retry later", is_transient=True,
    ))
    request = HitlRequest(entity_id=step.entity_id, workspace_id=step.workspace_id,
        origin_kind="step", origin_step_id=step.id, origin_plan_id=step.plan_id,
        dedup_key=f"operation:step:{step.plan_id}:{step.id}", status="consumed",
        consumed_at=datetime.now(timezone.utc), decided_by_user_id="operator")
    db_session.add(request)
    await db_session.flush()
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "TimeoutError", "message": "connection lost"})
    assert step.step_status == "pending"
    assert step.attempt_count == 1
    await db_session.refresh(request)
    assert request.status == "consumed" and request.consumed_at is not None
    assert request.decided_by_user_id == "operator"


@pytest.mark.asyncio
async def test_missing_file_proof_fails_without_human_retry_even_if_policy_requests_it(db_session):
    step, lease, effect = await _file_step(db_session, returned=True, auto_human=True)
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    assert step.step_status == lease.status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert step.error["failure"] == {
        "reason": step.error["message"], "retryable": False, "requires_human": False, "blockers": [],
    }
    assert step.error["recovery"]["file_operations"][0]["path"] == "documents/report.md"
    assert step.error["recovery"]["file_operations"][0]["result_sha256"] == _FILE_RESULT_SHA256
    assert step.evidence_refs == [effect]
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "write-1"
    assert (await db_session.execute(select(HitlRequest))).scalars().all() == []


@pytest.mark.asyncio
async def test_proven_file_output_contract_failure_stops_at_existing_attempt_budget(db_session):
    step, lease, _ = await _file_step(db_session, returned=True, observed=True, attempts=3, auto_human=True)
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "OutputSchemaError"})
    assert step.step_status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert step.error["cause"]["type"] == "OutputSchemaError"
    assert step.attempt_count == step.max_attempts == 3


@pytest.mark.asyncio
async def test_file_receipt_query_failure_propagates_without_terminal_classification(db_session, monkeypatch):
    from packages.core.services import runtime_file_effect_recovery

    step, lease, _ = await _file_step(db_session)
    monkeypatch.setattr(runtime_file_effect_recovery, "read_runtime_file_effect_receipt", AsyncMock(side_effect=OSError("database unavailable")))
    with pytest.raises(OSError, match="database unavailable"):
        await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    assert step.step_status == "running"
    assert lease.status == "active"


@pytest.mark.asyncio
async def test_internal_file_gap_does_not_become_reconciliation_card(db_session):
    step, lease, _ = await _file_step(db_session)
    await Dispatcher().lease_needs_human(db_session, lease.id, prompt="Verify the previous operation")
    assert step.step_status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert (await db_session.execute(select(HitlRequest))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "action"), [
    ("generate_file", "workspace.file.create"),
    ("video_edit", "workspace.file.modify"),
    ("delete_file", "workspace.file.delete"),
])
async def test_unsupported_internal_file_recovery_fails_without_inventing_journal_proof(db_session, monkeypatch, tool, action):
    from packages.core.services import runtime_file_effect_recovery

    step, lease, effect = await _file_step(db_session)
    effect["fields"].update(tool=tool, action_key=action)
    effect = ToolEffectEvidenceFactory.bind_effect_id(effect)
    step.evidence_refs = lease.evidence_refs = [effect]
    query = AsyncMock(side_effect=AssertionError("This tool has no exact journal recovery contract"))
    monkeypatch.setattr(runtime_file_effect_recovery, "read_runtime_file_effect_receipt", query)
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    assert step.step_status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert step.error["recovery"]["file_operations"][0]["tool"] == tool
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_provider_effect_keeps_conservative_hold(db_session):
    step, lease, effect = await _file_step(db_session)
    effect["fields"].update(tool="mcp__provider__publish", provider="provider", action_key="publish", capability_id="external.social")
    effect = ToolEffectEvidenceFactory.bind_effect_id(effect)
    assert not _is_system_file_effect(effect)
    step.evidence_refs = lease.evidence_refs = [effect]
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    assert lease.status == "needs_human"
    assert step.error["type"] == "MissingOutputContract"
    assert step.error["failure"]["requires_human"] is True


@pytest.mark.asyncio
async def test_mixed_internal_and_provider_gap_stops_with_all_evidence_retained(db_session):
    step, lease, file_effect = await _file_step(db_session)
    external = copy.deepcopy(file_effect)
    external["fields"].update(tool="mcp__provider__publish", provider="provider", tool_call_id="publish-1", action_key="publish", capability_id="external.social")
    external = ToolEffectEvidenceFactory.bind_effect_id(external)
    step.evidence_refs = lease.evidence_refs = [file_effect, external]
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    assert step.step_status == lease.status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert step.evidence_refs == lease.evidence_refs == [file_effect, external]
    assert step.error["failure"]["retryable"] is False
    assert (await db_session.execute(select(HitlRequest))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reporters", [2, 8])
async def test_concurrent_failure_reports_recover_exact_step_once(db_session, monkeypatch, reporters):
    from packages.core.services import runtime_file_effect_recovery

    step, lease, _ = await _file_step(db_session)
    lease.execution_claim_id = "exact-claim"
    lease.execution_claimed_at = datetime.now(timezone.utc)
    step_id, lease_id, worker_id = step.id, lease.id, lease.worker_id
    await db_session.commit()
    recover = AsyncMock(return_value=_receipt())
    monkeypatch.setattr(runtime_file_effect_recovery, "read_runtime_file_effect_receipt", recover)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def report():
        async with sessions() as db:
            try:
                await Dispatcher().fail_lease(
                    db, lease_id, error={"type": "MissingOutputContract"},
                    expected_worker_id=worker_id, expected_execution_claim_id="exact-claim",
                    require_execution_claim_id_if_present=True,
                )
                await db.commit()
                return "recovered"
            except LeaseNotActive:
                await db.rollback()
                return "stale"

    results = await asyncio.gather(*(report() for _ in range(reporters)))
    assert results.count("recovered") == 1
    assert results.count("stale") == reporters - 1
    recover.assert_awaited_once()
    await db_session.refresh(step)
    assert step.id == step_id and step.step_status == "pending"
    assert step.attempt_count == 1
    assert json.loads(step.runtime_checkpoint["pending_tool_result"]["content"])["kind"] == "runtime_file_reconciliation"
    assert await db_session.scalar(select(func.count()).select_from(WorkerActivityLog).where(
        WorkerActivityLog.lease_id == lease_id, WorkerActivityLog.event == "lease_fail",
    )) == 1


@pytest.mark.asyncio
async def test_genuine_input_after_proven_file_result_retains_original_question(db_session):
    step, lease, _ = await _file_step(db_session, returned=True, observed=True)
    workspace = Workspace(entity_id=step.entity_id, name="File recovery workspace", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    plan = await db_session.get(ExecutionPlan, step.plan_id)
    plan.workspace_id = step.workspace_id = lease.workspace_id = workspace.id
    await db_session.flush()
    previous = step.runtime_checkpoint
    checkpoint = RuntimeAgentCheckpoint(
        messages=[*previous["messages"], {"role": "tool", "tool_call_id": "write-1", "content": previous["pending_tool_result"]["content"]}],
        usage={}, rounds=2, tool_calls_made=["patch_file"],
        pending_tool_call={"id": "input-1", "name": "request_input", "arguments": {"question": "Which audience?"}},
    ).to_dict()
    await Dispatcher().lease_needs_human(
        db_session, lease.id, prompt="Which audience?", runtime_checkpoint=checkpoint,
        pending_action={"kind": "human_input", "fields": [{"name": "audience", "type": "text", "required": True}]},
    )
    assert step.step_status == "waiting_human"
    assert step.human_input_prompt == "Which audience?"
    assert step.runtime_checkpoint == checkpoint
    requests = (await db_session.execute(select(HitlRequest))).scalars().all()
    assert len(requests) == 1
    assert "effect_reconciliation" not in (requests[0].context or {})


@pytest.mark.asyncio
async def test_expired_internal_file_gap_is_system_failure(db_session):
    step, lease, _ = await _file_step(db_session, auto_human=True)
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    assert await Dispatcher().expire_leases(db_session) == 1
    assert lease.status == "expired"
    assert step.step_status == "failed"
    assert step.error["type"] == INTERNAL_FILE_RECOVERY_FAILED
    assert (await db_session.execute(select(HitlRequest))).scalars().all() == []


@pytest.mark.asyncio
async def test_system_recovery_failure_is_terminal_before_supervisor_human_or_replan_gates(db_session):
    step, lease, _ = await _file_step(db_session)
    await Dispatcher().fail_lease(db_session, lease.id, error={"type": "MissingOutputContract"})
    plan = await db_session.get(ExecutionPlan, step.plan_id)
    plan.task_id = "task-system-recovery"
    plan.last_error = {"type": "ActionOutcomeReconciliationRequired"}
    verdict = await evaluate_task_completion_gates(
        db_session, task=None, plan=plan, steps=[step], plan_status="failed", locale="en",
    )
    assert verdict.verdict.value == "failed"
    assert await PlanExecutor._maybe_replan(db_session, plan, [step]) is False
    assert plan.last_error["type"] == INTERNAL_FILE_RECOVERY_FAILED
