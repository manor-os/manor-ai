"""Approval-first source prose must not become another model-only hard block."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
from packages.core.ai.runtime.prompt_guidance import runtime_workspace_agent_mode_guidance
from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.approval_projection import replace_workspace_rules
from packages.core.workspace_chat.context import _search_rules


def _assert_approval_contract(text: str) -> None:
    assert "allows execution after human approval" in text
    assert "description says never or do not" in text
    assert "that prose is not an additional hard block" in text
    assert "Call the intended tool and follow its approval workflow" in text
    assert "all current hard blocks and other runtime gates still apply" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["Never publish posts.", "不要发布帖子。"])
async def test_rules_search_explains_approval_without_rewriting_manual_prose(monkeypatch, description):
    rules = [
        {
            "rule_key": "template_publish_review",
            "rule_type": "approval_required",
            "description": description,
            "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
        },
        {
            "rule_key": "manual_delete_ban",
            "rule_type": "deny",
            "description": "Never delete posts.",
            "approval_selectors": [{"kind": "action", "action_key": "social_post.delete"}],
        },
    ]
    before = deepcopy(rules)
    workspace = SimpleNamespace(operating_model={"rules": rules})
    policy = replace_workspace_rules(WorkspacePolicy(), rules)
    policy_before = deepcopy(policy)

    class ReadOnlyDb:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: workspace)

    async def get_policy(_db, _workspace_id):
        return policy

    monkeypatch.setattr("packages.core.governance.get_policy", get_policy)
    result = await _search_rules(ReadOnlyDb(), "workspace", "entity", "")

    _assert_approval_contract(result)
    sources = [json.loads(line[2:]) for line in result.splitlines() if line.startswith("- {")]
    assert sources[0]["description"] == description
    assert sources[0]["policy_effect"]["effect"] == "ask"
    assert sources[1]["description"] == "Never delete posts."
    assert sources[1]["policy_effect"]["effect"] == "deny"
    assert "deny: social_post.delete" in result
    assert "ask: social_post.publish" in result
    assert rules == before
    assert policy == policy_before


def test_workspace_routing_explains_approval_and_preserves_other_gates():
    envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
        surface=ChatSurface.WORKSPACE_CHAT,
        entity_id="entity",
        user_id="user",
        workspace_id="workspace",
    ))

    result = runtime_workspace_agent_mode_guidance(envelope=envelope, workspace_id="workspace")

    assert result is not None
    _assert_approval_contract(result)
    assert "apply only after the user explicitly confirms" in result
    assert "never bypass or silently weaken guardrails" in result
    assert "Do not interview the user field-by-field in chat" in result
    assert "Need Input via Strategist" in result
    assert runtime_workspace_agent_mode_guidance(envelope=envelope, workspace_id=None) is None
