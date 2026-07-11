"""Unified workspace readiness checks.

Readiness is not a single boolean. A workspace has separate operating parts,
each with a role and a source-of-truth check. This module keeps those concepts
explicit so Strategist can avoid turning one missing surface, such as a channel,
into a blanket claim that all outbound work is blocked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.channel import ChannelConfig
from packages.core.models.goal import Goal
from packages.core.models.workspace import AgentSubscription, Workspace


BUILT_IN_CHANNEL_TYPES = {"webchat", "internal_chat", "in_app"}


@dataclass(frozen=True)
class WorkspaceReadinessPartSpec:
    key: str
    name: str
    role: str
    check: str


@dataclass(frozen=True)
class WorkspaceReadinessPartStatus:
    key: str
    name: str
    role: str
    check: str
    status: str
    summary: str
    missing_setup_key: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_work(self) -> bool:
        return bool(self.missing_setup_key)


@dataclass(frozen=True)
class WorkspaceReadinessReport:
    parts: list[WorkspaceReadinessPartStatus]
    configured_channels: list[dict[str, Any]] = field(default_factory=list)
    missing_channel_requirements: list[dict[str, Any]] = field(default_factory=list)
    configured_integrations: list[str] = field(default_factory=list)

    @property
    def missing_setup_keys(self) -> list[str]:
        return [part.missing_setup_key for part in self.parts if part.missing_setup_key]

    def to_prompt_text(self) -> str:
        lines: list[str] = []
        for part in self.parts:
            line = f"- {part.name}: {part.status} — {part.summary}"
            line += f"\n  Role: {part.role}"
            line += f"\n  Check: {part.check}"
            if part.missing_setup_key:
                line += f"\n  Missing setup key: {part.missing_setup_key}"
            lines.append(line)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": [asdict(part) for part in self.parts],
            "configured_channels": self.configured_channels,
            "missing_channel_requirements": self.missing_channel_requirements,
            "configured_integrations": self.configured_integrations,
            "missing_setup": self.missing_setup_keys,
        }


WORKSPACE_READINESS_PARTS: tuple[WorkspaceReadinessPartSpec, ...] = (
    WorkspaceReadinessPartSpec(
        key="agents",
        name="Agents and services",
        role="Execution capacity: maps workspace service keys to agents or humans that can own tasks.",
        check="At least one active AgentSubscription scoped to this workspace.",
    ),
    WorkspaceReadinessPartSpec(
        key="goals",
        name="Goals",
        role="Direction and measurement: tells Strategist what progress means.",
        check="At least one active Goal scoped to this workspace.",
    ),
    WorkspaceReadinessPartSpec(
        key="integrations",
        name="External integrations",
        role="Credentialed external systems such as Twitter/X, Gmail, or browser-backed platforms.",
        check=(
            "Workspace-declared provider needs from goals, channel declarations, flagged integrations, "
            "and workspace text intersect active entity/OAuth providers."
        ),
    ),
    WorkspaceReadinessPartSpec(
        key="channels",
        name="Channels",
        role=(
            "Communication surfaces for inbound/outbound messages. Channels are routing surfaces; "
            "they are not the same thing as provider integrations or file delivery."
        ),
        check=(
            "Workspace-owned ChannelConfig rows, shared ChannelConfigs explicitly bound through Channel rows, "
            "plus built-in channel declarations. Missing only when a non-built-in channel is declared but absent."
        ),
    ),
    WorkspaceReadinessPartSpec(
        key="knowledge",
        name="Knowledge",
        role="Retrieval context: workspace document groups agents should search/cite before knowledge-dependent work.",
        check="Workspace knowledge nets and their document counts.",
    ),
    WorkspaceReadinessPartSpec(
        key="governance",
        name="Governance",
        role="Safety and approval policy for external, destructive, or high-risk actions.",
        check="Workspace governance policy if configured; absence falls back to runtime defaults.",
    ),
    WorkspaceReadinessPartSpec(
        key="memory",
        name="Operating memory",
        role="Durable workspace policy/context plus generated STATE.md and FILES.md caches.",
        check="Canonical workspace operating memory loaded for prompt context.",
    ),
)


def iter_workspace_channel_blocks(operating_model: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """Return declared channel blocks from a workspace operating model."""
    if not isinstance(operating_model, dict):
        return []
    channel_config = operating_model.get("channel_config") or {}
    if not isinstance(channel_config, dict):
        return []

    out: list[tuple[str, dict[str, Any]]] = []
    for key in ("primary_external_channel", "internal_channel"):
        block = channel_config.get(key)
        if isinstance(block, dict):
            out.append((key.replace("_channel", ""), block))
    for key in ("secondary_external_channels", "channels"):
        for block in channel_config.get(key) or []:
            if isinstance(block, dict):
                out.append((str(block.get("role") or "channel"), block))
    return out


def declared_channel_requirements(operating_model: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return non-built-in channel declarations that need real config."""
    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for role, block in iter_workspace_channel_blocks(operating_model):
        channel_type = str(block.get("channel_type") or "").strip()
        if not channel_type or channel_type in BUILT_IN_CHANNEL_TYPES:
            continue
        provider = str(block.get("provider") or channel_type).strip()
        key = (role, channel_type, provider)
        if key in seen:
            continue
        seen.add(key)
        requirements.append({
            "role": role,
            "channel_type": channel_type,
            "provider": provider,
            "purpose": block.get("purpose") or "",
            "linked_service_key": block.get("linked_service_key") or "",
        })
    return requirements


def missing_required_channels(
    configured_channels: list[dict[str, Any]],
    operating_model: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return declared external channels that are not configured yet."""
    configured_pairs = {
        (
            str(channel.get("channel_type") or "").strip(),
            str(channel.get("provider") or channel.get("channel_type") or "").strip(),
        )
        for channel in configured_channels
    }
    missing: list[dict[str, Any]] = []
    for requirement in declared_channel_requirements(operating_model):
        req_type = str(requirement.get("channel_type") or "").strip()
        req_provider = str(requirement.get("provider") or req_type).strip()
        if (req_type, req_provider) in configured_pairs or any(pair[0] == req_type for pair in configured_pairs):
            continue
        missing.append(requirement)
    return missing


async def list_configured_workspace_channels(
    db: AsyncSession,
    workspace: Workspace,
) -> list[dict[str, Any]]:
    """Return workspace-scoped and explicitly bound shared channels."""
    from packages.core.models.document import Channel

    binding_by_cc: dict[str, Channel] = {}
    cc_ids: set[str] = set()
    bindings = list((await db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.entity_id == workspace.entity_id,
            Channel.status == "active",
        )
    )).scalars().all())
    for binding in bindings:
        cc_id = (binding.config or {}).get("channel_config_id")
        if cc_id:
            cc_ids.add(str(cc_id))
            binding_by_cc[str(cc_id)] = binding

    filters = [ChannelConfig.workspace_id == workspace.id]
    if cc_ids:
        filters.append(ChannelConfig.id.in_(cc_ids))

    rows = list((await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == workspace.entity_id,
            ChannelConfig.status == "active",
            or_(*filters),
        ).order_by(ChannelConfig.created_at)
    )).scalars().all())

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        binding = binding_by_cc.get(row.id)
        merged_config = dict(row.config or {})
        if binding:
            merged_config.update({
                key: value
                for key, value in (binding.config or {}).items()
                if key != "channel_config_id"
            })
        role = str(merged_config.get("role") or row.name or row.channel_type or "channel")
        channel_type = str(row.channel_type or "")
        provider = str(row.provider or channel_type)
        key = (role, channel_type, provider)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "role": role,
            "channel_type": channel_type,
            "provider": provider,
            "status": row.status,
            "purpose": merged_config.get("purpose") or "",
            "linked_service_key": merged_config.get("linked_service_key") or "",
            "built_in": channel_type in BUILT_IN_CHANNEL_TYPES,
            "source_scope": "workspace" if row.workspace_id == workspace.id else "shared",
            "channel_config_id": row.id,
            "channel_binding_id": binding.id if binding else None,
        })

    for role, block in iter_workspace_channel_blocks(workspace.operating_model or {}):
        channel_type = str(block.get("channel_type") or "")
        if not channel_type or channel_type not in BUILT_IN_CHANNEL_TYPES:
            continue
        provider = str(block.get("provider") or channel_type)
        key = (role, channel_type, provider)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "role": role,
            "channel_type": channel_type,
            "provider": provider,
            "status": "active",
            "purpose": block.get("purpose") or "",
            "linked_service_key": block.get("linked_service_key") or "",
            "built_in": True,
            "source_scope": "operating_model",
        })

    return out


async def check_workspace_readiness(
    db: AsyncSession,
    workspace: Workspace,
    *,
    subscriptions: Iterable[AgentSubscription] | None = None,
    goals: Iterable[Goal] | None = None,
    declared_provider_keys: set[str] | None = None,
    active_provider_keys: set[str] | None = None,
    configured_integrations: list[str] | None = None,
    configured_channels: list[dict[str, Any]] | None = None,
    knowledge_nets: list[dict[str, Any]] | None = None,
    governance_policy: dict[str, Any] | None = None,
    operating_memory: str = "",
) -> WorkspaceReadinessReport:
    channels = (
        configured_channels
        if configured_channels is not None
        else await list_configured_workspace_channels(db, workspace)
    )
    return build_workspace_readiness_report(
        operating_model=workspace.operating_model or {},
        subscriptions=list(subscriptions or []),
        goals=list(goals or []),
        declared_provider_keys=declared_provider_keys or set(),
        active_provider_keys=active_provider_keys or set(),
        configured_integrations=configured_integrations or [],
        configured_channels=channels,
        knowledge_nets=knowledge_nets or [],
        governance_policy=governance_policy,
        operating_memory=operating_memory,
    )


def build_workspace_readiness_report(
    *,
    operating_model: dict[str, Any],
    subscriptions: list[AgentSubscription],
    goals: list[Goal],
    declared_provider_keys: set[str],
    active_provider_keys: set[str],
    configured_integrations: list[str],
    configured_channels: list[dict[str, Any]],
    knowledge_nets: list[dict[str, Any]],
    governance_policy: dict[str, Any] | None,
    operating_memory: str,
) -> WorkspaceReadinessReport:
    spec_by_key = {spec.key: spec for spec in WORKSPACE_READINESS_PARTS}
    missing_channels = missing_required_channels(configured_channels, operating_model)
    parts = [
        _part_status(
            spec_by_key["agents"],
            status="ready" if subscriptions else "missing",
            summary=(
                f"{len(subscriptions)} active service subscription(s)."
                if subscriptions
                else "No active service subscriptions; Strategist has no valid task owner."
            ),
            missing_setup_key="" if subscriptions else "no_agents",
            details={"count": len(subscriptions)},
        ),
        _part_status(
            spec_by_key["goals"],
            status="ready" if goals else "missing",
            summary=(
                f"{len(goals)} active goal(s)."
                if goals
                else "No active goals; Strategist cannot rank work by impact."
            ),
            missing_setup_key="" if goals else "no_goals",
            details={"count": len(goals)},
        ),
        _integration_status(
            spec_by_key["integrations"],
            declared_provider_keys=declared_provider_keys,
            active_provider_keys=active_provider_keys,
            configured_integrations=configured_integrations,
        ),
        _channel_status(
            spec_by_key["channels"],
            configured_channels=configured_channels,
            missing_channels=missing_channels,
        ),
        _part_status(
            spec_by_key["knowledge"],
            status="ready" if knowledge_nets else "not_required",
            summary=(
                f"{len(knowledge_nets)} workspace knowledge net(s) available."
                if knowledge_nets
                else "No workspace knowledge nets attached; not a blocker for non-document work."
            ),
            details={"count": len(knowledge_nets)},
        ),
        _part_status(
            spec_by_key["governance"],
            status="ready" if governance_policy else "defaulted",
            summary=(
                "Workspace governance policy configured."
                if governance_policy
                else "No workspace-specific governance policy; runtime defaults still apply."
            ),
            details={"configured": bool(governance_policy)},
        ),
        _part_status(
            spec_by_key["memory"],
            status="ready" if operating_memory else "empty",
            summary=(
                "Canonical workspace operating memory loaded."
                if operating_memory
                else "Canonical workspace operating memory is empty or unavailable."
            ),
            details={"loaded": bool(operating_memory)},
        ),
    ]
    return WorkspaceReadinessReport(
        parts=parts,
        configured_channels=configured_channels,
        missing_channel_requirements=missing_channels,
        configured_integrations=configured_integrations,
    )


def _part_status(
    spec: WorkspaceReadinessPartSpec,
    *,
    status: str,
    summary: str,
    missing_setup_key: str = "",
    details: dict[str, Any] | None = None,
) -> WorkspaceReadinessPartStatus:
    return WorkspaceReadinessPartStatus(
        key=spec.key,
        name=spec.name,
        role=spec.role,
        check=spec.check,
        status=status,
        summary=summary,
        missing_setup_key=missing_setup_key,
        details=details or {},
    )


def _integration_status(
    spec: WorkspaceReadinessPartSpec,
    *,
    declared_provider_keys: set[str],
    active_provider_keys: set[str],
    configured_integrations: list[str],
) -> WorkspaceReadinessPartStatus:
    if not declared_provider_keys:
        return _part_status(
            spec,
            status="not_required",
            summary="No workspace-specific external providers declared.",
            details={"declared": [], "active": sorted(active_provider_keys)},
        )

    configured = sorted(declared_provider_keys & active_provider_keys)
    missing = sorted(declared_provider_keys - active_provider_keys)
    if configured:
        status = "ready" if not missing else "partial"
        summary = (
            f"Configured providers: {', '.join(configured)}."
            if not missing
            else f"Configured providers: {', '.join(configured)}; missing: {', '.join(missing)}."
        )
        missing_key = ""
    else:
        status = "missing"
        summary = f"Declared providers missing credentials: {', '.join(missing)}."
        missing_key = "no_integrations"

    return _part_status(
        spec,
        status=status,
        summary=summary,
        missing_setup_key=missing_key,
        details={
            "declared": sorted(declared_provider_keys),
            "active": sorted(active_provider_keys),
            "configured": configured_integrations,
            "missing": missing,
        },
    )


def _channel_status(
    spec: WorkspaceReadinessPartSpec,
    *,
    configured_channels: list[dict[str, Any]],
    missing_channels: list[dict[str, Any]],
) -> WorkspaceReadinessPartStatus:
    if missing_channels:
        summary = f"{len(missing_channels)} declared external channel requirement(s) are not configured."
        status = "missing"
        missing_key = "no_channels"
    elif configured_channels:
        summary = f"{len(configured_channels)} configured or built-in channel(s) available."
        status = "ready"
        missing_key = ""
    else:
        summary = "No channel requirement is declared; channels are not a setup blocker."
        status = "not_required"
        missing_key = ""
    return _part_status(
        spec,
        status=status,
        summary=summary,
        missing_setup_key=missing_key,
        details={
            "configured_count": len(configured_channels),
            "missing_requirements": missing_channels,
        },
    )
