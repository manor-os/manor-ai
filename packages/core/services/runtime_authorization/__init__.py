"""Unified infra authorization facade.

Execution and HITL resolution share one :class:`AuthorizationService`.
Convenience functions preserve the original call surface while the service,
strategies, mappers, and factories remain replaceable in tests or deployments.
"""

from __future__ import annotations

from typing import Any, Iterable

from packages.core.services.runtime_authorization.domain import (
    AuthorizationOutcome,
    AuthorizationPrincipalKind,
    AuthorizationRule,
    HitlAuthorizationRequest,
    HitlRequestLike,
    PermissionDecision,
    RuntimeAuthorizationAccess,
    RuntimeAuthorizationRequest,
    RuntimeToolBindingDecision,
    RuntimeToolBindingSource,
)
from packages.core.services.runtime_authorization.interfaces import (
    AuthorizationService,
    EntityPermissionMapper,
    HitlAuthorityMapper,
    HitlAuthorizationStrategy,
    RuntimeAuthorizationStrategy,
    RuntimeToolBindingVerifier,
    WorkspaceRequirementMapper,
)
from packages.core.services.runtime_authorization.service import (
    AuthorizationServiceFactory,
)
from packages.core.services.runtime_authorization.strategies import (
    HitlAuthorizationStrategyFactory,
    RuntimeAuthorizationStrategyFactory,
)

_DEFAULT_SERVICE = AuthorizationServiceFactory.create_default()


def get_authorization_service() -> AuthorizationService:
    return _DEFAULT_SERVICE


async def authorize_runtime_action(
    db,
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    action_key: str | None,
    capability_id: str | None,
    access: RuntimeAuthorizationAccess | str | None = None,
    principal_kind: Any = None,
    principal_agent_id: str | None = None,
    principal_execution_user_id: str | None = None,
    tool_name: str | None = None,
    bound_tool_names: Iterable[str] | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    runtime_surface: Any = None,
) -> PermissionDecision:
    request = RuntimeAuthorizationRequest.create(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        action_key=action_key,
        capability_id=capability_id,
        access=access,
        principal_kind=principal_kind,
        principal_agent_id=principal_agent_id,
        principal_execution_user_id=principal_execution_user_id,
        tool_name=tool_name,
        bound_tool_names=bound_tool_names,
        conversation_id=conversation_id,
        task_id=task_id,
        runtime_surface=runtime_surface,
    )
    return await _DEFAULT_SERVICE.authorize_runtime(db, request)


async def authorize_hitl_resolution(
    db,
    *,
    request: HitlRequestLike,
    by_user_id: str | None,
    standing: bool = False,
) -> PermissionDecision:
    authorization_request = HitlAuthorizationRequest.from_request(
        request,
        standing=standing,
    )
    return await _DEFAULT_SERVICE.authorize_hitl(
        db,
        authorization_request,
        by_user_id=by_user_id,
    )


async def authorize_hitl_action(
    db,
    *,
    entity_id: str,
    workspace_id: str | None,
    by_user_id: str | None,
    action_key: str | None = None,
    capability_id: str | None = None,
    hitl_type: str | None = None,
    standing: bool = False,
    requested_by: str | None = None,
    origin_conversation_id: str | None = None,
) -> PermissionDecision:
    request = HitlAuthorizationRequest.create(
        entity_id=entity_id,
        workspace_id=workspace_id,
        action_key=action_key,
        capability_id=capability_id,
        hitl_type=hitl_type,
        standing=standing,
        requested_by=requested_by,
        origin_conversation_id=origin_conversation_id,
    )
    return await _DEFAULT_SERVICE.authorize_hitl(
        db,
        request,
        by_user_id=by_user_id,
    )


__all__ = [
    "AuthorizationOutcome",
    "AuthorizationPrincipalKind",
    "AuthorizationRule",
    "AuthorizationService",
    "AuthorizationServiceFactory",
    "EntityPermissionMapper",
    "HitlAuthorityMapper",
    "HitlAuthorizationRequest",
    "HitlAuthorizationStrategy",
    "HitlAuthorizationStrategyFactory",
    "HitlRequestLike",
    "PermissionDecision",
    "RuntimeAuthorizationRequest",
    "RuntimeAuthorizationAccess",
    "RuntimeAuthorizationStrategy",
    "RuntimeAuthorizationStrategyFactory",
    "RuntimeToolBindingDecision",
    "RuntimeToolBindingSource",
    "RuntimeToolBindingVerifier",
    "WorkspaceRequirementMapper",
    "authorize_hitl_action",
    "authorize_hitl_resolution",
    "authorize_runtime_action",
    "get_authorization_service",
]
