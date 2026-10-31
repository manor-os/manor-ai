"""Default infra authorization service and dependency factory."""

from __future__ import annotations

from dataclasses import replace
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
        if request.workspace_id:
            from packages.core.services.workspace_integration_scope import (
                integration_provider_for_tool,
                load_disabled_workspace_providers,
            )

            provider = integration_provider_for_tool(request.tool_name)
            if provider and provider in await load_disabled_workspace_providers(
                db, workspace_id=request.workspace_id, entity_id=request.entity_id,
            ):
                return PermissionDecision.deny(
                    "This integration is disabled for the current Workspace.",
                    AuthorizationRule.WORKSPACE_INTEGRATION_DISABLED,
                )
        # Before any strategy, including the system-worker one: a collaborator
        # reaches only allowlisted subjects inside a live-granted Workspace.
        # The denials above only narrow, so they may precede it.
        from packages.core.services.runtime_authorization.collaborator import (
            collaborator_runtime_gate,
        )

        roles: dict[str, str | None] = {}
        collaborator_scope, collaborator_denial = await collaborator_runtime_gate(db, request, roles)
        if collaborator_denial is not None:
            return collaborator_denial
        # Always set by the service, never trusted from the caller: a preset
        # value would skip the company-permission step of the strategies. The
        # gate's role resolution is reused by the strategies.
        user_key = str(request.user_id) if request.user_id else None
        request = replace(
            request,
            collaborator_workspace_ids=collaborator_scope,
            user_role_resolved=user_key in roles,
            resolved_user_role=roles.get(user_key) if user_key else None,
        )
        strategy = self._runtime_factory.create(request)
        decision = await strategy.authorize(db, request)
        if collaborator_scope is not None and decision.allowed:
            decision = replace(decision, collaborator_workspace_ids=collaborator_scope)
        return decision

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
        from packages.core.services.collaborator_scope import (
            actor_workspace_scope,
            collaborator_live_workspaces,
        )
        from packages.core.services.external_collaboration_access import COLLABORATOR_MEMBERSHIP_ROLE

        # The derived seat answers the strategies below; a collaborator first
        # needs a live grant on the card's own Workspace. The actor's raw
        # membership role (or the acting context) says who is one -- never a
        # StaffRole name -- without another query.
        collaborator_scope = await actor_workspace_scope(
            db, user_id=by_user_id, entity_id=request.entity_id,
        )
        if collaborator_scope is None and actor.membership_role == COLLABORATOR_MEMBERSHIP_ROLE:
            collaborator_scope = await collaborator_live_workspaces(
                db, user_id=by_user_id, entity_id=request.entity_id,
            ) or frozenset()
        if collaborator_scope is not None and (
            not request.workspace_id or request.workspace_id not in collaborator_scope
        ):
            return PermissionDecision.deny(
                "External collaborators resolve requests only in the Workspace their collaboration covers.",
                AuthorizationRule.COLLABORATOR_WORKSPACE,
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
        membership: dict = {}
        role = await resolve_user_role(
            db,
            user_id=user_id,
            entity_id=entity_id,
            membership_role=membership,
        )
        if role is None:
            return None
        return AuthorizationActor(
            id=user.id,
            entity_id=entity_id,
            role=str(role),
            membership_role=membership.get("role") or (
                str(role) if str(role) == "collaborator" else None
            ),
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
