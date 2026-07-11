"""Staff management endpoints — departments, schedules, and availability."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.staff_service import (
    list_departments,
    create_department,
    update_department,
    delete_department,
    get_staff_schedule,
    update_staff_schedule,
    add_schedule_adjustment,
    get_available_staff,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/staff", tags=["staff-management"])


# ── Schemas: Departments ──

class DepartmentResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    parent_id: str | None = None
    description: str | None = None
    sort_order: int = 0
    status: str = "active"


class DepartmentCreateRequest(BaseModel):
    name: str
    parent_id: str | None = None
    description: str | None = None
    sort_order: int = 0


class DepartmentUpdateRequest(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    description: str | None = None
    sort_order: int | None = None
    status: str | None = None


# ── Schemas: Schedules ──

class ScheduleEntry(BaseModel):
    day_of_week: int
    shift_start: str
    shift_end: str


class ScheduleResponse(BaseModel):
    id: str
    day_of_week: int
    shift_start: str | None = None
    shift_end: str | None = None


class ScheduleSetRequest(BaseModel):
    schedules: list[ScheduleEntry]


class ScheduleExceptionRequest(BaseModel):
    date: datetime
    adjustment_type: str = "day_off"
    shift_start: str | None = None
    shift_end: str | None = None
    reason: str | None = None


class ScheduleExceptionResponse(BaseModel):
    id: str
    staff_id: str
    date: str
    adjustment_type: str
    shift_start: str | None = None
    shift_end: str | None = None
    reason: str | None = None


# ── Schemas: Availability ──

class AvailableStaffResponse(BaseModel):
    id: str
    name: str
    email: str | None = None
    title: str | None = None
    department_id: str | None = None
    skills: list[str] = []


# ── Helpers ──

def _dept_response(d) -> DepartmentResponse:
    return DepartmentResponse(
        id=d.id,
        entity_id=d.entity_id,
        name=d.name,
        parent_id=d.parent_id,
        description=d.description,
        sort_order=d.sort_order,
        status=d.status,
    )


def _available_staff_response(s) -> AvailableStaffResponse:
    return AvailableStaffResponse(
        id=s.id,
        name=s.name,
        email=s.email,
        title=s.title,
        department_id=s.department_id,
        skills=s.skills or [],
    )


# ── Department Routes ──

@router.get("/departments", response_model=list[DepartmentResponse])
async def list_entity_departments(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all departments for the current entity."""
    departments = await list_departments(db, user.entity_id)
    return [_dept_response(d) for d in departments]


@router.post("/departments", response_model=DepartmentResponse, status_code=201)
async def create_new_department(
    req: DepartmentCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new department."""
    dept = await create_department(
        db, user.entity_id,
        name=req.name,
        parent_id=req.parent_id,
        description=req.description,
        sort_order=req.sort_order,
    )
    return _dept_response(dept)


@router.put("/departments/{dept_id}", response_model=DepartmentResponse)
async def update_one_department(
    dept_id: str,
    req: DepartmentUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a department."""
    dept = await update_department(
        db, dept_id, user.entity_id,
        **req.model_dump(exclude_none=True),
    )
    if not dept:
        raise HTTPException(404, "Department not found")
    return _dept_response(dept)


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_one_department(
    dept_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a department."""
    ok = await delete_department(db, dept_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Department not found")


# ── Schedule Routes ──

@router.get("/{staff_id}/schedule", response_model=list[ScheduleResponse])
async def get_weekly_schedule(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the weekly recurring schedule for a staff member."""
    entries = await get_staff_schedule(db, staff_id)
    return [ScheduleResponse(**e) for e in entries]


@router.put("/{staff_id}/schedule", response_model=list[ScheduleResponse])
async def set_weekly_schedule(
    staff_id: str,
    req: ScheduleSetRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace the full weekly schedule for a staff member (bulk upsert)."""
    schedules = [s.model_dump() for s in req.schedules]
    created = await update_staff_schedule(db, staff_id, schedules)
    return [
        ScheduleResponse(
            id=s.id,
            day_of_week=s.day_of_week,
            shift_start=s.shift_start.isoformat() if s.shift_start else None,
            shift_end=s.shift_end.isoformat() if s.shift_end else None,
        )
        for s in created
    ]


@router.post("/{staff_id}/schedule/exceptions", response_model=ScheduleExceptionResponse, status_code=201)
async def add_schedule_exception_endpoint(
    staff_id: str,
    req: ScheduleExceptionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a one-off schedule exception (PTO, shift swap, etc.)."""
    from datetime import time as _time

    shift_start = None
    shift_end = None
    if req.shift_start:
        parts = req.shift_start.split(":")
        shift_start = _time(int(parts[0]), int(parts[1]))
    if req.shift_end:
        parts = req.shift_end.split(":")
        shift_end = _time(int(parts[0]), int(parts[1]))

    adj = await add_schedule_adjustment(
        db, staff_id,
        date=req.date,
        adjustment_type=req.adjustment_type,
        shift_start=shift_start,
        shift_end=shift_end,
        reason=req.reason,
    )
    return ScheduleExceptionResponse(
        id=adj.id,
        staff_id=adj.staff_id,
        date=adj.date.isoformat(),
        adjustment_type=adj.adjustment_type,
        shift_start=adj.shift_start.isoformat() if adj.shift_start else None,
        shift_end=adj.shift_end.isoformat() if adj.shift_end else None,
        reason=adj.reason,
    )


# ── Availability Routes ──

@router.get("/availability", response_model=list[AvailableStaffResponse])
async def get_staff_availability_endpoint(
    date: str | None = Query(None, description="ISO date (YYYY-MM-DD). Defaults to now."),
    skills: str | None = Query(None, description="Comma-separated skill filter"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get staff members available on a given date/time."""
    at_dt = None
    if date:
        at_dt = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    skill_list = [s.strip() for s in skills.split(",")] if skills else None
    available = await get_available_staff(
        db, user.entity_id, at_datetime=at_dt, skills=skill_list,
    )
    return [_available_staff_response(s) for s in available]
