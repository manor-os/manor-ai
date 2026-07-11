"""Briefing service — main entry point.

``generate_briefing(workspace_id)``:
  1. Resolve workspace + execution mode (sandbox → simulated inboxes).
  2. Gather signals from each registered InboxSource.
  3. Pull workspace memory snippets + active goal pace for context.
  4. Single LLM call → validated Briefing.
  5. Post a rich card to workspace_chat with per-item actions.

Designed to be called from the ``run_morning_briefing`` Celery task
(scheduled per workspace) OR ad-hoc via API for "show me a briefing
right now".
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.briefing import inbox as inbox_mod
from packages.core.briefing.prompt import generate_briefing_via_llm
from packages.core.briefing.schema import Briefing
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.workspaces import is_sandbox_workspace
from packages.core.workspace_chat import service as chat_service

logger = logging.getLogger(__name__)


class BriefingError(Exception):
    pass


async def generate_briefing(
    db: AsyncSession,
    workspace_id: str,
    *,
    sources: Optional[list[str]] = None,
    max_per_source: int = 10,
    timezone_name: Optional[str] = None,
) -> dict:
    """Build + post one briefing for a workspace. Caller commits.

    ``sources`` defaults to all registered ``InboxSource`` names. For
    Demo B v0 that's just gmail — Slack / Calendar will register
    themselves into the same registry as their adapters land.
    """
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if workspace is None:
        raise BriefingError(f"workspace {workspace_id} not found")
    if workspace.status != "active":
        return {
            "workspace_id": workspace_id,
            "skipped": True,
            "reason": f"workspace_{workspace.status}",
        }

    execution_mode = "sandbox" if is_sandbox_workspace(workspace) else "live"
    chosen = sources or inbox_mod.supported_sources()

    # Gather signals from each source.
    signals: list[dict] = []
    for src_name in chosen:
        src = inbox_mod.get_source(src_name)
        if src is None:
            logger.debug("briefing: unknown source %s", src_name)
            continue
        try:
            fetched = await src.fetch(
                db, workspace.entity_id,
                execution_mode=execution_mode, max_n=max_per_source,
            )
        except Exception as exc:
            logger.warning("briefing: source %s failed: %s", src_name, exc)
            continue
        signals.extend(fetched)

    # Context: deterministic workspace summary + memory + active goals.
    summary_timezone = timezone_name or _workspace_timezone(workspace)
    workspace_summary = await _gather_workspace_daily_summary(
        db, workspace.entity_id, workspace_id, timezone_name=summary_timezone,
    )
    memory_snippets = await _gather_memory_snippets(
        db, workspace.entity_id, workspace_id,
    )
    goals_snapshot = await _gather_goals_snapshot(
        db, workspace.entity_id, workspace_id,
    )

    briefing_id = "bf_" + generate_ulid()
    briefing = await generate_briefing_via_llm(
        workspace_name=workspace.name,
        briefing_id=briefing_id,
        signals=signals,
        memory_snippets=memory_snippets,
        goals_snapshot=goals_snapshot,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        workspace_summary=workspace_summary,
    )
    briefing = _attach_workspace_summary(briefing, workspace_summary)

    # Post to workspace chat.
    await db.commit()
    await _post_briefing_to_chat(workspace, briefing)

    return {
        "workspace_id": workspace_id,
        "briefing_id": briefing.briefing_id,
        "headline": briefing.headline,
        "item_count": len(briefing.items),
        "items_by_category": _category_counts(briefing),
        "signal_count": len(signals),
        "workspace_summary_date": (workspace_summary or {}).get("window", {}).get("date"),
        "timezone": summary_timezone,
    }


# ── Context gathering ────────────────────────────────────────────────

async def _gather_workspace_daily_summary(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    timezone_name: str = "UTC",
) -> Optional[dict]:
    try:
        from packages.core.services.workspace_daily_summary_service import (
            get_workspace_daily_summary,
        )

        return await get_workspace_daily_summary(
            db,
            entity_id,
            workspace_id,
            timezone_name=timezone_name,
            limit_per_section=6,
        )
    except Exception as exc:
        logger.debug("briefing: workspace daily summary unavailable (%s)", exc)
        return None


def _workspace_timezone(workspace: Workspace) -> str:
    settings = workspace.settings or {}
    return (
        settings.get("timezone")
        or settings.get("tz")
        or settings.get("briefing_timezone")
        or "UTC"
    )


async def _gather_memory_snippets(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict]:
    """Top-K most relevant workspace memory entries. Best-effort."""
    try:
        from packages.core.memory import get_relevant_memory
        return await get_relevant_memory(
            db,
            entity_id=entity_id, workspace_id=workspace_id,
            query="urgent inbox triage decision preferences",
            k=8,
        )
    except Exception as exc:
        logger.debug("briefing: memory snippets unavailable (%s)", exc)
        return []


def _attach_workspace_summary(
    briefing: Briefing,
    workspace_summary: Optional[dict],
) -> Briefing:
    if not workspace_summary:
        return briefing
    metrics = dict(briefing.metrics_snapshot or {})
    metrics.setdefault("workspace_daily_summary", workspace_summary)
    briefing.metrics_snapshot = metrics
    return briefing


async def _gather_goals_snapshot(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[dict]:
    try:
        from packages.core.models.goal import Goal

        rows = list((await db.execute(
            select(Goal).where(
                Goal.entity_id == entity_id,
                Goal.workspace_id == workspace_id,
                Goal.status == "active",
            ).order_by(Goal.priority.desc())
        )).scalars().all())
        return [
            {
                "id": g.id,
                "title": g.title,
                "metric_key": g.metric_key,
                "current_value": float(g.current_value) if g.current_value is not None else None,
                "target_value": float(g.target_value) if g.target_value is not None else None,
                "pace_status": g.pace_status,
            }
            for g in rows
        ]
    except Exception:
        return []


# ── Chat post ────────────────────────────────────────────────────────

async def _post_briefing_to_chat(
    workspace: Workspace, briefing: Briefing,
) -> None:
    """One chat card per briefing. Rich body for human reading + a
    structured ``attachments`` payload the UI parses to render
    per-item action buttons."""
    if not workspace.id:
        return

    body_lines = [f"☀ Morning briefing — {briefing.headline}"]
    if briefing.items:
        body_lines.append("")
    for item in briefing.items:
        marker = _category_marker(item.category)
        body_lines.append(f"{marker} **{item.title}**")
        body_lines.append(f"   _{item.summary}_")
        if item.draft_reply:
            preview = item.draft_reply.replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            body_lines.append(f"   ✏ Draft: {preview}")
        body_lines.append("")
    if briefing.notes:
        body_lines.append(f"📝 {briefing.notes}")
    workspace_summary = _workspace_summary_from_briefing(briefing)
    if workspace_summary:
        body_lines.extend(_format_workspace_summary_lines(workspace_summary))

    attachments = {
        "kind": "briefing",
        "briefing_id": briefing.briefing_id,
        "items": [item.model_dump(mode="json") for item in briefing.items],
        "metrics_snapshot": briefing.metrics_snapshot,
    }
    refs = [
        {"type": "briefing_item", "source": item.source, "id": item.source_ref}
        for item in briefing.items
    ]

    try:
        from packages.core.database import async_session
        async with async_session() as db:
            await chat_service.post_message(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                body="\n".join(body_lines).rstrip(),
                message_kind="briefing",
                author_kind="agent",
                refs=refs,
                attachments=attachments,
            )
            await db.commit()
    except Exception:
        logger.warning("briefing: failed to post chat card", exc_info=True)


def _category_marker(category: str) -> str:
    return {
        "urgent": "🔴",
        "respond": "✉",
        "fyi": "ℹ",
        "auto_handled": "✓",
        "skip": "—",
    }.get(category, "•")


def _workspace_summary_from_briefing(briefing: Briefing) -> Optional[dict]:
    metrics = briefing.metrics_snapshot or {}
    summary = metrics.get("workspace_daily_summary")
    return summary if isinstance(summary, dict) else None


def _format_workspace_summary_lines(summary: dict) -> list[str]:
    outcomes = summary.get("yesterday_outcomes") or {}
    health = summary.get("current_health") or {}
    handoff = summary.get("needs_human_handling") or {}
    focus = summary.get("today_focus") or {}
    actions = summary.get("recommended_action_items") or []

    lines = [
        "",
        "**Workspace summary**",
        (
            f"- Yesterday: {outcomes.get('completed_count', 0)} completed, "
            f"{outcomes.get('failed_count', 0)} failed, "
            f"{outcomes.get('created_count', 0)} created."
        ),
        (
            f"- Health: {health.get('open_count', 0)} open, "
            f"{health.get('overdue_count', 0)} overdue, "
            f"{health.get('stalled_count', 0)} stalled."
        ),
        (
            f"- Human handling: {handoff.get('waiting_on_customer_count', 0)} waiting, "
            f"{handoff.get('blocked_count', 0)} blocked, "
            f"{handoff.get('proposed_count', 0)} proposed."
        ),
        (
            f"- Today: {focus.get('due_today_count', 0)} due, "
            f"{focus.get('priority_pending_count', 0)} priority pending."
        ),
    ]
    if actions:
        lines.append("")
        lines.append("**Recommended actions**")
        lines.extend(f"- {item}" for item in actions[:5])
    return lines


def _category_counts(briefing: Briefing) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in briefing.items:
        out[item.category] = out.get(item.category, 0) + 1
    return out
