"""Answer the tool calls a stopped agent loop left without a result.

Shared by every host that retries a stopped loop from its checkpoint: the
Task coordinator retry today, delegated child runs next. A retried transcript
must give every assistant tool call a result before the next model round.
"""
from __future__ import annotations

import copy
import json
from typing import Any


def _interrupted_result(executed: str) -> str:
    return json.dumps({"status": "interrupted", "executed": executed})


def _call_is_read_only(name: str, arguments: Any) -> bool:
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return False
    if not name or not isinstance(arguments, dict):
        return False
    try:
        return classify_runtime_tool(name, arguments).effect is RuntimeToolEffect.READ_ONLY
    except Exception:
        return False


def runtime_answer_interrupted_calls(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """The retried transcript, with a result for every unanswered tool call.

    A stopped attempt can end on an assistant turn whose calls never got a
    result: the parked call (wait or capacity), the calls queued behind it,
    or calls cut off mid-batch. The parked and queued calls never ran
    (executed "no"); any other unanswered call that is not proven read-only
    may have started I/O (executed "unknown"), so the model re-checks it.
    """

    parked = checkpoint.get("runtime_checkpoint")
    parked = parked if isinstance(parked, dict) else {}
    source = parked.get("messages") if isinstance(parked.get("messages"), list) and parked.get("messages") else checkpoint.get("messages")
    messages = copy.deepcopy([message for message in source or [] if isinstance(message, dict)])
    never_ran = {
        str(call.get("id") or "")
        for call in [parked.get("pending_tool_call"), *(parked.get("remaining_tool_calls") or [])]
        if isinstance(call, dict) and call.get("id")
    }
    last = next((
        index for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "assistant" and messages[index].get("tool_calls")
    ), None)
    if last is None:
        return messages
    answered = {
        str(message.get("tool_call_id") or "")
        for message in messages[last + 1:]
        if message.get("role") == "tool"
    }
    answers = []
    for call in messages[last].get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("id") or "")
        if not call_id or call_id in answered:
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "")
        arguments = function.get("arguments") if function else call.get("arguments")
        executed = "no" if call_id in never_ran or _call_is_read_only(name, arguments) else "unknown"
        answers.append({"role": "tool", "tool_call_id": call_id, "content": _interrupted_result(executed)})
    # Results follow the turn's existing ones, before any later message.
    insert_at = last + 1
    while insert_at < len(messages) and messages[insert_at].get("role") == "tool":
        insert_at += 1
    messages[insert_at:insert_at] = answers
    return messages
