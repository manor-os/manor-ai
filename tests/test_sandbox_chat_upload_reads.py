"""Sandbox input copies enforce the current chat's attachment grant."""
from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from packages.core import database
from packages.core.ai.tools import sandbox_tools
from packages.core.config import get_settings
from packages.core.services import ai_file_permissions


ENTITY = "01TESTCHATPDFTOOLS000000000"
PDF_PATH = "uploads/chat/0123456789abcdef.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_path", [PDF_PATH, "/workspace/" + PDF_PATH])
@pytest.mark.parametrize("conversation_id,user_id,task_id,allowed", [
    ("current-chat", "owner", None, True),
    ("other-chat", "owner", None, False),
    ("current-chat", "other-user", None, False),
    (None, "owner", None, False),
    (None, "owner", "task-with-synthetic-sandbox-binding", False),
])
async def test_sandbox_import_keeps_real_conversation_attachment_scope(
    tmp_path, monkeypatch, workspace_path, conversation_id, user_id, task_id, allowed,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    original = b"%PDF-1.7\noriginal binary source\x00\xff"
    source = tmp_path / ENTITY / PDF_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(original)
    checked = []
    writes = []

    @asynccontextmanager
    async def session():
        yield object()

    async def attachment_readable(_db, **scope):
        checked.append(scope)
        return (
            scope["entity_id"] == ENTITY and scope["user_id"] == "owner"
            and scope["conversation_id"] == "current-chat"
            and scope["workspace_id"] is None and scope["path"] == PDF_PATH
        )

    async def sandbox_owned(**_kwargs):
        return None

    class Client:
        async def write_file_base64(self, **kwargs):
            writes.append(kwargs)
            return SimpleNamespace(path=kwargs["path"])

        async def close(self):
            pass

    async def client_for_sandbox(_sandbox_id):
        return Client()

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(ai_file_permissions, "_chat_upload_readable", attachment_readable)
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", sandbox_owned)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for_sandbox)
    if not allowed:
        def forbidden_snapshot(*_args, **_kwargs):
            raise AssertionError("Rejected source bytes must not be opened")
        monkeypatch.setattr(sandbox_tools, "runtime_open_entity_file_snapshot", forbidden_snapshot)

    result = await sandbox_tools._sandbox_write_file(
        entity_id=ENTITY, user_id=user_id, conversation_id=conversation_id,
        sandbox_id="owned-sandbox", path="/skill/tmp/pdfs/source.pdf",
        workspace_path=workspace_path, task_id=task_id,
    )
    assert len(checked) == 1
    assert checked[0]["conversation_id"] == conversation_id
    assert source.read_bytes() == original
    if allowed:
        assert "Written to /skill/tmp/pdfs/source.pdf" in result
        assert len(writes) == 1
        assert base64.b64decode(writes[0]["content_base64"]) == original
    else:
        assert json.loads(result)["error"]["code"] == "sandbox_access_denied"
        assert writes == []
