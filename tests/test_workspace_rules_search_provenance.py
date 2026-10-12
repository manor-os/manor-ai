"""Restriction repair needs source rule identity, not only a description."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from packages.core.workspace_chat.context import _search_rules
from packages.core.governance import WorkspacePolicy
from packages.core.governance.approval_policy import ApprovalPolicyOverride
from packages.core.governance.approval_projection import compile_workspace_rules


def test_operation_surfaces_advertise_read_only_rule_inspection():
    from packages.core.ai.tools.workspace_agent_tools import WORKSPACE_AGENT_SCHEMA, WORKSPACE_OPERATION_SCHEMA

    operation = WORKSPACE_OPERATION_SCHEMA["function"]["description"]
    gateway = WORKSPACE_AGENT_SCHEMA["function"]["parameters"]["properties"]["params"]["description"]
    for description in (operation, gateway):
        assert "action=get_current" in description
        assert "read-only" in description
        assert "source rule" in description


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "social_post.publish", "external.social", "SOCIAL_POST.PUBLISH"])
async def test_rules_search_retains_each_gate_source_and_matches_patterns(monkeypatch, query):
    rules = [
        {
            "rule_key": "no_controversial_topics",
            "description": "不要发布争议话题",
            "rule_type": "deny",
            "action_patterns": ["social_post.publish"],
            "capability_patterns": ["external.social"],
        },
        {
            "rule_key": "approval_required_all_posts",
            "description": "每次发布都需要人工审批",
            "rule_type": "approval_required",
            "action_patterns": ["social_post.publish"],
            "capability_patterns": ["external.social"],
        },
    ]
    before = deepcopy(rules)
    workspace = SimpleNamespace(operating_model={"rules": rules})

    class ReadOnlyDb:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: workspace)

    async def get_policy(_db, _workspace_id):
        return WorkspacePolicy(approval=ApprovalPolicyOverride(rules=compile_workspace_rules(rules)))

    monkeypatch.setattr("packages.core.governance.get_policy", get_policy)
    result = await _search_rules(ReadOnlyDb(), "workspace", "entity", query)
    sources = [json.loads(line[2:]) for line in result.splitlines() if line.startswith('- {')]
    assert [rule["rule_key"] for rule in sources] == [rule["rule_key"] for rule in rules]
    assert sources[0]["policy_effect"]["effect"] == "deny"
    assert sources[1]["policy_effect"]["effect"] == "ask"
    assert all(rule["policy_effect"]["capability_patterns"] == ["external.social"] for rule in sources)
    assert all(rule["runtime_enforced"] for rule in sources)
    assert "Hard blocks win" in result
    assert "action=get_current" in result
    assert rules == before


@pytest.mark.asyncio
async def test_rules_search_does_not_trust_stale_runtime_enforced_flag(monkeypatch):
    rule = {
        "rule_key": "content_guidance",
        "description": "Avoid controversial topics",
        "rule_type": "notice",
        "action_patterns": ["social_post.publish"],
        "runtime_enforced": True,
    }

    class ReadOnlyDb:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(
                operating_model={"rules": [rule, "Keep content useful"]},
            ))

    async def get_policy(_db, _workspace_id):
        return WorkspacePolicy(approval=ApprovalPolicyOverride())

    monkeypatch.setattr("packages.core.governance.get_policy", get_policy)
    result = await _search_rules(ReadOnlyDb(), "workspace", "entity", "")
    sources = [json.loads(line[2:]) for line in result.splitlines() if line.startswith('- {')]
    assert len(sources) == 2
    assert sources[0]["rule_key"] == "content_guidance"
    assert not sources[0]["runtime_enforced"]
    assert "policy_effect" not in sources[0]
    assert not sources[1]["runtime_enforced"]
    assert rule["runtime_enforced"] is True
