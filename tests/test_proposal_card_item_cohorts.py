"""Non-task proposal cohorts reach the operator and resolve end-to-end.

The proposal card used to be posted only when the cohort contained kind=
"task" items, and ``_mirror_item_decisions`` was task-gated too. A proposal
made only of change / experiment items therefore minted a HitlRequest
nobody could see, and nothing would have applied it. Covered here:

* a change-only cohort posts a card whose ``pending_action.items`` carries a
  readable one-line summary per item, and approving it through the real
  resolve endpoint applies the change (row mutated, revision bumped, request
  granted+consumed, item succeeded);
* rejecting a change-only cohort marks the items rejected with the reason
  code and leaves the target row untouched;
* an experiment-only cohort gets a card and approving starts the experiment;
* a mixed cohort (1 task + 1 automation_change) approves both halves;
* ``approve_selected`` with a subset of item ids approves those and rejects
  the rest;
* the authority matrix: automation/workflow/experiment items demand
  ``approve_automation_changes``, goal items ``approve_goal_changes``, and a
  task-only cohort still passes on ``approve_tasks`` alone.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.experiment import Experiment
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Message, Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceStaff,
)
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

pytestmark = pytest.mark.asyncio

FLAG_KEY = "strategist_review_v2"

_seq = 0


# ── payload builders ──────────────────────────────────────────────────


def _task_entry(**overrides) -> dict:
    entry = {
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
    }
    entry.update(overrides)
    return entry


def _pause_automation(job_id: str, *, change_key: str = "pause_failing_digest") -> dict:
    return {
        "change_key": change_key,
        "target_kind": "scheduled_job",
        "operation": "pause",
        "target_id": job_id,
        "expected_revision": 1,
        "rationale": "The digest has failed every run this week.",
        "basis": {"report_refs": ["automation_portfolio"], "evidence_refs": []},
    }


def _raise_goal_target(goal_id: str) -> dict:
    return {
        "change_key": "raise_follower_target",
        "target_kind": "goal",
        "operation": "update_target",
        "target_id": goal_id,
        "expected_revision": 1,
        "patch": {"target_value": 2000},
        "rationale": "The current target was hit two cycles early.",
        "basis": {"report_refs": ["goal"], "evidence_refs": []},
    }


def _experiment(job_id: str) -> dict:
    return {
        "experiment_key": "shorter_prompt",
        "hypothesis": (
            "A shorter digest prompt should raise the automation success "
            "rate per the execution report."
        ),
        "target_kind": "scheduled_job",
        "target_id": job_id,
        "overlay_patch": {"payload_message": "patched digest message"},
        "max_runs": 5,
        "duration_days": 7,
        "success_metrics": {"success_rate": {"baseline": 0.5, "target": 0.9}},
        "guardrails": {"max_cost": 15, "rollback_on_consecutive_failures": 2},
    }


def _payload(**sections) -> dict:
    # ``tasks`` is a required (but freely empty) field of the Proposal
    # schema — an items-only cohort still declares it.
    return {"summary": "Cohort under test.", "tasks": [], **sections}


# ── seeding ───────────────────────────────────────────────────────────


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return {
        "headers": headers,
        "user_id": data["user_id"],
        "entity_id": me.json()["entity_id"],
    }


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


def _fake_completion(payload: dict):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _seed_cohort(
    client: AsyncClient,
    monkeypatch,
    username: str,
    *,
    payload_for,
) -> dict:
    """Register an owner, seed a workspace + automation + goal, then run one
    v2 review whose scripted completion is ``payload_for(job, goal)``.

    The REAL ``_post_proposal_chat`` runs — the card it posts is exactly what
    this suite is about.
    """
    global _seq
    import packages.core.database as dbmod

    owner = await _register(client, username)
    entity_id = owner["entity_id"]

    async with dbmod.async_session() as db:
        workspace = Workspace(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Item Cohort WS",
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
        agent = Agent(
            id=generate_ulid(), entity_id=entity_id, name="Ops Agent", status="active",
        )
        subscription = AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace.id,
            agent_id=agent.id,
            service_key="ops",
            status="active",
        )
        job = ScheduledJob(
            id=generate_ulid(),
            job_id=f"job_{generate_ulid()}",
            entity_id=entity_id,
            workspace_id=workspace.id,
            name="Daily digest",
            schedule_kind="cron",
            cron_expr="0 6 * * *",
            payload_message="original digest message",
            execution_type="agent",
            execution_target={"workspace_id": workspace.id},
            enabled=True,
        )
        db.add_all([workspace, goal, agent, subscription, job])
        await db.commit()

        await _set_flag(db, True)
        _seq += 1
        event = await record_event(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            event_type=et.EXECUTION_COMPLETED,
            source_kind="task",
            source_id=f"task_{_seq}",
            idempotency_key=f"item-cohort:{workspace.id}:{_seq}",
        )
        assert event is not None
        await db.commit()
        await asyncio.sleep(0.002)

        monkeypatch.setattr(
            "packages.core.strategist.prompt.runtime_execute_strategist_completion",
            _fake_completion(payload_for(job, goal)),
        )

        async def _noop_post(*args, **kwargs):
            return None
        monkeypatch.setattr(strategist_service, "_post_human_request_chat", _noop_post)
        monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)

        result = await _execute_strategist_review_cycle(db, workspace.id, "scheduled")
        assert not result.get("skipped"), result

    return {
        "headers": owner["headers"],
        "user_id": owner["user_id"],
        "entity_id": entity_id,
        "workspace_id": workspace.id,
        "goal_id": goal.id,
        "job_id": job.id,
        "review_id": result["review_id"],
        "task_ids": result["task_ids"],
        "result": result,
    }


# ── readers ───────────────────────────────────────────────────────────


async def _proposal_card(workspace_id: str) -> Message:
    import packages.core.database as dbmod
    from packages.core.models.task import Conversation

    async with dbmod.async_session() as db:
        rows = list((await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.workspace_id == workspace_id,
                Message.message_kind == "proposal",
            )
            .order_by(Message.created_at.desc())
        )).scalars().all())
    assert rows, f"no proposal card posted for workspace {workspace_id}"
    return rows[0]


async def _items(review_id: str) -> list[ProposalItemRecord]:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        return list((await db.execute(
            select(ProposalItemRecord)
            .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
            .where(ProposalRecord.review_id == review_id)
            .order_by(ProposalItemRecord.item_key.asc())
        )).scalars().all())


async def _get(model, pk):
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        return await db.get(model, pk)


async def _resolve(client: AsyncClient, ctx: dict, message_id: str, body: dict, *, headers=None):
    return await client.post(
        f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{message_id}/resolve",
        headers=headers or ctx["headers"],
        json=body,
    )


# ── change-only cohort ────────────────────────────────────────────────


async def test_change_only_cohort_posts_card_and_approval_applies(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_change",
        payload_for=lambda job, goal: _payload(
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    assert ctx["task_ids"] == []
    assert ctx["result"]["changes"][0]["outcome"] == "needs_human"

    card = await _proposal_card(ctx["workspace_id"])
    action = card.pending_action
    assert action["kind"] == "approve_proposals"
    # The task keys stay exactly as they were — an empty list, not absent.
    assert action["task_ids"] == []
    assert action["task_titles"] == []
    assert len(action["items"]) == 1
    entry = action["items"][0]
    assert entry["kind"] == "automation_change"
    assert entry["action_key"] == "workspace.proposal.automation_change.pause"
    assert entry["risk_level"] == "medium"
    assert entry["summary"] == 'Pause automation "Daily digest"'
    assert 'Pause automation "Daily digest"' in (card.content or "")

    item = (await _items(ctx["review_id"]))[0]
    assert entry["item_id"] == item.id
    request_id = item.approval_request_id
    assert request_id

    resp = await _resolve(client, ctx, card.id, {"choice": "approve"})
    assert resp.status_code == 200, resp.text

    item = (await _items(ctx["review_id"]))[0]
    assert item.status == "succeeded"
    assert item.decision["decision"] == "approved"
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is False
    assert job.revision == 2
    request = await _get(HitlRequest, request_id)
    assert request.status == "consumed"


async def test_change_only_cohort_reject_leaves_row_untouched(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_change_reject",
        payload_for=lambda job, goal: _payload(
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])
    request_id = (await _items(ctx["review_id"]))[0].approval_request_id

    resp = await _resolve(client, ctx, card.id, {
        "choice": "reject",
        "note": "Not this cycle",
        "payload": {"reason_code": "BAD_TIMING"},
    })
    assert resp.status_code == 200, resp.text

    item = (await _items(ctx["review_id"]))[0]
    assert item.status == "rejected"
    assert item.decision["reason_code"] == "BAD_TIMING"
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is True
    assert job.revision == 1
    request = await _get(HitlRequest, request_id)
    assert request.status == "denied"


async def test_goal_change_summary_reads_the_direction(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_goal",
        payload_for=lambda job, goal: _payload(
            goal_changes=[_raise_goal_target(goal.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])
    entry = card.pending_action["items"][0]
    assert entry["kind"] == "goal_change"
    assert entry["summary"] == 'Raise target of "Grow followers"'

    resp = await _resolve(client, ctx, card.id, {"choice": "approve"})
    assert resp.status_code == 200, resp.text
    goal = await _get(Goal, ctx["goal_id"])
    assert int(goal.target_value) == 2000


# ── experiment-only cohort ────────────────────────────────────────────


async def test_experiment_only_cohort_card_and_approval_starts_it(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_experiment",
        payload_for=lambda job, goal: _payload(experiments=[_experiment(job.id)]),
    )
    assert ctx["task_ids"] == []

    card = await _proposal_card(ctx["workspace_id"])
    entry = card.pending_action["items"][0]
    assert entry["kind"] == "experiment"
    assert entry["action_key"] == "workspace.proposal.experiment"
    assert entry["summary"].startswith("A shorter digest prompt")

    resp = await _resolve(client, ctx, card.id, {"choice": "approve"})
    assert resp.status_code == 200, resp.text

    item = (await _items(ctx["review_id"]))[0]
    assert item.status == "executing"
    experiment = await _get(Experiment, item.execution_root_id)
    assert experiment is not None
    assert experiment.status == "running"


# ── mixed cohort ──────────────────────────────────────────────────────


async def test_mixed_cohort_approves_task_and_change(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_mixed",
        payload_for=lambda job, goal: _payload(
            tasks=[_task_entry()],
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    assert len(ctx["task_ids"]) == 1

    card = await _proposal_card(ctx["workspace_id"])
    assert card.pending_action["task_ids"] == ctx["task_ids"]
    assert len(card.pending_action["items"]) == 1

    resp = await _resolve(client, ctx, card.id, {"choice": "approve"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"]["payload"]["approved_task_ids"] == ctx["task_ids"]

    by_kind = {item.kind: item for item in await _items(ctx["review_id"])}
    assert by_kind["task"].status == "approved"
    assert by_kind["automation_change"].status == "succeeded"
    task = await _get(Task, ctx["task_ids"][0])
    assert task.status == "in_progress"
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is False


async def test_approve_selected_splits_the_item_half(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_selected",
        payload_for=lambda job, goal: _payload(
            automation_changes=[_pause_automation(job.id)],
            goal_changes=[_raise_goal_target(goal.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])
    entries = {entry["kind"]: entry for entry in card.pending_action["items"]}
    assert set(entries) == {"automation_change", "goal_change"}

    resp = await _resolve(client, ctx, card.id, {
        "choice": "approve_selected",
        "payload": {
            "selected_task_ids": [],
            "selected_item_ids": [entries["automation_change"]["item_id"]],
        },
    })
    assert resp.status_code == 200, resp.text

    by_kind = {item.kind: item for item in await _items(ctx["review_id"])}
    assert by_kind["automation_change"].status == "succeeded"
    assert by_kind["goal_change"].status == "rejected"
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is False
    goal = await _get(Goal, ctx["goal_id"])
    assert int(goal.target_value) == 1000  # unselected → untouched


# ── authority matrix ──────────────────────────────────────────────────


async def _add_editor(ctx: dict, username: str) -> dict:
    """A workspace ``editor``: approve_tasks + approve_goal_changes by the
    role default map, never approve_automation_changes."""
    import packages.core.database as dbmod
    from packages.core.models.user import User
    from packages.core.services.auth_service import create_access_token, hash_password

    async with dbmod.async_session() as db:
        editor = User(
            entity_id=ctx["entity_id"],
            email=f"{username}@test.com",
            display_name=username,
            password_hash=hash_password("pass123"),
            role="member",
            status="active",
        )
        db.add(editor)
        await db.flush()
        db.add(WorkspaceStaff(
            workspace_id=ctx["workspace_id"],
            user_id=editor.id,
            role="editor",
            status="active",
            added_by=ctx["user_id"],
            added_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        editor_id = editor.id

    return {
        "Authorization": "Bearer " + create_access_token(
            editor_id, ctx["entity_id"], "member",
        ),
    }


async def test_change_cohort_requires_approve_automation_changes(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_authority",
        payload_for=lambda job, goal: _payload(
            tasks=[_task_entry()],
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])
    editor_headers = await _add_editor(ctx, "cohort_editor")

    denied = await _resolve(
        client, ctx, card.id, {"choice": "approve"}, headers=editor_headers,
    )
    assert denied.status_code == 403, denied.text
    assert "approve_automation_changes" in denied.json()["detail"]

    # Nothing moved — the card stays actionable for the owner.
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is True

    approved = await _resolve(client, ctx, card.id, {"choice": "approve"})
    assert approved.status_code == 200, approved.text
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is False


async def test_partial_selection_only_needs_the_picked_rows_authority(
    client: AsyncClient, monkeypatch,
):
    """The unified list lets a task-only approver ship the task half.

    The cohort carries an automation change (``approve_automation_changes``)
    the editor may NOT approve — but ``approve_selected`` with only the task
    ticked must pass on ``approve_tasks`` alone.
    """
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_partial_authority",
        payload_for=lambda job, goal: _payload(
            tasks=[_task_entry()],
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])
    editor_headers = await _add_editor(ctx, "partial_authority_editor")

    # The whole cohort still needs the automation authority.
    denied = await _resolve(
        client, ctx, card.id, {"choice": "approve"}, headers=editor_headers,
    )
    assert denied.status_code == 403, denied.text
    assert "approve_automation_changes" in denied.json()["detail"]

    resp = await _resolve(client, ctx, card.id, {
        "choice": "approve_selected",
        "payload": {
            "selected_task_ids": ctx["task_ids"],
            "selected_item_ids": [],
        },
    }, headers=editor_headers)
    assert resp.status_code == 200, resp.text

    task = await _get(Task, ctx["task_ids"][0])
    assert task.status == "in_progress"
    by_kind = {item.kind: item for item in await _items(ctx["review_id"])}
    assert by_kind["task"].status == "approved"
    # The unpicked automation change was rejected, never applied.
    assert by_kind["automation_change"].status == "rejected"
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is True


async def test_approve_selected_mixed_subset_splits_tasks_and_items(
    client: AsyncClient, monkeypatch,
):
    """One task + one change ticked out of a 2-task / 2-change cohort."""
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_mixed_subset",
        payload_for=lambda job, goal: _payload(
            tasks=[
                _task_entry(),
                {**_task_entry(), "task_key": "second_task", "title": "Second task"},
            ],
            automation_changes=[_pause_automation(job.id)],
            goal_changes=[_raise_goal_target(goal.id)],
        ),
    )
    assert len(ctx["task_ids"]) == 2
    card = await _proposal_card(ctx["workspace_id"])
    entries = {entry["kind"]: entry for entry in card.pending_action["items"]}
    assert set(entries) == {"automation_change", "goal_change"}

    resp = await _resolve(client, ctx, card.id, {
        "choice": "approve_selected",
        "payload": {
            "selected_task_ids": [ctx["task_ids"][0]],
            "selected_item_ids": [entries["goal_change"]["item_id"]],
        },
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"]["payload"]["approved_task_ids"] == [ctx["task_ids"][0]]

    approved_task = await _get(Task, ctx["task_ids"][0])
    assert approved_task.status == "in_progress"
    rejected_task = await _get(Task, ctx["task_ids"][1])
    assert rejected_task.status == "cancelled"

    by_kind = {item.kind: item for item in await _items(ctx["review_id"])}
    assert by_kind["goal_change"].status == "succeeded"
    assert by_kind["automation_change"].status == "rejected"
    goal = await _get(Goal, ctx["goal_id"])
    assert int(goal.target_value) == 2000
    job = await _get(ScheduledJob, ctx["job_id"])
    assert job.enabled is True  # unpicked → untouched


async def test_always_approve_grants_every_strategist_action_key(
    client: AsyncClient, monkeypatch,
):
    """"Always approve" is blanket: one click, every proposal type."""
    import packages.core.database as dbmod
    from packages.core.governance import get_policy
    from packages.core.models.governance import GovernanceRevision
    from packages.core.proposals.constants import STRATEGIST_ACTION_KEYS

    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_always",
        payload_for=lambda job, goal: _payload(
            tasks=[_task_entry()],
            automation_changes=[_pause_automation(job.id)],
        ),
    )
    card = await _proposal_card(ctx["workspace_id"])

    resp = await _resolve(client, ctx, card.id, {"choice": "always_approve"})
    assert resp.status_code == 200, resp.text

    async with dbmod.async_session() as db:
        policy = await get_policy(db, ctx["workspace_id"])
        revisions = list((await db.execute(
            select(GovernanceRevision).where(
                GovernanceRevision.workspace_id == ctx["workspace_id"]
            )
        )).scalars().all())
    granted = set(policy.auto_approve_actions)
    assert set(STRATEGIST_ACTION_KEYS) <= granted, sorted(granted)
    # Each key is its own auditable revision.
    summaries = {rev.change_summary for rev in revisions}
    for action_key in STRATEGIST_ACTION_KEYS:
        assert f"always-approve action: {action_key}" in summaries

    # The current cohort is still approved as before.
    task = await _get(Task, ctx["task_ids"][0])
    assert task.status == "in_progress"


async def test_task_only_cohort_still_passes_on_approve_tasks_alone(
    client: AsyncClient, monkeypatch,
):
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_task_only",
        payload_for=lambda job, goal: _payload(tasks=[_task_entry()]),
    )
    card = await _proposal_card(ctx["workspace_id"])
    assert card.pending_action["items"] == []
    editor_headers = await _add_editor(ctx, "task_only_editor")

    resp = await _resolve(
        client, ctx, card.id, {"choice": "approve"}, headers=editor_headers,
    )
    assert resp.status_code == 200, resp.text
    task = await _get(Task, ctx["task_ids"][0])
    assert task.status == "in_progress"


# ── readable priority / expected impact ───────────────────────────────


async def test_card_carries_structured_priority_and_resolved_goal_impact(
    client: AsyncClient, monkeypatch,
):
    """The card gets typed values, and the body says them in words.

    ``4`` and ``~+1`` used to be rendered into markdown and regex-recovered by
    the frontend. Now the priority stays a number in the payload, the metric
    delta arrives with the goal it moves, and the body text reads like a
    sentence for notifications and external channels.
    """
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_readable",
        payload_for=lambda job, goal: _payload(tasks=[_task_entry(
            priority=4,
            rationale="Followers stalled for two weeks.",
            estimated_impact={"goal_id": goal.id, "metric_delta": 1},
        )]),
    )
    card = await _proposal_card(ctx["workspace_id"])

    entry = card.pending_action["tasks"][0]
    assert entry["task_id"] == ctx["task_ids"][0]
    assert entry["title"] == "Draft source docs"
    assert entry["priority"] == 4
    assert entry["rationale"] == "Followers stalled for two weeks."
    # The goal is resolved server-side so the card can name what moves.
    assert entry["goal_id"] == ctx["goal_id"]
    assert entry["goal_title"] == "Grow followers"
    assert entry["metric_key"] == "follower_count"
    assert entry["metric_delta"] == 1.0
    # ``task_titles`` stays for older clients.
    assert card.pending_action["task_titles"] == ["Draft source docs"]

    # meta carries the same payload — it is the only channel a card without a
    # pending_action (auto-approved / policy-denied) has.
    meta_payload = card.meta["proposal"]
    assert meta_payload["tasks"] == card.pending_action["tasks"]
    assert meta_payload["summary"] == "Cohort under test."
    assert meta_payload["review_id"] == ctx["review_id"]

    body = card.content or ""
    assert (
        "• Draft source docs — high priority, expected +1 toward “Grow followers”"
        in body
    )
    assert "[4]" not in body
    assert "(~+1)" not in body


async def test_task_without_goal_link_omits_impact_entirely(
    client: AsyncClient, monkeypatch,
):
    """No goal to attach the prediction to → no number, not a partial payload."""
    ctx = await _seed_cohort(
        client, monkeypatch, "item_cohort_no_goal",
        payload_for=lambda job, goal: _payload(tasks=[_task_entry(
            priority=3,
            estimated_impact={"goal_id": None, "metric_delta": 7},
        )]),
    )
    card = await _proposal_card(ctx["workspace_id"])

    entry = card.pending_action["tasks"][0]
    assert entry["priority"] == 3
    for key in ("goal_id", "goal_title", "metric_key", "metric_delta"):
        assert key not in entry, entry
    body = card.content or ""
    # Medium priority is the model's default — unremarkable, so unlabelled.
    assert "• Draft source docs" in body
    assert "expected" not in body
    assert "+7" not in body
