from __future__ import annotations

import base64
import json

import pytest


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"messages": []}
        self.text = text

    @property
    def is_success(self) -> bool:
        return True

    def json(self) -> dict:
        return self._payload


class _Client:
    calls: list[dict] = []
    response = _Response()
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
                return _Response(payload=payload)
        return self.response


@pytest.fixture
def gmail_http(monkeypatch):
    from packages.core.ai.mcp import gmail

    _Client.calls = []
    _Client.response = _Response()
    _Client.routes = {}
    monkeypatch.setattr(gmail.httpx, "AsyncClient", _Client)
    return gmail


@pytest.mark.asyncio
async def test_missing_token_is_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", {}, "")

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_non_string_token_is_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", {}, {"token": "gmail-test-token"})

    assert result["isError"] is True
    assert "token" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_non_object_arguments_are_rejected_without_http(gmail_http):
    result = await gmail_http.call_tool("get_profile", [], "gmail-test-token")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_rejects_non_positive_limit_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 0},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_rejects_fractional_limit_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 1.5},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "max_results" in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_send_message_rejects_blank_body_without_http(gmail_http):
    result = await gmail_http.call_tool(
        "send_message",
        {"to": "qa@example.com", "subject": "Test", "body": "   "},
        "gmail-test-token",
    )

    assert result["isError"] is True
    assert "body" in result["content"][0]["text"].lower()
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_list_messages_caps_limit_and_uses_google_bearer(gmail_http):
    _Client.response = _Response(payload={"messages": []})
    result = await gmail_http.call_tool(
        "list_messages",
        {"query": "from:qa@example.com", "max_results": 999},
        "gmail-test-token",
    )

    assert result["isError"] is False
    call = _Client.calls[0]
    assert call["params"]["maxResults"] == 100
    assert call["headers"]["Authorization"] == "Bearer gmail-test-token"


@pytest.mark.asyncio
async def test_gmail_401_returns_reconnect_error_without_leaking_response(gmail_http):
    _Client.response = _Response(
        status_code=401,
        text='{"error":"invalid_grant"}',
    )
    result = await gmail_http.call_tool("get_profile", {}, "gmail-test-token")

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "reconnect" in message.lower()
    assert "invalid_grant" not in message


@pytest.mark.asyncio
async def test_message_id_is_path_encoded(gmail_http):
    await gmail_http.call_tool(
        "get_message",
        {"message_id": "msg?id#fragment"},
        "gmail-test-token",
    )

    call = _Client.calls[0]
    assert call["url"].endswith("/users/me/messages/msg%3Fid%23fragment")


def _b64url(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _text_part(part_id: str, mime: str, text: str) -> dict:
    return {
        "partId": part_id,
        "mimeType": mime,
        "headers": [{"name": "Content-Type", "value": f"{mime}; charset=UTF-8"}],
        "body": {"size": len(text), "data": _b64url(text)},
    }


def _gmail_message(message_id: str, body: str, *, html_only: bool = False) -> dict:
    # Real Gmail messages carry kilobytes of transport headers before the body.
    headers = [{"name": "Received", "value": "from mail.example.com " + "x" * 400}] * 12
    headers += [
        {"name": "From", "value": "Gary <gary@example.com>"},
        {"name": "To", "value": "me@example.com"},
        {"name": "Subject", "value": "Product feedback"},
        {"name": "Date", "value": "Mon, 2 Nov 2026 09:00:00 -0800"},
    ]
    html = "<div>" + body.replace("\n", "<br>") + "</div>"
    alternatives = [_text_part("0.1", "text/html", html)]
    if not html_only:
        alternatives.insert(0, _text_part("0.0", "text/plain", body))
    return {
        "id": message_id,
        "threadId": "thread-1",
        "labelIds": ["INBOX"],
        "snippet": body[:40],
        "payload": {
            "partId": "",
            "mimeType": "multipart/mixed",
            "headers": headers,
            "parts": [
                {"partId": "0", "mimeType": "multipart/alternative", "parts": alternatives},
                {
                    "partId": "1",
                    "mimeType": "application/pdf",
                    "filename": "Product structure.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "ANGjdJ" + "A" * 300, "size": 50_000},
                },
            ],
        },
    }


def _payload(result: dict) -> dict:
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_get_message_reads_whole_long_body_and_lists_attachments(gmail_http):
    body = "Basic and Starter product structure\n" + "Feedback sentence. " * 900 + "\nFINAL LINE"
    _Client.routes = {"/users/me/messages/msg-1": _gmail_message("msg-1", body)}

    first = _payload(await gmail_http.call_tool("get_message", {"message_id": "msg-1"}, "t"))

    assert first["from"] == "Gary <gary@example.com>"
    assert first["subject"] == "Product feedback"
    assert first["content"].startswith("Basic and Starter product structure")
    assert first["total_chars"] == len(body)
    assert first["attachments"] == [{
        "attachment_id": "1",
        "filename": "Product structure.pdf",
        "content_type": "application/pdf",
        "size": 50_000,
        "inline": False,
    }]
    assert first["next_offset"] is not None

    rest = _payload(await gmail_http.call_tool(
        "get_message", {"message_id": "msg-1", "offset": first["next_offset"]}, "t",
    ))
    assert first["content"] + rest["content"] == body
    assert rest["next_offset"] is None


@pytest.mark.asyncio
async def test_get_thread_shows_each_reply_without_quoted_history(gmail_http):
    original = _gmail_message("msg-1", "Here is the plan.", html_only=True)
    reply = _gmail_message(
        "msg-2",
        "Looks good, ship it.\n\nOn Mon, Nov 2, 2026 at 9:00 AM Gary <gary@example.com> wrote:\n"
        "> Here is the plan.",
    )
    _Client.routes = {"/users/me/threads/thread-1": {"id": "thread-1", "messages": [original, reply]}}

    thread = _payload(await gmail_http.call_tool("get_thread", {"thread_id": "thread-1"}, "t"))

    assert [m["body"] for m in thread["messages"]] == ["Here is the plan.", "Looks good, ship it."]
    assert thread["messages"][1]["quoted_history_omitted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_message", "get_draft", "get_thread"])
async def test_text_body_stored_separately_is_read_not_listed_as_a_file(gmail_http, tool):
    body = "The complete externally stored message body."
    message = _gmail_message("msg-1", "")
    message["payload"]["parts"][0] = {
        "partId": "0", "mimeType": "text/plain",
        "body": {"attachmentId": "body-store", "size": len(body)},
    }
    _Client.routes = {
        "/users/me/messages/msg-1": message,
        "/users/me/drafts/draft-1": {"id": "draft-1", "message": message},
        "/users/me/threads/thread-1": {"id": "thread-1", "messages": [message]},
        "/users/me/messages/msg-1/attachments/body-store": {"data": _b64url(body)},
    }
    args = {"message_id": "msg-1", "draft_id": "draft-1", "thread_id": "thread-1"}
    result = _payload(await gmail_http.call_tool(tool, args, "t"))
    if tool == "get_thread":
        result = result["messages"][0]
        assert result["body"] == body
    else:
        assert result["content"] == body
    assert [item["filename"] for item in result["attachments"]] == ["Product structure.pdf"]
    assert not any("ANGjdJ" in call["url"] for call in _Client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Please retain this.\n\n---------- Forwarded message ---------\nThe only copy of the contract.",
    "Please retain this.\n\n> The only copy of the contract.",
], ids=["forwarded", "blockquote"])
async def test_thread_does_not_drop_content_absent_from_other_messages(gmail_http, body):
    _Client.routes = {"/users/me/threads/thread-1": {
        "id": "thread-1", "messages": [_gmail_message("msg-1", "Earlier greeting."), _gmail_message("msg-2", body)],
    }}
    thread = _payload(await gmail_http.call_tool("get_thread", {"thread_id": "thread-1"}, "t"))
    assert thread["messages"][1]["body"] == body
    assert not thread["messages"][1].get("quoted_history_omitted")


@pytest.mark.asyncio
async def test_attachment_is_saved_whole_by_its_listed_id(gmail_http, monkeypatch):
    from packages.core.services import email_attachments

    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 200
    attachment_id = "ANGjdJ" + "A" * 300
    _Client.routes = {
        f"/users/me/messages/msg-1/attachments/{attachment_id}": {"size": len(pdf), "data": _b64url(pdf)},
        "/users/me/messages/msg-1": _gmail_message("msg-1", "See attached."),
    }
    captured = {}

    async def persist_attachment(**kwargs):
        captured.update(kwargs)
        return {"saved": True, "document_id": "document-1", "text_content": "Product structure"}

    monkeypatch.setattr(email_attachments, "persist_workspace_email_attachment", persist_attachment)
    gmail_http.set_call_context({"entity_id": "entity-1", "workspace_id": "workspace-1", "user_id": "user-1"})
    try:
        saved = _payload(await gmail_http.call_tool(
            "save_attachment_to_workspace", {"message_id": "msg-1", "attachment_id": "1"}, "t",
        ))
    finally:
        gmail_http.clear_call_context()

    assert saved["document_id"] == "document-1"
    assert captured["data"] == pdf
    assert captured["filename"] == "Product structure.pdf"
    assert captured["workspace_id"] == "workspace-1"
    assert captured["source"] == {"message_id": "msg-1", "attachment_id": "1"}

    downloaded = _payload(await gmail_http.call_tool(
        "download_attachment", {"message_id": "msg-1", "attachment_id": "1"}, "t",
    ))
    # Existing Gmail consumers read the native base64url `data` field.
    encoded = downloaded["data"]
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)) == pdf
    assert downloaded["size"] == len(pdf)


@pytest.mark.asyncio
async def test_simulated_message_feeds_the_inbox_briefing():
    from packages.core.ai.mcp import gmail
    from packages.core.briefing.inbox import _normalise_gmail_message

    simulated = _payload(await gmail.simulate_tool("get_message", {"message_id": "sim_msg_002"}))
    signal = _normalise_gmail_message(simulated)

    assert signal["sender"] == "Sarah Chen <sarah.chen@example.com>"
    assert "kickoff to Tuesday April 21st" in signal["body_text"]
