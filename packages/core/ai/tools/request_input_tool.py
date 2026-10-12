"""``request_input`` — let an agent stop mid-run and ask the user something.

Three delivery modes, chosen by the caller's runtime, never by the model:

* **Suspend.** The loop can park the turn on a checkpoint (a durable chat run,
  a plan subagent step): the handler returns a ``waiting_human``
  :class:`RuntimeToolSuspension`; the human's answer is later appended as THIS
  tool call's result and the model continues where it stopped.
* **End the turn.** The legacy task loop parks the task on the marker result
  and re-runs it with the question + answer in context.
* **Unavailable.** Anywhere else the runtime cannot pause for a human. The
  tool says so instead of pretending: the model should ask in its reply when
  it is talking to the user, or proceed on a stated assumption.

The payload shaping and result formats live in
``packages.core.ai.runtime.human_input`` so services never import this module.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeToolSuspension,
)
from packages.core.ai.runtime.human_input import (
    HUMAN_INPUT_MODE_END_TURN,
    HUMAN_INPUT_MODE_SUSPEND,
    REQUEST_INPUT_DEADLINE_HOURS,
    REQUEST_INPUT_JUSTIFICATION_SCHEMA,
    REQUEST_INPUT_RESULT_KEY,
    REQUEST_INPUT_TOOL_NAME,
    human_input_answer_result,
    human_input_mode_from_kwargs,
    input_request_from_tool_result,
    normalize_input_request,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.tool_input_validation import (
    RuntimeToolInputValidationFailure,
    validate_runtime_tool_arguments,
)
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

_FIELD_TYPES = ("boolean", "date", "number", "select", "text", "textarea")


async def _request_input(
    question: str = "",
    options: list[str] | None = None,
    fields: list[dict[str, Any]] | None = None,
    context: str | None = None,
    justification: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    arguments: dict[str, Any] = {"question": question}
    for key, value in (("options", options), ("fields", fields), ("context", context), ("justification", justification)):
        if value is not None:
            arguments[key] = value
    invalid = validate_runtime_tool_arguments(arguments=arguments, tool_schema=REQUEST_INPUT_SCHEMA)
    if invalid is not None:
        return RuntimePreflightValidationError(invalid.to_tool_result(tool_name=REQUEST_INPUT_TOOL_NAME))
    try:
        payload = normalize_input_request(
            question=question, options=options, fields=fields, context=context, justification=justification,
        )
    except ValueError as exc:
        return RuntimePreflightValidationError(RuntimeToolInputValidationFailure(
            code="tool_input_validation_failed", message=str(exc), path="$.question",
            schema_rule="minLength", expected="non-empty question",
        ).to_tool_result(tool_name=REQUEST_INPUT_TOOL_NAME))

    mode = human_input_mode_from_kwargs(kwargs)
    if mode == HUMAN_INPUT_MODE_SUSPEND:
        deadline = datetime.now(timezone.utc) + timedelta(hours=REQUEST_INPUT_DEADLINE_HOURS)
        return RuntimeToolSuspension(
            kind=HUMAN_INPUT_SUSPENSION_KIND,
            reservation_id="",
            poll_after_seconds=5,
            deadline_at=deadline,
            reason=HUMAN_INPUT_SUSPENSION_REASON,
            payload=payload,
        )
    if mode == HUMAN_INPUT_MODE_END_TURN:
        return json.dumps(
            {
                "ok": True,
                "status": HUMAN_INPUT_SUSPENSION_REASON,
                REQUEST_INPUT_RESULT_KEY: payload,
                "instruction": (
                    "The question has been sent to the user and this run will pause "
                    "until they answer. Stop calling tools now: end your reply by "
                    "restating the question in one short paragraph and wait."
                ),
            },
            ensure_ascii=False,
        )
    # No question was delivered and no effect occurred. Let the loop ask in
    # ordinary prose without relaxing failure handling for other serial tools.
    return RuntimePreflightValidationError(
        json.dumps(
            tool_error_payload(
                tool_name=REQUEST_INPUT_TOOL_NAME, code="human_input_unavailable",
                message="This runtime cannot pause for a user answer; no question was delivered.",
                details={
                    "question": payload["question"],
                    "instruction": (
                        "This runtime cannot pause for a user answer. If you are replying "
                        "to the user directly, ask the question in your reply and end your "
                        "turn — their next message is the answer. Otherwise proceed with "
                        "your best assumption and state it explicitly in your result."
                    ),
                },
            ),
            ensure_ascii=False,
        )
    )


REQUEST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REQUEST_INPUT_TOOL_NAME,
        "description": (
            "Ask for user-only facts, material, or decisions; resume with the answer. "
            "Ask only when it changes work; batch questions in `fields`. Check context "
            "and authorized tools first; infer what you can. Include `justification`: human-only reason and "
            "checks actually made. Handle tool errors, execution checks, file writes, and "
            "acceptance validation yourself. For `file_review`, cite exact existing "
            "`file_refs` and ask for human judgment, not write verification. Rationale "
            "does not prove execution or authorize action. For a missing tool, first use "
            "`search_tools`, then any capability acquisition search/prepare/apply flow. "
            "If still unbound, name the exact tool and purpose. Request a "
            "user-reviewed update to this Agent's configuration or its deployment "
            "binding in the current Workspace. Connect any required account separately. "
            "A bare yes to this input question cannot create a binding; use the "
            "typed configuration review and confirmation flow."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, in the user's language, self-contained.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short list of choices to pick from.",
                },
                "fields": {
                    "type": "array",
                    "description": "Optional structured fields to collect in one go.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": list(_FIELD_TYPES)},
                            "required": {"type": "boolean"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": "One or two sentences on why you need this and what you have so far.",
                },
                "justification": REQUEST_INPUT_JUSTIFICATION_SCHEMA,
            },
            "required": ["question", "justification"],
        },
    },
}


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(REQUEST_INPUT_SCHEMA, _request_input)]


__all__ = [
    "REQUEST_INPUT_RESULT_KEY",
    "REQUEST_INPUT_SCHEMA",
    "REQUEST_INPUT_TOOL_NAME",
    "get_tools",
    "human_input_answer_result",
    "input_request_from_tool_result",
    "normalize_input_request",
]
