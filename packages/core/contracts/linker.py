"""Plan-time contract linker: validate step I/O before execution.

Catches the two root causes of runtime step failures before a plan runs:
OutputSchemaError (a step has no declared output shape) and ReferenceError
(a step reads an upstream key the upstream shape never produces).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.contracts.shapes import get_shape

_LLM_KINDS = {"llm", "subagent"}

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


def _shape_top_level_keys(shape_name: str | None) -> set[str]:
    if not shape_name:
        return set()
    try:
        schema = get_shape(shape_name).json_schema()
    except KeyError:
        return set()
    return set((schema.get("properties") or {}).keys())


def lint_plan(steps: list[dict[str, Any]]) -> list[LinkIssue]:
    issues: list[LinkIssue] = []
    by_key = {s.get("key"): s for s in steps}

    for s in steps:
        key = s.get("key")
        kind = s.get("kind")
        shape = s.get("output_shape")

        if kind in _LLM_KINDS and not shape:
            issues.append(LinkIssue("missing_output_shape", key, f"{kind} step has no output_shape"))

        for ref_key, ref_field in s.get("input_refs") or []:
            producer = by_key.get(ref_key)
            if producer is None:
                issues.append(LinkIssue("dangling_reference", key, f"references unknown step {ref_key!r}"))
                continue
            # A bare `${{ steps.X.result }}` ref (ref_field is None) takes the
            # whole upstream result — always producible, never a gap.
            if ref_field is None:
                continue
            producer_shape = producer.get("output_shape")
            if not producer_shape:
                # Reading a specific field from a producer with no declared
                # output shape: the value isn't guaranteed producible. This is
                # the production ReferenceError class (e.g. reading `.content`
                # from a subagent step that was never bound to a shape).
                issues.append(LinkIssue(
                    "dangling_reference", key,
                    f"reads {ref_key}.{ref_field} but {ref_key} has no output shape",
                ))
                continue
            provided = _shape_top_level_keys(producer_shape)
            if provided and ref_field not in provided:
                issues.append(LinkIssue(
                    "dangling_reference", key,
                    f"reads {ref_key}.{ref_field} but shape provides {sorted(provided)}",
                ))

    return issues


def repair_plan(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[LinkIssue]]:
    repaired = [dict(s) for s in steps]

    # Pass 1: infer missing output shapes from how downstream steps reference them.
    for s in repaired:
        if s.get("output_shape") or s.get("kind") not in _LLM_KINDS:
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

    # Re-validate; report what remains unfixed.
    remaining = lint_plan(repaired)
    return repaired, remaining
