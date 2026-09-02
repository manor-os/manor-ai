"""Resume explicitly declared Blueprint startup work."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.constants.execution import ScheduledRunStatus
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
from packages.core.models.workspace import Workspace


BLUEPRINT_SETUP_OCCURRENCE_KEY = "blueprint-bootstrap:setup:v1"
BLUEPRINT_READY_OCCURRENCE_KEY = "blueprint-bootstrap:ready:v1"


@dataclass(frozen=True)
class BlueprintStartupResult:
    state: str
    dispatched_job_ids: tuple[str, ...] = ()
    blocking_check_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlueprintStartupContractResult:
    state: str
    missing_job_ids: tuple[str, ...] = ()


def blueprint_startup_contract_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


async def reconcile_missing_blueprint_startup_contract(
    db: AsyncSession,
    *,
    workspace: Workspace,
    payload: dict[str, Any],
    source_blueprint_id: str | None = None,
) -> BlueprintStartupContractResult:
    """Restore only an absent, identity-matched Blueprint startup declaration."""

    locked = (await db.execute(
        select(Workspace)
        .where(
            Workspace.id == workspace.id,
            Workspace.entity_id == workspace.entity_id,
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if locked is None:
        return BlueprintStartupContractResult(state="workspace_missing")
    settings = dict(locked.settings or {})
    record = (
        dict(settings.get("_blueprint") or {})
        if isinstance(settings.get("_blueprint"), dict)
        else {}
    )
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    slug = str((manifest or {}).get("slug") or "").strip()
    installed_slug = str(record.get("blueprint_slug") or "").strip()
    installed_id = str(record.get("blueprint_id") or "").strip()
    if (
        not slug
        or installed_slug != slug
        or not installed_id
        or (
            source_blueprint_id is not None
            and installed_id != str(source_blueprint_id)
        )
    ):
        return BlueprintStartupContractResult(state="blueprint_mismatch")

    recipe = payload.get("recipe") if isinstance(payload, dict) else None
    operating_model = (
        recipe.get("operating_model")
        if isinstance(recipe, dict)
        else None
    )
    portable_settings = (
        operating_model.get("settings")
        if isinstance(operating_model, dict)
        else None
    )
    desired = (
        portable_settings.get("blocking_setup")
        if isinstance(portable_settings, dict)
        else None
    )
    if not isinstance(desired, dict):
        return BlueprintStartupContractResult(state="not_configured")
    current = settings.get("blocking_setup")
    if isinstance(current, dict) and current != desired:
        return BlueprintStartupContractResult(state="workspace_modified")
    if current is not None and not isinstance(current, dict):
        return BlueprintStartupContractResult(state="workspace_modified")

    referenced_job_ids = list(dict.fromkeys(
        [
            str(check.get("setup_job_id") or "").strip()
            for check in desired.get("checks") or []
            if isinstance(check, dict)
            and str(check.get("setup_job_id") or "").strip()
        ]
        + [str(desired.get("on_ready_job_id") or "").strip()]
    ))
    referenced_job_ids = [value for value in referenced_job_ids if value]
    scoped_job_ids = {
        installed_blueprint_job_id(value, locked.id): value
        for value in referenced_job_ids
    }
    rows = list((await db.execute(select(ScheduledJob).where(
        ScheduledJob.entity_id == locked.entity_id,
        ScheduledJob.workspace_id == locked.id,
        ScheduledJob.job_id.in_(tuple(scoped_job_ids)),
        ScheduledJob.enabled.is_(True),
    ))).scalars().all()) if scoped_job_ids else []
    present = {row.job_id for row in rows}
    missing = tuple(
        base_job_id
        for scoped_job_id, base_job_id in scoped_job_ids.items()
        if scoped_job_id not in present
    )
    if missing:
        return BlueprintStartupContractResult(
            state="jobs_missing",
            missing_job_ids=missing,
        )

    original_settings = deepcopy(settings)
    materialized = deepcopy(current if isinstance(current, dict) else desired)
    settings["blocking_setup"] = materialized
    portable_keys = {
        str(value).strip()
        for value in record.get("portable_setting_keys") or []
        if str(value or "").strip()
    }
    portable_keys.add("blocking_setup")
    record["portable_setting_keys"] = sorted(portable_keys)
    fingerprints = dict(record.get("runtime_contract_fingerprints") or {})
    fingerprints["blocking_setup"] = blueprint_startup_contract_fingerprint(
        materialized
    )
    record["runtime_contract_fingerprints"] = fingerprints
    settings["_blueprint"] = record
    locked.settings = settings
    await db.flush()
    return BlueprintStartupContractResult(
        state="materialized" if settings != original_settings else "unchanged"
    )


async def dispatch_job_occurrence(**kwargs):
    from packages.core.tasks.scheduler_tasks import dispatch_job_occurrence as dispatch

    return await dispatch(**kwargs)


async def reconcile_blueprint_startup(
    db: AsyncSession,
    *,
    workspace_id: str,
    trigger: str,
) -> BlueprintStartupResult:
    """Dispatch only the startup work declared by one Blueprint Workspace."""

    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        return BlueprintStartupResult(state="workspace_missing")
    if workspace.status != "active":
        return BlueprintStartupResult(state=f"workspace_{workspace.status}")

    settings = workspace.settings if isinstance(workspace.settings, dict) else {}
    if settings.get("sandbox") is True:
        return BlueprintStartupResult(state="simulation")
    startup = settings.get("blocking_setup")
    if not isinstance(startup, dict):
        return BlueprintStartupResult(state="not_configured")
    record = settings.get("_blueprint")
    portable_keys = (
        set(record.get("portable_setting_keys") or [])
        if isinstance(record, dict)
        else set()
    )
    fingerprints = (
        record.get("runtime_contract_fingerprints")
        if isinstance(record, dict)
        else None
    )
    expected_fingerprint = (
        str(fingerprints.get("blocking_setup") or "").strip()
        if isinstance(fingerprints, dict)
        else ""
    )
    if (
        not isinstance(record, dict)
        or not (
            str(record.get("blueprint_id") or "").strip()
            or str(record.get("blueprint_slug") or "").strip()
        )
        or "blocking_setup" not in portable_keys
        or not expected_fingerprint
    ):
        return BlueprintStartupResult(state="contract_unverified")
    if expected_fingerprint != blueprint_startup_contract_fingerprint(startup):
        return BlueprintStartupResult(state="contract_modified")

    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
    )

    readiness = await evaluate_workspace_blocking_setup(db, workspace)
    incomplete = (
        list(readiness.details.get("incomplete_checks") or [])
        if readiness is not None and readiness.blocks_work
        else []
    )
    if incomplete:
        base_job_ids = list(dict.fromkeys(
            str(check.get("setup_job_id") or "").strip()
            for check in incomplete
            if isinstance(check, dict)
            and str(check.get("setup_job_id") or "").strip()
        ))
        occurrence_key = BLUEPRINT_SETUP_OCCURRENCE_KEY
        state = "setup_dispatched" if base_job_ids else "blocked"
    else:
        ready_job_id = str(startup.get("on_ready_job_id") or "").strip()
        base_job_ids = [ready_job_id] if ready_job_id else []
        occurrence_key = BLUEPRINT_READY_OCCURRENCE_KEY
        state = "ready_dispatched" if base_job_ids else "ready"

    blocking_keys = tuple(
        str(check.get("key") or "setup").strip()
        for check in incomplete
        if isinstance(check, dict)
    )
    if not base_job_ids:
        return BlueprintStartupResult(
            state=state,
            blocking_check_keys=blocking_keys,
        )

    scoped_job_ids = [
        installed_blueprint_job_id(base_job_id, workspace.id)
        for base_job_id in base_job_ids
    ]
    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == workspace.entity_id,
            ScheduledJob.workspace_id == workspace.id,
            ScheduledJob.job_id.in_(scoped_job_ids),
            ScheduledJob.enabled.is_(True),
        )
    )).scalars().all())
    jobs_by_id = {job.job_id: job for job in jobs}
    if not incomplete and scoped_job_ids:
        prior_success = (await db.execute(
            select(ScheduledJobRun.id)
            .where(
                ScheduledJobRun.job_id.in_(scoped_job_ids),
                ScheduledJobRun.status.in_((
                    ScheduledRunStatus.SUCCESS.value,
                    ScheduledRunStatus.COMPLETED.value,
                )),
            )
            .limit(1)
        )).scalar_one_or_none()
        if prior_success is not None:
            return BlueprintStartupResult(state="already_started")
    dispatched: list[str] = []
    attempted_dispatch = False
    for scoped_job_id in scoped_job_ids:
        job = jobs_by_id.get(scoped_job_id)
        if job is None:
            continue
        attempted_dispatch = True
        job_occurrence_key = occurrence_key
        if incomplete:
            latest = (await db.execute(
                select(ScheduledJobRun)
                .where(
                    ScheduledJobRun.job_id == scoped_job_id,
                    ScheduledJobRun.idempotency_key.startswith(
                        BLUEPRINT_SETUP_OCCURRENCE_KEY
                    ),
                )
                .order_by(
                    ScheduledJobRun.created_at.desc(),
                    ScheduledJobRun.id.desc(),
                )
                .limit(1)
            )).scalar_one_or_none()
            if latest is not None:
                if latest.status == ScheduledRunStatus.ERROR.value:
                    job_occurrence_key = (
                        f"{BLUEPRINT_SETUP_OCCURRENCE_KEY}:retry:{latest.id}"
                    )
                else:
                    job_occurrence_key = str(
                        latest.idempotency_key or BLUEPRINT_SETUP_OCCURRENCE_KEY
                    )
        occurrence = await dispatch_job_occurrence(
            job_db_id=job.id,
            occurrence_key=job_occurrence_key,
            trigger_type="blueprint_startup",
            trigger_detail=trigger,
        )
        if occurrence is not None and occurrence.published:
            dispatched.append(job.job_id)

    return BlueprintStartupResult(
        state=(
            state
            if dispatched
            else "dispatch_pending"
            if attempted_dispatch
            else "job_unavailable"
        ),
        dispatched_job_ids=tuple(dispatched),
        blocking_check_keys=blocking_keys,
    )
