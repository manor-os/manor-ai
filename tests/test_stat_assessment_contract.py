from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import Workspace, WorkspaceStaff, AgentSubscription
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.goals.service import create_goal, record_measurement
from packages.core.stats.service import create_stat, record_observation, update_stat
from packages.core.stats.review_setup import review_stat_config, configure_review
from packages.core.ai.runtime.stat_actions import runtime_stat_action
from packages.core.ai.runtime.stat_assessment_contract import StatAssessmentContract
from packages.core.ai.runtime.final_response import FinalResponseAction


async def setup(db, monkeypatch, *, review=True):
    from packages.core import database
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Assessment test", status="active")
    user_id = generate_ulid()
    db.add_all([workspace, WorkspaceStaff(workspace_id=workspace.id, user_id=user_id, role="owner", status="active")])
    await db.flush()
    stat = await create_stat(db, entity_id=workspace.entity_id, workspace_id=workspace.id,
                            key="readiness", name="Readiness", collector_type="agent_review" if review else "manual",
                            collector_config=review_stat_config("Score only verified evidence") if review else {},
                            collection_cadence=None)
    @asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(database, "async_session", session)
    return workspace, user_id, stat


def _assessment_recipe() -> dict:
    from packages.core.stats.review_setup import review_stat_config

    keys = ("source_quality", "delivery_readiness")
    return {
        "stats": [
            {
                "key": key,
                "name": key.replace("_", " ").title(),
                "unit": "score",
                "window": "latest",
                "value_type": "number",
                "description": "Review only verified Workspace evidence.",
                "collector_type": "agent_review",
                "collector_config": review_stat_config(
                    "Review only verified Workspace evidence."
                ),
                "collection_cadence": None,
            }
            for key in keys
        ],
        "workflows": [{
            "slug": "workspace-evidence-assessment",
            "name": "Assess Workspace Evidence",
            "trigger_type": "manual",
            "steps": [{
                "id": "assess",
                "type": "agent",
                "config": {
                    "service_key": "readiness_and_learning",
                    "stat_assessment_keys": list(keys),
                },
            }],
        }],
        "scheduled_jobs": [{
            "job_id": "workspace-evidence-assessment",
            "execution_type": "workflow",
            "execution_target": {"workflow_slug": "workspace-evidence-assessment"},
        }],
        "subscriptions": [{"service_key": "readiness_and_learning"}],
    }


def unknown(stat, key="run:readiness"):
    return {"stat_id": stat.id, "expected_revision": stat.revision, "idempotency_key": key,
            "status": "insufficient_evidence", "value": None, "evidence": [],
            "gaps": ["No verified readiness report"], "reasoning": "No numeric score is justified"}


async def act(workspace, user, params, run="run-a", action="record_stat_assessment"):
    return json.loads(await runtime_stat_action(action=action, entity_id=workspace.entity_id,
                      workspace_id=workspace.id, user_id=user, params=params, origin={"workflow_run_id": run}))


@pytest.mark.asyncio
async def test_bound_goal_write_updates_stat_and_single_projection(db_session, monkeypatch):
    ws, _, stat = await setup(db_session, monkeypatch, review=False)
    goal = await create_goal(db_session, entity_id=ws.entity_id, workspace_id=ws.id,
                             stat_id=stat.id, title="Readiness", metric_key=stat.key, target_value=100)
    measurement = await record_measurement(db_session, goal, value=45, source="manual", meta={"idempotency_key": "once"})
    again = await record_measurement(db_session, goal, value=45, source="manual", meta={"idempotency_key": "once"})
    assert measurement == again
    assert stat.current_value == goal.current_value == Decimal(45)
    assert measurement.meta["observation_id"]
    rows = (await db_session.execute(select(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_unknown_has_receipt_and_preserves_last_known_value(db_session, monkeypatch):
    ws, user, stat = await setup(db_session, monkeypatch, review=False)
    await record_observation(db_session, stat, value=40, source="manual")
    await update_stat(db_session, stat, collector_type="agent_review", collector_config=review_stat_config("Use evidence"))
    payload = unknown(stat)
    result = await act(ws, user, payload)
    assert result["status"] == "persisted"
    assert result["value"] is None and result["assessment_status"] == "insufficient_evidence"
    assert stat.current_value == Decimal(40)
    assert stat.last_collection_status == "insufficient_evidence"
    assert stat.last_assessment["evidence"]["gaps"] == payload["gaps"]
    assert (await act(ws, user, payload))["observation_id"] == result["observation_id"]
    conflict = await act(ws, user, {**payload, "gaps": ["Different conclusion"]})
    assert "idempotency" in json.dumps(conflict)


@pytest.mark.asyncio
async def test_contract_requires_this_run_and_actual_database_receipts(db_session, monkeypatch):
    ws, user, stat = await setup(db_session, monkeypatch)
    contract = StatAssessmentContract(entity_id=ws.entity_id, workspace_id=ws.id, workflow_run_id="run-a", stat_keys=[stat.key])
    assert (await contract.validate('{"status":"persisted"}')).action == FinalResponseAction.CONTINUE_EXECUTION
    await act(ws, user, unknown(stat, "old"), run="previous-run")
    assert (await contract.validate("Done")).action == FinalResponseAction.CONTINUE_EXECUTION
    receipt = await act(ws, user, unknown(stat))
    result = await contract.validate("Evidence missing")
    assert result.action == FinalResponseAction.ACCEPT
    assert json.loads(result.content)["assessments"][0]["observation_id"] == receipt["observation_id"]
    await update_stat(db_session, stat, description="Changed definition")
    assert (await contract.validate("Done")).action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"expected_revision": 999}, {"value": 0}, {"gaps": []}, {"reasoning": ""},
    {"status": "measured", "value": 120}, {"status": "measured", "value": 50},
])
async def test_invalid_assessment_returns_contract_without_writing(db_session, monkeypatch, change):
    ws, user, stat = await setup(db_session, monkeypatch)
    result = await act(ws, user, {**unknown(stat), **change})
    assert "stat_assessment_invalid" in json.dumps(result)
    assert stat.last_assessment is None
    assert (await db_session.execute(select(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))).scalars().all() == []


@pytest.mark.asyncio
async def test_review_cannot_be_bypassed_by_legacy_goal_writer(db_session, monkeypatch):
    ws, _, stat = await setup(db_session, monkeypatch)
    goal = await create_goal(db_session, entity_id=ws.entity_id, workspace_id=ws.id, stat_id=stat.id,
                            title="Readiness", metric_key=stat.key, target_value=100)
    with pytest.raises(ValueError, match="record_stat_assessment"):
        await record_measurement(db_session, goal, value=100, source="manual")
    with pytest.raises(ValueError):
        await record_observation(db_session, stat, value=100, source="agent_assessment")
    assert stat.current_value is None


@pytest.mark.asyncio
async def test_old_observation_does_not_roll_back_latest_stat_or_goal(db_session, monkeypatch):
    ws, _, stat = await setup(db_session, monkeypatch, review=False)
    goal = await create_goal(db_session, entity_id=ws.entity_id, workspace_id=ws.id, stat_id=stat.id,
                            title="Readiness", metric_key=stat.key, target_value=100)
    now = datetime.now(timezone.utc)
    await record_observation(db_session, stat, value=70, source="manual", observed_at=now)
    await record_observation(db_session, stat, value=20, source="manual", observed_at=now - timedelta(days=1))
    assert stat.current_value == goal.current_value == Decimal(70)


@pytest.mark.asyncio
async def test_source_version_scope_and_actor_are_checked(db_session, monkeypatch):
    from packages.core.stats import assessment
    ws, user, stat = await setup(db_session, monkeypatch)
    doc = Document(id=generate_ulid(), entity_id=ws.entity_id, name="Verified.md", is_trashed=False)
    group = DocumentGroup(id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id, name="Evidence")
    db_session.add_all([doc, group, DocumentGroupMember(group_id=group.id, document_id=doc.id)])
    await db_session.flush()
    # Keep production entity/trashed/version lookup; isolate only Document ACL here.
    async def readable(*args, **kwargs):
        return True
    monkeypatch.setattr("packages.core.services.document_access.user_can_read_document", readable)
    evidence = [{"document_id": doc.id, "updated_at": doc.created_at.isoformat(), "locator": "Readiness", "reason": "Reviewed rubric"}]
    payload = {**unknown(stat), "status": "measured", "value": 55, "gaps": [], "evidence": evidence}
    receipt = await act(ws, user, payload)
    assert receipt["status"] == "persisted" and receipt["value"] == 55
    doc.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await db_session.flush()
    assert (await act(ws, user, payload))["observation_id"] == receipt["observation_id"]
    stale = await act(ws, user, {**payload, "idempotency_key": "new"})
    assert "evidence changed" in json.dumps(stale)
    unauthorized = await act(ws, generate_ulid(), unknown(stat, "unauthorized"))
    assert "cannot manage" in json.dumps(unauthorized)
    with pytest.raises(ValueError, match="unavailable"):
        await assessment.readable_assessment_document(db_session, generate_ulid(), ws.id, user, doc.id)
    with pytest.raises(ValueError, match="does not belong"):
        await assessment.readable_assessment_document(db_session, ws.entity_id, generate_ulid(), user, doc.id)
    viewer = generate_ulid()
    db_session.add(WorkspaceStaff(workspace_id=ws.id, user_id=viewer, role="viewer", status="active"))
    await db_session.flush()
    assert (await act(ws, viewer, {}, action="get_stat_status"))["stats"][0]["stat_id"] == stat.id
    assert [row["stat_id"] for row in (await act(ws, viewer, {"stat_ids": [stat.id]}, action="get_stat_status"))["stats"]] == [stat.id]
    assert (await act(ws, viewer, {"key_prefix": "no.such.prefix"}, action="get_stat_status"))["stats"] == []
    assert "cannot manage" in json.dumps(await act(ws, viewer, unknown(stat, "viewer-write")))


@pytest.mark.asyncio
async def test_real_configuration_exports_and_reinstalls_one_shared_producer(db_session, monkeypatch):
    from packages.core.blueprints.exporter import _export_stats, _export_workflows, _export_scheduled_jobs
    from packages.core.blueprints.payload import _validate_stat_assessment_producers
    from packages.core.models.scheduler import ScheduledJob
    ws, user, stat = await setup(db_session, monkeypatch, review=False)
    db_session.add(AgentSubscription(entity_id=ws.entity_id, workspace_id=ws.id, agent_id=generate_ulid(), service_key="reviewer", status="active"))
    await db_session.flush()
    configured = await configure_review(db_session, workspace=ws, user_id=user, service_key="reviewer", rubrics={stat.key: "Use evidence"})
    repeated = await configure_review(db_session, workspace=ws, user_id=user, service_key="reviewer", rubrics={stat.key: "Use evidence"})
    assert configured == repeated
    workflows = await _export_workflows(db_session, ws.entity_id, ws.id)
    stats = await _export_stats(db_session, ws.entity_id, ws.id)
    jobs = await _export_scheduled_jobs(db_session, ws.entity_id, ws.id, portable_agent_service_keys={"reviewer"},
                                      workflow_key_by_id=workflows.key_by_workflow_id, workflow_key_by_binding_id=workflows.key_by_binding_id)
    recipe = {"stats": stats, "workflows": workflows.payloads, "scheduled_jobs": jobs, "subscriptions": [{"service_key": "reviewer"}]}
    _validate_stat_assessment_producers(recipe)
    assert len(jobs) == len(workflows.payloads) == 1
    assert jobs[0]["execution_target"]["workflow_slug"] == stat.collector_config["workflow_slug"]
    assert stat.collection_cadence is None
    assert (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws.id))).scalars().all()
    from packages.core.blueprints.installer import _install_workflow, _install_workflow_binding, _install_scheduled_job, _install_stat, InstallMode
    restored = Workspace(id=generate_ulid(), entity_id=ws.entity_id, name="Restored", status="active")
    db_session.add(restored)
    await db_session.flush()
    flow = workflows.payloads[0]
    workflow_id = await _install_workflow(db_session, entity_id=ws.entity_id, workspace_id=restored.id, w=flow, source_template_id=restored.id)
    await _install_workflow_binding(db_session, entity_id=ws.entity_id, workspace_id=restored.id, workflow_id=workflow_id, w=flow, source_template_id=restored.id)
    await _install_stat(db_session, entity_id=ws.entity_id, workspace_id=restored.id, payload=stats[0], mode=InstallMode.LIVE)
    await _install_scheduled_job(db_session, entity_id=ws.entity_id, workspace_id=restored.id, sj=jobs[0], user_id=user, mode=InstallMode.LIVE, source_template_id=restored.id)
    restored_jobs = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == restored.id))).scalars().all()
    assert len(restored_jobs) == 1
    assert restored_jobs[0].execution_target["workflow_id"] == workflow_id
    from apps.api.routers.workspace_stats import _stat_payload
    from packages.core.stats.service import attach_assessment_runtime as _attach_assessment_runtime
    from packages.core.models.workflow import WorkflowRun
    original_job = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id == configured["job_id"]))).scalar_one()
    original_job.enabled = True
    run = WorkflowRun(entity_id=ws.entity_id, workspace_id=ws.id, workflow_id=configured["workflow_id"],
                      binding_id=configured["binding_id"], status="running")
    db_session.add(run)
    await db_session.flush()
    for status, expected in (("running", "assessing"), ("failed", "assessment_error"), ("completed", "not_assessed")):
        run.status = status
        await db_session.flush()
        items = [_stat_payload(stat)]
        await _attach_assessment_runtime(db_session, items, ws.entity_id, ws.id)
        assert items[0]["freshness_status"] == expected
        assert items[0]["assessment_runtime"]["run_id"] == run.id
    original_job.enabled = False
    await configure_review(db_session, workspace=ws, user_id=user, service_key="reviewer", rubrics={stat.key: "Use evidence"})
    assert original_job.enabled is False




@pytest.mark.asyncio
async def test_workflow_uses_receipt_contract_and_can_continue_after_missing_result(db_session, monkeypatch):
    from types import SimpleNamespace
    from packages.core.ai import workflow_runner as module
    ws, user, stat = await setup(db_session, monkeypatch)
    async def prepare(*args, **kwargs):
        return SimpleNamespace(tool_schemas=[], allowed_tool_names=frozenset(), envelope=None,
                               context_section="", availability_section="")
    async def loop(**kwargs):
        contract = kwargs["final_response_contract"]
        assert isinstance(contract, StatAssessmentContract)
        assert contract.workflow_run_id == "run-a"
        assert (await contract.validate("Done, score=99")).action == FinalResponseAction.CONTINUE_EXECUTION
        await act(ws, user, unknown(stat))
        decision = await contract.validate("Evidence incomplete")
        assert decision.action == FinalResponseAction.ACCEPT
        return SimpleNamespace(content=decision.content, usage={}, tool_calls_made=[], rounds=2)
    async def attach(result, envelope):
        return result
    monkeypatch.setattr(module, "runtime_prepare_prompt_appendix_for_turn", prepare)
    monkeypatch.setattr(module, "runtime_execute_workflow_agent_loop", loop)
    monkeypatch.setattr(module, "runtime_attach_and_persist_workflow_runner_result", attach)
    result = await module.WorkflowRunner()._execute_agent_step(
        {"id": "assess", "type": "agent", "config": {"prompt": "Assess", "tools": ["manor"],
         "stat_assessment_keys": [stat.key]}}, {}, ws.entity_id, user,
        {"workspace_id": ws.id, "workflow_run_id": "run-a"}, db_session,
    )
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_concurrent_same_key_commits_one_observation(db_session, monkeypatch):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.workspace_stat import WorkspaceStat
    ws, _, stat = await setup(db_session, monkeypatch, review=False)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async def submit():
        async with factory() as db:
            row = await db.get(WorkspaceStat, stat.id)
            observation = await record_observation(db, row, value=33, source="manual", idempotency_key="concurrent")
            await db.commit()
            return observation.id
    results = await asyncio.gather(submit(), submit())
    assert results[0] == results[1]
    assert len((await db_session.execute(select(WorkspaceStatObservation).where(WorkspaceStatObservation.stat_id == stat.id))).scalars().all()) == 1


@pytest.mark.parametrize("gateway", ["manor", "workspace_agent"])
def test_assessment_tool_classification_gates_persisted_writes(gateway):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    write = {"action": "record_stat_assessment", "params": {"stat_id": "stat-1"}}
    read = {"action": "get_stat_status", "params": {}}
    if gateway == "manor":
        write = {"action": "workspace", "params": write}
        read = {"action": "workspace", "params": read}
    classification = classify_runtime_tool(gateway, write)
    assert classification.effect is RuntimeToolEffect.ACTION
    assert classification.action.action_key == "workspace.goal.update"
    assert classify_runtime_tool(gateway, read).effect is RuntimeToolEffect.READ_ONLY


@pytest.mark.asyncio
async def test_agent_adds_assessed_audience_metrics_through_the_stat_action(db_session, monkeypatch):
    from packages.core.models.user import User
    from packages.core.services.auth_service import hash_password
    ws, user, _ = await setup(db_session, monkeypatch, review=False)
    db_session.add_all([
        User(id=user, entity_id=ws.entity_id, email=f"{user}@test.com", display_name="Owner",
             password_hash=hash_password("pass123"), role="member", status="active", locale="zh"),
        AgentSubscription(entity_id=ws.entity_id, workspace_id=ws.id, agent_id=generate_ulid(), service_key="analyst", status="active"),
    ])
    await db_session.flush()
    result = await act(ws, user, {"templates": ["latest_video_views", "followers"], "cadence": "daily"}, action="add_assessed_metrics")
    assert result["service_key"] == "analyst" and len(result["stat_ids"]) == 2
    stats = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.id.in_(result["stat_ids"])))).scalars().all()
    assert {stat.name for stat in stats} == {"最新视频播放量", "粉丝 / 订阅数"}
    assert all(stat.collector_type == "agent_review" for stat in stats)
    viewer = generate_ulid()
    db_session.add(WorkspaceStaff(workspace_id=ws.id, user_id=viewer, role="viewer", status="active"))
    await db_session.flush()
    denied = await act(ws, viewer, {"templates": ["active_users"]}, action="add_assessed_metrics")
    assert "cannot manage" in json.dumps(denied)


@pytest.mark.parametrize("gateway", ["manor", "manor_workspace", "workspace_agent"])
def test_adding_assessed_metrics_is_a_configuration_change_on_every_gateway(gateway):
    """It creates a Workflow and schedule, so it never passes as a reversible measurement."""
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    call = {"action": "add_assessed_metrics", "params": {"templates": ["followers"]}}
    name = "manor" if gateway.startswith("manor") else gateway
    if gateway == "manor":
        call = {"action": "add_assessed_metrics", "params": {"templates": ["followers"]}}
    elif gateway == "manor_workspace":
        call = {"action": "workspace", "params": call}
    classification = classify_runtime_tool(name, call)
    assert classification.effect is RuntimeToolEffect.ACTION
    assert classification.action.action_key == "workspace.operation.apply"
    assert classification.action.risk_level == "high"


@pytest.mark.oss_regression
@pytest.mark.parametrize("fault", ["missing_workflow", "missing_schedule", "two_schedules", "double_clock", "unknown_key", "skill_delegate", "missing_service"])
def test_public_blueprint_rejects_incomplete_assessment_producer(fault):
    from copy import deepcopy
    from packages.core.blueprints.payload import _validate_stat_assessment_producers, PayloadError
    recipe = _assessment_recipe()
    step = next(
        step for workflow in recipe["workflows"] for step in workflow["steps"]
        if step["type"] == "agent" and step.get("config", {}).get("stat_assessment_keys")
    )
    if fault == "missing_workflow":
        recipe["workflows"] = []
    elif fault == "missing_schedule":
        recipe["scheduled_jobs"] = []
    elif fault == "two_schedules":
        recipe["scheduled_jobs"].append(deepcopy(recipe["scheduled_jobs"][0]))
    elif fault == "double_clock":
        recipe["stats"][0]["collection_cadence"] = "weekly"
    elif fault == "unknown_key":
        step["config"]["stat_assessment_keys"].append("imaginary_score")
    elif fault == "skill_delegate":
        step["config"]["skill"] = "unrelated-skill"
    else:
        step["config"]["service_key"] = "missing_service"
    with pytest.raises(PayloadError):
        _validate_stat_assessment_producers(recipe)




@pytest.mark.oss_regression
@pytest.mark.asyncio
async def test_workspace_metrics_are_configured_as_one_exportable_batch(db_session, monkeypatch):
    import os
    from packages.core.blueprints.exporter import _export_stats, _export_workflows, _export_scheduled_jobs
    from packages.core.blueprints.payload import _validate_stat_assessment_producers
    from packages.core.stats.service import delete_stat
    ws, user, placeholder = await setup(db_session, monkeypatch, review=False)
    await delete_stat(db_session, placeholder)
    payload = _assessment_recipe()
    rubrics = {}
    for spec in payload["stats"]:
        rubric = spec["description"].replace("Missing evidence scores 0.", "Missing or unverifiable evidence is unknown, not zero.")
        if "Workspace-level assessment" not in rubric:
            rubric += " Workspace-level assessment: identify the applicable releases and explain the rubric calculation from versioned evidence. Do not reuse a single release score as a workspace aggregate. If scope, denominator or evidence cannot be established, record insufficient_evidence with explicit gaps. A confirmed failed criterion may score zero; absence of evidence alone may not."
        rubrics[spec["key"]] = rubric
        await create_stat(db_session, entity_id=ws.entity_id, workspace_id=ws.id,
                          **{key: spec[key] for key in ("key", "name", "description", "unit", "window", "value_type")},
                          collector_type="manual", collection_cadence=None)
    db_session.add(AgentSubscription(entity_id=ws.entity_id, workspace_id=ws.id, agent_id=generate_ulid(), service_key="readiness_and_learning", status="active"))
    await db_session.flush()
    await configure_review(db_session, workspace=ws, user_id=user, service_key="readiness_and_learning", rubrics=rubrics)
    workflows = await _export_workflows(db_session, ws.entity_id, ws.id)
    stats = await _export_stats(db_session, ws.entity_id, ws.id)
    jobs = await _export_scheduled_jobs(db_session, ws.entity_id, ws.id, portable_agent_service_keys={"readiness_and_learning"},
                                      workflow_key_by_id=workflows.key_by_workflow_id, workflow_key_by_binding_id=workflows.key_by_binding_id)
    # The scheduler identity is installation-specific; give its portable component a stable name.
    jobs[0]["job_id"] = "workspace-evidence-assessment"
    exported = {"stats": stats, "workflows": workflows.payloads, "scheduled_jobs": jobs}
    _validate_stat_assessment_producers({**exported, "subscriptions": payload["subscriptions"]})
    assert len(stats) == 2 and len(workflows.payloads) == len(jobs) == 1
    assert jobs[0]["enabled"] is True
    producer = next(step for step in workflows.payloads[0]["steps"] if step["type"] == "agent")
    assert {s["key"] for s in stats} == set(producer["config"]["stat_assessment_keys"])
    if os.environ.get("STAT_REVIEW_EXPORT") == "1":
        print("STAT_REVIEW_EXPORT=" + json.dumps(exported))


