from __future__ import annotations

import json

import pytest


class _Response:
    def __init__(self, payload: dict | None = None, text: str | None = None):
        self.status_code = 200
        self._payload = payload if payload is not None else {"value": []}
        self.text = json.dumps(self._payload) if text is None else text

    @property
    def is_success(self) -> bool:
        return True

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
        return self.responses.pop(0) if self.responses else _Response()

    async def get(self, url, headers=None):
        self.calls.append(
            {"method": "GET", "url": url, "headers": headers, "json": None, "params": None}
        )
        return self.responses.pop(0) if self.responses else _Response()


@pytest.fixture
def onedrive_http(monkeypatch):
    from packages.core.ai.mcp import onedrive

    _Client.calls = []
    _Client.responses = []
    monkeypatch.setattr(onedrive.httpx, "AsyncClient", _Client)
    return onedrive


@pytest.mark.asyncio
async def test_list_files_rejects_non_positive_top_without_http(onedrive_http):
    result = await onedrive_http.call_tool(
        "list_files",
        {"top": 0},
        "onedrive-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_files_rejects_fractional_top_without_http(onedrive_http):
    result = await onedrive_http.call_tool(
        "list_files",
        {"top": 1.5},
        "onedrive-test-token",
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_item_id_is_path_encoded(onedrive_http):
    await onedrive_http.call_tool(
        "get_file",
        {"file_id": "item?id#fragment"},
        "onedrive-test-token",
    )

    assert _Client.calls[0]["url"].endswith("/items/item%3Fid%23fragment")


@pytest.mark.asyncio
async def test_onedrive_token_whitespace_is_normalized(onedrive_http):
    await onedrive_http.call_tool("get_drive_info", {}, "  onedrive-test-token  ")

    assert _Client.calls[0]["headers"]["Authorization"] == "Bearer onedrive-test-token"


@pytest.mark.asyncio
async def test_onedrive_rejects_non_string_token_without_http(onedrive_http):
    result = await onedrive_http.call_tool("get_drive_info", {}, {"token": "bad"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_onedrive_rejects_non_object_arguments_without_http(onedrive_http):
    result = await onedrive_http.call_tool("get_drive_info", [], "onedrive-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_read_file_rejects_binary_mime_before_download(onedrive_http):
    _Client.responses = [
        _Response(
            {
                "id": "item-1",
                "name": "report.pdf",
                "file": {"mimeType": "application/pdf"},
            }
        )
    ]

    result = await onedrive_http.call_tool(
        "read_file",
        {"file_id": "item-1"},
        "onedrive-test-token",
    )

    assert result["isError"] is True
    assert "binary" in result["content"][0]["text"].lower()
    assert len(_Client.calls) == 1
    assert _Client.calls[0]["url"].endswith("/items/item-1")


@pytest.mark.asyncio
async def test_read_file_rejects_pdf_format_without_http(onedrive_http):
    result = await onedrive_http.call_tool(
        "read_file",
        {"file_id": "item-1", "format": "pdf"},
        "onedrive-test-token",
    )

    assert result["isError"] is True
    assert "pdf" in result["content"][0]["text"].lower()
    assert _Client.calls == []
