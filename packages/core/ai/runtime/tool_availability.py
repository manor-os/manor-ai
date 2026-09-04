from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packages.core.ai.runtime.dynamic_mcp import (
        RuntimeDynamicMCPAccountRegistrySnapshot,
    )
    from packages.core.services.integration_account_service import (
        RuntimeIntegrationRegistry,
    )

from packages.core.ai.runtime.tool_discovery import (
    runtime_apply_mcp_availability,
    runtime_mark_match_available,
    runtime_mark_mcp_match_unavailable,
    runtime_mcp_provider_from_tool_name,
    runtime_mcp_tool_supports_all_accounts,
    runtime_sort_available_matches,
)
from packages.core.ai.runtime.dynamic_mcp import (
    RuntimeDynamicMCPFailureResultFactory,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeMCPCallPreflightResult:
    """Credential preflight plus the exact account selected for execution."""

    blocked_result: str = ""
    integration_account_id: str | None = None
    integration_registry: RuntimeIntegrationRegistry | None = None


def runtime_mcp_credentials_unavailable_result(
    *,
    provider: str,
    tool_name: str,
    reason: str,
    scope: str = "none",
    suggested_tool: str | None = None,
) -> str:
    """Return the canonical unavailable-credentials tool result.

    Chrome is backed by a paired local CLI worker rather than a reconnectable
    vendor token. Treat that missing worker as a terminal browser condition so
    nested ``invoke_skill(skill_id=<Chrome Skill.id>)`` runs propagate the real stop reason to
    the parent chat instead of converting ``credentials_unavailable`` into a
    successful ``completed`` answer.
    """

    payload: dict[str, object] = {
        "server": provider,
        "tool": tool_name,
        "error": "credentials_unavailable",
        "reason": reason,
        "scope": scope,
    }
    if suggested_tool:
        payload["suggested_tool"] = suggested_tool
    if provider == "chrome":
        payload.update({
            "status": "failed",
            "content": reason,
            "stop_parent": True,
            "stop_reason": "chrome_cli_worker_not_paired",
            "terminal_failure": True,
            "retryable": False,
            "recommended_next_action": "pair_cli_worker",
        })
    return json.dumps(payload, ensure_ascii=False)


async def runtime_annotate_tool_availability(
    matches: list[dict],
    entity_id: str,
    user_id: str,
) -> list[dict]:
    """Attach runtime availability metadata to search results.

    Non-MCP tools are always callable from this layer's perspective. MCP tools
    fail closed unless the current entity/user can prove connected credentials.
    """
    if not entity_id:
        for match in matches:
            runtime_mark_mcp_match_unavailable(
                match,
                "entity_id is required for MCP tool discovery.",
            )
        return matches

    try:
        from packages.core.database import async_session
        from packages.core.models.mcp import MCPServer
        from packages.core.services.agent_permission_service import (
            can_use_integration,
            provider_requires_integration_account_registry,
        )
        from packages.core.services.integration_service import coming_soon_servers
        from sqlalchemy import select
    except Exception:
        logger.exception("Failed to import MCP availability dependencies")
        for match in matches:
            runtime_mark_mcp_match_unavailable(match, "availability check failed")
        return matches

    provider_status: dict[str, dict] = {}
    provider_metadata: dict[str, dict] = {}
    provider_account_options: dict[str, list[dict]] = {}

    async with async_session() as db:
        providers = sorted({
            provider
            for match in matches
            if (
                provider := runtime_mcp_provider_from_tool_name(
                    str(match.get("name") or "")
                )
            )
        })
        integration_registry = None
        registry_error_reason = ""
        if providers:
            rows = list((await db.execute(
                select(MCPServer).where(MCPServer.server_key.in_(providers))
            )).scalars().all())
            coming_soon = coming_soon_servers()
            provider_metadata = {
                row.server_key: {
                    "name": row.name,
                    "auth_type": row.auth_type,
                    "transport": row.transport,
                    "endpoint": row.endpoint,
                    "coming_soon": row.server_key in coming_soon,
                }
                for row in rows
            }
            account_providers = [
                provider
                for provider in providers
                if provider_requires_integration_account_registry(provider)
            ]
            for provider in providers:
                if provider in account_providers:
                    continue
                try:
                    decision = await can_use_integration(
                        db,
                        user_id=user_id or "",
                        entity_id=entity_id,
                        provider=provider,
                        allow_env_fallback=False,
                    )
                    provider_status[provider] = {
                        "available": decision.allowed,
                        "reason": decision.reason,
                        "scope": decision.scope,
                        "account_id": decision.account_id,
                    }
                except Exception:
                    logger.exception(
                        "Non-account MCP availability check failed for provider %s",
                        provider,
                    )
                    provider_status[provider] = {
                        "available": False,
                        "reason": "availability check failed",
                        "scope": "none",
                    }
                provider_account_options[provider] = []
        else:
            account_providers = []
        if account_providers:
            from packages.core.services.integration_account_service import (
                try_load_runtime_integration_registry,
            )

            registry_result = await try_load_runtime_integration_registry(
                db,
                user_id=user_id or "",
                entity_id=entity_id,
                provider_keys=account_providers,
            )
            integration_registry = registry_result.registry
            if integration_registry is None:
                registry_error_reason = (
                    "Integration account registry could not be loaded; "
                    "tool availability fails closed. Retry discovery."
                )

        for match in matches:
            name = str(match.get("name") or "")
            if not name.startswith("mcp__"):
                runtime_mark_match_available(match)
                continue
            provider = runtime_mcp_provider_from_tool_name(name)
            if not provider:
                runtime_mark_match_available(match)
                continue
            metadata = provider_metadata.get(provider, {})
            if provider not in provider_status:
                requires_account_registry = (
                    provider_requires_integration_account_registry(provider)
                )
                if integration_registry is None and requires_account_registry:
                    provider_status[provider] = {
                        "available": False,
                        "reason": registry_error_reason,
                        "scope": "none",
                    }
                    provider_account_options[provider] = []
                    runtime_apply_mcp_availability(
                        match,
                        provider=provider,
                        metadata=metadata,
                        status=provider_status[provider],
                    )
                    continue
                try:
                    accounts = (
                        list(integration_registry.accounts_for(provider))
                        if integration_registry is not None
                        else []
                    )
                    provider_account_options[provider] = [
                        account.public_option() for account in accounts
                    ]
                    binding = (
                        integration_registry.integration(provider)
                        if integration_registry is not None
                        else None
                    )
                    if binding is not None and binding.requires_explicit_account:
                        scopes = {account.scope.value for account in accounts}
                        provider_status[provider] = {
                            "available": True,
                            "reason": (
                                f"The {provider} account registry is incomplete. "
                                "Select one exact known account; implicit default "
                                "selection remains blocked."
                            ),
                            "scope": next(iter(scopes)) if len(scopes) == 1 else "mixed",
                            "account_id": None,
                            "requires_explicit_account": True,
                        }
                    else:
                        decision = await can_use_integration(
                            db,
                            user_id=user_id or "",
                            entity_id=entity_id,
                            provider=provider,
                            integration_registry=integration_registry,
                            allow_env_fallback=False,
                        )
                        provider_status[provider] = {
                            "available": decision.allowed,
                            "reason": decision.reason,
                            "scope": decision.scope,
                            "account_id": decision.account_id,
                        }
                except Exception:
                    logger.exception(
                        "MCP availability check failed for provider %s",
                        provider,
                    )
                    provider_status[provider] = {
                        "available": False,
                        "reason": "availability check failed",
                        "scope": "none",
                    }
                    provider_account_options[provider] = []
            runtime_apply_mcp_availability(
                match,
                provider=provider,
                metadata=metadata,
                status=provider_status[provider],
            )
            account_options = provider_account_options.get(provider, [])
            if account_options:
                match["account_options"] = account_options
                match["default_account_id"] = provider_status[provider].get("account_id")

    return runtime_sort_available_matches(matches)


async def runtime_preflight_mcp_call(
    name: str,
    entity_id: str,
    user_id: str,
    arguments: Mapping[str, object] | None = None,
    declared_effect: str | None = None,
    allowed_account_ids: Iterable[str] | None = None,
    requires_explicit_account: bool = False,
    supports_all_accounts: bool | None = None,
    expected_account_registry_snapshot: (
        RuntimeDynamicMCPAccountRegistrySnapshot | None
    ) = None,
) -> RuntimeMCPCallPreflightResult:
    """Fail closed and pin the exact account before approval/provider code."""
    parts = name.split("__", 2)
    if len(parts) < 3:
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "error": f"Malformed MCP tool name: {name}",
            }),
        )
    _, provider, tool_name = parts
    supported_account_ids = frozenset(
        str(account_id).strip()
        for account_id in (allowed_account_ids or ())
        if str(account_id or "").strip()
    )
    selected_account_id = str(
        (arguments or {}).get("integration_account_id") or ""
    ).strip()
    from packages.core.services.integration_account_service import (
        IntegrationAccountSelectionMode,
    )

    try:
        selection_mode = IntegrationAccountSelectionMode.resolve(
            selector=(arguments or {}).get("integration_account_id"),
            requested=(arguments or {}).get("integration_account_selection"),
        )
    except ValueError as exc:
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "server": provider,
                "tool": tool_name,
                "error": "invalid_integration_account_selection",
                "reason": str(exc),
            }),
        )
    resolved_declared_effect = None
    if declared_effect is not None:
        from packages.core.services.official_remote_mcp import MCPActionEffect

        try:
            resolved_declared_effect = MCPActionEffect(
                str(declared_effect).strip().lower()
            )
        except ValueError:
            resolved_declared_effect = MCPActionEffect.WRITE
    effect_supports_all_accounts = (
        resolved_declared_effect is MCPActionEffect.READ
        if resolved_declared_effect is not None
        else runtime_mcp_tool_supports_all_accounts(name)
    )
    if (
        selection_mode is IntegrationAccountSelectionMode.ALL
        and not effect_supports_all_accounts
    ):
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "server": provider,
                "tool": tool_name,
                "error": "all_accounts_requires_read_only_tool",
                "reason": (
                    "All-account execution is restricted to tools explicitly "
                    "classified as read-only. Select one account for this action."
                ),
            }),
        )
    if (
        selection_mode is IntegrationAccountSelectionMode.ALL
        and supports_all_accounts is False
    ):
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "server": provider,
                "tool": tool_name,
                "error": "incompatible_account_contracts",
                "reason": (
                    "This action has different contracts across connected "
                    "accounts. Select one exact account."
                ),
            }),
        )
    if (
        selected_account_id
        and supported_account_ids
        and selected_account_id not in supported_account_ids
    ):
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "server": provider,
                "tool": tool_name,
                "error": "tool_unavailable_for_account",
                "reason": "This live MCP action is not exposed by the selected account.",
                "available_account_ids": sorted(supported_account_ids),
            }),
        )
    if (
        requires_explicit_account
        and not selected_account_id
        and expected_account_registry_snapshot is None
    ):
        return RuntimeMCPCallPreflightResult(
            blocked_result=json.dumps({
                "server": provider,
                "tool": tool_name,
                "error": "integration_account_registry_incomplete",
                "reason": (
                    "The connected-account registry is incomplete. Select an "
                    "exact known account or retry after the integration service recovers."
                ),
                "available_account_ids": sorted(supported_account_ids),
            }),
        )
    if not entity_id:
        return RuntimeMCPCallPreflightResult(
            blocked_result=runtime_mcp_credentials_unavailable_result(
                provider=provider,
                tool_name=tool_name,
                reason="entity_id is required for MCP tool calls.",
            ),
        )
    if (
        selection_mode is IntegrationAccountSelectionMode.ALL
        and expected_account_registry_snapshot is None
    ):
        # Complete read-only registries are fanned out by the handler, which
        # proves every exact account before provider dispatch.
        return RuntimeMCPCallPreflightResult()

    try:
        from packages.core.database import async_session
        from packages.core.models.mcp import MCPServer
        from packages.core.services.agent_permission_service import (
            can_use_integration,
            provider_requires_integration_account_registry,
        )
        from packages.core.services.integration_account_service import (
            IntegrationRegistryLoadStatus,
            load_runtime_integration_registry,
        )
        from sqlalchemy import select

        async with async_session() as db:
            integration_registry = (
                await load_runtime_integration_registry(
                    db,
                    user_id=user_id or "",
                    entity_id=entity_id,
                    provider_keys=[provider],
                )
                if provider_requires_integration_account_registry(provider)
                else None
            )
            registry_binding = (
                integration_registry.integration(provider)
                if integration_registry is not None
                else None
            )
            if (
                expected_account_registry_snapshot is not None
                and (
                    integration_registry is None
                    or not expected_account_registry_snapshot.matches_registry(
                        integration_registry,
                        provider=provider,
                    )
                )
            ):
                return RuntimeMCPCallPreflightResult(
                    blocked_result=(
                        RuntimeDynamicMCPFailureResultFactory.stale_binding(
                            provider=provider,
                            tool_name=tool_name,
                        )
                    ),
                )
            if expected_account_registry_snapshot is not None:
                server_row = (await db.execute(
                    select(MCPServer).where(MCPServer.server_key == provider)
                )).scalar_one_or_none()
                if (
                    server_row is None
                    or not expected_account_registry_snapshot.matches_server(
                        server_row
                    )
                ):
                    return RuntimeMCPCallPreflightResult(
                        blocked_result=(
                            RuntimeDynamicMCPFailureResultFactory.stale_binding(
                                provider=provider,
                                tool_name=tool_name,
                            )
                        ),
                    )
            if requires_explicit_account and not selected_account_id:
                return RuntimeMCPCallPreflightResult(
                    blocked_result=json.dumps({
                        "server": provider,
                        "tool": tool_name,
                        "error": "integration_account_registry_incomplete",
                        "reason": (
                            "The connected-account registry is incomplete. Select an "
                            "exact known account or retry after the integration service "
                            "recovers."
                        ),
                        "available_account_ids": sorted(supported_account_ids),
                    }),
                )
            if (
                not selected_account_id
                and registry_binding is not None
                and registry_binding.requires_explicit_account
            ):
                available_account_ids = [
                    account.id
                    for account in registry_binding.accounts
                    if (
                        not supported_account_ids
                        or account.id in supported_account_ids
                    )
                ]
                return RuntimeMCPCallPreflightResult(
                    blocked_result=json.dumps({
                        "server": provider,
                        "tool": tool_name,
                        "error": "integration_account_registry_incomplete",
                        "reason": (
                            "The connected-account registry is incomplete. Select an "
                            "exact known account or retry after the integration service "
                            "recovers."
                        ),
                        "registry_status": IntegrationRegistryLoadStatus.PARTIAL.value,
                        "registry_errors": list(registry_binding.load_errors),
                        "available_account_ids": available_account_ids,
                    }),
                )
            if selection_mode is IntegrationAccountSelectionMode.ALL:
                return RuntimeMCPCallPreflightResult(
                    integration_registry=integration_registry,
                )
            if (
                not selected_account_id
                and supported_account_ids
                and integration_registry is not None
            ):
                selected_account_id = next(
                    (
                        account.id
                        for account in integration_registry.accounts_for(provider)
                        if account.id in supported_account_ids
                    ),
                    "",
                )
                if not selected_account_id:
                    return RuntimeMCPCallPreflightResult(
                        blocked_result=runtime_mcp_credentials_unavailable_result(
                            provider=provider,
                            tool_name=tool_name,
                            reason=(
                                "No connected account currently exposes this live MCP action."
                            ),
                        ),
                    )
            decision = await can_use_integration(
                db,
                user_id=user_id or "",
                entity_id=entity_id,
                provider=provider,
                integration_account_id=selected_account_id or None,
                integration_registry=integration_registry,
                allow_env_fallback=False,
            )
    except Exception:
        return RuntimeMCPCallPreflightResult(
            blocked_result=runtime_mcp_credentials_unavailable_result(
                provider=provider,
                tool_name=tool_name,
                reason=(
                    "MCP credential availability check failed; refusing to call "
                    "an unverified MCP tool."
                ),
            ),
        )

    if decision.allowed:
        return RuntimeMCPCallPreflightResult(
            integration_account_id=decision.account_id,
            integration_registry=integration_registry,
        )

    return RuntimeMCPCallPreflightResult(
        blocked_result=runtime_mcp_credentials_unavailable_result(
            provider=provider,
            tool_name=tool_name,
            reason=decision.reason,
            scope=decision.scope,
            suggested_tool=(
                "generate_file" if tool_name == "generate_video" else None
            ),
        ),
    )


async def runtime_blocked_mcp_call_result(
    name: str,
    entity_id: str,
    user_id: str,
    arguments: Mapping[str, object] | None = None,
) -> str:
    """Compatibility projection for callers that only need a block result."""
    result = await runtime_preflight_mcp_call(
        name,
        entity_id,
        user_id,
        arguments,
    )
    return result.blocked_result
