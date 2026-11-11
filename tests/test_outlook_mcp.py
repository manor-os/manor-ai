from __future__ import annotations

import base64
import json

import pytest


class _Response:
    status_code = 200
    text = "{}"

    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload if payload is not None else {"value": []}

    @property
    def is_success(self) -> bool:
        return True

    def json(self) -> dict:
        return self._payload


class _Client:
    calls: list[dict] = []
    routes: dict[str, dict] = {}

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
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return _Response(payload)
        return _Response()


@pytest.fixture
def outlook_http(monkeypatch):
    from packages.core.ai.mcp import outlook

    _Client.calls = []
    _Client.routes = {}
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


@pytest.mark.asyncio
async def test_outlook_message_reads_as_text_and_attachment_saves_whole(outlook_http, monkeypatch):
    from packages.core.services import email_attachments

    html = "<p>Basic and Starter product structure</p>" + "<p>Feedback sentence.</p>" * 900
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 200
    _Client.routes = {
        "/me/messages/msg-1": {
            "id": "msg-1",
            "subject": "Product feedback",
            "from": {"emailAddress": {"name": "Gary", "address": "gary@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
            "receivedDateTime": "2026-11-02T17:00:00Z",
            "hasAttachments": True,
            "body": {"contentType": "html", "content": html},
        },
        "/me/messages/msg-1/attachments": {"value": [{
            "id": "att-1", "name": "Product structure.pdf",
            "contentType": "application/pdf", "size": len(pdf), "isInline": False,
        }]},
        "/me/messages/msg-1/attachments/att-1": {
            "name": "Product structure.pdf",
            "contentType": "application/pdf",
            "contentBytes": base64.b64encode(pdf).decode("ascii"),
        },
    }

    result = await outlook_http.call_tool("get_message", {"message_id": "msg-1"}, "t")
    message = json.loads(result["content"][0]["text"])
    assert message["from"] == "Gary <gary@example.com>"
    assert message["content"].startswith("Basic and Starter product structure")
    assert "<p>" not in message["content"]
    assert message["next_offset"] is not None
    assert [a["attachment_id"] for a in message["attachments"]] == ["att-1"]

    captured = {}

    async def persist_attachment(**kwargs):
        captured.update(kwargs)
        return {"saved": True, "document_id": "document-1"}

    monkeypatch.setattr(email_attachments, "persist_workspace_email_attachment", persist_attachment)
    outlook_http.set_call_context({"entity_id": "entity-1", "user_id": "user-1"})
    try:
        saved = await outlook_http.call_tool(
            "save_attachment_to_workspace", {"message_id": "msg-1", "attachment_id": "att-1"}, "t",
        )
    finally:
        outlook_http.clear_call_context()

    assert saved["isError"] is False
    assert captured["data"] == pdf
    assert captured["filename"] == "Product structure.pdf"
