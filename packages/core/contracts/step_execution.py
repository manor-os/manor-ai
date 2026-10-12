"""Step-local delivery intent and a narrowing-only execution effect boundary.

Payload JSON Schema describes data, not permission.  These declarations are
persisted by Plan materialization and installed by the worker, never taken
from tool arguments.  The context follows nested Skill/Agent calls and cannot
widen an outer Step's limit.  All ordinary Agent/governance gates still apply.
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


_EFFECT_ORDER = {
    StepEffects.READ_ONLY: 0,
    StepEffects.ARTIFACT_WRITE: 1,
    StepEffects.AUTHORIZED: 2,
}
_step_contracts: ContextVar[tuple[StepExecutionContract, ...]] = ContextVar(
    "runtime_step_execution_contracts", default=(),
)


@contextmanager
def step_execution_scope(
    contract: StepExecutionContract | dict[str, Any] | None,
) -> Iterator[None]:
    """Install a host-loaded Step restriction; absent legacy fields inherit."""
    if contract is None:
        yield
        return
    parsed = StepExecutionContract.model_validate(contract)
    token = _step_contracts.set((*_step_contracts.get(), parsed))
    try:
        yield
    finally:
        _step_contracts.reset(token)


def current_step_effects() -> StepEffects | None:
    contracts = _step_contracts.get()
    return min((item.effects for item in contracts), key=_EFFECT_ORDER.__getitem__) if contracts else None


def step_execution_prompt() -> str:
    contracts = _step_contracts.get()
    if not contracts:
        return ""
    contract = contracts[-1]
    effects = current_step_effects()
    delivery = (
        "Return this Step's payload inline. Do not turn an intermediate result "
        "into the whole Task's final deliverable."
        if contract.delivery is StepDelivery.INLINE
        else "Deliver a real, readable artifact reference. Reuse an existing "
        "artifact when that satisfies this Step; a filename alone is not evidence."
    )
    restriction = {
        StepEffects.READ_ONLY: (
            "This Step is read-only: do not create/change files, business records, "
            "or external state, including inside a Skill. After user input, return "
            "only this Step's declared result; later Steps own their own work."
        ),
        StepEffects.ARTIFACT_WRITE: (
            "This Step may create/modify its requested local artifacts, but may not "
            "publish/send externally or mutate other business records."
        ),
        StepEffects.AUTHORIZED: (
            "Only actions authorized by the Agent configuration and the Task scope "
            "may run; this contract adds no tool or approval permissions."
        ),
    }[effects]
    return f"[Runtime Step execution contract]\n{delivery}\n{restriction}"


def step_contract_tool_error(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    declared_effect: str | None = None,
) -> str | None:
    """Pure pre-I/O restriction using the same trusted tool classifier as Runtime."""
    effects = current_step_effects()
    if effects is None or effects is StepEffects.AUTHORIZED:
        return None
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    classification = classify_runtime_tool(
        tool_name, arguments, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task_id, declared_effect=declared_effect,
    )
    if classification.effect is RuntimeToolEffect.READ_ONLY:
        return None
    if (
        classification.effect is RuntimeToolEffect.CONTROL
        and tool_name in {"submit_result", "request_input", "sandbox_destroy"}
    ):
        return None
    if classification.effect is RuntimeToolEffect.ORCHESTRATOR and tool_name == "invoke_skill":
        # Prompt Skill tools re-enter this boundary; sandbox Skills separately
        # reject restricted Step scopes before executing unclassified code.
        return None
    action_key = getattr(classification.action, "action_key", None)
    if tool_name == "request_input" and action_key == "human.input.request":
        # Asking for input persists framework continuation state, not business
        # work. Its ordinary exact HITL and runtime gates still apply.
        return None
    if (
        effects is StepEffects.ARTIFACT_WRITE
        and classification.effect is RuntimeToolEffect.ACTION
        and action_key in {"workspace.file.create", "workspace.file.modify", "workspace.file.write"}
    ):
        return None
    return (
        f"Step execution contract effects={effects.value} does not permit tool "
        f"'{tool_name}'. No action was executed. Keep this Step's result within "
        "its declared scope; do not perform a downstream Step's work."
    )


# A Workflow agent step may refuse named operations of a tool, e.g.
# ``{"video_edit": ["render"]}`` for a step that must stop at review. Like the
# effect contract above, the scope follows nested Skill/Agent calls and nested
# scopes only add denials.
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
