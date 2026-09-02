"""Shared policy and canonical mutation helpers for document ACL grants."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
)


_DOCUMENT_GRANT_CAPABILITIES = frozenset({
    Capability.VIEW,
    Capability.VIEW_REDACTED,
    Capability.COMMENT,
    Capability.EDIT,
    Capability.MANAGE_METADATA,
    Capability.SHARE_INTERNAL,
    Capability.SHARE_EXTERNAL,
    Capability.DOWNLOAD,
    Capability.PRINT,
    Capability.RECLASSIFY,
    Capability.DELETE,
    Capability.GRANT_ACCESS,
})
_FOLDER_GRANT_CAPABILITIES = frozenset({
    *_DOCUMENT_GRANT_CAPABILITIES,
    Capability.UPLOAD_TO,
})
_MANUAL_ACL_SOURCE = "manual_acl"
_MANUAL_ACL_SOURCES = {None, "", _MANUAL_ACL_SOURCE, "access_request"}


class ResourceGrantPolicyError(ValueError):
    """A requested grant violates the resource capability policy."""


@dataclass(frozen=True)
class ResourceGrantPolicy:
    resource_type: str
    allowed_capabilities: frozenset[str]

    def validate(
        self,
        capabilities: Iterable[str],
        *,
        grantor_capabilities: Iterable[str] | None = None,
        requested_capabilities: Iterable[str] | None = None,
    ) -> list[str]:
        normalized = list(dict.fromkeys(str(value).strip() for value in capabilities))
        if not normalized or any(not value for value in normalized):
            raise ResourceGrantPolicyError("No capabilities to grant")

        capability_set = set(normalized)
        unknown = capability_set - self.allowed_capabilities
        if unknown:
            raise ResourceGrantPolicyError(
                f"Unknown capabilities: {sorted(unknown)}"
            )

        if requested_capabilities is not None:
            requested = {str(value).strip() for value in requested_capabilities}
            unsupported_requested = requested - self.allowed_capabilities
            if unsupported_requested:
                raise ResourceGrantPolicyError(
                    "Requested capabilities contain unsupported values: "
                    f"{sorted(unsupported_requested)}"
                )
            expanded = capability_set - requested
            if expanded:
                raise ResourceGrantPolicyError(
                    "Approved capabilities must be a subset of the request: "
                    f"{sorted(expanded)}"
                )

        if grantor_capabilities is not None:
            delegated = capability_set - {
                str(value).strip() for value in grantor_capabilities
            }
            if delegated:
                raise ResourceGrantPolicyError(
                    "Delegated grants cannot exceed the grantor's capabilities: "
                    f"{sorted(delegated)}"
                )
        return normalized

    def delegated_expiry(
        self,
        capabilities: Iterable[str],
        *,
        grants: Iterable[ResourceGrant],
        expires_at: datetime | None,
        authority_capabilities: Iterable[str] = (Capability.SHARE_INTERNAL, Capability.GRANT_ACCESS),
        clamp_expiry: bool = False,
        implicit_capabilities: Iterable[str] = (),
    ) -> datetime | None:
        """Bound every delegated capability by its live authority's lifetime.

        Independent grants are alternatives (latest expiry wins); requested
        capabilities and the right to delegate are requirements (earliest wins).
        An omitted expiry inherits the bound, never turns temporary access into
        permanent access. Callers hold the resource/ancestor policy locks.
        Implicit capabilities must be resolved independently of ResourceGrants;
        they have no grant expiry but cannot replace the right to delegate.
        """
        now = datetime.now(timezone.utc)
        forever = datetime.max.replace(tzinfo=timezone.utc)
        horizons: dict[str, datetime] = {}
        for grant in grants:
            expiry = grant.expires_at or forever
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if grant.status != GrantStatus.ACTIVE or expiry <= now:
                continue
            for capability in grant.capabilities or []:
                horizons[capability] = max(horizons.get(capability, now), expiry)
        delegation = max(horizons.get(cap, now) for cap in authority_capabilities)
        for capability in implicit_capabilities:
            horizons[capability] = forever
        bound = min(delegation, *(horizons.get(cap, now) for cap in capabilities))
        if bound <= now:
            raise ResourceGrantPolicyError("Delegated capabilities require live grant authority")
        requested = expires_at
        if requested is not None and requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        if requested is not None and requested > bound:
            if not clamp_expiry:
                raise ResourceGrantPolicyError("Delegated grants cannot outlive the grantor's access")
            requested = bound
        return requested if requested is not None else (None if bound == forever else bound)


class ResourceGrantPolicyFactory:
    """Resolve the canonical capability policy for a resource enum value."""

    @staticmethod
    def create(resource_type: str) -> ResourceGrantPolicy:
        policies = {
            ResourceType.DOCUMENT: ResourceGrantPolicy(
                resource_type=ResourceType.DOCUMENT,
                allowed_capabilities=_DOCUMENT_GRANT_CAPABILITIES,
            ),
            ResourceType.DOCUMENT_FOLDER: ResourceGrantPolicy(
                resource_type=ResourceType.DOCUMENT_FOLDER,
                allowed_capabilities=_FOLDER_GRANT_CAPABILITIES,
            ),
        }
        try:
            return policies[resource_type]
        except KeyError as exc:
            raise ResourceGrantPolicyError(
                f"Unsupported grant resource type: {resource_type}"
            ) from exc


def _grant_source(grant: ResourceGrant) -> str | None:
    metadata = grant.metadata_ if isinstance(grant.metadata_, dict) else {}
    value = metadata.get("source")
    return str(value).strip() if value is not None else None


def _is_manual_acl_grant(grant: ResourceGrant) -> bool:
    return _grant_source(grant) in _MANUAL_ACL_SOURCES


async def upsert_manual_resource_grant(
    db: AsyncSession,
    *,
    entity_id: str,
    resource_type: str,
    resource_id: str,
    subject_type: str,
    subject_id: str,
    equivalent_subject_ids: Iterable[str] = (),
    capabilities: list[str],
    granted_by: str,
    expires_at: datetime | None,
    merge_capabilities: bool = False,
    metadata: dict | None = None,
) -> ResourceGrant:
    """Upsert one manual ACL row while preserving source-owned grants.

    The caller must first lock the owning Document or DocumentFolder row. That
    resource lock serializes direct grants and access-request approvals without
    collapsing independent system grants that intentionally share a subject.
    """

    subject_ids = list(dict.fromkeys([subject_id, *equivalent_subject_ids]))
    rows = list((await db.execute(
        select(ResourceGrant)
        .where(
            ResourceGrant.entity_id == entity_id,
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.subject_type == subject_type,
            ResourceGrant.subject_id.in_(subject_ids),
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
        .order_by(desc(ResourceGrant.granted_at), desc(ResourceGrant.id))
        .with_for_update()
    )).scalars().all())
    manual_rows = [row for row in rows if _is_manual_acl_grant(row)]
    now = datetime.now(timezone.utc)
    candidate_rows = (
        [row for row in manual_rows if row.expires_at == expires_at]
        if merge_capabilities
        else manual_rows
    )
    candidate_rows.sort(
        key=lambda row: row.expires_at is None or row.expires_at > now,
        reverse=True,
    )

    if candidate_rows:
        grant = candidate_rows[0]
        if merge_capabilities:
            capabilities = list(dict.fromkeys([
                *(grant.capabilities or []),
                *capabilities,
            ]))
        grant.subject_id = subject_id
        grant.capabilities = capabilities
        grant.expires_at = expires_at
        grant.granted_by = granted_by
        grant.granted_at = now
        duplicates = candidate_rows[1:] if merge_capabilities else manual_rows[1:]
        for duplicate in duplicates:
            duplicate.status = GrantStatus.REVOKED
            duplicate.revoked_at = now
            duplicate.revoked_by = granted_by
    else:
        grant = ResourceGrant(
            id=generate_ulid(),
            entity_id=entity_id,
            resource_type=resource_type,
            resource_id=resource_id,
            subject_type=subject_type,
            subject_id=subject_id,
            capabilities=capabilities,
            granted_by=granted_by,
            granted_at=now,
            expires_at=expires_at,
            status=GrantStatus.ACTIVE,
        )
        db.add(grant)

    next_metadata = dict(grant.metadata_ or {})
    next_metadata["source"] = _MANUAL_ACL_SOURCE
    if metadata:
        next_metadata.update(metadata)
    grant.metadata_ = next_metadata
    await db.flush()
    return grant


async def revoke_resource_grant_family(
    db: AsyncSession,
    *,
    target: ResourceGrant,
    equivalent_subject_ids: Iterable[str] = (),
    revoked_by: str,
) -> None:
    """Revoke the target and any duplicate rows in its manual ACL family."""

    rows = [target]
    if _is_manual_acl_grant(target):
        subject_ids = list(dict.fromkeys([
            target.subject_id,
            *equivalent_subject_ids,
        ]))
        expiry_filter = (
            ResourceGrant.expires_at.is_(None)
            if target.expires_at is None
            else ResourceGrant.expires_at == target.expires_at
        )
        rows = list((await db.execute(
            select(ResourceGrant)
            .where(
                ResourceGrant.entity_id == target.entity_id,
                ResourceGrant.resource_type == target.resource_type,
                ResourceGrant.resource_id == target.resource_id,
                ResourceGrant.subject_type == target.subject_type,
                ResourceGrant.subject_id.in_(subject_ids),
                ResourceGrant.status == GrantStatus.ACTIVE,
                expiry_filter,
            )
            .with_for_update()
        )).scalars().all())
        rows = [row for row in rows if _is_manual_acl_grant(row)]

    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = GrantStatus.REVOKED
        row.revoked_at = now
        row.revoked_by = revoked_by
    await db.flush()


__all__ = [
    "ResourceGrantPolicy",
    "ResourceGrantPolicyError",
    "ResourceGrantPolicyFactory",
    "revoke_resource_grant_family",
    "upsert_manual_resource_grant",
]
