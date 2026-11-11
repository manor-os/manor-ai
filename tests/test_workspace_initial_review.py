"""Creation must commit a recoverable first review, including setup input."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.workspace import Workspace
from packages.core.tasks import scheduler_tasks
from test_blueprint_installer_workflows import _base_payload


def _payload(*, blocked=False, ready_job=False, automatic=True):
    setup = {"checks": []}
    if blocked:
        setup = {
            "checks": [{
                "key": "source_materials",
                "kind": "workspace_user_materials",
                "all_of": [{"documents": {"suffixes": [".csv"]}}],
                "blocking": True,
                "setup_task_key": "provide_source",
                "question": "Please upload the source CSV to begin.",
            }],
            "allowed_setup_task_keys": ["provide_source"],
        }
    jobs = []
    if ready_job:
        setup["on_ready_job_id"] = "first-review"
        jobs = [{
            "job_id": "first-review", "enabled": True,
            "schedule_kind": "manual", "execution_type": "strategist_review",
            "execution_target": {},
        }]
    return _base_payload(**{
        "recipe.operating_model": {
            "heartbeat_enabled": automatic,
            "heartbeat_cadence": "daily",
            "settings": {"blocking_setup": setup},
        },
        "recipe.scheduled_jobs": jobs,
    })


async def _install(db, **kwargs):
    return await install_blueprint(
        db, entity_id=generate_ulid(), payload=_payload(**kwargs),
        mode=InstallMode.LIVE,
    )


async def _initial_run(db, workspace_id):
    return await db.scalar(select(ScheduledJobRun).join(
        ScheduledJob, ScheduledJob.job_id == ScheduledJobRun.job_id,
    ).where(
        ScheduledJob.workspace_id == workspace_id,
        ScheduledJobRun.idempotency_key == "workspace-initial-review:v1",
    ))


@pytest.mark.parametrize("blocked", [False, True])
async def test_install_stages_initial_review_before_commit(db_session, monkeypatch, blocked):
    publish = Mock()
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    result = await _install(db_session, blocked=blocked)
    run = await _initial_run(db_session, result.workspace_id)
    assert run is not None, "Live install must persist its first assessment before commit"
    assert run.result["dispatch_status"] == "prepared"
    dispatch = run.result["dispatch"]
    assert dispatch["kind"] == "strategist_review"
    assert dispatch["args"] == [result.workspace_id]
    assert dispatch["kwargs"]["trigger_kind"] == "event"
    assert dispatch["kwargs"]["run_id"] == run.id
    assert "manual_request" not in run.result
    publish.assert_not_called()
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace.settings["provisioning"]["initial_review_run_id"] == run.id
    assert "_readiness_snapshot" in workspace.settings


async def test_ready_blueprint_keeps_declared_startup_owner(db_session):
    result = await _install(db_session, ready_job=True)
    assert await _initial_run(db_session, result.workspace_id) is None
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert "_readiness_snapshot" not in workspace.settings


async def test_blocked_setup_without_human_input_keeps_existing_startup_path(db_session):
    payload = _payload(blocked=True)
    setup = payload["recipe"]["operating_model"]["settings"]["blocking_setup"]
    setup["checks"][0].pop("setup_task_key")
    setup["allowed_setup_task_keys"] = []
    result = await install_blueprint(
        db_session, entity_id=generate_ulid(), payload=payload, mode=InstallMode.LIVE,
    )
    assert await _initial_run(db_session, result.workspace_id) is None
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert "_readiness_snapshot" not in workspace.settings


async def test_setup_input_does_not_wait_for_declared_ready_job(db_session):
    result = await _install(db_session, blocked=True, ready_job=True)
    assert await _initial_run(db_session, result.workspace_id) is not None


async def test_blueprint_installs_its_manual_jobs(db_session):
    # Both manual shapes run only on request and need no clock.
    payload = _base_payload(**{"recipe.scheduled_jobs": [
        {"job_id": "manual-kind", "schedule_kind": "manual",
         "execution_type": "strategist_review", "execution_target": {}},
        {"job_id": "manual-type", "job_type": "manual", "schedule_kind": None,
         "execution_type": "strategist_review", "execution_target": {}},
    ]})
    result = await install_blueprint(
        db_session, entity_id=generate_ulid(), payload=payload, mode=InstallMode.LIVE,
    )
    rows = (await db_session.execute(
        select(ScheduledJob.job_type, ScheduledJob.schedule_kind, ScheduledJob.name).where(
            ScheduledJob.workspace_id == result.workspace_id,
            ScheduledJob.id.in_(result.scheduled_job_ids),
        )
    )).all()
    assert {(job_type, kind) for job_type, kind, _ in rows} == {("cron", "manual"), ("manual", None)}
    # Declared without a name, each still gets one to be listed under.
    assert sorted(name for *_, name in rows) == ["Manual kind", "Manual type"]


@pytest.mark.parametrize("mode,automatic", [(InstallMode.SIMULATE, True), (InstallMode.LIVE, False)])
async def test_simulation_and_manual_install_do_not_stage_live_review(db_session, mode, automatic):
    result = await install_blueprint(
        db_session, entity_id=generate_ulid(), payload=_payload(automatic=automatic), mode=mode,
    )
    assert await _initial_run(db_session, result.workspace_id) is None


async def test_rollback_removes_initial_review_with_workspace(db_session):
    result = await _install(db_session, blocked=True)
    run = await _initial_run(db_session, result.workspace_id)
    assert run is not None
    run_id = run.id
    await db_session.rollback()
    assert await db_session.get(ScheduledJobRun, run_id) is None
    assert await db_session.get(Workspace, result.workspace_id) is None


async def test_initial_review_is_once_per_workspace(db_session):
    from packages.core.services.workspace_initial_review import prepare_initial_workspace_review

    first = await _install(db_session, blocked=True)
    second = await _install(db_session, blocked=True)
    workspace = await db_session.get(Workspace, first.workspace_id)
    before = await _initial_run(db_session, workspace.id)
    await prepare_initial_workspace_review(db_session, workspace)
    after = await _initial_run(db_session, workspace.id)
    other = await _initial_run(db_session, second.workspace_id)
    assert before.id == after.id
    assert other.id != before.id
    assert other.job_id != before.job_id


async def test_committed_startup_dispatch_and_broker_failure_are_recoverable(db_session, monkeypatch):
    from packages.core.services.workspace_initial_review import dispatch_initial_workspace_review

    result = await _install(db_session, blocked=True)
    workspace = await db_session.get(Workspace, result.workspace_id)
    run = await _initial_run(db_session, workspace.id)
    await db_session.commit()
    publish = Mock(side_effect=ConnectionError("broker unavailable"))
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    assert not await dispatch_initial_workspace_review(db_session, workspace)
    await db_session.refresh(run)
    assert run.result["dispatch_status"] == "prepared"
    recovery = scheduler_tasks._prepared_scheduled_dispatch_recovery(run)
    assert recovery["action"] == "republish_and_project"
    assert recovery["dispatch"] == run.result["dispatch"]

    publish.side_effect = None
    from packages.core import database
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: factory)
    assert await dispatch_initial_workspace_review(db_session, workspace)
    await db_session.refresh(run)
    assert run.result["dispatch_status"] == "published"
    publish.reset_mock()
    assert await dispatch_initial_workspace_review(db_session, workspace)
    publish.assert_not_called()


@pytest.mark.parametrize("change", ["paused", "deleted", "disabled"])
async def test_initial_review_rechecks_lifecycle_before_publish(db_session, monkeypatch, change):
    from packages.core.services.workspace_initial_review import dispatch_initial_workspace_review

    result = await _install(db_session)
    workspace = await db_session.get(Workspace, result.workspace_id)
    if change == "paused":
        workspace.status = "paused"
    elif change == "deleted":
        workspace.deleted_at = datetime.now(UTC)
    else:
        job = await db_session.scalar(select(ScheduledJob).where(
            ScheduledJob.job_id == f"sr:{workspace.id}",
        ))
        job.enabled = False
    await db_session.commit()
    publish = Mock()
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    assert not await dispatch_initial_workspace_review(db_session, workspace)
    publish.assert_not_called()


async def test_blocked_install_publishes_assessment_after_commit(db_session, monkeypatch):
    from apps.api.routers.blueprints import _commit_and_start_installed_workspace
    from packages.core import database
    from packages.core.services import workspace_setup_service
    from sqlalchemy.ext.asyncio import async_sessionmaker

    result = await _install(db_session, blocked=True)
    workspace = await db_session.get(Workspace, result.workspace_id)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: factory)
    monkeypatch.setattr(
        workspace_setup_service, "dispatch_workspace_agent_greetings_once", AsyncMock(return_value={}),
    )
    ordinary_start = AsyncMock()
    monkeypatch.setattr(workspace_setup_service, "dispatch_workspace_post_commit", ordinary_start)
    publish = Mock()
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", publish)
    await _commit_and_start_installed_workspace(db_session, result=result, entity_id=workspace.entity_id)
    publish.assert_called_once()
    ordinary_start.assert_not_awaited()
    async with factory() as observer:
        run = await _initial_run(observer, workspace.id)
        assert run.result["dispatch_status"] == "published"
        assert await observer.get(Workspace, workspace.id) is not None


async def test_first_assessment_asks_for_setup_without_creating_business_task(db_session, monkeypatch):
    from packages.core.models.participant import HumanCommitment
    from packages.core.models.task import Task
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.review import begin_review
    from packages.core.strategist import service
    from packages.core.strategist.proposal import Deliverable, Proposal, ProposedTask

    entity = Entity(id=generate_ulid(), name="Setup assessment test")
    actor = User(
        id=generate_ulid(), entity_id=entity.id, email=f"{generate_ulid()}@test.example",
        password_hash="unused", role="owner", status="active",
    )
    db_session.add_all([entity, actor, UserMembership(user_id=actor.id, entity_id=entity.id, role="owner")])
    await db_session.flush()
    result = await install_blueprint(
        db_session, entity_id=entity.id, user_id=actor.id,
        payload=_payload(blocked=True), mode=InstallMode.LIVE,
    )
    agent = Agent(id=generate_ulid(), entity_id=entity.id, name="Report analyst")
    db_session.add_all([agent, AgentSubscription(
        entity_id=entity.id, workspace_id=result.workspace_id,
        agent_id=agent.id, service_key="ops", status="active",
    )])
    await db_session.commit()
    review = await begin_review(
        db_session, entity_id=entity.id, workspace_id=result.workspace_id,
        trigger="event", lease_owner="initial-review-test",
    )
    await db_session.commit()

    async def proposal(*_args, review_id, **_kwargs):
        return Proposal(review_id=review_id, summary="Prepare the source report.", tasks=[
            ProposedTask(task_key="report", title="Prepare report", owner_service_key="ops", deliverables=[
                Deliverable(name="report", kind="value", shape="TextResult", acceptance="Complete report", usage="Review"),
            ]),
        ])

    monkeypatch.setattr(service, "generate_proposal", proposal)
    monkeypatch.setattr(service, "_post_strategist_activity", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_finish_strategist_activity", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_post_human_request_chat", AsyncMock())
    monkeypatch.setattr(service, "_publish_proposal_chat_message", AsyncMock())
    monkeypatch.setattr(service, "_notify_proposal_users", AsyncMock())
    await service.run_review(
        db_session, result.workspace_id, trigger="event", review_run=review,
        review_lease_owner="initial-review-test", user_id=actor.id,
        briefing_markdown="Initial assessment: source CSV has not been provided.",
    )
    await db_session.commit()
    assert list((await db_session.scalars(select(Task).where(Task.workspace_id == result.workspace_id))).all()) == []
    requests = list((await db_session.scalars(select(HumanCommitment).where(
        HumanCommitment.workspace_id == result.workspace_id,
    ))).all())
    assert len(requests) == 1
    assert requests[0].status == "waiting"
    assert requests[0].request_kind == "input"


async def test_initial_receipt_is_not_portable_blueprint_configuration(db_session):
    import json
    from packages.core.blueprints.exporter import export_workspace

    result = await _install(db_session, blocked=True)
    workspace = await db_session.get(Workspace, result.workspace_id)
    run = await _initial_run(db_session, workspace.id)
    exported = await export_workspace(db_session, workspace.id, title="Source reports")
    serialized = json.dumps(exported)
    assert run.id not in serialized
    assert "initial_review_run_id" not in serialized
    assert "_readiness_snapshot" not in serialized


async def test_initial_occurrence_uses_existing_execution_claim_and_terminal_guard(db_session, monkeypatch):
    from packages.core import database
    from packages.core.tasks import ai_tasks
    from sqlalchemy.ext.asyncio import async_sessionmaker

    result = await _install(db_session, blocked=True)
    run = await _initial_run(db_session, result.workspace_id)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: factory)
    body = AsyncMock(return_value={"workspace_id": result.workspace_id, "task_count": 0})
    executed, _ = await ai_tasks._run_scheduled_once(body, run_id=run.id, job_id_str=run.job_id)
    assert executed
    await db_session.refresh(run)
    assert run.status == "completed"
    executed, duplicate = await ai_tasks._run_scheduled_once(body, run_id=run.id, job_id_str=run.job_id)
    assert not executed
    assert duplicate["duplicate_suppressed"]
    body.assert_awaited_once()


async def test_existing_scheduler_sweep_finds_unpublished_initial_occurrence(db_session, monkeypatch):
    result = await _install(db_session, blocked=True)
    run = await _initial_run(db_session, result.workspace_id)
    now = datetime.now(UTC)
    run.created_at = now - timedelta(minutes=6)
    await db_session.commit()
    recover = Mock()
    monkeypatch.setattr(scheduler_tasks._recover_prepared_scheduled_dispatch_task, "delay", recover)
    await scheduler_tasks._queue_stale_prepared_dispatch_recovery(db_session, now)
    recover.assert_any_call(run.id)
    recover.reset_mock()
    await scheduler_tasks._queue_stale_prepared_dispatch_recovery(db_session, now)
    assert not [call for call in recover.call_args_list if call.args == (run.id,)]


async def test_readiness_sweep_does_not_duplicate_install_but_detects_later_changes(db_session, monkeypatch):
    from packages.core import database
    from packages.core.models.workspace import Agent
    from packages.core.tasks import ai_tasks, monitor_tasks
    from sqlalchemy.ext.asyncio import async_sessionmaker

    entity_id = generate_ulid()
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Analyst", slug="analyst")
    db_session.add(agent)
    await db_session.flush()
    payload = _payload(blocked=True)
    payload["recipe"]["subscriptions"] = [{"service_key": "ops", "agent_slug": agent.slug}]
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload, mode=InstallMode.LIVE)
    await db_session.commit()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "create_worker_session", lambda: factory)
    publish = Mock()
    monkeypatch.setattr(ai_tasks.run_strategist_review, "apply_async", publish)
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace.settings["_readiness_snapshot"]["agents"] == 1
    await monitor_tasks._async_readiness_check()
    assert not [call for call in publish.call_args_list if call.kwargs["args"] == [workspace.id]]
    publish.reset_mock()

    from packages.core.models.workspace import AgentSubscription
    db_session.add(AgentSubscription(
        entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id,
        service_key="second_service", status="active",
    ))
    await db_session.commit()
    await monitor_tasks._async_readiness_check()
    calls = [call for call in publish.call_args_list if call.kwargs["args"] == [workspace.id]]
    assert len(calls) == 1
    assert calls[0].kwargs["kwargs"]["trigger_detail"] == "readiness changed: agents: 1->2"
