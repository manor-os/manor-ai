"""Host-owned final-response validation, not another agent or execution owner."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator


def structured_response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Constrain each reply variant without an unsupported root-level union.

    The transport envelope is removed before host validation. Provider schemas
    require explicit claims (empty array when absent) and revisions (empty
    string when unknown); the host still verifies evidence and string bounds.
    """

    def portable(value: Any) -> Any:
        if isinstance(value, dict):
            result = {
                ("anyOf" if key == "oneOf" else key): portable(child)
                for key, child in value.items()
                if key not in {"minLength", "maxLength", "minItems", "maxItems"}
            }
            if "const" in result:
                result["enum"] = [result.pop("const")]
            if "enum" in result:
                result.setdefault("type", "string")
            if result.get("type") == "object":
                result["required"] = list(result["properties"])
                result["additionalProperties"] = False
            return result
        if isinstance(value, list):
            return [portable(child) for child in value]
        return value

    return {"type": "json_schema", "json_schema": {
        "name": name, "strict": True, "schema": {
            "type": "object", "additionalProperties": False,
            "required": ["response"], "properties": {"response": portable(schema)},
        },
    }}


STRUCTURED_RESPONSE_INSTRUCTIONS = """<response-transport>
Wrap the reply object described above inside {"response": ...}, including a
progress object when calling tools. This is an internal transport envelope;
only the answer/progress text is shown to the user. Include every field in the
response schema: use claims=[] when there are no operation claims, and use
revision="" when a Task revision has not yet been observed. A missing revision
requires host verification; never invent one. Return no surrounding prose.
</response-transport>"""


PROGRESS_RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "text"],
    "properties": {"kind": {"const": "progress"}, "text": {"type": "string"}},
}


def unwrap_structured_response(candidate: Any) -> Any:
    """Accept the provider envelope and legacy candidates, never mixed fields."""
    if isinstance(candidate, dict) and set(candidate) == {"response"}:
        return candidate["response"]
    return candidate


class _DuplicateJSONField(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONField("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def parse_response_object(content: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return bounded parse diagnostics without retaining rejected reply text."""
    if not isinstance(content, str):
        return None, {"reason": "invalid_reply_type", "actual_type": type(content).__name__}
    if len(content) > 131072:
        return None, {"reason": "reply_too_large", "actual_chars": len(content), "max_chars": 131072}
    try:
        candidate = unwrap_structured_response(json.loads(
            content, object_pairs_hook=_strict_object, parse_constant=_reject_json_constant,
        ))
    except json.JSONDecodeError as exc:
        return None, {"reason": "invalid_json", "message": exc.msg,
                      "line": exc.lineno, "column": exc.colno, "offset": exc.pos}
    except _DuplicateJSONField:
        return None, {"reason": "duplicate_json_field", "message": "duplicate JSON field"}
    except ValueError:
        return None, {"reason": "invalid_json_value", "message": "unsupported JSON value"}
    except RecursionError:
        return None, {"reason": "json_too_deep", "message": "JSON nesting exceeds parser limits"}
    if not isinstance(candidate, dict):
        return None, {"reason": "json_object_required", "actual_type": type(candidate).__name__}
    return candidate, None


def response_schema_error(candidate: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a rejected field using schema-owned names, never reply values."""
    variants = schema.get("oneOf", schema.get("anyOf"))
    if variants:
        # Validate the selected reply kind, not unrelated branches of a union.
        kinds = [branch["properties"]["kind"]["const"] for branch in variants]
        schema = next((branch for kind, branch in zip(kinds, variants)
                       if candidate.get("kind") == kind), {
            "required": ["kind"], "properties": {"kind": {"enum": kinds}},
        })
    error = next(Draft202012Validator(schema).iter_errors(candidate), None)
    if error is None:
        return None
    diagnostic = {"reason": "schema_validation_failed", "path": list(error.path),
                  "rule": error.validator}
    if error.validator == "required":
        diagnostic["missing_fields"] = [key for key in error.validator_value if key not in error.instance]
    elif error.validator == "additionalProperties":
        diagnostic["unexpected_field_count"] = len(set(error.instance) - set(error.schema.get("properties", {})))
    elif error.validator == "type":
        diagnostic.update(expected=error.validator_value, actual_type=type(error.instance).__name__)
    elif error.validator in {"enum", "const", "minItems", "maxItems", "minLength", "maxLength"}:
        diagnostic["expected"] = error.validator_value
    return diagnostic


class FinalResponseAction(StrEnum):
    ACCEPT = "accept"
    REVISE_REPLY = "revise_reply"
    CONTINUE_EXECUTION = "continue_execution"


@dataclass(frozen=True)
class FinalResponseContext:
    """Host execution limits; model output cannot reopen a terminal lane."""

    allow_execution: bool = True
    stop_reason: str | None = None
    # The round budget ran out and the host forced a tool-less summary; a
    # reply the model chose to give on its last round is not that.
    budget_exhausted: bool = False


@dataclass(frozen=True)
class FinalResponseDecision:
    action: FinalResponseAction
    content: str = ""
    diagnostic: dict[str, Any] | None = None
    evidence: tuple[dict[str, Any], ...] = ()
    outcome: dict[str, Any] | None = None
    stop_reason: str | None = None


class RuntimeFinalResponseContract(Protocol):
    """A caller-owned contract; it never grants tools or changes their scope."""

    instructions: str
    response_format: dict[str, Any] | None

    async def validate(self, content: str) -> FinalResponseDecision: ...


async def validate_final_response(
    contract: RuntimeFinalResponseContract, content: str, context: FinalResponseContext,
) -> FinalResponseDecision:
    """Keep existing caller contracts compatible with optional host context."""
    contextual_validate = getattr(contract, "validate_with_context", None)
    if callable(contextual_validate):
        return await contextual_validate(content, context)
    return await contract.validate(content)
