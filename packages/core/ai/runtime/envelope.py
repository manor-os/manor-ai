from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.services.sensitive_data import sanitize_sensitive_payload


@dataclass(frozen=True)
class RuntimeEnvelope:
    """Trace-level description of one Manor AI run."""

    surface: ChatSurface
    principal: RuntimePrincipal
    profile: RuntimeProfile
    legacy_runtime_profile: str | None = None
    entity_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    thread_ref_kind: str | None = None
    thread_ref_id: str | None = None
    tool_names: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()
    blocked_tool_names: tuple[str, ...] = ()
    capability_ids: tuple[str, ...] = ()
    tool_bindings: tuple[dict[str, Any], ...] = ()
    unclassified_tool_names: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    skill_descriptors: tuple[dict[str, Any], ...] = ()
    memory_mounts: tuple[str, ...] = ()
    file_context_mounts: tuple[str, ...] = ()
    subagent_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["surface"] = self.surface.value
        data["profile"] = self.profile.value
        data["principal"]["kind"] = self.principal.kind.value
        return data

    def to_message_meta(self) -> dict[str, Any]:
        data = self.to_trace_dict()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        data["metadata"] = sanitize_sensitive_payload({
            key: value
            for key, value in metadata.items()
            if key in {
                "legacy_path",
                "disable_tools",
                "legacy_tool_surface",
                "runtime_events",
                "runtime_attachment_context",
                "runtime_file_context_mounts",
                "runtime_memory_mounts",
                "runtime_middleware",
                "runtime_resolver_middleware",
                "runtime_skill_descriptors",
                "runtime_subagents",
            }
        })
        return data
