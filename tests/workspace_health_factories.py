"""Row builders shared by the Workspace health roll-up and snapshot suites.

Each builder writes the minimal real rows (required columns only) and
flushes, so the services under test read them through their own queries.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Iterable, Optional

from packages.core.blueprints.workflow_patterns import schedule_expansion_flows
from packages.core.models.base import generate_ulid
from packages.core.models.billing import CreditUsageLog
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import User
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_run_trace import build_definition_snapshot


async def _workspace(
    db, *, blueprint: Optional[tuple[str, str]] = None, sandbox: bool = False,
    status: str = "active",
) -> Workspace:
    settings: dict = {"sandbox": sandbox}
    if blueprint is not None:
        key, version = blueprint
        settings["_blueprint"] = {"blueprint_id": key, "blueprint_version": version}
    ws = Workspace(
        id=generate_ulid(), entity_id=generate_ulid(), name="Health", status=status,
        settings=settings,
    )
    db.add(ws)
    await db.flush()
    return ws


async def _tasks(
    db, ws: Workspace, specs: Iterable[tuple[str, Optional[str], Optional[int]]], *,
    finished_at: datetime,
) -> list[Task]:
    """``specs`` are (status, cancel_source, seconds); ``seconds`` sets
    ``started_at`` that long before ``finished_at``."""
    rows = [
        Task(
            id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id, title=status,
            status=status, cancel_source=cancel_source, details={},
            started_at=finished_at - timedelta(seconds=seconds) if seconds is not None else None,
            completed_at=finished_at, status_changed_at=finished_at,
            created_at=finished_at - timedelta(hours=1),
        )
        for status, cancel_source, seconds in specs
    ]
    db.add_all(rows)
    await db.flush()
    return rows


async def _reviews(
    db, ws: Workspace, specs: Iterable[tuple[str, str, bool]], *, at: datetime,
) -> list[HitlRequest]:
    """``specs`` are (status, hitl_type, decided_by_a_person)."""
    rows = [
        HitlRequest(
            id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
            origin_kind="plan_step", dedup_key=generate_ulid(), status=status,
            hitl_type=hitl_type, created_at=at - timedelta(hours=1), updated_at=at,
            decided_at=at if decided else None,
            decided_by_user_id=generate_ulid() if decided else None,
        )
        for status, hitl_type, decided in specs
    ]
    db.add_all(rows)
    await db.flush()
    return rows


async def _feedback(db, ws: Workspace, ratings: Iterable[str], *, at: datetime) -> None:
    """Thumbs on Task completion receipts, one completed Task per rating."""
    user = User(
        id=generate_ulid(), entity_id=ws.entity_id, email=f"{generate_ulid()}@health.test",
        password_hash="x",
    )
    conversation = Conversation(id=generate_ulid(), entity_id=ws.entity_id)
    db.add_all([user, conversation])
    await db.flush()
    for rating in ratings:
        [task] = await _tasks(db, ws, [("completed", None, None)], finished_at=at)
        message = Message(
            id=generate_ulid(), conversation_id=conversation.id, role="assistant",
            content="Done", author_kind="agent", message_kind="agent_update",
        )
        db.add(message)
        await db.flush()
        db.add(ChatMessageFeedback(
            entity_id=ws.entity_id, user_id=user.id, conversation_id=conversation.id,
            message_id=message.id, target_kind="task_completion", target_id=task.id,
            task_id=task.id, rating=rating, mutation_sequence=1,
            created_at=at, updated_at=at,
        ))
    await db.flush()


async def _proposal_items(db, ws: Workspace, statuses: Iterable[str], *, at: datetime) -> None:
    proposal_id = generate_ulid()
    db.add_all([
        ProposalItemRecord(
            id=generate_ulid(), proposal_id=proposal_id, entity_id=ws.entity_id,
            workspace_id=ws.id, item_key=generate_ulid(), kind="task", payload={},
            action_key="task.create", status=status, decided_at=at,
        )
        for status in statuses
    ])
    await db.flush()


async def _credits(db, ws: Workspace, amounts: Iterable[int], *, at: datetime) -> None:
    db.add_all([
        CreditUsageLog(
            id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
            total_credit=amount, created_at=at,
        )
        for amount in amounts
    ])
    await db.flush()


def _worker_steps() -> list[dict]:
    """The steps of the real every-15-minutes worker a Workflow pattern builds."""
    expansion = schedule_expansion_flows(
        slug="posts", item_type="post", item_schema={"type": "object", "properties": {}}, date_field="date",
        timezone="UTC", lead_minutes=30, waiting_stages=["planned"], scheduled_stage="scheduled",
        started_stage="started", run_workflow="make_post", run_input="post_id",
    )
    return next(flow for flow in expansion["workflows"] if flow["slug"] == "posts_due")["steps"]


def _step_result(step_id: str, status: str = "completed", **fields) -> dict:
    """A result as ``WorkflowRunner._execute_step_safe`` records it."""
    return {
        "status": status, "output": "done", "duration_ms": 1.0, "completed_at": "2026-10-27T12:00:00+00:00",
        "step_id": step_id, "inputs": {}, **fields,
    }


async def _flow_run(
    db, ws: Workspace, steps: dict[str, str] | list[dict], results: Iterable[str] | dict[str, dict], *,
    finished_at: Optional[datetime], status: str = "completed", current: Optional[str] = None,
    error: Optional[str] = None, trigger_data: Optional[dict] = None, cancel_source: Optional[str] = None,
    attempt: int = 1, lineage_root: Optional[str] = None, parent=None, parent_step: str = "run",
) -> WorkflowRun:
    """One run with the ``definition_snapshot`` the service stores. ``steps`` maps
    step ids to types, or is a real step list; ``results`` are the steps that ran."""
    run_id = generate_ulid()
    if isinstance(steps, dict):
        steps = [{"id": step_id, "type": kind} for step_id, kind in steps.items()]
    results = results if isinstance(results, dict) else {step_id: _step_result(step_id) for step_id in results}
    trigger = dict(trigger_data or {})
    if parent is not None:
        # What ``_execute_subworkflow_step`` gives a child of a run_workflow start.
        trigger.update(parent_run_id=parent.id, parent_step_id=parent_step, _workflow_runtime_context={})
    workflow = SimpleNamespace(id=generate_ulid(), name="Flow", version=1, steps=steps)
    run = WorkflowRun(
        id=run_id, workflow_id=workflow.id, entity_id=ws.entity_id, workspace_id=ws.id, status=status,
        current_step_id=current or next(reversed(results), None), error=error, cancel_source=cancel_source,
        attempt_number=attempt, lineage_root_run_id=lineage_root or run_id, lineage_is_legacy=False,
        step_results=results, trigger_data=trigger, definition_snapshot=build_definition_snapshot(workflow),
        started_at=finished_at - timedelta(seconds=5) if finished_at else None, completed_at=finished_at,
    )
    db.add(run)
    await db.flush()
    return run


async def _job(db, ws: Workspace, enabled: bool, next_run_at: Optional[datetime]) -> ScheduledJob:
    job = ScheduledJob(
        id=generate_ulid(), job_id=f"job-{generate_ulid()}", entity_id=ws.entity_id,
        workspace_id=ws.id, name="Health job", enabled=enabled, consecutive_errors=0,
        next_run_at=next_run_at,
    )
    db.add(job)
    await db.flush()
    return job
