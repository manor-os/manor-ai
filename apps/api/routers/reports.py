"""Scheduled reports — generate HTML reports, email delivery."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.report_service import (
    generate_activity_report,
    generate_task_report,
    generate_usage_report,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


# ── Schemas ──

class ReportResponse(BaseModel):
    title: str
    html: str
    text_summary: str
    generated_at: str
    data: dict = {}


class EmailReportRequest(BaseModel):
    report_type: str  # "tasks" | "usage" | "activity"
    recipients: list[str]
    days: int = 30


class EmailReportResponse(BaseModel):
    sent: int
    failed: int
    report_title: str


# ── Report generators (JSON) ──

@router.get("/tasks", response_model=ReportResponse)
async def task_report(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await generate_task_report(db, user.entity_id, days=days)
    return ReportResponse(**result)


@router.get("/usage", response_model=ReportResponse)
async def usage_report(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await generate_usage_report(db, user.entity_id, days=days)
    return ReportResponse(**result)


@router.get("/activity", response_model=ReportResponse)
async def activity_report(
    days: int = Query(7, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await generate_activity_report(db, user.entity_id, days=days)
    return ReportResponse(**result)


# ── Raw HTML endpoints ──

@router.get("/tasks/html", response_class=HTMLResponse)
async def task_report_html(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await generate_task_report(db, user.entity_id, days=days)
    return HTMLResponse(content=result["html"])


@router.get("/usage/html", response_class=HTMLResponse)
async def usage_report_html(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await generate_usage_report(db, user.entity_id, days=days)
    return HTMLResponse(content=result["html"])


# ── Email delivery ──

@router.post("/email", response_model=EmailReportResponse)
async def email_report(
    body: EmailReportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.email_service import send_email

    # Generate the requested report
    generators = {
        "tasks": generate_task_report,
        "usage": generate_usage_report,
        "activity": generate_activity_report,
    }
    gen = generators.get(body.report_type)
    if gen is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown report_type: {body.report_type}")

    result = await gen(db, user.entity_id, days=body.days)

    sent = 0
    failed = 0
    for recipient in body.recipients:
        ok = await send_email(
            to=recipient,
            subject=result["title"],
            html_body=result["html"],
            text_body=result.get("text_summary"),
        )
        if ok:
            sent += 1
        else:
            failed += 1

    return EmailReportResponse(
        sent=sent,
        failed=failed,
        report_title=result["title"],
    )
