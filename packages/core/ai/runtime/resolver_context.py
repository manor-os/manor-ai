from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.core.ai.runtime.capabilities import (
    capability_ids_for_profile_tools,
    unclassified_tool_names_for_profile,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.file_context import (
    FILE_CONTEXT_METADATA_KEY,
    file_context_mounts_for_request,
    file_context_mounts_to_trace,
)
from packages.core.ai.runtime.legacy_tool_surface import (
    is_workspace_agent_legacy_profile,
    legacy_tool_surface_spec,
)
from packages.core.ai.runtime.memory import (
    MEMORY_MOUNTS_METADATA_KEY,
    memory_mounts_for_request,
    memory_mounts_to_trace,
)
from packages.core.ai.runtime.policies import filter_runtime_tools
from packages.core.ai.runtime.principals import RuntimePrincipal, resolve_runtime_principal
from packages.core.ai.runtime.profiles import RuntimeProfile, profile_for_surface
from packages.core.ai.runtime.requests import AIRuntimeRequest
from packages.core.ai.runtime.subagents import (
    SUBAGENTS_METADATA_KEY,
    subagent_specs_for_surface,
    subagent_specs_to_trace,
)
from packages.core.ai.runtime.tool_bindings import tool_bindings_for_profile_tools


def tool_name_from_schema(schema: dict[str, Any]) -> str | None:
    if not isinstance(schema, dict):
        return None
    return schema.get("function", {}).get("name") or schema.get("name")


def skill_label(ref: dict[str, Any]) -> str | None:
    return ref.get("slug") or ref.get("name") or ref.get("display_name") or ref.get("id")


@dataclass
class RuntimeResolverContext:
    request: AIRuntimeRequest
    legacy_runtime_profile: str | None = None
    tool_schemas: tuple[dict[str, Any], ...] = ()
    incoming_allowed_tool_names: tuple[str, ...] = ()
    incoming_blocked_tool_names: tuple[str, ...] = ()
    skill_refs: tuple[dict[str, Any], ...] = ()

    profile: RuntimeProfile | None = None
    principal: RuntimePrincipal | None = None
    tool_names: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()
    blocked_tool_names: tuple[str, ...] = ()
    capability_ids: tuple[str, ...] = ()
    tool_bindings: tuple[dict[str, Any], ...] = ()
    unclassified_tool_names: tuple[str, ...] = ()
    memory_mounts: tuple[str, ...] = ()
    file_context_mounts: tuple[str, ...] = ()
    subagent_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    applied_stage_names: list[str] = field(default_factory=list)

    @classmethod
    def from_inputs(
        cls,
        request: AIRuntimeRequest,
        *,
        legacy_runtime_profile: str | None = None,
        tool_schemas: Iterable[dict[str, Any]] | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        blocked_tool_names: Iterable[str] | None = None,
        skill_refs: Iterable[dict[str, Any]] | None = None,
    ) -> "RuntimeResolverContext":
        return cls(
            request=request,
            legacy_runtime_profile=legacy_runtime_profile,
            tool_schemas=tuple(tool_schemas or ()),
            incoming_allowed_tool_names=tuple(str(name) for name in (allowed_tool_names or ()) if name),
            incoming_blocked_tool_names=tuple(str(name) for name in (blocked_tool_names or ()) if name),
            skill_refs=tuple(skill_refs or ()),
            metadata=dict(request.metadata),
        )

    @property
    def effective_tool_name_set(self) -> set[str]:
        return set(self.allowed_tool_names or self.tool_names)

    def to_envelope(self) -> RuntimeEnvelope:
        profile = self.profile or profile_for_surface(self.request.surface)
        if self.principal is None:
            raise ValueError("RuntimeResolverContext principal was not resolved")
        return RuntimeEnvelope(
            surface=self.request.surface,
            principal=self.principal,
            profile=profile,
            legacy_runtime_profile=self.legacy_runtime_profile,
            entity_id=self.request.entity_id,
            user_id=self.request.user_id,
            agent_id=self.request.agent_id,
            workspace_id=self.request.workspace_id,
            conversation_id=self.request.conversation_id,
            task_id=self.request.task_id,
            thread_ref_kind=self.request.thread_ref_kind,
            thread_ref_id=self.request.thread_ref_id,
            tool_names=self.tool_names,
            allowed_tool_names=self.allowed_tool_names,
            blocked_tool_names=self.blocked_tool_names,
            capability_ids=self.capability_ids,
            tool_bindings=self.tool_bindings,
            unclassified_tool_names=self.unclassified_tool_names,
            skill_refs=tuple(
                sorted(label for label in (skill_label(ref) for ref in self.skill_refs) if label)
            ),
            memory_mounts=self.memory_mounts,
            file_context_mounts=self.file_context_mounts,
            subagent_names=self.subagent_names,
            metadata=self.metadata,
        )


class ResolverStage(Protocol):
    name: str

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        ...


@dataclass(frozen=True)
class SurfaceResolverStage:
    name: str = "surface"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        context.profile = profile_for_surface(context.request.surface)
        return context


@dataclass(frozen=True)
class PrincipalResolverStage:
    name: str = "principal"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        channel_context = (
            context.request.channel_context.as_dict()
            if context.request.channel_context
            else None
        )
        context.principal = resolve_runtime_principal(
            surface=context.request.surface,
            entity_id=context.request.entity_id,
            user_id=context.request.user_id,
            agent_id=context.request.agent_id,
            workspace_id=context.request.workspace_id,
            channel_context=channel_context,
            system_worker=bool(context.request.metadata.get("system_worker")),
        )
        return context


@dataclass(frozen=True)
class ToolResolverStage:
    name: str = "tool"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        profile = context.profile or profile_for_surface(context.request.surface)
        incoming_allowed = set(context.incoming_allowed_tool_names)
        filtered_tools, filtered_allowed, runtime_blocked = filter_runtime_tools(
            surface=context.request.surface,
            profile=profile,
            tools=context.tool_schemas,
            allowed_tool_names=incoming_allowed,
        )
        blocked = set(context.incoming_blocked_tool_names)
        blocked.update(runtime_blocked)
        if blocked:
            filtered_tools = [
                schema
                for schema in filtered_tools
                if tool_name_from_schema(schema) not in blocked
            ]
            filtered_allowed = set(filtered_allowed) - blocked
        context.tool_schemas = tuple(filtered_tools)
        context.tool_names = tuple(
            sorted(name for name in (tool_name_from_schema(schema) for schema in filtered_tools) if name)
        )
        context.allowed_tool_names = tuple(sorted(filtered_allowed))
        context.blocked_tool_names = tuple(sorted(blocked))
        return context


@dataclass(frozen=True)
class CapabilityResolverStage:
    name: str = "capability"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        profile = context.profile or profile_for_surface(context.request.surface)
        tool_names = context.effective_tool_name_set
        context.capability_ids = capability_ids_for_profile_tools(profile, tool_names)
        context.tool_bindings = tuple(
            binding.to_trace_dict()
            for binding in tool_bindings_for_profile_tools(profile, tool_names)
        )
        context.unclassified_tool_names = unclassified_tool_names_for_profile(profile, tool_names)
        if is_workspace_agent_legacy_profile(context.legacy_runtime_profile):
            legacy_surface = legacy_tool_surface_spec(
                is_master=True,
                legacy_tool_profile=context.legacy_runtime_profile,
            )
            context.metadata.setdefault(
                "legacy_tool_surface",
                {
                    "name": legacy_surface.name,
                    "source": legacy_surface.source,
                    "capability_ids": legacy_surface.capability_ids,
                    "contextual_capability_ids": legacy_surface.contextual_capability_ids,
                    "eager_tool_names": tuple(sorted(legacy_surface.eager_tool_names)),
                    "contextual_tool_names": tuple(sorted(legacy_surface.contextual_tool_names)),
                },
            )
        return context


@dataclass(frozen=True)
class MemoryResolverStage:
    name: str = "memory"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        mounts = memory_mounts_for_request(context.request)
        context.memory_mounts = tuple(mount.key for mount in mounts)
        if mounts:
            context.metadata[MEMORY_MOUNTS_METADATA_KEY] = memory_mounts_to_trace(mounts)
        return context


@dataclass(frozen=True)
class FileContextResolverStage:
    name: str = "file_context"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        mounts = file_context_mounts_for_request(context.request)
        context.file_context_mounts = tuple(mount.path for mount in mounts)
        if mounts:
            context.metadata[FILE_CONTEXT_METADATA_KEY] = file_context_mounts_to_trace(mounts)
        return context


@dataclass(frozen=True)
class SubAgentResolverStage:
    name: str = "subagent"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        profile = context.profile or profile_for_surface(context.request.surface)
        specs = subagent_specs_for_surface(
            surface=context.request.surface,
            profile=profile,
            workspace_id=context.request.workspace_id,
            memory_mounts=context.memory_mounts,
        )
        context.subagent_names = tuple(spec.name for spec in specs)
        if specs:
            context.metadata[SUBAGENTS_METADATA_KEY] = subagent_specs_to_trace(specs)
        return context


@dataclass(frozen=True)
class TraceResolverStage:
    name: str = "trace"

    def apply(self, context: RuntimeResolverContext) -> RuntimeResolverContext:
        return context


def default_resolver_stages() -> tuple[ResolverStage, ...]:
    return (
        SurfaceResolverStage(),
        PrincipalResolverStage(),
        ToolResolverStage(),
        CapabilityResolverStage(),
        MemoryResolverStage(),
        FileContextResolverStage(),
        SubAgentResolverStage(),
        TraceResolverStage(),
    )


def apply_resolver_stages(
    context: RuntimeResolverContext,
    stages: Iterable[ResolverStage],
) -> RuntimeResolverContext:
    for stage in stages:
        context = stage.apply(context)
        stage_name = str(getattr(stage, "name", "") or "").strip()
        if stage_name:
            context.applied_stage_names.append(stage_name)
    if context.applied_stage_names:
        context.metadata.setdefault(
            "runtime_resolver_middleware",
            tuple(context.applied_stage_names),
        )
    return context
