"""Decode Chrome's MCP error envelope without discarding execution evidence."""

from __future__ import annotations

import json
from typing import Any


def normalize_chrome_result(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error") != "tool_error":
        return payload
    detail = payload.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (ValueError, TypeError):
            return payload
    if not isinstance(detail, dict):
        return payload
    # Outer Runtime controls remain authoritative. Keep the MCP failure marker,
    # while exposing the extension's status and execution facts as typed fields.
    result = {**detail, **{key: value for key, value in payload.items() if key != "detail"}}
    if not result.get("reason") and isinstance(detail.get("error"), str):
        if detail["error"] != "tool_error":
            result["reason"] = detail["error"]
    return result
