"""Template Protocol + registry.

Templates are pure Python so they can do real validation, compute
realistic baselines, and emit ScheduledJobs — things a flat YAML/JSON
recipe couldn't express without growing into its own DSL.

A template's ``apply()`` is the unit of orchestration. It runs inside
the caller's DB transaction; it calls existing services
(``goals.create_goal``, ``services.task_service.create_task``, etc.)
and never reaches into models directly. Tests can swap in fakes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── DTOs ──────────────────────────────────────────────────────────────

@dataclass
class TemplateInput:
    """Common fields every template accepts. Recipes can read additional
    fields out of ``params`` — schemas are advertised in ``Template.params_schema``.
    """

    entity_id: str
    workspace_id: str
    """Templates always create workspace-scoped artefacts. Entity-level
    onboarding wraps this with a synthetic 'default' workspace."""
    user_id: Optional[str] = None
    """Used as creator_id when seeding tasks."""

    params: dict[str, Any] = field(default_factory=dict)
    """Recipe-specific knobs. Validated against ``Template.params_schema``
    before ``apply()`` runs."""


@dataclass
class TemplateResult:
    """What ``apply_template`` returns to the caller — used by the API
    + onboarding UI to render a "you just got these things" summary."""

    template_key: str
    goal_id: Optional[str]
    task_ids: list[str] = field(default_factory=list)
    scheduled_job_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Free-form 'what this template did' lines for the operator."""


class TemplateError(Exception):
    """Raised when a template's required params are missing or invalid."""


# ── Protocol ─────────────────────────────────────────────────────────

@runtime_checkable
class Template(Protocol):
    """A registered template recipe."""

    key: str
    """Stable URL-safe identifier ("twitter_growth"). Used as the API
    handle and in audit logs."""

    title: str
    """Human-readable name for the template gallery."""

    summary: str
    """One-line elevator pitch shown in the picker."""

    params_schema: dict[str, Any]
    """JSON Schema fragment describing TemplateInput.params expectations.
    Validated by the registry before ``apply()`` runs — recipes can
    therefore assume params are well-typed."""

    async def apply(self, db: AsyncSession, inp: TemplateInput) -> TemplateResult:
        ...


# ── Registry ─────────────────────────────────────────────────────────

REGISTRY: dict[str, Template] = {}


def register(template: Template) -> Template:
    """Decorator-style registration. Used by recipe modules at import time."""
    if template.key in REGISTRY:
        raise ValueError(f"template key collision: {template.key!r}")
    REGISTRY[template.key] = template
    return template


def get_template(key: str) -> Template:
    if key not in REGISTRY:
        raise TemplateError(f"no template registered with key={key!r}")
    return REGISTRY[key]


def list_templates() -> list[dict[str, Any]]:
    """Compact summary for the picker UI. Doesn't expose ``apply``."""
    return [
        {
            "key": t.key,
            "title": t.title,
            "summary": t.summary,
            "params_schema": t.params_schema,
        }
        for t in REGISTRY.values()
    ]


async def apply_template(
    db: AsyncSession, key: str, inp: TemplateInput,
) -> TemplateResult:
    """Validate params + run the template. Caller commits."""
    tmpl = get_template(key)
    _validate_params(tmpl, inp.params)
    logger.info(
        "applying template key=%s entity=%s workspace=%s",
        key, inp.entity_id, inp.workspace_id,
    )
    return await tmpl.apply(db, inp)


def _validate_params(tmpl: Template, params: dict[str, Any]) -> None:
    """Tiny JSON-Schema-ish validator covering required + types.

    We intentionally avoid jsonschema as a dep here — the surface is
    narrow (``required`` array + ``properties.<name>.type``) and the
    error messages are clearer when we shape them ourselves.
    """
    schema = tmpl.params_schema or {}
    required = schema.get("required", [])
    for r in required:
        if r not in params:
            raise TemplateError(
                f"template {tmpl.key!r}: missing required param {r!r}"
            )
    props = schema.get("properties", {})
    for name, spec in props.items():
        if name not in params:
            continue
        expected = spec.get("type")
        if expected is None:
            continue
        actual = _json_type(params[name])
        # JSON-Schema allows arrays of types; collapse to a tuple for `in`.
        ok = actual == expected if isinstance(expected, str) else actual in expected
        if not ok:
            raise TemplateError(
                f"template {tmpl.key!r}: param {name!r} expected {expected}, got {actual}"
            )


def _json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


# Trigger recipe registration on import. Local import to avoid a cycle
# on first load (recipes import the registry's `register` function).
from packages.core.templates import recipes as _recipes  # noqa: E402,F401
