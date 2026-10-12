"""Coordinator mode: one durable control step coordinates typed child steps.

Step-by-step execution of a planned DAG fails in ways multi-agent chat does
not: a ``${{ steps.x.result.y }}`` ref that lands on a slightly different
shape, a worker that never picks a step up, a supervisor that cannot send
feedback back into the run.  Coordinator mode keeps the planner's work — the
DAG is still produced, validated and allow-listed — but persists it as a
single logical ``coordinate`` root whose prompt carries the DAG as a
checklist. Every checklist item is still materialised as its original typed,
durable ``ExecutionStep``. The root is therefore one control plane, not one
serial unit of work.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_INPUT_CONTEXT_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_TYPED_VERSION,
    COORDINATOR_VERSION_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    DELEGATE_TO_SERVICE_TOOL_NAME,
)
from packages.core.constants.supervisor import SUPERVISOR_STEP_RETRY_FLAG
from packages.core.plans.refs import _REF_RE
from packages.core.plans.schema import Plan, PlanMetadata, PlanStep
from packages.core.plans.task_constraints import (
    extract_binding_constraints,
    render_constraints_block,
)

if TYPE_CHECKING:
    from packages.core.services.task_execution import CoordinatorRuntimeSelection

COORDINATOR_STEP_KEY = "coordinate"
COORDINATOR_STEP_DESCRIPTION = "Coordinate the task across workspace services"
COORDINATOR_MAX_ROUNDS = 60
COORDINATOR_DEFAULT_OUTPUT_SHAPE = "TextResult"
TASK_COORDINATOR_PLAN_VERSION = 3
TASK_COORDINATOR_PLAN_VERSION_KEY = "task_coordinator_version"
TASK_COORDINATOR_RUN_ID_KEY = "task_coordinator_run_id"

_AGENT_KINDS = frozenset({"llm", "subagent"})
_COORDINATOR_ITEM_KINDS = frozenset({
    "llm", "subagent", "action", "human", "sleep",
})
_SERVICE_ITEM_KINDS = frozenset({"llm", "subagent", "action"})
_STEP_KEY_MAX_LENGTH = 64
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}

_SERVER_OWNED_PARAM_KEYS = frozenset({
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_HANDOFF_ACK_PARAM_KEY,
    COORDINATOR_CHILD_RESPONSE_DIGEST_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_INPUT_CONTEXT_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_VERSION_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    SUPERVISOR_STEP_RETRY_FLAG,
})


def coordinator_child_step_key(parent_key: str, item_key: str) -> str:
    """Stable, schema-safe key for one materialised coordinator child."""

    candidate = f"{parent_key}__{item_key}"
    if len(candidate) <= _STEP_KEY_MAX_LENGTH:
        return candidate
    digest = sha256(candidate.encode("utf-8")).hexdigest()[:12]
    prefix = candidate[: _STEP_KEY_MAX_LENGTH - len(digest) - 1].rstrip("_")
    return f"{prefix}_{digest}"


def coordinator_child_idempotency_key(
    plan_id: str,
    parent_key: str,
    item_key: str,
    generation: int = 0,
) -> str:
    """Stable effect identity shared by materialisation and dispatch."""

    generation = max(0, int(generation))
    identity = f"coordinator-child-v1\0{plan_id}\0{parent_key}\0{item_key}"
    if generation:
        identity = f"{identity}\0generation:{generation}"
    return "coordinator-child-v1:" + sha256(identity.encode("utf-8")).hexdigest()


def _reserved_param_keys(params: Any) -> set[str]:
    if not isinstance(params, dict):
        return set()
    return {
        str(key)
        for key in params
        if str(key) in _SERVER_OWNED_PARAM_KEYS
        or str(key).startswith("_action_effect")
        or str(key).startswith("_coordinator")
        or str(key).startswith("_supervisor")
    }


def assert_no_server_owned_plan_params(plan: Plan) -> None:
    """Reject planner/API attempts to mint Runtime-owned identities.

    The coordinator fold calls this before it creates its one trusted parent
    marker.  The persistence boundary calls :func:`is_generated_coordinator_plan`
    to admit only that exact folded shape; child and supervisor markers are
    never legal in an authored Plan.
    """

    for step in plan.steps:
        reserved = sorted(_reserved_param_keys(step.params))
        if reserved:
            raise ValueError(
                f"step {step.key}: params contain server-owned execution keys: "
                + ", ".join(reserved)
            )


def is_generated_coordinator_plan(plan: Plan) -> bool:
    """Whether ``plan`` has the exact shape produced by our coordinator fold."""

    if len(plan.steps) != 1:
        return False
    step = plan.steps[0]
    params = step.params if isinstance(step.params, dict) else {}
    if (
        step.key != COORDINATOR_STEP_KEY
        or step.kind != "subagent"
        or params.get(COORDINATOR_PARAM_KEY) is not True
        or params.get(COORDINATOR_VERSION_PARAM_KEY)
        != COORDINATOR_TYPED_VERSION
        or _reserved_param_keys(params) != {
            COORDINATOR_PARAM_KEY,
            COORDINATOR_VERSION_PARAM_KEY,
        }
    ):
        return False
    checklist = params.get("checklist")
    services = params.get("delegate_services")
    metadata = plan.metadata.model_dump().get("coordinator")
    if (
        not isinstance(checklist, list)
        or not checklist
        or not isinstance(services, list)
        or not services
        or not isinstance(metadata, dict)
    ):
        return False

    source_keys: list[str] = []
    source_services: set[str] = set()
    for item in checklist:
        if not isinstance(item, dict):
            return False
        item_key = str(item.get("delegation_key") or item.get("key") or "").strip()
        service_key = str(item.get("service_key") or "").strip()
        item_kind = str(item.get("kind") or "").strip()
        if (
            not item_key
            or item_key in source_keys
            or item_kind not in _COORDINATOR_ITEM_KINDS
            or (item_kind in _SERVICE_ITEM_KINDS and not service_key)
            or _reserved_param_keys(item.get("params"))
        ):
            return False
        source_keys.append(item_key)
        if service_key:
            source_services.add(service_key)

    declared_services = [str(value) for value in services]
    expected_services = source_services | {str(step.service_key or "").strip()}
    expected_services.discard("")
    return (
        declared_services == sorted(expected_services)
        and bool(str(step.service_key or "").strip())
        and metadata.get("source_steps") == source_keys
        and metadata.get("source_step_count") == len(source_keys)
        and bool(str(params.get("prompt") or "").strip())
    )


async def task_uses_coordinator_runtime(db: AsyncSession, task: Any) -> bool:
    """Whether the closed Task runtime factory selected coordinator execution.

    This delegates the decision instead of maintaining a second Task-type or
    rollout switch at the planning boundary.
    """

    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.services.task_execution import resolve_task_execution

    dispatch = await resolve_task_execution(db, task)
    return dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN


def coordinator_plan_from(
    plan: Plan,
    task: Any,
    *,
    coordinator: CoordinatorRuntimeSelection,
) -> Plan:
    """Fold a validated DAG into one logical coordinator root.

    The source plan is left untouched. Every supported item keeps its exact
    contract and is later materialised as the same typed child, so persistence
    contains ``1 + N`` ExecutionSteps rather than one physical work step.
    Actions retain provider/account/args and approval gates; human and sleep
    retain their executor-owned semantics.
    """
    ordered = validate_coordinator_source_plan(plan, coordinator=coordinator)
    service_keys = {
        str(step.service_key)
        for step in ordered
        if step.service_key
    }
    service_key = coordinator.root.service_key
    service_keys.add(service_key)

    params: dict[str, Any] = {
        # The root has no upstream results. Its readable prompt uses
        # placeholders, while the server-owned checklist below deliberately
        # retains exact refs for typed-child release time.
        "prompt": _neutralize_refs(_coordinator_prompt(task, ordered)),
        COORDINATOR_PARAM_KEY: True,
        COORDINATOR_VERSION_PARAM_KEY: COORDINATOR_TYPED_VERSION,
        "checklist": [_checklist_item(step) for step in ordered],
        "delegate_services": sorted(service_keys),
        "max_rounds": COORDINATOR_MAX_ROUNDS,
    }
    max_runtime_seconds = _max_runtime_seconds(ordered)
    if max_runtime_seconds is not None:
        params["max_runtime_seconds"] = max_runtime_seconds
    expects = _union_expects(ordered)
    step = PlanStep(
        key=COORDINATOR_STEP_KEY,
        kind="subagent",
        service_key=service_key,
        params=params,
        expects=expects or None,
        risk_level=_max_risk_level(ordered),
        # Do not create a broad approval for the whole coordinator. Each child
        # retains the source flag in its checklist contract, while the exact
        # nested tool call still crosses Workspace Runtime's normal approval
        # gate with its concrete action and arguments.
        requires_approval=False,
        description=COORDINATOR_STEP_DESCRIPTION,
        **_terminal_output_contract(ordered),
    )
    return Plan(steps=[step], metadata=_coordinator_metadata(plan.metadata, ordered))


def validate_coordinator_source_plan(
    plan: Plan,
    *,
    coordinator: CoordinatorRuntimeSelection,
) -> list[PlanStep]:
    """Validate the typed DAG shared by V2 folding and V3 Task control."""

    # LLM/API-authored plans may never opt themselves into privileged Runtime
    # behavior by smuggling underscore-prefixed coordinator/supervisor keys.
    assert_no_server_owned_plan_params(plan)
    ordered = plan.topo_order()
    unsupported = [
        step.key for step in ordered
        if step.kind not in _COORDINATOR_ITEM_KINDS
    ]
    if unsupported:
        raise ValueError(
            "coordinator mode does not have a typed executor for step(s): "
            + ", ".join(unsupported)
        )
    invalid_service = [
        step.key
        for step in ordered
        if step.kind in _SERVICE_ITEM_KINDS and not step.service_key
    ]
    if invalid_service:
        raise ValueError(
            "coordinator service-owned step(s) have no service_key: "
            + ", ".join(invalid_service)
        )

    service_keys = {
        str(step.service_key)
        for step in ordered
        if step.service_key
    }
    unsupported_services = service_keys - coordinator.service_keys
    if unsupported_services:
        raise ValueError(
            "coordinator services exceed the active Task subscription scope: "
            + ", ".join(sorted(unsupported_services))
        )
    return ordered


# ── prompt ────────────────────────────────────────────────────────────

def _coordinator_prompt(task: Any, ordered: list[PlanStep]) -> str:
    title = str(getattr(task, "title", "") or "").strip()
    description = str(getattr(task, "description", "") or "").strip()
    parts = [f"# Task\nTitle: {title}"]
    if description:
        parts.append(f"Description: {description}")
    # The user's verbatim task constraints, exactly as the planner saw them.
    constraints_block = render_constraints_block(
        extract_binding_constraints(getattr(task, "details", None))
    )
    if constraints_block:
        parts.append(constraints_block)

    lines = [
        "## Checklist",
        "Work through every item below, in order. Each item names the workspace "
        "service that owns it and what that service must deliver.",
    ]
    for step in ordered:
        lines.append(_checklist_line(step))
        if step.depends_on:
            lines.append(f"  depends on: {', '.join(step.depends_on)}")
        if step.expects:
            lines.append(f"  expects: {', '.join(step.expects)}")
    parts.append("\n".join(lines))
    parts.append(_RULES)
    return "\n\n".join(parts)


_RULES = (
    "## Rules\n"
    f"1. Delegate each checklist item to the service named on it with the "
    f"`{DELEGATE_TO_SERVICE_TOOL_NAME}` tool (checklist_item_key = the bracketed "
    "item key). Runtime derives the exact service, kind, and arguments from "
    "the persisted checklist contract. Do not "
    "do a service's work yourself and do not send an item to a service it does "
    "not name.\n"
    "2. Respect the order: an item that depends on other items starts only after "
    "those items have delivered.\n"
    "3. Reuse results: hand what earlier items produced to later delegations "
    "instead of re-deriving or re-requesting it.\n"
    "4. When an item needs input that is missing and cannot be derived from the "
    "task or from earlier results, ask for it with `request_input`; never invent it.\n"
    "5. Never claim a side effect (a publish, a send, a saved file) that a "
    "delegation did not actually perform and report back.\n"
    "6. When every item is done, finish by calling `submit_result` with the final "
    "deliverable."
)


def _checklist_line(step: PlanStep) -> str:
    label = str(step.description or step.key).strip()
    body = _checklist_body(step)
    return f"- [{step.key}] ({step.service_key or 'no service'}, {step.kind}) {label}: {body}"


def _checklist_body(step: PlanStep) -> str:
    params = step.params or {}
    if step.kind in _AGENT_KINDS:
        return _one_line(params.get("prompt"))
    if step.kind == "action":
        body = f"action {step.provider}.{step.action_key}"
        arguments = {key: value for key, value in params.items() if not str(key).startswith("_")}
        if arguments:
            body += " with arguments " + json.dumps(arguments, ensure_ascii=False, default=str)
        return body
    if step.kind == "human":
        return "ask the user: " + _one_line(params.get("prompt"))
    if step.kind == "sleep":
        if "until" in params:
            return f"wait until {params.get('until')}"
        return f"wait {params.get('seconds')} seconds"
    return _one_line(params.get("prompt")) or json.dumps(params, ensure_ascii=False, default=str)


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


# ── checklist / params ────────────────────────────────────────────────

def _checklist_item(step: PlanStep) -> dict[str, Any]:
    params = step.params or {}
    prompt = params.get("prompt") if step.kind in _AGENT_KINDS else None
    return {
        "key": step.key,
        # This key is the stable identity of one delegation within the source
        # DAG.  The runtime scopes it by plan + coordinator parent when it
        # materialises the child ExecutionStep.
        "delegation_key": step.key,
        "kind": step.kind,
        "service_key": step.service_key,
        "description": step.description,
        "prompt": str(prompt) if prompt else None,
        "params": deepcopy(params),
        "depends_on": list(step.depends_on),
        "expects": list(step.expects or []),
        "expected_input_schema": deepcopy(step.expected_input_schema),
        "expected_output_schema": deepcopy(step.expected_output_schema),
        "output_shape": step.output_shape,
        "execution_contract": (
            step.execution_contract.model_dump(mode="json")
            if step.execution_contract is not None else None
        ),
        "risk_level": step.risk_level,
        "requires_approval": bool(step.requires_approval),
        "max_attempts": step.max_attempts,
        "skill_refs": [str(ref) for ref in (params.get("skill_refs") or []) if str(ref or "").strip()],
        "provider": step.provider,
        "action_key": step.action_key,
        "capability_id": step.capability_id,
        "integration_id": step.integration_id,
    }


def _neutralize_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _neutralize_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_neutralize_refs(item) for item in value]
    if isinstance(value, str):
        return _REF_RE.sub(_ref_placeholder, value)
    return value


def _ref_placeholder(match: re.Match[str]) -> str:
    step_key, path = match.group(1), match.group(2)
    if path:
        return f"<result of step {step_key}: {path}>"
    return f"<result of step {step_key}>"


def _union_expects(ordered: list[PlanStep]) -> list[str]:
    seen: list[str] = []
    for step in ordered:
        for expect in step.expects or []:
            text = str(expect or "").strip()
            if text and text not in seen:
                seen.append(text)
    return seen


def _max_risk_level(ordered: list[PlanStep]) -> str:
    return max(
        (str(step.risk_level or "low") for step in ordered),
        key=lambda level: _RISK_RANK.get(level, 0),
        default="low",
    )


def _max_runtime_seconds(ordered: list[PlanStep]) -> int | float | None:
    from packages.core.services.step_deadline import step_max_runtime_config

    values: list[float] = []
    for step in ordered:
        try:
            seconds = float(step_max_runtime_config(step))
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            values.append(seconds)
    if not values:
        return None
    # The coordinator owns one sequential lease while it delegates multiple
    # source steps. Using only the largest child deadline prematurely killed a
    # healthy multi-item run after roughly one child's budget. The sum is the
    # conservative serial critical path; task/plan-level caps still bound it.
    total = sum(values)
    return int(total) if total.is_integer() else total


def _terminal_output_contract(ordered: list[PlanStep]) -> dict[str, Any]:
    """The actual terminal sink's declared contract, else ``TextResult``."""
    depended_on = {key for step in ordered for key in step.depends_on}
    sinks = [step for step in ordered if step.key not in depended_on]
    terminal = sinks[-1] if sinks else (ordered[-1] if ordered else None)
    if terminal is not None:
        if terminal.output_shape:
            return {"output_shape": terminal.output_shape}
        if isinstance(terminal.expected_output_schema, dict):
            return {"expected_output_schema": deepcopy(terminal.expected_output_schema)}
    return {"output_shape": COORDINATOR_DEFAULT_OUTPUT_SHAPE}


# ── metadata ──────────────────────────────────────────────────────────

def _coordinator_metadata(metadata: PlanMetadata, ordered: list[PlanStep]) -> PlanMetadata:
    # Start from the full dump so planner-attached extras (PlanMetadata allows
    # them — e.g. a plan-level runtime budget) survive the fold.
    payload: dict[str, Any] = {
        **metadata.model_dump(),
        "acceptance_contract": None,
        "coordinator": {
            "source_steps": [step.key for step in ordered],
            "source_step_count": len(ordered),
        },
    }
    if metadata.acceptance_contract is not None:
        contract = metadata.acceptance_contract.model_dump()
        for criterion in contract.get("criteria") or []:
            criterion["evidence_step_keys"] = [COORDINATOR_STEP_KEY]
        payload["acceptance_contract"] = contract
    return PlanMetadata.model_validate(payload)
