from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.workspace import Workspace


MODULE = "packages.core.services.blueprint_startup_service"


def _service():
    return importlib.import_module(MODULE)


def _startup_fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _workspace(*, settings: dict, status: str = "active") -> Workspace:
    settings = dict(settings)
    startup = settings.get("blocking_setup")
    record = settings.get("_blueprint")
    if isinstance(startup, dict) and isinstance(record, dict) and record.get("blueprint_id"):
        record = dict(record)
        portable_keys = set(record.get("portable_setting_keys") or [])
        portable_keys.add("blocking_setup")
        record["portable_setting_keys"] = sorted(portable_keys)
        fingerprints = dict(record.get("runtime_contract_fingerprints") or {})
        fingerprints["blocking_setup"] = _startup_fingerprint(startup)
        record["runtime_contract_fingerprints"] = fingerprints
        settings["_blueprint"] = record
    return Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Blueprint startup",
        status=status,
        artifact_folder_id=generate_ulid(),
        settings=settings,
    )


def _job(workspace: Workspace, base_job_id: str) -> ScheduledJob:
    return ScheduledJob(
        id=generate_ulid(),
        job_id=installed_blueprint_job_id(base_job_id, workspace.id),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name=base_job_id,
        job_type="manual",
        schedule_kind=None,
        execution_type="agent",
        execution_target={"workspace_id": workspace.id},
        payload_message="Run setup.",
        enabled=True,
    )


def test_blueprint_startup_service_module_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None


@pytest.mark.asyncio
async def test_reconcile_is_noop_without_explicit_startup_contract(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    workspace = _workspace(settings={})
    db_session.add(workspace)
    await db_session.commit()
    dispatched: list[str] = []

    async def capture(*_args, **kwargs):
        dispatched.append(kwargs["job_db_id"])

    monkeypatch.setattr(service, "dispatch_job_occurrence", capture)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="test",
    )

    assert result.state == "not_configured"
    assert result.dispatched_job_ids == ()
    assert dispatched == []


@pytest.mark.asyncio
async def test_reconcile_dispatches_only_incomplete_setup_job(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "blocking_setup": {
            "checks": [{
                "key": "identity",
                "kind": "not_ready",
                "blocking": True,
                "setup_job_id": "prepare-identity",
            }],
            "on_ready_job_id": "first-review",
        },
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "live_setup_requirements": [],
        },
    })
    setup_job = _job(workspace, "prepare-identity")
    ready_job = _job(workspace, "first-review")
    db_session.add_all([workspace, setup_job, ready_job])
    await db_session.commit()
    dispatched: list[tuple[str, str]] = []

    async def capture(*, job_db_id: str, occurrence_key: str, **_kwargs):
        dispatched.append((job_db_id, occurrence_key))
        return SimpleNamespace(published=True)

    monkeypatch.setattr(service, "dispatch_job_occurrence", capture)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="install",
    )

    assert result.state == "setup_dispatched"
    assert result.dispatched_job_ids == (setup_job.job_id,)
    assert dispatched == [
        (setup_job.id, service.BLUEPRINT_SETUP_OCCURRENCE_KEY),
    ]


@pytest.mark.asyncio
async def test_reconcile_dispatches_ready_job_when_setup_is_ready(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "blocking_setup": {
            "checks": [],
            "on_ready_job_id": "first-review",
        },
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "live_setup_requirements": [],
        },
    })
    ready_job = _job(workspace, "first-review")
    db_session.add_all([workspace, ready_job])
    await db_session.commit()
    dispatched: list[tuple[str, str]] = []

    async def capture(*, job_db_id: str, occurrence_key: str, **_kwargs):
        dispatched.append((job_db_id, occurrence_key))
        return SimpleNamespace(published=True)

    monkeypatch.setattr(service, "dispatch_job_occurrence", capture)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="setup_completed",
    )

    assert result.state == "ready_dispatched"
    assert result.dispatched_job_ids == (ready_job.job_id,)
    assert dispatched == [
        (ready_job.id, service.BLUEPRINT_READY_OCCURRENCE_KEY),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("success_status", ["completed", "success"])
async def test_reconcile_does_not_bootstrap_ready_job_after_prior_success(
    db_session,
    monkeypatch,
    success_status,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "blocking_setup": {
            "checks": [],
            "on_ready_job_id": "first-review",
        },
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "live_setup_requirements": [],
        },
    })
    ready_job = _job(workspace, "first-review")
    completed = ScheduledJobRun(
        id=generate_ulid(),
        job_id=ready_job.job_id,
        idempotency_key="cron:2026-08-30",
        status=success_status,
        trigger_type="cron",
    )
    db_session.add_all([workspace, ready_job, completed])
    await db_session.commit()

    async def unexpected(**_kwargs):
        raise AssertionError("completed production must not be bootstrapped again")

    monkeypatch.setattr(service, "dispatch_job_occurrence", unexpected)
    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="api_repair",
    )

    assert result.state == "already_started"
    assert result.dispatched_job_ids == ()


@pytest.mark.asyncio
async def test_reconcile_does_not_report_unpublished_job_as_dispatched(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "blocking_setup": {
            "checks": [],
            "on_ready_job_id": "first-review",
        },
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "live_setup_requirements": [],
        },
    })
    ready_job = _job(workspace, "first-review")
    db_session.add_all([workspace, ready_job])
    await db_session.commit()

    async def unpublished(**_kwargs):
        return SimpleNamespace(published=False)

    monkeypatch.setattr(service, "dispatch_job_occurrence", unpublished)
    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="install",
    )

    assert result.state == "dispatch_pending"
    assert result.dispatched_job_ids == ()


@pytest.mark.asyncio
async def test_reconcile_rejects_modified_startup_contract(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    original = {
        "checks": [],
        "on_ready_job_id": "first-review",
    }
    workspace = _workspace(settings={
        "blocking_setup": original,
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "blueprint_slug": "test",
            "live_setup_requirements": [],
        },
    })
    operator_job = _job(workspace, "operator-job")
    db_session.add_all([workspace, operator_job])
    await db_session.commit()
    settings = dict(workspace.settings)
    settings["blocking_setup"] = {
        "checks": [],
        "on_ready_job_id": "operator-job",
    }
    workspace.settings = settings
    await db_session.commit()

    async def unexpected(**_kwargs):
        raise AssertionError("modified startup contract must not dispatch")

    monkeypatch.setattr(service, "dispatch_job_occurrence", unexpected)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="api_repair",
    )

    assert result.state == "contract_modified"
    assert result.dispatched_job_ids == ()


@pytest.mark.asyncio
async def test_reconcile_retries_setup_after_terminal_error(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "blocking_setup": {
            "checks": [{
                "key": "identity",
                "kind": "not_ready",
                "blocking": True,
                "setup_job_id": "prepare-identity",
            }],
            "on_ready_job_id": "first-review",
        },
        "_blueprint": {
            "blueprint_id": "builtin:test",
            "blueprint_slug": "test",
            "live_setup_requirements": [],
        },
    })
    setup_job = _job(workspace, "prepare-identity")
    failed = ScheduledJobRun(
        id=generate_ulid(),
        job_id=setup_job.job_id,
        idempotency_key=service.BLUEPRINT_SETUP_OCCURRENCE_KEY,
        status="error",
        trigger_type="blueprint_startup",
    )
    db_session.add_all([workspace, setup_job, failed])
    await db_session.commit()
    dispatched: list[str] = []

    async def capture(*, occurrence_key: str, **_kwargs):
        dispatched.append(occurrence_key)
        return SimpleNamespace(published=True)

    monkeypatch.setattr(service, "dispatch_job_occurrence", capture)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="readiness_monitor",
    )

    assert result.state == "setup_dispatched"
    assert dispatched == [
        f"{service.BLUEPRINT_SETUP_OCCURRENCE_KEY}:retry:{failed.id}",
    ]


@pytest.mark.asyncio
async def test_reconcile_accepts_installer_owned_slug_without_marketplace_id(
    db_session,
    monkeypatch,
) -> None:
    service = _service()
    startup = {
        "checks": [],
        "on_ready_job_id": "first-review",
    }
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Raw Blueprint startup",
        status="active",
        artifact_folder_id=generate_ulid(),
        settings={
            "blocking_setup": startup,
            "_blueprint": {
                "blueprint_id": None,
                "blueprint_slug": "raw-startup",
                "portable_setting_keys": ["blocking_setup"],
                "runtime_contract_fingerprints": {
                    "blocking_setup": _startup_fingerprint(startup),
                },
                "live_setup_requirements": [],
            },
        },
    )
    ready_job = _job(workspace, "first-review")
    db_session.add_all([workspace, ready_job])
    await db_session.commit()

    async def capture(**_kwargs):
        return SimpleNamespace(published=True)

    monkeypatch.setattr(service, "dispatch_job_occurrence", capture)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="install",
    )

    assert result.state == "ready_dispatched"
    assert result.dispatched_job_ids == (ready_job.job_id,)


@pytest.mark.asyncio
async def test_repair_materializes_only_missing_matching_blueprint_startup_contract(
    db_session,
) -> None:
    service = _service()
    workspace = _workspace(settings={
        "_blueprint": {
            "blueprint_id": "builtin:test-startup",
            "blueprint_slug": "test-startup",
        },
    })
    setup_job = _job(workspace, "prepare-identity")
    ready_job = _job(workspace, "first-review")
    db_session.add_all([workspace, setup_job, ready_job])
    await db_session.commit()
    payload = {
        "manifest": {"slug": "test-startup"},
        "recipe": {
            "operating_model": {
                "settings": {
                    "blocking_setup": {
                        "checks": [{
                            "key": "identity",
                            "kind": "not_ready",
                            "blocking": True,
                            "setup_job_id": "prepare-identity",
                        }],
                        "on_ready_job_id": "first-review",
                    },
                },
            },
        },
    }

    result = await service.reconcile_missing_blueprint_startup_contract(
        db_session,
        workspace=workspace,
        payload=payload,
    )

    assert result.state == "materialized"
    assert workspace.settings["blocking_setup"] == payload["recipe"]["operating_model"]["settings"]["blocking_setup"]
    assert workspace.settings["_blueprint"]["portable_setting_keys"] == [
        "blocking_setup"
    ]


@pytest.mark.asyncio
async def test_repair_never_overwrites_other_or_modified_workspace_contract(
    db_session,
) -> None:
    service = _service()
    existing = {"checks": [], "on_ready_job_id": "operator-job"}
    workspace = _workspace(settings={
        "blocking_setup": existing,
        "_blueprint": {
            "blueprint_id": "builtin:other",
            "blueprint_slug": "other",
        },
    })
    db_session.add(workspace)
    await db_session.commit()
    payload = {
        "manifest": {"slug": "test-startup"},
        "recipe": {
            "operating_model": {
                "settings": {
                    "blocking_setup": {
                        "checks": [],
                        "on_ready_job_id": "first-review",
                    },
                },
            },
        },
    }

    other = await service.reconcile_missing_blueprint_startup_contract(
        db_session,
        workspace=workspace,
        payload=payload,
    )
    assert other.state == "blueprint_mismatch"
    assert workspace.settings["blocking_setup"] == existing

    settings = dict(workspace.settings)
    settings["_blueprint"] = {
        "blueprint_id": "builtin:test-startup",
        "blueprint_slug": "test-startup",
    }
    workspace.settings = settings
    await db_session.flush()
    modified = await service.reconcile_missing_blueprint_startup_contract(
        db_session,
        workspace=workspace,
        payload=payload,
    )
    assert modified.state == "workspace_modified"
    assert workspace.settings["blocking_setup"] == existing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "settings_patch", "expected_state"),
    [
        ("paused", {}, "workspace_paused"),
        ("active", {"sandbox": True}, "simulation"),
    ],
)
async def test_reconcile_does_not_dispatch_for_non_running_workspace(
    db_session,
    monkeypatch,
    status,
    settings_patch,
    expected_state,
) -> None:
    service = _service()
    workspace = _workspace(
        status=status,
        settings={
            **settings_patch,
            "blocking_setup": {
                "checks": [],
                "on_ready_job_id": "first-review",
            },
        },
    )
    db_session.add_all([workspace, _job(workspace, "first-review")])
    await db_session.commit()

    async def unexpected(**_kwargs):
        raise AssertionError("startup dispatch must not run")

    monkeypatch.setattr(service, "dispatch_job_occurrence", unexpected)

    result = await service.reconcile_blueprint_startup(
        db_session,
        workspace_id=workspace.id,
        trigger="monitor",
    )

    assert result.state == expected_state


@pytest.mark.asyncio
async def test_explicit_scheduler_occurrence_is_claimed_once(
    db_session,
    monkeypatch,
) -> None:
    scheduler_tasks = importlib.import_module("packages.core.tasks.scheduler_tasks")
    workspace = _workspace(settings={})
    job = _job(workspace, "prepare-identity")
    db_session.add_all([workspace, job])
    await db_session.commit()
    published: list[dict] = []

    monkeypatch.setattr(
        scheduler_tasks,
        "_publish_scheduled_dispatch",
        lambda dispatch, **_kwargs: published.append(dispatch),
    )

    first = await scheduler_tasks.dispatch_job_occurrence(
        job_db_id=job.id,
        occurrence_key="blueprint-bootstrap:setup:v1",
        trigger_type="blueprint_startup",
    )
    second = await scheduler_tasks.dispatch_job_occurrence(
        job_db_id=job.id,
        occurrence_key="blueprint-bootstrap:setup:v1",
        trigger_type="blueprint_startup",
    )

    run_count = (await db_session.execute(
        select(func.count()).select_from(ScheduledJobRun).where(
            ScheduledJobRun.job_id == job.job_id,
            ScheduledJobRun.idempotency_key == "blueprint-bootstrap:setup:v1",
        )
    )).scalar_one()
    assert first is not None and first.published is True
    assert second is not None and second.published is False
    assert run_count == 1
    assert len(published) == 1


@pytest.mark.asyncio
async def test_workspace_repair_endpoint_uses_generic_reconciler(
    db_session,
    monkeypatch,
) -> None:
    router = importlib.import_module("apps.api.routers.blueprints")
    service = _service()
    workspace = _workspace(settings={})
    user = SimpleNamespace(entity_id=workspace.entity_id)
    authorized: list[str] = []
    calls: list[tuple[str, str]] = []

    async def get_workspace(*_args, **_kwargs):
        return workspace

    async def require_writable(_db, _user, workspace_id):
        authorized.append(workspace_id)

    async def reconcile(_db, *, workspace_id: str, trigger: str):
        calls.append((workspace_id, trigger))
        return service.BlueprintStartupResult(
            state="not_configured",
            blocking_check_keys=("identity",),
        )

    monkeypatch.setattr(router, "get_workspace", get_workspace)
    monkeypatch.setattr(router, "require_workspace_writable", require_writable)
    monkeypatch.setattr(service, "reconcile_blueprint_startup", reconcile)

    response = await router.reconcile_workspace_blueprint_startup(
        workspace.id,
        user=user,
        db=db_session,
    )

    assert authorized == [workspace.id]
    assert calls == [(workspace.id, "api_repair")]
    assert response.state == "not_configured"
    assert response.dispatched_job_ids == []
    assert response.blocking_check_keys == ["identity"]
