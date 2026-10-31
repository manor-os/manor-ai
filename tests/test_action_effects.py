"""The effect declaration table is the only source of approval facts."""
from __future__ import annotations

import logging

import pytest

from packages.core.governance.action_effects import (
    ACTION_CATALOG,
    ACTION_EFFECTS,
    ActionEffect,
    EffectGate,
    STRICT_EFFECT_CONTRACT_ENV,
    UndeclaredEffectError,
    declared_action_effect,
    mcp_action_effect,
)
from packages.core.governance.approval_facts import UNDECLARED_EFFECT_EVENT, action_facts
from packages.core.governance.approval_policy import ActionFacts, ApprovalScope, evaluate_policy
from packages.core.governance.approval_policy_service import build_policy_chain


def _chain(mode: str, rules: list[dict] | None = None):
    scope = ApprovalScope(kind="account", id="acct", entity_id="ent")
    return build_policy_chain(scope=scope, root={"revision": 1, "policy": {"mode": mode, "rules": rules or []}})


def test_declarations_never_declare_unknown_impact():
    for key, effect in ACTION_EFFECTS.items():
        assert effect.impact != "unknown", key
        if effect.impact == "read":
            assert effect.category == "read", key
    with pytest.raises(ValueError):
        ActionEffect("unknown", "workspace")
    with pytest.raises(ValueError):
        ActionEffect("read", "files")


@pytest.mark.parametrize(("key", "impact", "category", "gate"), [
    ("workspace.task.create", "reversible", "workspace", EffectGate.POLICY),
    ("workspace.service.delegate", "reversible", "workspace", EffectGate.LEAF_ONLY),
    ("human.input.request", "reversible", "workspace", EffectGate.LEAF_ONLY),
    ("workspace.agent.capabilities.modify", "reversible", "workspace", EffectGate.POLICY),
    ("cli.exec", "reversible", "unknown", EffectGate.POLICY),
    ("browser.interact", "external", "unknown", EffectGate.POLICY),
    ("workspace.order.create", "reversible", "workspace", EffectGate.POLICY),
    ("sandbox.exec", "reversible", "workspace", EffectGate.POLICY),
    ("sandbox.exec.write_unknown", "reversible", "workspace", EffectGate.POLICY),
    ("sandbox.respond", "reversible", "workspace", EffectGate.POLICY),
    ("sandbox.create", "reversible", "unknown", EffectGate.POLICY),
    ("sandbox.cancel", "reversible", "workspace", EffectGate.POLICY),
    ("runtime.control.complete_task", "reversible", "unknown", EffectGate.LEAF_ONLY),
    ("skill.invoke", "reversible", "unknown", EffectGate.LEAF_ONLY),
    ("tool.read_file.read", "read", "read", EffectGate.POLICY),
    ("workspace.proposal.task", "external", "automation", EffectGate.POLICY),
])
def test_declared_effects(key, impact, category, gate):
    effect = declared_action_effect(key)
    assert effect is not None
    assert (effect.impact, effect.category, effect.gate) == (impact, category, gate)


@pytest.mark.parametrize("key", [
    "", "runtime.control.", "tool..read", "tool.read_file", "workspace.agent.action",
    "github.create_issue", "provider.custom_action", "workspace.proposal.",
])
def test_undeclared_keys_are_not_guessed(key):
    assert declared_action_effect(key) is None


def test_every_strategist_proposal_key_is_declared():
    from packages.core.proposals.constants import HUMAN_REQUEST_ACTION_KEY, STRATEGIST_ACTION_KEYS

    for key in (*STRATEGIST_ACTION_KEYS, HUMAN_REQUEST_ACTION_KEY):
        assert declared_action_effect(key) is not None, key


def test_mcp_effects_come_from_the_shared_enum_and_server_family():
    assert mcp_action_effect("github", "write") == ActionEffect("external", "unknown")
    assert mcp_action_effect("gmail", "write") == ActionEffect("external", "communication")
    assert mcp_action_effect("linkedin", "write") == ActionEffect("external", "publishing")
    assert mcp_action_effect("stripe", "write") == ActionEffect("external", "purchases")
    assert mcp_action_effect("github", "destructive") == ActionEffect("destructive", "deletion")
    assert mcp_action_effect("github", "read") == ActionEffect("read", "read")


@pytest.mark.parametrize(("server", "category"), [
    ("shopify", "publishing"), ("woocommerce", "publishing"), ("tiktok_shop", "publishing"),
    ("amazon", "publishing"), ("quickbooks", "purchases"), ("google_calendar", "communication"),
    ("ms_calendar", "communication"), ("notion", "unknown"),
])
def test_mcp_server_family_categories(server, category):
    assert mcp_action_effect(server, "write") == ActionEffect("external", category)


@pytest.mark.parametrize(("tool", "effect", "category"), [
    # A report is a read, not a financial write.
    ("mcp__quickbooks__run_report", "read_only", "read"),
    # Customer and order bookkeeping is a record, neither a purchase nor a listing.
    ("mcp__stripe__create_customer", "action", "unknown"),
    ("mcp__square__create_customer", "action", "unknown"),
    ("mcp__square__update_customer", "action", "unknown"),
    ("mcp__shopify__create_customer", "action", "unknown"),
    ("mcp__quickbooks__create_customer", "action", "unknown"),
    ("mcp__shopify__add_order_tags", "action", "unknown"),
    ("mcp__woocommerce__update_order_status", "action", "unknown"),
    ("mcp__paypal__create_shipment_tracking", "action", "unknown"),
    # Catalog and price listings are sale listings (publishing), not purchases.
    ("mcp__stripe__create_product", "action", "publishing"),
    ("mcp__stripe__create_price", "action", "publishing"),
    ("mcp__square__create_catalog_item", "action", "publishing"),
    # Organizing one's own mailbox sends nothing.
    ("mcp__gmail__add_label", "action", "unknown"),
    ("mcp__gmail__mark_read", "action", "unknown"),
    ("mcp__outlook__move_message", "action", "unknown"),
    ("mcp__gmail__trash_message", "action", "deletion"),
    # Money movement stays purchases; sending stays communication.
    ("mcp__stripe__create_refund", "action", "purchases"),
    ("mcp__paypal__pay_order", "action", "purchases"),
    ("mcp__google_calendar__create_event", "action", "communication"),
])
def test_per_action_mcp_categories(tool, effect, category):
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool,
        runtime_policy_action_for_classification,
    )

    classification = classify_runtime_tool(tool, {})
    assert classification.effect.value == effect
    action, _impact = runtime_policy_action_for_classification(classification)
    assert action.category == category


def test_action_facts_reads_the_declaration_and_ignores_verbs(monkeypatch):
    monkeypatch.delenv(STRICT_EFFECT_CONTRACT_ENV, raising=False)
    facts = action_facts(action_key="workspace.task.create", operation="delete", resource_kind="file")
    assert (facts.impact, facts.category, facts.gate) == ("reversible", "workspace", "policy")
    assert action_facts(action_key="workspace.service.delegate").gate == "leaf_only"
    explicit = action_facts(action_key="github.create_issue", impact="external", category="unknown")
    assert (explicit.impact, explicit.category) == ("external", "unknown")


def test_read_impact_is_always_the_read_category(monkeypatch):
    monkeypatch.delenv(STRICT_EFFECT_CONTRACT_ENV, raising=False)
    facts = action_facts(action_key="workspace.automation.list", impact="read", category="automation")
    assert (facts.impact, facts.category) == ("read", "read")


def test_undeclared_effect_is_unknown_and_reported(monkeypatch, caplog):
    monkeypatch.delenv(STRICT_EFFECT_CONTRACT_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="packages.core.governance.approval_facts"):
        facts = action_facts(action_key="provider.custom_action", operation="delete")
    assert (facts.impact, facts.category) == ("unknown", "unknown")
    record = next(item for item in caplog.records if item.getMessage() == UNDECLARED_EFFECT_EVENT)
    assert record.action_key == "provider.custom_action"
    assert record.caller.endswith(":test_undeclared_effect_is_unknown_and_reported")


def test_strict_mode_raises_for_undeclared_effects(monkeypatch, caplog):
    monkeypatch.setenv(STRICT_EFFECT_CONTRACT_ENV, "1")
    with pytest.raises(UndeclaredEffectError, match="provider.custom_action"):
        action_facts(action_key="provider.custom_action")
    # A subject that names no action has nothing to declare: it stays unknown
    # (ask) and is reported, even in strict mode.
    with caplog.at_level(logging.WARNING, logger="packages.core.governance.approval_facts"):
        facts = action_facts(action_key=None)
    assert (facts.action_key, facts.impact) == ("unclassified.action", "unknown")
    assert any(item.getMessage() == UNDECLARED_EFFECT_EVENT for item in caplog.records)
    # A factory-supplied effect is a declaration, not a guess.
    assert action_facts(action_key="provider.custom_action", impact="external").impact == "external"


def test_leaf_only_ignores_mode_and_category_rules_but_honors_explicit_rules():
    facts = ActionFacts(action_key="workspace.service.delegate", category="workspace",
                        impact="reversible", gate="leaf_only")
    category_ask = {"id": "c", "effect": "ask", "selector": {"kind": "category", "category": "workspace"}}
    assert evaluate_policy(_chain("confirm_changes", [category_ask]), facts).outcome == "allow"
    assert evaluate_policy(_chain("confirm_changes"), facts).reason_code == "approval.leaf_only"
    for effect in ("ask", "deny"):
        explicit = {"id": "a", "effect": effect, "selector": {"kind": "action", "action_key": facts.action_key}}
        assert evaluate_policy(_chain("automatic", [explicit]), facts).outcome == effect
    policy_facts = facts.model_copy(update={"gate": "policy"})
    assert evaluate_policy(_chain("confirm_changes"), policy_facts).outcome == "ask"


@pytest.mark.parametrize(("tool", "args"), [
    ("sandbox", {"action": "exec", "params": {"command": "curl https://example.com"}}),
    ("sandbox_exec", {"command": "python3 run.py"}),
    ("sandbox", {"action": "respond", "params": {"input": "y"}}),
    ("sandbox", {"action": "cancel"}),
])
def test_sandbox_work_is_declared_reversible_and_runs_by_default(tool, args):
    # Product decision (2026-10-06, #1071): sandbox work runs by default;
    # confirm_changes still asks. Creating a sandbox is a control call
    # (tests/test_sandbox_approval_defaults.py).
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from tests.approval_policy_test_utils import evaluate_runtime_action

    action = classify_runtime_tool(tool, args).action
    assert evaluate_runtime_action(action, mode="risk_based").outcome == "allow"
    assert evaluate_runtime_action(action, mode="automatic").outcome == "allow"
    assert evaluate_runtime_action(action, mode="confirm_changes").outcome == "ask"


@pytest.mark.parametrize(("mode", "outcome"), [
    # #1027 / aa873d444: an unnamed effect follows the user's mode; only
    # "Confirm by risk" keeps asking. leaf_only does not turn it into allow.
    ("automatic", "allow"), ("risk_based", "ask"), ("confirm_changes", "ask"),
])
def test_leaf_only_with_unknown_impact_falls_back_to_the_baseline(mode, outcome):
    facts = ActionFacts(action_key="x.unknown", impact="unknown", gate="leaf_only")
    result = evaluate_policy(_chain(mode), facts)
    assert (result.outcome, result.reason_code) == (outcome, "approval.unknown_impact")


@pytest.mark.parametrize(("facts", "rules", "outcome", "reason"), [
    # One policy on every surface (#1027): explicit rules apply to leaf_only.
    (ActionFacts(action_key="workspace.service.delegate", impact="reversible", category="workspace", gate="leaf_only"),
     [{"id": "d", "effect": "deny", "selector": {"kind": "action", "action_key": "workspace.service.delegate"}}],
     "deny", "approval.rule"),
    (ActionFacts(action_key="workspace.service.delegate", impact="reversible", category="workspace", gate="leaf_only"),
     [{"id": "a", "effect": "ask", "selector": {"kind": "action", "action_key": "workspace.service.delegate"}}],
     "ask", "approval.rule"),
    (ActionFacts(action_key="workspace.service.delegate", impact="reversible", category="workspace", gate="leaf_only"),
     [{"id": "c", "effect": "ask", "selector": {"kind": "category", "category": "workspace"}}],
     "allow", "approval.leaf_only"),
    # Declared facts follow the mode like any other.
    (ActionFacts(action_key="workspace.task.create", impact="reversible", category="workspace"), [],
     "ask", "approval.mode"),
])
def test_leaf_only_under_one_policy_for_every_surface(facts, rules, outcome, reason):
    result = evaluate_policy(_chain("confirm_changes", rules), facts)
    assert (result.outcome, result.reason_code) == (outcome, reason)


def test_action_catalog_is_derived_from_declarations():
    for row in ACTION_CATALOG:
        effect = declared_action_effect(row["action_key"])
        assert effect is not None
        assert (row["impact"], row["category"]) == (effect.impact, effect.category)
        assert row["label"]


def test_a_gate_resolved_key_has_unknown_static_facts_and_is_not_undeclared(monkeypatch, caplog):
    """Its effect exists only at the gate; a static caller gets the honest unknown, no error."""
    from packages.core.governance.action_effects import GATE_RESOLVED_ACTION_KEYS

    monkeypatch.setenv(STRICT_EFFECT_CONTRACT_ENV, "1")
    for key in GATE_RESOLVED_ACTION_KEYS:
        with caplog.at_level(logging.WARNING):
            facts = action_facts(action_key=key)
        assert (facts.impact, facts.category, facts.gate) == ("unknown", "unknown", "policy")
        assert UNDECLARED_EFFECT_EVENT not in caplog.text
    with pytest.raises(UndeclaredEffectError):
        action_facts(action_key="really.undeclared.action")


def test_action_catalog_stays_importable_from_approval_policy():
    """Its old home keeps re-exporting the one derived catalog (callers on dev import it there)."""
    from packages.core.governance import approval_policy
    from packages.core.governance.approval_policy import ACTION_CATALOG as reexported

    assert reexported is ACTION_CATALOG
    assert approval_policy.ACTION_CATALOG is ACTION_CATALOG
    assert "ACTION_CATALOG" in dir(approval_policy)
