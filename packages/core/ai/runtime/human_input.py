"""Runtime-owned contract for ``request_input`` (the ask-the-user tool).

Services, workers and the task loop import from here — never from the tool
module — so the tool stays a thin registry entry over a runtime bridge (the
same boundary rule as media / generated_files / manor_actions).
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
)

#: ``_runtime_human_input_from_context`` values. ``True`` (or ``"suspend"``)
#: means the loop can park on a checkpoint; ``"end_turn"`` means the legacy
#: task loop will park the task on the marker result; anything else means the
#: runtime cannot pause for a human and the tool must say so honestly.
HUMAN_INPUT_MODE_SUSPEND = "suspend"
HUMAN_INPUT_MODE_END_TURN = "end_turn"


def human_input_mode_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    value = kwargs.get("_runtime_human_input_from_context")
    if value is True or value == HUMAN_INPUT_MODE_SUSPEND:
        return HUMAN_INPUT_MODE_SUSPEND
    if value == HUMAN_INPUT_MODE_END_TURN:
        return HUMAN_INPUT_MODE_END_TURN
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
                "specific files; or account/permission setup through its normal "
                "authorization flow. Internal recovery and execution checks are "
                "not human decisions."
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
            "type": "array", "minItems": 1, "maxItems": 6,
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
#: Appended to a plan subagent step's prompt. Prod task FQ2881 (2026-09-14):
#: a content step could not find the product positioning it needed, filled
#: the deliverable with "暂缺" and let the plan run on. The tool exists so the
#: step can ask; the prompt must say so, or the model guesses anyway.
REQUEST_INPUT_PROMPT_SUFFIX = (
    " If a fact, decision, or piece of material that only a person can supply "
    "is missing, call `request_input` with the exact question and why you need "
    "it and a structured `justification` — this step pauses and resumes with the answer. "
    "The justification must name the human-only kind, why_human_required and "
    "attempted_resolution findings from the context/tools you actually checked. "
    "Use file_review only for human judgment on exact file_refs, not to check whether "
    "a write happened. Resolve internal tool errors, inspect execution records and "
    "file versions, and repair acceptance/validation calls within this loop. "
    "Those internal failures are not a reason to ask a person to verify execution. "
    "Never guess the missing human input or "
    "submit a placeholder. If a needed tool is not loaded, first use "
    "`search_tools` when available to load its schema within your existing "
    "permissions. If this loop exposes a capability acquisition tool, use it "
    "to search and prepare missing capabilities and apply them through its "
    "normal configuration gate before asking a person to configure tools. "
    "If acquisition is unavailable or a required account needs connection, use "
    "`request_input` to name the exact tool and purpose, and ask the operator "
    "to update this Agent's configuration or this Agent's deployment binding "
    "in the current Workspace through user-reviewed setup. Connect any "
    "required account separately through its normal setup flow. "
    "A bare 'yes' to this input question does not create a tool binding; "
    "only the capability tool's policy-authorized apply or a separate typed, "
    "user-confirmed configuration review can do that. "
    "Verify the tool is available after this same step resumes before using "
    "or claiming it."
)
#: Marker key on the end-turn result; the task runner keys on it.
REQUEST_INPUT_RESULT_KEY = "__request_input__"
#: How long a parked question stays answerable before the run is failed.
REQUEST_INPUT_DEADLINE_HOURS = 24
#: Grace for a parked run: past this the run is failed and the question closed.
HUMAN_INPUT_TIMEOUT_REASON = "human_input_timeout"
#: Bounds — a question is a short prompt, not a document.
_MAX_QUESTION_CHARS = 2000
_MAX_CONTEXT_CHARS = 4000
_MAX_OPTIONS = 12
_MAX_FIELDS = 12
_FIELD_TYPES = frozenset({"text", "textarea", "number", "select", "boolean", "date"})


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_input_request(
    *,
    question: Any,
    options: Any = None,
    fields: Any = None,
    context: Any = None,
    justification: Any = None,
) -> dict[str, Any]:
    """Shape a question, including historical payloads without justification.

    New model calls validate the explicit justification before reaching this
    compatibility normalizer; persisted questions/host-owned inputs stay readable.
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
    return payload


def input_request_from_tool_result(result: Any) -> dict[str, Any] | None:
    """The question payload when ``result`` is an end-turn request_input result."""
    text = result if isinstance(result, str) else None
    if text is None:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    payload = parsed.get(REQUEST_INPUT_RESULT_KEY)
    if not isinstance(payload, dict) or not str(payload.get("question") or "").strip():
        return None
    return payload


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
        "a bare 'yes', does not itself create a tool binding or connect an "
        "account; a separate typed configuration review may authorize one. "
        "If the question was "
        "about tool access, verify the current runtime tool scope before any "
        "call; if access is still unavailable, report the blocker rather than "
        "claiming the tool ran."
    )
    return json.dumps(body, ensure_ascii=False)



__all__ = [
    "HUMAN_INPUT_MODE_END_TURN",
    "HUMAN_INPUT_MODE_SUSPEND",
    "HUMAN_INPUT_SUSPENSION_KIND",
    "HUMAN_INPUT_SUSPENSION_REASON",
    "HUMAN_INPUT_TIMEOUT_REASON",
    "REQUEST_INPUT_DEADLINE_HOURS",
    "REQUEST_INPUT_JUSTIFICATION_SCHEMA",
    "REQUEST_INPUT_PROMPT_SUFFIX",
    "REQUEST_INPUT_RESULT_KEY",
    "REQUEST_INPUT_TOOL_NAME",
    "human_input_answer_result",
    "runtime_request_input_tool_schema",
    "human_input_mode_from_kwargs",
    "input_request_from_tool_result",
    "normalize_input_request",
]
