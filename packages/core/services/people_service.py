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
from packages.core.models.user import User, UserMembership
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.workspace_access import set_link_columns


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
    if "meta" in fields:
        fields["meta"] = {k: v for k, v in (fields["meta"] or {}).items() if k not in {"crm_invite_version", "relationship_links"}}
    client = Client(id=generate_ulid(), entity_id=entity_id, **fields)
    db.add(client)
    await db.flush()
    return client


async def update_client(db: AsyncSession, client_id: str, entity_id: str, **fields) -> Optional[Client]:
    client = await db.scalar(select(Client).where(Client.id == client_id, Client.entity_id == entity_id, Client.deleted_at.is_(None)).with_for_update())
    if not client:
        return None
    version = (client.meta or {}).get("crm_invite_version", 0)
    if client.status == "active" and "status" in fields and fields["status"] != "active":
        version += 1
        fields["meta"] = fields.get("meta") or client.meta or {}
    if "meta" in fields:
        # Only the explicit, Workspace-authorized linker may change these refs.
        fields["meta"] = {k: v for k, v in (fields["meta"] or {}).items() if k != "relationship_links"}
        if "relationship_links" in (client.meta or {}):
            fields["meta"]["relationship_links"] = client.meta["relationship_links"]
        fields["meta"]["crm_invite_version"] = version
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
    if member.user_id:
        await _sync_linked_staff_access(db, member)
        await db.flush()
    await bump_tool_cache_version(entity_id, "staff")
    return member


async def _sync_linked_staff_access(db: AsyncSession, member: Staff) -> None:
    """Keep a linked login, membership, and Staff authority fail-closed."""
    if not member.user_id:
        return

    active_staff_ids = list(
        (
            await db.execute(
                select(Staff.id)
                .where(
                    Staff.user_id == member.user_id,
                    Staff.entity_id == member.entity_id,
                    Staff.status == "active",
                    Staff.deleted_at.is_(None),
                )
                .limit(2)
            )
        ).scalars()
    )
    access_enabled = len(active_staff_ids) == 1
    await _apply_entity_access(
        db,
        user_id=member.user_id,
        entity_id=member.entity_id,
        enabled=access_enabled,
        staff_id=active_staff_ids[0] if access_enabled else None,
    )


async def _apply_entity_access(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    enabled: bool,
    staff_id: str | None = None,
) -> None:
    """Set one Entity's membership on or off and keep the login consistent.

    Turning access off revokes every token, and if this was the user's primary
    Entity moves them to their next active one (or marks them inactive).
    """
    access_enabled = enabled
    membership = (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if membership is not None:
        membership.status = "active" if access_enabled else "inactive"
        if access_enabled and staff_id:
            # The Staff link alone is not an authority change (see set_link_columns).
            await set_link_columns(db, membership, staff_id=staff_id)

    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        return
    if access_enabled:
        if user.entity_id == entity_id:
            user.status = "active"
        return

    # Tokens are versioned globally, so this immediately revokes every token
    # that could still carry the removed entity scope.
    user.token_version = int(user.token_version or 0) + 1
    if user.entity_id != entity_id:
        return

    # The person's own next company, never a collaborator membership: that
    # is access to one customer Workspace, not a company to work in.
    from packages.core.services.auth_service import normalize_membership_role
    from packages.core.services.external_collaboration_access import home_membership

    next_membership = await home_membership(db, user.id, entity_id)
    if next_membership is None:
        user.status = "inactive"
        return

    user.entity_id = next_membership.entity_id
    user.role = normalize_membership_role(next_membership.role)
    user.status = "active"


async def assign_staff_role(
    db: AsyncSession,
    member: Staff,
    *,
    role_id: str | None = None,
    role_name: str | None = None,
) -> None:
    """Point a Staff row at one role and carry it to the linked login.

    The StaffRole is the authority, so a role given by name (the Team page and
    the Users page both send names) resolves to the Entity's role of that name.
    A name that is neither a system role nor an existing StaffRole is only a
    descriptive label and leaves the assigned role alone.
    """
    from packages.core.models.staff import StaffRole
    from packages.core.permissions import _ROLE_HIERARCHY
    from packages.core.services.auth_service import _role_for_staff_membership

    normalized = (role_name or "").strip().lower()
    query = select(StaffRole).where(
        StaffRole.entity_id == member.entity_id,
        StaffRole.status == "active",
    )
    if role_id:
        query = query.where(StaffRole.id == role_id)
    elif normalized:
        query = query.where(func.lower(StaffRole.name) == normalized)
    else:
        return
    role = (
        await db.execute(query.order_by(StaffRole.created_at.asc()).limit(1))
    ).scalar_one_or_none()

    meta = dict(member.meta or {})
    if role is not None:
        member.role_id = role.id
        meta["role"] = role.name
    elif not role_id and normalized in _ROLE_HIERARCHY:
        # No seeded StaffRole yet: the system role name itself is the authority.
        member.role_id = None
        meta["role"] = normalized
    else:
        return
    member.meta = meta
    await db.flush()

    if not member.user_id:
        return
    membership = (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == member.user_id,
                UserMembership.entity_id == member.entity_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        return
    membership.role = await _role_for_staff_membership(db, member)
    user = await db.get(User, member.user_id)
    if user is not None and user.entity_id == member.entity_id:
        user.role = membership.role
    await db.flush()


async def deactivate_entity_member(
    db: AsyncSession, *, user_id: str, entity_id: str
) -> bool:
    """Remove one person's access to this Entity only, like deactivating them.

    Their live Staff rows become inactive (so the Team page agrees) and the
    membership is turned off; their other companies are untouched.
    """
    membership_id = (
        await db.execute(
            select(UserMembership.id).where(
                UserMembership.user_id == user_id,
                UserMembership.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    staff_rows = list(
        (
            await db.execute(
                select(Staff).where(
                    Staff.user_id == user_id,
                    Staff.entity_id == entity_id,
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    if membership_id is None and not staff_rows:
        return False
    for row in staff_rows:
        row.status = "inactive"
    await db.flush()
    await _apply_entity_access(db, user_id=user_id, entity_id=entity_id, enabled=False)
    await db.flush()
    await bump_tool_cache_version(entity_id, "staff")
    return True


async def update_staff_member(
    db: AsyncSession, staff_id: str, entity_id: str, **fields
) -> Optional[Staff]:
    member = await get_staff_member(db, staff_id, entity_id)
    if not member:
        return None
    status_was_provided = "status" in fields
    role_id = fields.pop("role_id", None)

    meta_updates = _pop_meta_labels(fields)
    if meta_updates:
        merged = dict(member.meta or {})
        merged.update(meta_updates)
        member.meta = merged

    for k, v in fields.items():
        if hasattr(member, k) and v is not None:
            setattr(member, k, v)

    if role_id or meta_updates.get("role"):
        await assign_staff_role(
            db, member, role_id=role_id, role_name=meta_updates.get("role"),
        )
    await db.flush()
    if status_was_provided:
        await _sync_linked_staff_access(db, member)
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
    await _sync_linked_staff_access(db, member)
    await db.flush()
    await bump_tool_cache_version(entity_id, "staff")
    return True
