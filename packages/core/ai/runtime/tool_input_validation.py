"""Deterministic validation for public Runtime tool arguments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.services.sensitive_data import (
    is_sensitive_key,
    sanitize_approval_credentials,
    sanitize_sensitive_payload,
)

# Runtime Harness attaches trusted execution context to the same mapping that
# carries model-authored arguments before ToolPool dispatch.  Those fields are
# not part of the public JSON schema and must never be validated as if the
# model supplied them.  Keeping the exclusion list sourced from tool_context
# also prevents new workflow/runtime fields from silently breaking every
# strict ``additionalProperties: false`` tool (notably ``search_tools``).
_RUNTIME_CONTROL_ARGUMENTS = frozenset({"approval_token"}) | RUNTIME_TOOL_CONTEXT_KEYS


@dataclass(frozen=True, slots=True)
class RuntimeToolInputValidationFailure:
    code: str
    message: str
    path: str
    schema_rule: str | None = None
    expected: Any = None
    line: int | None = None
    column: int | None = None
    parser_reason: str | None = None

    def to_tool_result(self, *, tool_name: str) -> str:
        details = {
            "path": self.path,
            "schema_rule": self.schema_rule,
        }
        for key in ("expected", "line", "column", "parser_reason"):
            value = getattr(self, key)
            if value is not None:
                details[key] = value
        payload = tool_error_payload(
            tool_name=tool_name, code=self.code, message=self.message, details=details,
        )
        return json.dumps(
            sanitize_sensitive_payload(sanitize_approval_credentials(payload)),
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
        if len(missing) > 1:
            fields = ", ".join(repr(key) for key in missing[:30])
            return f"Tool input is missing required fields {fields} at {path}."
        field = missing[0] if missing else "required field"
        return f"Tool input is missing required field {field!r} at {path}."
    if error.validator == "not" and "propertyNames" in error.absolute_schema_path:
        forbidden = error.validator_value.get("enum") if isinstance(error.validator_value, dict) else None
        if isinstance(forbidden, list):
            fields = ", ".join(repr(key) for key in forbidden[:30])
            return f"Tool input at {path} cannot include these fields for this operation: {fields}."
    if error.validator in {"anyOf", "oneOf"} and error.context:
        # Explain alternatives only when every branch failed on required
        # fields. Do not misdiagnose type/value failures or echo input values.
        if all(child.validator == "required" for child in error.context):
            alternatives: dict[int, list[str]] = {}
            for child in error.context:
                branch = next(iter(child.schema_path), 0)
                missing = [str(key) for key in child.validator_value if key not in child.instance]
                alternatives.setdefault(branch, []).extend(missing)
            choices = [
                " + ".join(repr(key) for key in dict.fromkeys(fields))
                for fields in alternatives.values()
            ]
            return (
                f"Tool input at {path} requires one of these field sets: "
                + " or ".join(choices[:6]) + "."
            )
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        schema = error.schema if isinstance(error.schema, dict) else {}
        properties = schema.get("properties")
        declared = set(properties) if isinstance(properties, dict) else set()
        pattern_properties = schema.get("patternProperties")
        patterns = (
            tuple(pattern_properties)
            if isinstance(pattern_properties, dict)
            else ()
        )
        unsupported = sorted(
            str(key)
            for key in error.instance
            if key not in declared
            and not any(re.search(pattern, str(key)) for pattern in patterns)
        )
        if unsupported:
            fields = ", ".join(repr(key) for key in unsupported)
            return f"Tool input has unsupported fields {fields} at {path}."
    return f"Tool input at {path} does not satisfy schema rule {error.validator!r}."


def _workspace_operation_patch_failure(
    arguments: dict[str, Any], tool_schema: dict[str, Any],
) -> RuntimeToolInputValidationFailure | None:
    function = tool_schema.get("function")
    name = str(function.get("name") or "") if isinstance(function, dict) else ""
    if name not in {"manor", "workspace_agent", "workspace_operation"}:
        return None

    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
    from packages.core.ai.runtime.workspace_operation_actions import (
        _resolve_workspace_operation_action,
        _resolve_workspace_operation_patches,
        supported_operation_patch_ops,
    )

    call = RuntimeCompositeToolCallFactory.create(name, arguments)
    if call.tool_name == "workspace_operation":
        params = call.arguments
        base_path = "$"
    elif call.tool_name == "workspace_agent" and call.arguments.get("action") == "operation":
        params = call.arguments.get("params")
        if not isinstance(params, dict):
            return None  # The public schema reports this type error.
        nested = arguments.get("params")
        base_path = (
            "$.params.params"
            if name == "manor" and isinstance(nested, dict) and isinstance(nested.get("params"), dict)
            else "$.params"
        )
    else:
        return None

    patch_key = next((key for key in ("patches", "operations", "ops") if key in params), None)
    action = _resolve_workspace_operation_action(params)
    if patch_key is None:
        if action != "create_draft":
            return None
        return RuntimeToolInputValidationFailure(
            "tool_input_validation_failed",
            "Creating a Workspace operation draft requires nonempty patches.",
            base_path,
            "required",
        )

    path = f"{base_path}.{patch_key}"
    patches = _resolve_workspace_operation_patches(params)
    if patches is None or (action == "create_draft" and not patches):
        return RuntimeToolInputValidationFailure(
            "tool_input_validation_failed",
            "Workspace operation patches must be a nonempty array of patch objects.",
            path,
            "type" if patches is None else "minItems",
        )

    from packages.core.services.workspace_operation_service import operation_patch_payload_schema

    supported = supported_operation_patch_ops()
    for index, patch in enumerate(patches):
        entry_path = f"{path}[{index}]" if isinstance(params[patch_key], list) else path
        op_key = next((key for key in ("op", "operation", "type") if patch.get(key)), "op")
        op = patch.get(op_key)
        op_path = f"{entry_path}.{op_key}"
        if not isinstance(op, str) or not op.strip():
            return RuntimeToolInputValidationFailure(
                "tool_input_validation_failed",
                "Workspace operation patch requires a supported op.",
                op_path,
                "required",
            )
        if op.strip() not in supported:
            return RuntimeToolInputValidationFailure(
                "tool_input_validation_failed",
                "Unsupported Workspace operation patch. Supported ops: " + ", ".join(supported) + ".",
                op_path,
                "enum",
            )
        if patch.get("payload") is not None and not isinstance(patch["payload"], dict):
            return RuntimeToolInputValidationFailure(
                "tool_input_validation_failed",
                "Workspace operation patch payload must be an object.",
                f"{entry_path}.payload",
                "type",
            )
        payload_schema = operation_patch_payload_schema(op.strip())
        if payload_schema is not None and "payload" in patch:
            error = next(Draft202012Validator(payload_schema).iter_errors(patch["payload"]), None)
            if error is not None:
                payload_path = f"{entry_path}.payload" + _json_path(error)[1:]
                return RuntimeToolInputValidationFailure(
                    "tool_input_validation_failed", _validation_message(error, payload_path),
                    payload_path, error.validator,
                )
    return None


def validate_runtime_tool_arguments(
    *,
    arguments: Any,
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
    try:
        Draft202012Validator.check_schema(parameters)
        if not isinstance(arguments, dict):
            return RuntimeToolInputValidationFailure(
                code="tool_input_validation_failed",
                message=(
                    "Tool arguments must be a JSON object, not "
                    f"{_instance_type(arguments)}. Nothing was executed. "
                    "Resend a complete JSON object matching the tool schema; "
                    "use {} only if the tool accepts no arguments."
                ),
                path="$", schema_rule="type", expected="object",
            )
        properties = parameters.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        # A context key the tool declares as its own input (get_task_details'
        # task_id) is the model's argument, not host context: stripping it
        # failed every such call as "missing task_id".
        public_arguments = {
            str(key): value for key, value in arguments.items()
            if str(key) not in _RUNTIME_CONTROL_ARGUMENTS or str(key) in properties
        }
        # Provider adapters preserve unparseable arguments in this envelope.
        # A declared _raw property remains ordinary tool input.
        if (
            set(public_arguments) == {"_raw"}
            and isinstance(public_arguments["_raw"], str)
            and "_raw" not in properties
        ):
            try:
                json.loads(public_arguments["_raw"])
            except json.JSONDecodeError as parse_error:
                fields = ", ".join(sorted(properties)[:30])
                return RuntimeToolInputValidationFailure(
                    code="tool_input_validation_failed",
                    message=(
                        "Tool arguments are not valid JSON "
                        f"(line {parse_error.lineno}, column {parse_error.colno}: "
                        f"{parse_error.msg}). Nothing was executed. "
                        "Resend a complete JSON object matching the tool schema, "
                        "not the internal '_raw' wrapper. Escape quotes, backslashes "
                        "and newlines inside string values. Preserve the operation "
                        "and all required fields; do not resend the same malformed payload."
                        + (f" Declared top-level fields: {fields}." if fields else "")
                    ),
                    path="$", schema_rule="json", expected="JSON object",
                    line=parse_error.lineno, column=parse_error.colno,
                    parser_reason=parse_error.msg,
                )
        workspace_failure = _workspace_operation_patch_failure(public_arguments, tool_schema or {})
        if workspace_failure is not None:
            return workspace_failure
        validator = Draft202012Validator(parameters)
        error = next(
            iter(
                sorted(
                    validator.iter_errors(public_arguments),
                    key=lambda item: (
                        tuple(str(part) for part in item.absolute_path),
                        item.validator != "required",
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
    expected = None
    if error.validator == "type":
        expected = error.validator_value
    elif (
        error.validator == "required"
        and isinstance(error.validator_value, list)
        and isinstance(error.instance, dict)
    ):
        expected = next(
            (str(key) for key in error.validator_value if key not in error.instance),
            "required field",
        )
    elif error.validator == "additionalProperties" and isinstance(error.schema, dict):
        properties = error.schema.get("properties")
        if isinstance(properties, dict):
            expected = sorted(str(key) for key in properties)[:30]
    elif error.validator in {
        "enum", "const", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems",
        "minProperties", "maxProperties", "multipleOf", "pattern", "format",
    }:
        sensitive_field = any(
            is_sensitive_key(part) or str(part).lower() in {"approval_token", "approvaltoken"}
            for part in error.absolute_path
        )
        if sensitive_field:
            expected = "<redacted>"
        elif error.validator == "enum" and isinstance(error.validator_value, list):
            expected = error.validator_value[:30]
        elif error.validator == "const" and not isinstance(error.validator_value, (str, int, float, bool, type(None))):
            expected = "declared constant value"
        else:
            expected = error.validator_value
    return RuntimeToolInputValidationFailure(
        code="tool_input_validation_failed",
        message=_validation_message(error, path),
        path=path,
        schema_rule=str(error.validator or "") or None,
        expected=expected,
    )


def runtime_tool_input_validation_result(
    *,
    tool_name: str,
    arguments: Any,
    tool_schema: dict[str, Any] | None,
) -> str | None:
    failure = validate_runtime_tool_arguments(
        arguments=arguments,
        tool_schema=tool_schema,
    )
    if failure is None:
        return None
    result = failure.to_tool_result(tool_name=tool_name)
    if failure.code == "tool_input_validation_failed" and failure.schema_rule:
        from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

        # Only a concrete, host-validated instance error permits same-loop
        # correction. Schema/configuration failures and provider JSON cannot
        # manufacture this proof; no approval or handler has run here.
        return RuntimePreflightValidationError(result)
    return result
