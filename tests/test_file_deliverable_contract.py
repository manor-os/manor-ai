"""Opt-in file delivery proves identity, per-project filing and write/readback."""
import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.core.ai.runtime.tool_execution import _tool_end_event_data
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.runtime_learning import RuntimeEventLog
from packages.core.models.workspace import Workspace
from packages.core.plans import completion_contracts
from packages.core.strategist.proposal import Deliverable


def _deliverable(**overrides):
    return Deliverable(**{
        "name": "report", "kind": "file", "shape": "DocumentResult",
        "acceptance": "Saved and verified", "usage": "Next specialist",
        "document_id_path": "/report/document_id", "workspace_folder": "projects/first",
        "require_readback": True, **overrides,
    })


@pytest.mark.parametrize("folder", ["/projects/a", "../a", "projects/../a", "projects//a", "a\\b"])
def test_file_folder_must_be_workspace_relative(folder):
    with pytest.raises(ValidationError):
        _deliverable(workspace_folder=folder)


def test_folder_contract_requires_document_selector():
    with pytest.raises(ValidationError):
        _deliverable(document_id_path=None)


def test_tool_receipts_capture_canonical_identity_and_hash():
    event = _tool_end_event_data("generate_file", {
        "document": {"document_id": "doc", "fs_path": "canonical/report.json", "source_sha256": "a" * 64},
    })
    assert event["document_id"] == "doc"
    assert event["fs_path"] == "canonical/report.json"
    assert event["source_sha256"] == "a" * 64
    read = _tool_end_event_data("read_file", {
        "document_id": "doc", "resolved_path": "canonical/report.json", "source_sha256": "a" * 64,
    })
    assert read["resolved_path"] == "canonical/report.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "other_project", "other_workspace", "other_entity", "trashed",
                                  "no_write", "no_read", "read_before_write", "changed", "fake_id",
                                  "stale_attempt", "model_claim", "array_index", "negative_index",
                                  "leading_zero_index", "positive_sign_index", "whitespace_index"])
async def test_delivery_matches_canonical_document_and_current_receipts(db_session, tmp_path, monkeypatch, fault):
    from datetime import datetime, timedelta, timezone
    from packages.core.services import entity_fs

    entity_id, workspace_id, task_id = generate_ulid(), generate_ulid(), generate_ulid()
    root = DocumentFolder(id=generate_ulid(), entity_id=entity_id, name="Workspace")
    projects = DocumentFolder(id=generate_ulid(), entity_id=entity_id, name="projects", parent_id=root.id)
    project = DocumentFolder(id=generate_ulid(), entity_id=entity_id, name="first", parent_id=projects.id)
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Research", artifact_folder_id=root.id)
    path = "source/report.json"  # Logical filing does not require moving bytes.
    target = tmp_path / path
    target.parent.mkdir()
    target.write_text('{"status":"draft"}')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    doc = Document(id=generate_ulid(), entity_id=entity_id, folder_id=project.id, name="report.json", fs_path=path)
    db_session.add_all([root, projects, project, workspace, doc])
    if fault == "other_project":
        project.name = "second"
    elif fault == "other_workspace":
        workspace.id = generate_ulid()
    elif fault == "other_entity":
        doc.entity_id = generate_ulid()
    elif fault == "trashed":
        doc.is_trashed = True
    elif fault == "changed":
        target.write_text("changed after readback")
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(id=task_id, entity_id=entity_id, workspace_id=workspace_id, started_at=now,
                           expected_output={"deliverables": [_deliverable().model_dump()]})
    events = [
        ("generate_file", {"document": {"document_id": doc.id, "fs_path": path}, "source_sha256": digest}),
        ("read_file", {"document_id": doc.id, "resolved_path": path, "source_sha256": digest}),
    ]
    if fault == "no_write":
        events = events[1:]
    elif fault == "no_read":
        events = events[:1]
    elif fault == "read_before_write":
        events.reverse()
    elif fault == "model_claim":
        events[0] = ("invoke_skill", events[0][1])
    for index, (tool, payload) in enumerate(events):
        db_session.add(RuntimeEventLog(
            entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
            surface="task_worker", profile="task", principal_kind="agent", event_type="tool_end",
            tool_name=tool, sequence=index, event_data=_tool_end_event_data(tool, payload),
            created_at=now + timedelta(seconds=-1 if fault == "stale_attempt" else index),
        ))
    await db_session.flush()
    monkeypatch.setattr(entity_fs, "canonical_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: str(tmp_path))
    payload = {"report": {"document_id": "invented" if fault == "fake_id" else doc.id}}
    array_tokens = {"array_index": "0", "negative_index": "-1", "leading_zero_index": "00",
                    "positive_sign_index": "+0", "whitespace_index": " 0"}
    if fault in array_tokens:
        task.expected_output["deliverables"][0]["document_id_path"] = f"/reports/{array_tokens[fault]}/document_id"
        payload = {"reports": [{"document_id": doc.id}]}
    issue = await completion_contracts.file_deliverable_contract_issue(db_session, task, payload)
    if fault not in {None, "array_index"}:
        assert issue is not None, fault
    else:
        assert issue is None


@pytest.mark.asyncio
async def test_supervisor_rejects_schema_valid_phantom_document(db_session, monkeypatch):
    from packages.core.contracts.task_output import task_output_envelope_schema
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor
    from packages.core.constants.supervisor import SupervisorVerdict, SupervisorDecisionSource

    async def no_llm(*args, **kwargs):
        pytest.fail("Document acceptance must be deterministic before LLM review")

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", no_llm)
    entity_id = generate_ulid()
    expected = {"type": "object", "required": ["report"], "properties": {
        "report": {"type": "object", "required": ["document_id"],
                   "properties": {"document_id": {"type": "string"}}},
    }, "deliverables": [_deliverable().model_dump()]}
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=generate_ulid(),
                title="Prepare report", status="in_progress", expected_output=expected)
    plan = ExecutionPlan(id=generate_ulid(), entity_id=entity_id, task_id=task.id,
                         workspace_id=task.workspace_id, status="completed", plan_dag={})
    step = ExecutionStep(id=generate_ulid(), entity_id=entity_id, plan_id=plan.id,
                         workspace_id=task.workspace_id,
                         step_key="report", kind="subagent", step_status="done", depends_on=[],
                         expected_output_schema=task_output_envelope_schema(expected),
                         result={"status": "succeeded", "summary": "Saved", "outputs": {
                             "data": {"report": {"document_id": "invented"}},
                         }})
    db_session.add_all([task, plan, step])
    await db_session.flush()
    decision = await PlanExecutor._supervise_outcome(db_session, plan, "completed")
    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    assert decision.source is SupervisorDecisionSource.GATE
    assert "canonical Document is unavailable" in decision.evidence
