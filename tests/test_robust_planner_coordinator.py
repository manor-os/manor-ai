"""V3 Task coordination persists source DAG steps; V2 fold remains readable."""
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.constants.execution import (
    COORDINATOR_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
)
from packages.core.contracts.task_output import task_output_envelope_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans import planner as planner_mod
from packages.core.plans.coordinator import (
    coordinator_plan_from,
    task_uses_coordinator_runtime,
)
from packages.core.plans.schema import Plan, PlanMetadata, PlanStep
from packages.core.plans.task_constraints import extract_binding_constraints, render_constraints_block
from packages.core.services.task_execution import (
    CoordinatorSelectionDriftError,
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
    resolve_task_execution,
)
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake,
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
)


REPORT_SCHEMA = {
    "type": "object", "required": ["summary"], "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
}
DETAILS = {"runtime_context": {
    "instructions": "Do not write an essay",
    "rules": [{"description": "Keep every message under 200 words"}],
}}


def _task(**overrides):
    fields = dict(
        id="task-1", entity_id="ent-1", workspace_id="ws-1",
        title="Launch brief", description="Prepare and send the launch brief.",
        details=DETAILS, expected_output=None,
        owner_service_key="writer", delegate_service_keys=["research", "outreach"],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _selection(root: str, *services: str) -> CoordinatorRuntimeSelection:
    keys = [root, *(service for service in services if service != root)]
    bindings = tuple(
        TaskPlanServiceBinding(
            subscription_id=f"subscription-{service}",
            service_key=service,
            agent_id=f"agent-{service}",
        )
        for service in keys
    )
    return CoordinatorRuntimeSelection(root=bindings[0], bindings=bindings)


def _fold(plan: Plan, task=None, *, root: str | None = None) -> Plan:
    task = task or _task()
    root = root or str(task.owner_service_key)
    services = sorted(
        {
            str(step.service_key)
            for step in plan.steps
            if step.service_key and str(step.service_key) != root
        }
    )
    return coordinator_plan_from(
        plan,
        task,
        coordinator=_selection(root, *services),
    )


@pytest.mark.unit
def test_coordinator_selection_fingerprint_is_canonical_and_identity_complete():
    writer = TaskPlanServiceBinding(
        subscription_id="subscription-writer",
        service_key="writer",
        agent_id="agent-writer",
    )
    research = TaskPlanServiceBinding(
        subscription_id="subscription-research",
        service_key="research",
        agent_id="agent-research",
    )
    selected = CoordinatorRuntimeSelection(
        root=writer,
        bindings=(writer, research),
    )
    reordered = CoordinatorRuntimeSelection(
        root=writer,
        bindings=(research, writer),
    )
    replacement = CoordinatorRuntimeSelection(
        root=TaskPlanServiceBinding(
            subscription_id="replacement-writer",
            service_key="writer",
            agent_id="agent-writer",
        ),
        bindings=(
            TaskPlanServiceBinding(
                subscription_id="replacement-writer",
                service_key="writer",
                agent_id="agent-writer",
            ),
            research,
        ),
    )

    assert selected.stable_identity() == reordered.stable_identity()
    assert selected.stable_fingerprint() == reordered.stable_fingerprint()
    assert selected.stable_fingerprint() != replacement.stable_fingerprint()


def _source_plan():
    return Plan(
        steps=[
            PlanStep(
                key="report", kind="subagent", service_key="writer",
                expected_output_schema=REPORT_SCHEMA,
                params={
                    "prompt": "Summarise ${{ steps.research.result.text }} and the receipt ${{ steps.notify.result }}.",
                    "max_runtime_seconds": 900,
                },
                depends_on=["research", "notify"], expects=["publish"],
                description="Write the report.",
            ),
            PlanStep(
                key="research", kind="subagent", service_key="research",
                output_shape="TextResult",
                params={
                    "prompt": "Find three facts about ACME.",
                    "skill_refs": ["web-research"],
                    "max_runtime_seconds": 600,
                },
                expects=["files"], risk_level="medium", description="Research ACME.",
            ),
            PlanStep(
                key="notify", kind="llm", service_key="outreach",
                params={
                    "prompt": "Email the summary from ${{ steps.research.result.text }}.",
                    "max_runtime_seconds": 300,
                },
                depends_on=["research"], risk_level="high",
                description="Email the summary.",
            ),
        ],
        metadata=PlanMetadata(
            rationale="three services", planner_model="test-model",
            estimated_cost_usd=0.5, estimated_duration_seconds=120,
            acceptance_contract={
                "expected_result": "A report is delivered.",
                "criteria": [
                    {"key": "report_accepted", "deliverable_name": "report",
                     "description": "Report exists.", "evidence_step_keys": ["report"]},
                    {"key": "email_sent", "deliverable_name": "email",
                     "description": "Ops were emailed.", "evidence_step_keys": ["notify", "research"]},
                ],
            },
        ),
    )


def _all_kinds_plan():
    return Plan(
        steps=[
            PlanStep(
                key="draft",
                kind="subagent",
                service_key="writer",
                output_shape="TextResult",
                params={"prompt": "Draft the launch update."},
                expects=["files"],
            ),
            PlanStep(
                key="notify",
                kind="action",
                service_key="outreach",
                provider="gmail",
                action_key="send_email",
                integration_id="gmail-account-1",
                params={
                    "to": ["ops@example.com"],
                    "body": "${{ steps.draft.result.text }}",
                    "options": {
                        "track_opens": False,
                        "priority": 0,
                        "reply_to": None,
                    },
                },
                expected_input_schema={
                    "type": "object",
                    "required": ["to", "body"],
                    "properties": {
                        "to": {"type": "array", "items": {"type": "string"}},
                        "body": {"type": "string"},
                    },
                },
                expected_output_schema={
                    "type": "object",
                    "required": ["message_id"],
                    "properties": {"message_id": {"type": "string"}},
                },
                depends_on=["draft"],
                expects=["publish"],
                risk_level="high",
                requires_approval=True,
                max_attempts=2,
                description="Send the approved update.",
            ),
            PlanStep(
                key="confirm",
                kind="human",
                params={
                    "prompt": "Confirm delivery of ${{ steps.notify.result.message_id }}."
                },
                expected_input_schema={
                    "type": "object",
                    "required": ["confirmed"],
                    "properties": {"confirmed": {"type": "boolean"}},
                },
                depends_on=["notify"],
                max_attempts=1,
                description="Ask for delivery confirmation.",
            ),
            PlanStep(
                key="wait",
                kind="sleep",
                params={"seconds": 30},
                depends_on=["confirm"],
                max_attempts=1,
                description="Wait before checking delivery.",
            ),
            PlanStep(
                key="report",
                kind="llm",
                service_key="writer",
                expected_output_schema=REPORT_SCHEMA,
                params={
                    "prompt": (
                        "Report delivery ${{ steps.notify.result.message_id }} "
                        "after ${{ steps.wait.result }}."
                    )
                },
                depends_on=["wait"],
            ),
        ],
    )


# ── coordinator_plan_from ─────────────────────────────────────────────


@pytest.mark.unit
def test_coordinator_plan_is_one_subagent_step_owned_by_the_task_owner_service():
    plan = _fold(_source_plan(), _task())

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.key == "coordinate"
    assert step.kind == "subagent"
    assert step.service_key == "writer"
    assert step.description == "Coordinate the task across workspace services"
    assert step.requires_approval is False, "exact nested tools own approval, not a broad parent gate"
    assert step.risk_level == "high"
    assert step.depends_on == []
    assert step.params[COORDINATOR_PARAM_KEY] is True
    assert step.params["max_rounds"] == 60
    assert step.params["max_runtime_seconds"] == 1800
    assert step.params["delegate_services"] == ["outreach", "research", "writer"]
    assert step.expects == ["files", "publish"]
    # Round-trips through the persisted plan_dag shape.
    Plan.model_validate(json.loads(plan.model_dump_json()))


@pytest.mark.unit
def test_task_owner_remains_the_root_control_service_when_not_a_source_item():
    plan = _fold(_source_plan(), _task(owner_service_key="ops"))

    assert plan.steps[0].service_key == "ops"
    assert plan.steps[0].params["delegate_services"] == [
        "ops", "outreach", "research", "writer",
    ]


@pytest.mark.unit
def test_checklist_lists_every_source_step_in_topo_order():
    step = _fold(_source_plan(), _task()).steps[0]
    checklist = step.params["checklist"]

    assert [item["key"] for item in checklist] == ["research", "notify", "report"]
    assert {
        "key", "delegation_key", "kind", "service_key", "description", "prompt",
        "params", "depends_on", "expects", "expected_input_schema",
        "expected_output_schema", "output_shape", "risk_level",
        "requires_approval", "max_attempts", "skill_refs", "provider",
        "action_key", "capability_id", "integration_id",
    } <= set(checklist[0])
    research, notify, report = checklist
    assert research["key"] == research["delegation_key"] == "research"
    assert research["kind"] == "subagent" and research["service_key"] == "research"
    assert research["description"] == "Research ACME."
    assert research["prompt"] == "Find three facts about ACME."
    assert research["params"] == {
        "prompt": "Find three facts about ACME.",
        "skill_refs": ["web-research"],
        "max_runtime_seconds": 600,
    }
    assert research["depends_on"] == [] and research["expects"] == ["files"]
    assert research["output_shape"] == "TextResult"
    assert research["risk_level"] == "medium" and research["requires_approval"] is False
    assert research["max_attempts"] == 3
    assert research["expected_input_schema"] is None
    assert research["expected_output_schema"] is None
    assert research["skill_refs"] == ["web-research"]
    assert research["provider"] is None and research["action_key"] is None
    assert notify["kind"] == "llm" and notify["service_key"] == "outreach"
    assert notify["provider"] is None and notify["action_key"] is None
    assert notify["depends_on"] == ["research"]
    assert notify["prompt"] == "Email the summary from ${{ steps.research.result.text }}."
    assert notify["risk_level"] == "high" and notify["requires_approval"] is False
    assert notify["params"] == {
        "prompt": "Email the summary from ${{ steps.research.result.text }}.",
        "max_runtime_seconds": 300,
    }
    assert report["depends_on"] == ["research", "notify"] and report["expects"] == ["publish"]
    assert report["expected_output_schema"] == REPORT_SCHEMA
    assert report["prompt"] == (
        "Summarise ${{ steps.research.result.text }} and the receipt "
        "${{ steps.notify.result }}."
    )
    # The root's human-facing prompt neutralises refs because it has no
    # upstream results.  The trusted checklist retains the exact expressions
    # so the typed child factory can resolve them from durable child outputs.
    assert "${{" not in step.params["prompt"]
    assert "${{" in json.dumps(checklist)


@pytest.mark.unit
def test_prompt_carries_task_checklist_rules_and_verbatim_constraints():
    task = _task()
    prompt = _fold(_source_plan(), task).steps[0].params["prompt"]

    assert "Launch brief" in prompt
    assert "Prepare and send the launch brief." in prompt
    constraints = render_constraints_block(extract_binding_constraints(task.details))
    assert constraints and constraints in prompt
    checklist_block = prompt.split("Checklist", 1)[1]
    assert checklist_block.index("[research]") < checklist_block.index("[notify]") < checklist_block.index("[report]")
    assert "- [research] (research, subagent) Research ACME.: Find three facts about ACME." in prompt
    assert (
        "- [notify] (outreach, llm) Email the summary.: "
        "Email the summary from <result of step research: text>."
    ) in prompt
    assert "depends on: research, notify" in prompt
    assert "expects: publish" in prompt
    assert DELEGATE_TO_SERVICE_TOOL_NAME in prompt
    assert "request_input" in prompt
    assert "submit_result" in prompt
    assert "${{" not in prompt


@pytest.mark.unit
def test_output_contract_comes_from_the_terminal_agent_step():
    step = _fold(_source_plan(), _task()).steps[0]
    assert step.output_shape is None
    assert step.expected_output_schema == REPORT_SCHEMA

    shaped = Plan(steps=[
        PlanStep(key="draft", kind="subagent", service_key="writer",
                 output_shape="TextResult", params={"prompt": "Draft."}),
        PlanStep(key="save", kind="subagent", service_key="writer",
                 output_shape="DocumentResult", params={"prompt": "Save it."}, depends_on=["draft"]),
    ])
    step = _fold(shaped, _task()).steps[0]
    assert step.output_shape == "DocumentResult" and step.expected_output_schema is None
    assert step.risk_level == "low" and step.expects is None
    assert "max_runtime_seconds" not in step.params

    bare = Plan(steps=[PlanStep(key="draft", kind="llm", service_key="writer", params={"prompt": "Draft."})])
    assert _fold(bare, _task()).steps[0].output_shape == "TextResult"


@pytest.mark.unit
def test_metadata_keeps_provenance_and_remaps_acceptance_evidence():
    source = _source_plan()
    plan = _fold(source, _task())
    metadata = plan.metadata

    assert metadata.rationale == "three services"
    assert metadata.planner_model == "test-model"
    assert metadata.estimated_cost_usd == 0.5
    assert metadata.estimated_duration_seconds == 120
    assert [c.evidence_step_keys for c in metadata.acceptance_contract.criteria] == [["coordinate"], ["coordinate"]]
    assert metadata.acceptance_contract.expected_result == "A report is delivered."
    assert metadata.model_dump()["coordinator"] == {
        "source_steps": ["research", "notify", "report"],
        "source_step_count": 3,
    }
    # The source plan is not mutated.
    assert [c.evidence_step_keys for c in source.metadata.acceptance_contract.criteria] == [["report"], ["notify", "research"]]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "step"),
    [
        (
            "llm",
            PlanStep(
                key="draft", kind="llm", service_key="writer",
                params={"prompt": "Draft the update."},
            ),
        ),
        (
            "subagent",
            PlanStep(
                key="research", kind="subagent", service_key="research",
                params={"prompt": "Research the launch."},
            ),
        ),
        (
            "action",
            PlanStep(
                key="notify", kind="action", service_key="outreach",
                provider="gmail", action_key="send_email",
                params={"to": "ops@example.com", "body": "Ready"},
            ),
        ),
        (
            "human",
            PlanStep(key="confirm", kind="human", params={"prompt": "Confirm."}),
        ),
        (
            "sleep",
            PlanStep(key="wait", kind="sleep", params={"seconds": 10}),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_supported_item_kind_folds_into_one_root_control_step(kind, step):
    source = Plan(steps=[step])

    result = _fold(source, _task())

    assert result is not source
    assert [root.key for root in result.steps] == ["coordinate"]
    checklist = result.steps[0].params["checklist"]
    assert [(item["key"], item["kind"]) for item in checklist] == [(step.key, kind)]


@pytest.mark.unit
def test_all_kinds_fold_keeps_exact_child_contracts_dependencies_and_refs():
    source = _all_kinds_plan()

    result = _fold(source, _task())

    # One persisted root owns the coordinator loop.  The checklist is not one
    # flattened prompt: each entry remains a typed contract that Runtime can
    # materialise as its own durable ExecutionStep.
    assert [step.key for step in result.steps] == ["coordinate"]
    root = result.steps[0]
    checklist = root.params["checklist"]
    assert [item["key"] for item in checklist] == [
        "draft", "notify", "confirm", "wait", "report",
    ]
    assert [item["kind"] for item in checklist] == [
        "subagent", "action", "human", "sleep", "llm",
    ]
    assert [item["delegation_key"] for item in checklist] == [
        "draft", "notify", "confirm", "wait", "report",
    ]

    by_key = {item["key"]: item for item in checklist}
    action = by_key["notify"]
    assert action["service_key"] == "outreach"
    assert action["provider"] == "gmail"
    assert action["action_key"] == "send_email"
    assert action["integration_id"] == "gmail-account-1"
    assert action["params"] == {
        "to": ["ops@example.com"],
        "body": "${{ steps.draft.result.text }}",
        "options": {
            "track_opens": False,
            "priority": 0,
            "reply_to": None,
        },
    }
    assert action["expected_input_schema"] == {
        "type": "object",
        "required": ["to", "body"],
        "properties": {
            "to": {"type": "array", "items": {"type": "string"}},
            "body": {"type": "string"},
        },
    }
    assert action["expected_output_schema"] == {
        "type": "object",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    }
    assert action["depends_on"] == ["draft"]
    assert action["expects"] == ["publish"]
    assert action["risk_level"] == "high"
    assert action["requires_approval"] is True
    assert action["max_attempts"] == 2

    human = by_key["confirm"]
    assert human["service_key"] is None
    assert human["params"] == {
        "prompt": "Confirm delivery of ${{ steps.notify.result.message_id }}."
    }
    assert human["expected_input_schema"]["required"] == ["confirmed"]
    assert human["depends_on"] == ["notify"]
    assert human["max_attempts"] == 1

    sleep = by_key["wait"]
    assert sleep["service_key"] is None
    assert sleep["params"] == {"seconds": 30}
    assert sleep["depends_on"] == ["confirm"]
    assert sleep["max_attempts"] == 1

    report = by_key["report"]
    assert report["depends_on"] == ["wait"]
    assert report["expected_output_schema"] == REPORT_SCHEMA
    assert "${{ steps.notify.result.message_id }}" in report["params"]["prompt"]
    assert "${{ steps.wait.result }}" in report["params"]["prompt"]

    # Human-facing prompt text may use readable placeholders, but the trusted
    # child contracts keep exact refs and lossless JSON values.
    assert "${{" not in root.params["prompt"]
    assert "${{" in json.dumps(checklist)
    assert root.requires_approval is False
    assert root.risk_level == "high"
    assert root.expects == ["files", "publish"]
    Plan.model_validate(json.loads(result.model_dump_json()))

    # Folding must not mutate the authored source DAG.
    assert source.step_by_key("notify").params["body"] == "${{ steps.draft.result.text }}"


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["parallel_fanout", "gather", "code"])
def test_unsupported_item_kinds_fail_closed_instead_of_skipping_fold(kind):
    source = Plan(steps=[
        PlanStep(
            key="unsupported",
            kind=kind,
            service_key="writer",
            params={"prompt": "Do not promptify this executor contract."},
        ),
    ])

    with pytest.raises(ValueError):
        _fold(source, _task())


# ── coordinator_mode_enabled ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_task_coordinates_by_default_through_runtime_factory(db_session):
    scenario = await _workspace_task(db_session, details={})
    assert await task_uses_coordinator_runtime(db_session, scenario.task) is True


@pytest.mark.asyncio
async def test_non_workspace_task_never_coordinates(db_session):
    task = _task(
        workspace_id=None,
        task_type="general",
        agent_id=None,
        agent_type=None,
        details={"coordinator_mode": True},
    )
    assert await task_uses_coordinator_runtime(db_session, task) is False


# ── plan_task integration ─────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _noop_billing(*_args, **_kwargs):
    yield


async def _workspace_task(
    db,
    *,
    details,
    expected_output=None,
    task_type: str = "general",
):
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active")
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={})
    subscriptions = [
        AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, agent_id=agent.id,
            workspace_id=workspace.id, service_key=key, status="active",
        )
        for key in ("writer", "research")
    ]
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Launch brief", description="Prepare the launch brief.", status="in_progress",
        task_type=task_type, details=details, expected_output=expected_output,
        owner_service_key="writer", delegate_service_keys=["research"],
    )
    db.add_all([workspace, agent, *subscriptions, task])
    await db.commit()
    return SimpleNamespace(entity_id=entity_id, task=task, subscriptions=subscriptions)


def _two_step_plan():
    return Plan(steps=[
        PlanStep(key="research", kind="subagent", service_key="research",
                 execution_contract={"delivery": "inline", "effects": "read_only"},
                 output_shape="TextResult", params={"prompt": "Research the market."}),
        PlanStep(key="report", kind="subagent", service_key="writer",
                 execution_contract={"delivery": "inline", "effects": "read_only"},
                 output_shape="TextResult", params={"prompt": "Write from ${{ steps.research.result.text }}."},
                 depends_on=["research"]),
    ])


def _patch_plan_task(monkeypatch, scenario) -> dict:
    async def _context(_db, _task):
        return planner_mod._Context(
            workspace=None, subscriptions=list(scenario.subscriptions), agents_by_id={},
            allowed_service_keys={"writer", "research"}, provider_actions={},
        )

    async def _generate(_task, _context):
        return _two_step_plan()

    persisted: dict = {}

    async def _persist(db, **kwargs):
        persisted.update(kwargs)
        return SimpleNamespace(id="plan-persisted", status="draft")

    monkeypatch.setattr(planner_mod, "_gather_context", _context)
    monkeypatch.setattr(planner_mod, "_generate_plan", _generate)
    monkeypatch.setattr(planner_mod, "runtime_planner_llm_billing_context", _noop_billing)
    monkeypatch.setattr(planner_mod, "create_plan_from_dag", _persist)
    return persisted


async def _claim_v3_task(db, task):
    selection = (await resolve_task_execution(db, task)).coordinator
    assert selection is not None
    await ensure_task_coordinator_run(db, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(db, task_id=task.id, reason="planner_test")
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db,
        task_id=task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="planner-test-owner",
        selection=selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    await db.commit()
    return claim


@pytest.mark.asyncio
async def test_plan_task_persists_v3_source_dag_under_task_claim(db_session, monkeypatch):
    scenario = await _workspace_task(
        db_session, details={}, expected_output=REPORT_SCHEMA,
    )
    persisted = _patch_plan_task(monkeypatch, scenario)
    claim = await _claim_v3_task(db_session, scenario.task)

    await planner_mod.plan_task(
        db_session, scenario.task.id, execution_mode="live",
        task_coordinator_claim=claim,
    )

    plan = persisted["plan"]
    assert [step.key for step in plan.steps] == ["research", "report"]
    assert all(COORDINATOR_PARAM_KEY not in step.params for step in plan.steps)
    expected_selection = persisted["expected_selection"]
    assert expected_selection.root.subscription_id == scenario.subscriptions[0].id
    assert expected_selection.stable_identity().bindings == tuple(
        sorted(
            expected_selection.bindings,
            key=lambda binding: (
                binding.service_key,
                binding.subscription_id,
                binding.agent_id,
            ),
        )
    )
    # The Task deliverable stays the persisted terminal contract.
    assert plan.step_by_key("report").expected_output_schema == task_output_envelope_schema(REPORT_SCHEMA)


@pytest.mark.asyncio
async def test_plan_task_discards_provider_result_when_subscription_is_replaced(
    db_session,
    monkeypatch,
):
    scenario = await _workspace_task(
        db_session,
        details={},
        expected_output=REPORT_SCHEMA,
    )
    persisted = _patch_plan_task(monkeypatch, scenario)
    claim = await _claim_v3_task(db_session, scenario.task)
    writer = scenario.subscriptions[0]

    async def _replace_during_provider(_task, _context):
        # Admission/context locks must be gone before provider I/O. This second
        # transaction would otherwise block trying to deactivate the selection.
        assert db_session.in_transaction() is False
        sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
        async with sessions() as concurrent:
            replacement_agent_id = generate_ulid()
            await concurrent.execute(
                update(AgentSubscription)
                .where(AgentSubscription.id == writer.id)
                .values(status="inactive")
            )
            concurrent.add(Agent(
                id=replacement_agent_id,
                entity_id=scenario.entity_id,
                name="Replacement Writer",
                status="active",
                config={},
            ))
            concurrent.add(AgentSubscription(
                id=generate_ulid(),
                entity_id=scenario.entity_id,
                agent_id=replacement_agent_id,
                workspace_id=scenario.task.workspace_id,
                service_key="writer",
                status="active",
            ))
            await concurrent.commit()
        return _two_step_plan()

    async def _dispatchable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(planner_mod, "_generate_plan", _replace_during_provider)
    monkeypatch.setattr(planner_mod, "_raise_if_not_dispatchable", _dispatchable)

    with pytest.raises(
        CoordinatorSelectionDriftError,
        match="coordinator selection changed while planning",
    ):
        await planner_mod.plan_task(
            db_session,
            scenario.task.id,
            execution_mode="live",
            task_coordinator_claim=claim,
        )

    assert "plan" not in persisted
    assert list((await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.task_id == scenario.task.id)
    )).scalars().all()) == []


@pytest.mark.asyncio
async def test_planner_rejects_direct_workspace_start_before_provider(
    db_session,
    monkeypatch,
):
    scenario = await _workspace_task(
        db_session,
        details={},
        task_type="approval",
    )

    async def _provider_must_not_run(*_args, **_kwargs):
        raise AssertionError("provider was called for a non-coordinated runtime")

    monkeypatch.setattr(planner_mod, "_generate_plan", _provider_must_not_run)

    with pytest.raises(
        planner_mod.TaskPlanOwnedByCoordinatorError,
        match="requires its Task coordinator",
    ):
        await planner_mod.plan_task(
            db_session,
            scenario.task.id,
            execution_mode="live",
        )
