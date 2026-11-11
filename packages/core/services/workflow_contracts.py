"""Normalize Workflow nodes to explicit, typed data-flow contracts.

The runner has always exposed every completed node under ``{{node_id}}`` and
under an optional ``output_var`` alias.  Blueprint-authored workflows may use
those variables directly without declaring ``config.inputs`` /
``config.outputs``.  That is executable, but it leaves the editor unable to
show an auditable source-to-target mapping.  The helpers in this module turn
that implicit contract into persisted binding rows without changing the
expressions a node executes.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


def workflow_step_label(step: Any) -> str:
    """What a step is called for people: its name, else its id read as words.

    A Flow written without step names keeps its ids (``owner_approval``) as
    names; people see "Owner approval", never the id.
    """
    if not isinstance(step, dict):
        return ""
    step_id = str(step.get("id") or "").strip()
    name = str(step.get("name") or "").strip()
    if name and name != step_id:
        return name
    words = " ".join(step_id.replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:]


CONFIRMATION_CHOICES = frozenset({"confirm", "done"})
# Choices of a generic review. A wait offering only these says nothing about
# whether the person judges work or reports something they did themselves.
_GENERIC_REVIEW_CHOICES = frozenset({
    "approve", "approved", "accept", "revise", "request_changes", "reject", "cancel",
})
_CONFIRMATION_ASK_RE = re.compile(r"\bconfirm\b|\bmark\b.*\b(done|complete)\b|确认", re.IGNORECASE)
_REVIEW_ASK_RE = re.compile(
    r"\b(approve|review|reject|revise|revision|sign[ -]off)\b|批准|审批|审核|审阅|修改",
    re.IGNORECASE,
)


def workflow_wait_is_confirmation(config: Any) -> bool:
    """Whether an approval wait asks the person to confirm what they did.

    ``options: ['confirm']`` says so. Flows written before that choice existed
    ask a confirmation with the generic approve/revise choices; their message
    then asks to confirm and never to approve or review, and they read the same.
    """
    if not isinstance(config, dict) or str(config.get("wait_type") or "approval") != "approval":
        return False
    options = {str(option).strip().lower() for option in config.get("options") or [] if str(option or "").strip()}
    if options and options <= CONFIRMATION_CHOICES:
        return True
    if options - _GENERIC_REVIEW_CHOICES or isinstance(config.get("review"), dict):
        return False
    message = str(config.get("message") or "")
    return bool(_CONFIRMATION_ASK_RE.search(message)) and not _REVIEW_ASK_RE.search(message)


def workflow_wait_options(config: Any) -> list[str] | None:
    """The choices a person is offered at an approval wait, if it names them."""
    if workflow_wait_is_confirmation(config):
        options = [str(option).strip() for option in config.get("options") or []]
        return [option for option in options if option.lower() in CONFIRMATION_CHOICES] or ["confirm"]
    options = config.get("options") if isinstance(config, dict) else None
    return [str(option) for option in options] if isinstance(options, list) and options else None


_TEMPLATE_REFERENCE_RE = re.compile(
    r"\{\{\s*([A-Za-z_][\w-]*(?:\.[\w-]+)*)[^}]*\}\}"
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][\w-]*")
_QUOTED_TEXT_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_LOCAL_REFERENCE_ROOTS = {"item", "index", "loop", "input", "inputs"}
_EXPRESSION_KEYWORDS = {
    "and", "or", "not", "in", "is", "true", "false", "null", "none",
}
_NON_INPUT_CONFIG_KEYS = {
    "inputs",
    "outputs",
    "run_inputs",
    "output_var",
    "response_variable",
    "output_schema",
    "input_schema",
    "schema",
    "next",
    "true_next",
    "false_next",
    "default_next",
}


def _collect_template_roots(value: Any, roots: set[str]) -> None:
    if isinstance(value, str):
        for match in _TEMPLATE_REFERENCE_RE.finditer(value):
            root = match.group(1).split(".", 1)[0]
            if root and root not in _LOCAL_REFERENCE_ROOTS:
                roots.add(root)
        return
    if isinstance(value, list):
        for item in value:
            _collect_template_roots(item, roots)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in _NON_INPUT_CONFIG_KEYS:
            continue
        _collect_template_roots(item, roots)


def _collect_expression_roots(config: dict[str, Any], roots: set[str]) -> None:
    """Collect bare variables from IF / Switch / Filter expressions.

    Conditions intentionally allow ``packet.approved == true`` without
    mustaches, so template-only inference cannot make those inputs visible.
    Quoted literals and property names after ``.`` are ignored.
    """
    expressions: list[str] = []
    for key in ("expression", "condition"):
        if isinstance(config.get(key), str):
            expressions.append(config[key])
    for case in config.get("cases") or []:
        if isinstance(case, dict) and isinstance(case.get("expression"), str):
            expressions.append(case["expression"])

    for expression in expressions:
        scrubbed = _QUOTED_TEXT_RE.sub("", expression)
        for match in _IDENTIFIER_RE.finditer(scrubbed):
            token = match.group(0)
            prefix = scrubbed[:match.start()].rstrip()
            if prefix.endswith("."):
                continue
            if token.lower() in _EXPRESSION_KEYWORDS:
                continue
            if token not in _LOCAL_REFERENCE_ROOTS:
                roots.add(token)


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (dict, list)):
        return "json"
    if isinstance(value, str):
        return "text"
    return "any"


def _step_output_type(step: dict[str, Any]) -> str:
    step_type = str(step.get("type") or "").lower()
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    if step_type in {"condition", "filter"}:
        return "boolean"
    if step_type in {"image", "video", "audio"}:
        return step_type
    if step_type == "media":
        return str(config.get("kind") or "any").lower()
    if step_type in {
        "rag", "extract", "code", "workflow_project", "workflow_effect_deadline", "publication_receipt", "subworkflow",
        "foreach_subworkflow", "loop", "merge", "aggregate", "split",
        "sort", "dedupe",
    }:
        return "json"
    output_schema = config.get("output_schema")
    if (
        str(config.get("output_format") or "").lower() == "json"
        or isinstance(output_schema, dict)
        and output_schema.get("type") in {"object", "array"}
    ):
        return "json"
    if step_type in {"agent", "llm", "classifier", "notify", "respond"}:
        return "text"
    return "any"


def _existing_binding_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _legacy_input_rows(
    value: Any,
    variable_types: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if not str(key).strip():
            continue
        item_type = _value_type(item)
        if isinstance(item, str):
            match = re.fullmatch(r"\s*\{\{\s*([^{}]+?)\s*\}\}\s*", item)
            if match:
                root = match.group(1).strip().split(".", 1)[0]
                item_type = variable_types.get(root, item_type)
        rows.append({
            "key": str(key),
            "value": deepcopy(item),
            "type": item_type,
        })
    return rows


def explicit_workflow_step_contracts(steps: list[dict] | None) -> list[dict]:
    """Return steps with persisted input/output rows for every runnable node.

    Existing binding rows win. Missing rows are inferred from template and
    condition references. Legacy ``{"input": ...}`` maps become the current
    ``[{"key": "input", "value": ...}]`` form, preserving structured values.
    """
    normalized = deepcopy(steps or [])
    variable_types: dict[str, str] = {}

    # Discover all producers first so input typing does not depend on array
    # order (branch nodes are not necessarily stored topologically).
    for step in normalized:
        if not isinstance(step, dict) or not step.get("id"):
            continue
        step_id = str(step["id"])
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        output_type = _step_output_type(step)
        variable_types[step_id] = output_type
        for row in _existing_binding_rows(config.get("outputs")):
            key = str(row.get("key") or row.get("name") or "").strip()
            if key:
                variable_types[key] = str(row.get("type") or output_type or "any")
        output_var = str(config.get("output_var") or "").strip()
        if output_var:
            variable_types[output_var] = output_type
        response_variable = str(config.get("response_variable") or "").strip()
        if response_variable:
            variable_types[response_variable] = "json"

    for step in normalized:
        if not isinstance(step, dict) or not step.get("id"):
            continue
        step_id = str(step["id"])
        config = dict(step.get("config") or {})

        raw_inputs = config.get("inputs")
        input_rows = _existing_binding_rows(raw_inputs)
        if not input_rows:
            input_rows = _legacy_input_rows(raw_inputs, variable_types)
        input_keys = {
            str(row.get("key") or row.get("name") or "").strip()
            for row in input_rows
        }
        roots: set[str] = set()
        _collect_template_roots(config, roots)
        if str(step.get("type") or "").lower() in {"condition", "switch", "filter"}:
            _collect_expression_roots(config, roots)
        for root in sorted(roots):
            if root in input_keys:
                continue
            input_rows.append({
                "key": root,
                "value": "{{" + root + "}}",
                "type": variable_types.get(root, "any"),
            })
            input_keys.add(root)
        if str(step.get("type") or "").lower() == "end":
            for row in input_rows:
                if str(row.get("key") or "").strip() != "input":
                    continue
                value = row.get("value")
                if not isinstance(value, str):
                    continue
                match = re.fullmatch(r"\s*\{\{\s*([^{}]+?)\s*\}\}\s*", value)
                if not match:
                    continue
                root = match.group(1).strip().split(".", 1)[0]
                source_type = variable_types.get(root)
                if source_type and source_type not in {"any", "text"}:
                    row["type"] = source_type
        config["inputs"] = input_rows

        output_type = _step_output_type(step)
        output_rows = _existing_binding_rows(config.get("outputs"))
        output_keys = {
            str(row.get("key") or row.get("name") or "").strip()
            for row in output_rows
        }
        output_var = str(config.get("output_var") or "").strip()
        if output_var and output_var not in output_keys:
            output_rows.append({
                "key": output_var,
                "value": "{{" + step_id + "}}",
                "type": output_type,
            })
            output_keys.add(output_var)
        response_variable = str(config.get("response_variable") or "").strip()
        if response_variable and response_variable not in output_keys:
            output_rows.append({
                "key": response_variable,
                "value": "",
                "type": "json",
            })
            output_keys.add(response_variable)
        if not output_rows:
            terminal_type = (
                str(input_rows[0].get("type") or output_type)
                if str(step.get("type") or "").lower() == "end" and input_rows
                else output_type
            )
            output_rows.append({
                "key": step_id,
                "value": "{{" + step_id + "}}",
                "type": terminal_type or "any",
            })
        elif str(step.get("type") or "").lower() == "end" and input_rows:
            terminal_type = str(input_rows[0].get("type") or output_type or "any")
            for row in output_rows:
                if (
                    str(row.get("key") or "").strip() == step_id
                    and row.get("value") == "{{" + step_id + "}}"
                ):
                    row["type"] = terminal_type
        config["outputs"] = output_rows
        step["config"] = config

    return normalized
