from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
from packages.core.models.task import Task
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.workspace import Workspace
from packages.core.ai.runtime import runtime_strategist_user_prompt
from packages.core.services.workspace_evaluation import (
    build_workspace_evaluation,
    format_workspace_evaluation_for_prompt,
    record_workspace_evaluation_snapshot,
)
from packages.core.services.runtime_learning import list_runtime_evidence


@pytest.mark.asyncio
async def test_workspace_evaluation_rolls_up_runtime_dimensions(db_session) -> None:
    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    goal_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    approval_id = generate_ulid()

    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Evaluation Workspace",
        monthly_spent_usd=Decimal("0.50"),
        monthly_budget_usd=Decimal("2.00"),
        auto_pause_on_budget=True,
        budget_alert_state="normal",
    )
    goal = Goal(
        id=goal_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Ship evaluated work",
        metric_key="deliverables",
        baseline_value=Decimal("0"),
        current_value=Decimal("4"),
        target_value=Decimal("10"),
        current_value_updated_at=now,
        pace_status="on_track",
        measurement_source={"provider": "workspace_internal"},
        measurement_cadence="daily",
        status="active",
    )
    measurement = GoalMeasurement(
        goal_id=goal_id,
        measured_at=now - timedelta(hours=1),
        value=Decimal("4"),
        source="workspace_internal",
        meta={"evidence": "linked tasks"},
    )
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Create launch deliverable",
        status="completed",
        priority=3,
        task_type="analysis",
        started_at=now - timedelta(hours=3),
        completed_at=now - timedelta(hours=1),
        actual_output={"files": [{"path": "/tmp/launch.pdf"}]},
    )
    link = GoalTaskLink(
        goal_id=goal_id,
        task_id=task_id,
        contribution="direct",
        estimated_impact=Decimal("4"),
        actual_impact=Decimal("4"),
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        plan_dag={"steps": []},
        status="completed",
        execution_mode="live",
        cost_tracking={"usd": 0.25},
        started_at=now - timedelta(hours=3),
        completed_at=now - timedelta(hours=1),
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        step_key="draft_report",
        kind="llm",
        service_key="ops",
        params={"prompt": "draft"},
        result={"text": "done"},
        depends_on=[],
        step_status="done",
        cost={"usd": 0.25, "llm_tokens_input": 100, "llm_tokens_output": 40},
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=1),
    )
    approval = RuntimeEvidence(
        id=approval_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        evidence_type="approval_decision",
        source="task_ui",
        status="succeeded",
        summary="Operator approved the deliverable.",
        details={"approved": True, "choice": "approve"},
        metrics={"approved": 1, "cost_usd": 0.01},
    )
    external_approval = RuntimeEvidence(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        evidence_type="external_message_decision",
        source="workspace_chat",
        status="succeeded",
        summary="Operator approved an external reply.",
        details={"choice": "approve", "pending_action_kind": "external_message_approval"},
        metrics={"approved": 1},
    )
    candidate = AgentLearningCandidate(
        entity_id=entity_id,
        workspace_id=workspace_id,
        candidate_type="memory",
        scope="workspace",
        title="Reuse launch deliverable format",
        summary="The deliverable format was approved.",
        payload={"content": "Use launch format."},
        evidence_ids=[approval_id],
        risk_level="low",
        status="applied",
        confidence=0.8,
        created_by="runtime",
    )
    usage = TokenUsageLog(
        entity_id=entity_id,
        workspace_id=workspace_id,
        model="test-model",
        provider="test",
        prompt_tokens=100,
        completion_tokens=40,
        total_tokens=140,
        cost_usd=Decimal("0.03"),
        source="strategist",
    )
    tool = ToolCallLog(
        entity_id=entity_id,
        workspace_id=workspace_id,
        tool_name="workspace_search",
        source="agent",
        duration_ms=50,
        result_chars=20,
        success=True,
    )

    db_session.add_all(
        [
            workspace,
            goal,
            measurement,
            task,
            link,
            plan,
            step,
            approval,
            external_approval,
            candidate,
            usage,
            tool,
        ]
    )
    await db_session.commit()

    snapshot = await build_workspace_evaluation(
        db_session,
        workspace_id,
        entity_id=entity_id,
        window_days=30,
        now=now,
    )

    assert snapshot["overall"]["score"] is not None
    assert snapshot["dimensions"]["goal_impact"]["aggregate"]["average_progress_pct"] == 40.0
    assert snapshot["dimensions"]["execution_health"]["completed_task_count"] == 1
    assert snapshot["dimensions"]["output_quality"]["approval_count"] == 2
    assert snapshot["dimensions"]["output_quality"]["approval_rate"] == 1.0
    assert snapshot["dimensions"]["user_feedback"]["positive_signal_count"] == 2
    assert snapshot["dimensions"]["cost_efficiency"]["window_credits"] > 0
    assert snapshot["dimensions"]["learning"]["candidate_status_counts"]["applied"] == 1
    assert snapshot["history"] == []
    assert snapshot["trend"]["direction"] == "unknown"

    evidence = await record_workspace_evaluation_snapshot(
        db_session,
        snapshot,
        entity_id=entity_id,
        workspace_id=workspace_id,
        source="test",
        trace_id="rv_eval_history",
    )
    await db_session.commit()

    next_snapshot = await build_workspace_evaluation(
        db_session,
        workspace_id,
        entity_id=entity_id,
        window_days=30,
        now=now,
    )
    assert next_snapshot["history"][0]["evidence_id"] == evidence.id
    assert next_snapshot["history"][0]["overall_score"] == snapshot["overall"]["score"]
    assert next_snapshot["trend"]["previous_score"] == snapshot["overall"]["score"]
    assert next_snapshot["trend"]["direction"] == "flat"
    assert (
        next_snapshot["dimensions"]["learning"]["runtime_evidence_count"]
        == snapshot["dimensions"]["learning"]["runtime_evidence_count"]
    )
    default_evidence = await list_runtime_evidence(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        limit=20,
    )
    assert evidence.id not in {row.id for row in default_evidence}
    explicit_history_evidence = await list_runtime_evidence(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        evidence_type="workspace_evaluation_snapshot",
        limit=20,
    )
    assert [row.id for row in explicit_history_evidence] == [evidence.id]


@pytest.mark.asyncio
async def test_strategist_review_records_workspace_evaluation_snapshot(db_session) -> None:
    from packages.core.strategist.service import _record_strategist_review_evidence

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Strategist Evaluation History",
    )
    db_session.add(workspace)
    await db_session.commit()

    evaluation_snapshot = {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"days": 30, "start": "2026-05-01T00:00:00+00:00", "end": "2026-05-02T00:00:00+00:00"},
        "overall": {"score": 81, "confidence": "medium", "summary": "healthy", "weights": {}},
        "dimensions": {"goal_impact": {"score": 80, "summary": "moving"}},
        "recommendations": ["Continue current loop."],
        "evidence_summary": {"runtime_evidence_count": 3},
        "history": [],
        "trend": {"direction": "unknown", "delta": None, "previous_score": None},
    }
    ctx = SimpleNamespace(
        goals=[],
        recent_tasks=[],
        recent_plans=[],
        recent_runtime_evidence=[],
        learning_candidates=[],
        open_proposed_tasks=[],
        missing_setup=[],
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy={},
        workspace_evaluation=evaluation_snapshot,
    )
    proposal = SimpleNamespace(
        review_id="rv_" + generate_ulid(),
        summary="Create the next operating wave.",
        notes="",
        tasks=[],
    )

    await _record_strategist_review_evidence(
        db_session,
        workspace=workspace,
        proposal=proposal,
        trigger="scheduled",
        task_ids=[],
        ctx=ctx,
    )
    await db_session.commit()

    rows = list(
        (await db_session.execute(select(RuntimeEvidence).where(RuntimeEvidence.workspace_id == workspace.id)))
        .scalars()
        .all()
    )
    by_type = {row.evidence_type: row for row in rows}
    assert by_type["workspace_evaluation_snapshot"].metrics["overall_score"] == 81
    strategist = by_type["strategist_review"]
    assert strategist.details["input_snapshot"]["workspace_evaluation_score"] == 81
    assert (
        strategist.details["input_snapshot"]["workspace_evaluation_evidence_id"]
        == by_type["workspace_evaluation_snapshot"].id
    )


def test_workspace_evaluation_formats_for_strategist_prompt() -> None:
    snapshot = {
        "overall": {"score": 74, "confidence": "medium"},
        "dimensions": {
            "goal_impact": {"score": 33, "summary": "1/3 goals measured"},
            "cost_efficiency": {"score": 78, "summary": "57 credits"},
            "time_efficiency": {"score": 64, "summary": "waiting on review"},
            "execution_health": {"score": 86, "summary": "healthy DAG"},
            "output_quality": {"score": 75, "summary": "3 approvals"},
            "user_feedback": {"score": 82, "summary": "positive signals"},
            "governance": {"score": 100, "summary": "no violations"},
            "learning": {"score": 70, "summary": "2 candidates"},
        },
        "recommendations": ["Review proposed learning candidates."],
    }

    text = format_workspace_evaluation_for_prompt(snapshot)

    assert "Overall score: 74%" in text
    assert "Goal impact: 33%" in text
    assert "Review proposed learning candidates" in text


def test_strategist_prompt_includes_workspace_evaluation_scorecard() -> None:
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
        recent_activity=[],
        recent_runtime_evidence=[],
        learning_candidates=[],
        operating_memory="",
        relevant_memory=[],
        open_proposed_tasks=[],
        recent_proposal_outcomes={},
        calibration={},
        workspace_evaluation={
            "overall": {"score": 88, "confidence": "high"},
            "dimensions": {
                "goal_impact": {"score": 80, "summary": "goals moving"},
                "cost_efficiency": {"score": 90, "summary": "cheap"},
                "time_efficiency": {"score": 85, "summary": "fast"},
                "execution_health": {"score": 92, "summary": "stable"},
                "output_quality": {"score": 86, "summary": "approved"},
                "user_feedback": {"score": 75, "summary": "some feedback"},
                "governance": {"score": 100, "summary": "safe"},
                "learning": {"score": 70, "summary": "learning"},
            },
            "recommendations": [],
        },
    )

    text = runtime_strategist_user_prompt(ctx, review_id="rv_eval")

    assert "# Workspace evaluation scorecard" in text
    assert "Overall score: 88%" in text
    assert "Execution health: 92%" in text
