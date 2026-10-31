"""plan_dag step subjects are judged as the action their factory classifies (I1, I3).

A step naming a raw MCP action is the provider's MCP action, never a native
Manor tool of the same name; a native step is its Manor tool with the step's
own arguments. The facts, the hard constraints and task rules all see the
classified action: its canonical action key, its classifier risk (a
planner-declared risk cannot lower it) and its resource. A plan step is never
leaf_only; anything unclassifiable stays undeclared (asks).
"""
from __future__ import annotations

import pytest

from packages.core.ai.runtime.approval_classifier import runtime_step_policy_action
from packages.core.governance.approval_facts import EXEMPT
from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain
from packages.core.governance.approvals import (
    ApprovalSubject,
    canonical_step_subject,
    subject_policy_facts,
)


def _chain(mode, rules=()):
    scope = ApprovalScope(kind="account", id="acct", entity_id="ent")
    return build_policy_chain(scope=scope, root={"revision": 1, "policy": {"mode": mode, "rules": list(rules)}})


def _action_rule(key, effect):
    return {"id": f"{effect}-{key}", "effect": effect, "selector": {"kind": "action", "action_key": key}}


@pytest.mark.parametrize(("provider", "action_key", "params", "key", "impact", "category"), [
    # Third-party generators are external MCP writes, not workspace files.
    ("jimeng", "generate_image", {}, "jimeng.generate_image", "external", "unknown"),
    ("replicate", "generate_video", {}, "replicate.generate_video", "external", "unknown"),
    # A third-party create_task is not a Manor Workspace Task.
    ("asana", "create_task", {}, "asana.create_task", "external", "unknown"),
    ("tiktok", "post_video", {}, "social_post.publish", "external", "publishing"),
    ("paypal", "send_invoice", {}, "paypal.send_invoice", "external", "purchases"),
    ("gmail", "send_email", {}, "email.send", "external", "communication"),
    # Native tools: judged with the step's own arguments.
    ("platform", "generate_file", {"kind": "document", "name": "brief.md"}, "workspace.file.create", "reversible", "files"),
    ("platform", "bash", {"command": "rm -rf drafts"}, "workspace.file.delete", "destructive", "deletion"),
    ("platform", "bash", {"command": "which python3"}, "cli.exec", "reversible", "unknown"),
    (None, "read_file", {"path": "brief.md"}, "tool.read_file.read", "read", "read"),
])
def test_plan_steps_resolve_to_their_classified_action(provider, action_key, params, key, impact, category):
    action = runtime_step_policy_action(action_key, provider=provider, arguments=params)
    assert (action.action_key, action.impact, action.category, action.gate) == (key, impact, category, "policy")


@pytest.mark.parametrize(("provider", "action_key"), [
    ("platform", "call_tool"),
    ("platform", "invoke_skill"),
    ("platform", "complete_task"),
    (None, "not_a_tool"),
    ("platform", "workspace_operation"),  # argument-dependent, no action given
])
def test_plan_steps_are_never_leaf_only_and_unknown_stays_undeclared(provider, action_key):
    assert runtime_step_policy_action(action_key, provider=provider, arguments={}) is None


def test_a_declared_action_key_keeps_its_declaration():
    assert runtime_step_policy_action("social_post.publish", provider="twitter_x", arguments={}) is None


def test_subject_facts_use_the_classified_action():
    subject = ApprovalSubject(entity_id="e", kind="action", action_key="generate_image",
                              provider="jimeng", arguments={}, risk_level="low")
    facts = subject_policy_facts(subject)
    assert (facts.action_key, facts.impact, facts.gate) == ("jimeng.generate_image", "external", "policy")
    assert facts.risk_level == "medium"  # the classifier's, not the planner's
    native = ApprovalSubject(entity_id="e", kind="action", action_key="generate_image",
                             provider="platform", arguments={"prompt": "x"}, risk_level="medium")
    assert subject_policy_facts(native).category == "files"
    assert subject_policy_facts(ApprovalSubject(entity_id="e", kind="llm")) is EXEMPT


def test_a_canonical_rule_catches_a_raw_plan_step_in_automatic_mode():
    subject = ApprovalSubject(entity_id="e", kind="action", action_key="post_video",
                              provider="tiktok", arguments={}, risk_level="low")
    facts = subject_policy_facts(subject)
    assert facts.action_key == "social_post.publish"
    rules = [_action_rule("social_post.publish", "ask")]
    assert evaluate_policy(_chain("automatic", rules), facts).outcome == "ask"
    assert evaluate_policy(_chain("automatic", [_action_rule("cli.exec", "deny")]), subject_policy_facts(
        ApprovalSubject(entity_id="e", kind="action", action_key="bash", provider="platform",
                        arguments={"command": "which python3"}),
    )).outcome == "deny"


@pytest.mark.parametrize("effect", ["allow", "ask", "deny"])
def test_a_rule_naming_the_raw_step_key_still_applies(effect):
    """Cards for plan steps were minted under the raw step key, so users hold
    rules on it ("always approve", legacy deny patterns); they keep working."""
    subject = ApprovalSubject(entity_id="e", kind="action", action_key="publish_tweet",
                              provider="twitter_x", arguments={"text": "hi"}, risk_level="low")
    facts = subject_policy_facts(subject)
    assert facts.action_key == "social_post.publish" and facts.aliases == ["publish_tweet"]
    result = evaluate_policy(_chain("confirm_changes", [_action_rule("publish_tweet", effect)]), facts)
    assert result.outcome == effect
    # A canonical deny still wins over a raw-key allow.
    rules = [_action_rule("publish_tweet", "allow"), _action_rule("social_post.publish", "deny")]
    assert evaluate_policy(_chain("automatic", rules), facts).outcome == "deny"


@pytest.mark.parametrize("effect", ["ask", "deny"])
def test_a_resource_rule_on_the_raw_step_resource_keeps_matching(effect):
    """The step's own resource stays a target next to the classifier's, so a
    legacy ResourceSelector on the raw key and resource still applies."""
    subject = ApprovalSubject(entity_id="e", kind="action", action_key="workspace_operation",
                              provider="platform", arguments={"action": "apply_draft", "draft_id": "d1"},
                              resource_kind="workspace", resource_id="ws-1", risk_level="low")
    facts = subject_policy_facts(subject)
    targets = {(target.resource_kind, target.resource_id) for target in facts.targets}
    assert {("workspace_operation", "d1"), ("workspace", "ws-1")} <= targets
    rule = {"id": "raw", "effect": effect, "selector": {
        "kind": "resource", "action_key": "workspace_operation", "resource_kind": "workspace",
        "resource_ids": ["ws-1"], "account_id": None}}
    assert evaluate_policy(_chain("automatic", [rule]), facts).outcome == effect
    other = {**rule, "selector": {**rule["selector"], "resource_ids": ["ws-2"]}}
    assert evaluate_policy(_chain("automatic", [other]), facts).outcome == "allow"


def test_a_planner_low_risk_cannot_lower_the_classified_risk():
    subject = ApprovalSubject(entity_id="e", kind="action", action_key="workspace_operation",
                              provider="platform", arguments={"action": "apply_draft", "draft_id": "d1"},
                              risk_level="low")
    canonical = canonical_step_subject(subject)
    assert (canonical.action_key, canonical.risk_level) == ("workspace.operation.apply", "high")
    assert canonical.resource_id == "d1"
    facts = subject_policy_facts(subject)
    assert facts.risk_level == "high"
    assert evaluate_policy(_chain("risk_based"), facts).outcome == "ask"


def test_canonicalisation_is_idempotent_and_leaves_runtime_subjects_alone():
    runtime_subject = ApprovalSubject(entity_id="e", action_key="workspace.task.create", impact="reversible",
                                      category="workspace", risk_level="medium")
    assert canonical_step_subject(runtime_subject) is runtime_subject
    step = ApprovalSubject(entity_id="e", kind="action", action_key="post_video", provider="tiktok", arguments={})
    once = canonical_step_subject(step)
    assert canonical_step_subject(once) is once


def test_blueprint_counterfactuals_use_the_same_step_facts():
    from types import SimpleNamespace

    from packages.core.blueprints import report

    step = SimpleNamespace(kind="action", action_key="generate_image", provider="jimeng",
                           params={}, risk_level="low", capability_id=None)
    facts = report._step_policy_facts(step, entity_id="e", workspace_id="ws")
    assert (facts.action_key, facts.impact, facts.gate) == ("jimeng.generate_image", "external", "policy")
    llm = SimpleNamespace(kind="llm", action_key=None, provider=None, params={},
                          risk_level="low", capability_id=None)
    assert report._step_policy_facts(llm, entity_id="e", workspace_id="ws") is EXEMPT
