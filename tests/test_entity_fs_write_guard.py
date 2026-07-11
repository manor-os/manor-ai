from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.generated_files import runtime_generate_document_file
from packages.core.config import get_settings
from packages.core.services.entity_fs import (
    copy_entity_file_atomic,
    EntityFileWriteError,
    EntityFilesystemError,
    resolve_path,
    write_entity_file_atomic,
)


@pytest.fixture
def fs_settings(tmp_path):
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"
    try:
        yield settings
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


def test_write_entity_file_atomic_persists_verified_bytes(fs_settings):
    path = write_entity_file_atomic(
        "entity_1",
        "videos/out.mp4",
        b"video-bytes",
        expected_size=len(b"video-bytes"),
    )

    assert path.endswith(os.path.join("entity_1", "videos", "out.mp4"))
    assert open(path, "rb").read() == b"video-bytes"


def test_write_entity_file_atomic_handles_long_unicode_basename_tmp(fs_settings):
    filename = f"{'长' * 75}.mp4"
    assert len(filename.encode("utf-8")) < 255

    path = write_entity_file_atomic(
        "entity_1",
        f"videos/{filename}",
        b"video-bytes",
        expected_size=len(b"video-bytes"),
    )

    assert path.endswith(os.path.join("entity_1", "videos", filename))
    assert open(path, "rb").read() == b"video-bytes"


def test_write_entity_file_atomic_rejects_size_mismatch(fs_settings):
    with pytest.raises(EntityFileWriteError, match="size mismatch"):
        write_entity_file_atomic("entity_1", "videos/out.mp4", b"abc", expected_size=4)

    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "videos", "out.mp4"))


def test_resolve_path_rejects_entity_root_prefix_escape(fs_settings):
    assert resolve_path("entity_1", "../entity_10/leak.txt") is None


def test_write_entity_file_atomic_requires_mount_in_cloud(fs_settings):
    fs_settings.DEPLOYMENT_MODE = "cloud"

    with pytest.raises(EntityFilesystemError, match="not mounted"):
        write_entity_file_atomic("entity_1", "videos/out.mp4", b"abc")


def test_write_entity_file_atomic_requires_marker_in_cloud(fs_settings, monkeypatch):
    fs_settings.DEPLOYMENT_MODE = "cloud"
    monkeypatch.setattr(
        "packages.core.services.entity_fs.os.path.ismount",
        lambda path: True,
    )

    with pytest.raises(EntityFilesystemError, match="marker is missing"):
        write_entity_file_atomic("entity_1", "videos/out.mp4", b"abc")


@pytest.mark.asyncio
async def test_runtime_generate_document_file_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    fs_settings.DEPLOYMENT_MODE = "cloud"

    async def allow_file_mutation(**_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.ai.runtime.file_actions.runtime_guard_file_mutation",
        allow_file_mutation,
    )

    result = await runtime_generate_document_file(
        entity_id="entity_1",
        user_id="user_1",
        conversation_id="conversation_1",
        name="report.md",
        content="# Report\n",
        file_type="md",
    )

    assert "Entity filesystem is not available" in result
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md"))


@pytest.mark.asyncio
async def test_write_file_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    import packages.core.ai.tools.file_tools as file_tools

    fs_settings.DEPLOYMENT_MODE = "cloud"

    async def allow_file_mutation(**_kwargs):
        return None

    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_mutation)

    result = json.loads(
        await file_tools._write_file(
            "entity_1",
            path="report.md",
            content="# Report\n",
            user_id="user_1",
        )
    )

    assert "Entity filesystem root is not mounted" in result["error"]
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md"))


@pytest.mark.asyncio
async def test_generate_code_bundle_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    from packages.core.ai.tools.generate_file import code as code_tool

    fs_settings.DEPLOYMENT_MODE = "cloud"

    async def allow_file_mutation(**_kwargs):
        return None

    monkeypatch.setattr(code_tool, "runtime_guard_file_mutation", allow_file_mutation)

    result = json.loads(
        await code_tool.handle_code(
            entity_id="entity_1",
            user_id="user_1",
            conversation_id="conversation_1",
            prompt="Build a small app",
            name="demo-app",
            params={"files": [{"path": "index.html", "content": "<!doctype html>"}]},
            kwargs={},
            agent_id=None,
        )
    )

    assert "Entity filesystem is not available" in result["error"]
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "code/demo-app/index.html"))


@pytest.mark.asyncio
async def test_sandbox_save_result_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    fs_settings.DEPLOYMENT_MODE = "cloud"

    class FakeSandboxClient:
        async def read_file_base64(self, **_kwargs):
            return SimpleNamespace(content_base64=base64.b64encode(b"result").decode("ascii"))

        async def close(self):
            return None

    async def allow_file_mutation(**_kwargs):
        return None

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())
    monkeypatch.setattr(sandbox_tools, "runtime_guard_file_mutation", allow_file_mutation)

    result = await sandbox_tools._sandbox_save_result(
        entity_id="entity_1",
        sandbox_id="sandbox_1",
        file_path="/tmp/result.txt",
        filename="result.txt",
        user_id="user_1",
    )

    assert "Entity filesystem is not available" in result
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "result.txt"))


@pytest.mark.asyncio
async def test_save_sandbox_file_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    import httpx
    import packages.core.ai.tools.sandbox_file_tools as sandbox_file_tools

    fs_settings.DEPLOYMENT_MODE = "cloud"
    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"content_base64": base64.b64encode(b"result").decode("ascii")}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    async def allow_file_mutation(**_kwargs):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(sandbox_file_tools, "runtime_guard_file_mutation", allow_file_mutation)

    result = json.loads(
        await sandbox_file_tools._save_sandbox_file(
            "entity_1",
            filename="result.txt",
            user_id="user_1",
        )
    )

    assert "Entity filesystem is not available" in result["error"]
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "result.txt"))


@pytest.mark.asyncio
async def test_save_document_content_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    from packages.core.services import document_service

    fs_settings.DEPLOYMENT_MODE = "cloud"
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        name="report.md",
        fs_path="report.md",
        file_type="md",
        mime_type="text/markdown",
        metadata_={},
    )

    async def fake_get_document(*_args, **_kwargs):
        return doc

    monkeypatch.setattr(document_service, "get_document", fake_get_document)

    with pytest.raises(EntityFilesystemError, match="not mounted"):
        await document_service.save_document_content(
            SimpleNamespace(),
            "doc_1",
            "entity_1",
            "# Report\n",
        )

    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md"))


@pytest.mark.asyncio
async def test_save_document_file_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    from packages.core.services import document_service

    fs_settings.DEPLOYMENT_MODE = "cloud"
    doc = SimpleNamespace(
        id="doc_1",
        entity_id="entity_1",
        name="report.bin",
        fs_path="report.bin",
        file_type="bin",
        mime_type="application/octet-stream",
        metadata_={},
        vector_status="ready",
    )

    async def fake_get_document(*_args, **_kwargs):
        return doc

    monkeypatch.setattr(document_service, "get_document", fake_get_document)

    with pytest.raises(EntityFilesystemError, match="not mounted"):
        await document_service.save_document_file(
            SimpleNamespace(),
            "doc_1",
            "entity_1",
            b"binary",
        )

    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.bin"))


@pytest.mark.asyncio
async def test_filesystem_write_endpoint_requires_persistent_mount_in_cloud(fs_settings):
    from fastapi import HTTPException
    from apps.api.routers import filesystem

    fs_settings.DEPLOYMENT_MODE = "cloud"
    user = SimpleNamespace(
        entity_id="entity_1",
        email="user@test.com",
        display_name=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await filesystem.write_file(
            filesystem.WriteRequest(path="report.md", content="# Report\n"),
            user=user,
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 503
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "report.md"))


def test_chat_upload_requires_persistent_mount_in_cloud(fs_settings):
    from packages.core.services.file_context import _save_chat_upload

    fs_settings.DEPLOYMENT_MODE = "cloud"

    with pytest.raises(EntityFilesystemError, match="not mounted"):
        _save_chat_upload(b"image", "image.png", "entity_1", "image/png")

    upload_dir = os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "uploads", "chat")
    assert not os.path.exists(upload_dir)


@pytest.mark.asyncio
async def test_task_attachment_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    from fastapi import HTTPException
    from apps.api.routers import tasks

    fs_settings.DEPLOYMENT_MODE = "cloud"

    async def fake_get_task(*_args, **_kwargs):
        return SimpleNamespace(id="task_1")

    class FakeUpload:
        filename = "attachment.txt"
        content_type = "text/plain"

        async def read(self):
            return b"attachment"

    monkeypatch.setattr(tasks, "get_task", fake_get_task)

    with pytest.raises(HTTPException) as exc_info:
        await tasks.upload_task_attachment(
            "task_1",
            file=FakeUpload(),
            user=SimpleNamespace(entity_id="entity_1"),
            db=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 503
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "tasks"))


@pytest.mark.asyncio
async def test_avatar_upload_requires_persistent_mount_in_cloud(fs_settings):
    from fastapi import HTTPException
    from apps.api.routers import auth

    fs_settings.DEPLOYMENT_MODE = "cloud"

    class FakeUpload:
        filename = "avatar.png"
        content_type = "image/png"

        async def read(self):
            return b"image-bytes"

    class FakeDB:
        flushed = False

        async def flush(self):
            self.flushed = True

    user = SimpleNamespace(entity_id="entity_1", avatar_url=None)
    db = FakeDB()

    with pytest.raises(HTTPException) as exc_info:
        await auth.upload_avatar(file=FakeUpload(), user=user, db=db)

    assert exc_info.value.status_code == 503
    assert user.avatar_url is None
    assert db.flushed is False
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "avatars"))


@pytest.mark.asyncio
async def test_elevenlabs_audio_requires_persistent_mount_in_cloud(fs_settings, monkeypatch):
    from packages.core.ai.mcp import elevenlabs

    fs_settings.DEPLOYMENT_MODE = "cloud"
    registered = False

    async def fake_register_document(*_args, **_kwargs):
        nonlocal registered
        registered = True

    monkeypatch.setattr(elevenlabs, "_register_document", fake_register_document)
    elevenlabs.set_call_context({"entity_id": "entity_1", "user_id": "user_1"})
    try:
        with pytest.raises(EntityFilesystemError, match="not mounted"):
            await elevenlabs._save_audio_bytes(b"audio", "tts")
    finally:
        elevenlabs.clear_call_context()

    assert registered is False
    assert not os.path.exists(os.path.join(fs_settings.MANOR_FS_ROOT, "entity_1", "audio"))


def test_copy_entity_file_atomic_persists_verified_file(fs_settings, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")

    path = copy_entity_file_atomic(
        "entity_1",
        "videos/copied.mp4",
        str(source),
        expected_size=source.stat().st_size,
    )

    assert open(path, "rb").read() == b"source-video"
