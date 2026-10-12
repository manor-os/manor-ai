"""One verified delivery feeds V3 acceptance and output, never a second writer."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.contracts.artifact_delivery import accepted_task_artifact_manifest
from packages.core.contracts.step_execution import STEP_EXECUTION_CONTRACT_PARAM
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.task import Task
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task_coordinator import TaskCoordinatorPhase
from packages.core.models.user import Entity, User, UserMembership
from packages.core.plans.executor import (
    PlanExecutor, finalize_task_coordinator_submission, prepare_task_coordinator_submission,
    resume_task_coordinator_delivery_review, resolve_resumed_delivery_review_hitl,
)
from packages.core.services.task_acceptance_review import TaskAcceptanceError, parse_task_acceptance_review
from packages.core.services.task_execution_reconcile import _actual_output_from_steps, reconcile_task_from_latest_completed_plan
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake, stage_task_coordinator_wake, settle_task_coordinator_wake, release_task_coordinator_claim,
)
from test_task_coordinator_executor_handoff import _seed


async def _delivery(db, tmp_path, monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")
    seeded = await _seed(db, step_status="done", run_phase=TaskCoordinatorPhase.RUNNING.value)
    user_id, document_id = generate_ulid(), generate_ulid()
    db.add_all([
        Entity(id=seeded.task.entity_id, name="Delivery regression"),
        User(id=user_id, entity_id=seeded.task.entity_id, email=f"{user_id}@test.invalid", password_hash="unused", role="owner", status="active"),
        UserMembership(entity_id=seeded.task.entity_id, user_id=user_id, role="owner", status="active"),
    ])
    seeded.task.creator_id = user_id
    seeded.task.details = {"requires_artifact": True}
    seeded.task.title = "Recover five reference Reels and save a report"
    seeded.step.step_key = "recover_and_save_report"
    seeded.plan.plan_dag = {"steps": [{"key": seeded.step.step_key, "kind": "llm", "depends_on": []}]}
    seeded.step.params = {
        "prompt": "Save the recovery report",
        STEP_EXECUTION_CONTRACT_PARAM: {"delivery": "artifact", "effects": "artifact_write"},
    }
    # This is the incident's exact envelope: the outer summary is not a
    # creation signal in outputs.data, and a viewer URL has no extension.
    seeded.step.result = {
        "status": "succeeded", "summary": "Saved recovery report",
        "outputs": {"data": {"report": "Recovered five references", "files": [
            {"document_url": f"/viewer/{document_id}"},
        ]}},
    }
    seeded.step.evidence_refs = [
        {"kind": "artifact", "fs_path": "recovery.md"},
        {"kind": "artifact", "type": "knowledge_document", "document_id": document_id},
        {"kind": "artifact", "document_id": "temporary-input-not-submitted"},
    ]
    document = Document(
        id=document_id, entity_id=seeded.task.entity_id, name="Recovery report", fs_path="recovery.md",
        owner_id=user_id, file_type="md", visibility="entity",
        metadata_={"origin": {"task_id": seeded.task.id, "workspace_id": seeded.task.workspace_id}},
    )
    db.add(document)
    root = tmp_path / seeded.task.entity_id
    root.mkdir()
    (root / "recovery.md").write_text("Recovered all five reference Reels.\n", encoding="utf-8")
    await db.flush()
    wake = await stage_task_coordinator_wake(db, task_id=seeded.task.id, reason="manifest_regression")
    claim, reason = await claim_task_coordinator_wake(
        db, task_id=seeded.task.id, event_id=wake.event_id, wake_generation=wake.wake_generation,
        claim_token="manifest-owner", selection=seeded.selection, lease_seconds=120,
    )
    assert reason == "claimed"
    return seeded, document, claim


def _review(snapshot):
    return parse_task_acceptance_review(json.dumps({
        "verdict": "accepted", "evidence": "The saved report contains the five-reference recovery.", "issues": [],
        "checks": [{"criterion": "Saved recovery report", "pointer": "/artifacts/0/text", "quote": "Recovered all five reference Reels."}],
    }), snapshot)


@pytest.mark.asyncio
async def test_nested_viewer_delivery_passes_gate_and_projects_without_rewriting_payload(db_session, tmp_path, monkeypatch):
    seeded, document, claim = await _delivery(db_session, tmp_path, monkeypatch)
    original = copy.deepcopy(seeded.step.result)
    seeded.step.expected_output_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"status": {"type": "string"}, "summary": {"type": "string"}, "outputs": {
            "type": "object", "additionalProperties": False, "properties": {"data": {
                "type": "object", "additionalProperties": False,
                "properties": {"report": {"type": "string"}, "files": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "properties": {"document_url": {"type": "string"}},
                }}},
            }},
        }},
    }
    await db_session.flush()
    provenance = copy.deepcopy(document.metadata_)
    snapshot, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert gate is None
    assert len(snapshot.payload["artifacts"]) == 1
    assert snapshot.payload["artifacts"][0]["document_id"] == document.id
    assert snapshot.payload["artifact_manifest"]["steps"][0]["refs"][0]["document_id"] == document.id
    assert seeded.step.result == original
    accepted, gate, _ = await finalize_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
        acceptance_review=_review(snapshot),
    )
    assert accepted and gate is None
    assert seeded.task.actual_output["files"][0]["document_id"] == document.id
    assert len(seeded.task.actual_output["files"]) == 1
    assert seeded.step.result == original and document.metadata_ == provenance
    rebuilt = _actual_output_from_steps(seeded.plan, [seeded.step], task=seeded.task,
                                       supervisor_review={"verdict": "completed"})
    assert rebuilt["files"] == seeded.task.actual_output["files"]
    seeded.task.actual_output = {**seeded.task.actual_output, "files": None}
    assert await reconcile_task_from_latest_completed_plan(db_session, seeded.task)
    assert seeded.task.actual_output["files"] == rebuilt["files"]
    assert document.metadata_ == provenance


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["artifact_evidence", "custom_release_document"])
async def test_declared_business_file_slot_reaches_same_plan_acceptance(db_session, tmp_path, monkeypatch, slot):
    from packages.core.contracts.task_output import task_output_envelope_schema
    from packages.core.plans.executor import _has_artifact_result, _produced_artifact_labels

    seeded, document, claim = await _delivery(db_session, tmp_path, monkeypatch)
    schema = {"type": "object", "required": ["result", slot], "additionalProperties": False, "properties": {
        "result": {"type": "string"},
        slot: {"type": "object", "required": ["fs_path"], "additionalProperties": False,
               "properties": {"fs_path": {"type": "string"}, "document_id": {"type": "string"}}},
    }}
    seeded.task.expected_output = schema
    seeded.step.expected_output_schema = task_output_envelope_schema(schema)
    seeded.step.result = {"status": "succeeded", "summary": "Saved report", "outputs": {"data": {
        "result": "Verified replacement created", slot: {"fs_path": document.fs_path, "document_id": document.id},
    }}}
    original = copy.deepcopy(seeded.step.result)
    original_attempt_count = seeded.step.attempt_count
    await db_session.flush()
    assert _has_artifact_result([seeded.step])
    assert _produced_artifact_labels([seeded.step]) == ["recovery.md"]
    diagnostic, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim, inspection=True,
    )
    assert gate is None and diagnostic.payload["verification"]["status"] == "verified"
    declaration = diagnostic.payload["steps"][0]["delivery_contract"]["declaration"]
    assert declaration == {"basis": "unique_required_schema", "pointers": [f"/outputs/data/{slot}"],
                           "requires_semantic_assessment": True}
    strict, gate = await prepare_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert gate is None and diagnostic.digest == strict.digest
    accepted, gate, _ = await finalize_task_coordinator_submission(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
        acceptance_review=_review(strict),
    )
    assert accepted and gate is None
    assert seeded.step.result == original
    assert seeded.step.attempt_count == original_attempt_count
    assert seeded.task.actual_output["files"][0]["document_id"] == document.id


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "conflict", "trash", "restricted"])
async def test_invalid_delivery_is_typed_verification_error_not_replan(db_session, tmp_path, monkeypatch, invalid):
    seeded, document, claim = await _delivery(db_session, tmp_path, monkeypatch)
    if invalid == "missing":
        seeded.step.result = {"summary": "The report was saved"}
    elif invalid == "conflict":
        seeded.step.result = {"files": [{"document_id": document.id, "fs_path": "other.md"}]}
    elif invalid == "trash":
        document.is_trashed = True
    else:
        document.classification = "restricted"
    await db_session.flush()
    with pytest.raises(TaskAcceptanceError) as caught:
        await prepare_task_coordinator_submission(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim)
    assert caught.value.code.startswith("artifact_")
    assert seeded.plan.status == "running" and seeded.task.status == "in_progress"
    assert seeded.step.step_status == "done"
    assert not seeded.plan.last_error


@pytest.mark.asyncio
async def test_explicit_inline_tool_artifacts_are_not_final_delivery(db_session, tmp_path, monkeypatch):
    seeded, _, claim = await _delivery(db_session, tmp_path, monkeypatch)
    seeded.task.details = {}
    seeded.task.title = "Summarize reference observations inline"
    seeded.step.params = {STEP_EXECUTION_CONTRACT_PARAM: {"delivery": "inline", "effects": "read_only"}}
    await db_session.flush()
    snapshot, gate = await prepare_task_coordinator_submission(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim)
    assert gate is None and snapshot.payload["artifacts"] == []
    assert snapshot.payload["artifact_manifest"]["steps"][0]["refs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "submission", "contract"])
async def test_changed_delivery_cannot_reuse_prior_acceptance(db_session, tmp_path, monkeypatch, change):
    seeded, _, claim = await _delivery(db_session, tmp_path, monkeypatch)
    snapshot, _ = await prepare_task_coordinator_submission(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim)
    if change == "bytes":
        (tmp_path / seeded.task.entity_id / "recovery.md").write_text("Different report", encoding="utf-8")
    elif change == "submission":
        seeded.step.result = {**seeded.step.result, "summary": "Changed submitted result"}
    else:
        seeded.step.params = {**seeded.step.params, "prompt": "Changed contract"}
    await db_session.flush()
    with pytest.raises(TaskAcceptanceError, match="stale") as caught:
        await finalize_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim, acceptance_review=_review(snapshot),
        )
    assert caught.value.code == "invalid_assessment"
    seeded.plan.dispatcher_state = {**seeded.plan.dispatcher_state, "task_content_acceptance": {
        "snapshot_digest": snapshot.digest, "verdict": "accepted", "artifact_manifest": snapshot.payload["artifact_manifest"],
    }}
    if change != "bytes":
        assert accepted_task_artifact_manifest(seeded.plan, [seeded.step]) is None


@pytest.mark.asyncio
async def test_concurrent_artifact_acceptance_keeps_one_terminal_writer(db_session, tmp_path, monkeypatch):
    seeded, _, claim = await _delivery(db_session, tmp_path, monkeypatch)
    task_id, plan_id = seeded.task.id, seeded.plan.id
    snapshot, _ = await prepare_task_coordinator_submission(db_session, task_id=task_id, plan_id=plan_id, claim=claim)
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    writes = []

    async def finalize(db, plan, status, *, server_decision):
        assert server_decision.verdict.value == "completed"
        writes.append(plan.id)
        plan.status = status
        task = await db.get(Task, task_id)
        task.status = status
        return {"event_type": "task.succeeded"}

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(finalize))

    async def submit():
        async with sessions() as db:
            try:
                result, _, _ = await finalize_task_coordinator_submission(
                    db, task_id=task_id, plan_id=plan_id, claim=claim, acceptance_review=_review(snapshot),
                )
                await db.commit()
                return result
            except ValueError:
                await db.rollback()
                return False

    assert sum(await asyncio.wait_for(asyncio.gather(*(submit() for _ in range(8))), timeout=30)) == 1
    assert writes == [plan_id]
    async with sessions() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        assert plan.dispatcher_state["task_content_acceptance"]["artifact_manifest"] == snapshot.payload["artifact_manifest"]


async def _held_delivery(db, tmp_path, monkeypatch):
    from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory

    seeded, document, claim = await _delivery(db, tmp_path, monkeypatch)
    assert await settle_task_coordinator_wake(db, task_id=seeded.task.id, event_id=claim.event_id,
                                            wake_generation=claim.wake_generation, claim_token=claim.claim_token)
    await release_task_coordinator_claim(db, task_id=seeded.task.id, claim_token=claim.claim_token,
                                        claim_generation=claim.wake_generation)
    seeded.run.phase = "waiting_human"
    seeded.task.status = "waiting_on_customer"
    seeded.plan.status = "failed"
    seeded.plan.last_error = {"type": "ActionOutcomeReconciliationRequired", "message":
                             "Step 'recover_and_save_report' recorded an external action; a new Plan changes its identity."}
    seeded.step.evidence_refs = [*seeded.step.evidence_refs, ToolEffectEvidenceFactory.effect("action", {
        "tool": "generate_file", "tool_call_id": "write-once", "intent_state": "returned",
        "document_id": document.id,
    })]
    await db.flush()
    return seeded, document


@pytest.mark.asyncio
async def test_explicit_resume_only_rechecks_same_plan_and_preserves_effects(db_session, tmp_path, monkeypatch):
    seeded, _ = await _held_delivery(db_session, tmp_path, monkeypatch)
    original = copy.deepcopy((seeded.step.result, seeded.step.evidence_refs, seeded.step.params, seeded.step.attempt_count))
    version = seeded.run.state_version
    resumed = await resume_task_coordinator_delivery_review(
        db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
        expected_state_version=version, actor_user_id=seeded.task.creator_id,
    )
    assert resumed["state_version"] == version + 1
    assert seeded.task.status == "in_progress" and seeded.plan.status == "running"
    assert seeded.run.current_plan_id == seeded.plan.id and seeded.run.claim_token is None
    assert seeded.step.step_status == "done"
    assert (seeded.step.result, seeded.step.evidence_refs, seeded.step.params, seeded.step.attempt_count) == original
    assert seeded.plan.last_error is None
    assert "task_content_acceptance" not in seeded.plan.dispatcher_state
    # The ordinary owner claim and ordinary acceptance path still decide
    # completion; resuming review alone cannot assert Task success.
    claim, reason = await claim_task_coordinator_wake(
        db_session, task_id=seeded.task.id, event_id=resumed["event_id"], wake_generation=resumed["wake_generation"],
        claim_token="resumed-owner", selection=seeded.selection, lease_seconds=120,
    )
    assert reason == "claimed"
    snapshot, gate = await prepare_task_coordinator_submission(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim)
    assert gate is None and snapshot.payload["artifacts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["stale_version", "live_claim", "unknown_effect", "unfinished_step", "revoked_file", "other_hold", "user_decision", "newer_plan", "cancelled"])
async def test_same_plan_resume_fails_closed_without_resetting_execution(db_session, tmp_path, monkeypatch, blocker):
    from datetime import datetime, timedelta, timezone
    from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
    from packages.core.models.hitl_request import HitlRequest

    seeded, document = await _held_delivery(db_session, tmp_path, monkeypatch)
    version = seeded.run.state_version
    if blocker == "stale_version":
        version -= 1
    elif blocker == "live_claim":
        seeded.run.claim_token = "other-owner"
        seeded.run.claim_generation = "other-generation"
        seeded.run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    elif blocker == "unknown_effect":
        seeded.step.evidence_refs = [*seeded.step.evidence_refs, ToolEffectEvidenceFactory.effect("pending", {
            "tool": "publish", "tool_call_id": "unknown-outcome", "intent_state": "pending",
        })]
    elif blocker == "unfinished_step":
        seeded.step.step_status = "failed"
    elif blocker == "revoked_file":
        document.is_trashed = True
    elif blocker == "other_hold":
        seeded.plan.last_error = {"type": "RealUserApprovalRequired"}
    elif blocker == "user_decision":
        db_session.add(HitlRequest(
            id=generate_ulid(), entity_id=seeded.task.entity_id, workspace_id=seeded.task.workspace_id,
            origin_kind="task", origin_task_id=seeded.task.id, origin_plan_id=seeded.plan.id,
            action_key="task.request_input", hitl_type="input", status="pending", dedup_key="real-decision",
        ))
    elif blocker == "newer_plan":
        db_session.add(ExecutionPlan(
            id=generate_ulid(), entity_id=seeded.task.entity_id, workspace_id=seeded.task.workspace_id,
            task_id=seeded.task.id, status="failed", plan_dag={},
            created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        ))
    else:
        seeded.task.status = "cancelled"
    await db_session.flush()
    original = copy.deepcopy((seeded.step.result, seeded.step.evidence_refs, seeded.step.params, seeded.step.attempt_count))
    with pytest.raises(ValueError):
        await resume_task_coordinator_delivery_review(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
                                                     expected_state_version=version, actor_user_id=seeded.task.creator_id)
    assert seeded.task.status == ("cancelled" if blocker == "cancelled" else "waiting_on_customer")
    assert seeded.plan.status == "failed"
    assert (seeded.step.result, seeded.step.evidence_refs, seeded.step.params, seeded.step.attempt_count) == original


@pytest.mark.asyncio
async def test_resume_endpoint_expires_only_its_error_preserving_hitl_history(db_session, tmp_path, monkeypatch):
    from apps.api.routers.tasks import ResumeDeliveryReviewRequest, resume_delivery_review_endpoint
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Message

    seeded, _ = await _held_delivery(db_session, tmp_path, monkeypatch)
    request = HitlRequest(
        id=generate_ulid(), entity_id=seeded.task.entity_id, workspace_id=seeded.task.workspace_id,
        origin_kind="task", origin_task_id=seeded.task.id, origin_plan_id=seeded.plan.id,
        action_key="task.recover", hitl_type="error", status="pending", dedup_key="obsolete-error",
        matched_rule="task.needs_recovery", reason=seeded.plan.last_error["message"].replace("recover_and_save_report", "recover and save report"),
    )
    already_decided = HitlRequest(
        id=generate_ulid(), entity_id=seeded.task.entity_id, workspace_id=seeded.task.workspace_id,
        origin_kind="task", origin_task_id=seeded.task.id, origin_plan_id=seeded.plan.id,
        action_key="task.request_input", hitl_type="input", status="consumed", dedup_key="answered-input",
    )
    card = Message(id=generate_ulid(), conversation_id=generate_ulid(), role="assistant", content="Original missing-file error",
                   pending_action={"kind": "task_recovery", "task_id": seeded.task.id, "approval_request_id": request.id})
    db_session.add_all([request, already_decided, card])
    await db_session.flush()
    user = await db_session.get(User, seeded.task.creator_id)
    result = await resume_delivery_review_endpoint(
        seeded.task.id, ResumeDeliveryReviewRequest(expected_plan_id=seeded.plan.id, expected_state_version=seeded.run.state_version),
        user=user, db=db_session,
    )
    assert result["status"] == "review_queued" and result["reset_steps"] == 0
    assert result["hitl_cleanup_pending"] is False
    assert request.status == "expired" and request.resolved_reason == "delivery_review_resumed"
    assert card.content == "Original missing-file error" and card.resolved_at is not None
    assert already_decided.status == "consumed"
    assert await resolve_resumed_delivery_review_hitl(db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
                                                     actor_user_id=user.id) == 0


@pytest.mark.asyncio
async def test_concurrent_review_resume_has_one_same_plan_wake(db_session, tmp_path, monkeypatch):
    seeded, _ = await _held_delivery(db_session, tmp_path, monkeypatch)
    task_id, plan_id, actor_id, version = seeded.task.id, seeded.plan.id, seeded.task.creator_id, seeded.run.state_version
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def resume():
        async with sessions() as db:
            try:
                result = await resume_task_coordinator_delivery_review(db, task_id=task_id, plan_id=plan_id,
                                                                     expected_state_version=version, actor_user_id=actor_id)
                await db.commit()
                return result
            except ValueError:
                await db.rollback()
                return None

    outcomes = await asyncio.wait_for(asyncio.gather(*(resume() for _ in range(8))), timeout=30)
    assert len([item for item in outcomes if item]) == 1
    assert next(item for item in outcomes if item)["plan_id"] == plan_id


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["cross_entity", "workspace_forbidden"])
async def test_resume_endpoint_checks_entity_and_workspace_authority(db_session, tmp_path, monkeypatch, denial):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from apps.api.routers import tasks as api

    seeded, _ = await _held_delivery(db_session, tmp_path, monkeypatch)
    user = await db_session.get(User, seeded.task.creator_id)
    expected = 404
    if denial == "cross_entity":
        user = SimpleNamespace(id=user.id, entity_id="another-entity")
    else:
        expected = 403

        async def denied(*_args, **_kwargs):
            raise HTTPException(403, "Workspace is not writable")

        monkeypatch.setattr(api, "require_workspace_writable", denied)
    with pytest.raises(HTTPException) as caught:
        await api.resume_delivery_review_endpoint(
            seeded.task.id, api.ResumeDeliveryReviewRequest(expected_plan_id=seeded.plan.id,
                                                           expected_state_version=seeded.run.state_version),
            user=user, db=db_session,
        )
    assert caught.value.status_code == expected
    assert seeded.task.status == "waiting_on_customer" and seeded.plan.status == "failed"


@pytest.mark.asyncio
async def test_old_retry_waiting_on_resume_lock_cannot_reset_done_writer(db_session, tmp_path, monkeypatch):
    from packages.core.services.task_retry_service import TaskRetryError, prepare_task_retry

    seeded, _ = await _held_delivery(db_session, tmp_path, monkeypatch)
    task_id, plan_id, actor_id, version = seeded.task.id, seeded.plan.id, seeded.task.creator_id, seeded.run.state_version
    original = copy.deepcopy((seeded.step.result, seeded.step.evidence_refs, seeded.step.attempt_count))
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    retry_started = asyncio.Event()
    async with sessions() as resume_db, sessions() as retry_db:
        stale_task = await retry_db.get(Task, task_id)
        await resume_task_coordinator_delivery_review(resume_db, task_id=task_id, plan_id=plan_id,
                                                     expected_state_version=version, actor_user_id=actor_id)

        async def old_retry():
            retry_started.set()
            with pytest.raises(TaskRetryError, match="already in progress") as caught:
                await prepare_task_retry(retry_db, task=stale_task, user_id=actor_id, user_label="Old recovery card")
            assert caught.value.status_code == 409
            await retry_db.rollback()

        competing = asyncio.create_task(old_retry())
        await retry_started.wait()
        await resume_db.commit()
        await asyncio.wait_for(competing, timeout=10)
    await db_session.refresh(seeded.step)
    assert seeded.step.step_status == "done"
    assert (seeded.step.result, seeded.step.evidence_refs, seeded.step.attempt_count) == original
