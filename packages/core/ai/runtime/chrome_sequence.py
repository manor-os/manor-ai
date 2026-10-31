"""Runtime-only sequential orchestration for a bounded Chrome tool subset."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool_action,
)
from packages.core.ai.runtime.control import (
    is_runtime_tool_suspension,
    runtime_owned_sequence_child_result,
    runtime_terminal_uncertainty_control,
)
from packages.core.ai.runtime.human_gate import runtime_detect_blocking_human_gate
from packages.core.ai.runtime.chrome_routing import runtime_record_chrome_tool_result
from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.ai.runtime.tool_registry import runtime_execute_tool
from packages.core.constants.hitl_envelope import parse_hitl_envelope


CHROME_RUN_SEQUENCE_TOOL_NAME = "mcp__chrome__run_sequence"
CHROME_RUN_SEQUENCE_MAX_STEPS = 8
CHROME_RUN_SEQUENCE_MAX_STEP_ID_CHARS = 64
CHROME_RUN_SEQUENCE_MAX_TIMEOUT_MS = 30_000
CHROME_RUN_SEQUENCE_MAX_WAIT_MS = 10_000
CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS = 12_000

_ALLOWED_CHILD_NAMES = frozenset(
    {
        "open_or_reuse",
        "target_state",
        "click_element",
        "fill_or_select",
        "hover",
        "scroll",
        "wait",
    }
)
_MUTATION_CHILD_NAMES = frozenset(
    {
        "mcp__chrome__open_or_reuse",
        "mcp__chrome__click_element",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__hover",
        "mcp__chrome__scroll",
    }
)
_NON_SEMANTIC_CLICK_ARGUMENT_KEYS = frozenset(
    {"ref", "node_id", "nodeId", "selector", "coordinates", "x", "y"}
)
_PRIVATE_CHILD_ARGUMENT_KEYS = frozenset(RUNTIME_TOOL_CONTEXT_KEYS)
_CHILD_RUNTIME_ARGUMENT_KEYS = frozenset(
    key
    for key in RUNTIME_TOOL_CONTEXT_KEYS
    if key.startswith("_workflow_")
    or key
    in {
        "_approved_plan_version_from_context",
        "_performance_run_id_from_context",
        "_runtime_run_id_from_context",
        "_runtime_tool_attempt_from_context",
    }
)
_MAX_RESULT_DEPTH = 4
_MAX_RESULT_ITEMS = 24
_MAX_RESULT_STRING_CHARS = 1000
_MAX_STEP_RESULT_CHARS = 600
_MAX_RUNTIME_TOOL_CALL_ID_CHARS = 160
_PUBLIC_BLOCKING_SCALAR_KEYS = (
    "ok",
    "status",
    "error",
    "message",
    "approval_required",
    "provider",
    "approvalId",
    "approval_id",
    "approval_token",
    "expires_at",
    "expiresAt",
    "action_key",
    "reason",
    "confirmation_mode",
    "policy_category",
    "preapproved",
    "target_label",
    "target_role",
    "url",
    "data_summary",
    "resource_id",
    "resourceId",
    "next_required_tool",
    "groupId",
    "group_id",
    "tabId",
    "tab_id",
    "ref",
    "node_id",
    "selector",
    "snapshot_id",
    "page_blocker",
)
_PUBLIC_HITL_KEYS = ("id", "type", "prompt", "action", "tool", "options")
_PUBLIC_OPERATION_KEYS = ("kind", "provider", "tool", "action", "status")
_PUBLIC_BLOCKING_STRING_CHARS = 500


def _result(
    *,
    status: str,
    completed_steps: list[dict[str, Any]],
    stopped_step: dict[str, Any] | None,
    elapsed_ms: int,
    timed_out: bool,
    stop_reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "completed_steps": completed_steps,
        "stopped_step": stopped_step,
        "elapsed_ms": max(0, min(int(elapsed_ms), CHROME_RUN_SEQUENCE_MAX_TIMEOUT_MS)),
        "timed_out": timed_out,
        "stop_reason": stop_reason,
    }


def _mutation_timeout_result(
    *,
    completed_steps: list[dict[str, Any]],
    step_id: str,
    tool_name: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        **_result(
            status="error",
            completed_steps=completed_steps,
            stopped_step={"id": step_id, "tool": tool_name, "result": None},
            elapsed_ms=elapsed_ms,
            timed_out=True,
            stop_reason="uncertain_result",
        ),
        "error": "mutation_timeout_uncertain",
        "content": (
            "Chrome mutation timed out after dispatch; its outcome is unknown. "
            "Observe the current browser state and reconcile before any further mutation."
        ),
        "action_executed": "unknown",
        "retryable": False,
        "do_not_replay": True,
        "stop_parent": True,
        "terminal_failure": True,
        "recommended_next_action": "observe_and_reconcile",
    }


def _bounded_result_summary(value: Any, *, depth: int = 0) -> Any:
    if is_runtime_tool_suspension(value):
        value = value.to_dict()
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                value = json.loads(stripped)
            except (TypeError, json.JSONDecodeError):
                return value[:_MAX_RESULT_STRING_CHARS]
        else:
            return value[:_MAX_RESULT_STRING_CHARS]
    if depth >= _MAX_RESULT_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: _bounded_result_summary(item, depth=depth + 1)
            for key, item in list(value.items())[:_MAX_RESULT_ITEMS]
            if not str(key).startswith("_") or str(key) == "__hitl__"
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_result_summary(item, depth=depth + 1)
            for item in list(value)[:_MAX_RESULT_ITEMS]
        ]
    if isinstance(value, str):
        return value[:_MAX_RESULT_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_RESULT_STRING_CHARS]


def _bounded_step_result(value: Any) -> Any:
    summary = _bounded_result_summary(value)
    serialized = json.dumps(summary, ensure_ascii=False, default=str)
    if len(serialized) <= _MAX_STEP_RESULT_CHARS:
        return summary
    return {
        "truncated": True,
        "preview": serialized[:_MAX_STEP_RESULT_CHARS],
    }


def _payload(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        payload = result
    elif isinstance(result, str) and result.strip().startswith("{"):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return None
        payload = parsed if isinstance(parsed, dict) else None
    else:
        payload = None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if payload.get("error") == "tool_error" and isinstance(detail, str):
        nested = _payload(detail)
        if nested is not None:
            return {**nested, "_runtime_wrapper": payload}
    return payload


def _public_blocking_value(value: Any, *, string_chars: int) -> Any:
    if isinstance(value, str):
        return value[:string_chars]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [
            _public_blocking_value(item, string_chars=min(string_chars, 160))
            for item in value[:8]
            if isinstance(item, (str, bool, int, float)) or item is None
        ]
    return str(value)[:string_chars]


def _public_blocking_result(result: Any) -> str:
    payload = _payload(result)
    if payload is None:
        raise ValueError("blocking Chrome sequence child result must be JSON")
    projected = {
        key: _public_blocking_value(
            payload[key],
            string_chars=_PUBLIC_BLOCKING_STRING_CHARS,
        )
        for key in _PUBLIC_BLOCKING_SCALAR_KEYS
        if key in payload
        and not isinstance(payload[key], (dict, tuple, set))
    }
    if payload.get("__hitl__") is True:
        projected["__hitl__"] = True
    for key, allowed_keys in (
        ("hitl", _PUBLIC_HITL_KEYS),
        ("operation", _PUBLIC_OPERATION_KEYS),
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            projected[key] = {
                child_key: _public_blocking_value(
                    value[child_key],
                    string_chars=_PUBLIC_BLOCKING_STRING_CHARS,
                )
                for child_key in allowed_keys
                if child_key in value
            }
    serialized = json.dumps(
        projected,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(serialized) <= CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS:
        return serialized
    compacted = {
        key: _public_blocking_value(value, string_chars=120)
        if key not in {"hitl", "operation"}
        else {
            child_key: _public_blocking_value(child_value, string_chars=120)
            for child_key, child_value in value.items()
        }
        for key, value in projected.items()
    }
    serialized = json.dumps(
        compacted,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(serialized) > CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS:
        raise ValueError("public Chrome sequence child result is too large")
    return serialized


def _normalize_child_tool(raw_tool: Any) -> str | None:
    tool = str(raw_tool or "").strip()
    if tool in _ALLOWED_CHILD_NAMES:
        return f"mcp__chrome__{tool}"
    prefix = "mcp__chrome__"
    if tool.startswith(prefix) and tool[len(prefix) :] in _ALLOWED_CHILD_NAMES:
        return tool
    return None


def _child_runtime_tool_call_id(
    outer_tool_call_id: str,
    *,
    step_id: str,
    step_index: int,
) -> str:
    material = f"{outer_tool_call_id}\0{step_index}\0{step_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    suffix = f":sequence:{step_index}:{digest}"
    prefix = outer_tool_call_id[: _MAX_RUNTIME_TOOL_CALL_ID_CHARS - len(suffix)]
    return f"{prefix}{suffix}"


def _private_child_argument(arguments: dict[str, Any]) -> bool:
    return any(
        str(key).startswith("_") or str(key) in _PRIVATE_CHILD_ARGUMENT_KEYS
        for key in arguments
    )


def _sequence_click_has_supported_shape(arguments: dict[str, Any]) -> bool:
    return (
        arguments.get("double") is not True
        and arguments.get("button", "left") == "left"
    )


def _sequence_click_has_exact_semantic_target(arguments: dict[str, Any]) -> bool:
    if any(arguments.get(key) is not None for key in _NON_SEMANTIC_CLICK_ARGUMENT_KEYS):
        return False
    target = arguments.get("target")
    if not isinstance(target, dict) or target.get("exact") is not True:
        return False
    if any(target.get(key) is not None for key in _NON_SEMANTIC_CLICK_ARGUMENT_KEYS):
        return False
    role = target.get("role")
    name = target.get("name")
    return (
        isinstance(role, str)
        and bool(role.strip())
        and isinstance(name, str)
        and bool(name.strip())
    )


def _sequence_child_is_final_action(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    if tool_name != "mcp__chrome__click_element":
        return False
    runtime_action = classify_runtime_tool_action(tool_name, arguments)
    return runtime_action is not None and runtime_action.operation == "publish"


def _invalid_sequence_reason(arguments: dict[str, Any]) -> str | None:
    steps = arguments.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= CHROME_RUN_SEQUENCE_MAX_STEPS:
        return "invalid_sequence"
    timeout_ms = arguments.get("timeout_ms", CHROME_RUN_SEQUENCE_MAX_TIMEOUT_MS)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, (int, float))
        or timeout_ms <= 0
        or timeout_ms > CHROME_RUN_SEQUENCE_MAX_TIMEOUT_MS
    ):
        return "invalid_sequence"
    seen_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            return "invalid_sequence"
        raw_step_id = step.get("id")
        if not isinstance(raw_step_id, str):
            return "invalid_sequence"
        step_id = raw_step_id.strip()
        if (
            not step_id
            or len(step_id) > CHROME_RUN_SEQUENCE_MAX_STEP_ID_CHARS
            or step_id in seen_ids
        ):
            return "invalid_sequence"
        seen_ids.add(step_id)
        if _normalize_child_tool(step.get("tool")) is None:
            return "child_tool_not_allowed"
        child_arguments = step.get("arguments")
        if not isinstance(child_arguments, dict):
            return "invalid_sequence"
        if _private_child_argument(child_arguments):
            return "private_context_rejected"
        child_tool = _normalize_child_tool(step.get("tool"))
        if (
            child_tool == "mcp__chrome__click_element"
            and not _sequence_click_has_supported_shape(child_arguments)
        ):
            return "sequence_click_shape_unsupported"
        if (
            child_tool == "mcp__chrome__click_element"
            and not _sequence_click_has_exact_semantic_target(child_arguments)
        ):
            return "exact_semantic_target_required"
        if child_tool is not None and _sequence_child_is_final_action(
            child_tool,
            child_arguments,
        ):
            return "final_action_not_allowed"
        if child_tool == "mcp__chrome__wait":
            for timeout_key in ("timeout_ms", "timeoutMs"):
                wait_timeout = child_arguments.get(timeout_key)
                if wait_timeout is None:
                    continue
                if (
                    isinstance(wait_timeout, bool)
                    or not isinstance(wait_timeout, (int, float))
                    or wait_timeout > CHROME_RUN_SEQUENCE_MAX_WAIT_MS
                ):
                    return "wait_timeout_exceeded"
    return None


def _stop_reason(tool_name: str, result: Any) -> str | None:
    if is_runtime_tool_suspension(result):
        return "needs_human"
    if parse_hitl_envelope(result) is not None:
        return "approval_required"
    payload = _payload(result)
    if payload is None:
        return "uncertain_result" if tool_name in _MUTATION_CHILD_NAMES else None
    if parse_hitl_envelope(payload) is not None:
        return "approval_required"
    status = str(payload.get("status") or "").strip().lower()
    error = str(payload.get("error") or "").strip()
    if error in {
        "tool_input_validation_failed",
        "tool_schema_invalid",
        "tool_schema_unavailable",
    }:
        return "schema_validation_failed"
    if error in {
        "invalid_tool",
        "tool_not_in_runtime_surface",
        "external_principal_tool_denied",
        "file_editor_tool_denied",
        "file_context_denied",
    }:
        return "authorization_failed"
    if error == "blocked_by_runtime_policy":
        return "policy_failed"
    if status in {"approval_required", "waiting_human"}:
        return "approval_required"
    if status in {"handoff_required", "needs_human", "needs_user"} or payload.get("page_blocker"):
        return "needs_human"
    if status == "interrupted":
        return "control_interrupted"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"tab_selection_required", "claim_required"}:
        return status
    if (
        payload.get("ok") is True and not error and payload.get("action_executed") is True
        and isinstance(payload.get("verification"), dict)
        and payload["verification"].get("status") == "pending"
    ):
        return "observation_required"
    if (
        status == "event_timeout"
        or (
            payload.get("action_executed") is True
            and isinstance(payload.get("event_receipt"), dict)
            and str(payload["event_receipt"].get("status") or "").strip().lower()
            == "timeout"
        )
    ):
        return "uncertain_result"
    if status == "timeout":
        return "timeout"
    if tool_name == "mcp__chrome__target_state":
        targets = payload.get("targets")
        if not isinstance(targets, list) or any(
            not isinstance(target, dict) or target.get("match_count") != 1
            for target in targets
        ):
            return "target_ambiguous"
    if (
        payload.get("action_executed") is False
        and status == "read_page_required"
        and str(payload.get("reason") or "").strip()
        in {"snapshot_mismatch", "target_moved", "ref_not_found"}
    ):
        return "observation_required"
    if status in {"error", "failed", "blocked", "rejected", "unavailable"}:
        return "child_failed"
    if error or payload.get("ok") is False:
        return "child_failed"
    if tool_name in _MUTATION_CHILD_NAMES and payload.get("ok") is not True:
        return "uncertain_result"
    return None


def _requested_boolean_state(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    normalized = " ".join(str(value or "").split()).casefold()
    if normalized in {"true", "1", "yes", "on", "checked", "selected"}:
        return True
    if normalized in {"false", "0", "no", "off", "unchecked", "unselected"}:
        return False
    return None


def _confirmed_fill_timeout_result(
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
) -> dict[str, Any] | None:
    if tool_name != "mcp__chrome__fill_or_select" or not isinstance(
        arguments.get("expect"),
        dict,
    ):
        return None
    payload = _payload(result)
    if (
        payload is None
        or str(payload.get("status") or "").strip().lower() != "event_timeout"
        or payload.get("action_executed") is not True
    ):
        return None
    event_receipt = payload.get("event_receipt")
    action = payload.get("action")
    if (
        not isinstance(event_receipt, dict)
        or str(event_receipt.get("status") or "").strip().lower() != "timeout"
        or not isinstance(action, dict)
        or action.get("ok") is not True
    ):
        return None

    requested_value = arguments.get("value")
    requested_checked = _requested_boolean_state(requested_value)
    checked_matches = (
        isinstance(action.get("checked"), bool)
        and requested_checked is not None
        and action["checked"] is requested_checked
    )
    current_value = action.get("current_value")
    value_matches = (
        current_value is not None
        and not isinstance(requested_value, (dict, list, tuple, set))
        and str(current_value) == str(requested_value)
    )
    if not checked_matches and not value_matches:
        return None

    reconciled = copy.deepcopy(payload)
    reconciled["ok"] = True
    reconciled["status"] = "filled"
    reconciled["event_timeout_reconciled"] = True
    reconciled.pop("next_required_tool", None)
    return reconciled


async def runtime_execute_chrome_sequence(
    arguments: dict[str, Any],
    *,
    execute_child: Callable[[str, dict[str, Any]], Awaitable[Any]],
) -> Any:
    """Execute validated children once, in order, through the supplied Runtime path."""

    validation_reason = _invalid_sequence_reason(arguments)
    if validation_reason is not None:
        return _result(
            status="stopped",
            completed_steps=[],
            stopped_step=None,
            elapsed_ms=0,
            timed_out=False,
            stop_reason=validation_reason,
        )

    loop = asyncio.get_running_loop()
    started = loop.time()
    timeout_ms = float(arguments.get("timeout_ms", CHROME_RUN_SEQUENCE_MAX_TIMEOUT_MS))
    completed_steps: list[dict[str, Any]] = []
    for raw_step in arguments["steps"]:
        step_id = str(raw_step["id"]).strip()
        tool_name = _normalize_child_tool(raw_step["tool"])
        assert tool_name is not None
        child_arguments = dict(raw_step["arguments"])
        child_owner_arguments = copy.deepcopy(child_arguments)
        elapsed_seconds = loop.time() - started
        remaining_seconds = (timeout_ms / 1000) - elapsed_seconds
        if remaining_seconds <= 0:
            elapsed_ms = round((loop.time() - started) * 1000)
            return _result(
                status="stopped",
                completed_steps=completed_steps,
                stopped_step={"id": step_id, "tool": tool_name, "result": None},
                elapsed_ms=elapsed_ms,
                timed_out=True,
                stop_reason="timeout",
            )
        try:
            child_result = await asyncio.wait_for(
                execute_child(tool_name, child_arguments),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            elapsed_ms = round((loop.time() - started) * 1000)
            if tool_name in _MUTATION_CHILD_NAMES:
                return _mutation_timeout_result(
                    completed_steps=completed_steps,
                    step_id=step_id,
                    tool_name=tool_name,
                    elapsed_ms=elapsed_ms,
                )
            return _result(
                status="stopped",
                completed_steps=completed_steps,
                stopped_step={"id": step_id, "tool": tool_name, "result": None},
                elapsed_ms=elapsed_ms,
                timed_out=True,
                stop_reason="timeout",
            )
        if is_runtime_tool_suspension(child_result):
            return child_result
        if runtime_detect_blocking_human_gate(
            tool_name,
            child_arguments,
            child_result,
        ) is not None:
            return runtime_owned_sequence_child_result(
                tool_name=tool_name,
                arguments=child_owner_arguments,
                result=child_result,
                public_result=_public_blocking_result(child_result),
            )
        confirmed_fill = _confirmed_fill_timeout_result(
            tool_name,
            child_arguments,
            child_result,
        )
        if confirmed_fill is not None:
            completed_steps.append(
                {
                    "id": step_id,
                    "tool": tool_name,
                    "result": _bounded_step_result(confirmed_fill),
                }
            )
            return {
                **_result(
                    status="stopped",
                    completed_steps=completed_steps,
                    stopped_step=None,
                    elapsed_ms=round((loop.time() - started) * 1000),
                    timed_out=False,
                    stop_reason="observation_required",
                ),
                "next_required_tool": "mcp__chrome__read_page",
            }
        step_result = {
            "id": step_id,
            "tool": tool_name,
            "result": _bounded_step_result(child_result),
        }
        stop_reason = _stop_reason(tool_name, child_result)
        terminal_uncertainty = (
            runtime_terminal_uncertainty_control(child_result)
            if tool_name in _MUTATION_CHILD_NAMES
            else None
        )
        if terminal_uncertainty is not None:
            stop_reason = "uncertain_result"
        if stop_reason == "observation_required":
            # A completed action or stale, unexecuted locator requires a fresh
            # observation before this sequence can continue.
            if _payload(child_result).get("action_executed") is True:
                completed_steps.append(step_result)
                stopped_step = None
            else:
                stopped_step = step_result
            return {
                **_result(status="stopped", completed_steps=completed_steps, stopped_step=stopped_step,
                          elapsed_ms=round((loop.time() - started) * 1000), timed_out=False,
                          stop_reason="observation_required"),
                "next_required_tool": "mcp__chrome__read_page",
            }
        if tool_name in _MUTATION_CHILD_NAMES and stop_reason == "uncertain_result":
            uncertainty_result = {
                **(terminal_uncertainty or {}),
                **_result(
                    status="error",
                    completed_steps=completed_steps,
                    stopped_step=step_result,
                    elapsed_ms=round((loop.time() - started) * 1000),
                    timed_out=False,
                    stop_reason="uncertain_result",
                ),
                "action_executed": "unknown",
                "retryable": False,
                "do_not_replay": True,
                "stop_parent": True,
                "terminal_failure": True,
                "recommended_next_action": "observe_and_reconcile",
            }
            if terminal_uncertainty is None:
                child_payload = _payload(child_result)
                event_receipt = (
                    child_payload.get("event_receipt")
                    if isinstance(child_payload, dict)
                    else None
                )
                if isinstance(event_receipt, dict):
                    uncertainty_result["event_receipt"] = copy.deepcopy(event_receipt)
            return uncertainty_result
        if stop_reason is not None:
            return _result(
                status="stopped",
                completed_steps=completed_steps,
                stopped_step=step_result,
                elapsed_ms=round((loop.time() - started) * 1000),
                timed_out=stop_reason == "timeout",
                stop_reason=stop_reason,
            )
        completed_steps.append(step_result)

    return _result(
        status="completed",
        completed_steps=completed_steps,
        stopped_step=None,
        elapsed_ms=round((loop.time() - started) * 1000),
        timed_out=False,
        stop_reason="completed",
    )


async def runtime_chrome_run_sequence_handler(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> Any:
    """ToolPool handler that preserves the outer server-owned Runtime context."""

    context = runtime_tool_call_context_from_kwargs(
        kwargs,
        entity_id=entity_id,
        user_id=user_id,
    )
    sequence_arguments = {
        "steps": kwargs.get("steps"),
    }
    if "timeout_ms" in kwargs:
        sequence_arguments["timeout_ms"] = kwargs["timeout_ms"]
    child_runtime_arguments = {
        key: kwargs[key]
        for key in _CHILD_RUNTIME_ARGUMENT_KEYS
        if key in kwargs
    }
    outer_tool_call_id = context.runtime_tool_call_id
    child_index = 0

    async def execute_child(tool_name: str, arguments: dict[str, Any]) -> Any:
        nonlocal child_index
        step_index = child_index
        child_index += 1
        step_id = str(sequence_arguments["steps"][step_index]["id"]).strip()
        child_arguments = dict(arguments)
        child_arguments.update(child_runtime_arguments)
        if outer_tool_call_id:
            child_arguments["_runtime_tool_call_id_from_context"] = (
                _child_runtime_tool_call_id(
                    outer_tool_call_id,
                    step_id=step_id,
                    step_index=step_index,
                )
            )
        result = await runtime_execute_tool(
            tool_name,
            child_arguments,
            entity_id=context.entity_id,
            user_id=context.user_id,
            agent_id=context.agent_id,
            runtime_artifact_urls=context.runtime_artifact_urls,
            dependency_artifact_urls=context.dependency_artifact_urls,
            workspace_id=context.workspace_id,
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            active_user_message=context.active_user_message,
            manual_skill_selected=context.manual_skill_selected,
            manual_skill_ids=list(context.manual_skill_ids),
            manual_skill_slugs=list(context.manual_skill_slugs),
            tool_profile=context.tool_profile,
            allowed_tool_names=(
                set(context.allowed_tool_names)
                if context.allowed_tool_names is not None
                else None
            ),
            llm_metadata=context.llm_metadata,
            llm_model=context.llm_model,
            runtime_envelope=context.runtime_envelope,
            chrome_sequence_child=True,
        )
        if is_runtime_tool_suspension(result):
            child_tool_call_id = str(
                child_arguments.get("_runtime_tool_call_id_from_context") or ""
            ).strip()
            if child_tool_call_id:
                return result.with_checkpoint_tool_call(
                    tool_call_id=child_tool_call_id,
                    tool_name=tool_name,
                    arguments=child_arguments,
                )
        return result

    result = await runtime_execute_chrome_sequence(
        sequence_arguments,
        execute_child=execute_child,
    )
    if (
        isinstance(result, dict)
        and result.get("stop_reason") == "uncertain_result"
        and isinstance(result.get("stopped_step"), dict)
    ):
        stopped_step = result["stopped_step"]
        step_id = str(stopped_step.get("id") or "").strip()
        raw_step = next(
            (
                step
                for step in sequence_arguments["steps"]
                if isinstance(step, dict)
                and str(step.get("id") or "").strip() == step_id
            ),
            None,
        )
        runtime_metadata = getattr(context.runtime_envelope, "metadata", None)
        if not isinstance(runtime_metadata, dict):
            runtime_metadata = context.llm_metadata
        if isinstance(raw_step, dict):
            runtime_record_chrome_tool_result(
                tool_name=str(stopped_step.get("tool") or ""),
                arguments=dict(raw_step.get("arguments") or {}),
                result=result,
                runtime_metadata=runtime_metadata,
            )
    if is_runtime_tool_suspension(result) or isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


__all__ = [
    "CHROME_RUN_SEQUENCE_TOOL_NAME",
    "CHROME_RUN_SEQUENCE_MAX_RESULT_CHARS",
    "runtime_chrome_run_sequence_handler",
    "runtime_execute_chrome_sequence",
]
