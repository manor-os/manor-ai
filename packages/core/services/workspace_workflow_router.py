"""Configuration-driven Workflow entrypoints for Workspace Chat."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import logging
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.contracts.json_schema import SchemaContractValidatorFactory
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.services.viewer_language import binding_name_for, binding_text_for, localized, without_i18n


logger = logging.getLogger(__name__)

_DEFAULT_MINIMUM_CONFIDENCE = 0.85
_MINIMUM_WINNING_MARGIN = 0.10
_CLASSIFICATION_TIMEOUT_SECONDS = 20.0
_STRUCTURED_PREFILL_TIMEOUT_SECONDS = 30.0
_PREFILL_INSTRUCTIONS_MAX_CHARS = 4000
_MISSING = object()

# Retired Flows stay hidden even from App View / draft validation.
# Unpublished statuses (draft, paused, ...) are only visible when requested.
_RETIRED_WORKFLOW_STATUSES = ("inactive",)


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
    # Rules across several inputs (at least one platform), checked before a run starts.
    input_rules: tuple[dict[str, Any], ...] = ()

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
class WorkspaceIntentClassification:
    status: str
    entrypoint: WorkspaceChatEntrypoint | None = None
    candidates: tuple[WorkspaceChatEntrypoint, ...] = ()
    confidence: float = 0.0
    reason: str = ""
    source_brief: str = ""
    request_mode: str = "unknown"


@dataclass(frozen=True)
class WorkspaceIntentChoicePrompt:
    conversation: Any
    user_message: Any
    assistant_message: Any


@dataclass(frozen=True)
class WorkspaceEntrypointRun:
    run: Any
    conversation: Any
    user_message: Any
    activity_message: Any


def normalize_chat_entrypoint(
    binding: Any,
    workflow: Any,
    *,
    include_unpublished: bool = False,
    locale: str | None = None,
) -> WorkspaceChatEntrypoint | None:
    """Project a binding's chat entry point; ``locale`` is the viewer's.

    Pass a locale only for what a person reads. Intent routing and prefill
    read the authored text.
    """
    config = _mapping(localized(getattr(binding, "config", None), locale))
    raw = _mapping(config.get("chat_entrypoint"))
    workspace_id = str(getattr(binding, "workspace_id", None) or "").strip()
    # Older Workspace UI bindings predate the explicit chat_entrypoint block.
    # Project only the unambiguous legacy marker; an explicit false remains a
    # deliberate opt-out and entity-level bindings are never promoted.
    if "enabled" in raw:
        enabled = raw.get("enabled") is True
    else:
        enabled = config.get("workspace_attached") is True and bool(workspace_id)
    if (not enabled and not include_unpublished) or not workspace_id:
        return None

    intent = _mapping(raw.get("intent"))
    projection = _mapping(raw.get("projection"))
    try:
        order = int(raw.get("order", 100))
    except (TypeError, ValueError):
        order = 100

    binding_name = binding_name_for(getattr(binding, "name", None), getattr(binding, "config", None), locale)
    title = str(raw.get("title") or binding_name or getattr(workflow, "name", "Workflow")).strip()
    # An entry without its own description shows its Flow's, translated beside the binding.
    description = str(raw.get("description") or binding_text_for(
        "description", getattr(workflow, "description", "") or "", getattr(binding, "config", None), locale,
    ) or "").strip()
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
        run_inputs=workflow_run_inputs(workflow, locale=locale),
        input_rules=workflow_run_input_rules(workflow, locale=locale),
        intent_enabled=bool(intent.get("enabled")),
        intent_description=str(intent.get("description") or description).strip(),
        intent_examples=_strings(intent.get("examples")),
        intent_negative_examples=_strings(intent.get("negative_examples")),
        minimum_confidence=threshold,
        projection={
            "progress": bool(projection.get("progress", True)),
            "step_outputs": str(projection.get("step_outputs") or "explicit"),
            "final_output": bool(projection.get("final_output", True)),
            "approval_review": str(projection.get("approval_review") or "inline"),
            "final_output_fields": [
                field
                for field in _strings(projection.get("final_output_fields"))[:32]
                if len(field) <= 128
            ],
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


def _run_input_prefill(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    source = str(value.get("source") or "").strip().lower()
    mode = str(value.get("mode") or "").strip().lower()
    if source == "chat_message" and mode in {"raw", "structured"}:
        result = {"source": source, "mode": mode}
        instructions = str(value.get("instructions") or "").strip()
        if instructions:
            result["instructions"] = instructions[:_PREFILL_INSTRUCTIONS_MAX_CHARS]
        return result
    if source == "chat_attachment" and mode == "unique":
        return {"source": source, "mode": mode}
    if source != "workflow_result" or mode not in {"", "latest"}:
        return None
    workflow_slug = str(
        value.get("workflow_slug") or value.get("workflow") or ""
    ).strip()
    terminal_step_id = str(value.get("terminal_step_id") or "").strip()
    path = str(value.get("path") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", workflow_slug):
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", terminal_step_id):
        return None
    if path and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){0,7}",
        path,
    ):
        return None
    return {
        "source": source,
        "mode": "latest",
        "workflow_slug": workflow_slug,
        "terminal_step_id": terminal_step_id,
        "path": path,
    }


def _entry_steps(workflow: Any, locale: str | None) -> list[dict[str, Any]]:
    """A Flow's entry nodes (trigger/webhook), in the viewer's language.

    Only these are translated: a translation never changes a node's ``type``,
    and translating every node of every Flow on each list dominated the cost.
    """
    steps = getattr(workflow, "steps", None)
    if not isinstance(steps, list):
        return []
    return [
        localized(step, locale) if locale else without_i18n(step)
        for step in steps
        if isinstance(step, dict) and str(step.get("type") or "").lower() in {"trigger", "webhook"}
    ]


# The same test as _entry_steps, run by Postgres: listing entry points reads a
# Flow's entry nodes instead of its whole step graph.
_ENTRY_STEPS_PATH = '$[*] ? (@.type like_regex "^(trigger|webhook)$" flag "i")'


def entry_steps_column():
    """A Flow's entry nodes as a select column (None when ``steps`` is no list)."""
    return case((
        func.jsonb_typeof(WorkflowDefinition.steps) == "array",
        func.jsonb_path_query_array(
            WorkflowDefinition.steps, cast(_ENTRY_STEPS_PATH, JSONPATH), type_=JSONB,
        ),
    )).label("entry_steps")


def workflow_run_input_rules(workflow: Any, *, locale: str | None = None) -> tuple[dict[str, Any], ...]:
    """Rules across a Flow's run inputs, declared on its entry node as ``input_rules``.

    Each rule is a JSON Schema the whole set of answers must meet (``anyOf`` of
    ``const`` answers for "choose at least one"), with an optional ``x-message``
    shown on every input the rule names. The form checks them before starting
    and the run is refused without them, so a run never starts only to fail on them.
    """
    rules: list[dict[str, Any]] = []
    for step in _entry_steps(workflow, locale):
        for rule in _mapping(step.get("config")).get("input_rules") or []:
            if isinstance(rule, dict) and rule:
                rules.append(deepcopy(rule))
    return tuple(rules)


def input_rule_fields(rule: Any) -> list[str]:
    """The inputs a rule is about: those it requires or constrains, at any depth."""
    if not isinstance(rule, dict):
        return []
    fields = [str(key) for key in rule.get("required") or []]
    fields += [str(key) for key in _mapping(rule.get("properties"))]
    for keyword in ("anyOf", "allOf", "oneOf"):
        for branch in rule.get(keyword) or []:
            fields += input_rule_fields(branch)
    for keyword in ("if", "then", "else", "not"):
        fields += input_rule_fields(rule.get(keyword))
    return list(dict.fromkeys(fields))


def input_rule_errors(rules: Iterable[dict[str, Any]], values: dict[str, Any],
                      labels: dict[str, str] | None = None) -> dict[str, str]:
    """Each broken rule's message, on every input it names."""
    errors: dict[str, str] = {}
    for rule in rules:
        schema = {key: value for key, value in rule.items() if key != "x-message"}
        try:
            broken = next(iter(SchemaContractValidatorFactory.build(schema).iter_errors(values)), None) is not None
        except Exception:
            continue  # a malformed rule is reported where the Flow is saved, never on a person's run
        if not broken:
            continue
        fields = input_rule_fields(rule)
        names = ", ".join((labels or {}).get(field, field) for field in fields)
        message = str(rule.get("x-message") or "").strip() or f"Fill in at least one of: {names}."
        for field in fields:
            errors.setdefault(field, message)
    return errors


def workflow_run_inputs(workflow: Any, *, locale: str | None = None) -> tuple[dict[str, Any], ...]:
    """Read the canonical runtime inputs declared by Workflow entry nodes.

    With the viewer's ``locale`` the labels, hints and defaults are theirs.
    """
    variables = _mapping(getattr(workflow, "variables", None))
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for step in _entry_steps(workflow, locale):
        config = _mapping(step.get("config"))
        # The run is validated against input_schema; the form reads each input's
        # schema, so its constraints travel with the input and the form refuses
        # what the run would refuse.
        declared = _mapping(_mapping(config.get("input_schema")).get("properties"))
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
            input_type = (
                "number"
                if raw_type == "integer"
                else raw_type
                if raw_type in {"string", "number", "boolean", "json"}
                else "string"
            )
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
            # A step only groups the form into pages; every input is still
            # submitted and validated together.
            step = str(row.get("step") or "").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", step):
                normalized["step"] = step
                step_label = str(row.get("step_label") or "").strip()
                if step_label:
                    normalized["step_label"] = step_label
            if "schema" in row:
                normalized["schema"] = deepcopy(row["schema"])
            elif raw_type == "integer":
                normalized["schema"] = {"type": "integer"}
            if isinstance(declared.get(key), dict):
                normalized["schema"] = {**normalized.get("schema", {}), **deepcopy(declared[key])}
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
    file_extensions = {
        "audio": {"wav", "flac", "mp3", "aiff", "aif"},
        "image": {"png", "jpg", "jpeg"},
    }
    for item in entrypoint.run_inputs:
        prefill = item.get("prefill") or {}
        if prefill.get("source") != "chat_attachment":
            continue
        schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
        ui = schema.get("x-ui") if isinstance(schema.get("x-ui"), dict) else {}
        extensions = file_extensions.get(ui.get("file_kind"))
        matches = []
        for ref in attachment_refs:
            path = ref.get("fs_path") or ref.get("path")
            if not isinstance(path, str) or not path.strip():
                continue
            if extensions is not None and path.rsplit(".", 1)[-1].lower() not in extensions:
                continue
            matches.append(path)
        if len(matches) == 1:
            values[str(item["key"])] = matches[0]
    text = str(message or "").strip()
    if text:
        for item in entrypoint.run_inputs:
            if (
                item.get("type") == "string"
                and item.get("prefill", {}).get("source") == "chat_message"
                and item.get("prefill", {}).get("mode") == "raw"
            ):
                values[str(item["key"])] = text
        legacy_string_inputs = []
        for item in entrypoint.run_inputs:
            schema = item.get("schema")
            schema = schema if isinstance(schema, dict) else {}
            ui = schema.get("x-ui")
            ui = ui if isinstance(ui, dict) else {}
            is_selection = (
                "enum" in schema
                or "const" in schema
                or ui.get("control") in {"select", "workflow_project_select", "knowledge_file", "record_select"}
            )
            if (
                item.get("type") == "string"
                and not item.get("prefill")
                and not is_selection
            ):
                legacy_string_inputs.append(item)
        preferred = next(
            (
                item
                for key in ("request", "message", "prompt", "chatInput")
                if (item := by_key.get(key)) in legacy_string_inputs
            ),
            # Legacy definitions did not declare an explicit chat-message
            # prefill. Preserve the useful old behavior for a required text
            # intake, but never pour the whole user message into an arbitrary
            # optional field such as ``knowledge_query`` or ``canonical_url``.
            # Those fields already have workflow defaults and a routing
            # instruction is not their value.
            next(
                (item for item in legacy_string_inputs if item.get("required")),
                None,
            ),
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
            SchemaContractValidatorFactory.build(schema).validate(normalized)
        except Exception:
            return deepcopy(default) if isinstance(default, list) else _MISSING
        return normalized

    candidate = _clean_prefilled_uri(proposed) if schema.get("format") == "uri" else proposed
    try:
        SchemaContractValidatorFactory.build(schema).validate(candidate)
    except Exception:
        return deepcopy(default) if default is not _MISSING else _MISSING
    return deepcopy(candidate)


def _choices_outside_options(proposed: Any, schema: dict[str, Any]) -> list[str]:
    """Extracted values of a choice input (``enum`` or array of ``enum``) it does not offer."""
    items = schema.get("items") if schema.get("type") == "array" else None
    options = (items if isinstance(items, dict) else schema).get("enum")
    if not isinstance(options, list):
        return []
    values = proposed if isinstance(proposed, list) else [proposed]
    offered = {str(option) for option in options}
    return [
        str(value).strip()
        for value in values
        if isinstance(value, (str, int, float)) and str(value).strip() and str(value) not in offered
    ][:10]


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
    unmatched: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build editable defaults, including declared structured Chat extraction.

    When given, ``unmatched`` collects, per input, extracted choices that are
    not among the input's options, so the caller can ask instead of guessing.
    """
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
            proposed = _unwrap_structured_prefill_value(extracted[key], item["schema"])
            merged = _merge_schema_draft(
                values.get(key, _MISSING),
                proposed,
                item["schema"],
            )
            if merged is not _MISSING:
                values[key] = merged
            if unmatched is not None:
                outside = _choices_outside_options(proposed, item["schema"])
                if outside:
                    unmatched[key] = outside
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
    return validate_workflow_run_input_values(entrypoint.run_inputs, values, rules=entrypoint.input_rules)


class WorkflowRunInputError(ValueError):
    """Declared entry-node inputs are missing or invalid for a direct run."""

    def __init__(self, errors: dict[str, str], labels: dict[str, str]):
        self.errors = errors
        parts = []
        for path, message in errors.items():
            key, _, rest = path.partition(".")
            label = labels.get(key, key)
            parts.append(f"{label}{f' ({rest})' if rest else ''}: {message}")
        super().__init__("Workflow inputs are missing or invalid — " + "; ".join(parts))


def _walk_variable_path(variables: dict[str, Any], target: str) -> tuple[bool, Any]:
    current: Any = variables
    for part in target.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def check_declared_workflow_run_inputs(
    workflow: Any,
    *,
    variables: dict[str, Any] | None,
    trigger_data: dict[str, Any] | None,
) -> None:
    """Enforce the entry node's visible run inputs, as the Run dialog does.

    Values are read where the runner's trigger step reads them: the input's
    ``target`` path in the merged run variables first, then the flat
    ``trigger_data`` key. Hidden inputs are server/chat-populated and skipped.
    """
    run_inputs, values = _declared_run_input_values(
        workflow, variables=variables, trigger_data=trigger_data,
    )
    if not run_inputs:
        return
    # Hidden inputs are filled by the server or chat, so only rules over visible inputs apply here.
    visible = {str(item["key"]) for item in run_inputs}
    rules = [rule for rule in workflow_run_input_rules(workflow) if set(input_rule_fields(rule)) <= visible]
    try:
        validate_workflow_run_input_values(run_inputs, values, rules=rules)
    except ValueError as exc:
        # The shared validator reports ``{input path: message}`` as JSON.
        errors = json.loads(str(exc))
        raise WorkflowRunInputError(errors, _run_input_labels(run_inputs)) from None


async def check_declared_workflow_record_selections(
    db: AsyncSession,
    workflow: Any,
    *,
    variables: dict[str, Any] | None,
    trigger_data: dict[str, Any] | None,
    entity_id: str,
    workspace_id: str | None,
) -> None:
    """Reject picked record keys or Workspace project ids the input does not offer."""
    from packages.core.services.workflow_record_source import record_selection_errors

    run_inputs, values = _declared_run_input_values(
        workflow, variables=variables, trigger_data=trigger_data,
    )
    errors = await record_selection_errors(
        db, run_inputs, values, entity_id=entity_id, workspace_id=workspace_id,
    )
    if errors:
        raise WorkflowRunInputError(errors, _run_input_labels(run_inputs))


def _run_input_labels(run_inputs: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {str(item["key"]): str(item.get("label") or item["key"]) for item in run_inputs}


def _declared_run_input_values(
    workflow: Any,
    *,
    variables: dict[str, Any] | None,
    trigger_data: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Visible run inputs and the values a run would read for them."""
    run_inputs = [item for item in workflow_run_inputs(workflow) if not item.get("hidden")]
    merged = dict(_mapping(getattr(workflow, "variables", None)))
    merged.update(variables or {})
    flat = trigger_data if isinstance(trigger_data, dict) else {}
    values: dict[str, Any] = {}
    for item in run_inputs:
        key = str(item["key"])
        found, value = _walk_variable_path(merged, str(item.get("target") or key))
        if found:
            values[key] = value
        elif key in flat:
            values[key] = flat[key]
    return run_inputs, values


def apply_declared_run_input_values(
    workflow: Any,
    merged_variables: dict[str, Any],
    trigger_data: dict[str, Any] | None,
    *,
    explicit_keys: Iterable[str] = (),
) -> dict[str, Any]:
    """Let submitted run inputs override the workflow's variable defaults.

    The Run dialog and API send declared inputs as ``trigger_data``. Blueprint
    Flows keep each input's default in ``variables`` and nodes read the
    variable, so without this the default silently won over the operator's
    value. Values the caller passed explicitly in ``variables`` still win.
    """
    if not isinstance(trigger_data, dict) or not trigger_data:
        return merged_variables
    explicit = set(explicit_keys)
    result = dict(merged_variables)
    for item in workflow_run_inputs(workflow):
        key = str(item["key"])
        if key not in trigger_data:
            continue
        target = str(item.get("target") or key)
        root = target.split(".", 1)[0]
        if key in explicit or root in explicit:
            continue
        _set_variable_path(result, target, deepcopy(trigger_data[key]))
    return result


def apply_declared_run_input_defaults(
    workflow: Any,
    merged_variables: dict[str, Any],
    trigger_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill each declared input the caller left out with its declared default.

    The Run dialog submits every field pre-filled with its default; API, MCP
    and chat starts may omit optional ones. Without this, nodes render the
    literal ``{{key}}`` placeholder for an omitted input instead of its default.
    """
    flat = trigger_data if isinstance(trigger_data, dict) else {}
    result = dict(merged_variables)
    for item in workflow_run_inputs(workflow):
        key = str(item["key"])
        default = item.get("default")
        target = str(item.get("target") or key)
        if default is None or key in flat or _walk_variable_path(result, target)[0]:
            continue
        _set_variable_path(result, target, deepcopy(default))
    return result


def _set_variable_path(variables: dict[str, Any], target: str, value: Any) -> None:
    parts = target.split(".")
    current = variables
    for part in parts[:-1]:
        nested = current.get(part)
        nested = dict(nested) if isinstance(nested, dict) else {}
        current[part] = nested
        current = nested
    current[parts[-1]] = value


def required_workflow_run_inputs_without_default(workflow: Any) -> list[str]:
    """Labels of visible required inputs an unattended trigger cannot supply."""
    return [
        str(item.get("label") or item["key"])
        for item in workflow_run_inputs(workflow)
        if item.get("required")
        and not item.get("hidden")
        and (
            item.get("default") is None
            or (isinstance(item.get("default"), str) and not item["default"].strip())
        )
    ]


_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_BARE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$", re.IGNORECASE)


def with_url_scheme(value: str) -> str:
    """People type www.example.com; a URL input means https://www.example.com."""
    text = value.strip()
    if _URL_SCHEME.match(text) or not _BARE_HOST.match(text.removeprefix("//")):
        return text
    return "https://" + text.removeprefix("//")


def validate_workflow_run_input_values(
    run_inputs: Iterable[dict[str, Any]],
    values: Any,
    *,
    rules: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    run_inputs = tuple(run_inputs)
    provided = values if isinstance(values, dict) else {}
    result: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def schema_error_path(key: str, error: Any) -> str:
        parts = [str(part) for part in error.absolute_path]
        if error.validator == "required" and isinstance(error.instance, dict):
            required = error.validator_value if isinstance(error.validator_value, list) else []
            absent = [str(field) for field in required if field not in error.instance]
            # jsonschema reports one error per missing field; name the one this
            # error is about, or every missing field collapses onto the first.
            missing = next(
                (field for field in absent if repr(field) in str(error.message)),
                absent[0] if absent else "",
            )
            if missing:
                parts.append(missing)
        return ".".join([key, *parts])

    for item in run_inputs:
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
                if isinstance(item.get("schema"), dict) and item["schema"].get("format") == "uri":
                    value = with_url_scheme(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            errors[key] = f"Enter a valid {input_type} value."
            continue
        schema = item.get("schema")
        if schema is not None:
            try:
                validation_errors = sorted(
                    SchemaContractValidatorFactory.build(schema).iter_errors(value),
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
    if not errors:
        labels = {str(item["key"]): str(item.get("label") or item["key"]) for item in run_inputs}
        errors = input_rule_errors(rules, result, labels)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return result


def auto_routing_allowed(
    *,
    workspace_id: str | None,
    message: str = "",
    agent_id: str | None = None,
    manual_skill_ids: str | None = None,
    manual_skill_refs: str | None = None,
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
    if (
        agent_id
        or str(manual_skill_ids or "").strip()
        or str(manual_skill_refs or "").strip()
    ):
        return False
    # A picked @mention arrives as ``agent_id`` (above). Text like "@nasa" is
    # content -- social handles, emails -- and must not bypass the Workflows.
    if str(chat_mode or "").strip().lower() not in {"", "auto"}:
        return False
    if disable_tools or str(blocked_tools or "").strip():
        return False
    if ephemeral or str(editor_context or "").strip():
        return False
    if thread_ref_kind or thread_ref_id:
        return False
    return True


def _ambiguous_intent_candidates(
    ranked: list[tuple[float, WorkspaceChatEntrypoint]],
) -> tuple[WorkspaceChatEntrypoint, ...]:
    if not ranked:
        return ()
    top_score = ranked[0][0]
    selected: list[WorkspaceChatEntrypoint] = []
    for index, (score, entrypoint) in enumerate(ranked):
        if index < 2 or top_score - score < _MINIMUM_WINNING_MARGIN:
            selected.append(entrypoint)
    return tuple(selected)


def select_intent_match(
    entrypoints: Iterable[WorkspaceChatEntrypoint | None],
    scores: Iterable[dict[str, Any]],
) -> WorkspaceIntentClassification:
    by_id = {
        entrypoint.binding_id: entrypoint
        for entrypoint in entrypoints
        if entrypoint is not None and entrypoint.intent_enabled
    }
    if not by_id:
        return WorkspaceIntentClassification(status="none")
    best_scores: dict[str, float] = {}
    for score in scores:
        if not isinstance(score, dict):
            continue
        entrypoint = by_id.get(str(score.get("binding_id") or ""))
        if entrypoint is None:
            continue
        if entrypoint.binding_id in best_scores:
            return WorkspaceIntentClassification(status="error", reason="duplicate_binding_score")
        best_scores[entrypoint.binding_id] = _bounded_confidence(score.get("confidence"))
    if set(best_scores) != set(by_id):
        return WorkspaceIntentClassification(status="error", reason="incomplete_scores")
    ranked = [
        (best_scores[binding_id], entrypoint)
        for binding_id, entrypoint in by_id.items()
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    top_score, top_entrypoint = ranked[0]
    if top_score < top_entrypoint.minimum_confidence:
        return WorkspaceIntentClassification(status="none", reason="below_threshold")
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if len(ranked) > 1 and top_score - runner_up < _MINIMUM_WINNING_MARGIN:
        candidates = _ambiguous_intent_candidates(ranked)
        return WorkspaceIntentClassification(
            status="ambiguous",
            candidates=candidates,
            confidence=top_score,
            reason="insufficient_margin",
        )
    return WorkspaceIntentClassification(
        status="matched",
        entrypoint=top_entrypoint,
        candidates=(top_entrypoint,),
        confidence=top_score,
    )


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
    user: Any | None = None,
    require_control: bool = False,
    include_unpublished: bool = False,
    locale: str | None = None,
) -> list[WorkspaceChatEntrypoint]:
    if require_control:
        from packages.core.services.workspace_access import (
            user_can_write_workspace_artifacts,
        )

        if user is None or str(getattr(user, "entity_id", "") or "") != entity_id:
            return []
        if not await user_can_write_workspace_artifacts(
            db,
            workspace_id=workspace_id,
            user_id=user.id,
            entity_role=user.role,
        ):
            return []
    # Chat launch keeps the published-only filter. App View and operation
    # drafts pass include_unpublished so they can reference Flows that exist
    # but are not live yet, without exposing disabled or retired bindings.
    if include_unpublished:
        publication_filters = (
            WorkflowBinding.status.notin_(_RETIRED_WORKFLOW_STATUSES),
            WorkflowDefinition.status.notin_(_RETIRED_WORKFLOW_STATUSES),
        )
    else:
        publication_filters = (
            WorkflowBinding.status == "active",
            WorkflowDefinition.status == "active",
        )
    # Only the Flow fields an entry point reads; its entry nodes stand in for
    # the step graph, which can be megabytes per Flow.
    rows = (await db.execute(
        select(
            WorkflowBinding, WorkflowDefinition.id, WorkflowDefinition.name,
            WorkflowDefinition.description, WorkflowDefinition.variables, entry_steps_column(),
        )
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowDefinition.is_active.is_(True),
            *publication_filters,
        )
    )).all()
    entrypoints = [
        normalized
        for binding, *flow in rows
        if (normalized := normalize_chat_entrypoint(
            binding,
            SimpleNamespace(**dict(zip(("id", "name", "description", "variables", "steps"), flow))),
            include_unpublished=include_unpublished, locale=locale,
        )) is not None
        and (not intent_only or normalized.intent_enabled)
    ]
    return sorted(entrypoints, key=lambda item: (item.order, item.title.lower(), item.binding_id))


async def list_global_chat_flow_entrypoints(
    db: AsyncSession,
    *,
    entity_id: str,
    user: Any,
    workspace_id: str | None = None,
    require_control: bool = True,
) -> list[tuple[WorkspaceChatEntrypoint, WorkflowBinding, WorkflowDefinition, Any]]:
    """Return Workspace-published Flows the current user may invoke from Chat.

    Global Chat has no implicit Workspace boundary, so every option carries its
    owning Workspace. Callers with an active Workspace pass ``workspace_id``;
    the query and launch lookup then fail closed to that exact Workspace. Every
    result is also filtered by the same read/control policy used at launch.
    """
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_access import (
        user_readable_workspace_ids,
        user_writable_workspace_ids,
    )

    if user is None or str(getattr(user, "entity_id", "") or "") != entity_id:
        return []
    query = (
        select(WorkflowBinding, WorkflowDefinition, Workspace)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .join(Workspace, Workspace.id == WorkflowBinding.workspace_id)
        .where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
            Workspace.deleted_at.is_(None),
        )
        .order_by(Workspace.name.asc(), WorkflowBinding.name.asc(), WorkflowBinding.id.asc())
    )
    if workspace_id:
        query = query.where(WorkflowBinding.workspace_id == workspace_id)
    rows = (await db.execute(query)).all()
    candidates = [
        (entrypoint, binding, workflow, workspace)
        for binding, workflow, workspace in rows
        if (entrypoint := normalize_chat_entrypoint(binding, workflow)) is not None
    ]
    # Many Flows share a Workspace: decide each Workspace once, by the same
    # read and control policy the launch applies.
    workspace_ids = {str(item[3].id) for item in candidates}
    allowed = await user_readable_workspace_ids(
        db,
        entity_id=entity_id,
        user_id=user.id,
        role=user.role,
        workspace_ids=workspace_ids,
    ) if workspace_ids else set()
    if require_control and allowed:
        allowed &= await user_writable_workspace_ids(
            db,
            entity_id=entity_id,
            workspace_ids=allowed,
            user_id=user.id,
            role=user.role,
        )
    visible = [item for item in candidates if str(item[3].id) in allowed]
    return sorted(
        visible,
        key=lambda item: (
            str(getattr(item[3], "name", "") or "").lower(),
            item[0].order,
            item[0].title.lower(),
            item[0].binding_id,
        ),
    )


async def get_global_chat_flow_entrypoint(
    db: AsyncSession,
    *,
    entity_id: str,
    user: Any,
    binding_id: str,
    workspace_id: str | None = None,
) -> tuple[WorkspaceChatEntrypoint, WorkflowBinding, WorkflowDefinition, Any] | None:
    rows = await list_global_chat_flow_entrypoints(
        db,
        entity_id=entity_id,
        user=user,
        workspace_id=workspace_id,
        require_control=True,
    )
    return next(
        (item for item in rows if item[0].binding_id == binding_id),
        None,
    )


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
    *,
    include_workflow_free_text: bool = False,
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
        # Chinese replies, written without the spaces the prefix check needs.
        "继续",
        "继续完成",
        "继续做",
        "接着做",
        "重试",
        "再试一次",
        "确认",
        "同意",
        "批准",
        "可以",
        "好的",
        "取消",
        "停止",
        "跳过",
        "不用了",
    }
    # Only phrases about finishing the work already under way: "继续写一首新歌"
    # (go on and write a new song) is new work, not a reply to a card.
    cjk_reply_prefixes = ("继续完成", "接着完成", "继续做完", "接着做完", "重试", "再试一次")
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
        if action["kind"] in {"workflow_input", "workflow_starter_input"} and not include_workflow_free_text:
            continue
        if any(text.startswith(f"{phrase} ") for phrase in reply_phrases):
            return True
        if text.startswith(cjk_reply_prefixes) and len(text) <= 40:
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
    *,
    include_workflow_free_text: bool = False,
) -> bool:
    if not conversation_id:
        return False
    from packages.core.models.task import Message
    from packages.core.models.workflow import WorkflowRun

    pending_actions = list((await db.execute(
        select(Message.pending_action).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
            Message.pending_action["kind"].as_string().isnot(None),
            Message.resolved_at.is_(None),
        ).order_by(Message.created_at.desc()).limit(20)
    )).scalars().all())
    input_kinds = {"workflow_input", "workflow_starter_input"}
    input_run_ids = {
        str(action.get("workflow_run_id"))
        for action in pending_actions
        if isinstance(action, dict)
        and action.get("kind") in input_kinds
        and action.get("workflow_run_id")
    }
    if input_run_ids:
        live_run_ids = set((await db.execute(
            select(WorkflowRun.id).where(
                WorkflowRun.id.in_(input_run_ids),
                WorkflowRun.status.in_(("pending", "running", "paused")),
            )
        )).scalars().all())
        pending_actions = [
            action for action in pending_actions
            if not isinstance(action, dict)
            or action.get("kind") not in input_kinds
            or str(action.get("workflow_run_id") or "") in live_run_ids
        ]
    return pending_action_reply_matches(
        message, pending_actions,
        include_workflow_free_text=include_workflow_free_text,
    )


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
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    route_source: str,
    client_message_id: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
) -> WorkspaceEntrypointRun:
    from packages.core.services.conversation_lifecycle import get_or_create_conversation
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.runtime_file_context import runtime_saved_message_with_file_references
    from packages.core.services.workspace_flow_launcher import launch_workspace_flow

    conversation = await get_or_create_conversation(
        db,
        entity_id,
        user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        title=message.splitlines()[0][:100].strip() or entrypoint.title,
    )
    saved_message = runtime_saved_message_with_file_references(message, attachments)
    user_message = await add_message(
        db,
        conversation.id,
        role="user",
        content=saved_message,
        attachments=workflow_attachment_descriptors(attachments) or None,
        meta={
            "author_user_id": user_id,
            **({"client_message_id": client_message_id} if client_message_id else {}),
            "workflow_route_source": route_source,
            "workflow_binding_id": binding.id,
        },
    )
    launched = await launch_workspace_flow(
        db,
        source=route_source,
        entrypoint=entrypoint,
        binding=binding,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        origin_message_id=user_message.id,
        source_brief=message,
        attachments=attachments,
        starter_policy="always_review",
        origin_metadata={
        **({"confidence": confidence} if confidence is not None else {}),
        **({"reason": reason} if reason else {}),
        },
    )
    return WorkspaceEntrypointRun(
        run=launched.run,
        conversation=conversation,
        user_message=user_message,
        activity_message=launched.activity_message,
    )


def _strip_flow_choice_prefix(message: str) -> str:
    text = str(message or "").strip()
    folded = text.casefold()
    for prefix in ("%flow", "% flow", "%"):
        if folded.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _intent_choice_ordinal(text: str) -> int | None:
    stripped = str(text or "").strip()
    digit = re.fullmatch(r"(\d+)\s*[.。、)]?", stripped)
    if digit:
        return int(digit.group(1))
    numbered = re.fullmatch(r"(?:用|选|选择)?\s*(\d+)\s*(?:条|个|号)?", stripped)
    if numbered:
        return int(numbered.group(1))
    indexed = re.fullmatch(r"第\s*(\d+)\s*条?", stripped)
    if indexed:
        return int(indexed.group(1))
    chinese_digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    chinese = re.fullmatch(r"第\s*([一二三四五六七八九十])\s*条?", stripped)
    if chinese:
        return chinese_digits.get(chinese.group(1))
    return None


def match_workspace_intent_choice(
    message: str,
    candidates: Iterable[WorkspaceChatEntrypoint | None],
) -> tuple[WorkspaceChatEntrypoint, ...]:
    items = [item for item in candidates if item is not None]
    text = _strip_flow_choice_prefix(message)
    if not items or not text:
        return ()
    folded = text.casefold()
    named = tuple(
        item
        for item in items
        if item.title.strip().casefold() == folded or item.binding_id.casefold() == folded
    )
    if named:
        return named
    ordinal = _intent_choice_ordinal(text)
    if ordinal is None or ordinal < 1 or ordinal > len(items):
        return ()
    return (items[ordinal - 1],)


def workspace_intent_choice_prompt(
    source_brief: str,
    candidates: Iterable[WorkspaceChatEntrypoint | None],
) -> str:
    items = [item for item in candidates if item is not None]
    numbered = "\n".join(
        f"{index}. {item.title}"
        for index, item in enumerate(items, start=1)
    )
    if any("\u4e00" <= char <= "\u9fff" for char in str(source_brief or "")):
        intro = "有几条 workflow 都能做这件事，第一条最贴近你的要求。选一条开始，或直接告诉我你的想法："
    else:
        intro = (
            "A few workflows can do this; the first fits your request best. "
            "Pick one to start, or tell me what you have in mind:"
        )
    return f"{intro}\n\n{numbered}" if numbered else intro


def workspace_intent_choice_question(items: tuple[WorkspaceChatEntrypoint, ...]) -> dict[str, Any] | None:
    """The choice as a question with buttons, the best match recommended.

    ``answer_as: "message"``: a click sends the option as the person's reply,
    the same text :func:`match_workspace_intent_choice` already understands,
    so the choice resolves on the path a typed reply takes.
    """
    from packages.core.services.decision_recommendation import ensure_recommendation, recommendation

    titles = list(dict.fromkeys(item.title for item in items if item.title))
    if not titles:
        return None
    return ensure_recommendation({
        "options": titles,
        "recommendation": recommendation(titles[0], reason_code="best_match"),
        "answer_as": "message",
    })


async def _load_intent_choice_conversation(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    conversation_id: str | None,
):
    from packages.core.models.task import Conversation

    if conversation_id:
        return (await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
        )).scalar_one_or_none()
    if not workspace_id:
        return None
    return (await db.execute(
        select(Conversation).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Conversation.scope == "workspace_main",
        ).limit(1)
    )).scalar_one_or_none()


async def persist_workspace_intent_choice(
    db: AsyncSession,
    *,
    candidates: Iterable[WorkspaceChatEntrypoint | None],
    entity_id: str,
    user_id: str,
    workspace_id: str,
    message: str,
    attachments: Any = None,
    conversation_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    client_message_id: str | None = None,
    source_brief: str | None = None,
) -> WorkspaceIntentChoicePrompt:
    from packages.core.services.conversation_lifecycle import get_or_create_conversation
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.runtime_file_context import runtime_saved_message_with_file_references

    items = tuple(item for item in candidates if item is not None)
    brief = str(source_brief or message or "").strip() or str(message or "").strip()
    conversation = await get_or_create_conversation(
        db,
        entity_id,
        user_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        title=(
            str(message or "").splitlines()[0][:100].strip()
            or (items[0].title if items else "Workspace")
        ),
    )
    saved_message = runtime_saved_message_with_file_references(message, attachments)
    user_message = await add_message(
        db,
        conversation.id,
        role="user",
        content=saved_message,
        attachments=workflow_attachment_descriptors(attachments) or None,
        meta={
            "author_user_id": user_id,
            **({"client_message_id": client_message_id} if client_message_id else {}),
            "workflow_route_source": "intent_choice",
        },
    )
    assistant_message = await add_message(
        db,
        conversation.id,
        role="assistant",
        content=workspace_intent_choice_prompt(brief, items),
        meta={
            "kind": "workspace_intent_choice",
            "candidate_binding_ids": [item.binding_id for item in items],
            "source_brief": brief,
            "origin_message_id": user_message.id,
            "question": workspace_intent_choice_question(items),
        },
    )
    return WorkspaceIntentChoicePrompt(
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
    )


async def resolve_workspace_intent_choice(
    db: AsyncSession,
    *,
    message: str,
    entrypoints: Iterable[WorkspaceChatEntrypoint | None],
    entity_id: str,
    user_id: str,
    workspace_id: str,
    conversation_id: str | None = None,
) -> WorkspaceIntentClassification:
    from packages.core.models.task import Message

    conversation = await _load_intent_choice_conversation(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        return WorkspaceIntentClassification(status="none")
    last = (await db.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
    )).scalar_one_or_none()
    meta = last.meta if last is not None and isinstance(last.meta, dict) else {}
    if str(meta.get("kind") or "") != "workspace_intent_choice":
        return WorkspaceIntentClassification(status="none")
    source_brief = str(meta.get("source_brief") or "").strip()
    candidate_ids = [
        str(item).strip()
        for item in (meta.get("candidate_binding_ids") or [])
        if str(item).strip()
    ]
    by_id = {
        item.binding_id: item
        for item in entrypoints
        if item is not None
    }
    candidates = tuple(
        by_id[binding_id]
        for binding_id in candidate_ids
        if binding_id in by_id
    )
    if not candidates:
        return WorkspaceIntentClassification(status="none")
    matched = match_workspace_intent_choice(message, candidates)
    if len(matched) == 1:
        return WorkspaceIntentClassification(
            status="matched",
            entrypoint=matched[0],
            candidates=matched,
            confidence=1.0,
            reason="user_choice",
            source_brief=source_brief,
        )
    if len(matched) > 1:
        return WorkspaceIntentClassification(
            status="ambiguous",
            candidates=matched,
            reason="user_choice_ambiguous",
            source_brief=source_brief,
        )
    return WorkspaceIntentClassification(status="none")


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
                "First classify the requested action as now, scheduled, contextual, or conversation. "
                "Use now only for an explicit, self-contained request to perform one action on this turn. "
                "Use scheduled for future, recurring, or mixed immediate-and-future work; arranging "
                "a schedule now is not a request to execute its deliverable now. "
                "Use contextual when an essential requested input refers to earlier conversation "
                "that is not provided here; an omitted optional input is not such a reference. "
                "Use conversation for questions, discussion, and other requests not to execute work. "
                "Only for now, classify whether the request should start one of the configured "
                "Workspace workflows. "
                "Score every candidate from 0 to 1 by semantic meaning, not keyword overlap or similarity "
                "to examples. Compare the requested outcome, necessary operations, and external effects with "
                "each candidate's end-to-end work. Treat subject, title, style, language, and other content "
                "details as inputs rather than separate intents; recognize paraphrases and mixed languages. "
                "A high score requires an explicit request to perform the work, not a question about it. "
                "Do not infer unrequested downstream actions from Workspace context or candidate defaults. "
                "Candidates requiring additional external effects must score low; candidates omitting an "
                "explicitly requested effect must also score lower. Do not assign near-equal high scores "
                "merely because candidates share an initial step. Use negative_examples to distinguish "
                "lookalike requests. Return JSON only "
                'with shape {"request_mode":"now|scheduled|contextual|conversation",'
                '"scores":[{"binding_id":"...","confidence":0.0,"reason":"..."}]}.'
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
) -> WorkspaceIntentClassification:
    if not entrypoints:
        return WorkspaceIntentClassification(status="none", source_brief=message)
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
        request_mode = parsed.get("request_mode") if isinstance(parsed, dict) else None
        if request_mode not in {"now", "scheduled", "contextual", "conversation"}:
            return WorkspaceIntentClassification(
                status="error",
                reason="invalid_request_mode",
                source_brief=message,
            )
        if request_mode != "now":
            return WorkspaceIntentClassification(
                status="none",
                reason=f"request_mode_{request_mode}",
                source_brief=message,
                request_mode=request_mode,
            )
        scores = parsed.get("scores") if isinstance(parsed, dict) else None
        if not isinstance(scores, list):
            return WorkspaceIntentClassification(
                status="error",
                reason="invalid_scores",
                source_brief=message,
                request_mode=request_mode,
            )
        decision = select_intent_match(entrypoints, scores)
        if decision.status != "matched" or decision.entrypoint is None:
            return WorkspaceIntentClassification(
                status=decision.status,
                entrypoint=decision.entrypoint,
                candidates=decision.candidates,
                confidence=decision.confidence,
                reason=decision.reason,
                source_brief=message,
                request_mode=request_mode,
            )
        selected_score = next((
            score
            for score in scores
            if isinstance(score, dict)
            and str(score.get("binding_id") or "") == decision.entrypoint.binding_id
        ), {})
        return WorkspaceIntentClassification(
            status="matched",
            entrypoint=decision.entrypoint,
            candidates=decision.candidates,
            confidence=_bounded_confidence(
                selected_score.get("confidence"),
                decision.confidence,
            ),
            reason=str(selected_score.get("reason") or "").strip(),
            source_brief=message,
            request_mode=request_mode,
        )
    except Exception:
        logger.info("Workspace Workflow intent classification fell back to Chat", exc_info=True)
        return WorkspaceIntentClassification(
            status="error",
            reason="classifier_failed",
            source_brief=message,
        )
