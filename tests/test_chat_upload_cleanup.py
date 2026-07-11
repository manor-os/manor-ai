from __future__ import annotations

import os

from packages.core.services.chat_upload_cleanup import (
    cleanup_chat_uploads_on_disk,
    local_upload_rel_path,
    media_param_upload_refs,
)


def _write(path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_cleanup_deletes_only_expired_hidden_chat_uploads(tmp_path):
    now = 1_800_000_000.0
    entity = "entity_1"
    entity_root = tmp_path / entity

    old_upload = entity_root / "uploads" / "chat" / "old.png"
    recent_upload = entity_root / "uploads" / "chat" / "recent.png"
    protected_upload = entity_root / "uploads" / "chat" / "protected.png"
    knowledge_file = entity_root / "campaign" / "reference.png"

    for path in (old_upload, recent_upload, protected_upload, knowledge_file):
        _write(path, b"image")

    os.utime(old_upload, (now - 40 * 86400, now - 40 * 86400))
    os.utime(protected_upload, (now - 40 * 86400, now - 40 * 86400))
    os.utime(recent_upload, (now - 2 * 86400, now - 2 * 86400))
    os.utime(knowledge_file, (now - 40 * 86400, now - 40 * 86400))

    report = cleanup_chat_uploads_on_disk(
        str(tmp_path),
        retention_days=30,
        active_refs={(entity, "uploads/chat/protected.png")},
        now=now,
    )

    assert report.files_deleted == 1
    assert not old_upload.exists()
    assert recent_upload.exists()
    assert protected_upload.exists()
    assert knowledge_file.exists()
    assert report.skipped_recent == 1
    assert report.skipped_active == 1


def test_media_param_upload_refs_extracts_only_local_chat_uploads():
    params = {
        "first_frame_url": "/api/v1/fs/entity_1/uploads/chat/start.png",
        "last_frame_url": "https://app.manorai.xyz/api/v1/fs/entity_1/uploads/chat/end.png",
        "reference_urls": [
            "/api/v1/fs/entity_1/campaign/reference.png",
            "/api/v1/fs/other_entity/uploads/chat/wrong.png",
            "uploads/chat/direct.png",
            "https://cdn.example.test/external.png",
        ],
    }

    assert media_param_upload_refs("entity_1", params) == {
        "uploads/chat/start.png",
        "uploads/chat/end.png",
        "uploads/chat/direct.png",
    }


def test_local_upload_rel_path_rejects_visible_knowledge_paths():
    assert local_upload_rel_path("/api/v1/fs/entity_1/campaign/ref.png", "entity_1") is None
    assert local_upload_rel_path("/api/v1/fs/entity_1/uploads/chat/ref.png", "entity_1") == "uploads/chat/ref.png"
