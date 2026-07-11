"""Unit tests for the recipe.strategist template wiring.

Pure-Python — no DB. Tests:
  * _evaluate_skip_conditions correctly parses + evaluates the tiny DSL
  * _enforce_proposal_shape caps + annotates the cohort
  * _format_strategist_template renders the expected blocks
  * Installer merges recipe.strategist into operating_model.strategist
    with the legacy ``cadence: str`` split

The Strategist code paths that touch the DB / LLM (gather_context,
generate_proposal) are exercised by the integration smoke; here we
focus on the new helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from packages.core.blueprints.payload import migrate_payload
from packages.core.ai.runtime.strategist import (
    runtime_strategist_user_prompt,
    runtime_strategist_template_block as _format_strategist_template,
)
from packages.core.strategist.service import (
    _enforce_proposal_shape,
    _eval_one_condition,
    _evaluate_skip_conditions,
    _resolve_skip_name,
)


# ── Stub Strategist context for the helpers ──────────────────────────


@dataclass
class _StubWorkspace:
    name: str = "Test workspace"
    monthly_budget_usd: float | None = None
    monthly_spent_usd: float | None = None
    operating_model: dict[str, Any] = field(default_factory=dict)


@dataclass
class _StubCtx:
    workspace: _StubWorkspace = field(default_factory=_StubWorkspace)
    strategist_template: dict[str, Any] = field(default_factory=dict)
    open_proposed_tasks: list = field(default_factory=list)
    recent_proposal_outcomes: dict = field(default_factory=dict)
    missing_setup: list = field(default_factory=list)
    workspace_readiness: dict = field(default_factory=dict)
    configured_integrations: list = field(default_factory=list)
    configured_channels: list = field(default_factory=list)
    knowledge_nets: list = field(default_factory=list)
    governance_policy: dict | None = None
    calibration: dict = field(default_factory=dict)
    goals: list = field(default_factory=list)
    workspace_evaluation: dict | None = None
    recent_tasks: list = field(default_factory=list)
    recent_plans: list = field(default_factory=list)
    work_batch_reconciliation: list = field(default_factory=list)
    recent_activity: list = field(default_factory=list)
    recent_runtime_evidence: list = field(default_factory=list)
    learning_candidates: list = field(default_factory=list)
    operating_memory: str = ""
    relevant_memory: list = field(default_factory=list)
    trigger: str = "manual"


@dataclass
class _StubTask:
    title: str
    task_key: str = ""
    depends_on_task_keys: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class _StubProposal:
    tasks: list = field(default_factory=list)
    notes: str | None = None
    summary: str = ""


# ── Skip-condition evaluator ──────────────────────────────────────────


def test_skip_no_template_returns_none():
    ctx = _StubCtx()
    assert _evaluate_skip_conditions(ctx) is None


def test_user_prompt_includes_workspace_readiness_roles_and_checks():
    ctx = _StubCtx(
        missing_setup=["no_channels"],
        workspace_readiness={
            "parts": [
                {
                    "key": "channels",
                    "name": "Channels",
                    "status": "missing",
                    "summary": "1 declared external channel requirement is not configured.",
                    "role": "Communication surfaces for inbound/outbound messages.",
                    "check": "Declared non-built-in channels have concrete ChannelConfig rows.",
                    "missing_setup_key": "no_channels",
                }
            ]
        },
        configured_channels=[],
    )

    prompt = runtime_strategist_user_prompt(ctx, review_id="rv_readiness")

    assert "# Workspace readiness" in prompt
    assert "Channels: missing" in prompt
    assert "Role: Communication surfaces" in prompt
    assert "Check: Declared non-built-in channels" in prompt
    assert "Required workspace channel declarations are not configured yet" in prompt


def test_user_prompt_includes_stale_work_batch_reconciliation():
    ctx = _StubCtx(
        work_batch_reconciliation=[
            {
                "batch_id": "batch_stale",
                "status": "stalled",
                "summary": "Strategist proposal task wave",
                "open_task_ids": ["task_1"],
                "stale_task_ids": ["task_1"],
                "missing_task_ids": [],
                "stale_tasks": [
                    {
                        "task_id": "task_1",
                        "title": "Draft launch assets",
                        "status": "in_progress",
                        "age_hours": 31.5,
                        "owner_service_key": "content_creation",
                    }
                ],
            }
        ],
    )

    prompt = runtime_strategist_user_prompt(ctx, review_id="rv_stale")

    assert "# Work batch reconciliation" in prompt
    assert "batch=batch_stale status=stalled" in prompt
    assert "[in_progress] Draft launch assets" in prompt
    assert "owner=content_creation" in prompt


def test_skip_no_trigger_conditions_returns_none():
    ctx = _StubCtx(strategist_template={"cadence": {"schedule": "daily"}})
    assert _evaluate_skip_conditions(ctx) is None


def test_skip_open_proposals_threshold():
    ctx = _StubCtx(
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": ["open_proposed_tasks_count >= 3"]}}},
        open_proposed_tasks=[1, 2, 3],
    )
    assert _evaluate_skip_conditions(ctx) == "open_proposed_tasks_count >= 3"


def test_skip_below_threshold_passes():
    ctx = _StubCtx(
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": ["open_proposed_tasks_count >= 3"]}}},
        open_proposed_tasks=[1],  # 1 < 3
    )
    assert _evaluate_skip_conditions(ctx) is None


def test_skip_first_matching_expression_wins():
    ctx = _StubCtx(
        strategist_template={
            "cadence": {
                "trigger_conditions": {
                    "skip_if_any": [
                        "calibration_sample_size < 1",
                        "open_proposed_tasks_count >= 5",
                    ]
                }
            }
        },
        calibration={"sample_size": 0},
        open_proposed_tasks=list(range(10)),
    )
    # Both match but evaluator stops at the first one.
    assert _evaluate_skip_conditions(ctx) == "calibration_sample_size < 1"


def test_skip_budget_remaining_pct():
    ws = _StubWorkspace(monthly_budget_usd=100.0, monthly_spent_usd=95.0)
    ctx = _StubCtx(
        workspace=ws,
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": ["budget_remaining_pct < 10"]}}},
    )
    # 5% remaining → matches < 10
    assert _evaluate_skip_conditions(ctx) is not None


def test_skip_budget_no_cap_means_full_remaining():
    ws = _StubWorkspace(monthly_budget_usd=None)
    ctx = _StubCtx(
        workspace=ws,
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": ["budget_remaining_pct < 10"]}}},
    )
    # No cap → 100% remaining → no match
    assert _evaluate_skip_conditions(ctx) is None


def test_skip_unknown_name_logged_as_zero():
    """Unknown variable name resolves to 0, not an exception."""
    ctx = _StubCtx(
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": ["typo_name >= 1"]}}},
    )
    # 0 >= 1 is false — review still proceeds
    assert _evaluate_skip_conditions(ctx) is None


def test_skip_all_operators():
    ctx = _StubCtx(
        strategist_template={"cadence": {"trigger_conditions": {"skip_if_any": []}}},
        open_proposed_tasks=[1, 2],  # count = 2
    )
    # Inject test cases via the lower-level evaluator
    assert _eval_one_condition("open_proposed_tasks_count == 2", ctx)
    assert _eval_one_condition("open_proposed_tasks_count != 3", ctx)
    assert _eval_one_condition("open_proposed_tasks_count < 5", ctx)
    assert _eval_one_condition("open_proposed_tasks_count <= 2", ctx)
    assert _eval_one_condition("open_proposed_tasks_count > 1", ctx)
    assert _eval_one_condition("open_proposed_tasks_count >= 2", ctx)
    assert not _eval_one_condition("open_proposed_tasks_count > 5", ctx)


def test_resolve_unknown_raises_keyerror():
    ctx = _StubCtx()
    with pytest.raises(KeyError):
        _resolve_skip_name("nonsense_metric", ctx)


# ── Proposal shape post-filter ────────────────────────────────────────


def test_proposal_shape_caps_tasks():
    ctx = _StubCtx(strategist_template={"proposal_shape": {"max_tasks_per_cycle": 2}})
    proposal = _StubProposal(
        tasks=[
            _StubTask(title="t1", task_key="k1"),
            _StubTask(title="t2", task_key="k2"),
            _StubTask(title="t3", task_key="k3"),
            _StubTask(title="t4", task_key="k4"),
        ]
    )
    _enforce_proposal_shape(proposal, ctx)
    assert [t.title for t in proposal.tasks] == ["t1", "t2"]
    assert "Dropped 2" in (proposal.notes or "")
    assert "max_tasks_per_cycle=2" in (proposal.notes or "")


def test_proposal_shape_rewrites_dependencies_after_truncate():
    ctx = _StubCtx(strategist_template={"proposal_shape": {"max_tasks_per_cycle": 1}})
    proposal = _StubProposal(
        tasks=[
            _StubTask(title="t1", task_key="k1"),
            _StubTask(title="t2", task_key="k2", depends_on_task_keys=["k1", "k3"]),
        ]
    )
    _enforce_proposal_shape(proposal, ctx)
    # Only k1 survives; t2 was dropped along with its deps
    assert len(proposal.tasks) == 1
    assert proposal.tasks[0].depends_on_task_keys == []


def test_proposal_shape_no_cap_no_change():
    ctx = _StubCtx(strategist_template={"proposal_shape": {}})
    proposal = _StubProposal(tasks=[_StubTask(title="t1"), _StubTask(title="t2")])
    _enforce_proposal_shape(proposal, ctx)
    assert len(proposal.tasks) == 2
    assert proposal.notes is None


def test_proposal_shape_preferred_categories_warns_not_drops():
    ctx = _StubCtx(strategist_template={"proposal_shape": {"preferred_categories": ["content", "experiment"]}})
    proposal = _StubProposal(
        tasks=[
            _StubTask(title="t1", details={"category": "content"}),
            _StubTask(title="t2", details={"category": "billing"}),
        ]
    )
    _enforce_proposal_shape(proposal, ctx)
    # Not dropped, just noted
    assert len(proposal.tasks) == 2
    assert "preferred_categories" in (proposal.notes or "")
    assert "billing" in (proposal.notes or "")


def test_proposal_shape_must_include_warns_when_missing():
    ctx = _StubCtx(strategist_template={"proposal_shape": {"must_include_categories_per_week": ["experiment"]}})
    proposal = _StubProposal(
        tasks=[
            _StubTask(title="t1", details={"category": "content"}),
        ]
    )
    _enforce_proposal_shape(proposal, ctx)
    assert "must-include" in (proposal.notes or "")
    assert "experiment" in (proposal.notes or "")


def test_proposal_shape_must_include_silent_when_satisfied():
    ctx = _StubCtx(strategist_template={"proposal_shape": {"must_include_categories_per_week": ["experiment"]}})
    proposal = _StubProposal(
        tasks=[
            _StubTask(title="t1", details={"category": "experiment"}),
        ]
    )
    _enforce_proposal_shape(proposal, ctx)
    assert proposal.notes is None or "must-include" not in proposal.notes


def test_proposal_shape_no_template_noops():
    ctx = _StubCtx()  # no strategist_template
    proposal = _StubProposal(tasks=[_StubTask(title="t1")] * 5)
    _enforce_proposal_shape(proposal, ctx)
    assert len(proposal.tasks) == 5


# ── Prompt block rendering ────────────────────────────────────────────


def test_format_template_empty_returns_empty_string():
    ctx = _StubCtx()
    assert _format_strategist_template(ctx) == ""


def test_format_template_business_model_block():
    ctx = _StubCtx(
        strategist_template={
            "business_model": {
                "model_type": "social_growth",
                "primary_signal": "follower_count",
                "anti_signals": ["follower_via_promo"],
                "decision_window": "weekly",
            }
        }
    )
    out = _format_strategist_template(ctx)
    assert "Business model" in out
    assert "social_growth" in out
    assert "follower_count" in out
    assert "follower_via_promo" in out
    assert "weekly" in out


def test_format_template_proposal_shape_block():
    ctx = _StubCtx(
        strategist_template={
            "proposal_shape": {
                "max_tasks_per_cycle": 3,
                "preferred_categories": ["content"],
                "task_horizon_hours": [4, 48],
            }
        }
    )
    out = _format_strategist_template(ctx)
    assert "Propose at most 3" in out
    assert "content" in out
    assert "4-48 hours" in out


def test_format_template_do_not_propose_block():
    ctx = _StubCtx(
        strategist_template={
            "do_not_propose": ["No mass DMs", "No competitor mentions"],
        }
    )
    out = _format_strategist_template(ctx)
    assert "Do not propose" in out
    assert "mass DMs" in out
    assert "competitor mentions" in out


def test_format_template_voice_block():
    ctx = _StubCtx(
        strategist_template={
            "voice": {
                "style": "concise, founder-direct",
                "examples": ["Draft 3 X posts", "Reply to DM #1234"],
            }
        }
    )
    out = _format_strategist_template(ctx)
    assert "Voice" in out
    assert "founder-direct" in out
    assert "Draft 3 X posts" in out


def test_format_template_rubric_block():
    ctx = _StubCtx(
        strategist_template={
            "evaluation_rubric": {
                "weights": {"goal_impact": 0.5, "cost": 0.5},
                "passing_score": 0.7,
            }
        }
    )
    out = _format_strategist_template(ctx)
    assert "rubric" in out.lower()
    assert "goal_impact" in out
    assert "weight=0.5" in out
    assert "0.7" in out


def test_format_template_multiple_blocks_in_order():
    ctx = _StubCtx(
        strategist_template={
            "business_model": {"model_type": "saas"},
            "proposal_shape": {"max_tasks_per_cycle": 1},
            "do_not_propose": ["never"],
        }
    )
    out = _format_strategist_template(ctx)
    bm_idx = out.index("Business model")
    ps_idx = out.index("Proposal shape")
    dnp_idx = out.index("Do not propose")
    # Order: business → proposal shape → do-not
    assert bm_idx < ps_idx < dnp_idx


# ── Installer merges recipe.strategist into operating_model.strategist ─


def test_installer_merge_nested_cadence_splits_for_legacy():
    """Verify that migrate_payload + the installer's split logic produces
    operating_model.strategist with a STRING cadence (legacy) + a peer
    trigger_conditions field, not a nested cadence object."""
    # Construct a v1.1-shaped payload directly for clarity
    payload = {
        "manifest": {"blueprint_version": "1.1", "title": "T"},
        "contract": {"variables": [], "channels": [], "sessions": [], "requires": {"tools": []}},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {},
            "strategist": {
                "cadence": {
                    "schedule": "daily",
                    "trigger_conditions": {"skip_if_any": ["X >= 1"]},
                },
                "business_model": {"model_type": "saas"},
                "do_not_propose": ["never"],
            },
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {"governance": {}, "post_install_checks": []},
    }

    # Simulate what the installer does to operating_model:
    om = dict(payload["recipe"]["operating_model"])
    strategist_cfg = payload["recipe"]["strategist"]
    merged: dict[str, Any] = dict(om.get("strategist") or {})
    cadence_obj = strategist_cfg.get("cadence")
    if isinstance(cadence_obj, dict):
        if cadence_obj.get("schedule"):
            merged["cadence"] = cadence_obj["schedule"]
        tc = cadence_obj.get("trigger_conditions")
        if tc is not None:
            merged["trigger_conditions"] = tc
    for key in ("business_model", "do_not_propose"):
        if key in strategist_cfg:
            merged[key] = strategist_cfg[key]

    # Legacy readers see a string here:
    assert merged["cadence"] == "daily"
    # New readers see the trigger_conditions next to it:
    assert merged["trigger_conditions"]["skip_if_any"] == ["X >= 1"]
    # And the other fields land verbatim:
    assert merged["business_model"]["model_type"] == "saas"
    assert merged["do_not_propose"] == ["never"]


def test_v10_payload_carries_no_strategist_template():
    """v1.0 payloads have no recipe.strategist section; migration must
    leave recipe.strategist == None so the installer's check skips."""
    p10 = {
        "blueprint_version": "1.0",
        "title": "T",
        "workspace": {"kind": "k", "operating_model": {}},
    }
    p11 = migrate_payload(p10)
    assert p11["recipe"]["strategist"] is None
