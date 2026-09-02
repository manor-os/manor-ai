"""JSON Schema validation at the lease boundary.

Two checkpoints:

  validate_step_input(step, resolved_params)
      Called by checkout — if the Planner declared an
      ``expected_input_schema`` on the step, the resolved params (after
      ${{ steps.X.result }} interpolation) must conform. Catches
      Planner mistakes before a worker burns time.

  validate_step_output(step, result)
      Called by complete_lease — if the step has an
      ``expected_output_schema``, the worker's result must conform.
      Catches adapter regressions / LLM hallucinations.

Both are optional: a step with no schema passes silently. The plan
designer (Planner LLM, by default) decides where to enforce shape.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from packages.core.contracts.json_schema import (
    SchemaContractError,
    SchemaContractValidatorFactory,
)
from packages.core.contracts.task_output import (
    output_contract_for_schema,
)
from packages.core.models.execution import ExecutionStep

logger = logging.getLogger(__name__)


class SchemaError(Exception):
    """Step input or output failed its declared JSON Schema."""

    def __init__(self, message: str, *, errors: list[dict]):
        super().__init__(message)
        self.errors = errors


def validate_step_input(
    step: ExecutionStep,
    resolved_params: dict[str, Any],
) -> None:
    """Raise SchemaError if step.expected_input_schema is set and the
    resolved params don't match. No-op when no schema."""
    schema = step.expected_input_schema
    if schema is None:
        return
    _check(schema, resolved_params, side="input", step=step)


def validate_step_output(
    step: ExecutionStep,
    result: Any,
) -> None:
    """Raise SchemaError if step.expected_output_schema is set and the
    result doesn't match. No-op when no schema."""
    schema = step.expected_output_schema
    if schema is None:
        return
    _check(schema, result, side="output", step=step)


# Already-materialized legacy llm/subagent steps can carry unmarked guessed
# schemas that don't match their open-ended output. Those remain advisory so an
# upgrade cannot strand in-flight work. New PlanStep output contracts and
# Task.expected_output schemas are provenance-marked hard contracts; structured
# action/code schemas also remain hard contracts with external systems.
_ADVISORY_OUTPUT_SCHEMA_KINDS = ("llm", "subagent")


def output_schema_is_advisory(
    step_kind: str | None,
    schema: dict[str, Any] | None = None,
) -> bool:
    """Whether an output-schema mismatch should be advisory (warn + accept)
    rather than a hard step failure.

    Unmarked schemas on already-materialized legacy free-form steps stay
    advisory. New plans mark exact or canonical PlanStep output schemas, and
    task deliverables come from Task.expected_output; both are declared before
    execution and must fail/retry before the step can be marked done.
    """
    if output_contract_for_schema(schema).is_hard:
        return False
    return str(step_kind or "").strip().lower() in _ADVISORY_OUTPUT_SCHEMA_KINDS


def _check(
    schema: dict, value: Any, *, side: str, step: ExecutionStep,
) -> None:
    try:
        validator = SchemaContractValidatorFactory.build(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    except SchemaContractError as exc:
        # A malformed/dangling contract is a planner bug. Surface it as a
        # controlled schema failure instead of allowing jsonschema's resolver
        # exception to escape complete_lease and crash the worker tick.
        raise SchemaError(
            f"step {step.step_key}: invalid {side} contract ({exc})",
            errors=[{"path": "$", "message": str(exc)}],
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Resolver and validator implementations can raise on malformed
        # composition during iter_errors; keep the lease boundary controlled.
        raise SchemaError(
            f"step {step.step_key}: {side} validation could not run ({exc})",
            errors=[{"path": "$", "message": str(exc)}],
        ) from exc
    if not errors:
        return

    summary = [
        {
            "path": "$" + "".join(f"[{p!r}]" for p in e.path),
            "message": e.message,
        }
        for e in errors[:10]
    ]
    msg = (
        f"step {step.step_key}: {side} validation failed "
        f"({len(errors)} error(s))"
    )
    raise SchemaError(msg, errors=summary)


def maybe_collect_errors(
    schema: Optional[dict], value: Any,
) -> list[dict]:
    """Soft variant: returns the same error list a SchemaError would
    carry, without raising. Useful for the Planner's self-check pass."""
    if schema is None:
        return []
    try:
        validator = SchemaContractValidatorFactory.build(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    except SchemaContractError as exc:
        return [{"path": "$", "message": str(exc)}]
    except Exception as exc:  # noqa: BLE001 - soft mirror of the hard boundary
        return [{"path": "$", "message": f"validation could not run ({exc})"}]
    return [
        {"path": "$" + "".join(f"[{p!r}]" for p in e.path), "message": e.message}
        for e in errors[:10]
    ]
