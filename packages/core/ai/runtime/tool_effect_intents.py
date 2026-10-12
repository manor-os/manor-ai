"""Durability hook immediately before a Runtime tool crosses external I/O.

The ordinary agent transcript is checkpointed after a tool returns.  A
coordinator child needs one earlier barrier for mutating tools: if the process
dies after the provider accepted a write but before the result reaches the
transcript, a successor must see an unresolved intent and reconcile instead of
replaying the call.  This context-local hook lets the coordinator install that
barrier without coupling generic Runtime execution to Plan database models.
"""

from __future__ import annotations

import contextvars
import copy
import inspect
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

from packages.core.ai.runtime.control import RuntimeTurnAborted


class RuntimeToolEffectPersistenceError(RuntimeTurnAborted):
    """A durability barrier failed; never turn this into a retryable tool error."""

if TYPE_CHECKING:
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )


RuntimeToolEffectIntentObserver = Callable[
    ...,
    Any,
]


_tool_effect_intent_observer: contextvars.ContextVar[
    RuntimeToolEffectIntentObserver | None
] = contextvars.ContextVar("runtime_tool_effect_intent_observer", default=None)
_tool_effect_pending_checkpoints: contextvars.ContextVar[
    dict[str, dict[str, Any]]
] = contextvars.ContextVar("runtime_tool_effect_pending_checkpoints", default={})
_consumed_approval_request_ids: contextvars.ContextVar[list[str] | None] = (
    contextvars.ContextVar("runtime_consumed_approval_request_ids", default=None)
)
_tool_effect_result_observer: contextvars.ContextVar[Callable[..., Any] | None] = (
    contextvars.ContextVar("runtime_tool_effect_result_observer", default=None)
)


@contextmanager
def runtime_tool_effect_result_scope(observer: Callable[..., Any]) -> Iterator[None]:
    token = _tool_effect_result_observer.set(observer)
    try:
        yield
    finally:
        _tool_effect_result_observer.reset(token)


def runtime_current_tool_checkpoint(tool_call_id: str | None = None) -> dict[str, Any] | None:
    checkpoints = _tool_effect_pending_checkpoints.get()
    if tool_call_id is not None:
        checkpoint = checkpoints.get(str(tool_call_id))
        if checkpoint is None:
            from packages.core.ai.runtime.nested_agent import runtime_scoped_tool_call_id

            checkpoint = next((value for key, value in checkpoints.items()
                               if runtime_scoped_tool_call_id(key) == tool_call_id), None)
    else:
        checkpoint = next(iter(checkpoints.values())) if len(checkpoints) == 1 else None
    return copy.deepcopy(checkpoint) if isinstance(checkpoint, dict) else None


def runtime_tool_effect_durability_enabled() -> bool:
    return _tool_effect_result_observer.get() is not None


async def runtime_record_tool_effect_result(
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    tool_call_id: str | None = None,
    classification: "RuntimeToolClassification | None" = None,
) -> None:
    """Commit the exact observed result before a parent can discard its frame."""
    from packages.core.ai.runtime.nested_agent import runtime_root_tool_checkpoint, runtime_scoped_tool_call_id

    observer = _tool_effect_result_observer.get()
    if observer is None:
        return
    checkpoint = runtime_current_tool_checkpoint(tool_call_id)
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    if getattr(classification, "effect", None) is RuntimeToolEffect.READ_ONLY and (
        tool_call_id is None or checkpoint is None
        or checkpoint.get("pending_tool_call", {}).get("name") != tool_name
    ):
        # Host prerequisite reads are not model tool calls and create no
        # effect. They must not borrow or overwrite the parent continuation.
        return
    if checkpoint is None:
        raise RuntimeToolEffectPersistenceError("Tool result has no exact execution checkpoint")
    execution_call_id = runtime_scoped_tool_call_id(checkpoint["pending_tool_call"]["id"])
    if tool_call_id not in {checkpoint["pending_tool_call"]["id"], execution_call_id}:
        raise RuntimeToolEffectPersistenceError("Tool result does not own its checkpoint call")
    try:
        observed = observer(
            tool_name, dict(arguments), result, execution_call_id, classification,
            runtime_root_tool_checkpoint(checkpoint),
        )
        if inspect.isawaitable(observed):
            await observed
    except RuntimeToolEffectPersistenceError:
        raise
    except Exception as exc:
        raise RuntimeToolEffectPersistenceError("Tool result could not be persisted") from exc


@contextmanager
def runtime_tool_effect_intent_scope(
    observer: RuntimeToolEffectIntentObserver | None,
) -> Iterator[None]:
    """Install one task-local pre-I/O durability observer."""

    if observer is None:
        yield
        return
    token = _tool_effect_intent_observer.set(observer)
    try:
        yield
    finally:
        _tool_effect_intent_observer.reset(token)


@contextmanager
def runtime_tool_effect_checkpoint_scope(
    checkpoints: dict[str, dict[str, Any]] | None,
) -> Iterator[None]:
    """Bind pre-I/O resumable checkpoints to their exact Runtime call ids."""

    normalized = {
        str(call_id): dict(checkpoint)
        for call_id, checkpoint in (checkpoints or {}).items()
        if str(call_id).strip() and isinstance(checkpoint, dict)
    }
    token = _tool_effect_pending_checkpoints.set(normalized)
    try:
        yield
    finally:
        _tool_effect_pending_checkpoints.reset(token)


@contextmanager
def runtime_consumed_approval_capture_scope() -> Iterator[list[str]]:
    """Capture exact unified approvals consumed by one tool preflight.

    The request id is reported by the server-owned approval consume path, not
    recovered from model-authored tool arguments.  A list makes an impossible
    multi-consume visible to the caller so it can fail closed before I/O.
    """

    captured: list[str] = []
    token = _consumed_approval_request_ids.set(captured)
    try:
        yield captured
    finally:
        _consumed_approval_request_ids.reset(token)


def runtime_record_consumed_approval_request_id(request_id: str) -> None:
    """Record a request id only inside the active server preflight capture."""

    normalized = str(request_id or "").strip()
    captured = _consumed_approval_request_ids.get()
    if normalized and captured is not None and normalized not in captured:
        captured.append(normalized)


async def runtime_record_tool_effect_intent(
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str | None = None,
    classification: "RuntimeToolClassification | None" = None,
    *,
    approval_request_id: str | None = None,
) -> None:
    """Await the active durability barrier before invoking a tool handler.

    Exceptions intentionally propagate.  Reaching the provider without a
    durable intent would reopen an at-least-once replay window, so a failed
    barrier must prevent the handler call.
    """

    observer = _tool_effect_intent_observer.get()
    if observer is None:
        return
    normalized_call_id = str(tool_call_id or "").strip() or None
    from packages.core.ai.runtime.nested_agent import runtime_root_tool_checkpoint, runtime_scoped_tool_call_id

    checkpoint = runtime_current_tool_checkpoint(normalized_call_id) if normalized_call_id else None
    if runtime_tool_effect_durability_enabled():
        from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

        effect = getattr(classification, "effect", None)
        if effect is RuntimeToolEffect.READ_ONLY and (
            checkpoint is None or checkpoint.get("pending_tool_call", {}).get("name") != tool_name
        ):
            return
        if effect not in {RuntimeToolEffect.READ_ONLY, RuntimeToolEffect.CONTROL, RuntimeToolEffect.ORCHESTRATOR}:
            if (
                checkpoint is None
                or checkpoint.get("pending_tool_call", {}).get("name") != tool_name
                or normalized_call_id not in {
                    checkpoint["pending_tool_call"]["id"],
                    runtime_scoped_tool_call_id(checkpoint["pending_tool_call"]["id"]),
                }
            ):
                raise RuntimeToolEffectPersistenceError("Mutating helper has no exact pre-I/O execution frame")
        if checkpoint is not None and checkpoint.get("pending_tool_call", {}).get("name") == tool_name:
            # Runtime may canonically scope artifact paths before approval.
            # Persist those actual approved arguments, not the pre-scope model
            # spelling, so the receipt and replay share one exact identity.
            raw_id = checkpoint["pending_tool_call"]["id"]
            checkpoint["pending_tool_call"]["arguments"] = copy.deepcopy(arguments)
            for message in checkpoint.get("messages") or []:
                for call in message.get("tool_calls") or []:
                    if call.get("id") == raw_id and isinstance(call.get("function"), dict):
                        if call["function"].get("name") == "call_tool":
                            from packages.core.ai.runtime.tool_gateway import runtime_gateway_arguments

                            call["function"]["arguments"] = json.dumps(
                                runtime_gateway_arguments(tool_name, arguments), ensure_ascii=False,
                            )
                        else:
                            call["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
            _tool_effect_pending_checkpoints.set({**_tool_effect_pending_checkpoints.get(), raw_id: checkpoint})
    if checkpoint is not None:
        normalized_call_id = runtime_scoped_tool_call_id(checkpoint["pending_tool_call"]["id"])
    checkpoint = runtime_root_tool_checkpoint(checkpoint)
    observer_args = (
        str(tool_name or ""),
        dict(arguments or {}),
        normalized_call_id,
        classification,
    )
    normalized_approval_request_id = (
        str(approval_request_id or "").strip() or None
    )
    try:
        observer_signature = inspect.signature(observer)
    except (TypeError, ValueError):
        observer_signature = None
    if observer_signature is not None:
        try:
            observer_signature.bind(
                *observer_args,
                checkpoint,
                approval_request_id=normalized_approval_request_id,
            )
        except TypeError:
            try:
                observer_signature.bind(*observer_args, checkpoint)
            except TypeError:
                result = observer(*observer_args)
            else:
                result = observer(*observer_args, checkpoint)
        else:
            result = observer(
                *observer_args,
                checkpoint,
                approval_request_id=normalized_approval_request_id,
            )
    else:
        result = observer(*observer_args)
    if inspect.isawaitable(result):
        await result


__all__ = [
    "RuntimeToolEffectPersistenceError",
    "runtime_current_tool_checkpoint",
    "runtime_consumed_approval_capture_scope",
    "runtime_record_consumed_approval_request_id",
    "runtime_record_tool_effect_intent",
    "runtime_record_tool_effect_result",
    "runtime_tool_effect_checkpoint_scope",
    "runtime_tool_effect_intent_scope",
    "runtime_tool_effect_result_scope",
]
