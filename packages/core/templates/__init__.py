"""Goal templates — pre-baked recipes that one-shot a goal + starter
tasks for a workspace.

A template is a Python class registered via ``REGISTRY``. Onboarding
asks the operator to pick one ("Twitter Growth", "Email Triage", "Daily
Briefing"), and ``apply_template`` mints the goal, the recurring
ScheduledJobs, and the seed Tasks in one transaction.

Template authors get a small surface (``apply``); the runtime gets
deterministic instantiation that's easy to test against a fresh DB.
The recipe namespace lives in ``packages/core/templates/recipes/``.
"""
from packages.core.templates.registry import (
    REGISTRY,
    Template,
    TemplateError,
    TemplateInput,
    TemplateResult,
    apply_template,
    get_template,
    list_templates,
)

__all__ = [
    "REGISTRY",
    "Template",
    "TemplateError",
    "TemplateInput",
    "TemplateResult",
    "apply_template",
    "get_template",
    "list_templates",
]
