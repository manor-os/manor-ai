from __future__ import annotations

import pytest


class _Response:
    status_code = 200
    text = "{}"

    @property
    def is_success(self) -> bool:
        return True

    def json(self) -> dict:
        return {"value": []}


class _Client:
    calls: list[dict] = []
    response = _Response()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "params": params,
            }
        )
        return self.response


@pytest.fixture
def excel_http(monkeypatch):
    from packages.core.ai.mcp import ms_excel

    _Client.calls = []
    monkeypatch.setattr(ms_excel.httpx, "AsyncClient", _Client)
    return ms_excel


@pytest.mark.asyncio
async def test_table_rows_rejects_non_positive_top_without_http(excel_http):
    result = await excel_http.call_tool(
        "get_table_rows",
        {"file_id": "workbook-1", "table": "Table1", "top": 0},
        "excel-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_table_rows_rejects_fractional_top_without_http(excel_http):
    result = await excel_http.call_tool(
        "get_table_rows",
        {"file_id": "workbook-1", "table": "Table1", "top": 1.5},
        "excel-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_workbook_and_worksheet_ids_are_path_encoded(excel_http):
    await excel_http.call_tool(
        "read_used_range",
        {"file_id": "book?id#fragment", "worksheet": "Sheet?id#fragment"},
        "excel-test-token",
    )

    assert _Client.calls[0]["url"].endswith(
        "/items/book%3Fid%23fragment/workbook/worksheets/Sheet%3Fid%23fragment/usedRange"
    )


@pytest.mark.asyncio
async def test_excel_token_whitespace_is_normalized(excel_http):
    await excel_http.call_tool("list_worksheets", {"file_id": "book-1"}, "  excel-test-token  ")

    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer excel-test-token"


@pytest.mark.asyncio
async def test_excel_rejects_non_string_token_without_http(excel_http):
    result = await excel_http.call_tool(
        "list_worksheets", {"file_id": "book-1"}, {"access_token": "bad"}
    )

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_excel_rejects_non_object_arguments_without_http(excel_http):
    result = await excel_http.call_tool("list_worksheets", [], "excel-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []
