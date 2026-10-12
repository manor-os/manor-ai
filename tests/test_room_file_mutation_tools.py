"""Path-free, exact-call contracts for approved group file mutations."""
from __future__ import annotations

import io
import importlib.util
import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import ChatRoomAgentRun, ChatRoomMessage, ChatRoomToolApproval
from packages.core.models.document import Document
from packages.core.config import get_settings
from packages.core.services.room_agent_runtime import room_audience_fingerprint
from packages.core.services.room_file_mutation_tools import (
    RoomFileMutationError,
    _render_file,
    execute_room_file_mutation,
    room_file_mutation_tool_schemas,
    validate_room_file_mutation,
)
from packages.core.services.room_tool_approval_service import (
    canonical_arguments_hash, finish_room_tool_call,
)
from packages.core.services.room_generated_files import project_room_generated_document


def test_room_mutation_schemas_are_path_free_and_summary_is_server_authored():
    schemas = room_file_mutation_tool_schemas()
    assert {schema["function"]["name"] for schema in schemas} == {
        "room_create_file", "room_replace_file", "room_rename_file", "room_delete_file",
    }
    serialized = json.dumps(schemas)
    assert "document_id" not in serialized
    assert "entity_id" not in serialized
    assert "source_path" not in serialized
    assert "external_url" not in serialized
    normalized, summary = validate_room_file_mutation(
        "room_create_file", {"name": "summary.docx", "content": "# Quarterly summary"},
    )
    assert normalized == {"name": "summary.docx", "content": "# Quarterly summary"}
    assert "DOCX" in summary and "UTF-8 bytes" in summary and "SHA-256" in summary
    assert "Quarterly summary" not in summary


@pytest.mark.parametrize("name", [
    "../secret.txt", "sub/file.txt", "\\private.txt", ".hidden.txt", "report.exe",
    "bad\x00name.txt", "report", "report.pdf/other.txt",
])
def test_room_create_rejects_unsafe_or_unsupported_names(name):
    with pytest.raises(RoomFileMutationError):
        validate_room_file_mutation(
            "room_create_file", {"name": name, "content": "example"},
        )


def test_room_mutations_reject_arbitrary_context_and_bad_content():
    file_id = generate_ulid()
    with pytest.raises(RoomFileMutationError):
        validate_room_file_mutation("room_delete_file", {
            "file_id": file_id, "entity_id": generate_ulid(),
        })
    with pytest.raises(RoomFileMutationError):
        validate_room_file_mutation("room_create_file", {
            "name": "bad.json", "content": "not json",
        })
    with pytest.raises(RoomFileMutationError):
        validate_room_file_mutation("room_create_file", {
            "name": "too-large.xlsx", "content": ",".join("x" for _ in range(65)),
        })
    renamed, _ = validate_room_file_mutation("room_rename_file", {
        "file_id": file_id, "new_name": "clip.mp4",
    })
    assert renamed["new_name"] == "clip.mp4"
    if importlib.util.find_spec("reportlab") is None and importlib.util.find_spec("fpdf") is None:
        with pytest.raises(RoomFileMutationError, match="unsupported_file_format"):
            validate_room_file_mutation("room_create_file", {
                "name": "unavailable.pdf", "content": "Body",
            })


@pytest.mark.asyncio
async def test_room_renderers_produce_supported_bytes_without_a_source_path():
    for name, content, signature in (
        ("note.txt", "hello", b"hello"),
        ("data.json", '{"count": 1}', b"{\"count\": 1}"),
        ("brief.docx", "# Title\nBody", b"PK"),
        ("brief.pptx", "# Title\nBody", b"PK"),
    ):
        output, mime = await _render_file(name, content)
        assert output.startswith(signature)
        assert mime
    output, _ = await _render_file("scores.xlsx", "Name,Score\nA,9")
    workbook = load_workbook(io.BytesIO(output), read_only=True)
    try:
        assert workbook.active["A2"].value == "A"
        assert workbook.active["B2"].value == "9"
    finally:
        workbook.close()


@pytest.mark.asyncio
async def test_pdf_renderer_when_optional_backend_is_available():
    if importlib.util.find_spec("reportlab") is None and importlib.util.find_spec("fpdf") is None:
        pytest.skip("PDF generation backend is not installed in this environment")
    output, mime = await _render_file("brief.pdf", "# Title\nBody")
    assert output.startswith(b"%PDF")
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_claimed_room_create_file_stays_private_until_approved_projection(
    client: AsyncClient, db_session, monkeypatch, tmp_path,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    registered = await client.post("/api/v1/auth/register", json={
        "email": "room_writer@example.com", "password": "test-password-123",
        "entity_name": "Room Writers",
    })
    assert registered.status_code in (200, 201), registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Generated files", "owner_scope": "entity",
        "recipient_email": "unjoined-writer@example.com",
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
        content="Create a document", created_at=now,
    )
    db_session.add(source)
    await db_session.flush()
    fingerprint, _ = await room_audience_fingerprint(db_session, room_id)
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=binding_id, agent_name="Manor AI", status="running",
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
    )
    args, summary = validate_room_file_mutation("room_create_file", {
        "name": "summary.txt", "content": "Approved group-only result",
    })
    approval = ChatRoomToolApproval(
        id=generate_ulid(), room_id=room_id, run_id=run.id,
        requester_user_id=owner["id"], binding_id=binding_id,
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
        tool_call_id="call-room-create-1", tool_name="room_create_file",
        arguments_hash=canonical_arguments_hash(args), display_summary=summary,
        status="claimed", decided_by_user_id=owner["id"],
        decided_at=now, claimed_at=now,
    )
    db_session.add_all([run, approval])
    await db_session.commit()

    result = await execute_room_file_mutation(
        "room_create_file", args, room_id=room_id, run_id=run.id,
        expected_execution_revision=1, approval_id=approval.id,
    )
    assert result.document_id is not None
    assert "document_id" not in result.json_result
    assert "Group chat files" not in result.json_result
    document = (await db_session.execute(select(Document).where(
        Document.id == result.document_id,
    ))).scalar_one()
    assert document.name == "summary.txt"
    assert document.owner_id == owner["id"]
    assert document.created_by == owner["id"]
    assert document.visibility == "private"
    assert document.fs_path.startswith("Group chat files/")
    assert document.fs_path.endswith("-summary.txt")

    await finish_room_tool_call(
        db_session, approval_id=approval.id, succeeded=True,
        result_document_id=result.document_id,
    )
    run.status = "succeeded"
    await db_session.commit()
    published = await project_room_generated_document(
        db_session, run_id=run.id, document_id=result.document_id,
    )
    assert published.document_id == result.document_id
    assert published.name == "summary.txt"
    assert published.content is None
