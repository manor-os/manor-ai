"""``_exec_subagent`` robustness (Section W1/W2/W3/W6/W7/W8), loop faked.

  * every tool call the subagent makes is captured on the snapshot so a
    failing attempt still reports evidence (W1);
  * every raise after the loop returned carries a transcript checkpoint, and
    a snapshot holding one resumes the conversation instead of restarting
    the step (W2);
  * a hard output contract is corrected in-loop before the attempt fails
    (W3);
  * cost carries a real USD estimate (W6);
  * supervisor feedback reaches the prompt (W7);
  * a coordinator step gets the delegate_to_service tool (W8 wiring).
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

import packages.core.workers.internal as internal
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.constants.execution import (
    CONTRACT_CORRECTION_ROUNDS,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
)
from packages.core.contracts.task_output import (
    MissingOutputContract,
    SubmittedStepFailure,
    plan_output_contract_schema,
)
from packages.core.contracts.shapes import get_shape
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


def _step(schema=None, **extra):
    step = {
        "params": {"prompt": "publish the post"},
        "entity_id": "ent",
        "resolved_agent_id": "agent",
        "user_id": "user",
        "workspace_id": "ws",
        "conversation_id": "conv",
        "task_id": "task",
        "step_id": "step",
        "expected_output_schema": schema,
    }
    step.update(extra)
    return step


@pytest.fixture
def worker_env(monkeypatch):
    async def fake_build_agent_context(*args, **kwargs):
        return _fake_ctx()

    async def fake_persist(*args, **kwargs):
        return None

    async def no_child_cost(_snapshot):
        return {
            "llm_tokens_input": 0,
            "llm_tokens_output": 0,
            "llm_rounds": 0,
            "tool_call_count": 0,
            "llm_usd": 0.0,
            "unpriced_tokens_input": 0,
            "unpriced_tokens_output": 0,
        }

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())
    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda ctx: {})
    monkeypatch.setattr(internal, "coordinator_child_cost_totals", no_child_cost)
    return monkeypatch


def _hard_schema() -> dict:
    return plan_output_contract_schema(
        {
            "type": "object",
            "required": ["leads"],
            "additionalProperties": False,
            "properties": {"leads": {"type": "array", "minItems": 2}},
        },
        source="plan.expected_output_schema",
    )


# ── W1: tool calls are captured as evidence-minable messages ──────────


def test_tool_end_callback_records_a_synthetic_tool_exchange_on_the_snapshot(monkeypatch):
    scheduled: list[dict] = []
    from packages.core.ai import chat_logger

    monkeypatch.setattr(chat_logger, "schedule_tool_call_log", lambda **kw: scheduled.append(kw))

    s = {"entity_id": "ent"}
    on_tool_start, on_tool_end = internal._worker_tool_call_log_callbacks(s)
    on_tool_start("mcp__twitter_x__create_tweet", {"text": "hi"})
    on_tool_end(
        "mcp__twitter_x__create_tweet",
        json.dumps({"tweet_id": "42", "tweet_url": "https://x.com/s/42"}),
        12.0,
        {"text": "hi"},
    )

    captured = s["_captured_tool_messages"]
    assert [m["role"] for m in captured] == ["assistant", "tool"]
    call_id = captured[0]["tool_calls"][0]["id"]
    assert call_id and captured[1]["tool_call_id"] == call_id
    assert captured[0]["tool_calls"][0]["function"]["name"] == "mcp__twitter_x__create_tweet"
    # The pair is exactly what the evidence miner reads.
    evidence = internal._collect_step_evidence(captured)
    assert evidence and evidence[0]["kind"] == "tool_effect"
    assert evidence[0]["fields"]["tweet_id"] == "42"
    # The log row still gets written.
    assert scheduled and scheduled[0]["tool_name"] == "mcp__twitter_x__create_tweet"


def test_tool_end_capture_survives_a_logging_failure(monkeypatch):
    from packages.core.ai import chat_logger

    def _explode(**_kw):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(chat_logger, "schedule_tool_call_log", _explode)
    s: dict = {}
    _, on_tool_end = internal._worker_tool_call_log_callbacks(s)

    on_tool_end("search", {"not": "a string"})

    assert len(s["_captured_tool_messages"]) == 2
    assert s["_captured_tool_messages"][1]["content"] == str({"not": "a string"})


@pytest.mark.parametrize(
    ("name", "payload", "success", "outcome"),
    [
        ("generate_file", {"error": "kind=document requires complete content"}, False, "error"),
        ("generate_file", {"document_id": "doc-1"}, True, "success"),
        ("search_tools", {"matches": []}, True, "empty_result"),
        ("search_tools", {"error": "unavailable", "matches": []}, False, "error"),
    ],
)
def test_worker_tool_log_outcome_uses_shared_runtime_classifier(
    monkeypatch, name, payload, success, outcome,
):
    from packages.core.ai import chat_logger

    scheduled = []
    monkeypatch.setattr(chat_logger, "schedule_tool_call_log", lambda **kw: scheduled.append(kw))
    _, on_tool_end = internal._worker_tool_call_log_callbacks({"entity_id": "ent"})
    on_tool_end(name, json.dumps(payload), 1, {})

    assert scheduled[0]["success"] is success
    assert scheduled[0]["outcome"] == outcome


def test_exec_action_shares_the_capture_list_with_the_original_snapshot(monkeypatch):
    seen: dict = {}

    async def fake_subagent(s):
        seen.update(s)
        s["_captured_tool_messages"].append({"role": "tool", "content": "{}"})
        return {"result": {}}

    monkeypatch.setattr(internal, "_exec_subagent", fake_subagent)
    snapshot = {"kind": "action", "params": {}}

    asyncio.run(internal._exec_action(snapshot))

    assert seen["kind"] == "action"
    assert snapshot["_captured_tool_messages"] == [{"role": "tool", "content": "{}"}]


def test_action_cost_combines_adapter_spend_and_provider_native_llm_usage(worker_env):
    async def allow_action(_snapshot):
        return None

    async def fake_adapter(_snapshot):
        return {"result": {"receipt_id": "r-1"}, "cost": {"usd": 0.25}}

    async def fake_worker_loop(**kwargs):
        await kwargs["dynamic_tool_handlers"]["execute_planned_action"]({})
        return SimpleNamespace(result=_loop_result(
            usage={"prompt": 100, "completion": 20},
            rounds=1,
            tool_calls_made=["execute_planned_action"],
            stop_reason="submit_result",
        ), run=None)

    worker_env.setattr(internal, "_require_active_action_lease", allow_action)
    worker_env.setattr(internal, "_call_action_adapter", fake_adapter)
    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(internal, "_estimate_usd", lambda *_args, **_kwargs: 0.05)

    out = asyncio.run(internal._exec_action(_step(
        kind="action",
        provider="test_provider",
        action_key="publish",
        integration_id="integration-1",
        execution_mode="live",
    )))

    assert out["cost"]["usd"] == pytest.approx(0.30)
    assert out["cost"]["llm_usd"] == pytest.approx(0.05)
    assert out["cost"]["budget_usd"] == pytest.approx(0.25)
    assert out["cost"]["llm_tokens_input"] == 100
    assert out["cost"]["llm_tokens_output"] == 20


# ── W2: raises carry a transcript checkpoint; success returns one ─────


def test_success_returns_a_transcript_checkpoint(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "posted", "result": {"text": "done"},
        })
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    out = asyncio.run(internal._exec_subagent(_step()))

    checkpoint = out["retry_checkpoint"]
    assert checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert checkpoint["messages"][0]["role"] == "user"
    assert checkpoint["rounds"] == 2
    assert checkpoint["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


def test_submitted_failure_carries_the_transcript(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "blocked", "status": "failed",
            "failure": {"reason": "no credentials", "retryable": True, "requires_human": False},
        })
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    async def no_correction(**_kwargs):
        raise AssertionError("a validated failed submission needs no result correction")

    worker_env.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup",
        no_correction,
    )

    with pytest.raises(SubmittedStepFailure) as raised:
        asyncio.run(internal._exec_subagent(_step(_hard_schema())))

    assert raised.value.retry_checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert raised.value.retry_checkpoint["messages"][0]["role"] == "user"


def test_empty_model_output_carries_the_transcript(worker_env):
    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(content="", messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "publish the post"},
            {"role": "assistant", "content": ""},
        ]), run=None)

    async def fake_followup(**kwargs):
        return _loop_result(content="", messages=kwargs["initial_messages"])

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)

    with pytest.raises(internal.EmptyModelOutput) as raised:
        asyncio.run(internal._exec_subagent(_step()))

    assert raised.value.retry_checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA


def test_pending_action_pause_carries_the_transcript(worker_env):
    hitl_message = {
        "role": "tool", "tool_call_id": "c1",
        "content": json.dumps({"__hitl__": True, "approval_token": "tok",
                               "hitl": {"prompt": "approve?", "action": "tool.approve"}}),
    }

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "publish the post"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "publish", "arguments": "{}"}}]},
            hitl_message,
        ]), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    with pytest.raises(internal._NeedsHumanInput) as raised:
        asyncio.run(internal._exec_subagent(_step()))

    assert raised.value.runtime_checkpoint is None
    assert raised.value.retry_checkpoint["messages"][-1]["role"] == "tool"


def test_loop_stop_error_carries_the_transcript(worker_env):
    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(stop_reason="error", error="provider_error",
                                                   content=""), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(internal._exec_subagent(_step()))

    assert getattr(raised.value, "retry_checkpoint")["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA


# ── W2: resume from a parked transcript ───────────────────────────────


def _transcript_checkpoint() -> dict:
    return build_worker_transcript_checkpoint(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "publish the post"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "write_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true, "fs_path": "a.md"}'},
        ],
        rounds=1, usage={"prompt_tokens": 4, "completion_tokens": 1}, tool_calls_made=["write_file"],
    )


def test_resume_helpers_route_a_transcript_checkpoint_to_the_retry_path(caplog):
    snapshot = {
        "step_id": "step",
        "runtime_checkpoint": _transcript_checkpoint(),
        "params": {"prompt": "publish", "human_input_response": {"note": "carry on", "user": "Amy"}},
    }
    with caplog.at_level(logging.WARNING, logger=internal.logger.name):
        assert internal._resume_checkpoint_with_answer(snapshot) is None
    assert not [r for r in caplog.records if "unreadable" in r.getMessage()]

    resumed = internal._resume_transcript_for_retry(snapshot)
    assert [m["role"] for m in resumed] == ["user", "assistant", "tool"]
    assert internal._resume_transcript_for_retry({"runtime_checkpoint": None}) is None
    request_input = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "call", "name": "request_input", "arguments": {}},
    ).to_dict()
    assert internal._resume_transcript_for_retry({"runtime_checkpoint": request_input}) is None


def test_exec_subagent_resumes_a_parked_transcript_with_feedback(worker_env):
    loop_calls: list[dict] = []

    async def fake_worker_loop(**kwargs):
        loop_calls.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "finished", "result": {"text": "done"},
        })
        return SimpleNamespace(
            result=_loop_result(
                messages=[
                    *kwargs["initial_messages"],
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "done"},
                ],
                stop_reason="submit_result",
                rounds=1,
            ),
            run=None,
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    step = _step(
        runtime_checkpoint=_transcript_checkpoint(),
        prior_error={"type": "OutputSchemaError", "message": "leads too short",
                     "errors": [{"path": "$['leads']", "message": "[] is too short"}]},
        params={"prompt": "publish the post", "max_rounds": 7, "skill_refs": ["docx"],
                "human_input_response": {"note": "use the hero shot", "user": "Amy"}},
    )
    out = asyncio.run(internal._exec_subagent(step))

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
    assert "submit_result" in feedback
    # The original prompt still anchors context building.
    assert kwargs["active_user_message"].startswith("[Operator guidance]") or "publish the post" in kwargs["active_user_message"]
    assert kwargs["user_message"] != kwargs["active_user_message"]
    # The native worker adapter resolves the full step budget (not a one-round
    # follow-up shim) after subagent selection.
    assert kwargs["requested_max_rounds"] == 7
    assert kwargs["initial_rounds"] == 1
    assert kwargs["initial_usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 1,
    }
    assert kwargs["initial_tool_calls_made"] == ["write_file"]
    # Tool work is not repeated: no forced skill invocation on resume.
    assert not kwargs.get("forced_tool_calls")
    tool_names = [t["function"]["name"] for t in kwargs["tools"]]
    assert SUBMIT_RESULT_TOOL_NAME in tool_names and "existing_tool" in tool_names
    assert kwargs["human_input_suspension"] is True
    assert kwargs["runtime_checkpoint"] is None
    assert out["result"]["text"] == "done"
    # The returned checkpoint continues from the resumed transcript.
    assert out["retry_checkpoint"]["messages"][0]["content"] == "publish the post"
    assert out["cost"]["llm_rounds"] == 1


def test_request_input_checkpoint_takes_precedence_over_the_transcript_path(worker_env):
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "done", "result": {"text": "ok"},
        })
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "publish"}],
        usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "call-ask", "name": "request_input", "arguments": {"question": "?"}},
    ).to_dict()
    step = _step(
        runtime_checkpoint=checkpoint,
        params={"prompt": "publish the post",
                "human_input_response": {"choice": "respond", "note": "hero", "user": "Amy"}},
    )

    asyncio.run(internal._exec_subagent(step))

    assert captured["runtime_checkpoint"]["pending_tool_result"]["id"] == "call-ask"
    assert "initial_messages" not in captured


# ── W3: in-loop contract correction ───────────────────────────────────


@pytest.mark.parametrize(
    ("schema", "repaired_result"),
    [
        (
            plan_output_contract_schema(
                get_shape("TextResult").json_schema(), source="plan.output_shape",
            ),
            {"text": "渠道｜标题｜一致性核查\n邮件｜标题｜一致"},
        ),
        (
            plan_output_contract_schema(
                {
                    "type": "object", "required": ["result"],
                    "properties": {"result": {"type": "string"}},
                    "additionalProperties": False,
                },
                source="plan.expected_output_schema",
            ),
            {"result": "渠道｜标题｜一致性核查\n邮件｜标题｜一致"},
        ),
    ],
)
@pytest.mark.parametrize(
    "initial_payload",
    [{}, {"summary": "table ready", "status": "succeeded"}],
)
def test_missing_hard_submit_result_is_repaired_in_same_lease(
    worker_env, schema, repaired_result, initial_payload,
):
    followups: list[dict] = []

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](initial_payload)
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def fake_followup(**kwargs):
        followups.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "table delivered", "status": "succeeded",
            "result": repaired_result,
        })
        return _loop_result(
            messages=[
                {"role": "system", "content": "sys"},
                *kwargs["initial_messages"],
                {"role": "assistant", "content": "corrected"},
            ],
            rounds=1,
            tool_calls_made=[SUBMIT_RESULT_TOOL_NAME],
            stop_reason="submit_result",
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup",
        fake_followup,
    )

    out = asyncio.run(internal._exec_subagent(_step(schema)))

    assert out["result"] == repaired_result
    assert len(followups) == 1
    assert [tool["function"]["name"] for tool in followups[0]["tools"]] == [
        SUBMIT_RESULT_TOOL_NAME,
    ]
    assert followups[0]["allowed_tool_names"] == [SUBMIT_RESULT_TOOL_NAME]
    feedback = followups[0]["initial_messages"][-1]["content"]
    assert "[Contract correction]" in feedback
    assert "$.result" in feedback
    assert next(iter(repaired_result)) in feedback
    assert out["retry_checkpoint"]["tool_calls_made"] == [SUBMIT_RESULT_TOOL_NAME]
    assert out["retry_checkpoint"]["rounds"] == 3


@pytest.mark.parametrize("recover", [True, False])
def test_absent_submission_continues_bounded_result_only_repair(worker_env, recover):
    followups: list[dict] = []
    schema = plan_output_contract_schema(
        get_shape("TextResult").json_schema(), source="plan.output_shape",
    )

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(), run=None)

    async def fake_followup(**kwargs):
        followups.append(kwargs)
        if recover and len(followups) == 2:
            kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
                "status": "succeeded", "result": {"text": "saved result"},
            })
        return _loop_result(
            messages=[
                {"role": "system", "content": "sys"},
                *kwargs["initial_messages"],
                {"role": "assistant", "content": "already saved"},
            ],
            rounds=1,
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup,
    )
    if recover:
        out = asyncio.run(internal._exec_subagent(_step(schema)))
        assert out["result"] == {"text": "saved result"}
        assert len(followups) == 2
    else:
        with pytest.raises(MissingOutputContract):
            asyncio.run(internal._exec_subagent(_step(schema)))
        assert len(followups) == 1 + CONTRACT_CORRECTION_ROUNDS
    for followup in followups:
        assert [tool["function"]["name"] for tool in followup["tools"]] == [SUBMIT_RESULT_TOOL_NAME]
        assert followup["allowed_tool_names"] == [SUBMIT_RESULT_TOOL_NAME]
    assert "[Contract correction]" in followups[1]["initial_messages"][-1]["content"]


def test_missing_hard_submit_result_exhausts_correction_budget_fail_closed(worker_env):
    followups: list[dict] = []
    schema = plan_output_contract_schema(
        get_shape("TextResult").json_schema(), source="plan.output_shape",
    )

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({})
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def fake_followup(**kwargs):
        followups.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({})
        return _loop_result(
            messages=[
                {"role": "system", "content": "sys"},
                *kwargs["initial_messages"],
                {"role": "assistant", "content": "still empty"},
            ],
            rounds=1,
            tool_calls_made=[SUBMIT_RESULT_TOOL_NAME],
            stop_reason="submit_result",
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup",
        fake_followup,
    )

    with pytest.raises(MissingOutputContract) as raised:
        asyncio.run(internal._exec_subagent(_step(schema)))

    assert len(followups) == CONTRACT_CORRECTION_ROUNDS
    assert raised.value.retry_checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert sum(
        "[Contract correction]" in str(message.get("content"))
        for message in raised.value.retry_checkpoint["messages"]
    ) == CONTRACT_CORRECTION_ROUNDS
    assert "result" in str(raised.value)


def test_natural_stop_cannot_coerce_missing_capabilities_into_success(worker_env):
    """The whole worker preserves the host diagnostic after a no-submit fallback."""
    snapshot = _step(params={
        "prompt": "Return the completed research", "required_capabilities": ["web.safe_search"],
    })
    diagnostic = {
        "contract": "tool_error", "status": "error", "tool": "submit_result",
        "error": "task_required_capabilities_missing", "message": "Required capability is unavailable",
        "details": {
            "required_capabilities": ["web.safe_search"], "missing_capabilities": ["web.safe_search"],
            "agent_id": "agent", "subscription_id": "subscription", "service_key": "research",
            "requires_human": False, "action_executed": False,
        },
    }
    calls = []

    async def fake_worker_loop(**kwargs):
        calls.append("worker")
        return SimpleNamespace(result=_loop_result(
            content="Research complete.",
            messages=[{"role": "assistant", "content": "Research complete."}],
        ), run=None)

    async def fake_followup(**kwargs):
        calls.append("followup")
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [SUBMIT_RESULT_TOOL_NAME]
        return _loop_result(messages=kwargs["initial_messages"], rounds=1, usage={})

    async def missing(current):
        calls.append("fresh_readiness")
        assert current is snapshot
        return diagnostic

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)
    worker_env.setattr(internal, "_required_step_capabilities_error", missing)

    with pytest.raises(SubmittedStepFailure) as raised:
        asyncio.run(internal._exec_subagent(snapshot))

    assert calls == ["worker", "followup", "fresh_readiness"]
    assert raised.value.failure["requires_human"] is False
    assert raised.value.failure["retryable"] is False
    assert raised.value.failure["diagnostic"] == diagnostic
    assert raised.value.retry_checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert "Research complete." in json.dumps(raised.value.retry_checkpoint["messages"])


def _invalid_submission():
    return {"summary": "here", "status": "succeeded", "result": {"leads": []}}


def _valid_submission():
    return {"summary": "here", "status": "succeeded",
            "result": {"leads": [{"name": "a"}, {"name": "b"}]}}


def test_hard_contract_violation_is_corrected_in_loop(worker_env):
    followup_calls: list[dict] = []

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return SimpleNamespace(result=_loop_result(
            stop_reason="submit_result",
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cache_read": 1,
                "cost_usd": 0.03,
            },
            tool_calls_made=["existing_tool"],
        ), run=None)

    async def fake_followup(**kwargs):
        followup_calls.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_valid_submission())
        return _loop_result(
            messages=[{"role": "system", "content": "sys"}, *kwargs["initial_messages"],
                      {"role": "assistant", "content": "corrected"}],
            usage={
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "cache_read": 2,
                "cost_usd": 0.02,
            },
            rounds=1,
            tool_calls_made=[SUBMIT_RESULT_TOOL_NAME],
            stop_reason="submit_result",
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)

    out = asyncio.run(internal._exec_subagent(_step(_hard_schema())))

    assert out["result"] == {"leads": [{"name": "a"}, {"name": "b"}]}
    assert len(followup_calls) == 1
    kwargs = followup_calls[0]
    correction = kwargs["initial_messages"][-1]
    assert correction["role"] == "user"
    assert "[Contract correction]" in correction["content"]
    assert "$['leads']" in correction["content"]
    assert "submit_result" in correction["content"]
    # No duplicated system message in the replayed transcript.
    assert all(m["role"] != "system" for m in kwargs["initial_messages"])
    assert [t["function"]["name"] for t in kwargs["tools"]] == [SUBMIT_RESULT_TOOL_NAME]
    assert kwargs["allowed_tool_names"] == [SUBMIT_RESULT_TOOL_NAME]
    assert kwargs["max_rounds"] == 1
    # Correction usage rolls into cost.
    assert out["cost"]["llm_tokens_input"] == 13
    assert out["cost"]["llm_tokens_output"] == 7
    assert out["cost"]["llm_rounds"] == 3
    assert out["cost"]["tool_call_count"] == 2
    assert out["cost"]["llm_usd"] == out["cost"]["usd"]
    assert out["cost"]["budget_usd"] == 0.0
    # The checkpoint includes the correction exchange.
    assert any("[Contract correction]" in str(m.get("content")) for m in out["retry_checkpoint"]["messages"])
    assert out["retry_checkpoint"]["usage"]["total_tokens"] == 20
    assert out["retry_checkpoint"]["usage"]["cache_read"] == 3
    assert out["retry_checkpoint"]["usage"]["cost_usd"] == pytest.approx(0.05)
    assert out["retry_checkpoint"]["tool_calls_made"] == [
        "existing_tool",
        SUBMIT_RESULT_TOOL_NAME,
    ]


def test_followup_round_is_counted_even_without_usage_telemetry():
    attempt = internal._AttemptTranscript(_loop_result(rounds=2))

    attempt.extend(_loop_result(
        messages=attempt.messages,
        usage={},
        rounds=1,
        tool_calls_made=[SUBMIT_RESULT_TOOL_NAME],
    ))

    assert attempt.rounds == 3
    assert attempt.prompt_tokens == 10
    assert attempt.completion_tokens == 5
    assert attempt.tool_calls_made == [SUBMIT_RESULT_TOOL_NAME]


def test_hard_contract_still_failing_after_the_budget_raises_step_contract_failure(worker_env):
    followup_calls: list[dict] = []

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def fake_followup(**kwargs):
        followup_calls.append(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return _loop_result(
            messages=[*kwargs["initial_messages"], {"role": "assistant", "content": "still wrong"}],
            usage={"prompt_tokens": 1, "completion_tokens": 1}, rounds=1, stop_reason="submit_result",
        )

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)

    with pytest.raises(internal.StepContractFailure) as raised:
        asyncio.run(internal._exec_subagent(_step(_hard_schema())))

    assert len(followup_calls) == CONTRACT_CORRECTION_ROUNDS
    # The last round replays every earlier correction turn plus its own.
    last = followup_calls[-1]["initial_messages"]
    assert sum("[Contract correction]" in str(m.get("content")) for m in last) == CONTRACT_CORRECTION_ROUNDS
    assert "[Contract correction]" in str(last[-1]["content"])
    assert raised.value.errors and raised.value.errors[0]["path"] == "$['leads']"
    assert raised.value.retry_checkpoint["schema_version"] == WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
    assert "[Contract correction]" in json.dumps(raised.value.retry_checkpoint["messages"])


def test_correction_round_without_a_new_submission_keeps_the_old_payload(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def fake_followup(**kwargs):
        return _loop_result(messages=kwargs["initial_messages"], rounds=1, usage={})

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_followup)

    with pytest.raises(internal.StepContractFailure):
        asyncio.run(internal._exec_subagent(_step(_hard_schema())))


def test_advisory_schema_violation_does_not_trigger_a_correction_round(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def fail_followup(**kwargs):
        raise AssertionError("advisory schemas are not corrected in-loop")

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fail_followup)

    out = asyncio.run(internal._exec_subagent(_step(
        {"type": "object", "required": ["leads"],
         "properties": {"leads": {"type": "array", "minItems": 2}}},
    )))
    assert out["result"]["leads"] == []


def test_correction_round_failure_is_swallowed_and_the_attempt_fails_cleanly(worker_env):
    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](_invalid_submission())
        return SimpleNamespace(result=_loop_result(stop_reason="submit_result"), run=None)

    async def exploding_followup(**kwargs):
        raise RuntimeError("provider down")

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", exploding_followup)

    with pytest.raises(internal.StepContractFailure):
        asyncio.run(internal._exec_subagent(_step(_hard_schema())))


# ── W6: real USD estimate ─────────────────────────────────────────────


def test_subagent_cost_carries_a_priced_usd_estimate(worker_env):
    priced: list[tuple] = []

    def fake_estimate(input_tokens, output_tokens, model=None, **_kw):
        priced.append((input_tokens, output_tokens, model))
        return 0.42

    worker_env.setattr(internal, "estimate_token_cost_usd", fake_estimate)

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    out = asyncio.run(internal._exec_subagent(_step()))

    assert out["cost"]["usd"] == 0.42
    assert priced == [(10, 5, "model-x")]


def test_subagent_cost_falls_back_to_zero_when_pricing_fails(worker_env):
    def exploding_estimate(*_a, **_kw):
        raise RuntimeError("no pricing table")

    worker_env.setattr(internal, "estimate_token_cost_usd", exploding_estimate)

    async def fake_worker_loop(**kwargs):
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    out = asyncio.run(internal._exec_subagent(_step()))
    assert out["cost"]["usd"] == 0.0


# ── W7: supervisor feedback in the guidance note ──────────────────────


def test_human_guidance_note_renders_supervisor_feedback():
    note = internal._human_guidance_note({SUPERVISOR_FEEDBACK_PARAM_KEY: "only 3 of 10 leads had a source_url"})

    assert note.startswith(
        "[Supervisor feedback] The previous run of this step was reviewed and found "
        "insufficient: only 3 of 10 leads had a source_url"
    )
    assert note.endswith("Apply this guidance directly in the current attempt.")


def test_human_guidance_note_combines_supervisor_feedback_with_the_operator_answer():
    note = internal._human_guidance_note({
        "human_input_response": {"note": "use the CRM export", "user": "Amy"},
        SUPERVISOR_FEEDBACK_PARAM_KEY: "the report is missing Q3",
    })
    assert "[Operator guidance] Amy answered" in note
    assert "[Supervisor feedback]" in note and "missing Q3" in note
    assert internal._human_guidance_note({SUPERVISOR_FEEDBACK_PARAM_KEY: "   "}) is None
    assert internal._human_guidance_note({SUPERVISOR_FEEDBACK_PARAM_KEY: 12}) is None


def test_supervisor_feedback_reaches_the_subagent_prompt(worker_env):
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    asyncio.run(internal._exec_subagent(_step(
        params={"prompt": "publish the post", SUPERVISOR_FEEDBACK_PARAM_KEY: "the post lacks a CTA"},
    )))

    assert "[Supervisor feedback]" in captured["user_message"]
    assert "the post lacks a CTA" in captured["user_message"]


# ── W8: coordinator wiring ────────────────────────────────────────────


def test_coordinator_step_gets_the_delegate_tool(worker_env):
    captured: dict = {}

    async def checklist_complete(_snapshot):
        return None

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        await kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(internal, "coordinator_submission_error", checklist_complete)

    asyncio.run(internal._exec_subagent(_step(
        params={"prompt": "coordinate", COORDINATOR_PARAM_KEY: True,
                "delegate_services": ["research", "writer"]},
    )))

    delegate = next(t for t in captured["tools"] if t["function"]["name"] == DELEGATE_TO_SERVICE_TOOL_NAME)
    assert delegate["function"]["parameters"]["properties"]["service_key"]["enum"] == ["research", "writer"]
    assert DELEGATE_TO_SERVICE_TOOL_NAME in captured["dynamic_tool_handlers"]
    assert DELEGATE_TO_SERVICE_TOOL_NAME in captured["allowed_tool_names"]
    assert asyncio.iscoroutinefunction(captured["dynamic_tool_handlers"][DELEGATE_TO_SERVICE_TOOL_NAME])


def test_coordinator_parent_folds_durable_child_cost_once(worker_env):
    priced: list[tuple[int, int]] = []

    async def checklist_complete(_snapshot):
        return None

    async def child_cost(_snapshot):
        return {
            "llm_tokens_input": 14,
            "llm_tokens_output": 5,
            "llm_rounds": 2,
            "tool_call_count": 2,
            "llm_usd": 0.10,
            "unpriced_tokens_input": 0,
            "unpriced_tokens_output": 0,
        }

    async def fake_worker_loop(**kwargs):
        await kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    def fake_estimate(input_tokens, output_tokens, **_kwargs):
        priced.append((input_tokens, output_tokens))
        return 0.25

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(internal, "coordinator_submission_error", checklist_complete)
    worker_env.setattr(internal, "coordinator_child_cost_totals", child_cost)
    worker_env.setattr(internal, "_estimate_usd", fake_estimate)

    out = asyncio.run(internal._exec_subagent(_step(
        params={
            "prompt": "coordinate",
            COORDINATOR_PARAM_KEY: True,
            "delegate_services": ["research"],
        },
    )))

    assert priced == [(10, 5)]
    assert out["cost"] == {
        "llm_tokens_input": 24,
        "llm_tokens_output": 10,
        "llm_rounds": 4,
        "tool_call_count": 2,
        "usd": 0.35,
        "llm_usd": 0.35,
        "budget_usd": 0.0,
    }


def test_typed_coordinator_does_not_double_roll_up_child_cost(worker_env):
    async def checklist_complete(_snapshot):
        return None

    async def child_cost(_snapshot):
        return {
            "llm_tokens_input": 14,
            "llm_tokens_output": 5,
            "llm_rounds": 2,
            "tool_call_count": 2,
            "llm_usd": 0.10,
            "unpriced_tokens_input": 0,
            "unpriced_tokens_output": 0,
        }

    async def fake_worker_loop(**kwargs):
        await kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(internal, "coordinator_submission_error", checklist_complete)
    worker_env.setattr(internal, "coordinator_child_cost_totals", child_cost)
    worker_env.setattr(internal, "_estimate_usd", lambda *_args, **_kwargs: 0.25)

    out = asyncio.run(internal._exec_subagent(_step(
        params={
            "prompt": "coordinate",
            COORDINATOR_PARAM_KEY: True,
            COORDINATOR_VERSION_PARAM_KEY: COORDINATOR_TYPED_VERSION,
            "delegate_services": ["research"],
        },
    )))

    assert out["cost"]["usd"] == 0.25
    assert out["cost"]["llm_tokens_input"] == 10
    assert out["cost"]["llm_tokens_output"] == 5
    assert out["cost"]["coordinator_children"]["llm_usd"] == 0.10


def test_ordinary_step_installs_the_pre_io_effect_intent_observer(worker_env):
    observed: list[tuple[str, dict, str | None]] = []

    async def fake_intent(
        _snapshot,
        tool_name,
        arguments,
        tool_call_id,
        _classification,
        _pending_checkpoint=None,
        _approval_request_id=None,
    ):
        observed.append((tool_name, arguments, tool_call_id))

    async def fake_worker_loop(**kwargs):
        from packages.core.ai.runtime.tool_effect_intents import (
            runtime_record_tool_effect_intent,
            runtime_tool_effect_checkpoint_scope,
        )
        from packages.core.ai.runtime.control import RuntimeAgentCheckpoint

        checkpoint = RuntimeAgentCheckpoint(
            messages=[], usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call={"id": "call-publish", "name": "mcp__publisher__publish", "arguments": {"text": "hello"}},
        ).to_dict()
        with runtime_tool_effect_checkpoint_scope({"call-publish": checkpoint}):
            await runtime_record_tool_effect_intent(
                "mcp__publisher__publish", {"text": "hello"}, "call-publish",
            )
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "_persist_worker_tool_effect_intent", fake_intent)
    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    asyncio.run(internal._exec_subagent(_step()))

    assert observed == [(
        "mcp__publisher__publish",
        {"text": "hello"},
        "call-publish",
    )]


def test_ordinary_step_does_not_get_the_delegate_tool(worker_env):
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({"summary": "ok", "result": {"text": "x"}})
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)

    asyncio.run(internal._exec_subagent(_step()))

    assert DELEGATE_TO_SERVICE_TOOL_NAME not in [t["function"]["name"] for t in captured["tools"]]
    assert DELEGATE_TO_SERVICE_TOOL_NAME not in captured["dynamic_tool_handlers"]


def test_folded_llm_uses_agent_loop_without_widening_tool_authority(worker_env):
    captured: dict = {}

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    async def one_shot_must_not_run(_snapshot):
        raise AssertionError("folded llm used the one-shot completion path")

    worker_env.setattr(
        internal,
        "runtime_execute_worker_subagent_loop",
        fake_worker_loop,
    )
    worker_env.setattr(internal, "_exec_llm", one_shot_must_not_run)

    snapshot = _step(
        kind="llm",
        params={
            "prompt": "write the answer",
            COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
        },
    )
    out = asyncio.run(internal._execute_by_kind(snapshot))

    tool_names = {
        tool["function"]["name"]
        for tool in captured["tools"]
    }
    assert tool_names == {"request_input", SUBMIT_RESULT_TOOL_NAME}
    assert captured["allowed_tool_names"] == [
        "request_input",
        SUBMIT_RESULT_TOOL_NAME,
    ]
    assert out["result"] == {
        "status": "succeeded",
        "summary": "ok",
        "text": "x",
    }


@pytest.mark.parametrize("kind", ["subagent", "llm"])
def test_step_system_guidance_cannot_replace_bound_agent_prompt(worker_env, kind):
    captured: dict = {}
    context_request: dict = {}

    async def fake_build_agent_context(*_args, **kwargs):
        context_request.update(kwargs)
        return _fake_ctx()

    async def fake_worker_loop(**kwargs):
        captured.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](
            {"summary": "ok", "result": {"text": "x"}}
        )
        return SimpleNamespace(result=_loop_result(), run=None)

    worker_env.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    worker_env.setattr(
        "packages.core.ai.context.build_agent_context", fake_build_agent_context,
    )
    params = {
        "prompt": "write the answer",
        "system_prompt": "Plan-authored style guidance",
    }
    if kind == "llm":
        params[COORDINATOR_TYPED_CHILD_PARAM_KEY] = True
    out = asyncio.run(internal._execute_by_kind(_step(
        kind=kind, params=params, resolved_subscription_id="sub-1",
    )))

    assert context_request["agent_id"] == "agent"
    assert context_request["agent_subscription_id"] == "sub-1"
    assert context_request["task_id"] == "task"
    assert captured["system_prompt"] == "sys"
    assert "[Plan step guidance]\nPlan-authored style guidance" in captured["user_message"]
    assert "write the answer" in captured["user_message"]
    tool_names = {tool["function"]["name"] for tool in captured["tools"]}
    if kind == "subagent":
        assert "existing_tool" in tool_names
    else:
        assert tool_names == {"request_input", SUBMIT_RESULT_TOOL_NAME}
    assert out["result"]["text"] == "x"
