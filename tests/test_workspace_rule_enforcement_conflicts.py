"""A hard-block rule and an approval rule on the same action must be visible.

Follow-up to the 2026-09-07 incident (PR #729). The workspace had
``social_post.publish`` in BOTH ``never_allow`` (from a ``draft_only`` rule)
and ``hitl_required`` (from an approval rule). Deny gates win, so the
approval rule was dead and the user could not tell why posting never
happened. Nothing at draft time, at ``ws_propose_rule`` time, or at
operation-draft validation time said so.

Contract pinned here:

* ``rule_enforcement_conflicts`` finds patterns claimed by both a hard-block
  rule type and an approval rule type (actions and capabilities);
* ``ws_lint_draft`` reports each conflict as a P1 issue;
* ``ws_propose_rule`` returns the conflicts its new rule created;
* ``validate_operation_draft`` warns on rule conflicts and on explicit
  never_allow/hitl overlap in the policy lists;
* the architect prompt and the ``ws_propose_rule`` schema say that
  ``draft_only`` is a hard block and must not be combined with approval.
"""

from __future__ import annotations

import json

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_operation_service import validate_operation_draft
from packages.core.services.workspace_setup_service import rule_enforcement_conflicts


def _draft_only_rule() -> dict:
    return {
        "rule_key": "social_draft_only",
        "description": "Social posts are draft only",
        "rule_type": "draft_only",
        "action_patterns": ["social_post.publish"],
    }


def _approval_rule() -> dict:
    return {
        "rule_key": "social_approval",
        "description": "Ask before publishing social posts",
        "rule_type": "approval_required",
        "action_patterns": ["social_post.publish"],
    }


# ── helper ────────────────────────────────────────────────────────────────


def test_conflicts_found_for_exact_action_without_invented_capability():
    conflicts = rule_enforcement_conflicts([_draft_only_rule(), _approval_rule()])

    by_pattern = {(c["kind"], c["pattern"]): c for c in conflicts}
    action = by_pattern[("action", "social_post.publish")]
    assert action["blocking_rule_keys"] == ["social_draft_only"]
    assert action["approval_rule_keys"] == ["social_approval"]
    assert action["blocking_rule_types"] == ["draft_only"]
    assert ("capability", "external.social") not in by_pattern


def test_no_conflict_when_only_one_enforcement_kind():
    assert rule_enforcement_conflicts([_draft_only_rule()]) == []
    assert rule_enforcement_conflicts([_approval_rule()]) == []
    tone = {"rule_key": "tone", "description": "Be warm"}
    assert rule_enforcement_conflicts([tone, _approval_rule()]) == []


def test_deny_and_block_types_also_count_as_hard_blocks():
    deny = dict(_draft_only_rule(), rule_key="deny_social", rule_type="deny")
    conflicts = rule_enforcement_conflicts([deny, _approval_rule()])
    assert [c["pattern"] for c in conflicts if c["kind"] == "action"] == ["social_post.publish"]


# ── ws_lint_draft / ws_propose_rule ──────────────────────────────────────


def _minimal_draft_fields(rules: list[dict]) -> dict:
    return {
        "name": "Social ops",
        "kind": "growth",
        "operating_context": "Runs the company X account",
        "primary_work": "Draft and publish posts",
        "services": [],
        "rules": rules,
    }


@pytest.mark.asyncio
async def test_lint_reports_conflict_as_p1(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_minimal_draft_fields([_draft_only_rule(), _approval_rule()]),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    lint = json.loads(await _lint_draft(
        db_session, entity_id=entity_id, user_id=creator_id, draft_id=draft.id,
    ))

    hits = [i for i in lint["issues"] if i["where"] == "rules.social_post.publish"]
    assert hits, lint["issues"]
    assert hits[0]["severity"] == "P1"
    assert "social_draft_only" in hits[0]["message"]
    assert "social_approval" in hits[0]["message"]
    assert "approval_required" in hits[0]["message"]


@pytest.mark.asyncio
async def test_propose_rule_returns_conflicts_it_creates(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_minimal_draft_fields([_draft_only_rule()]),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="social_approval",
        description="Ask before publishing social posts",
        rule_type="approval_required",
        action_patterns=["social_post.publish"],
    ))

    assert result["ok"] is True
    assert result["rule_key"] == "social_approval"
    warnings = result["warnings"]
    assert any("social_draft_only" in w and "social_post.publish" in w for w in warnings), warnings


@pytest.mark.asyncio
async def test_propose_rule_without_conflict_has_no_warnings(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_minimal_draft_fields([]),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="social_approval",
        description="Ask before publishing social posts",
        rule_type="approval_required",
        action_patterns=["social_post.publish"],
    ))

    assert result["ok"] is True
    assert result.get("warnings", []) == []


# ── validate_operation_draft ─────────────────────────────────────────────


class _NoDb:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("unexpected DB access")


def _operation_draft(state: dict) -> WorkspaceOperationDraft:
    return WorkspaceOperationDraft(
        id="01DRAFT000000000000000000",
        workspace_id="01WS00000000000000000000",
        entity_id="01ENT0000000000000000000",
        status="open",
        current_state=state,
        patches=[{"op": "rule.add", "payload": {"rule": _approval_rule()}}],
    )


@pytest.mark.asyncio
async def test_operation_validation_warns_on_rule_conflict():
    rules = [_draft_only_rule(), _approval_rule()]
    state = {
        "operating_model": {"rules": rules},
        "services": [{"key": "social"}],
        "rules": rules,
        "governance_policy": {},
    }

    validation = await validate_operation_draft(_NoDb(), _operation_draft(state), current_state=state)

    hits = [w for w in validation["warnings"] if w["path"] == "rules.social_post.publish"]
    assert hits, validation["warnings"]
    assert "social_draft_only" in hits[0]["message"]


@pytest.mark.asyncio
async def test_operation_validation_warns_on_explicit_policy_overlap():
    state = {
        "operating_model": {"rules": []},
        "services": [{"key": "social"}],
        "rules": [],
        "governance_policy": {
            "approval": {"mode": None, "rules": [
                {"id": "block-post", "effect": "deny", "selector": {"kind": "action", "action_key": "social_post.publish"}},
                {"id": "ask-post", "effect": "ask", "selector": {"kind": "action", "action_key": "social_post.publish"}},
                {"id": "ask-email", "effect": "ask", "selector": {"kind": "action", "action_key": "email.send"}},
            ]},
        },
    }

    validation = await validate_operation_draft(_NoDb(), _operation_draft(state), current_state=state)

    hits = [w for w in validation["warnings"] if w["path"] == "governance_policy.approval"]
    assert hits, validation["warnings"]
    assert "same selector" in hits[0]["message"]
    assert "prohibition wins" in hits[0]["message"]


# ── prompt + schema wording ──────────────────────────────────────────────


def test_propose_rule_schema_explains_rule_types():
    from packages.core.ai.tools.workspace_arch_tools import PROPOSE_RULE_SCHEMA

    description = PROPOSE_RULE_SCHEMA["function"]["parameters"]["properties"]["rule_type"]["description"]
    assert "draft_only" in description
    assert "never" in description.lower()
    assert "approval_required" in description
    assert "same action" in description.lower()


def test_architect_prompt_separates_draft_only_from_approval():
    from packages.core.services import workspace_architect

    prompt = " ".join(workspace_architect.ARCHITECT_SYSTEM_PROMPT.split()).lower()
    assert "draft_only" in prompt
    assert "hard block" in prompt
    assert "never" in prompt
    assert "same action" in prompt
