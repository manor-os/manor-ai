from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock

import pytest


def _module():
    return importlib.import_module("packages.core.ai.tools.stickman_topic_tools")


def test_topic_tool_schemas_are_workspace_scoped() -> None:
    schemas = {schema["function"]["name"]: schema["function"]["parameters"] for schema, _ in _module().get_tools()}

    assert set(schemas) == {
        "read_stickman_topic_ledger",
        "record_stickman_topic_ledger",
    }
    assert schemas["read_stickman_topic_ledger"]["properties"] == {}
    assert "path" not in schemas["record_stickman_topic_ledger"]["properties"]
    assert "workspace_id" not in schemas["record_stickman_topic_ledger"]["properties"]
    assert "backfill_used" in schemas["record_stickman_topic_ledger"]["properties"]["action"]["enum"]


@pytest.mark.asyncio
async def test_read_tool_returns_service_history(monkeypatch) -> None:
    module = _module()
    read = AsyncMock(
        return_value={
            "schema_version": 1,
            "run_key": "01KZWLEDGERRUN000000000001",
            "entry_count": 1,
            "used_topic_count": 1,
            "used_topics": ["A used Topic"],
            "recent_entries": [],
        }
    )
    monkeypatch.setattr(module, "read_topic_ledger", read)

    result = json.loads(
        await module._read_stickman_topic_ledger(
            entity_id="entity-1",
            workspace_id="workspace-1",
        )
    )

    assert result["ok"] is True
    assert result["used_topics"] == ["A used Topic"]
    read.assert_awaited_once_with(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )


@pytest.mark.asyncio
async def test_tools_require_workspace_context() -> None:
    result = json.loads(await _module()._read_stickman_topic_ledger(entity_id=""))

    assert result == {
        "ok": False,
        "error": {
            "code": "missing_workspace_context",
            "message": "Entity and Workspace context are required",
        },
    }
