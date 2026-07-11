"""Token usage tracking endpoints — log, list, and summarise LLM consumption."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.usage_service import (
    log_token_usage,
    list_usage,
    get_usage_summary,
)
from packages.core.services.usage_gateway import get_team_usage_gateway
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])

UsageScope = Literal["company", "member"]


# ── Schemas ──

class UsageLogResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    model: str | None = None
    provider: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    source: str | None = None
    created_at: str | None = None


class UsageListResponse(BaseModel):
    items: list[UsageLogResponse]
    total: int


class UsageLogCreateRequest(BaseModel):
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    workspace_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    provider: str | None = None
    cost_usd: float | None = None
    source: str | None = None


class ModelSummary(BaseModel):
    model: str | None = None
    tokens: int = 0
    cost: float = 0.0


class SourceSummary(BaseModel):
    source: str | None = None
    tokens: int = 0
    cost: float = 0.0


class UsageSummaryResponse(BaseModel):
    total_tokens: int = 0
    total_cost: float = 0.0
    by_model: list[ModelSummary] = []
    by_source: list[SourceSummary] = []


class TeamUsageTotals(BaseModel):
    credits_used: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    request_count: int = 0
    llm_calls: int = 0
    task_count: int = 0
    active_seconds: int = 0
    active_users: int = 0
    active_now: int = 0


class TeamActivityItem(BaseModel):
    id: str
    user_id: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    event_type: str
    summary: str
    details: dict[str, Any] = {}
    created_at: str | None = None


class TeamMemberUsage(BaseModel):
    credits_used: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    request_count: int = 0
    llm_calls: int = 0
    task_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_used_at: str | None = None


class TeamMemberActivity(BaseModel):
    session_count: int = 0
    active_seconds: int = 0
    avg_session_seconds: int = 0
    active_session_count: int = 0
    active_now: bool = False
    last_seen_at: str | None = None
    recent: list[TeamActivityItem] = []


class TeamUsageMember(BaseModel):
    staff_id: str
    user_id: str | None = None
    membership_status: str | None = None
    kind: str
    status: str
    name: str
    email: str | None = None
    avatar_url: str | None = None
    title: str | None = None
    role_id: str | None = None
    role_name: str | None = None
    usage: TeamMemberUsage
    activity: TeamMemberActivity


class TeamUsageResponse(BaseModel):
    entity_id: str
    scope: str = "company"
    days: int
    generated_at: str
    totals: TeamUsageTotals
    members: list[TeamUsageMember] = []
    recent_activity: list[TeamActivityItem] = []


def _to_response(log) -> UsageLogResponse:
    return UsageLogResponse(
        id=log.id,
        entity_id=log.entity_id,
        workspace_id=getattr(log, "workspace_id", None),
        user_id=log.user_id,
        conversation_id=log.conversation_id,
        model=log.model,
        provider=getattr(log, "provider", None),
        prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens,
        total_tokens=log.total_tokens,
        cost_usd=float(log.cost_usd) if log.cost_usd is not None else None,
        source=log.source,
        created_at=log.created_at.isoformat() if log.created_at else None,
    )


def _usage_scope_filters(usage_model, user: User, scope: UsageScope):
    filters = [
        usage_model.entity_id == user.entity_id,
    ]
    if scope == "member":
        filters.append(usage_model.user_id == user.id)
    return filters


# ── Endpoints ──

@router.get("", response_model=UsageListResponse)
async def list_usage_logs(
    model: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await list_usage(
        db, user.entity_id, model=model, source=source,
        limit=limit, offset=offset,
    )
    return UsageListResponse(
        items=[_to_response(i) for i in items],
        total=total,
    )


@router.post("", response_model=UsageLogResponse, status_code=201)
async def create_usage_log(
    req: UsageLogCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Provider auto-extract from model id when caller didn't supply one.
    from packages.core.services.model_resolver import llm_provider_from_model
    provider = req.provider or llm_provider_from_model(req.model)

    entry = await log_token_usage(
        db, user.entity_id,
        model=req.model,
        prompt_tokens=req.prompt_tokens,
        completion_tokens=req.completion_tokens,
        total_tokens=req.total_tokens,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        conversation_id=req.conversation_id,
        provider=provider,
        cost_usd=req.cost_usd,
        source=req.source,
    )

    # Record via unified billing (credits = provider_cost × margin × CREDITS_PER_USD)
    from packages.core.services.billing_service import record_token_usage
    await record_token_usage(
        db, user.entity_id,
        input_tokens=req.prompt_tokens or 0,
        output_tokens=req.completion_tokens or 0,
        model=req.model,
        provider=provider,
        pricing_source="openrouter" if provider == "openrouter" else "official",
        workspace_id=req.workspace_id,
        user_id=req.user_id,
        conversation_id=req.conversation_id,
        cost_usd=req.cost_usd,
        business_type=req.source,
    )

    return _to_response(entry)


@router.get("/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    summary = await get_usage_summary(
        db,
        user.entity_id,
        days=days,
        timezone_name=user.timezone,
    )
    return UsageSummaryResponse(**summary)


@router.get("/daily")
async def daily_usage(
    days: int = Query(30, ge=1, le=90),
    scope: UsageScope = Query("company"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get daily aggregated token usage for the chart."""
    from sqlalchemy import func, cast, Date
    from packages.core.services.timezone_utils import user_range_start_utc, user_timezone_name

    start = user_range_start_utc(user.timezone, days)
    from packages.core.models.usage import TokenUsageLog as UsageLog
    usage_day = cast(func.timezone(user_timezone_name(user.timezone), UsageLog.created_at), Date)
    filters = _usage_scope_filters(UsageLog, user, scope)

    rows = (await db.execute(
        select(
            usage_day.label("date"),
            func.sum(UsageLog.prompt_tokens).label("input"),
            func.sum(UsageLog.completion_tokens).label("output"),
            func.sum(UsageLog.total_tokens).label("total"),
        )
        .where(*filters, UsageLog.created_at >= start)
        .group_by(usage_day)
        .order_by(usage_day)
    )).all()

    return [
        {"date": str(r.date), "input": r.input or 0, "output": r.output or 0, "total": r.total or 0}
        for r in rows
    ]


@router.get("/by-source")
async def usage_by_source(
    days: int = Query(30, ge=1, le=365),
    scope: UsageScope = Query("company"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get token usage aggregated by source (agent name, chat, etc.)."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func

    start = datetime.now(timezone.utc) - timedelta(days=days)
    from packages.core.models.usage import TokenUsageLog as UsageLog
    filters = _usage_scope_filters(UsageLog, user, scope)

    rows = (await db.execute(
        select(
            UsageLog.source,
            func.count().label("request_count"),
            func.sum(UsageLog.prompt_tokens).label("input_tokens"),
            func.sum(UsageLog.completion_tokens).label("output_tokens"),
            func.sum(UsageLog.total_tokens).label("total_tokens"),
            func.sum(UsageLog.cost_usd).label("credit_used"),
            func.max(UsageLog.created_at).label("last_used"),
        )
        .where(*filters, UsageLog.created_at >= start)
        .group_by(UsageLog.source)
        .order_by(func.sum(UsageLog.total_tokens).desc())
    )).all()

    return [
        {
            "source": r.source or "Unknown",
            "request_count": r.request_count or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "credit_used": float(r.credit_used or 0),
            "last_used": r.last_used.isoformat() if r.last_used else None,
        }
        for r in rows
    ]


@router.get("/team", response_model=TeamUsageResponse)
async def team_usage(
    days: int = Query(30, ge=1, le=365),
    activity_limit: int = Query(80, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Company-context team usage/activity, gated by team + usage gateways."""
    return await get_team_usage_gateway(
        db,
        user=user,
        days=days,
        activity_limit=activity_limit,
    )
