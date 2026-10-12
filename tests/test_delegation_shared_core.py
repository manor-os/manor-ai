"""One delegation core for every host.

Chat runs delegated agents nested in the turn; durable Task children will run
them as their own runs. Both build the child, bill it, shape its result and
assemble the batch result with the same functions, so the parent model gets
byte-identical tool results whichever host ran the child.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as delegation


KEYS = ("writer", "analyst", "editor", "asker", "approver", "approverfail", "crasher")
SUBS = {
    key: SimpleNamespace(id=f"sub-{key}", agent_id=f"agent-{key}", service_key=key, custom_prompt=None, name=None)
    for key in KEYS
}
AGENTS = {key: SimpleNamespace(name=key.title()) for key in SUBS}
TARGETS = [
    {"label": key.title(), "agent_subscription_id": sub.id, "agent_id": sub.agent_id, "service_key": key, "summary": ""}
    for key, sub in SUBS.items()
]
_APPROVAL = {"__hitl__": True, "error": "approval_required", "approval_token": "tok-1", "hitl": {"prompt": "Publish?"}}
RECEIPTS = {
    "writer": [("web_search", {"status": "completed"}, {"q": "launch"})],
    "analyst": [("write_file", {"error": "write_failed"}, {"path": "/a.md"})],
    "editor": [],
    "asker": [("write_file", {"status": "completed", "path": "/x.md"}, {"path": "/x.md"})],
    "approver": [("publish", _APPROVAL, {"post": 1})],
    "approverfail": [("publish", _APPROVAL, {"post": 1})],
    "crasher": [("write_file", {"status": "completed"}, {"path": "/c.md"})],
}


def _loop_result(key: str) -> SimpleNamespace:
    """What the child loop of each agent returns (the same for every host)."""
    if key == "crasher":
        raise RuntimeError("child loop crashed")
    if key in ("approver", "approverfail"):
        return SimpleNamespace(
            content="Waiting for approval", messages=[{"role": "user", "content": "task"}], rounds=1,
            tool_calls_made=["publish"], usage={"prompt": 10}, stop_reason="hitl_required", error=None,
            error_detail=None, control={"tool_call_id": "call-1"}, tool_evidence=None,
        )
    if key == "asker":
        return SimpleNamespace(
            content="", messages=[{"role": "user", "content": "task"}], rounds=2,
            tool_calls_made=["write_file", "request_input"], usage={"prompt": 5}, stop_reason="waiting_resource",
            error=None, error_detail=None, tool_evidence=None,
            control={"kind": "waiting_human", "payload": {"question": "Which tone?", "options": ["warm", "dry"]}},
        )
    content = {"writer": "w" * 30_000, "analyst": "a" * 18_000, "editor": "edited 编辑"}[key]
    return SimpleNamespace(
        content=content,
        messages=[{"role": "user", "content": "task"}, {"role": "assistant", "content": content}],
        rounds=3,
        tool_calls_made=[name for name, _receipt, _args in RECEIPTS[key]] + (["read_file"] if key == "editor" else []),
        usage={"prompt": 120, "completion": 30},
        stop_reason="error" if key == "editor" else "completed",
        error="lost reply" if key == "editor" else None,
        error_detail=None,
        control={"do_not_replay": True, "effect_state": "unconfirmed"} if key == "editor" else {},
        tool_evidence=None,
    )


def install_chat_host(monkeypatch) -> None:
    """The chat path with everything below the delegation module faked.

    Uses only names that also exist before the shared core was extracted, so
    the frozen outputs below can be regenerated from any revision.
    """
    from packages.core import database
    from packages.core.ai import context
    from packages.core.ai.runtime import harness
    from packages.core.services import agent_delegation_session as sessions

    @asynccontextmanager
    async def session():
        yield AsyncMock()

    async def resolve(db, *, entity_id, workspace_id, raw_params):
        for key, sub in SUBS.items():
            if raw_params.get("agent_subscription_id") == sub.id:
                return sub, AGENTS[key], [], None
        return None, None, [], "No active workspace service agent matched agent_subscription_id."

    async def targets(db, **kwargs):
        return TARGETS

    async def claim(db, *, subscription, **kwargs):
        return SimpleNamespace(id=f"session-{subscription.id}"), f"run-{subscription.id}", []

    async def build_context(db, **kwargs):
        return SimpleNamespace(
            runtime_envelope=None, system_prompt="System", tools=[], task_id=None,
            tool_profile="workspace_agent", allowed_tool_names=[], model="test-model", llm_metadata=None,
        )

    async def persist(**kwargs):
        return kwargs["sub"].id == "sub-approver"

    async def loop(**kwargs):
        key = kwargs["agent_id"].removeprefix("agent-")
        for name, receipt, args in RECEIPTS[key]:
            kwargs["on_tool_start"](name, args)
            kwargs["on_tool_end"](name, json.dumps(receipt), 1.0, args)
        return _loop_result(key)

    monkeypatch.setattr(database, "async_session", session)
    monkeypatch.setattr(delegation, "_resolve_workspace_service_agent", resolve)
    monkeypatch.setattr(delegation, "runtime_workspace_delegation_targets", targets)
    monkeypatch.setattr(delegation, "_persist_delegation_resume", persist)
    monkeypatch.setattr(delegation, "PARALLEL_DELEGATION_STAGGER_SECONDS", 0)
    monkeypatch.setattr(sessions, "claim_delegation_session", claim)
    monkeypatch.setattr(sessions, "finish_delegation_session", AsyncMock())
    monkeypatch.setattr(sessions, "renew_delegation_session", AsyncMock())
    monkeypatch.setattr(sessions, "interrupt_delegation_session", AsyncMock())
    monkeypatch.setattr(context, "build_agent_context", build_context)
    monkeypatch.setattr(harness, "runtime_execute_chat_agent_loop", loop)


@pytest.fixture
def chat_host(monkeypatch):
    install_chat_host(monkeypatch)


async def chat(keys: list[str]) -> str:
    from packages.core.ai.tools import workspace_agent_tools

    return await workspace_agent_tools._delegate_to_agent_handler(
        entity_id="entity", user_id="user", workspace_id="workspace", conversation_id="conv",
        tasks=[{"agent": key.title(), "task": f"work {key}"} for key in keys],
    )


def fingerprint(raw: str) -> tuple[int, str]:
    """Length and sha256 of a tool result; a pause's random resume token is masked."""
    text = re.sub(r'("resume_token": ")[^"]+', r"\1*", str(raw))
    return len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


# The chat tool result of each case as (length, sha256), recorded from the
# pre-extraction chat code (origin/dev 35893e95d) with install_chat_host. A
# change here is a change in what the parent model sees: update it only on
# purpose, in the same commit as the change.
FROZEN_CHAT_RESULTS: dict[str, tuple[int, str]] = {
    "writer": (30410, "77b8bef796089ceccc74161356f6981b13965a6432546b89345e2cdb7da61788"),
    "editor": (634, "d96865c2ec0461950b97f4d5dee38832dcbf1734c09edda4b89cc12ab4dadb22"),
    "asker": (886, "2a508ae18dd4ffe3f00e939f806520fdc07969767fea1926bb0c751f90292f8b"),
    "approver": (107, "801bf8f47adcddf2dde9d3b78e9a990345ae6b8e6958fb9c833e0a1405e0945a"),
    "approverfail": (499, "036038f11aa88a9bf2a5f88e7ca241be2af362cb2472aa841062bf95566bad00"),
    "crasher": (81, "1b27e0eda245bb6eba5b4ef8cd59a10d8090810b0f2a722e2485ccdf1c6de3f3"),
    "writer+analyst+editor": (39800, "dc43b80b985782558dea8306e53970987050514b22cf202dc165f277a845ff99"),
    "writer+asker": (31469, "e61bb7562052d2eb844b6b59abe395cf8edaf2717ce6423e0895cd907a819439"),
    "writer+approver": (30671, "84db367c7510e7223d69d3caad361aa741e915bc9ed1995fa2052960fdf43537"),
    "analyst+crasher": (18659, "fed611ee82beb8c50e1fed0c39b2988de57fff6b23ac927cb5bbd0cd5342b8ed"),
}
GOLDEN_CASES = [
    ["writer"], ["editor"], ["asker"], ["approver"], ["approverfail"], ["crasher"],
    ["writer", "analyst", "editor"], ["writer", "asker"], ["writer", "approver"], ["analyst", "crasher"],
]


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", GOLDEN_CASES, ids="+".join)
async def test_chat_tool_results_match_the_frozen_outputs(chat_host, keys):
    assert fingerprint(await chat(keys)) == FROZEN_CHAT_RESULTS["+".join(keys)]


def _shared_core(keys: list[str]) -> str:
    """The same children through the shared core alone, as another host would."""
    payloads = []
    for key in keys:
        child = delegation.runtime_delegated_child_result(
            _loop_result(key),
            run_id=f"run-{SUBS[key].id}",
            session_id=f"session-{SUBS[key].id}",
            idempotency_key=None,
            sub=SUBS[key],
            agent=AGENTS[key],
            entity_id="entity",
            workspace_id="workspace",
            task_id=None,
            tool_results=[
                delegation.runtime_delegated_tool_outcome(name, json.dumps(receipt))
                for name, receipt, _args in RECEIPTS[key]
            ],
        )
        payloads.append(child.payload)
    return delegation.runtime_delegation_batch_result([{"agent": key.title()} for key in keys], payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [["writer"], ["editor"], ["writer", "analyst", "editor"]])
async def test_chat_and_the_shared_core_give_byte_identical_tool_results(chat_host, keys):
    result = await chat(keys)
    assert result == _shared_core(keys)
    payload = json.loads(result)
    if len(keys) == 1:
        assert payload["delegated"] is True and "parallel" not in payload
    else:
        assert payload["parallel"] is True and payload["total"] == len(keys)
        # The budget was exceeded: the longest answer was cut, readable by run_id.
        assert any("read_delegation_result" in entry["content"] for entry in payload["results"])
    if "editor" in keys:
        editor = payload if len(keys) == 1 else payload["results"][keys.index("editor")]
        assert editor["session_status"] == "interrupted" and editor["do_not_replay"] is True


def test_a_failed_approval_store_fails_the_child_like_chat_did():
    result = SimpleNamespace(
        content="Waiting for approval", messages=[], rounds=1, tool_calls_made=["publish"], usage={},
        stop_reason="hitl_required", error=None, error_detail=None, control={}, tool_evidence=None,
    )
    child = delegation.runtime_delegated_child_result(
        result, run_id="run", session_id="", idempotency_key=None, sub=SUBS["writer"], agent=AGENTS["writer"],
        entity_id="e", workspace_id="w", task_id=None, tool_results=[],
        forced_error="delegated_approval_resume_unavailable",
    )
    assert child.status == "failed" and child.error == "delegated_approval_resume_unavailable"
    assert child.payload["status"] == "failed" and child.payload["session_id"] == ""


def test_a_durable_child_parks_on_an_unconfirmed_effect_without_a_session():
    child = delegation.runtime_delegated_child_result(
        _loop_result("editor"), run_id="child-1", session_id=None, idempotency_key=None,
        sub=SUBS["editor"], agent=AGENTS["editor"], entity_id="e", workspace_id="w", task_id="task",
        tool_results=[], parks_when_unconfirmed=True, result_stored=True,
    )
    assert child.interrupted and child.payload["session_status"] == "interrupted"
    assert child.payload["session_id"] is None and child.payload["result_stored"] is True
    # Without a session and without parking, the same result is not interrupted.
    unparked = delegation.runtime_delegated_child_result(
        _loop_result("editor"), run_id="child-1", session_id=None, idempotency_key=None,
        sub=SUBS["editor"], agent=AGENTS["editor"], entity_id="e", workspace_id="w", task_id="task",
        tool_results=[],
    )
    assert not unparked.interrupted and "session_status" not in unparked.payload


def test_a_host_ended_child_has_the_same_shape_as_a_finished_one():
    finished = delegation.runtime_delegated_child_result(
        _loop_result("editor"), run_id="child-1", session_id=None, idempotency_key="idem",
        sub=SUBS["editor"], agent=AGENTS["editor"], entity_id="e", workspace_id="w", task_id="task",
        tool_results=[], parks_when_unconfirmed=True, result_stored=True,
    ).payload
    repair = delegation.runtime_delegated_child_failure(
        run_id="child-1", sub=SUBS["editor"], agent=AGENTS["editor"], status="failed",
        error="agent_child_repair_required", message="The agent stopped after repeated crashes.",
        idempotency_key="idem", rounds=4, tool_calls_made=["write_file"],
        replay_fields={"do_not_replay": True, "effect_state": "unconfirmed"}, result_stored=True,
    )
    # Same keys in the same order: the parent and the batch envelope read one shape.
    assert list(repair) == list(finished)
    assert repair["delegated"] is True and repair["run_id"] == "child-1"
    assert repair["status"] == "failed" and repair["stop_reason"] == "error"
    assert repair["session_status"] == "interrupted" and repair["do_not_replay"] is True
    cancelled = delegation.runtime_delegated_child_failure(
        run_id="child-2", sub=SUBS["writer"], agent=AGENTS["writer"], status="cancelled", error="task_cancelled",
    )
    assert cancelled["stop_reason"] == "cancelled" and "session_status" not in cancelled
    batch = json.loads(delegation.runtime_delegation_batch_result(
        [{"agent": "Editor"}, {"agent": "Writer"}], [repair, cancelled],
    ))
    assert [entry["delegation_status"] for entry in batch["results"]] == ["failed", "cancelled"]
    assert [entry["run_id"] for entry in batch["results"]] == ["child-1", "child-2"]
    with pytest.raises(ValueError):
        delegation.runtime_delegated_child_failure(
            run_id="child-3", sub=SUBS["writer"], agent=AGENTS["writer"], status="completed", error="x",
        )


def test_a_batch_result_needs_a_child_result():
    with pytest.raises(ValueError):
        delegation.runtime_delegation_batch_result([], [])


@pytest.mark.parametrize("stored,readable", [({"session_id": "s"}, True), ({"result_stored": True}, True), ({}, False)])
def test_a_cut_answer_points_to_storage_only_when_it_was_kept(stored, readable):
    raw = json.dumps({"delegated": True, "run_id": "run-1", "content": "y" * 90_000, **stored})
    content = json.loads(delegation.runtime_delegation_tool_result(raw))["content"]
    assert ("read_delegation_result, run_id run-1" in content) is readable
    assert ("the rest was not kept" in content) is (not readable)


@pytest.mark.asyncio
async def test_a_pinned_batch_resolves_every_task_before_anything_runs(chat_host):
    batch = await delegation.runtime_delegation_batch(
        entity_id="entity", workspace_id="workspace",
        tasks=[{"agent": "Writer", "task": "draft"}, {"agent": "Analyst", "task": "numbers", "continue_interrupted": True}],
        shared={"_agent_id_from_context": "agent-editor", "prompt": "not a task"},
    )
    assert batch.error is None and batch.shared == {"_agent_id_from_context": "agent-editor"}
    budget = delegation.runtime_delegation_answer_budget(2)
    assert batch.items == [
        {"agent": "Writer", "agent_id": "agent-writer", "service_key": "writer",
         "params": {"prompt": "draft", "agent_subscription_id": "sub-writer", "answer_max_chars": budget}},
        {"agent": "Analyst", "agent_id": "agent-analyst", "service_key": "analyst", "params": {
            "prompt": "numbers", "continue_interrupted": True, "agent_subscription_id": "sub-analyst", "answer_max_chars": budget,
        }},
    ]
    single = await delegation.runtime_delegation_batch(
        entity_id="entity", workspace_id="workspace", tasks=[{"agent": "Writer", "task": "draft"}], shared={},
    )
    assert single.items[0]["params"]["answer_max_chars"] == delegation.runtime_delegation_answer_budget(1)
    # Chat leaves pinning to its own runners.
    unpinned = await delegation.runtime_delegation_batch(
        entity_id="entity", workspace_id="workspace", tasks=[{"agent": "Writer", "task": "draft"}], shared={},
        pin_targets=False,
    )
    assert unpinned.items == [{"agent": "Writer", "params": {"prompt": "draft", "agent_subscription_id": "sub-writer"}}]
    for tasks, error in (
        ([{"agent": "Writer", "task": "a"}, {"agent": "Writer", "task": "b"}], "duplicate_delegation_target"),
        ([{"agent": "Nobody", "task": "a"}], "unknown_agent"),
        ([{"agent": "Writer", "task": " "}], "task is required"),
        ([{"agent": "Writer", "task": str(i)} for i in range(21)], "invalid_tasks"),
        ([{"agent_subscription_id": "sub-gone", "task": "a"}], "workspace_service_agent_not_found"),
    ):
        refused = await delegation.runtime_delegation_batch(
            entity_id="entity", workspace_id="workspace", tasks=tasks, shared={},
        )
        assert json.loads(refused.error)["error"] == error and refused.items == []


def test_delegate_to_agent_arguments_normalise_to_tasks_and_call_context():
    tasks, shared = delegation.runtime_delegation_tool_arguments(
        {"agent": "Writer", "task": "draft", "stray": 1, "_agent_id_from_context": "agent-1"},
    )
    assert tasks == [{"agent": "Writer", "task": "draft"}] and shared == {"_agent_id_from_context": "agent-1"}
    tasks, _shared = delegation.runtime_delegation_tool_arguments(
        {"tasks": [{"agent": "A", "task": "x", "session_id": "s"}, "bad"]},
    )
    assert tasks == [{"agent": "A", "task": "x"}, "bad"]


def test_child_ledger_items_are_run_prefixed():
    state = {"observations": [
        {"call_id": "c1", "tool": "write_file", "effect_state": "confirmed"},
        {"call_id": "c2", "parent_call_id": "c1", "tool": "read_file", "effect_state": "none"},
    ]}
    items = delegation.runtime_child_ledger_items("run-9", state)
    assert [item["call_id"] for item in items] == ["run-9/c1", "run-9/c2"]
    assert items[1]["parent_call_id"] == "run-9/c1"
    assert delegation.runtime_child_ledger_items("run-9", None) == []


def test_the_coordinator_retry_answers_interrupted_calls_through_the_shared_helper():
    from packages.core.ai.runtime.interrupted_tool_calls import runtime_answer_interrupted_calls
    from packages.core.services import task_coordinator_wakeup

    assert task_coordinator_wakeup._answer_interrupted_calls is runtime_answer_interrupted_calls
    messages = runtime_answer_interrupted_calls({"messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "write_file", "arguments": "{}"}},
            {"id": "b", "function": {"name": "wait_until", "arguments": "{}"}},
        ]},
    ], "runtime_checkpoint": {"pending_tool_call": {"id": "b"}}})
    answers = {message["tool_call_id"]: json.loads(message["content"])["executed"] for message in messages[1:]}
    assert answers == {"a": "unknown", "b": "no"}
