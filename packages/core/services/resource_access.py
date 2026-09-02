"""Unified resource authorization gateway.

Every first-class resource in Manor belongs to an ownership triple —
``(entity, owner user, workspace)`` — plus a ``visibility`` band. This module
is the single place that turns those four facts into an allow/deny decision,
so routers stop hand-rolling their own entity checks.

The decision order is fixed and identical for every resource type::

    entity mismatch          -> deny
    entity owner/admin       -> allow  (firm-wide override)
    resource owner           -> allow
    explicit ResourceGrant   -> allow if it carries the capability
    visibility band          -> private / workspace / entity / public

Reads and writes diverge only inside the visibility band: an ``entity``
resource is readable by the whole organization but writable only by its owner,
an entity admin, or — when it is scoped to a workspace — that workspace's
non-viewer members.

``document_access`` predates this module and keeps its own entrypoint because
documents layer extra rules on top (quarantine, classification, folder-path
inheritance, ``client_visible``). Those are document-specific concerns; the
skeleton here is the part every resource shares.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    SubjectType,
    Visibility,
)
from packages.core.models.staff import Staff
from packages.core.permissions import resolve_effective_user_role_name
from packages.core.models.workspace import WorkspaceStaff
from packages.core.services.workspace_access import (
    is_entity_admin_role,
    user_can_read_workspace_id,
    user_readable_workspace_ids,
    user_can_write_workspace_id,
)

# Roles that may read an entity-visible resource. Mirrors
# ``ENTITY_WORKSPACE_READ_ROLES``; ``client`` is deliberately excluded — client
# users only ever see resources explicitly marked for them.
ENTITY_READ_ROLES = {"owner", "admin", "member", "viewer"}

# Capabilities that only observe a resource. Everything else mutates it and
# therefore takes the stricter write path.
READ_CAPABILITIES = {Capability.VIEW, Capability.VIEW_REDACTED}


def is_read_capability(capability: str) -> bool:
    return capability in READ_CAPABILITIES


@dataclass(frozen=True)
class ResourceDescriptor:
    """The ownership facts the gateway needs, lifted off any resource row.

    ``owner_user_id`` and ``workspace_id`` are both optional: rows created
    before ownership columns existed have no owner, and a null workspace means
    the resource is shared entity-wide rather than scoped to one workspace.
    """

    resource_type: str
    resource_id: str
    entity_id: str
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: str = Visibility.ENTITY

    @classmethod
    def from_row(cls, row: object, resource_type: str) -> "ResourceDescriptor":
        """Build a descriptor from an ORM row using the conventional columns.

        Falls back to ``entity`` visibility when the column is absent or null,
        which keeps rows written before the ownership migration readable
        exactly as they are today.
        """
        return cls(
            resource_type=resource_type,
            resource_id=str(getattr(row, "id", "") or ""),
            entity_id=str(getattr(row, "entity_id", "") or ""),
            owner_user_id=_first_attr(row, "owner_user_id", "owner_id", "created_by"),
            workspace_id=_first_attr(row, "workspace_id"),
            visibility=str(getattr(row, "visibility", None) or Visibility.ENTITY),
        )


def _first_attr(row: object, *names: str) -> str | None:
    for name in names:
        value = getattr(row, name, None)
        if value:
            return str(value)
    return None


def _expires_after_now(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    now = datetime.now(UTC)
    if expires_at.tzinfo is None:
        return expires_at > now.replace(tzinfo=None)
    return expires_at > now


async def resolve_user_role(
    db: AsyncSession,
    *,
    user_id: str | None,
    entity_id: str,
    role: str | None = None,
) -> str | None:
    """Resolve the caller's authoritative role in this entity."""
    if not user_id:
        return role
    resolved_role = await resolve_effective_user_role_name(
        db,
        user_id=user_id,
        entity_id=entity_id,
        legacy_role=role,
    )
    return resolved_role or None


async def _user_subject_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
) -> set[str]:
    """IDs that may stand for this login in a ``subject_type=user`` grant.

    Older flows occasionally stored the linked ``Staff.id`` instead of the
    ``User.id``; include both so historical grants keep resolving.
    """
    ids = {user_id}
    staff_ids = (
        await db.execute(
            select(Staff.id).where(
                Staff.entity_id == entity_id,
                Staff.user_id == user_id,
                Staff.status == "active",
                Staff.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    ids.update(str(staff_id) for staff_id in staff_ids if staff_id)
    return ids


async def _workspace_role_subject_ids(
    db: AsyncSession,
    *,
    user_id: str,
) -> set[str]:
    """``{workspace_id}:{role}`` keys for every workspace the user staffs.

    Expired memberships are skipped: a time-limited seat must stop conferring
    grants the moment it lapses.
    """
    rows = (
        await db.execute(
            select(
                WorkspaceStaff.workspace_id,
                WorkspaceStaff.role,
                WorkspaceStaff.expires_at,
            ).where(
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
            )
        )
    ).all()
    subject_ids: set[str] = set()
    for workspace_id, role, expires_at in rows:
        if workspace_id and role and _expires_after_now(expires_at):
            subject_ids.add(
                f"{str(workspace_id).lower()}:{str(role).strip().lower()}"
            )
    return subject_ids


async def grant_capabilities_for_user(
    db: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    entity_id: str,
    user_id: str | None,
) -> set[str]:
    """Union of capabilities granted to this user on this resource.

    Considers ``user`` and ``workspace_role`` subjects; expired and revoked
    grants are ignored.
    """
    if not user_id or not resource_id:
        return set()

    user_ids = await _user_subject_ids(db, entity_id=entity_id, user_id=user_id)
    workspace_role_ids = await _workspace_role_subject_ids(db, user_id=user_id)
    if not user_ids and not workspace_role_ids:
        return set()

    rows = (
        await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.resource_type == resource_type,
                ResourceGrant.resource_id == resource_id,
                ResourceGrant.status == GrantStatus.ACTIVE,
            )
        )
    ).scalars().all()

    capabilities: set[str] = set()
    for grant in rows:
        if not _expires_after_now(grant.expires_at):
            continue
        subject_id = str(grant.subject_id or "")
        if grant.subject_type == SubjectType.USER:
            if subject_id not in user_ids:
                continue
        elif grant.subject_type == SubjectType.WORKSPACE_ROLE:
            if subject_id.strip().lower() not in workspace_role_ids:
                continue
        else:
            # staff_role / team / anonymous_link are resolved by their own
            # flows, not by a plain logged-in user check.
            continue
        capabilities.update(str(c) for c in (grant.capabilities or []))
    return capabilities


async def user_can_access_resource(
    db: AsyncSession,
    *,
    descriptor: ResourceDescriptor,
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
    capability: str = Capability.VIEW,
) -> bool:
    """The single authorization decision for a non-document resource."""
    if not descriptor.entity_id or descriptor.entity_id != entity_id:
        return False
    if not user_id:
        return False

    resolved_role = await resolve_user_role(
        db, user_id=user_id, entity_id=entity_id, role=role
    )
    return await _user_can_access_resource_with_resolved_role(
        db,
        descriptor=descriptor,
        entity_id=entity_id,
        user_id=user_id,
        resolved_role=resolved_role,
        capability=capability,
    )


async def _user_can_access_resource_with_resolved_role(
    db: AsyncSession,
    *,
    descriptor: ResourceDescriptor,
    entity_id: str,
    user_id: str | None,
    resolved_role: str | None,
    capability: str,
) -> bool:
    """Evaluate a resource after the caller role has been resolved once."""
    if not descriptor.entity_id or descriptor.entity_id != entity_id:
        return False
    if not user_id:
        return False

    if is_entity_admin_role(resolved_role):
        return True
    if descriptor.owner_user_id and descriptor.owner_user_id == user_id:
        return True

    granted = await grant_capabilities_for_user(
        db,
        resource_type=descriptor.resource_type,
        resource_id=descriptor.resource_id,
        entity_id=entity_id,
        user_id=user_id,
    )
    if capability in granted:
        return True

    reading = is_read_capability(capability)
    visibility = descriptor.visibility or Visibility.ENTITY

    if visibility == Visibility.PRIVATE:
        # Owner and admin already returned above.
        return False

    if visibility == Visibility.WORKSPACE:
        if not descriptor.workspace_id:
            # Claims workspace scope without naming one — refuse rather than
            # silently widening to the whole entity.
            return False
        if reading:
            return await user_can_read_workspace_id(
                db,
                workspace_id=descriptor.workspace_id,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
            )
        return await user_can_write_workspace_id(
            db,
            workspace_id=descriptor.workspace_id,
            entity_id=entity_id,
            user_id=user_id,
            role=resolved_role,
        )

    if visibility in (Visibility.ENTITY, Visibility.PUBLIC):
        if reading:
            if visibility == Visibility.PUBLIC:
                return True
            return str(resolved_role or "") in ENTITY_READ_ROLES
        # Writing an entity-wide resource is deliberately narrow: scoped to a
        # workspace it follows that workspace's write rule, otherwise only the
        # owner or an entity admin qualifies — both already returned above.
        if descriptor.workspace_id:
            return await user_can_write_workspace_id(
                db,
                workspace_id=descriptor.workspace_id,
                entity_id=entity_id,
                user_id=user_id,
                role=resolved_role,
            )
        return False

    return False


async def readable_resource_ids(
    db: AsyncSession,
    *,
    descriptors: list[ResourceDescriptor],
    entity_id: str,
    user_id: str | None,
    role: str | None = None,
) -> set[str]:
    """Subset of ``descriptors`` the user may view, resolved in batches.

    List surfaces can contain hundreds of Skills or Blueprints. Resolving role,
    Staff aliases, Workspace roles, grants, and Workspace membership once per
    resource turns that list into an authorization query storm. Keep the same
    decision order as ``user_can_access_resource`` while batching each shared
    input once.
    """
    candidates = [
        descriptor
        for descriptor in descriptors
        if descriptor.resource_id and descriptor.entity_id == entity_id
    ]
    if not candidates or not user_id:
        return set()

    resolved_role = await resolve_user_role(
        db, user_id=user_id, entity_id=entity_id, role=role
    )
    if is_entity_admin_role(resolved_role):
        return {descriptor.resource_id for descriptor in candidates}

    visible: set[str] = {
        descriptor.resource_id
        for descriptor in candidates
        if descriptor.owner_user_id == user_id
    }
    remaining = [
        descriptor
        for descriptor in candidates
        if descriptor.resource_id not in visible
    ]
    if not remaining:
        return visible

    user_ids = await _user_subject_ids(
        db,
        entity_id=entity_id,
        user_id=user_id,
    )
    workspace_role_ids = await _workspace_role_subject_ids(
        db,
        user_id=user_id,
    )
    subject_clauses = []
    if user_ids:
        subject_clauses.append(and_(
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.subject_id.in_(user_ids),
        ))
    if workspace_role_ids:
        subject_clauses.append(and_(
            ResourceGrant.subject_type == SubjectType.WORKSPACE_ROLE,
            func.lower(func.trim(ResourceGrant.subject_id)).in_(
                workspace_role_ids
            ),
        ))

    granted_pairs: set[tuple[str, str]] = set()
    if subject_clauses:
        resource_types = {descriptor.resource_type for descriptor in remaining}
        resource_ids = {descriptor.resource_id for descriptor in remaining}
        grants = (await db.execute(
            select(ResourceGrant).where(
                ResourceGrant.entity_id == entity_id,
                ResourceGrant.resource_type.in_(resource_types),
                ResourceGrant.resource_id.in_(resource_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                or_(*subject_clauses),
            )
        )).scalars().all()
        for grant in grants:
            if (
                _expires_after_now(grant.expires_at)
                and Capability.VIEW in (grant.capabilities or [])
            ):
                granted_pairs.add((grant.resource_type, grant.resource_id))

    workspace_ids = {
        str(descriptor.workspace_id)
        for descriptor in remaining
        if (
            (descriptor.visibility or Visibility.ENTITY)
            == Visibility.WORKSPACE
            and descriptor.workspace_id
            and (descriptor.resource_type, descriptor.resource_id)
            not in granted_pairs
        )
    }
    readable_workspaces = await user_readable_workspace_ids(
        db,
        entity_id=entity_id,
        user_id=user_id,
        role=resolved_role,
        workspace_ids=workspace_ids,
    ) if workspace_ids else set()

    for descriptor in remaining:
        if (descriptor.resource_type, descriptor.resource_id) in granted_pairs:
            visible.add(descriptor.resource_id)
            continue
        visibility = descriptor.visibility or Visibility.ENTITY
        if visibility == Visibility.PUBLIC:
            visible.add(descriptor.resource_id)
        elif (
            visibility == Visibility.ENTITY
            and str(resolved_role or "") in ENTITY_READ_ROLES
        ):
            visible.add(descriptor.resource_id)
        elif (
            visibility == Visibility.WORKSPACE
            and descriptor.workspace_id
            and str(descriptor.workspace_id) in readable_workspaces
        ):
            visible.add(descriptor.resource_id)
    return visible
