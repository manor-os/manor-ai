"""V3 content acceptance uses actual evidence and cannot become a second writer."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.plans.executor import (
    PlanExecutor,
    finalize_task_coordinator_submission,
    prepare_task_coordinator_submission,
)
from packages.core.services import task_acceptance_review as acceptance
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake, stage_task_coordinator_wake, task_coordinator_outbox_dedupe_key,
)
from test_task_coordinator_executor_handoff import _seed


def _accepted(snapshot):
    return acceptance.parse_task_acceptance_review(json.dumps({
        "verdict": "accepted", "evidence": "The actual output satisfies the supplied request.", "issues": [],
        "checks": [{"criterion": "A written result was delivered.",
                    "pointer": "/steps/0/result/text", "quote": "Done"}],
    }), snapshot)


def test_acceptance_digest_tracks_evidence_not_coordinator_claim():
    base = {
        "plan_id": "plan-1",
        "steps": [{"key": "write", "result": {"text": "Saved output"}}],
        "artifacts": [{"document_id": "doc-1", "source_sha256": "hash-1"}],
    }
    first = acceptance.acceptance_snapshot({
        **base,
        "claim": {"run_id": "run-1", "wake_generation": "wake-1", "claim_token": "token-1"},
    })
    successor = acceptance.acceptance_snapshot({
        **base,
        "claim": {"run_id": "run-1", "wake_generation": "wake-2", "claim_token": "token-2"},
    })
    changed = acceptance.acceptance_snapshot({
        **base,
        "steps": [{"key": "write", "result": {"text": "Different output"}}],
        "claim": {"run_id": "run-1", "wake_generation": "wake-2", "claim_token": "token-2"},
    })
    changed_artifact = acceptance.acceptance_snapshot({
        **base,
        "artifacts": [{"document_id": "doc-1", "source_sha256": "hash-2"}],
        "claim": {"run_id": "run-1", "wake_generation": "wake-2", "claim_token": "token-2"},
    })
    assert first.digest != successor.digest
    assert first.evidence_digest == successor.evidence_digest
    assert first.evidence_digest != changed.evidence_digest
    assert first.evidence_digest != changed_artifact.evidence_digest


def test_inspection_pages_manifest_without_hiding_later_steps_or_artifacts():
    snapshot = acceptance.acceptance_snapshot({
        "task": {"description": "Check every delivered item."},
        "plan_id": "plan-1",
        "steps": [{"key": f"step-{index}", "kind": "subagent", "status": "done",
                   "result": {"text": f"result-{index}"}} for index in range(14)],
        "artifacts": [{"document_id": "doc-1", "fs_path": "result.md",
                       "text": "verified text", "text_truncated": False}],
    })
    first = acceptance.inspect_task_delivery(snapshot)
    assert first["status"] == "ok"
    assert first["evidence_digest"] == snapshot.evidence_digest
    assert first["task"] == {"description": "Check every delivered item."}
    assert first["step_count"] == 14 and first["artifact_count"] == 1
    assert len(first["catalog"]) == 12 and first["next_offset"] == 12
    assert first["catalog"][0]["result_pointer"] == "/steps/0/result"
    assert first["catalog"][0]["citation_pointer"] == "/steps/0/result/text"
    second = acceptance.inspect_task_delivery(snapshot, kind="manifest", offset=first["next_offset"])
    assert [item["index"] for item in second["catalog"]] == [12, 13, 0]
    assert second["catalog"][-1] == {
        "kind": "artifact", "index": 0, "document_id": "doc-1",
        "fs_path": "result.md", "pointer": "/artifacts/0/text",
        "citation_pointer": "/artifacts/0/text", "value_type": "string",
        "size": None, "source_sha256": None,
        "text_chars": len("verified text"), "text_truncated": False,
        "text_available": True,
    }
    assert second["next_offset"] is None


def test_long_original_request_requires_every_bounded_request_page():
    task = {
        "id": "task-1", "title": "Detailed delivery",
        "description": "原始要求：" + ("保留完整原始需求。" * 900) + "最后一项必须交付。",
        "details": {"audience": "团队成员", "language": "中文"},
    }
    snapshot = acceptance.acceptance_snapshot({
        "task": task, "plan_id": "plan-1", "steps": [{"result": {"text": "Done"}}],
        "artifacts": [],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    assert manifest["task"]["id"] == "task-1"
    assert manifest["task"]["title"] == "Detailed delivery"
    assert manifest["task"]["request_truncated_in_manifest"] is True
    assert "最后一项必须交付" not in json.dumps(manifest, ensure_ascii=False)
    assert len(json.dumps(manifest, ensure_ascii=False)) < 3_900

    offsets = []
    pages = []
    offset = 0
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind="request", offset=offset)
        assert page["source_pointer"] == "/task"
        assert page["evidence_digest"] == manifest["evidence_digest"]
        assert len(page["content"]) <= acceptance.INSPECTION_CHUNK_CHARS
        offsets.append(offset)
        pages.append(page["content"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert len(offsets) > 1
    assert json.loads("".join(pages)) == task
    required = acceptance.required_delivery_inspections(snapshot)
    assert {item for item in required if item.startswith("request:")} == {
        f"request:{offset}" for offset in offsets
    }
    assert "manifest:0" in required and "step:0:0" in required


@pytest.mark.parametrize("kind", ["request", "step", "artifact"])
def test_highly_escaped_content_pages_fit_runtime_budget_and_keep_all_text(kind):
    hostile_text = ('"\\\n\t\r' * 1_500) + "最后一项必须保留。"
    task = {"description": hostile_text if kind == "request" else "Check the result."}
    step_result = {"text": hostile_text if kind == "step" else "Done"}
    artifact_text = hostile_text if kind == "artifact" else "short"
    snapshot = acceptance.acceptance_snapshot({
        "task": task, "steps": [{"result": step_result}],
        "artifacts": [{"document_id": "doc-1", "text": artifact_text}],
    })
    expected = (
        json.dumps(task, sort_keys=True, ensure_ascii=False) if kind == "request"
        else json.dumps(step_result, sort_keys=True, ensure_ascii=False) if kind == "step"
        else artifact_text
    )
    offset = 0
    offsets = []
    content_parts = []
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind=kind, offset=offset)
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        assert page["offset"] == offset and page["returned_chars"] == len(page["content"])
        offsets.append(offset)
        content_parts.append(page["content"])
        next_offset = page["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    assert len(offsets) > 1
    assert "".join(content_parts) == expected
    required = acceptance.required_delivery_inspections(snapshot)
    prefix = "request:" if kind == "request" else f"{kind}:0:"
    assert {item for item in required if item.startswith(prefix)} == {
        f"{prefix}{item}" for item in offsets
    }


def test_twelve_long_path_catalog_entries_fit_dynamic_manifest_pages():
    artifacts = [
        {"document_id": f"document-{index:02d}-" + "d" * 40,
         "fs_path": f"workspace/archive/{index:02d}/" + "p" * 400,
         "text": f"content-{index}"}
        for index in range(12)
    ]
    snapshot = acceptance.acceptance_snapshot({
        "task": {"title": "Review all artifacts"},
        "steps": [], "artifacts": artifacts,
    })
    offset = 0
    offsets = []
    catalog = []
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind="manifest", offset=offset)
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        offsets.append(offset)
        catalog.extend(page["catalog"])
        next_offset = page["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    assert len(offsets) > 1
    assert [entry["document_id"] for entry in catalog] == [
        artifact["document_id"] for artifact in artifacts
    ]
    required = acceptance.required_delivery_inspections(snapshot)
    assert {item for item in required if item.startswith("manifest:")} == {
        f"manifest:{item}" for item in offsets
    }
    assert {item for item in required if item.startswith("artifact:")} == {
        f"artifact:{index}:0" for index in range(12)
    }


@pytest.mark.parametrize("kind,value,pointer", [
    ("step", {"summary": "已确认" * 2500}, "/steps/0/result"),
    ("artifact", "正文内容" * 2500, "/artifacts/0/text"),
])
def test_inspection_pages_exact_content_without_mutating_snapshot(kind, value, pointer):
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": value}],
        "artifacts": [{"text": value if kind == "artifact" else "short", "text_truncated": False}],
    })
    before = snapshot.payload_json
    pages = []
    offset = 0
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind=kind, offset=offset)
        assert page["status"] == "ok"
        assert page["result_pointer" if kind == "step" else "pointer"] == pointer
        assert page["offset"] == offset
        assert page["evidence_digest"] == snapshot.evidence_digest
        assert len(page["content"]) <= acceptance.INSPECTION_CHUNK_CHARS
        pages.append(page["content"])
        offset = page["next_offset"]
        if offset is None:
            break
    content = "".join(pages)
    assert content == (json.dumps(value, sort_keys=True, ensure_ascii=False) if kind == "step" else value)
    assert snapshot.payload_json == before


def test_inspection_marks_unavailable_artifact_text_without_claiming_content():
    snapshot = acceptance.acceptance_snapshot({
        "steps": [], "artifacts": [{"document_id": "doc-1", "text_truncated": True}],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    assert manifest["catalog"][0]["text_available"] is False
    assert manifest["catalog"][0]["text_truncated"] is True
    page = acceptance.inspect_task_delivery(snapshot, kind="artifact", index=0)
    assert page["content"] == "" and page["total_chars"] == 0
    assert page["text_truncated"] is True


@pytest.mark.parametrize("arguments", [
    {"kind": "unknown"}, {"kind": "manifest", "offset": -1},
    {"kind": "manifest", "offset": 2}, {"kind": "step", "index": 1},
    {"kind": "artifact", "index": 0}, {"kind": "step", "offset": 100},
    {"kind": "step", "index": True}, {"kind": "step", "offset": 1.5},
])
def test_inspection_rejects_invalid_or_out_of_range_pages(arguments):
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": "ok"}], "artifacts": []})
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(snapshot, **arguments)


def test_twelve_full_length_artifacts_fit_paged_acceptance_snapshot():
    artifacts = [
        {"document_id": f"document-{index}", "text": (str(index % 10) * 24_000)}
        for index in range(12)
    ]
    snapshot = acceptance.acceptance_snapshot({"task": {"title": "Review all documents"},
                                               "steps": [], "artifacts": artifacts})
    assert 96_000 < len(snapshot.payload_json) <= acceptance.MAX_REVIEW_CHARS
    assert len(json.loads(snapshot.payload_json)["artifacts"]) == 12
    final_page = acceptance.inspect_task_delivery(snapshot, kind="artifact", index=11, offset=22_400)
    assert final_page["content"] == "1" * final_page["returned_chars"]
    assert final_page["next_offset"] is None


@pytest_asyncio.fixture
async def committed_tasks(db_session):
    """Committed race fixtures must not leak into another test's sweeps."""
    identities = []
    yield identities
    await db_session.rollback()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with sessions() as db:
        for task_id, plan_id in identities:
            task = await db.get(Task, task_id)
            workspace_id = task.workspace_id if task else None
            subscriptions = list((await db.execute(select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
            ))).scalars()) if workspace_id else []
            await db.execute(delete(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.dedupe_key == task_coordinator_outbox_dedupe_key(task_id),
            ))
            await db.execute(delete(ExecutionStep).where(ExecutionStep.plan_id == plan_id))
            await db.execute(delete(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id))
            await db.execute(delete(ExecutionPlan).where(ExecutionPlan.id == plan_id))
            await db.execute(delete(Task).where(Task.id == task_id))
            for subscription in subscriptions:
                await db.delete(subscription)
                await db.execute(delete(Agent).where(Agent.id == subscription.agent_id))
            if workspace_id:
                await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await db.commit()


@pytest.mark.parametrize("response", [
    "accepted", "{}", '{"verdict":"accepted","evidence":"","issues":[]}',
    '{"verdict":"accepted","evidence":"ok","issues":["wrong link"]}',
    '{"verdict":"changes_required","evidence":"wrong link","issues":[]}',
    '{"verdict":"accepted","evidence":"ok","issues":[],"snapshot_digest":"forged"}',
    '{"verdict":"completed","evidence":"ok","issues":[]}',
    '{"verdict":"changes_required","verdict":"accepted","evidence":"ok","issues":[]}',
])
def test_malformed_review_never_falls_back_to_completion(response):
    with pytest.raises(acceptance.TaskAcceptanceError):
        acceptance.parse_task_acceptance_review(response, acceptance.acceptance_snapshot({"task": "one"}))


def test_deeply_nested_assessment_is_a_bounded_input_error():
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review('[' * 2000 + ']' * 2000, _grounding_snapshot())
    assert caught.value.code == "invalid_assessment"


@pytest.mark.parametrize("verdict", ["changes_required", "unverifiable"])
def test_review_factory_owns_digest_and_preserves_concrete_rejection(verdict):
    snapshot = acceptance.acceptance_snapshot({"text": "unsupported no added sugar claim"})
    review = acceptance.parse_task_acceptance_review(json.dumps({
        "verdict": verdict, "evidence": "The source only says lightly sweet.",
        "issues": ["Remove the unsupported no added sugar claim."],
    }), snapshot)
    assert review.snapshot_digest == snapshot.digest
    assert review.verdict is acceptance.TaskAcceptanceVerdict(verdict)
    assert review.checks == ()


def _grounding_snapshot():
    return acceptance.acceptance_snapshot({
        "task": {"description": "Return the existing document and a Chinese summary."},
        "steps": [{
            "params": {"prompt": "A Chinese summary is required."},
            "result": {
                "summary": "已确认四项道具，无需修改原文档。",
                "escaped/key~name": "Literal escaped-key evidence.",
                "~1": "Single-pass pointer decoding.",
                "files": [{"document_id": "saved-document"}],
            },
        }],
        "artifacts": [{"text": "Required props: camera, light, mug, notebook."}],
    })


def _grounded_response(checks):
    return json.dumps({
        "verdict": "accepted", "evidence": "Each explicit requirement has supplied evidence.",
        "issues": [], "checks": checks,
    })


def test_step_text_inspection_advertises_a_citable_leaf_without_accepting_partial_object():
    snapshot = acceptance.acceptance_snapshot({
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [{
            "key": "production_plans", "evidence_step_keys": ["delivery"],
        }]}}},
        "steps": [{"key": "delivery", "result": {
            "text": "Three production plans, each with shots, assets, and editing steps.",
        }}],
        "artifacts": [],
    })
    manifest_entry = acceptance.inspect_task_delivery(snapshot)["catalog"][0]
    step_page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert manifest_entry["result_pointer"] == step_page["result_pointer"] == "/steps/0/result"
    assert manifest_entry["citation_pointer"] == step_page["citation_pointer"] == "/steps/0/result/text"
    assert json.loads(step_page["content"]) == snapshot.payload["steps"][0]["result"]

    check = {"criterion": "criterion:0:production_plans",
             "pointer": step_page["citation_pointer"], "quote": "shots, assets, and editing steps"}
    review = acceptance.parse_task_acceptance_review(_grounded_response([check]), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED

    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([{
            **check, "pointer": step_page["result_pointer"],
        }]), snapshot)
    assert caught.value.code == "invalid_assessment"
    assert "/steps/0/result/text" in str(caught.value.__cause__)


def _plan_criteria_snapshot():
    return acceptance.acceptance_snapshot({
        "task": {"description": "Deliver reference analysis and a new script."},
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [
            {"key": "reference_analysis", "deliverable_name": "Reference analysis",
             "description": "Analyse the reel before adapting it.",
             "evidence_step_keys": ["delivery"]},
            {"key": "new_script", "deliverable_name": "New script",
             "description": "Write a new script for the user.",
             "evidence_step_keys": ["delivery"]},
        ]}}},
        "steps": [{"key": "delivery", "result": {
            "analysis": "The source reel opens with a question.",
            "script": "Ask one question, then explain the answer.",
        }}],
        "artifacts": [],
    })


def _plan_criteria_checks():
    return [
        {"criterion": "criterion:0:reference_analysis", "pointer": "/steps/0/result/analysis",
         "quote": "opens with a question"},
        {"criterion": "criterion:1:new_script", "pointer": "/steps/0/result/script",
         "quote": "Ask one question"},
    ]


@pytest.mark.parametrize("missing_id", ["criterion:0:reference_analysis", "criterion:1:new_script"])
def test_plan_acceptance_requires_a_grounded_check_for_every_criterion_id(missing_id):
    snapshot = _plan_criteria_snapshot()
    manifest = acceptance.inspect_task_delivery(snapshot)
    assert manifest["criterion_count"] == 2
    assert {item["check_id"] for item in manifest["catalog"] if item["kind"] == "criterion"} == {
        "criterion:0:reference_analysis", "criterion:1:new_script",
    }
    checks = [check for check in _plan_criteria_checks() if check["criterion"] != missing_id]
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response(checks), snapshot)
    assert caught.value.code == "invalid_assessment"
    assert missing_id in str(caught.value.__cause__)


def test_plan_acceptance_accepts_each_criterion_with_real_pointer_and_quote():
    snapshot = _plan_criteria_snapshot()
    review = acceptance.parse_task_acceptance_review(
        _grounded_response(_plan_criteria_checks()), snapshot,
    )
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED
    assert {check.criterion for check in review.checks} == {
        "criterion:0:reference_analysis", "criterion:1:new_script",
    }
    assert review.snapshot_digest == snapshot.digest


def test_duplicate_plan_criterion_keys_require_distinct_grounded_check_ids():
    snapshot = acceptance.acceptance_snapshot({
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [
            {"key": "quality", "description": "Analyse the source.",
             "evidence_step_keys": ["delivery"]},
            {"key": "quality", "description": "Adapt the script.",
             "evidence_step_keys": ["delivery"]},
        ]}}},
        "steps": [{"key": "delivery", "result": {
            "analysis": "Source facts checked.", "script": "Fresh user-specific script.",
        }}],
        "artifacts": [],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    ids = [item["check_id"] for item in manifest["catalog"] if item["kind"] == "criterion"]
    assert ids == ["criterion:0:quality", "criterion:1:quality"]
    checks = [
        {"criterion": ids[0], "pointer": "/steps/0/result/analysis", "quote": "Source facts"},
        {"criterion": ids[1], "pointer": "/steps/0/result/script", "quote": "user-specific script"},
    ]
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response(checks[:1]), snapshot)
    assert caught.value.code == "invalid_assessment"
    assert ids[1] in str(caught.value.__cause__)
    review = acceptance.parse_task_acceptance_review(_grounded_response(checks), snapshot)
    assert {check.criterion for check in review.checks} == set(ids)


def test_long_plan_criterion_description_requires_complete_paged_inspection():
    criterion = {
        "key": "quality", "deliverable_name": "Source analysis",
        "description": ("Check exact source wording, including a quoted phrase. " * 150)
        + "The final condition must also be checked.",
    }
    snapshot = acceptance.acceptance_snapshot({
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [criterion]}}},
        "steps": [{"result": {"text": "Source wording and final condition checked."}}],
        "artifacts": [],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    entry = next(item for item in manifest["catalog"] if item["kind"] == "criterion")
    assert entry["check_id"] == "criterion:0:quality"
    assert entry["description_truncated"] is True
    assert "The final condition" not in entry["description"]

    offsets = []
    content_parts = []
    offset = 0
    while True:
        page = acceptance.inspect_task_delivery(snapshot, kind="criterion", index=0, offset=offset)
        assert page["source_pointer"] == "/plan_dag/metadata/acceptance_contract/criteria/0"
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        offsets.append(offset)
        content_parts.append(page["content"])
        next_offset = page["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    assert len(offsets) > 1
    assert json.loads("".join(content_parts)) == criterion
    required = acceptance.required_delivery_inspections(snapshot)
    assert {item for item in required if item.startswith("criterion:")} == {
        f"criterion:0:{item}" for item in offsets
    }


def test_plan_criterion_cannot_cite_an_unrelated_search_step():
    snapshot = acceptance.acceptance_snapshot({
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [
            {"key": "script", "description": "Deliver a user-specific script.",
             "evidence_step_keys": ["delivery"]},
        ]}}},
        "steps": [
            {"key": "search", "result": {"text": "A user-specific script was requested."}},
            {"key": "delivery", "result": {"text": "A new user-specific script was delivered."}},
        ],
        "artifacts": [],
    })
    manifest = acceptance.inspect_task_delivery(snapshot)
    criterion = next(item for item in manifest["catalog"] if item["kind"] == "criterion")
    assert criterion["evidence_step_keys"] == ["delivery"]
    check_id = criterion["check_id"]
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([{
            "criterion": check_id, "pointer": "/steps/0/result/text", "quote": "user-specific script",
        }]), snapshot)
    assert caught.value.code == "invalid_assessment"
    review = acceptance.parse_task_acceptance_review(_grounded_response([{
        "criterion": check_id, "pointer": "/steps/1/result/text", "quote": "script was delivered",
    }]), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED


def test_plan_criterion_artifact_citation_requires_delivery_step_to_submit_that_document():
    base = {
        "task": {"entity_id": "entity-1"},
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [
            {"key": "document", "description": "Deliver the final document.",
             "evidence_step_keys": ["delivery"]},
        ]}}},
        "artifacts": [{"document_id": "final-doc", "fs_path": "final.md",
                       "text": "The complete final document."}],
    }
    check = {"criterion": "criterion:0:document", "pointer": "/artifacts/0/text",
             "quote": "complete final document"}
    unrelated = acceptance.acceptance_snapshot({
        **base,
        "steps": [
            {"key": "search", "result": {"files": [{"document_id": "final-doc"}]}},
            {"key": "delivery", "result": {"files": [{"document_id": "other-doc"}]}},
        ],
    })
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([check]), unrelated)
    assert caught.value.code == "invalid_assessment"

    delivered = acceptance.acceptance_snapshot({
        **base,
        "steps": [
            {"key": "search", "result": {"files": [{"document_id": "other-doc"}]}},
            {"key": "delivery", "result": {"files": [{"document_id": "final-doc"}]}},
        ],
    })
    review = acceptance.parse_task_acceptance_review(_grounded_response([check]), delivered)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED


@pytest.mark.parametrize("pointer,quote", [
    ("/steps/0/result/missing_summary", "A hidden summary"),
    ("/steps/0/params/prompt", "Chinese summary"),
    ("/task/description", "Chinese summary"),
    ("/sse/summary", "A hidden summary"),
    ("/steps/0/result", "saved-document"),
    ("/steps/0/result/files", "saved-document"),
    ("/artifacts/0/text/0", "R"),
    ("/steps/1/result/summary", "已确认"),
    ("/steps/-1/result/summary", "已确认"),
    ("/steps/00/result/summary", "已确认"),
    ("/steps/+0/result/summary", "已确认"),
    ("/steps/0.0/result/summary", "已确认"),
    ("steps/0/result/summary", "已确认"),
    ("/steps/0/result/escaped~2key~0name", "Literal"),
    ("/steps/0/result/escaped/key~0name", "Literal"),
    ("/steps/0/result/summary", "Not actually in the supplied result"),
    ("/steps/0/result/summary", ""),
    ("/steps/0/result/summary", "   "),
    ("/steps/0/result/summary", 123),
])
def test_accepted_review_rejects_missing_or_invented_evidence(pointer, quote):
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([{
            "criterion": "Return a Chinese summary.", "pointer": pointer, "quote": quote,
        }]), _grounding_snapshot())
    assert caught.value.code == "invalid_assessment"


@pytest.mark.parametrize("checks", [
    None, [], {}, "verified", [None],
    [{"criterion": "Summary", "pointer": "/steps/0/result/summary"}],
    [{"criterion": "Summary", "pointer": "/steps/0/result/summary", "quote": "已确认", "hidden": True}],
    [{"criterion": " ", "pointer": "/steps/0/result/summary", "quote": "已确认"}],
    [{"criterion": "Summary", "pointer": "/steps/0/result/summary", "quote": "已确认"}]
    * (acceptance.MAX_ACCEPTANCE_CHECKS + 1),
])
def test_accepted_review_requires_bounded_exact_grounding_checks(checks):
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response(checks), _grounding_snapshot())
    assert caught.value.code == "invalid_assessment"


def test_accepted_review_without_checks_cannot_guess_summary_in_hidden_stream():
    snapshot = acceptance.acceptance_snapshot({
        "task": {"description": "Return a document and a Chinese summary."},
        "steps": [{"result": {"files": [{"document_id": "saved-document"}]}}],
    })
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(json.dumps({
            "verdict": "accepted", "issues": [],
            "evidence": "The summary is likely in a Step payload or SSE stream; the core deliverable is present.",
        }), snapshot)
    assert caught.value.code == "invalid_assessment"


def test_accepted_review_keeps_multiple_grounded_criteria_and_exact_pointer_escapes():
    checks = [
        {"criterion": "A Chinese summary is present.", "pointer": "/steps/0/result/summary", "quote": "已确认四项道具"},
        {"criterion": "The document names the required props.", "pointer": "/artifacts/0/text", "quote": "camera, light, mug, notebook"},
        {"criterion": "The escaped key has an exact result.", "pointer": "/steps/0/result/escaped~1key~0name", "quote": "Literal escaped-key evidence."},
        {"criterion": "Pointer escapes are decoded once.", "pointer": "/steps/0/result/~01", "quote": "Single-pass pointer decoding."},
    ]
    snapshot = _grounding_snapshot()
    review = acceptance.parse_task_acceptance_review(_grounded_response(checks), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED
    assert review.snapshot_digest == snapshot.digest
    assert len(review.checks) == 4


def test_grounding_accepts_root_scalar_result_but_not_overlong_quote():
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": "已核对原文档，无需写入。"}]})
    review = acceptance.parse_task_acceptance_review(_grounded_response([{
        "criterion": "A read-only confirmation is returned.", "pointer": "/steps/0/result", "quote": "无需写入",
    }]), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED
    assert len(review.checks) == 1

    quote = "x" * 1001
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": quote}]})
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([{
            "criterion": "Bounded evidence", "pointer": "/steps/0/result", "quote": quote,
        }]), snapshot)
    assert caught.value.code == "invalid_assessment"


@pytest.mark.parametrize("value,quote", [
    ({"document_id": "doc", "count": 2}, '{"count": 2, "document_id": "doc"}'),
    ([{"document_id": "doc"}], '[ { "document_id": "doc" } ]'),
    ({"text": "道具", "nested": [False, None, 0]}, '{"nested":[false,null,0],"text":"道具"}'),
    ({}, '{}'), ([], '[]'),
])
def test_grounding_accepts_complete_exact_container_json(value, quote):
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": value}]})
    review = acceptance.parse_task_acceptance_review(_grounded_response([{
        "criterion": "The complete structured result was delivered.",
        "pointer": "/steps/0/result", "quote": quote,
    }]), snapshot)
    assert review.checks[0].quote == quote


@pytest.mark.parametrize("value,quote", [
    ({"document_id": "doc", "count": 2}, '{"document_id":"doc"}'),
    ({"document_id": "doc"}, '{"document_id":"doc","extra":true}'),
    ({"document_id": "doc"}, '{"document_id":"other"}'),
    ({"document_id": "doc"}, '{"document_id":"other","document_id":"doc"}'),
    ({"nested": {"x": 1}}, '{"nested":{"x":0,"x":1}}'),
    ({"count": 1}, '{"count":true}'),
    ({"count": 1}, '{"count":1.0}'),
    ({"count": True}, '{"count":1}'),
    ([1, 2], '[2,1]'), ([1, 2], '[1]'),
    ([1, 2], '{"0":1,"1":2}'),
    ({"document_id": "doc"}, '"{\\"document_id\\":\\"doc\\"}"'),
    ({"document_id": "doc"}, '{\\"document_id\\":\\"doc\\"}'),
    ({"count": 1}, '{"count":NaN}'),
    ({"count": 1}, '{"count":Infinity}'),
    ({"count": 1}, '{"count":1e999}'),
])
def test_grounding_rejects_partial_coerced_or_forged_container_json(value, quote):
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": value}]})
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([{
            "criterion": "The complete structured result was delivered.",
            "pointer": "/steps/0/result", "quote": quote,
        }]), snapshot)
    assert caught.value.code == "invalid_assessment"


@pytest.mark.parametrize("value", [4, 4.5, True, False, None])
def test_grounding_uses_exact_json_spelling_for_nontext_scalar(value):
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": {"actual": value}}]})
    check = {"criterion": "The reported value is present.", "pointer": "/steps/0/result/actual",
             "quote": json.dumps(value)}
    review = acceptance.parse_task_acceptance_review(_grounded_response([check]), snapshot)
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED
    check["quote"] += " not actually present"
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(_grounded_response([check]), snapshot)
    assert caught.value.code == "invalid_assessment"


def test_snapshot_does_not_silently_truncate_original_requirements():
    with pytest.raises(acceptance.TaskAcceptanceError, match="bounded review"):
        acceptance.acceptance_snapshot({"task": "x" * acceptance.MAX_REVIEW_CHARS})


@pytest.mark.asyncio
async def test_missing_artifact_reference_is_a_recoverable_acceptance_error():
    step = SimpleNamespace(
        id="step", step_key="deliver", kind="llm", step_status="done",
        attempt_count=1, params={}, depends_on=[], result={"text": "Done"},
        evidence_refs=[{"kind": "artifact"}], expected_output_schema=None,
    )
    with pytest.raises(acceptance.TaskAcceptanceError, match="document_id or exact fs_path"):
        await acceptance.build_task_acceptance_snapshot(
            None, task=None, plan=None, steps=[step], claim=None,
        )


@pytest.fixture
def offline_artifact_snapshot(monkeypatch):
    """Exercise review budgets only; no real ACL, DB, filesystem or provider."""
    from packages.core.services import artifact_delivery, task_requester_identity

    calls = []

    async def requester(_db, _task):
        return "requester"

    async def verify(_db, **kwargs):
        calls.append(kwargs["refs"])
        # Model only the verifier's canonical-Document deduplication contract.
        document_ids = dict.fromkeys(ref["document_id"] for ref in kwargs["refs"])
        return SimpleNamespace(snapshots=[{
            "document_id": document_id, "fs_path": f"{document_id}.md", "text": "Verified document text.",
        } for document_id in document_ids])

    monkeypatch.setattr(task_requester_identity, "resolve_task_execution_user_id", requester)
    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)

    async def build(groups):
        steps = [SimpleNamespace(
            id=f"step-{index}", step_key=f"deliver-{index}", kind="llm", step_status="done",
            attempt_count=1, params={}, depends_on=[], result={"files": refs},
            evidence_refs=[{"kind": "artifact"}], expected_output_schema=None,
        ) for index, refs in enumerate(groups)]
        return await acceptance.build_task_acceptance_snapshot(
            None,
            task=SimpleNamespace(id="task", entity_id="entity", workspace_id="workspace", title="Delivery",
                                 description="Return the verified documents.", details=None, expected_output=None),
            plan=SimpleNamespace(id="plan", plan_dag={}), steps=steps,
            claim=SimpleNamespace(run_id="run", wake_generation=1, claim_token="claim"),
        )

    return build, calls


@pytest.mark.asyncio
async def test_review_artifact_budget_counts_canonical_documents_not_aliases(offline_artifact_snapshot):
    build, calls = offline_artifact_snapshot
    refs = [ref for index in range(5) for ref in (
        {"document_id": f"document-{index}"},
        {"document_id": f"document-{index}", "fs_path": f"document-{index}.md"},
        {"document_id": f"document-{index}", "open_url": f"/viewer/document-{index}"},
    )]
    snapshot = await build([refs])
    assert len(calls) == 1 and len(calls[0]) == 15
    assert len(json.loads(snapshot.payload_json)["artifacts"]) == 5


@pytest.mark.asyncio
async def test_paged_review_artifact_budget_allows_thirteen_verified_documents(offline_artifact_snapshot):
    build, calls = offline_artifact_snapshot
    snapshot = await build([[{"document_id": f"document-{index}"} for index in range(13)]])
    assert len(calls) == 1 and len(calls[0]) == 13
    assert len(json.loads(snapshot.payload_json)["artifacts"]) == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("across_steps", [False, True])
async def test_review_raw_reference_budget_rejects_before_verification(offline_artifact_snapshot, across_steps):
    build, calls = offline_artifact_snapshot
    refs = [{"document_id": "one-document"}] * 33
    groups = [[ref] for ref in refs] if across_steps else [refs]
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        await build(groups)
    assert caught.value.code == "artifact_reference_limit"
    assert calls == []


async def _prepared(db):
    seeded = await _seed(db, step_status="done", run_phase=TaskCoordinatorPhase.RUNNING.value)
    wake = await stage_task_coordinator_wake(db, task_id=seeded.task.id, reason="acceptance_test")
    claim, reason = await claim_task_coordinator_wake(
        db, task_id=seeded.task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="acceptance-owner",
        selection=seeded.selection, lease_seconds=120,
    )
    assert reason == "claimed"
    snapshot, gate = await prepare_task_coordinator_submission(
        db, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
    )
    assert snapshot is not None and gate is None
    return seeded, claim, snapshot


@pytest.mark.asyncio
async def test_db_all_done_is_not_content_acceptance(db_session):
    seeded, claim, _ = await _prepared(db_session)
    with pytest.raises(acceptance.TaskAcceptanceError, match="acceptance") as caught:
        await finalize_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
        )
    assert caught.value.code == "invalid_assessment"
    assert seeded.task.status == "in_progress"


@pytest.mark.asyncio
async def test_db_acceptance_without_grounding_cannot_finalize(db_session):
    seeded, claim, snapshot = await _prepared(db_session)
    review = replace(_accepted(snapshot), checks=())
    with pytest.raises(acceptance.TaskAcceptanceError, match="acceptance") as caught:
        await finalize_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
            acceptance_review=review,
        )
    assert caught.value.code == "invalid_assessment"
    assert seeded.task.status == "in_progress"


@pytest.mark.asyncio
async def test_busy_artifact_retries_submission_without_replanning(db_session, monkeypatch):
    seeded, claim, snapshot = await _prepared(db_session)

    async def busy(*args, **kwargs):
        raise acceptance.TaskAcceptanceError("A writer owns the file lock.", code="artifact_busy")

    monkeypatch.setattr(acceptance, "build_task_acceptance_snapshot", busy)
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        await finalize_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id,
            claim=claim, acceptance_review=_accepted(snapshot),
        )
    assert caught.value.code == "artifact_busy" and caught.value.retryable
    assert seeded.task.status == "in_progress"
    assert seeded.plan.status == "running" and seeded.step.step_status == "done"
    assert not acceptance.TaskAcceptanceError("Missing file").retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["result", "request"])
async def test_db_review_cannot_accept_changed_output_or_requirements(db_session, changed):
    seeded, claim, snapshot = await _prepared(db_session)
    if changed == "result":
        seeded.step.result = {"text": "Different output after review"}
    else:
        seeded.task.description = "A new explicit requirement"
    await db_session.flush()
    with pytest.raises(acceptance.TaskAcceptanceError, match="stale") as caught:
        await finalize_task_coordinator_submission(
            db_session, task_id=seeded.task.id, plan_id=seeded.plan.id, claim=claim,
            acceptance_review=_accepted(snapshot),
        )
    assert caught.value.code == "invalid_assessment"
    assert seeded.task.status == "in_progress" and seeded.step.step_status == "done"


@pytest.mark.asyncio
async def test_db_review_provider_wait_does_not_hold_task_lock(db_session, committed_tasks):
    seeded, claim, snapshot = await _prepared(db_session)
    task_id, plan_id = seeded.task.id, seeded.plan.id
    committed_tasks.append((task_id, plan_id))
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    await db_session.commit()
    # The coordinator releases its prepare transaction before provider work.
    async with sessions() as other:
        task = (await asyncio.wait_for(other.execute(
            select(Task).where(Task.id == task_id).with_for_update(),
        ), timeout=2)).scalar_one()
        task.status = "cancelled"
        await other.commit()
    async with sessions() as final:
        with pytest.raises(ValueError, match="lost its current claim"):
            await finalize_task_coordinator_submission(
                final, task_id=task_id, plan_id=plan_id, claim=claim,
                acceptance_review=_accepted(snapshot),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [2, 8, 32])
async def test_db_concurrent_submission_has_one_terminal_writer(db_session, monkeypatch, concurrency, committed_tasks):
    seeded, claim, snapshot = await _prepared(db_session)
    task_id, plan_id = seeded.task.id, seeded.plan.id
    committed_tasks.append((task_id, plan_id))
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    await db_session.commit()
    writes = []

    async def finalize(db, plan, status, *, server_decision):
        assert server_decision.verdict.value == "completed"
        writes.append(plan.id)
        plan.status = status
        task = await db.get(Task, task_id)
        task.status = status
        return {"event_type": "task.succeeded"}

    monkeypatch.setattr(PlanExecutor, "_finalize", staticmethod(finalize))
    ready = asyncio.Event()

    async def contender():
        await ready.wait()
        async with sessions() as db:
            try:
                accepted, _, _ = await finalize_task_coordinator_submission(
                    db, task_id=task_id, plan_id=plan_id, claim=claim,
                    acceptance_review=_accepted(snapshot),
                )
                await db.commit()
                return accepted
            except ValueError:
                await db.rollback()
                return False

    contenders = [asyncio.create_task(contender()) for _ in range(concurrency)]
    ready.set()
    results = await asyncio.wait_for(asyncio.gather(*contenders), timeout=30)
    assert sum(results) == 1
    assert writes == [plan_id]
    async with sessions() as verification:
        plan = await verification.get(ExecutionPlan, plan_id)
        assert plan.dispatcher_state["task_content_acceptance"]["checks"] == [{
            "criterion": "A written result was delivered.",
            "pointer": "/steps/0/result/text", "quote": "Done",
        }]
