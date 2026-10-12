"""Native Workflow delivery verification and source-media archiving."""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.ai.runtime.file_actions import runtime_guard_file_mutation
from packages.core.ai.runtime.file_contracts import FileMutationAction
from packages.core.database import async_session
from packages.core.services.file_collections import archive_file_collection, verify_file_collection


VERIFY_FILE_COLLECTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verify_file_collection",
        "description": "Verify explicit existing Knowledge Documents and return host-verified delivery metadata. Missing required roles returns delivery_verified false.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string"},
                            "role": {"type": "string"},
                            "group": {"type": "string"},
                            "name": {"type": "string", "description": "Candidate label; canonical name is returned from Knowledge."},
                        },
                        "required": ["document_id", "role", "group"],
                    },
                },
                "required_roles": {"type": "array", "items": {"type": "string"}},
                "verification_scope": {
                    "type": "string", "enum": ["preparation", "delivery"], "default": "delivery",
                    "description": "Preparation verifies saved stage files with empty expected_sources and reports preparation_completed only. Delivery also verifies fixed source receipts.",
                },
                "expected_sources": {
                    "type": "array",
                    "description": "Fixed source receipts selected earlier in this run; never infer these from agent output.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string"},
                            "role": {"type": "string", "enum": ["source_audio", "source_cover"]},
                            "source_sha256": {"type": "string", "description": "Optional host-issued receipt from an earlier verify_file_collection call."},
                        },
                        "required": ["document_id", "role"],
                    },
                },
                "confirmed_version": {"type": "string", "description": "Fixed approved release or workflow version."},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "pending_items": {"type": "array", "items": {}},
            },
            "required": ["files", "required_roles", "expected_sources", "confirmed_version", "title", "summary"],
        },
    },
}

ARCHIVE_FILE_COLLECTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "archive_file_collection",
        "description": "Copy verified source media into this Workflow lineage's durable archive and return archive Document receipts.",
        "parameters": {
            "type": "object",
            "properties": {"collection": {"type": "object"}},
            "required": ["collection"],
        },
    },
}


async def _verify_file_collection(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)
    async with async_session() as db:
        payload = await verify_file_collection(
            db,
            entity_id=entity_id,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            files=kwargs.get("files"),
            required_roles=kwargs.get("required_roles"),
            expected_sources=kwargs.get("expected_sources"),
            confirmed_version=kwargs.get("confirmed_version"),
            title=kwargs.get("title"),
            summary=kwargs.get("summary"),
            pending_items=kwargs.get("pending_items"),
            verification_scope=kwargs.get("verification_scope", "delivery"),
        )
    return json.dumps(payload, ensure_ascii=False)


async def _archive_file_collection(entity_id: str = "", user_id: str = "", **kwargs: Any) -> str:
    context = runtime_tool_call_context_from_handler(kwargs, user_id=user_id)

    async def guard(paths: list[str]) -> str | None:
        return await runtime_guard_file_mutation(
            entity_id=entity_id, user_id=context.user_id, conversation_id=context.conversation_id,
            workspace_id=context.workspace_id, task_id=context.task_id, runtime_envelope=context.runtime_envelope,
            tool_name="archive_file_collection", action=FileMutationAction.SAVE_FILE, paths=paths,
            content_preview={"archive": paths},
        )

    async with async_session() as db:
        payload = await archive_file_collection(
            db, entity_id=entity_id, workspace_id=context.workspace_id, user_id=context.user_id,
            workflow_run_id=context.workflow_run_id,
            workflow_lineage_root_run_id=context.workflow_lineage_root_run_id,
            collection=kwargs.get("collection"), write_guard=guard,
        )
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (VERIFY_FILE_COLLECTION_SCHEMA, _verify_file_collection),
        (ARCHIVE_FILE_COLLECTION_SCHEMA, _archive_file_collection),
    ]
