"""Runtime-owned contract for ``request_input`` (the ask-the-user tool).

Services, workers and the task loop import from here — never from the tool
module — so the tool stays a thin registry entry over a runtime bridge (the
same boundary rule as media / generated_files / manor_actions).
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Callable

from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
)

#: ``_runtime_human_input_from_context`` values. ``True`` (or ``"suspend"``)
#: means the loop can park on a checkpoint; anything else means the runtime
#: cannot pause for a human and the tool must say so honestly.
HUMAN_INPUT_MODE_SUSPEND = "suspend"


def human_input_mode_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    value = kwargs.get("_runtime_human_input_from_context")
    if value is True or value == HUMAN_INPUT_MODE_SUSPEND:
        return HUMAN_INPUT_MODE_SUSPEND
    return None


REQUEST_INPUT_TOOL_NAME = "request_input"

# Model-reported rationale, not an authorization receipt or proof that a
# reported lookup happened. Shared by schema discovery and handler preflight.
REQUEST_INPUT_JUSTIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Explain why a person is needed and what you checked. Internal errors, "
        "missing schemas, execution or file-write checks, and acceptance failures "
        "belong to the agent. This rationale grants no permission."
    ),
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["human_fact", "human_decision", "human_material", "file_review", "authorization_setup"],
            "description": (
                "Choose a user-only fact, decision, or material; human review of "
                "specific files; or connecting an external account through its "
                "normal authorization flow (authorization_setup never asks for tool "
                "access or Agent configuration). Internal recovery and execution "
                "checks are not human decisions."
            ),
        },
        "why_human_required": {
            "type": "string", "minLength": 1, "maxLength": 1200, "pattern": r"\S",
            "description": "Why the agent cannot obtain this answer from authorized tools, records or existing context.",
        },
        "attempted_resolution": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "description": (
                "What you actually checked before interrupting. Reading the current user "
                "request is enough for an explicit human preference; never invent tool calls."
            ),
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "source": {"type": "string", "enum": ["conversation", "workspace", "tool_result", "policy"]},
                    "finding": {"type": "string", "minLength": 1, "maxLength": 1200, "pattern": r"\S"},
                },
                "required": ["source", "finding"],
            },
        },
        "file_refs": {
            # A multi-page deliverable is reviewed as a whole: a ten-page
            # carousel plus its caption copy must fit in one review.
            "type": "array", "minItems": 1, "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": r"\S"},
            "description": (
                "For file_review, cite exact existing paths or Document references for "
                "human judgment. The agent verifies file writes. References grant no access."
            ),
        },
    },
    "required": ["kind", "why_human_required", "attempted_resolution"],
    "additionalProperties": False,
    "allOf": [{
        "if": {"properties": {"kind": {"const": "file_review"}}, "required": ["kind"]},
        "then": {"required": ["file_refs"]},
    }],
}
#: A person cannot grant a tool through request_input: their answer never
#: creates a binding, so asking only loops. Prod Task 01M5DVK8G26J4P6GW5RB4RX87M
#: (2026-10-21) asked for a "deployment configuration receipt"; the user
#: answered 可以 twice and was asked again each time. authorization_setup
#: questions that name tool access or Agent configuration are refused;
#: account connection keeps the kind.
_TOOL_GRANT_PATTERN = re.compile(
    r"configur|binding|\bbind\b|deploy|receipt|allowlist|"
    r"tool\s+(?:access|scope|permission|grant)|"
    r"配置|绑定|部署|回执|工具权限|开通工具|授予工具",
    re.IGNORECASE,
)


def requests_tool_grant(*, question: Any, context: Any = None, justification: Any = None) -> bool:
    """Whether a new request_input call asks a person for tool access or configuration."""
    if not isinstance(justification, dict) or justification.get("kind") != "authorization_setup":
        return False
    asked = " ".join(str(part or "") for part in (question, context, justification.get("why_human_required")))
    return _TOOL_GRANT_PATTERN.search(asked) is not None


# ── Who can resolve it: the gate every human park passes ─────────────────
#
# Prod Task 01M5DVK8G26J4P6GW5RB4RX87M (2026-10-21): replan_task refused on a
# missing pinned execution snapshot, the coordinator asked the customer for
# "the platform admin's fix", the customer answered 可以 and was asked again.
# A person can answer facts, decisions and material; a platform fault and an
# answered question are not theirs to resolve. The agentic loop runs this gate
# before any request_input handler (registry tool or host override); a refusal
# delivers no question.

REQUEST_INPUT_TOOL_GRANT_CODE = "tool_grant_not_requestable"
REQUEST_INPUT_PLATFORM_FAULT_CODE = "human_input_platform_fault"
REQUEST_INPUT_ALREADY_ANSWERED_CODE = "human_input_already_answered"

#: The blocked exit of each engine; a refusal names them all because the gate
#: does not know which host runs it.
REQUEST_INPUT_BLOCKED_EXITS = (
    "in a Task agent loop finish with complete_task(outcome=blocked); "
    "elsewhere use the host's blocked or failed result, or tell the user "
    "plainly in your reply"
)
_ANSWERED_STATUSES = frozenset({"answered", "declined"})
_MAX_ANSWER_ECHO_CHARS = 1200


def runtime_input_question_key(text: Any) -> str:
    """Case, punctuation and spacing do not make a new question."""
    return " ".join(re.sub(r"[\W_]+", " ", str(text or "").casefold()).split())


def _message_part(message: Any, key: str) -> Any:
    return message.get(key) if isinstance(message, dict) else getattr(message, key, None)


def _transcript_calls(transcript: Any) -> list[tuple[str, str, dict[str, Any], Any]]:
    """``(call_id, tool_name, arguments, result_content)`` in transcript order.

    Reads both provider-dict and ChatMessage transcripts. A call without a
    result yet has ``None`` content.
    """
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    ordered: list[tuple[str, str, dict[str, Any], Any]] = []
    for message in transcript or ():
        role = _message_part(message, "role")
        if role == "assistant":
            for call in _message_part(message, "tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(call.get("name") or function.get("name") or "")
                raw = call.get("arguments", function.get("arguments"))
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except (TypeError, ValueError):
                        raw = {}
                calls[str(call.get("id") or "")] = (name, raw if isinstance(raw, dict) else {})
        elif role == "tool":
            call_id = str(_message_part(message, "tool_call_id") or "")
            name, arguments = calls.get(call_id, (str(_message_part(message, "name") or ""), {}))
            ordered.append((call_id, name, arguments, _message_part(message, "content")))
    return ordered


def runtime_answered_input_questions(transcript: Any) -> dict[str, str]:
    """Questions this transcript already asked and a person answered or declined.

    Keyed by normalized question text; the value is the answer (empty for a
    decline), so a refused re-ask can hand the answer back.
    """
    answered: dict[str, str] = {}
    for _, name, arguments, content in _transcript_calls(transcript):
        if name != REQUEST_INPUT_TOOL_NAME or not isinstance(content, str):
            continue
        try:
            result = json.loads(content)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("status") not in _ANSWERED_STATUSES:
            continue
        key = runtime_input_question_key(arguments.get("question"))
        if key:
            answered[key] = str(result.get("answer") or "")[:_MAX_ANSWER_ECHO_CHARS]
    return answered


def runtime_transcript_platform_fault(transcript: Any) -> dict[str, Any] | None:
    """The latest platform fault in a transcript no later call of its tool got past.

    For loops without a tool evidence ledger; ledger-backed loops use
    ``runtime_unresolved_platform_error`` instead.
    """
    from packages.core.ai.runtime.streams import runtime_tool_call_error
    from packages.core.ai.runtime.tool_error_contract import runtime_platform_error

    unresolved: dict[str, dict[str, Any]] = {}
    for call_id, name, _, content in _transcript_calls(transcript):
        if not name or name == REQUEST_INPUT_TOOL_NAME or content is None:
            continue
        fault = runtime_platform_error(name, content)
        if fault is not None:
            unresolved.pop(name, None)
            unresolved[name] = {**fault, "tool": name, "call_id": call_id}
        elif runtime_tool_call_error(content) is None:
            unresolved.pop(name, None)
    return list(unresolved.values())[-1] if unresolved else None


#: Tools besides ``request_input`` whose call can park on a person, each with
#: how to read the call's question (None when this call does not park). The
#: agentic loop runs the gate below on that question before the handler, so a
#: second way to ask a person cannot skip it.
_HUMAN_PARK_TOOLS: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {}


def register_human_park_tool(
    tool_name: str, question_arguments: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> None:
    _HUMAN_PARK_TOOLS[tool_name] = question_arguments


def runtime_human_park_arguments(tool_name: str, arguments: Any) -> dict[str, Any] | None:
    """The question a call would park on, for the gate; None when it asks nobody."""
    args = arguments if isinstance(arguments, dict) else {}
    if tool_name == REQUEST_INPUT_TOOL_NAME:
        return args
    reader = _HUMAN_PARK_TOOLS.get(tool_name)
    return reader(args) if reader is not None else None


def runtime_request_input_refusal(
    arguments: Any,
    *,
    platform_fault: dict[str, Any] | None = None,
    answered_questions: dict[str, str] | None = None,
) -> str | None:
    """The tool error for a request_input call no person can resolve, or None.

    Refused, in order: asking for a tool grant or Agent configuration; any
    question while the run has an unresolved platform fault (by the
    ``internal_error`` contract no person can repair it, so the answer could
    only re-park on the same fault); and a question this run already had
    answered. No kind is exempt from the platform check: the prod question
    would have passed as a "decision". The fault clears once the same tool
    operation later succeeds, so recovered runs can ask again.
    """
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    args = arguments if isinstance(arguments, dict) else {}
    question = args.get("question")
    refusal: dict[str, Any] | None = None
    if requests_tool_grant(question=question, context=args.get("context"), justification=args.get("justification")):
        refusal = tool_error_payload(
            tool_name=REQUEST_INPUT_TOOL_NAME, code=REQUEST_INPUT_TOOL_GRANT_CODE,
            message=(
                "A person cannot grant tool access or Agent configuration through "
                "request_input; no question was delivered."
            ),
            details={"action_executed": False, "instruction": (
                "Load the tool with search_tools. If it is outside your scope, acquire it "
                "with acquire_capabilities when this loop offers it. If acquisition is "
                "unavailable, denied, or cannot bind it, stop as blocked and name the "
                f"missing capability and what it is for: {REQUEST_INPUT_BLOCKED_EXITS}. "
                "Use authorization_setup only to ask the person to connect an external account."
            )},
        )
    elif isinstance(platform_fault, dict):
        refusal = tool_error_payload(
            tool_name=REQUEST_INPUT_TOOL_NAME, code=REQUEST_INPUT_PLATFORM_FAULT_CODE,
            message=(
                "This run hit an internal Manor error that no person can fix, so no "
                "question was delivered. The platform, not the user, resolves it."
            ),
            details={
                "action_executed": False,
                "platform_error": str(platform_fault.get("code") or "internal_error"),
                **{key: platform_fault[key] for key in ("tool", "operation") if platform_fault.get(key)},
                "instruction": (
                    "Do not ask the user to fix, approve, confirm or verify this error. "
                    "If another available call repairs the same operation, make it; "
                    f"otherwise stop as blocked and name the internal error: {REQUEST_INPUT_BLOCKED_EXITS}. "
                    "The host records it as a platform issue and reports it to the Manor team."
                ),
            },
        )
    elif (key := runtime_input_question_key(question)) and key in (answered_questions or {}):
        answer = (answered_questions or {})[key]
        refusal = tool_error_payload(
            tool_name=REQUEST_INPUT_TOOL_NAME, code=REQUEST_INPUT_ALREADY_ANSWERED_CODE,
            message="The user already answered this exact question in this run; it was not asked again.",
            details={
                "action_executed": False,
                "previous_answer": answer,
                "declined": not answer,
                "instruction": (
                    "Continue with the previous answer. If it does not unblock the work, "
                    f"stop as blocked and say what is still missing: {REQUEST_INPUT_BLOCKED_EXITS}."
                ),
            },
        )
    return None if refusal is None else json.dumps(refusal, ensure_ascii=False)

#: How long a parked question stays answerable before the run is failed.
REQUEST_INPUT_DEADLINE_HOURS = 24
#: Grace for a parked run: past this the run is failed and the question closed.
HUMAN_INPUT_TIMEOUT_REASON = "human_input_timeout"
#: Bounds — a question is a short prompt, not a document.
_MAX_QUESTION_CHARS = 2000
_MAX_CONTEXT_CHARS = 4000
_MAX_OPTIONS = 12
_MAX_FIELDS = 12
#: ``file``: the person uploads or picks a Knowledge file; the answer is its
#: file reference. Ordered: the tool schema lists them in this order.
INPUT_FIELD_TYPES = ("boolean", "date", "file", "number", "select", "text", "textarea")
_FIELD_TYPES = frozenset(INPUT_FIELD_TYPES)


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_input_request(
    *,
    question: Any,
    options: Any = None,
    fields: Any = None,
    context: Any = None,
    justification: Any = None,
    action_link: Any = None,
    action_to_take: Any = None,
) -> dict[str, Any]:
    """Shape a question, including historical payloads without justification.

    New model calls validate the explicit justification before reaching this
    compatibility normalizer; persisted questions/host-owned inputs stay readable.

    ``action_link`` / ``action_to_take`` say where in Manor the person acts
    before answering (a screen hand-over opens a live view). They
    are host-owned: ``request_input`` never passes them, so a model cannot
    send a person to a page; only an in-app path is kept.
    """
    text = _clean_text(question, _MAX_QUESTION_CHARS)
    if not text:
        raise ValueError("request_input needs a non-empty question")
    normalized_options: list[str] = []
    if isinstance(options, (list, tuple)):
        for option in options:
            label = _clean_text(option, 200)
            if label and label not in normalized_options:
                normalized_options.append(label)
            if len(normalized_options) >= _MAX_OPTIONS:
                break
    normalized_fields: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    if isinstance(fields, (list, tuple)):
        for raw in fields:
            if not isinstance(raw, dict):
                continue
            name = _clean_text(raw.get("name") or raw.get("label"), 80)
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            field_type = _clean_text(raw.get("type"), 20).lower() or "text"
            if field_type not in _FIELD_TYPES:
                field_type = "text"
            entry: dict[str, Any] = {
                "name": name,
                "label": _clean_text(raw.get("label") or name, 200),
                "type": field_type,
                "required": bool(raw.get("required", True)),
            }
            if field_type == "select" and isinstance(raw.get("options"), (list, tuple)):
                entry["options"] = [
                    _clean_text(item, 200) for item in raw["options"] if _clean_text(item, 200)
                ][:_MAX_OPTIONS]
            normalized_fields.append(entry)
            if len(normalized_fields) >= _MAX_FIELDS:
                break
    payload: dict[str, Any] = {"question": text}
    if normalized_options:
        payload["options"] = normalized_options
    if normalized_fields:
        payload["fields"] = normalized_fields
    summary = _clean_text(context, _MAX_CONTEXT_CHARS)
    if summary:
        payload["context"] = summary
    if isinstance(justification, dict):
        payload["justification"] = deepcopy(justification)
    link = in_app_action_link(action_link)
    if link:
        payload["action_link"] = link
        step = _clean_text(action_to_take, 400)
        if step:
            payload["action_to_take"] = step
    return payload


def in_app_action_link(value: Any) -> str | None:
    """An in-app path (``/computer?takeover=1``), never another origin."""
    link = str(value or "").strip()[:500]
    if not link.startswith("/") or link.startswith("//") or "\\" in link or any(ch.isspace() for ch in link):
        return None
    return link


def runtime_request_input_tool_schema() -> dict[str, Any]:
    """The registry ``request_input`` schema, for loops whose tool surface
    was built without it (a plan subagent step bound to specific tools).

    Lazy import: the tool module imports this contract, not the other way
    round, and workers never import ``packages.core.ai.tools`` directly.
    """

    from packages.core.ai.tools.request_input_tool import REQUEST_INPUT_SCHEMA

    return REQUEST_INPUT_SCHEMA


def human_input_answer_result(
    *,
    answer: Any,
    answered_by: Any = None,
    fields: Any = None,
    choice: Any = None,
    attachments: Any = None,
) -> str:
    """The tool result the model reads once the human has answered."""
    body: dict[str, Any] = {
        "status": "answered",
        "answer": str(answer or "").strip(),
    }
    if isinstance(fields, dict) and fields:
        body["fields"] = fields
    if isinstance(attachments, list) and attachments:
        body["attachments"] = attachments
    if choice is not None and str(choice).strip():
        body["choice"] = str(choice).strip()
    if answered_by:
        body["answered_by"] = str(answered_by)
    body["instruction"] = (
        "The user answered your question. Continue the work using this answer; "
        "do not ask the same question again. This request_input answer, even "
        "a bare 'yes', does not create a tool binding or connect an account. "
        "If the question was about an account connection, verify the "
        "connection before relying on it; if a tool or connection is still "
        "unavailable, stop as blocked and name it rather than asking again "
        "or claiming the tool ran."
    )
    return json.dumps(body, ensure_ascii=False)



__all__ = [
    "HUMAN_INPUT_MODE_SUSPEND",
    "in_app_action_link",
    "register_human_park_tool",
    "runtime_human_park_arguments",
    "HUMAN_INPUT_SUSPENSION_KIND",
    "HUMAN_INPUT_SUSPENSION_REASON",
    "HUMAN_INPUT_TIMEOUT_REASON",
    "REQUEST_INPUT_ALREADY_ANSWERED_CODE",
    "REQUEST_INPUT_BLOCKED_EXITS",
    "REQUEST_INPUT_DEADLINE_HOURS",
    "REQUEST_INPUT_JUSTIFICATION_SCHEMA",
    "REQUEST_INPUT_PLATFORM_FAULT_CODE",
    "REQUEST_INPUT_TOOL_GRANT_CODE",
    "REQUEST_INPUT_TOOL_NAME",
    "human_input_answer_result",
    "runtime_answered_input_questions",
    "runtime_input_question_key",
    "runtime_request_input_refusal",
    "runtime_request_input_tool_schema",
    "runtime_transcript_platform_fault",
    "human_input_mode_from_kwargs",
    "normalize_input_request",
    "requests_tool_grant",
]
