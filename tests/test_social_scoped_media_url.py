"""Media reference conversion cannot downgrade scoped publication downloads."""

import pytest

from packages.core.config import get_settings
from packages.core.services.file_access_tokens import create_file_access_token
from packages.core.tasks import media_tasks


@pytest.mark.asyncio
async def test_scoped_video_url_keeps_original_claims_and_expiry(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    token = create_file_access_token(
        entity_id="entity-1",
        rel_path="uploads/chat/1234567890abcdef.mp4",
        user_id="user-1",
        conversation_id="conversation-1",
        source_sha256="a" * 64,
    )
    url = f"https://app.example.test/api/v1/fs/public/{token}/video.mp4"

    def no_remint(**_kwargs):
        pytest.fail("Scoped attachment token must not become a legacy bearer token")

    monkeypatch.setattr("packages.core.services.file_access_tokens.create_file_access_token", no_remint)
    assert media_tasks._entity_rel_path_from_reference(url, "entity-1", str(tmp_path / "entity-1")) is None
    assert await media_tasks._ensure_public_url(url, "entity-1", allow_data_uri=False) == url
    assert await media_tasks._ensure_inline_data_url(url, "entity-1", max_bytes=100) == url


def test_legacy_media_token_still_rehydrates_to_its_original_file(tmp_path):
    path = "uploads/chat/1234567890abcdef.mp4"
    token = create_file_access_token(entity_id="entity-1", rel_path=path)
    url = f"https://app.example.test/api/v1/fs/public/{token}/video.mp4"
    assert media_tasks._entity_rel_path_from_reference(url, "entity-1", str(tmp_path / "entity-1")) == path
