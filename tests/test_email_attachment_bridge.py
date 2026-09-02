from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services import email_attachments
from packages.core.services.channels.email_adapter import EmailChannelAdapter


@pytest.fixture
def email_attachment_fs(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    return tmp_path


@pytest.mark.asyncio
async def test_email_channel_normalizes_inline_attachments():
    adapter = EmailChannelAdapter()
    cc = SimpleNamespace(id="cc-1", entity_id="entity-1")
    body = json.dumps({
        "from": "ALICE@example.com",
        "subject": "Candidate",
        "message_id": "<message-1@example.com>",
        "attachments": [{
            "filename": "resume.txt",
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(b"Experienced engineer").decode("ascii"),
        }],
    }).encode("utf-8")

    inbound = await adapter.parse_inbound(cc, headers={}, query={}, body=body)

    assert inbound is not None
    assert inbound.source_id == "alice@example.com"
    assert inbound.message_type == "file"
    assert len(inbound.attachments) == 1
    attachment = inbound.attachments[0]
    assert attachment["filename"] == "resume.txt"
    assert attachment["content_type"] == "text/plain"
    assert attachment["message_id"] == "<message-1@example.com>"
    assert base64.b64decode(attachment["data_base64"]) == b"Experienced engineer"


@pytest.mark.asyncio
async def test_inbound_attachment_bridge_returns_knowledge_refs_and_agent_context(
    monkeypatch,
):
    async def persist_attachment(**kwargs):
        return {
            "saved": True,
            "filename": kwargs["filename"],
            "content_type": kwargs["content_type"],
            "size": len(kwargs["data"]),
            "fs_path": "Workspaces/_by_id/folder-1/Email attachments/resume.txt",
            "knowledge_path": "Workspaces/Recruiting/Email attachments/resume.txt",
            "document_id": "document-1",
            "viewer_url": "/viewer/document-1",
            "text_extracted": True,
            "text_content": "Experienced engineer",
        }

    monkeypatch.setattr(
        email_attachments,
        "persist_workspace_email_attachment",
        persist_attachment,
    )
    persisted, context = await (
        email_attachments.persist_inbound_workspace_email_attachments(
            attachments=[{
                "filename": "resume.txt",
                "content_type": "text/plain",
                "data_base64": base64.b64encode(b"Experienced engineer").decode("ascii"),
                "message_id": "<message-1@example.com>",
            }],
            entity_id="entity-1",
            workspace_id="workspace-1",
            user_id="user-1",
            conversation_id="conversation-1",
        )
    )

    assert persisted == [{
        "filename": "resume.txt",
        "content_type": "text/plain",
        "size": 20,
        "fs_path": "Workspaces/_by_id/folder-1/Email attachments/resume.txt",
        "knowledge_path": "Workspaces/Recruiting/Email attachments/resume.txt",
        "document_id": "document-1",
        "viewer_url": "/viewer/document-1",
        "text_extracted": True,
        "status": "saved",
    }]
    assert "document_id=document-1" in context
    assert "Experienced engineer" in context
    assert "untrusted source material" in context
    assert "data_base64" not in str(persisted)


@pytest.mark.asyncio
async def test_inbound_message_log_does_not_store_attachment_bytes(monkeypatch):
    from packages.core.services import channel_service

    class _Db:
        def __init__(self):
            self.added = None

        def add(self, value):
            self.added = value

        async def flush(self):
            return None

    async def get_config(_db, _config_id, _entity_id):
        return SimpleNamespace(channel_type="email")

    monkeypatch.setattr(channel_service, "_get_channel_config", get_config)
    db = _Db()
    logged = await channel_service.handle_inbound_message(
        db,
        entity_id="entity-1",
        channel_config_id="cc-1",
        payload={
            "from": "alice@example.com",
            "content": "Please review",
            "attachments": [{
                "filename": "resume.txt",
                "content_type": "text/plain",
                "data_base64": base64.b64encode(b"private resume").decode("ascii"),
            }],
        },
    )

    assert logged is db.added
    assert logged.attachments == {
        "items": [{"filename": "resume.txt", "content_type": "text/plain"}]
    }


@pytest.mark.asyncio
async def test_email_attachment_is_atomically_saved_and_projected(
    db_session,
    email_attachment_fs,
    monkeypatch,
):
    from packages.core.ai.runtime import file_actions

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Email Attachment Test")
    workspace = Workspace(
        entity_id=entity_id,
        name="Recruiting",
        operating_model={},
    )
    owner = User(
        entity_id=entity_id,
        email=f"{entity_id}@example.test",
        display_name="Owner",
        password_hash="unused",
        role="owner",
        status="active",
    )
    db_session.add_all([entity, workspace, owner])
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=workspace.id,
        user_id=owner.id,
        role="owner",
        status="active",
    ))
    db_session.add(UserMembership(
        user_id=owner.id,
        entity_id=entity_id,
        role="owner",
        status="active",
        is_primary=True,
    ))
    await db_session.commit()
    monkeypatch.setattr(
        file_actions,
        "runtime_trigger_document_embeddings",
        lambda _document_id: None,
    )

    saved = await email_attachments.persist_workspace_email_attachment(
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=owner.id,
        data=b"Candidate: Alice\nExperience: 8 years\n",
        filename="../resume.txt",
        content_type="text/plain",
        source={"message_id": "<message-1@example.com>", "uid": "7"},
        conversation_id="conversation-1",
    )

    document = await db_session.get(Document, saved["document_id"])
    assert document is not None
    assert document.name == "resume.txt"
    assert document.metadata_["origin"]["workspace_id"] == workspace.id
    assert document.metadata_["email_attachment"]["message_id"] == (
        "<message-1@example.com>"
    )
    assert saved["text_content"].startswith("Candidate: Alice")
    stored = email_attachment_fs / entity_id / saved["fs_path"]
    assert stored.read_bytes() == b"Candidate: Alice\nExperience: 8 years\n"

    from packages.core.database import async_session
    from packages.core.services.document_access import (
        document_workspace_ids,
        user_can_read_document,
    )
    from packages.core.services.document_service import get_document

    async with async_session() as check_db:
        readable_document = await get_document(
            check_db, saved["document_id"], entity_id
        )
        assert readable_document is not None
        assert workspace.id in await document_workspace_ids(
            check_db, readable_document
        )
        assert await user_can_read_document(
            check_db,
            readable_document,
            entity_id=entity_id,
            user_id=owner.id,
            workspace_id=workspace.id,
            allow_redacted=False,
        )

    loaded = await email_attachments.load_workspace_document_email_attachment(
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=owner.id,
        document_id=saved["document_id"],
    )
    assert loaded["filename"].endswith("resume.txt")
    assert base64.b64decode(loaded["data_base64"]) == stored.read_bytes()

    other_workspace = Workspace(
        entity_id=entity_id,
        name="Finance",
        operating_model={},
    )
    db_session.add(other_workspace)
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=other_workspace.id,
        user_id=owner.id,
        role="owner",
        status="active",
    ))
    await db_session.commit()
    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="outside the current Workspace scope",
    ):
        await email_attachments.load_workspace_document_email_attachment(
            entity_id=entity_id,
            workspace_id=other_workspace.id,
            user_id=owner.id,
            document_id=saved["document_id"],
        )


@pytest.mark.asyncio
async def test_email_attachment_end_to_end_from_imap_to_workspace_and_smtp(
    db_session,
    email_attachment_fs,
    monkeypatch,
):
    from packages.core.ai.mcp import email as email_mcp
    from packages.core.ai.runtime import file_actions

    attachment_bytes = b"Candidate: Ada\nExperience: 10 years\n"
    message = EmailMessage()
    message["From"] = "Recruiter <recruiter@example.test>"
    message["To"] = "Hiring <hiring@example.test>"
    message["Subject"] = "Candidate profile"
    message["Message-ID"] = "<candidate-ada@example.test>"
    message.set_content("Please review the attached profile.")
    message.add_attachment(
        attachment_bytes,
        maintype="text",
        subtype="plain",
        filename="candidate-ada.txt",
    )

    class FakeImap:
        def select(self, _folder, readonly=False):
            return "OK", [b"1"]

        def uid(self, command, uid, spec):
            assert command == "FETCH"
            assert str(uid) == "7"
            assert spec == "(RFC822)"
            return "OK", [(b"7 (RFC822)", message.as_bytes())]

        def logout(self):
            return "BYE", []

    class FakeSmtp:
        sent = []

        def __init__(self, host, port, timeout):
            assert host == "smtp.example.test"
            assert port == 587
            assert timeout > 0

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
            assert username == "hiring@example.test"
            assert password == "test-password"

        def send_message(self, sent_message, from_addr, to_addrs):
            self.sent.append((sent_message, from_addr, list(to_addrs)))

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Email Attachment E2E")
    workspace = Workspace(
        entity_id=entity_id,
        name="Recruiting",
        operating_model={},
    )
    owner = User(
        entity_id=entity_id,
        email=f"{entity_id}@example.test",
        display_name="Owner",
        password_hash="unused",
        role="owner",
        status="active",
    )
    db_session.add_all([entity, workspace, owner])
    await db_session.flush()
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=workspace.id,
            user_id=owner.id,
            role="owner",
            status="active",
        ),
        UserMembership(
            user_id=owner.id,
            entity_id=entity_id,
            role="owner",
            status="active",
            is_primary=True,
        ),
    ])
    await db_session.commit()

    monkeypatch.setattr(
        file_actions,
        "runtime_trigger_document_embeddings",
        lambda _document_id: None,
    )
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda _cfg: FakeImap())
    monkeypatch.setattr(email_mcp.smtplib, "SMTP", FakeSmtp)
    credentials = json.dumps({
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "username": "hiring@example.test",
        "password": "test-password",
        "from_address": "Hiring <hiring@example.test>",
        "use_tls_smtp": True,
    })

    email_mcp.set_call_context({
        "entity_id": entity_id,
        "workspace_id": workspace.id,
        "user_id": owner.id,
        "conversation_id": "conversation-e2e",
    })
    try:
        listed_result = await email_mcp.call_tool(
            "list_attachments",
            {"uid": "7", "folder": "INBOX"},
            credentials,
        )
        listed = json.loads(listed_result["content"][0]["text"])
        assert len(listed["attachments"]) == 1

        saved_result = await email_mcp.call_tool(
            "save_attachment_to_workspace",
            {
                "uid": "7",
                "folder": "INBOX",
                "attachment_id": listed["attachments"][0]["attachment_id"],
            },
            credentials,
        )
        saved = json.loads(saved_result["content"][0]["text"])
        assert saved_result["isError"] is False

        sent_result = await email_mcp.call_tool(
            "send_email",
            {
                "to": "reviewer@example.test",
                "subject": "Candidate profile",
                "body": "Attached from Workspace Knowledge.",
                "attachments": [{"document_id": saved["document_id"]}],
            },
            credentials,
        )
        assert sent_result["isError"] is False
    finally:
        email_mcp.clear_call_context()

    document = await db_session.get(Document, saved["document_id"])
    assert document is not None
    assert document.name == "candidate-ada.txt"
    assert document.metadata_["origin"]["workspace_id"] == workspace.id
    assert document.metadata_["email_attachment"]["message_id"] == (
        "<candidate-ada@example.test>"
    )
    stored = email_attachment_fs / entity_id / saved["fs_path"]
    assert stored.read_bytes() == attachment_bytes
    assert saved["viewer_url"] == f"/viewer/{document.id}"
    assert saved["text_content"].startswith("Candidate: Ada")

    sent_message, envelope_from, envelope_to = FakeSmtp.sent[-1]
    assert envelope_from == "hiring@example.test"
    assert envelope_to == ["reviewer@example.test"]
    sent_attachments = list(sent_message.iter_attachments())
    assert len(sent_attachments) == 1
    assert sent_attachments[0].get_filename() == "candidate-ada.txt"
    assert sent_attachments[0].get_payload(decode=True) == attachment_bytes
