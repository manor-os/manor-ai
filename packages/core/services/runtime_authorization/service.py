"""Default infra authorization service and dependency factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.services.runtime_authorization.domain import (
    AuthorizationActor,
    AuthorizationRule,
    HitlAuthorizationRequest,
    PermissionDecision,
    RuntimeAuthorizationRequest,
)
from packages.core.services.runtime_authorization.binding_verifier import (
    CurrentRuntimeToolBindingVerifier,
)
from packages.core.services.runtime_authorization.interfaces import (
    AuthorizationService,
)
from packages.core.services.runtime_authorization.mappers import (
    RegistryEntityPermissionMapper,
    RegistryHitlAuthorityMapper,
    RegistryWorkspaceRequirementMapper,
)
from packages.core.services.runtime_authorization.strategies import (
    DelegatedRuntimeAuthorizationStrategy,
    DirectHitlAuthorizationStrategy,
    EntityRuntimeAuthorizationStrategy,
    ExternalRuntimeAuthorizationStrategy,
    HitlAuthorizationStrategyFactory,
    RuntimeAuthorizationStrategyFactory,
    SystemRuntimeAuthorizationStrategy,
    UnknownPrincipalRuntimeAuthorizationStrategy,
    WorkspaceGovernanceHitlAuthorizationStrategy,
    WorkspaceResponseHitlAuthorizationStrategy,
    WorkspaceRuntimeAuthorizationStrategy,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class DefaultAuthorizationService(AuthorizationService):
    def __init__(
        self,
        *,
        runtime_factory: RuntimeAuthorizationStrategyFactory,
        hitl_factory: HitlAuthorizationStrategyFactory,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._hitl_factory = hitl_factory

    async def authorize_runtime(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        if request.workspace_id and not await self._workspace_exists(db, request):
            return PermissionDecision.deny(
                "Workspace is outside the actor's entity or no longer exists.",
                AuthorizationRule.WORKSPACE_SCOPE,
            )
        strategy = self._runtime_factory.create(request)
        return await strategy.authorize(db, request)

    async def authorize_hitl(
        self,
        db: "AsyncSession",
        request: HitlAuthorizationRequest,
        *,
        by_user_id: str | None,
    ) -> PermissionDecision:
        if not by_user_id:
            return PermissionDecision.deny(
                "HITL resolution requires an authenticated user.",
                AuthorizationRule.HITL_ACTOR_REQUIRED,
            )
        if request.workspace_id and not await self._workspace_exists(db, request):
            return PermissionDecision.deny(
                "The HITL request is outside this entity's Workspace scope.",
                AuthorizationRule.HITL_WORKSPACE_SCOPE,
            )

        actor = await self._active_actor(
            db,
            entity_id=request.entity_id,
            user_id=by_user_id,
        )
        if actor is None:
            return PermissionDecision.deny(
                "The HITL actor is not an active member of this entity.",
                AuthorizationRule.HITL_ENTITY_MEMBERSHIP,
            )

        strategy = self._hitl_factory.create(request)
        return await strategy.authorize(db, request, actor)

    @staticmethod
    async def _workspace_exists(db: "AsyncSession", request) -> bool:
        from sqlalchemy import select

        from packages.core.models.workspace import Workspace

        workspace_id = getattr(request, "workspace_id", None)
        workspace_exists = (
            await db.execute(
                select(Workspace.id).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == request.entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return workspace_exists is not None

    @staticmethod
    async def _active_actor(
        db: "AsyncSession",
        *,
        entity_id: str,
        user_id: str,
    ) -> AuthorizationActor | None:
        from sqlalchemy import select

        from packages.core.models.user import User
        from packages.core.services.resource_access import resolve_user_role

        user = (
            await db.execute(
                select(User).where(
                    User.id == user_id,
                    User.status == "active",
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            return None
        role = await resolve_user_role(
            db,
            user_id=user_id,
            entity_id=entity_id,
        )
        if role is None:
            return None
        return AuthorizationActor(
            id=user.id,
            entity_id=entity_id,
            role=str(role),
        )


class AuthorizationServiceFactory:
    """Composition root for the default authorization implementation."""

    @staticmethod
    def create_default() -> AuthorizationService:
        entity_permissions = RegistryEntityPermissionMapper()
        workspace_requirements = RegistryWorkspaceRequirementMapper()
        hitl_authorities = RegistryHitlAuthorityMapper()
        binding_verifier = CurrentRuntimeToolBindingVerifier()
        return DefaultAuthorizationService(
            runtime_factory=RuntimeAuthorizationStrategyFactory(
                unknown=UnknownPrincipalRuntimeAuthorizationStrategy(),
                delegated=DelegatedRuntimeAuthorizationStrategy(
                    binding_verifier,
                    entity_permissions,
                ),
                system=SystemRuntimeAuthorizationStrategy(),
                external=ExternalRuntimeAuthorizationStrategy(entity_permissions),
                workspace=WorkspaceRuntimeAuthorizationStrategy(
                    workspace_requirements,
                    entity_permissions,
                    binding_verifier,
                ),
                entity=EntityRuntimeAuthorizationStrategy(entity_permissions),
            ),
            hitl_factory=HitlAuthorizationStrategyFactory(
                direct=DirectHitlAuthorizationStrategy(),
                workspace_response=WorkspaceResponseHitlAuthorizationStrategy(),
                workspace_governance=(WorkspaceGovernanceHitlAuthorizationStrategy(hitl_authorities)),
            ),
        )
