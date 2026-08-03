from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_runtime_list_documents_maps_query_to_name_search(monkeypatch):
    from packages.core.ai.runtime import document_actions, manor_actions
    from packages.core.services import document_access

    calls: list[dict[str, object]] = []

    async def fake_get_cached_read(*_args, **_kwargs):
        return None

    async def fake_set_cached_read(*_args, **_kwargs):
        return None

    async def fake_list_visible_documents(_db, _entity_id, **kwargs):
        calls.append(dict(kwargs))
        return [], 0

    monkeypatch.setattr(document_actions, "_runtime_get_cached_document_action", fake_get_cached_read)
    monkeypatch.setattr(document_actions, "_runtime_set_cached_document_action", fake_set_cached_read)
    monkeypatch.setattr(document_access, "list_visible_documents", fake_list_visible_documents)

    payload = json.loads(
        await manor_actions.runtime_manor_list_documents(
            object(),
            entity_id="ent_docs",
            user_id="user_docs",
            params={"query": "quarterly report", "limit": 5},
        )
    )

    assert payload["total"] == 0
    assert calls[0]["name_search"] == "quarterly report"
    assert "query" not in calls[0]


@pytest.mark.asyncio
async def test_runtime_list_documents_drops_unsupported_path_filters(monkeypatch):
    from packages.core.ai.runtime import document_actions, manor_actions
    from packages.core.services import document_access

    calls: list[dict[str, object]] = []

    async def fake_get_cached_read(*_args, **_kwargs):
        return None

    async def fake_set_cached_read(*_args, **_kwargs):
        return None

    async def fake_list_visible_documents(_db, _entity_id, **kwargs):
        calls.append(dict(kwargs))
        return [], 0

    monkeypatch.setattr(document_actions, "_runtime_get_cached_document_action", fake_get_cached_read)
    monkeypatch.setattr(document_actions, "_runtime_set_cached_document_action", fake_set_cached_read)
    monkeypatch.setattr(document_access, "list_visible_documents", fake_list_visible_documents)

    payload = json.loads(
        await manor_actions.runtime_manor_list_documents(
            object(),
            entity_id="ent_docs",
            user_id="user_docs",
            params={
                "folder_path": "Knowledge/demo/captures",
                "path": "Knowledge/demo/audio/narration.wav",
                "limit": 5,
            },
        )
    )

    assert payload["total"] == 0
    assert "folder_path" not in calls[0]
    assert "path" not in calls[0]
    assert calls[0]["limit"] == 5


@pytest.mark.asyncio
async def test_runtime_manor_document_actions_delegate_to_shared_document_actions(monkeypatch):
    """manor(action=list_documents/search_documents) must share one implementation
    with the standalone document_tools.py tools instead of maintaining a second copy."""
    from packages.core.ai.runtime import document_actions, manor_actions

    delegate_calls: list[dict[str, object]] = []

    async def fake_list_documents_action(**kwargs):
        delegate_calls.append({"handler": "list", **kwargs})
        return "delegated-list"

    async def fake_search_documents_action(**kwargs):
        delegate_calls.append({"handler": "search", **kwargs})
        return "delegated-search"

    monkeypatch.setattr(document_actions, "runtime_list_documents_action", fake_list_documents_action)
    monkeypatch.setattr(document_actions, "runtime_search_documents_action", fake_search_documents_action)

    db = object()
    assert await manor_actions.runtime_manor_list_documents(
        db, entity_id="ent_docs", user_id="user_docs", workspace_id="ws_1", params={"limit": 5},
    ) == "delegated-list"
    assert await manor_actions.runtime_manor_search_documents(
        db, entity_id="ent_docs", user_id="user_docs", workspace_id="ws_1", params={"query": "brief"},
    ) == "delegated-search"

    assert delegate_calls == [
        {
            "handler": "list",
            "entity_id": "ent_docs",
            "user_id": "user_docs",
            "workspace_id": "ws_1",
            "params": {"limit": 5},
            "db": db,
        },
        {
            "handler": "search",
            "entity_id": "ent_docs",
            "user_id": "user_docs",
            "workspace_id": "ws_1",
            "params": {"query": "brief"},
            "db": db,
        },
    ]
