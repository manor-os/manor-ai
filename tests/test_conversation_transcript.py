"""Append-only model transcript for private chat (global owner chat, agent DMs).

Before this, a follow-up turn saw the previous turn only as its final reply plus
320-char tool previews (staging 2026-09-30: 1,376 of 14,908 tool-result chars).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.agentic_loop_prompts import runtime_host_feedback_message
from packages.core.models.base import generate_ulid
from packages.core.models.conversation_transcript import ConversationTranscriptItem
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_transcript import (
    TranscriptScope,
    append_run_transcript,
    load_or_import_transcript,
    transcript_messages,
    transcript_scope_for_turn,
)
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401

SEARCH_RESULTS = json.dumps({"results": [{"title": f"Listing {i}", "price": f"${800 + i}/month"} for i in range(40)]})
CALL = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_search", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"Bay Area RV park\"}"}},
]}
OUTPUT = {"role": "tool", "tool_call_id": "call_search", "content": SEARCH_RESULTS}


async def conversation(db, user_id="user-1"):
    conv = Conversation(id=generate_ulid(), entity_id="entity-1", user_id=user_id, channel="web")
    db.add(conv)
    await db.commit()
    return conv


def scope_for(conv, user_id="user-1"):
    return TranscriptScope(entity_id="entity-1", conversation_id=conv.id, user_id=user_id, agent_id=None, workspace_id=None)


async def items(db, conv):
    return (await db.execute(
        select(ConversationTranscriptItem).where(ConversationTranscriptItem.conversation_id == conv.id)
        .order_by(ConversationTranscriptItem.scope_key, ConversationTranscriptItem.seq)
    )).scalars().all()


def legacy(messages):
    calls = []

    async def load():
        calls.append(1)
        return [dict(m) for m in messages]
    return load, calls


def test_runtime_feedback_rejected_candidates_and_attachments_are_not_carried():
    feedback = runtime_host_feedback_message({"error": "current_evidence_required"})
    kept = transcript_messages([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": [{"type": "text", "text": "see photo"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": "{\"kind\":\"answer\",\"text\":\"rejected\"}"},
        feedback,
        CALL, OUTPUT,
        {"role": "assistant", "content": "The RV site is cheapest.", "reasoning_content": "r"},
    ])
    assert kept[0] == {"role": "user", "content": "see photo\n[image_url attached in this turn; not re-sent]"}
    assert kept[1:] == [CALL, OUTPUT, {"role": "assistant", "content": "The RV site is cheapest.", "reasoning_content": "r"}]


@pytest.mark.asyncio
async def test_next_run_sees_previous_runs_tool_calls_and_outputs_verbatim(db_session):
    conv = await conversation(db_session)
    scope = scope_for(conv)
    load, calls = legacy([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])

    first_initial = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())
    assert calls == [1] and first_initial == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    request = {"role": "user", "content": "Find a Bay Area RV site under $1000"}
    reply = {"role": "assistant", "content": "Listing 0 at $800/month is cheapest."}
    final = [{"role": "system", "content": "rules"}, *first_initial, request, CALL, OUTPUT, reply]
    await append_run_transcript(
        db_session, scope, run_id="run-1", initial_messages=first_initial, final_messages=final,
        user_message_id="msg-user-1", assistant_message_id="msg-reply-1",
    )

    second_initial = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())
    assert calls == [1], "a legacy import happens once"
    assert second_initial == [*first_initial, request, CALL, OUTPUT, reply]
    assert second_initial[4]["content"] == SEARCH_RESULTS
    stored = await items(db_session, conv)
    assert [(i.seq, i.kind, i.source_message_id) for i in stored] == [
        (1, "legacy", None), (2, "message", "msg-user-1"), (3, "message", None),
        (4, "message", None), (5, "message", "msg-reply-1"),
    ]


@pytest.mark.asyncio
async def test_a_compacted_run_appends_one_compaction_item_and_history_starts_there(db_session):
    conv = await conversation(db_session)
    scope = scope_for(conv)
    load, _ = legacy([{"role": "user", "content": "old " * 20}, {"role": "assistant", "content": "older " * 20}])
    initial = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())
    working_view = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "[Earlier conversation compacted: checkpoint v1]\nnote\n{\"summary\": {}}"},
        {"role": "user", "content": "current request"}, CALL, OUTPUT,
        {"role": "assistant", "content": "done"},
    ]
    await append_run_transcript(
        db_session, scope, run_id="run-2", initial_messages=initial, final_messages=working_view,
        user_message_id="msg-user-2", assistant_message_id="msg-reply-2",
    )
    stored = await items(db_session, conv)
    assert [i.kind for i in stored] == ["legacy", "compaction"]
    assert stored[1].payload["source_message_ids"] == ["msg-user-2", "msg-reply-2"]
    assert await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set()) == working_view[1:]


@pytest.mark.asyncio
async def test_rows_no_run_wrote_are_appended_once_and_current_turn_is_left_to_its_run(db_session):
    conv = await conversation(db_session)
    scope = scope_for(conv)
    load, _ = legacy([])
    await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    notice = Message(id=generate_ulid(), conversation_id=conv.id, role="assistant", content="Your weekly report is ready.", created_at=later)
    current = Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Summarize it", created_at=later + timedelta(seconds=1))
    db_session.add_all([notice, current])
    await db_session.commit()

    first = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids={current.id})
    again = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids={current.id})
    assert first == again == [{"role": "assistant", "content": "Your weekly report is ready."}]


async def _run(db, scope, request, reply, *, user_id, reply_id, compacted=False):
    initial = await load_or_import_transcript(db, scope, legacy_history=legacy([])[0], exclude_message_ids=set())
    # A compacted run hands back a working view that no longer starts with its initial history.
    kept = [{"role": "user", "content": "[Earlier conversation compacted]"}] if compacted else initial
    await append_run_transcript(
        db, scope, run_id=None, initial_messages=initial,
        final_messages=[*kept, {"role": "user", "content": request}, CALL, OUTPUT, {"role": "assistant", "content": reply}],
        user_message_id=user_id, assistant_message_id=reply_id,
    )


@pytest.mark.asyncio
async def test_a_turn_reads_back_only_to_the_snapshot_before_the_latest(db_session):
    async def turn_reads(old_turns):
        conv = await conversation(db_session)
        scope = scope_for(conv)
        await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set())
        for i in range(old_turns):
            await _run(db_session, scope, f"old {i}", "ok", user_id=f"old-user-{i}", reply_id=f"old-reply-{i}")
        for c in range(2):
            await _run(db_session, scope, f"long {c}", "ok", user_id=f"cmp-user-{c}", reply_id=f"cmp-reply-{c}", compacted=True)
        await _run(db_session, scope, "new", "ok", user_id="new-user", reply_id="new-reply")
        seen = {}

        async def rows_since(started_at, represented):
            seen.update(started_at=started_at, represented=represented)
            return []
        await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set(),
                                        external_rows=rows_since)
        first_compaction = next(i for i in await items(db_session, conv) if i.kind == "compaction")
        assert seen["started_at"] == first_compaction.created_at
        return seen["represented"]

    few, many = await turn_reads(1), await turn_reads(8)
    assert few == many == {"cmp-user-0", "cmp-reply-0", "cmp-user-1", "cmp-reply-1", "new-user", "new-reply"}


@pytest.mark.asyncio
async def test_rows_that_arrive_while_a_run_compacts_are_still_synced(db_session):
    conv = await conversation(db_session)
    scope = scope_for(conv)
    await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set())
    await _run(db_session, scope, "long 0", "ok", user_id=None, reply_id=None, compacted=True)
    initial = await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set())
    # Posted after this run loaded its history and before its compaction is written.
    notice = Message(id=generate_ulid(), conversation_id=conv.id, role="assistant", content="Your weekly report is ready.")
    db_session.add(notice)
    await db_session.commit()
    await append_run_transcript(
        db_session, scope, run_id=None, initial_messages=initial,
        final_messages=[{"role": "user", "content": "[Earlier conversation compacted]"}, {"role": "assistant", "content": "ok"}],
        user_message_id=None, assistant_message_id=None,
    )
    history = await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set())
    assert history[-1] == {"role": "assistant", "content": "Your weekly report is ready."}
    assert await load_or_import_transcript(db_session, scope, legacy_history=legacy([])[0], exclude_message_ids=set()) == history


@pytest.mark.asyncio
async def test_each_running_principal_has_its_own_stream(db_session):
    conv = await conversation(db_session)
    mine, theirs = scope_for(conv, "user-1"), scope_for(conv, "user-2")
    load_mine, _ = legacy([{"role": "user", "content": "private request with tool output"}])
    load_theirs, _ = legacy([])
    assert await load_or_import_transcript(db_session, mine, legacy_history=load_mine, exclude_message_ids=set())
    assert await load_or_import_transcript(db_session, theirs, legacy_history=load_theirs, exclude_message_ids=set()) == []
    assert {i.scope_key for i in await items(db_session, conv)} == {mine.key, theirs.key}


async def _register_owner(client, username):
    resp = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@example.com",
        "password": "securepass123", "entity_name": f"{username} Co",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    return data["user_id"], me.json()["entity_id"]


def _mock_prompt_pipeline(monkeypatch):
    # Adapted from tests/test_runtime_chat_context_byok.py: keep prompt/tool
    # assembly out of scope while the real resolver loads history from the DB.
    from types import SimpleNamespace

    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.services import runtime_chat_context as module

    envelopes = []

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"), tool_profile=None, is_master=False, task_id=None,
            thread_ref_kind=None, thread_ref_id=None, bound_tool_names=[], mcp_allowed_names=set(), extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **kwargs):
        ctx = ChatContext(db=_db, entity_id=request.entity_id, user_id=request.user_id,
                          workspace_id=request.workspace_id, conversation_id=request.conversation_id)
        envelope = SimpleNamespace(metadata={}, principal=None)
        envelopes.append(envelope)
        return SimpleNamespace(context=ctx, tool_schemas=[], prompt="system prompt", envelope=envelope)

    async def none(*_args, **_kwargs):
        return None

    async def no_calls(*_args, **_kwargs):
        return []

    async def model(*_args, **_kwargs):
        return "openai/gpt-5.5"

    monkeypatch.setattr("packages.core.services.workspace_runtime.resolve_workspace_runtime", fake_resolve_workspace_runtime)
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)
    monkeypatch.setattr(module, "runtime_auto_skill_forced_tool_calls", no_calls)
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_model_for_user", model)
    monkeypatch.setattr("packages.core.services.model_resolver.resolve_llm_metadata_for_user", none)
    return envelopes


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["global_owner_chat", "agent_dm"])
async def test_global_owner_chat_history_comes_from_the_transcript(client, monkeypatch, surface):
    import packages.core.database as dbmod
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_chat_context as module

    owner_chat = surface == "global_owner_chat"
    user_id, entity_id = await _register_owner(client, f"transcript_{surface}")
    _mock_prompt_pipeline(monkeypatch)
    async with dbmod.async_session() as db:
        conv = Conversation(id=generate_ulid(), entity_id=entity_id, user_id=user_id, channel="web")
        db.add(conv)
        await db.flush()
        earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.add_all([
            Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Find a Bay Area RV site", created_at=earlier),
            Message(id=generate_ulid(), conversation_id=conv.id, role="assistant", content="Listing 0 is cheapest.", created_at=earlier + timedelta(seconds=1)),
        ])
        await db.commit()

    async with dbmod.async_session() as db:
        _prompt, _tools, history, ctx = await module.resolve_runtime_chat_context(
            db, "Which option is best?", entity_id=entity_id, user_id=user_id, conversation_id=conv.id,
            disable_tools=True, runtime_surface=ChatSurface(surface),
        )
    assert [m["content"] for m in history][-2:] == ["Find a Bay Area RV site", "Listing 0 is cheapest."]
    if not owner_chat:
        assert ctx.conversation_transcript is None
        return

    assert ctx.conversation_transcript.conversation_id == conv.id
    assert ctx.transcript_initial_messages == history
    async with dbmod.async_session() as db:
        await append_run_transcript(
            db, ctx.conversation_transcript, run_id=None, initial_messages=history,
            final_messages=[{"role": "system", "content": "s"}, *history,
                            {"role": "user", "content": "Which option is best?"}, CALL, OUTPUT,
                            {"role": "assistant", "content": "The RV site."}],
            user_message_id=None, assistant_message_id=None,
        )
    async with dbmod.async_session() as db:
        _prompt, _tools, next_history, _ctx = await module.resolve_runtime_chat_context(
            db, "And with two cats?", entity_id=entity_id, user_id=user_id, conversation_id=conv.id,
            disable_tools=True,
        )
    assert OUTPUT in next_history and CALL in next_history


@pytest.mark.asyncio
async def test_transcript_runs_do_not_also_carry_the_previous_checkpoint(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX, _checkpoint_message
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    seen = []

    async def callbacks(**_kwargs):
        async def save(_messages):
            return None
        # Records history lacks, so carry-time de-duplication alone would keep them.
        other_output = {"role": "tool", "tool_call_id": "call_older", "content": "older run output " * 10}
        return None, None, save, _checkpoint_message({"records": [other_output]}, "archive-1", "digest")

    async def complete(messages, tools, **kwargs):
        seen.append(messages)
        return "Done.", [], {"total": 1}

    monkeypatch.setattr("packages.core.services.runtime_context_archive.runtime_context_archive_callbacks", callbacks)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(runtime_run_id="run-3", initial_messages=[CALL, OUTPUT], dynamic_tool_handlers={},
                  capability_refresh_tool_names=set(), max_rounds=2, carry_previous_checkpoint=False)
    await runtime_execute_agentic_loop(**kwargs)
    assert not [m for m in seen[0] if str(m.get("content", "")).startswith(CHECKPOINT_PREFIX)]
    assert sum(1 for m in seen[0] if m == OUTPUT) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,kept_at_least", [(None, 0), (40_000, 15_000)])
async def test_transcript_runs_keep_tool_outputs_up_to_about_10k_tokens(monkeypatch, limit, kept_at_least):
    import importlib

    loop = importlib.import_module("packages.core.ai.agentic_loop")
    long_output = "row " * 5_000
    rounds = []

    async def complete(messages, tools, **kwargs):
        rounds.append(messages)
        if len(rounds) == 1:
            return "", [{"id": "call_read", "name": "read_file", "arguments": {"path": "big.txt"}}], {"total": 1}
        return "done", [], {"total": 1}

    async def execute(name, args):
        return long_output

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", complete)
    await loop.agentic_loop(
        system_prompt="s", user_message="read it", tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=execute, max_rounds=3, tool_result_max_chars=limit,
    )
    tool_message = next(m for m in rounds[1] if m.get("role") == "tool")
    if limit is None:
        assert len(tool_message["content"]) <= loop.LOOP_TOOL_RESULT_MAX_CHARS + 500
    else:
        assert len(tool_message["content"]) >= kept_at_least


@pytest.mark.parametrize("surface,workspace_id,agent_id,expected", [
    ("global_owner_chat", None, None, True),
    ("global_owner_chat", "ws-1", None, False),
    ("agent_dm", None, "agent-1", True),
    ("agent_dm", "ws-1", "agent-1", True),
    ("agent_dm", None, None, False),
    ("workspace_chat", "ws-1", None, True),
    ("workspace_chat", "ws-1", "agent-1", True),
    ("task_comment_thread", "ws-1", None, True),
    ("workspace_chat", None, None, False),
    ("external_channel_chat", None, "agent-1", True),
    ("public_customer_chat", "ws-1", "agent-1", True),
])
def test_chat_surfaces_that_read_and_write_the_transcript(surface, workspace_id, agent_id, expected):
    from packages.core.ai.runtime.surfaces import ChatSurface

    scope = transcript_scope_for_turn(
        ChatSurface(surface), entity_id="entity-1", conversation_id="conv-1", user_id="user-1",
        agent_id=agent_id, workspace_id=workspace_id,
    )
    assert (scope is not None) is expected
    customer = surface in {"external_channel_chat", "public_customer_chat"}
    if scope is not None:
        # A customer conversation has one stream per Agent, signed in or not.
        principal = "" if customer else "user-1"
        assert scope.key == f"{principal}|{agent_id or ''}|{workspace_id or ''}"
    anonymous = transcript_scope_for_turn(
        ChatSurface(surface), entity_id="entity-1", conversation_id="conv-1", user_id=None,
        agent_id=agent_id, workspace_id=workspace_id,
    )
    assert (anonymous is not None) is (expected and customer)
    assert transcript_scope_for_turn(
        ChatSurface(surface), entity_id="entity-1", conversation_id=None, user_id="user-1",
        agent_id=agent_id, workspace_id=workspace_id,
    ) is None


PLAN = {"name": "plan.pdf", "path": "/workspace/files/plan.pdf", "mime": "application/pdf"}


@pytest.mark.asyncio
async def test_attached_files_keep_their_persisted_refs_in_the_transcript(db_session):
    conv = await conversation(db_session)
    scope = scope_for(conv)
    load, _ = legacy([])
    await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    request = Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Summarize this",
                      attachments=[PLAN], created_at=later)
    other = Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="From my phone",
                    attachments=[PLAN], created_at=later + timedelta(seconds=1))
    db_session.add_all([request, other])
    await db_session.commit()

    initial = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids={request.id})
    await append_run_transcript(
        db_session, scope, run_id=None, initial_messages=initial,
        final_messages=[*initial, {"role": "user", "content": "Summarize this\n\n<attached_files>...</attached_files>"},
                        {"role": "assistant", "content": "It is a plan."}],
        user_message_id=request.id, assistant_message_id=None,
    )
    history = await load_or_import_transcript(db_session, scope, legacy_history=load, exclude_message_ids=set())

    synced, asked, _reply = history
    for message in (synced, asked):
        assert "[Attached file context]" in message["content"]
        assert PLAN["path"] in message["content"]
    assert asked["content"].startswith("Summarize this\n\n<attached_files>")


@pytest.mark.asyncio
async def test_transcript_turns_still_hand_history_attachment_refs_to_child_skills(client, monkeypatch):
    import packages.core.database as dbmod
    from packages.core.ai.runtime.file_context import RUNTIME_HISTORY_ATTACHMENT_REFS_KEY
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_chat_context as module

    user_id, entity_id = await _register_owner(client, "transcript_attachment_refs")
    envelopes = _mock_prompt_pipeline(monkeypatch)
    async with dbmod.async_session() as db:
        conv = Conversation(id=generate_ulid(), entity_id=entity_id, user_id=user_id, channel="web")
        db.add(conv)
        await db.flush()
        db.add(Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Here is the plan",
                       attachments=[PLAN], created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
        await db.commit()

    for _turn in range(2):  # the import turn, then a turn served from the stored transcript
        async with dbmod.async_session() as db:
            _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
                db, "Turn it into slides", entity_id=entity_id, user_id=user_id, conversation_id=conv.id,
                disable_tools=True, runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
            )
        assert ctx.conversation_transcript is not None
        assert [ref["path"] for ref in envelopes[-1].metadata[RUNTIME_HISTORY_ATTACHMENT_REFS_KEY]] == [PLAN["path"]]


@pytest.mark.asyncio
async def test_non_streaming_turns_write_the_transcript_too(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.services import chat_service

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def commit(self):
            pass

    scope = TranscriptScope(entity_id="entity-1", conversation_id="conversation-1", user_id="user-1",
                            agent_id=None, workspace_id=None)
    context = SimpleNamespace(
        workspace_id=None, task_id=None, runtime_envelope=None, tool_profile=None, allowed_tool_names=None,
        user=None, entity=None, conversation_transcript=scope, transcript_initial_messages=[],
    )
    result = AgenticResult(
        content="The RV site.", rounds=2, tool_calls_made=["web_search"], usage={"total": 1},
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "Which?"}, CALL, OUTPUT,
                  {"role": "assistant", "content": "The RV site."}],
    )
    loop = AsyncMock(return_value=result)
    append = AsyncMock()
    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(chat_service, "ChatTrace", lambda **_kwargs: MagicMock(trace_id="non-stream"))
    monkeypatch.setattr(chat_service, "resolve_runtime_chat_context", AsyncMock(return_value=("system", [], [], context)))
    monkeypatch.setattr(chat_service, "runtime_execute_chat_agent_loop", loop)
    monkeypatch.setattr(chat_service, "add_message", AsyncMock(return_value=SimpleNamespace(id="reply-1")))
    monkeypatch.setattr(chat_service, "record_chat_runtime_learning", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_service, "schedule_learning_candidate_applies", AsyncMock())
    monkeypatch.setattr(chat_service, "runtime_persist_chat_runtime_events", AsyncMock())
    monkeypatch.setattr(chat_service, "_append_turn_transcript", append)

    response = await chat_service.run_chat_message(
        "Which?", "conversation-1", entity_id="entity-1", user_id="user-1", db=Session(),
    )

    assert response["message_id"] == "reply-1"
    assert loop.await_args.kwargs["carry_previous_checkpoint"] is False
    assert loop.await_args.kwargs["tool_result_max_chars"] == chat_service.TRANSCRIPT_TOOL_RESULT_MAX_CHARS
    append.assert_awaited_once()
    assert append.await_args.args == (context, result)
    assert append.await_args.kwargs["assistant_message_id"] == "reply-1"


async def _shared_workspace_chat(client, prefix):
    import packages.core.database as dbmod
    from packages.core.models.workspace import Workspace
    from tests.test_document_permissions import _create_entity_user

    alice_id, entity_id = await _register_owner(client, f"{prefix}_alice")
    bob = await _create_entity_user(entity_id, f"{prefix}_bob", display_name="Bob")
    async with dbmod.async_session() as db:
        workspace = Workspace(entity_id=entity_id, name="Launch", status="active")
        db.add(workspace)
        await db.flush()
        conv = Conversation(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                            scope="workspace_main", channel="workspace")
        db.add(conv)
        await db.commit()

    def scope(user_id):
        return TranscriptScope(entity_id=entity_id, conversation_id=conv.id, user_id=user_id,
                               agent_id=None, workspace_id=workspace.id)
    return conv, scope(alice_id), scope(bob["id"]), alice_id, bob["id"]


@pytest.mark.asyncio
async def test_shared_workspace_chat_keeps_tool_outputs_with_the_principal_that_ran_them(client):
    import packages.core.database as dbmod
    from packages.core.services.conversation_history import anonymous_chat_sender_label

    conv, alice, bob, alice_id, bob_id = await _shared_workspace_chat(client, "shared_streams")
    load, _ = legacy([])
    async with dbmod.async_session() as db:
        for principal in (alice, bob):
            await load_or_import_transcript(db, principal, legacy_history=load, exclude_message_ids=set())
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        request = Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Find a Bay Area RV site",
                          meta={"author_user_id": alice_id}, created_at=later)
        reply = Message(id=generate_ulid(), conversation_id=conv.id, role="assistant", content="Listing 0 is cheapest.",
                        created_at=later + timedelta(seconds=2))
        db.add_all([request, reply])
        await db.commit()
        await append_run_transcript(
            db, alice, run_id=None, initial_messages=[],
            final_messages=[{"role": "user", "content": "Find a Bay Area RV site"}, CALL, OUTPUT,
                            {"role": "assistant", "content": "Listing 0 is cheapest."}],
            user_message_id=request.id, assistant_message_id=reply.id,
        )
        db.add(Message(id=generate_ulid(), conversation_id=conv.id, role="user", content="Is it pet friendly?",
                       meta={"author_user_id": bob_id}, created_at=later + timedelta(seconds=3)))
        await db.commit()

        bob_view = await load_or_import_transcript(db, bob, legacy_history=load, exclude_message_ids=set())
        alice_view = await load_or_import_transcript(db, alice, legacy_history=load, exclude_message_ids=set())

    alice_label = anonymous_chat_sender_label(conv.id, "person", alice_id)
    bob_label = anonymous_chat_sender_label(conv.id, "person", bob_id)
    # Bob sees what Alice asked and was told, never her tool output.
    assert OUTPUT not in bob_view and CALL not in bob_view
    assert [m["role"] for m in bob_view] == ["user", "assistant", "user"]
    assert alice_label in bob_view[0]["content"] and bob_view[0]["content"].endswith("Find a Bay Area RV site")
    assert bob_label in bob_view[2]["content"]
    # Alice keeps her own tool output, and her request carries her label like everyone else's.
    assert CALL in alice_view and OUTPUT in alice_view
    assert alice_label in alice_view[0]["content"]
    assert bob_label in alice_view[-1]["content"] and alice_view[-1]["content"].endswith("Is it pet friendly?")


@pytest.mark.asyncio
async def test_shared_workspace_turns_name_the_current_sender_in_the_system_prompt(client, monkeypatch):
    import packages.core.database as dbmod
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_chat_context as module
    from packages.core.services.conversation_history import anonymous_chat_sender_label

    conv, alice, _bob, alice_id, _bob_id = await _shared_workspace_chat(client, "shared_note")
    _mock_prompt_pipeline(monkeypatch)
    async with dbmod.async_session() as db:
        prompt, _tools, history, ctx = await module.resolve_runtime_chat_context(
            db, "Is it pet friendly?", entity_id=alice.entity_id, user_id=alice_id, conversation_id=conv.id,
            workspace_id=alice.workspace_id, disable_tools=True, runtime_surface=ChatSurface.WORKSPACE_CHAT,
        )
    assert ctx.conversation_transcript == alice
    assert not [m for m in history if m.get("role") == "system"]
    assert "The next user message in this shared Workspace chat was sent by" in prompt
    assert anonymous_chat_sender_label(conv.id, "person", alice_id) in prompt


@pytest.mark.asyncio
async def test_importing_a_long_conversation_keeps_its_rolling_summary(db_session):
    conv = await conversation(db_session)
    summary = {"role": "system", "content": "[Earlier conversation summary]\nThe user is planning a Bay Area RV trip."}
    load, _ = legacy([summary, {"role": "user", "content": "Any updates?"},
                      {"role": "system", "content": "The next user message was sent by ..."}])
    history = await load_or_import_transcript(db_session, scope_for(conv), legacy_history=load, exclude_message_ids=set())
    assert history == [{"role": "user", "content": summary["content"]}, {"role": "user", "content": "Any updates?"}]
