"""M13 bounded experiments — controller lifecycle, guardrails, evaluation,
dispatch overlay merge, and the v2 review integration.

Covers:
* start: freezes a deterministic ledger baseline, hangs the
  ``_experiment_overlay`` on the target WITHOUT bumping its revision,
  stamps started_at/ends_at, emits experiment_started
* stop: removes the overlay idempotently (tolerating a deleted target),
  emits experiment_completed / experiment_guardrail_triggered
* guardrail tick: stops on consecutive cohort failures (seeded ledger
  events) and on max_runs, then auto-evaluates
* evaluate: deterministic success_rate-vs-baseline verdicts, unsupported
  metric marking, cost aggregation notes
* dispatch merge: ``_dispatch_job`` end-to-end with a RUNNING overlay uses
  the patched config and stamps the run's dispatch event with the
  ``xp:{id}:`` correlation; the STALE-overlay path (experiment no longer
  running) is unit-tested via ``effective_dispatch_config`` directly —
  driving a second full dispatch adds nothing over the helper contract
* v2 review proposing an experiment: standing-grant path (policy
  auto_approve_actions) → Experiment created + running, item executing;
  needs_human path → item proposed with a pending per-item
  HitlRequest, then the existing approve flow starts the experiment
  and consumes the request
* learning consolidator surfaces the experiments digest (numbers only)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.experiments import (
    EXPERIMENT_OVERLAY_KEY,
    ExperimentError,
    ExperimentTargetError,
    check_experiment_guardrails,
    effective_dispatch_config,
    evaluate_experiment,
    start_experiment,
    stop_experiment,
)
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.experiment import Experiment
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"
EXPERIMENT_ACTION_KEY = "workspace.proposal.experiment"

_seq = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── seeding helpers ───────────────────────────────────────────────────


async def _seed_workspace(db) -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Experiments WS",
        status="active",
        settings={},
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
    )
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Ops Agent", status="active")
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        service_key="ops",
        status="active",
    )
    db.add_all([workspace, goal, agent, subscription])
    await db.commit()
    return workspace


async def _seed_job(db, workspace: Workspace, **overrides) -> ScheduledJob:
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"job_{generate_ulid()}",
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name="Daily digest",
        schedule_kind="every",
        every_seconds=3600.0,
        payload_message="original digest message",
        execution_type="agent",
        execution_target={"workspace_id": workspace.id},
        enabled=True,
        **overrides,
    )
    db.add(job)
    await db.commit()
    return job


def _experiment_row(workspace: Workspace, job: ScheduledJob, **overrides) -> Experiment:
    fields = dict(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        proposal_item_id=None,
        hypothesis="A shorter prompt should raise this automation's success rate.",
        scope={
            "target_kind": "scheduled_job",
            "target_id": job.id,
            "max_runs": 5,
            "duration_days": 3,
        },
        success_metrics={"success_rate": {"baseline": 0.5, "target": 0.9}},
        guardrails={"max_cost": 10, "rollback_on_consecutive_failures": 2},
        overlay_patch={"payload_message": "patched digest message"},
        status="pending",
    )
    fields.update(overrides)
    return Experiment(**fields)


async def _emit(db, workspace: Workspace, **kwargs) -> WorkspaceEvent:
    global _seq
    _seq += 1
    defaults = dict(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"experiments-test:{workspace.id}:{_seq}",
    )
    defaults.update(kwargs)
    event = await record_event(db, **defaults)
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)  # keep ULID ordering strict
    return event


async def _seed_baseline_runs(db, workspace, job, outcomes: list[str]) -> None:
    for outcome in outcomes:
        event_type = (
            et.AUTOMATION_RUN_COMPLETED if outcome == "success"
            else et.AUTOMATION_RUN_FAILED
        )
        await _emit(
            db, workspace,
            event_type=event_type,
            source_kind="scheduled_job",
            source_id=job.id,
            run_id=f"base_{generate_ulid()}",
            status=outcome,
        )


async def _seed_cohort_run(
    db, workspace, experiment, job, *, outcome: str | None, cost: float | None = None,
) -> str:
    """One dispatched cohort run (+ optional final outcome event)."""
    run_id = f"xr_{generate_ulid()}"
    await _emit(
        db, workspace,
        event_type=et.AUTOMATION_RUN_DISPATCHED,
        source_kind="scheduled_job",
        source_id=job.id,
        run_id=run_id,
        correlation_id=f"xp:{experiment.id}:{run_id[-8:]}",
        status="dispatched",
    )
    if outcome is not None:
        event_type = (
            et.AUTOMATION_RUN_COMPLETED if outcome == "success"
            else et.AUTOMATION_RUN_FAILED
        )
        payload = {"cost": cost} if cost is not None else None
        await _emit(
            db, workspace,
            event_type=event_type,
            source_kind="scheduled_job",
            source_id=job.id,
            run_id=run_id,
            status=outcome,
            payload=payload,
        )
    return run_id


async def _experiment_events(db, workspace, event_type: str) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == workspace.entity_id,
            WorkspaceEvent.event_type == event_type,
            WorkspaceEvent.source_kind == "experiment",
        ).order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


# ── controller: start ─────────────────────────────────────────────────


async def test_start_applies_overlay_freezes_baseline_no_revision_bump(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    await _seed_baseline_runs(
        db_session, workspace, job, ["success", "success", "success", "error"],
    )

    item_id = generate_ulid()
    experiment = _experiment_row(
        workspace, job,
        proposal_item_id=item_id,
        scope={"target_kind": "scheduled_job", "target_id": job.id, "max_runs": 5},
    )
    db_session.add(experiment)
    await db_session.flush()

    await start_experiment(db_session, experiment)
    await db_session.commit()
    await db_session.refresh(job)
    await db_session.refresh(experiment)

    # Overlay applied, revision untouched (experiments are not formal changes).
    overlay = job.execution_target[EXPERIMENT_OVERLAY_KEY]
    assert overlay == {
        "experiment_id": experiment.id,
        "patch": {"payload_message": "patched digest message"},
    }
    assert job.revision == 1

    # Baseline frozen from the last runs: 3/4 success.
    assert experiment.baseline_snapshot["run_count"] == 4
    assert experiment.baseline_snapshot["success_rate"] == pytest.approx(0.75)
    assert experiment.baseline_snapshot["avg_duration_ms"] is None

    assert experiment.status == "running"
    assert experiment.started_at is not None
    # duration_days absent from scope → default 7.
    assert experiment.ends_at - experiment.started_at == timedelta(days=7)

    started = await _experiment_events(db_session, workspace, et.EXPERIMENT_STARTED)
    assert len(started) == 1
    assert started[0].source_id == experiment.id
    assert started[0].causation_id == item_id
    assert started[0].idempotency_key == f"xp:{experiment.id}:started"
    assert started[0].payload["target_id"] == job.id


async def test_start_rejects_bad_states_and_missing_target(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)

    running = _experiment_row(workspace, job, status="running")
    db_session.add(running)
    missing_target = _experiment_row(
        workspace, job,
        scope={"target_kind": "scheduled_job", "target_id": generate_ulid(), "max_runs": 3},
    )
    db_session.add(missing_target)
    other_ws_job = await _seed_job(
        db_session,
        SimpleNamespace(entity_id=workspace.entity_id, id=generate_ulid()),
    )
    cross_workspace = _experiment_row(
        workspace, other_ws_job,
        scope={"target_kind": "scheduled_job", "target_id": other_ws_job.id, "max_runs": 3},
    )
    db_session.add(cross_workspace)
    await db_session.flush()

    with pytest.raises(ExperimentError):
        await start_experiment(db_session, running)
    with pytest.raises(ExperimentTargetError):
        await start_experiment(db_session, missing_target)
    with pytest.raises(ExperimentTargetError):
        await start_experiment(db_session, cross_workspace)


# ── controller: stop ──────────────────────────────────────────────────


async def test_stop_removes_overlay_idempotently(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(workspace, job)
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.commit()

    await stop_experiment(db_session, experiment, outcome="completed", reason="max_runs 5 reached")
    await db_session.commit()
    await db_session.refresh(job)

    assert EXPERIMENT_OVERLAY_KEY not in (job.execution_target or {})
    assert experiment.status == "completed"
    completed = await _experiment_events(db_session, workspace, et.EXPERIMENT_COMPLETED)
    assert len(completed) == 1
    assert completed[0].payload == {"reason": "max_runs 5 reached"}

    # Second stop is a no-op (status + ledger unchanged, no error).
    await stop_experiment(db_session, experiment, outcome="stopped_guardrail")
    assert experiment.status == "completed"
    assert not await _experiment_events(
        db_session, workspace, et.EXPERIMENT_GUARDRAIL_TRIGGERED,
    )

    with pytest.raises(ValueError):
        await stop_experiment(db_session, experiment, outcome="nope")


async def test_stop_tolerates_deleted_target(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(workspace, job)
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.delete(job)
    await db_session.flush()

    await stop_experiment(
        db_session, experiment, outcome="stopped_guardrail", reason="2 consecutive failures",
    )
    assert experiment.status == "stopped_guardrail"
    triggered = await _experiment_events(
        db_session, workspace, et.EXPERIMENT_GUARDRAIL_TRIGGERED,
    )
    assert len(triggered) == 1
    assert triggered[0].payload == {"reason": "2 consecutive failures"}


# ── guardrail tick ────────────────────────────────────────────────────


async def test_guardrail_tick_stops_on_consecutive_failures(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(workspace, job)
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.commit()

    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="success")
    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="error")
    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="error")

    results = await check_experiment_guardrails(db_session)
    await db_session.commit()
    await db_session.refresh(experiment)
    await db_session.refresh(job)

    assert len(results) == 1
    assert results[0]["experiment_id"] == experiment.id
    assert results[0]["outcome"] == "stopped_guardrail"
    # Auto-evaluated in the same pass.
    assert experiment.status == "evaluated"
    assert experiment.evaluation["guardrail_violations"] is True
    assert experiment.evaluation["cohort"] == {
        "run_count": 3,
        "finished_count": 3,
        "completed": 1,
        "failed": 2,
        "success_rate": pytest.approx(1 / 3),
    }
    assert EXPERIMENT_OVERLAY_KEY not in (job.execution_target or {})
    assert len(await _experiment_events(
        db_session, workspace, et.EXPERIMENT_GUARDRAIL_TRIGGERED,
    )) == 1
    assert len(await _experiment_events(
        db_session, workspace, et.EXPERIMENT_EVALUATED,
    )) == 1


async def test_guardrail_tick_completes_on_max_runs(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(
        workspace, job,
        scope={"target_kind": "scheduled_job", "target_id": job.id, "max_runs": 2},
    )
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.commit()

    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="success")
    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="success", cost=1.5)

    results = await check_experiment_guardrails(db_session)
    await db_session.commit()
    await db_session.refresh(experiment)

    assert results[0]["outcome"] == "completed"
    assert experiment.status == "evaluated"
    verdict = experiment.evaluation["metrics"]["success_rate"]
    assert verdict["cohort"] == pytest.approx(1.0)
    assert verdict["target"] == 0.9
    assert verdict["met"] is True
    assert experiment.evaluation["guardrail_violations"] is False
    assert experiment.evaluation["cost"] == pytest.approx(1.5)

    # Second tick: nothing running anymore.
    assert await check_experiment_guardrails(db_session) == []


# ── evaluator ─────────────────────────────────────────────────────────


async def test_evaluate_computes_verdicts_and_marks_unsupported_metrics(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(
        workspace, job,
        success_metrics={
            "success_rate": {"baseline": 0.5, "target": 0.9},
            "run_count": {"baseline": None, "target": 3},
            "engagement_lift": {"baseline": 1.0, "target": 2.0},
        },
    )
    db_session.add(experiment)
    await db_session.flush()

    with pytest.raises(ExperimentError):
        await evaluate_experiment(db_session, experiment)  # pending → invalid

    await start_experiment(db_session, experiment)
    await db_session.commit()
    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="success")
    await _seed_cohort_run(db_session, workspace, experiment, job, outcome="error")

    await stop_experiment(db_session, experiment, outcome="completed", reason="test")
    await evaluate_experiment(db_session, experiment)
    await db_session.commit()
    await db_session.refresh(experiment)

    assert experiment.status == "evaluated"
    assert experiment.evaluated_at is not None
    metrics = experiment.evaluation["metrics"]
    assert metrics["success_rate"]["cohort"] == pytest.approx(0.5)
    assert metrics["success_rate"]["met"] is False
    # Baseline comes from the frozen snapshot (no baseline runs → None),
    # falling back to the declared prior only when the snapshot lacks the key.
    assert metrics["success_rate"]["baseline"] is None
    # Baseline run_count comes from the frozen snapshot (0 pre-experiment runs).
    assert metrics["run_count"] == {
        "baseline": 0, "cohort": 2, "target": 3, "met": False,
    }
    assert metrics["engagement_lift"] == {"status": "unsupported"}
    assert experiment.evaluation["cost"] is None
    assert "no cost data present on cohort events" in experiment.evaluation["notes"]

    evaluated = await _experiment_events(db_session, workspace, et.EXPERIMENT_EVALUATED)
    assert len(evaluated) == 1
    assert evaluated[0].payload["metrics_met"] == 0
    assert evaluated[0].payload["metrics_evaluable"] == 2
    assert evaluated[0].payload["metrics_declared"] == 3


# ── dispatch overlay merge ────────────────────────────────────────────


async def test_effective_dispatch_config_merges_only_while_running(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(workspace, job)
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.flush()

    # Running → patch merged, marker stripped, experiment id surfaced.
    config, experiment_id, patch = await effective_dispatch_config(
        db_session, job.execution_target,
    )
    assert experiment_id == experiment.id
    assert patch == {"payload_message": "patched digest message"}
    assert config["payload_message"] == "patched digest message"
    assert config["workspace_id"] == workspace.id
    assert EXPERIMENT_OVERLAY_KEY not in config

    # Stale overlay (experiment no longer running) → ignored but stripped.
    stale_target = dict(job.execution_target)
    experiment.status = "completed"
    await db_session.flush()
    config, experiment_id, patch = await effective_dispatch_config(
        db_session, stale_target,
    )
    assert experiment_id is None and patch is None
    assert EXPERIMENT_OVERLAY_KEY not in config
    assert "payload_message" not in config

    # No overlay at all → passthrough.
    config, experiment_id, patch = await effective_dispatch_config(
        db_session, {"workspace_id": workspace.id},
    )
    assert (experiment_id, patch) == (None, None)
    assert config == {"workspace_id": workspace.id}


async def test_dispatch_job_uses_patched_config_and_xp_correlation(db_session, monkeypatch):
    from packages.core.models.task import Task
    from packages.core.tasks.scheduler_tasks import _dispatch_job

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    experiment = _experiment_row(workspace, job)
    db_session.add(experiment)
    await db_session.flush()
    await start_experiment(db_session, experiment)
    await db_session.commit()
    await db_session.refresh(job)

    now = _utcnow()
    await _dispatch_job(db_session, job, now)
    await db_session.commit()

    dispatched = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.entity_id == workspace.entity_id,
            WorkspaceEvent.event_type == et.AUTOMATION_RUN_DISPATCHED,
            WorkspaceEvent.source_id == job.id,
        )
    )).scalars().all())
    assert len(dispatched) == 1
    assert dispatched[0].correlation_id.startswith(f"xp:{experiment.id}:")

    # The agent-less dispatch created a manual task whose prompt used the
    # PATCHED payload_message; the stored job config is untouched.
    tasks = list((await db_session.execute(
        select(Task).where(Task.entity_id == workspace.entity_id)
    )).scalars().all())
    auto_tasks = [t for t in tasks if (t.details or {}).get("scheduled_job_id") == job.job_id]
    assert len(auto_tasks) == 1
    assert "patched digest message" in (auto_tasks[0].description or "")
    await db_session.refresh(job)
    assert job.payload_message == "original digest message"
    assert job.execution_target[EXPERIMENT_OVERLAY_KEY]["experiment_id"] == experiment.id


# ── v2 review integration ─────────────────────────────────────────────


def _review_payload(job_id: str, *, max_cost: float = 15) -> dict:
    return {
        "summary": "One doc task plus a bounded automation experiment.",
        "tasks": [
            {
                "task_key": "draft_docs",
                "title": "Draft source docs",
                "description": "Write the source docs for this cycle.",
                "owner_service_key": "ops",
                "priority": 3,
                "deliverables": [{
                    "name": "docs",
                    "kind": "value",
                    "shape": "TextResult",
                    "acceptance": "docs drafted",
                    "usage": "operator review",
                }],
            },
        ],
        "experiments": [
            {
                "experiment_key": "shorter_prompt",
                "hypothesis": (
                    "A shorter digest prompt should raise the automation "
                    "success rate per the execution report."
                ),
                "target_kind": "scheduled_job",
                "target_id": job_id,
                "overlay_patch": {"payload_message": "patched digest message"},
                "max_runs": 5,
                "duration_days": 7,
                "success_metrics": {
                    "success_rate": {"baseline": 0.5, "target": 0.9},
                },
                "guardrails": {
                    "max_cost": max_cost,
                    "rollback_on_consecutive_failures": 2,
                },
            }
        ],
    }


def _fake_completion(payload: dict):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _set_flag(db, enabled: bool) -> None:
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == FLAG_KEY)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(key=FLAG_KEY, description="test", default_enabled=enabled))
    else:
        flag.default_enabled = enabled
    await db.commit()
    feature_flags_service._bump_cache()


async def _run_v2_review(db, monkeypatch, workspace: Workspace, *, payload: dict) -> dict:
    await _emit(db, workspace)
    await _set_flag(db, True)
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(payload),
    )

    async def _noop_post(*args, **kwargs):
        return None
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
    monkeypatch.setattr(strategist_service, "_post_human_request_chat", _noop_post)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)
    return await _execute_strategist_review_cycle(db, workspace.id, "scheduled")


async def _experiment_items(db, review_id: str) -> list[ProposalItemRecord]:
    return list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(
            ProposalRecord.review_id == review_id,
            ProposalItemRecord.kind == "experiment",
        )
        .order_by(ProposalItemRecord.item_key.asc())
    )).scalars().all())


async def test_v2_review_standing_grant_starts_experiment(db_session, monkeypatch):
    from packages.core.governance.policy import WorkspacePolicy
    from packages.core.governance.service import update_policy

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    await update_policy(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        policy=WorkspacePolicy(auto_approve_actions=[EXPERIMENT_ACTION_KEY]),
    )
    await db_session.commit()

    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    assert not result.get("skipped")
    assert len(result["experiments"]) == 1
    digest = result["experiments"][0]
    assert digest["outcome"] == "allow"
    assert digest["experiment_id"]
    assert digest["risk_level"] == "medium"  # max_cost 15 ≤ $20

    items = await _experiment_items(db_session, result["review_id"])
    assert len(items) == 1
    item = items[0]
    assert item.item_key == "xp_shorter_prompt"
    assert item.action_key == EXPERIMENT_ACTION_KEY
    assert item.status == "executing"
    assert item.execution_root_id == digest["experiment_id"]
    assert item.decision["decision"] == "approved"

    experiment = await db_session.get(Experiment, digest["experiment_id"])
    assert experiment is not None
    assert experiment.status == "running"
    assert experiment.proposal_item_id == item.id
    assert experiment.scope["target_id"] == job.id
    await db_session.refresh(job)
    assert job.execution_target[EXPERIMENT_OVERLAY_KEY]["experiment_id"] == experiment.id
    assert job.revision == 1  # no revision bump

    # Standing grant → no per-item HitlRequest was minted.
    reqs = list((await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.workspace_id == workspace.id,
            HitlRequest.action_key == EXPERIMENT_ACTION_KEY,
        )
    )).scalars().all())
    assert reqs == []


async def test_v2_review_needs_human_then_approve_flow_starts_experiment(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)

    # max_cost 25 > $20 → high risk item.
    result = await _run_v2_review(
        db_session, monkeypatch, workspace,
        payload=_review_payload(job.id, max_cost=25),
    )
    assert not result.get("skipped")
    digest = result["experiments"][0]
    assert digest["outcome"] == "needs_human"
    assert digest["experiment_id"] is None
    assert digest["risk_level"] == "high"

    items = await _experiment_items(db_session, result["review_id"])
    item = items[0]
    assert item.status == "proposed"
    assert item.risk_level == "high"
    assert item.approval_request_id == digest["approval_request_id"]

    request = await db_session.get(HitlRequest, item.approval_request_id)
    assert request.status == "pending"
    assert request.action_key == EXPERIMENT_ACTION_KEY
    assert request.dedup_key == f"proposal_item:{item.id}"
    assert request.resource_kind == "proposal_item"
    assert request.resource_id == item.id

    # No Experiment row exists yet.
    assert (await db_session.execute(
        select(Experiment).where(Experiment.workspace_id == workspace.id)
    )).scalar_one_or_none() is None

    # Operator approves the review via the existing flow → the experiment
    # item is approved with the cohort, the Experiment starts, and the
    # per-item request is consumed.
    actor = generate_ulid()
    approved_task_ids = await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        actor_kind="user",
        actor_id=actor,
    )
    await db_session.commit()
    assert approved_task_ids  # the cohort's task moved

    await db_session.refresh(item)
    assert item.status == "executing"
    assert item.execution_root_id
    experiment = await db_session.get(Experiment, item.execution_root_id)
    assert experiment.status == "running"
    await db_session.refresh(job)
    assert job.execution_target[EXPERIMENT_OVERLAY_KEY]["experiment_id"] == experiment.id
    await db_session.refresh(request)
    assert request.status == "consumed"

    started = await _experiment_events(db_session, workspace, et.EXPERIMENT_STARTED)
    assert [e.source_id for e in started] == [experiment.id]
    assert started[0].causation_id == item.id


async def test_v2_review_reject_flow_rejects_experiment_item(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    items = await _experiment_items(db_session, result["review_id"])
    request_id = items[0].approval_request_id
    assert request_id is not None

    await strategist_service.reject_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        reason="Not this cycle.",
        reason_code="BAD_TIMING",
        actor_kind="user",
        actor_id=generate_ulid(),
    )
    await db_session.commit()

    item = (await _experiment_items(db_session, result["review_id"]))[0]
    assert item.status == "rejected"
    assert item.decision["reason_code"] == "BAD_TIMING"
    request = await db_session.get(HitlRequest, request_id)
    assert request.status == "denied"
    assert (await db_session.execute(
        select(Experiment).where(Experiment.workspace_id == workspace.id)
    )).scalar_one_or_none() is None


# ── learning consolidator digest ──────────────────────────────────────


async def test_learning_consolidator_surfaces_experiment_digest(db_session):
    from packages.core.consolidators.base import SnapshotContext
    from packages.core.consolidators.learning_evidence import (
        LearningEvidenceConsolidator,
    )
    from packages.core.models.review_run import ReviewRun

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)

    running = _experiment_row(workspace, job, status="running")
    evaluated = _experiment_row(
        workspace, job,
        status="evaluated",
        evaluation={
            "metrics": {
                "success_rate": {"baseline": 0.5, "cohort": 1.0, "target": 0.9, "met": True},
                "engagement_lift": {"status": "unsupported"},
            },
            "cohort": {"run_count": 4, "success_rate": 1.0},
            "guardrail_violations": False,
            "cost": 2.5,
        },
    )
    db_session.add_all([running, evaluated])
    await db_session.commit()

    window_event = await _emit(
        db_session, workspace,
        event_type=et.EXPERIMENT_EVALUATED,
        source_kind="experiment",
        source_id=evaluated.id,
        payload={"metrics_met": 1},
    )

    review = ReviewRun(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger_kind="scheduled",
        status="running",
    )
    ctx = SnapshotContext(review=review, workspace=workspace, events=[window_event])
    report = await LearningEvidenceConsolidator().run(db_session, ctx)

    digest = {entry["id"]: entry for entry in report.metrics["experiments"]}
    assert set(digest) == {running.id, evaluated.id}
    assert digest[running.id]["status"] == "running"
    assert digest[running.id]["verdict_summary"] is None
    summary = digest[evaluated.id]["verdict_summary"]
    assert summary["metrics_met"] == 1
    assert summary["metrics_evaluable"] == 1
    assert summary["metrics_declared"] == 2
    assert summary["guardrail_violations"] is False
    assert summary["run_count"] == 4
    assert summary["cost"] == 2.5
    assert report.coverage.sources["experiment_events"] == 1
    assert report.coverage.sources["experiments"] == 2
