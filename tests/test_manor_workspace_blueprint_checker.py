from __future__ import annotations

import importlib.util
from pathlib import Path


_CHECKER_PATH = (
    Path(__file__).parents[1]
    / ".agents/skills/manor-workspace-blueprint/scripts/check_blueprint.py"
)
_SPEC = importlib.util.spec_from_file_location("manor_blueprint_checker", _CHECKER_PATH)
assert _SPEC and _SPEC.loader
_CHECKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHECKER)
check_blueprint_payload = _CHECKER.check


def _payload() -> dict:
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "No workflow workspace",
            "summary": "A daily operating workspace.",
            "description": "The workspace creates a reviewable deliverable.",
            "use_when": "Use for a daily operating loop.",
            "outcome_summary": "A finished deliverable is returned.",
        },
        "contract": {},
        "embedded": {},
        "recipe": {
            "operating_model": {
                "primary_work": "Create one deliverable each day.",
                "context": "No workflow is required for this workspace.",
            },
            "workflows": [],
            "scheduled_jobs": [],
            "prompts": [],
            "task_categories": [],
            "sla_policies": [],
            "escalation_rules": [],
            "task_templates": [],
        },
        "policy": {},
    }


def test_checker_does_not_scan_json_field_names_as_natural_language() -> None:
    errors = check_blueprint_payload(_payload())
    assert not any("text mentions workflow" in error for error in errors)


def test_checker_rejects_natural_language_workflow_without_definition() -> None:
    payload = _payload()
    payload["manifest"]["summary"] = "Runs a workflow to produce the deliverable."
    errors = check_blueprint_payload(payload)
    assert "text mentions workflow but recipe.workflows is empty" in errors


def test_checker_rejects_missing_workflow_dependency() -> None:
    payload = _payload()
    payload["recipe"]["workflows"] = [{
        "slug": "daily-loop",
        "steps": [{"id": "produce", "kind": "agent_call", "depends_on": ["missing"]}],
    }]
    errors = check_blueprint_payload(payload)
    assert any("missing dependency" in error for error in errors)


def test_checker_rejects_scheduled_workflow_with_unknown_target() -> None:
    payload = _payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "daily",
        "execution_type": "workflow",
        "execution_target": {"workflow_slug": "unknown"},
    }]
    errors = check_blueprint_payload(payload)
    assert any("unknown workflow" in error for error in errors)


def test_checker_rejects_entity_task_policy_as_nonportable() -> None:
    payload = _payload()
    payload["recipe"]["task_categories"] = [{"key": "production", "label": "Production"}]
    errors = check_blueprint_payload(payload)
    assert any("task_categories" in error and "not portable" in error for error in errors)


def test_checker_rejects_scheduled_agent_without_service_key() -> None:
    payload = _payload()
    payload["recipe"]["scheduled_jobs"] = [{
        "job_id": "daily",
        "execution_type": "agent_message",
        "execution_target": {},
    }]
    errors = check_blueprint_payload(payload)
    assert any("service_key" in error for error in errors)


def test_checker_rejects_goal_with_unknown_stat() -> None:
    payload = _payload()
    payload["recipe"]["goals"] = [{"title": "Goal", "stat_key": "missing"}]
    errors = check_blueprint_payload(payload)
    assert any("unknown stat_key" in error for error in errors)


def test_checker_rejects_task_templates() -> None:
    payload = _payload()
    payload["recipe"]["task_templates"] = [{"key": "daily", "title_template": "Daily"}]
    errors = check_blueprint_payload(payload)
    assert any("not portable" in error for error in errors)


def test_checker_rejects_escalation_rule_as_nonportable() -> None:
    payload = _payload()
    payload["recipe"]["escalation_rules"] = [{"key": "late", "action": "notify"}]
    errors = check_blueprint_payload(payload)
    assert any("escalation_rules" in error and "no condition" in error for error in errors)


def test_checker_rejects_source_entity_user_ids_with_nonportable_policy() -> None:
    payload = _payload()
    payload["recipe"]["escalation_rules"] = [{
        "key": "late",
        "condition": "manual",
        "action": "notify",
        "notify_user_ids": ["source-user"],
    }]
    errors = check_blueprint_payload(payload)
    assert any("escalation_rules" in error and "not portable" in error for error in errors)


def test_checker_accepts_governance_without_recipe_escalation_rule() -> None:
    payload = _payload()
    payload["policy"]["governance"] = {"hitl_required_actions": ["social.publish_*"]}
    assert not check_blueprint_payload(payload)
