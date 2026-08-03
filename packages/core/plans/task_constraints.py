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

from typing import Any


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
