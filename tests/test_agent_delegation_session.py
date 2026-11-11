"""Workspace service Agents keep one private context across delegations."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from packages.core.models.agent_delegation_session import AgentDelegationSession
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition


async def _workspace(client: AsyncClient) -> tuple[dict, dict]:
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "delegate_resume",
            "email": "delegate_resume@test.com",
            "password": "pass123",
            "entity_name": "Delegate Resume Corp",
        },
    )
    assert register.status_code == 200
    headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
    response = await client.post(
        "/api/v1/workspaces", headers=headers,
        json={"name": "Delegation continuation"},
    )
    assert response.status_code == 201
    return headers, response.json()


_CHILD_FILE = {"kind": "document", "name": "child.md", "content": "From the child"}


async def _main_chat_child(client, db_session, monkeypatch, tmp_path, tool, arguments, *, before_seed=None):
    """A Workspace main chat whose child runs for real; only its model is scripted.

    The result delegates to the child: the real child loop, tool gate and tool.
    """
    from packages.core.ai import agentic_loop as loop_module
    from packages.core.ai.runtime import billing, completions
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    headers, workspace = await _workspace(client)
    if before_seed is not None:
        await before_seed(headers, workspace)
    user_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["id"]
    agent_id, subscription_id, conversation_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add_all([
        Agent(id=agent_id, entity_id=workspace["entity_id"], name="Writer", status="active"),
        AgentSubscription(
            id=subscription_id, entity_id=workspace["entity_id"], workspace_id=workspace["id"],
            agent_id=agent_id, service_key="writer", status="active",
        ),
        Conversation(
            id=conversation_id, entity_id=workspace["entity_id"],
            workspace_id=workspace["id"], scope="workspace_main",
        ),
    ])
    if tool == "generate_file":
        definition = ToolDefinition(id=generate_ulid(), name=tool, display_name=tool, category="file")
        db_session.add(definition)
        await db_session.flush()
        db_session.add(AgentToolBinding(agent_id=agent_id, tool_id=definition.id))
    await db_session.commit()

    rounds = {"seen": 0}

    async def completion(_messages, tools=None, **_kwargs):
        rounds["seen"] += 1
        usage = {"prompt": 1, "completion": 1, "total": 2}
        if rounds["seen"] == 1:
            return "", [{"id": "call_1", "name": tool, "arguments": arguments}], usage
        return "done", None, usage

    async def text(_messages, **_kwargs):
        return "done", {"prompt": 1, "completion": 1, "total": 2}

    async def route(**_kwargs):
        return None, {}, None

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", text)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", text)
    monkeypatch.setattr(completions, "runtime_resolve_text_completion_route", route)
    monkeypatch.setattr(billing, "runtime_ensure_billing_context", lambda *_a, **_k: None)

    async def delegate(**params):
        return json.loads(await _workspace_agent_handler(
            entity_id=workspace["entity_id"], workspace_id=workspace["id"], conversation_id=conversation_id,
            user_id=user_id, action="delegate_service", params={"service_key": "writer", **params},
        ))

    return SimpleNamespace(
        delegate=delegate, agent_id=agent_id, conversation_id=conversation_id, user_id=user_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments", "error"), [
    # Past the gate: only the scripted sandbox id, which no one created, is refused.
    ("sandbox", {"action": "exec", "params": {"sandbox_id": "sbx-1", "command": "ls"}}, "sandbox_access_denied"),
    # File tools have a second identity check behind the tool gate.
    ("generate_file", _CHILD_FILE, None),
])
async def test_main_chat_child_write_passes_the_identity_gate_and_is_logged(
    client: AsyncClient, db_session, monkeypatch, tmp_path, tool, arguments, error,
) -> None:
    """Prod 2026-11: a child delegated from a Workspace main chat had no run
    identity, so every gated call it made was refused and it logged nothing.
    """
    from packages.core.ai.chat_logger import DELEGATION_TOOL_CALL_SOURCE
    from packages.core.models.usage import ToolCallLog

    child = await _main_chat_child(client, db_session, monkeypatch, tmp_path, tool, arguments)
    result = await child.delegate(prompt="Do the write.")

    for _ in range(50):
        rows = (await db_session.execute(
            select(ToolCallLog).where(ToolCallLog.conversation_id == child.conversation_id)
        )).scalars().all()
        if rows:
            break
        await asyncio.sleep(0.05)

    assert [row.tool_name for row in rows] == [tool], result
    assert rows[0].source == DELEGATION_TOOL_CALL_SOURCE
    assert rows[0].agent_id == child.agent_id
    if error:
        assert error in rows[0].error
    else:
        assert rows[0].error is None
        assert result["status"] == "completed"
        assert [path.read_text() for path in tmp_path.rglob("child.md")] == ["From the child"]


@pytest.mark.asyncio
async def test_main_chat_child_approval_holds_the_write_until_granted_and_once(
    client: AsyncClient, db_session, monkeypatch, tmp_path,
) -> None:
    from packages.core.governance.approvals import grant_approval
    from packages.core.models.hitl_request import HitlRequest

    async def ask_before_file_writes(headers, workspace):
        draft = await client.post(
            f"/api/v1/workspaces/{workspace['id']}/operation/drafts", headers=headers,
            json={"patches": [{"op": "rule.add", "payload": {"rule": {
                "rule_key": "review_files", "description": "Review new files",
                "rule_type": "approval_required", "action_patterns": ["workspace.file.create"],
            }}}]},
        )
        applied = await client.post(
            f"/api/v1/workspaces/{workspace['id']}/operation/drafts/{draft.json()['id']}/apply",
            headers=headers, json={"user_confirmation": True},
        )
        assert applied.status_code == 200, applied.text

    child = await _main_chat_child(
        client, db_session, monkeypatch, tmp_path, "generate_file", _CHILD_FILE,
        before_seed=ask_before_file_writes,
    )

    paused = await child.delegate(prompt="Do the write.")
    assert paused["error"] == "approval_required", paused
    assert not list(tmp_path.rglob("child.md")), paused
    token = paused["approval_token"]

    request = await db_session.get(HitlRequest, token)
    await grant_approval(db_session, request, by_user_id=child.user_id, via="chat")
    await db_session.commit()

    resumed = await child.delegate(approval_token=token)
    assert resumed["status"] == "completed", resumed
    assert [path.read_text() for path in tmp_path.rglob("child.md")] == ["From the child"]

    replayed = await child.delegate(approval_token=token)
    assert replayed["error"] == "delegated_approval_resume_rejected", replayed
    assert len(list(tmp_path.rglob("child.md"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_limit, requested, expected", [
    (None, None, 200), (20, None, 20), (20, 9999, 20), (20, 3, 3),
])
async def test_workspace_service_delegation_resumes_private_context(
    client: AsyncClient, db_session, monkeypatch, parent_limit, requested, expected,
) -> None:
    from packages.core.ai.tools.workspace_agent_tools import _workspace_agent_handler

    _, workspace = await _workspace(client)
    agent_id, subscription_id, conversation_id = (
        generate_ulid(), generate_ulid(), generate_ulid(),
    )
    db_session.add_all([
        Agent(id=agent_id, entity_id=workspace["entity_id"], name="Researcher", status="active"),
        AgentSubscription(
            id=subscription_id, entity_id=workspace["entity_id"],
            workspace_id=workspace["id"], agent_id=agent_id,
            service_key="research", status="active",
        ),
        Conversation(
            id=conversation_id, entity_id=workspace["entity_id"],
            workspace_id=workspace["id"], scope="workspace_main",
        ),
        Message(
            id=generate_ulid(), conversation_id=conversation_id, role="user",
            content="Private parent discussion must not be copied.",
        ),
    ])
    await db_session.commit()

    context_calls: list[dict] = []
    loop_calls: list[dict] = []

    async def fake_context(_db, **kwargs):
        context_calls.append(kwargs)
        return SimpleNamespace(
            runtime_envelope=None, system_prompt="Researcher instructions",
            tools=[{"type": "function", "function": {"name": "workspace_search"}}],
            allowed_tool_names={"workspace_search"}, task_id=None,
            tool_profile="workspace_agent", model="gpt-test", llm_metadata=None,
            turn_execution_plan={"max_rounds": parent_limit} if parent_limit else None,
        )

    async def fake_loop(**kwargs):
        loop_calls.append(kwargs)
        answer = "First finding" if len(loop_calls) == 1 else "Follow-up finding"
        return SimpleNamespace(
            content=answer,
            messages=[
                {"role": "system", "content": kwargs["system_prompt"]},
                *(kwargs.get("initial_messages") or []),
                {"role": "user", "content": kwargs["user_message"]},
                {"role": "assistant", "content": answer},
            ],
            rounds=2, tool_calls_made=[], usage={"total_tokens": 20},
            stop_reason="completed", error=None,
        )

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_context)
    monkeypatch.setattr(
        "packages.core.ai.runtime.harness.runtime_execute_chat_agent_loop", fake_loop,
    )
    common = {
        "entity_id": workspace["entity_id"],
        "workspace_id": workspace["id"],
        "conversation_id": conversation_id,
        "user_id": "delegate-user",
        "action": "delegate_service",
    }
    first = json.loads(await _workspace_agent_handler(
        **common, params={"service_key": "research", "prompt": "Find the answer.", "max_rounds": requested},
    ))
    second = json.loads(await _workspace_agent_handler(
        **common, params={"service_key": "research", "prompt": "Check the first finding.", "max_rounds": requested},
    ))

    assert first["status"] == second["status"] == "completed"
    assert first["session_id"] == second["session_id"]
    assert first["run_id"] != second["run_id"]
    assert loop_calls[0]["max_rounds"] == loop_calls[1]["max_rounds"] == expected
    assert loop_calls[0]["initial_messages"] is None
    assert loop_calls[1]["initial_messages"] == [
        {"role": "user", "content": "Find the answer."},
        {"role": "assistant", "content": "First finding"},
    ]
    # The answer limit is this call's turn context, never the agent's stored history.
    assert all("limited to" in call["turn_context"] for call in loop_calls)
    assert all(call["user_message"] in {"Find the answer.", "Check the first finding."} for call in loop_calls)
    assert all(call["load_history"] is False for call in context_calls)
    assert all(call["agent_subscription_id"] == subscription_id for call in context_calls)
    assert "Private parent discussion" not in json.dumps(loop_calls[1]["initial_messages"])
    assert "context_usage" not in second

    stored = (await db_session.execute(
        select(AgentDelegationSession).where(AgentDelegationSession.id == first["session_id"])
    )).scalar_one()
    assert stored.turn_count == 2
    assert stored.status == "idle"
    assert stored.messages[-1]["content"] == "Follow-up finding"
    assert stored.context_usage["context_window_tokens"] == 128_000
    assert stored.context_state["version"] == 1
    assert stored.context_state["history"][-1]["category"] == "messages"
    assert stored.context_state["sources"][0]["category"] == "system_tools"

    subscription = await db_session.get(AgentSubscription, subscription_id)
    subscription.status = "inactive"
    await db_session.commit()
    disabled = json.loads(await _workspace_agent_handler(
        **common, params={"service_key": "research", "prompt": "Try again."},
    ))
    assert disabled["error"] == "workspace_service_agent_not_found"
    assert len(loop_calls) == 2

    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.models.agent_delegation_session import AgentContextArchive
    main_id, archive_id = generate_ulid(), generate_ulid()
    db_session.add(RuntimeRun(id=main_id, root_run_id=main_id, entity_id=workspace["entity_id"],
                             user_id="delegate-user", conversation_id=conversation_id, status="completed",
                             context_state={"checkpoint": "private checkpoint"}))
    await db_session.flush()
    db_session.add(AgentContextArchive(id=archive_id, runtime_run_id=main_id, run_id=main_id,
                                     digest="a" * 64, messages=[{"role": "user", "content": "private"}], categories=[]))
    await db_session.commit()
    from packages.core.services.conversation_lifecycle import delete_conversation
    assert await delete_conversation(db_session, conversation_id, workspace["entity_id"])
    assert not (await db_session.execute(
        select(AgentDelegationSession).where(AgentDelegationSession.conversation_id == conversation_id)
    )).scalars().all()
    assert (await db_session.execute(select(AgentContextArchive).where(AgentContextArchive.id == archive_id))).scalar_one_or_none() is None
    assert (await db_session.execute(select(RuntimeRun.context_state).where(RuntimeRun.id == main_id))).scalar_one() is None


@pytest.mark.asyncio
async def test_session_claim_serializes_concurrent_turns_and_isolates_users(db_session):
    from packages.core.database import async_session
    from packages.core.services.agent_delegation_session import (
        DelegationSessionBusy, claim_delegation_session, finish_delegation_session,
    )

    scope = dict(
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        conversation_id=generate_ulid(), user_id=generate_ulid(),
        subscription=SimpleNamespace(id=generate_ulid(), agent_id=generate_ulid(), service_key="research"),
    )

    async def claim():
        async with async_session() as db:
            try:
                return await claim_delegation_session(db, **scope)
            except DelegationSessionBusy:
                return None

    results = await asyncio.gather(claim(), claim())
    assert sum(item is not None for item in results) == 1
    session, run_id, history = next(item for item in results if item is not None)
    assert history == []
    await finish_delegation_session(
        db_session, session_id=session.id, run_id=run_id,
        messages=[{"role": "assistant", "content": "Private result"}], context_usage={},
    )

    for changed_scope in (
        {**scope, "user_id": generate_ulid()},
        {**scope, "conversation_id": generate_ulid()},
        {**scope, "subscription": SimpleNamespace(id=generate_ulid(), agent_id=scope["subscription"].agent_id, service_key="research")},
    ):
        other, _, other_history = await claim_delegation_session(db_session, **changed_scope)
        assert other.id != session.id
        assert other_history == []

    with pytest.raises(ValueError, match="scope"):
        await claim_delegation_session(db_session, **scope, session_id=generate_ulid())
    await db_session.rollback()
    with pytest.raises(ValueError, match="scope"):
        await claim_delegation_session(db_session, **{**scope, "entity_id": generate_ulid()})


@pytest.mark.asyncio
async def test_expired_turn_requires_effect_check_and_cannot_overwrite_new_turn(db_session):
    from packages.core.services.agent_delegation_session import (
        DelegationSessionInterrupted, claim_delegation_session,
        finish_delegation_session, renew_delegation_session,
    )

    scope = dict(
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        conversation_id=generate_ulid(), user_id=generate_ulid(),
        subscription=SimpleNamespace(id=generate_ulid(), agent_id=generate_ulid(), service_key="research"),
    )
    session, old_run_id, _ = await claim_delegation_session(db_session, **scope)
    session_id = session.id
    session.active_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(DelegationSessionInterrupted):
        await claim_delegation_session(db_session, **scope)
    await db_session.rollback()
    continued, new_run_id, _ = await claim_delegation_session(
        db_session, **scope, continue_interrupted=True,
    )
    assert continued.id == session_id
    assert new_run_id != old_run_id
    await renew_delegation_session(db_session, session_id=session_id, run_id=new_run_id)
    with pytest.raises(RuntimeError, match="no longer active"):
        await finish_delegation_session(
            db_session, session_id=session_id, run_id=old_run_id,
            messages=[{"role": "assistant", "content": "Stale result"}], context_usage={},
        )


def test_context_window_reports_occupancy_instead_of_cumulative_usage():
    from packages.core.ai.runtime.context_window import runtime_context_window_usage

    usage = runtime_context_window_usage(
        system_prompt="Agent instructions", messages=[{"role": "user", "content": "Hello"}],
        tools=[
            {"type": "function", "function": {"name": "mcp__search__query"}},
            {"type": "function", "function": {"name": "read_file"}},
        ], context_window_tokens=128_000, output_reserve_tokens=16_384,
    )
    assert usage["mcp_tools_tokens"] > 0
    assert usage["system_tools_tokens"] > 0
    assert usage["occupied_tokens"] == sum(usage[k] for k in (
        "messages_tokens", "mcp_tools_tokens", "system_tools_tokens", "system_prompt_tokens",
    ))
    assert usage["occupied_tokens"] + usage["free_tokens"] + usage["autocompact_buffer_tokens"] + usage["output_reserve_tokens"] == 128_000


@pytest.mark.asyncio
async def test_context_archive_keeps_originals_private_and_outside_active_window(db_session):
    from packages.core.models.agent_delegation_session import AgentContextArchive
    from packages.core.services.agent_delegation_session import (
        archive_delegation_context, claim_delegation_session, finish_delegation_session,
        read_delegation_context_archive,
    )
    scope = dict(
        entity_id=generate_ulid(), workspace_id=generate_ulid(),
        conversation_id=generate_ulid(), user_id=generate_ulid(),
        subscription=SimpleNamespace(id=generate_ulid(), agent_id=generate_ulid(), service_key="research"),
    )
    session, run_id, _ = await claim_delegation_session(db_session, **scope)
    history = [
        {"role": "system", "content": "Current rules"},
        {"role": "user", "content": "Find a source"},
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "invoke_skill"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "Full skill result " * 1000},
    ]
    archive_id = await archive_delegation_context(db_session, session_id=session.id, run_id=run_id, messages=history)
    assert await archive_delegation_context(db_session, session_id=session.id, run_id=run_id, messages=history) == archive_id
    compacted = [{"role": "user", "content": "[Earlier conversation compacted: 3 messages]\nFinding saved."}]
    await finish_delegation_session(
        db_session, session_id=session.id, run_id=run_id, messages=compacted, context_usage={},
    )
    session, _, resumed = await claim_delegation_session(db_session, **scope)
    assert resumed == compacted
    assert session.context_state["archive_count"] == 1
    assert session.context_state["history"][0]["category"] == "summary"
    read_scope = {k: v for k, v in scope.items() if k != "subscription"}
    read_scope.update(session_id=session.id, agent_subscription_id=scope["subscription"].id)
    archive = await read_delegation_context_archive(db_session, archive_id=archive_id, **read_scope)
    assert archive.messages == history[1:]
    assert archive.categories[-1]["category"] == "skill_content"
    for field in ("entity_id", "workspace_id", "conversation_id", "user_id", "agent_subscription_id", "session_id"):
        assert await read_delegation_context_archive(
            db_session, archive_id=archive_id, **{**read_scope, field: generate_ulid()},
        ) is None
    await db_session.execute(delete(AgentDelegationSession).where(AgentDelegationSession.id == session.id))
    await db_session.commit()
    assert not (await db_session.execute(select(AgentContextArchive.id).where(AgentContextArchive.session_id == session.id))).scalars().all()


@pytest.mark.asyncio
async def test_main_context_archive_fences_writers_and_restores_only_same_scope(db_session):
    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.services.runtime_context_archive import runtime_context_archive_callbacks
    from packages.core.ai.runtime.context_compaction import CHECKPOINT_PREFIX
    from packages.core.models.agent_delegation_session import AgentContextArchive
    from sqlalchemy import update

    scope = dict(entity_id="entity", user_id="owner", conversation_id="conversation",
                 workspace_id="workspace", agent_id="agent")
    run_id = generate_ulid()
    run = RuntimeRun(id=run_id, root_run_id=run_id, **scope, status="running",
                     lease_owner="worker-one", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db_session.add(run)
    await db_session.commit()
    archive, read, checkpoint, previous = await runtime_context_archive_callbacks(run_id=run_id, **scope)
    assert previous is None
    history = [{"role": "system", "content": "Rules are rebuilt"},
               {"role": "tool", "tool_call_id": "receipt-id", "content": "source evidence " * 3000}]
    archive_id = await archive(history)
    assert await archive(history) == archive_id
    first = json.loads(await read({"archive_id": archive_id, "limit": 500}))
    assert first["next_offset"] == 500
    assert "Rules are rebuilt" not in first["content"]
    second = json.loads(await read({"archive_id": archive_id, "offset": 500, "limit": 500}))
    assert second["offset"] == 500 and second["next_offset"] == 1000
    saved = {"role": "user", "content": CHECKPOINT_PREFIX + "\nCompleted step; needs verification."}
    await checkpoint([history[0], saved])
    await db_session.execute(update(RuntimeRun).where(RuntimeRun.id == run_id).values(status="completed"))
    await db_session.commit()
    next_id = generate_ulid()
    db_session.add(RuntimeRun(id=next_id, root_run_id=next_id, **scope, status="running",
                             lease_owner="worker-two", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await db_session.commit()
    next_archive, next_read, next_checkpoint, previous = await runtime_context_archive_callbacks(run_id=next_id, **scope)
    assert previous == saved
    assert json.loads(await next_read({"archive_id": archive_id}))["archive_id"] == archive_id
    with pytest.raises(RuntimeError, match="no longer owns"):
        await archive(history + [{"role": "user", "content": "stale writer"}])
    for key in scope:
        with pytest.raises(ValueError, match="scope"):
            await runtime_context_archive_callbacks(run_id=next_id, **{**scope, key: "foreign"})
    foreign_id = generate_ulid()
    db_session.add(RuntimeRun(id=foreign_id, root_run_id=foreign_id, **{**scope, "user_id": "foreign", "conversation_id": "foreign"},
                             status="running", lease_owner="foreign", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await db_session.commit()
    _, foreign_read, _, foreign_previous = await runtime_context_archive_callbacks(
        run_id=foreign_id, **{**scope, "user_id": "foreign", "conversation_id": "foreign"})
    assert foreign_previous is None
    assert "error" in json.loads(await foreign_read({"archive_id": archive_id}))
    await db_session.execute(update(RuntimeRun).where(RuntimeRun.id == next_id).values(version=2, lease_owner="replacement"))
    await db_session.commit()
    with pytest.raises(RuntimeError, match="no longer owns"):
        await next_checkpoint([saved])
    await db_session.execute(delete(RuntimeRun).where(RuntimeRun.id == run_id))
    await db_session.commit()
    assert (await db_session.execute(select(AgentContextArchive).where(AgentContextArchive.id == archive_id))).scalar_one_or_none() is None
