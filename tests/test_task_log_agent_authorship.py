"""Regression: agent-authored task logs/comments must carry the running
agent's identity.

The workspace agent runtime used to stamp a literal ``"workspace-agent"`` as
the author of every task log/comment, so the activity UI showed a generic
label instead of the specific persona (e.g. "X Growth Analyst") that actually
did the work. ``agent_log_authorship`` now resolves the running agent into
``(created_by, metadata, actor)`` where ``metadata`` carries ``agent_id``
(+ name), which the task-log serializer surfaces as ``author_agent_id`` /
``author_agent_name`` for the frontend to resolve, and ``actor`` says which
kind of thing acted (see TaskActor).

With no agent id at all there is still no "generic agent": the master agent
is the one running as the workspace agent, so that is what it resolves to.
"""

from __future__ import annotations

import pytest

from packages.core.constants.agents import MANOR_AGENT_NAME
from packages.core.constants.task_actors import TaskActor
from packages.core.services import agent_service, task_service


@pytest.fixture
def fake_agent(monkeypatch):
    async def _get_agent(db, agent_id):
        if agent_id == "01AGENTX":
            return {"id": agent_id, "name": "X Growth Analyst"}
        return None

    # agent_log_authorship imports get_agent lazily from this module.
    monkeypatch.setattr(agent_service, "get_agent", _get_agent)


async def test_no_agent_resolves_to_the_master_agent(fake_agent):
    """Work does not run un-owned. No agent id means the master agent ran it,
    which is a determinate answer — not a placeholder."""
    created_by, meta, actor = await task_service.agent_log_authorship(object(), None)
    assert created_by == MANOR_AGENT_NAME
    assert meta is None
    assert actor is TaskActor.MANOR


async def test_no_agent_uses_human_fallback(fake_agent):
    created_by, meta, actor = await task_service.agent_log_authorship(
        object(), "", fallback="user-123",
    )
    assert created_by == "user-123"
    assert meta is None
    assert actor is TaskActor.USER


async def test_a_legacy_placeholder_fallback_is_not_mistaken_for_a_person(fake_agent):
    """Old call sites passed a placeholder as the fallback. It names an agent
    that failed to be recorded, never a user."""
    created_by, meta, actor = await task_service.agent_log_authorship(
        object(), None, fallback="workspace-agent",
    )
    assert created_by == MANOR_AGENT_NAME
    assert actor is TaskActor.MANOR


async def test_agent_stamps_id_and_name(fake_agent):
    created_by, meta, actor = await task_service.agent_log_authorship(
        object(),
        "01AGENTX",
        fallback="user-123",
    )
    assert created_by == "01AGENTX"
    assert meta == {"agent_id": "01AGENTX", "agent_name": "X Growth Analyst"}
    assert actor is TaskActor.AGENT


async def test_unknown_agent_still_stamps_id(fake_agent):
    # Even if the name can't be resolved server-side, stamping the id lets the
    # frontend resolve it against the workspace's agent list.
    created_by, meta, actor = await task_service.agent_log_authorship(object(), "01MISSING")
    assert created_by == "01MISSING"
    assert meta == {"agent_id": "01MISSING"}
    assert actor is TaskActor.AGENT


async def test_task_comment_routes_and_attributes_to_task_agent(monkeypatch):
    """Regression: task-comment replies were run by (and stamped as) the
    generic workspace agent. They must now be handled by the task's assigned
    agent and attributed to it, so the thread shows the specific persona that
    asked/answered/errored — not a blanket "Workspace Agent"."""
    import types

    from packages.core.services import workspace_runtime

    task = types.SimpleNamespace(
        id="T1",
        workspace_id="W1",
        title="t",
        conversation_id=None,
        agent_id="01AGENTX",
        agent_type=None,
    )

    class _FakeDB:
        async def commit(self):
            return None

    class _SessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *a):
            return False

    captured: dict = {}
    logs: list = []

    async def _load_task(db, **kw):
        return task

    async def _ensure_conv(db, **kw):
        return types.SimpleNamespace(id="C1")

    async def _add_message(*a, **k):
        return None

    async def _run_turn(message, conversation_id, **kw):
        captured["turn_agent_id"] = kw.get("agent_id")
        return {"content": "answer", "message_id": "M1", "tool_calls_made": []}

    async def _add_task_log(db, task_id, log_type, content, *, actor, created_by="system", metadata=None):
        logs.append({"log_type": log_type, "created_by": created_by, "metadata": metadata, "actor": actor})
        return types.SimpleNamespace(id="L1")

    async def _authorship(db, agent_id, *, fallback=None):
        if not agent_id:
            return (fallback or MANOR_AGENT_NAME), None, TaskActor.MANOR
        return agent_id, {"agent_id": agent_id, "agent_name": "X Growth Analyst"}, TaskActor.AGENT

    import packages.core.database as database
    import packages.core.services.conversation_messages as conversation_messages
    import packages.core.ai.runtime as runtime_pkg

    monkeypatch.setattr(workspace_runtime, "_load_task_for_runtime", _load_task)
    monkeypatch.setattr(workspace_runtime, "ensure_workspace_task_conversation", _ensure_conv)
    monkeypatch.setattr(database, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(conversation_messages, "add_message", _add_message)
    monkeypatch.setattr(runtime_pkg, "runtime_run_chat_turn", _run_turn)
    monkeypatch.setattr(task_service, "add_task_log", _add_task_log)
    monkeypatch.setattr(task_service, "agent_log_authorship", _authorship)

    await workspace_runtime.process_workspace_task_comment(
        task_id="T1",
        entity_id="E1",
        user_id=None,
        author_label="user",
        comment="hello?",
    )

    # the task's agent (not the generic workspace agent) handled the turn
    assert captured["turn_agent_id"] == "01AGENTX"
    # and the reply is attributed to that specific agent
    resp = [log for log in logs if log["log_type"] == "workspace_agent_response"]
    assert resp, "expected a workspace_agent_response log"
    assert resp[0]["created_by"] == "01AGENTX"
    assert resp[0]["metadata"]["agent_id"] == "01AGENTX"
