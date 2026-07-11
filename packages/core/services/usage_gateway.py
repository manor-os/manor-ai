"""Usage gateway for entity-scoped company usage views."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.billing import CreditUsageLog
from packages.core.models.task import Task
from packages.core.models.usage import TokenUsageLog
from packages.core.models.user import User
from packages.core.models.user_session import UserSessionLog
from packages.core.models.workspace import Workspace, WorkspaceActivity
from packages.core.services.team_gateway import (
    TeamGatewayMember,
    list_team_gateway_members,
    require_team_usage_access,
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _active_session_cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=90)


def _member_identity(member: TeamGatewayMember) -> dict[str, Any]:
    return {
        "staff_id": member.staff_id,
        "user_id": member.user_id,
        "membership_status": member.membership_status,
        "kind": member.kind,
        "status": member.status,
        "name": member.name,
        "email": member.email,
        "avatar_url": member.avatar_url,
        "title": member.title,
        "role_id": member.role_id,
        "role_name": member.role_name,
    }


async def get_team_usage_gateway(
    db: AsyncSession,
    *,
    user: User,
    days: int = 30,
    activity_limit: int = 80,
) -> dict[str, Any]:
    """Return owner/admin team usage for the current company context.

    The gateway only reads rows scoped to ``user.entity_id``.  Employee usage
    in a personal entity never appears in this report.
    """

    await require_team_usage_access(db, user)

    days = max(1, min(int(days or 30), 365))
    activity_limit = max(1, min(int(activity_limit or 80), 200))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    entity_id = user.entity_id

    members = await list_team_gateway_members(db, entity_id=entity_id)
    user_ids = [m.user_id for m in members if m.user_id]

    credit_by_user: dict[str, dict[str, Any]] = {}
    token_by_user: dict[str, dict[str, Any]] = {}
    session_by_user: dict[str, dict[str, Any]] = {}
    task_count_by_staff: dict[str, int] = {}
    activities_by_user: dict[str, list[dict[str, Any]]] = {}
    recent_activity: list[dict[str, Any]] = []

    if user_ids:
        credit_rows = (
            await db.execute(
                select(
                    CreditUsageLog.user_id,
                    func.count(CreditUsageLog.id).label("requests"),
                    func.coalesce(func.sum(CreditUsageLog.total_credit), 0).label("credits"),
                    func.coalesce(func.sum(CreditUsageLog.total_tokens), 0).label("tokens"),
                    func.coalesce(func.sum(CreditUsageLog.cost_usd), 0).label("cost_usd"),
                    func.max(CreditUsageLog.created_at).label("last_used_at"),
                )
                .where(
                    CreditUsageLog.entity_id == entity_id,
                    CreditUsageLog.user_id.in_(user_ids),
                    CreditUsageLog.created_at >= cutoff,
                )
                .group_by(CreditUsageLog.user_id)
            )
        ).all()
        credit_by_user = {
            str(r.user_id): {
                "request_count": int(r.requests or 0),
                "credits_used": int(r.credits or 0),
                "tokens_used": int(r.tokens or 0),
                "cost_usd": round(float(r.cost_usd or 0), 6),
                "last_used_at": _iso(r.last_used_at),
            }
            for r in credit_rows
            if r.user_id
        }

        token_rows = (
            await db.execute(
                select(
                    TokenUsageLog.user_id,
                    func.count(TokenUsageLog.id).label("llm_calls"),
                    func.coalesce(func.sum(TokenUsageLog.prompt_tokens), 0).label("prompt_tokens"),
                    func.coalesce(func.sum(TokenUsageLog.completion_tokens), 0).label("completion_tokens"),
                    func.coalesce(func.sum(TokenUsageLog.total_tokens), 0).label("total_tokens"),
                    func.max(TokenUsageLog.created_at).label("last_llm_at"),
                )
                .where(
                    TokenUsageLog.entity_id == entity_id,
                    TokenUsageLog.user_id.in_(user_ids),
                    TokenUsageLog.created_at >= cutoff,
                )
                .group_by(TokenUsageLog.user_id)
            )
        ).all()
        token_by_user = {
            str(r.user_id): {
                "llm_calls": int(r.llm_calls or 0),
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "total_tokens": int(r.total_tokens or 0),
                "last_llm_at": _iso(r.last_llm_at),
            }
            for r in token_rows
            if r.user_id
        }

        active_cutoff = _active_session_cutoff(now)
        active_case = case(
            (
                and_(
                    UserSessionLog.status == "active",
                    UserSessionLog.last_seen_at >= active_cutoff,
                ),
                1,
            ),
            else_=0,
        )
        session_rows = (
            await db.execute(
                select(
                    UserSessionLog.user_id,
                    func.count(UserSessionLog.id).label("session_count"),
                    func.coalesce(func.sum(UserSessionLog.duration_seconds), 0).label("active_seconds"),
                    func.coalesce(func.avg(UserSessionLog.duration_seconds), 0).label("avg_session_seconds"),
                    func.coalesce(func.sum(active_case), 0).label("active_session_count"),
                    func.max(UserSessionLog.last_seen_at).label("last_seen_at"),
                )
                .where(
                    UserSessionLog.entity_id == entity_id,
                    UserSessionLog.user_id.in_(user_ids),
                    UserSessionLog.last_seen_at >= cutoff,
                )
                .group_by(UserSessionLog.user_id)
            )
        ).all()
        session_by_user = {
            str(r.user_id): {
                "session_count": int(r.session_count or 0),
                "active_seconds": int(r.active_seconds or 0),
                "avg_session_seconds": int(float(r.avg_session_seconds or 0)),
                "active_session_count": int(r.active_session_count or 0),
                "active_now": int(r.active_session_count or 0) > 0,
                "last_seen_at": _iso(r.last_seen_at),
            }
            for r in session_rows
            if r.user_id
        }

        activity_rows = (
            await db.execute(
                select(WorkspaceActivity, Workspace.name)
                .outerjoin(
                    Workspace,
                    (Workspace.id == WorkspaceActivity.workspace_id)
                    & (Workspace.entity_id == WorkspaceActivity.entity_id),
                )
                .where(
                    WorkspaceActivity.entity_id == entity_id,
                    WorkspaceActivity.user_id.in_(user_ids),
                    WorkspaceActivity.created_at >= cutoff,
                )
                .order_by(desc(WorkspaceActivity.created_at))
                .limit(activity_limit)
            )
        ).all()
        for activity, workspace_name in activity_rows:
            item = {
                "id": activity.id,
                "user_id": activity.user_id,
                "workspace_id": activity.workspace_id,
                "workspace_name": workspace_name,
                "event_type": activity.event_type,
                "summary": activity.summary,
                "details": activity.details or {},
                "created_at": _iso(activity.created_at),
            }
            recent_activity.append(item)
            if activity.user_id:
                activities_by_user.setdefault(activity.user_id, []).append(item)

    task_identity_to_staff: dict[str, str] = {}
    for member in members:
        if member.staff_id:
            task_identity_to_staff[member.staff_id] = member.staff_id
        if member.user_id:
            task_identity_to_staff[member.user_id] = member.staff_id
    task_identities = list(task_identity_to_staff.keys())
    if task_identities:
        task_rows = (
            await db.execute(
                select(Task.id, Task.assignee_id, Task.creator_id)
                .where(
                    Task.entity_id == entity_id,
                    or_(
                        Task.created_at >= cutoff,
                        Task.started_at >= cutoff,
                        Task.completed_at >= cutoff,
                        Task.updated_at >= cutoff,
                    ),
                    Task.status != "inactive",
                    Task.details["scheduled_job_id"].astext.is_(None),
                    or_(
                        Task.assignee_id.in_(task_identities),
                        Task.creator_id.in_(task_identities),
                    ),
                )
            )
        ).all()
        task_ids_by_staff: dict[str, set[str]] = {}
        for task_id, assignee_id, creator_id in task_rows:
            matched_staff_ids = {
                staff_id
                for identity in (assignee_id, creator_id)
                if identity
                for staff_id in [task_identity_to_staff.get(identity)]
                if staff_id
            }
            for staff_id in matched_staff_ids:
                task_ids_by_staff.setdefault(staff_id, set()).add(str(task_id))
        task_count_by_staff = {
            staff_id: len(task_ids)
            for staff_id, task_ids in task_ids_by_staff.items()
        }

    totals = {
        "credits_used": 0,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "request_count": 0,
        "llm_calls": 0,
        "task_count": 0,
        "active_seconds": 0,
        "active_users": 0,
        "active_now": 0,
    }
    member_items: list[dict[str, Any]] = []
    for member in members:
        uid = member.user_id or ""
        credit = credit_by_user.get(uid, {})
        token = token_by_user.get(uid, {})
        session = session_by_user.get(uid, {})
        credits_used = int(credit.get("credits_used") or 0)
        tokens_used = int(credit.get("tokens_used") or token.get("total_tokens") or 0)
        cost_usd = float(credit.get("cost_usd") or 0)
        request_count = int(credit.get("request_count") or 0)
        llm_calls = int(token.get("llm_calls") or 0)
        task_count = int(task_count_by_staff.get(member.staff_id, 0))
        active_seconds = int(session.get("active_seconds") or 0)
        active_now = bool(session.get("active_now"))

        totals["credits_used"] += credits_used
        totals["tokens_used"] += tokens_used
        totals["cost_usd"] += cost_usd
        totals["request_count"] += request_count
        totals["llm_calls"] += llm_calls
        totals["task_count"] += task_count
        totals["active_seconds"] += active_seconds
        if int(session.get("session_count") or 0) > 0:
            totals["active_users"] += 1
        if active_now:
            totals["active_now"] += 1

        member_items.append({
            **_member_identity(member),
            "usage": {
                "credits_used": credits_used,
                "tokens_used": tokens_used,
                "cost_usd": round(cost_usd, 6),
                "request_count": request_count,
                "llm_calls": llm_calls,
                "task_count": task_count,
                "prompt_tokens": int(token.get("prompt_tokens") or 0),
                "completion_tokens": int(token.get("completion_tokens") or 0),
                "last_used_at": credit.get("last_used_at") or token.get("last_llm_at"),
            },
            "activity": {
                "session_count": int(session.get("session_count") or 0),
                "active_seconds": active_seconds,
                "avg_session_seconds": int(session.get("avg_session_seconds") or 0),
                "active_session_count": int(session.get("active_session_count") or 0),
                "active_now": active_now,
                "last_seen_at": session.get("last_seen_at"),
                "recent": activities_by_user.get(uid, [])[:5],
            },
        })

    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    return {
        "entity_id": entity_id,
        "scope": "company",
        "days": days,
        "generated_at": now.isoformat(),
        "totals": totals,
        "members": member_items,
        "recent_activity": recent_activity,
    }
