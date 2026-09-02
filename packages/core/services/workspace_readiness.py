"""Unified workspace readiness checks.

Readiness is not a single boolean. A workspace has separate operating parts,
each with a role and a source-of-truth check. This module keeps those concepts
explicit so Strategist can avoid turning one missing surface, such as a channel,
into a blanket claim that all outbound work is blocked.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    WorkerStatus,
)
from packages.core.constants.blueprints import (
    BlueprintInstallTodoKind,
    installed_blueprint_job_id,
)
from packages.core.models.channel import ChannelConfig
from packages.core.models.goal import Goal
from packages.core.models.workspace import AgentSubscription, Workspace


BUILT_IN_CHANNEL_TYPES = {"webchat", "internal_chat", "in_app"}


def iter_declared_service_keys(operating_model: dict[str, Any] | None) -> set[str]:
    """Return canonical service keys from Workspace operating-model config.

    Blueprint v1.1 payloads historically used ``key`` for entries under
    ``operating_model.services`` while runtime configuration uses
    ``service_key``. Accept both at this boundary, but fail closed when one
    entry declares two different values.
    """
    keys: set[str] = set()
    for index, service in enumerate((operating_model or {}).get("services") or []):
        if not isinstance(service, dict):
            continue
        canonical = str(service.get("service_key") or "").strip()
        legacy = str(service.get("key") or "").strip()
        if canonical and legacy and canonical != legacy:
            raise ValueError(
                f"Operating-model service {index} has conflicting key and service_key"
            )
        value = canonical or legacy
        if value:
            keys.add(value)
    return keys


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


def normalize_workspace_setup_task_key(value: object) -> str:
    """Use the Strategist task-key shape for persisted setup allowlists."""

    base = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", base).strip("_")[:80]


WORKSPACE_READINESS_PARTS: tuple[WorkspaceReadinessPartSpec, ...] = (
    WorkspaceReadinessPartSpec(
        key="blocking_setup",
        name="Blocking Workspace setup",
        role=(
            "Blueprint-defined prerequisites that must be ready before Strategist "
            "may propose or launch normal operating work."
        ),
        check=(
            "Every blocking_setup check in Workspace settings is evaluated from "
            "current persisted assets or live integration readiness."
        ),
    ),
    WorkspaceReadinessPartSpec(
        key="agents",
        name="Agents and services",
        role="Execution capacity: maps workspace service keys to agents or humans that can own tasks.",
        check=(
            "Every declared service has an active AgentSubscription and each "
            "subscription is bound to an active execution worker."
        ),
    ),
    WorkspaceReadinessPartSpec(
        key="goals",
        name="Goals",
        role="Optional direction and measurement for goal-attributed work.",
        check="Active Goals are reported when configured; their absence does not block work.",
    ),
    WorkspaceReadinessPartSpec(
        key="integrations",
        name="External integrations",
        role="Credentialed external systems such as Twitter/X, Gmail, or browser-backed platforms.",
        check=(
            "Typed provider declarations from goals, channels, and flagged integrations "
            "are each checked against active entity/OAuth connections."
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


def matching_blueprint_channels(
    configured_channels: list[dict[str, Any]],
    requirement: dict[str, Any],
) -> list[dict[str, Any]]:
    """Match a declared route, not any account with the same channel type.

    Installed identities pin indistinguishable same-type requirements. Older
    declarations and newly paired accounts may resolve by their portable route
    fields, but ambiguous matches are never evidence of a ready requirement.
    """
    fields = (
        "channel_type", "provider", "role", "linked_service_key",
        "channel_config_id", "channel_binding_id",
    )
    requirement_key = requirement.get("blueprint_requirement_key")
    return [
        channel for channel in configured_channels
        if all(
            not requirement.get(key)
            or str(channel.get(key) or "").strip() == str(requirement[key]).strip()
            for key in fields
        )
        and (
            not requirement_key or not channel.get("blueprint_requirement_key")
            or channel["blueprint_requirement_key"] == requirement_key
        )
    ]


def blueprint_channel_is_ready(
    configured_channels: list[dict[str, Any]],
    requirement: dict[str, Any],
) -> bool:
    channel_type = str(requirement.get("channel_type") or requirement.get("type") or "").strip()
    if channel_type in BUILT_IN_CHANNEL_TYPES:
        return True
    return bool(channel_type) and len(matching_blueprint_channels(
        configured_channels, {**requirement, "channel_type": channel_type},
    )) == 1


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
        # Distinct accounts with identical labels still satisfy different
        # Blueprint requirements; retain their identities for live checks.
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
            "blueprint_requirement_key": merged_config.get("blueprint_requirement_key"),
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
    runtime_bound_subscription_ids: set[str] | None = None,
) -> WorkspaceReadinessReport:
    subscription_rows = list(subscriptions or [])
    if runtime_bound_subscription_ids is None:
        runtime_bound_subscription_ids = await _runtime_bound_subscription_ids(
            db,
            workspace,
            subscription_rows,
        )
    channels = (
        configured_channels
        if configured_channels is not None
        else await list_configured_workspace_channels(db, workspace)
    )
    blocking_setup_status = await evaluate_workspace_blocking_setup(db, workspace)
    return build_workspace_readiness_report(
        operating_model=workspace.operating_model or {},
        subscriptions=subscription_rows,
        goals=list(goals or []),
        declared_provider_keys=declared_provider_keys or set(),
        active_provider_keys=active_provider_keys or set(),
        configured_integrations=configured_integrations or [],
        configured_channels=channels,
        knowledge_nets=knowledge_nets or [],
        governance_policy=governance_policy,
        operating_memory=operating_memory,
        runtime_bound_subscription_ids=runtime_bound_subscription_ids,
        blocking_setup_status=blocking_setup_status,
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
    runtime_bound_subscription_ids: set[str] | None = None,
    blocking_setup_status: WorkspaceReadinessPartStatus | None = None,
) -> WorkspaceReadinessReport:
    spec_by_key = {spec.key: spec for spec in WORKSPACE_READINESS_PARTS}
    missing_channels = missing_required_channels(configured_channels, operating_model)
    declared_service_keys = iter_declared_service_keys(operating_model)
    subscribed_service_keys = {
        str(getattr(subscription, "service_key", "") or "").strip()
        for subscription in subscriptions
        if str(getattr(subscription, "service_key", "") or "").strip()
    }
    missing_service_keys = sorted(declared_service_keys - subscribed_service_keys)
    subscription_ids = {
        str(getattr(subscription, "id", "") or "").strip()
        for subscription in subscriptions
        if str(getattr(subscription, "id", "") or "").strip()
    }
    bound_ids = (
        set(runtime_bound_subscription_ids)
        if runtime_bound_subscription_ids is not None
        else set(subscription_ids)
    )
    unbound_subscription_ids = sorted(subscription_ids - bound_ids)
    agents_ready = bool(subscriptions) and not missing_service_keys and not unbound_subscription_ids
    agent_status = "ready" if agents_ready else "partial" if subscriptions else "missing"
    agent_summary_bits: list[str] = [f"{len(subscriptions)} active service subscription(s)."]
    if missing_service_keys:
        agent_summary_bits.append("Missing services: " + ", ".join(missing_service_keys) + ".")
    if unbound_subscription_ids:
        agent_summary_bits.append(
            f"{len(unbound_subscription_ids)} subscription(s) have no active worker binding."
        )
    if not subscriptions:
        agent_summary_bits = ["No active service subscriptions; Strategist has no valid task owner."]

    parts = [
        _part_status(
            spec_by_key["agents"],
            status=agent_status,
            summary=" ".join(agent_summary_bits),
            missing_setup_key="" if agents_ready else "no_agents",
            details={
                "count": len(subscriptions),
                "declared_service_keys": sorted(declared_service_keys),
                "subscribed_service_keys": sorted(subscribed_service_keys),
                "missing_service_keys": missing_service_keys,
                "runtime_bound_subscription_ids": sorted(bound_ids),
                "unbound_subscription_ids": unbound_subscription_ids,
            },
        ),
        _part_status(
            spec_by_key["goals"],
            status="ready" if goals else "not_required",
            summary=(
                f"{len(goals)} active goal(s)."
                if goals
                else "No active goals configured; Strategist can prioritize primary work without goal attribution."
            ),
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
    if blocking_setup_status is not None:
        parts.insert(0, blocking_setup_status)
    return WorkspaceReadinessReport(
        parts=parts,
        configured_channels=configured_channels,
        missing_channel_requirements=missing_channels,
        configured_integrations=configured_integrations,
    )


async def evaluate_workspace_blocking_setup(
    db: AsyncSession,
    workspace: Workspace,
    *,
    check_keys: set[str] | None = None,
) -> WorkspaceReadinessPartStatus | None:
    """Evaluate the Blueprint-owned setup gate for one Workspace.

    Declarative checks live under ``Workspace.settings.blocking_setup`` and
    installed requirements under ``settings._blueprint.live_setup_requirements``.
    Results are derived on every read rather than cached, so replacing or
    disabling a dependency takes effect immediately and stale ``ready`` flags
    cannot bypass the gate.
    """

    settings = workspace.settings or {}
    setup = settings.get("blocking_setup")
    setup = setup if isinstance(setup, dict) else {}
    checks = setup.get("checks")
    checks = checks if isinstance(checks, list) else []
    blueprint = settings.get("_blueprint")
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    live_requirements = blueprint.get("live_setup_requirements")
    missing_live_contract = bool(blueprint) and not isinstance(
        live_requirements,
        list,
    )
    requirement_source = (
        live_requirements
        if isinstance(live_requirements, list)
        else []
    )
    # ``check_keys`` is used by one post-install assertion to inspect a
    # declared subset. Durable install requirements are the workspace-wide
    # gate and must not leak unrelated blockers into that scoped assertion;
    # ordinary runtime callers pass no filter and still evaluate all of them.
    install_todos = [] if check_keys is not None else [
        todo
        for todo in requirement_source
        if isinstance(todo, dict) and bool(todo.get("blocking", True))
    ]

    selected_checks = [
        check
        for check in checks
        if isinstance(check, dict)
        and (
            check_keys is None
            or str(check.get("key") or "").strip() in check_keys
        )
    ]
    if not selected_checks and not install_todos and not missing_live_contract:
        return None

    integration_checks = [
        check
        for check in selected_checks
        if str(check.get("kind") or "").strip() == "integration_provider"
        and str(check.get("provider") or "").strip()
        and bool(check.get("blocking", True))
    ]
    from packages.core.services.provider_keys import canonical_provider_key

    blocking_integration_check_providers = {
        canonical_provider_key(check.get("provider"))
        for check in integration_checks
        if bool(check.get("blocking", True))
    }
    integration_todo_providers = [
        str((todo.get("payload") or {}).get("provider") or "").strip()
        for todo in install_todos
        if str(todo.get("kind") or "").strip() == BlueprintInstallTodoKind.MISSING_INTEGRATION.value
        and isinstance(todo.get("payload"), dict)
        and str((todo.get("payload") or {}).get("provider") or "").strip()
    ]
    declared_integration_providers = {
        str(check.get("provider") or "").strip()
        for check in integration_checks
        if str(check.get("provider") or "").strip()
    }
    integration_states: dict[str, Any] = {}
    if integration_checks or integration_todo_providers:
        from packages.core.services.integration_resolution import (
            integration_provider_readiness,
        )

        integration_states = await integration_provider_readiness(
            db,
            entity_id=workspace.entity_id,
            user_id=str((workspace.settings or {}).get("created_by_user_id") or "") or None,
            provider_keys=[
                *[str(check["provider"]) for check in integration_checks],
                *integration_todo_providers,
            ],
        )

    results: list[dict[str, Any]] = []
    if missing_live_contract:
        results.append({
            "ready": False,
            "reason": (
                "This legacy Blueprint install has no trusted live setup "
                "contract. Re-sync or upgrade the Blueprint before normal work."
            ),
            "key": "blueprint_live_setup_contract",
            "kind": "blueprint_install_todo",
            "todo_kind": BlueprintInstallTodoKind.LIVE_SETUP_CONTRACT.value,
            "blocking": True,
            "setup_task_key": "",
            "setup_job_id": "",
        })
    for check in selected_checks:
        kind = str(check.get("kind") or "").strip()
        if kind == "workspace_identity_assets":
            result = _workspace_identity_setup_result(workspace, check)
        elif kind == "integration_provider":
            provider = canonical_provider_key(check.get("provider"))
            state = integration_states.get(provider)
            result = {
                "ready": bool(state and state.ready),
                "reason": (
                    str(state.reason)
                    if state is not None
                    else f"Integration provider {provider!r} has no readiness result."
                ),
                "provider": provider,
                "scope": str(state.scope) if state is not None else "none",
                "setup_kind": (
                    state.setup_kind
                    if state is not None
                    else check.get("setup_kind")
                ),
            }
        else:
            result = {
                "ready": False,
                "reason": f"Unknown blocking setup check kind {kind!r}; failing closed.",
            }
        result.update({
            "key": str(check.get("key") or kind or "setup").strip(),
            "kind": kind,
            "blocking": bool(check.get("blocking", True)),
            "setup_task_key": str(check.get("setup_task_key") or "").strip(),
            "setup_job_id": str(check.get("setup_job_id") or "").strip(),
        })
        results.append(result)

    configured_channels = (
        await list_configured_workspace_channels(db, workspace)
        if any(
            str(todo.get("kind") or "") == BlueprintInstallTodoKind.CHANNEL.value
            for todo in install_todos
        )
        else []
    )
    used_channel_accounts: set[str] = set()
    for index, todo in enumerate(install_todos):
        todo_kind = str(todo.get("kind") or "").strip()
        todo_payload = todo.get("payload")
        todo_payload = todo_payload if isinstance(todo_payload, dict) else {}
        # Required integrations are also persisted as generic install
        # requirements. When a Blueprint owns the same provider through a
        # richer declarative setup check, evaluate that single canonical check
        # instead of reporting the same dependency twice.
        if (
            todo_kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value
            and canonical_provider_key(todo_payload.get("provider"))
            in blocking_integration_check_providers
        ):
            continue
        # A blocking_setup todo is the install-time snapshot of the declarative
        # checks evaluated above. Re-adding it would keep readiness red after
        # those concrete checks become ready.
        # Integration checks have their own missing_integration todo; keeping
        # that row out of this aggregate prevents duplicate setup blockers.
        todo_provider = str((todo.get("payload") or {}).get("provider") or "").strip()
        if (
            str(todo.get("kind") or "").strip()
            == BlueprintInstallTodoKind.MISSING_INTEGRATION.value
            and todo_provider in declared_integration_providers
        ):
            continue
        if (
            todo_kind == BlueprintInstallTodoKind.BLOCKING_SETUP.value
            and selected_checks
        ):
            continue
        available_channels = [
            channel for channel in configured_channels
            if channel.get("channel_config_id") not in used_channel_accounts
        ]
        result = await _blueprint_install_todo_result(
            db,
            workspace,
            todo,
            integration_states=integration_states,
            configured_channels=available_channels,
        )
        if todo_kind == BlueprintInstallTodoKind.CHANNEL.value and result["ready"]:
            matches = matching_blueprint_channels(available_channels, todo_payload)
            if len(matches) == 1 and matches[0].get("channel_config_id"):
                used_channel_accounts.add(matches[0]["channel_config_id"])
        result.update({
            "key": f"blueprint_install_todo:{index}",
            "kind": "blueprint_install_todo",
            "todo_kind": str(todo.get("kind") or "setup").strip(),
            "blocking": True,
            "setup_task_key": "",
            "setup_job_id": "",
        })
        results.append(result)

    incomplete = [
        result
        for result in results
        if result["blocking"] and not result["ready"]
    ]
    configured_allowed_keys = {
        normalize_workspace_setup_task_key(value)
        for value in setup.get("allowed_setup_task_keys") or []
        if normalize_workspace_setup_task_key(value)
    }
    incomplete_setup_keys = [
        normalize_workspace_setup_task_key(result.get("setup_task_key"))
        for result in incomplete
        if normalize_workspace_setup_task_key(result.get("setup_task_key"))
    ]
    allowed_setup_task_keys = list(dict.fromkeys(
        key
        for key in incomplete_setup_keys
        if not configured_allowed_keys or key in configured_allowed_keys
    ))
    ready = not incomplete
    spec = next(spec for spec in WORKSPACE_READINESS_PARTS if spec.key == "blocking_setup")
    return _part_status(
        spec,
        status="ready" if ready else "missing",
        summary=(
            f"All {len(results)} blocking setup check(s) are ready."
            if ready
            else (
                f"{len(incomplete)} of {len(results)} blocking setup check(s) "
                "must be completed before normal work."
            )
        ),
        missing_setup_key="" if ready else "blocking_setup_incomplete",
        details={
            "ready": ready,
            "check_results": results,
            "incomplete_checks": incomplete,
            "allowed_setup_task_keys": allowed_setup_task_keys,
        },
    )


async def _blueprint_install_todo_result(
    db: AsyncSession,
    workspace: Workspace,
    todo: dict[str, Any],
    *,
    integration_states: dict[str, Any],
    configured_channels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Re-evaluate one durable Blueprint install blocker from live state."""

    kind = str(todo.get("kind") or "").strip()
    payload = todo.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    detail = str(todo.get("detail") or "Blueprint setup is incomplete.")

    if kind == BlueprintInstallTodoKind.CHANNEL.value:
        ready = blueprint_channel_is_ready(configured_channels, payload)
        return {"ready": ready, "reason": "Channel is configured." if ready else detail}

    if kind == BlueprintInstallTodoKind.BROWSER_SESSION.value:
        from packages.core.models.integration_session import IntegrationSession

        stmt = select(IntegrationSession.id).where(
            IntegrationSession.entity_id == workspace.entity_id,
            IntegrationSession.status == "active",
        )
        provider = str(payload.get("provider") or "").strip()
        label = str(payload.get("label") or "").strip()
        if provider:
            stmt = stmt.where(IntegrationSession.provider == provider)
        if label:
            stmt = stmt.where(IntegrationSession.label == label)
        ready = (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None
        return {"ready": ready, "reason": "Browser session is active." if ready else detail}

    if kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value:
        from packages.core.services.provider_keys import canonical_provider_key

        provider = canonical_provider_key(payload.get("provider"))
        state = integration_states.get(provider)
        return {
            "ready": bool(state and state.ready),
            "reason": "Integration is connected." if state and state.ready else detail,
            "provider": provider,
        }

    if kind == BlueprintInstallTodoKind.MISSING_AGENT.value:
        from packages.core.models.workspace import Agent

        service_key = str(payload.get("service_key") or "").strip()
        expected_slug = str(payload.get("agent_slug") or "").strip()
        expected_marketplace_id = str(
            payload.get("marketplace_agent_id") or ""
        ).strip()
        expected_installed_id = str(
            payload.get("installed_agent_id")
            or payload.get("placeholder_agent_id")
            or ""
        ).strip()
        stmt = (
            select(AgentSubscription, Agent)
            .join(Agent, Agent.id == AgentSubscription.agent_id)
            .where(
                AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.entity_id == workspace.entity_id,
                AgentSubscription.status == "active",
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )
        if service_key:
            stmt = stmt.where(AgentSubscription.service_key == service_key)
        rows = list((await db.execute(stmt)).all())
        matching_subscriptions: list[AgentSubscription] = []
        for subscription, agent in rows:
            source_agent_id = str(
                (agent.config or {}).get("source_agent_id") or ""
            ).strip()
            if expected_installed_id:
                identity_matches = str(agent.id) == expected_installed_id
            elif expected_marketplace_id:
                identity_matches = (
                    str(agent.id) == expected_marketplace_id
                    or source_agent_id == expected_marketplace_id
                )
            elif expected_slug:
                identity_matches = str(agent.slug or "").strip() == expected_slug
            else:
                # Generic agent_callable post-install checks intentionally name
                # only a service. They still require an active runtime worker;
                # identity-specific MISSING_AGENT todos take the stricter paths
                # above and cannot be satisfied by a different Agent.
                identity_matches = True
            if identity_matches:
                matching_subscriptions.append(subscription)
        callable_subscription_ids = await _runtime_bound_subscription_ids(
            db,
            workspace,
            matching_subscriptions,
        )
        ready = bool(callable_subscription_ids)
        return {
            "ready": ready,
            "reason": "Required Agent subscription is callable." if ready else detail,
        }

    if kind == BlueprintInstallTodoKind.KNOWLEDGE_PACK_CONTENT.value:
        from packages.core.models.document import Document, DocumentGroupMember

        group_id = str(payload.get("document_group_id") or "").strip()
        ready = False
        if group_id:
            ready = (await db.execute(
                select(Document.id)
                .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
                .where(
                    Document.entity_id == workspace.entity_id,
                    DocumentGroupMember.group_id == group_id,
                    Document.is_trashed == False,  # noqa: E712
                )
                .limit(1)
            )).scalar_one_or_none() is not None
        return {"ready": ready, "reason": "Knowledge content is available." if ready else detail}

    if kind == BlueprintInstallTodoKind.POST_INSTALL_CHECK.value:
        check = payload.get("check")
        check = check if isinstance(check, dict) else {}
        check_kind = str(check.get("kind") or "").strip()
        if check_kind == "agent_callable":
            return await _blueprint_install_todo_result(
                db,
                workspace,
                {
                    "kind": BlueprintInstallTodoKind.MISSING_AGENT.value,
                    "detail": detail,
                    "payload": {"service_key": check.get("service_key")},
                },
                integration_states=integration_states,
                configured_channels=configured_channels,
            )
        if check_kind == "session_alive":
            return await _blueprint_install_todo_result(
                db,
                workspace,
                {
                    "kind": BlueprintInstallTodoKind.BROWSER_SESSION.value,
                    "detail": detail,
                    "payload": {
                        "provider": check.get("provider"),
                        "label": check.get("session_label"),
                    },
                },
                integration_states=integration_states,
                configured_channels=configured_channels,
            )
        if check_kind == "cron_scheduled":
            from packages.core.models.scheduler import ScheduledJob

            job_id = str(check.get("job_id") or "").strip()
            scoped_job_id = installed_blueprint_job_id(job_id, workspace.id)
            ready = (await db.execute(
                select(ScheduledJob.id)
                .where(
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.job_id == scoped_job_id,
                    ScheduledJob.enabled.is_(True),
                )
                .limit(1)
            )).scalar_one_or_none() is not None
            return {"ready": ready, "reason": "Scheduled job is installed." if ready else detail}

        if check_kind in {"workflow_present", "workflow_dryrun"}:
            from packages.core.models.workflow import (
                WorkflowBinding,
                WorkflowDefinition,
            )

            workflow_slug = str(check.get("workflow_slug") or "").strip()
            bindings = list((await db.execute(
                select(WorkflowBinding)
                .join(
                    WorkflowDefinition,
                    WorkflowDefinition.id == WorkflowBinding.workflow_id,
                )
                .where(
                    WorkflowBinding.workspace_id == workspace.id,
                    WorkflowBinding.entity_id == workspace.entity_id,
                    WorkflowBinding.enabled.is_(True),
                    WorkflowBinding.status == "active",
                    WorkflowDefinition.entity_id == workspace.entity_id,
                    WorkflowDefinition.is_active.is_(True),
                    WorkflowDefinition.status == "active",
                )
            )).scalars().all())
            ready = any(
                str((binding.config or {}).get("workspace_blueprint_workflow_slug") or "").strip()
                == workflow_slug
                for binding in bindings
            )
            return {"ready": ready, "reason": "Workflow is installed." if ready else detail}

    if kind in {
        BlueprintInstallTodoKind.MCP_SERVER.value,
        BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
    }:
        from packages.core.models.mcp import AgentMCPBinding, MCPServer
        from packages.core.models.workspace import Agent

        server_slug = str(payload.get("server_slug") or "").strip()
        agent_slug = str(payload.get("agent_slug") or "").strip()
        installed_agent_id = str(payload.get("installed_agent_id") or "").strip()
        agent_component_key = str(
            payload.get("agent_component_key") or ""
        ).strip()
        deployed_agent_ids = select(AgentSubscription.agent_id).where(
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.status == "active",
        )
        binding_stmt = (
            select(AgentMCPBinding, MCPServer)
            .join(Agent, Agent.id == AgentMCPBinding.agent_id)
            .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
            .where(
                Agent.entity_id == workspace.entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
                or_(
                    Agent.workspace_id == workspace.id,
                    Agent.id.in_(deployed_agent_ids),
                ),
                AgentMCPBinding.status == "active",
                MCPServer.server_key == server_slug,
                MCPServer.status == "active",
            )
        )
        if installed_agent_id:
            binding_stmt = binding_stmt.where(Agent.id == installed_agent_id)
        elif agent_component_key:
            binding_stmt = binding_stmt.where(
                Agent.config["source_blueprint_component_key"].astext
                == agent_component_key
            )
        else:
            binding_stmt = binding_stmt.where(Agent.slug == agent_slug)
        rows = list((await db.execute(binding_stmt)).all())
        expected_allowed_tools = payload.get("allowed_tools")
        required_config_fields = [
            str(field).strip()
            for field in (
                payload.get("required_config_fields")
                or payload.get("config_override_allowlist")
                or []
            )
            if str(field).strip()
        ]
        ready = False
        for agent_binding, _server in rows:
            if "allowed_tools" in payload:
                if expected_allowed_tools is None:
                    if agent_binding.allowed_tools is not None:
                        continue
                elif (
                    agent_binding.allowed_tools is None
                    or set(agent_binding.allowed_tools) != set(expected_allowed_tools)
                ):
                    continue
            config_override = dict(agent_binding.config_override or {})
            if any(
                field not in config_override
                or config_override[field] is None
                or (
                    isinstance(config_override[field], str)
                    and not config_override[field].strip()
                )
                for field in required_config_fields
            ):
                continue
            ready = True
            break
        return {
            "ready": ready,
            "reason": "MCP binding is configured and active." if ready else detail,
        }

    if kind == BlueprintInstallTodoKind.MISSING_SKILL.value:
        from packages.core.models.skill import AgentSkillBinding, Skill
        from packages.core.models.workspace import Agent

        skill_slug = str(payload.get("skill_slug") or "").strip()
        agent_slug = str(payload.get("agent_slug") or "").strip()
        installed_skill_id = str(payload.get("installed_skill_id") or "").strip()
        marketplace_skill_id = str(
            payload.get("marketplace_skill_id") or ""
        ).strip()
        skill_component_key = str(
            payload.get("skill_component_key") or ""
        ).strip()
        installed_agent_id = str(payload.get("installed_agent_id") or "").strip()
        agent_component_key = str(
            payload.get("agent_component_key") or ""
        ).strip()
        deployed_agent_ids = select(AgentSubscription.agent_id).where(
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.status == "active",
        )
        rows = list((await db.execute(
            select(AgentSkillBinding, Agent, Skill)
            .join(Agent, Agent.id == AgentSkillBinding.agent_id)
            .join(Skill, Skill.id == AgentSkillBinding.skill_id)
            .where(
                Agent.entity_id == workspace.entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
                or_(
                    Agent.workspace_id == workspace.id,
                    Agent.id.in_(deployed_agent_ids),
                ),
                AgentSkillBinding.status == "active",
                Skill.status == "active",
            )
        )).all())
        ready = False
        for _binding, agent, skill in rows:
            if installed_agent_id:
                if str(agent.id) != installed_agent_id:
                    continue
            elif agent_component_key:
                if str(
                    (agent.config or {}).get(
                        "source_blueprint_component_key"
                    )
                    or ""
                ).strip() != agent_component_key:
                    continue
            elif str(agent.slug or "").strip() != agent_slug:
                continue
            source_skill_id = str(
                (skill.config or {}).get("source_skill_id") or ""
            ).strip()
            source_component_key = str(
                (skill.config or {}).get("source_blueprint_component_key") or ""
            ).strip()
            if installed_skill_id:
                identity_matches = str(skill.id) == installed_skill_id
            elif marketplace_skill_id:
                identity_matches = (
                    str(skill.id) == marketplace_skill_id
                    or source_skill_id == marketplace_skill_id
                )
            elif skill_component_key:
                identity_matches = source_component_key == skill_component_key
            else:
                identity_matches = str(skill.slug or "").strip() == skill_slug
            if identity_matches:
                ready = True
                break
        return {"ready": ready, "reason": "Skill binding is active." if ready else detail}

    # Unknown future todo kinds remain fail-closed until a concrete evaluator
    # is added.
    return {"ready": False, "reason": detail}


async def evaluate_current_workspace_blocking_setup(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str | None = None,
) -> WorkspaceReadinessPartStatus | None:
    """Reload the Workspace before making a new setup admission decision."""

    stmt = (
        select(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if entity_id:
        stmt = stmt.where(Workspace.entity_id == entity_id)
    workspace = (await db.execute(stmt)).scalar_one_or_none()
    if workspace is None or str(getattr(workspace, "status", "active") or "") != "active":
        spec = next(
            spec for spec in WORKSPACE_READINESS_PARTS
            if spec.key == "blocking_setup"
        )
        return _part_status(
            spec,
            status="missing",
            summary="Workspace is unavailable for new runtime work.",
            missing_setup_key="workspace_unavailable",
            details={"ready": False, "incomplete_checks": []},
        )
    return await evaluate_workspace_blocking_setup(db, workspace)


def _workspace_identity_setup_result(
    workspace: Workspace,
    check: dict[str, Any],
) -> dict[str, Any]:
    """Check the canonical person, narrator profile, and setup manifest."""

    from packages.core.services.entity_fs import get_entity_root, resolve_path
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    settings = workspace.settings or {}
    asset_key = str(check.get("asset_key") or "").strip()
    narrator_key = str(check.get("narrator_profile_key") or "").strip()
    asset_path = str(check.get("asset_path") or "").strip().replace("\\", "/").lstrip("/")
    manifest_path = str(check.get("manifest_path") or "").strip().replace("\\", "/").lstrip("/")

    assets = settings.get("reusable_media_assets")
    record = assets.get(asset_key) if isinstance(assets, dict) else None
    record_path = (
        str(record.get("fs_path") or "").strip().replace("\\", "/").lstrip("/")
        if isinstance(record, dict)
        else ""
    )
    record_url = str(record.get("result_url") or "").strip() if isinstance(record, dict) else ""
    asset_abs = resolve_path(workspace.entity_id, record_path) if record_path else None
    asset_ready = bool(
        isinstance(record, dict)
        and record.get("kind") == "image"
        and record_path
        and record_url
        and (not asset_path or record_path.endswith(asset_path))
        and asset_abs
        and os.path.isfile(asset_abs)
    )

    narrator = settings.get(narrator_key) if narrator_key else None
    narrator_ready = bool(
        isinstance(narrator, dict)
        and all(str(narrator.get(field) or "").strip() for field in ("provider", "model", "voice"))
    )

    storage_base = workspace_artifact_storage_base(workspace.artifact_folder_id)
    manifest_rel = "/".join(part for part in (storage_base, manifest_path) if part)
    manifest_abs = resolve_path(workspace.entity_id, manifest_rel) if manifest_rel else None
    manifest_ready = bool(manifest_abs and os.path.isfile(manifest_abs))

    missing: list[str] = []
    if not asset_ready:
        missing.append(asset_key or "workspace_character_asset")
    if not narrator_ready:
        missing.append(narrator_key or "workspace_narrator_profile")
    if not manifest_ready:
        missing.append(manifest_path or "workspace_asset_manifest")
    return {
        "ready": not missing,
        "reason": (
            "Workspace identity assets are ready."
            if not missing
            else "Missing or unreadable Workspace identity requirement(s): " + ", ".join(missing) + "."
        ),
        "asset_key": asset_key,
        "asset_fs_path": record_path,
        "asset_ready": asset_ready,
        "narrator_profile_key": narrator_key,
        "narrator_ready": narrator_ready,
        "manifest_fs_path": manifest_rel,
        "manifest_ready": manifest_ready,
        "entity_root": get_entity_root(workspace.entity_id),
    }


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
        missing_key = "no_integrations" if missing else ""
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


async def _runtime_bound_subscription_ids(
    db: AsyncSession,
    workspace: Workspace,
    subscriptions: list[AgentSubscription],
) -> set[str]:
    subscription_ids = {
        str(subscription.id)
        for subscription in subscriptions
        if getattr(subscription, "id", None)
    }
    if not subscription_ids:
        return set()

    from packages.core.models.worker import SubscriptionWorker, Worker

    rows = list((await db.execute(
        select(SubscriptionWorker.subscription_id)
        .join(Worker, Worker.id == SubscriptionWorker.worker_id)
        .where(
            SubscriptionWorker.subscription_id.in_(subscription_ids),
            Worker.entity_id == workspace.entity_id,
            Worker.status == WorkerStatus.ACTIVE,
        )
    )).scalars().all())
    return {str(subscription_id) for subscription_id in rows}


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
