"""Real PostgreSQL/filesystem admission tests. No external model requests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import packages.core.database as database
from packages.core.memory import chat_extractor, service
from packages.core.models.memory import AgentMemory
from packages.core.services import memory_service


@pytest.fixture
def memory_fs(tmp_path, monkeypatch):
    from packages.core.config import get_settings
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.memory.repo import ensure_workspace_memory_dirs
    from packages.core.memory import sync

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    provision_entity_filesystem("e_memory", "Memory Test")
    ensure_workspace_memory_dirs("e_memory", "w_memory")
    embed = AsyncMock()
    monkeypatch.setattr(sync, "_embed_into_row", embed)
    return tmp_path, embed


@pytest.mark.asyncio
async def test_workspace_duplicate_skips_md_rewrite_and_embedding(client, memory_fs):
    root, embed = memory_fs
    kwargs = dict(entity_id="e_memory", workspace_id="w_memory", scope="fact", title="预算", body="预算 100")
    async with database.async_session() as db:
        first = await service.record_memory(db, **kwargs)
        await db.commit()
        original_time = Path(first.file_path).stat().st_mtime_ns
        again = await service.record_memory(db, **{**kwargs, "title": "同义标题", "body": "预算   100"})
        await db.commit()
        assert again.frontmatter.id == first.frontmatter.id
        assert Path(first.file_path).stat().st_mtime_ns == original_time
        assert embed.await_count == 1
        second = await service.record_memory(db, **{**kwargs, "body": "预算 200"})
        await db.commit()
        assert first.file_path != second.file_path
        assert Path(first.file_path).is_file() and Path(second.file_path).is_file()
        rows = (await db.execute(select(AgentMemory).where(AgentMemory.entity_id == "e_memory"))).scalars().all()
        assert len(rows) == 2
        from packages.core.memory.canonical import read_workspace_memory_file

        canonical = read_workspace_memory_file("e_memory", "w_memory", "MEMORY.md")
        assert canonical.count(f"runtime-learning:{first.frontmatter.id}") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_two_independent_transactions_same_content_one_row(client, memory_fs, workspace):
    async def writer():
        async with database.async_session() as db:
            if workspace:
                item = await service.record_memory(
                    db,
                    entity_id="e_memory",
                    workspace_id="w_memory",
                    scope="preference",
                    title="Reports",
                    body="weekly reports",
                )
                item_id = item.frontmatter.id
            else:
                item = await memory_service.add_memory(db, "e_memory", "weekly reports", user_id="u_one")
                item_id = item.id
            await db.commit()
            return item_id

    ids = await asyncio.wait_for(asyncio.gather(writer(), writer()), timeout=10)
    assert ids[0] == ids[1]
    async with database.async_session() as db:
        rows = (await db.execute(select(AgentMemory).where(AgentMemory.entity_id == "e_memory"))).scalars().all()
        assert len(rows) == 1
    if workspace:
        assert memory_fs[1].await_count == 1


@pytest.mark.asyncio
async def test_duplicate_scope_status_expiry_and_stale_fingerprint(client, memory_fs):
    async with database.async_session() as db:
        original = await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_one")
        assert (await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_one")).id == original.id
        other = await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_two")
        assert other.id != original.id
        expiring = await memory_service.add_memory(
            db, "e_memory", "A fact", user_id="u_one", expires_at=datetime.now(timezone.utc) + timedelta(days=1)
        )
        assert expiring.id != original.id
        original.status = "archived"
        await db.flush()
        replacement = await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_one")
        assert replacement.id != original.id
        replacement.content = "Changed externally"  # old fingerprint must not hide a valid write
        await db.flush()
        fresh = await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_one")
        assert fresh.id != replacement.id
        expired = await memory_service.add_memory(
            db, "e_memory", "Expired fact", user_id="u_one", expires_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
        from packages.core.memory.dedup import existing_memories

        snapshot = await existing_memories(db, entity_id="e_memory", user_id="u_one")
        assert expired.id not in {item["id"] for item in snapshot}
        assert original.id not in {item["id"] for item in snapshot}
        assert (await memory_service.add_memory(db, "e_memory", "A fact", user_id="u_one")).id == fresh.id
        await db.commit()


@pytest.mark.asyncio
async def test_learning_candidates_share_canonical_memory_identity(client, memory_fs):
    from packages.core.services.runtime_learning import _apply_workspace_memory_candidate

    async with database.async_session() as db:
        ids = []
        for candidate_id in ("candidate_one", "candidate_two"):
            candidate = SimpleNamespace(
                id=candidate_id, workspace_id="w_memory", title="Lesson", candidate_type="memory", confidence=0.7
            )
            result = await _apply_workspace_memory_candidate(
                db, candidate, entity_id="e_memory", content="必须先校验输入", memory_type="learning"
            )
            ids.append(result["memory_id"])
            assert result["filename"] == "LEARNINGS.md"
        await db.commit()
        assert ids[0] == ids[1]
        from packages.core.memory.canonical import read_workspace_memory_file

        content = read_workspace_memory_file("e_memory", "w_memory", "LEARNINGS.md")
        assert content.count(f"runtime-learning:{ids[0]}") == 1


async def seed_chats(db, users, *, workspace_id=None):
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace

    db.add(Entity(id="e_memory", name="Test"))
    if workspace_id:
        db.add(Workspace(id=workspace_id, entity_id="e_memory", name="Memory", status="active"))
    for index, user in enumerate(users):
        db.add(User(id=user, entity_id="e_memory", email=f"{user}@test.invalid", password_hash="test", role="owner"))
        db.add(Conversation(id=f"c_{user}", entity_id="e_memory", user_id=user, workspace_id=workspace_id))
        db.add(
            Message(
                id=f"m_{user}",
                conversation_id=f"c_{user}",
                role="user",
                author_kind="user",
                content=f"Always write reports in language {user}.",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=10 - index),
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_entity_extraction_isolates_users_and_passes_existing(client, memory_fs, monkeypatch):
    async with database.async_session() as db:
        await seed_chats(db, ["u_one", "u_two"])
        await memory_service.add_memory(db, "e_memory", "u_one only", user_id="u_one")
        await db.commit()
    calls = []

    async def extract(operation, payload, **kwargs):
        calls.append((payload, kwargs))
        return {"entries": []}

    from packages.core.memory import compaction
    monkeypatch.setattr(compaction, "run_memory_skill", extract)
    async with database.async_session() as db:
        await chat_extractor.extract_entity_chat_insights(db, "e_memory")
        await db.commit()
    assert len(calls) == 2
    by_user = {kwargs["user_id"]: (payload, kwargs) for payload, kwargs in calls}
    assert "m_u_two" not in str(by_user["u_one"][0])
    assert "m_u_one" not in str(by_user["u_two"][0])
    assert by_user["u_one"][0]["existing"][0]["content"] == "u_one only"
    assert by_user["u_two"][0]["existing"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [False, True])
async def test_workspace_worker_failure_or_forged_source_keeps_bookmark(client, memory_fs, monkeypatch, forged):
    from packages.core.models.workspace import Workspace

    async with database.async_session() as db:
        await seed_chats(db, ["u_one"], workspace_id="w_memory")

    async def completion(operation, payload, **kwargs):
        assert payload["source_message_ids"] == ["m_u_one"]
        if not forged:
            raise TimeoutError("worker timeout")
        return {"entries": [{"source_message_id": "forged"}]}

    from packages.core.memory import compaction
    monkeypatch.setattr(compaction, "run_memory_skill", completion)
    async with database.async_session() as db:
        with pytest.raises((TimeoutError, ValueError)):
            await chat_extractor.extract_chat_insights(db, "w_memory")
        await db.rollback()
        workspace = await db.get(Workspace, "w_memory")
        assert not workspace.settings.get(chat_extractor.LAST_EXTRACT_KEY)
        assert not (await db.execute(select(AgentMemory).where(AgentMemory.entity_id == "e_memory"))).scalars().all()
