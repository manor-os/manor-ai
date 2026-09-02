"""Deterministic validation for public Runtime tool arguments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


_RUNTIME_CONTROL_ARGUMENTS = frozenset({"approval_token"})


@dataclass(frozen=True, slots=True)
class RuntimeToolInputValidationFailure:
    code: str
    message: str
    path: str
    schema_rule: str | None = None

    def to_tool_result(self, *, tool_name: str) -> str:
        return json.dumps(
            {
                "error": self.code,
                "message": self.message,
                "tool": tool_name,
                "path": self.path,
                "schema_rule": self.schema_rule,
                "retryable": True,
            },
            ensure_ascii=False,
        )


def runtime_tool_parameters_schema(
    tool_schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract the JSON Schema instance contract from a registered tool schema."""

    if not isinstance(tool_schema, dict):
        return None
    function = tool_schema.get("function")
    if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
        return function["parameters"]
    for key in ("inputSchema", "input_schema", "parameters"):
        value = tool_schema.get(key)
        if isinstance(value, dict):
            return value
    if any(key in tool_schema for key in ("type", "properties", "$ref", "allOf", "oneOf", "anyOf")):
        return tool_schema
    return None


def _json_path(error: ValidationError) -> str:
    path = "$"
    for item in error.absolute_path:
        if isinstance(item, int):
            path += f"[{item}]"
        else:
            key = str(item)
            if key.isidentifier():
                path += f".{key}"
            else:
                path += f"[{json.dumps(key, ensure_ascii=False)}]"
    return path


def _instance_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _validation_message(error: ValidationError, path: str) -> str:
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected_text = " or ".join(str(item) for item in expected)
        else:
            expected_text = str(expected)
        return (
            f"Tool input at {path} must be {expected_text}; "
            f"received {_instance_type(error.instance)}."
        )
    if error.validator == "required":
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = [str(key) for key in required if key not in error.instance]
        field = missing[0] if missing else "required field"
        return f"Tool input is missing required field {field!r} at {path}."
    return f"Tool input at {path} does not satisfy schema rule {error.validator!r}."


def validate_runtime_tool_arguments(
    *,
    arguments: dict[str, Any],
    tool_schema: dict[str, Any] | None,
) -> RuntimeToolInputValidationFailure | None:
    """Validate model-authored arguments before authorization or approval.

    Runtime-only control fields are removed from the instance because they are
    not part of the public tool contract and may be added only after approval.
    Values are never echoed into the failure result.
    """

    parameters = runtime_tool_parameters_schema(tool_schema)
    if parameters is None:
        return RuntimeToolInputValidationFailure(
            code="tool_schema_unavailable",
            message="The runtime could not resolve this tool's input schema.",
            path="$",
        )
    public_arguments = {
        str(key): value
        for key, value in arguments.items()
        if str(key) not in _RUNTIME_CONTROL_ARGUMENTS
    }
    try:
        Draft202012Validator.check_schema(parameters)
        validator = Draft202012Validator(parameters)
        error = next(
            iter(
                sorted(
                    validator.iter_errors(public_arguments),
                    key=lambda item: (
                        tuple(str(part) for part in item.absolute_path),
                        str(item.validator or ""),
                        item.message,
                    ),
                )
            ),
            None,
        )
    except SchemaError:
        return RuntimeToolInputValidationFailure(
            code="tool_schema_invalid",
            message="The registered tool input schema is invalid.",
            path="$",
        )
    except Exception:
        return RuntimeToolInputValidationFailure(
            code="tool_input_validation_failed",
            message="The tool input could not be validated safely.",
            path="$",
        )
    if error is None:
        return None
    path = _json_path(error)
    return RuntimeToolInputValidationFailure(
        code="tool_input_validation_failed",
        message=_validation_message(error, path),
        path=path,
        schema_rule=str(error.validator or "") or None,
    )


def runtime_tool_input_validation_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_schema: dict[str, Any] | None,
) -> str | None:
    failure = validate_runtime_tool_arguments(
        arguments=arguments,
        tool_schema=tool_schema,
    )
    return failure.to_tool_result(tool_name=tool_name) if failure else None
