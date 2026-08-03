"""M7/M8/M10 configuration-change proposal kinds — end-to-end.

Covers:
* Strategist output schema: per-target patch whitelists, create-vs-update
  field coherence (target_id / expected_revision / patch), per-kind caps
  (automation 3 / workflow 2 / goal 2) and duplicate change_key detection
* M7 validator: stale expected_revision → item rejected STALE_REVISION and
  never applied; a second open change on the same target → DUPLICATE; a
  target from another workspace → INSUFFICIENT_DATA; a missing target →
  INSUFFICIENT_DATA
* M10 executor: schedule update bumps the revision, writes the
  automation_revisions audit row, emits config_changed and refreshes the
  裁定 B derived operating_model index; pause flips enabled; workflow
  binding update; goal update_target rides goals/service.update_goal (so
  its bump AND the measurement-schedule side effects still fire)
* CAS race: the row moves between validation and apply → apply_change_item
  marks the item failed/STALE_REVISION and leaves the row untouched
* governance: standing grant on workspace.proposal.automation_change.update
  → auto-applied with no HitlRequest; no grant → item proposed with a
  pending per-item request, then the cohort card approve applies it and
  grants+consumes the request; policy deny → POLICY_BLOCKED, row untouched
* v2 review end-to-end: a scripted completion returning one
  automation_change produces item + application + ledger chain
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.automation_revision import AutomationRevision
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.proposals import (
    apply_change_item,
    create_change_items,
    validate_items,
)
from packages.core.proposals.change_executor import DERIVED_INDEX_SOURCE
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.strategist.proposal import (
    ProposedAutomationChange,
    ProposedGoalChange,
    ProposedWorkflowChange,
)
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"
AUTOMATION_UPDATE_ACTION_KEY = "workspace.proposal.automation_change.update"

_seq = 0


# ── seeding helpers ───────────────────────────────────────────────────


async def _seed_workspace(db, name: str = "Change WS") -> Workspace:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        status="active",
        settings={},
        operating_model={},
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
        schedule_kind="cron",
        cron_expr="0 6 * * *",
        payload_message="original digest message",
        execution_type="agent",
        execution_target={"workspace_id": workspace.id},
        enabled=True,
        **overrides,
    )
    db.add(job)
    await db.commit()
    return job


async def _seed_binding(db, workspace: Workspace) -> WorkflowBinding:
    definition = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        name="Publish flow",
        steps=[{"id": "s1", "type": "agent", "name": "draft", "config": {}}],
    )
    db.add(definition)
    await db.flush()
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workflow_id=definition.id,
        workspace_id=workspace.id,
        name="Publish flow (ws)",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    db.add(binding)
    await db.commit()
    return binding


async def _seed_goal(db, workspace: Workspace, **overrides) -> Goal:
    goal = Goal(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="MRR",
        metric_key="mrr",
        target_value=5000,
        status="active",
        measurement_cadence="daily",
        measurement_source={"provider": "workspace", "action": "count_tasks"},
        **overrides,
    )
    db.add(goal)
    await db.commit()
    return goal


async def _proposal_record(db, workspace: Workspace) -> ProposalRecord:
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=f"rv_{generate_ulid()}",
        summary="change cohort",
        status="open",
    )
    db.add(record)
    await db.flush()
    return record


def _automation_change(job_id: str, revision: int = 1, **overrides):
    fields = dict(
        change_key="retune_digest_schedule",
        target_kind="scheduled_job",
        operation="update",
        target_id=job_id,
        expected_revision=revision,
        patch={"cron_expr": "0 9 * * 1"},
        rationale="The 06:00 run misses the publishing window every weekday.",
    )
    fields.update(overrides)
    return ProposedAutomationChange(**fields)


async def _make_item(db, workspace, record, change, kind="automation_change"):
    items = await create_change_items(
        db, record=record, proposed_changes=[(kind, change)],
    )
    await db.flush()
    return items[0]


# ── schema validation ─────────────────────────────────────────────────


def test_patch_whitelist_rejects_unknown_keys():
    with pytest.raises(ValueError, match="not changeable on scheduled_job"):
        _automation_change("job_1", patch={"agent_id": "ag_1"})
    with pytest.raises(ValueError, match="not changeable on workflow_definition"):
        ProposedWorkflowChange(
            change_key="k",
            target_kind="workflow_definition",
            operation="update",
            target_id="wf_1",
            expected_revision=1,
            patch={"status": "archived"},
            rationale="steps drift from the documented process",
        )
    with pytest.raises(ValueError, match="not changeable on goal"):
        ProposedGoalChange(
            change_key="k",
            operation="update_target",
            target_id="g_1",
            expected_revision=1,
            patch={"target_value": 10, "metric_key": "arr"},
            rationale="the goal target was met two weeks early",
        )
    # A whitelisted key on each target passes.
    assert _automation_change("job_1", patch={"enabled": False}).patch == {"enabled": False}


def test_create_vs_update_field_coherence():
    with pytest.raises(ValueError, match="must not carry a target_id"):
        _automation_change("job_1", operation="create", expected_revision=None)
    with pytest.raises(ValueError, match="must not carry an expected_revision"):
        _automation_change(
            "job_1", operation="create", target_id=None, expected_revision=3,
        )
    with pytest.raises(ValueError, match="requires a target_id"):
        _automation_change("job_1", target_id=None)
    with pytest.raises(ValueError, match="requires expected_revision"):
        _automation_change("job_1", expected_revision=None)
    with pytest.raises(ValueError, match="requires a non-empty patch"):
        _automation_change("job_1", patch={})
    with pytest.raises(ValueError, match="requires patch.target_value"):
        ProposedGoalChange(
            change_key="k",
            operation="update_target",
            target_id="g_1",
            expected_revision=1,
            patch={"priority": 1},
            rationale="the goal target was met two weeks early",
        )
    # pause/delete need no patch at all.
    assert _automation_change("job_1", operation="pause", patch={}).patch == {}
    assert _automation_change("job_1", operation="delete", patch={}).operation == "delete"
    # create needs a patch and no ids.
    created = _automation_change(
        "x", operation="create", target_id=None, expected_revision=None,
        patch={"cron_expr": "0 9 * * 1", "name": "New digest"},
    )
    assert created.target_id is None


def test_per_kind_caps_and_unique_change_keys():
    from packages.core.strategist.proposal import Proposal

    base = dict(review_id="rv_1", summary="s", tasks=[])

    def automation(n):
        return [_automation_change(f"job_{i}", change_key=f"k{i}") for i in range(n)]

    Proposal(**base, automation_changes=automation(3))
    with pytest.raises(ValueError):
        Proposal(**base, automation_changes=automation(4))

    def workflow(n):
        return [
            ProposedWorkflowChange(
                change_key=f"w{i}",
                target_kind="workflow_binding",
                operation="pause",
                target_id=f"wb_{i}",
                expected_revision=1,
                rationale="this binding has not fired in six weeks",
            ) for i in range(n)
        ]

    Proposal(**base, workflow_changes=workflow(2))
    with pytest.raises(ValueError):
        Proposal(**base, workflow_changes=workflow(3))

    def goal(n):
        return [
            ProposedGoalChange(
                change_key=f"g{i}",
                operation="pause",
                target_id=f"goal_{i}",
                expected_revision=1,
                rationale="this goal is on hold until Q3 planning lands",
            ) for i in range(n)
        ]

    Proposal(**base, goal_changes=goal(2))
    with pytest.raises(ValueError):
        Proposal(**base, goal_changes=goal(3))

    with pytest.raises(ValueError, match="duplicate automation_changes change_key"):
        Proposal(**base, automation_changes=[
            _automation_change("job_a", change_key="same"),
            _automation_change("job_b", change_key="same"),
        ])


# ── validator ─────────────────────────────────────────────────────────


async def test_validator_rejects_stale_expected_revision(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    item = await _make_item(
        db_session, workspace, record, _automation_change(job.id, revision=7),
    )

    validated = await validate_items(db_session, SimpleNamespace(id=record.review_id), [], [item])
    assert item.status == "rejected"
    assert item.decision["reason_code"] == "STALE_REVISION"
    assert "expected revision 7" in validated[0][1]

    # Not applied: the row is untouched.
    await db_session.refresh(job)
    assert job.cron_expr == "0 6 * * *"
    assert job.revision == 1


async def test_validator_rejects_duplicate_open_change_on_same_target(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    items = await create_change_items(db_session, record=record, proposed_changes=[
        ("automation_change", _automation_change(job.id, change_key="first")),
        ("automation_change", _automation_change(
            job.id, change_key="second", patch={"enabled": False},
        )),
    ])
    await db_session.flush()

    await validate_items(db_session, SimpleNamespace(id=record.review_id), [], items)
    assert items[0].status == "proposed"
    assert items[1].status == "rejected"
    assert items[1].decision["reason_code"] == "DUPLICATE"


async def test_validator_rejects_cross_workspace_and_missing_targets(db_session):
    workspace = await _seed_workspace(db_session)
    other = await _seed_workspace(db_session, name="Other WS")
    foreign_job = await _seed_job(db_session, other)
    record = await _proposal_record(db_session, workspace)

    items = await create_change_items(db_session, record=record, proposed_changes=[
        ("automation_change", _automation_change(foreign_job.id, change_key="foreign")),
        ("automation_change", _automation_change(generate_ulid(), change_key="ghost")),
    ])
    await db_session.flush()
    await validate_items(db_session, SimpleNamespace(id=record.review_id), [], items)

    assert items[0].status == "rejected"
    assert items[0].decision["reason_code"] == "INSUFFICIENT_DATA"
    assert "does not belong to workspace" in items[0].decision["comment"]
    assert items[1].status == "rejected"
    assert items[1].decision["reason_code"] == "INSUFFICIENT_DATA"
    assert "does not exist" in items[1].decision["comment"]


# ── executor happy paths ──────────────────────────────────────────────


async def _config_events(db, workspace, event_type=et.CONFIG_CHANGED) -> list[WorkspaceEvent]:
    return list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == event_type,
        ).order_by(WorkspaceEvent.id.asc())
    )).scalars().all())


async def test_schedule_update_bumps_revision_audits_and_refreshes_index(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    item = await _make_item(db_session, workspace, record, _automation_change(job.id))

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    assert result["revision"] == 2
    assert item.status == "succeeded"
    assert item.finished_at is not None
    assert item.execution_root_id == item.id

    await db_session.refresh(job)
    assert job.cron_expr == "0 9 * * 1"
    assert job.revision == 2

    audits = list((await db_session.execute(
        select(AutomationRevision).where(AutomationRevision.target_id == job.id)
    )).scalars().all())
    assert len(audits) == 1
    assert audits[0].target_kind == "scheduled_job"
    assert audits[0].revision == 2
    assert audits[0].patch == {"cron_expr": "0 9 * * 1"}
    assert audits[0].changed_by_kind == "agent"
    assert audits[0].causation_id == item.id

    events = await _config_events(db_session, workspace)
    assert len(events) == 1
    assert events[0].source_kind == "config"
    assert events[0].source_id == job.id
    assert events[0].causation_id == item.id
    assert events[0].config_versions == {"scheduled_job_revision": 2}

    # 裁定 B: derived index refreshed from the canonical row.
    await db_session.refresh(workspace)
    index = workspace.operating_model["automations"]
    assert len(index) == 1
    assert index[0]["target_kind"] == "scheduled_job"
    assert index[0]["target_id"] == job.id
    assert index[0]["revision"] == 2
    assert index[0]["trigger"] == "cron 0 9 * * 1"
    assert index[0]["source"] == DERIVED_INDEX_SOURCE
    # Derived refresh must NOT move the operator-authored model revision.
    assert int(workspace.operation_revision or 0) == 0


async def test_pause_sets_enabled_false_and_preserves_authored_index_entries(db_session):
    workspace = await _seed_workspace(db_session)
    workspace.operating_model = {"automations": [
        {"automation_key": "hand_written", "description": "authored", "trigger": "daily"},
    ]}
    await db_session.commit()
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    item = await _make_item(db_session, workspace, record, _automation_change(
        job.id, change_key="pause_it", operation="pause", patch={},
    ))

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    await db_session.refresh(job)
    assert job.enabled is False
    assert job.revision == 2

    await db_session.refresh(workspace)
    index = workspace.operating_model["automations"]
    keys = {entry["automation_key"] for entry in index}
    assert "hand_written" in keys  # authored entry survives the refresh
    derived = [e for e in index if e.get("source") == DERIVED_INDEX_SOURCE]
    assert derived[0]["enabled"] is False


async def test_workflow_binding_update_applies_and_bumps(db_session):
    workspace = await _seed_workspace(db_session)
    binding = await _seed_binding(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    change = ProposedWorkflowChange(
        change_key="rename_binding",
        target_kind="workflow_binding",
        operation="update",
        target_id=binding.id,
        expected_revision=1,
        patch={"name": "Publish flow (weekly)", "trigger_type": "schedule"},
        rationale="the binding name no longer matches what it actually runs",
    )
    item = await _make_item(db_session, workspace, record, change, kind="workflow_change")

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    await db_session.refresh(binding)
    assert binding.name == "Publish flow (weekly)"
    assert binding.trigger_type == "schedule"
    assert binding.revision == 2

    events = await _config_events(db_session, workspace)
    assert events[0].config_versions == {"workflow_binding_revision": 2}


async def test_goal_update_target_goes_through_update_goal(db_session):
    workspace = await _seed_workspace(db_session)
    goal = await _seed_goal(db_session, workspace)
    # The measurement schedule the goals service installs for this goal.
    from packages.core.goals.scheduling import install_measurement_schedule
    await install_measurement_schedule(db_session, goal)
    await db_session.commit()

    record = await _proposal_record(db_session, workspace)
    change = ProposedGoalChange(
        change_key="raise_mrr_target",
        operation="update_target",
        target_id=goal.id,
        expected_revision=1,
        patch={"target_value": 9000, "deadline": "2026-12-31"},
        rationale="the 5000 target was met four weeks ahead of the deadline",
    )
    item = await _make_item(db_session, workspace, record, change, kind="goal_change")

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    await db_session.refresh(goal)
    assert int(goal.target_value) == 9000
    assert goal.deadline == date(2026, 12, 31)
    # update_goal's own bump fired (revision moved exactly once).
    assert goal.revision == 2
    audits = list((await db_session.execute(
        select(AutomationRevision).where(AutomationRevision.target_id == goal.id)
    )).scalars().all())
    assert [a.target_kind for a in audits] == ["goal"]

    # goal_changed (not config_changed) and the measurement schedule survives.
    assert await _config_events(db_session, workspace) == []
    goal_events = await _config_events(db_session, workspace, et.GOAL_CHANGED)
    assert len(goal_events) == 1
    assert goal_events[0].source_kind == "goal"
    assert goal_events[0].config_versions == {"goal_revision": 2}

    schedule = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.goal_id == goal.id)
    )).scalars().all()
    assert len(schedule) == 1


async def test_goal_archive_abandons_and_removes_measurement_schedule(db_session):
    workspace = await _seed_workspace(db_session)
    goal = await _seed_goal(db_session, workspace)
    from packages.core.goals.scheduling import install_measurement_schedule
    await install_measurement_schedule(db_session, goal)
    await db_session.commit()

    record = await _proposal_record(db_session, workspace)
    change = ProposedGoalChange(
        change_key="archive_goal",
        operation="archive",
        target_id=goal.id,
        expected_revision=1,
        rationale="this metric is no longer part of the workspace's mandate",
    )
    item = await _make_item(db_session, workspace, record, change, kind="goal_change")
    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    await db_session.refresh(goal)
    assert goal.status == "abandoned"
    assert goal.revision == 2
    remaining = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.goal_id == goal.id)
    )).scalars().all()
    assert remaining == []


async def test_delete_hard_deletes_after_writing_audit_row(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    item = await _make_item(db_session, workspace, record, _automation_change(
        job.id, change_key="drop_it", operation="delete", patch={},
    ))

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is True
    assert await db_session.get(ScheduledJob, job.id) is None
    audits = list((await db_session.execute(
        select(AutomationRevision).where(AutomationRevision.target_id == job.id)
    )).scalars().all())
    assert audits[0].patch == {"deleted": True}


# ── CAS race ──────────────────────────────────────────────────────────


async def test_cas_race_marks_item_failed_and_leaves_row_untouched(db_session):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    record = await _proposal_record(db_session, workspace)
    item = await _make_item(db_session, workspace, record, _automation_change(job.id))

    # Validation passes at revision 1 …
    await validate_items(db_session, SimpleNamespace(id=record.review_id), [], [item])
    assert item.status == "proposed"

    # … then someone else edits the row while the item waits for approval.
    job.payload_message = "edited by the operator"
    from packages.core.revisions import bump_revision
    await bump_revision(db_session, job, patch={"payload_message": "edited"})
    await db_session.commit()

    result = await apply_change_item(db_session, item)
    await db_session.commit()

    assert result["ok"] is False
    assert result["error_code"] == "STALE_REVISION"
    assert item.status == "failed"
    assert item.finished_at is not None
    assert item.decision["error_code"] == "STALE_REVISION"

    await db_session.refresh(job)
    assert job.cron_expr == "0 6 * * *"      # the proposed patch never landed
    assert job.payload_message == "edited by the operator"
    assert job.revision == 2                  # only the operator's bump
    assert await _config_events(db_session, workspace) == []


# ── governance / v2 review ────────────────────────────────────────────


async def _emit(db, workspace: Workspace) -> None:
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"change-kinds:{workspace.id}:{_seq}",
    )
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)


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


def _review_payload(job_id: str, *, revision: int = 1) -> dict:
    return {
        "summary": "Retune the digest schedule; one doc task.",
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
        "automation_changes": [
            {
                "change_key": "retune_digest_schedule",
                "target_kind": "scheduled_job",
                "operation": "update",
                "target_id": job_id,
                "expected_revision": revision,
                "patch": {"cron_expr": "0 9 * * 1"},
                "rationale": (
                    "The 06:00 run misses the publishing window every weekday."
                ),
                "basis": {"report_refs": ["automation_portfolio"], "evidence_refs": []},
            }
        ],
    }


def _fake_completion(payload: dict):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


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


async def _change_items(db, review_id: str) -> list[ProposalItemRecord]:
    from packages.core.proposals import CHANGE_KINDS

    return list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(
            ProposalRecord.review_id == review_id,
            ProposalItemRecord.kind.in_(CHANGE_KINDS),
        )
        .order_by(ProposalItemRecord.item_key.asc())
    )).scalars().all())


async def test_v2_review_standing_grant_auto_applies_change(db_session, monkeypatch):
    from packages.core.governance.policy import WorkspacePolicy
    from packages.core.governance.service import update_policy

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    await update_policy(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        policy=WorkspacePolicy(auto_approve_actions=[AUTOMATION_UPDATE_ACTION_KEY]),
    )
    await db_session.commit()

    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    assert not result.get("skipped")
    assert len(result["changes"]) == 1
    digest = result["changes"][0]
    assert digest["outcome"] == "allow"
    assert digest["applied"] is True
    assert digest["risk_level"] == "medium"
    assert digest["action_key"] == AUTOMATION_UPDATE_ACTION_KEY
    assert digest["revision"] == 2

    items = await _change_items(db_session, result["review_id"])
    assert len(items) == 1
    item = items[0]
    assert item.item_key == "ac_retune_digest_schedule"
    assert item.kind == "automation_change"
    assert item.status == "succeeded"
    assert item.expected_revision == 1
    assert item.execution_root_id == item.id
    assert item.basis == {"report_refs": ["automation_portfolio"], "evidence_refs": []}

    await db_session.refresh(job)
    assert job.cron_expr == "0 9 * * 1"
    assert job.revision == 2

    # Standing grant → no per-item HitlRequest.
    reqs = list((await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.workspace_id == workspace.id,
            HitlRequest.action_key == AUTOMATION_UPDATE_ACTION_KEY,
        )
    )).scalars().all())
    assert reqs == []

    # Ledger chain: proposal_created → config_changed(causation=item).
    events = await _config_events(db_session, workspace)
    assert len(events) == 1
    assert events[0].causation_id == item.id
    assert events[0].payload["operation"] == "update"


async def test_v2_review_needs_human_then_cohort_approval_applies_change(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)

    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    digest = result["changes"][0]
    assert digest["outcome"] == "needs_human"
    assert digest["applied"] is False

    items = await _change_items(db_session, result["review_id"])
    item = items[0]
    assert item.status == "proposed"
    assert item.approval_request_id == digest["approval_request_id"]

    request = await db_session.get(HitlRequest, item.approval_request_id)
    assert request.status == "pending"
    assert request.action_key == AUTOMATION_UPDATE_ACTION_KEY
    assert request.dedup_key == f"proposal_item:{item.id}"

    await db_session.refresh(job)
    assert job.cron_expr == "0 6 * * *"  # nothing applied yet

    actor = generate_ulid()
    await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        actor_kind="user",
        actor_id=actor,
    )
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "succeeded"
    await db_session.refresh(job)
    assert job.cron_expr == "0 9 * * 1"
    assert job.revision == 2
    await db_session.refresh(request)
    assert request.status == "consumed"


async def test_v2_review_policy_deny_blocks_change(db_session, monkeypatch):
    from packages.core.governance.policy import WorkspacePolicy
    from packages.core.governance.service import update_policy

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    await update_policy(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        policy=WorkspacePolicy(never_allow_actions=[AUTOMATION_UPDATE_ACTION_KEY]),
    )
    await db_session.commit()

    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    digest = result["changes"][0]
    assert digest["outcome"] == "deny"
    assert digest["reason_code"] == "POLICY_BLOCKED"

    item = (await _change_items(db_session, result["review_id"]))[0]
    assert item.status == "rejected"
    assert item.decision["reason_code"] == "POLICY_BLOCKED"

    await db_session.refresh(job)
    assert job.cron_expr == "0 6 * * *"
    assert job.revision == 1


async def test_v2_review_stale_revision_never_reaches_approval(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)

    result = await _run_v2_review(
        db_session, monkeypatch, workspace,
        payload=_review_payload(job.id, revision=9),
    )
    digest = result["changes"][0]
    assert digest["outcome"] == "rejected"
    assert digest["reason_code"] == "STALE_REVISION"
    assert digest["approval_request_id"] is None

    item = (await _change_items(db_session, result["review_id"]))[0]
    assert item.status == "rejected"
    await db_session.refresh(job)
    assert job.revision == 1
    assert job.cron_expr == "0 6 * * *"


async def test_v2_review_reject_flow_denies_change_item(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    result = await _run_v2_review(
        db_session, monkeypatch, workspace, payload=_review_payload(job.id),
    )
    request_id = (await _change_items(db_session, result["review_id"]))[0].approval_request_id

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

    item = (await _change_items(db_session, result["review_id"]))[0]
    assert item.status == "rejected"
    assert item.decision["reason_code"] == "BAD_TIMING"
    request = await db_session.get(HitlRequest, request_id)
    assert request.status == "denied"
    await db_session.refresh(job)
    assert job.cron_expr == "0 6 * * *"


# ── briefing exposes the CAS token ────────────────────────────────────


async def test_consolidator_digests_expose_revision(db_session):
    from packages.core.consolidators.base import SnapshotContext
    from packages.core.consolidators.registry import REGISTRY
    from packages.core.review.service import begin_review

    workspace = await _seed_workspace(db_session)
    job = await _seed_job(db_session, workspace)
    goal = await _seed_goal(db_session, workspace)
    binding = await _seed_binding(db_session, workspace)
    await db_session.commit()

    review = await begin_review(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger="scheduled",
    )
    await db_session.flush()
    ctx = SnapshotContext(review=review, workspace=workspace, events=[])

    automation_report = await REGISTRY["automation_portfolio"].run(db_session, ctx)
    digest = automation_report.metrics["automations"][0]
    assert digest["job_id"] == job.id
    assert digest["revision"] == job.revision
    bindings = automation_report.metrics["workflow_bindings"]
    assert bindings[0]["binding_id"] == binding.id
    assert bindings[0]["revision"] == binding.revision

    goal_report = await REGISTRY["goal"].run(db_session, ctx)
    goal_digest = [g for g in goal_report.metrics["goals"] if g["goal_id"] == goal.id][0]
    assert goal_digest["revision"] == goal.revision
