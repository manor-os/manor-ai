"""Regression tests for the chat compression / chat memory design fixes.

Covers:
* rolling conversation summaries merge the prior summary (not overwrite)
  and run on their own session;
* CJK-aware token estimation;
* relevance-ranked chat memory injection falls back to importance;
* entity-level (workspace-less) chats get insight extraction.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import packages.core.database as db_module
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity


# ── Rolling summary ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_conversation_summary_merges_prior_summary(
    client, monkeypatch
):
    from packages.core.services import conversation_history

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_sum_merge", name="Summary merge"))
        db.add(Conversation(
            id="conv_sum_merge",
            entity_id="ent_sum_merge",
            summary="User is preparing for interviews.",
        ))
        await db.commit()

    captured: dict = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="merged summary text")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        fake_completion,
    )

    snapshots = [
        SimpleNamespace(role="user", content="talked about option pricing"),
        SimpleNamespace(role="assistant", content="explained Black-Scholes"),
    ]
    # No db argument: the task owns its session (it outlives the request).
    await conversation_history.update_conversation_summary(
        "conv_sum_merge", snapshots
    )

    assert captured["prior_summary"] == "User is preparing for interviews."
    assert "option pricing" in captured["text_block"]

    async with db_module.async_session() as db:
        conv = await db.get(Conversation, "conv_sum_merge")
        assert conv.summary == "merged summary text"


def test_summary_prompt_includes_prior_summary():
    from packages.core.ai.runtime.memory import runtime_conversation_summary_prompt

    merged = runtime_conversation_summary_prompt(
        "USER: new message", prior_summary="old facts"
    )
    assert "EXISTING SUMMARY:\nold facts" in merged
    assert "USER: new message" in merged

    fresh = runtime_conversation_summary_prompt("USER: new message")
    assert "EXISTING SUMMARY" not in fresh


def test_summary_text_keeps_the_start_of_a_bounded_page():
    from packages.core.ai.runtime.memory import runtime_conversation_summary_text

    messages = [
        SimpleNamespace(role="user", content=f"fact {index}")
        for index in range(40)
    ]

    text = runtime_conversation_summary_text(messages, max_messages=30)

    assert "fact 0" in text
    assert "fact 29" in text
    assert "fact 30" not in text


def test_summary_text_keeps_a_late_fact_in_an_ordinary_long_message():
    from packages.core.ai.runtime.memory import runtime_conversation_summary_text

    text = runtime_conversation_summary_text([
        SimpleNamespace(role="user", content=("x" * 700) + " durable late fact"),
    ])

    assert "durable late fact" in text


@pytest.mark.asyncio
async def test_history_always_includes_stored_summary_within_budget(client):
    from packages.core.services.conversation_history import load_conversation_history

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_always", name="Summary always"))
        db.add(Conversation(
            id="conv_summary_always",
            entity_id="ent_summary_always",
            summary="Earlier decision: ship the API first.",
        ))
        db.add(Message(
            id="msg_summary_always",
            conversation_id="conv_summary_always",
            role="user",
            content="What remains?",
        ))
        await db.commit()

    async with db_module.async_session() as db:
        history = await load_conversation_history(
            db, "conv_summary_always", token_budget=1_000
        )

    assert history[0]["role"] == "system"
    assert "ship the API first" in history[0]["content"]
    assert history[1]["content"] == "What remains?"


@pytest.mark.asyncio
async def test_history_includes_stored_summary_without_message_rows(client):
    from packages.core.services.conversation_history import load_conversation_history

    async with db_module.async_session() as db:
        db.add(Conversation(
            id="conv_summary_empty",
            entity_id="ent_summary_empty",
            summary="Persisted context remains useful.",
        ))
        await db.commit()

    async with db_module.async_session() as db:
        history = await load_conversation_history(db, "conv_summary_empty")

    assert history == [{
        "role": "system",
        "content": "[Earlier conversation summary]\nPersisted context remains useful.",
    }]


@pytest.mark.asyncio
async def test_background_summary_advances_durable_coverage(client, monkeypatch):
    from packages.core.services import conversation_history

    first = datetime.now(timezone.utc) - timedelta(minutes=2)
    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_coverage", name="Summary coverage"))
        db.add(Conversation(
            id="conv_summary_coverage",
            entity_id="ent_summary_coverage",
        ))
        db.add_all([
            Message(
                id="msg_summary_coverage_1",
                conversation_id="conv_summary_coverage",
                role="user",
                content="First durable fact.",
                created_at=first,
            ),
            Message(
                id="msg_summary_coverage_2",
                conversation_id="conv_summary_coverage",
                role="assistant",
                content="Second durable fact.",
                created_at=first + timedelta(seconds=1),
            ),
        ])
        await db.commit()

    async def completion(**_kwargs):
        return SimpleNamespace(content="Durable summary.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_coverage")

    async with db_module.async_session() as db:
        conversation = await db.get(Conversation, "conv_summary_coverage")

    assert conversation.summary == "Durable summary."
    assert conversation.summary_covered_message_id == "msg_summary_coverage_2"
    assert conversation.summary_version == 1


@pytest.mark.asyncio
async def test_deleted_entity_prevents_summary_generation(client, monkeypatch):
    from packages.core.services import conversation_history

    async with db_module.async_session() as db:
        db.add(Entity(
            id="ent_summary_deleted",
            name="Deleted summary entity",
            deleted_at=datetime.now(timezone.utc),
        ))
        db.add(Conversation(
            id="conv_summary_deleted",
            entity_id="ent_summary_deleted",
        ))
        db.add(Message(
            id="msg_summary_deleted",
            conversation_id="conv_summary_deleted",
            role="user",
            content="Do not bill this.",
        ))
        await db.commit()

    async def completion(**_kwargs):
        raise AssertionError("deleted entity must not call the model")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_deleted")


@pytest.mark.asyncio
async def test_entity_deleted_during_summary_rejects_publication(client, monkeypatch):
    from packages.core.services import conversation_history

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_delete_race", name="Delete race entity"))
        db.add(Conversation(
            id="conv_summary_delete_race",
            entity_id="ent_summary_delete_race",
        ))
        db.add(Message(
            id="msg_summary_delete_race",
            conversation_id="conv_summary_delete_race",
            role="user",
            content="Race with deletion.",
        ))
        await db.commit()

    async def completion(**_kwargs):
        async with db_module.async_session() as db:
            entity = await db.get(Entity, "ent_summary_delete_race")
            entity.deleted_at = datetime.now(timezone.utc)
            await db.commit()
        return SimpleNamespace(content="Must not publish.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_delete_race")

    async with db_module.async_session() as db:
        conversation = await db.get(Conversation, "conv_summary_delete_race")

    assert conversation.summary is None


@pytest.mark.asyncio
async def test_background_summary_starts_at_the_oldest_uncovered_page(client, monkeypatch):
    from packages.core.services import conversation_history

    first = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_oldest", name="Summary oldest"))
        db.add(Conversation(id="conv_summary_oldest", entity_id="ent_summary_oldest"))
        db.add_all([
            Message(
                id=f"msg_summary_oldest_{index:02}",
                conversation_id="conv_summary_oldest",
                role="user",
                content=f"fact from page {index}",
                created_at=first + timedelta(seconds=index),
            )
            for index in range(35)
        ])
        await db.commit()

    captured = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="Oldest page summary.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_oldest")

    assert "fact from page 0" in captured["text_block"]
    assert "fact from page 29" in captured["text_block"]
    assert "fact from page 30" not in captured["text_block"]


@pytest.mark.asyncio
async def test_empty_summary_page_advances_to_the_next_fact_page(client, monkeypatch):
    from packages.core.services import conversation_history

    first = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_empty_page", name="Empty summary page"))
        db.add(Conversation(
            id="conv_summary_empty_page",
            entity_id="ent_summary_empty_page",
        ))
        db.add_all([
            Message(
                id=f"msg_summary_empty_{index:02}",
                conversation_id="conv_summary_empty_page",
                role="assistant",
                content="",
                created_at=first + timedelta(seconds=index),
            )
            for index in range(30)
        ])
        db.add(Message(
            id="msg_summary_empty_fact",
            conversation_id="conv_summary_empty_page",
            role="user",
            content="Reach this later fact.",
            created_at=first + timedelta(seconds=30),
        ))
        await db.commit()

    captured = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="Later fact summary.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_empty_page")
    await conversation_history.update_conversation_summary("conv_summary_empty_page")

    assert "Reach this later fact" in captured["text_block"]


@pytest.mark.asyncio
async def test_background_summary_stops_before_a_running_assistant(client, monkeypatch):
    from packages.core.services import conversation_history

    first = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_barrier", name="Summary barrier"))
        db.add(Conversation(id="conv_summary_barrier", entity_id="ent_summary_barrier"))
        db.add_all([
            Message(
                id="msg_summary_stable",
                conversation_id="conv_summary_barrier",
                role="user",
                content="Stable fact.",
                created_at=first,
            ),
            Message(
                id="msg_summary_running",
                conversation_id="conv_summary_barrier",
                role="assistant",
                content="Incomplete response.",
                meta={"stream_status": "streaming"},
                created_at=first + timedelta(seconds=1),
            ),
            Message(
                id="msg_summary_after_barrier",
                conversation_id="conv_summary_barrier",
                role="user",
                content="Must stay uncovered.",
                created_at=first + timedelta(seconds=2),
            ),
        ])
        await db.commit()

    async def completion(**_kwargs):
        return SimpleNamespace(content="Stable summary.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_barrier")

    async with db_module.async_session() as db:
        conversation = await db.get(Conversation, "conv_summary_barrier")

    assert conversation.summary_covered_message_id == "msg_summary_stable"


@pytest.mark.asyncio
async def test_summary_cursor_stops_before_a_row_that_exceeds_input_budget(client, monkeypatch):
    from packages.core.services import conversation_history

    first = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_budget", name="Summary budget"))
        db.add(Conversation(id="conv_summary_budget", entity_id="ent_summary_budget"))
        db.add_all([
            Message(
                id="msg_summary_budget_1",
                conversation_id="conv_summary_budget",
                role="user",
                content="complete",
                created_at=first,
            ),
            Message(
                id="msg_summary_budget_2",
                conversation_id="conv_summary_budget",
                role="user",
                content="must remain uncovered",
                created_at=first + timedelta(seconds=1),
            ),
        ])
        await db.commit()

    monkeypatch.setattr(conversation_history, "SUMMARY_INPUT_MAX_CHARS", 10)

    async def completion(**_kwargs):
        return SimpleNamespace(content="One complete row only.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await conversation_history.update_conversation_summary("conv_summary_budget")

    async with db_module.async_session() as db:
        conversation = await db.get(Conversation, "conv_summary_budget")

    assert conversation.summary_covered_message_id == "msg_summary_budget_1"


@pytest.mark.asyncio
async def test_background_summary_rejects_stale_concurrent_publication(client, monkeypatch):
    from packages.core.services import conversation_history

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_summary_cas", name="Summary CAS"))
        db.add(Conversation(id="conv_summary_cas", entity_id="ent_summary_cas"))
        db.add(Message(
            id="msg_summary_cas",
            conversation_id="conv_summary_cas",
            role="user",
            content="One stable fact.",
        ))
        await db.commit()

    arrived = 0
    both_started = asyncio.Event()

    async def completion(**_kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_started.set()
        await both_started.wait()
        return SimpleNamespace(content=f"Summary writer {arrived}.")

    monkeypatch.setattr(
        conversation_history,
        "runtime_execute_conversation_summary_completion",
        completion,
    )
    await asyncio.gather(
        conversation_history.update_conversation_summary("conv_summary_cas"),
        conversation_history.update_conversation_summary("conv_summary_cas"),
    )

    async with db_module.async_session() as db:
        conversation = await db.get(Conversation, "conv_summary_cas")

    assert conversation.summary_version == 1
    assert conversation.summary_covered_message_id == "msg_summary_cas"


# ── CJK-aware token estimation ────────────────────────────────────────


def test_token_estimate_weights_cjk_higher_than_ascii():
    from packages.core.ai.runtime.token_estimate import (
        runtime_estimate_tokens_for_text,
    )

    ascii_text = "a" * 100
    cjk_text = "按" * 100
    assert runtime_estimate_tokens_for_text(ascii_text) == 25
    # 100 CJK chars ≈ 58 tokens — the old flat chars//4 said 25.
    assert runtime_estimate_tokens_for_text(cjk_text) >= 55
    assert runtime_estimate_tokens_for_text("") == 0
    assert runtime_estimate_tokens_for_text(None) == 0


# ── Relevance-ranked memory injection ─────────────────────────────────


@pytest.mark.asyncio
async def test_context_memories_fall_back_to_importance_without_embeddings(
    client, monkeypatch
):
    from packages.core.services import memory_service

    async with db_module.async_session() as db:
        await memory_service.add_memory(
            db, "ent_mem_rank", "low importance fact", importance=2,
            user_id="user_rank",
        )
        await memory_service.add_memory(
            db, "ent_mem_rank", "high importance fact", importance=9,
            user_id="user_rank",
        )
        await db.commit()

    async def failing_ranker(*args, **kwargs):
        raise RuntimeError("no embedding service")

    monkeypatch.setattr(
        memory_service, "_relevance_ranked_memories", failing_ranker
    )

    async with db_module.async_session() as db:
        context = await memory_service.get_context_memories(
            db, "ent_mem_rank", user_id="user_rank", query="anything at all?"
        )

    assert "high importance fact" in context
    assert context.index("high importance fact") < context.index(
        "low importance fact"
    )


@pytest.mark.asyncio
async def test_context_memories_use_relevance_ranking_when_available(
    client, monkeypatch
):
    from packages.core.services import memory_service

    async with db_module.async_session() as db:
        relevant = await memory_service.add_memory(
            db, "ent_mem_vec", "user prefers dark roast coffee", importance=2,
            user_id="user_vec",
        )
        await memory_service.add_memory(
            db, "ent_mem_vec", "company is a property firm", importance=9,
            user_id="user_vec",
        )
        await db.commit()
        relevant_id = relevant.id

    async def fake_ranker(db, entity_id, **kwargs):
        rows = await memory_service.list_memories(
            db, entity_id, user_id=kwargs.get("user_id"), limit=100
        )
        return sorted(rows, key=lambda r: r.id != relevant_id)

    monkeypatch.setattr(memory_service, "_relevance_ranked_memories", fake_ranker)

    async with db_module.async_session() as db:
        context = await memory_service.get_context_memories(
            db, "ent_mem_vec", user_id="user_vec", query="coffee order?"
        )

    # Relevance order wins over importance order.
    assert context.index("dark roast") < context.index("property firm")


# ── Entity-level chat insight extraction ──────────────────────────────


@pytest.mark.asyncio
async def test_entity_chat_insights_cover_workspaceless_conversations(
    client, monkeypatch
):
    from packages.core.memory import chat_extractor
    from packages.core.models.memory import AgentMemory
    from packages.core.models.user import Entity, User
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from sqlalchemy import select

    start = datetime.now(timezone.utc) - timedelta(hours=1)

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_chat_extract", name="Extract Test Entity"))
        db.add(User(id="user_extract", entity_id="ent_chat_extract", email="extract@test.invalid", password_hash="test", role="owner"))
        db.add(Conversation(
            id="conv_entity_extract",
            entity_id="ent_chat_extract",
            user_id="user_extract",
            workspace_id=None,
        ))
        db.add(Message(
            id="msg_entity_pref",
            conversation_id="conv_entity_extract",
            role="user",
            author_kind="user",
            content="以后所有报告都用中文写，不要用英文。",
            created_at=start,
        ))
        await db.commit()

    async def fake_llm(operation, payload, *, entity_id, workspace_id, user_id, agent_id):
        source_message_ids, existing = payload["source_message_ids"], payload["existing"]
        assert workspace_id is None
        assert user_id == "user_extract"
        assert source_message_ids == ["msg_entity_pref"]
        assert existing == []
        return {"entries": [{
            "scope": "preference",
            "title": "Reports in Chinese",
            "body": "The operator wants all reports written in Chinese.",
            "tags": [],
            "source_message_id": "msg_entity_pref",
            "confidence": 0.7,
        }]}

    from packages.core.memory import compaction
    monkeypatch.setattr(compaction, "run_memory_skill", fake_llm)

    async with db_module.async_session() as db:
        result = await chat_extractor.extract_entity_chat_insights(
            db, "ent_chat_extract"
        )
        await db.commit()

    assert result["extracted"] == 1

    async with db_module.async_session() as db:
        rows = (await db.execute(
            select(AgentLearningCandidate).where(AgentLearningCandidate.entity_id == "ent_chat_extract")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == "user_extract"
        assert rows[0].status == "proposed"
        assert rows[0].payload["memory_type"] == "preference"
        assert "Chinese" in rows[0].summary
        assert rows[0].payload["source_message_id"] == "msg_entity_pref"
        assert not (await db.scalars(select(AgentMemory).where(AgentMemory.entity_id == "ent_chat_extract"))).all()

        entity = await db.get(Entity, "ent_chat_extract")
        assert entity.settings.get(chat_extractor.LAST_EXTRACT_KEY)

    # Second pass: bookmark advanced, nothing new to extract.
    async with db_module.async_session() as db:
        result = await chat_extractor.extract_entity_chat_insights(
            db, "ent_chat_extract"
        )
        await db.commit()
    assert result["extracted"] == 0
