"""Monitoring Celery tasks — HITL reminders, digest, SLA sweep, readiness.

Tasks:
  hitl_waiting_reminder     — reminds people about cards waiting on them
  workspace.digest_sweep    — one daily digest per person
  sla_breach_check          — runs every 5 min, flips ``Task.sla_breached`` and
                              fires escalation rules when SLA windows expire
  workspace_readiness_check — setup monitoring and readiness-driven reviews

The former daily health briefing (a per-entity stats push that went out even
when every number was zero) and the 30-minute heartbeat check (a canned message
written into channel conversations but never sent to the channel) were retired;
the digest and reminders cover what people need to act on.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from packages.core.constants.goals import GoalStatus
from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)

MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() != "false"


def _run_async(coro):
    """Run an async coroutine from synchronous Celery worker context."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. HITL Waiting Reminder
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.hitl_waiting_reminder", max_retries=1)
def hitl_waiting_reminder(self):
    """Remind people about cards waiting on them (24h, then +72h)."""
    if not MONITOR_ENABLED:
        logger.info("hitl_waiting_reminder: disabled via MONITOR_ENABLED=false")
        return

    try:
        from packages.core.services.hitl_reminders import (
            run_hitl_reminder_sweep,
            run_unanswered_authorization_expiry,
        )

        expired = _run_async(run_unanswered_authorization_expiry())
        if expired:
            logger.info("hitl_waiting_reminder: skipped %d unanswered authorization(s)", expired)
        delivered = _run_async(run_hitl_reminder_sweep())
        if delivered:
            logger.info("hitl_waiting_reminder: delivered %d reminder(s)", delivered)
    except Exception as exc:
        logger.error("hitl_waiting_reminder failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(name="workspace.digest_sweep")
def workspace_digest_sweep() -> int:
    """Send each person's daily Workspace digest at 09:00 their time (every 15 min)."""
    if not MONITOR_ENABLED:
        logger.info("workspace_digest_sweep: disabled via MONITOR_ENABLED=false")
        return 0
    from packages.core.services.workspace_digest import run_workspace_digest_sweep

    sent = _run_async(run_workspace_digest_sweep())
    if sent:
        logger.info("workspace_digest_sweep: sent %d digest(s)", sent)
    return sent


@celery_app.task(name="workspace.attention_sweep")
def workspace_attention_sweep() -> dict:
    """Reconcile attention items with platform state, then tell what is due (every 5 min).

    Deterministic and read-only over source tables. Telling happens only for
    entities with the ``attention_delivery`` flag on (attention.delivery).
    """
    if not MONITOR_ENABLED:
        logger.info("workspace_attention_sweep: disabled via MONITOR_ENABLED=false")
        return {}
    from packages.core.attention.sweep import run_attention_sweep

    results = _run_async(run_attention_sweep())
    changed = {key: stats for key, stats in results.items() if any(stats.values())}
    if changed:
        logger.info("workspace_attention_sweep: %s", changed)
    return results


# ---------------------------------------------------------------------------
# 2. SLA Breach Check
# ---------------------------------------------------------------------------
#
# Periodic sweep that flips ``Task.sla_breached`` to True for any open
# task whose SLA response or resolution window has elapsed, and fires
# the first escalation rule configured for the policy. Without this
# beat entry the SLA chip in the UI would never go red on its own —
# ``check_sla_deadlines`` is a request-time function that nothing was
# calling on a timer.

@celery_app.task(bind=True, name="monitor.sla_breach_check", max_retries=1)
def sla_breach_check(self):
    """Sweep all entities for SLA breaches.

    Cheap when no policies exist (the join filters out entities with
    no SLAs immediately). Idempotent — already-breached tasks are
    excluded by the query, so re-running is safe.
    """
    if not MONITOR_ENABLED:
        logger.info("sla_breach_check: disabled via MONITOR_ENABLED=false")
        return

    try:
        breached = _run_async(_async_sla_breach_check())
        if breached:
            logger.info("sla_breach_check: %d task(s) newly breached", breached)
    except Exception as exc:
        logger.error("sla_breach_check failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc, countdown=60)


async def _async_sla_breach_check() -> int:
    from packages.core.database import create_worker_session
    from packages.core.services.task_automation_service import check_sla_deadlines

    async with create_worker_session()() as db:
        # entity_id=None → check all entities in one pass; the join
        # against task_sla_policies restricts the scan to tasks that
        # actually have a policy attached, so this stays cheap.
        breached = await check_sla_deadlines(db, entity_id=None)
        await db.commit()
        return breached


# ---------------------------------------------------------------------------
# Workspace readiness check — lightweight, no LLM
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="monitor.workspace_readiness_check", max_retries=0)
def workspace_readiness_check(self):
    """Check if any workspace became unblocked and trigger Strategist.

    Runs every 10 min. Compares workspace state snapshots to detect
    when missing setup items get resolved (integrations added, goals
    created, agents mapped). No LLM calls — pure DB queries.
    """
    try:
        triggered = _run_async(_async_readiness_check())
        if triggered:
            logger.info("workspace_readiness_check: triggered %d review(s)", triggered)
    except Exception as exc:
        logger.error("workspace_readiness_check failed: %s", exc, exc_info=True)


async def _async_readiness_check() -> int:
    from sqlalchemy import select, func
    from packages.core.database import create_worker_session
    from packages.core.models.workspace import Workspace, AgentSubscription
    from packages.core.models.goal import Goal
    from packages.core.services.notification_service import create_notification_once
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
    )
    from packages.core.strategist.context import workspace_configured_provider_keys

    triggered = 0
    async with create_worker_session()() as db:
        # Connection monitoring applies to every live Blueprint Workspace;
        # the legacy count-based Strategist trigger remains heartbeat-only.
        workspaces = list((await db.execute(
            select(Workspace).where(
                Workspace.status.in_((
                    "active",
                    "paused",
                    "provisioning",
                    "needs_setup",
                )),
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all())

        for ws in workspaces:
            settings = ws.settings or {}
            last_check = settings.get("_readiness_snapshot", {})
            last_check = last_check if isinstance(last_check, dict) else {}
            current = dict(last_check) if isinstance(last_check, dict) else {}
            changed = False
            if ws.heartbeat_enabled and ws.status == "active":
                agent_count = (await db.execute(
                    select(func.count()).select_from(AgentSubscription).where(
                        AgentSubscription.workspace_id == ws.id,
                        AgentSubscription.status == "active",
                    )
                )).scalar_one()
                active_goals = list((await db.execute(
                    select(Goal).where(
                        Goal.workspace_id == ws.id,
                        Goal.status == GoalStatus.ACTIVE.value,
                    )
                )).scalars().all())
                # Only providers this Workspace works with count: a new
                # integration elsewhere in the entity is not news here.
                providers = await workspace_configured_provider_keys(
                    db, ws, active_goals,
                )
                current = {
                    "agents": agent_count,
                    "goals": len(active_goals),
                    "integration_providers": len(providers),
                }
                changed = any(
                    current[key] > last_check.get(key, 0)
                    for key in ("agents", "goals")
                ) or (
                    # No baseline yet (first check after this key existed):
                    # record it without starting a review.
                    "integration_providers" in last_check
                    and current["integration_providers"] > last_check["integration_providers"]
                )

            if changed:
                # Something new was added — trigger Strategist
                try:
                    from packages.core.tasks.ai_tasks import run_strategist_review
                    diff_parts = []
                    for key in ("agents", "goals", "integration_providers"):
                        old = last_check.get(key, 0)
                        new = current[key]
                        if new > old:
                            diff_parts.append(f"{key}: {old}->{new}")
                    from packages.core.strategist import (
                        ReviewTrigger, ReviewTriggerKind,
                    )
                    run_strategist_review.apply_async(
                        args=[ws.id],
                        kwargs={
                            **ReviewTrigger(
                                kind=ReviewTriggerKind.EVENT,
                                detail=f"readiness changed: {', '.join(diff_parts)}",
                            ).celery_kwargs(),
                            "user_id": (ws.settings or {}).get("created_by_user_id"),
                        },
                        countdown=5,
                    )
                    triggered += 1
                except Exception:
                    logger.warning(
                        "workspace_readiness_check: failed to dispatch strategist for %s",
                        ws.id, exc_info=True,
                    )

            # Save snapshot for next check
            from packages.core.services.workspace_access import (
                lock_workspace_access_boundary,
            )

            locked_ws = await lock_workspace_access_boundary(
                db,
                workspace_id=ws.id,
                entity_id=ws.entity_id,
            )
            if locked_ws is None or locked_ws.deleted_at is not None:
                await db.rollback()
                continue
            from packages.core.attention.delivery import delivery_enabled

            new_settings = dict(locked_ws.settings or {})
            new_settings["_readiness_snapshot"] = current
            blueprint = new_settings.get("_blueprint")
            if isinstance(blueprint, dict):
                setup_status = await evaluate_workspace_blocking_setup(
                    db,
                    locked_ws,
                )
                previous_monitor = blueprint.get("setup_monitor")
                monitor, incident_started = _workspace_setup_monitor_state(
                    setup_status,
                    previous_monitor if isinstance(previous_monitor, dict) else {},
                )
                blueprint = dict(blueprint)
                blueprint["setup_monitor"] = monitor
                new_settings["_blueprint"] = blueprint
                # With attention delivery on, setup_incomplete tells instead.
                if incident_started and not await delivery_enabled(db, locked_ws.entity_id):
                    recipient = str(
                        new_settings.get("created_by_user_id") or ""
                    ).strip()
                    if recipient:
                        labels = [
                            item.get("label") or item.get("provider") or item.get("todo_kind")
                            for item in monitor["incomplete"]
                        ]
                        labels = [str(label) for label in labels if label]
                        await create_notification_once(
                            db,
                            locked_ws.entity_id,
                            recipient,
                            "workspace_setup_required",
                            f"{locked_ws.name} needs a connection update",
                            body=(
                                "Reconnect or configure the required Blueprint "
                                "dependencies before normal Workspace work resumes."
                            ),
                            link=f"/workspaces/{locked_ws.id}?tab=overview",
                            meta={
                                "incident": monitor["incident"],
                                "requirements": monitor["incomplete"],
                                "labels": labels,
                            },
                            workspace_id=locked_ws.id,
                            idempotency_key=(
                                f"workspace-setup:{locked_ws.id}:"
                                f"{monitor['incident']}"
                            ),
                        )
            locked_ws.settings = new_settings
            startup_workspace_id = str(locked_ws.id)
            await db.commit()
            await _reconcile_blueprint_startup_after_monitor(
                db,
                workspace_id=startup_workspace_id,
                settings=new_settings,
            )

    return triggered


async def _reconcile_blueprint_startup_after_monitor(
    db,
    *,
    workspace_id: str,
    settings: dict[str, Any] | None,
) -> None:
    current_settings = settings if isinstance(settings, dict) else {}
    startup = current_settings.get("blocking_setup")
    if not isinstance(startup, dict):
        return
    checks = startup.get("checks")
    opted_in = bool(str(startup.get("on_ready_job_id") or "").strip()) or any(
        isinstance(check, dict)
        and str(check.get("setup_job_id") or "").strip()
        for check in (checks if isinstance(checks, list) else [])
    )
    if not opted_in:
        return
    try:
        from packages.core.services.blueprint_startup_service import (
            reconcile_blueprint_startup,
        )

        await reconcile_blueprint_startup(
            db,
            workspace_id=workspace_id,
            trigger="readiness_monitor",
        )
    except Exception:
        await db.rollback()
        logger.exception(
            "workspace_readiness_check: Blueprint startup reconciliation "
            "failed for %s",
            workspace_id,
        )


def _workspace_setup_monitor_state(
    setup_status: Any,
    previous: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return a stable connection-incident snapshot and transition signal."""
    details = getattr(setup_status, "details", None)
    raw_incomplete = (
        details.get("incomplete_checks")
        if isinstance(details, dict)
        else []
    )
    connection_todo_kinds = {
        "channel",
        "browser_session",
        "missing_integration",
        "mcp_server",
        "mcp_configuration",
    }
    incomplete: list[dict[str, str]] = []
    for check in raw_incomplete or []:
        if not isinstance(check, dict):
            continue
        kind = str(check.get("kind") or "").strip()
        todo_kind = str(check.get("todo_kind") or "").strip()
        if kind != "integration_provider" and todo_kind not in connection_todo_kinds:
            continue
        incomplete.append({
            key: str(check.get(key) or "").strip()
            for key in (
                "key",
                "kind",
                "todo_kind",
                "provider",
                "setup_kind",
                "label",
            )
            if str(check.get(key) or "").strip()
        })
    incomplete.sort(key=lambda item: json.dumps(item, sort_keys=True))
    fingerprint = (
        hashlib.sha256(
            json.dumps(incomplete, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        if incomplete
        else ""
    )
    prior_fingerprint = str(previous.get("fingerprint") or "")
    incident = int(previous.get("incident") or 0)
    incident_started = bool(fingerprint and fingerprint != prior_fingerprint)
    if incident_started:
        incident += 1
    return ({
        "ready": not incomplete,
        "fingerprint": fingerprint,
        "incident": incident,
        "incomplete": incomplete,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }, incident_started)


# ---------------------------------------------------------------------------
# Experiment guardrail tick (M13) — deterministic, no LLM
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="experiments.guardrail_tick", max_retries=0)
def experiment_guardrail_tick(self):
    """Sweep running experiments against their guardrails (M13).

    Stops an experiment (and removes its overlay from the target) on
    consecutive cohort failures, on reaching ``scope.max_runs``, or when
    ``ends_at`` has passed — then auto-evaluates it. Pure DB arithmetic.
    """
    if not MONITOR_ENABLED:
        return
    try:
        acted = _run_async(_async_experiment_guardrail_tick())
        if acted:
            logger.info("experiment_guardrail_tick: acted on %d experiment(s): %s",
                        len(acted), acted)
    except Exception as exc:
        logger.error("experiment_guardrail_tick failed: %s", exc, exc_info=True)


async def _async_experiment_guardrail_tick() -> list[dict]:
    from packages.core.database import create_worker_session
    from packages.core.experiments import check_experiment_guardrails

    async with create_worker_session()() as db:
        results = await check_experiment_guardrails(db)
        await db.commit()
    return results
