"""Typed, fail-closed runtime tool-effect classification.

The execution boundary must distinguish an explicitly read-only tool from an
unclassified tool.  This module owns that domain contract and the strategy
composition; provider/tool-specific catalogs remain in ``approval_classifier``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from packages.core.ai.runtime.approvals import RuntimeApprovalAction
from packages.core.services.runtime_authorization.domain import (
    RuntimeAuthorizationAccess,
)


class RuntimeToolEffect(str, Enum):
    """The direct execution effect of one concrete runtime tool call."""

    READ_ONLY = "read_only"
    ORCHESTRATOR = "orchestrator"
    CONTROL = "control"
    ACTION = "action"
    UNKNOWN = "unknown"

    __str__ = str.__str__
    __format__ = str.__format__


class WorkspaceFileExistence(str, Enum):
    EXISTS = "exists"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeWorkspaceFileScope:
    """Exact physical Workspace artifact scope used during file classification."""

    existence: WorkspaceFileExistence
    artifact_base_dir: str | None = None


@dataclass(frozen=True)
class RuntimeToolAuthorization:
    """Hard-authorization subject carried by every executable effect."""

    action_key: str
    capability_id: str | None
    access: RuntimeAuthorizationAccess

    def __post_init__(self) -> None:
        if not str(self.action_key or "").strip():
            raise ValueError("Runtime tool authorization requires an action key")
        if not isinstance(self.access, RuntimeAuthorizationAccess):
            raise ValueError("Runtime tool authorization requires a typed access mode")


@dataclass(frozen=True)
class RuntimeToolClassification:
    """Closed result consumed by the runtime execution boundary."""

    effect: RuntimeToolEffect
    action: RuntimeApprovalAction | None = None
    authorization: RuntimeToolAuthorization | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect, RuntimeToolEffect):
            raise ValueError("Runtime tool classification requires a typed effect")
        if self.effect is RuntimeToolEffect.ACTION and self.action is None:
            raise ValueError("ACTION classification requires authorization metadata")
        if self.effect is not RuntimeToolEffect.ACTION and self.action is not None:
            raise ValueError("Only ACTION classification may carry authorization metadata")
        if self.effect in {
            RuntimeToolEffect.ORCHESTRATOR,
            RuntimeToolEffect.CONTROL,
            RuntimeToolEffect.UNKNOWN,
        } and not str(self.reason or "").strip():
            raise ValueError(f"{self.effect.value} classification requires a reason")
        if self.effect is RuntimeToolEffect.UNKNOWN and self.authorization is not None:
            raise ValueError("UNKNOWN classification cannot carry authorization metadata")
        if self.effect is not RuntimeToolEffect.UNKNOWN and self.authorization is None:
            raise ValueError("Executable classification requires hard-authorization metadata")
        if self.authorization is not None:
            expected_access = {
                RuntimeToolEffect.READ_ONLY: RuntimeAuthorizationAccess.READ,
                RuntimeToolEffect.ORCHESTRATOR: RuntimeAuthorizationAccess.USE,
                RuntimeToolEffect.CONTROL: RuntimeAuthorizationAccess.CONTROL,
                RuntimeToolEffect.ACTION: RuntimeAuthorizationAccess.ACTION,
            }.get(self.effect)
            if expected_access is not None and self.authorization.access is not expected_access:
                raise ValueError(
                    f"{self.effect.value} classification requires {expected_access.value} access"
                )
        if self.effect is RuntimeToolEffect.ACTION and self.authorization is not None:
            if self.action is not None and (
                self.authorization.action_key != self.action.action_key
                or self.authorization.capability_id != self.action.capability_id
            ):
                raise ValueError("ACTION authorization subject must match approval metadata")

    @classmethod
    def read_only(
        cls,
        authorization: RuntimeToolAuthorization,
    ) -> "RuntimeToolClassification":
        return cls(RuntimeToolEffect.READ_ONLY, authorization=authorization)

    @classmethod
    def action_call(
        cls,
        action: RuntimeApprovalAction,
    ) -> "RuntimeToolClassification":
        return cls(
            RuntimeToolEffect.ACTION,
            action=action,
            authorization=RuntimeToolAuthorization(
                action_key=action.action_key,
                capability_id=action.capability_id,
                access=RuntimeAuthorizationAccess.ACTION,
            ),
        )

    @classmethod
    def orchestrator(
        cls,
        reason: str,
        authorization: RuntimeToolAuthorization,
    ) -> "RuntimeToolClassification":
        return cls(
            RuntimeToolEffect.ORCHESTRATOR,
            authorization=authorization,
            reason=reason,
        )

    @classmethod
    def control(
        cls,
        reason: str,
        authorization: RuntimeToolAuthorization,
    ) -> "RuntimeToolClassification":
        return cls(
            RuntimeToolEffect.CONTROL,
            authorization=authorization,
            reason=reason,
        )

    @classmethod
    def unknown(cls, reason: str) -> "RuntimeToolClassification":
        return cls(RuntimeToolEffect.UNKNOWN, reason=reason)


@dataclass(frozen=True)
class RuntimeToolClassificationRequest:
    tool_name: str
    arguments: dict[str, Any]
    entity_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    workspace_file_scope: RuntimeWorkspaceFileScope | None = None

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        entity_id: str | None = None,
        workspace_id: str | None = None,
        task_id: str | None = None,
        workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
    ) -> "RuntimeToolClassificationRequest":
        return cls(
            tool_name=str(tool_name or "").strip(),
            arguments=dict(arguments or {}),
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            workspace_file_scope=workspace_file_scope,
        )


class RuntimeReadOnlyPredicate(Protocol):
    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> bool: ...


class RuntimeActionResolver(Protocol):
    def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        entity_id: str | None = None,
        workspace_id: str | None = None,
        task_id: str | None = None,
        workspace_file_scope: RuntimeWorkspaceFileScope | None = None,
    ) -> RuntimeApprovalAction | None: ...


class RuntimeAuthorizationResolver(Protocol):
    def __call__(
        self,
        request: RuntimeToolClassificationRequest,
        effect: RuntimeToolEffect,
    ) -> RuntimeToolAuthorization: ...


class RuntimeToolClassificationStrategy(ABC):
    """One ordered classifier in the runtime tool-effect chain."""

    @abstractmethod
    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification | None:
        """Return a classification when handled, otherwise defer."""


class RuntimeToolClassifier(ABC):
    """Stable interface used by execution callers."""

    @abstractmethod
    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification:
        """Return one exhaustive, fail-closed classification."""


class OrchestratorClassificationStrategy(RuntimeToolClassificationStrategy):
    def __init__(
        self,
        reasons_by_tool: Mapping[str, str],
        authorization_resolver: RuntimeAuthorizationResolver,
    ) -> None:
        self._reasons_by_tool = dict(reasons_by_tool)
        self._authorization_resolver = authorization_resolver

    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification | None:
        reason = self._reasons_by_tool.get(request.tool_name)
        if reason is None:
            return None
        return RuntimeToolClassification.orchestrator(
            reason,
            self._authorization_resolver(request, RuntimeToolEffect.ORCHESTRATOR),
        )


class ControlClassificationStrategy(RuntimeToolClassificationStrategy):
    def __init__(
        self,
        reasons_by_tool: Mapping[str, str],
        authorization_resolver: RuntimeAuthorizationResolver,
    ) -> None:
        self._reasons_by_tool = dict(reasons_by_tool)
        self._authorization_resolver = authorization_resolver

    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification | None:
        reason = self._reasons_by_tool.get(request.tool_name)
        if reason is None:
            return None
        return RuntimeToolClassification.control(
            reason,
            self._authorization_resolver(request, RuntimeToolEffect.CONTROL),
        )


class ReadOnlyClassificationStrategy(RuntimeToolClassificationStrategy):
    def __init__(
        self,
        predicate: RuntimeReadOnlyPredicate,
        authorization_resolver: RuntimeAuthorizationResolver,
    ) -> None:
        self._predicate = predicate
        self._authorization_resolver = authorization_resolver

    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification | None:
        if not self._predicate(request.tool_name, request.arguments):
            return None
        return RuntimeToolClassification.read_only(
            self._authorization_resolver(request, RuntimeToolEffect.READ_ONLY)
        )


class ActionClassificationStrategy(RuntimeToolClassificationStrategy):
    def __init__(self, resolver: RuntimeActionResolver) -> None:
        self._resolver = resolver

    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification | None:
        action = self._resolver(
            request.tool_name,
            request.arguments,
            entity_id=request.entity_id,
            workspace_id=request.workspace_id,
            task_id=request.task_id,
            workspace_file_scope=request.workspace_file_scope,
        )
        if action is None:
            return None
        return RuntimeToolClassification.action_call(action)


class OrderedRuntimeToolClassifier(RuntimeToolClassifier):
    """Composite classifier whose unmatched result is always UNKNOWN."""

    def __init__(self, strategies: Sequence[RuntimeToolClassificationStrategy]) -> None:
        self._strategies = tuple(strategies)

    def classify(
        self,
        request: RuntimeToolClassificationRequest,
    ) -> RuntimeToolClassification:
        if not request.tool_name:
            return RuntimeToolClassification.unknown("Runtime tool name is empty.")
        for strategy in self._strategies:
            classification = strategy.classify(request)
            if classification is not None:
                return classification
        return RuntimeToolClassification.unknown(
            f"Runtime tool {request.tool_name!r} has no declared "
            "execution-effect classification."
        )


class RuntimeToolClassifierFactory:
    """Composition root for the default ordered classification strategy."""

    @staticmethod
    def create_default(
        *,
        read_only_predicate: RuntimeReadOnlyPredicate,
        action_resolver: RuntimeActionResolver,
        authorization_resolver: RuntimeAuthorizationResolver,
        orchestrator_reasons: Mapping[str, str],
        control_reasons: Mapping[str, str] | None = None,
    ) -> RuntimeToolClassifier:
        return OrderedRuntimeToolClassifier((
            ControlClassificationStrategy(
                control_reasons or {},
                authorization_resolver,
            ),
            OrchestratorClassificationStrategy(
                orchestrator_reasons,
                authorization_resolver,
            ),
            ReadOnlyClassificationStrategy(
                read_only_predicate,
                authorization_resolver,
            ),
            ActionClassificationStrategy(action_resolver),
        ))


__all__ = [
    "ActionClassificationStrategy",
    "ControlClassificationStrategy",
    "OrderedRuntimeToolClassifier",
    "OrchestratorClassificationStrategy",
    "ReadOnlyClassificationStrategy",
    "RuntimeActionResolver",
    "RuntimeAuthorizationResolver",
    "RuntimeReadOnlyPredicate",
    "RuntimeToolClassification",
    "RuntimeToolAuthorization",
    "RuntimeToolClassificationRequest",
    "RuntimeToolClassificationStrategy",
    "RuntimeToolClassifier",
    "RuntimeToolClassifierFactory",
    "RuntimeToolEffect",
]
