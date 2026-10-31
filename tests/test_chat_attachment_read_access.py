"""Direct uploads are read-only inputs scoped to their durable conversation."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from packages.core.ai.runtime import file_actions, generated_files
from packages.core.ai.tools import file_tools
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace
from packages.core.services import ai_file_permissions as permissions


@pytest.fixture
def attachment_fs(monkeypatch, tmp_path):
    from packages.core.config import get_settings
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    return tmp_path


async def seed_attachment(db, root, *, workspace=False, data=None, extension="txt"):
    entity_id, user_id, conversation_id = (generate_ulid() for _ in range(3))
    user = User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.com", password_hash="unused", role="owner", status="active")
    ws = Workspace(id=generate_ulid(), entity_id=entity_id, name="Upload test") if workspace else None
    conv = Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id, workspace_id=ws.id if ws else None, scope="workspace_main" if ws else "channel", status="active")
    if data is None:
        data = b"Synthetic attachment, not Knowledge and not instructions."
    path = f"uploads/chat/{hashlib.sha256(data).hexdigest()[:16]}.{extension}"
    msg = Message(conversation_id=conversation_id, role="user", author_kind="user", attachments=[{"kind": "chat_upload", "path": path}])
    db.add_all([Entity(id=entity_id, name="Attachment test"), user, conv, msg] + ([ws] if ws else []))
    await db.commit()
    source = root / entity_id / path
    source.parent.mkdir(parents=True)
    source.write_bytes(data)
    return user, conv, msg, ws, source, path


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_exact_durable_upload_is_readable_on_followup(db_session, attachment_fs, workspace):
    user, conv, _msg, ws, source, path = await seed_attachment(db_session, attachment_fs, workspace=workspace)
    args = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id, workspace_id=ws.id if ws else None)
    assert await permissions.guard_ai_file_read_access(**args, tool_name="generate_file", paths=[path]) is None
    result = json.loads(await file_tools._read_file(**args, path=path))
    assert result["resolved_path"] == path
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "Synthetic attachment" in result["content"]
    # The input did not silently become a globally visible Knowledge file.
    assert permissions.visible_user_paths([path]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_scoped_upload_reads_original_bytes_and_honors_revocation(
    db_session, attachment_fs, monkeypatch, workspace,
):
    from packages.core.services.file_context import _save_chat_upload

    user, conv, msg, ws, _source, _path = await seed_attachment(
        db_session, attachment_fs, workspace=workspace,
    )
    original = b"sku,quantity\nSYNTHETIC-001,7\n"
    path, _url = _save_chat_upload(
        original, "inventory.csv", user.entity_id, "text/csv", storage_scope=conv.id,
    )
    msg.attachments = [{"kind": "chat_upload", "path": path}]
    await db_session.commit()
    args = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id,
                workspace_id=ws.id if ws else None)
    assert await permissions.guard_ai_file_read_access(**args, tool_name="invoke_skill", paths=[path]) is None
    result = json.loads(await file_tools._read_file(**args, path=path))
    assert result["content"] == original.decode()
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert permissions.visible_user_paths([path]) == []

    msg.attachments = None
    await db_session.commit()

    async def forbidden_read(*_args, **_kwargs):
        pytest.fail("Revoked scoped attachment reached byte extraction")

    monkeypatch.setattr(file_tools, "_read_supported_text", forbidden_read)
    assert await permissions.guard_ai_file_read_access(**args, tool_name="invoke_skill", paths=[path])
    denied = json.loads(await file_tools._read_file(**args, path=path))
    assert "error" in denied and "content" not in denied


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    None, "with_conversation", "invalid_conversation", "paused_workspace", "inactive_user",
    "trashed", "foreign_entity", "wrong_workspace", "restricted", "userless",
])
async def test_probe_knowledge_document_keeps_canonical_source_under_workflow_scope(
    db_session, attachment_fs, monkeypatch, restriction,
):
    from packages.core.ai.tools import media_tools
    from packages.core.services.generated_media_naming import WorkspaceArtifactScope, workspace_artifact_scope

    user, conv, _msg, ws, _upload, _path = await seed_attachment(db_session, attachment_fs, workspace=True)
    if restriction == "paused_workspace":
        ws.status = "paused"
    if restriction == "inactive_user":
        user.status = "inactive"
    allowed = restriction in (None, "with_conversation")
    source = attachment_fs / user.entity_id / "source.wav"
    source.write_bytes(b"synthetic root Knowledge audio")
    document = Document(
        entity_id=user.entity_id, name="source.wav", fs_path="source.wav", file_type="wav",
        source="upload", owner_id=user.id, visibility="entity", is_trashed=restriction == "trashed",
        classification="restricted" if restriction == "restricted" else "internal",
    )
    db_session.add(document)
    await db_session.commit()
    calls = []

    async def probe(_ffprobe, actual_path, *, pass_fds):
        from pathlib import Path

        assert allowed, "Unauthorized Knowledge source reached ffprobe"
        assert pass_fds == (int(Path(actual_path).name),)
        assert Path(actual_path).read_bytes() == source.read_bytes()
        calls.append(actual_path)
        return {"duration_seconds": 10.0, "has_audio": True}

    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: "/test/ffprobe")
    monkeypatch.setattr(media_tools, "_probe_media_report", probe)
    with workspace_artifact_scope(WorkspaceArtifactScope(
        entity_id=user.entity_id, workspace_id=ws.id, workflow_run_id=generate_ulid(),
    )):
        result = json.loads(await media_tools._probe_media_handler(
            entity_id=generate_ulid() if restriction == "foreign_entity" else user.entity_id,
            _user_id_from_context=None if restriction == "userless" else user.id,
            workspace_id=generate_ulid() if restriction == "wrong_workspace" else ws.id,
            conversation_id=(
                conv.id if restriction == "with_conversation"
                else generate_ulid() if restriction == "invalid_conversation" else None
            ), document_id=document.id,
        ))
    if not allowed:
        assert result["status"] == "error"
        assert not calls
    else:
        assert result["status"] == "completed", result
        assert result["document_id"] == document.id
        assert result["fs_path"] == "source.wav"
        assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert result["report"] == {"duration_seconds": 10.0, "has_audio": True}
        assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [None, "unprojected", "foreign_workspace", "unlinked", "private", "trashed", "restricted", "quarantined", "userless", "symlink", "missing_file"])
@pytest.mark.parametrize("reader", ["read_file", "probe_media", "probe_media_url"])
async def test_workspace_task_thread_reads_only_authorized_projected_upload(
    db_session, attachment_fs, monkeypatch, restriction, reader,
):
    user, _main, _msg, ws, source, path = await seed_attachment(
        db_session, attachment_fs, workspace=True,
    )
    thread = Conversation(
        entity_id=user.entity_id, user_id=user.id, workspace_id=ws.id,
        scope="workspace_thread", status="active", thread_ref_kind="task",
        thread_ref_id=generate_ulid(),
    )
    db_session.add(thread)
    if restriction == "private":
        from packages.core.models.user import UserMembership
        from packages.core.models.workspace import WorkspaceStaff

        user.role = "member"
        db_session.add(UserMembership(
            entity_id=user.entity_id, user_id=user.id, role="member", status="active",
        ))
        db_session.add(WorkspaceStaff(workspace_id=ws.id, user_id=user.id, role="member", status="active"))
    if restriction != "unprojected":
        db_session.add(Document(
            entity_id=user.entity_id, name="projected.txt", fs_path=path,
            file_type="txt", source="chat_upload", owner_id=generate_ulid() if restriction == "private" else user.id,
            visibility="private" if restriction == "private" else "workspace", is_trashed=restriction == "trashed",
            classification="restricted" if restriction == "restricted" else "internal",
            quarantine_status="quarantined" if restriction == "quarantined" else "clean",
            metadata_={} if restriction == "unlinked" else {"origin": {"workspace_id": generate_ulid() if restriction == "foreign_workspace" else ws.id}},
        ))
    await db_session.commit()
    if restriction == "symlink":
        target = source.with_name("private.txt")
        source.rename(target)
        source.symlink_to(target)
    elif restriction == "missing_file":
        source.unlink()
    if restriction == "private":
        from packages.core.services.document_access import DocumentAccessContext

        ctx = await DocumentAccessContext.load(db_session, entity_id=user.entity_id, user_id=user.id)
        assert await ctx.workspace_readable(db_session, ws.id)
    if reader == "read_file":
        result = json.loads(await file_tools._read_file(
            entity_id=user.entity_id, user_id=None if restriction == "userless" else user.id, conversation_id=thread.id,
            workspace_id=ws.id, path=path,
        ))
    else:
        from packages.core.ai.tools import media_tools

        async def workspace_base(**_kwargs):
            return "Workspaces/_by_id/test-artifacts"

        async def probe(_ffprobe, actual_path):
            assert restriction is None, "Unauthorized attachment reached ffprobe"
            assert actual_path == str(source)
            return {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}

        monkeypatch.setattr(media_tools.shutil, "which", lambda _name: "/test/ffprobe")
        monkeypatch.setattr(media_tools, "_workspace_media_base_dir", workspace_base)
        monkeypatch.setattr(media_tools, "_probe_media_report", probe)
        reference = f"/api/v1/fs/{user.entity_id}/{path}" if reader.endswith("_url") else path
        result = json.loads(await media_tools._probe_media_handler(
            entity_id=user.entity_id, _user_id_from_context=None if restriction == "userless" else user.id,
            conversation_id=thread.id, workspace_id=ws.id, input_path=reference,
        ))
    if restriction:
        assert "error" in result
        assert "content" not in result
    else:
        evidence = result if reader == "read_file" else result.get("report", {})
        assert evidence.get("source_sha256") == hashlib.sha256(source.read_bytes()).hexdigest(), result
        if reader == "read_file":
            assert "Synthetic attachment" in result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_template", [False, True])
@pytest.mark.parametrize("restriction", [
    "other_user", "other_conversation", "other_entity", "no_conversation", "userless",
    "inactive_user", "deleted_user", "deleted_conversation", "no_reference",
    "assistant_reference", "prompt_only", "wrong_workspace", "deleted_workspace",
    "trashed_document", "restricted_document", "quarantined_document", "symlink",
])
async def test_upload_guard_fails_closed_before_read(db_session, attachment_fs, monkeypatch, restriction, as_template):
    from tests.test_office_template_generation import template_patch

    user, conv, msg, ws, source, path = await seed_attachment(
        db_session, attachment_fs, workspace=restriction == "deleted_workspace",
        extension="docx" if as_template else "txt",
    )
    args = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id, workspace_id=ws.id if ws else None)
    if restriction == "other_user":
        args["user_id"] = generate_ulid()
    elif restriction == "other_conversation":
        other = Conversation(entity_id=user.entity_id, user_id=user.id, status="active", scope="channel")
        db_session.add(other)
        await db_session.flush()
        args["conversation_id"] = other.id
    elif restriction == "other_entity":
        args["entity_id"] = generate_ulid()
    elif restriction == "no_conversation":
        args["conversation_id"] = None
    elif restriction == "userless":
        args["user_id"] = None
    elif restriction == "inactive_user":
        user.status = "inactive"
    elif restriction == "deleted_user":
        user.deleted_at = datetime.now(timezone.utc)
    elif restriction == "deleted_conversation":
        await db_session.delete(conv)
    elif restriction in {"no_reference", "prompt_only"}:
        msg.attachments = None
        msg.content = path if restriction == "prompt_only" else "hello"
    elif restriction == "assistant_reference":
        msg.role = "assistant"
    elif restriction == "wrong_workspace":
        other_workspace = Workspace(entity_id=user.entity_id, name="Other workspace")
        db_session.add(other_workspace)
        await db_session.flush()
        args["workspace_id"] = other_workspace.id
    elif restriction == "deleted_workspace":
        ws.deleted_at = datetime.now(timezone.utc)
    elif restriction.endswith("_document"):
        doc = Document(entity_id=user.entity_id, name="test.txt", fs_path=path, file_type="txt", owner_id=user.id, visibility="private")
        if restriction == "trashed_document":
            doc.is_trashed = True
        elif restriction == "restricted_document":
            doc.classification = "restricted"
        else:
            doc.quarantine_status = "quarantined"
        db_session.add(doc)
    elif restriction == "symlink":
        other = source.with_name("private.txt")
        source.rename(other)
        source.symlink_to(other)
    await db_session.commit()

    async def no_read(*_args, **_kwargs):
        pytest.fail("Unauthorized attachment reached byte extraction")

    monkeypatch.setattr(file_tools, "_read_supported_text", no_read)
    if as_template:
        def no_snapshot(*_args, **_kwargs):
            pytest.fail("Unauthorized attachment reached template snapshot")

        monkeypatch.setattr(generated_files, "_read_office_template_sync", no_snapshot)
        monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", no_read)
    assert await permissions.guard_ai_file_read_access(**args, tool_name="generate_file", paths=[path])
    assert "error" in json.loads(await file_tools._read_file(**args, path=path))
    if as_template:
        if restriction == "deleted_workspace":
            # Output scope rejects a deleted Workspace before source admission.
            with pytest.raises(RuntimeError, match="Workspace artifact scope could not resolve"):
                await generated_files.runtime_generate_document_file(
                    **args, name="copy.docx", file_type="docx", operations=[template_patch("docx")],
                    template={"path": path, "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                )
            return
        result = json.loads(await generated_files.runtime_generate_document_file(
            **args, name="copy.docx", file_type="docx", operations=[template_patch("docx")],
            template={"path": path, "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        ))
        assert result.get("error"), result
        assert result["mode"] == "entity_file_acl_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_template", [False, True])
async def test_attachment_generation_uses_the_same_acl_and_source_snapshot(db_session, attachment_fs, monkeypatch, as_template):
    user, conv, msg, _ws, source, path = await seed_attachment(db_session, attachment_fs)
    from tests.test_office_template_generation import template_bytes, template_patch
    data = template_bytes("docx")
    office_path = f"uploads/chat/{hashlib.sha256(data).hexdigest()[:16]}.docx"
    source = source.parent / office_path.rsplit("/", 1)[1]
    source.write_bytes(data)
    msg.attachments = [{"kind": "chat_upload", "path": office_path}]
    await db_session.commit()
    calls = []

    async def approval(**kwargs):
        calls.append(kwargs)
        return json.dumps({"error": "test_approval_required"})

    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", approval)
    input_key = "template" if as_template else "source"
    args = dict(
        entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id,
        name="copy.docx" if as_template else "converted.pdf",
        file_type="docx" if as_template else "pdf",
        **{input_key: {"path": office_path, "expected_sha256": hashlib.sha256(data).hexdigest()}},
    )
    if as_template:
        args["operations"] = [template_patch("docx")]
    result = json.loads(await generated_files.runtime_generate_document_file(**args))
    assert result["error"] == "test_approval_required"
    assert len(calls) == 1  # Input permission does not grant output mutation.
    assert calls[0]["approval_payload"][input_key] == args[input_key]
    assert source.read_bytes() == data
    args[input_key]["expected_sha256"] = "0" * 64
    assert json.loads(await generated_files.runtime_generate_document_file(**args))["error"] == "source_changed"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_attachment_template_creates_formatted_knowledge_copy(db_session, attachment_fs, monkeypatch, workspace):
    from sqlalchemy import select

    from packages.core.ai.agentic_loop import _compact_tool_result_for_context
    from packages.core.services import knowledge_sync
    from tests.test_office_template_generation import assert_template_result, template_bytes

    data = template_bytes("docx")
    user, conv, _msg, ws, source, path = await seed_attachment(
        db_session, attachment_fs, workspace=workspace, data=data, extension="docx",
    )
    args = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id, workspace_id=ws.id if ws else None)
    read = json.loads(await file_tools._read_file(**args, path=path))
    assert "template=" in read["editing_hint"]
    oversized_read = {**read, "content": "X" * 27000, "structure": {f"field_{index}": "X" * 1000 for index in range(50)}}
    compacted = _compact_tool_result_for_context("read_file", json.dumps(oversized_read), max_chars=4000)
    assert read["editing_hint"] in compacted
    approvals = []

    async def approval(**kwargs):
        approvals.append(kwargs)

    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", approval)
    monkeypatch.setattr(knowledge_sync, "_schedule_document_reembed", lambda _id: None)
    result = json.loads(await generated_files.runtime_generate_document_file(
        **args, name="completed.docx", file_type="docx",
        operations=[{"operation": "replace_text", "old_text": "Proposal", "new_text": "Template client"}],
        template={"path": read["resolved_path"], "expected_sha256": read["source_sha256"]},
    ))

    assert result.get("created") is True, result
    assert len(approvals) == 1
    assert approvals[0]["user_id"] == user.id
    assert approvals[0]["conversation_id"] == conv.id
    assert source.read_bytes() == data
    assert permissions.visible_user_paths([path]) == []
    document = await db_session.scalar(select(Document).where(Document.id == result["document"]["document_id"]))
    assert document is not None
    assert document.fs_path != path
    assert approvals[0]["paths"] == [document.fs_path]
    output = attachment_fs / user.entity_id / document.fs_path
    assert_template_result(output.read_bytes(), "docx", "Template client")
    assert result["document"]["source_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["document"]["viewer_url"]
    if ws:
        assert document.metadata_["origin"]["conversation_id"] == conv.id
        assert document.metadata_["origin"]["workspace_id"] == ws.id
    assert await db_session.scalar(select(Document.id).where(Document.entity_id == user.entity_id, Document.fs_path == path)) is None

    async def require_pdf_approval(**kwargs):
        approvals.append(kwargs)
        return json.dumps({"error": "test_approval_required"})

    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", require_pdf_approval)
    pdf = json.loads(await generated_files.runtime_generate_document_file(
        **args, name="completed.pdf", file_type="pdf",
        source={"path": document.fs_path, "expected_sha256": result["document"]["source_sha256"]},
    ))
    assert pdf["error"] == "test_approval_required"
    assert len(approvals) == 2


@pytest.mark.asyncio
async def test_expired_upload_template_cannot_create_a_copy(db_session, attachment_fs, monkeypatch):
    from tests.test_office_template_generation import template_patch

    user, conv, _msg, _ws, source, path = await seed_attachment(db_session, attachment_fs, extension="docx")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source.unlink()

    async def no_approval(**_kwargs):
        pytest.fail("Missing attachment reached output approval")

    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", no_approval)
    result = json.loads(await generated_files.runtime_generate_document_file(
        entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id,
        name="copy.docx", file_type="docx", operations=[template_patch("docx")],
        template={"path": path, "expected_sha256": digest},
    ))
    assert result["error"] == "template_unavailable"
    assert not (attachment_fs / user.entity_id / "copy.docx").exists()


@pytest.mark.asyncio
async def test_expired_upload_does_not_fall_back_to_same_basename(db_session, attachment_fs, monkeypatch):
    user, conv, _msg, _ws, source, path = await seed_attachment(db_session, attachment_fs)
    replacement = source.parents[2] / "docs" / source.name
    replacement.parent.mkdir()
    source.rename(replacement)

    async def no_search(*_args, **_kwargs):
        pytest.fail("An expired attachment must not search other folders")

    monkeypatch.setattr(file_tools, "_locate_entity_file", no_search)
    result = json.loads(await file_tools._read_file(
        entity_id=user.entity_id, user_id=user.id, conversation_id=conv.id, path=path,
    ))
    assert "error" in result
    assert "content" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "uploads/chat", "uploads/chat/guess.docx", "uploads/chat/../private.docx", ".ai/secret",
    "uploads/chat/1234567890abcdef.docx/extra",
    "uploads/chat/0123456789abcdef/../1234567890abcdef.docx",
    "uploads/chat/0123456789abcdef/0123456789abcdef/1234567890abcdef.docx",
    "uploads/chat/0123456789abcdef/1234567890abcdef.docx?x=1",
])
async def test_hidden_aliases_never_become_attachment_authority(attachment_fs, path):
    assert not permissions.is_chat_upload_path(path)
    assert await permissions.guard_ai_file_read_access(entity_id="entity", user_id="user", conversation_id="chat", tool_name="generate_file", paths=[path])
