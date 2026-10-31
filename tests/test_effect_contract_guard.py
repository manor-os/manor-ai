"""Effect contract guard: every tool call a host can make resolves to a declared effect.

Repository-wide and schema-driven (no hand-written tool list):

* every ToolPool tool plus every host-injected tool (host_tools.py), expanded
  over its schema's discriminating enums (``action``, ``kind``), the Manor
  action catalog and the nested Workspace operation actions;
* every call classifies to a known effect; an ACTION carries a declared,
  known impact; a READ_ONLY call evaluates as read/read; CONTROL,
  ORCHESTRATOR and discovery reads never reach the consent evaluator;
* only the subject factories compute facts or pass effect keywords (AST);
* the guard itself is mutation-tested: deleting a declaration or turning a
  control tool into an undeclared action is reported.
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool,
    runtime_effect_gate,
    runtime_policy_action_for_classification,
)
from packages.core.ai.runtime.host_tools import (
    ALL_HOST_INJECTED_TOOLS,
    HOST_INJECTED_TOOLS,
    host_injected_tool_schema,
)
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.governance import action_effects
from packages.core.governance.action_effects import EffectGate, declared_action_effect
from packages.core.governance.approval_facts import action_facts

REPO = Path(__file__).resolve().parents[1]

# The plan_dag coordinator's delegation tool has no effect classification and
# is blocked fail-closed by the runtime gate. plan_dag is being deleted; this
# is the only tolerated UNKNOWN and it must stay UNKNOWN (never silently allowed).
PLAN_DAG_UNCLASSIFIED = frozenset({"delegate_to_service"})


def _parameters(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    params = function.get("parameters") or function.get("input_schema") or {}
    return params.get("properties") or {}


def _variants(name: str, schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Schema-driven argument variants that change the classified effect."""
    from packages.core.ai.runtime.workspace_operation_actions import WORKSPACE_OPERATION_ACTIONS
    from packages.core.ai.tools.manor_tool import get_manor_action_names

    props = _parameters(schema)
    actions = list((props.get("action") or {}).get("enum") or [])
    if name == "manor":
        actions = sorted(get_manor_action_names())
    if not actions:
        yield {}
    for action in actions:
        if name == "manor" and action == "workspace":
            continue  # canonicalized to workspace_agent, enumerated there
        if action == "operation" and name == "workspace_agent":
            for nested in WORKSPACE_OPERATION_ACTIONS:
                yield {"action": "operation", "params": {"action": nested}}
            continue
        yield {"action": action}
    for kind in (props.get("kind") or {}).get("enum") or []:
        yield {"kind": kind}


def governed_calls() -> list[tuple[str, dict[str, Any]]]:
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    schemas = dict(pool.registered_tool_schemas(include_undiscoverable=True))
    for name in ALL_HOST_INJECTED_TOOLS:
        schema = host_injected_tool_schema(name)
        assert schema is not None, f"host tool {name} has no schema"
        schemas.setdefault(name, schema)
    return [(name, args) for name, schema in sorted(schemas.items()) for args in _variants(name, schema)]


def _chrome_refused_before_consent(name: str, args: dict[str, Any]) -> bool:
    """A Chrome call without target evidence or a supported contract.

    The runtime gate refuses it (``read_page_required`` / unsupported) before
    any consent, so it stays UNKNOWN: missing evidence is never an approval.
    """
    from packages.core.ai.runtime.approval_classifier import classify_chrome_call

    if not name.startswith("mcp__chrome__"):
        return False
    return classify_chrome_call(name, dict(args)).kind in {"unsupported", "observation_required", "handoff"}


def effect_contract_violations(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    classify: Callable[..., Any] = classify_runtime_tool,
) -> list[str]:
    """Every way a call escapes the declared effect contract."""
    problems: list[str] = []
    for name, args in calls:
        label = f"{name}({json.dumps(args, sort_keys=True)})"
        classification = classify(name, dict(args))
        effect = classification.effect
        if effect is RuntimeToolEffect.UNKNOWN:
            if name not in PLAN_DAG_UNCLASSIFIED and not _chrome_refused_before_consent(name, args):
                problems.append(f"{label}: UNKNOWN effect")
            continue
        if classification.authorization is None:
            problems.append(f"{label}: no hard-authorization metadata")
            continue
        action, _impact = runtime_policy_action_for_classification(classification)
        gate = runtime_effect_gate(classification)
        if effect in {RuntimeToolEffect.CONTROL, RuntimeToolEffect.ORCHESTRATOR}:
            if gate is not EffectGate.LEAF_ONLY:
                problems.append(f"{label}: {effect.value} must be leaf_only")
            continue
        if action.action_key in action_effects.GATE_RESOLVED_ACTION_KEYS:
            # Resolved by the gate from durable state; never declared statically.
            if declared_action_effect(action.action_key) is not None or action.impact is not None:
                problems.append(f"{label}: gate-resolved {action.action_key} must not carry a static effect")
            continue
        try:
            facts = action_facts(
                action_key=action.action_key, capability_id=action.capability_id,
                resource_kind=action.resource_kind, operation=action.operation,
                risk_level=action.risk_level, **action.policy_effect(),
            )
        except Exception as exc:  # strict contract: an undeclared key raises
            problems.append(f"{label}: {action.action_key} {type(exc).__name__}")
            continue
        if effect is RuntimeToolEffect.READ_ONLY:
            # A read is read/read, except a basic Chrome read declared in the
            # browser category (read/browser).
            declared_read = declared_action_effect(action.action_key)
            expected = ("read", declared_read.category if declared_read and declared_read.impact == "read" else "read")
            if (facts.impact, facts.category) != expected:
                problems.append(f"{label}: READ_ONLY facts {facts.impact}/{facts.category}")
            continue
        if (
            name.startswith("mcp__chrome__")
            and action.action_key.endswith(".confirm")
            and action.action_key.split(".")[1] not in action_effects.PROVIDER_CATEGORY_EFFECTS
        ):
            # Chrome's own confirmation category with no Manor fact: an unnamed
            # effect by design (it follows the user's mode; see 095618379).
            if action.impact != "unknown":
                problems.append(f"{label}: unmapped Chrome category must stay unknown")
            continue
        declared = declared_action_effect(action.action_key)
        factory_declared = name.startswith("mcp__") and action.impact not in {None, "unknown"}
        if declared is None and not factory_declared:
            problems.append(f"{label}: ACTION {action.action_key} has no declared effect")
        if facts.impact == "unknown":
            problems.append(f"{label}: ACTION {action.action_key} impact unknown")
        # The Chrome factory writes the read and file-selection effects of
        # its one basic-browsing key; the category stays the declared one.
        chrome_basic = name.startswith("mcp__chrome__") and action.action_key == "chrome.browser.interact"
        if declared is not None and (facts.impact, facts.category, facts.gate) != (
            declared.impact, declared.category, declared.gate.value,
        ) and not (chrome_basic and (facts.category, facts.gate) == (declared.category, declared.gate.value)):
            problems.append(f"{label}: facts differ from the declaration of {action.action_key}")
    return problems


@pytest.fixture(scope="module")
def calls() -> list[tuple[str, dict[str, Any]]]:
    logging.disable(logging.WARNING)
    try:
        return governed_calls()
    finally:
        logging.disable(logging.NOTSET)


def test_enumeration_covers_the_registry_and_every_host(calls):
    names = {name for name, _ in calls}
    assert len(names) > 500
    assert ALL_HOST_INJECTED_TOOLS <= names
    assert {"manor", "workspace_agent", "sandbox", "generate_file", "call_tool"} <= names
    assert ("workspace_agent", {"action": "operation", "params": {"action": "apply_draft"}}) in calls


def test_every_governed_call_resolves_to_a_declared_effect(calls):
    assert effect_contract_violations(calls) == []


def test_plan_dag_unclassified_tool_stays_fail_closed():
    for name in PLAN_DAG_UNCLASSIFIED:
        assert classify_runtime_tool(name, {}).effect is RuntimeToolEffect.UNKNOWN


@pytest.mark.parametrize(("name", "args", "effect"), [
    ("call_tool", {"rejected_reason": "not loaded", "name": "generate_file"}, RuntimeToolEffect.CONTROL),
    ("call_tool", {}, RuntimeToolEffect.CONTROL),
    ("wait_until", {}, RuntimeToolEffect.CONTROL),
    ("update_task_plan", {}, RuntimeToolEffect.CONTROL),
    ("complete_task", {}, RuntimeToolEffect.CONTROL),
    ("mcp__discord__get_connection_info", {}, RuntimeToolEffect.READ_ONLY),
    ("mcp__discord__list_channels", {}, RuntimeToolEffect.READ_ONLY),
])
def test_named_classification_fixes(name, args, effect):
    assert classify_runtime_tool(name, args).effect is effect


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "action_key", "bound", "allowed"), [
    ("call_tool", "runtime.control.call_tool", True, True),
    ("call_tool", "runtime.control.call_tool", False, False),
    ("call_tool", "runtime.control.submit_result", True, False),
    ("submit_result", "runtime.control.call_tool", True, False),
])
async def test_runtime_discovery_accepts_control_access_for_a_rejected_call_tool(tool, action_key, bound, allowed):
    """call_tool moved from READ to CONTROL access; an external principal bound
    to discovery still receives the rejection echo (and nothing else)."""
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationAccess,
        RuntimeAuthorizationRequest,
    )
    from packages.core.services.runtime_authorization.mappers import RegistryEntityPermissionMapper
    from packages.core.services.runtime_authorization.strategies import ExternalRuntimeAuthorizationStrategy

    classification = classify_runtime_tool("call_tool", {"rejected_reason": "not loaded"})
    assert classification.authorization.access is RuntimeAuthorizationAccess.CONTROL
    assert classification.authorization.capability_id == "runtime.discovery"
    request = RuntimeAuthorizationRequest.create(
        entity_id="e", user_id=None, workspace_id="ws", conversation_id="c",
        action_key=action_key, capability_id="runtime.discovery",
        access=RuntimeAuthorizationAccess.CONTROL, tool_name=tool,
        bound_tool_names=[tool] if bound else [], runtime_surface="public_customer_chat",
    )
    result = await ExternalRuntimeAuthorizationStrategy(RegistryEntityPermissionMapper()).authorize(None, request)
    assert result.allowed is allowed


@pytest.mark.parametrize("name", sorted(action_effects.DISCOVERY_LEAF_TOOL_NAMES))
def test_discovery_reads_are_leaf_only(name):
    classification = classify_runtime_tool(name, {})
    assert classification.effect is RuntimeToolEffect.READ_ONLY
    assert runtime_effect_gate(classification) is EffectGate.LEAF_ONLY




def test_delegation_is_leaf_only_through_both_names():
    for name, args in [
        ("delegate_to_agent", {"agent": "a", "task": "t"}),
        ("workspace_agent", {"action": "delegate_service"}),
    ]:
        classification = classify_runtime_tool(name, args)
        assert classification.action.action_key == "workspace.service.delegate"
        assert runtime_effect_gate(classification) is EffectGate.LEAF_ONLY


def _observed_risk_levels(calls, classify=classify_runtime_tool) -> dict[str, set[str]]:
    risks: dict[str, set[str]] = {}
    for name, args in calls:
        classification = classify(name, dict(args))
        if classification.action is not None:
            risks.setdefault(classification.action.action_key, set()).add(classification.action.risk_level)
    return risks


def test_effect_declarations_never_change_risk_levels(calls):
    """Risk is not an effect declaration: every action key keeps exactly the
    risk levels its classifier tables had before the effect contract
    (snapshot taken from the base commit; outcomes stay the user's policy)."""
    from tests.action_risk_levels_snapshot import RISK_LEVELS

    snapshot = {key: set(levels) for key, levels in RISK_LEVELS.items()}
    observed = _observed_risk_levels(calls)
    assert observed, "no governed action was enumerated"
    changed = {key: (sorted(snapshot.get(key, ())), sorted(levels))
               for key, levels in observed.items() if snapshot.get(key) != levels}
    assert changed == {}
    for effect in action_effects.ACTION_EFFECTS.values():
        assert not hasattr(effect, "risk_level")


def test_risk_snapshot_guard_reports_a_changed_risk(calls):
    from dataclasses import replace

    def mutated(name, args):
        classification = classify_runtime_tool(name, args)
        if classification.action is not None and classification.action.action_key == "workspace.hitl.resolve":
            return replace(classification, action=replace(classification.action, risk_level="high"))
        return classification

    from tests.action_risk_levels_snapshot import RISK_LEVELS as snapshot

    observed = _observed_risk_levels(calls, mutated)
    assert observed["workspace.hitl.resolve"] != set(snapshot["workspace.hitl.resolve"])


@pytest.mark.parametrize(("kind", "action_key", "capability_id", "exempt"), [
    ("llm", None, None, True),
    ("subagent", None, None, True),
    ("subagent", None, "external.message", False),
    ("subagent", "social_post.publish", None, False),
    ("action", None, None, False),
    ("human", None, None, False),
])
def test_only_effectless_step_kinds_are_exempt(kind, action_key, capability_id, exempt):
    from packages.core.governance.approval_facts import EXEMPT
    from packages.core.governance.approvals import ApprovalSubject, subject_policy_facts

    subject = ApprovalSubject(entity_id="e", kind=kind, action_key=action_key, capability_id=capability_id)
    assert (subject_policy_facts(subject) is EXEMPT) is exempt


@pytest.mark.asyncio
async def test_exempt_facts_skip_consent_but_keep_hard_constraints(monkeypatch):
    from packages.core.governance import service as governance_service
    from packages.core.governance.approval_facts import EXEMPT
    from packages.core.governance.policy import WorkspacePolicy

    class _Db:
        async def get(self, _model, _id):
            from types import SimpleNamespace

            return SimpleNamespace(entity_id="e")

    async def capped(*_args, **_kwargs):
        return WorkspacePolicy(budget_caps_per_kind={"llm": 1})

    monkeypatch.setattr(governance_service, "get_policy", capped)
    decision = await governance_service.check_step_policy(
        _Db(), workspace_id="ws", kind="llm", action_key=None, risk_level="low",
        spent_credits_per_kind={"llm": 0}, facts=EXEMPT,
    )
    assert decision.allowed and decision.matched_rule == "approval.no_effect"
    blocked = await governance_service.check_step_policy(
        _Db(), workspace_id="ws", kind="llm", action_key=None, risk_level="low",
        spent_credits_per_kind={"llm": 5}, facts=EXEMPT,
    )
    assert not blocked.allowed and blocked.matched_rule == "budget_caps_per_kind.llm"


# ── mutation self-test ──────────────────────────────────────────────────


def test_guard_reports_a_deleted_declaration(monkeypatch, calls):
    monkeypatch.delitem(action_effects._DECLARED, "workspace.service.delegate")
    problems = effect_contract_violations(calls)
    assert any("delegate_to_agent" in item and "workspace.service.delegate" in item for item in problems)
    assert any("delegate_service" in item for item in problems)


def test_guard_reports_a_control_tool_turned_into_an_undeclared_action(calls):
    from packages.core.ai.runtime.approvals import RuntimeApprovalAction
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification

    def mutated(name, args):
        if name == "complete_task":
            return RuntimeToolClassification.action_call(
                RuntimeApprovalAction("action", "runtime.task.complete", "low", "complete", "task", "modify"),
            )
        return classify_runtime_tool(name, args)

    problems = effect_contract_violations(calls, classify=mutated)
    assert any(item.startswith("complete_task(") and "runtime.task.complete" in item for item in problems)


def test_guard_reports_an_unknown_tool(calls):
    def mutated(name, args):
        if name == "update_task_plan":
            return classify_runtime_tool("not_a_registered_tool", args)
        return classify_runtime_tool(name, args)

    problems = effect_contract_violations(calls, classify=mutated)
    assert any(item.startswith("update_task_plan(") and "UNKNOWN" in item for item in problems)


def test_host_handler_outside_the_registry_fails_loudly():
    from packages.core.ai.runtime.host_tools import UnregisteredHostToolError, runtime_check_host_tools

    for host, names in HOST_INJECTED_TOOLS.items():
        runtime_check_host_tools(host, names)
    runtime_check_host_tools(None, ALL_HOST_INJECTED_TOOLS)
    with pytest.raises(UnregisteredHostToolError, match="sneaky_tool"):
        runtime_check_host_tools("task_agent_loop", {"complete_task", "sneaky_tool"})
    with pytest.raises(UnregisteredHostToolError):
        runtime_check_host_tools("chat", {"update_task_plan"})


@pytest.mark.asyncio
async def test_harness_rejects_an_unlisted_dynamic_handler():
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.runtime.host_tools import UnregisteredHostToolError

    async def handler(_args):
        return "{}"

    with pytest.raises(UnregisteredHostToolError, match="sneaky_tool"):
        await runtime_execute_agentic_loop(
            system_prompt="x", user_message="y", tools=[], entity_id="e",
            runtime_envelope=None, agent_id=None,
            dynamic_tool_handlers={"sneaky_tool": handler},
        )


# ── leaf-only calls never reach the consent evaluator ───────────────────


@pytest.mark.asyncio
async def test_leaf_only_calls_never_reach_the_consent_evaluator(monkeypatch, calls):
    from packages.core import database
    from packages.core.ai.runtime import approval_service
    from packages.core.governance import approvals as governance_approvals
    from packages.core.governance import service as governance_service
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    async def evaluator_reached(*_args, **_kwargs):
        raise AssertionError("a leaf-only call reached the consent evaluator")

    async def allow(*_args, **_kwargs):
        return PermissionDecision.allow()

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", allow)
    monkeypatch.setattr(governance_service, "check_step_policy", evaluator_reached)
    monkeypatch.setattr(governance_approvals, "resolve_approval", evaluator_reached)
    monkeypatch.setattr(approval_service, "consume_runtime_approval", evaluator_reached)
    checked = 0
    for name, args in calls:
        classification = classify_runtime_tool(name, dict(args))
        if classification.effect is RuntimeToolEffect.UNKNOWN or classification.effect is RuntimeToolEffect.ACTION:
            continue
        if runtime_effect_gate(classification) is not EffectGate.LEAF_ONLY:
            continue
        if name == "search_tools":
            # The gate validates discovery input first (an empty search is a
            # tool input error, not an approval question).
            args = {**args, "query": "probe"}
        arguments = {**args, "approval_token": "legacy-card"}
        result = await approval_service.guard_runtime_tool_action(
            name=name, arguments=arguments, entity_id="e", user_id="u",
            workspace_id=None, conversation_id="c", classification=classification,
        )
        assert result is None, (name, args, result)
        assert "approval_token" not in arguments
        checked += 1
    assert checked >= 8


# ── AST guard: facts are computed only by the subject factories ─────────

# Modules allowed to call ``action_facts(`` and why. A new caller must build
# its subject through a factory (or be added here with a reason in review).
ACTION_FACTS_CALLERS = {
    "packages/core/governance/service.py": "check_step_policy / task rules: the shared evaluator",
    "packages/core/ai/runtime/tool_search.py": "discovery judges a tool by the runtime factory's action",
    "packages/core/services/workspace_operation_service.py": "capability apply recheck via the runtime factory",
    "packages/core/governance/approval_commands.py": "re-evaluates a stored card with its minted facts",
    "packages/core/governance/hard_block_provenance.py": "names the rule behind a denial",
    "packages/core/governance/approvals.py": "subject_policy_facts: the subject factory",
}
# Modules allowed to pass explicit effect keywords to the subject builders.
EFFECT_KEYWORD_CALLERS = {
    "packages/core/ai/runtime/approval_classifier.py": "the runtime subject factory",
    "packages/core/governance/approvals.py": "forwards the subject's declared effect",
    "packages/core/governance/service.py": "forwards the caller's declared effect",
    "packages/core/governance/approval_commands.py": "the effect stored when the card was minted",
    "packages/core/services/workspace_operation_service.py": "a raw tool-name rule selector (explicit rules only)",
    "packages/core/governance/_smoke.py": "evaluator smoke script over literal example facts",
}
# Modules allowed to splat (``**``) effect keywords into the subject builders;
# each splat is a factory's own declared effect.
EFFECT_SPLAT_CALLERS = {
    "packages/core/ai/runtime/approval_classifier.py": "MCP factory effect fields",
    "packages/core/ai/runtime/approval_service.py": "the runtime gate: action.policy_effect() / provider_policy_effect()",
    "packages/core/governance/approvals.py": "subject_policy_facts: declared or step-factory effect",
    "packages/core/ai/runtime/tool_search.py": "discovery: the runtime factory's action.policy_effect()",
    "packages/core/services/workspace_operation_service.py": "capability recheck: the runtime factory's action.policy_effect()",
    "packages/core/services/task_capability_recovery.py": "recovery: the runtime factory's action.policy_effect()",
    "packages/core/governance/approval_policy_service.py": "settings rows from ACTION_CATALOG (derived from the declarations)",
}
_SUBJECT_BUILDERS = {"ApprovalSubject", "check_step_policy", "action_facts", "RuntimeApprovalAction", "ActionFacts"}
_EFFECT_KEYWORDS = {"impact", "category", "gate"}


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _ast_violations(root: Path) -> list[str]:
    problems = []
    for path in sorted((root / "packages").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in rel or rel.endswith("/action_effects.py") or rel.endswith("/approval_facts.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee(node)
            if callee == "action_facts" and rel not in ACTION_FACTS_CALLERS:
                problems.append(f"{rel}:{node.lineno} computes action_facts outside a subject factory")
            if callee in _SUBJECT_BUILDERS and rel not in EFFECT_KEYWORD_CALLERS:
                for keyword in node.keywords:
                    if keyword.arg in _EFFECT_KEYWORDS:
                        problems.append(f"{rel}:{node.lineno} passes {keyword.arg}= to {callee}")
            if callee in _SUBJECT_BUILDERS and rel not in EFFECT_SPLAT_CALLERS:
                for keyword in node.keywords:
                    if keyword.arg is None:
                        problems.append(f"{rel}:{node.lineno} splats ** into {callee}")
    return problems


def test_only_subject_factories_compute_facts_or_pass_effect_keywords():
    assert _ast_violations(REPO) == []
    for rel in (*ACTION_FACTS_CALLERS, *EFFECT_KEYWORD_CALLERS, *EFFECT_SPLAT_CALLERS):
        assert (REPO / rel).is_file(), rel


def test_ast_guard_reports_a_local_effect_guess(tmp_path):
    package = tmp_path / "packages" / "core" / "rogue"
    package.mkdir(parents=True)
    (package / "gate.py").write_text(
        "def f(check_step_policy, action_facts):\n"
        "    action_facts(action_key='x')\n"
        "    check_step_policy(None, action_key='x', **{'impact': 'read'})\n"
        "    return check_step_policy(None, action_key='x', impact='read')\n",
        encoding="utf-8",
    )
    problems = _ast_violations(tmp_path)
    assert any("computes action_facts" in item for item in problems)
    assert any("passes impact=" in item for item in problems)
    assert any("splats ** into check_step_policy" in item for item in problems)


# ── Import boundary: the declaration layer stays a leaf ───────────────────

# ``packages.core.ai.runtime/__init__`` loads the approval classifier, which
# imports ``action_effects``. If the declaration layer imports anything under
# ``packages.core.ai`` it closes that cycle, and a cold import that reaches it
# first (ai.mcp -> settings_service -> approval_policy_service) fails on a
# partially initialized module.
_DECLARATION_LAYER = "packages.core.governance.action_effects"


def _module_file(root: Path, module: str) -> Path | None:
    base = root / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _ai_package_imports(root: Path, start: str) -> dict[str, str]:
    """``packages.core.ai`` modules ``start`` loads at import time -> their importer."""
    seen: dict[str, str] = {}
    pending = [start]
    while pending:
        module = pending.pop()
        path = _module_file(root, module)
        if path is None:
            continue
        for imported in _module_level_imports(path):
            if imported.startswith("packages.core.") and imported not in seen:
                seen[imported] = module
                pending.append(imported)
    return {module: importer for module, importer in seen.items()
            if module == "packages.core.ai" or module.startswith("packages.core.ai.")}


def test_declaration_layer_never_imports_the_ai_package():
    assert _ai_package_imports(REPO, _DECLARATION_LAYER) == {}


def test_import_boundary_guard_reports_an_ai_import(tmp_path):
    governance = tmp_path / "packages" / "core" / "governance"
    governance.mkdir(parents=True)
    (governance / "action_effects.py").write_text(
        "from packages.core.governance.helper import X\n", encoding="utf-8",
    )
    (governance / "helper.py").write_text(
        "from packages.core.ai.runtime.chrome_tool_catalog import X\n", encoding="utf-8",
    )
    assert _ai_package_imports(tmp_path, _DECLARATION_LAYER) == {
        "packages.core.ai.runtime.chrome_tool_catalog": "packages.core.governance.helper",
    }


@pytest.mark.parametrize("module", [
    _DECLARATION_LAYER,
    "packages.core.governance.approval_facts",
    "packages.core.governance.approval_policy_service",
    "packages.core.services.settings_service",
    "packages.core.ai.mcp",
    "packages.core.ai.runtime.approval_classifier",
])
def test_effect_contract_modules_import_from_a_cold_interpreter(module):
    import os
    import subprocess
    import sys

    pythonpath = os.pathsep.join(filter(None, (str(REPO), os.environ.get("PYTHONPATH"))))
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO, env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]


# ── Policy-change re-decision (#1125) judges the declared effect ──────────

def _pending_request(context: dict, action_key: str = "workspace.task.update"):
    from packages.core.models.hitl_request import HitlRequest

    return HitlRequest(
        entity_id="e", workspace_id="w", action_key=action_key, capability_id=None,
        resource_kind="task", resource_id="t", risk_level="medium",
        origin_kind="tool_call", origin_conversation_id="c", context=context,
    )


def test_redecision_snapshot_keeps_the_declared_effect_and_its_judging_keys():
    import json as _json

    from packages.core.governance.approvals import (
        APPROVAL_POLICY_SUBJECT_CONTEXT_KEY, ApprovalOrigin, ApprovalSubject,
        _policy_subject_from_request, _policy_subject_snapshot, subject_policy_facts,
    )

    # A relay judged as the destructive operation it guards (not its own key's
    # reversible declaration), with the relay key restrict-only.
    subject = ApprovalSubject(
        entity_id="e", workspace_id="w", action_key="workspace.task.update",
        resource_kind="task", resource_id="t", impact="destructive", category="deletion",
        gate="policy", action_key_aliases=("workspace.file.delete",),
        restrict_only_keys=("workspace.task.update",), extra_targets=(("file", "f1"),),
    )
    companion = ApprovalSubject(entity_id="e", action_key="workspace.file.create", resource_kind="file")
    snapshot = _json.loads(_json.dumps(_policy_subject_snapshot(
        subject, (companion,), ApprovalOrigin(kind="tool_call", conversation_id="c"),
    )))
    restored, companions, _origin = _policy_subject_from_request(
        _pending_request({APPROVAL_POLICY_SUBJECT_CONTEXT_KEY: snapshot}),
    )
    assert subject_policy_facts(restored) == subject_policy_facts(subject)
    # A companion with no effect of its own stores its declaration's.
    assert (companions[0].impact, companions[0].category) == ("reversible", "files")
    assert subject_policy_facts(companions[0]) == subject_policy_facts(companion)


def test_redecision_of_an_older_row_never_reuses_a_guessed_evaluation_impact():
    from packages.core.governance.approvals import _policy_subject_from_request, subject_policy_facts

    # Minted before the snapshot under the old verb guess: the stored
    # evaluation says "destructive" for a declared reversible key.
    guessed = {"policy_evaluation": {"source": "mode", "reason_params": {"impact": "destructive"}}}
    subject, _, _ = _policy_subject_from_request(_pending_request(guessed))
    facts = subject_policy_facts(subject)
    assert (facts.impact, facts.category) == ("reversible", "workspace")
    # The effect stored when the card was minted wins over the declaration.
    stored = {**guessed, "approval_facts": {
        "impact": "external", "category": "communication", "gate": "policy",
        "aliases": ["external_message.send"], "restrict_only_keys": ["workspace.task.update"],
    }}
    subject, _, _ = _policy_subject_from_request(_pending_request(stored))
    facts = subject_policy_facts(subject)
    assert (facts.impact, facts.category, facts.aliases, facts.restrict_only_keys) == (
        "external", "communication", ["external_message.send"], ["workspace.task.update"],
    )
