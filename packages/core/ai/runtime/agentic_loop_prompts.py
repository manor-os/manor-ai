from __future__ import annotations

import json
from typing import Any


# Provider transcripts only carry system/user/assistant/tool roles, and
# mid-conversation system messages are not portable, so Runtime feedback rides
# in a user-role message. Without an explicit source the model reads a raw
# diagnostic as something the user typed and answers it to the user.
RUNTIME_HOST_FEEDBACK_TAG = "runtime-feedback"
RUNTIME_HOST_FEEDBACK_KEY = "runtime_feedback"
RUNTIME_HOST_FEEDBACK_NOTE = (
    "Written by the Manor runtime, not by the user; the user has not seen it. "
    "Act on it silently and continue the user's latest request. Never attribute "
    "it to the user or mention it in the reply."
)


def runtime_host_feedback_message(content: str | dict[str, Any]) -> dict[str, str]:
    """Build a user-role transcript message that is marked as Runtime-authored.

    Structured diagnostics stay one JSON object with every original key, so
    callers and the model still read ``error``/``message`` directly.
    """

    if isinstance(content, dict):
        payload = {
            RUNTIME_HOST_FEEDBACK_KEY: RUNTIME_HOST_FEEDBACK_NOTE,
            **{key: value for key, value in content.items() if key != RUNTIME_HOST_FEEDBACK_KEY},
        }
        text = json.dumps(payload, ensure_ascii=False)
    else:
        text = (
            f'<{RUNTIME_HOST_FEEDBACK_TAG} source="manor-runtime">\n'
            f"{RUNTIME_HOST_FEEDBACK_NOTE}\n{content}\n"
            f"</{RUNTIME_HOST_FEEDBACK_TAG}>"
        )
    return {"role": "user", "content": text}


def runtime_is_host_feedback_message(message: Any) -> bool:
    """Return whether a transcript message came from runtime_host_feedback_message."""

    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    if content.startswith(f"<{RUNTIME_HOST_FEEDBACK_TAG} "):
        return True
    if not content.startswith(f'{{"{RUNTIME_HOST_FEEDBACK_KEY}"'):
        return False
    try:
        payload = json.loads(content)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get(RUNTIME_HOST_FEEDBACK_KEY) == RUNTIME_HOST_FEEDBACK_NOTE


RUNTIME_TURN_CONTEXT_TAG = "turn-context"


def runtime_turn_context_message(content: str) -> dict[str, str]:
    """System-role context for this turn, sent after the history.

    It holds what follows the current message, turn state or the clock, so the
    tools, system prompt and history before it stay byte-identical from turn to
    turn and can be served from the provider's prompt cache. It is rebuilt every
    call and never stored in the transcript.
    """
    return {
        "role": "system",
        "content": f"<{RUNTIME_TURN_CONTEXT_TAG}>\n{content.strip()}\n</{RUNTIME_TURN_CONTEXT_TAG}>",
    }


def runtime_is_turn_context_message(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(f"<{RUNTIME_TURN_CONTEXT_TAG}>")
    )


def runtime_with_turn_context(messages: list[dict[str, Any]], content: str | None) -> list[dict[str, Any]]:
    """Insert the turn context right before the current request.

    The current request is the last user message that is not runtime feedback;
    inserting there keeps tool-call/result groups intact and keeps the position
    the same for every round of the run.
    """
    if not content or not content.strip():
        return list(messages)
    result = list(messages)
    index = next(
        (
            i for i in range(len(result) - 1, -1, -1)
            if result[i].get("role") == "user" and not runtime_is_host_feedback_message(result[i])
        ),
        len(result),
    )
    result.insert(index, runtime_turn_context_message(content))
    return result


RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX = (
    "[Context compacted — earlier tool rounds summarized]"
)
RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX = "[Earlier context — LLM summary]"


def runtime_agentic_structural_compaction_message(summary: str) -> str:
    """Build the compacted-context marker inserted into the loop history."""

    return f"{RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX}\n{summary}"


def runtime_agentic_is_structural_compaction_message(content: str) -> bool:
    """Return whether a message is the structural compaction placeholder."""

    return str(content or "").startswith(RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX)


def runtime_agentic_llm_compaction_prompt(summary_text: str) -> str:
    """Build the worker-model prompt used to summarize oversized loop context."""

    return (
        "Summarize the following conversation context in 3-5 sentences. "
        "Include key findings from tool calls, decisions made, and what "
        "the user originally asked for:\n\n"
        f"{summary_text}"
    )


def runtime_agentic_llm_compaction_replacement(summary: str) -> str:
    """Build the history message that replaces the structural summary."""

    return f"{RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX}\n{summary.strip()}"


def runtime_agentic_empty_response_retry_message(*, has_tools: bool) -> str:
    """Build the retry instruction after a provider returns an empty response."""

    if not has_tools:
        retry = (
            "Retry the user's latest request now and respond with the requested "
            "content directly."
        )
    else:
        retry = (
            "Retry the user's latest request now. Choose an available tool only "
            "if the request needs one; all tool calls remain subject to their "
            "normal permission checks."
        )
    return (
        "[System: The model provider returned an empty response with no tool calls. "
        f"{retry}]"
    )


def runtime_agentic_truncated_tool_call_retry_message() -> str:
    """Build the retry instruction after a tool call is truncated."""

    return (
        "[System: Your last tool call was truncated by the output limit and was "
        "not executed. Reassess the request and, if a tool is still needed, send "
        "a complete valid call that fits the limit.]"
    )


def runtime_agentic_auto_next_calls_message() -> str:
    """Explain host-owned follow-ups already scheduled by the Runtime."""

    return (
        "[System: Runtime is continuing a pending operation. The follow-up "
        "remains subject to normal authorization and safety checks. Consider its result before "
        "responding to the user.]"
    )


def runtime_agentic_max_rounds_final_prompt() -> str:
    """Build the final no-tools prompt after max_rounds is exhausted."""

    return (
        "You've used all available tool rounds. Please provide your final "
        "response based on everything you've gathered so far."
    )








PSEUDO_TOOL_MARKUP_FINAL_CONTENT = (
    "The model described tool activity in text instead of calling a tool, so no "
    "action was taken and its claimed results were discarded."
)


def runtime_agentic_pseudo_tool_markup_retry_message(markup_kinds: list[str]) -> str:
    """One repair note after the final text imitated tool-call markup."""

    return (
        "[System: Your previous response wrote tool-call markup as text ("
        + ", ".join(markup_kinds[:6])
        + "). No tool was executed. Tools run only through function calling. "
        "Call the tool, or say the capability is unavailable. Do not write "
        "tool-call or tool-result markup in your answer. If you are showing the "
        "syntax, put it in a code block.]"
    )


def runtime_agentic_output_schema_retry_message(validation_error: str) -> str:
    """Build the no-tools repair prompt for a schema-invalid final response."""

    return (
        "[System: Your previous final response failed JSON Schema validation: "
        f"{validation_error}. Return a corrected, complete JSON value only. "
        "Do not call tools, repeat side effects, add markdown fences, or include "
        "explanatory prose.]"
    )
