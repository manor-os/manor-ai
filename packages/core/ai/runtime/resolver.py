from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.middleware import (
    SyncRuntimeMiddleware,
    apply_runtime_middleware_sync,
)
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.resolver_context import (
    ResolverStage,
    RuntimeResolverContext,
    apply_resolver_stages,
    default_resolver_stages,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope

logger = logging.getLogger(__name__)


class RuntimeResolver:
    """Build a trace-only Manor runtime envelope from legacy resolution facts."""

    def __init__(
        self,
        *,
        middleware: Iterable[SyncRuntimeMiddleware] | None = None,
        resolver_stages: Iterable[ResolverStage] | None = None,
    ) -> None:
        self.middleware = tuple(middleware or ())
        self.resolver_stages = (
            tuple(resolver_stages)
            if resolver_stages is not None
            else default_resolver_stages()
        )

    def resolve_tool_surface(
        self,
        request: AIRuntimeRequest,
        *,
        legacy_runtime_profile: str | None = None,
        tool_schemas: Iterable[dict[str, Any]] | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        blocked_tool_names: Iterable[str] | None = None,
        skill_refs: Iterable[dict[str, Any]] | None = None,
    ) -> "ResolvedRuntimeToolSurface":
        context = self._resolve_context(
            request,
            legacy_runtime_profile=legacy_runtime_profile,
            tool_schemas=tool_schemas,
            allowed_tool_names=allowed_tool_names,
            blocked_tool_names=blocked_tool_names,
            skill_refs=skill_refs,
        )
        envelope = self._envelope_from_context(context)
        return ResolvedRuntimeToolSurface(
            envelope=envelope,
            tool_schemas=list(context.tool_schemas),
            allowed_tool_names=set(envelope.allowed_tool_names),
            blocked_tool_names=set(envelope.blocked_tool_names),
        )

    def resolve_trace_envelope(
        self,
        request: AIRuntimeRequest,
        *,
        legacy_runtime_profile: str | None = None,
        tool_schemas: Iterable[dict[str, Any]] | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        blocked_tool_names: Iterable[str] | None = None,
        skill_refs: Iterable[dict[str, Any]] | None = None,
    ) -> RuntimeEnvelope:
        return self._envelope_from_context(
            self._resolve_context(
                request,
                legacy_runtime_profile=legacy_runtime_profile,
                tool_schemas=tool_schemas,
                allowed_tool_names=allowed_tool_names,
                blocked_tool_names=blocked_tool_names,
                skill_refs=skill_refs,
            )
        )

    def _resolve_context(
        self,
        request: AIRuntimeRequest,
        *,
        legacy_runtime_profile: str | None = None,
        tool_schemas: Iterable[dict[str, Any]] | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        blocked_tool_names: Iterable[str] | None = None,
        skill_refs: Iterable[dict[str, Any]] | None = None,
    ) -> RuntimeResolverContext:
        context = RuntimeResolverContext.from_inputs(
            request,
            legacy_runtime_profile=legacy_runtime_profile,
            tool_schemas=tool_schemas,
            allowed_tool_names=allowed_tool_names,
            blocked_tool_names=blocked_tool_names,
            skill_refs=skill_refs,
        )
        return apply_resolver_stages(context, self.resolver_stages)

    def _envelope_from_context(self, context: RuntimeResolverContext) -> RuntimeEnvelope:
        envelope = context.to_envelope()
        if self.middleware:
            envelope = apply_runtime_middleware_sync(envelope, self.middleware)
        logger.debug("Resolved Manor AI runtime envelope: %s", envelope.to_trace_dict())
        return envelope


@dataclass(frozen=True)
class ResolvedRuntimeToolSurface:
    envelope: RuntimeEnvelope
    tool_schemas: list[dict[str, Any]]
    allowed_tool_names: set[str]
    blocked_tool_names: set[str]
