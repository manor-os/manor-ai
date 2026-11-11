"""Work the user hands to Tasks is not also done in Chat.

Asked to "split this into Tasks and start them", the Workspace Agent ran
every stage itself with ``delegate_to_agent`` inside one long Chat turn: no
Task, no dependency, nothing resumable or verifiable. When it did create the
Tasks it still delegated each stage in Chat and then started the Tasks, so
the work ran twice. Before a delegation runs, two facts send it back:

- this turn already created or started a Task (the work is handed off);
- the user's current message asks for Tasks.
"""
from __future__ import annotations

import json
import re
from typing import Any

from packages.core.ai.runtime.figure_provenance import DELEGATION_TOOLS, _text_of

HANDED_TO_TASKS_CODE = "work_handed_to_tasks"
TASKS_REQUESTED_CODE = "tasks_requested"

_TASK_REQUEST = re.compile(
    r"(创建|新建|建立|建|拆成|拆分成|拆分为|分成|分解成|开)[^。！？!?\n]{0,16}(task|任务)"
    r"|\b(create|set up|split (it |this )?into|break (it |this )?into)\b[^.!?\n]{0,30}\btasks?\b",
    re.IGNORECASE,
)


def _current_turn(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The user's latest message and everything the run did after it."""
    from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_host_feedback_message

    for index in range(len(messages or []) - 1, -1, -1):
        message = messages[index]
        if (isinstance(message, dict) and message.get("role") == "user"
                and not runtime_is_host_feedback_message(message)):
            return _text_of(message.get("content")), messages[index + 1:]
    return "", []


def _handed_task_ids(turn: list[dict[str, Any]]) -> list[str]:
    """Tasks this turn created or started, from their tool results."""
    ids: list[str] = []
    for message in turn:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            result = json.loads(_text_of(message.get("content")))
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict) or not isinstance(result.get("task"), dict):
            continue
        if result.get("created") is True or result.get("started") is True:
            task_id = str(result["task"].get("id") or "")
            if task_id and task_id not in ids:
                ids.append(task_id)
    return ids


def user_asked_for_tasks(text: str) -> bool:
    """Whether a user message asks for the work to be created as Tasks."""
    return bool(_TASK_REQUEST.search(text or ""))


def delegation_handoff_refusal(
    tool_name: str, arguments: Any, messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The tool error for a delegation that would do Task work in Chat, if any."""
    if tool_name not in DELEGATION_TOOLS:
        return None
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    request, turn = _current_turn(messages)
    if handed := _handed_task_ids(turn):
        return tool_error_payload(
            tool_name=tool_name, code=HANDED_TO_TASKS_CODE,
            message=(
                "This turn already handed the work to Tasks (" + ", ".join(handed[:8]) + "). They run "
                "with their own Agents and deliver their own outputs; doing a stage here as well would "
                "run it twice. Start a pending one with start_task, change one with "
                "update_task_runtime, and report them. Nothing was delegated."
            ),
            details={"task_ids": handed[:8], "action_executed": False},
        )
    if user_asked_for_tasks(request):
        return tool_error_payload(
            tool_name=tool_name, code=TASKS_REQUESTED_CODE,
            message=(
                "The user asked for Tasks, not for the work to be done in this Chat turn. Create one "
                "Workspace Task per stage with create_task: owner_service_key for the Agent that owns "
                "it, depends_on_task_ids naming the earlier stages' task ids, and start=true. Later "
                "stages start on their own when the earlier ones complete. Nothing was delegated."
            ),
            details={"action_executed": False},
        )
    return None
