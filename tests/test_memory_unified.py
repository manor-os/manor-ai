"""Unified memory contracts: real isolated storage and local worker doubles."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import packages.core.database as database
from packages.core.memory import chat_extractor, compaction, manager
from packages.core.models.memory import AgentMemory
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services import memory_service
from tests import test_memory_manager_storage as storage_tests

memory_fs = storage_tests.memory_fs
seed_chats = storage_tests.seed_chats


def entry(source, content="Use Chinese in weekly reports"):
    return {"scope": "preference", "layer": "preferences", "title": "Reports", "body": content,
            "source_message_id": source, "confidence": 0.7}


async def scheduled(db, workspace):
    if workspace:
        return await chat_extractor.extract_chat_insights(db, "w_memory")
    return await chat_extractor.extract_entity_chat_insights(db, "e_memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("first", ["schedule", "compact"])
async def test_schedule_and_compact_share_admission_and_receipts(client, memory_fs, monkeypatch, workspace, first):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one"], workspace_id="w_memory" if workspace else None)
    ctx = SimpleNamespace(entity_id="e_memory", user_id="u_one", agent_id=None,
                          workspace_id="w_memory" if workspace else None, conversation_id="c_u_one")
    worker = AsyncMock(return_value={"entries": [entry("m_u_one")]})
    monkeypatch.setattr(compaction, "run_memory_skill", worker)

    async def compact():
        return await compaction.prepare_compaction_memory(ctx, [{"role": "user", "content": "Always write reports in language u_one."}])

    async def schedule():
        async with database.async_session() as db:
            result = await scheduled(db, workspace)
            await db.commit()
            return result

    actions = {"schedule": schedule, "compact": compact}
    await actions[first]()
    await actions["compact" if first == "schedule" else "schedule"]()
    await schedule()
    assert worker.await_count == 1
    async with database.async_session() as db:
        candidate = (await db.scalars(select(AgentLearningCandidate))).one()
        assert candidate.status == "applied" and candidate.user_id == "u_one"
        assert candidate.payload["source_message_id"] == "m_u_one"
        assert candidate.payload["target"] == "my_preferences"
        assert len((await db.scalars(select(RuntimeEvidence))).all()) == 1
        assert not (await db.scalars(select(AgentMemory))).all()
        receipt = (await db.get(Message, "m_u_one")).meta[compaction.RECEIPTS][compaction._receipt_key(ctx)]
        assert receipt["status"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_oldest_first_cursor_covers_more_than_one_batch_with_identical_times(client, memory_fs, monkeypatch, workspace):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one"], workspace_id="w_memory" if workspace else None)
        stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
        for i in range(85):
            db.add(Message(id=f"batch_{i:03}", conversation_id="c_u_one", role="user",
                           content=f"A durable preference number {i}", created_at=stamp))
        await db.commit()
    seen = []

    async def worker(operation, payload, **scope):
        seen.extend(payload["source_message_ids"])
        return {"entries": []}

    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    for _ in range(3):
        async with database.async_session() as db:
            await scheduled(db, workspace)
            await db.commit()
    assert len(seen) == 86 and len(set(seen)) == 86
    assert seen[0] == "m_u_one" and seen[-1] == "batch_084"


@pytest.mark.asyncio
async def test_shared_workspace_authors_and_generated_messages_are_isolated(client, memory_fs, monkeypatch):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one", "u_two"], workspace_id="w_memory")
        conv = await db.get(Conversation, "c_u_one")
        conv.user_id = None  # shared channel has no owner
        msg = await db.get(Message, "m_u_one")
        msg.meta = {"author_user_id": "u_one"}
        for row in (
            Message(id="foreign", role="user", content="Always use another user's preference", meta={"author_user_id": "u_two"}),
            Message(id="assistant", role="assistant", content="I assert an unverified user preference"),
            Message(id="card", role="user", message_kind="proposal", content="Generated proposal is not evidence"),
            Message(id="system", role="user", author_kind="system", content="System-generated message body"),
            Message(id="unknown", role="user", content="Unknown author without durable identity"),
        ):
            row.conversation_id = conv.id
            db.add(row)
        await db.commit()
    seen = []

    async def worker(operation, payload, **scope):
        seen.append((scope["user_id"], payload["source_message_ids"]))
        return {"entries": []}

    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    async with database.async_session() as db:
        result = await scheduled(db, True)
        await db.commit()
    assert result["skipped_invalid"] == 1
    assert {i for _, ids in seen for i in ids} == {"m_u_one", "m_u_two", "foreign"}
    assert all("foreign" not in ids for actor, ids in seen if actor == "u_one")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "paused", "deleted", "revoked", "edited"])
async def test_scheduled_admission_rechecks_changes_while_worker_runs(client, memory_fs, monkeypatch, change):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one"], workspace_id="w_memory")

    async def worker(*args, **kwargs):
        async with database.async_session() as db:
            workspace = await db.get(Workspace, "w_memory")
            if change == "disabled":
                workspace.settings = {"runtime_learning_enabled": False}
            elif change == "paused":
                workspace.status = "paused"
            elif change == "deleted":
                workspace.deleted_at = datetime.now(timezone.utc)
            elif change == "revoked":
                (await db.get(User, "u_one")).status = "disabled"
            else:
                (await db.get(Message, "m_u_one")).content = "A corrected preference supersedes the old text"
            await db.commit()
        return {"entries": [entry("m_u_one")]}

    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    async with database.async_session() as db:
        result = await scheduled(db, True)
        await db.commit()
    assert not result["extracted"]
    async with database.async_session() as db:
        assert not (await db.scalars(select(AgentLearningCandidate))).all()
        assert not (await db.scalars(select(AgentMemory))).all()


@pytest.mark.asyncio
async def test_admission_rechecks_duplicate_written_during_worker(client, memory_fs, monkeypatch):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one"])

    async def worker(*args, **kwargs):
        async with database.async_session() as db:
            await memory_service.add_memory(db, "e_memory", "Use Chinese in weekly reports", user_id="u_one", memory_type="preference")
            await db.commit()
        return {"entries": [entry("m_u_one")]}

    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    async with database.async_session() as db:
        result = await scheduled(db, False)
        await db.commit()
        assert result["extracted"] == 0
        assert not (await db.scalars(select(AgentLearningCandidate))).all()
        assert len((await db.scalars(select(AgentMemory))).all()) == 1


@pytest.mark.asyncio
async def test_prompt_retrieval_exact_null_scope_and_shared_selection(client, memory_fs, monkeypatch):
    async with database.async_session() as db:
        db.add_all([
            AgentMemory(id="one", entity_id="e_memory", content="Relevant " * 1100, memory_type="fact", importance=5),
            AgentMemory(id="two", entity_id="e_memory", content="Irrelevant " * 1000, memory_type="fact", importance=9),
            AgentMemory(id="private", entity_id="e_memory", user_id="u_one", content="PRIVATE", memory_type="fact"),
            AgentMemory(id="agent", entity_id="e_memory", agent_id="a", content="AGENT ONLY", memory_type="fact"),
            AgentMemory(id="workspace", entity_id="e_memory", workspace_id="w_memory", content="WORKSPACE ONLY", memory_type="fact"),
            AgentMemory(id="expired", entity_id="e_memory", content="EXPIRED", memory_type="fact", expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)),
        ])
        await db.commit()
    # A buggy alternate ranker deliberately returns everything. The shared
    # prompt adapter must constrain it before any worker receives record text.
    async def ranker(db, *args, **kwargs):
        return list((await db.scalars(select(AgentMemory))).all())

    monkeypatch.setattr(memory_service, "_relevance_ranked_memories", ranker)
    calls = []

    async def select_records(operation, payload, **scope):
        calls.append((payload, scope))
        assert {r["id"] for r in payload["records"]} == {"one", "two"}
        return {"ids": ["one"]}

    monkeypatch.setattr(manager, "run_memory_skill", select_records)
    async with database.async_session() as db:
        context = await memory_service.get_context_memories(db, "e_memory", query="relevant?", sync_files=False)
    assert "Relevant" in context and "Irrelevant" not in context
    assert not any(word in context for word in ("PRIVATE", "AGENT ONLY", "WORKSPACE ONLY", "EXPIRED"))
    assert len(calls) == 1 and calls[0][1]["user_id"] is None


@pytest.mark.asyncio
async def test_personal_md_sync_cannot_reassign_another_scopes_existing_row(client, memory_fs):
    from packages.core.services.agent_memory_files import agent_memory_root, sync_agent_memory_files
    from pathlib import Path

    async with database.async_session() as db:
        db.add(AgentMemory(id="other", entity_id="e_memory", user_id="u_two", content="Original", memory_type="fact"))
        await db.commit()
        path = Path(agent_memory_root("e_memory", None, user_id="u_one")) / "facts" / "forged.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nid: other\nuser_id: u_one\nmemory_type: fact\n---\nInjected content\n')
        await sync_agent_memory_files(db, entity_id="e_memory", user_id="u_one", exact_scope=True)
        await db.flush()
        row = await db.get(AgentMemory, "other", populate_existing=True)
        assert row.user_id == "u_two" and row.content == "Original"


@pytest.mark.asyncio
async def test_workspace_db_retrieval_uses_common_worker_and_excludes_private_rows(client, memory_fs, monkeypatch):
    from packages.core.memory import service
    from packages.core.services import embedding_service

    async with database.async_session() as db:
        for row in (
            AgentMemory(id="ws_one", workspace_id="w_memory", content="Relevant workspace fact " * 500),
            AgentMemory(id="ws_two", workspace_id="w_memory", content="Irrelevant workspace fact " * 500),
            AgentMemory(id="ws_private", workspace_id="w_memory", user_id="u_one", content="PRIVATE"),
            AgentMemory(id="ws_other", workspace_id="elsewhere", content="OTHER WORKSPACE"),
        ):
            row.entity_id, row.memory_type, row.scope, row.confidence = "e_memory", "fact", "fact", 0.7
            db.add(row)
        await db.commit()
    monkeypatch.setattr(embedding_service, "generate_embedding", AsyncMock(return_value=None))
    seen = []

    async def worker(operation, payload, **scope):
        seen.append(scope)
        assert {r["id"] for r in payload["records"]} == {"ws_one", "ws_two"}
        return {"ids": ["ws_one"]}

    monkeypatch.setattr(manager, "run_memory_skill", worker)
    async with database.async_session() as db:
        result = await service.get_relevant_memory(db, entity_id="e_memory", workspace_id="w_memory", query="fact?")
    assert [r["id"] for r in result] == ["ws_one"]
    assert seen[0]["workspace_id"] == "w_memory" and seen[0]["user_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "archived", "expired", "edited", "scope"])
async def test_db_prompt_rechecks_record_after_selection_worker(client, memory_fs, monkeypatch, change):
    async with database.async_session() as db:
        db.add(AgentMemory(id="one", entity_id="e_memory", user_id="u_one", content="Old fact " * 1100, memory_type="fact"))
        await db.commit()
    monkeypatch.setattr(memory_service, "_relevance_ranked_memories", AsyncMock(return_value=None))

    async def worker(operation, payload, **scope):
        async with database.async_session() as db:
            row = await db.get(AgentMemory, "one")
            if change == "deleted":
                await db.delete(row)
            elif change == "archived":
                row.status = "archived"
            elif change == "expired":
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif change == "edited":
                row.content = "A new, unselected fact"
            else:
                row.user_id = "u_two"
            await db.commit()
        return {"ids": ["one"]}

    monkeypatch.setattr(manager, "run_memory_skill", worker)
    async with database.async_session() as db:
        result = await memory_service.get_context_memories(db, "e_memory", user_id="u_one", query="fact?", sync_files=False)
        assert result == ""


@pytest.mark.asyncio
async def test_extract_api_returns_written_memory_and_rejects_impersonation(client, memory_fs, monkeypatch):
    from tests.test_memories import _auth

    headers = await _auth(client, "unified_extract")
    async with database.async_session() as db:
        user = await db.scalar(select(User).where(User.email == "unified_extract@test.com"))
        entity_id, user_id = user.entity_id, user.id
        db.add(Conversation(id="api_memory_conv", entity_id=entity_id, user_id=user_id))
        db.add(Message(id="api_memory_msg", conversation_id="api_memory_conv", role="user", content="Use Chinese in weekly reports"))
        await db.commit()
    worker = AsyncMock(return_value={"entries": [entry("api_memory_msg")]})
    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    forged = await client.post("/api/v1/memories/extract", headers=headers,
                               json={"conversation_id": "api_memory_conv", "user_id": "another_user"})
    assert forged.status_code == 403 and worker.await_count == 0
    result = await client.post("/api/v1/memories/extract", headers=headers, json={"conversation_id": "api_memory_conv"})
    assert result.status_code == 200, result.text
    candidate = result.json()[0]
    assert candidate["status"] == "applied" and not candidate["metadata"]["review_required"]
    assert candidate["id"] == candidate["metadata"]["learning_candidate_id"]
    async with database.async_session() as db:
        assert not (await db.scalars(select(AgentMemory).where(AgentMemory.entity_id == entity_id))).all()
        assert (await db.get(AgentLearningCandidate, candidate["id"])).user_id == user_id
    # A scheduler replay shares the same receipt, including original evidence.
    async with database.async_session() as db:
        await chat_extractor.extract_entity_chat_insights(db, entity_id)
        await db.commit()
    assert worker.await_count == 1


@pytest.mark.asyncio
async def test_vector_prompt_query_uses_exact_null_scope_in_real_sql(client, memory_fs, monkeypatch):
    from sqlalchemy import text
    from packages.core.services import embedding_service
    from tests.conftest import TEST_EMBEDDING_DIMENSIONS

    async with database.async_session() as db:
        # create_all fixtures omit this optional migration-owned pgvector column.
        await db.execute(text(f"ALTER TABLE agent_memories ADD COLUMN IF NOT EXISTS embedding vector({TEST_EMBEDDING_DIMENSIONS})"))
        for i, scope in enumerate(({}, {"user_id": "u_one"}, {"agent_id": "a"}, {"workspace_id": "w_memory"})):
            db.add(AgentMemory(id=f"vector_{i}", entity_id="e_memory", memory_type="fact", content=f"Scoped fact {i}", **scope))
        await db.commit()
    embedding = AsyncMock(return_value=[1.0] + [0.0] * (TEST_EMBEDDING_DIMENSIONS - 1))
    monkeypatch.setattr(embedding_service, "generate_embedding", embedding)
    async with database.async_session() as db:
        result = await memory_service._relevance_ranked_memories(db, "e_memory", agent_id=None, user_id=None,
                                                               query="fact?", limit=100)
    assert [r.id for r in result] == ["vector_0"]
    assert embedding.await_count == 1
