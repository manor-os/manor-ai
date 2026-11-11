"""Plan-step round cap (Section B1).

``subagent_specs_for_surface`` gives the two worker surfaces exactly one spec
and ``runtime_prepare_subagent_run`` clamps every worker step to its
``max_steps``. That cap used to be 12: a coordinator asking for 60 rounds got
12, and a step that never asked got 12 too. It is now ``PLAN_STEP_MAX_ROUNDS``.
"""
from __future__ import annotations

import pytest

from packages.core.ai.runtime import (
    AIRuntimeRequest,
    RuntimeResolver,
    runtime_prepare_subagent_run,
    runtime_subagent_specs_from_envelope,
    subagent_specs_for_surface,
)
from packages.core.ai.runtime.profiles import RuntimeProfile, profile_spec_for_surface
from packages.core.ai.runtime.subagents import PLAN_STEP_MAX_ROUNDS, SUBAGENT_MAX_ROUNDS
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.workspace_delegation_actions import (
    _delegation_max_rounds,
)

WORKER_SURFACES = (ChatSurface.SCHEDULED_AGENT_RUN, ChatSurface.WORKFLOW_AGENT_STEP)


def _worker_envelope(surface: ChatSurface):
    return RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(surface=surface, entity_id="ent_1", user_id="user_1", workspace_id="ws_1")
    )


def test_plan_step_round_cap_is_declared_once():
    assert PLAN_STEP_MAX_ROUNDS == SUBAGENT_MAX_ROUNDS == 200


@pytest.mark.parametrize("surface", WORKER_SURFACES)
def test_worker_surfaces_expose_exactly_one_spec_with_the_plan_step_cap(surface):
    specs = subagent_specs_for_surface(
        surface=surface,
        profile=profile_spec_for_surface(surface).profile,
        workspace_id="ws_1",
    )
    # Two visible specs with no requested name would be ``subagent_ambiguous``
    # and every plan step would fail — exactly one per surface, by name.
    assert [spec.name for spec in specs] == ["long_research_worker"]
    (spec,) = specs
    assert spec.max_steps == PLAN_STEP_MAX_ROUNDS
    assert "plan step" in spec.purpose.lower()
    assert "delegate" in spec.purpose.lower()


@pytest.mark.parametrize("surface", WORKER_SURFACES)
def test_the_resolved_envelope_carries_the_raised_cap(surface):
    envelope = _worker_envelope(surface)
    assert envelope.profile not in {RuntimeProfile.EXTERNAL_CUSTOMER_SAFE, RuntimeProfile.VOICE_SAFE}
    specs = runtime_subagent_specs_from_envelope(envelope)
    assert [spec.name for spec in specs] == ["long_research_worker"]
    assert specs[0].max_steps == PLAN_STEP_MAX_ROUNDS


@pytest.mark.parametrize("surface", WORKER_SURFACES)
def test_a_worker_step_requesting_60_rounds_keeps_60(surface):
    run = runtime_prepare_subagent_run(_worker_envelope(surface), requested_max_rounds=60)
    assert run.spec is not None and run.spec.name == "long_research_worker"
    assert run.max_rounds == 60


@pytest.mark.parametrize("surface", WORKER_SURFACES)
def test_a_worker_step_requesting_nothing_gets_the_full_cap(surface):
    run = runtime_prepare_subagent_run(_worker_envelope(surface))
    assert run.max_rounds == PLAN_STEP_MAX_ROUNDS == 200


def test_a_request_above_the_cap_is_still_clamped():
    run = runtime_prepare_subagent_run(
        _worker_envelope(ChatSurface.SCHEDULED_AGENT_RUN), requested_max_rounds=500,
    )
    assert run.max_rounds == PLAN_STEP_MAX_ROUNDS


def test_chat_surface_subagents_share_the_one_round_cap():
    """Every sub-agent, interactive or not, gets the same round cap."""
    workspace = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT, entity_id="ent_1", user_id="user_1", workspace_id="ws_1",
        )
    )
    specs = {spec.name: spec.max_steps for spec in runtime_subagent_specs_from_envelope(workspace)}
    assert specs == {"workspace_strategist": 200, **{f"workspace_{part}_designer": 200 for part in (
        "app", "workflow", "agents", "knowledge", "automations")}}


def test_nested_plan_child_uses_plan_cap_and_chat_uses_general_bound():
    assert _delegation_max_rounds(None, task_id="task", step_id="child") == 200
    assert _delegation_max_rounds(60, task_id="task", step_id="child") == 60
    assert _delegation_max_rounds(500, task_id="task", step_id="child") == 200
    # Chat applies its turn-policy cap at the call site after this helper.
    assert _delegation_max_rounds(None, task_id=None, step_id=None) == 200
    assert _delegation_max_rounds(500, task_id=None, step_id=None) == 200
