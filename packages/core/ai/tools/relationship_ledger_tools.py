"""Tool boundary for the optional generic Relationship Ledger contract."""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.ledger_access import (
    WorkspaceLedgerConfigurationError,
    runtime_workspace_ledger_config,
    runtime_workspace_ledger_write_allowed,
    workspace_ledger_write_forbidden_payload,
)
from packages.core.services.relationship_ledger import (
    RELATIONSHIP_LEDGER_CONTRACT_ID,
    RelationshipContactConsentStatus,
    RelationshipContactDeliverabilityStatus,
    RelationshipContactKind,
    RelationshipContactStatus,
    RelationshipContactVerificationStatus,
    RelationshipLedgerError,
    RelationshipSubjectType,
    read_relationship_ledger,
    record_relationship_event,
)


logger = logging.getLogger(__name__)


CONTACT_CLEAR_FIELDS = [
    "label",
    "is_primary",
    "status",
    "verification_status",
    "deliverability_status",
    "consent_status",
    "verified_at",
    "source_url",
    "metadata",
]


CONTACT_POINT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactKind],
        },
        "value": {"type": "string"},
        "label": {"type": "string"},
        "is_primary": {"type": "boolean"},
        "status": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactStatus],
            "description": "Legacy summary status; prefer the three independent status fields.",
        },
        "verification_status": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactVerificationStatus],
        },
        "deliverability_status": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactDeliverabilityStatus],
        },
        "consent_status": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactConsentStatus],
        },
        "verified_at": {"type": "string"},
        "source_url": {"type": "string"},
        "metadata": {"type": "object"},
        "clear_fields": {
            "type": "array",
            "items": {"type": "string", "enum": CONTACT_CLEAR_FIELDS},
            "uniqueItems": True,
            "description": "Existing optional fields to clear while applying this contact patch.",
        },
    },
    "required": ["kind", "value"],
}


CONTACT_REFERENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [item.value for item in RelationshipContactKind],
        },
        "value": {"type": "string"},
    },
    "required": ["kind", "value"],
}


READ_RELATIONSHIP_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_relationship_ledger",
        "description": "Read the validated current relationship projection and recent interaction events.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "recent_limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    },
}


RECORD_RELATIONSHIP_LEDGER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_relationship_ledger",
        "description": (
            "Append one immutable CRM-style relationship event for a person, organization, "
            "customer, investor, partner, vendor, prospect, or lead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "identity_key": {"type": "string", "description": "Stable Workspace-local identity key."},
                "identity_aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Non-contact alternate ids for the same subject, such as external CRM ids. "
                        "Use contact_points for email, phone, website, and social routes."
                    ),
                },
                "identity_aliases_removed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Previously recorded aliases to remove from the current projection.",
                },
                "contact_points": {
                    "type": "array",
                    "items": CONTACT_POINT_SCHEMA,
                    "description": (
                        "Typed contact routes to add or patch without clearing omitted fields. "
                        "Supported kinds include email, phone, "
                        "LinkedIn, website, TikTok, Instagram, Facebook, WhatsApp, YouTube, Skool, "
                        "X, Threads, Bluesky, Telegram, Signal, WeChat, LINE, Discord, Slack, GitHub, "
                        "and other."
                    ),
                },
                "contact_points_removed": {
                    "type": "array",
                    "items": CONTACT_REFERENCE_SCHEMA,
                    "description": "Typed contact routes to remove from the current projection.",
                },
                "subject_type": {
                    "type": "string",
                    "enum": [item.value for item in RelationshipSubjectType],
                },
                "display_name": {"type": "string"},
                "relationship_type": {"type": "string"},
                "status": {"type": "string"},
                "stage": {"type": "string"},
                "event": {"type": "string", "description": "Interaction or lifecycle event name."},
                "occurred_at": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "payload": {"type": "object"},
                "evidence_refs": {"type": "array", "items": {"type": "object"}},
                "directory": {"type": "string"},
            },
            "required": ["identity_key", "event", "idempotency_key"],
        },
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error() -> str:
    return _json({"ok": False, "error": {"code": "missing_workspace_context", "message": "Entity and Workspace context are required"}})


def _service_error(exc: RelationshipLedgerError) -> str:
    return _json({"ok": False, "error": {"code": exc.code, "message": str(exc)}})


def _optional_list_argument(kwargs: dict[str, Any], field: str) -> list[Any] | None:
    if field not in kwargs or kwargs[field] is None:
        return None
    value = kwargs[field]
    if not isinstance(value, list):
        raise RelationshipLedgerError("invalid_input", f"{field} must be an array")
    return value


async def _read_relationship_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=RELATIONSHIP_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
        )
        result = await read_relationship_ledger(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            directory=str(config["directory"]),
            recent_limit=kwargs.get("recent_limit", 100),
        )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(RelationshipLedgerError(exc.code, str(exc)))
    except RelationshipLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to read relationship ledger")
        return _service_error(RelationshipLedgerError("ledger_read_failed", f"Relationship Ledger could not be read: {exc}"))
    return _json({"ok": True, **result})


async def _record_relationship_ledger(entity_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id or not context.workspace_id:
        return _context_error()
    if not await runtime_workspace_ledger_write_allowed(context):
        return _json(workspace_ledger_write_forbidden_payload())
    try:
        config = await runtime_workspace_ledger_config(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            contract_id=RELATIONSHIP_LEDGER_CONTRACT_ID,
            requested_directory=kwargs.get("directory"),
        )
        result = await record_relationship_event(
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            identity_key=str(kwargs.get("identity_key") or ""),
            identity_aliases=_optional_list_argument(kwargs, "identity_aliases"),
            identity_aliases_removed=_optional_list_argument(kwargs, "identity_aliases_removed"),
            contact_points=_optional_list_argument(kwargs, "contact_points"),
            contact_points_removed=_optional_list_argument(kwargs, "contact_points_removed"),
            subject_type=(
                str(kwargs["subject_type"])
                if kwargs.get("subject_type") is not None
                else None
            ),
            display_name=kwargs.get("display_name"),
            relationship_type=kwargs.get("relationship_type"),
            status=kwargs.get("status"),
            stage=kwargs.get("stage"),
            event=str(kwargs.get("event") or ""),
            occurred_at=kwargs.get("occurred_at"),
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
            payload=kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else None,
            evidence_refs=kwargs.get("evidence_refs") if isinstance(kwargs.get("evidence_refs"), list) else None,
            directory=str(config["directory"]),
            agent_id=context.agent_id,
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            workflow_run_id=context.workflow_run_id,
        )
    except WorkspaceLedgerConfigurationError as exc:
        return _service_error(RelationshipLedgerError(exc.code, str(exc)))
    except RelationshipLedgerError as exc:
        return _service_error(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to record relationship ledger")
        return _service_error(RelationshipLedgerError("ledger_write_failed", f"Relationship Ledger could not be recorded: {exc}"))
    return _json(result)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (READ_RELATIONSHIP_LEDGER_SCHEMA, _read_relationship_ledger),
        (RECORD_RELATIONSHIP_LEDGER_SCHEMA, _record_relationship_ledger),
    ]


__all__ = ["get_tools"]
