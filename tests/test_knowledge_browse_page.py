from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from apps.api.routers import documents as routes
from packages.core.services import plan_gate
from packages.core.services.document_service import list_documents as list_raw_documents


@pytest.mark.asyncio
async def test_browse_page_cursor_counts_raw_rows_after_permission_filter(monkeypatch):
    user = SimpleNamespace(id="u1", entity_id="e1", role="owner")
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(
            id=letter, entity_id="e1", name=f"{letter}.md", folder_id=None,
            fs_path=None, source="upload", file_type="md", mime_type="text/markdown",
            metadata_={}, created_at=now - timedelta(seconds=index),
        )
        for index, letter in enumerate("abcd")
    ]
    positions = []

    async def list_documents(_db, _entity_id, **kwargs):
        assert kwargs["count_total"] is False
        positions.append(kwargs["after_key"])
        candidates = rows
        if kwargs["after_key"] is not None:
            value, document_id = kwargs["after_key"]
            candidates = [
                row for row in rows
                if row.created_at < value or (row.created_at == value and row.id < document_id)
            ]
        return candidates[:kwargs["limit"]], 0

    async def readable(_db, candidates, **_kwargs):
        # The first raw row is unavailable, but it still occupies an offset.
        return [candidate for candidate in candidates if candidate.id != "a"]

    async def authorize(_db, *, documents, **_kwargs):
        allowed = [doc for doc in documents if doc.id != "a"]
        return SimpleNamespace(
            documents=allowed,
            access_context=SimpleNamespace(),
        )

    async def folders(_db, _user, **_kwargs):
        return [], {}, SimpleNamespace()

    async def response(_db, document, _user, _access_context, **_kwargs):
        return routes.DocumentResponse(
            id=document.id, entity_id=document.entity_id, name=document.name,
        )

    async def plan(_db, _entity_id, _feature):
        return SimpleNamespace(current=0, limit=100)

    async def blocked_folders(_db, _entity_id):
        return set()

    monkeypatch.setattr(routes, "list_documents", list_documents)
    monkeypatch.setattr(routes, "_filter_readable_local_documents", readable)
    monkeypatch.setattr(routes.ResourcePermissionGate, "authorize_document_batch_read", authorize)
    monkeypatch.setattr(routes, "_visible_document_folders_for_user", folders)
    monkeypatch.setattr(routes, "_workspace_delete_blocked_folder_ids", blocked_folders)
    monkeypatch.setattr(routes, "_doc_resp_for_user", response)
    monkeypatch.setattr(plan_gate, "check", plan)

    def page(cursor):
        return routes.browse_documents_page(
            search=None, folder_id=None, workspace_id=None, scope="all",
            include_generated_assets=True, favorite_only=False,
            file_type_filter=None, sort_by="date", limit=2, cursor=cursor,
            user=user, db=object(),
        )

    first = await page(None)
    assert [item.id for item in first.items] == ["b", "c"]
    assert first.next_cursor is not None
    assert routes._decode_browse_cursor(first.next_cursor, "date") == (rows[2].created_at, "c")
    # A concurrent insert ahead of the cursor cannot shift page two.
    rows.insert(0, SimpleNamespace(**{**vars(rows[0]), "id": "new", "created_at": now + timedelta(seconds=1)}))
    second = await page(first.next_cursor)
    assert [item.id for item in second.items] == ["d"]
    assert second.next_cursor is None
    assert positions == [None, (rows[3].created_at, "c")]


def test_browse_cursor_rejects_malformed_or_wrong_sort():
    with pytest.raises(HTTPException) as malformed:
        routes._decode_browse_cursor("not-a-cursor", "date")
    assert malformed.value.status_code == 422

    cursor = routes._encode_browse_cursor(("report", "doc_1"), "name")
    with pytest.raises(HTTPException) as wrong_sort:
        routes._decode_browse_cursor(cursor, "size")
    assert wrong_sort.value.status_code == 422


@pytest.mark.asyncio
async def test_cached_thumbnail_rechecks_access_after_redis_await(monkeypatch):
    document = SimpleNamespace(id="doc_1", entity_id="e1", name="image.png")
    user = SimpleNamespace(id="u1", entity_id="e1", role="member")
    visible = AsyncMock(side_effect=[document, None])
    monkeypatch.setattr(routes, "get_visible_document", visible)
    monkeypatch.setattr(
        routes,
        "get_cached_document_blob",
        AsyncMock(return_value=SimpleNamespace(data=b"old thumbnail", media_type="image/jpeg")),
    )
    db = SimpleNamespace(rollback=AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await routes.document_thumbnail("doc_1", user=user, db=db)
    assert denied.value.status_code == 404
    assert visible.await_count == 2
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_page_applies_favorites_type_and_keyset_before_limit():
    statements = []

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    class Database:
        async def execute(self, statement):
            statements.append(statement)
            return Rows()

    documents, total = await list_raw_documents(
        Database(), "entity_1",
        favorite_user_id="user_1",
        file_type_filter="md",
        sort_by="name",
        after_key=("m", "doc_1"),
        limit=50,
        count_total=False,
    )
    assert documents == []
    assert total == 0
    assert len(statements) == 1  # no full-table COUNT query
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "favorites" in sql
    assert "documents.name >" in sql
    assert "ORDER BY documents.name ASC, documents.id ASC" in sql
    assert "LIMIT" in sql
