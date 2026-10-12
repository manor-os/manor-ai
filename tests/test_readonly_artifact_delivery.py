"""A delivered existing Document is evidence, not a new write or provenance."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.contracts.artifact_delivery import (
    ARTIFACT_DELIVERY_SOURCE,
    readonly_artifact_delivery,
    reused_artifact_evidence,
    reused_artifact_snapshots,
)
from packages.core.contracts.step_execution import STEP_EXECUTION_CONTRACT_PARAM
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.worker import WorkLease
from packages.core.models.workspace import Workspace
from packages.core.services.artifact_delivery import (
    ArtifactDeliveryError,
    _identity,
    submitted_artifact_refs,
    verify_artifact_delivery_refs,
    verify_artifact_snapshot_access,
)
from packages.core.services.entity_fs import canonical_entity_root, entity_filesystem_mutation_lock
from tests.test_dispatcher_artifact_projection_transaction import _seed_artifact_lease


REUSE = {STEP_EXECUTION_CONTRACT_PARAM: {"delivery": "artifact", "effects": "read_only"}}


@pytest.fixture(autouse=True)
def local_files(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")


def test_reuse_contract_is_explicit_not_inferred_from_output_or_dag_position():
    assert readonly_artifact_delivery(REUSE)
    assert not readonly_artifact_delivery({})
    assert not readonly_artifact_delivery({STEP_EXECUTION_CONTRACT_PARAM: {"delivery": "inline", "effects": "read_only"}})
    assert not readonly_artifact_delivery({STEP_EXECUTION_CONTRACT_PARAM: {"delivery": "artifact", "effects": "artifact_write"}})


@pytest.mark.parametrize("payload", [
    {"summary": "Read /viewer/doc_a"},
    {"references": [{"document_id": "doc_a"}]},
    {"source": {"fs_path": "source.md"}},
    {"text": "Saved file: source.md"},
])
def test_context_or_prose_never_counts_as_submitted_artifact(payload):
    with pytest.raises(ArtifactDeliveryError, match="Submit the existing"):
        submitted_artifact_refs(payload)


def test_exact_submission_preserves_conflicting_identity_assertions():
    ref = {"document_id": "doc_a", "fs_path": "a.md", "file_url": "/viewer/doc_b"}
    assert submitted_artifact_refs({"outputs": {"data": ref}}) == [ref]
    with pytest.raises(ArtifactDeliveryError, match="same file"):
        _identity(ref, "entity")


@pytest.mark.parametrize("ref", [
    {"fs_path": "../secret.md"},
    {"url": "/api/v1/fs/other/file.md"},
    {"document_id": "/viewer/doc_a"},
    {"url": "https://example.test/viewer/doc_a"},
    {"fs_path": "a.md", "path": "b.md"},
])
def test_invalid_or_cross_entity_addresses_fail_closed(ref):
    with pytest.raises(ArtifactDeliveryError):
        _identity(ref, "entity")


def test_host_receipt_is_bound_to_exact_step_and_submission_and_cannot_be_reported():
    finished_at = datetime.now(timezone.utc)
    step = SimpleNamespace(id="step", plan_id="plan", params=REUSE, result={"document_id": "doc"}, attempt_count=1, finished_at=finished_at)
    snapshot = {"document_id": "doc", "fs_path": "file.md", "source_sha256": "a" * 64}
    step.evidence_refs = reused_artifact_evidence(step, step.result, [snapshot], lease_id="lease", completed_at=finished_at)
    assert reused_artifact_snapshots(step) == [snapshot]
    assert ToolEffectEvidenceFactory.merge_untrusted([], step.evidence_refs) == []
    assert ToolEffectEvidenceFactory.merge_untrusted(step.evidence_refs, []) == step.evidence_refs
    step.result = {"document_id": "other"}
    assert reused_artifact_snapshots(step) == []
    step.result = {"document_id": "doc"}
    step.id = "other-step"
    assert reused_artifact_snapshots(step) == []
    step.id = "step"
    step.attempt_count = 2
    assert reused_artifact_snapshots(step) == []
    step.attempt_count = 1
    step.finished_at = None
    assert reused_artifact_snapshots(step) == []


async def _seed(db, tmp_path):
    ids = await _seed_artifact_lease(db, tmp_path, name="existing.md")
    step = await db.get(ExecutionStep, ids["step_id"])
    plan = await db.get(ExecutionPlan, step.plan_id)
    task = await db.get(Task, plan.task_id)
    user_id = generate_ulid()
    document_id = generate_ulid()
    db.add_all([
        Entity(id=ids["entity_id"], name="Artifact test entity"),
        User(id=user_id, entity_id=ids["entity_id"], email=f"{user_id}@test.invalid", password_hash="unused-test-hash", role="owner", status="active"),
        UserMembership(entity_id=ids["entity_id"], user_id=user_id, role="owner", status="active"),
        Document(id=document_id, entity_id=ids["entity_id"], name="Existing source", fs_path="existing.md", owner_id=user_id, file_type="md", visibility="entity", metadata_={"origin": {"task_id": "producer-task", "workspace_id": ids["workspace_id"]}}),
    ])
    task.creator_id = user_id
    step.kind = "subagent"
    step.params = copy.deepcopy(REUSE)
    await db.commit()
    return {**ids, "user_id": user_id, "document_id": document_id, "plan_id": plan.id, "task_id": task.id}


def _verify_args(ids):
    return {"entity_id": ids["entity_id"], "workspace_id": ids["workspace_id"], "user_id": ids["user_id"], "refs": [{"document_id": ids["document_id"], "fs_path": "existing.md", "url": f"/viewer/{ids['document_id']}"}]}


@pytest.mark.asyncio
async def test_verified_reuse_is_current_canonical_and_does_not_touch_document(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    document = await db_session.get(Document, ids["document_id"])
    before = (copy.deepcopy(document.metadata_), document.updated_at)
    result = await verify_artifact_delivery_refs(db_session, **_verify_args(ids), text_limit=4)
    assert result.projection.refs[0]["open_url"] == f"/viewer/{document.id}"
    assert result.snapshots[0]["source_sha256"] == hashlib.sha256(b"completed artifact\n").hexdigest()
    assert result.snapshots[0]["text"] == "comp"
    assert result.snapshots[0]["text_available"] is True
    assert result.snapshots[0]["text_truncated"] is True
    await db_session.commit()
    await db_session.refresh(document)
    assert (document.metadata_, document.updated_at) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["trash", "restricted", "identity", "membership_revoked"])
async def test_cached_inspection_access_rechecks_document_and_acl(db_session, tmp_path, change):
    ids = await _seed(db_session, tmp_path)
    prior = await verify_artifact_delivery_refs(db_session, **_verify_args(ids), text_limit=24_000)
    access = {
        "entity_id": ids["entity_id"], "workspace_id": ids["workspace_id"],
        "user_id": ids["user_id"], "expected_snapshots": prior.snapshots,
    }
    await verify_artifact_snapshot_access(db_session, **access)
    document = await db_session.get(Document, ids["document_id"])
    if change == "trash":
        document.is_trashed = True
    elif change == "restricted":
        document.classification = "restricted"
    elif change == "identity":
        document.fs_path = "different.md"
    else:
        membership = (await db_session.execute(
            select(UserMembership).where(UserMembership.user_id == ids["user_id"])
        )).scalar_one()
        membership.status = "inactive"
    await db_session.commit()
    with pytest.raises(ArtifactDeliveryError):
        await verify_artifact_snapshot_access(db_session, **access)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["trash", "restricted", "quarantined", "workspace_deleted", "identity", "bytes", "symlink", "membership_revoked"])
async def test_reused_delivery_rejects_revoked_changed_or_inconsistent_artifact(db_session, tmp_path, change):
    ids = await _seed(db_session, tmp_path)
    prior = await verify_artifact_delivery_refs(db_session, **_verify_args(ids))
    document = await db_session.get(Document, ids["document_id"])
    if change == "trash":
        document.is_trashed = True
    elif change == "restricted":
        document.classification = "restricted"
    elif change == "quarantined":
        document.quarantine_status = "quarantined"
    elif change == "workspace_deleted":
        workspace = await db_session.get(Workspace, ids["workspace_id"])
        workspace.deleted_at = datetime.now(timezone.utc)
    elif change == "membership_revoked":
        membership = (await db_session.execute(select(UserMembership).where(UserMembership.user_id == ids["user_id"]))).scalar_one()
        membership.status = "inactive"
    elif change == "identity":
        document.fs_path = "different.md"
    elif change == "bytes":
        (tmp_path / ids["entity_id"] / "existing.md").write_text("changed", encoding="utf-8")
    elif change == "symlink":
        target = tmp_path / ids["entity_id"] / "existing.md"
        target.unlink()
        target.symlink_to(tmp_path / "outside.md")
    await db_session.commit()
    with pytest.raises(ArtifactDeliveryError):
        await verify_artifact_delivery_refs(db_session, **_verify_args(ids), expected_snapshots=prior.snapshots)


@pytest.mark.asyncio
async def test_two_parallel_consumers_share_reads_without_rewriting_source(db_session, tmp_path, monkeypatch):
    from packages.core.services import artifact_delivery

    ids = await _seed(db_session, tmp_path)
    source = await db_session.get(Document, ids["document_id"])
    before = (copy.deepcopy(source.metadata_), source.updated_at)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    barrier = threading.Barrier(2, timeout=3)
    read_snapshot = artifact_delivery._read_snapshot

    def overlapping_read(*args, **kwargs):
        # Both readers must hold the shared filesystem read lock concurrently.
        barrier.wait()
        return read_snapshot(*args, **kwargs)

    monkeypatch.setattr(artifact_delivery, "_read_snapshot", overlapping_read)

    async def read():
        async with sessions() as db:
            result = await verify_artifact_delivery_refs(db, **_verify_args(ids))
            await db.commit()
            return result.snapshots

    first, second = await asyncio.gather(read(), read())
    assert first == second
    await db_session.refresh(source)
    assert (source.metadata_, source.updated_at) == before


@pytest.mark.asyncio
async def test_concurrent_writer_returns_busy_instead_of_waiting_under_database_locks(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    async with entity_filesystem_mutation_lock(canonical_entity_root(ids["entity_id"])):
        with pytest.raises(ArtifactDeliveryError) as error:
            await asyncio.wait_for(verify_artifact_delivery_refs(db_session, **_verify_args(ids)), timeout=1)
        assert error.value.code == "artifact_busy"


@pytest.mark.asyncio
async def test_revocation_during_physical_read_is_rechecked(db_session, tmp_path, monkeypatch):
    from packages.core.services import artifact_delivery

    ids = await _seed(db_session, tmp_path)
    started, release = threading.Event(), threading.Event()
    read_snapshot = artifact_delivery._read_snapshot
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    def waiting_read(*args, **kwargs):
        facts = read_snapshot(*args, **kwargs)
        started.set()
        assert release.wait(timeout=3)
        return facts

    monkeypatch.setattr(artifact_delivery, "_read_snapshot", waiting_read)
    verification = asyncio.create_task(verify_artifact_delivery_refs(db_session, **_verify_args(ids)))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        async with sessions() as db:
            await db.execute(update(UserMembership).where(UserMembership.user_id == ids["user_id"]).values(status="inactive"))
            await db.commit()
    finally:
        release.set()
    with pytest.raises(ArtifactDeliveryError) as error:
        await verification
    assert error.value.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_parallel_step_settlement_keeps_independent_receipts_and_original_producer(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    original_lease = await db_session.get(WorkLease, ids["lease_id"])
    task_id, plan_id, step_id, lease_id = [generate_ulid() for _ in range(4)]
    db_session.add_all([
        Task(id=task_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], title="Second consumer", status="in_progress", creator_id=ids["user_id"]),
        ExecutionPlan(id=plan_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], task_id=task_id, status="running", plan_dag={"steps": []}),
        ExecutionStep(id=step_id, plan_id=plan_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], step_key="reuse", kind="subagent", params=copy.deepcopy(REUSE), depends_on=[], step_status="running", attempt_count=1, max_attempts=3, current_lease_id=lease_id),
        WorkLease(id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], worker_id=original_lease.worker_id, status="active", lease_until=original_lease.lease_until),
    ])
    await db_session.commit()
    source = await db_session.get(Document, ids["document_id"])
    before = (copy.deepcopy(source.metadata_), source.updated_at)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def settle(target):
        async with sessions() as db:
            lease = await Dispatcher().complete_lease(db, target, result={"document_id": ids["document_id"], "fs_path": "existing.md"})
            await db.commit()
            return lease.status

    assert await asyncio.gather(settle(ids["lease_id"]), settle(lease_id)) == ["completed", "completed"]
    for target in (ids["step_id"], step_id):
        step = await db_session.get(ExecutionStep, target, populate_existing=True)
        assert reused_artifact_snapshots(step)[0]["document_id"] == source.id
        assert step.evidence_refs[0]["fields"]["owner_step_id"] == target
    await db_session.refresh(source)
    assert (source.metadata_, source.updated_at) == before


@pytest.mark.asyncio
async def test_dispatcher_mints_receipt_and_reconciliation_preserves_producer(db_session, tmp_path, monkeypatch):
    from packages.core.services import artifact_knowledge
    from packages.core.services.task_execution_reconcile import _project_actual_output_artifacts

    ids = await _seed(db_session, tmp_path)
    source = await db_session.get(Document, ids["document_id"])
    before = (copy.deepcopy(source.metadata_), source.updated_at)

    async def forbidden(**_kwargs):
        raise AssertionError("read-only delivery must not sync or rebind a Document")

    monkeypatch.setattr(artifact_knowledge, "project_artifact_refs_to_knowledge", forbidden)
    payload = {"document_id": ids["document_id"], "fs_path": "existing.md"}
    lease = await Dispatcher().complete_lease(db_session, ids["lease_id"], result=payload)
    await db_session.commit()
    step = await db_session.get(ExecutionStep, ids["step_id"])
    assert lease.status == "completed" and step.step_status == "done"
    receipts = [ref for ref in step.evidence_refs if ref.get("source") == ARTIFACT_DELIVERY_SOURCE]
    assert len(receipts) == 1
    assert reused_artifact_snapshots(step)
    assert not any(ref.get("kind") == "tool_effect" for ref in step.evidence_refs)
    task = await db_session.get(Task, ids["task_id"])
    actual = {"plan_id": ids["plan_id"], "files": [payload]}
    projected = await _project_actual_output_artifacts(task, actual, db=db_session)
    await db_session.commit()
    assert projected["files"][0]["document_id"] == source.id
    unexpected = {**actual, "files": [payload, {"fs_path": "unsubmitted.md"}]}
    rejected = await _project_actual_output_artifacts(task, unexpected, db=db_session)
    assert rejected["artifact_knowledge_issues"] == [{"reason": "artifact_evidence_mismatch"}]
    await db_session.refresh(source)
    assert (source.metadata_, source.updated_at) == before


@pytest.mark.asyncio
async def test_worker_bad_reference_can_be_corrected_before_terminal_submit(monkeypatch):
    from packages.core.services import artifact_delivery
    from packages.core.workers import internal

    calls = []

    async def verify(_db, **kwargs):
        calls.append(kwargs)
        if kwargs["refs"][0]["document_id"] == "wrong":
            raise ArtifactDeliveryError("artifact_unavailable", "Read and submit the correct Document.")
        return SimpleNamespace()

    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    handler, get_payload = internal._artifact_aware_submit_result_capture({"params": REUSE, "entity_id": "entity"})
    bad = json.loads(await handler({"result": {"document_id": "wrong"}}))
    assert bad["error"] == "artifact_unavailable" and get_payload() is None
    good = {"result": {"document_id": "correct"}}
    assert json.loads(await handler(good))["ok"] is True
    assert get_payload() == good and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "missing_receipt", "bytes", "document_revision", "requester_revoked", "busy"])
async def test_acceptance_requires_the_version_delivered_by_readonly_step(db_session, tmp_path, change):
    from packages.core.services.task_acceptance_review import TaskAcceptanceError, build_task_acceptance_snapshot

    ids = await _seed(db_session, tmp_path)
    await Dispatcher().complete_lease(db_session, ids["lease_id"], result={"document_id": ids["document_id"], "fs_path": "existing.md"})
    await db_session.commit()
    step = await db_session.get(ExecutionStep, ids["step_id"])
    task = await db_session.get(Task, ids["task_id"])
    plan = await db_session.get(ExecutionPlan, ids["plan_id"])
    if change == "missing_receipt":
        step.evidence_refs = []
    elif change == "bytes":
        (tmp_path / ids["entity_id"] / "existing.md").write_text("changed before first review", encoding="utf-8")
    elif change == "document_revision":
        document = await db_session.get(Document, ids["document_id"])
        document.name = "Changed Document revision"
    elif change == "requester_revoked":
        await db_session.execute(update(UserMembership).where(UserMembership.user_id == ids["user_id"]).values(status="inactive"))
    await db_session.commit()
    kwargs = {"task": task, "plan": plan, "steps": [step], "claim": SimpleNamespace(run_id="run", wake_generation=1, claim_token="claim")}
    if change == "busy":
        async with entity_filesystem_mutation_lock(canonical_entity_root(ids["entity_id"])):
            with pytest.raises(TaskAcceptanceError) as error:
                await build_task_acceptance_snapshot(db_session, **kwargs)
            assert error.value.code == "artifact_busy" and error.value.retryable
    elif change:
        with pytest.raises(TaskAcceptanceError):
            await build_task_acceptance_snapshot(db_session, **kwargs)
    else:
        snapshot = await build_task_acceptance_snapshot(db_session, **kwargs)
        assert json.loads(snapshot.payload_json)["artifacts"][0]["source_sha256"] == reused_artifact_snapshots(step)[0]["source_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", [False, True])
async def test_coordinator_submit_awaits_async_artifact_preflight_and_keeps_sync_compatibility(monkeypatch, readonly):
    from packages.core.constants.execution import COORDINATOR_PARAM_KEY
    from packages.core.services import artifact_delivery
    from packages.core.workers import internal
    from tests import test_robust_worker_exec_subagent as worker_tests

    worker_tests.worker_env.__wrapped__(monkeypatch)
    verified = []

    async def verify(_db, **kwargs):
        verified.append(kwargs["refs"])
        return SimpleNamespace()

    async def checklist_complete(_snapshot):
        return None

    async def fake_worker_loop(**kwargs):
        reply = await kwargs["dynamic_tool_handlers"]["submit_result"]({"summary": "Existing file", "result": {"document_id": "doc"}})
        assert not inspect.isawaitable(reply)
        assert json.loads(reply)["ok"] is True
        return SimpleNamespace(result=worker_tests._loop_result(), run=None)

    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    monkeypatch.setattr(internal, "coordinator_submission_error", checklist_complete)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    params = {"prompt": "Reuse the existing file", COORDINATOR_PARAM_KEY: True, "delegate_services": ["writer"]}
    if readonly:
        params.update(copy.deepcopy(REUSE))
    outcome = await internal._exec_subagent(worker_tests._step(params=params))
    assert "doc" in json.dumps(outcome["result"])
    assert bool(verified) is readonly


@pytest.mark.asyncio
@pytest.mark.parametrize("with_producer", [False, True])
async def test_runtime_hydration_never_mutates_verified_readonly_delivery(monkeypatch, with_producer):
    from packages.core.plans import completion_contracts

    finished_at = datetime.now(timezone.utc)
    readonly = SimpleNamespace(
        id="reuse", plan_id="plan", params=copy.deepcopy(REUSE), service_key="writer",
        step_status="done", result={"document_id": "doc", "fs_path": "a.md"},
        attempt_count=1, finished_at=finished_at,
    )
    readonly.evidence_refs = reused_artifact_evidence(
        readonly, readonly.result,
        [{"document_id": "doc", "fs_path": "a.md", "source_sha256": "a" * 64}],
        lease_id="lease", completed_at=finished_at,
    )
    before = copy.deepcopy((readonly.result, readonly.evidence_refs))
    producer = SimpleNamespace(params={}, service_key="writer", step_status="done", result={}, evidence_refs=[])
    steps = [producer, readonly] if with_producer else [readonly]
    task = SimpleNamespace(owner_service_key="writer", details={"completion_requirements": {"artifact_evidence": [{"tool": "generate_file"}]}})
    event = SimpleNamespace(event_type="tool_end", tool_name="generate_file", event_data={"status": "success", "fs_path": "a.md"})
    monkeypatch.setattr(completion_contracts, "task_runtime_events", AsyncMock(return_value=[event]))
    db = SimpleNamespace(flush=AsyncMock())

    await completion_contracts.hydrate_completion_artifacts(db, task, steps)

    assert (readonly.result, readonly.evidence_refs) == before
    assert reused_artifact_snapshots(readonly)
    if with_producer:
        assert producer.result["files"][0]["fs_path"] == "a.md"
        db.flush.assert_awaited_once()
    else:
        db.flush.assert_not_awaited()
