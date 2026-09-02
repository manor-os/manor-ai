"""Private credential-source authorization for end-user integrations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Integration
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.user import OAuthAccount, User, UserMembership

ConnectionKind = Literal["integration", "oauth_account"]
ConnectionAction = Literal["view", "use", "manage", "share", "bind_channel"]


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


def _resource_type(kind: ConnectionKind) -> str:
    if kind == "integration":
        return ResourceType.INTEGRATION
    if kind == "oauth_account":
        return ResourceType.OAUTH_ACCOUNT
    raise ValueError(f"Unsupported connection kind: {kind}")


async def _load_source(
    db: AsyncSession, *, kind: ConnectionKind, connection_id: str,
) -> _ConnectionSource | None:
    if kind == "integration":
        row = await db.get(Integration, connection_id)
        if not row:
            return None
        return _ConnectionSource(
            id=row.id,
            kind=kind,
            owner_user_id=row.owner_user_id,
            entity_id=row.entity_id,
        )
    if kind == "oauth_account":
        row = await db.get(OAuthAccount, connection_id)
        if not row:
            return None
        return _ConnectionSource(
            id=row.id,
            kind=kind,
            owner_user_id=row.user_id,
            entity_id=None,
        )
    raise ValueError(f"Unsupported connection kind: {kind}")


async def _is_active_entity_member(
    db: AsyncSession, *, user_id: str | None, entity_id: str,
) -> bool:
    if not user_id or not entity_id:
        return False
    membership = (await db.execute(
        select(UserMembership.status, UserMembership.deleted_at).where(
            UserMembership.user_id == user_id,
            UserMembership.entity_id == entity_id,
        ).limit(1)
    )).one_or_none()
    if membership is not None:
        if membership.status != "active" or membership.deleted_at is not None:
            return False
        active_user = (await db.execute(
            select(User.id).where(
                User.id == user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        return active_user is not None
    # Older primary-entity accounts predate UserMembership backfill. They are
    # still the current authenticated owner, but never grant cross-Entity use.
    # An explicit inactive/deleted membership is authoritative and must never
    # fall through to this compatibility path.
    legacy_owner = (await db.execute(
        select(User.id).where(
            User.id == user_id,
            User.entity_id == entity_id,
            User.status == "active",
            User.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    return legacy_owner is not None


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


async def _active_user_use_grant(
    db: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    entity_id: str,
    user_id: str,
) -> bool:
    now = datetime.now(UTC)
    grants = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.subject_id == user_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
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
    if not await _is_active_entity_member(
        db, user_id=source.owner_user_id, entity_id=entity_id,
    ):
        return IntegrationAccessDecision(False, "connection_not_found", kind)
    if source.owner_user_id == user_id:
        return IntegrationAccessDecision(
            True, "owner", kind, source.id, source.owner_user_id,
        )
    if action in {"view", "use"} and await _active_user_use_grant(
        db,
        resource_type=_resource_type(kind),
        resource_id=source.id,
        entity_id=entity_id,
        user_id=user_id,
    ):
        return IntegrationAccessDecision(
            True, "shared_use", kind, source.id, source.owner_user_id,
        )
    reason = "connection_not_owned" if action in {"manage", "share", "bind_channel"} else "connection_not_shared"
    return IntegrationAccessDecision(False, reason, kind, source.id, source.owner_user_id)


async def grant_connection_use(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    entity_id: str,
    owner_user_id: str,
    grantee_user_id: str,
) -> ResourceGrant:
    """Grant one active Entity member use of an owner-controlled connection."""
    source = await _load_source(db, kind=kind, connection_id=connection_id)
    if not source or source.owner_user_id != owner_user_id:
        raise ValueError("Connection not found")
    if source.entity_id is not None and source.entity_id != entity_id:
        raise ValueError("Connection not found")
    if not await _is_active_entity_member(db, user_id=owner_user_id, entity_id=entity_id):
        raise ValueError("Connection owner is not an active Entity member")
    if not await _is_active_entity_member(db, user_id=grantee_user_id, entity_id=entity_id):
        raise ValueError("Share recipient is not an active Entity member")

    resource_type = _resource_type(kind)
    existing = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.subject_id == grantee_user_id,
            ResourceGrant.status == GrantStatus.ACTIVE,
        ).order_by(ResourceGrant.granted_at.desc()).limit(1)
    )).scalar_one_or_none()
    if existing:
        capabilities = set(existing.capabilities or [])
        capabilities.add(Capability.USE)
        existing.capabilities = sorted(capabilities)
        return existing

    grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=resource_type,
        resource_id=connection_id,
        subject_type=SubjectType.USER,
        subject_id=grantee_user_id,
        capabilities=[Capability.USE],
        granted_by=owner_user_id,
        granted_at=datetime.now(UTC),
        status=GrantStatus.ACTIVE,
    )
    db.add(grant)
    await db.flush()
    return grant


async def revoke_connection_use(
    db: AsyncSession,
    *,
    kind: ConnectionKind,
    connection_id: str,
    grant_id: str,
    entity_id: str,
    owner_user_id: str,
) -> bool:
    """Revoke one use grant without changing another user's connection."""
    source = await _load_source(db, kind=kind, connection_id=connection_id)
    if not source or source.owner_user_id != owner_user_id:
        return False
    if source.entity_id is not None and source.entity_id != entity_id:
        return False

    grant = (await db.execute(
        select(ResourceGrant).where(
            ResourceGrant.id == grant_id,
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == _resource_type(kind),
            ResourceGrant.resource_id == connection_id,
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if not grant:
        return False

    grant.status = GrantStatus.REVOKED
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by = owner_user_id
    await db.flush()
    return True
