"""The agent-visible operation surface must be derived from the implementation.

Companion to ``test_workspace_operation_patch_catalog.py``. In the 2026-09-07
incident the chat only had the ``manor`` gateway loaded, so the rich
``workspace_operation`` schema text never reached the model; the gateway's own
parameter hint said nothing about patch ops, and the first attempt used
``op`` instead of ``action`` and got a bare ``action is required``.
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from packages.core.ai.runtime.workspace_operation_actions import (
    _resolve_workspace_operation_action,
    runtime_workspace_operation_action,
)
from packages.core.ai.tools.workspace_agent_tools import (
    WORKSPACE_AGENT_SCHEMA,
    WORKSPACE_OPERATION_SCHEMA,
)
from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.governance.approval_projection import compile_workspace_rules
from packages.core.services.workspace_operation_service import (
    OPERATION_PATCH_OP_CATALOG,
    SUPPORTED_OPERATION_PATCH_OPS,
    validate_operation_draft,
)

_CANONICAL_OPS = [entry.canonical for entry in OPERATION_PATCH_OP_CATALOG]


# ── schemas advertise the derived vocabulary ─────────────────────────────


def test_workspace_operation_schema_lists_every_canonical_op():
    description = WORKSPACE_OPERATION_SCHEMA["function"]["parameters"]["properties"]["patches"]["description"]
    for op in _CANONICAL_OPS:
        assert op in description, op

    item = WORKSPACE_OPERATION_SCHEMA["function"]["parameters"]["properties"]["patches"]["items"]
    assert set(item["properties"]["op"]["enum"]) == set(SUPPORTED_OPERATION_PATCH_OPS)


def test_workspace_gateway_schema_lists_every_canonical_op():
    description = WORKSPACE_AGENT_SCHEMA["function"]["parameters"]["properties"]["params"]["description"]
    assert "operation" in description
    for op in _CANONICAL_OPS:
        assert op in description, op


# ── gateway parameter tolerance ──────────────────────────────────────────


def test_resolve_action_accepts_op_and_operation_keys_as_fallback():
    assert _resolve_workspace_operation_action({"action": "create"}) == "create_draft"
    assert _resolve_workspace_operation_action({"op": "create"}) == "create_draft"
    assert _resolve_workspace_operation_action({"operation": "apply"}) == "apply_draft"
    assert _resolve_workspace_operation_action({"action": "patch", "op": "create"}) == "patch_draft"
    assert _resolve_workspace_operation_action({"title": "x"}) == ""


@pytest.mark.asyncio
async def test_missing_action_error_lists_valid_actions():
    raw = await runtime_workspace_operation_action(
        entity_id="entity",
        workspace_id="workspace",
        params={"title": "Enable X publishing"},
    )
    payload = json.loads(raw)
    assert "action is required" in payload["error"]
    for action in ("create_draft", "patch_draft", "apply_draft", "discard_draft"):
        assert action in payload["error"]


@pytest.mark.asyncio
async def test_unknown_action_error_lists_valid_actions():
    raw = await runtime_workspace_operation_action(
        entity_id="entity",
        workspace_id="workspace",
        params={"action": "publish"},
    )
    payload = json.loads(raw)
    assert "publish" in payload["error"]
    assert "create_draft" in payload["error"]


# ── validator explains rule-derived guardrails ───────────────────────────


class _NoDb:
    """validate_operation_draft only touches the DB for agent lookups."""

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("unexpected DB access")


def _draft(state: dict, patches: list[dict]) -> WorkspaceOperationDraft:
    return WorkspaceOperationDraft(
        id="01DRAFT000000000000000000",
        workspace_id="01WS00000000000000000000",
        entity_id="01ENT0000000000000000000",
        status="open",
        current_state=state,
        patches=patches,
    )


@pytest.mark.asyncio
async def test_governance_edit_is_invalid_when_a_rule_still_implies_the_gate():
    rule = {
        "rule_key": "draft_only_social",
        "description": "Social posts are draft only",
        "rule_type": "draft_only",
        "action_patterns": ["social_post.publish"],
        "capability_patterns": ["external.social"],
        "runtime_enforced": True,
    }
    state = {
        "operating_model": {"rules": [rule], "governance": {}},
        "services": [{"key": "social"}],
        "rules": [rule],
        # A persisted stale draft attempts to erase a source-owned rule.
        # New generic writes reject this payload before creating a draft.
        "governance_policy": {"approval": {"mode": None, "rules": []}},
    }
    draft = _draft(
        state,
        [{"op": "governance_policy.update", "payload": {"approval": {"mode": None, "rules": []}}}],
    )

    before = deepcopy(state)
    before["governance_policy"]["approval"]["rules"] = [r.model_dump() for r in compile_workspace_rules([rule])]
    validation = await validate_operation_draft(_NoDb(), draft, current_state=before)

    messages = [
        error["message"] for error in validation["errors"]
        if error.get("code") == "rule_implied_governance"
    ]
    hit = [m for m in messages if "draft_only_social" in m]
    assert hit, messages
    assert "draft_only_social" in hit[0]
    assert "edit that source" in hit[0]
    assert validation["valid"] is False


@pytest.mark.asyncio
async def test_governance_edit_does_not_warn_when_rule_was_removed_too():
    state = {
        "operating_model": {"rules": [], "governance": {}},
        "services": [{"key": "social"}],
        "rules": [],
        "governance_policy": {"approval": {"mode": None, "rules": []}},
    }
    draft = _draft(
        state,
        [
            {"op": "rule.remove", "payload": {"rule_key": "draft_only_social"}},
            {"op": "governance_policy.update", "payload": {"max_risk_level": "high"}},
        ],
    )

    validation = await validate_operation_draft(_NoDb(), draft, current_state=state)

    assert not [w for w in validation["warnings"] if "social_post.publish" in w["message"]]
    assert not [
        error for error in validation["errors"]
        if error.get("code") == "rule_implied_governance"
    ]


# ── patches key aliases and empty drafts ─────────────────────────────────


def test_operations_key_is_accepted_as_patches_alias():
    from packages.core.ai.runtime.workspace_operation_actions import _resolve_workspace_operation_patches

    patches = _resolve_workspace_operation_patches({
        "action": "create_draft",
        "operations": [{"op": "rule.update", "payload": {"rule_key": "x", "rule": {"rule_type": "approval_required"}}}],
    })
    assert patches == [{"op": "rule.update", "payload": {"rule_key": "x", "rule": {"rule_type": "approval_required"}}}]
    assert _resolve_workspace_operation_patches({"ops": [{"op": "rule.add"}]}) == [{"op": "rule.add"}]
    assert _resolve_workspace_operation_patches({"patches": [{"op": "rule.add"}]}) == [{"op": "rule.add"}]
    assert _resolve_workspace_operation_patches({"title": "x"}) == []


@pytest.mark.asyncio
async def test_create_draft_without_patches_is_rejected_with_vocabulary():
    # In the 2026-09-07 E2E the agent sent `operations: [...]`, the runtime
    # read `patches` (absent), and created an EMPTY draft plus a review card
    # with nothing in it. An empty create is never what the caller meant.
    raw = await runtime_workspace_operation_action(
        entity_id="entity",
        workspace_id="workspace",
        params={"action": "create_draft", "title": "Enable X publishing"},
    )
    payload = json.loads(raw)
    assert "patches" in payload["error"]
    assert "rule.update" in payload["error"]
