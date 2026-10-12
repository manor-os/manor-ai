"""Strategist execution claims stay bound to the current Task-owned evidence."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.strategist_helpers import proposal_with_execution_review

from packages.core.ai.runtime import strategist as runtime
from packages.core.models.task import Task
from packages.core.services.task_capability_recovery import BLOCKER_KEY
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.strategist import context as strategist_context, prompt
from packages.core.strategist.context import StrategistContext
from packages.core.strategist.proposal import Proposal
from packages.core.strategist.service import _suppress_capability_recovery_duplicates


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(prompt, "_load_skill_preamble", AsyncMock(return_value=None))
    observed_at = "2026-09-26T04:41:33+00:00"
    return StrategistContext(
        workspace=SimpleNamespace(
            id="workspace-1", entity_id="entity-1", name="Workspace", settings={},
            operating_model={},
        ),
        operating_memory="Historical service note: all release operations are running.",
        relevant_memory=[{
            "title": "Old blocked research", "scope": "workspace", "confidence": 0.8,
            "content": "Historical memory: the old search integration was unavailable.",
        }],
        execution_snapshot={
            "status": "available", "observed_at": observed_at, "revision": "revision-current",
            "tasks": [
                {
                    "task_id": "task-A", "title": "Verify the repaired document",
                    "task_status": "blocked", "observed_state": "blocked", "updated_at": observed_at,
                    "plan_id": "plan-A", "plan_status": "running",
                    "coordinator": {"phase": "waiting_human"}, "step_counts": {"done": 1},
                    "current_blocker": None,
                    "latest_error": {"code": "coordinator_acceptance_exhausted"},
                },
                {
                    "task_id": "task-B", "title": "Research the next release",
                    "task_status": "blocked", "observed_state": "blocked", "updated_at": observed_at,
                    "plan_id": "plan-B", "plan_status": "pending",
                    "coordinator": {"phase": "waiting_human"}, "step_counts": {"pending": 1},
                    "current_blocker": {
                        "source_task_id": "task-B", "ref": "blocker-B", "kind": "capability_unavailable",
                        "recorded_missing_capabilities": ["web.safe_search"],
                        "current_missing_capabilities": ["web.safe_search"], "scope_status": "still_missing",
                    },
                    "latest_error": None,
                },
                {
                    "task_id": "task-C", "title": "Prepare release notes",
                    "task_status": "pending", "observed_state": "not_started", "updated_at": observed_at,
                    "plan_id": None, "plan_status": None, "coordinator": None, "step_counts": {},
                    "current_blocker": None, "latest_error": None,
                },
            ],
        },
    )


def _proposal(context):
    snapshot = context.execution_snapshot
    return {
        "summary": "The document verification is blocked; research has a separate capability gap. Release notes have not started.",
        "tasks": [],
        "execution_review": {
            "revision": snapshot["revision"],
            "references": [{
                "task_id": task["task_id"], "plan_id": task["plan_id"],
                "observed_state": task["observed_state"],
                "blocker_ref": (task["current_blocker"] or {}).get("ref"),
            } for task in snapshot["tasks"]],
        },
    }


def _completion(proposal):
    return SimpleNamespace(content=json.dumps(proposal))


def _repair_feedback(completion):
    retry_prompt = completion.await_args_list[1].args[1]
    return json.loads(retry_prompt.split("Your previous response failed validation. ", 1)[1])


@pytest.mark.parametrize("briefing", [None, "Historical report: release operations were running."])
def test_current_execution_snapshot_is_present_after_historical_context(context, briefing):
    if briefing is None:
        rendered = runtime.runtime_strategist_user_prompt(context, review_id="review-1")
    else:
        rendered = runtime.runtime_strategist_user_prompt_v2(briefing, context, review_id="review-1")
    section_at = rendered.index("# Current execution evidence")
    assert section_at > rendered.index("Historical service note")
    assert section_at > rendered.index("Historical memory")
    if briefing is not None:
        assert section_at > rendered.index(briefing)
    current_section = rendered[section_at:]
    assert context.execution_snapshot["revision"] in current_section
    for task in context.execution_snapshot["tasks"]:
        assert task["task_id"] in current_section
        assert task["observed_state"] in current_section
    assert "blocker-B" in current_section
    assert "source_task_id" in current_section
    assert "current_missing_capabilities" in current_section
    assert "coordinator_acceptance_exhausted" in current_section


@pytest.mark.parametrize("briefing_mode", [False, True])
def test_custom_preamble_cannot_remove_execution_evidence_contract(context, briefing_mode):
    rendered = runtime.runtime_strategist_system_prompt(
        context, preamble="Custom workspace tone for {workspace_name}.", briefing_mode=briefing_mode,
    )
    assert rendered.startswith("Custom workspace tone for Workspace.")
    assert "execution_review" in rendered
    assert "revision" in rendered
    assert "task_id" in rendered
    assert "plan_id" in rendered
    assert "blocker_ref" in rendered
    assert "not_started" in rendered
    assert "pending" in rendered.lower()


def test_historical_proposal_parsing_does_not_require_new_execution_review(context):
    historical = Proposal.model_validate({"review_id": "old-review", "summary": "Old proposal", "tasks": []})
    assert historical.execution_review is None
    current = prompt._parse_proposal(json.dumps(_proposal(context)), review_id="review-1")
    assert current.execution_review.model_dump() == _proposal(context)["execution_review"]


@pytest.mark.parametrize("revision", ["first-observation", "changed-after-recovery"])
def test_success_mock_acknowledges_actual_prompt_revision_without_mutating_fixture(context, revision):
    context.execution_snapshot["revision"] = revision
    rendered = runtime.runtime_strategist_user_prompt_v2("Briefing", context, review_id="review-1")
    fixture = {"summary": "No new strategy change", "tasks": []}
    completed = proposal_with_execution_review(fixture, rendered)
    assert completed["execution_review"] == {"revision": revision, "references": []}
    assert "execution_review" not in fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("briefing", [None, "Evidence briefing"])
@pytest.mark.parametrize("mistake,expected_path", [
    ("missing_review", ["execution_review"]),
    ("stale_revision", ["execution_review", "revision"]),
    ("wrong_plan", ["execution_review", "references", 0, "plan_id"]),
    ("wrong_state", ["execution_review", "references", 2, "observed_state"]),
    ("cross_bound_blocker", ["execution_review", "references", 0, "blocker_ref"]),
])
async def test_execution_evidence_mismatch_is_repaired_with_scoped_contract(
    context, monkeypatch, briefing, mistake, expected_path,
):
    correct = _proposal(context)
    invalid = copy.deepcopy(correct)
    if mistake == "missing_review":
        invalid.pop("execution_review")
    elif mistake == "stale_revision":
        invalid["execution_review"]["revision"] = "revision-before-recovery"
    elif mistake == "wrong_plan":
        invalid["execution_review"]["references"][0]["plan_id"] = "plan-B"
    elif mistake == "wrong_state":
        invalid["summary"] = "All services are running, so release notes are already running."
        invalid["execution_review"]["references"][2]["observed_state"] = "running"
    else:
        invalid["execution_review"]["references"][0]["blocker_ref"] = "blocker-B"
    completion = AsyncMock(side_effect=[_completion(invalid), _completion(correct)])
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)

    proposal = await prompt.generate_proposal(
        context, review_id="review-1", user_id="actor-1", briefing_markdown=briefing,
    )
    assert proposal.execution_review.model_dump() == correct["execution_review"]
    assert completion.await_count == 2
    feedback = _repair_feedback(completion)
    assert feedback["contract"] == "tool_error"
    assert feedback["error"] == "proposal_execution_evidence_mismatch"
    errors = feedback["details"]["validation_errors"]
    matching = [error for error in errors if error["path"] == expected_path]
    assert matching, errors
    assert all("expected" in error for error in matching)
    for call in completion.await_args_list:
        assert call.kwargs == {"entity_id": "entity-1", "workspace_id": "workspace-1", "user_id": "actor-1"}
        assert "# Current execution evidence" in call.args[1]
        assert context.execution_snapshot["revision"] in call.args[1]
    assert context.execution_snapshot["tasks"][2]["task_status"] == "pending"
    assert context.execution_snapshot["tasks"][2]["observed_state"] == "not_started"


@pytest.mark.asyncio
async def test_correct_execution_references_need_no_repair(context, monkeypatch):
    value = _proposal(context)
    completion = AsyncMock(return_value=_completion(value))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    proposal = await prompt.generate_proposal(context, review_id="review-1")
    assert proposal.execution_review.model_dump() == value["execution_review"]
    completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_execution_mismatch_raises_instead_of_inventing_success(context, monkeypatch):
    invalid = _proposal(context)
    invalid["execution_review"]["references"][0]["blocker_ref"] = "blocker-B"
    completion = AsyncMock(return_value=_completion(invalid))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        await prompt.generate_proposal(context, review_id="review-1")
    assert caught.value.code == "proposal_execution_evidence_mismatch"
    assert caught.value.details["attempts"] == 2
    assert caught.value.details["review_id"] == "review-1"
    assert caught.value.details["validation_errors"]
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_available_empty_snapshot_still_requires_revision_acknowledgement(context, monkeypatch):
    context.execution_snapshot["tasks"] = []
    invalid = {"summary": "No current execution", "tasks": []}
    correct = {**invalid, "execution_review": {"revision": "revision-current", "references": []}}
    completion = AsyncMock(side_effect=[_completion(invalid), _completion(correct)])
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    proposal = await prompt.generate_proposal(context, review_id="review-1")
    assert proposal.execution_review.model_dump() == correct["execution_review"]
    assert _repair_feedback(completion)["error"] == "proposal_execution_evidence_mismatch"
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_no_execution_snapshot_preserves_historical_caller_compatibility(context, monkeypatch):
    context.execution_snapshot = {}
    completion = AsyncMock(return_value=_completion({"summary": "No current evidence supplied", "tasks": []}))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    proposal = await prompt.generate_proposal(context, review_id="review-1")
    assert proposal.execution_review is None
    completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_recent_outcomes_keep_queued_and_blocked_tasks_out_of_running_and_abandoned():
    statuses = ("pending", "blocked", "waiting_on_customer", "on_hold", "in_progress", "failed")
    tasks = [SimpleNamespace(
        id=f"task-{status}", status=status, details={"strategist_review_id": "review-1"},
    ) for status in statuses]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: tasks),
    )))

    outcomes = await strategist_context._recent_proposal_outcomes(db, "workspace-1")

    assert {bucket: [task.status for task in rows] for bucket, rows in outcomes.items()} == {
        "pending": ["pending"],
        "blocked": ["blocked", "waiting_on_customer", "on_hold"],
        "in_progress": ["in_progress"],
        "abandoned": ["failed"],
    }
    db.execute.assert_awaited_once()


def _recovery_proposal():
    def task(key, owner, capabilities=(), dependencies=()):
        return SimpleNamespace(
            task_key=key, owner_service_key=owner, required_capabilities=list(capabilities),
            delegate_service_keys=[], depends_on_task_keys=list(dependencies),
        )
    return SimpleNamespace(notes=None, tasks=[
        task("recover", "research", ["web.safe_search"]),
        task("dependent", "analysis", dependencies=["recover"]),
        task("dependent-chain", "delivery", dependencies=["dependent"]),
        task("other-owner", "publishing", ["web.safe_search"]),
        task("independent", "research", ["file.write"]),
    ])


def _historical_blocker(context, *, owner="research"):
    task = Task(
        id="task-B", entity_id="entity-1", workspace_id="workspace-1",
        title="Research the next release", status="blocked", task_type="general",
        owner_service_key=owner, owner_subscription_id="subscription-1",
        delegate_service_keys=[], details={},
    )
    task.details = {BLOCKER_KEY: {
        "generation": task_execution_intent_generation(task), "missing_capabilities": ["web.safe_search"],
    }}
    context.recent_tasks = [task]
    blocker = context.execution_snapshot["tasks"][1]["current_blocker"]
    blocker["affected_agents"] = [{"agent_id": "agent-1", "service_key": "research"}]
    return blocker


@pytest.mark.parametrize("scope_status", ["configuration_changed", "unavailable"])
def test_old_capability_failure_cannot_override_changed_or_unknown_scope(context, scope_status):
    blocker = _historical_blocker(context)
    blocker["scope_status"] = scope_status
    blocker["current_missing_capabilities"] = []
    proposal = _recovery_proposal()
    original = list(proposal.tasks)

    _suppress_capability_recovery_duplicates(proposal, context)

    assert proposal.tasks == original
    assert proposal.notes is None
    historical = runtime._format_tasks(context)
    assert "waiting_configuration" not in historical
    assert "web.safe_search" not in historical
    rendered = runtime.runtime_strategist_user_prompt(context, review_id="review-1")
    assert scope_status in rendered
    assert '"scope_status": "still_missing"' not in rendered


@pytest.mark.parametrize("old_owner", ["research", None])
def test_current_exact_task_agent_scope_suppresses_only_its_matching_recovery_chain(context, old_owner):
    _historical_blocker(context, owner=old_owner)
    proposal = _recovery_proposal()

    _suppress_capability_recovery_duplicates(proposal, context)

    assert [task.task_key for task in proposal.tasks] == ["other-owner", "independent"]
    assert "waiting for Agent configuration" in proposal.notes


def test_blocker_from_another_task_cannot_suppress_recovery(context):
    blocker = _historical_blocker(context)
    blocker["source_task_id"] = "task-A"
    proposal = _recovery_proposal()
    original = list(proposal.tasks)

    _suppress_capability_recovery_duplicates(proposal, context)

    assert proposal.tasks == original
    assert proposal.notes is None


def test_capable_selected_delegate_keeps_recovery_proposal_runnable(context):
    _historical_blocker(context)
    context.service_capabilities = {"research-delegate": {"capability_ids": ["web.safe_search"]}}
    proposal = _recovery_proposal()
    proposal.tasks[0].delegate_service_keys = ["research-delegate"]
    original = list(proposal.tasks)

    _suppress_capability_recovery_duplicates(proposal, context)

    assert proposal.tasks == original
    assert proposal.notes is None


@pytest.mark.parametrize("observed_state,observed_plan_id,live", [
    ("blocked", "plan-A", False),
    ("not_started", "plan-A", False),
    ("running", "different-plan", False),
    ("running", "plan-A", True),
])
def test_raw_running_plan_is_live_only_with_matching_current_execution_evidence(
    context, observed_state, observed_plan_id, live,
):
    context.recent_tasks = [SimpleNamespace(id="task-A", status="blocked")]
    context.recent_plans = [SimpleNamespace(
        id="plan-A", task_id="task-A", status="running", execution_mode="autonomous", cost_tracking={},
    )]
    observation = context.execution_snapshot["tasks"][0]
    observation["observed_state"] = observed_state
    observation["plan_id"] = observed_plan_id
    if live:
        observation["task_status"] = "in_progress"

    rendered = runtime._format_plans(context)

    assert ("Live plans: none." not in rendered) is live
    assert "status=running" in rendered
    if live:
        assert rendered.startswith("Live plans:\n- plan-A")
        assert "observed_state=running" in rendered
    else:
        assert "Other recorded plans (not proof of active execution):" in rendered


def test_pending_and_blocked_outcomes_are_not_rendered_as_still_running(context):
    context.recent_proposal_outcomes = {
        status: [SimpleNamespace(
            id=f"task-{status}", title=f"Recorded {status} task", status=status,
            owner_service_key="research", details={}, actual_output={},
        )]
        for status in ("pending", "blocked", "in_progress")
    }

    rendered = runtime._format_proposal_outcomes(context)

    assert "still running" not in rendered.lower()
    assert "Queued / not started (not running)" in rendered
    assert "Blocked / paused (not running)" in rendered
    assert "Recorded in_progress (consult execution evidence for activity)" in rendered
    for status in context.recent_proposal_outcomes:
        assert f"task_id=task-{status} status={status}" in rendered
