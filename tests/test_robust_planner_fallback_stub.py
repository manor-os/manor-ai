"""P1 — the rule-based planner stub is opt-in; a provider outage must retry.

Before this gate a transient LLM outage on the first planner turn silently
produced the two-step "think / summarize" stub, which then ran as a real plan
and delivered nothing useful.  The stub now only appears when
``MANOR_PLANNER_FALLBACK_STUB`` opts in (dev / CI smoke); otherwise the
outage surfaces as ``PlannerError`` so the Celery task retries with backoff.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError
from packages.core.ai.runtime import RuntimePlannerChatTurnResult
from packages.core.plans import planner
from packages.core.services.task_execution import (
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
)


ENV = "MANOR_PLANNER_FALLBACK_STUB"


def _task():
    return SimpleNamespace(
        id="task-test", entity_id="entity-test", workspace_id=None,
        title="Draft the brief", description="Write the launch brief.",
        details={}, input_contract=None, expected_output=None,
        owner_service_key="writer", delegate_service_keys=[],
    )


def _context():
    return planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="writer", id="sub", agent_id="writer-agent")],
        agents_by_id={"writer-agent": SimpleNamespace(id="writer-agent", name="Writer", system_prompt="")},
        allowed_service_keys={"writer"},
        provider_actions={},
    )


def _outage(calls: list):
    async def _turn(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("connection refused")
    return _turn


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("no", False),
    ("off", False), ("1", True), ("true", True), ("TRUE", True), ("Yes", True),
    (" yes ", True),
])
def test_fallback_stub_gate_reads_the_environment(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert planner._planner_fallback_stub_enabled() is expected


@pytest.mark.asyncio
async def test_turn_zero_outage_raises_planner_error_by_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    calls: list = []
    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _outage(calls))

    with pytest.raises(planner.PlannerError, match=r"Planner LLM unavailable: connection refused") as exc_info:
        await planner._generate_plan(_task(), _context())

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "no"])
async def test_explicit_opt_out_values_do_not_enable_the_stub(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _outage([]))

    with pytest.raises(planner.PlannerError, match="Planner LLM unavailable"):
        await planner._generate_plan(_task(), _context())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", "true", "YES"])
async def test_turn_zero_outage_uses_the_stub_only_when_enabled(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _outage([]))

    plan = await planner._generate_plan(_task(), _context())

    assert [step.key for step in plan.steps] == ["think", "summarize"]
    assert {step.service_key for step in plan.steps} == {"writer"}
    assert plan.steps[1].depends_on == ["think"]


@pytest.mark.asyncio
async def test_workspace_fallback_uses_factory_coordinator_root(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _outage([]))
    task = _task()
    task.workspace_id = "workspace-test"
    task.owner_service_key = "stale-owner-field"
    context = _context()
    context.allowed_service_keys = {"stale-owner-field", "factory-root"}
    root = TaskPlanServiceBinding(
        subscription_id="subscription-factory-root",
        service_key="factory-root",
        agent_id="agent-factory-root",
    )
    context.coordinator = CoordinatorRuntimeSelection(
        root=root,
        bindings=(root,),
    )

    plan = await planner._generate_plan(task, context)

    assert {step.service_key for step in plan.steps} == {"factory-root"}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    CreditExhaustedError("credits exhausted"),
    CreditCheckUnavailableError("credit gate offline"),
])
async def test_credit_gate_errors_bypass_the_stub_even_when_enabled(monkeypatch, error):
    monkeypatch.setenv(ENV, "1")

    async def _credit_failure(**_kwargs):
        raise error

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _credit_failure)

    with pytest.raises(type(error)):
        await planner._generate_plan(_task(), _context())


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_later_turn_outage_never_falls_back_to_the_stub(monkeypatch, enabled):
    """The stub gate is a turn-0 decision: once the model has started
    researching, an outage is a failed planning attempt, never a stub."""
    if enabled:
        monkeypatch.setenv(ENV, "1")
    else:
        monkeypatch.delenv(ENV, raising=False)
    calls: list = []

    async def _turn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return RuntimePlannerChatTurnResult("", [{
                "id": "list-1", "name": "list_tools",
                "arguments": '{"service_key": "writer"}',
            }], {})
        raise RuntimeError("connection reset")

    monkeypatch.setattr(planner, "runtime_execute_planner_chat_turn", _turn)

    with pytest.raises(planner.PlannerError, match="failed to produce a valid plan"):
        await planner._generate_plan(_task(), _context())
    assert len(calls) == 2
