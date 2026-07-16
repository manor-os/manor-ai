from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.dashboard_submission import (
    DASHBOARD_SUBMIT_TOOL_NAME,
    runtime_record_dashboard_submission,
    runtime_unvalidated_dashboard_changes,
)


def _data_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"},
            "source": {
                "type": "string",
                "enum": [
                    "tasks",
                    "workspaces",
                    "activity",
                    "task_trends",
                    "stats",
                    "news",
                    "stocks",
                    "http_json",
                    "tool",
                ],
            },
            "params": {"type": "object"},
            "url": {"type": "string", "maxLength": 2000},
            "tool_name": {"type": "string"},
            "tool_arguments": {"type": "object"},
            "refresh_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
        },
        "required": ["key", "source"],
    }


def _module_code_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [1]},
            "runtime": {"type": "string", "enum": ["sandboxed_html"]},
            "html": {"type": "string"},
            "css": {"type": "string"},
            "javascript": {"type": "string"},
            "data_requests": {
                "type": "array",
                "maxItems": 8,
                "items": _data_request_schema(),
            },
        },
        "required": [
            "version",
            "runtime",
            "html",
            "css",
            "javascript",
            "data_requests",
        ],
    }


def _schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": DASHBOARD_SUBMIT_TOOL_NAME,
            "description": (
                "Submit the complete, loadable Dashboard layout/module code after every create/update "
                "bundle has passed code(action='dashboard_module_validate')"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "widgets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "visible": {"type": "boolean"},
                            },
                            "required": ["id", "visible"],
                        },
                    },
                    "module_changes": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["create", "update", "remove"],
                                },
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "description": {"type": ["string", "null"]},
                                "visible": {"type": "boolean"},
                                "size": {
                                    "type": "string",
                                    "enum": ["compact", "wide"],
                                },
                                "code": _module_code_schema(),
                            },
                            "required": ["action"],
                        },
                    },
                    "assistant_message": {"type": "string", "maxLength": 400},
                },
                "required": ["widgets", "module_changes"],
            },
        },
    }


async def _submit_dashboard_module(
    widgets: list[dict[str, Any]],
    module_changes: list[dict[str, Any]],
    assistant_message: str | None = None,
    **_kwargs: Any,
) -> str:
    unvalidated = runtime_unvalidated_dashboard_changes(module_changes)
    if unvalidated:
        return json.dumps(
            {
                "status": "rejected",
                "error": "Dashboard module code must pass Code validation before submission.",
                "unvalidated_change_indexes": unvalidated,
                "next_step": (
                    "Call code(action='dashboard_module_validate', params={'code': <bundle>}) "
                    "for each complete create/update bundle, revise all findings, then submit again."
                ),
            }
        )
    runtime_record_dashboard_submission(
        {
            "widgets": widgets,
            "module_changes": module_changes,
            **({"assistant_message": assistant_message} if assistant_message else {}),
        }
    )
    return json.dumps(
        {
            "status": "accepted",
            "message": "Dashboard code captured. Briefly summarize the result for the user.",
        }
    )


def get_tools():
    return [(_schema(), _submit_dashboard_module)]
