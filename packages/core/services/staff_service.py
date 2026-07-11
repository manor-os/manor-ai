"""
Staff management service — CRUD for staff, departments, roles, schedules.

Ported from Java BusinessClientStaffServiceImpl with additions for
departments, roles, schedules, and availability queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.staff import (
    Department,
    Staff,
    StaffRole,
    StaffSchedule,
    StaffScheduleAdjustment,
)


# ── Staff CRUD ──────────────────────────────────────────────────────────

async def list_staff(
    db: AsyncSession,
    entity_id: str,
    *,
    department_id: str | None = None,
    status: str | None = None,
) -> list[Staff]:
    """List staff for an entity with optional filters."""
    q = select(Staff).where(
        Staff.entity_id == entity_id,
        Staff.deleted_at.is_(None),
    )
    if department_id:
        q = q.where(Staff.department_id == department_id)
    if status:
        q = q.where(Staff.status == status)
    q = q.order_by(Staff.created_at.asc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_staff(db: AsyncSession, staff_id: str, entity_id: str) -> Staff | None:
    result = await db.execute(
        select(Staff).where(
            Staff.id == staff_id,
            Staff.entity_id == entity_id,
            Staff.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_staff(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    department_id: str | None = None,
    role_id: str | None = None,
    avatar_url: str | None = None,
    skills: list[str] | None = None,
) -> Staff:
    """Create a new staff member."""
    staff = Staff(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        email=email,
        phone=phone,
        title=title,
        department_id=department_id,
        role_id=role_id,
        avatar_url=avatar_url,
        skills=skills,
        status="active",
    )
    db.add(staff)
    await db.flush()
    return staff


async def update_staff(
    db: AsyncSession, staff_id: str, entity_id: str, **fields
) -> Staff | None:
    """Update mutable fields on a staff record."""
    staff = await get_staff(db, staff_id, entity_id)
    if not staff:
        return None
    allowed = {
        "name", "email", "phone", "title", "department_id",
        "role_id", "avatar_url", "skills", "status", "user_id",
    }
    for k, v in fields.items():
        if k in allowed and v is not None:
            setattr(staff, k, v)
    await db.flush()
    return staff


async def deactivate_staff(db: AsyncSession, staff_id: str, entity_id: str) -> bool:
    """Soft-deactivate a staff member (sets status to inactive)."""
    staff = await get_staff(db, staff_id, entity_id)
    if not staff:
        return False
    staff.status = "inactive"
    await db.flush()
    return True


async def invite_staff(
    db: AsyncSession,
    entity_id: str,
    *,
    email: str,
    name: str,
    role_id: str | None = None,
) -> Staff:
    """Create a staff record in 'invited' status (email send is caller's responsibility)."""
    staff = Staff(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        email=email,
        role_id=role_id,
        status="invited",
    )
    db.add(staff)
    await db.flush()
    return staff


# ── Departments ─────────────────────────────────────────────────────────

async def list_departments(db: AsyncSession, entity_id: str) -> list[Department]:
    result = await db.execute(
        select(Department).where(
            Department.entity_id == entity_id,
            Department.deleted_at.is_(None),
        ).order_by(Department.sort_order.asc(), Department.name.asc())
    )
    return list(result.scalars().all())


async def create_department(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    parent_id: str | None = None,
    description: str | None = None,
    sort_order: int = 0,
) -> Department:
    dept = Department(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        parent_id=parent_id,
        description=description,
        sort_order=sort_order,
    )
    db.add(dept)
    await db.flush()
    return dept


async def update_department(
    db: AsyncSession, dept_id: str, entity_id: str, **fields
) -> Department | None:
    result = await db.execute(
        select(Department).where(
            Department.id == dept_id,
            Department.entity_id == entity_id,
            Department.deleted_at.is_(None),
        )
    )
    dept = result.scalar_one_or_none()
    if not dept:
        return None
    for k, v in fields.items():
        if hasattr(dept, k) and v is not None:
            setattr(dept, k, v)
    await db.flush()
    return dept


async def delete_department(db: AsyncSession, dept_id: str, entity_id: str) -> bool:
    """Soft-delete a department."""
    result = await db.execute(
        select(Department).where(
            Department.id == dept_id,
            Department.entity_id == entity_id,
            Department.deleted_at.is_(None),
        )
    )
    dept = result.scalar_one_or_none()
    if not dept:
        return False
    dept.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    return True


# ── Roles ───────────────────────────────────────────────────────────────

async def list_roles(db: AsyncSession, entity_id: str) -> list[StaffRole]:
    result = await db.execute(
        select(StaffRole).where(StaffRole.entity_id == entity_id)
        .order_by(StaffRole.name.asc())
    )
    return list(result.scalars().all())


async def create_role(
    db: AsyncSession,
    entity_id: str,
    *,
    name: str,
    permissions: list[str],
    is_default: bool = False,
) -> StaffRole:
    role = StaffRole(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        permissions=permissions,
        is_default=is_default,
    )
    db.add(role)
    await db.flush()
    return role


async def check_permission(
    db: AsyncSession, staff_id: str, entity_id: str, permission: str
) -> bool:
    """Return True if the staff member's role includes *permission*."""
    staff = await get_staff(db, staff_id, entity_id)
    if not staff or not staff.role_id:
        return False
    result = await db.execute(
        select(StaffRole).where(StaffRole.id == staff.role_id)
    )
    role = result.scalar_one_or_none()
    if not role:
        return False
    return permission in (role.permissions or [])


# ── Schedules ───────────────────────────────────────────────────────────

async def get_staff_schedule(db: AsyncSession, staff_id: str) -> list[dict]:
    """Return the weekly recurring schedule for a staff member."""
    result = await db.execute(
        select(StaffSchedule).where(StaffSchedule.staff_id == staff_id)
        .order_by(StaffSchedule.day_of_week.asc(), StaffSchedule.shift_start.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "day_of_week": r.day_of_week,
            "shift_start": r.shift_start.isoformat() if r.shift_start else None,
            "shift_end": r.shift_end.isoformat() if r.shift_end else None,
        }
        for r in rows
    ]


async def update_staff_schedule(
    db: AsyncSession, staff_id: str, schedules: list[dict]
) -> list[StaffSchedule]:
    """Replace the full weekly schedule for a staff member.

    Each dict: {"day_of_week": int, "shift_start": "HH:MM", "shift_end": "HH:MM"}
    """
    from datetime import time as _time

    # Delete existing
    existing = await db.execute(
        select(StaffSchedule).where(StaffSchedule.staff_id == staff_id)
    )
    for row in existing.scalars().all():
        await db.delete(row)
    await db.flush()

    created: list[StaffSchedule] = []
    for s in schedules:
        start = s["shift_start"]
        end = s["shift_end"]
        if isinstance(start, str):
            parts = start.split(":")
            start = _time(int(parts[0]), int(parts[1]))
        if isinstance(end, str):
            parts = end.split(":")
            end = _time(int(parts[0]), int(parts[1]))
        sched = StaffSchedule(
            id=generate_ulid(),
            staff_id=staff_id,
            day_of_week=s["day_of_week"],
            shift_start=start,
            shift_end=end,
        )
        db.add(sched)
        created.append(sched)
    await db.flush()
    return created


async def add_schedule_adjustment(
    db: AsyncSession,
    staff_id: str,
    *,
    date: datetime,
    adjustment_type: str,
    shift_start=None,
    shift_end=None,
    reason: str | None = None,
) -> StaffScheduleAdjustment:
    adj = StaffScheduleAdjustment(
        id=generate_ulid(),
        staff_id=staff_id,
        date=date,
        adjustment_type=adjustment_type,
        shift_start=shift_start,
        shift_end=shift_end,
        reason=reason,
    )
    db.add(adj)
    await db.flush()
    return adj


async def get_available_staff(
    db: AsyncSession,
    entity_id: str,
    *,
    at_datetime: datetime | None = None,
    skills: list[str] | None = None,
) -> list[Staff]:
    """Find staff available at a given time with optionally matching skills.

    Logic:
    1. Filter active staff in the entity.
    2. Check recurring schedule for the day-of-week & time.
    3. Exclude staff who have a day_off adjustment for that date.
    4. If skills provided, filter to staff whose skills array overlaps.
    """
    now = at_datetime or datetime.now(timezone.utc)
    dow = now.weekday()  # 0=Mon
    current_time = now.time()

    # Get all active staff
    q = select(Staff).where(
        Staff.entity_id == entity_id,
        Staff.status == "active",
        Staff.deleted_at.is_(None),
    )
    result = await db.execute(q)
    all_staff = list(result.scalars().all())

    if not all_staff:
        return []

    staff_ids = [s.id for s in all_staff]

    # Get schedules for the day
    sched_result = await db.execute(
        select(StaffSchedule).where(
            StaffSchedule.staff_id.in_(staff_ids),
            StaffSchedule.day_of_week == dow,
            StaffSchedule.shift_start <= current_time,
            StaffSchedule.shift_end > current_time,
        )
    )
    on_shift_ids = {r.staff_id for r in sched_result.scalars().all()}

    # Check adjustments for the date (day_off removes availability)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    adj_result = await db.execute(
        select(StaffScheduleAdjustment).where(
            StaffScheduleAdjustment.staff_id.in_(staff_ids),
            StaffScheduleAdjustment.date >= day_start,
            StaffScheduleAdjustment.date <= day_end,
            StaffScheduleAdjustment.adjustment_type == "day_off",
        )
    )
    off_ids = {r.staff_id for r in adj_result.scalars().all()}

    available_ids = on_shift_ids - off_ids

    available = [s for s in all_staff if s.id in available_ids]

    # Filter by skills if requested
    if skills:
        skill_set = set(skills)
        available = [
            s for s in available
            if s.skills and skill_set.intersection(s.skills)
        ]

    return available
