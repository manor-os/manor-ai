from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface


SUBAGENTS_METADATA_KEY = "runtime_subagents"


@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    purpose: str
    profile: RuntimeProfile
    allowed_tools: tuple[str, ...] = ()
    memory_mounts: tuple[str, ...] = ()
    max_steps: int = 12
    max_cost_credits: int | None = None
    output_contract: dict = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "profile": self.profile.value,
            "allowed_tools": self.allowed_tools,
            "memory_mounts": self.memory_mounts,
            "max_steps": self.max_steps,
            "max_cost_credits": self.max_cost_credits,
            "output_contract": dict(self.output_contract or {}),
        }


@dataclass(frozen=True)
class RuntimeSubAgentDecision:
    allowed: bool
    spec: SubAgentSpec | None = None
    code: str | None = None
    reason: str | None = None


_SUBAGENT_DENIED_PROFILES = {
    RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
    RuntimeProfile.EXTERNAL_CHANNEL_SAFE,
    RuntimeProfile.FILE_EDITOR_PATCH,
    RuntimeProfile.VOICE_SAFE,
}


def subagent_specs_for_surface(
    *,
    surface: ChatSurface,
    profile: RuntimeProfile,
    workspace_id: str | None = None,
    memory_mounts: tuple[str, ...] = (),
) -> tuple[SubAgentSpec, ...]:
    if profile in _SUBAGENT_DENIED_PROFILES:
        return ()

    workspace_memory_mounts = tuple(
        mount for mount in memory_mounts if str(mount or "").startswith("workspace:")
    )

    if surface == ChatSurface.WORKSPACE_DRAFT_ARCHITECT:
        return (
            SubAgentSpec(
                name="workspace_architect_reviewer",
                purpose="Review workspace architecture drafts and evaluate capability bindings.",
                profile=RuntimeProfile.WORKSPACE_ARCHITECT,
                allowed_tools=("workspace_search", "search_tools"),
                memory_mounts=workspace_memory_mounts,
                max_steps=8,
                max_cost_credits=6,
                output_contract={
                    "format": "structured_review",
                    "required_fields": ("risks", "missing_capabilities", "recommendations"),
                },
            ),
        )

    if surface == ChatSurface.WORKSPACE_CHAT and workspace_id:
        return (
            SubAgentSpec(
                name="workspace_strategist",
                purpose="Do bounded workspace strategy, research, and planning analysis.",
                profile=RuntimeProfile.WORKSPACE_OPERATOR,
                allowed_tools=("workspace_search", "web_search", "web_fetch", "search_tools"),
                memory_mounts=workspace_memory_mounts,
                max_steps=10,
                max_cost_credits=8,
                output_contract={
                    "format": "strategy_brief",
                    "required_fields": ("findings", "recommendations", "open_questions"),
                },
            ),
        )

    if surface in {
        ChatSurface.WORKFLOW_AGENT_STEP,
        ChatSurface.SCHEDULED_AGENT_RUN,
    }:
        return (
            SubAgentSpec(
                name="long_research_worker",
                purpose="Perform bounded background research for a workflow or scheduled run.",
                profile=profile,
                allowed_tools=("workspace_search", "web_search", "web_fetch", "search_tools"),
                memory_mounts=workspace_memory_mounts,
                max_steps=12,
                max_cost_credits=10,
                output_contract={
                    "format": "research_notes",
                    "required_fields": ("summary", "evidence", "next_actions"),
                },
            ),
        )

    return ()


def subagent_specs_to_trace(specs: tuple[SubAgentSpec, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(spec.to_trace_dict() for spec in specs)


def _subagent_spec_from_trace(data: dict[str, Any]) -> SubAgentSpec | None:
    try:
        profile = data.get("profile")
        if not isinstance(profile, RuntimeProfile):
            profile = RuntimeProfile(str(profile))
        return SubAgentSpec(
            name=str(data.get("name") or ""),
            purpose=str(data.get("purpose") or ""),
            profile=profile,
            allowed_tools=tuple(str(v) for v in (data.get("allowed_tools") or ()) if str(v or "").strip()),
            memory_mounts=tuple(str(v) for v in (data.get("memory_mounts") or ()) if str(v or "").strip()),
            max_steps=int(data.get("max_steps") or 12),
            max_cost_credits=data.get("max_cost_credits"),
            output_contract=dict(data.get("output_contract") or {}),
        )
    except Exception:
        return None


def runtime_subagent_specs_from_envelope(envelope) -> tuple[SubAgentSpec, ...]:
    if envelope is None:
        return ()
    metadata = getattr(envelope, "metadata", None) or {}
    raw_specs = metadata.get(SUBAGENTS_METADATA_KEY) or ()
    specs: list[SubAgentSpec] = []
    for item in raw_specs:
        if isinstance(item, dict):
            spec = _subagent_spec_from_trace(item)
            if spec and spec.name:
                specs.append(spec)
    return tuple(specs)


def runtime_select_subagent(
    envelope,
    *,
    name: str | None = None,
) -> RuntimeSubAgentDecision:
    if envelope is None:
        return RuntimeSubAgentDecision(
            allowed=False,
            code="runtime_envelope_missing",
            reason="Subagent execution requires a RuntimeEnvelope.",
        )
    if not runtime_allows_subagents(envelope):
        return RuntimeSubAgentDecision(
            allowed=False,
            code="subagent_not_allowed",
            reason="This runtime profile does not allow subagent execution.",
        )
    specs = runtime_subagent_specs_from_envelope(envelope)
    if not specs:
        return RuntimeSubAgentDecision(
            allowed=False,
            code="subagent_spec_missing",
            reason="No Runtime SubAgentSpec is available on this envelope.",
        )
    requested = str(name or "").strip()
    if requested:
        for spec in specs:
            if spec.name == requested:
                return RuntimeSubAgentDecision(allowed=True, spec=spec)
        return RuntimeSubAgentDecision(
            allowed=False,
            code="subagent_not_visible",
            reason=f"Subagent `{requested}` is not visible in this runtime.",
        )
    if len(specs) == 1:
        return RuntimeSubAgentDecision(allowed=True, spec=specs[0])
    return RuntimeSubAgentDecision(
        allowed=False,
        code="subagent_ambiguous",
        reason="Multiple subagents are visible; choose one by name.",
    )


def runtime_allows_subagents(envelope) -> bool:
    if envelope is None:
        return True
    profile = getattr(envelope, "profile", None)
    if profile in _SUBAGENT_DENIED_PROFILES:
        return False
    return bool(getattr(envelope, "subagent_names", ()) or ())


def runtime_allows_subagent(envelope, name: str) -> bool:
    if not runtime_allows_subagents(envelope):
        return False
    clean = str(name or "").strip()
    return clean in set(getattr(envelope, "subagent_names", ()) or ())
