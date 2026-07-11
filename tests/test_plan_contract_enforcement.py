"""Always-on fail-closed contract enforcement + replan guidance.

Plans with contract gaps (dangling refs / unshaped producers that survive
auto-repair) get one re-plan attempt; if the gap persists, plan_task raises
PlanContractError instead of dispatching a plan that would die at runtime.
There is no opt-out flag — this is the default behavior.
"""

import contextlib

import pytest

from packages.core.plans import planner as planner_mod
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.service import PlanContractError


# ── fail-closed enforcement in plan_task ──────────────────────────────


def _llm_step(key, output_shape=None, params=None):
    p = {"prompt": "do the thing"}
    if params:
        p.update(params)
    return PlanStep(key=key, kind="llm", service_key="content_creation", output_shape=output_shape, params=p)


def _gappy_plan():
    """A plan with an unresolvable contract gap: step b reads
    ${{ steps.a.result.content }} but a has no output shape and `content`
    is not auto-inferable, so the gap survives repair."""
    return Plan(
        steps=[
            _llm_step("a"),
            _llm_step("b", params={"prompt": "use ${{ steps.a.result.content }}"}),
        ]
    )


def _clean_plan():
    """A plan with no contract gaps — single self-contained llm step that
    declares its output shape."""
    return Plan(steps=[_llm_step("a", output_shape="TextResult")])


class _FakeResult:
    def __init__(self, task):
        self._task = task

    def scalar_one_or_none(self):
        return self._task


class _FakeTask:
    def __init__(self):
        self.id = "task_1"
        self.entity_id = "ent_1"
        self.workspace_id = None
        self.owner_subscription_id = None
        self.details = {}


class _FakeDB:
    def __init__(self, task):
        self._task = task

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._task)


@contextlib.asynccontextmanager
async def _noop_billing(*_args, **_kwargs):
    yield


def _patch_common(monkeypatch):
    """Patch the heavy collaborators of plan_task; return a mutable dict
    holding the persisted plan so assertions can inspect it."""
    monkeypatch.setattr(planner_mod, "_gather_context", _fake_gather_context)
    monkeypatch.setattr(planner_mod, "runtime_planner_llm_billing_context", _noop_billing)

    persisted = {}

    async def _fake_create(db, **kwargs):
        persisted["plan"] = kwargs.get("plan")
        return object()

    monkeypatch.setattr(planner_mod, "create_plan_from_dag", _fake_create)
    return persisted


async def _fake_gather_context(db, task):
    return object()


@pytest.mark.asyncio
async def test_enforcement_persistently_gappy_raises(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)

    async def _always_gappy(task, ctx):
        return _gappy_plan()

    monkeypatch.setattr(planner_mod, "_generate_plan", _always_gappy)

    db = _FakeDB(_FakeTask())
    with pytest.raises(PlanContractError):
        await planner_mod.plan_task(db, "task_1", execution_mode="live")


@pytest.mark.asyncio
async def test_enforcement_replan_recovers(monkeypatch):
    persisted = _patch_common(monkeypatch)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)

    calls = {"n": 0}

    async def _gappy_then_clean(task, ctx):
        calls["n"] += 1
        return _gappy_plan() if calls["n"] == 1 else _clean_plan()

    monkeypatch.setattr(planner_mod, "_generate_plan", _gappy_then_clean)

    db = _FakeDB(_FakeTask())
    await planner_mod.plan_task(db, "task_1", execution_mode="live")

    assert calls["n"] == 2
    # The clean (second) plan was the one persisted.
    assert persisted["plan"].steps[0].output_shape == "TextResult"
    assert len(persisted["plan"].steps) == 1


@pytest.mark.asyncio
async def test_contract_replan_preserves_existing_replan_context(monkeypatch):
    # A runtime replan may already have populated _replan_context with
    # prior_plan_id + succeeded_steps. A contract-gap re-plan must MERGE, not
    # clobber — lineage and the minimal-replan context must survive.
    _patch_common(monkeypatch)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)

    calls = {"n": 0}

    async def _gappy_then_clean(task, ctx):
        calls["n"] += 1
        return _gappy_plan() if calls["n"] == 1 else _clean_plan()

    monkeypatch.setattr(planner_mod, "_generate_plan", _gappy_then_clean)

    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "prior_plan_id": "plan_prev",
            "succeeded_steps": [{"step_key": "s1", "result_summary": "ok"}],
        }
    }
    db = _FakeDB(task)
    await planner_mod.plan_task(db, "task_1", execution_mode="live")

    ctx = task.details["_replan_context"]
    assert ctx["prior_plan_id"] == "plan_prev"  # lineage preserved
    assert ctx["succeeded_steps"] == [{"step_key": "s1", "result_summary": "ok"}]
    assert "contract_gaps" in ctx  # new gap info added alongside


# ── PlanContractError fails the celery task cleanly (no retry) ─────────


def test_plan_and_run_task_contract_error_fails_no_retry(monkeypatch):
    from packages.core.tasks import ai_tasks

    def _raise_contract(coro):
        # _run_async is what executes _go(); close the unawaited coroutine to
        # avoid a RuntimeWarning, then simulate plan_task raising.
        coro.close()
        raise PlanContractError(
            [type("_G", (), {"step_key": "b", "detail": "reads a.content but a has no output shape"})()]
        )

    monkeypatch.setattr(ai_tasks, "_run_async", _raise_contract)

    failed = {}
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda task_id, reason: failed.update(task_id=task_id, reason=reason),
    )

    def _no_retry(*_args, **_kwargs):
        raise AssertionError("self.retry must not be called for PlanContractError")

    monkeypatch.setattr(ai_tasks.plan_and_run_task, "retry", _no_retry, raising=False)

    result = ai_tasks.plan_and_run_task.run("task_1")

    assert result == {"plan_id": None, "status": "failed"}
    assert failed["task_id"] == "task_1"
    assert "contract" in failed["reason"].lower()


# ── minimal replan guidance in the planner system prompt ──────────────


def test_planner_system_prompt_has_minimal_replan_guidance():
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    out = runtime_planner_system_prompt(
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=[],
    )
    assert "_replan_context" in out
    assert "minimal" in out.lower()
