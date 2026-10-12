"""Generic IMAP/SMTP Email MCP coverage.

These tests use protocol fakes so the suite never contacts a real mailbox.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage

import pytest

from packages.core.ai.mcp import email as email_mcp
from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool,
    classify_runtime_tool_action,
)
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect


def _message_bytes(*, with_attachment: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "Me <me@example.com>, Bob <bob@example.com>"
    msg["Cc"] = "Carol <carol@example.com>, me@example.com"
    msg["Subject"] = "Quarterly plan"
    msg["Date"] = "Tue, 11 Aug 2026 10:00:00 -0700"
    msg["Message-ID"] = "<original@example.com>"
    msg["References"] = "<root@example.com>"
    msg.set_content("Plain body")
    msg.add_alternative("<p>HTML body</p>", subtype="html")
    if with_attachment:
        msg.add_attachment(
            b"attachment bytes",
            maintype="text",
            subtype="plain",
            filename="notes.txt",
        )
    return msg.as_bytes()


def _thread_message_bytes(
    *, sender: str, recipient: str, subject: str, message_id: str,
    date: str, in_reply_to: str = "", references: str = "",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(f"Body for {message_id}")
    return msg.as_bytes()


class _FakeImap:
    def __init__(self, raw: bytes):
        self.raw = raw

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, uid, spec):
        assert command == "FETCH"
        assert str(uid) == "7"
        assert spec == "(RFC822)"
        return "OK", [(b"7 (RFC822)", self.raw)]

    def logout(self):
        return "BYE", []


class _FakeSmtp:
    sent = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, *, context=None):
        assert context is not None
        return 220, b"ready"

    def login(self, username, password):
        assert username == "me@example.com"
        assert password == "app-password"

    def send_message(self, msg, from_addr, to_addrs):
        self.sent.append((msg, from_addr, list(to_addrs)))


class _ThreadImap:
    def __init__(self, messages):
        self.messages = messages
        self.folder = "INBOX"

    def select(self, folder, readonly=False):
        self.folder = str(folder).strip('"')
        return "OK", [str(len(self.messages.get(self.folder, {}))).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            uids = " ".join(self.messages.get(self.folder, {}).keys()).encode()
            return "OK", [uids]
        if command == "FETCH":
            uid, spec = str(args[0]), str(args[1])
            raw = self.messages[self.folder][uid]
            prefix = f"{uid} (FLAGS (\\Seen) RFC822 {{{len(raw)}}})".encode()
            if "HEADER.FIELDS" in spec:
                header = raw.split(b"\n\n", 1)[0] + b"\n\n"
                return "OK", [(prefix, header)]
            return "OK", [(prefix, raw)]
        raise AssertionError((command, args))

    def logout(self):
        return "BYE", []


class _DraftImap:
    def __init__(self):
        self.appended = []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Drafts) "/" "Drafts"']

    def append(self, folder, flags, date_time, raw):
        self.appended.append((folder, flags, raw))
        return "OK", [b"appended"]

    def response(self, name):
        assert name == "APPENDUID"
        return "APPENDUID", [b"9 42"]

    def logout(self):
        return "BYE", []


def _credentials() -> dict:
    return {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "username": "me@example.com",
        "password": "app-password",
        "from_address": "Me <me@example.com>",
        "use_tls_smtp": True,
    }


def test_tool_catalog_exposes_full_generic_email_actions():
    names = {tool["name"] for tool in email_mcp.list_tools()}
    assert {
        "list_attachments",
        "download_attachment",
        "save_attachment_to_workspace",
        "list_threads",
        "get_thread",
        "reply_to_message",
        "reply_all",
        "list_drafts",
        "get_draft",
        "create_draft",
        "update_draft",
        "send_draft",
        "delete_draft",
    } <= names


@pytest.mark.asyncio
async def test_download_attachment_returns_lossless_base64(monkeypatch):
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda cfg: _FakeImap(_message_bytes(with_attachment=True)))

    listed = await email_mcp.call_tool(
        "list_attachments",
        {"uid": "7", "folder": "INBOX"},
        json.dumps(_credentials()),
    )
    listing = json.loads(listed["content"][0]["text"])
    assert listing["attachments"][0]["filename"] == "notes.txt"

    downloaded = await email_mcp.call_tool(
        "download_attachment",
        {
            "uid": "7",
            "folder": "INBOX",
            "attachment_id": listing["attachments"][0]["attachment_id"],
        },
        json.dumps(_credentials()),
    )
    payload = json.loads(downloaded["content"][0]["text"])
    assert base64.b64decode(payload["data_base64"]) == b"attachment bytes"


@pytest.mark.asyncio
async def test_reply_all_preserves_thread_and_excludes_self(monkeypatch):
    _FakeSmtp.sent.clear()
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda cfg: _FakeImap(_message_bytes()))
    monkeypatch.setattr(email_mcp.smtplib, "SMTP", _FakeSmtp)

    result = await email_mcp.call_tool(
        "reply_all",
        {"uid": "7", "folder": "INBOX", "body": "Approved."},
        json.dumps(_credentials()),
    )
    assert result["isError"] is False
    sent, envelope_from, envelope_to = _FakeSmtp.sent[-1]
    assert envelope_from == "me@example.com"
    assert set(envelope_to) == {
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    }
    assert "me@example.com" not in sent["To"].lower()
    assert "me@example.com" not in sent["Cc"].lower()
    assert sent["Subject"] == "Re: Quarterly plan"
    assert sent["In-Reply-To"] == "<original@example.com>"
    assert sent["References"] == "<root@example.com> <original@example.com>"


@pytest.mark.asyncio
async def test_send_email_accepts_base64_attachments(monkeypatch):
    _FakeSmtp.sent.clear()
    monkeypatch.setattr(email_mcp.smtplib, "SMTP", _FakeSmtp)

    result = await email_mcp.call_tool(
        "send_email",
        {
            "to": "alice@example.com",
            "subject": "File",
            "body": "Attached.",
            "attachments": [{
                "filename": "report.csv",
                "content_type": "text/csv",
                "data_base64": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
            }],
        },
        json.dumps(_credentials()),
    )
    assert result["isError"] is False
    sent = _FakeSmtp.sent[-1][0]
    attachments = list(sent.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "report.csv"
    assert attachments[0].get_payload(decode=True) == b"a,b\n1,2\n"


@pytest.mark.asyncio
async def test_send_email_accepts_authorized_workspace_document(monkeypatch):
    from packages.core.services import email_attachments as attachment_service

    _FakeSmtp.sent.clear()
    monkeypatch.setattr(email_mcp.smtplib, "SMTP", _FakeSmtp)
    captured = {}

    async def load_attachment(**kwargs):
        captured.update(kwargs)
        return {
            "filename": "workspace-report.csv",
            "content_type": "text/csv",
            "data_base64": base64.b64encode(b"a,b\n3,4\n").decode("ascii"),
        }

    monkeypatch.setattr(
        attachment_service,
        "load_workspace_document_email_attachment",
        load_attachment,
    )
    email_mcp.set_call_context({
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
    })
    try:
        result = await email_mcp.call_tool(
            "send_email",
            {
                "to": "alice@example.com",
                "subject": "Workspace file",
                "body": "Attached.",
                "attachments": [{"document_id": "document-1"}],
            },
            json.dumps(_credentials()),
        )
    finally:
        email_mcp.clear_call_context()

    assert result["isError"] is False
    assert captured == {
        "entity_id": "entity-1",
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "document_id": "document-1",
        "filename": None,
    }
    sent_attachment = list(_FakeSmtp.sent[-1][0].iter_attachments())[0]
    assert sent_attachment.get_filename() == "workspace-report.csv"
    assert sent_attachment.get_payload(decode=True) == b"a,b\n3,4\n"


@pytest.mark.asyncio
async def test_save_attachment_projects_to_current_workspace(monkeypatch):
    from packages.core.services import email_attachments as attachment_service

    monkeypatch.setattr(
        email_mcp,
        "_imap_connect",
        lambda cfg: _FakeImap(_message_bytes(with_attachment=True)),
    )
    captured = {}

    async def persist_attachment(**kwargs):
        captured.update(kwargs)
        return {
            "saved": True,
            "filename": kwargs["filename"],
            "document_id": "document-1",
            "viewer_url": "/viewer/document-1",
            "text_content": "attachment bytes",
        }

    monkeypatch.setattr(
        attachment_service,
        "persist_workspace_email_attachment",
        persist_attachment,
    )
    email_mcp.set_call_context({
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
    })
    try:
        result = await email_mcp.call_tool(
            "save_attachment_to_workspace",
            {"uid": "7", "folder": "INBOX", "attachment_id": "4"},
            json.dumps(_credentials()),
        )
    finally:
        email_mcp.clear_call_context()

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["document_id"] == "document-1"
    assert captured["data"] == b"attachment bytes"
    assert captured["entity_id"] == "entity-1"
    assert captured["workspace_id"] == "workspace-1"
    assert captured["user_id"] == "user-1"
    assert captured["conversation_id"] == "conversation-1"
    assert captured["source"]["uid"] == "7"


@pytest.mark.asyncio
async def test_get_thread_scans_inbox_and_sent(monkeypatch):
    root = _thread_message_bytes(
        sender="Alice <alice@example.com>",
        recipient="Me <me@example.com>",
        subject="Project",
        message_id="<root@example.com>",
        date="Tue, 11 Aug 2026 09:00:00 -0700",
    )
    reply = _thread_message_bytes(
        sender="Me <me@example.com>",
        recipient="Alice <alice@example.com>",
        subject="Re: Project",
        message_id="<reply@example.com>",
        in_reply_to="<root@example.com>",
        references="<root@example.com>",
        date="Tue, 11 Aug 2026 10:00:00 -0700",
    )
    fake = _ThreadImap({"INBOX": {"1": root}, "Sent": {"9": reply}})
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda cfg: fake)

    result = await email_mcp.call_tool(
        "get_thread",
        {"uid": "1", "folder": "INBOX", "search_folders": ["INBOX", "Sent"]},
        json.dumps(_credentials()),
    )
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["message_count"] == 2
    assert [item["folder"] for item in payload["messages"]] == ["INBOX", "Sent"]


@pytest.mark.asyncio
async def test_create_draft_uses_imap_drafts_folder(monkeypatch):
    fake = _DraftImap()
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda cfg: fake)

    result = await email_mcp.call_tool(
        "create_draft",
        {"to": "alice@example.com", "subject": "Draft", "body": "Review me"},
        json.dumps(_credentials()),
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["draft_uid"] == "42"
    assert fake.appended[0][0] == "Drafts"
    assert "\\Draft" in fake.appended[0][1]
    saved = email_mcp.message_from_bytes(fake.appended[0][2])
    assert saved["To"] == "alice@example.com"
    assert saved["Subject"] == "Draft"


def test_rfc_headers_group_messages_into_threads():
    records = [
        {
            "uid": "1", "folder": "INBOX", "message_id": "<root@example.com>",
            "in_reply_to": "", "references": "",
        },
        {
            "uid": "2", "folder": "INBOX", "message_id": "<reply@example.com>",
            "in_reply_to": "<root@example.com>", "references": "<root@example.com>",
        },
        {
            "uid": "3", "folder": "INBOX", "message_id": "<other@example.com>",
            "in_reply_to": "", "references": "",
        },
    ]
    groups = email_mcp._group_thread_records(records)
    assert sorted(len(group) for group in groups) == [1, 2]


@pytest.mark.parametrize("tool_name", [
    "mcp__email__reply_to_message",
    "mcp__email__reply_all",
])
def test_reply_actions_use_email_send_approval(tool_name):
    action = classify_runtime_tool_action(tool_name, {"uid": "7", "body": "Approved."})
    assert action is not None
    assert action.action_key == "email.send"
    assert action.risk_level == "high"


def test_save_attachment_uses_workspace_knowledge_approval():
    action = classify_runtime_tool_action(
        "mcp__email__save_attachment_to_workspace",
        {"uid": "7", "attachment_id": "3"},
    )
    assert action is not None
    assert action.action_key == "workspace.knowledge.update"
    assert action.risk_level == "medium"


@pytest.mark.parametrize("action", [
    "list_attachments",
    "download_attachment",
    "list_threads",
    "get_thread",
    "list_drafts",
    "get_draft",
])
def test_generic_email_mailbox_reads_are_classified_read_only(action):
    classification = classify_runtime_tool(f"mcp__email__{action}", {})
    assert classification.effect is RuntimeToolEffect.READ_ONLY
