"""Service-side Strategist guard for duplicate active Workflow runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.core.constants.workflow import (
    WORKFLOW_RUN_OPEN_STATUSES,
    WORKFLOW_RUN_TERMINAL_STATUSES,
    WorkflowRunStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowBinding, WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.strategist.service import (
    _suppress_active_workflow_run_proposals,
)


@dataclass
class _WorkflowRef:
    blueprint_slug: str
    workflow_slug: str


@dataclass
class _WorkflowProposal:
    workflow_ref: _WorkflowRef


@dataclass
class _Proposal:
    workflow_runs: list[_WorkflowProposal] = field(default_factory=list)
    notes: str | None = None


def test_workflow_run_status_enum_partitions_the_lifecycle():
    assert set(WORKFLOW_RUN_OPEN_STATUSES) == {
        WorkflowRunStatus.PENDING,
        WorkflowRunStatus.RUNNING,
        WorkflowRunStatus.PAUSED,
    }
    assert set(WORKFLOW_RUN_TERMINAL_STATUSES) == {
        WorkflowRunStatus.COMPLETED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.CANCELLED,
    }
    assert str(WorkflowRunStatus.PAUSED) == "paused"


async def test_workflow_run_model_defaults_to_pending_enum_value(db_session):
    run = WorkflowRun(
        workflow_id=generate_ulid(),
        entity_id=generate_ulid(),
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_trace=[],
    )
    db_session.add(run)
    await db_session.flush()

    assert run.status == WorkflowRunStatus.PENDING


async def test_active_workflow_run_suppresses_only_matching_flow(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        entity_id=entity_id,
        name="Workflow dedupe",
        settings={"_blueprint": {"blueprint_slug": "stickman-studio"}},
    )
    db_session.add(workspace)
    await db_session.flush()

    workflow_id = generate_ulid()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow_id,
        workspace_id=workspace.id,
        config={"workspace_blueprint_workflow_slug": "daily-video"},
    )
    db_session.add(binding)
    await db_session.flush()
    active_run = WorkflowRun(
        workflow_id=workflow_id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        binding_id=binding.id,
        status="paused",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_trace=[],
    )
    db_session.add(active_run)
    await db_session.flush()

    matching = _WorkflowProposal(_WorkflowRef("stickman-studio", "daily-video"))
    unrelated = _WorkflowProposal(_WorkflowRef("stickman-studio", "metrics"))
    wrong_blueprint = _WorkflowProposal(_WorkflowRef("another-studio", "daily-video"))
    proposal = _Proposal(
        workflow_runs=[matching, unrelated, wrong_blueprint],
    )

    await _suppress_active_workflow_run_proposals(
        db_session,
        workspace,
        proposal,  # type: ignore[arg-type]
    )

    assert proposal.workflow_runs == [unrelated, wrong_blueprint]
    assert "already active" in (proposal.notes or "")
    assert active_run.id in (proposal.notes or "")


async def test_terminal_workflow_run_does_not_trigger_active_guard(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        entity_id=entity_id,
        name="Workflow terminal",
        settings={"_blueprint": {"blueprint_slug": "stickman-studio"}},
    )
    db_session.add(workspace)
    await db_session.flush()

    workflow_id = generate_ulid()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow_id,
        workspace_id=workspace.id,
        config={"workspace_blueprint_workflow_slug": "daily-video"},
    )
    db_session.add(binding)
    await db_session.flush()
    db_session.add(
        WorkflowRun(
            workflow_id=workflow_id,
            entity_id=entity_id,
            workspace_id=workspace.id,
            binding_id=binding.id,
            status="failed",
            variables={},
            step_results={},
            trigger_data={},
            definition_snapshot={},
            execution_trace=[],
        )
    )
    await db_session.flush()

    item = _WorkflowProposal(_WorkflowRef("stickman-studio", "daily-video"))
    proposal = _Proposal(workflow_runs=[item])
    await _suppress_active_workflow_run_proposals(
        db_session,
        workspace,
        proposal,  # type: ignore[arg-type]
    )

    assert proposal.workflow_runs == [item]
    assert proposal.notes is None
