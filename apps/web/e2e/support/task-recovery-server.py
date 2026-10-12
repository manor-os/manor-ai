"""Disposable loopback API for task-recovery-live.spec.ts.

Real routers, authorization, PostgreSQL and Celery publication; no worker or
external provider runs. Only prerequisite historical Task/Plan rows are seeded.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import time
from pathlib import Path

DATABASE_NAME = f"manor_task_browser_{int(time.time())}_test"
ROOT = Path(tempfile.mkdtemp(prefix="manor-task-browser-"))
os.environ.update({
    "DEPLOYMENT_MODE": "oss",
    "DATABASE_URL": f"postgresql+asyncpg://manor:manor_secret@127.0.0.1:5434/{DATABASE_NAME}",
    "DATABASE_URL_SYNC": f"postgresql://manor:manor_secret@127.0.0.1:5434/{DATABASE_NAME}",
    "JWT_SECRET_KEY": secrets.token_urlsafe(48),
    "MANOR_FS_ENABLED": "true",
    "MANOR_FS_ROOT": str(ROOT / "files"),
    "EMAIL_ENABLED": "false",
    "CELERY_BROKER_URL": "memory://",
    "CELERY_RESULT_BACKEND": "cache+memory://",
    "REDIS_URL": "redis://127.0.0.1:6389/14",
    "APP_URL": "http://127.0.0.1:18921",
})

from celery.signals import before_task_publish  # noqa: E402
from fastapi import Depends  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from apps.api.deps import get_current_user  # noqa: E402
from apps.api.main import app  # noqa: E402
from packages.core.database import async_session, engine  # noqa: E402
from packages.core.constants.supervisor import SupervisorDecision, SupervisorDecisionSource, SupervisorVerdict  # noqa: E402
from packages.core.models.base import Base  # noqa: E402
from packages.core.models.execution import ExecutionPlan, ExecutionStep  # noqa: E402
from packages.core.models.task import Message, Task  # noqa: E402
from packages.core.models.workspace import Agent, AgentSubscription, Workspace  # noqa: E402
from packages.core.plans.executor import PlanExecutor  # noqa: E402
from packages.core.services.demo_account import ensure_demo_account  # noqa: E402
from packages.core.services.task_chat_hitl import ensure_task_approval_hitl, ensure_task_recovery_hitl  # noqa: E402

PUBLISHED = []


@before_task_publish.connect
def capture_publication(sender=None, body=None, **kwargs):
    PUBLISHED.append({"name": sender, "args": list(body[0]) if body else []})


async def initialize():
    import asyncpg

    connection = await asyncpg.connect("postgresql://manor:manor_secret@127.0.0.1:5434/manor")
    try:
        await connection.execute(f'CREATE DATABASE "{DATABASE_NAME}"')
    finally:
        await connection.close()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
        await connection.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
    async with async_session() as db:
        await ensure_demo_account(db)
        await db.commit()
    await engine.dispose()


@app.post("/__qa/fixtures")
async def fixture(payload: dict, user=Depends(get_current_user)):
    kind = payload.get("kind", "recovery")
    async with async_session() as db:
        workspace = Workspace(entity_id=user.entity_id, name=f"Browser QA {kind}", status="paused")
        db.add(workspace)
        await db.flush()
        agent = Agent(entity_id=user.entity_id, name="QA Writer", status="active")
        db.add(agent)
        await db.flush()
        sub = AgentSubscription(entity_id=user.entity_id, workspace_id=workspace.id,
                                agent_id=agent.id, service_key="content_creation", status="active")
        db.add(sub)
        task = Task(entity_id=user.entity_id, workspace_id=workspace.id,
                    creator_id=user.id, title=f"Browser QA {kind}",
                    description="Local routing verification only. No external publication authorized.",
                    status="pending" if kind == "initial" else "waiting_on_customer",
                    task_type="approval" if kind == "approval" else "general")
        db.add(task)
        await db.flush()
        plan = None
        card = None
        if kind in {"recovery", "unavailable", "cancel"}:
            if kind == "unavailable":
                sub.status = "inactive"
            plan = ExecutionPlan(entity_id=user.entity_id, workspace_id=workspace.id,
                                 task_id=task.id, status="completed", plan_dag={"steps": [{
                                     "key": "publish_post", "kind": "subagent",
                                     "service_key": "content_creation", "params": {"prompt": "Prepare draft"},
                                     "output_shape": "TextResult", "depends_on": [],
                                 }]})
            db.add(plan)
            await db.flush()
            # Match the executor's persisted business verdict. A mechanically
            # completed Plan is not accepted publication; GET reconciliation
            # must retain the Supervisor's needs-human decision.
            task.actual_output = {
                "plan_id": plan.id, "plan_status": "completed",
                "supervisor_verdict": "needs_human",
                "supervisor_evidence": "publish_post 仅产出草稿，没有发布回执。",
            }
            db.add(ExecutionStep(plan_id=plan.id, entity_id=user.entity_id, workspace_id=workspace.id,
                                 step_key="publish_post", kind="subagent", service_key="content_creation",
                                 params={"prompt": "Prepare draft"}, depends_on=[], step_status="done",
                                 result={"text": "Draft only; no publication receipt."}))
            await db.flush()
            await PlanExecutor._log_supervisor_verdict(db, task, plan, SupervisorDecision(
                verdict=SupervisorVerdict.NEEDS_HUMAN,
                evidence="publish_post 仅产出草稿，没有发布回执。",
                source=SupervisorDecisionSource.GATE,
            ))
            card = await ensure_task_recovery_hitl(
                db, task, plan_id=plan.id, prompt="This task stopped before it finished.",
                issue="publish_post 仅产出草稿，没有发布回执。请确认目标账号与发布审批。",
            )
        elif kind == "approval":
            card = await ensure_task_approval_hitl(db, task)
        await db.commit()
        return {"task_id": task.id, "workspace_id": workspace.id,
                "message_id": card.id if card else None, "plan_id": plan.id if plan else None}


@app.get("/__qa/state/{task_id}")
async def state(task_id: str, user=Depends(get_current_user)):
    async with async_session() as db:
        task = await db.get(Task, task_id)
        if task is None or task.entity_id != user.entity_id:
            return {"error": "not found"}
        plans = list((await db.execute(select(ExecutionPlan).where(ExecutionPlan.task_id == task_id))).scalars())
        cards = list((await db.execute(select(Message).where(Message.meta["task_id"].astext == task_id))).scalars())
        return {"status": task.status, "owner": task.owner_subscription_id,
                "details": task.details, "plans": [{"id": p.id, "status": p.status} for p in plans],
                "cards": [{"id": m.id, "resolved": m.resolved_at is not None} for m in cards],
                "published": [p for p in PUBLISHED if task_id in p["args"]]}


if __name__ == "__main__":
    import uvicorn

    asyncio.run(initialize())
    print(f"Task browser QA: database={DATABASE_NAME}; files={ROOT}; worker=disabled", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=18920, lifespan="off")
