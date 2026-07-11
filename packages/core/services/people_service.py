"""Client and staff service — CRUD operations.

The staff functions in this module operate on the unified :class:`Staff`
model from ``staff.py`` (employees + contractors + vendors). The legacy
``StaffMember`` model has been removed.

Two free-form string fields from the old API — ``department`` and
``role`` — are stored inside ``Staff.meta`` to preserve the wire
contract used by older callers and tests. Proper org placement via
``department_id`` / ``role_id`` / ``kind`` is supported in parallel.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.people import Client
from packages.core.models.staff import Staff, STAFF_KIND_EMPLOYEE
from packages.core.services.tool_cache_version import bump_tool_cache_version


# ── Clients ────────────────────────────────────────────────────────────────

async def list_clients(
    db: AsyncSession,
    entity_id: str,
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Client], int]:
    base = select(Client).where(Client.entity_id == entity_id, Client.deleted_at.is_(None))
    if search:
        pattern = f"%{search}%"
        base = base.where(Client.name.ilike(pattern))
    if status:
        base = base.where(Client.status == status)

    count_result = await db.execute(select(func.count()).select_from(base.subquery()))
    total = count_result.scalar() or 0

    result = await db.execute(
        base.order_by(Client.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), total


async def get_client(db: AsyncSession, client_id: str, entity_id: str) -> Optional[Client]:
    result = await db.execute(
        select(Client).where(
            Client.id == client_id,
            Client.entity_id == entity_id,
            Client.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_client(db: AsyncSession, entity_id: str, **fields) -> Client:
    client = Client(id=generate_ulid(), entity_id=entity_id, **fields)
    db.add(client)
    await db.flush()
    return client


async def update_client(db: AsyncSession, client_id: str, entity_id: str, **fields) -> Optional[Client]:
    client = await get_client(db, client_id, entity_id)
    if not client:
        return None
    for k, v in fields.items():
        if hasattr(client, k) and v is not None:
            setattr(client, k, v)
    await db.flush()
    await db.refresh(client)
    return client


async def delete_client(db: AsyncSession, client_id: str, entity_id: str) -> bool:
    client = await get_client(db, client_id, entity_id)
    if not client:
        return False
    client.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    return True


# ── Staff (employees / contractors / vendors / externals) ───────────────────

def _pop_meta_labels(fields: dict) -> dict:
    """Extract legacy string labels (department, role) into a meta dict."""
    meta: dict = {}
    if "department" in fields:
        dept = fields.pop("department")
        if dept is not None:
            meta["department"] = dept
    if "role" in fields:
        role = fields.pop("role")
        if role is not None:
            meta["role"] = role
    return meta


async def list_staff(
    db: AsyncSession,
    entity_id: str,
    department: Optional[str] = None,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    search: Optional[str] = None,
    email: Optional[str] = None,
) -> list[Staff]:
    query = select(Staff).where(
        Staff.entity_id == entity_id,
        Staff.deleted_at.is_(None),
    )
    if kind:
        query = query.where(Staff.kind == kind)
    if department is not None:
        query = query.where(Staff.meta["department"].astext == department)
    if role is not None:
        query = query.where(Staff.meta["role"].astext == role)
    if email is not None:
        query = query.where(Staff.email.ilike(email))
    if search:
        pattern = f"%{search}%"
        query = query.where(
            (Staff.name.ilike(pattern))
            | (Staff.email.ilike(pattern))
        )
    result = await db.execute(query.order_by(Staff.created_at.desc()))
    return list(result.scalars().all())


async def get_staff_member(
    db: AsyncSession, staff_id: str, entity_id: str
) -> Optional[Staff]:
    result = await db.execute(
        select(Staff).where(
            Staff.id == staff_id,
            Staff.entity_id == entity_id,
            Staff.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_staff_member(
    db: AsyncSession, entity_id: str, **fields
) -> Staff:
    meta = _pop_meta_labels(fields)
    fields.setdefault("kind", STAFF_KIND_EMPLOYEE)
    member = Staff(id=generate_ulid(), entity_id=entity_id, meta=meta, **fields)
    db.add(member)
    await db.flush()
    await bump_tool_cache_version(entity_id, "staff")
    return member


async def update_staff_member(
    db: AsyncSession, staff_id: str, entity_id: str, **fields
) -> Optional[Staff]:
    member = await get_staff_member(db, staff_id, entity_id)
    if not member:
        return None

    meta_updates = _pop_meta_labels(fields)
    if meta_updates:
        merged = dict(member.meta or {})
        merged.update(meta_updates)
        member.meta = merged

    for k, v in fields.items():
        if hasattr(member, k) and v is not None:
            setattr(member, k, v)

    await db.flush()
    await db.refresh(member)
    await bump_tool_cache_version(entity_id, "staff")
    return member


async def delete_staff_member(
    db: AsyncSession, staff_id: str, entity_id: str
) -> bool:
    member = await get_staff_member(db, staff_id, entity_id)
    if not member:
        return False
    member.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    await bump_tool_cache_version(entity_id, "staff")
    return True
