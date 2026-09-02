"""Runtime-owned configuration and authorization guards for Workspace Ledgers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import RuntimeToolCallContext
from packages.core.database import async_session
from packages.core.models.workspace import Workspace
from packages.core.services.ledger_query_service import (
    workspace_queryable_ledger_configs,
)
from packages.core.services.workspace_access import user_can_write_workspace_artifacts


_BACKGROUND_WRITE_SURFACES = {
    ChatSurface.SCHEDULED_AGENT_RUN.value,
    ChatSurface.WORKFLOW_AGENT_STEP.value,
}
_EXTERNAL_SURFACES = {
    ChatSurface.EXTERNAL_CHANNEL_CHAT.value,
    ChatSurface.PUBLIC_CUSTOMER_CHAT.value,
}
_EXTERNAL_PROFILES = {
    RuntimeProfile.EXTERNAL_CHANNEL_SAFE.value,
    RuntimeProfile.EXTERNAL_CUSTOMER_SAFE.value,
}


class WorkspaceLedgerConfigurationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _runtime_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text or None


async def runtime_workspace_ledger_write_allowed(
    context: RuntimeToolCallContext,
) -> bool:
    """Authorize a Ledger write at the Runtime tool-execution boundary."""

    if not context.workspace_id:
        return False
    envelope = context.runtime_envelope
    surface = _runtime_value(getattr(envelope, "surface", None))
    profile = _runtime_value(getattr(envelope, "profile", None))
    if surface in _EXTERNAL_SURFACES or profile in _EXTERNAL_PROFILES:
        return False
    if surface in _BACKGROUND_WRITE_SURFACES:
        return True
    if not context.user_id:
        return surface is None
    async with async_session() as db:
        return await user_can_write_workspace_artifacts(
            db,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
        )


async def runtime_workspace_ledger_config(
    *,
    entity_id: str,
    workspace_id: str,
    contract_id: str,
    requested_directory: object = None,
    requested_legacy_directories: object = None,
    requested_legacy_identity_fields: object = None,
) -> dict[str, Any]:
    """Resolve an installed Ledger config and reject caller-selected storage."""

    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace)
                .where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    configs = workspace_queryable_ledger_configs(
        getattr(workspace, "settings", None) if workspace is not None else None
    )
    config = configs.get(contract_id)
    if config is None:
        raise WorkspaceLedgerConfigurationError(
            "ledger_contract_not_installed",
            "The requested Ledger is not installed in this Workspace",
        )

    configured_directory = str(config.get("directory") or "").strip()
    requested = str(requested_directory or "").strip()
    if requested and requested != configured_directory:
        raise WorkspaceLedgerConfigurationError(
            "ledger_directory_not_allowed",
            "directory must match the installed Workspace ledger configuration",
        )

    def requested_values(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple, set)):
            raise WorkspaceLedgerConfigurationError(
                "ledger_configuration_invalid",
                "Ledger compatibility fields must be arrays",
            )
        return tuple(str(item).strip() for item in value if str(item).strip())

    configured_legacy_directories = tuple(config.get("legacy_directories") or ())
    requested_directories = requested_values(requested_legacy_directories)
    if requested_directories and not set(requested_directories).issubset(
        set(configured_legacy_directories)
    ):
        raise WorkspaceLedgerConfigurationError(
            "ledger_directory_not_allowed",
            "legacy_directories must match the installed Workspace ledger configuration",
        )

    configured_identity_fields = tuple(config.get("legacy_identity_fields") or ())
    requested_identity_fields = requested_values(requested_legacy_identity_fields)
    if requested_identity_fields and not set(requested_identity_fields).issubset(
        set(configured_identity_fields)
    ):
        raise WorkspaceLedgerConfigurationError(
            "ledger_field_not_allowed",
            "legacy_identity_fields must match the installed Workspace ledger configuration",
        )
    return config


def workspace_ledger_write_forbidden_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "workspace_ledger_write_forbidden",
            "message": "You do not have permission to write this Workspace Ledger",
        },
    }


__all__ = [
    "WorkspaceLedgerConfigurationError",
    "runtime_workspace_ledger_config",
    "runtime_workspace_ledger_write_allowed",
    "workspace_ledger_write_forbidden_payload",
]
