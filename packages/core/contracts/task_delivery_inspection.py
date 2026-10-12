"""Read-only Task delivery page addresses, distinct from citation pointers.

The provider schema and host validation describe the same request. Rejected
arguments are never rewritten into a different inspection or execution action.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DELIVERY_INSPECTION_KINDS = (
    "manifest", "request", "criterion", "step", "artifact", "citations",
    "evidence", "verification", "contract",
)
DELIVERY_INSPECTION_PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string", "enum": list(DELIVERY_INSPECTION_KINDS),
            "description": (
                "Page kind; omitted means manifest. manifest lists available pages; "
                "request reads the original Task; criterion, step, artifact, citations, "
                "verification and contract select their catalog index. Only evidence "
                "uses a citation pointer to read a scalar value."
            ),
        },
        "index": {
            "type": "integer", "minimum": 0,
            "description": (
                "Zero-based catalog index for criterion, step, artifact, citations, "
                "verification or contract. Omit or use 0 for manifest, request and "
                "evidence. Omitted means 0."
            ),
        },
        "offset": {
            "type": "integer", "minimum": 0,
            "description": (
                "Page offset; start at 0, then use the returned next_offset exactly. "
                "manifest/citations count entries; other kinds count characters in "
                "their returned content. Omitted means 0."
            ),
        },
        "pointer": {
            "type": ["string", "null"],
            "description": (
                "Required nonempty canonical citation_pointer only for kind=evidence. "
                "For every other kind omit this field or use null. A result_pointer, "
                "source_pointer or citation_pointer in a response is not a page "
                "address and must not be copied into a manifest/step/artifact request."
            ),
        },
    },
    # Keep a flat object: native Anthropic rejects top-level combinators.
    # Conditional field rules are enforced below before any snapshot reads.
    "additionalProperties": False,
}


class DeliveryInspectionError(ValueError):
    """A request constraint, not a failed writer or missing business input."""

    code = "invalid_inspection"

    def __init__(self, message: str, *, path: str, schema_rule: str, expected: Any) -> None:
        super().__init__(message)
        self.details = {"path": path, "schema_rule": schema_rule, "expected": expected}

    def to_tool_result(self) -> str:
        from packages.core.ai.runtime.tool_error_contract import tool_error_payload

        return json.dumps(tool_error_payload(
            tool_name="inspect_task_delivery", code=self.code,
            message=str(self), details=self.details,
        ), ensure_ascii=False)


@dataclass(frozen=True)
class DeliveryInspectionRequest:
    kind: str = "manifest"
    index: int = 0
    offset: int = 0
    pointer: str | None = None

    def as_arguments(self) -> dict[str, Any]:
        """An explicit page address, never a queued or authorized tool call."""
        return {
            "kind": self.kind, "index": self.index,
            "offset": self.offset, "pointer": self.pointer,
        }


def parse_delivery_inspection(arguments: Any) -> DeliveryInspectionRequest:
    """Apply declared defaults only; never coerce or discard invalid arguments."""
    from packages.core.ai.runtime.tool_input_validation import validate_runtime_tool_arguments

    if isinstance(arguments, dict) and set(arguments) - DELIVERY_INSPECTION_PARAMETERS["properties"].keys():
        raise DeliveryInspectionError(
            "Inspection accepts only kind, index, offset and pointer.",
            path="$", schema_rule="additionalProperties",
            expected={"allowed_fields": list(DELIVERY_INSPECTION_PARAMETERS["properties"])},
        )
    failure = validate_runtime_tool_arguments(
        arguments=arguments, tool_schema=DELIVERY_INSPECTION_PARAMETERS,
    )
    if failure is not None:
        raise DeliveryInspectionError(
            failure.message, path=failure.path,
            schema_rule=failure.schema_rule or "schema", expected=failure.expected,
        )
    for key in ("index", "offset"):
        # JSON Schema considers 0.0 an integer; Python sequence addressing does
        # not. Keep the host contract strict instead of coercing a page index.
        if key in arguments and type(arguments[key]) is not int:
            raise DeliveryInspectionError(
                f"Inspection {key} must be a nonnegative integer without coercion.",
                path=f"$.{key}", schema_rule="type", expected="integer",
            )
    request = DeliveryInspectionRequest(**arguments)
    if request.kind in {"manifest", "request", "evidence"} and request.index != 0:
        raise DeliveryInspectionError(
            f"{request.kind} inspection does not use a catalog index; omit index or use 0.",
            path="$.index", schema_rule="const", expected=0,
        )
    if request.kind == "evidence":
        if not isinstance(request.pointer, str) or not request.pointer:
            raise DeliveryInspectionError(
                "evidence inspection needs a nonempty canonical citation_pointer.",
                path="$.pointer", schema_rule="required_for_kind",
                expected={"kind": "evidence", "type": "string", "minLength": 1},
            )
    elif request.pointer is not None:
        raise DeliveryInspectionError(
            "pointer is only valid for evidence inspection; for this page kind omit pointer or use null.",
            path="$.pointer", schema_rule="forbidden_for_kind",
            expected={"kind": request.kind, "pointer": None},
        )
    return request
