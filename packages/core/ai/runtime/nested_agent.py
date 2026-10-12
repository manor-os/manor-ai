"""Host-owned nested Agent continuations, independent of worker persistence.

A child checkpoint is always rooted in its pending parent tool call. Only the
host's checkpoint observer sees this tree; tool JSON cannot install a frame.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint, RuntimeTurnAborted


_frames: ContextVar[tuple["RuntimeNestedSkillFrame", ...]] = ContextVar(
    "runtime_nested_skill_frames", default=(),
)
_observer: ContextVar[Callable[..., Any] | None] = ContextVar(
    "runtime_nested_checkpoint_observer", default=None,
)
_capture_native_approvals: ContextVar[bool] = ContextVar("runtime_nested_approval_checkpoints", default=False)
_FRAME_KEY = "nested_skill"


def _qualified_call_id(call_id: str, path: list[tuple[str, str]]) -> str:
    if not path:
        return call_id
    identity = json.dumps([path, call_id], separators=(",", ":"), ensure_ascii=False)
    return "nested_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def runtime_scoped_tool_call_id(call_id: str) -> str:
    """Host execution identity; model-facing IDs remain local to each loop."""
    return _qualified_call_id(str(call_id), [
        (frame.parent_checkpoint["pending_tool_call"]["id"], frame.skill_id)
        for frame in _frames.get()
    ])


def runtime_checkpoint_tool_owner(
    checkpoint: dict[str, Any], tool_call_id: str,
) -> dict[str, Any]:
    """Find the exact execution call in a persisted host checkpoint tree."""
    leaf = checkpoint
    path: list[tuple[str, str]] = []
    for _ in range(9):
        pending = leaf["pending_tool_call"]
        if _qualified_call_id(pending["id"], path) == tool_call_id:
            return leaf
        state = runtime_nested_skill_state(leaf)
        if not state or state.get("kind") != "pending":
            break
        path.append((pending["id"], state["skill_id"]))
        # Use the original mutable tree, not the copied public inspection.
        leaf = leaf["tool_continuations"][pending["id"]][_FRAME_KEY]["checkpoint"]
    raise RuntimeTurnAborted("Tool result does not own this execution frame")


def runtime_checkpoint_leaf_call_id(checkpoint: dict[str, Any]) -> str:
    leaf = checkpoint
    path: list[tuple[str, str]] = []
    for _ in range(9):
        pending = leaf["pending_tool_call"]
        state = runtime_nested_skill_state(leaf)
        if not state or state.get("kind") != "pending":
            return _qualified_call_id(pending["id"], path)
        path.append((pending["id"], state["skill_id"]))
        leaf = state["checkpoint"]
    raise RuntimeTurnAborted("Nested Skill checkpoint depth exceeded")


@contextmanager
def runtime_nested_checkpoint_scope(
    observer: Callable[..., Any], *, checkpoint_approvals: bool = False,
) -> Iterator[None]:
    token = _observer.set(observer)
    approval_token = _capture_native_approvals.set(checkpoint_approvals)
    try:
        yield
    finally:
        _capture_native_approvals.reset(approval_token)
        _observer.reset(token)


def runtime_nested_skill_state(checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    pending = checkpoint.get("pending_tool_call") or {}
    if pending.get("name") != "invoke_skill":
        return None
    continuation = (checkpoint.get("tool_continuations") or {}).get(pending.get("id")) or {}
    state = continuation.get(_FRAME_KEY)
    if not isinstance(state, dict):
        return None
    if state.get("version") != 1 or not state.get("skill_id"):
        raise RuntimeTurnAborted("Invalid nested Skill checkpoint identity")
    return copy.deepcopy(state)


def runtime_pending_checkpoint_leaf(
    checkpoint: dict[str, Any], *, tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Return the mutable pending leaf of a server-owned checkpoint tree."""
    leaf = checkpoint
    for _ in range(9):
        RuntimeAgentCheckpoint.from_dict(leaf)
        pending = leaf["pending_tool_call"]
        if tool_call_id is not None and pending["id"] == tool_call_id:
            return leaf
        continuation = (leaf.get("tool_continuations") or {}).get(pending["id"]) or {}
        state = continuation.get(_FRAME_KEY) if pending["name"] == "invoke_skill" else None
        if not isinstance(state, dict) or state.get("kind") != "pending":
            return leaf
        if state.get("version") != 1 or not state.get("skill_id") or not isinstance(state.get("checkpoint"), dict):
            raise RuntimeTurnAborted("Invalid nested Skill continuation")
        leaf = state["checkpoint"]
    raise RuntimeTurnAborted("Nested Skill checkpoint depth exceeded")


@dataclass
class RuntimeNestedSkillFrame:
    parent_checkpoint: dict[str, Any]
    skill_id: str
    state: dict[str, Any]

    def wrap(self, state: dict[str, Any]) -> dict[str, Any]:
        self.state = {"version": 1, "skill_id": self.skill_id, **copy.deepcopy(state)}
        parent = copy.deepcopy(self.parent_checkpoint)
        call_id = parent["pending_tool_call"]["id"]
        parent.setdefault("tool_continuations", {}).setdefault(call_id, {})[_FRAME_KEY] = self.state
        return parent

    @property
    def completed_response(self) -> dict[str, Any] | None:
        value = self.state.get("response")
        if self.state.get("kind") == "completed" and isinstance(value, dict):
            return copy.deepcopy(value)
        if self.state.get("terminal_failure") == "tool_outcome_unconfirmed":
            # A crash between the failed tool round and final Skill summary
            # must not replay the nested loop with its tools available again.
            return {
                "skill": self.skill_id,
                "content": (
                    "A tool reported an error, but its action may have happened. "
                    "Check the external result before retrying."
                ),
                "usage": copy.deepcopy(self.state.get("usage") or {}),
                "tools_used": list(self.state.get("tool_calls_made") or []),
                "rounds": int(self.state.get("rounds") or 0),
                "stop_reason": "error",
                "error": "tool_error",
                "control": {
                    "kind": "tool_error",
                    "error_reason": "tool_outcome_unconfirmed",
                    "terminal_failure": True,
                    "retryable": False,
                },
            }
        return None

    def loop_kwargs(self) -> dict[str, Any]:
        approval_kwargs = {"hitl_resume_checkpoint": True} if _capture_native_approvals.get() else {}
        state = self.state
        if state.get("kind") == "pending":
            checkpoint = RuntimeAgentCheckpoint.from_dict(state["checkpoint"])
            return {**approval_kwargs, "resume_checkpoint": checkpoint.to_dict(), "forced_tool_calls": None}
        if state.get("kind") == "transcript":
            if state.get("terminal_failure") == "tool_outcome_unconfirmed":
                raise RuntimeTurnAborted(
                    "Nested Skill tool outcome is unconfirmed; do not replay it"
                )
            return {
                **approval_kwargs,
                "initial_messages": [
                    item for item in copy.deepcopy(state["messages"])
                    if item.get("role") != "system"
                ],
                "initial_rounds": state["rounds"],
                "initial_usage": copy.deepcopy(state["usage"]),
                "initial_tool_calls_made": list(state["tool_calls_made"]),
                "initial_tool_evidence": copy.deepcopy(state.get("tool_evidence") or {}),
                "forced_tool_calls": None,
            }
        return approval_kwargs

    async def on_round_end(
        self, rounds, messages, usage, tool_calls_made, runtime_state=None,
    ) -> None:
        state = {
            "kind": "transcript", "rounds": rounds,
            "messages": messages, "usage": usage, "tool_calls_made": tool_calls_made,
            "tool_evidence": copy.deepcopy((runtime_state or {}).get("tool_evidence") or {}),
        }
        if (
            isinstance(runtime_state, dict)
            and runtime_state.get("terminal_failure") == "tool_outcome_unconfirmed"
        ):
            state["terminal_failure"] = "tool_outcome_unconfirmed"
        await self._persist(state, messages)

    async def complete(self, response: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        control = response.get("control") or {}
        if isinstance(control.get("checkpoint"), dict):
            # A wait is not a completed Skill. Keep its exact child call pending.
            checkpoint = copy.deepcopy(control["checkpoint"])
            previous = self.state.get("checkpoint") if self.state.get("kind") == "pending" else None
            if (_capture_native_approvals.get() and isinstance(previous, dict)
                and previous.get("pending_tool_call", {}).get("id") == checkpoint.get("pending_tool_call", {}).get("id")
                and runtime_nested_skill_state(previous) is not None):
                _attach_nested_approval_child(checkpoint, previous)
            await self._persist({"kind": "pending", "checkpoint": checkpoint}, messages)
        elif response.get("stop_reason") not in {"hitl_required", "waiting_resource"}:
            # Keep exact child observations until the parent return commits its
            # host-owned handoff. Parent prose is not child execution evidence.
            await self._persist({"kind": "completed", "response": response, "messages": messages}, messages)

    async def _persist(self, state: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        observer = _observer.get()
        if observer is None:
            return
        checkpoint = self.wrap(state)
        for ancestor in reversed(_frames.get()[:-1]):
            checkpoint = ancestor.wrap({"kind": "pending", "checkpoint": checkpoint})
        try:
            result = observer(checkpoint, copy.deepcopy(messages))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            from packages.core.ai.runtime.tool_effect_intents import RuntimeToolEffectPersistenceError

            raise RuntimeToolEffectPersistenceError("Nested Skill checkpoint could not be persisted") from exc


def runtime_root_tool_checkpoint(checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    rooted = copy.deepcopy(checkpoint)
    for frame in reversed(_frames.get()):
        rooted = frame.wrap({"kind": "pending", "checkpoint": rooted})
    return rooted


@contextmanager
def runtime_nested_skill_scope(skill_id: str) -> Iterator[RuntimeNestedSkillFrame | None]:
    from packages.core.ai.runtime.tool_effect_intents import runtime_current_tool_checkpoint

    parent = runtime_current_tool_checkpoint()
    if _observer.get() is None or parent is None:
        yield None
        return
    parsed = RuntimeAgentCheckpoint.from_dict(parent)
    if parsed.pending_tool_call.get("name") != "invoke_skill":
        raise RuntimeTurnAborted("Nested Skill has no parent invoke_skill call")
    state = runtime_nested_skill_state(parent) or {}
    if state and state["skill_id"] != str(skill_id):
        raise RuntimeTurnAborted("Nested Skill identity changed during resume")
    if len(_frames.get()) >= 8:
        raise RuntimeTurnAborted("Nested Skill checkpoint depth exceeded")
    frame = RuntimeNestedSkillFrame(parent, str(skill_id), state)
    token = _frames.set((*_frames.get(), frame))
    try:
        yield frame
    finally:
        _frames.reset(token)


async def runtime_execute_with_nested_approval_checkpoints(operation, *args, **kwargs):
    """Keep host-owned child continuations on a Workflow's approval checkpoint."""
    observed = {}

    def observe(checkpoint, _messages):
        pending = checkpoint["pending_tool_call"]
        observed[pending["id"]] = copy.deepcopy(checkpoint)

    with runtime_nested_checkpoint_scope(observe, checkpoint_approvals=True):
        result = await operation(*args, **kwargs)
    control = result.get("control") if isinstance(result, dict) else getattr(result, "control", None)
    if not isinstance(control, dict) or control.get("kind") != "approval_required":
        return result
    checkpoint = control.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return result
    pending = checkpoint.get("pending_tool_call") or {}
    tree = observed.get(pending.get("id"))
    if tree is None:
        return result
    _attach_nested_approval_child(checkpoint, tree)
    return result


def _attach_nested_approval_child(checkpoint, tree):
    pending = checkpoint["pending_tool_call"]
    if tree["pending_tool_call"] != pending:
        raise RuntimeTurnAborted("Nested approval parent call changed")
    state = runtime_nested_skill_state(tree)
    if not state or state.get("kind") != "pending":
        raise RuntimeTurnAborted("Nested approval has no pending child checkpoint")
    checkpoint.setdefault("tool_continuations", {})[pending["id"]] = copy.deepcopy(
        tree["tool_continuations"][pending["id"]]
    )
