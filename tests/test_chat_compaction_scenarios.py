"""Composed chat scenarios with real persistence and a deterministic provider.

These cover orchestration and replay, not the semantic quality of an LLM summary.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from packages.core import database
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.tools import request_input_tool
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity
from packages.core.services import conversation_history as history
from packages.core.services.runtime_human_input import answer_runtime_human_input
from packages.core.services.runtime_run_service import (
    create_runtime_run,
    persist_runtime_run_suspension,
)

loop = importlib.import_module("packages.core.ai.agentic_loop")
pytestmark = pytest.mark.asyncio


async def test_multiturn_history_retry_stream_completion_and_reload(client, monkeypatch):
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with database.async_session() as db:
        db.add(Entity(id="scenario-entity", name="Compaction scenario"))
        db.add(Conversation(id="scenario-history", entity_id="scenario-entity"))
        db.add_all([
            Message(
                id=f"scenario-{index:04}", conversation_id="scenario-history",
                role="assistant" if index == 10 else "user",
                content=f"fact:{index:03}", created_at=timestamp,
                meta={"stream_status": "running"} if index == 10 else {},
            )
            for index in range(262)
        ])
        await db.commit()

    seen_pages: list[list[str]] = []
    fail_next = True

    async def summarize(**kwargs):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise TimeoutError("simulated provider interruption")
        page = re.findall(r"fact:\d{3}", kwargs["text_block"])
        seen_pages.append(page)
        return SimpleNamespace(content=" ".join([
            kwargs.get("prior_summary") or "", *page,
        ]).strip())

    monkeypatch.setattr(history, "runtime_execute_conversation_summary_completion", summarize)

    async def load_and_drain():
        async with database.async_session() as db:
            restored = await history.load_conversation_history(db, "scenario-history")
        task = history._summary_tasks.get("scenario-history")
        if task is not None:
            await task
            await asyncio.sleep(0)
        return restored

    # A failed background provider must not damage coverage or block chat.
    assert len(await load_and_drain()) == 200
    async with database.async_session() as db:
        conv = await db.get(Conversation, "scenario-history")
        assert conv.summary_version == 0 and conv.summary is None
    await load_and_drain()
    await load_and_drain()
    assert seen_pages == [[f"fact:{i:03}" for i in range(10)]]
    async with database.async_session() as db:
        conv = await db.get(Conversation, "scenario-history")
        assert conv.summary_covered_message_id == "scenario-0009"
        streaming = await db.get(Message, "scenario-0010")
        streaming.meta = {"stream_status": "completed"}
        streaming.content = "fact:010 completed reply"
        await db.commit()

    # Subsequent turns advance bounded pages across the old streaming barrier.
    for _ in range(3):
        restored = await load_and_drain()
    covered_facts = [fact for page in seen_pages for fact in page]
    assert covered_facts == [f"fact:{i:03}" for i in range(62)]
    assert restored[0]["role"] == "system"
    assert "fact:000" in restored[0]["content"]
    assert "fact:061" in restored[0]["content"]
    assert restored[1]["content"] == "fact:062"

    # The real loop receives durable memory and the current correction together.
    correction = "预算改为8000元，只生成草稿，不能发布或覆盖原文件。"

    async def reply(messages, **_kwargs):
        assert "fact:000" in messages[1]["content"]
        assert "fact:061" in messages[1]["content"]
        assert any(m.get("content") == correction for m in messages)
        return "按8000元预算继续草稿。", {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_text_completion", reply)
    result = await loop.agentic_loop(
        system_prompt="Keep user constraints.", user_message=correction,
        initial_messages=restored, tools=[], tool_executor=AsyncMock(), max_rounds=1,
    )
    assert result.error is None
    async with database.async_session() as db:
        db.add(Message(
            id="scenario-next-user", conversation_id="scenario-history",
            role="user", content=correction,
        ))
        db.add(Message(
            id="scenario-next-answer", conversation_id="scenario-history",
            role="assistant", content=result.content,
        ))
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Message)) == 264
    monkeypatch.setattr(history, "MAX_HISTORY_ROWS", 500)
    restored = await load_and_drain()
    assert "fact:000" in restored[0]["content"]
    assert all("fact:000" not in m["content"] for m in restored[1:])
    assert any(m["content"] == correction for m in restored)
    assert any(m["content"] == result.content for m in restored)


def _assert_complete_tool_protocol(messages):
    pending = set()
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending, "A provider request contains an unanswered tool call"
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


async def test_repeated_compaction_then_durable_input_pause_and_resume(client, monkeypatch):
    tools = [{"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}},
    }} for name in ("inspect_record", "request_input")]
    observed_summary_counts = []
    checkpoint_digests = []
    executed = []
    provider_round = 0
    resumed = False
    request = "核对数据；不要发布，最后让我选择草稿。"

    async def provider(messages, *_args, **_kwargs):
        nonlocal provider_round
        _assert_complete_tool_protocol(messages)
        assert any(m.get("content") == request for m in messages)
        assert any("保留原文件" in str(m.get("content")) for m in messages)
        assert loop._estimate_request_tokens(messages, tools) + 2_000 < loop.MAX_CONTEXT_TOKENS
        observed_summary_counts.append(sum(loop._is_compaction_summary_message(m) for m in messages))
        if resumed:
            answer = next(m for m in messages if m.get("tool_call_id") == "scenario-ask")
            assert json.loads(answer["content"])["answer"] == "草稿B；预算8000元。"
            return "已按8000元选定草稿B，等待审核，未发布。", [], {}
        provider_round += 1
        if provider_round <= 60:
            return "", [{
                "id": f"inspect-{provider_round}", "name": "inspect_record",
                "arguments": {"index": provider_round},
            }], {}
        return "", [{
            "id": "scenario-ask", "name": "request_input",
            "arguments": {
                "question": "请选择草稿", "options": ["草稿A", "草稿B"],
                "justification": {
                    "kind": "human_decision",
                    "why_human_required": "只有用户能选择最终草稿。",
                    "attempted_resolution": [{
                        "source": "conversation", "finding": "用户要求最后由自己选择草稿。",
                    }],
                },
            },
        }], {}

    async def checkpointed(messages):
        from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str) and content.startswith(CHECKPOINT_PREFIX):
                checkpoint_digests.append(json.loads(content.split("\n", 2)[2])["source_digest"])

    async def execute(name, args):
        executed.append(name)
        if name == "request_input":
            return await request_input_tool._request_input(**args)
        # Under the loop's first-delivery budget, so 60 rounds stay below the
        # token trigger and only the message-count guard compacts.
        return json.dumps({"index": args["index"], "data": "checked " * 450})

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", provider)
    # Repeated lossless compaction comes from the opt-in message-count guard;
    # 60 rounds stay below the token trigger.
    monkeypatch.setattr(loop, "MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    result = await loop.agentic_loop(
        system_prompt="保留原文件，审批由运行时负责。", user_message=request,
        tools=tools, tool_executor=execute, max_rounds=70, max_tokens=2_000,
        human_input_suspension=True, on_context_checkpoint=checkpointed,
    )
    assert result.error is None and result.stop_reason == "waiting_resource"
    # Repeated compaction replaces the checkpoint instead of stacking summaries.
    assert max(observed_summary_counts) == 1
    assert len(set(checkpoint_digests)) >= 2
    assert executed.count("inspect_record") == 60
    assert executed.count("request_input") == 1

    async with database.async_session() as db:
        run = await create_runtime_run(
            db, conversation_id="scenario-resume", assistant_message_id="scenario-answer",
            entity_id="scenario-entity", user_id="scenario-owner", agent_id=None,
            workspace_id=None, execution_payload={"message": request},
        )
        await db.flush()
        run = await persist_runtime_run_suspension(db, run_id=run.id, control=result.control)
        run_id = run.id
        await db.commit()
    async with database.async_session() as db:
        answered = await answer_runtime_human_input(
            db, run_id=run_id, entity_id="scenario-entity", user_id="scenario-owner",
            answer="草稿B；预算8000元。",
        )
        checkpoint = RuntimeAgentCheckpoint.from_dict(answered.checkpoint)
        assert checkpoint.pending_tool_result["id"] == "scenario-ask"
        await db.commit()

    resumed = True
    execution_before_resume = list(executed)
    finished = await loop.agentic_loop(
        system_prompt="保留原文件，审批由运行时负责。", user_message=request,
        tools=tools, tool_executor=execute, max_rounds=70, max_tokens=2_000,
        human_input_suspension=True, resume_checkpoint=checkpoint.to_dict(),
    )
    assert finished.error is None and finished.stop_reason == "completed"
    assert "草稿B" in finished.content and "8000" in finished.content
    assert executed == execution_before_resume, "Resume must not replay completed tools"
