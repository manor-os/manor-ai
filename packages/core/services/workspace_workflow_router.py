"""Configuration-driven Workflow entrypoints for Workspace Chat."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition


logger = logging.getLogger(__name__)

_DEFAULT_MINIMUM_CONFIDENCE = 0.85
_MINIMUM_WINNING_MARGIN = 0.10
_CLASSIFICATION_TIMEOUT_SECONDS = 20.0
_STRUCTURED_PREFILL_TIMEOUT_SECONDS = 30.0
_PREFILL_INSTRUCTIONS_MAX_CHARS = 4000
_MISSING = object()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _bounded_confidence(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WorkspaceChatEntrypoint:
    binding_id: str
    workflow_id: str
    workspace_id: str
    title: str
    description: str
    placeholder: str
    order: int
    run_inputs: tuple[dict[str, Any], ...]
    intent_enabled: bool
    intent_description: str
    intent_examples: tuple[str, ...]
    intent_negative_examples: tuple[str, ...]
    minimum_confidence: float
    projection: dict[str, Any]
    wait_bridge: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "workflow_id": self.workflow_id,
            "title": self.title,
            "description": self.description,
            "placeholder": self.placeholder,
            "order": self.order,
            "intent_enabled": self.intent_enabled,
            "inputs": [dict(item) for item in self.run_inputs],
        }


@dataclass(frozen=True)
class WorkspaceIntentDecision:
    entrypoint: WorkspaceChatEntrypoint
    confidence: float
    reason: str


@dataclass(frozen=True)
class WorkspaceEntrypointRun:
    run: Any
    conversation: Any
    user_message: Any
    activity_message: Any


def normalize_chat_entrypoint(
    binding: Any,
    workflow: Any,
) -> WorkspaceChatEntrypoint | None:
    config = _mapping(getattr(binding, "config", None))
    raw = _mapping(config.get("chat_entrypoint"))
    workspace_id = str(getattr(binding, "workspace_id", None) or "").strip()
    if not raw.get("enabled") or not workspace_id:
        return None

    intent = _mapping(raw.get("intent"))
    projection = _mapping(raw.get("projection"))
    try:
        order = int(raw.get("order", 100))
    except (TypeError, ValueError):
        order = 100

    title = str(raw.get("title") or getattr(binding, "name", None) or getattr(workflow, "name", "Workflow")).strip()
    description = str(raw.get("description") or getattr(workflow, "description", "") or "").strip()
    threshold = _bounded_confidence(
        intent.get("minimum_confidence"),
        _DEFAULT_MINIMUM_CONFIDENCE,
    )
    return WorkspaceChatEntrypoint(
        binding_id=str(binding.id),
        workflow_id=str(workflow.id),
        workspace_id=workspace_id,
        title=title or "Workflow",
        description=description,
        placeholder=str(raw.get("placeholder") or "").strip(),
        order=order,
        run_inputs=workflow_run_inputs(workflow),
        intent_enabled=bool(intent.get("enabled")),
        intent_description=str(intent.get("description") or description).strip(),
        intent_examples=_strings(intent.get("examples")),
        intent_negative_examples=_strings(intent.get("negative_examples")),
        minimum_confidence=threshold,
        projection={
            "progress": bool(projection.get("progress", True)),
            "step_outputs": str(projection.get("step_outputs") or "explicit"),
            "final_output": bool(projection.get("final_output", True)),
        },
        wait_bridge=bool(raw.get("wait_bridge", True)),
    )


def _run_input_default(value: Any, variables: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r"\s*\{\{([^{}]+)\}\}\s*", value)
    if match is None:
        return value
    parts = [part for part in match.group(1).strip().split(".") if part]
    current: Any = variables
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _run_input_prefill(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    source = str(value.get("source") or "").strip().lower()
    mode = str(value.get("mode") or "").strip().lower()
    if source != "chat_message" or mode not in {"raw", "structured"}:
        return None
    result = {"source": source, "mode": mode}
    instructions = str(value.get("instructions") or "").strip()
    if instructions:
        result["instructions"] = instructions[:_PREFILL_INSTRUCTIONS_MAX_CHARS]
    return result


def workflow_run_inputs(workflow: Any) -> tuple[dict[str, Any], ...]:
    """Read the canonical runtime inputs declared by Workflow entry nodes."""
    variables = _mapping(getattr(workflow, "variables", None))
    steps = getattr(workflow, "steps", None)
    if not isinstance(steps, list):
        return ()
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict) or str(step.get("type") or "").lower() not in {"trigger", "webhook"}:
            continue
        config = _mapping(step.get("config"))
        default_prefill = _run_input_prefill(config.get("run_input_prefill"))
        rows = config.get("run_inputs")
        if not isinstance(rows, list):
            rows = config.get("inputs")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or row.get("name") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            raw_type = str(row.get("type") or "string").strip().lower()
            input_type = raw_type if raw_type in {"string", "number", "boolean", "json"} else "string"
            raw_default = row.get("defaultValue", row.get("default", row.get("value")))
            normalized = {
                "key": key,
                "label": str(row.get("label") or key).strip() or key,
                "type": input_type,
                "required": bool(row.get("required", row.get("requiredField", True))),
                "hidden": bool(row.get("hidden", False)),
                "placeholder": str(row.get("placeholder") or "").strip(),
                "default": _run_input_default(raw_default, variables),
            }
            target = str(row.get("target") or key).strip()
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}",
                target,
            ):
                normalized["target"] = target
            if isinstance(row.get("schema"), dict):
                normalized["schema"] = deepcopy(row["schema"])
            prefill = (
                _run_input_prefill(row.get("prefill"))
                if "prefill" in row
                else default_prefill
            )
            if prefill is not None:
                normalized["prefill"] = prefill
            if str(row.get("description") or "").strip():
                normalized["description"] = str(row["description"]).strip()
            result.append(normalized)
    return tuple(result)


def prefill_workspace_workflow_inputs(
    entrypoint: WorkspaceChatEntrypoint,
    *,
    message: str,
    attachment_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    values = {
        str(item["key"]): item.get("default")
        for item in entrypoint.run_inputs
        if item.get("default") is not None
    }
    by_key = {str(item["key"]): item for item in entrypoint.run_inputs}
    attachment_input = by_key.get("attachments")
    if attachment_input and attachment_input.get("type") == "json" and attachment_refs:
        values["attachments"] = attachment_refs
    text = str(message or "").strip()
    if text:
        for item in entrypoint.run_inputs:
            if (
                item.get("type") == "string"
                and item.get("prefill", {}).get("source") == "chat_message"
                and item.get("prefill", {}).get("mode") == "raw"
            ):
                values[str(item["key"])] = text
        legacy_string_inputs = [
            item
            for item in entrypoint.run_inputs
            if item.get("type") == "string"
            and not item.get("prefill")
        ]
        preferred = next(
            (
                item
                for key in ("request", "message", "prompt", "chatInput")
                if (item := by_key.get(key)) in legacy_string_inputs
            ),
            legacy_string_inputs[0] if legacy_string_inputs else None,
        )
        if preferred is not None:
            values[str(preferred["key"])] = text
    return values


def assemble_workspace_workflow_inputs(
    entrypoint: WorkspaceChatEntrypoint,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Map flat Starter fields into the variable paths declared by the Workflow."""
    result: dict[str, Any] = {}
    for item in entrypoint.run_inputs:
        key = str(item["key"])
        if key not in values:
            continue
        target = str(item.get("target") or key)
        parts = target.split(".")
        current = result
        for part in parts[:-1]:
            nested = current.get(part)
            if not isinstance(nested, dict):
                nested = {}
                current[part] = nested
            current = nested
        current[parts[-1]] = deepcopy(values[key])
    return result


def preserve_server_captured_workflow_inputs(
    entrypoint: WorkspaceChatEntrypoint,
    values: Any,
    captured_values: Any,
) -> dict[str, Any]:
    """Keep hidden raw Chat inputs authoritative when a paused Run is confirmed."""
    result = deepcopy(values) if isinstance(values, dict) else {}
    captured = captured_values if isinstance(captured_values, dict) else {}
    for item in entrypoint.run_inputs:
        prefill = item.get("prefill") if isinstance(item.get("prefill"), dict) else {}
        if not (
            item.get("hidden")
            and prefill.get("source") == "chat_message"
            and prefill.get("mode") == "raw"
        ):
            continue
        key = str(item["key"])
        if key in captured:
            result[key] = deepcopy(captured[key])
        else:
            result.pop(key, None)
    return result


def _clean_prefilled_uri(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    match = re.search(r"https?://", raw, flags=re.IGNORECASE)
    if match is None:
        return raw
    candidate = raw[match.start():]
    delimiters = [
        found.start()
        for found in (
            re.search(r"[\s。；，！？、]", candidate),
            re.search(
                r"%(?:E3%80%82|EF%BC%9B|EF%BC%8C|EF%BC%81|EF%BC%9F|E3%80%81)",
                candidate,
                flags=re.IGNORECASE,
            ),
        )
        if found is not None
    ]
    return candidate[:min(delimiters)] if delimiters else candidate


def _merge_schema_draft(default: Any, proposed: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return deepcopy(proposed)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null_types = [item for item in schema_type if item != "null"]
        schema_type = non_null_types[0] if len(non_null_types) == 1 else None

    if schema_type == "object":
        if not isinstance(proposed, dict):
            return deepcopy(default) if isinstance(default, dict) else _MISSING
        result = deepcopy(default) if isinstance(default, dict) else {}
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        allow_extra = schema.get("additionalProperties", True) is not False
        for key, value in proposed.items():
            child_schema = properties.get(key)
            if not isinstance(child_schema, dict):
                if allow_extra:
                    result[str(key)] = deepcopy(value)
                continue
            child = _merge_schema_draft(result.get(key, _MISSING), value, child_schema)
            if child is not _MISSING:
                result[str(key)] = child
        return result

    if schema_type == "array":
        if not isinstance(proposed, list):
            return deepcopy(default) if isinstance(default, list) else _MISSING
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        normalized = [
            child
            for value in proposed
            if (child := _merge_schema_draft(_MISSING, value, item_schema)) is not _MISSING
        ]
        try:
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(normalized)
        except Exception:
            return deepcopy(default) if isinstance(default, list) else _MISSING
        return normalized

    candidate = _clean_prefilled_uri(proposed) if schema.get("format") == "uri" else proposed
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(candidate)
    except Exception:
        return deepcopy(default) if default is not _MISSING else _MISSING
    return deepcopy(candidate)


def _unwrap_structured_prefill_value(value: Any, schema: dict[str, Any]) -> Any:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    if (
        isinstance(value, dict)
        and "value" in value
        and "value" not in properties
        and set(value).issubset({"value", "confidence", "reason", "source"})
    ):
        return value["value"]
    return value


def _structured_prefill_prompt(
    inputs: list[dict[str, Any]],
    *,
    message: str,
    attachment_refs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    contracts = {str(item["key"]): item["schema"] for item in inputs}
    instructions = list(dict.fromkeys(
        str(item.get("prefill", {}).get("instructions") or "").strip()
        for item in inputs
        if str(item.get("prefill", {}).get("instructions") or "").strip()
    ))
    return [
        {
            "role": "system",
            "content": (
                "Extract editable Workflow input drafts from the user's latest message. "
                "Use only information explicitly stated or directly and safely derivable from it. "
                "Do not invent credentials, private data, product facts, target pages, or calls to action. "
                "Preserve exact constraints, list items, language, durations, exclusions, storage rules, "
                "browser-session requirements, and interruption conditions. A Markdown link may have "
                "swallowed adjacent prose: recover the visible text and end URLs before sentence "
                "punctuation instead of treating the prose as part of the URL. Omit unknown fields. "
                "Return JSON only with shape {\"inputs\":{\"input_key\": value}}. Each input_key "
                "must map directly to its schema value; never wrap a value in an object such as "
                "{\"value\": ...}."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "message": message,
                    "attachments": workflow_intent_attachment_descriptors(
                        attachment_refs
                    ),
                    "workflow_instructions": instructions,
                    "input_schemas": contracts,
                },
                ensure_ascii=False,
            ),
        },
    ]


async def prepare_workspace_workflow_inputs(
    entrypoint: WorkspaceChatEntrypoint,
    *,
    message: str,
    attachment_refs: list[dict[str, Any]],
    entity_id: str,
    user_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Build editable defaults, including declared structured Chat extraction."""
    values = prefill_workspace_workflow_inputs(
        entrypoint,
        message=message,
        attachment_refs=attachment_refs,
    )
    structured_inputs = [
        item
        for item in entrypoint.run_inputs
        if isinstance(item.get("schema"), dict)
        and item.get("prefill", {}).get("source") == "chat_message"
        and item.get("prefill", {}).get("mode") == "structured"
        and not item.get("hidden")
    ]
    if not structured_inputs or not str(message or "").strip():
        return values

    try:
        from packages.core.ai.runtime.completions import runtime_execute_text_completion
        from packages.core.ai.runtime.sources import RUNTIME_CHAT_SOURCE
        from packages.core.services.skill_bundle import extract_json_object

        completion = await asyncio.wait_for(
            runtime_execute_text_completion(
                _structured_prefill_prompt(
                    structured_inputs,
                    message=message,
                    attachment_refs=attachment_refs,
                ),
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                source=RUNTIME_CHAT_SOURCE,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=4000,
            ),
            timeout=_STRUCTURED_PREFILL_TIMEOUT_SECONDS,
        )
        parsed = extract_json_object(completion.content)
        extracted = parsed.get("inputs") if isinstance(parsed, dict) else None
        if not isinstance(extracted, dict):
            return values
        for item in structured_inputs:
            key = str(item["key"])
            if key not in extracted:
                continue
            merged = _merge_schema_draft(
                values.get(key, _MISSING),
                _unwrap_structured_prefill_value(extracted[key], item["schema"]),
                item["schema"],
            )
            if merged is not _MISSING:
                values[key] = merged
    except Exception:
        logger.info(
            "Workspace Workflow structured input prefill fell back to defaults",
            exc_info=True,
        )
    return values


def validate_workspace_workflow_inputs(
    entrypoint: WorkspaceChatEntrypoint,
    values: Any,
) -> dict[str, Any]:
    provided = values if isinstance(values, dict) else {}
    result: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def schema_error_path(key: str, error: Any) -> str:
        parts = [str(part) for part in error.absolute_path]
        if error.validator == "required" and isinstance(error.instance, dict):
            required = error.validator_value if isinstance(error.validator_value, list) else []
            missing = next((str(field) for field in required if field not in error.instance), "")
            if missing:
                parts.append(missing)
        return ".".join([key, *parts])

    for item in entrypoint.run_inputs:
        key = str(item["key"])
        input_type = str(item.get("type") or "string")
        value = provided.get(key, item.get("default"))
        missing = value is None or (isinstance(value, str) and not value.strip())
        if missing:
            if item.get("required"):
                errors[key] = "This input is required."
            elif input_type == "string" and key in provided:
                result[key] = ""
            continue
        try:
            if input_type == "number":
                number = float(value)
                value = int(number) if number.is_integer() else number
            elif input_type == "boolean":
                if isinstance(value, bool):
                    pass
                elif str(value).strip().lower() in {"true", "1", "yes", "on"}:
                    value = True
                elif str(value).strip().lower() in {"false", "0", "no", "off"}:
                    value = False
                else:
                    raise ValueError
            elif input_type == "json" and isinstance(value, str):
                value = json.loads(value)
            elif input_type == "string":
                value = str(value).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            errors[key] = f"Enter a valid {input_type} value."
            continue
        schema = item.get("schema")
        if isinstance(schema, dict):
            try:
                validation_errors = sorted(
                    Draft202012Validator(
                        schema,
                        format_checker=FormatChecker(),
                    ).iter_errors(value),
                    key=lambda error: (
                        tuple(str(part) for part in error.absolute_path),
                        str(error.message),
                    ),
                )
            except Exception:
                errors[key] = "This Workflow input has an invalid schema."
                continue
            for error in validation_errors:
                errors.setdefault(schema_error_path(key, error), error.message)
            if validation_errors:
                continue
        result[key] = value
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return result


def auto_routing_allowed(
    *,
    workspace_id: str | None,
    message: str = "",
    agent_id: str | None = None,
    manual_skill_ids: str | None = None,
    chat_mode: str | None = None,
    ephemeral: bool = False,
    editor_context: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    disable_tools: bool = False,
    blocked_tools: str | None = None,
) -> bool:
    if not str(workspace_id or "").strip():
        return False
    if agent_id or str(manual_skill_ids or "").strip():
        return False
    if re.search(r"(^|\s)@\S", str(message or "")):
        return False
    if str(chat_mode or "").strip().lower() not in {"", "auto"}:
        return False
    if disable_tools or str(blocked_tools or "").strip():
        return False
    if ephemeral or str(editor_context or "").strip():
        return False
    if thread_ref_kind or thread_ref_id:
        return False
    return True


def select_intent_match(
    entrypoints: Iterable[WorkspaceChatEntrypoint | None],
    scores: Iterable[dict[str, Any]],
) -> WorkspaceChatEntrypoint | None:
    by_id = {
        entrypoint.binding_id: entrypoint
        for entrypoint in entrypoints
        if entrypoint is not None and entrypoint.intent_enabled
    }
    best_scores: dict[str, float] = {}
    for score in scores:
        if not isinstance(score, dict):
            continue
        entrypoint = by_id.get(str(score.get("binding_id") or ""))
        if entrypoint is None:
            continue
        if entrypoint.binding_id in best_scores:
            return None
        confidence = _bounded_confidence(score.get("confidence"))
        best_scores[entrypoint.binding_id] = confidence
    if set(best_scores) != set(by_id):
        return None
    ranked = [
        (best_scores[binding_id], entrypoint)
        for binding_id, entrypoint in by_id.items()
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    top_score, top_entrypoint = ranked[0]
    if top_score < top_entrypoint.minimum_confidence:
        return None
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if len(ranked) > 1 and top_score - runner_up < _MINIMUM_WINNING_MARGIN:
        return None
    return top_entrypoint


def prefer_workspace_bindings(
    pairs: Iterable[tuple[Any, Any]],
    *,
    workspace_id: str | None,
) -> list[tuple[Any, Any]]:
    eligible = [
        pair
        for pair in pairs
        if (
            getattr(pair[0], "workspace_id", None) is None
            or getattr(pair[0], "workspace_id", None) == workspace_id
        )
    ]
    if workspace_id is None:
        return [pair for pair in eligible if getattr(pair[0], "workspace_id", None) is None]

    chosen: dict[str, tuple[Any, Any]] = {}
    for binding, workflow in eligible:
        workflow_id = str(getattr(workflow, "id", None) or getattr(binding, "workflow_id", ""))
        current = chosen.get(workflow_id)
        if current is None or (
            getattr(current[0], "workspace_id", None) is None
            and getattr(binding, "workspace_id", None) == workspace_id
        ):
            chosen[workflow_id] = (binding, workflow)
    return sorted(
        chosen.values(),
        key=lambda pair: 0 if getattr(pair[0], "workspace_id", None) == workspace_id else 1,
    )


async def list_workspace_chat_entrypoints(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    intent_only: bool = False,
) -> list[WorkspaceChatEntrypoint]:
    rows = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )).all()
    entrypoints = [
        normalized
        for binding, workflow in rows
        if (normalized := normalize_chat_entrypoint(binding, workflow)) is not None
        and (not intent_only or normalized.intent_enabled)
    ]
    return sorted(entrypoints, key=lambda item: (item.order, item.title.lower(), item.binding_id))


async def get_workspace_chat_entrypoint(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    binding_id: str,
) -> tuple[WorkspaceChatEntrypoint, WorkflowBinding, WorkflowDefinition] | None:
    row = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.id == binding_id,
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )).one_or_none()
    if row is None:
        return None
    binding, workflow = row
    entrypoint = normalize_chat_entrypoint(binding, workflow)
    if entrypoint is None:
        return None
    return entrypoint, binding, workflow


def _normalized_action_text(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(re.sub(r"[^\w\s]+", " ", normalized).split())


def pending_action_reply_matches(
    message: str,
    pending_actions: Iterable[dict[str, Any]],
) -> bool:
    text = _normalized_action_text(message)
    if not text:
        return False
    reply_phrases = {
        "approve",
        "approve all",
        "reject",
        "reject all",
        "cancel",
        "confirm",
        "continue",
        "feedback",
        "go ahead",
        "looks good",
        "no",
        "proceed",
        "skip",
        "stop",
        "this looks good",
        "yes",
    }
    for action in pending_actions:
        if not isinstance(action, dict) or not action.get("kind"):
            continue
        options = {
            _normalized_action_text(option)
            for option in action.get("options") or []
            if _normalized_action_text(option)
        }
        if text in options or text in reply_phrases:
            return True
        if any(text.startswith(f"{phrase} ") for phrase in reply_phrases):
            return True
        if str(action.get("kind")) in {
            "human_input",
            "needs_input",
            "workflow_input",
            "workflow_starter_input",
        }:
            return len(text) <= 2000
    return False


async def conversation_message_is_pending_action_reply(
    db: AsyncSession,
    conversation_id: str | None,
    message: str,
) -> bool:
    if not conversation_id:
        return False
    from packages.core.models.task import Message

    pending_actions = list((await db.execute(
        select(Message.pending_action).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
            Message.pending_action["kind"].as_string().isnot(None),
            Message.resolved_at.is_(None),
        ).order_by(Message.created_at.desc()).limit(20)
    )).scalars().all())
    return pending_action_reply_matches(message, pending_actions)


def workflow_attachment_descriptors(attachments: Any) -> list[dict[str, Any]]:
    refs = getattr(attachments, "attachment_refs", None)
    if not isinstance(refs, list):
        return []
    allowed = {
        "id",
        "name",
        "filename",
        "title",
        "type",
        "kind",
        "mime_type",
        "document_id",
        "fs_path",
        "path",
        "url",
    }
    return [
        {key: value for key, value in ref.items() if key in allowed and value not in (None, "")}
        for ref in refs
        if isinstance(ref, dict)
    ]


def workflow_intent_attachment_descriptors(attachments: Any) -> list[dict[str, Any]]:
    """Return only non-locating attachment metadata for intent classification."""
    safe_keys = {"name", "filename", "title", "type", "kind", "mime_type"}
    descriptors = (
        attachments
        if isinstance(attachments, list)
        else workflow_attachment_descriptors(attachments)
    )
    return [
        {key: value for key, value in ref.items() if key in safe_keys}
        for ref in descriptors
        if isinstance(ref, dict)
    ]


async def start_workspace_chat_entrypoint(
    db: AsyncSession,
    *,
    entrypoint: WorkspaceChatEntrypoint,
    binding: WorkflowBinding,
    entity_id: str,
    user_id: str,
    workspace_id: str,
    message: str,
    attachments: Any,
    conversation_id: str | None = None,
    route_source: str,
    confidence: float | None = None,
    reason: str | None = None,
) -> WorkspaceEntrypointRun:
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services.conversation_lifecycle import get_or_create_conversation
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.runtime_file_context import runtime_saved_message_with_file_references
    from packages.core.services.workflow_service import start_workflow_from_binding

    conversation = await get_or_create_conversation(
        db,
        entity_id,
        user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        title=message.splitlines()[0][:100].strip() or entrypoint.title,
    )
    saved_message = runtime_saved_message_with_file_references(message, attachments)
    user_message = await add_message(
        db,
        conversation.id,
        role="user",
        content=saved_message,
        meta={
            "author_user_id": user_id,
            "workflow_route_source": route_source,
            "workflow_binding_id": binding.id,
        },
    )
    attachment_descriptors = workflow_attachment_descriptors(attachments)
    initial_values = await prepare_workspace_workflow_inputs(
        entrypoint,
        message=message,
        attachment_refs=attachment_descriptors,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    initial_variables = assemble_workspace_workflow_inputs(
        entrypoint,
        initial_values,
    )
    requires_input = bool(entrypoint.run_inputs)
    entrypoint_context = {
        "enabled": True,
        "route_source": route_source,
        "conversation_id": conversation.id,
        "user_message_id": user_message.id,
        "projection": entrypoint.projection,
        "wait_bridge": entrypoint.wait_bridge,
        **({"confidence": confidence} if confidence is not None else {}),
        **({"reason": reason} if reason else {}),
    }
    run = await start_workflow_from_binding(
        db,
        binding,
        variables=None,
        trigger_data={
            **deepcopy(initial_values),
            **deepcopy(initial_variables),
            "runtime_context": {
                "workspace_id": workspace_id,
                "conversation_id": conversation.id,
            },
            "_workspace_chat_entrypoint": entrypoint_context,
        },
        trigger_source="workspace_chat",
        started_by=user_id,
        execution_workspace_id=workspace_id,
    )
    if requires_input:
        run.status = "paused"
    route_label = "Automatically selected" if route_source == "intent" else "Selected"
    activity_status = "paused" if requires_input else "queued"
    from packages.core.services.workflow_chat_projection import workflow_progress_steps

    activity_message = await add_message(
        db,
        conversation.id,
        role="system",
        content=(
            f"{route_label} {entrypoint.title}. Review the workflow inputs to continue."
            if requires_input
            else f"{route_label} {entrypoint.title}. Preparing the first workflow step."
        ),
        message_kind="workflow_activity",
        refs=[
            {"type": "workflow", "id": run.workflow_id, "title": entrypoint.title},
            {"type": "workflow_run", "id": run.id},
        ],
        meta={
            "workflow_run_id": run.id,
            "workflow_binding_id": binding.id,
            "workflow_title": entrypoint.title,
            "workflow_status": activity_status,
            "workflow_route_source": route_source,
            "workflow_steps": workflow_progress_steps(
                run,
                activity_status=activity_status,
            ),
        },
    )
    updated_trigger_data = dict(run.trigger_data or {})
    entrypoint_context = dict(updated_trigger_data.get("_workspace_chat_entrypoint") or {})
    entrypoint_context["activity_message_id"] = activity_message.id
    updated_trigger_data["_workspace_chat_entrypoint"] = entrypoint_context
    run.trigger_data = updated_trigger_data
    if requires_input:
        await add_message(
            db,
            conversation.id,
            role="system",
            content=f"Provide the inputs required to run {entrypoint.title}.",
            message_kind="hitl_request",
            refs=[
                {"type": "workflow", "id": run.workflow_id, "title": entrypoint.title},
                {"type": "workflow_run", "id": run.id},
            ],
            pending_action={
                "kind": PendingActionKind.WORKFLOW_STARTER_INPUT.value,
                "title": entrypoint.title,
                "description": entrypoint.description,
                "workflow_run_id": run.id,
                "workflow_binding_id": binding.id,
                "inputs": [dict(item) for item in entrypoint.run_inputs],
                "values": initial_values,
                "options": ["run", "cancel"],
            },
            meta={
                "workflow_run_id": run.id,
                "workflow_input_stage": "starter",
            },
        )
    await db.commit()
    if not requires_input:
        if WorkflowRunner.enqueue(run.id) is False:
            run.status = "failed"
            run.error = "Workflow could not be queued. Please start it again."
            run.completed_at = datetime.now(timezone.utc)
            from packages.core.services.workflow_run_trace import (
                update_workflow_history_summary,
            )

            update_workflow_history_summary(run)
            from packages.core.services.workflow_chat_projection import (
                project_workflow_run_status,
            )

            await project_workflow_run_status(db, run=run)
            await db.commit()
    return WorkspaceEntrypointRun(
        run=run,
        conversation=conversation,
        user_message=user_message,
        activity_message=activity_message,
    )


def _intent_prompt(entrypoints: list[WorkspaceChatEntrypoint], message: str, attachment_refs: list[dict]) -> list[dict]:
    candidates = [
        {
            "binding_id": item.binding_id,
            "title": item.title,
            "description": item.intent_description,
            "examples": list(item.intent_examples),
            "negative_examples": list(item.intent_negative_examples),
        }
        for item in entrypoints
    ]
    return [
        {
            "role": "system",
            "content": (
                "Classify whether the user's request should start one of the configured Workspace workflows. "
                "Score every candidate from 0 to 1. A high score requires an explicit request to perform the "
                "candidate's end-to-end work, not a question about it. Return JSON only with shape "
                '{"scores":[{"binding_id":"...","confidence":0.0,"reason":"..."}]}.'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "message": message,
                    "attachments": attachment_refs,
                    "candidates": candidates,
                },
                ensure_ascii=False,
            ),
        },
    ]


async def classify_workspace_intent(
    *,
    entrypoints: list[WorkspaceChatEntrypoint],
    message: str,
    attachment_refs: list[dict[str, Any]],
    entity_id: str,
    user_id: str,
    workspace_id: str,
) -> WorkspaceIntentDecision | None:
    if not entrypoints:
        return None
    try:
        from packages.core.ai.runtime.completions import runtime_execute_text_completion
        from packages.core.ai.runtime.sources import RUNTIME_CHAT_SOURCE
        from packages.core.services.skill_bundle import extract_json_object

        result = await asyncio.wait_for(
            runtime_execute_text_completion(
                _intent_prompt(entrypoints, message, attachment_refs),
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                source=RUNTIME_CHAT_SOURCE,
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=600,
            ),
            timeout=_CLASSIFICATION_TIMEOUT_SECONDS,
        )
        parsed = extract_json_object(result.content)
        scores = parsed.get("scores") if isinstance(parsed, dict) else None
        if not isinstance(scores, list):
            return None
        selected = select_intent_match(entrypoints, scores)
        if selected is None:
            return None
        selected_score = next((
            score
            for score in scores
            if isinstance(score, dict)
            and str(score.get("binding_id") or "") == selected.binding_id
        ), {})
        return WorkspaceIntentDecision(
            entrypoint=selected,
            confidence=_bounded_confidence(selected_score.get("confidence")),
            reason=str(selected_score.get("reason") or "").strip(),
        )
    except Exception:
        logger.info("Workspace Workflow intent classification fell back to Chat", exc_info=True)
        return None
