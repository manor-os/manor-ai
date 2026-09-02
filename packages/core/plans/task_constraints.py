"""The user's binding, task-scoped constraints — one extractor, two consumers.

Constraints the user stated for THIS task (e.g. "只保存表格,不要写 essay") are
captured by ``workspace_create_task`` into ``task.details.runtime_context``
(``instructions`` + ``rules``). Historically they reached neither the planner
as anything but an opaque ``Details JSON`` blob, nor the executing subagent at
all (the worker snapshot dropped them). This module is the single source of
"what did the user bind this task to", rendered verbatim so both the planner
prompt and the subagent prompt carry the exact words — never a paraphrase.
"""
from __future__ import annotations

import re
from typing import Any


_ARTIFACT_WRITE_PROHIBITION_PATTERNS = (
    re.compile(
        r"\b(?:do not|don't|must not|never)\s+"
        r"(?:create|write|generate|save|export|attach)"
        r"(?:\s+or\s+(?:create|write|generate|save|export|attach))*\s+"
        r"(?:any\s+)?(?:new\s+)?(?:files?|artifacts?|attachments?)\b"
    ),
    re.compile(
        r"\bwithout\s+"
        r"(?:creating|writing|generating|saving|exporting|attaching)"
        r"(?:\s+or\s+(?:creating|writing|generating|saving|exporting|attaching))*\s+"
        r"(?:any\s+)?(?:new\s+)?(?:files?|artifacts?|attachments?)\b"
    ),
    re.compile(
        r"\b(?:prohibit(?:s|ed)?|forbid(?:s|den)?)\s+"
        r"(?:creating|writing|generating|saving|exporting|attaching)"
        r"(?:\s+or\s+(?:creating|writing|generating|saving|exporting|attaching))*\s+"
        r"(?:any\s+)?(?:new\s+)?(?:files?|artifacts?|attachments?)\b"
    ),
    re.compile(
        r"\bno\s+(?:saved\s+|generated\s+)"
        r"(?:files?|artifacts?|attachments?|file attachments?)\b"
    ),
    re.compile(
        r"\bno\s+(?:files?|artifacts?|attachments?|file attachments?)\b"
        r"(?="
        r"\s+(?:(?:should|must|may|can|will)\s+)?(?:be\s+)?"
        r"(?:created|written|generated|saved|exported|attached)\b"
        r"|\s*(?:[.!;,]|$)"
        r")"
    ),
)
_ARTIFACT_WRITE_PROHIBITION_TERMS = (
    "不要创建文件",
    "不要写入文件",
    "不要写文件",
    "禁止创建文件",
    "禁止写入文件",
    "禁止写文件",
    "不生成文件",
    "不要生成文件",
    "不要创建制品",
    "不要生成制品",
    "不要创建附件",
    "不要生成附件",
)
_ARTIFACT_OUTPUT_SHAPES = {"ArtifactResult", "DocumentResult"}
_ARTIFACT_SCHEMA_KEYS = {
    "artifact_path",
    "artifact_url",
    "artifacts",
    "document_id",
    "document_url",
    "download_url",
    "file_path",
    "file_url",
    "files",
    "fs_path",
    "image_url",
    "images",
    "output_path",
    "saved_to",
    "video_url",
}


def extract_binding_constraints(details: Any) -> list[str]:
    """Verbatim, task-scoped constraints from ``task.details``.

    Pulls ``runtime_context.instructions`` (the user's task-only instruction)
    and each ``runtime_context.rules[*].description`` (task guardrails). Order
    preserved, de-duplicated, empty entries dropped. Tolerant of missing/odd
    shapes — returns [] rather than raising, since it runs on every plan.
    """
    if not isinstance(details, dict):
        return []
    rc = details.get("runtime_context")
    if not isinstance(rc, dict):
        return []

    out: list[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    instructions = rc.get("instructions")
    if isinstance(instructions, (list, tuple)):
        for item in instructions:
            _add(item)
    else:
        _add(instructions)

    rules = rc.get("rules")
    if isinstance(rules, (list, tuple)):
        for rule in rules:
            if isinstance(rule, dict):
                _add(rule.get("description") or rule.get("rule") or rule.get("text"))
            else:
                _add(rule)

    return out


def render_constraints_block(constraints: list[str], *, heading: str = "USER CONSTRAINTS") -> str:
    """A prominently-framed, binding block for injection into a prompt.

    Empty ⇒ empty string (nothing to inject). The framing is deliberately
    strong: these are the user's own words and must be obeyed as-given,
    including prohibitions like "do not write an essay"."""
    if not constraints:
        return ""
    lines = "\n".join(f"- {c}" for c in constraints)
    return (
        f"## {heading} (verbatim — binding, obey exactly, including any prohibitions)\n"
        f"{lines}"
    )


def binding_constraints_forbid_artifact_writes(details: Any) -> bool:
    """Whether the user's task-only wording explicitly forbids saved output.

    This intentionally reads only the verbatim binding constraint block. A
    read-only source mount or an Agent's general role description is not a
    prohibition on creating a separate Workspace artifact.
    """

    for constraint in extract_binding_constraints(details):
        text = " ".join(constraint.lower().split())
        if any(pattern.search(text) for pattern in _ARTIFACT_WRITE_PROHIBITION_PATTERNS):
            return True
        if any(term in text for term in _ARTIFACT_WRITE_PROHIBITION_TERMS):
            return True
        if "file attachment" in text and any(
            marker in text for marker in ("do not create", "don't create", "must not create", "never create")
        ):
            return True
    return False


def plan_step_requires_artifact_write(step: Any) -> bool:
    """Whether a Plan step structurally requires a saved file artifact."""

    def value(name: str, default: Any = None) -> Any:
        if isinstance(step, dict):
            return step.get(name, default)
        return getattr(step, name, default)

    if str(value("capability_id") or "") == "file.write":
        return True
    if str(value("output_shape") or "") in _ARTIFACT_OUTPUT_SHAPES:
        return True
    if "files" in {
        str(expect).strip().lower()
        for expect in (value("expects", []) or [])
        if str(expect).strip()
    }:
        return True
    return _schema_requires_artifact(value("expected_output_schema"))


def plan_requires_artifact_write(plan_dag: Any) -> bool:
    if not isinstance(plan_dag, dict):
        return False
    steps = plan_dag.get("steps")
    return isinstance(steps, list) and any(
        plan_step_requires_artifact_write(step)
        for step in steps
        if isinstance(step, dict)
    )


def _schema_requires_artifact(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if isinstance(properties, dict):
        required = {
            str(key).lower()
            for key in (schema.get("required") or [])
            if str(key).strip()
        }
        if required & _ARTIFACT_SCHEMA_KEYS:
            return True
        if any(
            _schema_requires_artifact(value)
            for key, value in properties.items()
            if str(key).lower() in required
        ):
            return True
    items = schema.get("items")
    return isinstance(items, dict) and _schema_requires_artifact(items)
