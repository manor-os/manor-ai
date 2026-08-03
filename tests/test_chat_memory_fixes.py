"""Regression tests for the chat compression / chat memory design fixes.

Covers:
* rolling conversation summaries merge the prior summary (not overwrite)
  and run on their own session;
* CJK-aware token estimation;
* relevance-ranked chat memory injection falls back to importance;
* entity-level (workspace-less) chats get insight extraction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import packages.core.database as db_module
from packages.core.models.task import Conversation, Message


# ── Rolling summary ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_conversation_summary_merges_prior_summary(
    client, monkeypatch
):
    from packages.core.services import conversation_history

    async with db_module.async_session() as db:
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
    from packages.core.models.user import Entity
    from sqlalchemy import select

    start = datetime.now(timezone.utc) - timedelta(hours=1)

    async with db_module.async_session() as db:
        db.add(Entity(id="ent_chat_extract", name="Extract Test Entity"))
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

    async def fake_llm(payload, *, entity_id, workspace_id):
        assert workspace_id is None
        return [{
            "scope": "preference",
            "title": "Reports in Chinese",
            "body": "The operator wants all reports written in Chinese.",
            "tags": [],
            "source_message_id": "msg_entity_pref",
            "confidence": 0.7,
        }]

    monkeypatch.setattr(chat_extractor, "_call_llm", fake_llm)

    async with db_module.async_session() as db:
        result = await chat_extractor.extract_entity_chat_insights(
            db, "ent_chat_extract"
        )
        await db.commit()

    assert result["extracted"] == 1

    async with db_module.async_session() as db:
        rows = (await db.execute(
            select(AgentMemory).where(AgentMemory.entity_id == "ent_chat_extract")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == "user_extract"
        assert rows[0].memory_type == "preference"
        assert "Chinese" in rows[0].content
        assert rows[0].source == "chat_extract:msg_entity_pref"

        entity = await db.get(Entity, "ent_chat_extract")
        assert entity.settings.get(chat_extractor.LAST_EXTRACT_KEY)

    # Second pass: bookmark advanced, nothing new to extract.
    async with db_module.async_session() as db:
        result = await chat_extractor.extract_entity_chat_insights(
            db, "ent_chat_extract"
        )
        await db.commit()
    assert result["extracted"] == 0
