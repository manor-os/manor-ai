"""Regression tests for Blueprint-owned Workspace setup gates."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.workspace import Workspace
from packages.core.plans.executor import PlanExecutor
from packages.core.services.entity_service import (
    ProtectedWorkspaceSettingsError,
    update_workspace,
)
from packages.core.services.workspace_readiness import (
    WorkspaceReadinessPartStatus,
    evaluate_workspace_blocking_setup,
    normalize_workspace_setup_task_key,
)
from packages.core.strategist.service import _restrict_proposal_to_setup_work
from packages.core.tasks.monitor_tasks import _workspace_setup_monitor_state


def _blocking_status(*allowed_keys: str) -> WorkspaceReadinessPartStatus:
    return WorkspaceReadinessPartStatus(
        key="blocking_setup",
        name="Blocking Workspace setup",
        role="setup gate",
        check="live requirements",
        status="missing",
        summary="setup incomplete",
        missing_setup_key="blocking_setup_incomplete",
        details={"allowed_setup_task_keys": list(allowed_keys)},
    )


def _task(
    task_key: str,
    *,
    dependencies: list[str] | None = None,
    external_action=None,
):
    return SimpleNamespace(
        task_key=task_key,
        depends_on_task_keys=list(dependencies or []),
        external_action=external_action,
    )


def test_installed_blueprint_job_id_is_scoped_and_idempotent() -> None:
    workspace_id = "01JABCDEF0123456789ABCDEFG"
    installed_id = installed_blueprint_job_id("prepare-assets", workspace_id)

    assert installed_id == "prepare-assets-9ABCDEFG"
    assert installed_blueprint_job_id(installed_id, workspace_id) == installed_id


def test_setup_request_keys_use_one_normalized_shape() -> None:
    assert normalize_workspace_setup_task_key(
        "Prepare-Workspace Identity"
    ) == "prepare_workspace_identity"
    assert normalize_workspace_setup_task_key(None) == ""


def test_workspace_setup_monitor_notifies_once_per_connection_incident() -> None:
    missing = _blocking_status()
    missing = WorkspaceReadinessPartStatus(
        **{
            **missing.__dict__,
            "details": {
                "incomplete_checks": [{
                    "key": "blueprint_install_todo:0",
                    "kind": "blueprint_install_todo",
                    "todo_kind": "missing_integration",
                    "provider": "slack",
                }],
            },
        }
    )

    first, notify_first = _workspace_setup_monitor_state(missing, {})
    repeated, notify_repeated = _workspace_setup_monitor_state(missing, first)
    recovered, notify_recovered = _workspace_setup_monitor_state(None, repeated)
    disconnected_again, notify_again = _workspace_setup_monitor_state(
        missing,
        recovered,
    )

    assert notify_first is True
    assert notify_repeated is False
    assert notify_recovered is False
    assert notify_again is True
    assert first["incident"] == repeated["incident"] == 1
    assert disconnected_again["incident"] == 2
    assert recovered["ready"] is True


@pytest.mark.asyncio
async def test_readiness_monitor_reconciles_only_opted_in_blueprint_startup(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import blueprint_startup_service
    from packages.core.tasks import monitor_tasks

    opted_in = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Opted in startup",
        status="active",
        settings={
            "blocking_setup": {
                "checks": [],
                "on_ready_job_id": "first-review",
            },
        },
    )
    ordinary = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Ordinary Workspace",
        status="active",
        settings={},
    )
    db_session.add_all([opted_in, ordinary])
    await db_session.commit()
    calls: list[str] = []

    async def reconcile(_db, *, workspace_id: str, trigger: str):
        assert trigger == "readiness_monitor"
        calls.append(workspace_id)

    monkeypatch.setattr(
        blueprint_startup_service,
        "reconcile_blueprint_startup",
        reconcile,
    )

    await monitor_tasks._reconcile_blueprint_startup_after_monitor(
        db_session,
        workspace_id=ordinary.id,
        settings=ordinary.settings,
    )
    await monitor_tasks._reconcile_blueprint_startup_after_monitor(
        db_session,
        workspace_id=opted_in.id,
        settings=opted_in.settings,
    )

    assert calls == [opted_in.id]


def test_strategist_setup_gate_rejects_every_model_generated_task() -> None:
    allowed = _task("prepare_workspace_identity")
    depends_on_dropped = _task(
        "ask_owner_for_assets",
        dependencies=["normal_growth_task"],
    )
    disguised_external_action = _task(
        "prepare_workspace_identity",
        external_action=object(),
    )
    normal = _task("normal_growth_task")
    proposal = SimpleNamespace(
        tasks=[allowed, depends_on_dropped, disguised_external_action, normal],
        human_requests=[
            SimpleNamespace(request_key="ask_owner_for_assets"),
            SimpleNamespace(request_key="approve_campaign"),
        ],
        experiments=[object()],
        workflow_runs=[object()],
        automation_changes=[object()],
        workflow_changes=[object()],
        goal_changes=[object()],
    )

    active = _restrict_proposal_to_setup_work(
        proposal,
        _blocking_status(
            "prepare_workspace_identity",
            "ask_owner_for_assets",
        ),
    )

    assert active is True
    # A model-authored key is guidance, not trusted setup provenance. Automated
    # setup runs only through the matching Blueprint ScheduledJob.
    assert proposal.tasks == []
    assert [item.request_key for item in proposal.human_requests] == [
        "ask_owner_for_assets"
    ]
    assert proposal.experiments == []
    assert proposal.workflow_runs == []
    assert proposal.automation_changes == []
    assert proposal.workflow_changes == []
    assert proposal.goal_changes == []


@pytest.mark.asyncio
async def test_generic_settings_update_preserves_server_owned_setup_contract(
    db_session,
) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Protected setup",
        status="active",
        artifact_folder_id=generate_ulid(),
        settings={
            "_blueprint": {
                "blueprint_id": "builtin:protected",
                "live_setup_requirements": [],
            },
            "blocking_setup": {"checks": []},
            "created_by_user_id": "user_creator",
            "user_theme": "dark",
        },
    )
    db_session.add(workspace)
    await db_session.flush()

    updated = await update_workspace(
        db_session,
        workspace.id,
        entity_id,
        settings={},
    )

    assert updated is not None
    assert updated.settings == {
        "_blueprint": {
            "blueprint_id": "builtin:protected",
            "live_setup_requirements": [],
        },
        "blocking_setup": {"checks": []},
        "created_by_user_id": "user_creator",
    }

    with pytest.raises(ProtectedWorkspaceSettingsError):
        await update_workspace(
            db_session,
            workspace.id,
            entity_id,
            settings={"_blueprint": {}},
        )

    with pytest.raises(ProtectedWorkspaceSettingsError):
        await update_workspace(
            db_session,
            workspace.id,
            entity_id,
            settings={"created_by_user_id": "different_user"},
        )


@pytest.mark.asyncio
async def test_legacy_blueprint_without_live_contract_fails_closed(
    db_session,
) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Legacy Blueprint",
        status="active",
        settings={
            "_blueprint": {
                "blueprint_id": "builtin:legacy",
                "content_fingerprint": "legacy-fingerprint",
                "install_todos": [],
            },
        },
    )
    db_session.add(workspace)
    await db_session.flush()

    status = await evaluate_workspace_blocking_setup(db_session, workspace)

    assert status is not None and status.blocks_work
    [missing] = status.details["incomplete_checks"]
    assert missing["todo_kind"] == "live_setup_contract"
    assert "re-sync" in missing["reason"].lower()


@pytest.mark.asyncio
async def test_nonblocking_check_does_not_hide_durable_integration_blocker(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import integration_resolution

    async def disconnected_provider(*_args, **_kwargs):
        return {
            "slack": SimpleNamespace(
                ready=False,
                reason="Slack is not connected.",
                scope="none",
                setup_kind="oauth",
            ),
        }

    monkeypatch.setattr(
        integration_resolution,
        "integration_provider_readiness",
        disconnected_provider,
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Durable integration blocker",
        status="active",
        settings={
            "blocking_setup": {
                "checks": [{
                    "key": "optional_slack_status",
                    "kind": "integration_provider",
                    "provider": "slack",
                    "blocking": False,
                }],
            },
            "_blueprint": {
                "blueprint_id": "builtin:durable-integration",
                "live_setup_requirements": [{
                    "kind": "missing_integration",
                    "detail": "Connect Slack before normal work.",
                    "payload": {"provider": "slack"},
                    "blocking": True,
                }],
            },
        },
    )
    db_session.add(workspace)
    await db_session.flush()

    status = await evaluate_workspace_blocking_setup(db_session, workspace)

    assert status is not None and status.blocks_work
    [missing] = status.details["incomplete_checks"]
    assert missing["todo_kind"] == "missing_integration"
    assert missing["provider"] == "slack"


@pytest.mark.asyncio
async def test_plan_executor_does_not_advance_materialized_plan_while_setup_blocks(
    db_session,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Blocked plan",
        status="active",
        settings={
            "_blueprint": {
                "blueprint_id": "builtin:blocked",
                "live_setup_requirements": [{
                    "kind": "note",
                    "detail": "Complete setup first.",
                    "payload": {},
                    "blocking": True,
                }],
            },
        },
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        status="draft",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    db_session.add_all([workspace, plan])
    await db_session.flush()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    result = await PlanExecutor(session_factory=session_factory).run_cycle(plan.id)

    assert result["status"] == "blocked_setup"
    assert result["next_action"] == "schedule_self"
    assert plan.status == "draft"


@pytest.mark.asyncio
async def test_plan_executor_stops_polling_when_workspace_is_paused(
    db_session,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Paused workspace",
        status="paused",
        settings={},
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        status="draft",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
    )
    db_session.add_all([workspace, plan])
    await db_session.flush()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    result = await PlanExecutor(session_factory=session_factory).run_cycle(plan.id)

    assert result == {
        "plan_id": plan.id,
        "status": "workspace_unavailable",
        "next_action": "stop",
    }
    assert plan.status == "draft"
