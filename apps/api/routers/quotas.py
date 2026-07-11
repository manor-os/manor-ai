"""Entity quota endpoints — usage reports, quota checks, and limit management.

DEPRECATED — these endpoints back the legacy ``EntityQuota`` table
which nothing enforces against. Real enforcement (HTTP 402 / pre-flight
credit gate) lives in ``plan_gate``; per-call observability lives in
``token_usage_logs``. The only live consumer is ``Subscription.tsx``
reading the plan label, which now derives from ``Entity.plan_id``.

Migration plan: repoint that one frontend read to a billing-summary
endpoint, then drop this router and the ``entity_quotas`` table.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.quota_service import (
    check_quota,
    get_usage_report,
    update_quota,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/quotas", tags=["quotas"])


# ── Schemas ──

class ResourceUsage(BaseModel):
    used: int = 0
    limit: int = 0
    pct: float | None = None


class ResourceLimit(BaseModel):
    limit: int = 0


class UsageReportResponse(BaseModel):
    plan: str
    tokens: ResourceUsage
    api_calls: ResourceUsage
    storage: ResourceUsage
    users: ResourceLimit
    agents: ResourceLimit
    documents: ResourceLimit


class QuotaCheckResponse(BaseModel):
    allowed: bool
    reason: str = ""


class QuotaUpdateRequest(BaseModel):
    plan_name: Optional[str] = None
    max_users: Optional[int] = None
    max_agents: Optional[int] = None
    max_documents: Optional[int] = None
    max_storage_bytes: Optional[int] = None
    max_tokens_monthly: Optional[int] = None
    max_api_calls_daily: Optional[int] = None


class QuotaUpdateResponse(BaseModel):
    plan_name: str
    max_users: int
    max_agents: int
    max_documents: int
    max_storage_bytes: int
    max_tokens_monthly: int
    max_api_calls_daily: int


# ── Endpoints ──

@router.get("", response_model=UsageReportResponse)
async def get_quota_report(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current usage vs limits for the authenticated entity."""
    report = await get_usage_report(db, user.entity_id)
    return UsageReportResponse(**report)


@router.get("/check/{resource}", response_model=QuotaCheckResponse)
async def check_resource_quota(
    resource: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if a specific resource is within quota.

    resource: users | agents | documents | storage | tokens | api_calls
    """
    valid_resources = {"users", "agents", "documents", "storage", "tokens", "api_calls"}
    if resource not in valid_resources:
        raise HTTPException(status_code=400, detail=f"Invalid resource. Must be one of: {', '.join(sorted(valid_resources))}")

    allowed, reason = await check_quota(db, user.entity_id, resource)
    return QuotaCheckResponse(allowed=allowed, reason=reason)


@router.put("", response_model=QuotaUpdateResponse)
async def update_entity_quota(
    req: QuotaUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update quota limits (owner only)."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only owners and admins can update quotas")

    quota = await update_quota(db, user.entity_id, **req.model_dump(exclude_none=True))
    return QuotaUpdateResponse(
        plan_name=quota.plan_name,
        max_users=quota.max_users,
        max_agents=quota.max_agents,
        max_documents=quota.max_documents,
        max_storage_bytes=quota.max_storage_bytes,
        max_tokens_monthly=quota.max_tokens_monthly,
        max_api_calls_daily=quota.max_api_calls_daily,
    )
