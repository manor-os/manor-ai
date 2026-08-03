"""M10 execution router for the three configuration-change kinds.

``apply_change_item(db, item)`` is the FORMAL counterpart of the experiment
overlay (``packages.core.experiments.controller``): where an experiment
hangs a temporary patch on a target *without* touching its revision, an
approved change item edits the canonical row and **bumps** it.

Authoritative CAS lives HERE, not in the validator: the target row is
re-read ``SELECT … FOR UPDATE`` and ``assert_revision``-checked inside the
apply savepoint, because the interesting race is exactly the approval
wait (operator sits on the card while an operator/agent edits the row).
The validator precheck only saves an obviously-doomed item from ever
minting a HitlRequest.

裁定 B: ``ScheduledJob`` / ``WorkflowBinding`` rows are the canonical
automation store; ``Workspace.operating_model["automations"]`` is a
derived display index refreshed here after every applied change.

Delete semantics per model (mirrors the existing service delete paths —
there is no soft-delete convention anywhere in this codebase):

* ``scheduled_job`` — hard delete (``scheduler_service.delete_scheduled_job``
  does the same). The ``automation_revisions`` audit row is written first,
  so the deletion stays reconstructible from the ledger + audit trail.
* ``workflow_binding`` — hard delete (``workflow_service.delete_binding``),
  refused while automations still reference the binding.
* ``workflow_definition`` — hard delete (``workflow_service.delete_workflow``),
  refused while any binding still deploys it (deleting a template out from
  under its deployments would silently break every run).
* ``goal`` — ``archive`` is genuinely soft: Goal has a real status
  vocabulary, so archive → ``status="abandoned"`` through ``update_goal``
  (which also removes the measurement schedule).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.workspace import Workspace
from packages.core.proposals.constants import change_patch_whitelist
from packages.core.revisions import StaleRevisionError, assert_revision, bump_revision

logger = logging.getLogger(__name__)

MODEL_BY_TARGET_KIND: dict[str, type] = {
    "scheduled_job": ScheduledJob,
    "workflow_binding": WorkflowBinding,
    "workflow_definition": WorkflowDefinition,
    "goal": Goal,
}

# Derived-index entries carry this marker so a refresh only ever rebuilds
# its own rows and leaves operator/architect-authored automation
# declarations (which have no canonical row) alone.
DERIVED_INDEX_SOURCE = "canonical_row"


class ChangeApplyError(Exception):
    """The change cannot be applied as specified (bad target, missing
    required field, referenced-elsewhere delete, …)."""

    def __init__(self, message: str, *, code: str = "APPLY_ERROR"):
        self.code = code
        super().__init__(message)


# ── value coercion ────────────────────────────────────────────────────


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ChangeApplyError(
            f"invalid date {value!r}: {exc}", code="INVALID_PATCH",
        ) from exc


def _as_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChangeApplyError(
            f"invalid numeric value {value!r}", code="INVALID_PATCH",
        ) from exc


def _clean_patch(target_kind: str, operation: str, patch: Any) -> dict:
    """Second-line whitelist enforcement (the pydantic layer is the first).

    The payload could have been written by an older schema version or a
    non-LLM caller, so the executor never trusts it blind.
    """
    patch = patch if isinstance(patch, dict) else {}
    allowed = change_patch_whitelist(target_kind, operation)
    unknown = sorted(set(patch) - allowed)
    if unknown:
        raise ChangeApplyError(
            f"patch keys {unknown} are not changeable on {target_kind} "
            f"({operation})",
            code="INVALID_PATCH",
        )
    return dict(patch)


# ── derived automation index (裁定 B) ──────────────────────────────────


def _schedule_summary(job: ScheduledJob) -> str:
    if job.cron_expr:
        return f"cron {job.cron_expr}"
    if job.every_seconds:
        return f"every {job.every_seconds:g}s"
    if job.run_at:
        return f"at {job.run_at}"
    return job.schedule_kind or job.job_type or "unknown"


def _automation_key_for(row: Any, target_kind: str) -> str:
    import re

    raw = (
        getattr(row, "job_id", None)
        or getattr(row, "name", None)
        or f"{target_kind}_{row.id}"
    )
    key = re.sub(r"[^a-z0-9_]+", "_", str(raw).lower()).strip("_")
    return key or f"{target_kind}_{row.id}".lower()


async def refresh_automation_index(
    db: AsyncSession, workspace: Workspace,
) -> list[dict]:
    """裁定 B: rebuild ``operating_model["automations"]`` from the canonical
    ScheduledJob / WorkflowBinding rows.

    Non-destructive by design: only entries this function owns (marked
    ``source == DERIVED_INDEX_SOURCE``, or carrying a ``target_id``) are
    rebuilt. Architect-authored declarations — the ``{automation_key,
    description, trigger, service_key}`` shape written by ``ws_add_automation``
    into the workspace draft — have no canonical row and survive untouched;
    where an authored entry shares an automation_key with a canonical row,
    the authored prose (description/service_key) is preserved and only the
    canonical facts are overwritten. That keeps the existing readers
    (`workspace_operation_service` state, workspace validation) working on
    the shape they already expect.

    **``operation_revision`` is deliberately NOT bumped.** That counter is
    the operator-authored operating-model version: it gates draft
    publication (``draft.base_revision`` CAS in workspace_operation_service)
    and is frozen into ``review_runs.workspace_revision``. Bumping it for a
    derived refresh would invalidate in-flight operating-model drafts and
    make every automation toggle look like an operator model edit. The
    authoritative version for these changes is the row's own ``revision``.
    """
    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == workspace.entity_id,
        ).order_by(ScheduledJob.id.asc())
    )).scalars().all())
    jobs = [
        job for job in jobs
        if job.workspace_id == workspace.id
        or (job.execution_target or {}).get("workspace_id") == workspace.id
    ]
    bindings = list((await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == workspace.id,
        ).order_by(WorkflowBinding.id.asc())
    )).scalars().all())

    operating_model = (
        dict(workspace.operating_model)
        if isinstance(workspace.operating_model, dict) else {}
    )
    existing = [
        entry for entry in (operating_model.get("automations") or [])
        if isinstance(entry, dict)
    ]
    by_key = {str(entry.get("automation_key") or ""): entry for entry in existing}

    derived: list[dict] = []
    for job in jobs:
        key = _automation_key_for(job, "scheduled_job")
        prior = by_key.get(key, {})
        derived.append({
            **prior,
            "automation_key": key,
            "description": prior.get("description") or (job.name or job.job_id),
            "trigger": _schedule_summary(job),
            "service_key": prior.get("service_key", ""),
            "target_kind": "scheduled_job",
            "target_id": job.id,
            "enabled": bool(job.enabled),
            "revision": int(job.revision or 1),
            "source": DERIVED_INDEX_SOURCE,
        })
    for binding in bindings:
        key = _automation_key_for(binding, "workflow_binding")
        prior = by_key.get(key, {})
        derived.append({
            **prior,
            "automation_key": key,
            "description": prior.get("description") or (binding.name or binding.workflow_id),
            "trigger": binding.trigger_type or "manual",
            "service_key": prior.get("service_key", ""),
            "target_kind": "workflow_binding",
            "target_id": binding.id,
            "enabled": bool(binding.enabled) and binding.status == "active",
            "revision": int(binding.revision or 1),
            "source": DERIVED_INDEX_SOURCE,
        })

    derived_keys = {entry["automation_key"] for entry in derived}
    authored = [
        entry for entry in existing
        if entry.get("source") != DERIVED_INDEX_SOURCE
        and not entry.get("target_id")
        and str(entry.get("automation_key") or "") not in derived_keys
    ]
    automations = derived + authored
    operating_model["automations"] = automations
    # Reassign (not mutate) so SQLAlchemy sees the JSONB change.
    workspace.operating_model = operating_model
    await db.flush()
    return automations


# ── ledger ────────────────────────────────────────────────────────────


async def _record_change_event(
    db: AsyncSession,
    item: ProposalItemRecord,
    *,
    target_kind: str,
    target_id: str,
    operation: str,
    revision: Optional[int],
) -> None:
    from packages.core.ledger.adapters import record_change_item_applied

    await record_change_item_applied(
        db,
        item,
        target_kind=target_kind,
        target_id=target_id,
        operation=operation,
        revision=revision,
    )


# ── per-target apply ──────────────────────────────────────────────────


async def _load_for_update(db: AsyncSession, model: type, target_id: str):
    """Re-read the row under a row lock, discarding any stale in-session
    copy, so the CAS compares against what is actually committed."""
    return (await db.execute(
        select(model)
        .where(model.id == target_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


def _apply_scheduled_job_patch(job: ScheduledJob, patch: dict) -> dict:
    applied: dict = {}
    for key, value in patch.items():
        if key == "execution_target":
            if not isinstance(value, dict):
                raise ChangeApplyError(
                    "execution_target patch must be an object", code="INVALID_PATCH",
                )
            merged = {**(job.execution_target or {}), **value}
            job.execution_target = merged
            applied["execution_target"] = value
            continue
        if key == "enabled":
            job.enabled = _as_bool(value)
            applied["enabled"] = job.enabled
            continue
        if key == "every_seconds":
            job.every_seconds = float(value) if value is not None else None
            applied["every_seconds"] = job.every_seconds
            continue
        setattr(job, key, value)
        applied[key] = value
    return applied


def _apply_binding_patch(binding: WorkflowBinding, patch: dict) -> dict:
    applied: dict = {}
    for key, value in patch.items():
        if key == "enabled":
            binding.enabled = _as_bool(value)
            applied["enabled"] = binding.enabled
            continue
        if key == "trigger_ref":
            # ``trigger_ref`` is the proposal-facing name for the binding's
            # trigger target; the column is the wider ``trigger_config``
            # JSONB, so it lands on its ``ref`` key (shallow merge — the
            # webhook token and friends must survive).
            binding.trigger_config = {**(binding.trigger_config or {}), "ref": value}
            applied["trigger_ref"] = value
            continue
        setattr(binding, key, value)
        applied[key] = value
    return applied


def _apply_definition_patch(definition: WorkflowDefinition, patch: dict) -> dict:
    applied: dict = {}
    for key, value in patch.items():
        setattr(definition, key, value)
        applied[key] = value
    return applied


async def _apply_automation_or_workflow(
    db: AsyncSession,
    item: ProposalItemRecord,
    *,
    target_kind: str,
    operation: str,
    target_id: Optional[str],
    expected_revision: Optional[int],
    patch: dict,
) -> dict:
    model = MODEL_BY_TARGET_KIND[target_kind]

    if operation == "create":
        row = await _create_row(db, item, target_kind=target_kind, patch=patch)
        revision = int(getattr(row, "revision", 1) or 1)
        return {"target_id": row.id, "revision": revision, "applied": patch}

    row = await _load_for_update(db, model, str(target_id))
    if row is None:
        raise ChangeApplyError(
            f"{target_kind} {target_id} no longer exists", code="INSUFFICIENT_DATA",
        )
    # Authoritative CAS — raises StaleRevisionError on a concurrent edit.
    await assert_revision(row, expected_revision)

    if operation in ("pause", "resume"):
        enabled = operation == "resume"
        if target_kind == "workflow_definition":
            row.is_active = enabled
            row.status = "active" if enabled else "paused"
            applied = {"is_active": enabled, "status": row.status}
        else:
            row.enabled = enabled
            applied = {"enabled": enabled}
    elif operation == "update":
        if target_kind == "scheduled_job":
            applied = _apply_scheduled_job_patch(row, patch)
        elif target_kind == "workflow_binding":
            applied = _apply_binding_patch(row, patch)
        else:
            applied = _apply_definition_patch(row, patch)
    elif operation == "delete":
        applied = {"deleted": True}
        await _assert_deletable(db, row, target_kind)
    else:
        raise ChangeApplyError(
            f"unsupported operation {operation!r} for {target_kind}",
            code="INVALID_CHANGE",
        )

    revision = await bump_revision(
        db, row, patch=applied, changed_by_kind="agent", causation_id=item.id,
    )
    if operation == "delete":
        # Audit row is written above; the row itself goes (mirrors the
        # existing hard-delete service paths).
        await db.delete(row)
        await db.flush()
    return {"target_id": str(target_id), "revision": revision, "applied": applied}


async def _assert_deletable(db: AsyncSession, row: Any, target_kind: str) -> None:
    if target_kind == "workflow_binding":
        from packages.core.services.workflow_service import (
            binding_automation_references,
        )
        refs = await binding_automation_references(db, row)
        if refs:
            raise ChangeApplyError(
                f"workflow_binding {row.id} is still referenced by "
                f"{len(refs)} automation(s)",
                code="TARGET_IN_USE",
            )
    elif target_kind == "workflow_definition":
        bound = (await db.execute(
            select(WorkflowBinding.id).where(
                WorkflowBinding.workflow_id == row.id,
            ).limit(1)
        )).scalar_one_or_none()
        if bound is not None:
            raise ChangeApplyError(
                f"workflow_definition {row.id} still has binding(s) deployed",
                code="TARGET_IN_USE",
            )


async def _create_row(
    db: AsyncSession,
    item: ProposalItemRecord,
    *,
    target_kind: str,
    patch: dict,
) -> Any:
    if target_kind == "scheduled_job":
        if not (patch.get("cron_expr") or patch.get("every_seconds")):
            raise ChangeApplyError(
                "creating a scheduled_job requires cron_expr or every_seconds",
                code="INSUFFICIENT_PATCH",
            )
        execution_target = dict(patch.get("execution_target") or {})
        execution_target.setdefault("workspace_id", item.workspace_id)
        row = ScheduledJob(
            id=generate_ulid(),
            job_id=str(patch.get("job_id") or f"job_{generate_ulid()}"),
            entity_id=item.entity_id,
            workspace_id=item.workspace_id,
            name=patch.get("name"),
            schedule_kind=patch.get("schedule_kind")
            or ("cron" if patch.get("cron_expr") else "every"),
            cron_expr=patch.get("cron_expr"),
            every_seconds=(
                float(patch["every_seconds"])
                if patch.get("every_seconds") is not None else None
            ),
            timezone=patch.get("timezone") or "UTC",
            payload_message=patch.get("payload_message"),
            execution_type=patch.get("execution_type") or "agent",
            agent_id=patch.get("agent_id"),
            execution_target=execution_target,
            enabled=_as_bool(patch.get("enabled", True)),
        )
    elif target_kind == "workflow_binding":
        workflow_id = patch.get("workflow_id")
        if not workflow_id:
            raise ChangeApplyError(
                "creating a workflow_binding requires patch.workflow_id",
                code="INSUFFICIENT_PATCH",
            )
        row = WorkflowBinding(
            id=generate_ulid(),
            entity_id=item.entity_id,
            workflow_id=str(workflow_id),
            workspace_id=item.workspace_id,
            name=patch.get("name"),
            trigger_type=patch.get("trigger_type") or "manual",
            trigger_config=dict(patch.get("trigger_config") or {}),
            variables=dict(patch.get("variables") or {}),
            enabled=_as_bool(patch.get("enabled", True)),
            status=patch.get("status") or "active",
        )
    elif target_kind == "workflow_definition":
        if not patch.get("name") or not patch.get("steps"):
            raise ChangeApplyError(
                "creating a workflow_definition requires patch.name and patch.steps",
                code="INSUFFICIENT_PATCH",
            )
        row = WorkflowDefinition(
            id=generate_ulid(),
            entity_id=item.entity_id,
            name=str(patch["name"]),
            description=patch.get("description"),
            trigger_type=patch.get("trigger_type") or "manual",
            steps=list(patch.get("steps") or []),
            variables=dict(patch.get("variables") or {}),
        )
    else:
        raise ChangeApplyError(
            f"cannot create target_kind {target_kind!r}", code="INVALID_CHANGE",
        )
    db.add(row)
    await db.flush()
    return row


# ── goals ─────────────────────────────────────────────────────────────


_GOAL_STATUS_BY_OPERATION = {"pause": "paused", "archive": "abandoned"}


async def _apply_goal_change(
    db: AsyncSession,
    item: ProposalItemRecord,
    *,
    operation: str,
    target_id: Optional[str],
    expected_revision: Optional[int],
    patch: dict,
) -> dict:
    from packages.core.goals.service import create_goal, update_goal

    if operation == "create":
        if not patch.get("title") or not patch.get("metric_key"):
            raise ChangeApplyError(
                "creating a goal requires patch.title and patch.metric_key",
                code="INSUFFICIENT_PATCH",
            )
        if patch.get("target_value") is None:
            raise ChangeApplyError(
                "creating a goal requires patch.target_value",
                code="INSUFFICIENT_PATCH",
            )
        goal = await create_goal(
            db,
            entity_id=item.entity_id,
            workspace_id=item.workspace_id,
            title=str(patch["title"]),
            description=patch.get("description"),
            metric_key=str(patch["metric_key"]),
            target_value=_as_decimal(patch["target_value"]),
            baseline_value=_as_decimal(patch.get("baseline_value")),
            deadline=_as_date(patch.get("deadline")),
            measurement_source=patch.get("measurement_source"),
            measurement_cadence=patch.get("measurement_cadence"),
            priority=int(patch.get("priority") or 3),
        )
        return {
            "target_id": goal.id,
            "revision": int(goal.revision or 1),
            "applied": patch,
        }

    goal = await _load_for_update(db, Goal, str(target_id))
    if goal is None:
        raise ChangeApplyError(
            f"goal {target_id} no longer exists", code="INSUFFICIENT_DATA",
        )
    await assert_revision(goal, expected_revision)

    fields: dict = {}
    if operation in _GOAL_STATUS_BY_OPERATION:
        fields["status"] = _GOAL_STATUS_BY_OPERATION[operation]
    for key, value in patch.items():
        if key == "deadline":
            fields["deadline"] = _as_date(value)
        elif key in ("target_value", "baseline_value"):
            fields[key] = _as_decimal(value)
        elif key == "priority":
            fields["priority"] = int(value)
        else:
            fields[key] = value

    # Goes through the goals service so the revision bump AND the
    # measurement-schedule side effects (install/remove) both fire.
    updated = await update_goal(db, goal.id, item.entity_id, **fields)
    if updated is None:
        raise ChangeApplyError(
            f"goal {target_id} could not be updated", code="INSUFFICIENT_DATA",
        )
    return {
        "target_id": updated.id,
        "revision": int(updated.revision or 1),
        "applied": {k: str(v) if isinstance(v, (date, Decimal)) else v
                    for k, v in fields.items()},
    }


# ── entry point ───────────────────────────────────────────────────────


async def apply_change_item(db: AsyncSession, item: ProposalItemRecord) -> dict:
    """Apply one approved change item; never raises.

    Marks the item ``succeeded`` / ``failed`` (with ``finished_at`` and
    ``execution_root_id`` = the item id, per the M10 routing table), emits
    the ledger fact, refreshes the 裁定 B derived index, and returns a
    digest ``{item_id, ok, target_kind, target_id, operation, revision,
    error, error_code}``.

    The mutation runs inside a SAVEPOINT so a CAS miss (or any other
    failure) leaves the target row exactly as it was while the item's
    failure bookkeeping still commits with the review.
    """
    payload = item.payload if isinstance(item.payload, dict) else {}
    operation = str(payload.get("operation") or "")
    target_kind = str(payload.get("target_kind") or "")
    target_id = payload.get("target_id")
    expected_revision = payload.get("expected_revision")
    if expected_revision is None:
        expected_revision = item.expected_revision

    digest: dict = {
        "item_id": item.id,
        "kind": item.kind,
        "operation": operation,
        "target_kind": target_kind,
        "target_id": str(target_id) if target_id else None,
        "ok": False,
        "revision": None,
        "error": None,
        "error_code": None,
    }

    try:
        if target_kind not in MODEL_BY_TARGET_KIND:
            raise ChangeApplyError(
                f"unknown target_kind {target_kind!r}", code="INVALID_CHANGE",
            )
        patch = _clean_patch(target_kind, operation, payload.get("patch"))
        async with db.begin_nested():
            if item.kind == "goal_change":
                result = await _apply_goal_change(
                    db, item,
                    operation=operation,
                    target_id=target_id,
                    expected_revision=expected_revision,
                    patch=patch,
                )
            else:
                result = await _apply_automation_or_workflow(
                    db, item,
                    target_kind=target_kind,
                    operation=operation,
                    target_id=target_id,
                    expected_revision=expected_revision,
                    patch=patch,
                )
            await _record_change_event(
                db, item,
                target_kind=target_kind,
                target_id=result["target_id"],
                operation=operation,
                revision=result.get("revision"),
            )
            if target_kind != "goal":
                workspace = await db.get(Workspace, item.workspace_id)
                if workspace is not None:
                    await refresh_automation_index(db, workspace)
    except StaleRevisionError as exc:
        digest["error"] = str(exc)
        digest["error_code"] = "STALE_REVISION"
    except ChangeApplyError as exc:
        digest["error"] = str(exc)
        digest["error_code"] = exc.code
    except Exception as exc:  # noqa: BLE001 — one bad item must not kill the review
        logger.warning(
            "change item %s failed to apply: %s", item.id, exc, exc_info=True,
        )
        digest["error"] = str(exc)
        digest["error_code"] = "APPLY_ERROR"
    else:
        digest["ok"] = True
        digest["target_id"] = result["target_id"]
        digest["revision"] = result.get("revision")

    now = datetime.now(timezone.utc)
    item.finished_at = now
    item.execution_root_id = item.id  # M10: change items are their own root
    if digest["ok"]:
        item.status = "succeeded"
    else:
        item.status = "failed"
        decision = dict(item.decision or {})
        decision["error"] = digest["error"]
        decision["error_code"] = digest["error_code"]
        item.decision = decision
    await db.flush()
    return digest
