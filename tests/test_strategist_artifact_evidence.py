from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal, GoalTaskLink
from packages.core.models.runtime_learning import RuntimeEvidence
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceActivity,
    WorkspaceWorkBatch,
)
from packages.core.ai.runtime import (
    runtime_strategist_system_prompt,
    runtime_strategist_tasks_text,
    runtime_strategist_user_prompt,
)
from packages.core.strategist import service
from packages.core.strategist.evaluation import evaluate_workspace_outcomes
from packages.core.strategist.proposal import Deliverable, EstimatedImpact, Proposal, ProposedTask


def test_completed_file_task_without_files_is_marked_text_only() -> None:
    task = SimpleNamespace(
        status="completed",
        title="生成客户可下载的PDF报价单",
        description="输出一个可交付给客户的报价单文件。",
        owner_service_key="sales_ops",
        completed_at=None,
        expected_output=None,
        actual_output={"steps": [{"status": "done", "result_summary": "文字方案"}]},
    )
    ctx = SimpleNamespace(recent_tasks=[task])

    text = runtime_strategist_tasks_text(ctx)

    assert "artifacts=0" in text
    assert "text-only" in text


def test_completed_file_task_with_files_reports_artifact_count() -> None:
    task = SimpleNamespace(
        status="completed",
        title="生成客户可下载的PDF报价单",
        description="输出一个可交付给客户的报价单文件。",
        owner_service_key="sales_ops",
        completed_at=None,
        expected_output=None,
        actual_output={
            "files": [
                {"type": "pdf", "url": "/api/v1/fs/ent/quote-a.pdf"},
                {"type": "pdf", "url": "/api/v1/fs/ent/quote-b.pdf"},
            ]
        },
    )
    ctx = SimpleNamespace(recent_tasks=[task])

    text = runtime_strategist_tasks_text(ctx)

    assert "artifacts=2" in text
    assert "text-only" not in text


def test_user_prompt_includes_workspace_operating_memory() -> None:
    ctx = SimpleNamespace(
        trigger="scheduled",
        missing_setup=[],
        configured_integrations=[],
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_runtime_evidence=[],
        learning_candidates=[],
        operating_memory="### RULES.md\n- External posts require operator approval.",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
    )

    text = runtime_strategist_user_prompt(ctx, review_id="review_operating_memory")

    assert "# Workspace operating memory (canonical docs)" in text
    assert "External posts require operator approval" in text


def test_user_prompt_includes_runtime_evidence_and_learning_candidates() -> None:
    ctx = SimpleNamespace(
        trigger="work_batch_completed:batch_123",
        missing_setup=[],
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy={},
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_runtime_evidence=[
            SimpleNamespace(
                id="ev_task_123",
                status="succeeded",
                evidence_type="task_run",
                metrics={"cost_usd": 0.0123},
                details={"tool_calls_made": ["generate_file", "workspace_search"]},
                summary="Task completed with generated artifact evidence.",
            )
        ],
        learning_candidates=[
            SimpleNamespace(
                id="lc_agent_profile",
                status="proposed",
                candidate_type="agent_profile_patch",
                scope="workspace_agent",
                risk_level="low",
                confidence=0.86,
                title="Prefer lease consultative summaries before recommendations.",
                summary="The leasing agent should summarize customer needs before listing units.",
            )
        ],
        operating_memory="",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
    )

    text = runtime_strategist_user_prompt(ctx, review_id="review_runtime_evidence")

    assert "# Runtime evidence + agent learning candidates" in text
    assert "candidate_id=lc_agent_profile" in text
    assert "Prefer lease consultative summaries" in text
    assert "evidence_id=ev_task_123" in text
    assert "Task completed with generated artifact evidence." in text


def test_user_prompt_includes_recent_workspace_activity() -> None:
    ctx = SimpleNamespace(
        trigger="scheduled",
        missing_setup=[],
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy={},
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_activity=[
            SimpleNamespace(
                created_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
                event_type="workspace_operation.runtime_repaired",
                summary="Workspace operation runtime repaired",
                details={"review_id": "rv_activity"},
            )
        ],
        recent_runtime_evidence=[],
        learning_candidates=[],
        operating_memory="",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
    )

    text = runtime_strategist_user_prompt(ctx, review_id="review_activity")

    assert "# Recent workspace activity" in text
    assert "workspace_operation.runtime_repaired" in text
    assert "Workspace operation runtime repaired" in text
    assert "review_id=rv_activity" in text


def test_user_prompt_separates_builtin_channels_from_external_integrations() -> None:
    ctx = SimpleNamespace(
        trigger="scheduled",
        missing_setup=[],
        configured_integrations=[],
        configured_channels=[
            {
                "role": "primary_external",
                "channel_type": "webchat",
                "linked_service_key": "lead_intake",
                "purpose": "Primary inbound leasing channel.",
                "built_in": True,
            }
        ],
        knowledge_nets=[
            {
                "name": "Unit Inventory",
                "document_count": 1,
                "linked_service_keys": ["unit_recommendation"],
                "purpose": "Available unit facts.",
            }
        ],
        governance_policy={"hitl_required_actions": ["external_message.send"]},
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_runtime_evidence=[],
        learning_candidates=[],
        operating_memory="",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
    )

    text = runtime_strategist_user_prompt(ctx, review_id="review_channels")

    assert "# Configured channels" in text
    assert "primary_external: webchat" in text
    assert "No integrations/channels are configured" not in text
    assert "Connect email integration" not in text
    assert "HITL required actions: external_message.send" in text
    assert "Auto-approve actions: _(none configured)_" in text


def test_user_prompt_marks_setup_owned_starter_knowledge() -> None:
    ctx = SimpleNamespace(
        trigger="workspace_created",
        missing_setup=[],
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[
            {
                "name": "Unit Inventory & Floor Plans",
                "document_count": 0,
                "linked_service_keys": ["unit_matching"],
                "purpose": "Availability facts.",
                "starter_document_status": "scheduled",
                "starter_task_key": "seed_unit_inventory_knowledge",
            }
        ],
        governance_policy={},
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_activity=[],
        recent_runtime_evidence=[],
        learning_candidates=[],
        operating_memory="",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
    )

    text = runtime_strategist_user_prompt(ctx, review_id="review_starter_knowledge")
    system = runtime_strategist_system_prompt(
        SimpleNamespace(
            workspace=SimpleNamespace(name="Leasing Ops"),
            subscriptions=[],
            allowed_service_keys=[],
            strategist_template={},
        )
    )

    assert "starter_document=scheduled" in text
    assert "starter_task_key=seed_unit_inventory_knowledge" in text
    assert "do NOT propose another task to draft/seed the same knowledge" in system


def test_strategist_sanitizes_unsupported_auto_approval_claims() -> None:
    proposal = Proposal(
        review_id="review_auto_claims",
        summary=("These draft tasks align with the auto-approved action types per governance policy."),
        notes="Drafting is auto-approved; sending still needs approval.",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                title="Draft follow-up messages",
                description="Drafting-only task that is automatically approved per governance policy.",
                owner_service_key="followup_drafting",
                priority=3,
                rationale="This is auto-approved per governance policy.",
                estimated_impact=EstimatedImpact(
                    rationale="Auto approve action for drafting.",
                    metric_delta=1,
                ),
            )
        ],
    )
    ctx = SimpleNamespace(governance_policy={"auto_approve_actions": []})

    service._sanitize_governance_language(proposal, ctx)
    combined = "\n".join(
        [
            proposal.summary,
            proposal.notes or "",
            proposal.tasks[0].description or "",
            proposal.tasks[0].rationale or "",
            proposal.tasks[0].estimated_impact.rationale or "",
        ]
    ).lower()

    assert "auto-approved" not in combined
    assert "automatically approved" not in combined
    assert "auto approve" not in combined
    assert "low-risk internal" in combined


def test_strategist_suppresses_setup_owned_starter_doc_proposals() -> None:
    proposal = Proposal(
        review_id="review_starter_filter",
        summary="Seed knowledge, then report status.",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="seed_unit_inventory_knowledge",
                title="Draft unit inventory starter document",
                owner_service_key="unit_matching",
            ),
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="baseline_report",
                title="Generate baseline report",
                owner_service_key="pipeline_reporting",
            ),
        ],
    )
    ctx = SimpleNamespace(
        knowledge_nets=[
            {
                "starter_task_key": "seed_unit_inventory_knowledge",
                "starter_document_status": "scheduled",
            }
        ]
    )

    service._suppress_starter_document_proposals(proposal, ctx)

    assert [task.task_key for task in proposal.tasks] == ["baseline_report"]
    assert "Skipped setup-owned starter knowledge proposal" in (proposal.notes or "")


@pytest.mark.asyncio
async def test_strategist_cleanup_updates_stale_starter_doc_proposal_card(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_starter_cleanup",
        name="Starter Cleanup",
        status="active",
    )
    review_id = "rv_starter_cleanup"
    starter = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Draft unit inventory starter document",
        status="proposed",
        task_type="ai_generated",
        details={
            "strategist_review_id": review_id,
            "strategist_task_key": "seed_unit_inventory_knowledge",
        },
    )
    baseline = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Generate baseline report",
        status="proposed",
        task_type="ai_generated",
        details={
            "strategist_review_id": review_id,
            "strategist_task_key": "baseline_report",
        },
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Workspace chat",
        status="active",
        scope="workspace",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Strategist proposal",
        author_kind="agent",
        message_kind="proposal",
        pending_action={
            "kind": "approve_proposals",
            "review_id": review_id,
            "task_ids": [starter.id, baseline.id],
            "task_titles": [starter.title, baseline.title],
        },
        refs=[{"type": "task", "id": starter.id}, {"type": "task", "id": baseline.id}],
    )
    db_session.add_all([workspace, starter, baseline, conversation, message])
    await db_session.flush()

    ctx = SimpleNamespace(
        knowledge_nets=[
            {
                "starter_task_key": "seed_unit_inventory_knowledge",
                "starter_document_status": "ready",
                "document_count": 1,
            }
        ]
    )

    result = await service._resolve_fulfilled_starter_document_proposals(
        db_session,
        workspace,
        ctx,
    )
    await db_session.flush()

    assert result["task_ids"] == [starter.id]
    assert starter.status == "cancelled"
    assert starter.details["obsolete_reason"] == "fulfilled_by_workspace_starter_document"
    assert baseline.status == "proposed"
    assert message.resolved_at is None
    assert message.pending_action["task_ids"] == [baseline.id]
    assert message.pending_action["task_titles"] == [baseline.title]
    assert "Remaining task(s)" in (message.content or "")


def test_proposal_rejects_task_dependency_cycles() -> None:
    with pytest.raises(ValueError, match="dependency cycle"):
        Proposal(
            review_id="review_cycle",
            summary="Invalid cycle.",
            tasks=[
                ProposedTask(
                    deliverables=[
                        Deliverable(
                            name="result",
                            kind="value",
                            shape="TextResult",
                            acceptance="task output produced",
                            usage="reviewed by operator",
                        )
                    ],
                    task_key="a",
                    title="A",
                    owner_service_key="content",
                    depends_on_task_keys=["b"],
                ),
                ProposedTask(
                    deliverables=[
                        Deliverable(
                            name="result",
                            kind="value",
                            shape="TextResult",
                            acceptance="task output produced",
                            usage="reviewed by operator",
                        )
                    ],
                    task_key="b",
                    title="B",
                    owner_service_key="content",
                    depends_on_task_keys=["a"],
                ),
            ],
        )


def test_proposal_normalizes_dependency_keys_before_validation() -> None:
    proposal = Proposal(
        review_id="review_key_normalization",
        summary="Normalize keys.",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="Draft Docs",
                title="Draft source docs",
                owner_service_key="content",
            ),
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="Publish Calendar",
                title="Publish calendar",
                owner_service_key="content",
                depends_on_task_keys=["draft-docs"],
            ),
        ],
    )

    assert proposal.tasks[0].task_key == "draft_docs"
    assert proposal.tasks[1].task_key == "publish_calendar"
    assert proposal.tasks[1].depends_on_task_keys == ["draft_docs"]


@pytest.mark.asyncio
async def test_strategist_review_records_runtime_evidence(db_session, monkeypatch) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_evidence",
        name="Strategy Evidence",
        status="active",
    )
    db_session.add(workspace)
    await db_session.commit()

    async def _fake_generate_proposal(ctx, *, review_id: str, db=None):
        return Proposal(
            review_id=review_id,
            summary="No new tasks this cycle; keep monitoring current results.",
            tasks=[],
            notes=None,
        )

    monkeypatch.setattr(service, "generate_proposal", _fake_generate_proposal)

    result = await service.run_review(db_session, workspace.id, trigger="manual")

    rows = list(
        (
            await db_session.execute(
                select(RuntimeEvidence).where(
                    RuntimeEvidence.workspace_id == workspace.id,
                    RuntimeEvidence.evidence_type == "strategist_review",
                )
            )
        )
        .scalars()
        .all()
    )
    assert result["task_count"] == 0
    assert len(rows) == 1
    assert rows[0].trace_id == result["review_id"]
    assert rows[0].source == "strategist"
    assert rows[0].details["trigger"] == "manual"
    assert rows[0].details["review_id"] == result["review_id"]
    assert rows[0].metrics["task_count"] == 0


@pytest.mark.asyncio
async def test_outcome_evaluation_records_runtime_evidence(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_outcome_evidence",
        name="Outcome Evidence",
        status="active",
    )
    task = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Completed strategist task",
        status="completed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=10),
        details={"strategist_review_id": "rv_outcome_evidence"},
    )
    db_session.add_all([workspace, task])
    await db_session.commit()

    result = await evaluate_workspace_outcomes(db_session, workspace.id)
    await db_session.commit()

    rows = list(
        (
            await db_session.execute(
                select(RuntimeEvidence).where(
                    RuntimeEvidence.workspace_id == workspace.id,
                    RuntimeEvidence.evidence_type == "outcome_evaluation",
                )
            )
        )
        .scalars()
        .all()
    )
    assert result["labeled"] == 1
    assert result["by_label"] == {"untracked": 1}
    assert len(rows) == 1
    assert rows[0].source == "strategist"
    assert rows[0].summary == "Labeled 1 proposal(s) (untracked=1)"
    assert rows[0].details["by_label"] == {"untracked": 1}
    assert rows[0].metrics["labeled"] == 1


@pytest.mark.asyncio
async def test_scheduled_strategist_skips_active_work_batch(db_session, monkeypatch) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_batch_skip",
        name="Strategy Batch Skip",
        status="active",
    )
    open_task_id = generate_ulid()
    db_session.add(workspace)
    db_session.add(
        Task(
            id=open_task_id,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            title="Still running",
            status="in_progress",
        )
    )
    db_session.add(
        WorkspaceWorkBatch(
            id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            source_kind="strategist_proposal",
            status="active",
            task_ids=[open_task_id],
            details={},
        )
    )
    await db_session.commit()

    async def _should_not_generate(*args, **kwargs):
        raise AssertionError("scheduled review should skip active work batches")

    monkeypatch.setattr(service, "generate_proposal", _should_not_generate)

    result = await service.run_review(db_session, workspace.id, trigger="scheduled")

    assert result["skipped"] is True
    assert result["reason"] == "active_work_batch"
    assert result["open_task_ids"] == [open_task_id]


@pytest.mark.asyncio
async def test_strategist_auto_approves_proposals_when_workspace_pref_enabled(
    db_session,
    monkeypatch,
) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_auto_approve",
        name="Strategy Auto Approve",
        status="active",
        settings={"strategist": {"auto_approve_proposals": True}},
    )
    agent_id = generate_ulid()
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
            summary="Start the next operating packet.",
            tasks=[
                ProposedTask(
                    deliverables=[
                        Deliverable(
                            name="result",
                            kind="value",
                            shape="TextResult",
                            acceptance="task output produced",
                            usage="reviewed by operator",
                        )
                    ],
                    task_key="packet",
                    title="Prepare operating packet",
                    owner_service_key="ops",
                    priority=3,
                ),
            ],
            notes=None,
        )

    posted: list[dict] = []

    async def _fake_post_proposal_chat(workspace_arg, proposal, task_ids, *, auto_approved=False):
        posted.append(
            {
                "workspace_id": workspace_arg.id,
                "task_ids": list(task_ids),
                "auto_approved": auto_approved,
            }
        )

    monkeypatch.setattr(service, "generate_proposal", _fake_generate_proposal)
    monkeypatch.setattr(service, "_post_proposal_chat", _fake_post_proposal_chat)

    result = await service.run_review(db_session, workspace.id, trigger="manual")

    assert result["auto_approved"] is True
    assert result["approved_task_ids"] == result["task_ids"]
    assert posted == [
        {
            "workspace_id": workspace.id,
            "task_ids": result["task_ids"],
            "auto_approved": True,
        }
    ]
    tasks = list((await db_session.execute(select(Task).where(Task.workspace_id == workspace.id))).scalars().all())
    assert len(tasks) == 1
    assert tasks[0].status == "in_progress"
    assert tasks[0].owner_service_key == "ops"


@pytest.mark.asyncio
async def test_approve_proposal_creates_work_batch(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_batch_approve",
        name="Strategy Batch Approve",
        status="active",
    )
    review_id = "rv_batch_approve"
    task_ids = [generate_ulid(), generate_ulid()]
    db_session.add(workspace)
    for task_id in task_ids:
        db_session.add(
            Task(
                id=task_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title=f"Proposal task {task_id[-4:]}",
                status="proposed",
                details={"strategist_review_id": review_id},
            )
        )
    await db_session.commit()

    moved = await service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=review_id,
    )
    await db_session.commit()

    assert set(moved) == set(task_ids)
    tasks = list(
        (await db_session.execute(select(Task).where(Task.id.in_(task_ids)).order_by(Task.id))).scalars().all()
    )
    batch_ids = {task.details.get("workspace_work_batch_id") for task in tasks}
    assert {task.status for task in tasks} == {"in_progress"}
    assert len(batch_ids) == 1
    batch_id = next(iter(batch_ids))
    batch = (await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.id == batch_id))).scalar_one()
    assert batch.source_kind == "strategist_proposal"
    assert set(batch.task_ids) == set(task_ids)
    assert batch.details["strategist_review_id"] == review_id
    activity_events = list(
        (
            await db_session.execute(
                select(WorkspaceActivity.event_type, WorkspaceActivity.details).where(
                    WorkspaceActivity.workspace_id == workspace.id,
                    WorkspaceActivity.entity_id == workspace.entity_id,
                )
            )
        ).all()
    )
    event_types = {event_type for event_type, _ in activity_events}
    assert "workspace_work_batch.started" in event_types
    assert "strategist_proposal.approved" in event_types
    assert any(
        details.get("batch_id") == batch_id and set(details.get("task_ids") or []) == set(task_ids)
        for _, details in activity_events
    )


@pytest.mark.asyncio
async def test_strategist_persists_task_dependency_ids(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_dependencies",
        name="Strategy Dependencies",
        status="active",
    )
    proposal = Proposal(
        review_id="rv_deps",
        summary="Build docs before publishing.",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="draft_docs",
                title="Draft source docs",
                owner_service_key="content",
            ),
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="publish_calendar",
                title="Publish calendar",
                owner_service_key="content",
                depends_on_task_keys=["draft_docs"],
            ),
        ],
    )
    db_session.add(workspace)
    await db_session.flush()

    task_ids = await service._persist_tasks(db_session, workspace, proposal)
    await db_session.commit()

    tasks = list(
        (await db_session.execute(select(Task).where(Task.id.in_(task_ids)).order_by(Task.title))).scalars().all()
    )
    by_title = {task.title: task for task in tasks}
    assert by_title["Draft source docs"].details["strategist_task_key"] == "draft_docs"
    assert by_title["Publish calendar"].details["depends_on_task_keys"] == ["draft_docs"]
    assert by_title["Publish calendar"].details["depends_on_task_ids"] == [by_title["Draft source docs"].id]


@pytest.mark.asyncio
async def test_strategist_persists_goal_task_links_from_estimated_impact(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id="ent_strategy_goal_links",
        name="Strategy Goal Links",
        status="active",
    )
    goal = Goal(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Increase weekly tours",
        metric_key="tour_count",
        target_value=10,
        status="active",
    )
    proposal = Proposal(
        review_id="rv_goal_links",
        summary="Move the tour goal.",
        tasks=[
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="recommend_units",
                title="Recommend units to qualified leads",
                owner_service_key="unit_recommendation",
                estimated_impact=EstimatedImpact(
                    goal_id=goal.id,
                    metric_delta=2.5,
                    rationale="More recommendations should create more tours.",
                ),
            ),
            ProposedTask(
                deliverables=[
                    Deliverable(
                        name="result",
                        kind="value",
                        shape="TextResult",
                        acceptance="task output produced",
                        usage="reviewed by operator",
                    )
                ],
                task_key="invalid_goal",
                title="Ignore hallucinated goal link",
                owner_service_key="unit_recommendation",
                estimated_impact=EstimatedImpact(
                    goal_id=generate_ulid(),
                    metric_delta=99,
                ),
            ),
        ],
    )
    db_session.add_all([workspace, goal])
    await db_session.flush()

    task_ids = await service._persist_tasks(db_session, workspace, proposal)
    await db_session.commit()

    links = list(
        (await db_session.execute(select(GoalTaskLink).where(GoalTaskLink.goal_id == goal.id))).scalars().all()
    )
    assert len(links) == 1
    linked_task = (await db_session.execute(select(Task).where(Task.id == links[0].task_id))).scalar_one()
    assert linked_task.id in task_ids
    assert linked_task.title == "Recommend units to qualified leads"
    assert float(links[0].estimated_impact) == pytest.approx(2.5)
    assert links[0].contribution == "direct"


@pytest.mark.asyncio
async def test_approve_proposal_gates_dependent_tasks_until_outputs_exist(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services.task_service import update_task
    from packages.core.tasks import ai_tasks

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Strategy Dependency Gate",
        status="active",
    )
    review_id = "rv_dependency_gate"
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    dispatched: list[str] = []

    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: dispatched.append(task_id))

    db_session.add(workspace)
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Draft strategy document",
                status="proposed",
                details={"strategist_review_id": review_id, "strategist_task_key": "draft_strategy"},
            ),
            Task(
                id=task_b_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Use strategy document for calendar",
                status="proposed",
                owner_service_key="content",
                details={
                    "strategist_review_id": review_id,
                    "strategist_task_key": "calendar",
                    "depends_on_task_keys": ["draft_strategy"],
                    "depends_on_task_ids": [task_a_id],
                },
            ),
        ]
    )
    await db_session.commit()

    moved = await service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=review_id,
    )
    await db_session.commit()
    assert set(moved) == {task_a_id, task_b_id}

    task_a = (await db_session.execute(select(Task).where(Task.id == task_a_id))).scalar_one()
    task_b = (await db_session.execute(select(Task).where(Task.id == task_b_id))).scalar_one()
    assert task_a.status == "in_progress"
    assert task_b.status == "pending"
    assert task_b.details["dependency_status"] == "waiting"
    assert dispatched == []

    await update_task(
        db_session,
        task_a_id,
        workspace.entity_id,
        status="completed",
        actual_output={
            "summary": "Strategy doc is ready.",
            "files": [{"name": "strategy.md", "fs_path": "/workspace/strategy.md"}],
        },
    )
    await db_session.commit()

    task_b = (await db_session.execute(select(Task).where(Task.id == task_b_id))).scalar_one()
    assert task_b.status == "in_progress"
    assert task_b.details["dependency_status"] == "completed"
    assert task_b.details["dep_outputs"][0]["task_id"] == task_a_id
    assert task_b.details["dep_outputs"][0]["files"][0]["name"] == "strategy.md"
    assert dispatched == [task_b_id]


@pytest.mark.asyncio
async def test_approve_selected_includes_required_predecessors(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Selected Dependency Closure",
        status="active",
    )
    review_id = "rv_selected_dependency_closure"
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    task_c_id = generate_ulid()

    db_session.add(workspace)
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Prepare source artifact",
                status="proposed",
                details={"strategist_review_id": review_id, "strategist_task_key": "source"},
            ),
            Task(
                id=task_b_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Use source artifact",
                status="proposed",
                details={
                    "strategist_review_id": review_id,
                    "strategist_task_key": "downstream",
                    "depends_on_task_ids": [task_a_id],
                },
            ),
            Task(
                id=task_c_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Optional unrelated task",
                status="proposed",
                details={"strategist_review_id": review_id, "strategist_task_key": "optional"},
            ),
        ]
    )
    await db_session.commit()

    moved = await service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=review_id,
        only_task_ids=[task_b_id],
    )
    await db_session.commit()

    tasks = list(
        (await db_session.execute(select(Task).where(Task.id.in_([task_a_id, task_b_id, task_c_id])))).scalars().all()
    )
    by_id = {task.id: task for task in tasks}

    assert set(moved) == {task_a_id, task_b_id}
    assert by_id[task_a_id].status == "in_progress"
    assert by_id[task_b_id].status == "pending"
    assert by_id[task_b_id].details["dependency_status"] == "waiting"
    assert by_id[task_c_id].status == "proposed"


@pytest.mark.asyncio
async def test_dependency_gate_blocks_manual_start_before_outputs(db_session) -> None:
    from packages.core.services.task_service import move_task, update_task
    from packages.core.services.task_state_machine import TaskStatusTransitionError

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Manual Dependency Gate",
        status="active",
    )
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    db_session.add(workspace)
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Prepare source artifact",
                status="in_progress",
            ),
            Task(
                id=task_b_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Use source artifact",
                status="pending",
                details={"depends_on_task_ids": [task_a_id]},
                owner_service_key="content",
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(TaskStatusTransitionError, match="dependencies are not completed"):
        await update_task(db_session, task_b_id, workspace.entity_id, status="in_progress")
    with pytest.raises(TaskStatusTransitionError, match="dependencies are not completed"):
        await move_task(db_session, task_b_id, workspace.entity_id, "in_progress")

    task_b = (await db_session.execute(select(Task).where(Task.id == task_b_id))).scalar_one()
    assert task_b.status == "pending"


@pytest.mark.asyncio
async def test_dependency_gate_blocks_manual_retry_before_outputs(db_session) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from apps.api.routers.tasks import retry_task_endpoint

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Retry Dependency Gate",
        status="active",
    )
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    db_session.add(workspace)
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Prepare source artifact",
                status="failed",
            ),
            Task(
                id=task_b_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Use source artifact",
                status="blocked",
                details={"depends_on_task_ids": [task_a_id]},
                owner_service_key="content",
            ),
        ]
    )
    await db_session.commit()

    user = SimpleNamespace(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        display_name="Tester",
        email="tester@example.com",
    )
    with pytest.raises(HTTPException) as exc_info:
        await retry_task_endpoint(task_b_id, None, user, db_session)

    assert exc_info.value.status_code == 409
    assert "dependencies are not completed" in str(exc_info.value.detail)
    task_b = (await db_session.execute(select(Task).where(Task.id == task_b_id))).scalar_one()
    assert task_b.status == "blocked"


@pytest.mark.asyncio
async def test_dependency_gate_blocks_retry_before_resetting_plan_steps(db_session) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from apps.api.routers.tasks import retry_task_endpoint
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Retry Step Gate",
        status="active",
    )
    task_a_id = generate_ulid()
    task_b_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    db_session.add(workspace)
    db_session.add_all(
        [
            Task(
                id=task_a_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Prepare source artifact",
                status="failed",
            ),
            Task(
                id=task_b_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title="Use source artifact",
                status="blocked",
                details={"depends_on_task_ids": [task_a_id]},
                owner_service_key="content",
            ),
            ExecutionPlan(
                id=plan_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                task_id=task_b_id,
                status="failed",
                plan_dag={"steps": []},
            ),
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                step_key="failed_step",
                kind="llm",
                params={},
                depends_on=[],
                step_status="failed",
                error={"message": "boom"},
                attempt_count=2,
            ),
        ]
    )
    await db_session.commit()

    user = SimpleNamespace(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        display_name="Tester",
        email="tester@example.com",
    )
    with pytest.raises(HTTPException) as exc_info:
        await retry_task_endpoint(task_b_id, None, user, db_session)

    assert exc_info.value.status_code == 409
    step = (await db_session.execute(select(ExecutionStep).where(ExecutionStep.id == step_id))).scalar_one()
    plan = (await db_session.execute(select(ExecutionPlan).where(ExecutionPlan.id == plan_id))).scalar_one()
    assert step.step_status == "failed"
    assert step.error == {"message": "boom"}
    assert step.attempt_count == 2
    assert plan.status == "failed"


@pytest.mark.asyncio
async def test_dependency_gate_blocks_missing_predecessor_ids(db_session) -> None:
    from packages.core.services.task_dependencies import dependency_status

    status, statuses = await dependency_status(
        db_session,
        entity_id=generate_ulid(),
        dependency_ids=[generate_ulid()],
    )

    assert status == "blocked"
    assert statuses == {}
