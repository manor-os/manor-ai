"""M11 execution-config revision system.

Covers:
* bump_revision increments the row's revision and appends an
  AutomationRevision audit row (target_kind inferred from the ORM class,
  JSON-safe patch, changed_by/causation stamped)
* assert_revision: None skips, match passes, mismatch raises
  StaleRevisionError
* schedule upsert installers bump ONLY when config actually changed
  (idempotent re-install keeps revision 1)
* update_goal bumps on operator-driven target/deadline/status changes
  but record_measurement (current_value writes) never bumps
* record_automation_dispatched stamps config_versions.automation_revision
  onto the automation_run_dispatched ledger event
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.goals.service import record_measurement, update_goal
from packages.core.ledger import event_types as et
from packages.core.ledger.adapters import record_automation_dispatched
from packages.core.models.automation_revision import AutomationRevision
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.revisions import StaleRevisionError, assert_revision, bump_revision
from packages.core.strategist.scheduling import install_strategist_schedule


async def _audit_rows(db, target_id: str) -> list[AutomationRevision]:
    return list((await db.execute(
        select(AutomationRevision)
        .where(AutomationRevision.target_id == target_id)
        .order_by(AutomationRevision.revision.asc())
    )).scalars().all())


# ── bump_revision / assert_revision ───────────────────────────────────

async def test_bump_revision_increments_and_audits(db_session):
    entity_id = generate_ulid()
    goal = Goal(
        entity_id=entity_id,
        title="Rev goal",
        metric_key="mrr",
        target_value=Decimal("100"),
        status="active",
    )
    db_session.add(goal)
    await db_session.flush()
    assert goal.revision == 1

    new_rev = await bump_revision(
        db_session, goal,
        patch={"target_value": Decimal("250"), "deadline": None},
        changed_by_kind="user",
        changed_by_id="u_123",
        causation_id="pi_abc",
    )
    assert new_rev == 2
    assert goal.revision == 2

    rows = await _audit_rows(db_session, goal.id)
    assert len(rows) == 1
    audit = rows[0]
    assert audit.target_kind == "goal"
    assert audit.revision == 2
    assert audit.entity_id == entity_id
    # Decimal was coerced to a JSON-safe string.
    assert audit.patch == {"target_value": "250", "deadline": None}
    assert audit.changed_by_kind == "user"
    assert audit.changed_by_id == "u_123"
    assert audit.causation_id == "pi_abc"

    # Second bump on the same row → revision 3, second audit row.
    await bump_revision(db_session, goal, patch={"status": "paused"})
    rows = await _audit_rows(db_session, goal.id)
    assert [r.revision for r in rows] == [2, 3]
    assert rows[1].changed_by_kind == "system"


async def test_bump_revision_infers_target_kind_per_table(db_session):
    entity_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"revkind:{generate_ulid()}",
        entity_id=entity_id,
        execution_type="agent",
    )
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity_id,
        workflow_id=generate_ulid(),
    )
    db_session.add_all([job, binding])
    await db_session.flush()

    await bump_revision(db_session, job, patch={"enabled": False})
    await bump_revision(db_session, binding, patch={"trigger_type": "schedule"})

    assert (await _audit_rows(db_session, job.id))[0].target_kind == "scheduled_job"
    assert (await _audit_rows(db_session, binding.id))[0].target_kind == "workflow_binding"

    # Non-revisioned rows are rejected loudly.
    with pytest.raises(ValueError, match="not revisioned"):
        await bump_revision(db_session, Workspace(entity_id=entity_id, name="x"), patch={})


async def test_assert_revision_cas(db_session):
    goal = Goal(
        entity_id=generate_ulid(),
        title="CAS goal",
        metric_key="mrr",
        target_value=Decimal("10"),
    )
    db_session.add(goal)
    await db_session.flush()

    await assert_revision(goal, None)   # no expectation → skip
    await assert_revision(goal, 1)      # match → pass

    await bump_revision(db_session, goal, patch={"status": "paused"})
    with pytest.raises(StaleRevisionError) as exc_info:
        await assert_revision(goal, 1)
    assert exc_info.value.expected == 1
    assert exc_info.value.actual == 2
    assert exc_info.value.target_kind == "goal"


# ── schedule upsert: bump only on real change ─────────────────────────

async def test_strategist_schedule_upsert_bumps_only_on_change(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Rev WS")
    db_session.add(workspace)
    await db_session.flush()

    job = await install_strategist_schedule(db_session, workspace, cadence="daily")
    assert job.revision == 1

    # Idempotent re-install with identical cadence → NO bump, no audit.
    again = await install_strategist_schedule(db_session, workspace, cadence="daily")
    assert again.id == job.id
    assert again.revision == 1
    assert await _audit_rows(db_session, job.id) == []

    # Cadence change → bump + audit carrying the changed fields.
    changed = await install_strategist_schedule(db_session, workspace, cadence="weekly")
    assert changed.id == job.id
    assert changed.revision == 2
    rows = await _audit_rows(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].target_kind == "scheduled_job"
    assert rows[0].revision == 2
    assert rows[0].patch.get("every_seconds") == 604800.0


# ── goals: operator config changes bump, measurements never do ────────

async def test_update_goal_bumps_on_config_change_not_measurement(db_session):
    entity_id = generate_ulid()
    goal = Goal(
        entity_id=entity_id,
        title="Bump goal",
        metric_key="follower_count",
        target_value=Decimal("1000"),
        status="active",
    )
    db_session.add(goal)
    await db_session.flush()

    # Operator changes the target → bump.
    updated = await update_goal(
        db_session, goal.id, entity_id, target_value=2000,
    )
    assert updated is not None
    assert updated.revision == 2
    rows = await _audit_rows(db_session, goal.id)
    assert len(rows) == 1
    assert rows[0].patch == {"target_value": "2000"}

    # No-op update (same values) → no bump.
    updated = await update_goal(
        db_session, goal.id, entity_id, target_value=2000,
    )
    assert updated.revision == 2
    assert len(await _audit_rows(db_session, goal.id)) == 1

    # Status change → bump.
    updated = await update_goal(db_session, goal.id, entity_id, status="paused")
    assert updated.revision == 3

    # Measurement-driven current_value write → NEVER bumps.
    await record_measurement(
        db_session, updated, value=Decimal("500"),
        recompute_pace_now=False,
    )
    assert updated.revision == 3
    assert updated.current_value == Decimal("500")
    assert len(await _audit_rows(db_session, goal.id)) == 2


# ── dispatch stamps config_versions.automation_revision ───────────────

async def test_dispatch_stamps_automation_revision_in_ledger_event(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"revstamp:{workspace_id}",
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Stamped job",
        execution_type="agent",
        execution_target={"workspace_id": workspace_id},
        schedule_kind="every",
        every_seconds=86400.0,
        revision=3,
    )
    db_session.add(job)
    await db_session.flush()

    run_id = generate_ulid()
    await record_automation_dispatched(
        db_session, job,
        run_id=run_id,
        now=datetime.now(timezone.utc),
        revision=job.revision,
    )

    event = (await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == entity_id,
            WorkspaceEvent.event_type == et.AUTOMATION_RUN_DISPATCHED,
            WorkspaceEvent.run_id == run_id,
        )
    )).scalar_one()
    assert event.config_versions == {"automation_revision": 3}
