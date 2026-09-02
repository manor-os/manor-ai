"""Plan-time contract linker: validate step I/O before execution.

Catches the two root causes of runtime step failures before a plan runs:
OutputSchemaError (a step has no declared output shape) and ReferenceError
(a step reads an upstream key the upstream shape never produces).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.contracts.shapes import get_shape
from packages.core.contracts.task_output import (
    known_schema_path,
    known_top_level_keys,
)

_LLM_KINDS = {"llm", "subagent"}

# Default shape for llm/subagent steps whose shape can't be inferred from
# downstream refs: the legacy StepResult envelope (contracts/envelope.py). Its
# top-level fields (status, summary, outputs, progress, failure, next_steps)
# come from the shape's json_schema, so ``_shape_top_level_keys`` makes refs
# like ``steps.x.result.outputs.text`` / ``.status`` valid automatically. A
# schema whose top-level properties cannot be resolved is not treated as an
# open contract; named field refs fail closed instead. Explicit
# canonical/custom payload contracts are different: their fields are top-level
# on ``result`` and must not be treated as if they had an envelope.
_DEFAULT_LLM_SHAPE = "StepResult"

# field -> shape that canonically provides it (for inferring a missing shape).
# Only map a field to a shape when the field is a *real top-level key* of that
# shape. Mapping an alias the shape normalizes away (e.g. `content`/`posts`)
# would mark a ref "resolved" while the runtime read still misses — the exact
# production ReferenceError. Aliased reads stay flagged as gaps, to be fixed by
# shaping the producer explicitly (proposal/enforcement phases), not papered over.
_FIELD_TO_SHAPE = {
    "files": "ArtifactResult",
    "fs_path": "DocumentResult",
    "document_id": "DocumentResult",
    "text": "TextResult",
    "items": "ListResult",
    "url": "PublishResult",
    "count": "CountResult",
    "drafts": "DraftPack",
}


@dataclass(frozen=True)
class LinkIssue:
    kind: str          # "missing_output_shape" | "dangling_reference"
    step_key: str
    detail: str


def _schema_top_level_keys(schema: Any) -> set[str] | None:
    """Return statically known object keys from the shared schema contract."""

    # A custom/action schema is a producer guarantee, so only required fields
    # are safe to expose to downstream refs. Canonical shapes use the legacy
    # helper below and retain their established envelope vocabulary.
    return known_top_level_keys(schema, require_required=True)


def _shape_top_level_keys(shape_name: str | None) -> set[str] | None:
    if not shape_name:
        return None
    try:
        schema = get_shape(shape_name).json_schema()
    except KeyError:
        return None
    # Canonical shapes define their complete vocabulary; optional envelope
    # fields such as StepResult.outputs remain linkable for legacy plans.
    return known_top_level_keys(schema)


def lint_plan(steps: list[dict[str, Any]]) -> list[LinkIssue]:
    issues: list[LinkIssue] = []
    by_key = {s.get("key"): s for s in steps}

    for s in steps:
        key = s.get("key")
        kind = s.get("kind")
        shape = s.get("output_shape")

        if kind in _LLM_KINDS and not shape and s.get("expected_output_schema") is None:
            issues.append(LinkIssue("missing_output_shape", key, f"{kind} step has no output_shape"))

        input_ref_paths = s.get("input_ref_paths")
        if input_ref_paths:
            refs = [
                (
                    ref_key,
                    full_path.split(".", 1)[0].split("[", 1)[0]
                    if full_path else None,
                    full_path,
                )
                for ref_key, full_path in input_ref_paths
            ]
        else:
            refs = [
                (ref_key, ref_field, ref_field)
                for ref_key, ref_field in (s.get("input_refs") or [])
            ]

        for ref_key, ref_field, full_ref_path in refs:
            producer = by_key.get(ref_key)
            if producer is None:
                issues.append(LinkIssue("dangling_reference", key, f"references unknown step {ref_key!r}"))
                continue
            # A bare `${{ steps.X.result }}` ref (ref_field is None) takes the
            # whole upstream result — always producible, never a gap.
            if ref_field is None:
                continue
            producer_shape = producer.get("output_shape")
            producer_schema = producer.get("expected_output_schema")
            if not producer_shape and producer_schema is None:
                # Reading a specific field from a producer with no declared
                # output shape: the value isn't guaranteed producible. This is
                # the production ReferenceError class (e.g. reading `.content`
                # from a subagent step that was never bound to a shape).
                issues.append(LinkIssue(
                    "dangling_reference", key,
                    f"reads {ref_key}.{ref_field} but {ref_key} has no output shape",
                ))
                continue
            # Materialization gives canonical shapes precedence for free-form
            # agent steps (the shape is the explicit producer contract there),
            # but structured/action steps keep the provider schema attached to
            # the row.  Mirror that same precedence here so a hand-authored
            # action carrying both fields cannot link against a shape the
            # dispatcher will ignore.
            if producer_shape and producer.get("kind") in _LLM_KINDS:
                provided = _shape_top_level_keys(producer_shape)
            elif producer_schema is not None:
                provided = _schema_top_level_keys(producer_schema)
            else:
                provided = _shape_top_level_keys(producer_shape)
            if provided is None:
                # A named field must be statically linkable for every producer
                # kind. Treat composed/open schemas as unresolved rather than
                # silently allowing a field that the runtime value may not
                # contain. Whole-result refs (handled above) remain valid.
                issues.append(LinkIssue(
                    "dangling_reference", key,
                    f"reads {ref_key}.{ref_field} but {ref_key} has no "
                    "statically declared top-level fields",
                ))
                continue
            if ref_field not in provided:
                issues.append(LinkIssue(
                    "dangling_reference", key,
                    f"reads {ref_key}.{ref_field} but shape provides {sorted(provided)}",
                ))
                continue

            if full_ref_path and full_ref_path != ref_field:
                if producer_shape and producer.get("kind") in _LLM_KINDS:
                    try:
                        path_schema = get_shape(producer_shape).json_schema()
                    except KeyError:
                        path_schema = None
                else:
                    path_schema = producer_schema
                path_ok = known_schema_path(
                    path_schema,
                    full_ref_path,
                    require_required=not (
                        producer_shape and producer.get("kind") in _LLM_KINDS
                    ),
                )
                if path_ok is not True:
                    issues.append(LinkIssue(
                        "dangling_reference", key,
                        f"reads {ref_key}.{full_ref_path} but the producer schema "
                        "does not guarantee that nested path",
                    ))

    return issues


def repair_plan(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[LinkIssue]]:
    repaired = [dict(s) for s in steps]

    # Pass 1: infer missing output shapes from how downstream steps reference them.
    for s in repaired:
        if (
            s.get("output_shape")
            or s.get("expected_output_schema") is not None
            or s.get("kind") not in _LLM_KINDS
        ):
            continue
        wanted_fields = [
            field
            for other in repaired
            for (rk, field) in (other.get("input_refs") or [])
            if rk == s.get("key")
        ]
        for field in wanted_fields:
            shape = _FIELD_TO_SHAPE.get(field)
            if shape:
                s["output_shape"] = shape
                break
        else:
            # No specialized shape inferable — default to the StepResult
            # envelope so free-form llm/subagent steps are always shaped
            # (no missing_output_shape gap, no planner-guessed schema).
            s["output_shape"] = _DEFAULT_LLM_SHAPE

    # Re-validate; report what remains unfixed.
    remaining = lint_plan(repaired)
    return repaired, remaining
