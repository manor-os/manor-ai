"""``request_input`` in a plan subagent step: ask the operator instead of guessing.

Prod task FQ2881 (2026-09-14): the content step could not find the product
positioning it needed, filled the deliverable with "暂缺" and moved on.
Nothing in the worker let it *ask*.

There is one ``request_input`` tool — the runtime registry tool that durable
chat and task loops park on. A plan subagent step offers that same tool and
runs its loop in suspend mode: the call parks the step on the loop's own
transcript (``_human_input_pause_from_result``), the lease parks the step
``waiting_human`` with a card the chat bridge can answer, and the answer
resumes the transcript at the pending call (``_resume_checkpoint_with_answer``)
instead of re-running the step from scratch.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from packages.core.ai.pending_action import KIND_NEEDS_INPUT, LEASE_HITL_CLOSEABLE_KINDS
from packages.core.constants.execution import ExecutionStepStatus
from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeAgentCheckpoint,
)
from packages.core.ai.runtime.human_input import (
    REQUEST_INPUT_PROMPT_SUFFIX,
    REQUEST_INPUT_TOOL_NAME,
    runtime_request_input_tool_schema,
)
from packages.core.services.task_blockers import ANSWERABLE_BLOCKER_KINDS
from packages.core.workers import internal
from packages.core.workers.submit_result import SUBMIT_RESULT_TOOL_NAME


# ── fixtures ─────────────────────────────────────────────────────────────


def _fake_ctx(*, with_request_input: bool = False):
    tools = [{"type": "function", "function": {"name": "existing_tool", "parameters": {}}}]
    allowed = ["existing_tool"]
    if with_request_input:
        tools.append({"type": "function", "function": {"name": REQUEST_INPUT_TOOL_NAME, "parameters": {}}})
        allowed.append(REQUEST_INPUT_TOOL_NAME)
    return SimpleNamespace(
        system_prompt="sys",
        runtime_envelope=None,
        tools=tools,
        tool_profile="profile",
        allowed_tool_names=allowed,
        model="model-x",
        llm_metadata=None,
    )


def _wire_common(monkeypatch, *, with_request_input: bool = False):
    async def fake_build_agent_context(*args, **kwargs):
        return _fake_ctx(with_request_input=with_request_input)

    class _FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())

    async def fake_persist(_envelope):
        return None

    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda ctx: {})


def _checkpoint(call_id: str = "call-ask") -> dict:
    call = {"id": call_id, "name": REQUEST_INPUT_TOOL_NAME, "arguments": {"question": "Which positioning?"}}
    return RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "write the post"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call=call,
    ).to_dict()


def _suspended(payload: dict) -> SimpleNamespace:
    """What the loop returns when the runtime tool parked the turn."""
    return SimpleNamespace(
        stop_reason="waiting_resource",
        control={
            "kind": HUMAN_INPUT_SUSPENSION_KIND,
            "reservation_id": "",
            "poll_after_seconds": 5,
            "deadline_at": "2026-09-19T00:00:00+00:00",
            "reason": HUMAN_INPUT_SUSPENSION_REASON,
            "payload": payload,
            "checkpoint": _checkpoint(),
        },
        messages=[],
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        content="",
        tool_calls_made=[REQUEST_INPUT_TOOL_NAME],
        rounds=1,
    )


_STEP = {
    "params": {"prompt": "write the post"},
    "entity_id": "ent", "resolved_agent_id": "agent", "user_id": "user",
    "workspace_id": "ws", "conversation_id": "conv", "task_id": "task",
    "expected_output_schema": None,
}


def _run_step(monkeypatch, loop_result, *, with_request_input: bool = False) -> dict:
    _wire_common(monkeypatch, with_request_input=with_request_input)
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(result=loop_result, run=None)

    async def fail_fallback(**kwargs):
        raise AssertionError("a paused step must not run the forced submit round")

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fail_fallback)
    with pytest.raises(internal._NeedsHumanInput) as raised:
        asyncio.run(internal._exec_subagent(dict(_STEP)))
    captured["_raised"] = raised.value
    return captured


# ── the step offers the one runtime tool, in suspend mode ────────────────


def test_exec_subagent_offers_the_runtime_request_input_once_in_suspend_mode(monkeypatch):
    captured = _run_step(monkeypatch, _suspended({"question": "Which positioning?"}))

    tool_names = [t["function"]["name"] for t in captured["tools"]]
    assert tool_names.count(REQUEST_INPUT_TOOL_NAME) == 1
    assert SUBMIT_RESULT_TOOL_NAME in tool_names
    # The registry schema, not a worker-private copy: one tool, one contract.
    assert captured["tools"][-1] is runtime_request_input_tool_schema()
    assert REQUEST_INPUT_TOOL_NAME in captured["allowed_tool_names"]
    # Executed by the registry handler, which parks the loop on a checkpoint.
    assert REQUEST_INPUT_TOOL_NAME not in captured["dynamic_tool_handlers"]
    assert captured["human_input_suspension"] is True
    assert REQUEST_INPUT_PROMPT_SUFFIX.strip() in captured["user_message"]


def test_exec_subagent_does_not_duplicate_a_bound_request_input(monkeypatch):
    """A step whose tool binding already includes the tool must not send it
    twice — providers reject duplicate tool names."""
    captured = _run_step(monkeypatch, _suspended({"question": "Which positioning?"}), with_request_input=True)

    tool_names = [t["function"]["name"] for t in captured["tools"]]
    assert tool_names.count(REQUEST_INPUT_TOOL_NAME) == 1
    assert len(tool_names) == len(set(tool_names))


# ── the pause carries the transcript and an askable card ─────────────────


def test_exec_subagent_parks_the_step_on_its_transcript(monkeypatch):
    captured = _run_step(monkeypatch, _suspended({
        "question": "Post in which language?",
        "options": ["中文", "English"],
        "context": "No language requirement was found.",
    }))
    parked = captured["_raised"]

    assert parked.prompt.startswith("Post in which language?")
    assert "中文 / English" in parked.prompt
    assert parked.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"
    card = parked.pending_action
    assert card["kind"] == KIND_NEEDS_INPUT.value
    assert card["questions"][0]["options"] == ["中文", "English"]


def test_free_text_question_parks_with_a_plain_prompt(monkeypatch):
    """Without options or fields the chat layer renders a text-input dialog
    from the prompt; the transcript still resumes at the answered call."""
    captured = _run_step(monkeypatch, _suspended({"question": "Which positioning?"}))
    parked = captured["_raised"]

    assert parked.prompt == "Which positioning?"
    assert parked.pending_action is None
    assert parked.runtime_checkpoint["pending_tool_call"]["name"] == REQUEST_INPUT_TOOL_NAME


def test_needs_input_is_closeable_on_the_lease_and_answerable_from_chat():
    """Minting a kind the chat endpoint cannot close strands the step forever
    (see LEASE_HITL_CLOSEABLE_KINDS); needs_input must stay in both sets."""
    assert KIND_NEEDS_INPUT.value in LEASE_HITL_CLOSEABLE_KINDS
    assert KIND_NEEDS_INPUT.value in ANSWERABLE_BLOCKER_KINDS


def test_prompt_tells_the_model_to_ask_instead_of_guessing():
    assert "request_input" in REQUEST_INPUT_PROMPT_SUFFIX
    assert "placeholder" in REQUEST_INPUT_PROMPT_SUFFIX or "guess" in REQUEST_INPUT_PROMPT_SUFFIX


# ── the answer reaches the resumed attempt ────────────────────────────────


def test_structured_answers_resume_the_transcript_as_the_tool_result():
    """A needs_input card resolves through ``params.answers``; the resumed
    attempt must see that answer as the pending call's result."""
    resumed = internal._resume_checkpoint_with_answer({
        "step_id": "step-1",
        "runtime_checkpoint": _checkpoint(),
        "params": {"answers": {"response": "中文"}},
    })
    assert resumed is not None
    assert resumed["pending_tool_result"]["id"] == "call-ask"
    assert '"answer": "中文"' in resumed["pending_tool_result"]["content"]


def test_needs_input_answers_are_rendered_as_operator_guidance():
    """When there is no transcript to resume (the step never parked on a
    checkpoint), the answer still reaches the retried attempt as guidance."""
    note = internal._human_guidance_note({"answers": {"answer": "Lead with 'AI agents for small business'."}})
    assert note is not None
    assert note.startswith("[Operator guidance]")
    assert "AI agents for small business" in note

    labelled = internal._human_guidance_note({"answers": {"language": "中文", "tone": "founder voice"}})
    assert "language: 中文" in labelled and "tone: founder voice" in labelled
    assert internal._human_guidance_note({"answers": {}}) is None
    assert internal._human_guidance_note({"answers": {"answer": "  "}}) is None


def test_note_and_answers_render_together_without_duplicating_the_prefix():
    note = internal._human_guidance_note({
        "human_input_response": {"note": "Keep it short.", "user": "Lin"},
        "answers": {"answer": "Founder voice."},
    })
    assert note.count("[Operator guidance]") == 1
    assert "Keep it short." in note and "Founder voice." in note


def test_completed_dependency_exposes_recorded_choice_to_following_subagent(monkeypatch):
    """Task 07: the resumed first step knows A, but the second step has no ref."""
    _wire_common(monkeypatch)
    captured = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(result=_suspended({"question": "Which title?"}), run=None)

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    with pytest.raises(internal._NeedsHumanInput):
        asyncio.run(internal._exec_subagent({
            **_STEP,
            "params": {"prompt": "If the user chose A, use A's title."},
            "dependency_context": [{
                "step_key": "propose_and_request_choice",
                "answers": {"choice": "A"},
                "result": {"text": "橙组欢迎会：一起开启新旅程"},
            }],
        }))
    assert '"choice": "A"' in captured["user_message"]
    assert "橙组欢迎会：一起开启新旅程" in captured["user_message"]
    assert captured["user_message"].index(".answers:") < captured["user_message"].index(".result:")
    assert "Do not request an answer that is already recorded" in captured["user_message"]


def test_dependency_context_reads_only_done_steps_in_the_same_plan_and_keeps_order():
    queries = []

    class _Db:
        async def execute(self, statement):
            queries.append(statement)
            return SimpleNamespace(all=lambda: [
                ("second", {"text": "B"}, {}, None),
                ("first", {"text": "A title"}, {"answers": {"choice": "A"}}, None),
                ("unrelated", {"text": "must not appear"}, {}, None),
            ])

    step = SimpleNamespace(kind="subagent", plan_id="plan-1", depends_on=["first", "second"])
    context = asyncio.run(internal._load_dependency_context(_Db(), step))
    assert [item["step_key"] for item in context] == ["first", "second"]
    assert context[0]["answers"] == {"choice": "A"}
    assert context[0]["result"] == {"text": "A title"}
    assert "unrelated" not in str(context)
    statement = queries[0]
    assert "execution_steps.plan_id" in str(statement)
    assert "execution_steps.step_status" in str(statement)
    assert "plan-1" in statement.compile().params.values()
    assert ExecutionStepStatus.DONE.value in statement.compile().params.values()
    assert asyncio.run(internal._load_dependency_context(_Db(), SimpleNamespace(
        kind="action", plan_id="plan-1", depends_on=["first"],
    ))) == []
    assert len(queries) == 1


def test_completed_dependency_is_visible_to_following_llm(monkeypatch):
    captured = {}

    async def fake_llm_step(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="ok", usage={})

    monkeypatch.setattr(internal, "runtime_execute_internal_worker_llm_step", fake_llm_step)
    asyncio.run(internal._exec_llm({
        "params": {"prompt": "Finalize after choice."},
        "dependency_context": [{
            "step_key": "choice",
            "answers": {"choice": "A"},
            "result": {"text": "A title"},
        }],
    }))
    assert '"choice": "A"' in captured["prompt"]
    assert '"text": "A title"' in captured["prompt"]
    assert captured["prompt"].endswith("Finalize after choice.")
