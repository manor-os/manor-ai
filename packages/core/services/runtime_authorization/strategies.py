"""Concrete authorization strategies and their selection factories."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from packages.core.humans import participant_can
from packages.core.services.runtime_authorization.domain import (
    AuthorizationActor,
    AuthorizationPrincipalKind,
    AuthorizationRule,
    AuthorizationStrategyKind,
    HitlAuthorizationRequest,
    HitlAuthorizationScope,
    PermissionDecision,
    RuntimeAuthorizationAccess,
    RuntimeAuthorizationRequest,
    WorkspaceAccessRequirement,
)
from packages.core.services.runtime_authorization.interfaces import (
    EntityPermissionMapper,
    HitlAuthorityMapper,
    HitlAuthorizationStrategy,
    RuntimeAuthorizationStrategy,
    RuntimeToolBindingVerifier,
    WorkspaceRequirementMapper,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class DelegatedRuntimeAuthorizationStrategy(RuntimeAuthorizationStrategy):
    def __init__(
        self,
        binding_verifier: RuntimeToolBindingVerifier,
        permission_mapper: EntityPermissionMapper,
    ) -> None:
        self._binding_verifier = binding_verifier
        self._permission_mapper = permission_mapper

    async def authorize(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        if not request.principal_agent_id:
            return PermissionDecision.deny(
                "Delegated runtime action is missing an agent identity.",
                AuthorizationRule.AGENT_IDENTITY,
            )
        if not request.tool_name or request.tool_name not in request.bound_tool_names:
            return PermissionDecision.deny(
                "The delegated agent is not bound to this runtime tool.",
                AuthorizationRule.AGENT_TOOL_BINDING,
            )

        from packages.core.constants.agents import is_master_agent

        principal_is_master = is_master_agent(request.principal_agent_id)
        if not principal_is_master:
            from sqlalchemy import or_, select

            from packages.core.models.workspace import Agent

            active_agent = (
                await db.execute(
                    select(Agent.id).where(
                        Agent.id == request.principal_agent_id,
                        Agent.status == "active",
                        Agent.deleted_at.is_(None),
                        or_(
                            Agent.entity_id == request.entity_id,
                            Agent.entity_id.is_(None),
                        ),
                    )
                )
            ).scalar_one_or_none()
            if active_agent is None:
                return PermissionDecision.deny(
                    "The delegated agent is inactive or outside this entity.",
                    AuthorizationRule.AGENT_IDENTITY,
                )

        # Runtime-owned control tools are created for one concrete loop and
        # have no durable catalog binding. The envelope is sufficient only
        # after the non-master agent identity has been revalidated above.
        # A master Host normally keeps its broad first-party surface, but an
        # MCP call inside a Workspace/Task must still re-read the current
        # contextual provider scope contributed by service Agents.
        contextual_master_mcp = bool(
            principal_is_master
            and request.tool_name
            and request.tool_name.startswith("mcp__")
            and (request.workspace_id or request.task_id or request.conversation_id)
        )
        if (
            (not principal_is_master or contextual_master_mcp)
            and not (
                request.access is RuntimeAuthorizationAccess.CONTROL
                and request.tool_name == "submit_result"
            )
        ):
            binding = await self._binding_verifier.verify(db, request)
            if not binding.current:
                return PermissionDecision.deny(
                    "The delegated agent's current tool binding has been revoked.",
                    AuthorizationRule.AGENT_TOOL_BINDING,
                )

        if request.access in {
            RuntimeAuthorizationAccess.READ,
            RuntimeAuthorizationAccess.USE,
            RuntimeAuthorizationAccess.CONTROL,
        } and request.user_id:
            if (
                request.principal_execution_user_id
                and request.principal_execution_user_id != request.user_id
            ):
                return PermissionDecision.deny(
                    "Runtime actor does not match the execution user.",
                    AuthorizationRule.RUNTIME_IDENTITY_MISMATCH,
                )
            from packages.core.permissions import user_has_effective_permission
            from packages.core.services.resource_access import resolve_user_role

            role = await resolve_user_role(
                db,
                user_id=request.user_id,
                entity_id=request.entity_id,
            )
            if role is None:
                return PermissionDecision.deny(
                    "The execution user is not an active member of this entity.",
                    AuthorizationRule.ENTITY_MEMBERSHIP,
                )
            required = self._permission_mapper.permission_for(request.subject)
            allowed = await user_has_effective_permission(
                db,
                request.user_id,
                request.entity_id,
                str(role),
                required,
            )
            if not allowed:
                return PermissionDecision.deny(
                    f"This action requires {required.value}.",
                    required,
                )
            return PermissionDecision.allow(required)

        return PermissionDecision.allow(AuthorizationRule.AGENT_TOOL_BINDING)


class UnknownPrincipalRuntimeAuthorizationStrategy(RuntimeAuthorizationStrategy):
    async def authorize(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        del db, request
        return PermissionDecision.deny(
            "Runtime principal kind is not recognized.",
            AuthorizationRule.PRINCIPAL_KIND,
        )


class SystemRuntimeAuthorizationStrategy(RuntimeAuthorizationStrategy):
    async def authorize(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        del db, request
        return PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER)


class ExternalRuntimeAuthorizationStrategy(RuntimeAuthorizationStrategy):
    def __init__(self, permission_mapper: EntityPermissionMapper) -> None:
        self._permission_mapper = permission_mapper

    async def authorize(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        del db
        if not request.tool_name or request.tool_name not in request.bound_tool_names:
            return PermissionDecision.deny(
                "This external runtime is not bound to the requested tool.",
                AuthorizationRule.EXTERNAL_TOOL_BINDING,
            )
        if request.access in {
            RuntimeAuthorizationAccess.READ,
            RuntimeAuthorizationAccess.USE,
        }:
            return PermissionDecision.allow(AuthorizationRule.EXTERNAL_TOOL_BINDING)
        if request.subject.action_key == "channel.reply":
            return PermissionDecision.allow(AuthorizationRule.EXTERNAL_SUPPORT_REPLY)
        if request.workspace_id and request.subject.action_key == "workspace.task.create":
            return PermissionDecision.allow(AuthorizationRule.EXTERNAL_SUPPORT_TASK_INTAKE)
        if request.workspace_id:
            return PermissionDecision.deny(
                "External principals cannot mutate this workspace.",
                AuthorizationRule.WORKSPACE_EXTERNAL_WRITE,
            )
        required = self._permission_mapper.permission_for(request.subject)
        return PermissionDecision.deny(
            f"This action requires {required.value}.",
            required,
        )


class HumanRuntimeAuthorizationStrategy(RuntimeAuthorizationStrategy):
    """Template method for human strategies.

    Active membership and execution-identity checks are identical for entity
    and Workspace requests, so subclasses implement only their scope-specific
    permission decision.
    """

    async def authorize(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        if not request.user_id:
            return self.missing_actor_decision(request)

        from sqlalchemy import select

        from packages.core.models.user import User

        active_user = (
            await db.execute(
                select(User.id).where(
                    User.id == request.user_id,
                    User.status == "active",
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if active_user is None:
            return PermissionDecision.deny(
                "The runtime user is not active.",
                AuthorizationRule.ENTITY_MEMBERSHIP,
            )

        if (
            request.principal_kind is not None
            and request.principal_execution_user_id
            and str(request.principal_execution_user_id) != str(request.user_id)
        ):
            return PermissionDecision.deny(
                "Runtime actor does not match the execution user.",
                AuthorizationRule.RUNTIME_IDENTITY_MISMATCH,
            )

        from packages.core.services.resource_access import resolve_user_role

        role = await resolve_user_role(
            db,
            user_id=request.user_id,
            entity_id=request.entity_id,
        )
        if role is None:
            return PermissionDecision.deny(
                "The user is not an active member of this entity.",
                AuthorizationRule.ENTITY_MEMBERSHIP,
            )
        return await self.authorize_active_actor(db, request, str(role))

    @abstractmethod
    def missing_actor_decision(
        self,
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        """Return the scope-specific denial for an absent human actor."""

    @abstractmethod
    async def authorize_active_actor(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
        role: str,
    ) -> PermissionDecision:
        """Authorize an active entity member in the concrete scope."""


class WorkspaceRuntimeAuthorizationStrategy(HumanRuntimeAuthorizationStrategy):
    def __init__(
        self,
        requirement_mapper: WorkspaceRequirementMapper,
        permission_mapper: EntityPermissionMapper,
        binding_verifier: RuntimeToolBindingVerifier | None = None,
    ) -> None:
        self._requirement_mapper = requirement_mapper
        self._permission_mapper = permission_mapper
        if binding_verifier is None:
            from packages.core.services.runtime_authorization.binding_verifier import (
                CurrentRuntimeToolBindingVerifier,
            )

            binding_verifier = CurrentRuntimeToolBindingVerifier()
        self._binding_verifier = binding_verifier

    async def _mcp_binding_denial(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision | None:
        if not request.tool_name or not request.tool_name.startswith("mcp__"):
            return None
        if request.tool_name not in request.bound_tool_names:
            return PermissionDecision.deny(
                "This MCP action is outside the active Workspace runtime scope.",
                AuthorizationRule.AGENT_TOOL_BINDING,
            )
        binding = await self._binding_verifier.verify(db, request)
        if binding.current:
            return None
        return PermissionDecision.deny(
            "The Workspace's current MCP binding does not allow this action.",
            AuthorizationRule.AGENT_TOOL_BINDING,
        )

    def missing_actor_decision(
        self,
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        del request
        return PermissionDecision.deny(
            "Workspace access requires an authenticated user or delegated agent.",
            AuthorizationRule.WORKSPACE_ACTOR_REQUIRED,
        )

    async def authorize_active_actor(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
        role: str,
    ) -> PermissionDecision:
        from packages.core.services.workspace_access import (
            user_can_read_workspace_id,
            user_can_manage_workspace,
            user_can_write_workspace_artifacts,
        )

        if request.access in {
            RuntimeAuthorizationAccess.READ,
            RuntimeAuthorizationAccess.USE,
            RuntimeAuthorizationAccess.CONTROL,
        }:
            allowed = await user_can_read_workspace_id(
                db,
                workspace_id=str(request.workspace_id),
                entity_id=request.entity_id,
                user_id=request.user_id,
                role=role,
            )
            if not allowed:
                return PermissionDecision.deny(
                    "This action requires a readable Workspace role.",
                    AuthorizationRule.WORKSPACE_READ,
                )
            from packages.core.permissions import user_has_effective_permission

            required = self._permission_mapper.permission_for(request.subject)
            has_entity_permission = await user_has_effective_permission(
                db,
                str(request.user_id),
                request.entity_id,
                role,
                required,
            )
            if not has_entity_permission:
                return PermissionDecision.deny(
                    f"This action requires {required.value}.",
                    required,
                )
            if denial := await self._mcp_binding_denial(db, request):
                return denial
            return PermissionDecision.allow(required)

        requirement = self._requirement_mapper.requirement_for(request.subject)
        if requirement is WorkspaceAccessRequirement.MANAGE:
            allowed = await user_can_manage_workspace(
                db,
                workspace_id=str(request.workspace_id),
                user_id=request.user_id,
                entity_role=role,
            )
            if not allowed:
                return PermissionDecision.deny(
                    "This action requires Workspace owner authority.",
                    AuthorizationRule.WORKSPACE_MANAGE,
                )
            if denial := await self._mcp_binding_denial(db, request):
                return denial
            return PermissionDecision.allow(AuthorizationRule.WORKSPACE_MANAGE)

        allowed = await user_can_write_workspace_artifacts(
            db,
            workspace_id=str(request.workspace_id),
            user_id=request.user_id,
            entity_role=role,
        )
        if not allowed:
            return PermissionDecision.deny(
                "This action requires a writable Workspace role.",
                AuthorizationRule.WORKSPACE_WRITE,
            )
        if denial := await self._mcp_binding_denial(db, request):
            return denial
        return PermissionDecision.allow(AuthorizationRule.WORKSPACE_WRITE)


class EntityRuntimeAuthorizationStrategy(HumanRuntimeAuthorizationStrategy):
    def __init__(self, permission_mapper: EntityPermissionMapper) -> None:
        self._permission_mapper = permission_mapper

    def missing_actor_decision(
        self,
        request: RuntimeAuthorizationRequest,
    ) -> PermissionDecision:
        required = self._permission_mapper.permission_for(request.subject)
        return PermissionDecision.deny(
            f"This action requires {required.value}.",
            required,
        )

    async def authorize_active_actor(
        self,
        db: "AsyncSession",
        request: RuntimeAuthorizationRequest,
        role: str,
    ) -> PermissionDecision:
        from packages.core.permissions import user_has_effective_permission

        required = self._permission_mapper.permission_for(request.subject)
        allowed = await user_has_effective_permission(
            db,
            str(request.user_id),
            request.entity_id,
            role,
            required,
        )
        if not allowed:
            return PermissionDecision.deny(
                f"This action requires {required.value}.",
                required,
            )
        return PermissionDecision.allow(required)


class DirectHitlAuthorizationStrategy(HitlAuthorizationStrategy):
    async def authorize(
        self,
        db: "AsyncSession",
        request: HitlAuthorizationRequest,
        actor: AuthorizationActor,
    ) -> PermissionDecision:
        if request.requested_by:
            if request.requested_by == actor.id:
                return PermissionDecision.allow(AuthorizationRule.HITL_REQUEST_OWNER)
            return PermissionDecision.deny(
                "Only the user who requested this action may resolve it.",
                AuthorizationRule.HITL_REQUEST_OWNER,
            )

        if request.origin_conversation_id:
            from sqlalchemy import select

            from packages.core.models.task import Conversation

            conversation_owner = (
                await db.execute(
                    select(Conversation.user_id).where(
                        Conversation.id == request.origin_conversation_id,
                        Conversation.entity_id == request.entity_id,
                    )
                )
            ).scalar_one_or_none()
            if conversation_owner == actor.id:
                return PermissionDecision.allow(AuthorizationRule.HITL_CONVERSATION_OWNER)
            return PermissionDecision.deny(
                "Only the conversation owner may resolve this request.",
                AuthorizationRule.HITL_CONVERSATION_OWNER,
            )

        if actor.role.strip().lower() in {"owner", "admin"}:
            return PermissionDecision.allow(AuthorizationRule.HITL_ENTITY_ADMIN)
        return PermissionDecision.deny(
            "Entity administrator authority is required to resolve this request.",
            AuthorizationRule.HITL_ENTITY_ADMIN,
        )


class WorkspaceResponseHitlAuthorizationStrategy(HitlAuthorizationStrategy):
    async def authorize(
        self,
        db: "AsyncSession",
        request: HitlAuthorizationRequest,
        actor: AuthorizationActor,
    ) -> PermissionDecision:
        from packages.core.services.workspace_access import (
            user_can_write_workspace_artifacts,
        )

        allowed = await user_can_write_workspace_artifacts(
            db,
            workspace_id=str(request.workspace_id),
            user_id=actor.id,
            entity_role=actor.role,
        )
        if allowed:
            return PermissionDecision.allow(AuthorizationRule.HITL_WORKSPACE_RESPOND)
        return PermissionDecision.deny(
            "A writable Workspace role is required to answer this request.",
            AuthorizationRule.HITL_WORKSPACE_RESPOND,
        )


class WorkspaceGovernanceHitlAuthorizationStrategy(HitlAuthorizationStrategy):
    def __init__(self, authority_mapper: HitlAuthorityMapper) -> None:
        self._authority_mapper = authority_mapper

    async def authorize(
        self,
        db: "AsyncSession",
        request: HitlAuthorizationRequest,
        actor: AuthorizationActor,
    ) -> PermissionDecision:
        authority = self._authority_mapper.authority_for(
            request.subject,
            standing=request.standing,
        )
        allowed = await participant_can(
            db,
            user=actor,
            entity_id=request.entity_id,
            workspace_id=str(request.workspace_id),
            permission_key=authority,
        )
        matched_rule = f"permission.hitl.{authority.value}"
        if not allowed:
            return PermissionDecision.deny(
                f"This HITL decision requires {authority.value} authority.",
                matched_rule,
            )
        return PermissionDecision.allow(matched_rule)


class RuntimeAuthorizationStrategyFactory:
    def __init__(
        self,
        *,
        unknown: RuntimeAuthorizationStrategy,
        delegated: RuntimeAuthorizationStrategy,
        system: RuntimeAuthorizationStrategy,
        external: RuntimeAuthorizationStrategy,
        workspace: RuntimeAuthorizationStrategy,
        entity: RuntimeAuthorizationStrategy,
    ) -> None:
        self._strategies = {
            AuthorizationStrategyKind.UNKNOWN: unknown,
            AuthorizationStrategyKind.DELEGATED: delegated,
            AuthorizationStrategyKind.SYSTEM: system,
            AuthorizationStrategyKind.EXTERNAL: external,
            AuthorizationStrategyKind.WORKSPACE: workspace,
            AuthorizationStrategyKind.ENTITY: entity,
        }

    def create(
        self,
        request: RuntimeAuthorizationRequest,
    ) -> RuntimeAuthorizationStrategy:
        kind = request.principal_kind
        if request.principal_kind_supplied and kind is None:
            strategy_kind = AuthorizationStrategyKind.UNKNOWN
        elif kind in {
            AuthorizationPrincipalKind.AGENT,
            AuthorizationPrincipalKind.DELEGATED,
        }:
            strategy_kind = AuthorizationStrategyKind.DELEGATED
        elif kind is AuthorizationPrincipalKind.SYSTEM_WORKER:
            strategy_kind = AuthorizationStrategyKind.SYSTEM
        elif kind in {
            AuthorizationPrincipalKind.EXTERNAL_CONTACT,
            AuthorizationPrincipalKind.ANONYMOUS_PUBLIC,
        }:
            strategy_kind = AuthorizationStrategyKind.EXTERNAL
        elif request.workspace_id:
            strategy_kind = AuthorizationStrategyKind.WORKSPACE
        else:
            strategy_kind = AuthorizationStrategyKind.ENTITY
        return self._strategies[strategy_kind]


class HitlAuthorizationStrategyFactory:
    def __init__(
        self,
        *,
        direct: HitlAuthorizationStrategy,
        workspace_response: HitlAuthorizationStrategy,
        workspace_governance: HitlAuthorizationStrategy,
    ) -> None:
        self._strategies = {
            HitlAuthorizationScope.DIRECT: direct,
            HitlAuthorizationScope.WORKSPACE_RESPONSE: workspace_response,
            HitlAuthorizationScope.WORKSPACE_GOVERNANCE: workspace_governance,
        }

    def create(
        self,
        request: HitlAuthorizationRequest,
    ) -> HitlAuthorizationStrategy:
        if not request.workspace_id:
            scope = HitlAuthorizationScope.DIRECT
        elif request.is_governance:
            scope = HitlAuthorizationScope.WORKSPACE_GOVERNANCE
        else:
            scope = HitlAuthorizationScope.WORKSPACE_RESPONSE
        return self._strategies[scope]
