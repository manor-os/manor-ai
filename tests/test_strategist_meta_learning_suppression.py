"""Strategist meta/bookkeeping learning-task suppression.

Learnings flow through the automatic learning pipeline (runtime evidence →
AgentLearningCandidate → operator resolve → memory apply) and must never be
proposed as tasks. Two layers enforce this:

  1. Prompt: RUNTIME_STRATEGIST_DEFAULT_PREAMBLE prohibits meta/bookkeeping
     proposals on both the legacy and the v2 (briefing) paths.
  2. Deterministic suppressor: service._suppress_meta_learning_proposals
     drops matching goal-unlinked tasks pre-persist in run_review.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.approval_settings_test_utils import initialize_test_approval_owners

from packages.core.ai.runtime import runtime_strategist_system_prompt
from packages.core.ai.runtime.strategist import RUNTIME_STRATEGIST_DEFAULT_PREAMBLE, RUNTIME_STRATEGIST_EMPTY_OK, RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE
from packages.core.models.base import generate_ulid
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.strategist import context, service
from packages.core.strategist.proposal import (
    Deliverable,
    EstimatedImpact,
    Proposal,
    ProposedTask,
)


def _deliverable() -> Deliverable:
    return Deliverable(
        name="result",
        kind="value",
        shape="TextResult",
        acceptance="task output produced",
        usage="reviewed by operator",
    )


@pytest.mark.asyncio
async def test_run_review_drops_meta_learning_task_and_keeps_real_task(
    db_session,
    monkeypatch,
) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_meta_learning_suppress",
        name="Meta Learning Suppress",
        status="active",
    )
    agent_id = generate_ulid()
    goal_id = generate_ulid()
    db_session.add(workspace)
    db_session.add_all([
        User(id=workspace.entity_id, entity_id=workspace.entity_id,
             email=f"{workspace.entity_id}@example.test", password_hash="fixture",
             role="owner", status="active"),
        UserMembership(user_id=workspace.entity_id, entity_id=workspace.entity_id,
                       role="owner", status="active"),
    ])
    db_session.add(
        Agent(
            id=agent_id,
            entity_id=workspace.entity_id,
            name="Ops Agent",
            status="active",
        )
    )
    db_session.add(
        AgentSubscription(
            id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            agent_id=agent_id,
            service_key="ops",
            status="active",
        )
    )
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=workspace.entity_id)
    await db_session.commit()

    async def _fake_generate_proposal(ctx, *, review_id: str, db=None):
        return Proposal(
            review_id=review_id,
            summary="Record learnings and prepare the weekly report.",
            tasks=[
                ProposedTask(
                    deliverables=[_deliverable()],
                    task_key="record_learnings",
                    title="Record this week's learnings to LEARNINGS.md",
                    description="Capture what we learned this cycle.",
                    owner_service_key="ops",
                ),
                ProposedTask(
                    deliverables=[_deliverable()],
                    task_key="weekly_report",
                    title="Prepare weekly pipeline report",
                    description="Summarize pipeline movement for the operator.",
                    owner_service_key="ops",
                    estimated_impact=EstimatedImpact(
                        goal_id=goal_id,
                        metric_delta=1.0,
                    ),
                ),
            ],
            notes=None,
        )

    async def _fake_post_proposal_chat(*args, **kwargs):
        return None

    monkeypatch.setattr(service, "generate_proposal", _fake_generate_proposal)
    monkeypatch.setattr(service, "_post_proposal_chat", _fake_post_proposal_chat)

    result = await service.run_review(db_session, workspace.id, trigger="manual")

    assert result["task_count"] == 1
    tasks = list(
        (
            await db_session.execute(
                select(Task).where(Task.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert tasks[0].title == "Prepare weekly pipeline report"
    assert "Suppressed 1 meta/bookkeeping task(s)" in (result["notes"] or "")
    assert "Record this week's learnings to LEARNINGS.md" in (result["notes"] or "")


def test_goal_linked_learning_content_task_is_not_suppressed() -> None:
    proposal = Proposal(
        review_id="rv_learning_content_kept",
        summary="Ship customer education content.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="course_module",
                title="Create learning course module for customers",
                description=(
                    "Write and record the onboarding learning module our "
                    "customers requested."
                ),
                owner_service_key="content",
                estimated_impact=EstimatedImpact(
                    goal_id="goal_customer_education",
                    metric_delta=5.0,
                ),
            ),
        ],
    )

    service._suppress_meta_learning_proposals(proposal)

    assert [task.task_key for task in proposal.tasks] == ["course_module"]
    assert proposal.notes is None


def test_consolidate_workspace_memory_without_goal_link_is_suppressed() -> None:
    proposal = Proposal(
        review_id="rv_consolidate_memory",
        summary="Tidy up workspace memory.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="consolidate_memory",
                title="Consolidate workspace memory",
                description="Merge and tidy the accumulated workspace memory notes.",
                owner_service_key="ops",
            ),
        ],
    )

    service._suppress_meta_learning_proposals(proposal)

    assert proposal.tasks == []
    assert "Suppressed 1 meta/bookkeeping task(s)" in (proposal.notes or "")
    assert "Consolidate workspace memory" in (proposal.notes or "")


def test_preamble_does_not_wait_for_workspace_chat() -> None:
    assert "Do not wait for the user to send a workspace chat message" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE


def test_preamble_does_not_treat_unscored_goals_as_missing_materials() -> None:
    assert "Missing Goal current_value is a measurement" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "Do not re-propose recently completed" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "do not automatically create a scoring task" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "An empty proposal" in RUNTIME_STRATEGIST_EMPTY_OK
    assert "does not automatically require a task" in RUNTIME_STRATEGIST_EMPTY_OK
    assert "An empty proposal is only valid when remaining Goals" not in RUNTIME_STRATEGIST_EMPTY_OK
    assert "appear as Need Input" in RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE
    assert "Never ask for this in workspace chat" in RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE
    assert "scores predate newer Goal-driving work" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "missing local uploads" not in RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE


def test_rejected_work_takes_priority_over_measurement_gaps_on_both_paths() -> None:
    for prompt in (RUNTIME_STRATEGIST_DEFAULT_PREAMBLE, RUNTIME_STRATEGIST_EMPTY_OK):
        assert "reason_code is feedback even" in prompt
        assert "under a new title" in prompt
        assert "explain what changed" in prompt
        assert "propose one scoring" not in prompt
        assert "missing evidence = 0" not in prompt



def test_preamble_treats_not_ready_existing_artifacts_as_agent_work() -> None:
    for blob in (RUNTIME_STRATEGIST_DEFAULT_PREAMBLE, RUNTIME_STRATEGIST_EMPTY_OK):
        assert "that do not already exist" not in blob
        assert "repair an existing" in blob
        assert "not-ready artifact in place" in blob
        assert "A not-ready existing artifact is not" in blob
        assert "leftover for the operator" in blob
        assert "Leftover for the operator is only login" in blob
        assert "Forbidding another copy of the same artifact is not forbidding" in blob
        assert "in-place repair of the current one" in blob


def test_public_strategist_prompt_has_no_music_release_domain_rules() -> None:
    blobs = (
        RUNTIME_STRATEGIST_DEFAULT_PREAMBLE,
        RUNTIME_STRATEGIST_EMPTY_OK,
        RUNTIME_STRATEGIST_HUMAN_REQUESTS_GUIDANCE,
    )
    banned = (
        "DistroKid",
        "royalty-register",
        "royalty registration",
        "audio master",
        "cover artwork",
        "lyrics",
        "first-release",
        "ISRC",
        "UPC",
        "operator-only leftover",
        "already-provided inputs",
        "path traversal",
        "cannot read a local path",
        "inventing new user-owned",
        "WAV master",
    )
    joined = "\n".join(blobs).lower()
    for word in banned:
        assert word.lower() not in joined


def test_preamble_prohibits_meta_learning_tasks_on_both_paths() -> None:
    assert "Never propose meta/bookkeeping tasks" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
    assert "records learnings automatically" in RUNTIME_STRATEGIST_DEFAULT_PREAMBLE

    ctx = SimpleNamespace(
        workspace=SimpleNamespace(name="Prohibition Check"),
        subscriptions=[],
        allowed_service_keys=[],
        strategist_template={},
    )
    legacy = runtime_strategist_system_prompt(ctx)
    v2 = runtime_strategist_system_prompt(ctx, briefing_mode=True)

    for prompt in (legacy, v2):
        assert "Never propose meta/bookkeeping tasks" in prompt
        assert "LEARNINGS.md/MEMORY.md/STATE.md" in prompt
        assert "Missing Goal current_value is a measurement" in prompt
    assert "appear as Need Input" in v2
    assert "Never ask for this in workspace chat" in v2

def _completed_task(*, title: str, when: datetime, status: str = "completed"):
    return SimpleNamespace(
        id="task_" + title[:12].replace(" ", "_"),
        title=title,
        status=status,
        completed_at=when,
        updated_at=when,
        created_at=when,
    )


def test_duplicate_recent_handoff_proposal_is_suppressed() -> None:
    now = datetime.now(timezone.utc)
    completed = _completed_task(
        title="Compile Tonight DistroKid operator handoff packet",
        when=now - timedelta(minutes=10),
    )
    proposal = Proposal(
        review_id="rv_dup_handoff",
        summary="Compile another DistroKid handoff.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="compile_handoff",
                title="Compile DistroKid operator handoff for Tonight",
                description="Assemble the operator DistroKid self-submit packet.",
                owner_service_key="distribution_delivery",
            ),
        ],
    )
    ctx = SimpleNamespace(
        recent_tasks=[completed],
        recent_proposal_outcomes={"completed": [completed]},
    )

    service._suppress_duplicate_completed_work_proposals(proposal, ctx)

    assert proposal.tasks == []
    notes = proposal.notes or ""
    assert "measurement" in notes.lower()
    assert "Compile DistroKid operator handoff for Tonight" in notes


def test_distinct_goal_scoring_proposal_is_kept() -> None:
    now = datetime.now(timezone.utc)
    completed = _completed_task(
        title="Compile Tonight DistroKid operator handoff packet",
        when=now - timedelta(minutes=10),
    )
    proposal = Proposal(
        review_id="rv_score_goals_kept",
        summary="Score the active Goals.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="score_goals",
                title="Score active Goals from existing evidence",
                description="Read the existing operator handoff and score the five goals.",
                owner_service_key="release_operations",
            ),
        ],
    )
    ctx = SimpleNamespace(
        recent_tasks=[completed],
        recent_proposal_outcomes={"completed": [completed]},
    )

    service._suppress_duplicate_completed_work_proposals(proposal, ctx)

    assert [task.task_key for task in proposal.tasks] == ["score_goals"]
    assert proposal.notes is None


def test_similar_handoff_title_outside_12h_window_is_kept() -> None:
    now = datetime.now(timezone.utc)
    completed = _completed_task(
        title="Compile Tonight DistroKid operator handoff packet",
        when=now - timedelta(days=3),
    )
    proposal = Proposal(
        review_id="rv_old_handoff_kept",
        summary="Compile a DistroKid handoff later.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="compile_handoff",
                title="Compile DistroKid operator handoff for Tonight",
                description="Assemble the operator DistroKid self-submit packet.",
                owner_service_key="distribution_delivery",
            ),
        ],
    )
    ctx = SimpleNamespace(
        recent_tasks=[completed],
        recent_proposal_outcomes={"completed": [completed]},
    )

    service._suppress_duplicate_completed_work_proposals(proposal, ctx)

    assert [task.task_key for task in proposal.tasks] == ["compile_handoff"]
    assert proposal.notes is None


@pytest.mark.asyncio
async def test_proposal_item_rejection_enters_strategist_outcomes_without_task_comment(
    db_session,
) -> None:
    workspace_id = generate_ulid()
    review_id = generate_ulid()
    proposal_id = generate_ulid()
    item_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add(Task(
        id=task_id,
        entity_id="ent_rejected_outcome",
        workspace_id=workspace_id,
        title="Score active Goals from current evidence",
        status="cancelled",
        details={
            "strategist_review_id": review_id,
            "strategist_proposal_item_id": item_id,
            "strategist_task_key": "score_goals",
        },
    ))
    db_session.add(ProposalRecord(
        id=proposal_id,
        entity_id="ent_rejected_outcome",
        workspace_id=workspace_id,
        review_id=review_id,
        summary="No work",
        status="resolved",
    ))
    db_session.add(ProposalItemRecord(
        id=item_id,
        proposal_id=proposal_id,
        entity_id="ent_rejected_outcome",
        workspace_id=workspace_id,
        item_key="score_goals",
        kind="task",
        payload={"task_id": task_id},
        action_key="workspace.proposal.task",
        status="rejected",
        decision={"decision": "rejected", "reason_code": "DUPLICATE"},
    ))
    await db_session.commit()

    outcomes = await context._recent_proposal_outcomes(db_session, workspace_id)

    assert [task.id for task in outcomes.get("rejected", [])] == [task_id]
    assert outcomes.get("abandoned", []) == []



@pytest.mark.asyncio
@pytest.mark.parametrize("rejection_source", ["proposal_item", "reason", "reason_code", "none"])
async def test_outcomes_preserve_both_item_and_task_rejection_evidence(
    rejection_source: str,
) -> None:
    details = {"strategist_review_id": "review"}
    if rejection_source == "proposal_item":
        details["strategist_proposal_item_id"] = "rejected-item"
    elif rejection_source == "reason":
        details["rejection_reason"] = "Wrong direction"
    elif rejection_source == "reason_code":
        details["rejection_reason_code"] = "WRONG_DIRECTION"
    task = SimpleNamespace(id="task", status="cancelled", details=details)
    query_rows = [[task]]
    if rejection_source == "proposal_item":
        query_rows.append(["rejected-item"])
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalars=lambda values=values: SimpleNamespace(all=lambda: values))
        for values in query_rows
    ]))

    outcomes = await context._recent_proposal_outcomes(db, "workspace")

    expected_bucket = "abandoned" if rejection_source == "none" else "rejected"
    assert outcomes == {expected_bucket: [task]}


def test_goal_scoring_proposal_is_not_meta_dropped_without_goal_id() -> None:
    proposal = Proposal(
        review_id="rv_score_not_meta",
        summary="Score active Goals from current evidence.",
        tasks=[
            ProposedTask(
                deliverables=[_deliverable()],
                task_key="score_goals",
                title="Score all five Goals from current workspace evidence",
                description=(
                    "Record current Goal scores from workspace evidence "
                    "and update LEARNINGS.md with the measurement."
                ),
                owner_service_key="readiness_and_learning",
            ),
        ],
    )

    service._suppress_meta_learning_proposals(proposal)

    assert [task.task_key for task in proposal.tasks] == ["score_goals"]
    assert proposal.notes is None
