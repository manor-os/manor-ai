"""Action execution is LLM-selected, scope-bound, and receipt-backed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from packages.core.contracts.task_output import SubmittedStepFailure
from packages.core.workers import internal
from test_submit_result_finalization import _loop_result, _step, _wire_common as _wire_agent_context

pytestmark = pytest.mark.oss_regression


def _wire_common(monkeypatch):
    _wire_agent_context(monkeypatch)

    async def current_lease(snapshot):
        return None

    monkeypatch.setattr(internal, "_require_active_action_lease", current_lease)


def _action():
    return {**_step(), "kind": "action", "provider": "twitter_x", "action_key": "create_tweet",
            "execution_mode": "live", "integration_id": "approved-account",
            "params": {"text": "approved text"}, "task_binding_constraints": ["Do not change the copy"]}


@pytest.mark.parametrize("value", [{"tweet_id": "123"}, [1, 2], "ready", None])
async def test_llm_executes_exact_action_once_and_cannot_rewrite_receipt(monkeypatch, value):
    _wire_common(monkeypatch)
    calls = []

    async def adapter(snapshot):
        calls.append(snapshot)
        return {"result": value, "cost": {"api_calls": 1}}

    async def loop(**kwargs):
        assert kwargs["allowed_tool_names"] == ["execute_planned_action", "submit_result"]
        assert "Do not change the copy" in kwargs["user_message"]
        execute = kwargs["dynamic_tool_handlers"]["execute_planned_action"]
        assert "error" in json.loads(await execute({"text": "changed", "integration_id": "other"}))
        assert calls == []
        first, duplicate = await asyncio.gather(execute({}), execute({}))
        assert first == duplicate
        kwargs["dynamic_tool_handlers"]["submit_result"]({
            "status": "done", "summary": "claimed", "result": {"tweet_id": "invented"},
        })
        return SimpleNamespace(result=_loop_result(), run=None)

    monkeypatch.setattr(internal, "_call_action_adapter", adapter)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    outcome = await internal._execute_by_kind(_action())
    assert len(calls) == 1
    assert calls[0]["params"] == {"text": "approved text"}
    assert calls[0]["integration_id"] == "approved-account"
    assert outcome["result"] == value
    assert outcome["cost"]["api_calls"] == 1
    assert outcome["cost"]["llm_tokens_input"] == 10
    assert any(e["effect"] == "action" for e in outcome["evidence_refs"])
    if isinstance(value, dict):
        assert any(e["effect"] == "publish" and e["fields"]["tweet_id"] == "123"
                   for e in outcome["evidence_refs"])


async def test_model_claim_without_tool_call_is_not_action_success(monkeypatch):
    _wire_common(monkeypatch)

    async def loop(**kwargs):
        kwargs["dynamic_tool_handlers"]["submit_result"]({
            "status": "done", "summary": "posted", "result": {"tweet_id": "invented"},
        })
        return SimpleNamespace(result=_loop_result(), run=None)

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    with pytest.raises(SubmittedStepFailure) as error:
        await internal._execute_by_kind(_action())
    assert error.value.failure["retryable"] is False


@pytest.mark.parametrize("signal", [
    internal._NeedsHumanInput("Approve this action"),
    internal._FreshMCPApprovalRequired(provider="remote", action_key="publish", effect="write"),
])
async def test_action_preserves_approval_control_and_does_not_repeat_adapter(monkeypatch, signal):
    _wire_common(monkeypatch)
    calls = []

    async def adapter(snapshot):
        calls.append(snapshot)
        raise signal

    async def loop(**kwargs):
        execute = kwargs["dynamic_tool_handlers"]["execute_planned_action"]
        await execute({})
        await execute({})
        return SimpleNamespace(result=_loop_result(), run=None)

    monkeypatch.setattr(internal, "_call_action_adapter", adapter)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    with pytest.raises(type(signal)):
        await internal._execute_by_kind(_action())
    assert len(calls) == 1


async def test_simulated_action_never_creates_external_effect_evidence(monkeypatch):
    _wire_common(monkeypatch)

    async def adapter(snapshot):
        return {"result": {"tweet_id": "simulated"}}

    async def loop(**kwargs):
        await kwargs["dynamic_tool_handlers"]["execute_planned_action"]({})
        return SimpleNamespace(result=_loop_result(), run=None)

    monkeypatch.setattr(internal, "_call_action_adapter", adapter)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    outcome = await internal._execute_by_kind({**_action(), "execution_mode": "dry_run"})
    assert outcome["evidence_refs"] == []


async def test_planned_action_runs_through_real_llm_tool_loop_and_runtime_gate(monkeypatch):
    from dataclasses import replace
    from packages.core.ai.agentic_loop import agentic_loop
    from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
    from packages.core.ai.runtime.tool_execution import runtime_execute_scoped_dynamic_tool_handler

    _wire_common(monkeypatch)
    # This test exercises the model loop and Runtime gate without a persisted
    # WorkLease; the effect-result database fence is covered separately below.
    async def persist_result(*args):
        return None

    monkeypatch.setattr(internal, "_persist_worker_tool_effect_result", persist_result)
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(surface=ChatSurface.EXTERNAL_CHANNEL_CHAT, entity_id="ent", user_id="user"),
        tool_schemas=[], allowed_tool_names=set(),
    )
    # Match the worker Harness's per-run dynamic-handler injection, not a
    # public surface/catalog grant for this internal lease-bound tool.
    envelope = replace(envelope, tool_names=("execute_planned_action",),
                       allowed_tool_names=("execute_planned_action",))
    calls = []

    async def completion(*args, **kwargs):
        return "", [{"id": "call_action", "name": "execute_planned_action", "arguments": {}}], {}

    async def adapter(snapshot):
        calls.append(snapshot)
        return {"result": {"tweet_id": "provider-receipt"}}

    async def loop(**kwargs):
        responses = []
        async def execute(name, args):
            response = await runtime_execute_scoped_dynamic_tool_handler(
                tool_name=name, arguments=args,
                handler=kwargs["dynamic_tool_handlers"][name], runtime_envelope=envelope,
            )
            responses.append(response)
            return response
        result = await agentic_loop(
            system_prompt=kwargs["system_prompt"], user_message=kwargs["user_message"],
            tools=kwargs["tools"], tool_executor=execute,
            terminal_tool_result_policy=kwargs["terminal_tool_result_policy"], max_rounds=1,
        )
        assert len(calls) == 1, responses
        return SimpleNamespace(result=result, run=None)

    monkeypatch.setattr("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(internal, "_call_action_adapter", adapter)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", loop)
    outcome = await internal._execute_by_kind(_action())
    assert outcome["result"] == {"tweet_id": "provider-receipt"}
    assert len(calls) == 1
    events = envelope.metadata["runtime_events"]
    assert [e["type"] for e in events] == ["tool_start", "tool_end"]


@pytest.mark.parametrize("changed", ["claim", "lease", "plan", "account", "expired"])
async def test_action_rechecks_current_lease_before_external_io(client, db_session, monkeypatch, changed):
    from datetime import datetime, timedelta, timezone
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import WorkLease
    from packages.core.services.runtime_authorization.binding_verifier import CurrentRuntimeToolBindingVerifier
    from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationRequest
    from test_dispatcher_envelope_status import _setup_step

    monkeypatch.setattr(internal, "async_session", dbmod.async_session)
    step_id, lease_id = await _setup_step(db_session, kind="action")
    step = await db_session.get(ExecutionStep, step_id)
    lease = await db_session.get(WorkLease, lease_id)
    plan = await db_session.get(ExecutionPlan, step.plan_id)
    step.provider, step.action_key = "twitter_x", "create_tweet"
    step.resolved_agent_id = generate_ulid()
    lease.execution_claim_id = generate_ulid()
    snapshot = {**_action(), "lease_id": lease_id, "step_id": step.id, "plan_id": plan.id,
                "entity_id": step.entity_id, "workspace_id": None, "task_id": None,
                "integration_id": None, "resolved_agent_id": step.resolved_agent_id,
                "execution_claim_id": lease.execution_claim_id}
    await db_session.commit()
    await internal._require_active_action_lease(snapshot)
    request = RuntimeAuthorizationRequest.create(
        entity_id=step.entity_id, user_id=None, workspace_id=None,
        action_key="tool.execute_planned_action.read", capability_id=None,
        principal_agent_id=step.resolved_agent_id, tool_name="execute_planned_action",
    )
    verifier = CurrentRuntimeToolBindingVerifier()
    assert (await verifier.verify(db_session, request)).current
    if changed == "claim":
        lease.execution_claim_id = generate_ulid()
    elif changed == "lease":
        lease.status = "cancelled"
    elif changed == "plan":
        plan.status = "paused"
    elif changed == "account":
        snapshot["integration_id"] = "another-account"
    else:
        lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(SubmittedStepFailure):
        await internal._require_active_action_lease(snapshot)
    # A model cannot use the internal tool without any claimed lease.
    lease.execution_claim_id = None
    await db_session.flush()
    assert not (await verifier.verify(db_session, request)).current


@pytest.mark.parametrize("state", ["paused_manual", "paused_automatic", "deleted", "cancelled"])
async def test_action_rechecks_task_and_workspace_lifecycle(client, db_session, monkeypatch, state):
    from datetime import datetime, timezone
    import packages.core.database as dbmod
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import WorkLease
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.services.task_execution_origin import record_manual_plan_execution
    from test_dispatcher_envelope_status import _setup_step

    monkeypatch.setattr(internal, "async_session", dbmod.async_session)
    step_id, lease_id = await _setup_step(db_session, kind="action")
    step = await db_session.get(ExecutionStep, step_id)
    lease = await db_session.get(WorkLease, lease_id)
    plan = await db_session.get(ExecutionPlan, step.plan_id)
    workspace = Workspace(id=generate_ulid(), entity_id=step.entity_id, name="Action scope", status="active")
    task = Task(id=generate_ulid(), entity_id=step.entity_id, workspace_id=workspace.id,
                title="Execute action", status="in_progress")
    db_session.add_all([workspace, task])
    plan.task_id, plan.workspace_id, step.workspace_id = task.id, workspace.id, workspace.id
    step.provider, step.action_key = "twitter_x", "create_tweet"
    lease.execution_claim_id = generate_ulid()
    snapshot = {**_action(), "lease_id": lease_id, "step_id": step.id, "plan_id": plan.id,
                "entity_id": step.entity_id, "workspace_id": workspace.id, "task_id": task.id,
                "integration_id": None, "resolved_agent_id": None, "execution_claim_id": lease.execution_claim_id}
    await db_session.commit()
    await internal._require_active_action_lease(snapshot)
    if state.startswith("paused"):
        workspace.status = "paused"
        if state == "paused_manual":
            record_manual_plan_execution(plan, user_id="operator", action="task_retry")
    elif state == "deleted":
        workspace.deleted_at = datetime.now(timezone.utc)
    else:
        task.status = "cancelled"
    await db_session.commit()
    if state == "paused_manual":
        await internal._require_active_action_lease(snapshot)
    else:
        with pytest.raises(SubmittedStepFailure):
            await internal._require_active_action_lease(snapshot)
