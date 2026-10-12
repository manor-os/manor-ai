"""Agent/service reviews expose the actual additions and removals."""

from packages.core.ai.runtime.workspace_operation_actions import _operation_review_payload
from packages.core.services.workspace_operation_service import operation_review_hitl_payload


def _draft(changes):
    return {"diff": {"changed_keys": list(changes), "changes": changes}}


def test_service_removal_review_names_every_removed_service():
    draft = _draft({"services": {"before": [
        {"service_key": "research", "name": "Market Research"},
        {"service_key": "content", "name": "Marketing Content"},
        {"service_key": "leads", "name": "Lead Nurturing"},
    ], "after": []}})
    payload = _operation_review_payload(draft, prompt="Apply changes?", content={})
    assert "Remove services: Market Research, Marketing Content, Lead Nurturing." in payload["hitl"]["payload"]["why"]


def test_additive_agent_review_separates_new_service_and_agent_binding():
    draft = _draft({
        "services": {"before": [{"key": "content"}], "after": [
            {"key": "content"}, {"key": "video", "name": "Marketing Video"},
        ]},
        "agent_mappings": {"before": [{"service_key": "content", "agent_id": "copywriter"}], "after": [
            {"service_key": "content", "agent_id": "copywriter"},
            {"service_key": "video", "agent_id": "producer"},
        ]},
    })
    reason = operation_review_hitl_payload(draft)["why"]
    assert "Add services: Marketing Video." in reason
    assert "Add Agent bindings for services: video." in reason
    assert "Replace Agents" not in reason
    assert "Remove" not in reason


def test_existing_service_agent_replacement_is_explicit():
    draft = _draft({"agent_mappings": {
        "before": [{"service_key": "marketing_content", "agent_id": "copywriter"}],
        "after": [{"service_key": "marketing_content", "agent_id": "video-producer"}],
    }})
    assert "Replace Agents for services: marketing_content." in operation_review_hitl_payload(draft)["why"]


def test_prompt_edit_is_not_an_agent_replacement():
    draft = _draft({"agent_mappings": {
        "before": [{"service_key": "content", "agent_id": "copywriter", "custom_prompt": "old"}],
        "after": [{"service_key": "content", "agent_id": "copywriter", "custom_prompt": "new"}],
    }})
    assert "Replace Agents" not in operation_review_hitl_payload(draft)["why"]


def test_missing_before_snapshot_does_not_invent_additions_or_removals():
    draft = _draft({"services": {"after": [{"key": "video"}]}})
    reason = operation_review_hitl_payload(draft)["why"]
    assert "Add services" not in reason
    assert "Remove services" not in reason
