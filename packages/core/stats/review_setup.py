"""Configure a real, scoped assessment Workflow using existing runtime services."""
from __future__ import annotations

from sqlalchemy import select

from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import AgentSubscription
from packages.core.models.workspace_stat import WorkspaceStat
from packages.core.stats.service import StatError, update_stat


REVIEW_WORKFLOW_SLUG = "workspace-evidence-assessment"


def review_workflow_spec(service_key: str, stat_keys: list[str]) -> dict:
    return {
        "slug": REVIEW_WORKFLOW_SLUG,
        "name": "Assess workspace metrics from evidence",
        "description": "Read current evidence, persist assessed metrics and report concrete gaps.",
        "trigger_type": "manual", "variables": {},
        "steps": [{
            "id": "assess", "name": "Assess and record metrics", "type": "agent", "next": [],
            "config": {
                "service_key": service_key, "stat_assessment_keys": sorted(stat_keys),
                "max_rounds": 30,
                "prompt": (
                    "Read get_stat_status and the current Workspace evidence using available read tools. "
                    "Follow each metric's assessment rubric and scale, preserving the Workspace-level scope. "
                    "Never transfer one release's readiness to another or invent an aggregate. "
                    "Record each required metric with record_stat_assessment. Use insufficient_evidence "
                    "with value=null and concrete gaps when the evidence cannot support a score. "
                    "Use the current Workflow run identity plus metric key as the stable idempotency key. "
                    "Only saved observation receipts count as completion. Internal read/validation problems "
                    "should be investigated with available tools; ask a person only for genuinely missing "
                    "human facts or required approval. Do not create, replace, publish or delete release assets."
                ),
            },
        }],
    }


def review_stat_config(rubric: str) -> dict:
    return {
        "workflow_slug": REVIEW_WORKFLOW_SLUG,
        "assessment": {"subject": "workspace", "rubric": rubric,
                       "scale": {"minimum": 0, "maximum": 100}, "missing_evidence": "unknown"},
    }


async def configure_review(db, *, workspace, user_id: str, service_key: str, rubrics: dict[str, str]):
    """Caller owns commit and explicit authorization; never run the Workflow here."""
    from packages.core.blueprints.installer import _install_workflow, _install_workflow_binding
    from packages.core.services.product_growth import persist_scheduled_job
    from packages.core.services.workspace_access import lock_workspace_access_boundary, user_can_write_workspace_id

    workspace = await lock_workspace_access_boundary(db, workspace_id=workspace.id, entity_id=workspace.entity_id)
    if workspace is None or not await user_can_write_workspace_id(
        db, workspace_id=workspace.id, entity_id=workspace.entity_id, user_id=user_id,
    ):
        raise StatError("workspace is not writable")
    subscriptions = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.entity_id == workspace.entity_id, AgentSubscription.workspace_id == workspace.id,
        AgentSubscription.service_key == service_key, AgentSubscription.status == "active",
    ))).scalars().all()
    if len(subscriptions) != 1:
        raise StatError("assessment service must resolve to one active subscription")
    stats = (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == workspace.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.key.in_(rubrics), WorkspaceStat.status == "active",
    ))).scalars().all()
    if not rubrics or {stat.key for stat in stats} != set(rubrics):
        raise StatError("every configured metric must exist in this workspace")
    spec = review_workflow_spec(service_key, list(rubrics))
    job_id = f"stat-review:{workspace.id}"
    job = (await db.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id))).scalar_one_or_none()
    if job is not None:
        from packages.core.models.workflow import WorkflowDefinition
        from packages.core.blueprints.installer import _blueprint_workflow_definition_values
        flow = (await db.execute(select(WorkflowDefinition).where(
            WorkflowDefinition.id == (job.execution_target or {}).get("workflow_id"),
            WorkflowDefinition.entity_id == workspace.entity_id,
            WorkflowDefinition.workspace_id == workspace.id,
        ))).scalar_one_or_none()
        if (job.entity_id != workspace.entity_id or job.workspace_id != workspace.id or
                flow is None or flow.steps != _blueprint_workflow_definition_values(spec)["steps"] or
                any(stat.collector_type != "agent_review" or
                    stat.collector_config != review_stat_config(rubrics[stat.key]) for stat in stats)):
            raise StatError("existing assessment setup differs; review its changes before replacing it")
        # Idempotent setup never resets an operator's paused Automation.
        return {"workflow_id": flow.id, "binding_id": job.execution_target.get("binding_id"),
                "job_id": job.job_id, "stat_ids": [stat.id for stat in stats]}
    if any(stat.collector_type == "agent_review" for stat in stats):
        raise StatError("assessment metrics already have a producer; manage the existing Workflow Automation")
    source = f"workspace-stat-review:{workspace.id}"
    workflow_id = await _install_workflow(
        db, entity_id=workspace.entity_id, workspace_id=workspace.id, w=spec,
        source_template_id=source, installed_by=user_id, adopt_unmapped=False,
    )
    binding_id = await _install_workflow_binding(
        db, entity_id=workspace.entity_id, workspace_id=workspace.id,
        workflow_id=workflow_id, w=spec, source_template_id=source,
    )
    for stat in stats:
        await update_stat(db, stat, collector_type="agent_review", collection_cadence=None,
                          description=rubrics[stat.key], collector_config=review_stat_config(rubrics[stat.key]))
    fields = {
        "name": "Assess workspace metrics from evidence", "execution_type": "workflow",
        "execution_target": {"workflow_id": workflow_id, "binding_id": binding_id},
        "user_id": user_id, "schedule_kind": "every", "every_seconds": 604800,
        "job_type": "interval", "enabled": workspace.status == "active" and bool(workspace.heartbeat_enabled),
    }
    job = ScheduledJob(job_id=job_id, entity_id=workspace.entity_id, workspace_id=workspace.id, **fields)
    await persist_scheduled_job(db, job)
    await db.flush()
    return {"workflow_id": workflow_id, "binding_id": binding_id, "job_id": job.job_id,
            "stat_ids": [stat.id for stat in stats]}
