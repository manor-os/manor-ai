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


def _stale_artifact_review_plan():
    """Reproduce the production artifact -> review -> report contract gap."""
    artifact_ref = "${{ steps.prepare_outreach_pack.result.outputs.files }}"
    return Plan(
        steps=[
            PlanStep(
                key="prepare_outreach_pack",
                kind="subagent",
                service_key="partnership_development",
                output_shape="ArtifactResult",
                params={"prompt": "Prepare the outreach packet as files."},
            ),
            PlanStep(
                key="calvin_approval",
                kind="human",
                params={
                    "prompt": "Review the outreach packet.",
                    "review_artifacts": artifact_ref,
                },
                depends_on=["prepare_outreach_pack"],
            ),
            PlanStep(
                key="send_update_and_report",
                kind="llm",
                service_key="partnership_development",
                output_shape="TextResult",
                params={"prompt": "Report on " + artifact_ref},
                depends_on=["calvin_approval"],
            ),
        ]
    )


def _direct_artifact_review_plan():
    plan = _stale_artifact_review_plan()
    direct_ref = "${{ steps.prepare_outreach_pack.result.files }}"
    plan.steps[1].params["review_artifacts"] = direct_ref
    plan.steps[2].params["prompt"] = "Report on " + direct_ref
    return plan


def test_required_plan_steps_reject_a_plan_missing_the_upload_step():
    task = _FakeTask()
    task.details = {
        "required_plan_steps": [
            {
                "key": "produce_video",
                "kind": "subagent",
                "service_key": "stickman.production",
            },
            {
                "key": "upload_to_youtube",
                "kind": "subagent",
                "service_key": "stickman.production",
                "depends_on": ["produce_video"],
                "prompt_substrings": ["Chrome", "YouTube Studio"],
                "required_refs": ["produce_video"],
            },
        ],
    }
    plan = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            output_shape="ArtifactResult",
            params={"prompt": "Produce the finished MP4."},
        ),
    ])

    assert planner_mod._required_plan_step_errors(task, plan) == [
        "missing required step 'upload_to_youtube'",
    ]


@pytest.mark.asyncio
async def test_required_plan_steps_trigger_one_replan_before_persisting(monkeypatch):
    persisted = _patch_common(monkeypatch)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)

    task = _FakeTask()
    task.details = {
        "required_plan_steps": [
            {
                "key": "produce_video",
                "kind": "subagent",
                "service_key": "stickman.production",
            },
            {
                "key": "upload_to_youtube",
                "kind": "subagent",
                "service_key": "stickman.production",
                "depends_on": ["produce_video"],
                "prompt_substrings": ["Chrome", "YouTube Studio"],
                "required_refs": ["produce_video"],
            },
        ],
    }

    first = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            output_shape="ArtifactResult",
            params={"prompt": "Produce the finished MP4."},
        ),
    ])
    second = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            output_shape="ArtifactResult",
            params={"prompt": "Produce the finished MP4."},
        ),
        PlanStep(
            key="upload_to_youtube",
            kind="subagent",
            service_key="stickman.production",
            output_shape="ArtifactResult",
            depends_on=["produce_video"],
            params={
                "prompt": "Use Chrome to upload the exact MP4 to YouTube Studio.",
                "video": "${{ steps.produce_video.result }}",
            },
        ),
    ])
    calls = {"count": 0}

    async def fake_generate(_task, _context):
        calls["count"] += 1
        return first if calls["count"] == 1 else second

    monkeypatch.setattr(planner_mod, "_generate_plan", fake_generate)
    db = _FakeDB(task)

    await planner_mod.plan_task(db, "task_1", execution_mode="live")

    assert calls["count"] == 2
    assert [step.key for step in persisted["plan"].steps] == [
        "produce_video", "upload_to_youtube",
    ]
    assert task.details["_replan_context"]["required_plan_step_errors"] == [
        "missing required step 'upload_to_youtube'",
    ]


def test_required_plan_steps_reject_upload_without_producer_reference():
    task = _FakeTask()
    task.details = {
        "required_plan_steps": [
            {
                "key": "produce_video",
                "kind": "subagent",
                "service_key": "stickman.production",
            },
            {
                "key": "upload_to_youtube",
                "kind": "subagent",
                "service_key": "stickman.production",
                "depends_on": ["produce_video"],
                "required_refs": ["produce_video"],
            },
        ],
    }
    plan = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            params={"prompt": "Produce the finished MP4."},
        ),
        PlanStep(
            key="upload_to_youtube",
            kind="subagent",
            service_key="stickman.production",
            depends_on=["produce_video"],
            params={"prompt": "Use Chrome to upload the MP4 to YouTube Studio."},
        ),
    ])

    assert planner_mod._required_plan_step_errors(task, plan) == [
        "required step 'upload_to_youtube' must reference required step 'produce_video'",
    ]


def test_required_plan_steps_reject_video_producer_without_artifact_contract():
    task = _FakeTask()
    task.details = {
        "required_plan_steps": [
            {
                "key": "produce_video",
                "kind": "subagent",
                "service_key": "stickman.production",
                "output_shape": "ArtifactResult",
                "expects": ["files"],
            },
            {
                "key": "upload_to_youtube",
                "kind": "subagent",
                "service_key": "stickman.production",
                "depends_on": ["produce_video"],
                "required_refs": ["produce_video"],
                "required_ref_fields": {"produce_video": ["files"]},
            },
        ],
    }
    plan = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            params={"prompt": "Produce the finished MP4."},
        ),
        PlanStep(
            key="upload_to_youtube",
            kind="subagent",
            service_key="stickman.production",
            depends_on=["produce_video"],
            params={
                "prompt": "Use Chrome to upload the MP4 to YouTube Studio.",
                "video": "${{ steps.produce_video.result }}",
            },
        ),
    ])

    assert planner_mod._required_plan_step_errors(task, plan) == [
        "required step 'produce_video' must have output_shape='ArtifactResult'",
        "required step 'produce_video' must declare expects=['files']",
        "required step 'upload_to_youtube' must reference produce_video.files",
    ]


def test_required_plan_steps_accept_exact_video_artifact_handoff():
    task = _FakeTask()
    task.details = {
        "required_plan_steps": [
            {
                "key": "produce_video",
                "kind": "subagent",
                "service_key": "stickman.production",
                "output_shape": "ArtifactResult",
                "expects": ["files"],
            },
            {
                "key": "upload_to_youtube",
                "kind": "subagent",
                "service_key": "stickman.production",
                "depends_on": ["produce_video"],
                "required_refs": ["produce_video"],
                "required_ref_fields": {"produce_video": ["files"]},
            },
        ],
    }
    plan = Plan(steps=[
        PlanStep(
            key="produce_video",
            kind="subagent",
            service_key="stickman.production",
            output_shape="ArtifactResult",
            expects=["files"],
            params={"prompt": "Produce the finished MP4."},
        ),
        PlanStep(
            key="upload_to_youtube",
            kind="subagent",
            service_key="stickman.production",
            depends_on=["produce_video"],
            params={
                "prompt": "Use Chrome to upload the MP4 to YouTube Studio.",
                "video": "${{ steps.produce_video.result.files }}",
            },
        ),
    ])

    assert planner_mod._required_plan_step_errors(task, plan) == []


class _FakeResult:
    def __init__(self, task):
        self._task = task

    def one_or_none(self):
        return self._task

    def scalar_one_or_none(self):
        return self._task


class _FakeTask:
    def __init__(self):
        self.id = "task_1"
        self.entity_id = "ent_1"
        self.workspace_id = None
        self.task_type = "general"
        self.status = "in_progress"
        self.owner_subscription_id = None
        self.details = {}


def test_replan_keeps_same_denied_operation_behind_approval() -> None:
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "decision": {
                    "choice": "request_changes",
                    "guidance": "Use the approved image instead.",
                },
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_post",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params={"prompt": "Publish the revised post."},
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is True


def test_replan_allows_a_materially_different_alternative() -> None:
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="save_draft_for_user",
        kind="subagent",
        service_key="content",
        capability_id="file.write",
        params={"prompt": "Save a local draft without publishing."},
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is False


def test_replan_gates_renamed_step_with_same_subject_and_params() -> None:
    from packages.core.ai.runtime import (
        approval_args_hash,
        approval_stable_target_hash,
    )

    params = {
        "prompt": "Publish the approved launch post.",
        "channel": "launch-account",
    }
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "params_fingerprint": approval_args_hash(params),
                "target_fingerprint": approval_stable_target_hash(params),
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_launch_post",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params=params,
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is True


def test_replan_gates_renamed_step_after_reviewable_content_changes() -> None:
    from packages.core.ai.runtime import approval_stable_target_hash

    original_params = {
        "prompt": "Publish the original launch post.",
        "channel": "launch-account",
        "payload": {"caption": "Original caption", "visibility": "public"},
    }
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "target_fingerprint": approval_stable_target_hash(original_params),
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_revised_post",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params={
            "prompt": "Publish the revised launch post.",
            "channel": "launch-account",
            "payload": {"caption": "Revised caption", "visibility": "public"},
        },
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is True


def test_replan_does_not_gate_same_capability_for_different_target() -> None:
    from packages.core.ai.runtime import (
        approval_args_hash,
        approval_stable_target_hash,
    )

    original_params = {
        "prompt": "Publish launch post.",
        "channel": "launch-account",
    }

    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "params_fingerprint": approval_args_hash(original_params),
                "target_fingerprint": approval_stable_target_hash(original_params),
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_support_update",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params={
            "prompt": "Publish support update.",
            "channel": "support-account",
        },
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is False


def test_replan_gates_renamed_step_with_dynamic_target_reference() -> None:
    from packages.core.ai.runtime import approval_stable_target_hash

    original_params = {
        "prompt": "Publish launch post.",
        "channel": "launch-account",
    }
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "target_fingerprint": approval_stable_target_hash(original_params),
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_selected_channel",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params={
            "prompt": "Publish the revised launch post.",
            "channel": "${{ steps.select_channel.result.name }}",
        },
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is True


def test_replan_dynamic_review_content_does_not_gate_different_target() -> None:
    from packages.core.ai.runtime import approval_stable_target_hash

    original_params = {
        "prompt": "Publish launch post.",
        "channel": "launch-account",
    }
    task = _FakeTask()
    task.details = {
        "_replan_context": {
            "approval_constraints": [{
                "step_key": "publish_post",
                "kind": "subagent",
                "action_key": "social.publish",
                "capability_id": "external.social",
                "target_fingerprint": approval_stable_target_hash(original_params),
            }],
        },
    }
    plan = Plan(steps=[PlanStep(
        key="publish_support_update",
        kind="subagent",
        service_key="content",
        action_key="social.publish",
        capability_id="external.social",
        params={
            "prompt": "${{ steps.draft_support_update.result.text }}",
            "channel": "support-account",
        },
        requires_approval=False,
    )])

    constrained = planner_mod._apply_replan_approval_constraints(task, plan)

    assert constrained.steps[0].requires_approval is False


class _FakeDB:
    def __init__(self, task):
        self._task = task

    async def execute(self, statement, *_args, **_kwargs):
        if "execution_plans" in str(statement):
            return _FakeResult(None)
        return _FakeResult(self._task)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_plan_task_rejects_inactive_workspace_before_context(monkeypatch):
    from types import SimpleNamespace

    task = _FakeTask()
    task.workspace_id = "ws_1"

    async def _deleted_workspace(*_args, **_kwargs):
        return SimpleNamespace(deleted_at=object(), status="active")

    async def _unexpected_context(*_args, **_kwargs):
        raise AssertionError("inactive Workspace must fail before context/provider work")

    monkeypatch.setattr(
        "packages.core.services.workspace_access.lock_workspace_access_boundary",
        _deleted_workspace,
    )
    monkeypatch.setattr(planner_mod, "_gather_context", _unexpected_context)

    with pytest.raises(planner_mod.PlannerError, match="not active"):
        await planner_mod.plan_task(_FakeDB(task), task.id, execution_mode="live")


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
async def test_plan_task_rejects_model_labeled_setup_work_while_setup_is_blocked(
    monkeypatch,
):
    from types import SimpleNamespace

    from packages.core.services import workspace_readiness
    from packages.core.services.workspace_readiness import (
        WorkspaceReadinessPartStatus,
    )

    task = _FakeTask()
    task.workspace_id = "ws_1"
    # Matching the allowlisted label is not authorization. Setup automation
    # enters through the server-owned ScheduledJob path, not Planner.
    task.details = {"strategist_task_key": "prepare_workspace_identity"}
    workspace = SimpleNamespace(id="ws_1")

    async def _context(_db, _task):
        return SimpleNamespace(workspace=workspace)

    async def _blocked(*_args, **_kwargs):
        return WorkspaceReadinessPartStatus(
            key="blocking_setup",
            name="Blocking Workspace setup",
            role="setup gate",
            check="live requirements",
            status="missing",
            summary="setup incomplete",
            missing_setup_key="blocking_setup_incomplete",
            details={"allowed_setup_task_keys": ["prepare_workspace_identity"]},
        )

    generated = False

    async def _generate(_task, _context):
        nonlocal generated
        generated = True
        return _clean_plan()

    async def _active_workspace(*_args, **_kwargs):
        return SimpleNamespace(deleted_at=None, status="active")

    monkeypatch.setattr(planner_mod, "_gather_context", _context)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.lock_workspace_access_boundary",
        _active_workspace,
    )
    monkeypatch.setattr(
        workspace_readiness,
        "evaluate_current_workspace_blocking_setup",
        _blocked,
    )
    monkeypatch.setattr(planner_mod, "_generate_plan", _generate)

    with pytest.raises(planner_mod.PlannerError, match="setup is incomplete"):
        await planner_mod.plan_task(_FakeDB(task), task.id, execution_mode="live")

    assert generated is False


@pytest.mark.asyncio
async def test_plan_task_rechecks_setup_before_plan_persistence(monkeypatch):
    from types import SimpleNamespace

    from packages.core.services import workspace_readiness
    from packages.core.services.workspace_readiness import (
        WorkspaceReadinessPartStatus,
    )

    task = _FakeTask()
    task.workspace_id = "ws_1"
    task.details = {"strategist_task_key": "normal_growth_task"}
    context = SimpleNamespace(workspace=SimpleNamespace(id="ws_1"))
    checks = 0

    async def _context(_db, _task):
        return context

    async def _becomes_blocked(*_args, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 1:
            return None
        return WorkspaceReadinessPartStatus(
            key="blocking_setup",
            name="Blocking Workspace setup",
            role="setup gate",
            check="live requirements",
            status="missing",
            summary="setup became incomplete",
            missing_setup_key="blocking_setup_incomplete",
            details={"allowed_setup_task_keys": ["repair_connection"]},
        )

    async def _generate(_task, _context):
        return _clean_plan()

    persisted = False

    async def _persist(*_args, **_kwargs):
        nonlocal persisted
        persisted = True
        return object()

    async def _active_workspace(*_args, **_kwargs):
        return SimpleNamespace(deleted_at=None, status="active")

    monkeypatch.setattr(planner_mod, "_gather_context", _context)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.lock_workspace_access_boundary",
        _active_workspace,
    )
    monkeypatch.setattr(
        workspace_readiness,
        "evaluate_current_workspace_blocking_setup",
        _becomes_blocked,
    )
    monkeypatch.setattr(planner_mod, "runtime_planner_llm_billing_context", _noop_billing)
    monkeypatch.setattr(planner_mod, "_generate_plan", _generate)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)
    monkeypatch.setattr(planner_mod, "create_plan_from_dag", _persist)

    with pytest.raises(planner_mod.PlannerError, match="setup is incomplete"):
        await planner_mod.plan_task(_FakeDB(task), task.id, execution_mode="live")

    assert checks == 2
    assert persisted is False


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
async def test_enforcement_replan_recovers_stale_artifact_reference(monkeypatch):
    """The screenshot-shaped stale ref is corrected before persistence."""
    persisted = _patch_common(monkeypatch)
    monkeypatch.setattr(planner_mod, "_enforce_allowlists", lambda plan, ctx: None)

    calls = {"n": 0}

    async def _stale_then_direct(task, ctx):
        calls["n"] += 1
        return _stale_artifact_review_plan() if calls["n"] == 1 else _direct_artifact_review_plan()

    monkeypatch.setattr(planner_mod, "_generate_plan", _stale_then_direct)

    task = _FakeTask()
    db = _FakeDB(task)
    await planner_mod.plan_task(db, "task_1", execution_mode="live")

    assert calls["n"] == 2
    assert "contract_gaps" in task.details["_replan_context"]
    saved = persisted["plan"]
    assert saved.steps[1].params["review_artifacts"] == "${{ steps.prepare_outreach_pack.result.files }}"
    assert saved.steps[2].params["prompt"].endswith("${{ steps.prepare_outreach_pack.result.files }}")


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


def test_plan_and_run_task_capability_error_fails_no_retry(monkeypatch):
    from packages.core.tasks import ai_tasks

    def _raise_capability(coro):
        coro.close()
        raise planner_mod.CapabilityError("skill is no longer callable")

    monkeypatch.setattr(ai_tasks, "_run_async", _raise_capability)

    failed = {}
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda task_id, reason, **kwargs: failed.update(
            task_id=task_id,
            reason=reason,
            **kwargs,
        ),
    )

    def _no_retry(*_args, **_kwargs):
        raise AssertionError("capability rejection must not burn a Planner retry")

    monkeypatch.setattr(ai_tasks.plan_and_run_task, "retry", _no_retry, raising=False)

    result = ai_tasks.plan_and_run_task.run("task-capability-rejected")

    assert result == {
        "plan_id": None,
        "status": "failed",
        "error": "skill is no longer callable",
    }
    assert failed == {
        "task_id": "task-capability-rejected",
        "reason": "Plan capability validation failed: skill is no longer callable",
        "error_type": "CapabilityError",
    }


def test_plan_and_run_task_credit_exhaustion_returns_terminal_failure(monkeypatch):
    from packages.core.ai.llm_client import CreditExhaustedError
    from packages.core.tasks import ai_tasks

    def _raise_credit_exhaustion(coro):
        coro.close()
        raise CreditExhaustedError("no credits")

    failed = {}
    monkeypatch.setattr(ai_tasks, "_run_async", _raise_credit_exhaustion)
    monkeypatch.setattr(
        ai_tasks,
        "_mark_task_failed",
        lambda task_id, reason, **kwargs: failed.update(
            task_id=task_id,
            reason=reason,
            **kwargs,
        ),
    )

    result = ai_tasks.plan_and_run_task.run("task-credit-exhausted")

    assert result == {
        "plan_id": None,
        "status": "failed",
        "error": "Credits exhausted: no credits",
    }
    assert failed == {
        "task_id": "task-credit-exhausted",
        "reason": "Credits exhausted: no credits",
        "error_type": "CreditExhaustedError",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [
        pytest.param("CreditExhaustedError", id="exhausted"),
        pytest.param("CreditCheckUnavailableError", id="check-unavailable"),
    ],
)
async def test_generate_plan_never_falls_back_after_credit_gate_failure(
    monkeypatch,
    error_type,
):
    from packages.core.ai import llm_client

    error_class = getattr(llm_client, error_type)

    async def _reject_credit(*_args, **_kwargs):
        raise error_class("credit gate rejected the call")

    monkeypatch.setattr(
        planner_mod,
        "runtime_execute_planner_chat_turn",
        _reject_credit,
    )
    context = planner_mod._Context(
        workspace=None,
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=set(),
        provider_actions={},
    )

    with pytest.raises(error_class, match="credit gate rejected"):
        await planner_mod._generate_plan(_FakeTask(), context)


def test_plan_and_run_task_redispatches_the_committed_active_plan(monkeypatch):
    from packages.core.plans.service import ActiveTaskPlanError
    from packages.core.tasks import ai_tasks

    def _raise_active(coro):
        coro.close()
        raise ActiveTaskPlanError("task-1", "plan-1", "draft")

    dispatched: list[str] = []
    monkeypatch.setattr(ai_tasks, "_run_async", _raise_active)
    monkeypatch.setattr(
        ai_tasks.run_plan,
        "delay",
        lambda plan_id: dispatched.append(plan_id),
    )

    result = ai_tasks.plan_and_run_task.run("task-1")

    assert result == {"plan_id": "plan-1", "status": "draft"}
    assert dispatched == ["plan-1"]


@pytest.mark.asyncio
async def test_from_task_api_retry_dispatches_the_committed_active_plan(monkeypatch):
    from types import SimpleNamespace

    from apps.api.routers import plans as plans_router
    from packages.core.plans.service import ActiveTaskPlanError

    plan = SimpleNamespace(
        id="plan-1",
        task_id="task-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        execution_mode="live",
    )

    class _Db:
        def __init__(self):
            self.rollbacks = 0
            self.refreshed = []

        async def rollback(self):
            self.rollbacks += 1

        async def refresh(self, row):
            self.refreshed.append(row)

    db = _Db()
    dispatched: list[str] = []

    async def _authorize(*_args, **_kwargs):
        return None

    async def _raise_active(*_args, **_kwargs):
        raise ActiveTaskPlanError("task-1", "plan-1", "draft")

    async def _get_plan(*_args, **_kwargs):
        return plan

    async def _writable(*_args, **_kwargs):
        return None

    async def _dispatch(_db, row, **_kwargs):
        dispatched.append(row.id)

    monkeypatch.setattr(plans_router, "_authorize_task_for_planner", _authorize)
    monkeypatch.setattr(plans_router, "plan_task_and_commit", _raise_active)
    monkeypatch.setattr(plans_router, "get_plan", _get_plan)
    monkeypatch.setattr(plans_router, "require_workspace_writable", _writable)
    monkeypatch.setattr(plans_router, "_maybe_dispatch", _dispatch)
    monkeypatch.setattr(plans_router, "_to_plan", lambda row: row.id)

    result = await plans_router.create_from_task(
        "task-1",
        plans_router.PlanFromTaskRequest(),
        user=SimpleNamespace(id="user-1", entity_id="entity-1"),
        db=db,
    )

    assert result == "plan-1"
    assert db.rollbacks == 1
    assert db.refreshed == [plan]
    assert dispatched == ["plan-1"]


def test_plan_and_run_task_schedules_one_claim_recovery(monkeypatch):
    from packages.core.plans.planner import TaskPlanClaimHeldError
    from packages.core.tasks import ai_tasks

    def _raise_claim_held(coro):
        coro.close()
        raise TaskPlanClaimHeldError("task-1")

    scheduled: list[dict] = []
    monkeypatch.setattr(ai_tasks, "_run_async", _raise_claim_held)
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "apply_async",
        lambda *args, **kwargs: scheduled.append({"args": args, **kwargs}),
    )

    result = ai_tasks.plan_and_run_task.run("task-1")

    assert result["duplicate_suppressed"] is True
    assert result["recheck_scheduled"] is True
    assert scheduled[0]["args"] == ["task-1"]
    assert scheduled[0]["kwargs"] == {"planning_claim_recheck": True}


def test_plan_claim_recovery_reschedules_while_claim_remains_held(monkeypatch):
    from packages.core.plans.planner import TaskPlanClaimHeldError
    from packages.core.tasks import ai_tasks

    def _raise_claim_held(coro):
        coro.close()
        raise TaskPlanClaimHeldError("task-1")

    scheduled: list[dict] = []
    monkeypatch.setattr(ai_tasks, "_run_async", _raise_claim_held)
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "apply_async",
        lambda *args, **kwargs: scheduled.append({"args": args, **kwargs}),
    )

    result = ai_tasks.plan_and_run_task.run(
        "task-1",
        planning_claim_recheck=True,
    )

    assert result["duplicate_suppressed"] is True
    assert result["recheck_scheduled"] is True
    assert scheduled[0]["args"] == ["task-1"]
    assert scheduled[0]["kwargs"] == {"planning_claim_recheck": True}


def test_plan_claim_recovery_stops_when_the_task_is_terminal(monkeypatch):
    from packages.core.plans.planner import TaskPlanAdmissionError
    from packages.core.tasks import ai_tasks

    def _raise_terminal(coro):
        coro.close()
        raise TaskPlanAdmissionError(
            "task-1",
            "completed",
            "task status 'completed' cannot start a new Plan",
        )

    monkeypatch.setattr(ai_tasks, "_run_async", _raise_terminal)
    monkeypatch.setattr(
        ai_tasks.plan_and_run_task,
        "retry",
        lambda **_kwargs: pytest.fail("terminal recovery must not retry"),
    )

    result = ai_tasks.plan_and_run_task.run(
        "task-1",
        planning_claim_recheck=True,
    )

    assert result == {
        "plan_id": None,
        "status": "completed",
        "duplicate_suppressed": True,
        "recheck_scheduled": False,
    }


@pytest.mark.asyncio
async def test_plan_task_and_commit_denied_claim_skips_billable_work(monkeypatch):
    from contextlib import asynccontextmanager

    from packages.core.services import workflow_run_execution_claim as claim_service

    @asynccontextmanager
    async def _held_claim(task_id):
        yield claim_service._claim(
            task_id,
            "other-owner",
            granted=False,
            reason=claim_service.CLAIM_HELD,
        )

    async def _unexpected_plan(*_args, **_kwargs):
        raise AssertionError("a denied claim must not reach Planner/provider work")

    monkeypatch.setattr(planner_mod, "task_plan_execution_claim", _held_claim)
    monkeypatch.setattr(planner_mod, "plan_task", _unexpected_plan)

    with pytest.raises(planner_mod.TaskPlanClaimHeldError):
        await planner_mod.plan_task_and_commit(object(), "task-1")


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


def test_planner_system_prompt_tells_planner_to_reuse_completed_artifacts():
    from packages.core.ai.runtime.planning import runtime_planner_system_prompt

    out = runtime_planner_system_prompt(
        subscriptions=[],
        agents_by_id={},
        allowed_service_keys=[],
    )
    assert "HAS ALREADY RUN" in out
    assert "no_output" in out
    # The reuse handles the executor now emits.
    assert "artifacts[].fs_path" in out
    assert "document_id" in out
    # Cross-plan refs do not resolve — the planner must inline literals.
    assert "CANNOT reach a previous plan" in out
    assert "inline the literal value" in out
