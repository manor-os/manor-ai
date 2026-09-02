"""Typed domain objects for infra authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Protocol, runtime_checkable

from packages.core.constants.approvals import HitlType, is_governance_hitl
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.permissions import Permission


class AuthorizationOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"

    __str__ = str.__str__
    __format__ = str.__format__


class AuthorizationRule(str, Enum):
    """Stable audit rule identifiers emitted by the infra boundary."""

    DENIED = "permission.denied"
    PRINCIPAL_KIND = "permission.principal.kind"
    ENTITY_MEMBERSHIP = "permission.entity.membership"
    RUNTIME_IDENTITY_MISMATCH = "permission.runtime.identity_mismatch"
    WORKSPACE_SCOPE = "permission.workspace.scope"
    WORKSPACE_ACTOR_REQUIRED = "permission.workspace.actor_required"
    WORKSPACE_EXTERNAL_WRITE = "permission.workspace.external_write"
    WORKSPACE_READ = "permission.workspace.read"
    WORKSPACE_MANAGE = "permission.workspace.manage"
    WORKSPACE_WRITE = "permission.workspace.write"
    AGENT_IDENTITY = "permission.agent.identity"
    AGENT_TOOL_BINDING = "permission.agent_tool_binding"
    EXTERNAL_TOOL_BINDING = "permission.external_tool_binding"
    EXTERNAL_SUPPORT_REPLY = "permission.external_support_reply"
    SYSTEM_WORKER = "permission.system_worker"
    DISPATCHER_WORKER = "permission.dispatcher_worker"
    STRATEGIST_PROPOSAL = "permission.strategist_proposal"
    EXTERNAL_SUPPORT_TASK_INTAKE = "permission.external_support_task_intake"
    HITL_ACTOR_REQUIRED = "permission.hitl.actor_required"
    HITL_WORKSPACE_SCOPE = "permission.hitl.workspace_scope"
    HITL_REQUEST_OWNER = "permission.hitl.request_owner"
    HITL_CONVERSATION_OWNER = "permission.hitl.conversation_owner"
    HITL_ENTITY_MEMBERSHIP = "permission.hitl.entity_membership"
    HITL_ENTITY_ADMIN = "permission.hitl.entity_admin"
    HITL_WORKSPACE_RESPOND = "permission.hitl.workspace_respond"

    __str__ = str.__str__
    __format__ = str.__format__


# Backward-compatible authorization spelling. It is an alias, not a second
# enum: runtime resolution and the infra gate share one closed vocabulary.
AuthorizationPrincipalKind = RuntimePrincipalKind


class AuthorizationStrategyKind(str, Enum):
    UNKNOWN = "unknown"
    DELEGATED = "delegated"
    SYSTEM = "system"
    EXTERNAL = "external"
    WORKSPACE = "workspace"
    ENTITY = "entity"


class RuntimeAuthorizationAccess(str, Enum):
    """What level of runtime access the concrete tool call needs.

    This is intentionally independent from approval risk.  Read/use/control
    calls still need an active identity and current tool binding, while only
    action calls continue into Workspace governance and HITL.
    """

    READ = "read"
    USE = "use"
    CONTROL = "control"
    ACTION = "action"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def parse(cls, value: Any) -> "RuntimeAuthorizationAccess":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value or cls.ACTION.value))
        except ValueError:
            return cls.ACTION


class RuntimeToolBindingSource(str, Enum):
    """Where the current delegated tool grant was verified."""

    DIRECT = "direct"
    MCP = "mcp"
    CONTEXTUAL = "contextual"
    REVOKED = "revoked"


@dataclass(frozen=True)
class RuntimeToolBindingDecision:
    """Fresh binding result used by delegated runtime authorization."""

    source: RuntimeToolBindingSource

    @property
    def current(self) -> bool:
        return self.source is not RuntimeToolBindingSource.REVOKED


class HitlAuthorizationScope(str, Enum):
    DIRECT = "direct"
    WORKSPACE_RESPONSE = "workspace_response"
    WORKSPACE_GOVERNANCE = "workspace_governance"


class WorkspaceAccessRequirement(str, Enum):
    WRITE = "write"
    MANAGE = "manage"


RuleReference = AuthorizationRule | Permission | str


def rule_value(rule: RuleReference) -> str:
    if isinstance(rule, Permission):
        return f"permission.{rule.value}"
    return str(getattr(rule, "value", rule))


@dataclass(frozen=True)
class PermissionDecision:
    """Non-approvable identity, role, and delegated-binding decision."""

    outcome: AuthorizationOutcome
    reason: str | None = None
    matched_rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is AuthorizationOutcome.ALLOW

    @classmethod
    def allow(cls, matched_rule: RuleReference | None = None) -> "PermissionDecision":
        return cls(
            AuthorizationOutcome.ALLOW,
            matched_rule=rule_value(matched_rule) if matched_rule is not None else None,
        )

    @classmethod
    def deny(cls, reason: str, matched_rule: RuleReference) -> "PermissionDecision":
        return cls(
            AuthorizationOutcome.DENY,
            reason=reason,
            matched_rule=rule_value(matched_rule),
        )


@dataclass(frozen=True)
class AuthorizationSubject:
    action_key: str = ""
    capability_id: str = ""

    @classmethod
    def create(
        cls,
        action_key: str | None,
        capability_id: str | None,
    ) -> "AuthorizationSubject":
        return cls(
            action_key=str(action_key or "").strip(),
            capability_id=str(capability_id or "").strip(),
        )


@dataclass(frozen=True)
class RuntimeAuthorizationRequest:
    entity_id: str
    user_id: str | None
    workspace_id: str | None
    subject: AuthorizationSubject
    access: RuntimeAuthorizationAccess = RuntimeAuthorizationAccess.ACTION
    principal_kind: AuthorizationPrincipalKind | None = None
    principal_kind_supplied: bool = False
    principal_agent_id: str | None = None
    principal_execution_user_id: str | None = None
    tool_name: str | None = None
    bound_tool_names: frozenset[str] = frozenset()
    conversation_id: str | None = None
    task_id: str | None = None
    runtime_surface: str | None = None

    @classmethod
    def create(
        cls,
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
    ) -> "RuntimeAuthorizationRequest":
        raw_principal_kind = getattr(principal_kind, "value", principal_kind)
        return cls(
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            subject=AuthorizationSubject.create(action_key, capability_id),
            access=RuntimeAuthorizationAccess.parse(access),
            principal_kind=AuthorizationPrincipalKind.parse(principal_kind),
            principal_kind_supplied=bool(str(raw_principal_kind or "").strip()),
            principal_agent_id=principal_agent_id,
            principal_execution_user_id=principal_execution_user_id,
            tool_name=str(tool_name or "").strip() or None,
            bound_tool_names=frozenset(
                str(value).strip() for value in (bound_tool_names or ()) if str(value or "").strip()
            ),
            conversation_id=str(conversation_id or "").strip() or None,
            task_id=str(task_id or "").strip() or None,
            runtime_surface=(
                str(getattr(runtime_surface, "value", runtime_surface) or "").strip() or None
            ),
        )


@runtime_checkable
class HitlRequestLike(Protocol):
    entity_id: str
    workspace_id: str | None
    action_key: str | None
    capability_id: str | None
    hitl_type: str
    context: dict
    origin_conversation_id: str | None


@dataclass(frozen=True)
class HitlAuthorizationRequest:
    entity_id: str
    workspace_id: str | None
    subject: AuthorizationSubject
    hitl_type: str
    requested_by: str | None = None
    origin_conversation_id: str | None = None
    standing: bool = False

    @property
    def is_governance(self) -> bool:
        return is_governance_hitl(self.hitl_type)

    @classmethod
    def from_request(
        cls,
        request: HitlRequestLike,
        *,
        standing: bool = False,
    ) -> "HitlAuthorizationRequest":
        context = getattr(request, "context", None) or {}
        return cls.create(
            entity_id=request.entity_id,
            workspace_id=request.workspace_id,
            action_key=getattr(request, "action_key", None),
            capability_id=getattr(request, "capability_id", None),
            hitl_type=getattr(request, "hitl_type", None),
            requested_by=context.get("requested_by"),
            origin_conversation_id=getattr(request, "origin_conversation_id", None),
            standing=standing,
        )

    @classmethod
    def create(
        cls,
        *,
        entity_id: str,
        workspace_id: str | None,
        action_key: str | None = None,
        capability_id: str | None = None,
        hitl_type: str | None = None,
        requested_by: str | None = None,
        origin_conversation_id: str | None = None,
        standing: bool = False,
    ) -> "HitlAuthorizationRequest":
        return cls(
            entity_id=entity_id,
            workspace_id=workspace_id,
            subject=AuthorizationSubject.create(action_key, capability_id),
            hitl_type=str(hitl_type or HitlType.AUTHORIZE.value),
            requested_by=str(requested_by or "").strip() or None,
            origin_conversation_id=(str(origin_conversation_id or "").strip() or None),
            standing=standing,
        )


@dataclass(frozen=True)
class AuthorizationActor:
    id: str
    entity_id: str
    role: str
