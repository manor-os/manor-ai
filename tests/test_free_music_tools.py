"""The video Skill's catalog fallback uses Manor's existing licensed importer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.services.chat_artifacts import chat_attachments_from_tool_results
from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
from packages.core.ai.tools import media_tools


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def test_free_music_import_stays_under_file_create_governance():
    assert classify_runtime_tool_action("search_free_music", {"query": "ambient"}) is None
    action = classify_runtime_tool_action(
        "import_free_music", {"track_id": "track-1", "source_document_id": "video-1"}
    )
    assert action is not None
    assert action.action_key == "workspace.file.create"


@pytest.mark.asyncio
async def test_free_music_tools_use_current_actor_and_api_import_contract(monkeypatch):
    from apps.api.routers import media
    from packages.core import database
    from packages.core.services import open_music_catalog

    assert runtime_tool_schema("search_free_music") is not None
    assert "CANVAS.md" in json.dumps(runtime_tool_schema("import_free_music"))

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    actor_calls = []

    async def actor(_db, *, entity_id, user_id):
        actor_calls.append((entity_id, user_id))
        return SimpleNamespace(id=user_id, entity_id=entity_id)

    monkeypatch.setattr(media_tools, "_free_music_actor", actor)

    async def search(query, *, page, page_size):
        assert (query, page, page_size) == ("warm piano", 1, 18)
        return {"items": [{"id": "track-1", "license": "cc0"}], "page": 1, "page_count": 1, "total": 1}

    monkeypatch.setattr(open_music_catalog, "search_open_music", search)
    searched = json.loads(await media_tools._search_free_music_handler(
        entity_id="entity-1", user_id="user-1", query="warm piano",
    ))
    assert searched["items"][0]["id"] == "track-1"

    async def imported(*, body, user, db):
        assert body.track_id == "track-1"
        assert body.source_document_id == "canvas-doc-1"
        assert user.id == "user-1"
        assert db is not None
        return SimpleNamespace(
            document={"id": "music-doc-1", "fs_path": "music/bed.mp3"},
            track=SimpleNamespace(model_dump=lambda: {"id": "track-1", "license": "cc0"}),
        )

    monkeypatch.setattr(media, "import_free_music", imported)
    result = json.loads(await media_tools._import_free_music_handler(
        entity_id="entity-1", user_id="user-1",
        track_id="track-1", source_document_id="canvas-doc-1",
    ))
    assert result["status"] == "completed"
    assert result["document"]["id"] == "music-doc-1"
    assert result["document"]["document_id"] == "music-doc-1"
    assert chat_attachments_from_tool_results([
        {"name": "import_free_music", "raw_result": json.dumps(result)}
    ])[0]["open_url"] == "/viewer/music-doc-1"
    assert actor_calls == [("entity-1", "user-1"), ("entity-1", "user-1")]


@pytest.mark.asyncio
async def test_free_music_import_reports_knowledge_quota_block(monkeypatch):
    from apps.api.routers import media
    from packages.core import database
    from packages.core.services.document_service import StorageLimitExceeded

    monkeypatch.setattr(database, "async_session", lambda: _Session())

    async def actor(_db, *, entity_id, user_id):
        return SimpleNamespace(id=user_id, entity_id=entity_id)

    async def imported(*, body, user, db):
        raise StorageLimitExceeded("Knowledge full", plan="free", limit=100, current=100)

    monkeypatch.setattr(media_tools, "_free_music_actor", actor)
    monkeypatch.setattr(media, "import_free_music", imported)
    result = json.loads(await media_tools._import_free_music_handler(
        entity_id="entity-1", user_id="user-1",
        track_id="track-1", source_document_id="canvas-doc-1",
    ))
    assert result == {
        "status": "blocked", "code": "knowledge_storage_limit",
        "error": "Knowledge full", "plan": "free", "limit": 100, "current": 100,
    }
