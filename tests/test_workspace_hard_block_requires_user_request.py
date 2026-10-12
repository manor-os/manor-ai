"""A hard-block rule (deny / draft_only) exists only because the user asked.

Owner decision (2026-09-07): the platform must never add a prohibition on
its own. "Draft first", "show me before posting", "posts are draft only"
are approval wishes, not bans. A ban needs the user to say so (禁止 / 不允许
/ never / do not), and the architect must pass that on explicitly.

``ws_propose_rule`` first requires ``enforcement_scope=all_matching_actions``
for new ``deny`` / ``draft_only`` rules; missing or conditional scope is
rejected without changing the draft. That declaration is not proof of the
user's natural-language meaning. It also requires
``hard_block_requested_by_user=true``; without it the rule is recorded as
``approval_required`` and the response says so, so the LLM cannot quietly
install a ban by picking a rule_type.
"""

from __future__ import annotations

import json

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.workspace_draft import WorkspaceDraft


def _draft_fields() -> dict:
    return {
        "name": "Social ops",
        "kind": "growth",
        "operating_context": "Runs the company X account",
        "primary_work": "Draft and publish posts",
        "services": [],
        "rules": [],
    }


async def _new_draft(db_session, user_messages: list[str] | None = None):
    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_draft_fields(),
        messages=[{"role": "user", "content": text} for text in (user_messages or [])],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()
    return entity_id, creator_id, draft


@pytest.mark.parametrize("rule_type", ["draft_only", "deny"])
@pytest.mark.asyncio
async def test_hard_block_without_user_request_is_recorded_as_approval(db_session, rule_type):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id, creator_id, draft = await _new_draft(db_session)

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="social_draft_first",
        description="Posts are drafted first and shown to me",
        rule_type=rule_type,
        enforcement_scope="all_matching_actions",
        action_patterns=["social_post.publish"],
    ))

    assert result["ok"] is True
    assert result["rule_type"] == "approval_required"
    assert result["downgraded_from"] == rule_type
    assert any("hard_block_requested_by_user" in w for w in result["warnings"]), result
    stored = next(r for r in draft.fields["rules"] if r["rule_key"] == "social_draft_first")
    assert stored["rule_type"] == "approval_required"


@pytest.mark.asyncio
async def test_hard_block_with_quoted_user_prohibition_is_kept(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id, creator_id, draft = await _new_draft(
        db_session, ["帮我建社媒工作区。另外，禁止删除已发布的帖子。"],
    )

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="never_delete_posts",
        description="禁止删除已发布的帖子",
        rule_type="deny",
        enforcement_scope="all_matching_actions",
        action_patterns=["social_post.delete"],
        hard_block_requested_by_user=True,
        user_request_quote="禁止删除已发布的帖子",
    ))

    assert result["ok"] is True
    assert result["rule_type"] == "deny"
    assert "downgraded_from" not in result
    stored = next(r for r in draft.fields["rules"] if r["rule_key"] == "never_delete_posts")
    assert stored["rule_type"] == "deny"
    assert stored["enforcement_scope"] == "all_matching_actions"


@pytest.mark.asyncio
async def test_flag_without_a_real_user_quote_is_downgraded(db_session):
    """The flag alone is the model's opinion; the quote is checked against the chat."""
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id, creator_id, draft = await _new_draft(
        db_session, ["帮我建社媒工作区，帖子发之前先给我看。"],
    )

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="never_publish",
        description="禁止直接发布",
        rule_type="deny",
        enforcement_scope="all_matching_actions",
        action_patterns=["social_post.publish"],
        hard_block_requested_by_user=True,
        user_request_quote="禁止直接发布",  # the user never said this
    ))

    assert result["rule_type"] == "approval_required"
    assert result["downgraded_from"] == "deny"
    assert any("quote" in w.lower() or "原话" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_draft_only_wording_is_not_a_prohibition_even_when_quoted(db_session):
    """"帖子只出草稿，发之前先给我看" is a review wish (the 2026-09-07 E2E case)."""
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    brief = "帮我建一个社媒运营工作区：负责在 X 上发帖。帖子只出草稿，发之前先给我看。"
    entity_id, creator_id, draft = await _new_draft(db_session, [brief])

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="review_before_x_publishing",
        description="所有 X 帖子只能先生成草稿；在用户查看并明确批准前，不得发布或发送。",
        rule_type="draft_only",
        enforcement_scope="all_matching_actions",
        action_patterns=["social_post.publish", "external_message.send"],
        hard_block_requested_by_user=True,
        user_request_quote="帖子只出草稿，发之前先给我看",
    ))

    assert result["rule_type"] == "approval_required"
    assert result["downgraded_from"] == "draft_only"
    stored = next(r for r in draft.fields["rules"] if r["rule_key"] == "review_before_x_publishing")
    assert stored["rule_type"] == "approval_required"


@pytest.mark.asyncio
async def test_approval_rule_is_unaffected_by_the_flag(db_session):
    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    entity_id, creator_id, draft = await _new_draft(db_session)

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="review_posts",
        description="Show me every post before publishing",
        rule_type="approval_required",
        action_patterns=["social_post.publish"],
    ))

    assert result["rule_type"] == "approval_required"
    assert "downgraded_from" not in result
    assert result["warnings"] == []


def test_schema_requires_explicit_user_request_for_hard_blocks():
    from packages.core.ai.tools.workspace_arch_tools import PROPOSE_RULE_SCHEMA

    props = PROPOSE_RULE_SCHEMA["function"]["parameters"]["properties"]
    flag = props["hard_block_requested_by_user"]
    assert flag["type"] == "boolean"
    assert "explicit" in flag["description"].lower()
    assert "approval_required" in flag["description"]
    assert "hard_block_requested_by_user" in props["rule_type"]["description"]
    quote = props["user_request_quote"]
    assert quote["type"] == "string"
    assert "verbatim" in quote["description"].lower() or "own words" in quote["description"].lower()
    assert props["enforcement_scope"]["enum"] == ["all_matching_actions", "conditional"]
    assert "enforcement_scope" in props["rule_type"]["description"]


def test_architect_prompt_never_invents_a_ban():
    from packages.core.services import workspace_architect

    prompt = " ".join(workspace_architect.ARCHITECT_SYSTEM_PROMPT.split()).lower()
    assert "hard_block_requested_by_user" in prompt
    assert "user_request_quote" in prompt
    assert "never add a hard block" in prompt or "never invent" in prompt
    # "draft only" wording is an approval wish, not a ban
    assert "approval_required" in prompt
    assert "enforcement_scope" in prompt
    assert 'rule_type="notice"' in prompt


@pytest.mark.parametrize("rule_type", ["deny", "draft_only"])
@pytest.mark.parametrize("enforcement_scope", [None, "conditional"])
@pytest.mark.asyncio
async def test_hard_block_requires_scope_without_changing_existing_rules(
    db_session, rule_type, enforcement_scope,
):
    from copy import deepcopy

    from packages.core.ai.tools.workspace_arch_tools import _propose_rule

    quote = "不要发布争议话题"
    entity_id, creator_id, draft = await _new_draft(db_session, [quote])
    draft.fields = {
        **draft.fields,
        "rules": [{
            "rule_key": "existing_ban",
            "description": "Do not delete posts",
            "rule_type": "deny",
            "action_patterns": ["social_post.delete"],
        }],
    }
    await db_session.flush()
    original = deepcopy(draft.fields)

    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="no_controversial_topics",
        description=quote,
        rule_type=rule_type,
        enforcement_scope=enforcement_scope,
        action_patterns=["social_post.publish"],
        hard_block_requested_by_user=True,
        user_request_quote=quote,
    ))

    assert result["ok"] is False
    assert result["code"] == "hard_block_scope_required"
    assert "notice" in result["error"]
    assert "downgraded_from" not in result
    await db_session.refresh(draft)
    assert draft.fields == original

    # The same draft/key can be repaired into conditional operating guidance.
    result = json.loads(await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        rule_key="no_controversial_topics",
        description=quote,
        rule_type="notice",
        enforcement_scope="conditional",
    ))

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    assert draft.fields["rules"][0] == original["rules"][0]
    stored = draft.fields["rules"][1]
    assert stored["description"] == quote
    assert stored["enforcement_scope"] == "conditional"
    from packages.core.services.workspace_setup_service import _infer_rule_enforcement

    assert _infer_rule_enforcement(stored) is None
