"""A plan that cannot do its external effect fails before any step runs.

Prod task FQ2881 (2026-09-14): the workspace forbade publishing (a draft_only
rule → never_allow) and had no publishing route (no X account, no paired
Chrome). The planner produced "prepare → publish" anyway, the prepare step
ran, and the operator learned about both problems only from the wreckage.

Pre-flight runs once when a plan starts, on the same deterministic detector
the partial-upstream gate uses: an external effect the governance hard-blocks
fails with the rule named and the fix attached; an effect no connected route
can carry fails with the setup links; the other steps are skipped so the
task fails with an actionable blocker and zero credits spent.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests.approval_settings_test_utils import initialize_test_approval_owners
from packages.core.governance.approval_projection import replace_workspace_rules

from packages.core.constants.execution import (
    NO_EXTERNAL_ROUTE_ERROR_TYPE,
    PREFLIGHT_SKIPPED_ERROR_TYPE,
    ExecutionStepStatus,
)
from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.plans.executor import partial_upstream_publish_issue
from packages.core.plans.external_effects import (
    external_effect_subject,
    step_external_effect,
)
from packages.core.services.external_effect_readiness import external_effect_route_readiness

_DRAFT_ONLY = {
    "rule_key": "draft_only_social",
    "description": "社交帖子只出草稿",
    "rule_type": "draft_only",
    "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
}


def _ns(**kw):
    base = dict(step_key="publish", kind="subagent", action_key=None, capability_id=None, provider=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── the detector ──────────────────────────────────────────────────────


def test_detector_reads_the_plan_declaration_first():
    dag = {"steps": [{"key": "publish", "output_shape": "PublishResult"}]}
    assert step_external_effect(_ns(), dag) == "external.social"
    assert step_external_effect(_ns(), {"steps": [{"key": "publish", "expects": ["publish"]}]}) == "external.social"
    assert step_external_effect(_ns(), {"steps": [{"key": "publish", "expects": ["send"]}]}) == "external.message"


def test_detector_reads_the_steps_own_capability_or_action_key():
    assert step_external_effect(_ns(capability_id="external.email"), {}) == "external.email"
    assert step_external_effect(_ns(kind="action", action_key="social_post.publish"), {}) == "external.social"
    assert step_external_effect(_ns(kind="action", action_key="email.send"), {}) == "external.email"
    assert step_external_effect(_ns(kind="action", action_key="workspace.task.create"), {}) is None
    assert step_external_effect(_ns(), {}) is None


def test_subject_falls_back_to_the_effects_canonical_keys():
    assert external_effect_subject(_ns(), "external.social") == ("social_post.publish", "external.social")
    assert external_effect_subject(_ns(action_key="social_post.reply", capability_id="external.social"), "external.social") == (
        "social_post.reply", "external.social",
    )


def test_partial_gate_now_covers_any_external_effect_not_only_publish_shapes():
    dep = ExecutionStep(
        id=generate_ulid(), plan_id="p", entity_id="e", step_key="draft", kind="subagent",
        step_status="done", result={"status": "partial", "summary": "half", "outputs": {"text": ""}},
        expected_output_schema=step_result_envelope_schema(),
    )
    send = ExecutionStep(
        id=generate_ulid(), plan_id="p", entity_id="e", step_key="send", kind="action",
        step_status="pending", depends_on=["draft"], action_key="email.send", capability_id="external.email",
    )
    assert partial_upstream_publish_issue(send, {"steps": [{"key": "draft"}, {"key": "send"}]}, {"draft": dep}) is not None


def test_planner_is_told_to_declare_expects():
    from packages.core.ai.runtime.planning import RUNTIME_PLAN_JSON_HINT, runtime_planner_system_prompt

    assert "expects" in RUNTIME_PLAN_JSON_HINT["steps"][0]
    prompt = runtime_planner_system_prompt(
        subscriptions=[], agents_by_id={}, allowed_service_keys=[],
    )
    assert "expects=['files']" in prompt
    assert "expects=['publish']" in prompt
    assert "Text or a queued job is not completion evidence" in prompt


def test_planner_is_told_not_to_cap_max_runtime_on_lease_expired():
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    prompt = runtime_planner_system_prompt(
        subscriptions=[], agents_by_id={}, allowed_service_keys=[],
    )
    assert "Omit max_runtime_seconds" in prompt
    assert "lease_expired is not a reason to shorten work" in prompt


def test_planner_is_told_not_to_interview_in_workspace_chat():
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    prompt = runtime_planner_system_prompt(
        subscriptions=[], agents_by_id={}, allowed_service_keys=[],
    )
    assert "Missing decisive information needs a human step" in prompt
    assert "Use human for necessary input/decision" in prompt


# ── route readiness ───────────────────────────────────────────────────


def _readiness(**flags):
    from packages.core.services.integration_resolution import IntegrationProviderReadiness

    async def fake(db, *, entity_id, provider_keys, user_id=None):
        return {
            key: IntegrationProviderReadiness(
                provider=key, ready=flags.get(key, False),
                reason="ok" if flags.get(key) else f"{key} is not connected", scope="user",
            )
            for key in provider_keys
        }

    return fake


@pytest.mark.asyncio
async def test_no_ready_route_names_the_providers_and_chrome(monkeypatch):
    from packages.core.services import integration_resolution as res

    async def supported(db):
        return {"twitter_x", "linkedin", "gmail", "stripe"}

    monkeypatch.setattr(res, "supported_integration_provider_keys", supported)
    monkeypatch.setattr(res, "integration_provider_readiness", _readiness())

    route = await external_effect_route_readiness(None, entity_id="e", user_id="u", effect="external.social")
    assert route is not None and route.ready is False
    assert route.checked_providers == ["chrome", "linkedin", "twitter_x"]
    assert route.setup_links["twitter_x"] == "/integrations?provider=twitter_x"
    assert "twitter_x" in route.message and "Chrome" in route.message


@pytest.mark.asyncio
async def test_commerce_and_ads_catalog_keys_are_not_publishing_routes(monkeypatch):
    """"tiktok" must not pull "tiktok_shop" into the route list (seen in E2E)."""
    from packages.core.services import integration_resolution as res

    async def supported(db):
        return {"tiktok", "tiktok_shop", "facebook", "facebook_ads", "linkedin"}

    monkeypatch.setattr(res, "supported_integration_provider_keys", supported)
    monkeypatch.setattr(res, "integration_provider_readiness", _readiness())
    route = await external_effect_route_readiness(None, entity_id="e", user_id="u", effect="external.social")
    assert route.checked_providers == ["chrome", "facebook", "linkedin", "tiktok"]


@pytest.mark.asyncio
async def test_a_paired_chrome_or_a_connected_account_is_a_route(monkeypatch):
    from packages.core.services import integration_resolution as res

    async def supported(db):
        return {"twitter_x"}

    monkeypatch.setattr(res, "supported_integration_provider_keys", supported)
    monkeypatch.setattr(res, "integration_provider_readiness", _readiness(chrome=True))
    assert (await external_effect_route_readiness(None, entity_id="e", user_id="u", effect="external.social")).ready

    monkeypatch.setattr(res, "integration_provider_readiness", _readiness(twitter_x=True))
    route = await external_effect_route_readiness(None, entity_id="e", user_id="u", effect="external.social")
    assert route.ready and route.ready_providers == ["twitter_x"]


@pytest.mark.asyncio
async def test_declared_provider_is_checked_directly_and_unknown_effects_are_not(monkeypatch):
    from packages.core.services import integration_resolution as res

    monkeypatch.setattr(res, "integration_provider_readiness", _readiness())
    route = await external_effect_route_readiness(
        None, entity_id="e", user_id="u", effect="external.social", declared_provider="linkedin",
    )
    assert route.checked_providers == ["chrome", "linkedin"]
    assert await external_effect_route_readiness(None, entity_id="e", user_id="u", effect="external.message") is None


# ── the executor pre-flight ───────────────────────────────────────────


async def _fresh_plan(db_session, *, rules):
    from packages.core.governance import get_policy, update_policy
    from packages.core.models.task import Task
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    entity_id = generate_ulid()
    ws_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    operator = User(id=generate_ulid(), entity_id=entity_id, email=f"{generate_ulid()}@pf.example", password_hash="x", role="owner")
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Social Agent", status="active")
    db_session.add(operator)
    db_session.add(UserMembership(user_id=operator.id, entity_id=entity_id, role="owner", status="active"))
    db_session.add(Workspace(id=ws_id, entity_id=entity_id, name="X Growth", operating_model={"rules": rules}))
    db_session.add(agent)
    db_session.add(AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=ws_id,
        agent_id=agent.id, service_key="social", status="active",
    ))
    db_session.add(Task(id=task_id, entity_id=entity_id, workspace_id=ws_id, creator_id=operator.id,
                        title="发布一条 X 帖子", status="in_progress", priority=3, task_type="general", details={}))
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, task_id=task_id, workspace_id=ws_id, status="draft",
        plan_dag={"steps": [{"key": "prepare_post"}, {"key": "publish_post", "depends_on": ["prepare_post"], "output_shape": "PublishResult"}]},
    ))
    prepare = ExecutionStep(id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, workspace_id=ws_id,
                            step_key="prepare_post", kind="subagent", step_status="pending",
                            params={"prompt": "draft"}, expected_output_schema=step_result_envelope_schema())
    publish = ExecutionStep(id=generate_ulid(), plan_id=plan_id, entity_id=entity_id, workspace_id=ws_id,
                            step_key="publish_post", kind="subagent", step_status="pending", depends_on=["prepare_post"],
                            params={"prompt": "publish"}, expected_output_schema=step_result_envelope_schema())
    db_session.add_all([prepare, publish])
    await db_session.flush()
    workspace = await db_session.get(Workspace, ws_id)
    await initialize_test_approval_owners(db_session, workspace, user_id=operator.id)
    policy = replace_workspace_rules(await get_policy(db_session, ws_id), rules)
    await update_policy(db_session, entity_id=entity_id, workspace_id=ws_id,
                        policy=policy, changed_by=operator.id, change_summary="Test source rules")
    await db_session.commit()
    return {"task_id": task_id, "plan_id": plan_id, "prepare_id": prepare.id, "publish_id": publish.id}


def _quiet_executor(monkeypatch):
    from packages.core.plans.executor import PlanExecutor

    async def fail_llm(*_a, **_k):
        raise AssertionError("pre-flight is deterministic; no LLM call expected")

    async def quiet(*_a, **_k):
        return None

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_llm)
    monkeypatch.setattr(PlanExecutor, "_announce", staticmethod(quiet))
    monkeypatch.setattr(PlanExecutor, "_emit_task_event", staticmethod(quiet))
    return PlanExecutor


async def _run(db_session, PlanExecutor, plan_id):
    @asynccontextmanager
    async def factory():
        yield db_session

    return await PlanExecutor(session_factory=factory).run_cycle(plan_id)


@pytest.mark.asyncio
async def test_governance_hard_block_parks_the_plan_before_any_step_runs(db_session, monkeypatch):
    from packages.core.models.task import Task
    from packages.core.services import integration_resolution as res

    monkeypatch.setattr(res, "integration_provider_readiness", _readiness(twitter_x=True))
    PlanExecutor = _quiet_executor(monkeypatch)
    fx = await _fresh_plan(
        db_session, rules=[dict(_DRAFT_ONLY)],
    )

    await _run(db_session, PlanExecutor, fx["plan_id"])
    db_session.expire_all()

    publish = await db_session.get(ExecutionStep, fx["publish_id"])
    prepare = await db_session.get(ExecutionStep, fx["prepare_id"])
    assert publish.step_status == ExecutionStepStatus.FAILED
    assert publish.error["type"] == "GovernancePolicy"
    assert [r["rule_key"] for r in publish.error["blocking_rules"]] == ["draft_only_social"]
    assert publish.error["fix"]["workspace_operation"]["patches"][0]["payload"]["rule_key"] == "draft_only_social"
    assert publish.error["failure"]["requires_human"] is True
    assert prepare.step_status == ExecutionStepStatus.SKIPPED
    assert prepare.error["type"] == PREFLIGHT_SKIPPED_ERROR_TYPE
    task = await db_session.get(Task, fx["task_id"])
    assert task.status == "failed"


@pytest.mark.asyncio
async def test_missing_publish_route_parks_the_plan_with_setup_links(db_session, monkeypatch):
    from packages.core.models.task import Task
    from packages.core.services import integration_resolution as res

    async def supported(db):
        return {"twitter_x", "linkedin"}

    monkeypatch.setattr(res, "supported_integration_provider_keys", supported)
    monkeypatch.setattr(res, "integration_provider_readiness", _readiness())
    PlanExecutor = _quiet_executor(monkeypatch)
    fx = await _fresh_plan(db_session, rules=[])

    await _run(db_session, PlanExecutor, fx["plan_id"])
    db_session.expire_all()

    publish = await db_session.get(ExecutionStep, fx["publish_id"])
    prepare = await db_session.get(ExecutionStep, fx["prepare_id"])
    assert publish.step_status == ExecutionStepStatus.FAILED
    assert publish.error["type"] == NO_EXTERNAL_ROUTE_ERROR_TYPE
    assert publish.error["setup_links"]["twitter_x"] == "/integrations?provider=twitter_x"
    assert publish.error["failure"]["requires_human"] is True
    assert "Chrome" in publish.error["message"]
    assert prepare.step_status == ExecutionStepStatus.SKIPPED
    assert (await db_session.get(Task, fx["task_id"])).status == "failed"


@pytest.mark.asyncio
async def test_a_ready_route_lets_the_plan_proceed(db_session, monkeypatch):
    from packages.core.models.task import Task
    from packages.core.services import integration_resolution as res

    async def supported(db):
        return {"twitter_x"}

    monkeypatch.setattr(res, "supported_integration_provider_keys", supported)
    monkeypatch.setattr(res, "integration_provider_readiness", _readiness(twitter_x=True))
    PlanExecutor = _quiet_executor(monkeypatch)
    fx = await _fresh_plan(db_session, rules=[])

    await _run(db_session, PlanExecutor, fx["plan_id"])
    db_session.expire_all()

    prepare = await db_session.get(ExecutionStep, fx["prepare_id"])
    publish = await db_session.get(ExecutionStep, fx["publish_id"])
    assert prepare.step_status == ExecutionStepStatus.PENDING  # handed to the dispatcher
    assert publish.step_status == ExecutionStepStatus.PENDING
    assert (await db_session.get(Task, fx["task_id"])).status == "in_progress"
