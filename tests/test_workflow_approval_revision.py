"""Approval revise loops and approvals delegated from a parent workflow."""

from __future__ import annotations

import pytest

from packages.core.ai.workflow_runner import (
    apply_workflow_approval_revision,
    delegated_parent_workflow_approval,
)
from packages.core.models.workflow import WorkflowRun

STEPS = [
    {"id": "start", "type": "trigger", "next": ["draft"]},
    {"id": "draft", "type": "agent_call", "next": ["review"]},
    {
        "id": "review",
        "type": "wait",
        "next": ["gate"],
        "config": {
            "wait_type": "approval",
            "response_variable": "review_decision",
            "revise_from": "draft",
            "revision_note_variable": "review_notes",
            "revision_history_variable": "review_note_history",
            "max_revisions": 2,
        },
    },
    {"id": "gate", "type": "condition", "true_next": ["done"], "false_next": ["stopped"]},
    {"id": "done", "type": "end"},
    {"id": "stopped", "type": "end"},
    {"id": "side", "type": "agent_call", "depends_on": ["start"]},
]


def _run(choice: str, note: str | None = None, **extra) -> WorkflowRun:
    completed = {"status": "completed"}
    return WorkflowRun(
        id="run-1",
        workflow_id="wf-1",
        entity_id="entity-1",
        started_by="user-1",
        status="running",
        variables={"review_decision": {"choice": choice, **({"note": note} if note else {})}},
        step_results={
            "start": completed,
            "draft": completed,
            "side": completed,
            "review": {**completed, "decision": choice, "approved_by": "user-1"},
        },
        execution_trace=[],
        **extra,
    )


def test_revise_clears_only_the_reviewed_route_and_keeps_the_note() -> None:
    run = _run("revise", "Make it shorter")

    assert apply_workflow_approval_revision(STEPS, run) == "review"

    assert set(run.step_results) == {"start", "side"}
    assert run.current_step_id == "draft"
    assert run.variables["review_notes"] == "Make it shorter"
    assert run.variables["review_note_history"] == ["Make it shorter"]
    assert "review_decision" not in run.variables
    assert run.variables["__revisions"] == [{
        "step_id": "review",
        "revise_from": "draft",
        "round": 1,
        "note": "Make it shorter",
        "decided_by": "user-1",
        "decided_at": None,
    }]
    assert run.execution_trace[-1]["status"] == "revision_requested"


def test_approve_and_cancel_do_not_rewind() -> None:
    for choice in ("approve", "cancel"):
        run = _run(choice)
        assert apply_workflow_approval_revision(STEPS, run) is None
        assert "review" in run.step_results


def test_revise_is_bounded_by_max_revisions() -> None:
    run = _run("revise", "Again")
    run.variables["__revisions"] = [{"step_id": "review"}, {"step_id": "review"}]

    assert apply_workflow_approval_revision(STEPS, run) is None
    assert "review" in run.step_results


def test_revise_moves_a_cleared_retry_checkpoint_to_the_revised_route() -> None:
    run = _run("revise", "Brighter", retry_from_step_id="draft")

    apply_workflow_approval_revision(STEPS, run)

    assert run.retry_from_step_id == "draft"
    upstream = _run("revise", "Brighter", retry_from_step_id="start")
    apply_workflow_approval_revision(STEPS, upstream)
    assert upstream.retry_from_step_id == "start"


class _Db:
    def __init__(self, runs: dict[str, WorkflowRun]):
        self.runs = runs

    async def get(self, _model, run_id):
        return self.runs.get(run_id)


def _parent(approved_by: str | None = "user-1", actions: list[str] | None = None) -> WorkflowRun:
    return WorkflowRun(
        id="parent-1",
        workflow_id="wf-parent",
        entity_id="entity-1",
        started_by="user-1",
        trigger_data={},
        execution_snapshot={"steps": [{
            "id": "approve_media",
            "type": "wait",
            "config": {"approves_actions": actions or ["x.create_post"]},
        }]},
        step_results={"approve_media": {
            "status": "completed",
            "approved": True,
            "approved_by": approved_by,
            "approved_at": "2026-09-30T00:00:00+00:00",
        }},
    )


def _child(started_by: str | None = "user-1") -> WorkflowRun:
    return WorkflowRun(
        id="child-1",
        workflow_id="wf-child",
        entity_id="entity-1",
        started_by=started_by,
        trigger_data={"parent_run_id": "parent-1"},
    )


@pytest.mark.asyncio
async def test_child_approval_passes_on_the_parent_approval_that_lists_its_action() -> None:
    db = _Db({"parent-1": _parent()})

    delegated = await delegated_parent_workflow_approval(
        db, run=_child(), config={"approval_action_key": "x.create_post"},
    )

    assert delegated == {
        "parent_run_id": "parent-1",
        "parent_step_id": "approve_media",
        "approved_by": "user-1",
        "approved_at": "2026-09-30T00:00:00+00:00",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "child", "action"),
    [
        (_parent(actions=["facebook.create_post"]), _child(), "x.create_post"),  # not listed
        (_parent(approved_by="user-2"), _child(), "x.create_post"),  # another approver
        (_parent(approved_by=None), _child(started_by=None), "x.create_post"),  # no approver at all
        (_parent(), _child(), ""),  # the child declares no action
    ],
)
async def test_child_approval_is_not_delegated_without_a_matching_approval(parent, child, action) -> None:
    db = _Db({"parent-1": parent})

    assert await delegated_parent_workflow_approval(
        db, run=child, config={"approval_action_key": action},
    ) is None


def test_retry_keeps_a_value_produced_by_the_branch_that_ran() -> None:
    """Two branches may produce one variable; the unrun branch must not erase it."""
    from types import SimpleNamespace

    from packages.core.services.workflow_service import _retry_variables

    steps = [
        {"id": "start", "type": "trigger", "next": ["notes"]},
        {"id": "notes", "type": "condition", "true_next": ["rewrite"], "false_next": ["use_drafted"]},
        {"id": "use_drafted", "type": "transform", "next": ["render"], "config": {"set": {"prompts": {"video": "drafted"}}}},
        {"id": "rewrite", "type": "agent_call", "next": ["render"], "config": {"output_var": "prompts"}},
        {"id": "render", "type": "video", "config": {"prompt": "{{prompts.video}}"}},
    ]
    prior = WorkflowRun(
        id="run-1",
        workflow_id="wf-1",
        entity_id="entity-1",
        variables={"prompts": {"video": "drafted"}, "brief": "Launch"},
        step_results={
            "start": {"status": "completed", "output": {}},
            "notes": {"status": "completed", "output": False},
            "use_drafted": {"status": "completed", "output": {"prompts": {"video": "drafted"}, "brief": "Launch"}},
            "render": {"status": "failed", "error": "provider error"},
        },
    )
    workflow = SimpleNamespace(steps=steps, variables={})

    variables = _retry_variables(workflow, prior, {"start", "notes", "use_drafted"}, None, "render")

    assert variables["prompts"] == {"video": "drafted"}


def test_revision_history_accumulates_every_note() -> None:
    run = _run("revise", "Second")
    run.variables["review_note_history"] = ["First"]
    run.variables["__revisions"] = [{"step_id": "review"}]

    apply_workflow_approval_revision(STEPS, run)

    assert run.variables["review_note_history"] == ["First", "Second"]
    assert run.variables["review_notes"] == "Second"


def test_a_failed_skill_step_reports_the_skill_reason_not_the_contract_code() -> None:
    from packages.core.ai.workflow_runner import _skill_failure_result

    rejected = _skill_failure_result({
        "error": "output_schema_validation_failed",
        "content": '{"error": "The anchor image was rejected because it may contain a real person."}',
    })
    assert rejected["error"] == "The anchor image was rejected because it may contain a real person."
    assert rejected["code"] == "output_schema_validation_failed"
    assert rejected["status"] == "failed"

    plain = _skill_failure_result({"error": "output_schema_validation_failed", "content": "Clip 2 failed: timeout"})
    assert plain["error"] == "Clip 2 failed: timeout"

    # Nothing to explain: the code stays the error. Other errors pass through.
    assert _skill_failure_result({"error": "output_schema_validation_failed", "content": ""})["error"] == (
        "output_schema_validation_failed"
    )
    assert _skill_failure_result({"error": "Skill not found: x", "content": ""})["error"] == "Skill not found: x"


def test_a_node_output_file_must_exist_in_the_account_storage(tmp_path, monkeypatch):
    import os

    from packages.core.ai.workflow_runner import _missing_output_file_failure
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    os.makedirs(tmp_path / "ent", exist_ok=True)
    (tmp_path / "ent" / "final.mp4").write_bytes(b"video")

    assert _missing_output_file_failure(["video_url"], {"video_url": "/api/v1/fs/ent/final.mp4"}, entity_id="ent") is None
    # Invented links, another account's path, a path escaping the account, or
    # a file that was never written all fail the node.
    for value in (
        "https://example.com/video.mp4",
        "/api/v1/fs/other/final.mp4",
        "/api/v1/fs/ent/../other/final.mp4",
        "/api/v1/fs/ent/never-written.mp4",
        "",
    ):
        failure = _missing_output_file_failure(["video_url"], {"video_url": value}, entity_id="ent")
        assert failure and failure["code"] == "output_file_missing", value
    # Nodes without the setting are unaffected.
    assert _missing_output_file_failure(None, {"video_url": "https://example.com"}, entity_id="ent") is None
