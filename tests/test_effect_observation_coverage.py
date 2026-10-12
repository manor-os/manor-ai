"""Every ACTION call the classifier knows is visible as an effect in the ledger.

Guard the class, not the instance: the scan covers every tool in the ToolPool
(plus the classifier's fixed table), classified per action argument the tool's
schema enumerates, exactly as the receipt projection coverage scan does. A
confirmed ACTION call must carry its classifier facts on the observation, and
a non-file one must reach the run's effects ledger, so a created Task, a sent
message or any new action never disappears from the Task's result.
"""

from __future__ import annotations

import json
from functools import lru_cache

from packages.core.ai.runtime import approval_classifier
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
from tests.test_receipt_projection_coverage import _schema_variants


@lru_cache(maxsize=1)
def _action_calls() -> tuple[tuple[str, tuple[tuple[str, str], ...], object], ...]:
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    schemas = dict(pool.registered_tool_schemas(include_undiscoverable=True))
    for name in approval_classifier._FIXED_TOOL_ACTIONS:
        schemas.setdefault(name, {})
    out = []
    for name, schema in sorted(schemas.items()):
        for arguments in _schema_variants(schema):
            try:
                classification = classify_runtime_tool(name, arguments)
            except Exception:
                continue
            if classification.effect is RuntimeToolEffect.ACTION:
                out.append((name, tuple(sorted(arguments.items())), classification.action))
    return tuple(out)


def _listed(action) -> bool:
    return not (
        (action.resource_kind == "file" and action.operation in {"create", "modify"})
        or action.operation == "read"
        or action.action_key == "workspace.service.delegate"
    )


def test_the_scan_sees_action_tools():
    names = {name for name, _args, _action in _action_calls()}
    assert {"generate_file", "workspace_agent", "manor", "publish_site"} <= names


def test_every_confirmed_action_call_is_observed_with_its_action_key():
    missing, unlisted = [], []
    for name, arguments, action in _action_calls():
        ledger = RuntimeToolEvidence()
        ledger.observe_tool_result(name, "call-1", json.dumps({"ok": True}), dict(arguments))
        observation = ledger.observations["call-1"]
        if observation.get("effect") != "action" or observation.get("action_key") != action.action_key:
            missing.append(f"{name}{dict(arguments)}")
            continue
        assert observation["effect_state"] == "confirmed", (name, arguments)
        listed = [effect["action_key"] for effect in ledger.effects]
        if _listed(action) and listed != [action.action_key]:
            unlisted.append(f"{name}{dict(arguments)}")
    assert missing == [], f"ACTION calls observed without classifier facts: {missing}"
    assert unlisted == [], f"confirmed non-file ACTION calls missing from the effects ledger: {unlisted}"
