"""Durable delegation (W4a PR6): a Task agent loop's delegated agents as child runs.

These run the real AgentLoop for the root run and for every child run, the
real wake/claim/checkpoint storage, the real park, settlement, sweeps and
Task state machine against the test database. Only the model rounds, the
Workspace/agent lookups, the agent contexts and the billing callbacks are
faked. The ``task_agent_durable_delegation`` flag is pinned on per test (the
default is off, see ``test_the_flag_is_registered_off``).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import workspace_delegation_actions as delegation_actions
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task, TaskLog
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.task_run_journal import TaskRunJournal
from packages.core.services import durable_run
from packages.core.services import feature_flags
from packages.core.services import task_agent_child_settlement as settlement
from packages.core.services import task_agent_delegation as delegation
from packages.core.services.task_agent_child_wakeup import (
    TASK_AGENT_CHILD_RUN_SPEC,
    task_agent_child_outbox_dedupe_key,
)
from packages.core.services.task_coordinator_wakeup import (
    ensure_task_coordinator_run,
    task_coordinator_outbox_dedupe_key,
)
from packages.core.services.task_execution import task_execution_intent_generation
from packages.core.tasks import runtime_tasks
from packages.core.workers import task_agent_child_loop as child_worker
from packages.core.workers import task_agent_loop as worker
from packages.core.workers import task_run_host

from test_task_agent_loop import (
    COMPLETE_ARGS,
    NOTE_TOOL,
    PLAN_ARGS,
    TWO_CRITERIA_PLAN,
    _agent_loop_run,
    _call,
    _install_host,
)


P = TaskCoordinatorPhase
KEYS = ("writer", "analyst", "editor")
GENERATE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_file",
        "description": "Write a document.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string"}}},
    },
}
DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_to_agent",
        "description": "Hand tasks to other agents.",
        "parameters": {"type": "object", "properties": {"tasks": {"type": "array"}}},
    },
}
ROOT_PROMPT = "AGENT PROMPT"


def _doc(key: str) -> dict:
    return {"created": True, "document": {
        "document_id": f"doc-{key}", "fs_path": f"Workspaces/w/{key}.md", "name": f"{key}.md",
    }}


def _tasks(*keys: str, **extra) -> dict:
    return {"tasks": [{"agent": key.title(), "task": f"Work for {key}", **extra} for key in keys]}


class Harness:
    """One Task's root agent loop plus its agents' child runs, wired to the test DB."""

    def __init__(self, db, monkeypatch, root, scripts):
        self.db = db
        self.monkeypatch = monkeypatch
        self.task, self.run, self.selection, self.event_id, self.generation = root
        # Plain ids: the rows themselves expire on a test's rollback.
        self.task_id, self.run_id = self.task.id, self.run.id
        self.scripts = scripts
        self.calls: dict[str, int] = {}
        self.billed: list[dict] = []
        self.child_loops: list[dict] = []
        self.child_contexts: list[dict] = []
        self.child_tools: dict = {}

    async def event(self, key: str) -> RuntimeOutboxEvent | None:
        return (await self.db.execute(
            select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.dedupe_key == key)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()

    async def root_wake(self) -> dict:
        event = await self.event(task_coordinator_outbox_dedupe_key(self.task_id))
        assert event is not None and event.delivered_at is None, "the root has a pending wake"
        return await worker.run_task_agent_loop_once(
            event_id=event.id, task_id=self.task_id, wake_generation=event.payload["wake_generation"],
        )

    async def child_wake(self, child_id: str) -> dict:
        event = await self.event(task_agent_child_outbox_dedupe_key(child_id))
        assert event is not None and event.delivered_at is None, "the child has a pending wake"
        return await child_worker.run_task_agent_child_once(
            event_id=event.id, task_id=self.task_id, child_id=child_id,
            wake_generation=event.payload["wake_generation"],
        )

    async def children(self) -> list[TaskAgentChildRun]:
        return list((await self.db.execute(
            select(TaskAgentChildRun).where(TaskAgentChildRun.task_id == self.task_id)
            .order_by(TaskAgentChildRun.created_at, TaskAgentChildRun.item_index)
            .execution_options(populate_existing=True)
        )).scalars().all())

    async def root(self) -> TaskCoordinatorRun:
        return (await self.db.execute(
            select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == self.run_id)
            .execution_options(populate_existing=True)
        )).scalar_one()

    async def fresh_task(self) -> Task:
        return (await self.db.execute(
            select(Task).where(Task.id == self.task_id).execution_options(populate_existing=True)
        )).scalar_one()

    async def journal(self, run_id: str) -> list[TaskRunJournal]:
        return list((await self.db.execute(
            select(TaskRunJournal).where(TaskRunJournal.run_id == run_id)
            .order_by(TaskRunJournal.seq).execution_options(populate_existing=True)
        )).scalars().all())

    async def run_children(self) -> list[dict]:
        """Every child wake that is due, until none is (children run one by one)."""

        outcomes = []
        for _ in range(20):
            due = [
                child for child in await self.children()
                if child.phase not in P.terminal()
                and (event := await self.event(task_agent_child_outbox_dedupe_key(child.id))) is not None
                and event.delivered_at is None
            ]
            if not due:
                return outcomes
            for child in due:
                outcomes.append(await self.child_wake(child.id))
        raise AssertionError("children never settled")


@pytest.fixture
def subs():
    return make_subs()


def make_subs() -> dict:
    """Three fake Workspace agents (subscription, agent) by service key."""

    return {
        key: (
            SimpleNamespace(
                id=generate_ulid(), agent_id=generate_ulid(), service_key=key,
                custom_prompt=None, name=None, status="active",
            ),
            SimpleNamespace(name=key.title()),
        )
        for key in KEYS
    }


@pytest.fixture
async def harness(db_session, monkeypatch, subs):
    root = await _agent_loop_run(db_session, monkeypatch)
    h = await build_harness(db_session, monkeypatch, subs, root)
    yield h
    await drop_harness_children(db_session, h)


async def build_harness(db_session, monkeypatch, subs, root, *, user_id: str = "user-1") -> Harness:
    """Wire one root run (task, run, selection, event id, generation) and its agents' child runs."""

    host = _install_host(monkeypatch, db_session, root[2])
    session_factory = host.session_factory
    scripts: dict[str, list] = {}
    h = Harness(db_session, monkeypatch, root, scripts)
    h.host = host
    h.subs = subs

    from packages.core import database
    from packages.core.ai import context as context_module
    from packages.core.services import usage_service

    # Durable delegation is pinned on for these runs (the flag's default is off).
    monkeypatch.setattr(delegation, "resolve_delegation_mode", AsyncMock(return_value="durable"))
    monkeypatch.setattr(database, "create_worker_session", lambda: session_factory)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def resolve(_db, *, entity_id, workspace_id, raw_params):
        for sub, agent in subs.values():
            if raw_params.get("agent_subscription_id") == sub.id:
                return sub, agent, [], None
        return None, None, [], "No active workspace service agent matched."

    async def targets(_db, **_kwargs):
        return [
            {"label": key.title(), "agent_subscription_id": sub.id, "agent_id": sub.agent_id,
             "service_key": key, "summary": ""}
            for key, (sub, _agent) in subs.items()
        ]

    monkeypatch.setattr(delegation_actions, "_resolve_workspace_service_agent", resolve)
    monkeypatch.setattr(delegation_actions, "runtime_workspace_delegation_targets", targets)

    async def child_context(_db, **kwargs):
        h.child_contexts.append(kwargs)
        key = next(key for key, (sub, _a) in subs.items() if sub.id == kwargs["agent_subscription_id"])
        return SimpleNamespace(
            runtime_envelope=None, system_prompt=f"CHILD:{key}", prompt_context=None,
            tools=[NOTE_TOOL, GENERATE_FILE_TOOL, DELEGATE_TOOL], task_id=None,
            tool_profile="workspace_agent", allowed_tool_names=None, model="test-model",
            llm_metadata=None,
        )

    monkeypatch.setattr(context_module, "build_agent_context", child_context)
    monkeypatch.setattr(child_worker, "_pinned_agent_active", AsyncMock(return_value=True))
    monkeypatch.setattr(child_worker, "resolve_task_run_user_id", AsyncMock(return_value=user_id))

    def billing(*, source_prefix, task_id=None, **_kwargs):
        async def reserve(*_args, **_kw):
            return None

        async def settle(*args, **_kw):
            h.billed.append({"source_prefix": source_prefix, "task_id": task_id, "usage": args[-1] if args else None})

        return reserve, settle

    monkeypatch.setattr(usage_service, "build_durable_llm_billing_callbacks", billing)

    async def child_loop(**kwargs):
        h.child_loops.append({**kwargs, "_delegation_depth": delegation_actions._delegation_depth.get()})
        handlers = kwargs["dynamic_tool_handlers"]

        async def executor(name, args):
            if name in handlers:
                return await handlers[name](args)
            if name == "record_note":
                return json.dumps({"ok": True, "note": args.get("text")})
            if name == "generate_file":
                return json.dumps(_doc(kwargs["system_prompt"].split(":", 1)[1]))
            if name in h.child_tools:
                return await h.child_tools[name](args)
            raise AssertionError(f"unexpected child tool {name}")

        from packages.core.ai.runtime.execution_nesting import runtime_nested_execution_scope
        from packages.core.ai.runtime.harness import runtime_run_state_round_observer

        evidence = kwargs["tool_evidence"]
        resume: dict = {}
        if kwargs.get("runtime_checkpoint"):
            resume["resume_checkpoint"] = kwargs["runtime_checkpoint"]
        run_state = kwargs.get("run_state")
        if run_state is not None:
            resume.update(run_state.resume_loop_kwargs())
            if run_state.tool_evidence:
                evidence.restore_checkpoint_state(run_state.tool_evidence)
        resume["on_round_end"] = runtime_run_state_round_observer(
            kwargs["on_round_end"], run_state_slots=kwargs.get("run_state_slots"),
        )
        keys = (
            "system_prompt", "user_message", "tools", "max_rounds", "on_round_end_strict",
            "on_llm_call_before", "on_llm_usage_settled", "runtime_cancel_checker", "initial_messages",
            "on_tool_end", "human_input_suspension", "hitl_resume_checkpoint", "final_response_contract",
            "turn_context", "forced_tool_calls",
        )
        with runtime_nested_execution_scope():
            result = await loop_module.agentic_loop(
                tool_executor=executor, tool_evidence=evidence, performance_run_id=kwargs["task_id"],
                **resume, **{key: kwargs[key] for key in keys if key in kwargs and key not in resume},
            )
        result.tool_evidence = evidence.checkpoint_state()
        return result

    monkeypatch.setattr(child_worker, "runtime_execute_chat_agent_loop", child_loop)

    async def model(messages, tools, **_kwargs):
        system = str(messages[0].get("content") or "")
        key = "root" if system.startswith(ROOT_PROMPT) else system.split(":", 1)[1].split()[0]
        h.calls[key] = h.calls.get(key, 0) + 1
        script = scripts[key]
        step = script[min(h.calls[key], len(script)) - 1]
        outcome = step(messages, tools) if callable(step) else step
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if isinstance(outcome, BaseException):
            raise outcome
        content, calls = outcome
        return content, calls, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    return h


async def drop_harness_children(db_session, h: Harness) -> None:
    """Teardown: only this test's own child rows (other tests' rows are theirs)."""

    await db_session.rollback()
    await db_session.execute(delete(TaskAgentChildRun).where(TaskAgentChildRun.task_id == h.task_id))
    await db_session.commit()


async def _fresh_child(h, child_id: str) -> TaskAgentChildRun:
    return (await h.db.execute(
        select(TaskAgentChildRun).where(TaskAgentChildRun.id == child_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


def _complete():
    return ("", [_call("done", "complete_task", COMPLETE_ARGS)])


def _child_done(key: str, *, effects: bool = True) -> list:
    steps = [("", [_call(f"{key}-file", "generate_file", {"kind": "document"})])] if effects else []
    return [*steps, (f"{key.title()} finished the work.", None)]


def _tool_result(messages: list[dict], call_id: str) -> str:
    return next(m["content"] for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


# ── the flag ────────────────────────────────────────────────────────────


def test_the_flag_is_registered_off():
    flag = next(flag for flag in feature_flags.KNOWN_FLAGS if flag.key == delegation.TASK_AGENT_DURABLE_DELEGATION_FLAG)
    assert flag.default_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "mode"), [(True, "durable"), (False, "inline"), (RuntimeError("x"), "inline")])
async def test_the_mode_comes_from_the_flag_and_fails_closed_to_inline(db_session, monkeypatch, flag, mode):
    seen = []

    async def is_enabled(_db, key, *, entity_id=None, user_id=None, fallback=False):
        seen.append((key, entity_id, user_id, fallback))
        if isinstance(flag, BaseException):
            raise flag
        return flag

    monkeypatch.setattr(feature_flags, "is_enabled", is_enabled)
    assert await delegation.resolve_delegation_mode(db_session, entity_id="e1", user_id="u1") == mode
    assert seen == [("task_agent_durable_delegation", "e1", "u1", False)]


@pytest.mark.asyncio
async def test_the_mode_is_frozen_at_the_first_wake_that_can_delegate(harness):
    h = harness
    h.scripts["root"] = [("", [_call("plan", "update_task_plan", PLAN_ARGS)]), _complete()]
    assert (await h.root_wake())["status"] == "completed"
    assert delegation.resolve_delegation_mode.await_count == 1
    assert (await h.root()).checkpoint["delegation_mode"] == "durable"
    loop = h.host.loop_calls[0]
    assert "delegate_to_agent" in loop["dynamic_tool_handlers"]
    assert "Delegated agents work in the background" in loop["system_prompt"]


_INLINE_CALL = {"role": "assistant", "content": "", "tool_calls": [{
    "id": "old", "type": "function", "function": {"name": "delegate_to_agent", "arguments": "{}"},
}]}


@pytest.mark.parametrize(("checkpoint", "started"), [
    ({"messages": [{"role": "user", "content": "brief"}]}, False),
    ({"messages": [{"role": "user", "content": "brief"}, _INLINE_CALL]}, True),
    ({"messages": [], "tool_calls_made": ["delegate_to_agent"]}, True),
    ({"runtime_checkpoint": {"messages": [_INLINE_CALL], "pending_tool_call": {"id": "x", "name": "y"}}}, True),
    ({"task_loop_wait": {"kind": "waiting_delegated_input", "resolution": "answered"}}, True),
    ({"human_resume_note": "The delegated agent asked ..."}, True),
])
def test_an_inline_delegation_already_in_the_transcript_is_detected(checkpoint, started):
    assert delegation.inline_delegation_started(checkpoint) is started


@pytest.mark.asyncio
async def test_a_run_in_flight_at_rollout_keeps_inline_delegation(harness):
    h = harness
    from packages.core.ai.runtime.run_state import RuntimeRunState

    root = await h.root()
    root.checkpoint = RuntimeRunState(messages=[
        {"role": "user", "content": "brief"}, _INLINE_CALL,
        {"role": "tool", "tool_call_id": "old", "content": json.dumps({"delegated": True, "content": "done"})},
    ], tool_calls_made=["delegate_to_agent"]).snapshot()
    await h.db.commit()
    h.scripts["root"] = [_complete()]
    await h.root_wake()
    assert delegation.resolve_delegation_mode.await_count == 0, "the flag is never read for it"
    assert (await h.root()).checkpoint["delegation_mode"] == "inline"
    assert "delegate_to_agent" not in h.host.loop_calls[0]["dynamic_tool_handlers"]


@pytest.mark.asyncio
async def test_inline_mode_keeps_the_registered_delegation(harness):
    h = harness
    delegation.resolve_delegation_mode.return_value = "inline"
    h.scripts["root"] = [_complete()]
    await h.root_wake()
    loop = h.host.loop_calls[0]
    assert "delegate_to_agent" not in loop["dynamic_tool_handlers"]
    assert "Delegated agents work in the background" not in loop["system_prompt"]


# ── round trips ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_delegation_round_trip(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = _tool_result(messages, "deleg")
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        finish,
    ]
    h.scripts["writer"] = _child_done("writer")

    parked = await h.root_wake()
    assert parked["status"] == "waiting_child"
    root = await h.root()
    (child,) = await h.children()
    assert root.phase == P.WAITING_CHILD.value and root.claim_token is None
    wait = root.checkpoint["task_loop_wait"]
    assert wait["kind"] == "waiting_child" and wait["child_ids"] == [child.id] and wait["resolution"] is None
    assert (child.phase, child.depth, child.parent_ref, child.root_run_id) == ("ready", 1, root.id, root.id)
    assert child.agent_subscription_id == h.subs["writer"][0].id
    assert child.answer_max_chars == delegation_actions.runtime_delegation_answer_budget(1)
    # The parent released its worker: its own wake is ACKed, the child's staged.
    assert (await h.event(task_coordinator_outbox_dedupe_key(h.task.id))).delivered_at is not None
    assert (await h.event(task_agent_child_outbox_dedupe_key(child.id))).delivered_at is None
    assert [row.kind for row in await h.journal(root.id)][-1] == "delegate"
    logs = (await h.db.execute(select(TaskLog.content).where(TaskLog.task_id == h.task.id))).scalars().all()
    assert "Delegated 1 task(s): Writer" in logs

    assert (await h.child_wake(child.id))["status"] == "completed"
    (child,) = await h.children()
    assert (child.phase, child.outcome_status, child.interrupted) == ("completed", "completed", False)
    assert child.claim_token is None and child.result["run_id"] == child.id
    root = await h.root()
    assert root.phase == P.READY.value
    assert root.checkpoint["task_loop_wait"]["resolution"] == "children_settled"
    pending = root.checkpoint["runtime_checkpoint"]["pending_tool_result"]
    assert pending["id"] == "deleg" and pending["ledger_complete"] is True
    assert [row.kind for row in await h.journal(child.id)][-1] == "child_settled"
    assert [row.kind for row in await h.journal(root.id)][-1] == "batch_settled"

    assert (await h.root_wake())["status"] == "completed"
    # The model saw exactly what chat shows for the same child loop result.
    assert seen["result"] == settlement.batch_tool_result([child])
    assert seen["result"] == delegation_actions.runtime_delegation_tool_result(json.dumps(child.result, ensure_ascii=False, default=str))
    payload = json.loads(seen["result"])
    assert payload["delegated"] is True and payload["run_id"] == child.id and payload["status"] == "completed"
    assert payload["content"] == "Writer finished the work."
    # The child's receipt reached the parent ledger through the host carrier.
    root = await h.root()
    receipts = root.checkpoint["tool_evidence"]["receipts"]
    assert [receipt.get("document_id") for receipt in receipts] == ["doc-writer"]
    task = await h.fresh_task()
    assert task.status == "completed"
    assert task.actual_output["outcome"]["effect_state"] == "confirmed"


@pytest.mark.asyncio
async def test_a_proposal_authorization_reaches_the_root_and_its_delegated_agents(harness):
    """A Strategist Proposal's pre-authorized action rides every run of its Task, unchanged."""

    h = harness
    authorization = {
        "version": 1, "authorization_id": f"proposal-item:{h.task_id}", "task_id": h.task_id,
        "workspace_id": h.task.workspace_id, "provider": "youtube", "action": "publish_video",
        "visibility": "public", "max_executions": 1, "consumed_at": None,
    }
    task = await h.fresh_task()
    task.details = {"proposal_external_authorization": authorization}
    await h.db.commit()
    h.scripts["root"] = [("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]), _complete()]
    h.scripts["writer"] = _child_done("writer", effects=False)

    assert (await h.root_wake())["status"] == "waiting_child"
    (child,) = await h.children()
    assert (await h.child_wake(child.id))["status"] == "completed"

    expected = {"proposal_external_authorization": authorization}
    assert h.host.context.await_args.kwargs["runtime_metadata"] == expected
    assert [context["runtime_metadata"] for context in h.child_contexts] == [expected]


@pytest.mark.asyncio
async def test_a_child_delivery_with_a_caveat_completes_the_parent_with_exceptions(harness):
    """The child's caveat reaches the parent, which delivers it as an exception."""

    h = harness
    seen = {}
    unmet = {
        "criterion": "The note is under 50 words.",
        "met": False,
        "evidence": "The writer could not count the words; not verified.",
    }

    def finish(messages, _tools):
        seen["result"] = json.loads(_tool_result(messages, "deleg"))
        return ("", [_call("done", "complete_task", {
            **COMPLETE_ARGS, "acceptance": [*COMPLETE_ARGS["acceptance"], unmet],
        })])

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", TWO_CRITERIA_PLAN)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        finish,
        lambda messages, _tools: pytest.fail(messages[-1]["content"]),
    ]
    h.scripts["writer"] = [
        ("", [_call("writer-file", "generate_file", {"kind": "document"})]),
        ("Wrote the note with the launch date; I could not check the word count.", None),
    ]

    assert (await h.root_wake())["status"] == "waiting_child"
    (child,) = await h.children()
    assert (await h.child_wake(child.id))["status"] == "completed"
    assert (await h.root_wake())["status"] == "completed"

    assert seen["result"]["status"] == "completed"
    assert "could not check the word count" in seen["result"]["content"]
    task = await h.fresh_task()
    assert task.status == "completed"
    assert task.actual_output["acceptance_exceptions"] == [
        {"criterion": unmet["criterion"], "evidence": unmet["evidence"]},
    ]
    comment = (await h.db.execute(select(TaskLog.content).where(
        TaskLog.task_id == h.task.id, TaskLog.log_type == "comment",
    ))).scalar_one()
    assert comment.startswith("## Delivered — not every acceptance check passed")
    terminal = [row for row in await h.journal((await h.root()).id) if row.kind == "terminal"]
    assert [row.payload["acceptance_exceptions"] for row in terminal] == [1]


@pytest.mark.asyncio
async def test_usage_is_attributed_to_the_parent_and_billed_once_per_child_call(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    (child,) = await h.children()
    await h.child_wake(child.id)
    root_before = (await h.root()).checkpoint["runtime_checkpoint"]

    # Root and child calls settle separately, with the same owning Task.
    def billed_to(prefix):
        return [entry for entry in h.billed if entry["source_prefix"].rsplit(":", 1)[0] == prefix]

    child_prefix = f"delegation:{child.id}"
    root_prefix = f"task:{h.task.id}"
    assert len(billed_to(child_prefix)) == h.calls["writer"] == 2
    assert len(billed_to(root_prefix)) == h.calls["root"] == 2
    assert len(h.billed) == 4
    (child,) = await h.children()
    assert child.usage["total_tokens"] == 30
    # Attribution: the parked parent's usage gains the child's usage once.
    pending = root_before["pending_tool_result"]
    (attributed,) = pending["child_usage"]
    assert attributed["run_id"] == child.id
    assert {key: attributed["usage"][key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")} == {
        "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
    }
    root_usage = (await h.root()).checkpoint["runtime_checkpoint"]["usage"]
    assert root_usage["total_tokens"] == 2 * 15 + 30

    await h.root_wake()
    # The resumed root settles its new call without replaying child usage.
    assert len(billed_to(child_prefix)) == 2
    assert len(billed_to(root_prefix)) == h.calls["root"] == 3
    assert len(h.billed) == 5
    assert {entry["task_id"] for entry in h.billed} == {h.task.id}

    # The journal: each run's rows carry only its own usage, so a reader that
    # sums usage_delta over the Task's runs counts every call once; the
    # children's share shows on the parent as child_usage_delta.
    def total(rows, key):
        return sum(int((row.payload.get(key) or {}).get("total_tokens") or 0) for row in rows if row.kind == "round")

    root_rows = await h.journal(h.run_id)
    child_rows = await h.journal(child.id)
    assert total(root_rows, "usage_delta") == 15 * h.calls["root"]
    assert total(child_rows, "usage_delta") == 30
    assert total(root_rows, "child_usage_delta") == 30


@pytest.mark.asyncio
async def test_three_parallel_children_round_trip(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = _tool_result(messages, "deleg")
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks(*KEYS))]),
        finish,
    ]
    for key in KEYS:
        h.scripts[key] = _child_done(key)
    await h.root_wake()
    children = await h.children()
    assert [child.agent_label for child in children] == ["Writer", "Analyst", "Editor"]
    assert {child.batch_size for child in children} == {3}
    budget = delegation_actions.runtime_delegation_answer_budget(3)
    assert {child.answer_max_chars for child in children} == {budget}
    # Staggered as chat staggers its parallel children.
    events = [await h.event(task_agent_child_outbox_dedupe_key(child.id)) for child in children]
    assert events[0].available_at < events[1].available_at < events[2].available_at

    await h.run_children()
    children = await h.children()
    assert {child.phase for child in children} == {"completed"}
    root = await h.root()
    assert [row.kind for row in await h.journal(root.id)].count("batch_settled") == 1
    assert (await h.root_wake())["status"] == "completed"
    payload = json.loads(seen["result"])
    assert payload["parallel"] is True and (payload["completed"], payload["total"]) == (3, 3)
    assert [entry["agent"] for entry in payload["results"]] == ["Writer", "Analyst", "Editor"]
    # Chat's batch function over the same child payloads, byte for byte.
    assert seen["result"] == delegation_actions.runtime_delegation_batch_result(
        [{"agent": child.agent_label} for child in children], [child.result for child in children],
    )
    receipts = (await h.root()).checkpoint["tool_evidence"]["receipts"]
    assert sorted(receipt.get("document_id") for receipt in receipts) == ["doc-analyst", "doc-editor", "doc-writer"]


@pytest.mark.asyncio
async def test_a_partial_failure_returns_every_childs_status(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = json.loads(_tool_result(messages, "deleg"))
        return ("", [_call("done", "complete_task", {
            **COMPLETE_ARGS, "outcome": "blocked", "blocked_reason": "The analyst failed.",
        })])

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
        finish,
    ]
    h.scripts["writer"] = _child_done("writer")
    # The analyst's agent was removed from the Workspace meanwhile.
    original = delegation_actions._resolve_workspace_service_agent

    async def gone(db, *, entity_id, workspace_id, raw_params):
        if raw_params.get("agent_subscription_id") == h.subs["analyst"][0].id and h.calls.get("root", 0) >= 2:
            return None, None, [], "The analyst is gone."
        return await original(db, entity_id=entity_id, workspace_id=workspace_id, raw_params=raw_params)

    await h.root_wake()
    h.monkeypatch.setattr(delegation_actions, "_resolve_workspace_service_agent", gone)
    await h.run_children()
    writer, analyst = await h.children()
    assert (writer.phase, analyst.phase) == ("completed", "failed")
    assert analyst.result["error"] == "workspace_service_agent_not_found"
    # Its ledger is whole and nothing ran: a plain failure, not interrupted.
    assert analyst.interrupted is False and "do_not_replay" not in analyst.result
    await h.root_wake()
    statuses = [entry["delegation_status"] for entry in seen["result"]["results"]]
    assert statuses == ["completed", "failed"]
    assert seen["result"]["completed"] == 1
    assert "session_status" not in seen["result"]["results"][1]
    root = await h.root()
    assert root.checkpoint["tool_evidence"].get("effect_state") != "unconfirmed"


@pytest.mark.asyncio
async def test_after_a_known_failure_the_agent_is_delegated_again_without_continue_interrupted(harness):
    h = harness
    seen = {}

    def again(messages, _tools):
        return "", [_call("again", "delegate_to_agent", _tasks("analyst"))]

    def finish(messages, _tools):
        seen["result"] = json.loads(_tool_result(messages, "again"))
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("analyst"))]),
        again,
        finish,
    ]
    h.scripts["analyst"] = _child_done("analyst")
    original = delegation_actions._resolve_workspace_service_agent
    gone_once = {"done": False}

    async def gone(db, *, entity_id, workspace_id, raw_params):
        if not gone_once["done"]:
            gone_once["done"] = True
            return None, None, [], "The analyst is gone."
        return await original(db, entity_id=entity_id, workspace_id=workspace_id, raw_params=raw_params)

    await h.root_wake()
    h.monkeypatch.setattr(delegation_actions, "_resolve_workspace_service_agent", gone)
    await h.run_children()
    (first,) = await h.children()
    assert (first.phase, first.interrupted) == ("failed", False)
    # The second delegation is accepted as is: no continue_interrupted needed.
    assert (await h.root_wake())["status"] == "waiting_child"
    first, second = await h.children()
    assert second.continue_interrupted is False and second.phase == P.READY.value
    await h.run_children()
    assert (await h.root_wake())["status"] == "completed"
    assert seen["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_a_host_failure_is_interrupted_only_when_effects_are_unknown():
    known = {"tool_evidence": {"effect_state": "confirmed", "receipts": []}}
    unconfirmed = {"tool_evidence": {"effect_state": "unconfirmed"}}
    in_flight = {"tool_evidence": {"effect_state": "none", "unconfirmed_calls": ["pub"]}}
    assert settlement.child_effects_unknown(known, ledger_complete=True) is False
    assert settlement.child_effects_unknown({}, ledger_complete=True) is False
    assert settlement.child_effects_unknown(unconfirmed, ledger_complete=True) is True
    assert settlement.child_effects_unknown(in_flight, ledger_complete=True) is True
    # The last round is unknown (a crash, an in-flight call without a receipt).
    assert settlement.child_effects_unknown(known, ledger_complete=False) is True
    child = TaskAgentChildRun(
        id=generate_ulid(), agent_subscription_id="s", agent_id="a", service_key="writer",
        agent_label="Writer",
    )
    plain = child_worker._host_failure(
        None, child, code="workspace_service_identity_changed", message="gone",
        checkpoint=known, ledger_complete=True,
    )
    assert plain.interrupted is False and "do_not_replay" not in plain.result
    unknown = child_worker._host_failure(
        None, child, code=child_worker.CHILD_REPAIR_REQUIRED, message="crashed",
        checkpoint=known, ledger_complete=False,
    )
    assert unknown.interrupted is True and unknown.result["do_not_replay"] is True
    assert unknown.result["session_status"] == "interrupted"
    # This very wake crashed before (its failure record names it), or its
    # previous owner died mid-wake: the lost work is unknown.
    crashed = {**known, "wake_failures": {"wake_generation": "g1", "count": 1}}
    assert settlement.child_effects_unknown(crashed, ledger_complete=True, wake_generation="g1") is True
    assert settlement.child_effects_unknown(crashed, ledger_complete=True, wake_generation="g2") is False
    assert settlement.child_effects_unknown(known, ledger_complete=True, took_over=True) is True


@pytest.mark.asyncio
async def test_a_known_failure_after_a_crash_in_the_same_wake_is_interrupted(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]
    await h.root_wake()
    (child,) = await h.children()
    original_loop = child_worker.runtime_execute_chat_agent_loop

    async def crash(**_kwargs):
        raise KeyError("the child crashed mid-wake")

    h.monkeypatch.setattr(child_worker, "runtime_execute_chat_agent_loop", crash)
    event = await h.event(task_agent_child_outbox_dedupe_key(child.id))
    with pytest.raises(KeyError):
        await child_worker.run_task_agent_child_once(
            event_id=event.id, task_id=h.task.id, child_id=child.id,
            wake_generation=event.payload["wake_generation"],
        )
    # The same wake is redelivered; meanwhile the writer's identity changed.
    h.monkeypatch.setattr(child_worker, "runtime_execute_chat_agent_loop", original_loop)

    async def gone(_db, *, entity_id, workspace_id, raw_params):
        return None, None, [], "The writer is gone."

    h.monkeypatch.setattr(delegation_actions, "_resolve_workspace_service_agent", gone)
    outcome = await child_worker.run_task_agent_child_once(
        event_id=event.id, task_id=h.task.id, child_id=child.id,
        wake_generation=event.payload["wake_generation"],
    )
    assert outcome["status"] == "failed"
    (child,) = await h.children()
    assert child.result["error"] == "workspace_service_agent_not_found"
    # The crashed attempt may have acted after its last checkpoint.
    assert child.interrupted is True and child.result["do_not_replay"] is True
    assert child.checkpoint["child_settlement"]["ledger_complete"] is False


# ── refusals: do_not_replay, continue_interrupted, busy, depth ──────────


async def _interrupted_writer(h) -> TaskAgentChildRun:
    """A finished delegation whose writer left an unconfirmed effect behind."""

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]

    async def failing_publish(_args):
        return json.dumps({"status": "error", "error": "provider_timeout"})

    h.child_tools["publish_post"] = failing_publish
    h.scripts["writer"] = [
        ("", [_call("pub", "publish_post", {"text": "Launch"})]),
        ("I could not confirm the post.", None),
    ]
    await h.root_wake()
    (child,) = await h.children()
    await h.child_wake(child.id)
    (child,) = await h.children()
    return child


@pytest.mark.asyncio
async def test_do_not_replay_refuses_a_new_delegation_and_continue_interrupted_inherits_the_transcript(harness):
    h = harness
    previous = await _interrupted_writer(h)
    assert previous.interrupted is True and previous.result["do_not_replay"] is True
    assert previous.result["session_status"] == "interrupted"
    refused = {}

    def after_settled(messages, _tools):
        return "", [_call("again", "delegate_to_agent", _tasks("writer"))]

    def after_refusal(messages, _tools):
        refused["result"] = json.loads(_tool_result(messages, "again"))
        return "", [_call("cont", "delegate_to_agent", _tasks("writer", continue_interrupted=True))]

    h.scripts["root"] += [after_settled, after_refusal]
    h.scripts["writer"] = [
        lambda messages, _tools: (
            "Checked: the post did not go out." if any(
                "publish_post" in json.dumps(message) for message in messages
            ) else "NO HISTORY", None,
        ),
    ]
    assert (await h.root_wake())["status"] == "waiting_child"
    assert refused["result"]["error"] == delegation.INTERRUPTED_ERROR
    assert refused["result"]["details"]["run_id"] == previous.id
    from packages.core.services.agent_delegation_session import DELEGATION_INTERRUPTED_MESSAGE

    assert DELEGATION_INTERRUPTED_MESSAGE in refused["result"]["message"]
    _first, second = await h.children()
    assert second.previous_child_id == previous.id and second.continue_interrupted is True
    await h.child_wake(second.id)
    # The new run started from the interrupted one's transcript, its unanswered
    # call answered, with the new task after it.
    initial = h.child_loops[-1]["initial_messages"]
    assert any(message.get("tool_call_id") == "pub" for message in initial)
    (_first, second) = await h.children()
    assert second.result["content"] == "Checked: the post did not go out."


@pytest.mark.asyncio
async def test_a_busy_agent_is_refused_in_place_and_a_park_race_replays_as_a_gate_result(harness):
    h = harness
    replayed = {}

    def after_race(messages, _tools):
        replayed["result"] = _tool_result(messages, "deleg")
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        after_race,
    ]
    # Another run of the writer is live in this Task, but the handler's own
    # check missed it (it appeared after): the park transaction finds it.
    live = TaskAgentChildRun(
        id=generate_ulid(), task_id=h.task.id, entity_id=h.task.entity_id,
        workspace_id=h.task.workspace_id, root_run_id=h.run.id, parent_ref=h.run.id,
        parent_tool_call_id="other-call", item_index=0, batch_size=1, depth=1,
        agent_subscription_id=h.subs["writer"][0].id, agent_id=h.subs["writer"][0].agent_id,
        service_key="writer", agent_label="Writer", prompt="Earlier work.", answer_max_chars=4000,
        phase=P.RUNNING.value, lifecycle_generation=h.run.lifecycle_generation, state_version=0,
    )
    h.db.add(live)
    await h.db.commit()
    h.monkeypatch.setattr(delegation, "_agent_history", AsyncMock(return_value={}))

    assert (await h.root_wake())["status"] == "delegation_busy"
    assert [child.id for child in await h.children()] == [live.id], "nothing was created"
    root = await h.root()
    stored = root.checkpoint["runtime_checkpoint"]["pending_tool_result"]
    assert stored["gate"] is True
    # The earlier run ends meanwhile (completion waits for open delegated runs).
    live = await _fresh_child(h, live.id)
    live.phase = P.COMPLETED.value
    await h.db.commit()
    outcome = await h.root_wake()
    assert outcome["status"] == "completed"
    busy = json.loads(replayed["result"])
    assert busy["error"] == delegation.BUSY_ERROR and busy["details"]["run_id"] == live.id
    # The refusal was a gate: the delegate call left no effect in the ledger.
    assert (await h.root()).checkpoint["tool_evidence"]["effect_state"] != "unconfirmed"


@pytest.mark.asyncio
async def test_the_handler_refuses_a_busy_agent_before_any_park(harness):
    h = harness
    live_child = TaskAgentChildRun(
        id=generate_ulid(), task_id=h.task.id, entity_id=h.task.entity_id,
        workspace_id=h.task.workspace_id, root_run_id=h.run.id, parent_ref=h.run.id,
        parent_tool_call_id="other-call", item_index=0, batch_size=1, depth=1,
        agent_subscription_id=h.subs["writer"][0].id, agent_id=h.subs["writer"][0].agent_id,
        service_key="writer", agent_label="Writer", prompt="Earlier.", answer_max_chars=4000,
        phase=P.RUNNING.value, lifecycle_generation="x", state_version=0,
    )
    h.db.add(live_child)
    await h.db.commit()
    scope = delegation.DelegationScope(
        task_id=h.task.id, entity_id=h.task.entity_id, workspace_id=h.task.workspace_id,
        root_run_id=h.run.id, run_id=h.run.id, parent_child_id=None, depth=0, agent_id=None,
    )
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    refusal = await delegation.delegate_to_agent_call(
        {**_tasks("writer"), "_runtime_tool_call_id_from_context": "c1"},
        scope=scope, resumed_wait=None, session_factory=h.host.session_factory,
    )
    assert isinstance(refusal, RuntimePreflightValidationError)
    assert json.loads(refusal)["error"] == delegation.BUSY_ERROR


@pytest.mark.asyncio
async def test_a_depth_one_child_delegates_durably_and_a_depth_two_child_gets_no_delegation_tool(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    h.scripts["writer"] = [
        ("", [_call("sub", "delegate_to_agent", _tasks("analyst"))]),
        ("The analyst helped.", None),
    ]
    h.scripts["analyst"] = _child_done("analyst")
    await h.root_wake()
    (writer,) = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "waiting_child"
    writer, analyst = await h.children()
    assert writer.phase == P.WAITING_CHILD.value
    assert (analyst.depth, analyst.parent_ref, analyst.parent_child_id) == (2, writer.id, writer.id)
    writer_loop = h.child_loops[0]
    assert "delegate_to_agent" in writer_loop["dynamic_tool_handlers"]
    await h.child_wake(analyst.id)
    analyst_loop = h.child_loops[1]
    assert "delegate_to_agent" not in analyst_loop["dynamic_tool_handlers"]
    assert "delegate_to_agent" not in [tool["function"]["name"] for tool in analyst_loop["tools"]]
    # Each child runs at its own depth, so the registered delegation's own
    # depth check refuses the deepest one whatever its tool list holds.
    assert (writer_loop["_delegation_depth"], analyst_loop["_delegation_depth"]) == (1, 2)
    with delegation_actions.runtime_delegation_depth_scope(analyst_loop["_delegation_depth"]):
        refused = json.loads(await delegation_actions.runtime_workspace_delegate_service_action(
            entity_id=h.task.entity_id, workspace_id=h.task.workspace_id, params={"prompt": "x"},
        ))
    assert refused["error"] == "delegation_depth_limit"
    assert delegation_actions._delegation_depth.get() == 0, "the scope is reset after the run"
    # The analyst settled into the writer, which continues and settles into the root.
    assert (await h.child_wake(writer.id))["status"] == "completed"
    writer, analyst = await h.children()
    assert writer.result["content"] == "The analyst helped."
    assert (await h.root_wake())["status"] == "completed"
    receipts = (await h.root()).checkpoint["tool_evidence"]["receipts"]
    assert [receipt.get("document_id") for receipt in receipts] == ["doc-analyst"]


# ── M5: a root woken while it waits on children never calls the model ──


@pytest.mark.asyncio
async def test_a_root_woken_while_children_run_reparks_without_a_model_call(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
        _complete(),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    root_calls = h.calls["root"]
    writer, analyst = await h.children()
    # Lose the analyst's wake, then wake the root (the backstop or a stray wake).
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    await durable_run.close_run_wake(h.db, TASK_AGENT_CHILD_RUN_SPEC, key=analyst.id, last_error="lost")
    await stage_task_coordinator_wake(h.db, task_id=h.task.id, reason="stray")
    await h.db.commit()

    assert (await h.root_wake())["status"] == "waiting_child"
    assert h.calls["root"] == root_calls, "no model round while children run"
    root = await h.root()
    assert root.phase == P.WAITING_CHILD.value and root.checkpoint["task_loop_wait"]["resolution"] is None
    # The re-park gave the analyst its wake back.
    assert (await h.event(task_agent_child_outbox_dedupe_key(analyst.id))).delivered_at is None
    assert [row.kind for row in await h.journal(root.id)][-1] == "delegate"
    assert (await h.journal(root.id))[-1].payload.get("reparked") is True


@pytest.mark.asyncio
async def test_a_root_woken_on_a_finished_batch_settles_it_and_continues(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = _tool_result(messages, "deleg")
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        finish,
    ]
    await h.root_wake()
    (child,) = await h.children()
    # The child ended but its settlement never ran (a crash between the two).
    child.phase = P.COMPLETED.value
    child.outcome_status = "completed"
    child.result = {"delegated": True, "run_id": child.id, "status": "completed", "content": "Done."}
    child.checkpoint = {settlement.SETTLEMENT_KEY: {"ledger_complete": True}}
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    await stage_task_coordinator_wake(h.db, task_id=h.task.id, reason="stray")
    await h.db.commit()
    assert (await h.root_wake())["status"] == "completed"
    assert json.loads(seen["result"])["content"] == "Done."
    assert [row.kind for row in await h.journal(h.run.id)].count("batch_settled") == 1


@pytest.mark.asyncio
async def test_a_root_woken_on_a_broken_batch_gets_an_honest_unavailable_result(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = json.loads(_tool_result(messages, "deleg"))
        return ("", [_call("done", "complete_task", {
            **COMPLETE_ARGS, "outcome": "blocked", "blocked_reason": "Lost the delegated results.",
        })])

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        finish,
    ]
    await h.root_wake()
    (child,) = await h.children()
    child.lifecycle_generation = "stale"
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    await stage_task_coordinator_wake(h.db, task_id=h.task.id, reason="stray")
    await h.db.commit()
    assert (await h.root_wake())["status"] == "blocked"
    assert seen["result"]["error"] == "delegation_results_unavailable"
    (child,) = await h.children()
    assert child.phase == "cancelled"
    # Unknown children's effects leave the parent's ledger unconfirmed.
    assert (await h.root()).checkpoint["tool_evidence"]["effect_floor"] == "unconfirmed"


@pytest.mark.asyncio
async def test_a_broken_batch_stops_every_open_run_below_it(harness):
    h = harness
    seen = {}

    def after_broken(messages, _tools):
        seen["broken"] = json.loads(_tool_result(messages, "deleg"))
        return "", [_call("try", "delegate_to_agent", _tasks("analyst"))]

    def after_refusal(messages, _tools):
        seen["refusal"] = json.loads(_tool_result(messages, "try"))
        return "", [_call("again", "delegate_to_agent", _tasks("analyst", continue_interrupted=True))]

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        after_broken,
        after_refusal,
    ]
    h.scripts["writer"] = [("", [_call("sub", "delegate_to_agent", _tasks("analyst"))])]
    await h.root_wake()
    (writer,) = await h.children()
    assert (await h.child_wake(writer.id))["status"] == "waiting_child"
    writer, analyst = await h.children()
    writer.lifecycle_generation = "stale"
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    await stage_task_coordinator_wake(h.db, task_id=h.task_id, reason="stray")
    await h.db.commit()

    assert (await h.root_wake())["status"] == "waiting_child"
    assert seen["broken"]["error"] == "delegation_results_unavailable"
    rows = {row.id: row for row in await h.children()}
    assert rows[writer.id].phase == "cancelled"
    assert rows[analyst.id].phase == "cancelled", "the grandchild stops with its parent"
    # The Analyst is free again: not busy, only interrupted (its last round is
    # unknown), so a delegation that passes continue_interrupted parks.
    assert seen["refusal"]["error"] == delegation.INTERRUPTED_ERROR
    (fresh,) = [row for row in rows.values() if row.parent_tool_call_id == "again"]
    assert fresh.agent_label == "Analyst" and fresh.phase == "ready"


# ── crash windows (a)-(g) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crash_a_a_park_that_fails_before_commit_leaves_no_orphan(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        # The redelivered wake resumes after the plan round and delegates again.
        ("", [_call("deleg2", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    from packages.core.workers.task_loop_host import TaskLoopHost

    real = TaskLoopHost._park_on_children
    failures = []

    async def crash_once(self, db, **kwargs):
        if not failures:
            failures.append(1)
            raise KeyError("crash inside the park")
        return await real(self, db, **kwargs)

    h.monkeypatch.setattr(TaskLoopHost, "_park_on_children", crash_once)
    with pytest.raises(KeyError):
        await h.root_wake()
    assert await h.children() == [], "no child row outlives a park that did not commit"
    # The root's wake was never ACKed: it is redelivered and parks this time.
    assert (await h.root_wake())["status"] == "waiting_child"
    (child,) = await h.children()
    assert child.parent_tool_call_id == "deleg2"


@pytest.mark.asyncio
async def test_crash_b_a_lost_parent_wake_and_an_unsettled_batch_are_recovered(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
        _complete(),
    ]
    h.scripts["writer"] = _child_done("writer")
    h.scripts["analyst"] = _child_done("analyst")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    await h.child_wake(analyst.id)
    # The settled batch's wake is lost.
    await durable_run.close_run_wake(
        h.db, runtime_tasks.TASK_COORDINATOR_RUN_SPEC, key=h.task.id, last_error="lost",
    )
    run = await h.root()
    run.updated_at = datetime(1990, 1, 1, tzinfo=timezone.utc)
    await h.db.commit()
    assert await runtime_tasks.recover_stalled_task_coordinator_wakes_once() >= 1
    assert (await h.event(task_coordinator_outbox_dedupe_key(h.task.id))).delivered_at is None
    assert (await h.root_wake())["status"] == "completed"


@pytest.mark.asyncio
async def test_crash_b_every_child_ended_but_nothing_settled_the_batch(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    await h.root_wake()
    (child,) = await h.children()
    child.phase = P.COMPLETED.value
    child.outcome_status = "completed"
    child.result = {"delegated": True, "run_id": child.id, "status": "completed", "content": "Done."}
    child.checkpoint = {settlement.SETTLEMENT_KEY: {"ledger_complete": True}}
    run = await h.root()
    run.updated_at = datetime(1990, 1, 1, tzinfo=timezone.utc)
    await h.db.commit()
    assert await runtime_tasks.recover_stalled_task_coordinator_wakes_once() >= 1
    root = await h.root()
    assert root.checkpoint["task_loop_wait"]["resolution"] == "children_settled"
    assert root.phase == P.READY.value
    assert [row.kind for row in await h.journal(root.id)].count("batch_settled") == 1
    assert (await h.root_wake())["status"] == "completed"


@pytest.mark.asyncio
async def test_crash_c_a_resumed_parent_that_crashes_merges_child_evidence_once(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        ("", [_call("note", "record_note", {"text": "checked"})]),
        _complete(),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    (child,) = await h.children()
    await h.child_wake(child.id)
    from packages.core.workers.task_loop_host import TaskLoopHost

    real = TaskLoopHost.checkpoint
    crashed = []

    async def crash_once(self, *args, **kwargs):
        if not crashed:
            # The resumed call's result and merged ledger never reach a round
            # checkpoint: the process dies first.
            crashed.append(1)
            raise KeyError("crash before the first resumed round checkpoint")
        return await real(self, *args, **kwargs)

    h.monkeypatch.setattr(TaskLoopHost, "checkpoint", crash_once)
    with pytest.raises(KeyError):
        await h.root_wake()
    root = await h.root()
    assert root.checkpoint["runtime_checkpoint"]["pending_tool_result"]["id"] == "deleg"
    assert (await h.root_wake())["status"] == "completed"
    receipts = (await h.root()).checkpoint["tool_evidence"]["receipts"]
    assert [receipt.get("document_id") for receipt in receipts] == ["doc-writer"], "merged exactly once"


@pytest.mark.asyncio
async def test_crash_d_two_children_ending_at_once_settle_the_batch_once(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
    ]
    await h.root_wake()
    claims = []
    for child in await h.children():
        event = await h.event(task_agent_child_outbox_dedupe_key(child.id))
        claim, state = await durable_run.claim_run_wake(
            h.db, TASK_AGENT_CHILD_RUN_SPEC, key=child.id, event_id=event.id,
            wake_generation=event.payload["wake_generation"], claim_token=generate_ulid(), lease_seconds=120,
        )
        assert state == "claimed"
        claims.append((child, claim))
    await h.db.commit()

    async def finish(child, claim):
        async with h.host.session_factory() as db:
            ok = await settlement.settle_task_agent_child(
                db, child_id=child.id, event_id=claim.event_id, claim_token=claim.claim_token,
                claim_generation=claim.wake_generation,
                outcome=settlement.ChildOutcome(
                    phase="completed", outcome_status="completed", interrupted=False,
                    result={"delegated": True, "run_id": child.id, "status": "completed", "content": child.id},
                    checkpoint={}, usage={}, ledger_complete=True,
                ),
            )
            await db.commit()
            return ok

    assert await asyncio.gather(*(finish(child, claim) for child, claim in claims)) == [True, True]
    root = await h.root()
    assert root.checkpoint["task_loop_wait"]["resolution"] == "children_settled"
    assert [row.kind for row in await h.journal(root.id)].count("batch_settled") == 1
    event = await h.event(task_coordinator_outbox_dedupe_key(h.task.id))
    assert event.delivered_at is None and event.payload["reason"] == "delegated_children_settled"


@pytest.mark.asyncio
async def test_crash_e_a_child_crashing_three_times_fails_with_an_effect_floor(harness):
    h = harness
    seen = {}

    def finish(messages, _tools):
        seen["result"] = json.loads(_tool_result(messages, "deleg"))
        return _complete()

    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        finish,
    ]
    await h.root_wake()
    (child,) = await h.children()

    async def crash(**_kwargs):
        raise KeyError("the child keeps crashing")

    h.monkeypatch.setattr(child_worker, "runtime_execute_chat_agent_loop", crash)
    event = await h.event(task_agent_child_outbox_dedupe_key(child.id))
    for _ in range(2):
        with pytest.raises(KeyError):
            await child_worker.run_task_agent_child_once(
                event_id=event.id, task_id=h.task.id, child_id=child.id,
                wake_generation=event.payload["wake_generation"],
            )
    outcome = await child_worker.run_task_agent_child_once(
        event_id=event.id, task_id=h.task.id, child_id=child.id,
        wake_generation=event.payload["wake_generation"],
    )
    assert outcome["status"] == "failed"
    (child,) = await h.children()
    assert child.interrupted is True and child.result["error"] == child_worker.CHILD_REPAIR_REQUIRED
    pending = (await h.root()).checkpoint["runtime_checkpoint"]["pending_tool_result"]
    assert pending["ledger_complete"] is False
    assert any(item["call_id"].endswith("~effect_floor") for item in pending["child_evidence"])
    # The parent cannot complete over the child's unknown last round unchecked.
    await h.root_wake()
    assert (await h.root()).checkpoint["tool_evidence"]["effect_floor"] == "unconfirmed"
    assert seen["result"]["details"]["session_status"] == "interrupted"


@pytest.mark.asyncio
async def test_crash_f_a_cancel_during_a_child_tool_stops_it_without_a_stale_write(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]
    h.scripts["writer"] = [("", [_call("slow", "slow_tool", {})]), ("done", None)]
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_tool(_args):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "{}"

    h.child_tools["slow_tool"] = slow_tool
    h.monkeypatch.setattr(child_worker, "_HEARTBEAT_SECONDS", 0.05)
    await h.root_wake()
    (child,) = await h.children()
    run = asyncio.create_task(h.child_wake(child.id))
    await asyncio.wait_for(started.wait(), 10)

    async with h.host.session_factory() as db:
        task = (await db.execute(select(Task).where(Task.id == h.task.id))).scalar_one()
        task.status = "cancelled"
        task.status_changed_at = datetime.now(timezone.utc)
        await db.commit()
    await runtime_tasks.reconcile_terminal_task_coordinator_runs_once()
    outcome = await asyncio.wait_for(run, 30)
    assert cancelled.is_set(), "the running tool was cancelled"
    assert outcome["status"] in {"superseded", "stale"}
    (child,) = await h.children()
    assert (child.phase, child.interrupted, child.claim_token) == ("cancelled", True, None)
    assert child.result["status"] == "cancelled"
    assert [row.kind for row in await h.journal(child.id)] == ["terminal"], "no write after the cancel"


@pytest.mark.asyncio
async def test_crash_g_a_pause_while_waiting_on_children_rebinds_them_and_reparks(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    (child,) = await h.children()
    event = await h.event(task_agent_child_outbox_dedupe_key(child.id))
    claim, _state = await durable_run.claim_run_wake(
        h.db, TASK_AGENT_CHILD_RUN_SPEC, key=child.id, event_id=event.id,
        wake_generation=event.payload["wake_generation"], claim_token=generate_ulid(), lease_seconds=120,
    )
    task = await h.fresh_task()
    task.status = "on_hold"
    task.status_changed_at = datetime.now(timezone.utc)
    await h.db.commit()
    child_id = child.id
    task = await h.fresh_task()
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await h.db.commit()
    # A child still holding its claim blocks the rebind (the intent retries).
    with pytest.raises(ValueError, match="delegated child"):
        await ensure_task_coordinator_run(h.db, task=await h.fresh_task(), selection=h.selection)
    await h.db.rollback()
    await durable_run.release_run_claim(
        h.db, TASK_AGENT_CHILD_RUN_SPEC, key=child_id,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
    )
    await h.db.commit()
    task = await h.fresh_task()
    run = await ensure_task_coordinator_run(h.db, task=task, selection=h.selection)
    await h.db.commit()
    generation = task_execution_intent_generation(task)
    (child,) = await h.children()
    assert run.phase == P.READY.value and run.lifecycle_generation == generation
    assert child.lifecycle_generation == generation, "the child is rebound with its root"

    root_calls = h.calls["root"]
    assert (await h.root_wake())["status"] == "waiting_child"
    assert h.calls["root"] == root_calls, "the resumed root re-parks without a model call"
    assert (await h.child_wake(child.id))["status"] == "completed"
    assert (await h.root_wake())["status"] == "completed"


@pytest.mark.asyncio
async def test_a_resume_restages_grandchildren_and_journals_their_wakes(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
        _complete(),
    ]
    h.scripts["writer"] = [("", [_call("sub", "delegate_to_agent", _tasks("analyst"))]), ("done", None)]
    h.scripts["analyst"] = _child_done("analyst", effects=False)
    await h.root_wake()
    (writer,) = await h.children()
    await h.child_wake(writer.id)
    _writer, analyst = await h.children()
    analyst_id = analyst.id
    # Pause (the analyst's wake goes stale and is closed), then resume.
    task = await h.fresh_task()
    task.status = "on_hold"
    task.status_changed_at = datetime.now(timezone.utc)
    await h.db.commit()
    await durable_run.close_run_wake(h.db, TASK_AGENT_CHILD_RUN_SPEC, key=analyst_id, last_error="closed")
    task = await h.fresh_task()
    task.status = "in_progress"
    task.status_changed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    await h.db.commit()
    await ensure_task_coordinator_run(h.db, task=await h.fresh_task(), selection=h.selection)
    await h.db.commit()

    assert (await h.root_wake())["status"] == "waiting_child"
    assert (await h.event(task_agent_child_outbox_dedupe_key(analyst_id))).delivered_at is None
    last = (await h.journal(analyst_id))[-1]
    assert last.kind == "wake" and last.payload["resolution"] == "task_resumed"
    assert last.payload["task_resumed"] is True
    # The tree finishes from there.
    await h.run_children()
    assert (await h.root_wake())["status"] == "completed"


# ── a root that ends with open children; retry ─────────────────────────


@pytest.mark.asyncio
async def test_a_root_that_ends_stops_its_children_and_a_retry_keeps_their_work(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    # The root is blocked while the analyst is still open (a failed re-park).
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    await stage_task_coordinator_wake(h.db, task_id=h.task.id, reason="stray")
    await h.db.commit()
    event = await h.event(task_coordinator_outbox_dedupe_key(h.task.id))
    claim, _ = await task_run_host.admit_and_claim(
        event_id=event.id, task_id=h.task.id, wake_generation=event.payload["wake_generation"],
        engine="agent_loop", lease_seconds=60, session_factory=h.host.session_factory,
    )
    root = await h.root()
    outcome = await worker._block(
        h.host.session_factory, claim, code="agent_loop_test_stop", message="Stopped.",
        checkpoint=dict(root.checkpoint),
    )
    assert outcome["status"] == "blocked"
    writer, analyst = await h.children()
    assert writer.phase == "completed" and analyst.phase == "cancelled" and analyst.interrupted
    root = await h.root()
    assert root.checkpoint["task_loop_wait"]["resolution"] == "children_cancelled"
    pending = root.checkpoint["runtime_checkpoint"]["pending_tool_result"]
    batch = json.loads(pending["content"])
    assert [entry["delegation_status"] for entry in batch["results"]] == ["completed", "cancelled"]

    from packages.core.services.task_coordinator_wakeup import agent_loop_retry_checkpoint

    retried = agent_loop_retry_checkpoint(root.checkpoint)
    answered = next(m for m in retried["messages"] if m.get("tool_call_id") == "deleg")
    assert answered["content"] == pending["content"], "the stored result, not executed: no"
    receipts = retried["tool_evidence"]["receipts"]
    assert [receipt.get("document_id") for receipt in receipts] == ["doc-writer"]
    assert retried["tool_evidence"]["effect_floor"] == "unconfirmed", "the analyst's last round is unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_task_stopped_from_outside_keeps_its_childrens_work_for_a_retry(harness, status):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
    ]
    h.scripts["writer"] = _child_done("writer")
    await h.root_wake()
    writer, analyst = await h.children()
    await h.child_wake(writer.id)
    task = await h.fresh_task()
    task.status = status
    task.status_changed_at = datetime.now(timezone.utc)
    await h.db.commit()
    await runtime_tasks.reconcile_terminal_task_coordinator_runs_once()

    writer, analyst = await h.children()
    assert analyst.phase == "cancelled" and analyst.interrupted
    root = await h.root()
    assert root.phase in P.terminal()
    assert root.checkpoint["task_loop_wait"]["resolution"] == "children_cancelled"
    from packages.core.services.task_coordinator_wakeup import agent_loop_retry_checkpoint

    retried = agent_loop_retry_checkpoint(root.checkpoint)
    answered = next(m for m in retried["messages"] if m.get("tool_call_id") == "deleg")
    batch = json.loads(answered["content"])
    assert [entry["delegation_status"] for entry in batch["results"]] == ["completed", "cancelled"]
    receipts = retried["tool_evidence"]["receipts"]
    assert [receipt.get("document_id") for receipt in receipts] == ["doc-writer"]
    assert retried["tool_evidence"]["effect_floor"] == "unconfirmed"


def _parked_with(pending_result: dict) -> dict:
    call = {"id": "c1", "name": "tiktok_publish", "arguments": {"text": "Launch"}}
    other = {"id": "c2", "name": "record_note", "arguments": {"text": "x"}}
    assistant = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "tiktok_publish", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "record_note", "arguments": "{}"}},
    ]}
    return {"messages": [{"role": "user", "content": "brief"}, assistant], "runtime_checkpoint": {
        "messages": [{"role": "user", "content": "brief"}, assistant],
        "pending_tool_call": call, "remaining_tool_calls": [other],
        "pending_tool_result": pending_result,
    }}


@pytest.mark.parametrize("stored", [
    # A rejected approval's host-written gate result.
    {"id": "c1", "content": json.dumps({"status": "rejected", "action_executed": False}), "gate": True},
    # A person's stored answer to request_input.
    {"id": "c1", "content": json.dumps({"status": "answered", "answer": "warm"})},
])
def test_a_retried_transcript_replays_any_stored_result_of_the_parked_call(stored):
    """Flag-independent: a stored result is the call's truth, never "did not run"."""

    from packages.core.ai.runtime.interrupted_tool_calls import runtime_answer_interrupted_calls

    messages = runtime_answer_interrupted_calls(_parked_with(stored))
    results = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    assert results["c1"] == stored["content"]
    assert json.loads(results["c2"]) == {"status": "interrupted", "executed": "no"}


@pytest.mark.asyncio
async def test_a_retry_is_refused_while_a_delegated_run_is_open(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]
    await h.root_wake()
    task = await h.fresh_task()
    task.status = "blocked"
    await h.db.commit()
    from packages.core.services import task_retry_service, workspace_autonomy

    h.monkeypatch.setattr(workspace_autonomy, "workspace_allows_manual_work", lambda _workspace: True)
    with pytest.raises(task_retry_service.TaskRetryError) as raised:
        await task_retry_service.prepare_task_retry(
            h.db, task=await h.fresh_task(), user_id="user-1", user_label="User",
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == task_retry_service.OPEN_DELEGATED_RUNS_DETAIL
    await h.db.rollback()


# ── readers ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_delegation_result_falls_back_to_the_child_run(harness):
    h = harness
    long_answer = "x" * 50_000
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]
    h.scripts["writer"] = [(long_answer, None), (long_answer, None)]
    await h.root_wake()
    (child,) = await h.children()
    await h.child_wake(child.id)
    pending = (await h.root()).checkpoint["runtime_checkpoint"]["pending_tool_result"]
    assert f"run_id {child.id}" in pending["content"], "a cut answer points at its stored run"
    task = await h.fresh_task()
    page = json.loads(await delegation_actions.runtime_read_delegation_result_action(
        entity_id=task.entity_id, workspace_id=task.workspace_id, user_id="user-1",
        conversation_id=task.conversation_id, params={"run_id": child.id, "offset": 20_000},
    ))
    assert page["run_id"] == child.id and page["total_chars"] == 50_000
    assert page["content"] == long_answer[20_000:40_000] and page["next_offset"] == 40_000
    other = json.loads(await delegation_actions.runtime_read_delegation_result_action(
        entity_id=task.entity_id, workspace_id=task.workspace_id, user_id="user-1",
        conversation_id="another-chat", params={"run_id": child.id},
    ))
    assert other["error"] == "delegation_result_not_found"


@pytest.mark.asyncio
async def test_completion_waits_for_open_delegated_runs(harness):
    h = harness
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer"))]),
    ]
    await h.root_wake()
    from packages.core.workers.task_agent_loop_completion import running_work_problems

    task = await h.fresh_task()
    problems = await running_work_problems(
        h.db, entity_id=task.entity_id, task_id=task.id, conversation_id=None,
    )
    assert problems and "Writer" in problems[0]


def test_execution_evidence_reports_a_loop_waiting_on_its_agents():
    from packages.core.services.workspace_execution_evidence import (
        ExecutionObservationState,
        task_execution_state,
        task_execution_wait,
    )

    now = datetime.now(timezone.utc)
    task = Task(
        id="t", entity_id="e", workspace_id="w", status="in_progress", task_type="general",
        title="T", details={}, status_changed_at=now,
    )
    from packages.core.services.task_execution import task_execution_intent_generation as generation

    coordinator = SimpleNamespace(
        phase="waiting_child", engine="agent_loop", lifecycle_generation=generation(task),
        claim_token=None, claim_generation=None, wake_generation=None, claim_expires_at=None,
        current_plan_id=None,
    )
    idle = SimpleNamespace(agent_label="Writer", phase="ready", claim_token=None, claim_generation=None,
                           wake_generation=None, claim_expires_at=None)
    busy = SimpleNamespace(agent_label="Analyst", phase="running", claim_token="tok", claim_generation="g",
                           wake_generation="g", claim_expires_at=now + timedelta(minutes=1))
    assert task_execution_state(task, coordinator=coordinator, children=(idle,)) is ExecutionObservationState.WAITING
    assert task_execution_state(task, coordinator=coordinator, children=(idle, busy)) is ExecutionObservationState.RUNNING
    waiting = task_execution_wait("waiting_child", {"kind": "waiting_child", "child_ids": ["a", "b"]}, [idle, busy])
    assert waiting == {
        "kind": "waiting_child",
        "children": [{"agent": "Writer", "phase": "ready"}, {"agent": "Analyst", "phase": "running"}],
        "label": "Waiting for 2 delegated agent(s)",
    }


def test_a_delegate_approval_is_resumable_only_with_durable_delegation():
    control = {
        "tool_name": "delegate_to_agent",
        "blocking_result": json.dumps({
            "__hitl__": True, "error": "approval_required", "approval_token": "h1",
            "hitl": {"id": "h1", "prompt": "Delegate?"},
        }),
        "checkpoint": {"pending_tool_call": {"id": "c1", "name": "delegate_to_agent", "arguments": {}}},
    }
    assert worker._approval_park_target(control) == worker._DELEGATED_APPROVAL_CODE
    hitl_id, _envelope, _pre_call = worker._approval_park_target(control, durable_delegation=True)
    assert hitl_id == "h1"


# ── the running cap ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_settled_child_advances_the_earliest_deferred_sibling(harness):
    h = harness
    h.monkeypatch.setenv("TASK_AGENT_CHILD_MAX_RUNNING", "1")
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks("writer", "analyst"))]),
    ]
    h.scripts["writer"] = _child_done("writer", effects=False)
    await h.root_wake()
    writer, analyst = await h.children()
    # The writer holds the only slot; the analyst's wake is deferred.
    event = await h.event(task_agent_child_outbox_dedupe_key(writer.id))
    claim, _ = await durable_run.claim_run_wake(
        h.db, TASK_AGENT_CHILD_RUN_SPEC, key=writer.id, event_id=event.id,
        wake_generation=event.payload["wake_generation"], claim_token=generate_ulid(), lease_seconds=120,
    )
    await h.db.commit()
    assert (await h.child_wake(analyst.id)) == {"status": "deferred", "reason": "running_cap"}
    deferred = await h.event(task_agent_child_outbox_dedupe_key(analyst.id))
    assert deferred.available_at > datetime.now(timezone.utc) + timedelta(seconds=5)

    async with h.host.session_factory() as db:
        assert await settlement.settle_task_agent_child(
            db, child_id=writer.id, event_id=claim.event_id, claim_token=claim.claim_token,
            claim_generation=claim.wake_generation,
            outcome=settlement.ChildOutcome(
                phase="completed", outcome_status="completed", interrupted=False,
                result={"delegated": True, "run_id": writer.id, "status": "completed", "content": "w"},
                checkpoint={}, usage={}, ledger_complete=True,
            ),
        )
        await db.commit()
    advanced = await h.event(task_agent_child_outbox_dedupe_key(analyst.id))
    assert advanced.available_at <= datetime.now(timezone.utc), "the deferred sibling goes now"


# ── guard: an open child always has an open root and an open Task ──────


_STOPPED_TASK = {"completed", "cancelled", "failed", "blocked"}


async def _assert_tree_invariant(h, step: str) -> None:
    children = [child for child in await h.children() if child.phase not in P.terminal()]
    if not children:
        return
    root = await h.root()
    task = await h.fresh_task()
    assert root.phase not in P.terminal(), f"{step}: open child under a {root.phase} root"
    assert task.status not in _STOPPED_TASK, f"{step}: open child under a {task.status} Task"


async def _random_step(h, rng, step: str) -> None:
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake

    task = await h.fresh_task()
    if step == "child":
        due = [
            child for child in await h.children()
            if child.phase not in P.terminal()
            and (event := await h.event(task_agent_child_outbox_dedupe_key(child.id))) is not None
            and event.delivered_at is None
        ]
        if due:
            await h.child_wake(rng.choice(due).id)
    elif step == "root":
        event = await h.event(task_coordinator_outbox_dedupe_key(h.task_id))
        if event is not None and event.delivered_at is None and task.status == "in_progress":
            await h.root_wake()
    elif step == "sweep":
        await runtime_tasks.recover_stalled_task_coordinator_wakes_once()
        await runtime_tasks.recover_stalled_task_agent_child_wakes_once()
        await runtime_tasks.reconcile_terminal_task_coordinator_runs_once()
    elif step == "cancel" and task.status == "in_progress":
        task.status = rng.choice(["cancelled", "failed"])
        task.status_changed_at = datetime.now(timezone.utc)
        await h.db.commit()
        await runtime_tasks.reconcile_terminal_task_coordinator_runs_once()
    elif step == "block" and task.status == "in_progress" and (await h.root()).phase not in P.terminal():
        await stage_task_coordinator_wake(h.db, task_id=h.task_id, reason="test_block")
        await h.db.commit()
        event = await h.event(task_coordinator_outbox_dedupe_key(h.task_id))
        claim, _ = await task_run_host.admit_and_claim(
            event_id=event.id, task_id=h.task_id, wake_generation=event.payload["wake_generation"],
            engine="agent_loop", lease_seconds=60, session_factory=h.host.session_factory,
        )
        if claim is not None:
            await worker._block(
                h.host.session_factory, claim, code="agent_loop_test_stop", message="Stopped.",
                checkpoint=dict((await h.root()).checkpoint or {}),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(4))
async def test_an_open_child_never_outlives_its_root_or_task(harness, seed):
    """Property: across random interleavings, non-terminal child => root and Task non-terminal."""

    import random

    h = harness
    rng = random.Random(seed)
    h.scripts["root"] = [
        ("", [_call("plan", "update_task_plan", PLAN_ARGS)]),
        ("", [_call("deleg", "delegate_to_agent", _tasks(*KEYS))]),
        _complete(),
    ]
    for key in KEYS:
        h.scripts[key] = _child_done(key, effects=False)
    await h.root_wake()
    await _assert_tree_invariant(h, "park")
    steps = rng.choices(["child", "child", "root", "sweep", "cancel", "block"], k=8)
    for step in steps:
        await _random_step(h, rng, step)
        await _assert_tree_invariant(h, step)


def test_the_tree_invariant_check_catches_an_open_child_under_a_stopped_root():
    """Mutation check of the property's own assertion."""

    import asyncio as _asyncio

    class Fake:
        async def children(self):
            return [SimpleNamespace(phase="running")]

        async def root(self):
            return SimpleNamespace(phase="failed")

        async def fresh_task(self):
            return SimpleNamespace(status="blocked")

    with pytest.raises(AssertionError, match="failed root"):
        _asyncio.run(_assert_tree_invariant(Fake(), "mutated"))
