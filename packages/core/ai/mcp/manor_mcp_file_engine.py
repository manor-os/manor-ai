"""First-party Manor File Engine MCP.

Thin MCP wrapper over the same runtime file tools used by normal agent calls.
It exists so skills and MCP discovery expose one create/read/patch contract
without forking Knowledge write, approval, or projection behavior.
"""
from __future__ import annotations

import contextvars
import json
from typing import Any, Dict, List

from packages.core.ai.runtime import file_engine as runtime_file_engine


_call_ctx_var: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "manor_file_engine_mcp_call_ctx",
    default={},
)


def set_call_context(ctx: Dict[str, str]) -> None:
    _call_ctx_var.set({
        k: str(v) for k, v in (ctx or {}).items() if v is not None
    })


def clear_call_context() -> None:
    _call_ctx_var.set({})


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "inspect",
            "description": "Inspect unified file-engine generate and patch capabilities.",
            "parameters": runtime_file_engine.runtime_inspect_file_engine_parameters(),
        },
        {
            "name": "generate",
            "description": "Generate a new Knowledge file through generate_file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "generate_file kind, e.g. document, presentation, spreadsheet."},
                    "prompt": {"type": "string"},
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "file_type": {"type": "string"},
                    "params": {"type": "object", "additionalProperties": True},
                    "approval_token": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["kind"],
            },
        },
        {
            "name": "patch",
            "description": "Patch an existing Knowledge file through patch_file.",
            "parameters": runtime_file_engine.runtime_patch_file_parameters(),
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    del bearer_token
    args = dict(arguments or {})
    ctx = _call_ctx_var.get()
    entity_id = str(ctx.get("entity_id") or "")
    user_id = str(ctx.get("user_id") or "")
    common_ctx = {
        key: value
        for key in ("conversation_id", "workspace_id", "task_id")
        if (value := ctx.get(key))
    }
    try:
        if name == "inspect":
            text = await runtime_file_engine.runtime_inspect_file_engine(
                entity_id=entity_id,
                **args,
            )
        elif name == "generate":
            text = await runtime_file_engine.runtime_generate_file(
                entity_id=entity_id,
                user_id=user_id,
                **common_ctx,
                **args,
            )
        elif name == "patch":
            text = await runtime_file_engine.runtime_patch_file(
                entity_id=entity_id,
                user_id=user_id,
                **common_ctx,
                **args,
            )
        else:
            return _error(f"Unknown file_engine tool: {name}")
        data = json.loads(text)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc))


def _ok(data: Any) -> Dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(data, ensure_ascii=False, indent=2, default=str),
        }],
        "structuredContent": data,
        "isError": bool(isinstance(data, dict) and data.get("error")),
    }


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}
