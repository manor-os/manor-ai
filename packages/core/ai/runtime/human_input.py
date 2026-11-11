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
    "description": "Explain the human dependency. Handle tool errors, file writes and validation yourself. No permission is granted.",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["human_fact", "human_decision", "human_material", "file_review", "authorization_setup"],
            "description": "authorization_setup is only for connecting an external account through its normal flow.",
        },
        "why_human_required": {
            "type": "string", "minLength": 1, "maxLength": 1200, "pattern": r"\S",
            "description": "Why authorized tools or context cannot answer.",
        },
        "attempted_resolution": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "description": "Actual checks, not invented calls. An explicit preference in the request counts.",
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
            "description": "Exact existing paths or Document references for human judgment; grants no access.",
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
#: Each field type is one form control; ``input_field_schema`` gives the JSON
#: schema the shared form (web, phone) draws it from and the answer is checked
#: against. ``file`` / ``photo`` / ``audio`` / ``signature`` answer with
#: Document references; ``record`` / ``records`` with Workspace project ids of
#: ``project_type``. Ordered: the tool schema lists them in this order.
INPUT_FIELD_TYPES = (
    "address", "audio", "boolean", "color", "date", "date_range", "datetime", "email", "file",
    "line_items", "multi_select", "number", "phone", "photo", "rating", "record", "records",
    "rich_text", "select", "signature", "tags", "text", "textarea",
)
_FIELD_TYPES = frozenset(INPUT_FIELD_TYPES)
#: A line-item row's cells: words, numbers, or one of a few values.
LINE_ITEM_COLUMN_TYPES = ("integer", "number", "select", "text")
_MAX_LINE_ITEM_COLUMNS = 8
_PROJECT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DOCUMENT_ITEMS: dict[str, Any] = {
    "type": "object",
    "required": ["document_id"],
    "properties": {"document_id": {"type": "string", "minLength": 1}},
}


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_options(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    options: list[str] = []
    for item in raw:
        label = _clean_text(item, 200)
        if label and label not in options:
            options.append(label)
    return options[:_MAX_OPTIONS]


def _line_item_columns(raw: Any) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name") or item.get("label"), 60)
        if not name or name in seen:
            continue
        seen.add(name)
        kind = _clean_text(item.get("type"), 20).lower() or "text"
        column: dict[str, Any] = {
            "name": name,
            "label": _clean_text(item.get("label") or name, 120),
            "type": kind if kind in LINE_ITEM_COLUMN_TYPES else "text",
        }
        if column["type"] == "select":
            column["options"] = _clean_options(item.get("options"))
            if not column["options"]:
                column["type"] = "text"
                del column["options"]
        columns.append(column)
        if len(columns) >= _MAX_LINE_ITEM_COLUMNS:
            break
    return columns


def _documents_schema(**ui: Any) -> dict[str, Any]:
    return {"type": "array", "items": deepcopy(_DOCUMENT_ITEMS), "x-ui": {"control": "documents", **ui}}


def input_field_schema(field: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema of one normalized ``request_input`` field.

    The same vocabulary as Workflow run inputs (``workflow_run_input_guidance``):
    the form picks the control from ``format`` and ``x-ui.control``, and the
    answer is validated against this schema.
    """
    kind = field.get("type") or "text"
    options = field.get("options") or []
    schema: dict[str, Any]
    if kind == "textarea":
        schema = {"type": "string", "x-ui": {"control": "textarea"}}
    elif kind == "rich_text":
        schema = {"type": "string", "x-ui": {"control": "rich_text"}}
    elif kind == "number":
        schema = {"type": "number"}
    elif kind == "rating":
        schema = {"type": "integer", "minimum": 1, "maximum": 5, "x-ui": {"control": "rating"}}
    elif kind == "boolean":
        schema = {"type": "boolean"}
    elif kind == "date":
        schema = {"type": "string", "format": "date"}
    elif kind == "datetime":
        schema = {"type": "string", "format": "date-time"}
    elif kind == "date_range":
        schema = {
            "type": "object",
            "properties": {
                "start": {"type": "string", "format": "date"},
                "end": {"type": "string", "format": "date"},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
            "x-ui": {"control": "date_range"},
        }
    elif kind == "select":
        schema = {"type": "string", "enum": list(options)}
    elif kind == "multi_select":
        schema = {"type": "array", "items": {"type": "string", "enum": list(options)}, "uniqueItems": True}
    elif kind == "tags":
        schema = {"type": "array", "items": {"type": "string", "minLength": 1}, "x-ui": {"control": "tags"}}
    elif kind == "email":
        schema = {"type": "string", "format": "email"}
    elif kind == "phone":
        schema = {"type": "string", "format": "phone"}
    elif kind == "color":
        schema = {"type": "string", "x-ui": {"control": "color"}}
    elif kind == "address":
        schema = {"type": "string", "x-ui": {"control": "address"}}
    elif kind == "file":
        schema = _documents_schema()
    elif kind == "photo":
        schema = _documents_schema(accept=["image"], capture="environment")
    elif kind == "audio":
        schema = _documents_schema(accept=["audio"], capture="user")
    elif kind == "signature":
        schema = {"type": "array", "items": deepcopy(_DOCUMENT_ITEMS), "maxItems": 1, "x-ui": {"control": "signature"}}
    elif kind == "record":
        schema = {"type": "string", "x-ui": {"control": "workflow_project_select", "project_type": field["project_type"]}}
    elif kind == "records":
        schema = {
            "type": "array", "items": {"type": "string"}, "uniqueItems": True,
            "x-ui": {"control": "workflow_project_select", "project_type": field["project_type"]},
        }
    elif kind == "line_items":
        properties: dict[str, Any] = {}
        for column in field.get("columns") or []:
            cell: dict[str, Any] = {"title": column["label"]}
            if column["type"] == "select":
                cell.update({"type": "string", "enum": list(column.get("options") or [])})
            else:
                cell["type"] = {"text": "string"}.get(column["type"], column["type"])
            properties[column["name"]] = cell
        schema = {
            "type": "array",
            "items": {"type": "object", "properties": properties, "additionalProperties": False},
            "x-ui": {"control": "line_items"},
        }
    else:
        schema = {"type": "string"}
    schema["title"] = field.get("label") or field.get("name") or ""
    if field.get("description"):
        schema["description"] = field["description"]
    if field.get("placeholder"):
        schema.setdefault("x-ui", {})["placeholder"] = field["placeholder"]
    if field.get("required") and schema["type"] == "array":
        # A required list is answered by at least one item.
        schema["minItems"] = 1
    return schema


def input_fields_schema(fields: Any) -> dict[str, Any] | None:
    """One object schema over a question's fields that carry a schema, or None."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields if isinstance(fields, (list, tuple)) else ():
        if not isinstance(field, dict) or not isinstance(field.get("schema"), dict):
            continue
        name = str(field.get("name") or "")
        if not name:
            continue
        properties[name] = field["schema"]
        if field.get("required"):
            required.append(name)
    if not properties:
        return None
    return {"type": "object", "properties": properties, "required": required}


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def input_field_answer_errors(fields: Any, submitted: Any) -> dict[str, str]:
    """Field name -> why the submitted value is refused, for fields declared with a schema.

    Blank optional answers are absent (the form leaves them out). Legacy
    questions whose fields carry no schema are not checked.
    """
    from packages.core.contracts.json_schema import SchemaContractValidatorFactory

    schema = input_fields_schema(fields)
    if schema is None:
        return {}
    values = {
        key: value for key, value in (submitted if isinstance(submitted, dict) else {}).items()
        if key in schema["properties"] and not _blank(value)
    }
    errors: dict[str, str] = {}
    for key in schema["required"]:
        if key not in values:
            errors[key] = "This field is required."
    for error in SchemaContractValidatorFactory.build({**schema, "required": []}).iter_errors(values):
        path = [str(part) for part in error.absolute_path]
        errors.setdefault(path[0] if path else "", error.message)
    return errors


def input_field_answer_text(value: Any) -> str:
    """One answer value as a person reads it in the transcript."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        if set(value) >= {"start", "end"} and len(value) == 2:
            return f"{value.get('start')} – {value.get('end')}"
        if "document_id" in value:
            return str(value.get("name") or value.get("document_id"))
        return ", ".join(f"{key}: {input_field_answer_text(item)}" for key, item in value.items() if not _blank(item))
    if isinstance(value, list):
        return "; ".join(input_field_answer_text(item) for item in value if not _blank(item))
    return "" if value is None else str(value).strip()


def normalize_input_request(
    *,
    question: Any,
    options: Any = None,
    fields: Any = None,
    context: Any = None,
    justification: Any = None,
    recommended: Any = None,
    recommendation_reason: Any = None,
    recommendation: Any = None,
    action_link: Any = None,
    action_to_take: Any = None,
) -> dict[str, Any]:
    """Shape a question, including historical payloads without justification.

    New model calls validate the explicit justification and recommendation
    before reaching this compatibility normalizer; persisted questions and
    host-owned inputs stay readable. ``recommended`` /
    ``recommendation_reason`` are the tool's arguments; ``recommendation`` is
    an already-shaped one (a stored question being re-normalized).

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
            for key, limit in (("description", 400), ("placeholder", 200)):
                if note := _clean_text(raw.get(key), limit):
                    entry[key] = note
            # A control that lacks what it needs falls back to one that works.
            if field_type in {"select", "multi_select"}:
                entry["options"] = _clean_options(raw.get("options"))
                if not entry["options"]:
                    del entry["options"]
                    entry["type"] = "text" if field_type == "select" else "tags"
            elif field_type in {"record", "records"}:
                project_type = _clean_text(raw.get("project_type"), 64).lower()
                if _PROJECT_TYPE_PATTERN.match(project_type):
                    entry["project_type"] = project_type
                else:
                    entry["type"] = "text" if field_type == "record" else "textarea"
            elif field_type == "line_items":
                entry["columns"] = _line_item_columns(raw.get("columns"))
                if not entry["columns"]:
                    del entry["columns"]
                    entry["type"] = "textarea"
            entry["schema"] = input_field_schema(entry)
            normalized_fields.append(entry)
            if len(normalized_fields) >= _MAX_FIELDS:
                break
    if isinstance(recommendation, dict) and recommended is None:
        recommended = recommendation.get("option")
        recommendation_reason = recommendation.get("reason")
    shaped_recommendation = _shape_recommendation(
        recommended, recommendation_reason, normalized_options, has_fields=bool(normalized_fields),
    )
    payload: dict[str, Any] = {"question": text}
    if normalized_options:
        payload["options"] = normalized_options
    if shaped_recommendation is not None:
        payload["recommendation"] = shaped_recommendation
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


def _shape_recommendation(
    recommended: Any, reason: Any, options: list[str], *, has_fields: bool,
) -> dict[str, Any] | None:
    """The question's one recommended answer, placed first among ``options``.

    A recommended answer that is not one of the options becomes the first
    option (the model named its best answer; the person picks it in one tap).
    A question with neither options nor fields offers it as its single option;
    the person can always answer in their own words instead. Mutates
    ``options`` so the recommended answer leads.
    """
    from packages.core.services.decision_recommendation import MAX_REASON_CHARS

    answer = _clean_text(recommended, 200)
    if not answer:
        return None
    why = " ".join(str(reason or "").split())[:MAX_REASON_CHARS]
    if answer in options:
        options.remove(answer)
        options.insert(0, answer)
    elif options or not has_fields:
        options.insert(0, answer)
        del options[_MAX_OPTIONS:]
    value: dict[str, Any] = {"option": answer, "reason": why or "This is the most likely answer."}
    if not why:
        value["reason_code"] = "most_likely"
    return value


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
    "INPUT_FIELD_TYPES",
    "LINE_ITEM_COLUMN_TYPES",
    "human_input_answer_result",
    "input_field_answer_errors",
    "input_field_answer_text",
    "input_field_schema",
    "input_fields_schema",
    "runtime_answered_input_questions",
    "runtime_input_question_key",
    "runtime_request_input_refusal",
    "runtime_request_input_tool_schema",
    "runtime_transcript_platform_fault",
    "human_input_mode_from_kwargs",
    "normalize_input_request",
    "requests_tool_grant",
]
