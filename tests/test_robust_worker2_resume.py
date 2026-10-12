"""Transcript resume through the runtime subagent adapter (Section B2 + B4).

Phase 1 resumed a failed attempt's transcript through the one-round follow-up
entrypoint, bypassing subagent selection, the ``max_steps`` clamp and the
``subagent_start`` / ``subagent_end`` bookkeeping. The resume now goes through
``runtime_execute_worker_subagent_loop`` with ``initial_messages``, and the
attempt's result metadata says it resumed (B4).
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import packages.core.workers.internal as internal
from packages.core.ai.runtime import (
    AIRuntimeRequest,
    RuntimeResolver,
    harness,
    runtime_execute_worker_subagent_loop,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.workers.submit_result import SUBMIT_RESULT_TOOL_NAME
from packages.core.workers.transcript import build_worker_transcript_checkpoint


# ── fixtures ──────────────────────────────────────────────────────────


def _fake_ctx():
    return SimpleNamespace(
        system_prompt="sys",
        runtime_envelope=None,
        tools=[{"type": "function", "function": {"name": "existing_tool", "parameters": {}}}],
        tool_profile="profile",
        allowed_tool_names=["existing_tool"],
        model="model-x",
        llm_metadata=None,
    )


class _FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _loop_result(**overrides):
    base = dict(
        content="final prose",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "publish the post"},
            {"role": "assistant", "content": "final prose"},
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        rounds=2,
        tool_calls_made=[],
        stop_reason="completed",
        control=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _step(**extra):
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


def _transcript_checkpoint(rounds: int = 3) -> dict:
    return build_worker_transcript_checkpoint(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "publish the post"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "write_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true, "fs_path": "a.md"}'},
        ],
        rounds=rounds, usage={"prompt_tokens": 4, "completion_tokens": 1}, tool_calls_made=["write_file"],
    )


@pytest.fixture
def worker_env(monkeypatch):
    async def fake_build_agent_context(*args, **kwargs):
        return _fake_ctx()

    async def fake_persist(*args, **kwargs):
        return None

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda ctx: {"envelope": "meta"})
    return monkeypatch


# ── B2: the harness adapter accepts initial_messages ──────────────────


def test_subagent_loop_adapter_declares_initial_messages():
    for entrypoint in (harness.runtime_execute_subagent_loop, harness.runtime_execute_agentic_loop):
        assert "initial_messages" in inspect.signature(entrypoint).parameters, entrypoint.__name__


def test_subagent_loop_adapter_forwards_initial_messages_and_keeps_the_bookkeeping(monkeypatch):
    captured: dict = {}

    async def fake_agentic_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(usage={}, rounds=4, tool_calls_made=[], messages=[])

    monkeypatch.setattr(harness, "runtime_execute_agentic_loop", fake_agentic_loop)
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.SCHEDULED_AGENT_RUN, entity_id="ent_1", user_id="user_1", workspace_id="ws_1",
        )
    )
    transcript = [{"role": "user", "content": "publish"}, {"role": "assistant", "content": "ok"}]

    out = asyncio.run(runtime_execute_worker_subagent_loop(
        runtime_envelope=envelope,
        system_prompt="sys",
        user_message="continue",
        tools=[],
        entity_id="ent_1",
        agent_id=None,
        workspace_id="ws_1",
        conversation_id=None,
        initial_messages=transcript,
        requested_max_rounds=60,
    ))

    assert captured["initial_messages"] == transcript
    assert captured["user_message"] == "continue"
    assert captured["billing_source"] == harness.RUNTIME_WORKER_SUBAGENT_BILLING_SOURCE
    # Subagent selection and the round clamp applied to the resumed run.
    assert captured["max_rounds"] == 60
    assert out.run.spec is not None and out.run.spec.name == "long_research_worker"
    events = [event["type"] for event in envelope.metadata["runtime_events"]]
    assert events[-2:] == ["subagent_start", "subagent_end"]
    assert envelope.metadata["runtime_events"][-1]["rounds"] == 4


# ── B2: the worker resumes through the subagent adapter ───────────────


def test_phase1_resume_shim_is_gone():
    assert not hasattr(internal, "_resume_worker_subagent_loop")
    assert not hasattr(internal, "_DEFAULT_SUBAGENT_MAX_ROUNDS")


def test_exec_subagent_resumes_a_parked_transcript_through_the_worker_subagent_loop(worker_env):
    loop_calls: list[dict] = []
    followup_calls: list[dict] = []

    async def fake_worker_loop(**kwargs):
        loop_calls.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "finished", "result": {"text": "done"},
        })
        return SimpleNamespace(
            result=_loop_result(
                messages=[
                    {"role": "system", "content": "sys"},
                    *kwargs["initial_messages"],
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "done"},
                ],
                stop_reason="submit_result", rounds=1,
            ),
            run=None,
        )

    async def fake_followup(**kwargs):
        followup_calls.append(kwargs)
        raise AssertionError("the main resume must not go through the one-round follow-up")

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)

    step = _step(
        runtime_checkpoint=_transcript_checkpoint(rounds=3),
        prior_error={"type": "OutputSchemaError", "message": "leads too short",
                     "errors": [{"path": "$['leads']", "message": "[] is too short"}]},
        params={"prompt": "publish the post", "max_rounds": 7, "skill_refs": ["docx"],
                "subagent": "long_research_worker",
                "human_input_response": {"note": "use the hero shot", "user": "Amy"}},
    )
    out = asyncio.run(internal._exec_subagent(step))

    assert followup_calls == []
    assert len(loop_calls) == 1
    kwargs = loop_calls[0]
    # The transcript is replayed verbatim (no system message: the loop adds its own).
    assert [m["role"] for m in kwargs["initial_messages"]] == ["user", "assistant", "tool"]
    assert kwargs["initial_messages"][0]["content"] == "publish the post"
    # The new user turn is the feedback, not the original prompt.
    feedback = kwargs["user_message"]
    assert "[Retry context]" in feedback and "leads too short" in feedback
    assert "[Operator guidance]" in feedback and "use the hero shot" in feedback
    assert "Continue from the transcript above" in feedback
    assert kwargs["user_message"] != kwargs["active_user_message"]
    assert "publish the post" in kwargs["active_user_message"]
    # Subagent selection and the round budget stay with the runtime adapter.
    assert kwargs["requested_max_rounds"] == 7
    assert kwargs["requested_name"] == "long_research_worker"
    assert "max_rounds" not in kwargs
    # Tool work is not repeated: no forced skill invocation, no pending call.
    assert kwargs["forced_tool_calls"] is None
    assert kwargs["runtime_checkpoint"] is None
    assert kwargs["human_input_suspension"] is True
    tool_names = [t["function"]["name"] for t in kwargs["tools"]]
    assert SUBMIT_RESULT_TOOL_NAME in tool_names and "existing_tool" in tool_names
    assert out["result"]["text"] == "done"
    assert out["retry_checkpoint"]["messages"][0]["content"] == "publish the post"
    assert out["cost"]["llm_rounds"] == 1


# ── B4: a resumed attempt says so ─────────────────────────────────────


def test_resumed_attempt_reports_the_replayed_transcript_in_its_metadata(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    out = asyncio.run(internal._exec_subagent(_step(runtime_checkpoint=_transcript_checkpoint(rounds=3))))

    assert out["metadata"]["resumed_from_transcript"] == {"rounds": 3, "messages": 3}
    # The runtime metadata is kept, not replaced.
    assert out["metadata"]["envelope"] == "meta"


def test_fresh_attempt_carries_no_resume_marker(worker_env):
    async def fake_worker_loop(**kwargs):
        assert "initial_messages" not in kwargs or kwargs["initial_messages"] is None
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    out = asyncio.run(internal._exec_subagent(_step()))

    assert "resumed_from_transcript" not in out["metadata"]
