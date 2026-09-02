"""Persisted Blueprint simulation runs inside normal Workspace Chat.

The runtime is deliberately small and deterministic.  A Blueprint owns the
scenario; this service writes each stage through the same conversation/message
service as a live Workspace and stops at the same pending-action cards.  It
never calls tools, connectors, publishers, or external systems.

Run state lives in ``workspace.settings`` so existing installations can be
repaired without a schema migration.  Messages remain the durable audit trail.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.simulation import (
    generate_simulation_experience,
    resolve_simulation_experience,
)
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.workspace_chat import service as chat_service


SIMULATION_RUN_STATE_KEY = "simulation_run"
SIMULATION_RUNTIME_VERSION = "1.0"


class SimulationRuntimeError(ValueError):
    """Raised when a Workspace cannot run a safe Blueprint simulation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_sandbox(workspace: Workspace) -> bool:
    settings = _record(workspace.settings)
    return settings.get("sandbox") is True or workspace.kind == "sandbox"


async def _lock_workspace_settings(
    db: AsyncSession,
    workspace: Workspace,
) -> Workspace:
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )

    locked_workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
    )
    if locked_workspace is None or locked_workspace.deleted_at is not None:
        raise SimulationRuntimeError("workspace no longer exists")
    return locked_workspace


async def _source_payload(
    db: AsyncSession,
    workspace: Workspace,
) -> dict[str, Any] | None:
    blueprint = _record(_record(workspace.settings).get("_blueprint"))
    blueprint_id = str(blueprint.get("blueprint_id") or "").strip()
    slug = str(blueprint.get("blueprint_slug") or "").strip()

    builtin_slug = (
        blueprint_id.removeprefix("builtin:")
        if blueprint_id.startswith("builtin:")
        else slug
    )
    if builtin_slug:
        try:
            # Lazy import avoids making the frozen Blueprint registry part of
            # module import order for the simulation contract itself.
            from packages.core.blueprints.solo_company import (
                get_solo_company_blueprint,
            )

            return get_solo_company_blueprint(builtin_slug)
        except KeyError:
            pass

    if blueprint_id and not blueprint_id.startswith("builtin:"):
        row = await db.get(WorkspaceBlueprint, blueprint_id)
        if row is not None and isinstance(row.payload, dict):
            return deepcopy(row.payload)
    return None


def _fallback_payload(workspace: Workspace) -> dict[str, Any]:
    title = workspace.name.removeprefix("[SIM] ").strip() or "Workspace"
    primary_work = (
        str(workspace.primary_work or "").strip()
        or str(workspace.description or "").strip()
        or f"Complete the primary work for {title}."
    )
    return {
        "manifest": {
            "slug": f"workspace-{workspace.id.lower()}",
            "title": title,
            "summary": str(workspace.description or primary_work),
            "category": str(workspace.category or workspace.kind or "workspace"),
        },
        "contract": {"requires": {"tools": []}},
        "recipe": {
            "operating_model": {
                **_record(workspace.operating_model),
                "primary_work": primary_work,
            },
            "workflows": [],
            "goals": [],
            "scheduled_jobs": [],
        },
        "policy": {"expected_baseline": {}},
    }


async def ensure_simulation_experience(
    db: AsyncSession,
    *,
    workspace: Workspace,
) -> dict[str, Any]:
    """Return a stage-complete experience and repair legacy sandboxes.

    Old installs often contain only ``sandbox=true`` (or an artifact-only v1.0
    experience).  Rehydrate from the installed Blueprint when possible; fall
    back to the Workspace operating model when the source Blueprint no longer
    exists.
    """
    if not _is_sandbox(workspace):
        raise SimulationRuntimeError("workspace is not in Workspace simulation mode")

    settings = dict(_record(workspace.settings))
    current = _record(settings.get("simulation_experience"))
    if isinstance(current.get("stages"), list) and current["stages"]:
        return deepcopy(current)

    workspace = await _lock_workspace_settings(db, workspace)
    settings = dict(_record(workspace.settings))
    current = _record(settings.get("simulation_experience"))
    if isinstance(current.get("stages"), list) and current["stages"]:
        return deepcopy(current)

    payload = await _source_payload(db, workspace)
    experience = (
        resolve_simulation_experience(payload)
        if payload is not None
        else generate_simulation_experience(_fallback_payload(workspace))
    )
    settings["sandbox"] = True
    settings["simulation_experience"] = deepcopy(experience)
    settings["simulation_repaired_at"] = _now()
    workspace.settings = settings
    await db.flush()
    return experience


async def _subscription_map(
    db: AsyncSession,
    *,
    workspace: Workspace,
) -> dict[str, str]:
    subscriptions = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    return {
        str(subscription.service_key): subscription.id
        for subscription in subscriptions
        if subscription.service_key
    }


def _public_state(
    workspace: Workspace,
    *,
    experience: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = _record(workspace.settings)
    resolved_experience = experience or _record(settings.get("simulation_experience"))
    stages = resolved_experience.get("stages")
    stage_count = len(stages) if isinstance(stages, list) else 0
    state = _record(settings.get(SIMULATION_RUN_STATE_KEY))
    next_index = max(0, min(int(state.get("next_stage_index") or 0), stage_count))
    return {
        "workspace_id": workspace.id,
        "enabled": _is_sandbox(workspace),
        "run_id": state.get("run_id"),
        "status": state.get("status") or "idle",
        "title": resolved_experience.get("title") or "Workspace simulation",
        "goal_title": resolved_experience.get("sample_prompt") or workspace.primary_work or workspace.name,
        "stage_index": next_index,
        "stage_count": stage_count,
        "stage_id": state.get("current_stage_id"),
        "stage_title": state.get("current_stage_title") or "Preparing Blueprint scenario",
        "waiting_message_id": state.get("waiting_message_id"),
        "last_message_id": state.get("last_message_id"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "completed_at": state.get("completed_at"),
        "decision_count": len(state.get("decisions") or []),
        "runtime_version": state.get("runtime_version") or SIMULATION_RUNTIME_VERSION,
    }


async def get_simulation_run(
    db: AsyncSession,
    *,
    workspace: Workspace,
) -> dict[str, Any]:
    if not _is_sandbox(workspace):
        return _public_state(workspace)
    experience = await ensure_simulation_experience(db, workspace=workspace)
    return _public_state(workspace, experience=experience)


async def _close_open_run_actions(
    db: AsyncSession,
    *,
    workspace: Workspace,
    run_id: str | None,
    user_id: str | None,
) -> None:
    if not run_id:
        return
    rows = list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == workspace.entity_id,
            Conversation.workspace_id == workspace.id,
            Message.pending_action.isnot(None),
            Message.resolved_at.is_(None),
        )
    )).scalars().all())
    for message in rows:
        meta = _record(message.meta)
        if meta.get("simulation_run_id") != run_id:
            continue
        message.resolved_at = datetime.now(timezone.utc)
        message.resolved_by_user_id = user_id
        message.resolution = {
            "choice": "stopped",
            "note": "Superseded by a new simulation run.",
        }


async def start_simulation_run(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user_id: str | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    workspace = await _lock_workspace_settings(db, workspace)
    experience = await ensure_simulation_experience(db, workspace=workspace)
    settings = dict(_record(workspace.settings))
    existing = _record(settings.get(SIMULATION_RUN_STATE_KEY))
    if existing.get("run_id") and not restart:
        return _public_state(workspace, experience=experience)

    if restart:
        await _close_open_run_actions(
            db,
            workspace=workspace,
            run_id=str(existing.get("run_id") or "") or None,
            user_id=user_id,
        )

    started_at = _now()
    state = {
        "runtime_version": SIMULATION_RUNTIME_VERSION,
        "run_id": generate_ulid(),
        "status": "running",
        "next_stage_index": 0,
        "current_stage_id": None,
        "current_stage_title": "Preparing Blueprint scenario",
        "waiting_message_id": None,
        "last_message_id": None,
        "started_at": started_at,
        "updated_at": started_at,
        "completed_at": None,
        "decisions": [],
    }
    settings[SIMULATION_RUN_STATE_KEY] = state
    workspace.settings = settings
    await db.flush()
    return await advance_simulation_run(
        db,
        workspace=workspace,
        user_id=user_id,
        experience=experience,
    )


async def advance_simulation_run(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user_id: str | None = None,
    experience: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = await _lock_workspace_settings(db, workspace)
    resolved_experience = experience or await ensure_simulation_experience(
        db, workspace=workspace,
    )
    stages = resolved_experience.get("stages")
    if not isinstance(stages, list) or not stages:
        raise SimulationRuntimeError("simulation experience has no stages")

    settings = dict(_record(workspace.settings))
    state = dict(_record(settings.get(SIMULATION_RUN_STATE_KEY)))
    if not state.get("run_id"):
        raise SimulationRuntimeError("simulation run has not been started")
    if state.get("status") == "completed":
        return _public_state(workspace, experience=resolved_experience)

    subscriptions = await _subscription_map(db, workspace=workspace)
    artifact_by_id = {
        str(artifact.get("id")): artifact
        for artifact in resolved_experience.get("artifacts") or []
        if isinstance(artifact, dict) and artifact.get("id")
    }
    created_by_user_id = str(
        user_id or _record(workspace.settings).get("created_by_user_id") or ""
    ) or None
    next_index = max(0, int(state.get("next_stage_index") or 0))
    run_id = str(state["run_id"])
    state["status"] = "running"
    state["waiting_message_id"] = None

    while next_index < len(stages):
        raw_stage = stages[next_index]
        if not isinstance(raw_stage, dict):
            raise SimulationRuntimeError(f"simulation stage {next_index} is invalid")
        stage = deepcopy(raw_stage)
        stage_id = str(stage.get("id") or f"stage-{next_index + 1}")
        stage_title = str(stage.get("title") or stage_id)
        author = str(stage.get("author") or "agent")
        service_key = str(stage.get("service_key") or "")
        subscription_id = subscriptions.get(service_key) if service_key else None
        pending_action = deepcopy(stage.get("pending_action"))
        if isinstance(pending_action, dict) and pending_action.get("kind"):
            pending_action.update({
                "simulation_runtime": True,
                "simulation_run_id": run_id,
                "simulation_stage_id": stage_id,
                "simulation_next_stage_index": next_index + 1,
            })
        else:
            pending_action = None

        meta = dict(_record(stage.get("meta")))
        meta.update({
            "simulation_runtime": True,
            "simulation_run_id": run_id,
            "simulation_stage_id": stage_id,
            "simulation_stage_kind": stage.get("kind"),
            "simulation_stage_index": next_index + 1,
            "simulation_stage_count": len(stages),
            "simulation_stage_title": stage_title,
        })
        if subscription_id and meta.get("workflow_steps"):
            meta["workflow_current_subscription_id"] = subscription_id

        artifact_ids = stage.get("artifact_ids")
        if isinstance(artifact_ids, list):
            simulation_artifacts = [
                deepcopy(artifact_by_id[artifact_id])
                for artifact_id in artifact_ids
                if artifact_id in artifact_by_id
            ]
            if simulation_artifacts:
                meta["simulation_artifacts"] = simulation_artifacts

        message_kind = str(
            stage.get("message_kind")
            or ("proposal" if stage.get("kind") == "proposal" else "text")
        )
        message = await chat_service.post_message(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            body=str(stage.get("body") or stage_title),
            message_kind=message_kind,
            author_kind=author,
            author_user_id=created_by_user_id if author == "user" else None,
            author_subscription_id=subscription_id if author == "agent" else None,
            pending_action=pending_action,
            meta=meta,
        )

        next_index += 1
        state.update({
            "next_stage_index": next_index,
            "current_stage_id": stage_id,
            "current_stage_title": stage_title,
            "last_message_id": message.id,
            "updated_at": _now(),
        })
        if pending_action:
            state["status"] = "waiting"
            state["waiting_message_id"] = message.id
            break

    if next_index >= len(stages) and state.get("status") != "waiting":
        state["status"] = "completed"
        state["completed_at"] = _now()
        state["current_stage_title"] = str(
            stages[-1].get("title") or "Goal completed"
        )

    settings[SIMULATION_RUN_STATE_KEY] = state
    workspace.settings = settings
    await db.flush()
    return _public_state(workspace, experience=resolved_experience)


async def resolve_simulation_action(
    db: AsyncSession,
    *,
    workspace: Workspace,
    message: Message,
    user_id: str,
    choice: str,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Message:
    workspace = await _lock_workspace_settings(db, workspace)
    action = _record(message.pending_action)
    if action.get("simulation_runtime") is not True:
        raise SimulationRuntimeError("message is not a simulation action")

    settings = dict(_record(workspace.settings))
    state = dict(_record(settings.get(SIMULATION_RUN_STATE_KEY)))
    run_id = str(action.get("simulation_run_id") or "")
    if not run_id or run_id != str(state.get("run_id") or ""):
        raise SimulationRuntimeError("simulation action belongs to an inactive run")

    resolution: dict[str, Any] = {"choice": choice}
    if note:
        resolution["note"] = note
    if payload is not None:
        resolution["payload"] = payload
    resolved = await chat_service.resolve_pending_action(
        db,
        message_id=message.id,
        user_id=user_id,
        resolution=resolution,
        emit_followup=False,
    )
    if resolved is None:
        raise SimulationRuntimeError("simulation action message no longer exists")

    decisions = list(state.get("decisions") or [])
    decisions.append({
        "stage_id": action.get("simulation_stage_id"),
        "choice": choice,
        "note": note,
        "resolved_at": _now(),
        "resolved_by_user_id": user_id,
    })
    state.update({
        "status": "running",
        "waiting_message_id": None,
        "next_stage_index": int(
            action.get("simulation_next_stage_index")
            or state.get("next_stage_index")
            or 0
        ),
        "decisions": decisions,
        "updated_at": _now(),
    })
    settings[SIMULATION_RUN_STATE_KEY] = state
    workspace.settings = settings
    await db.flush()

    normalized = str(choice or "").strip().lower().replace("_", " ")
    if "reject" in normalized or "revise" in normalized or normalized in {"skip", "cancel"}:
        decision_body = (
            f"Decision recorded: **{normalized or 'alternate path'}**. "
            "The simulation will use the safe alternate branch and continue the Blueprint run."
        )
    else:
        decision_body = (
            f"Decision recorded: **{normalized or 'continue'}**. "
            "The persisted simulation is continuing from the next stage."
        )
    await chat_service.post_message(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        body=decision_body,
        message_kind="system",
        author_kind="system",
        meta={
            "simulation_runtime": True,
            "simulation_run_id": run_id,
            "simulation_resolution_receipt": True,
            "simulation_stage_id": action.get("simulation_stage_id"),
        },
    )
    await advance_simulation_run(db, workspace=workspace, user_id=user_id)
    return resolved


__all__ = [
    "SIMULATION_RUNTIME_VERSION",
    "SimulationRuntimeError",
    "advance_simulation_run",
    "ensure_simulation_experience",
    "get_simulation_run",
    "resolve_simulation_action",
    "start_simulation_run",
]
