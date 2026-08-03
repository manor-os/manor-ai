"""Forced submit_result finalization for InternalWorker subagent steps.

Part ② of the StepResult envelope rollout (① = fixed envelope, PR #295/#296).

Instead of inferring a step's result from whatever text the model happened to
end on — and patching it up with heuristics — the subagent loop carries a
``submit_result`` tool and is instructed to END by calling it. The tool call
is the deliberate, structured hand-off:

  * the tool's ``result`` parameter carries the step's expected_output_schema
    (when the plan declared an object schema), so the model states the fields
    directly instead of prose the coercion layer must mine;
  * calling it TERMINATES the loop (terminal_tool_result_policy), so the
    model cannot submit and then wander;
  * the handler never rejects — malformed payloads degrade to a summary-only
    result and the envelope layer (contracts/envelope.py) still validates,
    preserving the "no OutputSchemaError" invariant of part ①;
  * if the loop ends WITHOUT a submit (model just stopped talking), the
    caller runs one cheap follow-up round whose ONLY tool is submit_result;
    if even that yields nothing, the legacy text-coercion path applies — so
    this is strictly additive, never a new failure mode.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from packages.core.ai.terminal_stops import register_terminal_success_stop_reason
from packages.core.contracts.envelope import (
    StepResultStatus,
    normalize_step_result_status,
)

SUBMIT_RESULT_TOOL_NAME = "submit_result"

# The loop's stop_reason when submit_result terminated it. Registered as a
# terminal-tool SUCCESS so stop_reason gates (workers/internal.py) never read
# a compliant hand-off as a failed run. Declared once, used everywhere.
SUBMIT_RESULT_STOP_REASON = "submit_result"
register_terminal_success_stop_reason(SUBMIT_RESULT_STOP_REASON)

# Loop-terminal rule: any submit_result call ends the loop.
SUBMIT_RESULT_TERMINAL_POLICY: dict[str, Any] = {
    "terminal_tool_results": [
        {
            "tool_names": [SUBMIT_RESULT_TOOL_NAME],
            "stop_reason": SUBMIT_RESULT_STOP_REASON,
            "notice": "Result submitted.",
            # The submitted payload is the deliverable; the terminal notice is
            # bookkeeping, not user-facing content to preserve.
            "replace_visible_text": False,
        }
    ]
}

SUBMIT_RESULT_PROMPT_SUFFIX = (
    "\n\nWhen the work is complete (or you cannot proceed), you MUST finish by "
    "calling the `submit_result` tool exactly once with your final outcome. "
    "Do not end with a plain text message."
)


def build_submit_result_tool(expected_output_schema: Optional[dict]) -> dict[str, Any]:
    """The submit_result tool schema, shaped by the step's expected output.

    ``result`` embeds the plan's expected_output_schema when it is an object
    schema so the model fills the declared fields directly; otherwise a free
    object. ``summary`` is the only required field — the envelope philosophy:
    never let schema strictness turn a finished piece of work into a failure.
    """
    result_schema: dict[str, Any] = {"type": "object"}
    if (
        isinstance(expected_output_schema, dict)
        and expected_output_schema.get("type") == "object"
        and isinstance(expected_output_schema.get("properties"), dict)
    ):
        result_schema = {
            "type": "object",
            "properties": expected_output_schema["properties"],
            # Deliberately no `required`: proven-but-missing fields are
            # backfilled by the tool-evidence mergers, and the envelope never
            # fails validation.
        }
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_RESULT_TOOL_NAME,
            "description": (
                "Submit the final result of this step and finish. Call this exactly "
                "once, at the end. `summary` is a short factual account of what was "
                "done and the outcome; put the deliverable's fields in `result`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One short paragraph: what was done and the outcome.",
                    },
                    "status": {
                        "type": "string",
                        # The canonical vocabulary — the same enum the envelope
                        # schema, the dispatcher success gate and the executor
                        # blocker check use. Advertising any other words here
                        # (this once offered done|partial|blocked) means the
                        # agent's answer is silently rewritten downstream.
                        "enum": list(StepResultStatus.values()),
                        "description": (
                            "succeeded = objective met; partial = some of it; "
                            "failed = could not proceed."
                        ),
                    },
                    "result": result_schema,
                },
                "required": ["summary"],
            },
        },
    }


def submit_result_capture() -> tuple[Callable[[dict[str, Any]], str], Callable[[], Optional[dict]]]:
    """A (handler, get_payload) pair.

    The handler is a runtime dynamic tool handler: it records the payload and
    acknowledges. It NEVER errors — a malformed call still captures whatever
    arrived (the finalizer degrades gracefully) because failing the tool would
    push the model back into prose, defeating the whole mechanism.
    """
    captured: dict[str, Any] = {}

    def handler(arguments: dict[str, Any]) -> str:
        payload = arguments if isinstance(arguments, dict) else {}
        captured["payload"] = payload
        return json.dumps({"ok": True, "status": "submitted"})

    def get_payload() -> Optional[dict]:
        value = captured.get("payload")
        return value if isinstance(value, dict) else None

    return handler, get_payload


def step_result_from_submit(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a submit_result payload into the step_result dict shape the
    downstream mergers/envelope consume. Tolerant by construction."""
    result = payload.get("result")
    if not isinstance(result, dict):
        result = {}
    else:
        result = dict(result)

    summary = str(payload.get("summary") or "").strip()
    if summary and not str(result.get("text") or "").strip():
        result["text"] = summary
    if summary:
        result.setdefault("summary", summary)

    # Normalize onto the canonical enum. A model that answers "done" (the
    # word this tool used to advertise, and the one it reaches for naturally)
    # must land on SUCCEEDED, not fall through to the envelope's "partial"
    # inference — that downgrade is what made a finished step look unfinished.
    #
    # `status` is optional in the tool schema, and calling submit_result is
    # itself the deliberate "here is my final result" hand-off — it registers
    # a terminal SUCCESS stop reason. So an omitted status means succeeded,
    # not "infer something". Without this every submit that skipped the
    # optional field landed on PARTIAL and tripped the supervisor's blocker
    # check, asking the operator to resolve a step that had in fact finished.
    status = normalize_step_result_status(payload.get("status"))
    result.setdefault(
        "status", (status or StepResultStatus.SUCCEEDED).value
    )
    return result


def submit_result_followup_message(expected_output_schema: Optional[dict]) -> str:
    """The nudge for the fallback round when the loop ended without a submit."""
    hint = ""
    if (
        isinstance(expected_output_schema, dict)
        and isinstance(expected_output_schema.get("properties"), dict)
    ):
        fields = ", ".join(sorted(expected_output_schema["properties"].keys()))
        if fields:
            hint = f" Fill these result fields where you have real values: {fields}."
    return (
        "The step is over but no result was submitted. Call `submit_result` NOW "
        "with the final outcome of the work above — do not do anything else, do "
        "not restate the answer as text." + hint
    )
