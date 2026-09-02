from __future__ import annotations

from pathlib import Path

import pytest


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"files": []}
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload


class _Client:
    calls: list[dict] = []
    responses: list[_Response] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def _next_response(self) -> _Response:
        if self.responses:
            return self.responses.pop(0)
        return _Response()

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
        return self._next_response()

    async def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self._next_response()


@pytest.fixture
def drive_http(monkeypatch):
    from packages.core.ai.mcp import google_drive

    _Client.calls = []
    _Client.responses = [_Response()]
    monkeypatch.setattr(google_drive.httpx, "AsyncClient", _Client)
    return google_drive


def test_drive_catalog_excludes_full_scope_empty_trash(drive_http):
    tools = {tool["name"]: tool for tool in drive_http.list_tools()}

    assert "empty_trash" not in tools
    assert "delete_file" in tools
    assert "delete_file_permanent" in tools

    skill_body = Path(
        "packages/core/ai/skills/mcp_google_drive/SKILL.md"
    ).read_text(encoding="utf-8")
    assert "empty_trash" not in skill_body


def test_drive_mutation_catalog_explains_per_file_scope_boundary(drive_http):
    tools = {tool["name"]: tool for tool in drive_http.list_tools()}
    mutation_tools = {
        "move_file",
        "rename_file",
        "delete_file",
        "share_file",
        "copy_file",
        "restore_file",
        "delete_file_permanent",
        "update_permission",
        "delete_permission",
        "delete_revision",
        "create_comment",
        "resolve_comment",
        "delete_comment",
        "create_reply",
    }

    for name in mutation_tools:
        description = tools[name]["description"]
        assert "created by Manor" in description
        assert "opened/shared with Manor" in description


@pytest.mark.asyncio
async def test_missing_token_is_rejected_without_http(drive_http):
    result = await drive_http.call_tool("get_about", {}, "")

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_files_rejects_non_positive_limit_without_http(drive_http):
    result = await drive_http.call_tool(
        "list_files",
        {"max_results": 0},
        "drive-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_files_rejects_fractional_limit_without_http(drive_http):
    result = await drive_http.call_tool(
        "list_files",
        {"max_results": 1.5},
        "drive-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_read_file_metadata_failure_is_mcp_error(drive_http):
    _Client.responses = [_Response(status_code=403, text="forbidden")]
    result = await drive_http.call_tool(
        "read_file",
        {"file_id": "file-1"},
        "drive-test-token",
    )

    assert result["isError"] is True
    assert "forbidden" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_read_file_rejects_binary_mime_before_download(drive_http):
    _Client.responses = [
        _Response(
            payload={"name": "report.pdf", "mimeType": "application/pdf"},
            text='{"name":"report.pdf","mimeType":"application/pdf"}',
        )
    ]

    result = await drive_http.call_tool(
        "read_file",
        {"file_id": "file-1"},
        "drive-test-token",
    )

    assert result["isError"] is True
    assert "binary" in result["content"][0]["text"].lower()
    assert len(_Client.calls) == 1


@pytest.mark.asyncio
async def test_read_file_rejects_pdf_export_for_text_tool(drive_http):
    _Client.responses = [
        _Response(
            payload={
                "name": "report",
                "mimeType": "application/vnd.google-apps.document",
            }
        )
    ]

    result = await drive_http.call_tool(
        "read_file",
        {"file_id": "file-1", "export_format": "application/pdf"},
        "drive-test-token",
    )

    assert result["isError"] is True
    assert "text" in result["content"][0]["text"].lower()
    assert len(_Client.calls) == 1


@pytest.mark.asyncio
async def test_list_files_escapes_query_literals_and_caps_limit(drive_http):
    result = await drive_http.call_tool(
        "list_files",
        {"query": "owner's report", "max_results": 999},
        "drive-test-token",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["params"]["pageSize"] == 100
    assert "owner\\'s report" in call["params"]["q"]
    assert call["headers"]["Authorization"] == "Bearer drive-test-token"


@pytest.mark.asyncio
async def test_create_file_sends_metadata_to_drive_api(drive_http):
    _Client.responses = [_Response(payload={"id": "file-1", "name": "QA.txt"})]
    result = await drive_http.call_tool(
        "create_file",
        {"name": "QA.txt", "mime_type": "text/plain"},
        "drive-test-token",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/files")
    assert call["json"] == {"name": "QA.txt", "mimeType": "text/plain"}


@pytest.mark.asyncio
async def test_file_id_is_path_encoded(drive_http):
    await drive_http.call_tool(
        "get_file",
        {"file_id": "file?id#fragment"},
        "drive-test-token",
    )

    call = _Client.calls[0]
    assert call["url"].endswith("/files/file%3Fid%23fragment")


@pytest.mark.asyncio
async def test_drive_token_whitespace_is_normalized(drive_http):
    await drive_http.call_tool("get_about", {}, "  drive-test-token  ")

    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer drive-test-token"


@pytest.mark.asyncio
async def test_drive_rejects_non_string_token_without_http(drive_http):
    result = await drive_http.call_tool("get_about", {}, {"token": "bad"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_drive_rejects_non_object_arguments_without_http(drive_http):
    result = await drive_http.call_tool("get_about", [], "drive-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []
