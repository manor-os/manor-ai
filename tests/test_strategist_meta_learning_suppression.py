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

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai.runtime import runtime_strategist_system_prompt
from packages.core.ai.runtime.strategist import RUNTIME_STRATEGIST_DEFAULT_PREAMBLE
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.strategist import service
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
