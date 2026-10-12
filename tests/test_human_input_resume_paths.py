"""request_input on the other two loops: a plan subagent step parks with its
transcript and resumes at the answered call; a legacy agent task parks on the
question and re-runs with question + answer in context."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import packages.core.workers.internal as internal
from packages.core.ai.runtime import task_agent
from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeAgentCheckpoint,
)
from packages.core.ai.tools.request_input_tool import REQUEST_INPUT_RESULT_KEY
from packages.core.constants.execution import ExecutionStepStatus
from packages.core.services.step_resume import apply_step_cancel, apply_step_resume
from packages.core.services.task_input_resume import task_input_log_meta


def _checkpoint_dict(call_id: str = "call-ask") -> dict:
    return RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "publish"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "name": "request_input", "arguments": {"question": "Which screenshot?"}}
            ]},
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": call_id, "name": "request_input", "arguments": {"question": "Which screenshot?"}},
    ).to_dict()


def _waiting_result(question: str = "Which screenshot?") -> SimpleNamespace:
    control = {
        "kind": HUMAN_INPUT_SUSPENSION_KIND,
        "reservation_id": "",
        "poll_after_seconds": 5,
        "deadline_at": "2026-09-07T00:00:00+00:00",
        "reason": HUMAN_INPUT_SUSPENSION_REASON,
        "payload": {"question": question, "options": ["hero", "detail"]},
        "checkpoint": _checkpoint_dict(),
    }
    return SimpleNamespace(
        stop_reason="waiting_resource", control=control, messages=[], usage={}, content="",
    )


# ── worker helpers ────────────────────────────────────────────────────────


def test_pause_translates_a_human_input_suspension_with_its_checkpoint() -> None:
    pause = internal._human_input_pause_from_result(_waiting_result())
    assert isinstance(pause, internal._NeedsHumanInput)
    assert pause.prompt.startswith("Which screenshot?")
    assert "hero / detail" in pause.prompt
    assert pause.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"


def test_pause_ignores_other_stop_reasons_and_sandbox_waits() -> None:
    done = SimpleNamespace(stop_reason="completed", control=None)
    assert internal._human_input_pause_from_result(done) is None
    sandbox = SimpleNamespace(
        stop_reason="waiting_resource",
        control={"kind": "waiting_resource", "reservation_id": "r1", "checkpoint": {}},
    )
    assert internal._human_input_pause_from_result(sandbox) is None
    max_rounds = SimpleNamespace(stop_reason="max_rounds", control=None)
    assert internal._human_input_pause_from_result(max_rounds) is None


@pytest.mark.parametrize("encoded", [False, True])
def test_approval_card_keeps_exact_pending_action_for_json_and_dict(encoded):
    payload = {"__hitl__": True, "hitl": {
        "id": "exact-approval", "prompt": "Approve configuration?", "tool": "configure",
    }}
    checkpoint = _checkpoint_dict()
    result = SimpleNamespace(stop_reason="hitl_required", control={
        "blocking_result": json.dumps(payload) if encoded else payload, "checkpoint": checkpoint,
    })
    pause = internal._human_input_pause_from_result(result)
    assert pause.prompt == "Approve configuration?"
    assert pause.pending_action["kind"] == "needs_confirmation"
    assert pause.pending_action["approval_token"] == "exact-approval"
    assert pause.runtime_checkpoint == checkpoint


def test_pause_parks_hitl_required_without_raising(worker_env) -> None:
    blocking = {
        "__hitl__": True,
        "hitl": {"prompt": "Approve this tool action?", "tool": "sandbox_exec"},
    }
    hitl_result = SimpleNamespace(
        stop_reason="hitl_required",
        control={
            "kind": "approval_required",
            "tool_name": "sandbox_exec",
            "blocking_result": blocking,
        },
        messages=[],
        usage={},
        content="",
        rounds=1,
        tool_calls_made=["sandbox_exec"],
        error_detail=None,
        error=None,
    )

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=hitl_result, run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    with pytest.raises(internal._NeedsHumanInput) as exc:
        asyncio.run(internal._exec_subagent(_base_step()))

    assert exc.value.pending_action is not None
    assert "Approve this tool action?" in str(exc.value.prompt or "")

    empty = SimpleNamespace(
        stop_reason="hitl_required",
        control={"kind": "approval_required", "tool_name": "update_goal_value"},
        messages=[],
        usage={},
        content="",
    )
    pause = internal._human_input_pause_from_result(empty)
    assert isinstance(pause, internal._NeedsHumanInput)
    assert pause.pending_action is None


def test_resume_checkpoint_carries_the_answer_as_the_pending_result() -> None:
    snapshot = {
        "step_id": "step",
        "runtime_checkpoint": _checkpoint_dict(),
        "params": {
            "prompt": "publish",
            "human_input_response": {"choice": "respond", "note": "use the hero shot", "user": "Amy"},
        },
    }
    resumed = internal._resume_checkpoint_with_answer(snapshot)
    checkpoint = RuntimeAgentCheckpoint.from_dict(resumed)
    assert checkpoint.pending_tool_result["id"] == "call-ask"
    stored = json.loads(checkpoint.pending_tool_result["content"])
    assert stored["answer"] == "use the hero shot"
    assert stored["answered_by"] == "Amy"
    assert stored["choice"] == "respond"


def test_resume_checkpoint_uses_structured_answers_when_no_note() -> None:
    snapshot = {
        "runtime_checkpoint": _checkpoint_dict(),
        "params": {
            "human_input_response": {"choice": "provide_answers", "user": "Amy"},
            "answers": {"screenshot": "hero"},
        },
    }
    stored = json.loads(
        RuntimeAgentCheckpoint.from_dict(
            internal._resume_checkpoint_with_answer(snapshot)
        ).pending_tool_result["content"]
    )
    assert stored["fields"] == {"screenshot": "hero"}


def test_resume_checkpoint_is_skipped_without_answer_or_transcript() -> None:
    assert internal._resume_checkpoint_with_answer({"params": {"human_input_response": {"note": "x"}}}) is None
    assert internal._resume_checkpoint_with_answer({"runtime_checkpoint": _checkpoint_dict(), "params": {}}) is None
    unreadable = {"runtime_checkpoint": {"schema_version": 99}, "params": {"human_input_response": {"note": "x"}}}
    assert internal._resume_checkpoint_with_answer(unreadable) is None


# ── _exec_subagent integration (loop faked) ───────────────────────────────


def _fake_ctx():
    return SimpleNamespace(
        system_prompt="sys",
        runtime_envelope=None,
        tools=[],
        tool_profile="profile",
        allowed_tool_names=set(),
        model="model-x",
        llm_metadata=None,
    )


class _FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _base_step(**extra) -> dict:
    step = {
        "params": {"prompt": "publish the post"},
        "entity_id": "ent",
        "resolved_agent_id": "agent",
        "user_id": "user",
        "workspace_id": "ws",
        "conversation_id": "conv",
        "task_id": "task",
        "step_id": "step",
        "expected_output_schema": None,
    }
    step.update(extra)
    return step


@pytest.fixture
def worker_env(monkeypatch):
    async def fake_build_agent_context(*args, **kwargs):
        return _fake_ctx()

    async def fake_persist(*args, **kwargs):
        return None

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist, raising=False)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda ctx: {}, raising=False)
    return monkeypatch


def test_exec_subagent_parks_the_step_with_its_transcript(worker_env) -> None:
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(result=_waiting_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    with pytest.raises(internal._NeedsHumanInput) as exc:
        asyncio.run(internal._exec_subagent(_base_step()))

    assert captured["human_input_suspension"] is True
    assert captured["runtime_checkpoint"] is None
    assert exc.value.runtime_checkpoint["pending_tool_call"]["name"] == "request_input"
    assert exc.value.prompt.startswith("Which screenshot?")


def test_exec_subagent_resumes_the_transcript_with_the_answer(worker_env) -> None:
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            result=SimpleNamespace(
                stop_reason="submit_result", control=None, messages=[], usage={}, content="",
                rounds=1, tool_calls_made=["request_input"],
            ),
            run=None,
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    # A submit_result payload is captured by the loop; fake the capture.
    worker_env.setattr(
        internal,
        "submit_result_capture",
        lambda: (None, lambda: {"summary": "done", "status": "succeeded", "result": {"ok": True}}),
    )

    step = _base_step(
        runtime_checkpoint=_checkpoint_dict(),
        params={
            "prompt": "publish the post",
            "human_input_response": {"choice": "respond", "note": "hero", "user": "Amy"},
        },
    )
    asyncio.run(internal._exec_subagent(step))

    resumed = RuntimeAgentCheckpoint.from_dict(captured["runtime_checkpoint"])
    assert resumed.pending_tool_result["id"] == "call-ask"
    assert json.loads(resumed.pending_tool_result["content"])["answer"] == "hero"


# ── step row mutations ────────────────────────────────────────────────────


def test_step_resume_keeps_the_checkpoint_and_cancel_drops_it() -> None:
    step = SimpleNamespace(
        params={"prompt": "publish"},
        step_status=ExecutionStepStatus.WAITING_HUMAN.value,
        human_input_prompt="Which screenshot?",
        human_input_response=None,
        current_lease_id="lease",
        error=None,
        finished_at=None,
        runtime_checkpoint=_checkpoint_dict(),
    )
    apply_step_resume(step, human_input_response={"choice": "respond", "note": "hero"})
    assert step.step_status == ExecutionStepStatus.PENDING.value
    assert step.runtime_checkpoint["pending_tool_call"]["id"] == "call-ask"

    apply_step_cancel(step, "user skipped")
    assert step.step_status == ExecutionStepStatus.FAILED.value
    assert step.runtime_checkpoint is None


# ── legacy task loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_turn_parks_on_request_input_and_skips_sibling_calls(monkeypatch) -> None:
    class Msg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    async def fake_chat(**kwargs):
        return SimpleNamespace(
            content="",
            usage={},
            tool_calls=[
                {"id": "c1", "name": "request_input", "arguments": {"question": "Which unit?"}},
                {"id": "c2", "name": "workspace_search", "arguments": {"query": "units"}},
            ],
        )

    executed: list[str] = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append(name)
        if name == "request_input":
            assert args["_runtime_human_input_from_context"] == "end_turn"
            return json.dumps({"ok": True, REQUEST_INPUT_RESULT_KEY: {"question": "Which unit?"}})
        return "{}"

    monkeypatch.setattr(task_agent, "runtime_execute_task_agent_chat", fake_chat)
    monkeypatch.setattr(task_agent, "runtime_execute_tool", fake_execute_tool)
    monkeypatch.setattr(task_agent, "ChatMessage", Msg, raising=False)

    result = await task_agent.runtime_execute_task_agent_turn(
        engine=None,
        messages=[],
        tools=[],
        loaded_tool_names=set(),
        system_prompt="sys",
        runtime_envelope=None,
        entity_id="ent",
        agent_id="agent",
    )
    assert result.input_request == {"question": "Which unit?", "skipped_tool_calls": ["workspace_search"]}
    assert result.had_tool_calls is True
    assert executed == ["request_input"], "calls issued alongside the question must not run"
    # Every tool_call id gets a result so the transcript stays well-formed.
    tool_ids = [m.tool_call_id for m in result.messages if getattr(m, "role", "") == "tool"]
    assert tool_ids == ["c1", "c2"]
    assert json.loads(result.messages[-1].content)["error"] == "not_executed"


def test_task_input_log_meta_matches_what_the_task_page_reads() -> None:
    meta = task_input_log_meta({
        "question": "Which unit?",
        "options": ["4C", "5A"],
        "fields": [{"name": "budget", "label": "Budget", "type": "number", "required": True}],
    })
    assert meta["question"] == "Which unit?"
    assert meta["options"] == ["4C", "5A"]
    assert meta["hitl"] == {
        "kind": "human_input",
        "fields": [{"name": "budget", "label": "Budget", "type": "number", "required": True}],
    }
    assert task_input_log_meta({"question": "Q"})["hitl"]["fields"] == [
        {"name": "response", "type": "textarea", "required": True}
    ]


def test_resumed_task_replays_the_question_as_supervisor_evidence() -> None:
    from packages.core.ai import task_runner

    evidence = task_runner._hitl_supervisor_evidence(
        {"question": "Which unit?", "options": ["4C", "5A"]}, "5A", "Amy",
    )
    assert evidence["tool_name"] == "request_input"
    stored = json.loads(evidence["result"])
    assert stored == {
        "status": "answered",
        "question": "Which unit?",
        "answer": "5A",
        "answered_by": "Amy",
        "note": stored["note"],
    }


def test_resume_checkpoint_accepts_a_structured_answer_without_a_note() -> None:
    """The needs_input card writes only params["answers"]; that must resume too."""
    snapshot = {
        "runtime_checkpoint": _checkpoint_dict(),
        "params": {"prompt": "publish", "answers": {"response": "hero"}},
    }
    stored = json.loads(
        RuntimeAgentCheckpoint.from_dict(
            internal._resume_checkpoint_with_answer(snapshot)
        ).pending_tool_result["content"]
    )
    assert stored["answer"] == "hero"
    assert stored["fields"] == {"response": "hero"}


def test_attachment_only_input_resumes_at_the_question_and_reaches_fallback_prompt() -> None:
    attachments = [{"id": "doc-1", "document_id": "doc-1", "type": "knowledge",
                    "name": "voice.wav", "fs_path": "uploads/voice.wav"}]
    params = {"human_input_response": {"attachments": attachments, "choice": "respond"}}
    checkpoint = RuntimeAgentCheckpoint.from_dict(internal._resume_checkpoint_with_answer({
        "runtime_checkpoint": _checkpoint_dict(), "params": params,
    }))
    assert checkpoint.pending_tool_call["id"] == "call-ask"
    assert json.loads(checkpoint.pending_tool_result["content"])["attachments"] == attachments
    assert "uploads/voice.wav" in internal._human_guidance_note(params)
    legacy_prompt = task_agent.runtime_task_user_prompt(
        base_prompt="Generate a music video", hitl_question="Upload audio",
        hitl_payload={"attachments": attachments},
    )
    assert "Upload audio" in legacy_prompt.prompt
    assert "uploads/voice.wav" in legacy_prompt.prompt


def test_pause_builds_a_structured_card_when_options_exist() -> None:
    pause = internal._human_input_pause_from_result(_waiting_result())
    assert pause.pending_action["kind"] == "needs_input"
    assert pause.pending_action["title"] == "Which screenshot?"
    assert pause.pending_action["questions"][0]["options"] == ["hero", "detail"]
    plain = internal._human_input_pause_from_result(
        SimpleNamespace(
            stop_reason="waiting_resource",
            control={"kind": HUMAN_INPUT_SUSPENSION_KIND, "payload": {"question": "Free text?"},
                     "checkpoint": _checkpoint_dict()},
        )
    )
    assert plain.pending_action is None
