from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services import knowledge_hot_cache


def _document(**overrides):
    values = {
        "id": "doc_1",
        "entity_id": "ent_1",
        "name": "report.md",
        "fs_path": "docs/report.md",
        "file_url": None,
        "file_size": 5,
        "mime_type": "text/markdown",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_document_hot_cache_key_is_versioned_and_hides_paths():
    original = _document()
    changed = _document(updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))

    original_key = knowledge_hot_cache.document_hot_cache_key(original, "content")
    changed_key = knowledge_hot_cache.document_hot_cache_key(changed, "content")

    assert original_key != changed_key
    assert "docs/report.md" not in original_key
    assert "ent_1" not in original_key


@pytest.mark.asyncio
async def test_text_cache_hit_uses_sliding_ttl(monkeypatch):
    get = AsyncMock(return_value={"type": "text", "content": "hello", "size": 5})
    touch = AsyncMock(return_value=True)
    monkeypatch.setattr(knowledge_hot_cache.cache, "get", get)
    monkeypatch.setattr(knowledge_hot_cache.cache, "touch", touch)

    assert await knowledge_hot_cache.get_cached_document_text(_document()) == "hello"
    touch.assert_awaited_once()


@pytest.mark.asyncio
async def test_blob_cache_round_trip_and_size_cap(monkeypatch):
    stored: dict = {}

    async def fake_set(key, value, ttl):
        stored.update({"key": key, "value": value, "ttl": ttl})
        return True

    monkeypatch.setattr(knowledge_hot_cache.cache, "set", fake_set)
    assert await knowledge_hot_cache.cache_document_blob(
        _document(),
        "thumbnail",
        b"jpeg",
        media_type="image/jpeg",
    ) is True

    monkeypatch.setattr(
        knowledge_hot_cache.cache,
        "get",
        AsyncMock(return_value=stored["value"]),
    )
    monkeypatch.setattr(knowledge_hot_cache.cache, "touch", AsyncMock(return_value=True))
    cached = await knowledge_hot_cache.get_cached_document_blob(_document(), "thumbnail")
    assert cached is not None
    assert cached.data == b"jpeg"
    assert cached.media_type == "image/jpeg"

    monkeypatch.setattr(knowledge_hot_cache, "KNOWLEDGE_CACHE_MAX_ENTRY_BYTES", 2)
    assert await knowledge_hot_cache.cache_document_blob(
        _document(),
        "thumbnail",
        b"too large",
        media_type="image/jpeg",
    ) is False
