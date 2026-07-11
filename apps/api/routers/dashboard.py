"""Dashboard analytics endpoints — stats, trends, goals, activity feed."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.analytics_service import (
    get_active_goals,
    get_dashboard_stats,
    get_recent_activity,
    get_task_trends,
    get_usage_trends,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


# ── Schemas ──

class TaskStats(BaseModel):
    total: int = 0
    by_status: dict[str, int] = {}
    overdue: int = 0


class DocumentStats(BaseModel):
    total: int = 0
    indexed: int = 0


class AgentStats(BaseModel):
    total: int = 0
    subscribed: int = 0


class ConversationStats(BaseModel):
    total: int = 0
    today: int = 0


class ClientStats(BaseModel):
    total: int = 0
    active: int = 0


class StaffStats(BaseModel):
    total: int = 0


class UsageStats(BaseModel):
    total_tokens: int = 0
    total_cost: float = 0.0
    today_tokens: int = 0


class DashboardStatsResponse(BaseModel):
    tasks: TaskStats = TaskStats()
    documents: DocumentStats = DocumentStats()
    agents: AgentStats = AgentStats()
    conversations: ConversationStats = ConversationStats()
    clients: ClientStats = ClientStats()
    staff: StaffStats = StaffStats()
    usage: UsageStats = UsageStats()


class TaskTrendItem(BaseModel):
    date: str
    created: int = 0
    completed: int = 0


class UsageTrendItem(BaseModel):
    date: str
    tokens: int = 0
    cost: float = 0.0


class ActiveGoalItem(BaseModel):
    id: str
    task_id: str | None = None
    status: str
    execution_mode: str | None = None
    progress_pct: int = 0
    step_count: int = 0
    step_done: int = 0
    updated_at: str | None = None


class ActivityItem(BaseModel):
    type: str
    id: str
    name: str
    action: str
    description: str | None = None
    created_by: str | None = None
    timestamp: str | None = None
    task_id: str | None = None


# ── Endpoints ──

@router.get("/stats", response_model=DashboardStatsResponse)
async def dashboard_stats(
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await get_dashboard_stats(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        timezone_name=user.timezone,
    )
    return DashboardStatsResponse(**data)


@router.get("/task-trends", response_model=list[TaskTrendItem])
async def task_trends(
    days: int = Query(30, ge=1, le=365),
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_task_trends(
        db,
        user.entity_id,
        days=days,
        workspace_id=workspace_id,
        timezone_name=user.timezone,
    )


@router.get("/usage-trends", response_model=list[UsageTrendItem])
async def usage_trends(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_usage_trends(db, user.entity_id, days=days, timezone_name=user.timezone)


@router.get("/active-goals", response_model=list[ActiveGoalItem])
async def active_goals(
    limit: int = Query(5, ge=1, le=50),
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_active_goals(db, user.entity_id, limit=limit, workspace_id=workspace_id)


@router.get("/recent-activity")
async def recent_activity(
    workspace_id: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await get_recent_activity(db, user.entity_id, limit=limit, workspace_id=workspace_id)
    except Exception:
        return []
