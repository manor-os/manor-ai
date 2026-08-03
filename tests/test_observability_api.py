"""M14 observability API — timeline / strategy reviews / automation health.

Seeds a full review chain directly (review → reports → proposal → items →
ledger events) and exercises the read-side endpoints:

* timeline selector validation (exactly one of root/review/correlation);
* timeline by root_execution_id — id-ordered events + causes echo;
* timeline by review_id — the M14 tree (review/reports/proposal/executions);
* strategy/reviews list aggregates (report status + item kind/status counts);
* strategy/reviews/{id} full detail (briefing as stored, payload/basis);
* automation-health 30d ledger aggregation incl. a missed run and an
  active experiment overlay;
* task provenance — proposal-item / scheduled-job / manual triggers, the
  causation chain, config_versions from the terminal event, cross-
  workspace 404;
* auth — a user from another entity gets 404 (sibling-router convention).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from auth_helpers import register_user_and_get_token
from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.experiment import Experiment
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.review_run import ReviewRun
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowBinding
from packages.core.models.workspace import Workspace

pytestmark = pytest.mark.asyncio


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str]:
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
    return headers, me.json()["entity_id"]


async def _seed(client: AsyncClient, username: str) -> dict:
    """Owner + workspace + review/reports/proposal/items + ledger chain."""
    import packages.core.database as dbmod

    headers, entity_id = await _register_owner(client, username)
    now = datetime.now(timezone.utc)

    async with dbmod.async_session() as db:
        ws = Workspace(
            id=generate_ulid(), entity_id=entity_id,
            name="Observability WS", status="active", settings={},
        )
        db.add(ws)

        review = ReviewRun(
            entity_id=entity_id, workspace_id=ws.id,
            trigger_kind="scheduled", status="succeeded",
            watermark_start=None, watermark_end=generate_ulid(),
            window_start=now - timedelta(hours=1), window_end=now,
            briefing={"coverage_gaps": ["execution: partial"], "sections": {}},
            completed_at=now,
        )
        db.add(review)
        await db.flush()

        for domain, status in (("goal", "complete"), ("execution", "failed")):
            db.add(ConsolidationReport(
                entity_id=entity_id, workspace_id=ws.id, review_id=review.id,
                domain=domain, scope={}, status=status,
                summary=f"{domain} digest",
                metrics={"events": 3},
                analyzer_version=f"{domain}-v1", input_hash="0" * 64,
            ))

        proposal = ProposalRecord(
            entity_id=entity_id, workspace_id=ws.id, review_id=review.id,
            summary="Cycle proposal", status="resolved",
            notes="Suppressed 1 meta/bookkeeping task proposal(s): review the review",
        )
        db.add(proposal)
        await db.flush()

        root_id = f"batch-{generate_ulid()}"
        item_approved = ProposalItemRecord(
            proposal_id=proposal.id, entity_id=entity_id, workspace_id=ws.id,
            item_key="draft_docs", kind="task", status="succeeded",
            payload={"title": "Draft docs", "task_id": "t1"},
            basis={"report_refs": ["goal"], "evidence_refs": ["evt:1"]},
            risk_level="low", action_key="workspace.proposal.task",
            decision={"decision": "approved", "decided_by": "u1"},
            execution_root_id=root_id,
            decided_at=now,
        )
        item_rejected = ProposalItemRecord(
            proposal_id=proposal.id, entity_id=entity_id, workspace_id=ws.id,
            item_key="spam_task", kind="task", status="rejected",
            payload={"title": "Spam"},
            basis={"report_refs": ["execution"], "evidence_refs": []},
            risk_level="low", action_key="workspace.proposal.task",
            decision={"decision": "rejected", "reason_code": "NOT_RELEVANT"},
            decided_at=now,
        )
        db.add_all([item_approved, item_rejected])
        await db.flush()

        # Execution chain under the approved item's root, caused by the item.
        for i, (etype, status) in enumerate((
            (et.EXECUTION_REQUESTED, "requested"),
            (et.EXECUTION_STARTED, "running"),
            (et.EXECUTION_COMPLETED, "succeeded"),
        )):
            await record_event(
                db,
                entity_id=entity_id, workspace_id=ws.id,
                event_type=etype, source_kind="task", source_id="t1",
                status=status,
                root_execution_id=root_id,
                causation_id=item_approved.id,
                correlation_id="corr-goal-week",
                occurred_at=now - timedelta(minutes=30 - i),
                idempotency_key=f"t1:{etype}",
            )
        # The cause echo target: an event whose source_id IS the causation id.
        await record_event(
            db,
            entity_id=entity_id, workspace_id=ws.id,
            event_type=et.PROPOSAL_ITEM_APPROVED,
            source_kind="proposal", source_id=item_approved.id,
            status="approved",
            occurred_at=now - timedelta(minutes=35),
            idempotency_key=f"item:{item_approved.id}:approved",
        )

        # ── automation health fixtures ────────────────────────────
        experiment = Experiment(
            entity_id=entity_id, workspace_id=ws.id,
            hypothesis="faster cadence helps", status="running",
            scope={}, success_metrics={}, guardrails={}, overlay_patch={},
        )
        db.add(experiment)
        await db.flush()

        job = ScheduledJob(
            job_id=f"job-{generate_ulid()}",
            entity_id=entity_id, workspace_id=ws.id,
            name="Daily digest", schedule_kind="interval", every_seconds=3600,
            enabled=True, consecutive_errors=2, last_status="error",
            last_run_at=now - timedelta(hours=2),
            revision=3,
            execution_target={
                "_experiment_overlay": {
                    "experiment_id": experiment.id, "patch": {"x": 1},
                },
            },
        )
        disabled_job = ScheduledJob(
            job_id=f"job-off-{generate_ulid()}",
            entity_id=entity_id, workspace_id=ws.id,
            name="Disabled", enabled=False,
        )
        binding = WorkflowBinding(
            entity_id=entity_id, workflow_id=generate_ulid(),
            workspace_id=ws.id, name="Weekly report flow",
            trigger_type="schedule", trigger_config={"cron": "0 9 * * 1"},
            enabled=True, revision=2,
        )
        db.add_all([job, disabled_job, binding])
        await db.flush()

        for i, (etype, status) in enumerate((
            (et.AUTOMATION_RUN_DISPATCHED, "dispatched"),
            (et.AUTOMATION_RUN_COMPLETED, "success"),
            (et.AUTOMATION_RUN_DISPATCHED, "dispatched"),
            (et.AUTOMATION_RUN_FAILED, "failed"),
            (et.AUTOMATION_RUN_MISSED, "missed"),
        )):
            await record_event(
                db,
                entity_id=entity_id, workspace_id=ws.id,
                event_type=etype, source_kind="scheduled_job",
                source_id=job.id, status=status,
                occurred_at=now - timedelta(days=1, minutes=i),
                idempotency_key=f"sj:{job.id}:{i}:{etype}",
            )
        # Outside the 30d window — must NOT be counted.
        await record_event(
            db,
            entity_id=entity_id, workspace_id=ws.id,
            event_type=et.AUTOMATION_RUN_COMPLETED, source_kind="scheduled_job",
            source_id=job.id, status="success",
            occurred_at=now - timedelta(days=40),
            idempotency_key=f"sj:{job.id}:old:completed",
        )
        await record_event(
            db,
            entity_id=entity_id, workspace_id=ws.id,
            event_type=et.WORKFLOW_RUN_COMPLETED, source_kind="workflow",
            source_id=binding.id, status="completed",
            occurred_at=now - timedelta(hours=3),
            idempotency_key=f"wf:{binding.id}:completed",
        )
        # ── provenance fixtures ───────────────────────────────────
        # Three tasks with three different origins, on their own execution
        # roots so the review-timeline assertions above stay exact.
        prov_root = f"prov-{generate_ulid()}"
        task_from_item = Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            title="Task from proposal item", status="completed",
            details={
                "proposal_item_id": item_approved.id,
                "root_execution_id": prov_root,
            },
        )
        task_from_job = Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            title="Task from automation", status="in_progress",
            details={"scheduled_job_id": job.id},
        )
        task_manual = Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=ws.id,
            title="Task typed by a human", status="pending", details={},
        )
        other_ws = Workspace(
            id=generate_ulid(), entity_id=entity_id,
            name="Other WS", status="active", settings={},
        )
        db.add(other_ws)
        await db.flush()
        foreign_task = Task(
            id=generate_ulid(), entity_id=entity_id, workspace_id=other_ws.id,
            title="Not in this workspace", status="pending", details={},
        )
        db.add_all([task_from_item, task_from_job, task_manual, foreign_task])

        # Started stamps an early revision; the terminal event's stamp wins.
        for etype, status, versions in (
            (et.EXECUTION_STARTED, "in_progress", {"agent_revision": 3}),
            (
                et.EXECUTION_COMPLETED, "completed",
                {"agent_revision": 4, "skill_revision": 2},
            ),
        ):
            await record_event(
                db,
                entity_id=entity_id, workspace_id=ws.id,
                event_type=etype, source_kind="task",
                source_id=task_from_item.id, status=status,
                root_execution_id=prov_root,
                causation_id=item_approved.id,
                config_versions=versions,
                occurred_at=now - timedelta(minutes=10),
                idempotency_key=f"prov:{task_from_item.id}:{etype}",
            )
        await db.commit()

        return {
            "headers": headers,
            "entity_id": entity_id,
            "workspace_id": ws.id,
            "other_workspace_id": other_ws.id,
            "prov_root": prov_root,
            "task_from_item_id": task_from_item.id,
            "task_from_job_id": task_from_job.id,
            "task_manual_id": task_manual.id,
            "foreign_task_id": foreign_task.id,
            "review_id": review.id,
            "proposal_id": proposal.id,
            "item_approved_id": item_approved.id,
            "root_id": root_id,
            "job_id": job.id,
            "binding_id": binding.id,
            "experiment_id": experiment.id,
        }


# ── timeline ──────────────────────────────────────────────────────────

async def test_timeline_requires_exactly_one_selector(client: AsyncClient):
    seed = await _seed(client, "obs_selector")
    base = f"/api/v1/workspaces/{seed['workspace_id']}/timeline"

    none = await client.get(base, headers=seed["headers"])
    assert none.status_code == 400

    both = await client.get(
        f"{base}?root_execution_id=r1&review_id={seed['review_id']}",
        headers=seed["headers"],
    )
    assert both.status_code == 400


async def test_timeline_by_root_orders_events_and_echoes_causes(client: AsyncClient):
    seed = await _seed(client, "obs_root")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/timeline"
        f"?root_execution_id={seed['root_id']}",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selector"] == {"root_execution_id": seed["root_id"]}

    events = body["events"]
    assert [e["event_type"] for e in events].count("execution_completed") == 1
    assert len(events) == 3
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)  # ordered by ledger id
    assert all(e["root_execution_id"] == seed["root_id"] for e in events)
    assert all(e["causation_id"] == seed["item_approved_id"] for e in events)

    # causes echo: the causation id maps to the proposal_item_approved
    # event's id (ids only, no nested events).
    causes = body["causes"]
    assert seed["item_approved_id"] in causes
    assert len(causes[seed["item_approved_id"]]) == 1


async def test_timeline_by_review_returns_tree(client: AsyncClient):
    seed = await _seed(client, "obs_tree")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/timeline"
        f"?review_id={seed['review_id']}",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["review"]["id"] == seed["review_id"]
    assert body["review"]["status"] == "succeeded"
    assert body["review"]["trigger_kind"] == "scheduled"

    domains = {r["domain"]: r["status"] for r in body["reports"]}
    assert domains == {"goal": "complete", "execution": "failed"}

    proposal = body["proposal"]
    assert proposal["id"] == seed["proposal_id"]
    items = {i["item_key"]: i for i in proposal["items"]}
    assert items["draft_docs"]["status"] == "succeeded"
    assert items["draft_docs"]["decision"]["decision"] == "approved"
    assert items["draft_docs"]["execution_root_id"] == seed["root_id"]
    assert items["spam_task"]["decision"]["reason_code"] == "NOT_RELEVANT"

    executions = body["executions"]
    assert list(executions) == [seed["root_id"]]
    assert [e["event_type"] for e in executions[seed["root_id"]]] == [
        "execution_requested", "execution_started", "execution_completed",
    ]


async def test_timeline_by_correlation(client: AsyncClient):
    seed = await _seed(client, "obs_corr")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/timeline"
        "?correlation_id=corr-goal-week",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 3
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)
    assert all(e["correlation_id"] == "corr-goal-week" for e in events)


# ── strategy reviews ──────────────────────────────────────────────────

async def test_strategy_reviews_list_counts(client: AsyncClient):
    seed = await _seed(client, "obs_list")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/strategy/reviews",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    reviews = resp.json()["reviews"]
    assert len(reviews) == 1
    digest = reviews[0]
    assert digest["id"] == seed["review_id"]
    assert digest["reports"] == {"complete": 1, "partial": 0, "failed": 1}
    assert digest["proposal_id"] == seed["proposal_id"]
    assert digest["item_counts"] == [
        {"kind": "task", "status": "rejected", "count": 1},
        {"kind": "task", "status": "succeeded", "count": 1},
    ]
    # The digest never inlines the (potentially large) briefing.
    assert "briefing" not in digest


async def test_strategy_review_detail_full(client: AsyncClient):
    seed = await _seed(client, "obs_detail")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/strategy/reviews/"
        f"{seed['review_id']}",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["review"]["briefing"]["coverage_gaps"] == ["execution: partial"]
    goal_report = next(r for r in body["reports"] if r["domain"] == "goal")
    assert goal_report["metrics"] == {"events": 3}
    assert goal_report["input_hash"] == "0" * 64

    items = {i["item_key"]: i for i in body["proposal"]["items"]}
    assert items["draft_docs"]["payload"]["task_id"] == "t1"
    assert items["draft_docs"]["basis"]["report_refs"] == ["goal"]
    assert items["spam_task"]["decision"]["reason_code"] == "NOT_RELEVANT"
    assert "Suppressed" in body["proposal"]["notes"]

    missing = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/strategy/reviews/"
        f"{generate_ulid()}",
        headers=seed["headers"],
    )
    assert missing.status_code == 404


# ── automation health ─────────────────────────────────────────────────

async def test_automation_health_aggregates(client: AsyncClient):
    seed = await _seed(client, "obs_health")
    resp = await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/automation-health",
        headers=seed["headers"],
    )
    assert resp.status_code == 200
    automations = {a["id"]: a for a in resp.json()["automations"]}

    # Disabled job excluded; enabled job + binding present.
    assert set(automations) == {seed["job_id"], seed["binding_id"]}

    job = automations[seed["job_id"]]
    assert job["kind"] == "scheduled_job"
    assert job["name"] == "Daily digest"
    assert job["schedule"]["kind"] == "interval"
    assert job["schedule"]["every_seconds"] == 3600
    assert job["revision"] == 3
    assert job["consecutive_errors"] == 2
    assert job["last_status"] == "error"
    # 40d-old completion excluded from the 30d window.
    assert job["runs_30d"] == {
        "dispatched": 2, "completed": 1, "failed": 1, "missed": 1,
    }
    assert job["active_experiment"] == {
        "id": seed["experiment_id"], "status": "running",
    }

    binding = automations[seed["binding_id"]]
    assert binding["kind"] == "workflow_binding"
    assert binding["schedule"]["kind"] == "schedule"
    assert binding["schedule"]["cron_expr"] == "0 9 * * 1"
    assert binding["revision"] == 2
    assert binding["runs_30d"] == {
        "dispatched": 0, "completed": 1, "failed": 0, "missed": 0,
    }
    assert binding["active_experiment"] is None


# ── task provenance ───────────────────────────────────────────────────

async def _provenance(client: AsyncClient, seed: dict, task_id: str):
    return await client.get(
        f"/api/v1/workspaces/{seed['workspace_id']}/tasks/{task_id}/provenance",
        headers=seed["headers"],
    )


async def test_provenance_proposal_item_chain_and_config_versions(client: AsyncClient):
    seed = await _seed(client, "obs_prov_item")
    resp = await _provenance(client, seed, seed["task_from_item_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["task"] == {
        "id": seed["task_from_item_id"],
        "title": "Task from proposal item",
        "status": "completed",
    }
    assert body["root_execution_id"] == seed["prov_root"]

    assert body["trigger"]["kind"] == "proposal_item"
    assert body["trigger"]["id"] == seed["item_approved_id"]
    assert body["trigger"]["label"] == "Draft docs"

    # review → proposal item → this task
    chain = body["causation_chain"]
    assert [step["kind"] for step in chain] == ["review", "proposal_item", "task"]
    assert chain[0]["id"] == seed["review_id"]
    assert chain[0]["label"] == f"Review {seed['review_id']}"
    assert chain[2]["id"] == seed["task_from_item_id"]

    # Terminal event's stamp wins over the started event's.
    assert body["config_versions"] == {"agent_revision": 4, "skill_revision": 2}

    assert [e["event_type"] for e in body["events"]] == [
        "execution_started", "execution_completed",
    ]


async def test_provenance_scheduled_job_trigger(client: AsyncClient):
    seed = await _seed(client, "obs_prov_job")
    resp = await _provenance(client, seed, seed["task_from_job_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["trigger"] == {
        "kind": "scheduled_job", "id": seed["job_id"], "label": "Daily digest",
    }
    assert [step["kind"] for step in body["causation_chain"]] == [
        "scheduled_job", "task",
    ]
    # No ledger events yet — the task is its own root.
    assert body["root_execution_id"] == seed["task_from_job_id"]
    assert body["config_versions"] == {}
    assert body["events"] == []


async def test_provenance_manual_task(client: AsyncClient):
    seed = await _seed(client, "obs_prov_manual")
    resp = await _provenance(client, seed, seed["task_manual_id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["trigger"] == {"kind": "manual", "id": None, "label": None}
    assert [step["kind"] for step in body["causation_chain"]] == ["task"]
    assert body["config_versions"] == {}


async def test_provenance_cross_workspace_404(client: AsyncClient):
    seed = await _seed(client, "obs_prov_404")

    # A real task, but in another workspace of the same entity.
    foreign = await _provenance(client, seed, seed["foreign_task_id"])
    assert foreign.status_code == 404

    missing = await _provenance(client, seed, generate_ulid())
    assert missing.status_code == 404


# ── auth ──────────────────────────────────────────────────────────────

async def test_non_member_gets_404(client: AsyncClient):
    seed = await _seed(client, "obs_owner")
    outsider_headers, _ = await _register_owner(client, "obs_outsider")

    ws = seed["workspace_id"]
    for path in (
        f"/api/v1/workspaces/{ws}/timeline?review_id={seed['review_id']}",
        f"/api/v1/workspaces/{ws}/strategy/reviews",
        f"/api/v1/workspaces/{ws}/strategy/reviews/{seed['review_id']}",
        f"/api/v1/workspaces/{ws}/automation-health",
        f"/api/v1/workspaces/{ws}/tasks/{seed['task_manual_id']}/provenance",
    ):
        resp = await client.get(path, headers=outsider_headers)
        assert resp.status_code == 404, path
