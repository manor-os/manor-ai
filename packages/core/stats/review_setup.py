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


SCORE_SCALE = {"minimum": 0, "maximum": 100}
# Counts (views, comments, followers) have a floor but no ceiling.
COUNT_SCALE = {"minimum": 0}
_WEEK_SECONDS = 604800


def review_stat_config(rubric: str, scale: dict | None = None) -> dict:
    return {
        "workflow_slug": REVIEW_WORKFLOW_SLUG,
        "assessment": {"subject": "workspace", "rubric": rubric,
                       "scale": dict(scale or SCORE_SCALE), "missing_evidence": "unknown"},
    }


def _metric_spec(value) -> tuple[str, dict]:
    """A rubric string (a 0–100 score) or ``{"rubric": ..., "scale": ...}``."""
    if isinstance(value, str):
        return value, SCORE_SCALE
    if isinstance(value, dict) and isinstance(value.get("rubric"), str) and value["rubric"].strip():
        return value["rubric"], dict(value.get("scale") or SCORE_SCALE)
    raise StatError("each assessed metric needs a rubric")


def _review_steps(service_key: str, stat_keys: list[str]) -> list:
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values
    return _blueprint_workflow_definition_values(review_workflow_spec(service_key, stat_keys))["steps"]


async def _existing_review(db, workspace):
    """The Workspace's one assessment Workflow, its binding and schedule, if any.

    Found by its Workflow slug, so a Blueprint-installed assessment is
    recognised as well as one this module created.
    """
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    bindings = [binding for binding in (await db.execute(select(WorkflowBinding).where(
        WorkflowBinding.entity_id == workspace.entity_id, WorkflowBinding.workspace_id == workspace.id,
    ))).scalars() if (binding.config or {}).get("workspace_blueprint_workflow_slug") == REVIEW_WORKFLOW_SLUG]
    if not bindings:
        return None
    if len(bindings) > 1:
        raise StatError("this workspace has more than one assessment Workflow; keep one before adding metrics")
    binding = bindings[0]
    flow = (await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.id == binding.workflow_id, WorkflowDefinition.entity_id == workspace.entity_id,
    ))).scalar_one_or_none()
    jobs = [job for job in (await db.execute(select(ScheduledJob).where(
        ScheduledJob.entity_id == workspace.entity_id, ScheduledJob.workspace_id == workspace.id,
    ))).scalars() if (job.execution_target or {}).get("binding_id") == binding.id]
    if flow is None or len(jobs) != 1:
        raise StatError("the existing assessment Workflow has no single schedule; review it before adding metrics")
    return flow, binding, jobs[0]


async def configure_review(db, *, workspace, user_id: str, service_key: str, rubrics: dict,
                           every_seconds: int | None = None):
    """Caller owns commit and explicit authorization; never run the Workflow here.

    ``rubrics`` maps stat keys to a rubric (a 0–100 score) or to
    ``{"rubric": ..., "scale": {...}}``. Repeating a call changes nothing; new
    metrics join an unmodified existing assessment instead of creating a
    second producer.
    """
    from packages.core.blueprints.installer import _install_workflow, _install_workflow_binding
    from packages.core.services.product_growth import persist_scheduled_job
    from packages.core.services.workspace_access import lock_workspace_access_boundary, user_can_write_workspace_id

    workspace = await lock_workspace_access_boundary(db, workspace_id=workspace.id, entity_id=workspace.entity_id)
    if workspace is None or not await user_can_write_workspace_id(
        db, workspace_id=workspace.id, entity_id=workspace.entity_id, user_id=user_id,
    ):
        raise StatError("workspace is not writable")
    specs = {key: _metric_spec(value) for key, value in (rubrics or {}).items()}
    stats = (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == workspace.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.key.in_(specs), WorkspaceStat.status == "active",
    ))).scalars().all()
    if not specs or {stat.key for stat in stats} != set(specs):
        raise StatError("every configured metric must exist in this workspace")

    existing = await _existing_review(db, workspace)
    if existing is not None:
        flow, binding, job = existing
        producer = next((step.get("config") or {} for step in flow.steps or [] if step.get("type") == "agent"), {})
        current_keys = list(producer.get("stat_assessment_keys") or [])
        current_service = str(producer.get("service_key") or "")
        if current_service != service_key:
            raise StatError(f"metrics here are assessed by the {current_service} service; use it to add more")
        if flow.steps != _review_steps(current_service, current_keys) or any(
            stat.collector_type == "agent_review" and (
                stat.key not in current_keys or stat.collector_config != review_stat_config(*specs[stat.key]))
            for stat in stats
        ):
            raise StatError("existing assessment setup differs; review its changes before replacing it")
        new_keys = [key for key in specs if key not in current_keys]
        if new_keys:
            flow.steps = _review_steps(current_service, current_keys + new_keys)
            flow.version = int(flow.version or 1) + 1
            for stat in stats:
                if stat.key in new_keys:
                    rubric, scale = specs[stat.key]
                    await update_stat(db, stat, collector_type="agent_review", collection_cadence=None,
                                      description=rubric, collector_config=review_stat_config(rubric, scale))
        # Joining metrics may make assessment more frequent, never rarer. An
        # operator's paused Automation stays paused.
        if every_seconds and (job.every_seconds is None or every_seconds < job.every_seconds):
            job.every_seconds = every_seconds
        return {"workflow_id": flow.id, "binding_id": binding.id, "job_id": job.job_id,
                "stat_ids": [stat.id for stat in stats], "enabled": bool(job.enabled)}

    if any(stat.collector_type == "agent_review" for stat in stats):
        raise StatError("assessment metrics already have a producer; manage the existing Workflow Automation")
    subscriptions = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.entity_id == workspace.entity_id, AgentSubscription.workspace_id == workspace.id,
        AgentSubscription.service_key == service_key, AgentSubscription.status == "active",
    ))).scalars().all()
    if len(subscriptions) != 1:
        raise StatError("assessment service must resolve to one active subscription")
    spec = review_workflow_spec(service_key, list(specs))
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
        rubric, scale = specs[stat.key]
        await update_stat(db, stat, collector_type="agent_review", collection_cadence=None,
                          description=rubric, collector_config=review_stat_config(rubric, scale))
    fields = {
        "name": "Assess workspace metrics from evidence", "execution_type": "workflow",
        "execution_target": {"workflow_id": workflow_id, "binding_id": binding_id},
        "user_id": user_id, "schedule_kind": "every", "every_seconds": every_seconds or _WEEK_SECONDS,
        # The heartbeat drives the Strategist only. Like any Automation, the
        # assessment runs on its own; a paused Workspace is skipped at dispatch.
        "job_type": "interval", "enabled": True,
    }
    job = ScheduledJob(job_id=f"stat-review:{workspace.id}", entity_id=workspace.entity_id,
                       workspace_id=workspace.id, **fields)
    await persist_scheduled_job(db, job)
    await db.flush()
    return {"workflow_id": workflow_id, "binding_id": binding_id, "job_id": job.job_id,
            "stat_ids": [stat.id for stat in stats], "enabled": bool(job.enabled)}
