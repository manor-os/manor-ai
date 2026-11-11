"""Step-local delivery intent and Workflow tool-operation denials.

Payload JSON Schema describes data, not permission.  A Step's execution
contract is a closed, server-owned declaration that a human resume cannot
widen.  A Workflow step's operation denylist follows nested Skill/Agent calls
and only narrows.  All ordinary Agent/governance gates still apply.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict


STEP_EXECUTION_CONTRACT_PARAM = "_execution_contract"


class StepDelivery(StrEnum):
    INLINE = "inline"
    ARTIFACT = "artifact"


class StepEffects(StrEnum):
    READ_ONLY = "read_only"
    ARTIFACT_WRITE = "artifact_write"
    AUTHORIZED = "authorized"


class StepExecutionContract(BaseModel):
    """A further restriction, never a grant of the selected Agent's tools.

    Artifact delivery may reuse an existing artifact under read_only. Inline
    delivery may summarize an authorized write. Neither axis infers the other.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    delivery: StepDelivery
    effects: StepEffects


# A Workflow agent step may refuse named operations of a tool, e.g.
# ``{"video_edit": ["render"]}`` for a step that must stop at review. The
# scope follows nested Skill/Agent calls and nested scopes only add denials.
TOOL_OPERATION_DENYLIST_KEY = "tool_operation_denylist"
_tool_operation_denylists: ContextVar[tuple[dict[str, frozenset[str]], ...]] = ContextVar(
    "runtime_step_tool_operation_denylists", default=(),
)


def parse_tool_operation_denylist(value: Any) -> dict[str, frozenset[str]]:
    """Return ``{tool: operations}`` or raise ValueError for a malformed declaration."""
    if not isinstance(value, dict) or not value:
        raise ValueError("must be a non-empty object of tool name -> operation list")
    parsed: dict[str, frozenset[str]] = {}
    for tool_name, operations in value.items():
        name = str(tool_name or "").strip()
        if not name or not isinstance(operations, list) or not operations or not all(
            isinstance(item, str) and item.strip() for item in operations
        ):
            raise ValueError(f"'{tool_name}' must map to a non-empty list of operation names")
        parsed[name] = frozenset(item.strip().lower() for item in operations)
    return parsed


@contextmanager
def tool_operation_denylist_scope(denylist: dict[str, Any] | None) -> Iterator[None]:
    if not denylist:
        yield
        return
    token = _tool_operation_denylists.set(
        (*_tool_operation_denylists.get(), parse_tool_operation_denylist(denylist)),
    )
    try:
        yield
    finally:
        _tool_operation_denylists.reset(token)


def tool_operation_denied_error(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Pure pre-I/O refusal of an operation the current Workflow step denies."""
    operation = str((arguments or {}).get("operation") or "").strip().lower()
    if not operation or not any(
        operation in denylist.get(tool_name, ()) for denylist in _tool_operation_denylists.get()
    ):
        return None
    return (
        f"This Workflow step does not permit {tool_name}(operation='{operation}'). "
        "No action was executed. A later step of this Flow owns that operation; "
        "finish this step's own work and report its result."
    )


# A Workflow agent step may declare what it must leave behind, e.g.
# ``"expects": ["files"]`` for a step that saves a draft. The host checks the
# run's receipt ledger, never the model's own summary: a step whose declared
# output was not produced does not complete.
STEP_EXPECTS_KEY = "expects"


class StepExpectation(StrEnum):
    # At least one saved file (a Document or workspace file receipt).
    FILES = "files"
    # At least one confirmed external action (a sent message, a published post ...).
    EFFECTS = "effects"


# Approval categories of an action that reached people outside the Workspace.
# Status checks, lookups and in-Workspace changes are actions too, but none of
# them is the outside action an ``effects`` step promises.
OUTWARD_EFFECT_CATEGORIES = frozenset({"communication", "publishing"})


def parse_step_expectations(value: Any) -> tuple[StepExpectation, ...]:
    """Return the declared expectations or raise ValueError for a malformed list."""
    if not isinstance(value, list) or not value:
        raise ValueError(
            "must be a non-empty list of " + ", ".join(f"'{item.value}'" for item in StepExpectation)
        )
    parsed: list[StepExpectation] = []
    for item in value:
        try:
            expectation = StepExpectation(str(item).strip().lower())
        except ValueError:
            raise ValueError(
                f"has unknown value {item!r}; use "
                + ", ".join(f"'{member.value}'" for member in StepExpectation)
            ) from None
        if expectation not in parsed:
            parsed.append(expectation)
    return tuple(parsed)
