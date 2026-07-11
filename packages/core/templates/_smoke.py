"""Smoke test for the templates registry.

We intentionally skip a full end-to-end SQL test because the recipes
delegate to existing well-tested services (goals.create_goal,
task_service.create_task). What's worth verifying here is the registry
contract: params validation, key lookup, list shape.

Run with: uv run python -m packages.core.templates._smoke
"""
from __future__ import annotations

import sys

from packages.core.templates import (
    REGISTRY,
    TemplateError,
    TemplateInput,
    apply_template,
    get_template,
    list_templates,
)
from packages.core.templates.registry import _validate_params


def _check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        sys.exit(1)


def case_registry_loaded() -> None:
    print("[case] registry loads bundled recipes")
    keys = set(REGISTRY.keys())
    _check("twitter_growth" in keys, "twitter_growth registered")
    _check("email_triage" in keys, "email_triage registered")
    _check("daily_briefing" in keys, "daily_briefing registered")


def case_list_shape() -> None:
    print("\n[case] list_templates returns picker-friendly shape")
    items = list_templates()
    _check(len(items) >= 3, f"at least 3 templates listed (got {len(items)})")
    sample = next(i for i in items if i["key"] == "twitter_growth")
    _check("title" in sample and "summary" in sample, "title + summary present")
    _check("params_schema" in sample, "params_schema exposed")
    _check(
        "target_followers" in sample["params_schema"]["properties"],
        "schema describes target_followers",
    )


def case_param_validation_required() -> None:
    print("\n[case] missing required param raises TemplateError")
    tmpl = get_template("twitter_growth")
    try:
        _validate_params(tmpl, {"target_followers": 10000})
        # missing 'deadline'
        _check(False, "should have raised TemplateError")
    except TemplateError as exc:
        _check("deadline" in str(exc), "error names the missing field")


def case_param_validation_type() -> None:
    print("\n[case] wrong type raises TemplateError")
    tmpl = get_template("twitter_growth")
    try:
        _validate_params(tmpl, {"target_followers": "lots", "deadline": "2026-12-31"})
        _check(False, "should have raised TemplateError on string for integer")
    except TemplateError as exc:
        _check("target_followers" in str(exc), "error names the bad field")
        _check("integer" in str(exc), "error names the expected type")


def case_unknown_key() -> None:
    print("\n[case] unknown key raises TemplateError")
    try:
        get_template("definitely_not_a_template")
        _check(False, "should have raised")
    except TemplateError as exc:
        _check("definitely_not_a_template" in str(exc), "error names the missing key")


def case_email_recipe_defaults() -> None:
    print("\n[case] email_triage accepts mailbox-only payload (defaults work)")
    tmpl = get_template("email_triage")
    _validate_params(tmpl, {"mailbox": "ops@example.com"})
    _check(True, "no exception")


if __name__ == "__main__":
    case_registry_loaded()
    case_list_shape()
    case_param_validation_required()
    case_param_validation_type()
    case_unknown_key()
    case_email_recipe_defaults()
    print("\nSMOKE OK")
