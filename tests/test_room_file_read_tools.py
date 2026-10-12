"""Group AI file reads never borrow Entity or Workspace Knowledge access."""
from __future__ import annotations

import io
import json
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document as WordDocument
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import (
    ChatRoom,
    ChatRoomAgentRun,
    ChatRoomFile,
    ChatRoomMember,
    ChatRoomMessage,
)
from packages.core.models.document import Document
from packages.core.services import entity_fs
from packages.core.services.room_agent_runtime import room_audience_fingerprint
from packages.core.services.room_file_read_tools import (
    _extractable_type,
    _safe_office_archive,
    execute_room_file_tool,
    room_file_tool_schemas,
)


async def _register(client: AsyncClient, name: str) -> tuple[dict[str, str], dict]:
    response = await client.post("/api/v1/auth/register", json={
        "email": f"{name}@example.com",
        "password": "test-password-123",
        "entity_name": f"{name} Co",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


@pytest.mark.asyncio
async def test_room_file_tools_only_read_common_group_attachments(client: AsyncClient, db_session):
    headers, owner = await _register(client, "room_tool_owner")
    _, guest = await _register(client, "room_tool_guest")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Room-only files", "owner_scope": "entity",
        "recipient_email": "unjoined-room-tool@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    enabled = await client.patch(
        f"/api/v1/chat/rooms/{room_id}/ai-execution",
        headers=headers, json={"enabled": True, "expected_revision": 0},
    )
    assert enabled.status_code == 200, enabled.text
    now = datetime.now(timezone.utc)
    db_session.add(ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=now,
    ))
    old_message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="old", created_at=now - timedelta(seconds=1),
    )
    visible_message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="shared", created_at=now + timedelta(seconds=1),
    )
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="Please read our shared file", created_at=now + timedelta(seconds=2),
    )
    foreign_room = ChatRoom(
        id=generate_ulid(), kind="group", title="Other room",
        owner_entity_id=owner["entity_id"], created_by_user_id=owner["id"],
        status="active",
    )
    foreign_message = ChatRoomMessage(
        id=generate_ulid(), room_id=foreign_room.id, sender_user_id=owner["id"],
        content="foreign", created_at=now + timedelta(seconds=1),
    )
    db_session.add_all([old_message, visible_message, source, foreign_room, foreign_message])
    await db_session.flush()
    old_file = ChatRoomFile(
        id=generate_ulid(), message_id=old_message.id, name="old.txt",
        mime_type="text/plain", size=6, content=b"secret",
    )
    visible_file = ChatRoomFile(
        id=generate_ulid(), message_id=visible_message.id, name="shared.txt",
        mime_type="text/plain", size=15, content=b"shared evidence",
    )
    foreign_file = ChatRoomFile(
        id=generate_ulid(), message_id=foreign_message.id, name="foreign.txt",
        mime_type="text/plain", size=7, content=b"foreign",
    )
    db_session.add_all([old_file, visible_file, foreign_file])
    await db_session.flush()
    fingerprint, _ = await room_audience_fingerprint(db_session, room_id)
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=binding_id, agent_name="Manor AI", status="running",
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
    )
    db_session.add(run)
    await db_session.commit()

    scope = {"room_id": room_id, "run_id": run.id, "expected_execution_revision": 1, "db": db_session}
    listed = json.loads(await execute_room_file_tool("room_list_files", {}, **scope))
    assert [item["file_id"] for item in listed["files"]] == [visible_file.id]
    assert "secret" not in json.dumps(listed)
    read = json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": visible_file.id, "max_chars": 6}, **scope,
    ))
    assert read["content"] == "shared"
    assert read["content_is_untrusted"] is True
    assert read["next_offset"] == 6
    assert json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": visible_file.id, "offset": 6}, **scope,
    ))["content"] == " evidence"
    for hidden_id in (old_file.id, foreign_file.id):
        hidden = json.loads(await execute_room_file_tool(
            "room_read_file", {"file_id": hidden_id}, **scope,
        ))
        assert hidden == {"error": "room_file_unavailable"}
    visible_file.deleted_at = now + timedelta(seconds=3)
    await db_session.commit()
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "files": [], "has_more": False,
    }
    assert json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": visible_file.id}, **scope,
    )) == {"error": "room_file_unavailable"}


@pytest.mark.asyncio
async def test_room_file_read_rechecks_role_revision_audience_and_run(client: AsyncClient, db_session):
    headers, owner = await _register(client, "room_tool_guard_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Guarded", "owner_scope": "personal",
        "recipient_email": "not-joined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    enabled = await client.patch(
        f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=headers,
        json={"enabled": True, "expected_revision": 0},
    )
    assert enabled.status_code == 200, enabled.text
    now = datetime.now(timezone.utc)
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="List files", created_at=now,
    )
    db_session.add(source)
    await db_session.flush()
    fingerprint, _ = await room_audience_fingerprint(db_session, room_id)
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=binding_id, agent_name="Manor AI", status="running",
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
    )
    db_session.add(run)
    await db_session.commit()
    scope = {"room_id": room_id, "run_id": run.id, "expected_execution_revision": 1, "db": db_session}
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "files": [], "has_more": False,
    }
    assert json.loads(await execute_room_file_tool(
        "room_list_files", {}, **{**scope, "expected_execution_revision": 0},
    )) == {"error": "room_file_unavailable"}
    assert json.loads(await execute_room_file_tool("room_list_files", {"limit": True}, **scope)) == {
        "error": "invalid_arguments",
    }
    assert json.loads(await execute_room_file_tool("room_read_file", {"file_id": "../private"}, **scope)) == {
        "error": "invalid_arguments",
    }

    owner_member = (await db_session.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id,
        ChatRoomMember.user_id == owner["id"],
    ))).scalar_one()
    owner_member.role = "member"
    await db_session.commit()
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "files": [], "has_more": False,
    }
    owner_member.status = "left"
    await db_session.commit()
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "error": "room_file_unavailable",
    }
    owner_member.status = "active"
    owner_member.role = "owner"
    run.status = "failed"
    await db_session.commit()
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "error": "room_file_unavailable",
    }
    run.status = "running"
    room = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one()
    room.ai_tool_execution_enabled = False
    room.ai_file_actions_direct_enabled = False
    room.ai_tool_execution_revision = 2
    await db_session.commit()
    assert json.loads(await execute_room_file_tool("room_list_files", {}, **scope)) == {
        "error": "room_file_unavailable",
    }


@pytest.mark.asyncio
async def test_room_file_preview_extracts_office_and_keeps_binary_metadata_only(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    headers, owner = await _register(client, "room_tool_doc_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Documents", "owner_scope": "personal",
        "recipient_email": "not-joined@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    binding_id = created.json()["agents"][0]["id"]
    assert (await client.patch(
        f"/api/v1/chat/rooms/{room_id}/ai-execution", headers=headers,
        json={"enabled": True, "expected_revision": 0},
    )).status_code == 200
    now = datetime.now(timezone.utc)
    message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="Attachments", created_at=now,
    )
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="Read the attached document", created_at=now + timedelta(seconds=1),
    )
    db_session.add_all([message, source])
    await db_session.flush()
    word = WordDocument()
    word.add_paragraph("Quarterly group result")
    content = io.BytesIO()
    word.save(content)
    document_file = ChatRoomFile(
        id=generate_ulid(), message_id=message.id, name="result.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=len(content.getvalue()), content=content.getvalue(),
    )
    image_file = ChatRoomFile(
        id=generate_ulid(), message_id=message.id, name="image.png",
        mime_type="image/png", size=8, content=b"\x89PNG....",
    )
    db_session.add_all([document_file, image_file])
    await db_session.flush()
    fingerprint, _ = await room_audience_fingerprint(db_session, room_id)
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=binding_id, agent_name="Manor AI", status="running",
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
    )
    db_session.add(run)
    await db_session.commit()
    scope = {"room_id": room_id, "run_id": run.id, "expected_execution_revision": 1, "db": db_session}
    read_doc = json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": document_file.id}, **scope,
    ))
    assert "Quarterly group result" in read_doc["content"]
    read_image = json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": image_file.id}, **scope,
    ))
    assert read_image["preview_unavailable"] is True
    assert "content" not in read_image

    monkeypatch.setattr(
        entity_fs, "get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ROOT=str(tmp_path), MANOR_FS_ENABLED=True,
            DEPLOYMENT_MODE="oss",
        ),
    )
    generated_bytes = b"Generated for this exact room"
    path = tmp_path / owner["entity_id"] / "group-generated.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generated_bytes)
    document = Document(
        id=generate_ulid(), entity_id=owner["entity_id"], owner_id=owner["id"],
        name="group-generated.txt", fs_path="group-generated.txt",
        file_size=len(generated_bytes), mime_type="text/plain",
        visibility="private", quarantine_status="clean",
    )
    db_session.add(document)
    await db_session.flush()
    generated_file = ChatRoomFile(
        id=generate_ulid(), message_id=message.id, name="group-generated.txt",
        mime_type="text/plain", size=len(generated_bytes), content=None,
        document_id=document.id, document_entity_id=document.entity_id,
        content_sha256=sha256(generated_bytes).hexdigest(), source_run_id=run.id,
    )
    db_session.add(generated_file)
    await db_session.commit()
    generated = json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": generated_file.id}, **scope,
    ))
    assert generated.get("content") == generated_bytes.decode(), generated
    assert "document_id" not in generated and "fs_path" not in generated

    extracted_bytes: list[bytes] = []

    async def mutate_original_before_extract(file_path: str, **_kwargs: object) -> str:
        path.write_bytes(b"Z" * len(generated_bytes))
        extracted_bytes.append(Path(file_path).read_bytes())
        return extracted_bytes[-1].decode()

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            "packages.core.services.room_file_read_tools.extract_text",
            mutate_original_before_extract,
        )
        raced = json.loads(await execute_room_file_tool(
            "room_read_file", {"file_id": generated_file.id}, **scope,
        ))
    assert extracted_bytes == [generated_bytes]
    assert raced == {"error": "room_file_unavailable"}

    stale = json.loads(await execute_room_file_tool(
        "room_read_file", {"file_id": generated_file.id}, **scope,
    ))
    assert stale == {"error": "room_file_unavailable"}

    assert _extractable_type("report.pdf", "application/pdf") == "pdf"
    assert _extractable_type("sheet.xlsx", "application/zip") == "xlsx"
    assert _extractable_type("deck.pptx", "application/zip") == "pptx"
    assert _extractable_type("report.pdf", "image/png") is None
    assert {schema["function"]["name"] for schema in room_file_tool_schemas()} == {
        "room_list_files", "room_read_file",
    }


def test_office_preview_rejects_excessive_archive_expansion(tmp_path):
    archive_path = tmp_path / "expanded.docx"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (41 * 1024 * 1024))
    assert archive_path.stat().st_size < 1024 * 1024
    assert _safe_office_archive(archive_path) is False
