from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrant
from packages.core.ai.runtime.tool_discovery import (
    runtime_mcp_provider_from_tool_name,
)
from packages.core.services.integration_account_service import (
    IntegrationRegistryLoadStatus,
)
from packages.core.services.official_remote_mcp import OfficialRemoteMCPProvider


logger = logging.getLogger(__name__)


class RuntimeDynamicMCPServerTransport(StrEnum):
    """Transport supported by actor-scoped official remote discovery."""

    HTTP = "http"


@dataclass(frozen=True, slots=True)
class RuntimeDynamicMCPAccountRegistrySnapshot:
    """Account and remote-server identity proven during live discovery."""

    account_ids: tuple[str, ...]
    status: IntegrationRegistryLoadStatus
    endpoint: str
    token_in: str
    transport: RuntimeDynamicMCPServerTransport = (
        RuntimeDynamicMCPServerTransport.HTTP
    )

    def __post_init__(self) -> None:
        if not isinstance(self.status, IntegrationRegistryLoadStatus):
            object.__setattr__(
                self,
                "status",
                IntegrationRegistryLoadStatus(self.status),
            )
        object.__setattr__(self, "endpoint", str(self.endpoint or "").strip())
        object.__setattr__(
            self,
            "token_in",
            str(self.token_in or "header").strip().lower() or "header",
        )
        if not isinstance(self.transport, RuntimeDynamicMCPServerTransport):
            object.__setattr__(
                self,
                "transport",
                RuntimeDynamicMCPServerTransport(self.transport),
            )

    def matches_registry(self, registry: Any, *, provider: str) -> bool:
        """Compare the actor account boundary within one loaded registry."""

        current_status = (
            IntegrationRegistryLoadStatus.PARTIAL
            if tuple(getattr(registry, "load_errors", ()) or ())
            else IntegrationRegistryLoadStatus.READY
        )
        current_account_ids = tuple(
            str(account.id) for account in registry.accounts_for(provider)
        )
        return (
            current_account_ids == self.account_ids
            and current_status is self.status
        )

    def matches_server(self, server: Any) -> bool:
        """Compare the credential-free remote transport discovery boundary."""

        server_config = (
            server.default_config
            if isinstance(getattr(server, "default_config", None), dict)
            else {}
        )
        endpoint = str(getattr(server, "endpoint", None) or "").strip()
        token_in = str(server_config.get("mcp_token_in") or "header").strip().lower()
        transport = str(getattr(server, "transport", None) or "").strip().lower()
        return (
            endpoint == self.endpoint
            and token_in == self.token_in
            and transport == self.transport.value
        )


class RuntimeDynamicMCPAccountRegistrySnapshotFactory:
    """Build the comparable account boundary used by the dynamic-tool cache."""

    @staticmethod
    def from_discovered(
        tool: Any,
    ) -> RuntimeDynamicMCPAccountRegistrySnapshot | None:
        account_ids = getattr(tool, "registry_account_ids", None)
        status = getattr(tool, "registry_status", None)
        endpoint = getattr(tool, "endpoint", None)
        if account_ids is None or status is None or endpoint is None:
            return None
        return RuntimeDynamicMCPAccountRegistrySnapshot(
            account_ids=tuple(str(account_id) for account_id in account_ids),
            status=IntegrationRegistryLoadStatus(status),
            endpoint=str(endpoint),
            token_in=str(getattr(tool, "token_in", None) or "header"),
            transport=RuntimeDynamicMCPServerTransport.HTTP,
        )

    @staticmethod
    def from_registry_result(
        result: Any,
        *,
        provider: str,
        endpoint: str,
        token_in: str,
        transport: str,
    ) -> RuntimeDynamicMCPAccountRegistrySnapshot | None:
        registry = getattr(result, "registry", None)
        if registry is None:
            return None
        return RuntimeDynamicMCPAccountRegistrySnapshot(
            account_ids=tuple(
                str(account.id) for account in registry.accounts_for(provider)
            ),
            status=IntegrationRegistryLoadStatus(result.status),
            endpoint=endpoint,
            token_in=token_in,
            transport=RuntimeDynamicMCPServerTransport(transport),
        )


@dataclass(frozen=True, slots=True)
class RuntimeDynamicMCPToolBinding:
    """Actor-scoped executable identity proven by live MCP discovery."""

    provider: str
    action: str
    expires_at: float
    schema: dict[str, Any] | None = None
    account_ids: tuple[str, ...] = ()
    effect: str = "write"
    requires_explicit_account: bool = False
    supports_all_accounts: bool = True
    incomplete_account_ids: tuple[str, ...] = ()
    account_registry_snapshot: RuntimeDynamicMCPAccountRegistrySnapshot | None = None


class RuntimeDynamicMCPToolBindingFactory:
    """Normalize a vendor discovery result into the Runtime cache contract."""

    @staticmethod
    def from_discovered(
        tool: Any,
        *,
        expires_at: float,
    ) -> RuntimeDynamicMCPToolBinding:
        raw_effect = getattr(tool, "effect", "write")
        effect = getattr(raw_effect, "value", raw_effect)
        return RuntimeDynamicMCPToolBinding(
            provider=str(getattr(tool, "provider", "") or ""),
            action=str(getattr(tool, "action", "") or ""),
            schema=(
                dict(getattr(tool, "schema"))
                if isinstance(getattr(tool, "schema", None), dict)
                else None
            ),
            account_ids=tuple(getattr(tool, "account_ids", ()) or ()),
            effect=str(effect or "write"),
            requires_explicit_account=bool(
                getattr(tool, "requires_explicit_account", False)
            ),
            supports_all_accounts=bool(
                getattr(tool, "supports_all_accounts", True)
            ),
            incomplete_account_ids=tuple(
                getattr(tool, "incomplete_account_ids", ()) or ()
            ),
            account_registry_snapshot=(
                RuntimeDynamicMCPAccountRegistrySnapshotFactory.from_discovered(
                    tool
                )
            ),
            expires_at=expires_at,
        )


class RuntimeDynamicMCPToolGrantFactory:
    """Project a cached binding into one run-local authorization envelope."""

    @staticmethod
    def from_binding(
        tool_name: str,
        binding: RuntimeDynamicMCPToolBinding,
    ) -> RuntimeDiscoveredToolGrant:
        return RuntimeDiscoveredToolGrant(
            name=tool_name,
            effect=binding.effect,
            account_ids=binding.account_ids,
            requires_explicit_account=binding.requires_explicit_account,
            supports_all_accounts=binding.supports_all_accounts,
            incomplete_account_ids=binding.incomplete_account_ids,
            account_registry_snapshot=binding.account_registry_snapshot,
        )


class RuntimeDynamicMCPRehydrationStatus(StrEnum):
    """Typed outcome so official unavailability cannot look non-applicable."""

    NOT_OFFICIAL = "not_official"
    BOUND = "bound"
    OFFICIAL_UNAVAILABLE = "official_unavailable"
    DISCOVERY_FAILED = "discovery_failed"


class RuntimeDynamicMCPErrorCode(StrEnum):
    """Internal fail-closed results that ToolPool may handle once."""

    STALE_BINDING = "stale_dynamic_mcp_binding"


@dataclass(frozen=True, slots=True)
class RuntimeDynamicMCPRehydrationResult:
    status: RuntimeDynamicMCPRehydrationStatus
    tool_name: str
    provider: str | None = None
    binding: RuntimeDynamicMCPToolBinding | None = None
    reason: str | None = None


class RuntimeDynamicMCPFailureResultFactory:
    """Map typed live-resolution failures to the stable tool-result contract."""

    @staticmethod
    def create(resolution: RuntimeDynamicMCPRehydrationResult) -> str:
        if resolution.status is RuntimeDynamicMCPRehydrationStatus.DISCOVERY_FAILED:
            return json.dumps({
                "server": resolution.provider,
                "tool": resolution.tool_name,
                "error": "mcp_tool_discovery_failed",
                "reason": (
                    resolution.reason
                    or "Live MCP discovery failed; no account action was executed."
                ),
                "retryable": True,
            })
        if (
            resolution.status
            is RuntimeDynamicMCPRehydrationStatus.OFFICIAL_UNAVAILABLE
        ):
            return json.dumps({
                "server": resolution.provider,
                "tool": resolution.tool_name,
                "error": "tool_unavailable_for_account",
                "reason": (
                    "No connected account currently exposes this live MCP action."
                ),
                "available_account_ids": [],
            })
        raise ValueError(
            f"Cannot build an MCP failure result for status {resolution.status.value}."
        )

    @staticmethod
    def stale_binding(*, provider: str, tool_name: str) -> str:
        """Build the internal retry signal for a changed discovery boundary."""

        return json.dumps({
            "server": provider,
            "tool": tool_name,
            "error": RuntimeDynamicMCPErrorCode.STALE_BINDING.value,
            "reason": (
                "The connected-account or remote-server boundary changed after "
                "live tool discovery; rediscovery is required before execution."
            ),
            "retryable": True,
        })


async def runtime_discover_official_remote_mcp_tools(
    *,
    provider_keys: frozenset[str],
    entity_id: str,
    user_id: str,
) -> dict[str, list[Any]]:
    """Runtime-owned bridge to actor-scoped official MCP discovery."""

    from packages.core.ai.tools.mcp_builtin import (
        discover_official_remote_tool_schemas,
    )

    return await discover_official_remote_tool_schemas(
        provider_keys=provider_keys,
        entity_id=entity_id,
        user_id=user_id,
    )


async def runtime_current_dynamic_mcp_account_registry_snapshot(
    *,
    provider: str,
    entity_id: str,
    user_id: str,
) -> RuntimeDynamicMCPAccountRegistrySnapshot | None:
    """Load the current actor-scoped account boundary without credentials."""

    from packages.core.database import async_session
    from packages.core.models.mcp import MCPServer
    from packages.core.services.integration_account_service import (
        try_load_runtime_integration_registry,
    )
    from sqlalchemy import select

    try:
        async with async_session() as db:
            result = await try_load_runtime_integration_registry(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider_keys=[provider],
            )
            server_row = (await db.execute(
                select(MCPServer).where(MCPServer.server_key == provider)
            )).scalar_one_or_none()
    except Exception:
        logger.warning(
            "Could not validate the dynamic MCP account registry for %s",
            provider,
            exc_info=True,
        )
        return None
    if server_row is None or not str(server_row.endpoint or "").strip():
        return None
    server_config = (
        server_row.default_config
        if isinstance(server_row.default_config, dict)
        else {}
    )
    return RuntimeDynamicMCPAccountRegistrySnapshotFactory.from_registry_result(
        result,
        provider=provider,
        endpoint=str(server_row.endpoint),
        token_in=str(server_config.get("mcp_token_in") or "header"),
        transport=str(server_row.transport),
    )


async def runtime_dynamic_mcp_binding_is_current(
    binding: RuntimeDynamicMCPToolBinding,
    *,
    entity_id: str,
    user_id: str,
) -> bool:
    """Return whether a cached binding still matches the actor's registry."""

    cached = binding.account_registry_snapshot
    if cached is None:
        return False
    current = await runtime_current_dynamic_mcp_account_registry_snapshot(
        provider=binding.provider,
        entity_id=entity_id,
        user_id=user_id,
    )
    return current is not None and current == cached


async def runtime_rehydrate_dynamic_mcp_tool_binding(
    tool_name: str,
    *,
    entity_id: str,
    user_id: str,
    expires_at: float,
) -> RuntimeDynamicMCPRehydrationResult:
    """Re-discover one persisted dynamic tool name after process restart."""

    provider = runtime_mcp_provider_from_tool_name(tool_name)
    if not provider:
        return RuntimeDynamicMCPRehydrationResult(
            status=RuntimeDynamicMCPRehydrationStatus.NOT_OFFICIAL,
            tool_name=tool_name,
        )
    try:
        OfficialRemoteMCPProvider(provider)
    except ValueError:
        return RuntimeDynamicMCPRehydrationResult(
            status=RuntimeDynamicMCPRehydrationStatus.NOT_OFFICIAL,
            tool_name=tool_name,
            provider=provider,
        )
    try:
        discovered = await runtime_discover_official_remote_mcp_tools(
            provider_keys=frozenset({provider}),
            entity_id=entity_id,
            user_id=user_id,
        )
        tool = next(
            (
                candidate
                for candidate in discovered.get(provider, ())
                if candidate.name == tool_name
            ),
            None,
        )
        if tool is None:
            return RuntimeDynamicMCPRehydrationResult(
                status=RuntimeDynamicMCPRehydrationStatus.OFFICIAL_UNAVAILABLE,
                tool_name=tool_name,
                provider=provider,
            )
        binding = RuntimeDynamicMCPToolBindingFactory.from_discovered(
            tool,
            expires_at=expires_at,
        )
    except Exception as exc:
        logger.warning(
            "Dynamic MCP discovery failed for provider %s and tool %s (%s)",
            provider,
            tool_name,
            type(exc).__name__,
        )
        return RuntimeDynamicMCPRehydrationResult(
            status=RuntimeDynamicMCPRehydrationStatus.DISCOVERY_FAILED,
            tool_name=tool_name,
            provider=provider,
            reason="Live MCP discovery failed; no account action was executed.",
        )
    return RuntimeDynamicMCPRehydrationResult(
        status=RuntimeDynamicMCPRehydrationStatus.BOUND,
        tool_name=tool_name,
        provider=provider,
        binding=binding,
    )


def runtime_dynamic_mcp_failure_result(
    resolution: RuntimeDynamicMCPRehydrationResult,
) -> str:
    """Return a stable fail-closed result before approval or provider dispatch."""

    return RuntimeDynamicMCPFailureResultFactory.create(resolution)


def runtime_dynamic_mcp_result_is_stale(result: Any) -> bool:
    """Return whether preflight rejected a changed discovery boundary."""

    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("error") == RuntimeDynamicMCPErrorCode.STALE_BINDING.value
    )


def runtime_dynamic_mcp_tool_handler(
    binding: RuntimeDynamicMCPToolBinding,
) -> Callable | None:
    """Build the executable handler while keeping ToolPool implementation-free."""

    from packages.core.ai.tools.mcp_builtin import (
        build_official_remote_dynamic_handler,
    )

    kwargs = {
        "account_ids": binding.account_ids,
        "effect": binding.effect,
        "requires_explicit_account": binding.requires_explicit_account,
        "incomplete_account_ids": binding.incomplete_account_ids,
    }
    if not binding.supports_all_accounts:
        kwargs["supports_all_accounts"] = False
    return build_official_remote_dynamic_handler(
        binding.provider,
        binding.action,
        **kwargs,
    )
