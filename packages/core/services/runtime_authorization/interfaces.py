"""Interfaces used by the authorization service and its factories."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.humans.authority import ParticipantAuthority
    from packages.core.permissions import Permission
    from packages.core.services.runtime_authorization.domain import (
        AuthorizationActor,
        AuthorizationSubject,
        HitlAuthorizationRequest,
        PermissionDecision,
        RuntimeAuthorizationRequest,
        RuntimeToolBindingDecision,
        WorkspaceAccessRequirement,
    )


class EntityPermissionMapper(ABC):
    @abstractmethod
    def permission_for(self, subject: "AuthorizationSubject") -> "Permission":
        """Return the tenant RBAC permission required by a subject."""


class HitlAuthorityMapper(ABC):
    @abstractmethod
    def authority_for(
        self,
        subject: "AuthorizationSubject",
        *,
        standing: bool,
    ) -> "ParticipantAuthority":
        """Return the human authority required to decide a HITL request."""


class WorkspaceRequirementMapper(ABC):
    @abstractmethod
    def requirement_for(
        self,
        subject: "AuthorizationSubject",
    ) -> "WorkspaceAccessRequirement":
        """Return write or manage scope for a Workspace mutation."""


class RuntimeAuthorizationStrategy(ABC):
    @abstractmethod
    async def authorize(
        self,
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> "PermissionDecision":
        """Authorize one runtime request inside a validated entity scope."""


class RuntimeToolBindingVerifier(ABC):
    @abstractmethod
    async def verify(
        self,
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> "RuntimeToolBindingDecision":
        """Revalidate one delegated tool binding against current storage."""


class HitlAuthorizationStrategy(ABC):
    @abstractmethod
    async def authorize(
        self,
        db: "AsyncSession",
        request: "HitlAuthorizationRequest",
        actor: "AuthorizationActor",
    ) -> "PermissionDecision":
        """Authorize a human verdict inside a validated actor/scope."""


class AuthorizationService(ABC):
    """Single infra interface shared by execution and HITL resolution."""

    @abstractmethod
    async def authorize_runtime(
        self,
        db: "AsyncSession",
        request: "RuntimeAuthorizationRequest",
    ) -> "PermissionDecision":
        """Authorize the operation before governance or HITL is evaluated."""

    @abstractmethod
    async def authorize_hitl(
        self,
        db: "AsyncSession",
        request: "HitlAuthorizationRequest",
        *,
        by_user_id: str | None,
    ) -> "PermissionDecision":
        """Authorize the person resolving an existing HITL request."""
