"""Crash recovery must settle known lost ownership without replaying steps."""
from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.execution_claim import RuntimeExecutionClaim
from packages.core.models.workflow import WorkflowRun
from packages.core.services import workflow_run_execution_claim as claims
from packages.core.tasks import ai_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "renewed", "replaced", "deleted"])
async def test_expired_token_takeover_is_atomic(client, db_session, change):
    now = datetime.now(timezone.utc)
    key = "workflow-run:interrupted"
    old_token, new_token = "0" * 26, "1" * 26
    row = RuntimeExecutionClaim(
        claim_key=key, claim_token=old_token,
        expires_at=now - timedelta(seconds=1),
    )
    db_session.add(row)
    await db_session.commit()
    if change == "renewed":
        row.expires_at = now + timedelta(minutes=10)
    elif change == "replaced":
        row.claim_token = "2" * 26
    elif change == "deleted":
        await db_session.delete(row)
    await db_session.commit()

    acquired = await claims._acquire_postgres_execution_claim(
        key, new_token, lease_ttl_seconds=60, expired_claim_token=old_token,
    )
    assert acquired is (change == "unchanged")
    if change != "deleted":
        await db_session.refresh(row)
    if change == "unchanged":
        assert row.claim_token == new_token
        assert row.expires_at > now
    elif change == "replaced":
        assert row.claim_token == "2" * 26
    elif change == "renewed":
        assert row.claim_token == old_token


@pytest.mark.asyncio
async def test_sweep_only_selects_running_with_expired_ownership(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    now = datetime.now(timezone.utc)
    cases = [
        ("expired", "running", -1),
        ("live", "running", 600),
        ("pending", "pending", -1),
        ("paused", "paused", -1),
        ("approval", "waiting_approval", -1),
        ("done", "completed", -1),
        ("legacy", "running", None),
    ]
    for run_id, status, ttl in cases:
        db_session.add(WorkflowRun(
            id=run_id, workflow_id="workflow", entity_id="entity", status=status,
        ))
        if ttl is not None:
            db_session.add(RuntimeExecutionClaim(
                claim_key=f"workflow-run:{run_id}", claim_token="0" * 26,
                expires_at=now + timedelta(seconds=ttl),
            ))
    await db_session.commit()
    seen = []

    async def settle(run_id, **kwargs):
        seen.append((run_id, kwargs["expired_claim_token"]))
        return "failed"

    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)
    monkeypatch.setattr(WorkflowRunner, "_fail_lost_execution_claim", settle)
    assert await ai_tasks._recover_expired_workflow_claims() == {"checked": 1, "settled": 1}
    assert seen == [("expired", "0" * 26)]


@pytest.mark.asyncio
async def test_expired_run_settles_once_and_late_delivery_cannot_execute(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session
    from packages.core.models.user import Entity

    now = datetime.now(timezone.utc)
    db_session.add(Entity(id="entity", name="Recovery test"))
    await db_session.commit()
    run = WorkflowRun(
        id="interrupted-run", workflow_id="missing-workflow", entity_id="entity",
        status="running", current_step_id="produce", started_at=now,
        step_results={"evidence": {"status": "completed", "output": "accepted"}},
    )
    db_session.add(run)
    db_session.add(RuntimeExecutionClaim(
        claim_key="workflow-run:interrupted-run", claim_token="0" * 26,
        expires_at=now - timedelta(seconds=1),
    ))
    await db_session.commit()
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: async_session)
    # The entity has no Chat origin or configured error workflows. The normal
    # lifecycle still commits its failed state and drains terminal effects.
    assert await ai_tasks._recover_expired_workflow_claims() == {"checked": 1, "settled": 1}
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error == "Workflow execution claim was lost"
    assert run.completed_at is not None
    assert run.step_results["evidence"]["output"] == "accepted"
    assert run.terminal_effects_completed_at is not None

    async def forbidden(*args, **kwargs):
        pytest.fail("late delivery replayed an interrupted step")

    monkeypatch.setattr(WorkflowRunner, "_run_loop", forbidden)
    await WorkflowRunner(session_factory=async_session).run(run.id)
    assert await ai_tasks._recover_expired_workflow_claims() == {"checked": 0, "settled": 0}


def test_recovery_beat_has_a_control_consumer_and_expires():
    from packages.core.celery_app import celery_app
    from packages.core.queues import CeleryQueue, queue_for_task

    entry = celery_app.conf.beat_schedule["workflow-expired-claim-recovery"]
    assert entry["task"] == "workflow.recover_expired_claims"
    assert queue_for_task(entry["task"]) is CeleryQueue.CONTROL
    assert entry["options"]["expires"] <= entry["schedule"]
