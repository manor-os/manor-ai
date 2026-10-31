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
async def test_free_music_search_failure_names_each_catalog(monkeypatch):
    from packages.core import database
    from packages.core.services import open_music_catalog

    monkeypatch.setattr(database, "async_session", lambda: _Session())

    async def actor(_db, *, entity_id, user_id):
        return SimpleNamespace(id=user_id, entity_id=entity_id)

    failures = [
        {"catalog": "openverse", "error": "timed out after 7s"},
        {"catalog": "wikimedia_commons", "error": "Wikimedia Commons HTTP 503"},
    ]

    async def search(*_args, **_kwargs):
        raise open_music_catalog.OpenMusicSearchUnavailable(failures)

    monkeypatch.setattr(media_tools, "_free_music_actor", actor)
    monkeypatch.setattr(open_music_catalog, "search_open_music", search)
    result = json.loads(await media_tools._search_free_music_handler(
        entity_id="entity-1", user_id="user-1", query="warm piano",
    ))
    assert result["status"] == "error"
    assert result["code"] == "free_music_search_failed"
    assert result["catalog_failures"] == failures


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "expected"), [
    # 2026-10-20 eval: an Openverse timeout was reported as "denied".
    (("catalog", True), {"code": "free_music_import_unavailable", "retryable": True}),
    (("download", True), {"code": "free_music_import_unavailable", "retryable": True}),
    (("catalog", False), {"code": "free_music_import_failed"}),
    (("license", False), {"code": "free_music_import_denied"}),
], ids=["catalog-timeout", "download-5xx", "item-unusable", "license-refused"])
async def test_free_music_import_separates_unavailability_from_refusal(monkeypatch, failure, expected):
    from fastapi import HTTPException

    from apps.api.routers import media
    from packages.core import database
    from packages.core.services import open_music_catalog

    monkeypatch.setattr(database, "async_session", lambda: _Session())

    async def actor(_db, *, entity_id, user_id):
        return SimpleNamespace(id=user_id, entity_id=entity_id)

    kind, retryable = failure

    async def imported(*, body, user, db):
        if kind == "license":
            raise HTTPException(422, "Track license is not approved for commercial video use")
        error_type = (
            open_music_catalog.OpenMusicDownloadError if kind == "download"
            else open_music_catalog.OpenMusicCatalogUnavailable
        )
        raise media._free_music_upstream_error(error_type("upstream failed", retryable=retryable))

    monkeypatch.setattr(media_tools, "_free_music_actor", actor)
    monkeypatch.setattr(media, "import_free_music", imported)
    result = json.loads(await media_tools._import_free_music_handler(
        entity_id="entity-1", user_id="user-1",
        track_id="track-1", source_document_id="canvas-doc-1",
    ))
    assert result["status"] == "error"
    assert {key: result.get(key) for key in expected} == expected
    assert result.get("retryable", False) is retryable


CANVAS_PATH = "Workspaces/_by_id/ws-folder/workflow/run-1/Videos/city-pulse/CANVAS.md"


async def _canvas_import(db_session, monkeypatch, tmp_path):
    """A later Flow step holds only the canvas_path the canvas step returned."""
    from apps.api.routers import media
    from packages.core import database
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import Entity

    entity = Entity(id=generate_ulid(), name="Free music by path")
    db_session.add(entity)
    await db_session.flush()
    canvas = Document(entity_id=entity.id, name="CANVAS.md", fs_path=CANVAS_PATH, file_type="md",
                      source="ai_generated", visibility="workspace")
    db_session.add(canvas)
    await db_session.flush()
    (tmp_path / CANVAS_PATH).parent.mkdir(parents=True)
    (tmp_path / CANVAS_PATH).write_text("# CANVAS", encoding="utf-8")

    class SharedSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    async def actor(_db, *, entity_id, user_id):
        return SimpleNamespace(id=user_id, entity_id=entity_id)

    async def base_dir(**_kwargs):
        return "Workspaces/_by_id/ws-folder/workflow/run-1"  # the run's archive, as in a Flow step

    imported_from = []

    async def imported(*, body, user, db):
        imported_from.append(body.source_document_id)
        return SimpleNamespace(
            document={"id": "music-doc-1", "fs_path": "music/bed.mp3"},
            track=SimpleNamespace(model_dump=lambda: {"id": "track-1"}),
        )

    monkeypatch.setattr(database, "async_session", SharedSession)
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity: str(tmp_path))
    monkeypatch.setattr(media_tools, "_workspace_media_base_dir", base_dir)
    monkeypatch.setattr(media_tools, "_free_music_actor", actor)
    monkeypatch.setattr(media, "import_free_music", imported)

    async def call(**kwargs):
        return json.loads(await media_tools._import_free_music_handler(
            entity_id=entity.id, user_id="user-1", track_id="track-1", workspace_id="ws-1", **kwargs))

    return canvas, call, imported_from


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    CANVAS_PATH,                      # canvas_path exactly as the canvas step returned it
    "Videos/city-pulse/CANVAS.md",    # the same file named within the run's Workspace scope
    "Knowledge/" + CANVAS_PATH,       # the UI's Knowledge-prefixed form
], ids=["canvas-path", "scoped-relative", "knowledge-prefixed"])
async def test_free_music_imports_beside_a_canvas_named_by_path(db_session, monkeypatch, tmp_path, source):
    # 2026-10-14 eval: a later Flow step had only canvas_path, the tool took only a Document ID,
    # and a name search failed to find it, so no library music was imported.
    canvas, call, imported_from = await _canvas_import(db_session, monkeypatch, tmp_path)

    result = await call(source_path=source)

    assert result["status"] == "completed"
    assert imported_from == [canvas.id]


@pytest.mark.asyncio
async def test_free_music_source_path_refuses_unknown_and_conflicting_sources(db_session, monkeypatch, tmp_path):
    canvas, call, imported_from = await _canvas_import(db_session, monkeypatch, tmp_path)

    assert (await call(source_path=f"/viewer/{canvas.id}"))["status"] == "completed"
    assert (await call(source_path="Videos/other/CANVAS.md"))["code"] == "source_not_found"
    assert (await call(source_path=CANVAS_PATH, source_document_id="another-doc"))["code"] == "identity_conflict"
    assert (await call())["code"] == "invalid_request"
    assert imported_from == [canvas.id]
