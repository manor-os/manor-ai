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
        return _Response()


@pytest.fixture
def outlook_http(monkeypatch):
    from packages.core.ai.mcp import outlook

    _Client.calls = []
    monkeypatch.setattr(outlook.httpx, "AsyncClient", _Client)
    return outlook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected_path"),
    [
        (
            "get_message",
            {"message_id": "message/id+with=reserved"},
            "/me/messages/message%2Fid%2Bwith%3Dreserved",
        ),
        (
            "list_messages",
            {"folder_id": "folder/id+with=reserved"},
            "/me/mailFolders/folder%2Fid%2Bwith%3Dreserved/messages",
        ),
        (
            "create_folder",
            {"name": "QA", "parent_folder_id": "folder/id+with=reserved"},
            "/me/mailFolders/folder%2Fid%2Bwith%3Dreserved/childFolders",
        ),
        (
            "download_attachment",
            {
                "message_id": "message/id+with=reserved",
                "attachment_id": "attachment/id+with=reserved",
            },
            "/me/messages/message%2Fid%2Bwith%3Dreserved/attachments/attachment%2Fid%2Bwith%3Dreserved",
        ),
    ],
)
async def test_outlook_resource_ids_are_encoded_as_single_path_segments(
    outlook_http,
    tool,
    arguments,
    expected_path,
):
    result = await outlook_http.call_tool(tool, arguments, "outlook-test-token")

    assert result["isError"] is False
    assert _Client.calls[0]["url"].endswith(expected_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_messages", "list_folders"])
@pytest.mark.parametrize("top", [0, -1, True])
async def test_outlook_list_requests_reject_invalid_top_without_http(outlook_http, tool, top):
    result = await outlook_http.call_tool(tool, {"top": top}, "outlook-test-token")

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_outlook_rejects_whitespace_required_argument_without_http(outlook_http):
    result = await outlook_http.call_tool(
        "get_message", {"message_id": "   "}, "outlook-test-token"
    )

    assert result["isError"] is True
    assert "message_id" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_outlook_rejects_fractional_top_without_http(outlook_http):
    result = await outlook_http.call_tool(
        "list_messages", {"top": 1.5}, "outlook-test-token"
    )

    assert result["isError"] is True
    assert "top" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "token", "message"),
    [
        ([], "outlook-test-token", "arguments must be an object"),
        ({"message_id": "message-1"}, {"access_token": "bad"}, "access token"),
    ],
)
async def test_outlook_rejects_non_object_arguments_and_non_string_tokens_before_http(
    outlook_http, arguments, token, message
):
    result = await outlook_http.call_tool("get_message", arguments, token)

    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert _Client.calls == []
