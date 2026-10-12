"""An inspection failure preserves observations, without accepting delivery."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from packages.core.services import task_acceptance_review as acceptance
from packages.core.services.artifact_delivery import ArtifactDeliveryError


def _rows():
    task = SimpleNamespace(id="task", entity_id="entity", workspace_id="workspace",
                           title="Save the report", description="Save and verify the report",
                           details={}, expected_output={})
    plan = SimpleNamespace(id="plan", plan_dag={})
    step = SimpleNamespace(
        id="step", step_key="save_report", kind="subagent", step_status="done", attempt_count=1,
        params={"_execution_contract": {"delivery": "artifact", "effects": "artifact_write"}},
        depends_on=[], expected_output_schema=None, result={"summary": "Saved the report"},
        evidence_refs=[{"kind": "artifact", "source": "generate_file", "document_id": "doc", "fs_path": "report.md"}],
    )
    claim = SimpleNamespace(run_id="run", wake_generation="wake", claim_token="claim")
    return task, plan, step, claim


async def _snapshot(step, *, inspection=True):
    task, plan, _, claim = _rows()
    return await acceptance.build_task_acceptance_snapshot(
        None, task=task, plan=plan, steps=[step], claim=claim, inspection=inspection,
    )


@pytest.mark.asyncio
async def test_missing_declaration_does_not_hide_successful_execution_or_historical_receipt():
    _, _, step, _ = _rows()
    original = copy.deepcopy(step.__dict__)
    snapshot = await _snapshot(step)
    manifest = acceptance.inspect_task_delivery(snapshot)
    assert manifest["status"] == "ok"
    assert manifest["verification"]["status"] == "unverified"
    assert manifest["artifact_count"] == 0
    assert manifest["catalog"][0]["status"] == "done"
    assert snapshot.payload["artifact_manifest"]["steps"][0]["refs"] == []
    page = acceptance.inspect_task_delivery(snapshot, kind="verification", index=0)
    issue = json.loads(page["content"])
    assert issue["code"] == "artifact_reference_missing"
    assert issue["failure_scope"] == "delivery_verification"
    assert issue["recorded_artifacts"][0]["document_id"] == "doc"
    assert issue["file_existence"] == "not_established_by_this_check"
    assert "verification:0:0" in acceptance.required_delivery_inspections(snapshot)
    assert step.__dict__ == original
    with pytest.raises(acceptance.TaskAcceptanceError):
        await _snapshot(step, inspection=False)


@pytest.mark.asyncio
async def test_diagnostic_snapshot_cannot_be_accepted_with_true_step_quote():
    _, _, step, _ = _rows()
    snapshot = await _snapshot(step)
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(json.dumps({
            "verdict": "accepted", "evidence": "Step reported a saved file.", "issues": [],
            "checks": [{"criterion": "Save", "pointer": "/steps/0/result/summary", "quote": "Saved the report"}],
        }), snapshot)
    assert caught.value.code == "invalid_assessment"
    assert "verification is unresolved" in caught.value.details["cause"]
    review = acceptance.parse_task_acceptance_review(json.dumps({
        "verdict": "unverifiable", "evidence": "The declaration cannot be verified.",
        "issues": ["The declared result has no final artifact identity."], "checks": [],
    }), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.UNVERIFIABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["artifact_identity_conflict", "artifact_access_denied", "artifact_missing"])
async def test_failed_file_verification_exposes_cause_but_never_file_bytes(monkeypatch, code):
    from packages.core.services import artifact_delivery, task_requester_identity

    _, _, step, _ = _rows()
    step.result = {"files": [{"document_id": "doc"}]}

    async def requester(*args):
        return "user"

    async def verify(*args, **kwargs):
        raise ArtifactDeliveryError(code, "The requested identity could not be verified.", details={"pointer": "/files/0"})

    monkeypatch.setattr(task_requester_identity, "resolve_task_execution_user_id", requester)
    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    snapshot = await _snapshot(step)
    assert snapshot.payload["artifacts"] == []
    assert snapshot.payload["verification"]["issues"][0]["code"] == code
    assert snapshot.payload["verification"]["issues"][0]["details"]["pointer"] == "/files/0"
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        await _snapshot(step, inspection=False)
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_file_lock_contention_retains_read_retry_protocol(monkeypatch):
    from packages.core.services import artifact_delivery, task_requester_identity

    _, _, step, _ = _rows()
    step.result = {"files": [{"document_id": "doc"}]}

    async def requester(*args):
        return "user"

    async def verify(*args, **kwargs):
        raise ArtifactDeliveryError("artifact_busy", "File is locked by a current writer")

    monkeypatch.setattr(task_requester_identity, "resolve_task_execution_user_id", requester)
    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        await _snapshot(step)
    assert caught.value.retryable


@pytest.mark.asyncio
async def test_multiple_diagnostics_keep_correct_step_pointer_and_paging():
    task, plan, first, claim = _rows()
    second = copy.deepcopy(first)
    second.id, second.step_key = "second", "zzz"
    snapshot = await acceptance.build_task_acceptance_snapshot(
        None, task=task, plan=plan, steps=[second, first], claim=claim, inspection=True,
    )
    issues = snapshot.payload["verification"]["issues"]
    assert [issue["result_pointer"] for issue in issues] == ["/steps/0/result", "/steps/1/result"]
    for index in range(2):
        page = acceptance.inspect_task_delivery(snapshot, kind="verification", index=index)
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        assert "citation_pointer" not in page


def test_declaration_contract_is_paged_visible_and_not_delivery_evidence():
    contract = {
        "execution": {"delivery": "artifact", "effects": "read_only"},
        "output_schema": {"description": "Task result contract. " * 600},
        "declaration": {"basis": "unique_required_schema", "pointers": ["/outputs/data/source"],
                        "requires_semantic_assessment": True},
        "submitted_refs": [{"fs_path": "source.md"}],
        "semantic_acceptance": "not_established_by_file_identity",
    }
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"key": "deliver", "result": {"text": "Source found"}, "delivery_contract": contract}],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    entry = manifest["catalog"][0]
    assert entry["declaration_basis"] == "unique_required_schema"
    assert entry["requires_semantic_assessment"] is True
    assert entry["contract_index"] == 0
    offset, pieces, markers = 0, [], []
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind="contract", index=0, offset=offset)
        assert "citation_pointer" not in page
        assert page["source_pointer"] == "/steps/0/delivery_contract"
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        pieces.append(page["content"])
        markers.append(f"contract:0:{offset}")
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(pieces)) == contract
    assert len(markers) > 1
    assert set(markers) <= set(acceptance.required_delivery_inspections(snapshot))
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(snapshot, kind="evidence", pointer="/steps/0/delivery_contract/submitted_refs/0/fs_path")


def test_schema_declared_delivery_visible_to_legacy_log_projection_without_mutation():
    from packages.core.plans.executor import _produced_artifact_labels, _has_artifact_result

    _, _, step, _ = _rows()
    step.result = {"custom_delivery": {"fs_path": "report.md"}}
    step.expected_output_schema = {"type": "object", "required": ["custom_delivery"], "properties": {
        "custom_delivery": {"type": "object", "required": ["fs_path"], "properties": {"fs_path": {"type": "string"}}},
    }}
    original = copy.deepcopy(step.result)
    assert _produced_artifact_labels([step]) == ["report.md"]
    assert _has_artifact_result([step])
    assert step.result == original
