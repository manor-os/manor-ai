from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.ai.runtime import workspace_setup


@pytest.mark.asyncio
async def test_workspace_startup_generation_uses_persisted_creator(monkeypatch):
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalar.return_value = {"created_by_user_id": "installer"}
    factory = MagicMock(return_value=db)
    factory.kw = {"bind": SimpleNamespace(dispose=AsyncMock())}
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: factory)
    complete = AsyncMock(return_value=SimpleNamespace(content="你好", usage={}))
    monkeypatch.setattr(workspace_setup, "runtime_execute_text_completion", complete)

    await workspace_setup.runtime_execute_agent_greeting_completion(
        entity_id="tenant", workspace_id="workspace", agent_name="Agent",
        service_key="support", workspace_name="Support", workspace_kind="team", system_prompt="Help",
    )
    await workspace_setup.runtime_execute_knowledge_starter_document_completion(
        entity_id="tenant", workspace_id="workspace", group_name="Handbook",
        purpose="Guidance", workspace_name="Support", workspace_kind="team", primary_work="Help customers",
    )
    assert all(call.kwargs["user_id"] == "installer" for call in complete.await_args_list)
    query = db.scalar.await_args.args[0]
    assert query.compile().params == {"id_1": "workspace", "entity_id_1": "tenant"}
    assert "deleted_at IS NULL" in str(query)
    assert factory.kw["bind"].dispose.await_count == 2


@pytest.mark.asyncio
async def test_missing_workspace_creator_does_not_choose_another_user(monkeypatch):
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalar.return_value = {}
    factory = MagicMock(return_value=db)
    factory.kw = {"bind": SimpleNamespace(dispose=AsyncMock())}
    monkeypatch.setattr("packages.core.database.create_worker_session", lambda: factory)

    assert await workspace_setup._workspace_creation_user_id("tenant", "workspace") is None
    assert db.scalar.await_count == 1
