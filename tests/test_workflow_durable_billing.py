"""Workflow agent calls settle each provider call through a durable callback, tied to their run."""

import pytest

from packages.core.ai import workflow_runner
from packages.core.ai.runtime.billing import runtime_current_billing_context
from packages.core.ai.runtime.harness import RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE
from packages.core.services import usage_service


@pytest.mark.asyncio
async def test_settlement_labels_the_row_with_its_source_and_run(monkeypatch):
    captured = {}

    async def fake_settle(db, **kwargs):
        captured.update(kwargs)
        return True

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

        async def rollback(self):
            return None

    monkeypatch.setattr(usage_service, "settle_chat_llm_call_usage", fake_settle)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _Session())
    _reserve, settle = usage_service.build_durable_llm_billing_callbacks(
        source_prefix="workflow:run_1:step_a:nonce",
        entity_id="ent_1",
        user_id="user_1",
        agent_id="agent_1",
        workspace_id="ws_1",
        conversation_id=None,
        fallback_model="openai/gpt-x",
        source=RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE,
        operation_id="run_1",
    )

    await settle(0, 120.0, {"prompt_tokens": 10, "completion_tokens": 5, "model": "openai/gpt-x"})

    assert captured["source"] == RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE
    assert captured["operation_id"] == "run_1"
    assert captured["source_id"] == "workflow:run_1:step_a:nonce:0"


@pytest.mark.asyncio
async def test_each_invocation_binds_a_durable_suppressed_context_and_its_callbacks(monkeypatch):
    seen = {}

    async def fake_loop(**kwargs):
        ctx = runtime_current_billing_context()
        seen["durable"] = ctx.durable_per_call
        seen["suppress"] = ctx.suppress
        seen["source"] = ctx.source
        seen["task_id"] = ctx.task_id
        seen["callbacks"] = (kwargs["on_llm_call_before"], kwargs["on_llm_usage_settled"])
        return "ok"

    monkeypatch.setattr(workflow_runner, "runtime_execute_workflow_agent_loop", fake_loop)
    loop_kwargs = {
        "entity_id": "ent_1",
        "user_id": "user_1",
        "agent_id": "agent_1",
        "workspace_id": "ws_1",
        "conversation_id": None,
        "model": "openai/gpt-x",
        "billing_operation_id": "run_1",
    }

    result = await workflow_runner._run_billed_workflow_loop(
        loop_kwargs,
        step={"id": "step_a"},
        runtime_context={"workflow_run_id": "run_1", "workspace_id": "ws_1", "task_id": "task_1"},
    )

    assert result == "ok"
    assert seen["durable"] is True
    # Non-round calls (tools, completions) keep their automatic record.
    assert seen["suppress"] is False
    assert seen["source"] == RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE
    # A run started from a Task bills its agent steps to that Task.
    assert seen["task_id"] == "task_1"
    assert all(callable(cb) for cb in seen["callbacks"])
    # The bound context is released once the invocation returns.
    assert runtime_current_billing_context() is None


@pytest.mark.asyncio
async def test_a_tool_step_binds_its_run_and_task_for_the_calls_it_makes(monkeypatch):
    from types import SimpleNamespace

    seen = {}

    async def fake_execute(**_kwargs):
        # A media tool reads these when it records its own usage row.
        ctx = runtime_current_billing_context()
        seen.update(
            operation_id=ctx.operation_id, task_id=ctx.task_id,
            workspace_id=ctx.workspace_id, source=ctx.source, suppress=ctx.suppress,
        )
        return SimpleNamespace(output="done", envelope=None)

    async def fake_attach(result, _envelope):
        return result

    monkeypatch.setattr(workflow_runner, "runtime_execute_workflow_tool_step", fake_execute)
    monkeypatch.setattr(workflow_runner, "runtime_attach_and_persist_workflow_runner_result", fake_attach)

    await workflow_runner.WorkflowRunner()._execute_tool_step(
        {"id": "image", "type": "tool", "config": {"tool": "generate_image", "args": {"prompt": "a cat"}}},
        {},
        "ent_1",
        "user_1",
        {"workflow_run_id": "run_1", "workspace_id": "ws_1", "task_id": "task_1"},
    )

    assert seen == {
        "operation_id": "run_1", "task_id": "task_1", "workspace_id": "ws_1",
        "source": RUNTIME_WORKFLOW_AGENT_BILLING_SOURCE,
        # Model calls inside the tool keep their automatic record.
        "suppress": False,
    }
    assert runtime_current_billing_context() is None


def test_the_round_mark_is_scoped_to_the_provider_call_and_restored_on_error():
    from packages.core.ai import llm_client

    assert llm_client._llm_round_settled_by_callback.get() is False
    with pytest.raises(RuntimeError):
        with llm_client.llm_round_settled_by_callback_scope():
            assert llm_client._llm_round_settled_by_callback.get() is True
            raise RuntimeError("provider call failed")
    # A failed round leaves later calls in the context with their own record.
    assert llm_client._llm_round_settled_by_callback.get() is False


@pytest.mark.asyncio
async def test_an_abandoned_round_settles_at_zero_unless_the_provider_billed():
    from packages.core.ai import agentic_loop

    settled = []

    async def settle(round_num, duration_ms, usage):
        settled.append((round_num, usage))

    meter = agentic_loop._RoundStreamMeter(None)
    await agentic_loop._settle_abandoned_round(
        settle, 3, model="m", llm_messages=[{"role": "user", "content": "hello world"}],
        meter=meter, billed=False,
    )
    assert settled[-1] == (3, {"prompt": 0, "completion": 0, "total": 0, "model": "m", "abandoned": True})

    meter.text.append("partial answer streamed before the cancel")
    await agentic_loop._settle_abandoned_round(
        settle, 4, model="m", llm_messages=[{"role": "user", "content": "hello world"}],
        meter=meter, billed=True,
    )
    _round, usage = settled[-1]
    assert usage["partial"] is True
    assert usage["prompt"] > 0 and usage["completion"] > 0


@pytest.mark.asyncio
async def test_the_stream_meter_counts_text_and_keeps_withheld_events():
    from packages.core.ai import agentic_loop
    from packages.core.ai.provider_retry import withheld_stream_events, withhold_stream_events

    seen = []

    async def handler(event_type, payload):
        seen.append(event_type)

    withhold_stream_events(handler, {"text_delta"})
    meter = agentic_loop._RoundStreamMeter(handler)
    await meter.handler("text_delta", {"content": "abc"})
    await meter.handler("text_delta", {"content": "def"})

    assert meter.streamed == "abcdef"
    assert seen == ["text_delta", "text_delta"]
    assert withheld_stream_events(meter.handler) == withheld_stream_events(handler)

@pytest.mark.asyncio
async def test_a_rerun_invocation_gets_a_fresh_idempotency_prefix(monkeypatch):
    prefixes = []

    def fake_builder(**kwargs):
        prefixes.append(kwargs["source_prefix"])
        return (lambda *a, **k: None, lambda *a, **k: None)

    async def fake_loop(**kwargs):
        return None

    monkeypatch.setattr(usage_service, "build_durable_llm_billing_callbacks", fake_builder)
    monkeypatch.setattr(workflow_runner, "runtime_execute_workflow_agent_loop", fake_loop)
    loop_kwargs = {"entity_id": "ent_1", "model": "m"}

    await workflow_runner._run_billed_workflow_loop(loop_kwargs, step={"id": "s"}, runtime_context={"workflow_run_id": "r"})
    await workflow_runner._run_billed_workflow_loop(loop_kwargs, step={"id": "s"}, runtime_context={"workflow_run_id": "r"})

    assert len(prefixes) == 2
    assert prefixes[0] != prefixes[1]


@pytest.mark.asyncio
async def test_a_cloud_provider_call_without_a_billing_context_is_refused(monkeypatch):
    from packages.core.ai import llm_client

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    with pytest.raises(llm_client.UnattributedLLMCallError):
        await llm_client.chat_completion([{"role": "user", "content": "hi"}])
    with pytest.raises(llm_client.UnattributedLLMCallError):
        await llm_client.chat_completion_with_tools([{"role": "user", "content": "hi"}], [])


def test_the_automatic_record_skips_a_round_its_callback_settles(monkeypatch):
    from packages.core.ai import llm_client

    scheduled = []
    monkeypatch.setattr(llm_client, "_schedule_billing_record", lambda *args, **kwargs: scheduled.append(args))
    token = llm_client._billing_ctx_var.set(llm_client.LLMBillingContext(entity_id="ent_1", durable_per_call=True))
    try:
        llm_client._record_llm_call(call_type="completion", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1})
        with llm_client.llm_round_settled_by_callback_scope():
            llm_client._record_llm_call(call_type="completion", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1})
    finally:
        llm_client._billing_ctx_var.reset(token)

    # Only the call outside a round is recorded here; the round's call is the callback's.
    assert len(scheduled) == 1
