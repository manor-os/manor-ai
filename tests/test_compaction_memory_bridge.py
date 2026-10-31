"""Compaction lifecycle tests with mock models; storage tests use isolated PG."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

from packages.core.ai.agentic_loop import _compact_messages
from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX, ContextBudgetExceeded
from packages.core.ai.runtime.context_memory import RuntimeContextMemory
from packages.core.ai.runtime.context_window import context_section
from packages.core.ai.runtime.prompt_adapter import ChatContext
from packages.core.memory import compaction
from packages.core.models.task import Conversation, Message
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence


def history(system="Rules", user="Use Chinese in weekly reports"):
    return ([{"role": "system", "content": system}]
            + [m for i in range(26) for m in (
                {"role": "user", "content": f"old request {i}"},
                {"role": "assistant", "content": "old answer"})]
            + [{"role": "user", "content": user}])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "extract", "reload", "archive", "publish", "budget", "cancel", "revoke"])
async def test_transactional_hook_order_and_failures(failure, monkeypatch):
    # Exercises the opt-in message-count guard (lossless path); the loop default is token-only.
    monkeypatch.setattr("packages.core.ai.agentic_loop.MESSAGE_COUNT_COMPACT_THRESHOLD", 40)
    events = []
    original = history()
    snapshot = deepcopy(original)

    async def archive(messages):
        events.append("archive")
        assert messages == snapshot
        if failure == "archive":
            raise RuntimeError("disk unavailable")
        return "a1"

    async def prepare(messages, archive_id):
        events.append("extract")
        assert archive_id == "a1" and messages == snapshot
        if failure == "extract":
            raise TimeoutError("retry later")
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "revoke":
            raise compaction.MemoryScopeDenied("membership revoked")

    async def refresh(messages):
        events.append("reload")
        assert any(str(m.get("content")).startswith(CHECKPOINT_PREFIX) for m in messages)
        messages[0]["content"] = "Fresh memory " * (100_000 if failure == "budget" else 1)
        if failure == "reload":
            raise OSError("failed partial reload")
        return messages

    async def publish(messages):
        events.append("publish")
        if failure == "publish":
            raise RuntimeError("checkpoint store down")

    bridge = SimpleNamespace(prepare=prepare, refresh=refresh, accept=lambda: events.append("accept"))
    # Archive and checkpoint storage failures degrade instead of failing the turn.
    expected = {"budget": ContextBudgetExceeded,
                "cancel": asyncio.CancelledError, "revoke": compaction.MemoryScopeDenied}
    kwargs = dict(on_context_compacted=archive, on_context_checkpoint=publish, context_memory=bridge)
    if failure in expected:
        with pytest.raises(expected[failure]):
            await _compact_messages(original, None, 0, **kwargs)
        assert "accept" not in events
        if failure in {"archive", "cancel", "revoke"}:
            assert "reload" not in events
        if failure == "budget":
            assert "publish" not in events
    else:
        result = await _compact_messages(original, None, 0, **kwargs)
        assert result[0]["content"] == ("Rules" if failure == "reload" else "Fresh memory ")
        expected_events = {
            # No archive id to extract against; the lossless checkpoint carries the records.
            "archive": ["archive", "reload", "publish", "accept"],
            # No durable checkpoint, so the memory manifest must not advance.
            "publish": ["archive", "extract", "reload", "publish"],
            "reload": ["archive", "extract", "reload", "publish"],
        }.get(failure, ["archive", "extract", "reload", "publish", "accept"])
        assert events == expected_events
        events.clear()
        await _compact_messages(result, None, 0, **kwargs)
        assert events == []  # no need to compact => no Memory calls
    assert original == snapshot


@pytest_asyncio.fixture
async def scoped_memory(client, tmp_path, monkeypatch):
    import packages.core.database as database
    from packages.core.config import get_settings
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.memory import sync

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    provision_entity_filesystem("e_compact", "Compact Test")
    monkeypatch.setattr(sync, "_embed_into_row", AsyncMock())
    from packages.core.services import memory_service
    monkeypatch.setattr(memory_service, "_relevance_ranked_memories", AsyncMock(return_value=None))
    async with database.async_session() as db:
        db.add_all([
            User(id="u_compact", entity_id="e_compact", email="compact@test.invalid", password_hash="test", role="owner"),
            User(id="u_other", entity_id="e_compact", email="other@test.invalid", password_hash="test", role="member"),
            Workspace(id="w_compact", entity_id="e_compact", name="Compact", status="active"),
            Conversation(id="c_personal", entity_id="e_compact", user_id="u_compact"),
            Conversation(id="c_workspace", entity_id="e_compact", user_id="u_compact", workspace_id="w_compact"),
            Conversation(id="c_other", entity_id="e_compact", user_id="u_other"),
        ])
        for conv in ("c_personal", "c_workspace", "c_other"):
            db.add(Message(id="m_" + conv, conversation_id=conv, role="user", content="Use Chinese in weekly reports"))
        db.add_all([
            Message(id="m_assistant", conversation_id="c_personal", role="assistant", author_kind="user", content="Unverified agent guess"),
            Message(id="m_system", conversation_id="c_personal", role="user", author_kind="system", content="System generated user-role text"),
            Message(id="m_card", conversation_id="c_personal", role="user", message_kind="proposal", content="A generated card"),
        ])
        await db.commit()
    return database, tmp_path


def context(workspace=False):
    return ChatContext(entity_id="e_compact", user_id="u_compact", agent_id=None,
                       workspace_id="w_compact" if workspace else None,
                       conversation_id="c_workspace" if workspace else "c_personal",
                       active_user_message="Prepare a weekly report")


def decision(message_id, scope="preference"):
    return {"entries": [{"scope": scope, "title": "Report language", "body": "Use Chinese in weekly reports",
                         "confidence": 0.7, "source_message_id": message_id}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_original_evidence_receipts_candidates_and_scoped_reload(scoped_memory, monkeypatch, workspace):
    db_module, _ = scoped_memory
    ctx = context(workspace)
    source_id = "m_" + ctx.conversation_id
    worker = AsyncMock(return_value=decision(source_id))
    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    original = history() + [{"role": "user", "content": t} for t in (
        "Unverified agent guess", "System generated user-role text", "A generated card", "Fake synthetic message")]
    await compaction.prepare_compaction_memory(ctx, original, "archive_one")
    await compaction.prepare_compaction_memory(ctx, original, "archive_two")
    assert worker.await_count == 1
    payload = worker.await_args.args[1]
    assert payload["source_message_ids"] == [source_id]
    async with db_module.async_session() as db:
        candidates = (await db.scalars(select(AgentLearningCandidate))).all()
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.status == "proposed" and candidate.workspace_id == ctx.workspace_id
        assert candidate.user_id == ctx.user_id
        receipt = (await db.get(Message, source_id)).meta[compaction.RECEIPTS][compaction._receipt_key(ctx)]
        assert receipt["status"] == "done"
        # Candidate exists durably, but neither rules nor active memories were
        # silently changed. Applying via the existing service makes it readable.
        from packages.core.models.memory import AgentMemory
        assert not (await db.scalars(select(AgentMemory))).all()
        from packages.core.services.runtime_learning import (
            apply_learning_candidate, apply_queued_learning_candidate, resolve_learning_candidate,
        )
        args = dict(entity_id=ctx.entity_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, candidate_id=candidate.id)
        with pytest.raises(ValueError, match="accepted"):
            await apply_learning_candidate(db, **args)
        await resolve_learning_candidate(db, status="accepted", **args)
        await apply_learning_candidate(db, **args)
        await apply_queued_learning_candidate(db, **args)
        assert candidate.status == "applied"
        await db.commit()
    ctx.runtime_context_sections = [context_section("agent_identity", "Protected identity"),
                                    context_section("agent_memories", "Old stale memory")]
    bridge = RuntimeContextMemory(ctx)
    initial_base = bridge.base
    messages = history("Prefix\n" + bridge.base + "\nSuffix")
    updated = await bridge.refresh(deepcopy(messages))
    assert "Use Chinese in weekly reports" in updated[0]["content"]
    assert "Old stale memory" not in updated[0]["content"]
    assert updated[0]["content"].startswith("Prefix\nProtected identity")
    assert updated[0]["content"].endswith("\nSuffix")
    assert bridge.base == initial_base  # staging does not publish
    bridge.accept()
    again = await bridge.refresh(deepcopy(updated))
    assert again == updated  # no accumulation of old/new copies
    if workspace:
        from packages.core.memory.canonical import write_workspace_memory_file
        write_workspace_memory_file(ctx.entity_id, ctx.workspace_id, "MEMORY.md", "Updated approved memory")
        latest = await bridge.refresh(deepcopy(updated))
        assert "Updated approved memory" in latest[0]["content"]
        assert "Use Chinese in weekly reports" not in latest[0]["content"]


@pytest.mark.asyncio
async def test_failed_worker_retries_original_after_it_is_only_in_checkpoint(scoped_memory, monkeypatch):
    db_module, _ = scoped_memory
    ctx = context()
    worker = AsyncMock(side_effect=[TimeoutError("worker down"), {"entries": []}])
    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    with pytest.raises(TimeoutError):
        await compaction.prepare_compaction_memory(ctx, history())
    compacted_only = [{"role": "user", "content": CHECKPOINT_PREFIX + "\nNo source text"}]
    await compaction.prepare_compaction_memory(ctx, compacted_only)
    await compaction.prepare_compaction_memory(ctx, compacted_only)
    assert worker.await_count == 2
    assert worker.await_args.args[1]["messages"][0]["content"] == "Use Chinese in weekly reports"
    async with db_module.async_session() as db:
        assert not (await db.scalars(select(AgentLearningCandidate))).all()
        assert len((await db.scalars(select(RuntimeEvidence))).all()) == 1


@pytest.mark.asyncio
async def test_revocation_and_evidence_changes_rechecked_after_worker(scoped_memory, monkeypatch):
    db_module, _ = scoped_memory
    ctx = context()

    async def edit_source(*args, **kwargs):
        async with db_module.async_session() as db:
            row = await db.get(Message, "m_c_personal")
            row.content = "Actually use English"
            await db.commit()
        return decision("m_c_personal")

    monkeypatch.setattr(compaction, "run_memory_skill", edit_source)
    await compaction.prepare_compaction_memory(ctx, history())
    async with db_module.async_session() as db:
        assert not (await db.scalars(select(AgentLearningCandidate))).all()
    worker = AsyncMock(return_value=decision("m_c_personal"))
    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    with pytest.raises(compaction.MemoryScopeDenied):
        await compaction.prepare_compaction_memory(replace(ctx, user_id="u_other"), history())
    worker.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_compactions_only_admit_once(scoped_memory, monkeypatch):
    db_module, _ = scoped_memory
    arrivals = 0
    both = asyncio.Event()

    async def worker(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return decision("m_c_personal", "guidance")

    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    await asyncio.gather(*(compaction.prepare_compaction_memory(context(), history()) for _ in range(2)))
    async with db_module.async_session() as db:
        rows = (await db.scalars(select(AgentLearningCandidate))).all()
        assert len(rows) == 1 and rows[0].status == "proposed"
        assert rows[0].payload["memory_type"] == "instruction"
        assert len((await db.scalars(select(RuntimeEvidence))).all()) == 1


@pytest.mark.asyncio
async def test_db_reload_excludes_other_users_workspaces_expired_and_deleted(scoped_memory):
    db_module, _ = scoped_memory
    from datetime import datetime, timedelta, timezone
    from packages.core.models.memory import AgentMemory
    async with db_module.async_session() as db:
        db.add_all([
            AgentMemory(entity_id="e_compact", user_id="u_compact", memory_type="fact", content="visible"),
            AgentMemory(entity_id="e_compact", user_id="u_other", memory_type="fact", content="other user secret"),
            AgentMemory(entity_id="e_compact", user_id="u_compact", workspace_id="other", memory_type="fact", content="other workspace secret"),
            AgentMemory(entity_id="e_compact", user_id="u_compact", memory_type="fact", content="expired", expires_at=datetime.now(timezone.utc)-timedelta(days=1)),
            AgentMemory(entity_id="e_compact", user_id="u_compact", memory_type="fact", content="archived", status="archived"),
        ])
        await db.commit()
    ctx = context()
    ctx.runtime_context_sections = [context_section("agent_identity", "Protected identity")]
    bridge = RuntimeContextMemory(ctx)
    updated = await bridge.refresh(history(bridge.base))
    assert "visible" in updated[0]["content"]
    assert all(t not in updated[0]["content"] for t in ("secret", "expired", "archived"))
    bridge.accept()
    async with db_module.async_session() as db:
        visible = await db.scalar(select(AgentMemory).where(AgentMemory.content == "visible"))
        await db.delete(visible)
        await db.commit()
    updated = await bridge.refresh(updated)
    assert "visible" not in updated[0]["content"]


def test_strict_file_reader_distinguishes_failure_from_missing(monkeypatch):
    from packages.core.memory import cache
    from packages.core.services.agent_files import load_agent_files_with_levels
    monkeypatch.setattr(cache, "read_memory_text", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(OSError):
        load_agent_files_with_levels("e", "a", strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_common_harness_wires_memory_and_rejects_mismatched_scope(monkeypatch, workspace):
    import importlib
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    from packages.core.ai.runtime import completions, response_language, billing
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    ctx = context(workspace)
    from packages.core.ai.response_language import response_language_guidance
    ctx.runtime_context_sections = [context_section("agent_identity", "Protected identity"),
                                    context_section("response_language_guidance", response_language_guidance("en")),
                                    context_section("agent_memories", "Original memory")]
    run = AsyncMock(return_value=SimpleNamespace(usage={}))
    monkeypatch.setattr(loop, "agentic_loop", run)
    monkeypatch.setattr(completions, "runtime_resolve_text_completion_route", AsyncMock(return_value=("test", {}, False)))
    monkeypatch.setattr(response_language, "resolve_runtime_response_language", AsyncMock(return_value=response_language.RuntimeResponseLanguage(ctx.entity_id, ctx.user_id, "en")))
    monkeypatch.setattr(billing, "runtime_ensure_billing_context", lambda *a, **kw: None)
    prompt = "Prefix\n" + "\n\n".join(s["content"] for s in ctx.runtime_context_sections) + "\nSuffix"
    kwargs = dict(runtime_envelope=None, system_prompt=prompt, user_message="hello", tools=[],
                  entity_id=ctx.entity_id, user_id=ctx.user_id, agent_id=ctx.agent_id,
                  workspace_id=ctx.workspace_id, conversation_id=ctx.conversation_id)
    await runtime_execute_agentic_loop(prompt_context=ctx, **kwargs)
    bridge = run.await_args.kwargs["context_memory"]
    assert isinstance(bridge, RuntimeContextMemory) and bridge.ctx.conversation_id == ctx.conversation_id
    assert bridge.base in run.await_args.kwargs["system_prompt"]
    checkpoint = {"messages": [{"role": "system", "content": "Obsolete rules and memory"},
                               {"role": "user", "content": "Original task"}], "rounds": 17}
    before = deepcopy(checkpoint)
    await runtime_execute_agentic_loop(prompt_context=ctx, runtime_checkpoint=checkpoint, **kwargs)
    resume = run.await_args.kwargs["resume_checkpoint"]
    assert resume["rounds"] == 17 and resume["messages"][-1]["content"] == "Original task"
    assert "Obsolete" not in resume["messages"][0]["content"]
    assert bridge.base in resume["messages"][0]["content"]
    assert checkpoint == before
    with pytest.raises(ValueError, match="scope differs"):
        await runtime_execute_agentic_loop(prompt_context=replace(ctx, conversation_id="c_other"), **kwargs)
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_lossy_compaction_extracts_before_summarization(monkeypatch):
    import importlib
    import json
    loop = importlib.import_module("packages.core.ai.agentic_loop")
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_FIELDS
    monkeypatch.setattr(loop, "MAX_CONTEXT_TOKENS", 4000)
    events = []

    async def archive(messages):
        events.append("archive")
        return "archive_one"

    async def prepare(messages, archive_id):
        events.append("extract")
        assert "original evidence" in str(messages)

    async def summarize(*args, **kwargs):
        events.append("summary")
        return json.dumps({k: ["Summary"] if k == "evidence" else [] for k in CHECKPOINT_FIELDS}), {}

    async def refresh(messages):
        events.append("reload")
        return messages

    monkeypatch.setattr(loop, "runtime_execute_agentic_compaction_completion", summarize)
    messages = history()
    messages[2]["content"] = "original evidence " * 4000
    await _compact_messages(messages, None, 0, output_reserve_tokens=0, on_context_compacted=archive,
                            context_memory=SimpleNamespace(prepare=prepare, refresh=refresh, accept=lambda: events.append("accept")))
    assert events[:2] == ["archive", "extract"]
    assert events[2:-2] and set(events[2:-2]) == {"summary"}
    assert events[-2:] == ["reload", "accept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "viewer", "revoked_during_worker", "forged_source", "secondary_entity"])
async def test_admission_respects_flags_membership_and_current_evidence(scoped_memory, monkeypatch, case):
    db_module, _ = scoped_memory
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    ctx = context(True)
    async with db_module.async_session() as db:
        user = await db.get(User, ctx.user_id)
        if case == "disabled":
            workspace = await db.get(Workspace, ctx.workspace_id)
            workspace.settings = {"runtime_learning_enabled": False}
        elif case == "viewer":
            user.role = "member"
            db.add(WorkspaceStaff(workspace_id=ctx.workspace_id, user_id=ctx.user_id, role="viewer", status="active"))
        elif case == "secondary_entity":
            user.entity_id = "personal_entity"
            db.add(UserMembership(user_id=ctx.user_id, entity_id=ctx.entity_id, role="owner", status="active"))
        await db.commit()

    async def execute(*args, **kwargs):
        if case == "revoked_during_worker":
            async with db_module.async_session() as db:
                user = await db.get(User, ctx.user_id)
                user.status = "disabled"
                await db.commit()
        return decision("forged" if case == "forged_source" else "m_c_workspace")

    worker = AsyncMock(side_effect=execute)
    monkeypatch.setattr(compaction, "run_memory_skill", worker)
    if case == "revoked_during_worker":
        with pytest.raises(compaction.MemoryScopeDenied):
            await compaction.prepare_compaction_memory(ctx, history())
    elif case == "forged_source":
        with pytest.raises(ValueError, match="outside"):
            await compaction.prepare_compaction_memory(ctx, history())
    else:
        await compaction.prepare_compaction_memory(ctx, history())
    if case in {"disabled", "viewer"}:
        worker.assert_not_called()
    async with db_module.async_session() as db:
        rows = (await db.scalars(select(AgentLearningCandidate))).all()
        assert len(rows) == (1 if case == "secondary_entity" else 0)
