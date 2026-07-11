"""People endpoints — clients and staff members CRUD."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.staff import Staff, StaffRole
from packages.core.permissions import (
    Permission,
    check_effective_permission,
    legacy_role_for_staff_role,
    legacy_role_from_role_name,
    user_effective_permission_keys,
    user_has_effective_permission,
    user_staff_role_summary,
)
from packages.core.services.auth_service import (
    activate_user_membership,
    create_access_token,
    ensure_user_membership,
    get_user_membership,
    hash_password,
    list_user_memberships,
)
from packages.core.services.team_invite_service import (
    accept_team_invite_for_user,
    decline_team_invite_for_user,
)
from packages.core.services.people_service import (
    list_clients, get_client, create_client, update_client, delete_client,
    list_staff, get_staff_member, create_staff_member, update_staff_member, delete_staff_member,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1", tags=["people"])


# ── Schemas: Clients ──

class ClientResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    metadata: dict = {}
    status: str = "active"


class ClientCreateRequest(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    metadata: dict = {}
    status: str = "active"


class ClientUpdateRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    metadata: dict | None = None
    status: str | None = None


class ClientListResponse(BaseModel):
    items: list[ClientResponse]
    total: int


# ── Schemas: People gateway ──

class GatewayUserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    timezone: str | None = None
    locale: str | None = None


class GatewayEntityResponse(BaseModel):
    id: str
    name: str
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    logo_url: str | None = None
    plan_id: str | None = None
    plan_name: str | None = None


class GatewayMembershipResponse(BaseModel):
    entity_id: str
    entity_name: str | None = None
    role: str
    status: str
    staff_id: str | None = None
    staff_role_id: str | None = None
    staff_role_name: str | None = None
    is_primary: bool = False
    is_current: bool = False
    can_switch: bool = False
    can_leave: bool = False
    can_manage_team: bool = False
    can_manage_billing: bool = False


class GatewayInviteResponse(BaseModel):
    invite_id: str
    invite_token: str | None = None
    entity_id: str
    entity_name: str | None = None
    email: str
    name: str | None = None
    role_id: str | None = None
    role_name: str | None = None
    status: str
    can_accept: bool = False
    can_decline: bool = False


class GatewayBillingResponse(BaseModel):
    plan_id: str | None = None
    plan_name: str | None = None
    scope: str = "member"
    can_manage_billing: bool = False
    total_credits: int | None = None
    used_credits: int | None = None
    remaining_credits: int | None = None
    own_credits_used: int = 0
    own_tokens_used: int = 0
    own_cost_usd: float = 0


class GatewayActionsResponse(BaseModel):
    can_switch_entity: bool = False
    can_leave_entity: bool = False
    can_manage_team: bool = False
    can_manage_billing: bool = False
    can_accept_invites: bool = False
    can_decline_invites: bool = False


class PeopleContextResponse(BaseModel):
    user: GatewayUserResponse
    active_entity: GatewayEntityResponse | None = None
    active_membership: GatewayMembershipResponse | None = None
    memberships: list[GatewayMembershipResponse] = Field(default_factory=list)
    pending_invites: list[GatewayInviteResponse] = Field(default_factory=list)
    declined_invites: list[GatewayInviteResponse] = Field(default_factory=list)
    effective_permissions: list[str] = Field(default_factory=list)
    billing: GatewayBillingResponse
    usage_scope: str = "company"
    actions: GatewayActionsResponse


class PeopleContextActionResponse(BaseModel):
    access_token: str | None = None
    context: PeopleContextResponse


class PeopleDirectoryEntry(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    membership_status: str = "active"
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role_id: str | None = None
    staff_role_name: str | None = None


# ── Schemas: Staff ──
#
# Staff covers employees, contractors, vendors, and externals — discriminated
# by `kind`. Vendor/contractor-specific fields are optional; they stay null
# for employees. The free-form `department` and `role` strings are the
# legacy labels that live inside Staff.meta JSONB.

class StaffResponse(BaseModel):
    id: str
    entity_id: str
    kind: str = "employee"
    user_id: str | None = None
    role_id: str | None = None
    name: str
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    department: str | None = None     # legacy label (from meta)
    avatar_url: str | None = None
    role: str = "staff"               # legacy label (from meta)
    role_name: str | None = None
    login_email: str | None = None
    login_avatar_url: str | None = None
    skills: list[str] = []
    service_categories: list[str] = []

    # Vendor / contractor commercials
    company_name: str | None = None
    tax_id: str | None = None
    billing_rate: float | None = None
    billing_currency: str | None = None
    payment_terms: str | None = None
    preferred_payment_method: str | None = None

    # Contact + geo
    address: str | None = None
    website: str | None = None
    notes: str | None = None

    status: str = "active"
    has_unlinked_user_account: bool = False


class StaffCreateRequest(BaseModel):
    name: str
    kind: str = "employee"            # employee | contractor | vendor | external
    role_id: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    department: str | None = None
    avatar_url: str | None = None
    role: str = "staff"
    skills: list[str] = []
    service_categories: list[str] = []
    user_id: str | None = None

    company_name: str | None = None
    tax_id: str | None = None
    billing_rate: float | None = None
    billing_currency: str | None = None
    payment_terms: str | None = None
    preferred_payment_method: str | None = None

    address: str | None = None
    website: str | None = None
    notes: str | None = None

    status: str = "active"


class StaffUpdateRequest(BaseModel):
    name: str | None = None
    kind: str | None = None
    role_id: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    department: str | None = None
    avatar_url: str | None = None
    role: str | None = None
    skills: list[str] | None = None
    service_categories: list[str] | None = None

    company_name: str | None = None
    tax_id: str | None = None
    billing_rate: float | None = None
    billing_currency: str | None = None
    payment_terms: str | None = None
    preferred_payment_method: str | None = None

    address: str | None = None
    website: str | None = None
    notes: str | None = None

    status: str | None = None


# ── Helpers ──

async def _require_admin(db: AsyncSession, user: User) -> None:
    await check_effective_permission(
        db,
        user.id,
        user.entity_id,
        user.role,
        Permission.ADMIN_SETTINGS,
    )


async def _require_role_in_entity(
    db: AsyncSession,
    role_id: str | None,
    entity_id: str,
) -> None:
    if not role_id:
        return
    role = (
        await db.execute(
            select(StaffRole).where(
                StaffRole.id == role_id,
                StaffRole.entity_id == entity_id,
                StaffRole.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not role:
        raise HTTPException(404, "Role not found")


def _client_response(c) -> ClientResponse:
    return ClientResponse(
        id=c.id, entity_id=c.entity_id, name=c.name,
        email=c.email, phone=c.phone, address=c.address,
        metadata=c.meta or {}, status=c.status,
    )


def _normalized_email(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


async def _existing_user_emails(
    db: AsyncSession,
    entity_id: str,
    emails: list[str | None],
) -> set[str]:
    normalized = {e for e in (_normalized_email(email) for email in emails) if e}
    if not normalized:
        return set()
    rows = await db.execute(
        select(func.lower(User.email)).where(
            User.deleted_at.is_(None),
            func.lower(User.email).in_(normalized),
        )
    )
    return {email for email in rows.scalars().all() if email}


async def _staff_response(
    db: AsyncSession,
    s,
    *,
    existing_user_emails: set[str] | None = None,
) -> StaffResponse:
    meta = s.meta or {}
    normalized_email = _normalized_email(s.email)
    linked_user = None
    if s.user_id:
        linked_user = (
            await db.execute(
                select(User).where(User.id == s.user_id, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
    role_row = None
    if getattr(s, "role_id", None):
        role_row = (
            await db.execute(
                select(StaffRole).where(
                    StaffRole.id == s.role_id,
                    StaffRole.entity_id == s.entity_id,
                    StaffRole.status == "active",
                )
            )
        ).scalar_one_or_none()
    legacy_role = (meta.get("role") or "").strip()
    if legacy_role.lower() == "staff":
        legacy_role = ""
    display_name = (
        (linked_user.display_name if linked_user else None)
        or s.name
        or (linked_user.email.split("@")[0] if linked_user and linked_user.email else None)
        or s.email
        or "Team member"
    )
    display_email = s.email or (linked_user.email if linked_user else None)
    avatar_url = (
        (linked_user.avatar_url if linked_user else None)
        or s.avatar_url
    )
    return StaffResponse(
        id=s.id,
        entity_id=s.entity_id,
        kind=getattr(s, "kind", None) or "employee",
        user_id=s.user_id,
        role_id=getattr(s, "role_id", None),
        name=display_name,
        email=display_email,
        phone=s.phone,
        title=s.title,
        department=meta.get("department"),
        avatar_url=avatar_url,
        role=legacy_role,
        role_name=role_row.name if role_row else None,
        login_email=linked_user.email if linked_user else None,
        login_avatar_url=linked_user.avatar_url if linked_user else None,
        skills=s.skills or [],
        service_categories=getattr(s, "service_categories", None) or [],
        company_name=getattr(s, "company_name", None),
        tax_id=getattr(s, "tax_id", None),
        billing_rate=(
            float(s.billing_rate)
            if getattr(s, "billing_rate", None) is not None
            else None
        ),
        billing_currency=getattr(s, "billing_currency", None),
        payment_terms=getattr(s, "payment_terms", None),
        preferred_payment_method=getattr(s, "preferred_payment_method", None),
        address=getattr(s, "address", None),
        website=getattr(s, "website", None),
        notes=getattr(s, "notes", None),
        status=s.status,
        has_unlinked_user_account=bool(
            not s.user_id
            and normalized_email
            and existing_user_emails
            and normalized_email in existing_user_emails
        ),
    )


def _gateway_user(user: User) -> GatewayUserResponse:
    return GatewayUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        avatar_url=user.avatar_url,
        timezone=user.timezone,
        locale=user.locale,
    )


def _plan_name(plan_id: str | None) -> str | None:
    from packages.core.constants.plans import get_plan

    plan = get_plan(plan_id)
    return plan.get("name") if isinstance(plan, dict) else None


def _gateway_entity(entity: Entity | None) -> GatewayEntityResponse | None:
    if entity is None:
        return None
    return GatewayEntityResponse(
        id=entity.id,
        name=entity.name,
        address=entity.address,
        phone=entity.phone,
        email=entity.email,
        logo_url=entity.logo_url,
        plan_id=entity.plan_id,
        plan_name=_plan_name(entity.plan_id),
    )


async def _membership_response(
    db: AsyncSession,
    *,
    membership: UserMembership,
    entity: Entity | None,
    user: User,
    current_entity_id: str,
) -> GatewayMembershipResponse:
    staff_role_id = None
    staff_role_name = None
    if membership.staff_id:
        staff = await get_staff_member(db, membership.staff_id, membership.entity_id)
        if staff and staff.role_id:
            role = await db.get(StaffRole, staff.role_id)
            if role and role.entity_id == membership.entity_id:
                staff_role_id = role.id
                staff_role_name = role.name
    if not staff_role_id and membership.entity_id == current_entity_id:
        staff_role_id, staff_role_name, _ = await user_staff_role_summary(
            db,
            user.id,
            membership.entity_id,
        )

    can_manage_team = await user_has_effective_permission(
        db,
        user.id,
        membership.entity_id,
        membership.role,
        Permission.USERS_MANAGE,
    ) or await user_has_effective_permission(
        db,
        user.id,
        membership.entity_id,
        membership.role,
        Permission.USERS_INVITE,
    )
    can_manage_billing = await user_has_effective_permission(
        db,
        user.id,
        membership.entity_id,
        membership.role,
        Permission.ADMIN_BILLING,
    )
    is_current = membership.entity_id == current_entity_id
    can_leave = (
        is_current
        and membership.status == "active"
        and bool(membership.staff_id)
        and (membership.role or "").lower() not in {"owner", "admin"}
    )
    return GatewayMembershipResponse(
        entity_id=membership.entity_id,
        entity_name=entity.name if entity else None,
        role=membership.role,
        status=membership.status,
        staff_id=membership.staff_id,
        staff_role_id=staff_role_id,
        staff_role_name=staff_role_name,
        is_primary=bool(membership.is_primary),
        is_current=is_current,
        can_switch=membership.status == "active" and not is_current,
        can_leave=can_leave,
        can_manage_team=can_manage_team,
        can_manage_billing=can_manage_billing,
    )


async def _invite_rows_for_user(
    db: AsyncSession,
    user: User,
    *,
    statuses: tuple[str, ...],
) -> list[tuple[Staff, Entity | None, StaffRole | None]]:
    email = _normalized_email(user.email)
    if not email:
        return []
    rows = (
        await db.execute(
            select(Staff, Entity, StaffRole)
            .join(Entity, Entity.id == Staff.entity_id, isouter=True)
            .join(StaffRole, StaffRole.id == Staff.role_id, isouter=True)
            .where(
                func.lower(Staff.email) == email,
                Staff.status.in_(statuses),
                Staff.deleted_at.is_(None),
            )
            .order_by(Staff.created_at.desc())
        )
    ).all()
    return list(rows)


async def _invite_responses_for_user(
    db: AsyncSession,
    user: User,
    *,
    statuses: tuple[str, ...],
) -> list[GatewayInviteResponse]:
    return [
        _invite_response(staff, entity, role)
        for staff, entity, role in await _invite_rows_for_user(
            db,
            user,
            statuses=statuses,
        )
    ]


def _invite_response(
    staff: Staff,
    entity: Entity | None,
    role: StaffRole | None,
) -> GatewayInviteResponse:
    token = None
    if staff.status == "invited":
        token = (staff.meta or {}).get("invite_token")
    can_act = staff.status == "invited" and bool(token)
    return GatewayInviteResponse(
        invite_id=staff.id,
        invite_token=token,
        entity_id=staff.entity_id,
        entity_name=entity.name if entity else None,
        email=staff.email or "",
        name=staff.name,
        role_id=staff.role_id,
        role_name=role.name if role else None,
        status=staff.status,
        can_accept=can_act,
        can_decline=can_act,
    )


async def _own_credit_usage_summary(db: AsyncSession, user: User) -> dict[str, int | float]:
    from packages.core.models.billing import CreditUsageLog

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(CreditUsageLog.total_credit), 0).label("credits_used"),
                func.coalesce(func.sum(CreditUsageLog.total_tokens), 0).label("tokens_used"),
                func.coalesce(func.sum(CreditUsageLog.cost_usd), 0).label("cost_usd"),
            ).where(
                CreditUsageLog.entity_id == user.entity_id,
                CreditUsageLog.user_id == user.id,
            )
        )
    ).one()
    return {
        "credits_used": int(row.credits_used or 0),
        "tokens_used": int(row.tokens_used or 0),
        "cost_usd": round(float(row.cost_usd or 0), 6),
    }


async def _billing_response(
    db: AsyncSession,
    *,
    user: User,
    entity: Entity | None,
    can_manage_billing: bool,
) -> GatewayBillingResponse:
    from packages.core.constants.plans import canonical_plan_id, get_plan, is_cloud
    from packages.core.services.plan_enforcement import get_usage_summary

    settings = (entity.settings if entity else {}) or {}
    plan_id = canonical_plan_id((entity.plan_id if entity else None) or settings.get("plan", "plan_free"))
    plan_id = plan_id or "plan_free"
    plan = get_plan(plan_id)
    own_usage = await _own_credit_usage_summary(db, user)
    plan_summary = await get_usage_summary(db, user.entity_id)
    total = plan_summary.get("credits_total")
    used = plan_summary.get("credits_used")
    if total is None:
        plan_credits = int(plan.get("credit_amount") or 0)
        budget_usd = plan.get("ai_budget_usd") or 0
        total = int(settings.get("total_credits") or plan_credits or float(budget_usd) * 1000 or 0)
    used = int(used or settings.get("used_credits") or 0)
    return GatewayBillingResponse(
        plan_id=plan_summary.get("plan_id", plan_id),
        plan_name=plan_summary.get("plan") or plan.get("name"),
        scope="company",
        can_manage_billing=can_manage_billing,
        total_credits=total,
        used_credits=used,
        remaining_credits=max(0, int(total or 0) - used),
        own_credits_used=int(own_usage["credits_used"]),
        own_tokens_used=int(own_usage["tokens_used"]),
        own_cost_usd=float(own_usage["cost_usd"]),
    )


async def _people_context(db: AsyncSession, user: User) -> PeopleContextResponse:
    current_entity = await db.get(Entity, user.entity_id)
    membership_rows = await list_user_memberships(db, user)
    memberships = [
        await _membership_response(
            db,
            membership=membership,
            entity=entity,
            user=user,
            current_entity_id=user.entity_id,
        )
        for membership, entity in membership_rows
    ]
    active_membership = next((m for m in memberships if m.is_current), None)

    effective_permissions = sorted(await user_effective_permission_keys(
        db,
        user.id,
        user.entity_id,
        user.role,
    ))
    can_manage_team = (
        Permission.USERS_MANAGE.value in effective_permissions
        or Permission.USERS_INVITE.value in effective_permissions
    )
    can_manage_billing = Permission.ADMIN_BILLING.value in effective_permissions

    invite_responses = await _invite_responses_for_user(
        db,
        user,
        statuses=("invited", "declined"),
    )
    pending_invites = [
        invite for invite in invite_responses
        if invite.status == "invited"
    ]
    declined_invites = [
        invite for invite in invite_responses
        if invite.status == "declined"
    ]

    return PeopleContextResponse(
        user=_gateway_user(user),
        active_entity=_gateway_entity(current_entity),
        active_membership=active_membership,
        memberships=memberships,
        pending_invites=pending_invites,
        declined_invites=declined_invites,
        effective_permissions=effective_permissions,
        billing=await _billing_response(
            db,
            user=user,
            entity=current_entity,
            can_manage_billing=can_manage_billing,
        ),
        usage_scope="company" if can_manage_billing else "member",
        actions=GatewayActionsResponse(
            can_switch_entity=any(m.can_switch for m in memberships),
            can_leave_entity=bool(active_membership and active_membership.can_leave),
            can_manage_team=can_manage_team,
            can_manage_billing=can_manage_billing,
            can_accept_invites=any(i.can_accept for i in pending_invites),
            can_decline_invites=any(i.can_decline for i in pending_invites),
        ),
    )


async def _current_user_invite(
    db: AsyncSession,
    *,
    user: User,
    invite_id: str,
) -> Staff:
    email = _normalized_email(user.email)
    staff = (
        await db.execute(
            select(Staff).where(
                Staff.id == invite_id,
                func.lower(Staff.email) == email,
                Staff.status == "invited",
                Staff.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not staff:
        raise HTTPException(404, "Pending invite not found")
    token = (staff.meta or {}).get("invite_token")
    if not token:
        raise HTTPException(400, "Invite has no active token")
    return staff


# ── Client Routes ──


@router.get("/people/me", response_model=PeopleContextResponse)
async def people_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _people_context(db, user)


@router.get("/people/directory", response_model=list[PeopleDirectoryEntry])
async def people_directory(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(User, UserMembership, Staff, StaffRole)
            .join(
                UserMembership,
                UserMembership.user_id == User.id,
            )
            .join(
                Staff,
                (Staff.user_id == User.id)
                & (Staff.entity_id == user.entity_id)
                & (Staff.deleted_at.is_(None)),
                isouter=True,
            )
            .join(StaffRole, StaffRole.id == Staff.role_id, isouter=True)
            .where(
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
                User.deleted_at.is_(None),
                User.status == "active",
            )
            .order_by(User.display_name.asc().nulls_last(), User.email.asc())
        )
    ).all()
    return [
        PeopleDirectoryEntry(
            id=row_user.id,
            email=row_user.email,
            display_name=row_user.display_name,
            avatar_url=row_user.avatar_url,
            membership_status=membership.status,
            staff_id=staff.id if staff else None,
            staff_name=staff.name if staff else None,
            staff_role_id=staff.role_id if staff else None,
            staff_role_name=role.name if role else None,
        )
        for row_user, membership, staff, role in rows
    ]


@router.post("/people/invites/{invite_id}/accept", response_model=PeopleContextActionResponse)
async def accept_people_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    staff = await _current_user_invite(db, user=user, invite_id=invite_id)
    token = (staff.meta or {}).get("invite_token")
    await accept_team_invite_for_user(db, token=token, user=user)
    access_token = create_access_token(user.id, user.entity_id, user.role)
    return PeopleContextActionResponse(
        access_token=access_token,
        context=await _people_context(db, user),
    )


@router.post("/people/invites/{invite_id}/decline", response_model=PeopleContextActionResponse)
async def decline_people_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    staff = await _current_user_invite(db, user=user, invite_id=invite_id)
    token = (staff.meta or {}).get("invite_token")
    await decline_team_invite_for_user(db, token=token, user=user)
    return PeopleContextActionResponse(
        access_token=None,
        context=await _people_context(db, user),
    )


@router.post("/people/memberships/{entity_id}/switch", response_model=PeopleContextActionResponse)
async def switch_people_membership(
    entity_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    membership = await get_user_membership(db, user=user, entity_id=entity_id)
    if not membership or membership.status != "active":
        raise HTTPException(403, "You do not have an active membership in this company.")
    await activate_user_membership(db, user=user, membership=membership)
    access_token = create_access_token(user.id, membership.entity_id, membership.role)
    return PeopleContextActionResponse(
        access_token=access_token,
        context=await _people_context(db, user),
    )


@router.post("/people/memberships/{entity_id}/leave", response_model=PeopleContextActionResponse)
async def leave_people_membership(
    entity_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if entity_id != user.entity_id:
        membership = await get_user_membership(db, user=user, entity_id=entity_id)
        if not membership or membership.status != "active":
            raise HTTPException(404, "Active team membership not found")
        raise HTTPException(400, "Switch to this company before leaving it.")
    if user.role in {"owner", "admin"}:
        raise HTTPException(403, "Owners and admins must transfer or change role before leaving.")

    member = (
        await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == user.id,
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Active team membership not found")

    staff = None
    if member.staff_id:
        staff = await get_staff_member(db, member.staff_id, user.entity_id)
    if staff is None:
        staff = (
            await db.execute(
                select(Staff).where(
                    Staff.user_id == user.id,
                    Staff.entity_id == user.entity_id,
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if staff is None:
        raise HTTPException(404, "Staff member not found")

    staff.status = "inactive"
    member.status = "inactive"

    next_membership = (
        await db.execute(
            select(UserMembership)
            .where(
                UserMembership.user_id == user.id,
                UserMembership.status == "active",
                UserMembership.entity_id != user.entity_id,
            )
            .order_by(UserMembership.is_primary.desc(), UserMembership.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    access_token = None
    if next_membership is not None:
        await activate_user_membership(db, user=user, membership=next_membership)
        access_token = create_access_token(user.id, next_membership.entity_id, next_membership.role)
    await db.flush()
    return PeopleContextActionResponse(
        access_token=access_token,
        context=await _people_context(db, user),
    )

@router.get("/clients", response_model=ClientListResponse)
async def list_my_clients(
    search: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await list_clients(db, user.entity_id, search=search, status=status, limit=limit, offset=offset)
    return ClientListResponse(items=[_client_response(c) for c in items], total=total)


@router.post("/clients", response_model=ClientResponse, status_code=201)
async def create_new_client(
    req: ClientCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    c = await create_client(
        db, user.entity_id,
        name=req.name, email=req.email, phone=req.phone,
        address=req.address, meta=req.metadata, status=req.status,
    )
    return _client_response(c)


@router.get("/clients/{client_id}", response_model=ClientResponse)
async def get_one_client(
    client_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    c = await get_client(db, client_id, user.entity_id)
    if not c:
        raise HTTPException(404, "Client not found")
    return _client_response(c)


@router.put("/clients/{client_id}", response_model=ClientResponse)
async def update_one_client(
    client_id: str,
    req: ClientUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    fields = req.model_dump(exclude_none=True)
    # Map 'metadata' key to 'meta' attribute
    if "metadata" in fields:
        fields["meta"] = fields.pop("metadata")
    c = await update_client(db, client_id, user.entity_id, **fields)
    if not c:
        raise HTTPException(404, "Client not found")
    return _client_response(c)


@router.delete("/clients/{client_id}", status_code=204)
async def delete_one_client(
    client_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_client(db, client_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Client not found")


# ── Staff Routes ──

@router.get("/staff", response_model=list[StaffResponse])
async def list_my_staff(
    department: str | None = Query(None),
    role: str | None = Query(None),
    kind: str | None = Query(None, description="Filter by kind: employee|contractor|vendor|external"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    members = await list_staff(
        db, user.entity_id, department=department, role=role, kind=kind,
    )
    existing_user_emails = await _existing_user_emails(
        db,
        user.entity_id,
        [s.email for s in members],
    )
    return [
        await _staff_response(db, s, existing_user_emails=existing_user_emails)
        for s in members
    ]


@router.post("/staff", response_model=StaffResponse, status_code=201)
async def create_new_staff(
    req: StaffCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_role_in_entity(db, req.role_id, user.entity_id)
    s = await create_staff_member(
        db, user.entity_id, **req.model_dump(exclude_none=True),
    )
    existing_user_emails = await _existing_user_emails(db, user.entity_id, [s.email])
    return await _staff_response(db, s, existing_user_emails=existing_user_emails)


@router.get("/staff/{staff_id}", response_model=StaffResponse)
async def get_one_staff(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await get_staff_member(db, staff_id, user.entity_id)
    if not s:
        raise HTTPException(404, "Staff member not found")
    existing_user_emails = await _existing_user_emails(db, user.entity_id, [s.email])
    return await _staff_response(db, s, existing_user_emails=existing_user_emails)


@router.put("/staff/{staff_id}", response_model=StaffResponse)
async def update_one_staff(
    staff_id: str,
    req: StaffUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_role_in_entity(db, req.role_id, user.entity_id)
    s = await update_staff_member(db, staff_id, user.entity_id, **req.model_dump(exclude_none=True))
    if not s:
        raise HTTPException(404, "Staff member not found")
    existing_user_emails = await _existing_user_emails(db, user.entity_id, [s.email])
    return await _staff_response(db, s, existing_user_emails=existing_user_emails)


@router.delete("/staff/{staff_id}", status_code=204)
async def delete_one_staff(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # DELETE is intentionally idempotent. Team pages can hold stale staff
    # cards after another tab/user removes a row; returning 204 keeps the
    # UI cleanup path smooth without leaking whether a cross-entity ID exists.
    await delete_staff_member(db, staff_id, user.entity_id)


# ── Staff account creation ──

class StaffAccountResponse(BaseModel):
    staff_id: str
    user_id: str
    email: str
    password: str


class StaffInviteDeliveryResponse(BaseModel):
    staff_id: str
    invite_token: str
    invite_url: str
    email: str
    role_id: str | None = None
    status: str
    email_sent: bool = False


class LeaveTeamResponse(BaseModel):
    staff_id: str
    status: str


@router.post("/staff/me/leave", response_model=LeaveTeamResponse)
async def leave_my_team(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role in {"owner", "admin"}:
        raise HTTPException(403, "Owners and admins must transfer or change role before leaving.")

    member = (
        await db.execute(
            select(UserMembership)
            .where(
                UserMembership.user_id == user.id,
                UserMembership.entity_id == user.entity_id,
                UserMembership.status == "active",
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Active team membership not found")

    staff = None
    if member.staff_id:
        staff = await get_staff_member(db, member.staff_id, user.entity_id)
    if staff is None:
        staff = (
            await db.execute(
                select(Staff).where(
                    Staff.user_id == user.id,
                    Staff.entity_id == user.entity_id,
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if staff is None:
        raise HTTPException(404, "Staff member not found")

    staff.status = "inactive"
    member.status = "inactive"

    next_membership = (
        await db.execute(
            select(UserMembership)
            .where(
                UserMembership.user_id == user.id,
                UserMembership.status == "active",
                UserMembership.entity_id != user.entity_id,
            )
            .order_by(UserMembership.is_primary.desc(), UserMembership.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if next_membership is not None:
        await activate_user_membership(db, user=user, membership=next_membership)
    await db.flush()
    return LeaveTeamResponse(staff_id=staff.id, status=staff.status)


@router.post("/staff/{staff_id}/resend-invite", response_model=StaffInviteDeliveryResponse)
async def resend_staff_invite(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_effective_permission(
        db,
        user.id,
        user.entity_id,
        user.role,
        Permission.USERS_INVITE,
    )
    member = await get_staff_member(db, staff_id, user.entity_id)
    if not member:
        raise HTTPException(404, "Staff member not found")
    if member.user_id:
        raise HTTPException(409, "Staff member already has a login account")
    if not member.email:
        raise HTTPException(400, "Staff member must have an email address before resending an invite")

    from apps.api.routers.permissions import (
        _build_invite_url,
        _find_user_by_email,
        _notify_staff_invite_received,
        _notify_staff_invite_sent,
        _send_staff_invite_email_best_effort,
    )

    meta = dict(member.meta or {})
    token = meta.get("invite_token")
    if not token:
        token = secrets.token_urlsafe(32)
        meta["invite_token"] = token
    member.meta = meta
    member.status = "invited"
    await db.flush()

    invite_url = _build_invite_url(token, member.email)
    entity = await db.get(Entity, user.entity_id)
    entity_name = entity.name if entity and entity.name else "your team"
    inviter_name = user.display_name or user.email or "A Manor AI admin"
    email_sent = await _send_staff_invite_email_best_effort(
        to=member.email,
        entity_name=entity_name,
        inviter_name=inviter_name,
        invite_url=invite_url,
    )
    await _notify_staff_invite_sent(
        db=db,
        entity_id=user.entity_id,
        user_id=user.id,
        invitee_email=member.email,
        invite_url=invite_url,
        email_sent=email_sent,
    )
    invitee_user = await _find_user_by_email(db, member.email)
    if invitee_user and invitee_user.id != user.id:
        await _notify_staff_invite_received(
            db=db,
            invitee_user=invitee_user,
            entity_id=user.entity_id,
            entity_name=entity_name,
            inviter_name=inviter_name,
            inviter_user_id=user.id,
            invite_token=token,
            invitee_email=member.email,
            invite_url=invite_url,
        )

    return StaffInviteDeliveryResponse(
        staff_id=member.id,
        invite_token=token,
        invite_url=invite_url,
        email=member.email,
        role_id=member.role_id,
        status=member.status,
        email_sent=email_sent,
    )


@router.post("/staff/{staff_id}/create-account", response_model=StaffAccountResponse, status_code=201)
async def create_staff_account(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a login account for an existing staff member.

    Generates a random password and creates a User row in the same entity,
    then links it back via staff.user_id. The plain-text password is returned
    once — the caller must display and hand it to the staff member immediately.
    """
    await _require_admin(db, user)
    member = await get_staff_member(db, staff_id, user.entity_id)
    if not member:
        raise HTTPException(404, "Staff member not found")
    if not member.email:
        raise HTTPException(400, "Staff member must have an email address before creating an account")
    if member.user_id:
        raise HTTPException(409, "Staff member already has a login account")

    existing = await db.execute(select(User).where(User.email == member.email))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A user with that email already exists")

    from packages.core.services.auth_service import _generate_avatar_url
    from packages.core.models.base import generate_ulid

    plain_password = secrets.token_urlsafe(12)

    meta = member.meta or {}
    legacy_default = legacy_role_from_role_name(meta.get("role"), default="member")
    role = await legacy_role_for_staff_role(
        db,
        member.role_id,
        user.entity_id,
        default=legacy_default,
    )

    new_user = User(
        id=generate_ulid(),
        entity_id=user.entity_id,
        email=member.email,
        display_name=member.name,
        password_hash=hash_password(plain_password),
        avatar_url=member.avatar_url or _generate_avatar_url(member.name),
        role=role,
        status="active",
    )
    # The email pre-check above is best-effort: a concurrent create-account
    # request can insert the same email between the SELECT and this flush.
    # Guard the insert in a savepoint so the UNIQUE violation surfaces as a
    # clean 409 instead of a raw 500.
    try:
        async with db.begin_nested():
            db.add(new_user)
            await db.flush()
    except IntegrityError:
        raise HTTPException(409, "A user with that email already exists")

    member.user_id = new_user.id
    member.status = "active"
    await ensure_user_membership(
        db,
        user=new_user,
        entity_id=user.entity_id,
        role=role,
        status="active",
        staff_id=member.id,
        is_primary=True,
    )
    await db.flush()

    return StaffAccountResponse(
        staff_id=member.id,
        user_id=new_user.id,
        email=member.email,
        password=plain_password,
    )


@router.post("/staff/{staff_id}/reset-password", response_model=StaffAccountResponse)
async def reset_staff_account_password(
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_admin(db, user)
    member = await get_staff_member(db, staff_id, user.entity_id)
    if not member:
        raise HTTPException(404, "Staff member not found")
    if not member.user_id:
        raise HTTPException(409, "Staff member does not have a login account")

    linked = (
        await db.execute(
            select(User).where(User.id == member.user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not linked:
        raise HTTPException(404, "Linked user account not found")

    plain_password = secrets.token_urlsafe(12)
    linked.password_hash = hash_password(plain_password)
    await db.flush()

    return StaffAccountResponse(
        staff_id=member.id,
        user_id=linked.id,
        email=linked.email,
        password=plain_password,
    )
