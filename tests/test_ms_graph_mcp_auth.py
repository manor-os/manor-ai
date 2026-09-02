"""Microsoft Graph MCP wrappers must reject missing bearer tokens locally."""

from __future__ import annotations

import pytest

from packages.core.ai.mcp import ms_calendar, ms_excel, ms_teams, onedrive, outlook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "tool"),
    [
        (outlook, "list_messages"),
        (ms_teams, "list_my_teams"),
        (onedrive, "list_files"),
        (ms_calendar, "list_calendars"),
        (ms_excel, "list_worksheets"),
    ],
)
async def test_graph_mcp_rejects_missing_bearer_token(monkeypatch, module, tool):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Graph API must not be called without a bearer token")

    monkeypatch.setattr(module, "_api", fail_if_called)

    arguments = {"file_id": "qa-file"} if module is ms_excel else {}
    result = await module.call_tool(tool, arguments, "   ")

    assert result["isError"] is True
    assert "access token is missing" in result["content"][0]["text"].lower()
