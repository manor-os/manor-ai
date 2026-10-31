"""Private credential-source authorization for end-user integrations."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, false, or_, select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from packages.core.models.document import Integration
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.user_avatars import person_avatar_url, staff_avatar_column

logger = logging.getLogger(__name__)

ConnectionKind = Literal["integration", "oauth_account"]
ConnectionAction = Literal["view", "use", "manage", "share", "bind_channel"]
# Internal team roles. Clients, external public-chat users and future guest
# roles are excluded from Entity-wide shares and from named share recipients,
# and a connection's shares only work while its owner holds one of them.
SHARING_MEMBER_ROLES = frozenset({"owner", "admin", "member", "viewer"})
# Viewers cannot run personal integrations through agents (no mcp.use_personal),
# so they are neither offered nor accepted as named recipients. They still match
# an Entity-wide share, which then applies as soon as their role is raised.
SHARE_RECIPIENT_ROLES = SHARING_MEMBER_ROLES - {"viewer"}
_SHAREABLE_SUBJECT_TYPES = (SubjectType.USER, SubjectType.ENTITY)


@dataclass(frozen=True)
class IntegrationAccessDecision:
    allowed: bool
    reason: str
    kind: str
    connection_id: str | None = None
    owner_user_id: str | None = None


@dataclass(frozen=True)
class _ConnectionSource:
    id: str
    kind: ConnectionKind
    owner_user_id: str | None
    entity_id: str | None
    provider: str


def _resource_type(kind: ConnectionKind) -> str:
    if kind == "integration":
        return ResourceType.INTEGRATION
    if kind == "oauth_account":
        return ResourceType.OAUTH_ACCOUNT
    raise ValueError(f"Unsupported connection kind: {kind}")


async def _load_row(db: AsyncSession, model, connection_id: str, *, lock: bool):
    if not lock:
        return await db.get(model, connection_id)
    return (await db.execute(
        select(model)
        .where(model.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _load_source(
    db: AsyncSession, *, kind: ConnectionKind, connection_id: str, lock: bool = False,
) -> _ConnectionSource | None:
    """Load a connection's ownership facts.

    ``lock`` takes the row lock that serializes share writes on one
    connection, so concurrent clicks or retries cannot both insert a grant.
    """
    if kind == "integration":
        row = await _load_row(db, Integration, connection_id, lock=lock)
        if not row:
            return None
        return _ConnectionSource(
            id=row.id,
            kind=kind,
            owner_user_id=row.owner_user_id,
            entity_id=row.entity_id,
            provider=row.provider,
        )
    if kind == "oauth_account":
        row = await _load_row(db, OAuthAccount, connection_id, lock=lock)
        if not row:
            return None
        from packages.core.services.manor_integration_oauth import is_manor_oauth_provider, manor_oauth_binding

        return _ConnectionSource(
            id=row.id,
            kind=kind,
            owner_user_id=row.user_id,
            entity_id=(manor_oauth_binding(row).get("entity_id") or "invalid") if is_manor_oauth_provider(row.provider) else None,
            provider=row.provider,
        )
    raise ValueError(f"Unsupported connection kind: {kind}")


async def _active_entity_member_role(
    db: AsyncSession, *, user_id: str | None, entity_id: str,
) -> str | None:
    """Role of an active Entity member, or None when not an active member.

    One round trip: this runs on every access decision and registry load.
    """
    if not user_id or not entity_id:
        return None
    row = (await db.execute(
        select(
            User.status,
            User.deleted_at,
            User.entity_id,
            User.role,
            UserMembership.id.label("membership_id"),
            UserMembership.status.label("membership_status"),
            UserMembership.deleted_at.label("membership_deleted_at"),
            UserMembership.role.label("membership_role"),
        )
        .outerjoin(
            UserMembership,
            and_(UserMembership.user_id == User.id, UserMembership.entity_id == entity_id),
        )
        .where(User.id == user_id)
        .limit(1)
    )).one_or_none()
    if row is None:
        return None
    user_active = row.status == "active" and row.deleted_at is None
    if row.membership_id is not None:
        if row.membership_status != "active" or row.membership_deleted_at is not None:
            return None
        return (row.membership_role or "member") if user_active else None
    # Older primary-entity accounts predate UserMembership backfill. They are
    # still the current authenticated owner, but never grant cross-Entity use.
    # An explicit inactive/deleted membership is authoritative and must never
    # fall through to this compatibility path.
    if user_active and row.entity_id == entity_id:
        return row.role or "member"
    return None


async def _is_active_entity_member(
    db: AsyncSession, *, user_id: str | None, entity_id: str,
) -> bool:
    return await _active_entity_member_role(db, user_id=user_id, entity_id=entity_id) is not None


async def lock_connection_owner_authorization(
    db: AsyncSession,
    *,
    entity_id: str,
    owner_user_id: str,
) -> bool:
    """Freeze the persisted inputs to a connection-owner access decision.

    Callers first lock their Workspace and route rows, then hold these shared
    locks through provider I/O. Membership/account revocation therefore
    commits either before the final access check or after the provider call.
    """

    statements = (
        select(UserMembership.id)
        .where(
            UserMembership.entity_id == entity_id,
            UserMembership.user_id == owner_user_id,
        )
        .order_by(UserMembership.id)
        .with_for_update(read=True),
        select(User.id)
        .where(User.id == owner_user_id)
        .with_for_update(read=True),
    )
    for statement in statements:
        await db.execute(statement)
    return await _is_active_entity_member(
        db,
        user_id=owner_user_id,
        entity_id=entity_id,
    )


async def use_grant_subjects(
    db: AsyncSession, *, entity_id: str, user_id: str,
) -> tuple[tuple[str, str], ...]:
    """Grant subjects whose ``use`` grants apply to this user in this Entity."""
    role = await _active_entity_member_role(db, user_id=user_id, entity_id=entity_id)
    if role not in SHARING_MEMBER_ROLES:
        return ()
    return ((SubjectType.USER, user_id), (SubjectType.ENTITY, entity_id))


def grant_subject_clause(subjects: tuple[tuple[str, str], ...]) -> ColumnElement[bool]:
    """SQL predicate matching grants held by any subject; matches nothing when empty."""
    if not subjects:
        return false()
    return or_(*(
        and_(ResourceGrant.subject_type == subject_type, ResourceGrant.subject_id == subject_id)
        for subject_type, subject_id in subjects
    ))


async def _active_use_grant(
    db: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    entity_id: str,
    user_id: str,
) -> bool:
    now = datetime.now(UTC)
    subjects = await use_grant_subjects(db, entity_id=entity_id, user_id=user_id)
    grants = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
            grant_subject_clause(subjects),
        )
    )).scalars().all()
    return any(
        Capability.USE in (grant.capabilities or [])
        and (grant.expires_at is None or grant.expires_at > now)
        for grant in grants
    )


async def resolve_integration_access(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    user_id: str,
    action: ConnectionAction,
) -> IntegrationAccessDecision:
    """Authorize a source without the generic resource gateway's admin override."""
    source = await _load_source(db, kind=kind, connection_id=connection_id)
    if not source or (source.entity_id is not None and source.entity_id != entity_id):
        return IntegrationAccessDecision(False, "connection_not_found", kind)
    owner_role = await _active_entity_member_role(
        db, user_id=source.owner_user_id, entity_id=entity_id,
    )
    if owner_role is None:
        return IntegrationAccessDecision(False, "connection_not_found", kind)
    if source.owner_user_id == user_id:
        return IntegrationAccessDecision(
            True, "owner", kind, source.id, source.owner_user_id,
        )
    if (
        action in {"view", "use"}
        and owner_role in SHARING_MEMBER_ROLES
        and await _active_use_grant(
            db,
            resource_type=_resource_type(kind),
            resource_id=source.id,
            entity_id=entity_id,
            user_id=user_id,
        )
    ):
        return IntegrationAccessDecision(
            True, "shared_use", kind, source.id, source.owner_user_id,
        )
    reason = "connection_not_owned" if action in {"manage", "share", "bind_channel"} else "connection_not_shared"
    return IntegrationAccessDecision(False, reason, kind, source.id, source.owner_user_id)


async def _require_owned_source(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
) -> _ConnectionSource:
    # Every share write starts here; the row lock serializes them per connection.
    source = await _load_source(db, kind=kind, connection_id=connection_id, lock=True)
    if not source or source.owner_user_id != owner_user_id:
        raise ValueError("Connection not found")
    if source.entity_id is not None and source.entity_id != entity_id:
        raise ValueError("Connection not found")
    return source


async def _require_shareable_source(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
) -> _ConnectionSource:
    source = await _require_owned_source(
        db, kind=kind, connection_id=connection_id,
        entity_id=entity_id, owner_user_id=owner_user_id,
    )
    if canonical_provider_key(source.provider) == "chrome":
        raise ValueError("Chrome connections control the owner's browser and cannot be shared")
    owner_role = await _active_entity_member_role(db, user_id=owner_user_id, entity_id=entity_id)
    if owner_role is None:
        raise ValueError("Connection owner is not an active Entity member")
    if owner_role not in SHARING_MEMBER_ROLES:
        raise ValueError("Connection owner can no longer share connections in this Entity")
    return source


async def _active_grant(
    db: AsyncSession,
    *,
    entity_id: str,
    resource_type: str,
    resource_id: str,
    subject_type: str,
    subject_id: str,
) -> ResourceGrant | None:
    return (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.subject_type == subject_type,
            ResourceGrant.subject_id == subject_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
        ).order_by(ResourceGrant.granted_at.desc()).limit(1)
    )).scalar_one_or_none()


def _revoke(grant: ResourceGrant, *, revoked_by: str | None) -> None:
    grant.status = GrantStatus.REVOKED
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by = revoked_by


async def _upsert_use_grant(
    db: AsyncSession,
    *,
    entity_id: str,
    resource_type: str,
    resource_id: str,
    subject_type: str,
    subject_id: str,
    granted_by: str,
) -> ResourceGrant:
    existing = await _active_grant(
        db, entity_id=entity_id, resource_type=resource_type, resource_id=resource_id,
        subject_type=subject_type, subject_id=subject_id,
    )
    if existing:
        capabilities = set(existing.capabilities or [])
        capabilities.add(Capability.USE)
        existing.capabilities = sorted(capabilities)
        return existing
    grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=resource_type,
        resource_id=resource_id,
        subject_type=subject_type,
        subject_id=subject_id,
        capabilities=[Capability.USE],
        granted_by=granted_by,
        granted_at=datetime.now(UTC),
        status=GrantStatus.ACTIVE,
    )
    db.add(grant)
    await db.flush()
    return grant


async def grant_connection_use(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
    grantee_user_id: str,
) -> ResourceGrant:
    """Grant one active non-client Entity member use of an owned connection."""
    await _require_shareable_source(
        db, kind=kind, connection_id=connection_id,
        entity_id=entity_id, owner_user_id=owner_user_id,
    )
    if grantee_user_id == owner_user_id:
        raise ValueError("You already own this connection")
    role = await _active_entity_member_role(db, user_id=grantee_user_id, entity_id=entity_id)
    if role == "viewer":
        raise ValueError("Viewers cannot use shared accounts; change their role first")
    if role not in SHARE_RECIPIENT_ROLES:
        raise ValueError("Share recipient is not an active Entity member")
    return await _upsert_use_grant(
        db, entity_id=entity_id, resource_type=_resource_type(kind),
        resource_id=connection_id, subject_type=SubjectType.USER,
        subject_id=grantee_user_id, granted_by=owner_user_id,
    )


async def set_connection_entity_share(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
    enabled: bool,
) -> ResourceGrant | None:
    """Turn "every member can use this" on or off; named grants are untouched.

    Turning it off only needs ownership: taking access back stays possible
    after the owner lost the right to share.
    """
    require_source = _require_shareable_source if enabled else _require_owned_source
    await require_source(
        db, kind=kind, connection_id=connection_id,
        entity_id=entity_id, owner_user_id=owner_user_id,
    )
    resource_type = _resource_type(kind)
    if enabled:
        return await _upsert_use_grant(
            db, entity_id=entity_id, resource_type=resource_type,
            resource_id=connection_id, subject_type=SubjectType.ENTITY,
            subject_id=entity_id, granted_by=owner_user_id,
        )
    # No unique constraint backs the upsert, so concurrent enables can leave
    # several active rows; disabling must revoke all of them.
    existing = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.subject_type == SubjectType.ENTITY,
            ResourceGrant.subject_id == entity_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalars().all()
    for grant in existing:
        _revoke(grant, revoked_by=owner_user_id)
    if existing:
        await db.flush()
    return None


async def revoke_connection_use(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    grant_id: str,
    entity_id: str,
    owner_user_id: str,
) -> bool:
    """Revoke one recipient's access without changing another user's connection.

    Revokes every active grant that recipient holds on the connection, so
    duplicate rows (written before share writes were serialized) cannot keep
    access alive after the owner removes the person once.
    """
    source = await _load_source(db, kind=kind, connection_id=connection_id, lock=True)
    if not source or source.owner_user_id != owner_user_id:
        return False
    if source.entity_id is not None and source.entity_id != entity_id:
        return False

    scope = (
        ResourceGrant.entity_id == entity_id,
        ResourceGrant.resource_type == _resource_type(kind),
        ResourceGrant.resource_id == connection_id,
        ResourceGrant.subject_type.in_(_SHAREABLE_SUBJECT_TYPES),
        ResourceGrant.status == GrantStatus.ACTIVE,
    )
    grant = (await db.execute(
        select(ResourceGrant).where(ResourceGrant.id == grant_id, *scope)
    )).scalar_one_or_none()
    if not grant:
        return False

    same_subject = (await db.execute(
        select(ResourceGrant).where(
            *scope,
            ResourceGrant.subject_type == grant.subject_type,
            ResourceGrant.subject_id == grant.subject_id,
        )
    )).scalars().all()
    for row in same_subject:
        _revoke(row, revoked_by=owner_user_id)
    await db.flush()
    return True


async def revoke_all_connection_grants(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    revoked_by: str | None,
) -> int:
    """Revoke every active share of a connection that is being removed.

    Not Entity-filtered: an OAuth account carries no Entity, so its grants may
    have been issued in any Entity the owner shared it from.
    """
    grants = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.resource_type == _resource_type(kind),
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalars().all()
    for grant in grants:
        _revoke(grant, revoked_by=revoked_by)
    await db.flush()
    return len(grants)


@dataclass(frozen=True)
class ShareMember:
    user_id: str
    display_name: str | None
    email: str | None
    avatar_url: str | None = None


@dataclass(frozen=True)
class UserShare:
    grant_id: str
    member: ShareMember


@dataclass(frozen=True)
class ConnectionShareState:
    entity_grant_id: str | None
    users: tuple[UserShare, ...]
    candidates: tuple[ShareMember, ...]


@dataclass(frozen=True)
class ConnectionShareSummary:
    entity: bool
    user_count: int
    # The earliest few named recipients, for an avatar stack on the row.
    people: tuple[ShareMember, ...] = ()


SHARE_SUMMARY_PREVIEW = 3


async def _sharing_members(
    db: AsyncSession, *, entity_id: str,
) -> dict[str, tuple[ShareMember, str]]:
    """Active internal members with their role, for naming and candidacy."""
    rows = (await db.execute(
        select(
            User.id, User.display_name, User.email, User.avatar_url,
            staff_avatar_column(entity_id).label("staff_avatar_url"),
            UserMembership.role,
        )
        .join(UserMembership, UserMembership.user_id == User.id)
        .where(
            UserMembership.entity_id == entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
            UserMembership.role.in_(SHARING_MEMBER_ROLES),
            User.status == "active",
            User.deleted_at.is_(None),
        )
        .order_by(User.display_name.asc().nulls_last(), User.email.asc())
    )).all()
    return {
        row.id: (
            ShareMember(
                row.id, row.display_name, row.email,
                person_avatar_url(row.avatar_url, row.staff_avatar_url),
            ),
            row.role,
        )
        for row in rows
    }


async def connection_share_state(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
) -> ConnectionShareState:
    """Who can use an owned connection, and who could still be added.

    Callers must already have authorized ``owner_user_id`` as the owner.
    """
    grants = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == _resource_type(kind),
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.subject_type.in_(_SHAREABLE_SUBJECT_TYPES),
            ResourceGrant.status == GrantStatus.ACTIVE,
        ).order_by(ResourceGrant.granted_at.asc())
    )).scalars().all()
    members = await _sharing_members(db, entity_id=entity_id)
    # One entry per person even if duplicate active rows exist; removing that
    # entry revokes all of them (see revoke_connection_use).
    user_grants = list({
        grant.subject_id: grant
        for grant in reversed(grants)
        if grant.subject_type == SubjectType.USER
    }.values())[::-1]
    granted_ids = {grant.subject_id for grant in user_grants}
    return ConnectionShareState(
        entity_grant_id=next(
            (grant.id for grant in grants if grant.subject_type == SubjectType.ENTITY), None,
        ),
        users=tuple(
            UserShare(
                grant.id,
                members[grant.subject_id][0]
                if grant.subject_id in members
                else ShareMember(grant.subject_id, None, None),
            )
            for grant in user_grants
        ),
        candidates=tuple(
            member for user_id, (member, role) in members.items()
            if user_id != owner_user_id
            and user_id not in granted_ids
            and role in SHARE_RECIPIENT_ROLES
        ),
    )


async def connection_share_summaries(
    db: AsyncSession,
    *,
    entity_id: str,
    oauth_account_ids: list[str],
    integration_ids: list[str],
) -> dict[tuple[str, str], ConnectionShareSummary]:
    """Share badges for connections the caller owns, keyed by (kind, id)."""
    wanted = [
        (ResourceType.OAUTH_ACCOUNT, oauth_account_ids),
        (ResourceType.INTEGRATION, integration_ids),
    ]
    conditions = [
        and_(ResourceGrant.resource_type == resource_type, ResourceGrant.resource_id.in_(ids))
        for resource_type, ids in wanted if ids
    ]
    if not conditions:
        return {}
    grants = (await db.execute(
        select(
            ResourceGrant.resource_type, ResourceGrant.resource_id,
            ResourceGrant.subject_type, ResourceGrant.subject_id,
        ).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
            ResourceGrant.subject_type.in_(_SHAREABLE_SUBJECT_TYPES),
            or_(*conditions),
        ).order_by(ResourceGrant.granted_at.asc())
    )).all()
    kind_by_type = {ResourceType.OAUTH_ACCOUNT: "oauth_account", ResourceType.INTEGRATION: "integration"}
    entity: set[tuple[str, str]] = set()
    # Ordered by first grant; duplicate active rows for one user are possible
    # (no unique constraint), so count people, not rows.
    users: dict[tuple[str, str], dict[str, None]] = {}
    for grant in grants:
        key = (kind_by_type[grant.resource_type], grant.resource_id)
        if grant.subject_type == SubjectType.ENTITY:
            entity.add(key)
        else:
            users.setdefault(key, {})[grant.subject_id] = None
    preview_ids = {
        user_id for ids in users.values() for user_id in list(ids)[:SHARE_SUMMARY_PREVIEW]
    }
    members = {
        row.id: ShareMember(
            row.id, row.display_name, row.email,
            person_avatar_url(row.avatar_url, row.staff_avatar_url),
        )
        for row in (await db.execute(
            select(
                User.id, User.display_name, User.email, User.avatar_url,
                staff_avatar_column(entity_id).label("staff_avatar_url"),
            )
            .where(User.id.in_(preview_ids))
        )).all()
    } if preview_ids else {}
    return {
        key: ConnectionShareSummary(
            entity=key in entity,
            user_count=len(users.get(key, ())),
            people=tuple(
                members.get(user_id) or ShareMember(user_id, None, None)
                for user_id in list(users.get(key, ()))[:SHARE_SUMMARY_PREVIEW]
            ),
        )
        for key in entity | set(users)
    }


@dataclass(frozen=True)
class EntityConnectionShare:
    grant_id: str
    connection_kind: str
    connection_id: str
    provider: str
    connection_label: str
    owner_user_id: str
    owner_display_name: str | None
    owner_active: bool
    subject_type: str
    recipient_user_id: str | None
    recipient_display_name: str | None
    granted_at: datetime


def _connection_label(kind: str, row) -> str:
    from packages.core.services.integration_account_service import RuntimeIntegrationAccountFactory

    provider = canonical_provider_key(row.provider) or row.provider
    try:
        if kind == "oauth_account":
            return RuntimeIntegrationAccountFactory.from_oauth(
                row, actor_user_id=row.user_id, provider=provider,
            ).display_name
        return RuntimeIntegrationAccountFactory.from_entity(
            row, actor_user_id=row.owner_user_id or "", provider=provider,
        ).display_name
    except Exception:
        logger.debug("Connection label fell back to provider key", exc_info=True)
        return provider


async def list_entity_connection_shares(
    db: AsyncSession, *, entity_id: str,
) -> list[EntityConnectionShare]:
    """Every active connection share in an Entity, for admin review."""
    grants = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type.in_((ResourceType.INTEGRATION, ResourceType.OAUTH_ACCOUNT)),
            ResourceGrant.subject_type.in_(_SHAREABLE_SUBJECT_TYPES),
            ResourceGrant.status == GrantStatus.ACTIVE,
        ).order_by(ResourceGrant.granted_at.desc())
    )).scalars().all()
    integration_ids = {g.resource_id for g in grants if g.resource_type == ResourceType.INTEGRATION}
    oauth_ids = {g.resource_id for g in grants if g.resource_type == ResourceType.OAUTH_ACCOUNT}
    integrations = {
        row.id: row for row in (await db.execute(
            select(Integration).options(load_only(
                Integration.id, Integration.entity_id, Integration.owner_user_id,
                Integration.provider, Integration.config,
            )).where(Integration.id.in_(integration_ids))
        )).scalars().all()
    } if integration_ids else {}
    oauth_accounts = {
        row.id: row for row in (await db.execute(
            select(OAuthAccount).options(load_only(
                OAuthAccount.id, OAuthAccount.user_id, OAuthAccount.provider,
                OAuthAccount.provider_user_id, OAuthAccount.profile,
            )).where(OAuthAccount.id.in_(oauth_ids))
        )).scalars().all()
    } if oauth_ids else {}

    sources: dict[str, tuple[str, object, str]] = {}
    for row in integrations.values():
        if row.entity_id == entity_id and row.owner_user_id:
            sources[row.id] = ("integration", row, row.owner_user_id)
    for row in oauth_accounts.values():
        sources[row.id] = ("oauth_account", row, row.user_id)

    user_ids = {owner for _, _, owner in sources.values()} | {
        g.subject_id for g in grants if g.subject_type == SubjectType.USER
    }
    names = {
        row.id: row.display_name or row.email
        for row in (await db.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(user_ids))
        )).all()
    } if user_ids else {}
    owner_active: dict[str, bool] = {}
    shares: list[EntityConnectionShare] = []
    seen: set[tuple[str, str, str]] = set()
    for grant in grants:
        source = sources.get(grant.resource_id)
        if source is None:
            continue  # connection deleted before cascade revoke existed
        share_key = (grant.resource_id, grant.subject_type, grant.subject_id)
        if share_key in seen:
            continue  # duplicate rows of one share; revoking it clears them all
        seen.add(share_key)
        kind, row, owner_id = source
        if owner_id not in owner_active:
            # "Active" means the owner's shares still work, not mere membership.
            owner_active[owner_id] = (
                await _active_entity_member_role(db, user_id=owner_id, entity_id=entity_id)
                in SHARING_MEMBER_ROLES
            )
        is_user = grant.subject_type == SubjectType.USER
        shares.append(EntityConnectionShare(
            grant_id=grant.id,
            connection_kind=kind,
            connection_id=grant.resource_id,
            provider=canonical_provider_key(row.provider) or row.provider,
            connection_label=_connection_label(kind, row),
            owner_user_id=owner_id,
            owner_display_name=names.get(owner_id),
            owner_active=owner_active[owner_id],
            subject_type=grant.subject_type,
            recipient_user_id=grant.subject_id if is_user else None,
            recipient_display_name=names.get(grant.subject_id) if is_user else None,
            granted_at=grant.granted_at,
        ))
    return shares


async def admin_revoke_connection_share(
    db: AsyncSession, *, entity_id: str, grant_id: str, revoked_by: str,
) -> bool:
    grant = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.id == grant_id,
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type.in_((ResourceType.INTEGRATION, ResourceType.OAUTH_ACCOUNT)),
            ResourceGrant.subject_type.in_(_SHAREABLE_SUBJECT_TYPES),
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if grant is None:
        return False
    # Same rule as the owner's revoke: one share is one subject on one
    # connection, however many duplicate rows back it.
    same_subject = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == grant.resource_type,
            ResourceGrant.resource_id == grant.resource_id,
            ResourceGrant.subject_type == grant.subject_type,
            ResourceGrant.subject_id == grant.subject_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalars().all()
    for row in same_subject:
        _revoke(row, revoked_by=revoked_by)
    await db.flush()
    return True
