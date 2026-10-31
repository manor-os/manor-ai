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
    HUMAN_INPUT_MODE_SUSPEND,
    INPUT_FIELD_TYPES,
    REQUEST_INPUT_DEADLINE_HOURS,
    REQUEST_INPUT_JUSTIFICATION_SCHEMA,
    REQUEST_INPUT_TOOL_NAME,
    human_input_answer_result,
    human_input_mode_from_kwargs,
    normalize_input_request,
    runtime_request_input_refusal,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.tool_input_validation import (
    RuntimeToolInputValidationFailure,
    validate_runtime_tool_arguments,
)
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError



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
    # The agentic loop already ran the full gate with its run's evidence; this
    # handler also serves loops that call the registry directly.
    refusal = runtime_request_input_refusal(arguments)
    if refusal is not None:
        return RuntimePreflightValidationError(refusal)
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
            "Ask only when it changes work; batch questions in `fields`; documents go in a "
            "`file` field. Check context "
            "and authorized tools first; infer what you can. Include `justification`: human-only reason and "
            "checks made. Handle tool errors, execution checks, file writes, and "
            "acceptance validation yourself. For `file_review`, cite exact existing "
            "`file_refs` and ask for human judgment, not write verification. Rationale "
            "does not prove execution or authorize action. Never ask for tool access or "
            "Agent configuration; a person's yes cannot create a binding. For a missing "
            "tool, use `search_tools`, then `acquire_capabilities` when offered; if "
            "acquisition fails or is unavailable, stop as blocked naming the capability "
            "and purpose (in a Task, complete_task(outcome=blocked)). For tools, only "
            "ask to connect an external account."
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
                    "description": "Choices to pick from.",
                },
                "fields": {
                    "type": "array",
                    "description": "Structured fields to collect at once.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": list(INPUT_FIELD_TYPES)},
                            "required": {"type": "boolean"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": "Why you ask and what you have so far, briefly.",
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
    "REQUEST_INPUT_SCHEMA",
    "REQUEST_INPUT_TOOL_NAME",
    "get_tools",
    "human_input_answer_result",
    "normalize_input_request",
]
