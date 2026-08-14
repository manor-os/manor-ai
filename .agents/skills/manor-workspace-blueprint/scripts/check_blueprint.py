#!/usr/bin/env python3
"""Check portable, cross-section invariants of a Workspace Blueprint."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


UNSUPPORTED_NONEMPTY: tuple[tuple[str, ...], ...] = ()

_NATURAL_LANGUAGE_PATHS = (
    ("manifest", "title"),
    ("manifest", "summary"),
    ("manifest", "use_when"),
    ("manifest", "outcome_summary"),
    ("manifest", "description"),
    ("recipe", "operating_model", "primary_work"),
    ("recipe", "operating_model", "context"),
)


def _get(payload: dict[str, Any], *path: str) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text_tree(payload: Any) -> str:
    if isinstance(payload, dict):
        return " ".join(f"{key} {_text_tree(value)}" for key, value in payload.items())
    if isinstance(payload, list):
        return " ".join(_text_tree(item) for item in payload)
    return str(payload or "")


def _natural_language_text(payload: dict[str, Any]) -> str:
    """Read prose fields only; JSON key names are not behavioral claims."""
    values: list[str] = []
    for path in _NATURAL_LANGUAGE_PATHS:
        value = _get(payload, *path)
        if isinstance(value, str):
            values.append(value)
    return " ".join(values).lower()


def _check_task_contract_sections(recipe: dict[str, Any], errors: list[str]) -> None:
    categories = recipe.get("task_categories") or []
    if categories:
        errors.append(
            "recipe.task_categories is not portable; task policy is entity-scoped"
        )
    elif not isinstance(categories, list):
        errors.append("recipe.task_categories must be an array")

    slas = recipe.get("sla_policies") or []
    if slas:
        errors.append(
            "recipe.sla_policies is not portable; task policy is entity-scoped"
        )
    elif not isinstance(slas, list):
        errors.append("recipe.sla_policies must be an array")

    rules = recipe.get("escalation_rules") or []
    if not isinstance(rules, list):
        errors.append("recipe.escalation_rules must be an array")
    else:
        keys = set()
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"recipe.escalation_rules[{index}] must be an object")
                continue
            key = str(rule.get("key") or rule.get("slug") or "").strip()
            if not key:
                errors.append(f"recipe.escalation_rules[{index}] has no portable key")
            elif key in keys:
                errors.append(f"duplicate escalation rule key: {key}")
            keys.add(key)
            if not str(rule.get("action") or rule.get("action_type") or "").strip():
                errors.append(f"recipe.escalation_rules[{index}] has no action")
            if str(rule.get("sla_policy_key") or rule.get("sla_key") or "").strip():
                errors.append(
                    f"recipe.escalation_rules[{index}] is not portable; SLA-linked rules are entity-scoped"
                )
            if not str(rule.get("condition") or "").strip():
                errors.append(f"recipe.escalation_rules[{index}] has no condition")
            if rule.get("notify_user_ids"):
                errors.append(
                    f"recipe.escalation_rules[{index}].notify_user_ids is not portable"
                )

    templates = recipe.get("task_templates") or []
    if templates:
        errors.append(
            "recipe.task_templates is not portable; Proposal generates Task "
            "instances dynamically"
        )

    prompts = recipe.get("prompts") or []
    if isinstance(prompts, list):
        for index, prompt in enumerate(prompts):
            if not isinstance(prompt, dict):
                errors.append(f"recipe.prompts[{index}] must be an object")


def check(payload: dict[str, Any], *, allow_known_unsupported: bool = False) -> list[str]:
    errors: list[str] = []
    for section in ("manifest", "contract", "embedded", "recipe", "policy"):
        if not isinstance(payload.get(section), dict):
            errors.append(f"missing object section: {section}")

    recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
    _check_task_contract_sections(recipe, errors)
    workflows = recipe.get("workflows") or []
    if not isinstance(workflows, list):
        errors.append("recipe.workflows must be an array")
        workflows = []

    workflow_slugs: set[str] = set()
    for index, workflow in enumerate(workflows):
        if not isinstance(workflow, dict):
            errors.append(f"recipe.workflows[{index}] must be an object")
            continue
        slug = str(workflow.get("slug") or workflow.get("name") or "").strip()
        if not slug:
            errors.append(f"recipe.workflows[{index}] has no portable slug")
        elif slug in workflow_slugs:
            errors.append(f"duplicate workflow slug: {slug}")
        workflow_slugs.add(slug)
        steps = workflow.get("steps") or []
        if not isinstance(steps, list):
            errors.append(f"workflow {slug or index} steps must be an array")
            continue
        step_ids: set[str] = set()
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"workflow {slug or index} step {step_index} must be an object")
                continue
            step_id = str(step.get("id") or step.get("key") or "").strip()
            if not step_id:
                errors.append(f"workflow {slug or index} step {step_index} has no id")
            elif step_id in step_ids:
                errors.append(f"workflow {slug or index} duplicate step id: {step_id}")
            step_ids.add(step_id)
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            dependencies = step.get("depends_on") or step.get("needs") or []
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if not isinstance(dependencies, list):
                errors.append(f"workflow {slug or index} step {step_index} dependencies must be an array")
                continue
            for dependency in dependencies:
                if str(dependency) not in step_ids:
                    errors.append(f"workflow {slug or index} step {step_index} references missing dependency {dependency!r}")

    stats = recipe.get("stats") or []
    stat_keys: set[str] = set()
    if not isinstance(stats, list):
        errors.append("recipe.stats must be an array")
        stats = []
    for index, stat in enumerate(stats):
        if not isinstance(stat, dict):
            errors.append(f"recipe.stats[{index}] must be an object")
            continue
        key = str(stat.get("key") or "").strip()
        if not key:
            errors.append(f"recipe.stats[{index}] has no portable key")
        elif key in stat_keys:
            errors.append(f"duplicate stat key: {key}")
        stat_keys.add(key)

    goals = recipe.get("goals") or []
    if not isinstance(goals, list):
        errors.append("recipe.goals must be an array")
        goals = []
    for index, goal in enumerate(goals):
        if not isinstance(goal, dict):
            errors.append(f"recipe.goals[{index}] must be an object")
            continue
        stat_key = str(goal.get("stat_key") or "").strip()
        if stat_key and stat_key not in stat_keys:
            errors.append(f"recipe.goals[{index}] references unknown stat_key {stat_key!r}")

    jobs = recipe.get("scheduled_jobs") or []
    if not isinstance(jobs, list):
        errors.append("recipe.scheduled_jobs must be an array")
        jobs = []
    job_ids: set[str] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            errors.append(f"recipe.scheduled_jobs[{index}] must be an object")
            continue
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            errors.append(f"recipe.scheduled_jobs[{index}] has no portable job_id")
        elif job_id in job_ids:
            errors.append(f"duplicate scheduled job_id: {job_id}")
        job_ids.add(job_id)
        execution_type = str(job.get("execution_type") or "agent").strip()
        target = job.get("execution_target") or {}
        if not isinstance(target, dict):
            errors.append(f"scheduled job {job_id or index} execution_target must be an object")
            continue
        if execution_type == "workflow":
            workflow_slug = str(target.get("workflow_slug") or "").strip()
            if not workflow_slug:
                errors.append(f"scheduled workflow {job_id or index} has no workflow_slug")
            elif workflow_slug not in workflow_slugs:
                errors.append(f"scheduled workflow {job_id or index} targets unknown workflow {workflow_slug}")
        elif execution_type in {"agent", "agent_message"} and not str(target.get("service_key") or "").strip():
            errors.append(f"scheduled agent job {job_id or index} has no execution_target.service_key")

    for path in UNSUPPORTED_NONEMPTY:
        value = _get(payload, *path)
        if value and not allow_known_unsupported:
            errors.append(f"unsupported non-empty section: {'.'.join(path)}; implement exporter/installer parity or empty it")

    text = _natural_language_text(payload)
    positive_workflow_claim = bool(re.search(r"\b(?:run|runs|uses?|with|trigger(?:s|ed)?)\s+(?:a\s+)?workflow\b|\bworkflow\s+to\b", text))
    if "workflow" in text and not workflows and ("no workflow" not in text or positive_workflow_claim):
        errors.append("text mentions workflow but recipe.workflows is empty")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--allow-known-unsupported", action="store_true")
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("error: Blueprint payload must be a JSON object", file=sys.stderr)
        return 2
    errors = check(payload, allow_known_unsupported=args.allow_known_unsupported)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("Blueprint contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
