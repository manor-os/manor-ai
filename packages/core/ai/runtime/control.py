"""Typed internal control results for durable Runtime execution."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


_CHROME_RUN_SEQUENCE_TOOL_NAME = "mcp__chrome__run_sequence"
_RUNTIME_SEQUENCE_OWNER_TOOL_NAMES = frozenset({
    _CHROME_RUN_SEQUENCE_TOOL_NAME,
    "invoke_skill",
})
_CHROME_SEQUENCE_CHILD_TOOL_NAMES = frozenset({
    "mcp__chrome__open_or_reuse",
    "mcp__chrome__target_state",
    "mcp__chrome__click_element",
    "mcp__chrome__fill_or_select",
    "mcp__chrome__hover",
    "mcp__chrome__scroll",
    "mcp__chrome__wait",
})
_RUNTIME_SUSPENSION_CHECKPOINT_ARGUMENTS_MAX_BYTES = 64_000


def runtime_terminal_uncertainty_control(result: Any) -> dict[str, Any] | None:
    """Return the exact terminal control for an unknown mutation outcome."""

    if isinstance(result, dict):
        payload = result
    elif isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return None
        payload = parsed if isinstance(parsed, dict) else None
    else:
        payload = None
    if not isinstance(payload, dict):
        return None
    if not (
        payload.get("stop_parent") is True
        and payload.get("stop_reason") == "uncertain_result"
        and payload.get("action_executed") == "unknown"
        and payload.get("retryable") is False
        and payload.get("terminal_failure") is True
    ):
        return None
    return copy.deepcopy(payload)


class _RuntimeOwnedSequenceChildResult(str):
    """Public JSON text carrying non-serialized Runtime ownership metadata."""

    def __new__(
        cls,
        public_result: str,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        raw_result: Any,
    ) -> "_RuntimeOwnedSequenceChildResult":
        owned = super().__new__(cls, public_result)
        owned._runtime_child_tool_name = tool_name
        owned._runtime_child_arguments = copy.deepcopy(arguments)
        owned._runtime_child_raw_result = copy.deepcopy(raw_result)
        return owned


def runtime_owned_sequence_child_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    public_result: str,
) -> str:
    """Attach trusted child ownership without changing the public JSON text."""

    normalized_name = str(tool_name or "").strip()
    if (
        normalized_name not in _CHROME_SEQUENCE_CHILD_TOOL_NAMES
        or not isinstance(arguments, dict)
        or not isinstance(public_result, str)
    ):
        raise ValueError("invalid Runtime-owned Chrome sequence child result")
    try:
        public_payload = json.loads(public_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid public Chrome sequence child result") from exc
    if not isinstance(public_payload, dict):
        raise ValueError("invalid public Chrome sequence child result")
    return _RuntimeOwnedSequenceChildResult(
        public_result,
        tool_name=normalized_name,
        arguments=arguments,
        raw_result=result,
    )


def runtime_sequence_child_owner(
    outer_tool_name: str,
    result: Any,
) -> dict[str, Any] | None:
    """Read private ownership attached by the server-owned sequence handler."""

    if str(outer_tool_name or "").strip() not in _RUNTIME_SEQUENCE_OWNER_TOOL_NAMES:
        return None
    if not isinstance(result, _RuntimeOwnedSequenceChildResult):
        return None
    tool_name = str(result._runtime_child_tool_name or "").strip()
    arguments = result._runtime_child_arguments
    if tool_name not in _CHROME_SEQUENCE_CHILD_TOOL_NAMES or not isinstance(arguments, dict):
        return None
    return {
        "tool_name": tool_name,
        "arguments": copy.deepcopy(arguments),
        "raw_result": copy.deepcopy(result._runtime_child_raw_result),
    }


def runtime_rewrap_sequence_child_result(
    result: Any,
    *,
    public_result: str,
) -> str:
    """Carry a sequence child's private owner across a host-authored wrapper."""

    if not isinstance(result, _RuntimeOwnedSequenceChildResult):
        return public_result
    return runtime_owned_sequence_child_result(
        tool_name=result._runtime_child_tool_name,
        arguments=result._runtime_child_arguments,
        result=result._runtime_child_raw_result,
        public_result=public_result,
    )


class RuntimeTurnAborted(ValueError):
    """Stop a stale/invalid turn unless a different tool path remains safe.

    ``allow_alternate_path`` is deliberately opt-in.  Runtime callers may set
    it only when they know the failure happened before external I/O, so the
    agent loop can keep working without repeating the failed approved action.
    """

    def __init__(
        self,
        message: str,
        *,
        allow_alternate_path: bool = False,
    ) -> None:
        super().__init__(message)
        self.allow_alternate_path = allow_alternate_path


#: ``RuntimeToolSuspension.kind`` for a turn parked on a human answer.
HUMAN_INPUT_SUSPENSION_KIND = "waiting_human"
#: ``RuntimeToolSuspension.reason`` / ``RuntimeRun.status_reason`` while parked.
HUMAN_INPUT_SUSPENSION_REASON = "awaiting_human_input"
#: ``RuntimeRun.status_reason`` once the answer is written and resume is queued.
HUMAN_INPUT_ANSWERED_REASON = "human_input_answered"


@dataclass(frozen=True)
class RuntimeToolSuspension:
    """A tool-boundary wait that must never be shown to the model as an error.

    ``payload`` carries what the waiting side needs to render the wait — for a
    human-input suspension that is the question, its options and fields. A
    sandbox-capacity suspension leaves it ``None``.
    """

    kind: str
    reservation_id: str
    poll_after_seconds: int
    deadline_at: datetime | str
    reason: str = "sandbox_capacity"
    payload: dict[str, Any] | None = None
    _checkpoint_tool_call: dict[str, Any] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def checkpoint_tool_call(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._checkpoint_tool_call)

    def with_checkpoint_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> "RuntimeToolSuspension":
        normalized_id = str(tool_call_id or "").strip()
        normalized_name = str(tool_name or "").strip()
        if not normalized_id or not normalized_name or not isinstance(arguments, dict):
            raise ValueError("invalid Runtime suspension checkpoint owner")
        try:
            encoded_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Runtime suspension checkpoint arguments are not JSON serializable"
            ) from exc
        if len(encoded_arguments) > _RUNTIME_SUSPENSION_CHECKPOINT_ARGUMENTS_MAX_BYTES:
            raise ValueError("Runtime suspension checkpoint arguments are too large")
        owned = replace(self)
        object.__setattr__(
            owned,
            "_checkpoint_tool_call",
            {
                "id": normalized_id,
                "name": normalized_name,
                "arguments": copy.deepcopy(arguments),
            },
        )
        return owned

    def to_dict(self) -> dict[str, Any]:
        deadline = (
            self.deadline_at.isoformat()
            if isinstance(self.deadline_at, datetime)
            else str(self.deadline_at)
        )
        control: dict[str, Any] = {
            "kind": self.kind,
            "reservation_id": self.reservation_id,
            "poll_after_seconds": self.poll_after_seconds,
            "deadline_at": deadline,
            "reason": self.reason,
        }
        if self.payload is not None:
            control["payload"] = dict(self.payload)
        return control

    @property
    def is_human_input(self) -> bool:
        return self.kind == HUMAN_INPUT_SUSPENSION_KIND


def is_human_input_suspension_control(control: Any) -> bool:
    """True when a suspension control dict came from ``request_input``."""
    return isinstance(control, dict) and control.get("kind") == HUMAN_INPUT_SUSPENSION_KIND


@dataclass
class RuntimeAgentCheckpoint:
    """Serializable state needed to resume at one pending tool call."""

    messages: list[dict[str, Any]]
    usage: dict[str, Any]
    rounds: int
    tool_calls_made: list[str]
    pending_tool_call: dict[str, Any]
    remaining_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_attempt: int = 1
    seen_tool_result_digests: dict[str, str] = field(default_factory=dict)
    disable_followup_tools: bool = False
    tool_continuations: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``{"id": <pending tool call id>, "content": <tool result text>}`` — a
    #: result that arrived while the turn was parked (a human's answer to
    #: ``request_input``). On resume the loop appends it as the pending call's
    #: result instead of re-executing the tool.
    pending_tool_result: dict[str, Any] | None = None
    final_response_evidence: dict[str, Any] = field(default_factory=dict)
    tool_evidence: dict[str, Any] = field(default_factory=dict)
    web_source_slices: list[dict[str, Any]] = field(default_factory=list)
    #: Tools discovery loaded before the park (names only); the resume
    #: re-authorizes them by exact discovery (RuntimeRunState).
    loaded_gateway_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "messages": [dict(message) for message in self.messages],
            "usage": dict(self.usage),
            "rounds": self.rounds,
            "tool_calls_made": list(self.tool_calls_made),
            "pending_tool_call": dict(self.pending_tool_call),
            "remaining_tool_calls": [dict(item) for item in self.remaining_tool_calls],
            "pending_attempt": self.pending_attempt,
            "seen_tool_result_digests": dict(self.seen_tool_result_digests),
            "disable_followup_tools": self.disable_followup_tools,
            "tool_continuations": {
                str(key): dict(item)
                for key, item in self.tool_continuations.items()
                if isinstance(item, dict)
            },
            "pending_tool_result": (
                dict(self.pending_tool_result)
                if isinstance(self.pending_tool_result, dict)
                else None
            ),
            "final_response_evidence": dict(self.final_response_evidence),
            "tool_evidence": dict(self.tool_evidence),
            "web_source_slices": [dict(item) for item in self.web_source_slices],
            "loaded_gateway_tools": [str(name) for name in self.loaded_gateway_tools],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeAgentCheckpoint":
        schema_version = int(value.get("schema_version") or 0)
        if schema_version not in {1, 2, 3, 4}:
            raise ValueError("unsupported Runtime checkpoint schema")
        pending = value.get("pending_tool_call")
        if not isinstance(pending, dict) or not pending.get("id") or not pending.get("name"):
            raise ValueError("Runtime checkpoint is missing pending tool identity")
        web_source_slices = value.get("web_source_slices") or []
        if not isinstance(web_source_slices, list) or any(
            not isinstance(item, dict) for item in web_source_slices
        ):
            raise ValueError("invalid web source checkpoint")
        return cls(
            messages=[dict(item) for item in value.get("messages") or []],
            usage=dict(value.get("usage") or {}),
            rounds=max(0, int(value.get("rounds") or 0)),
            tool_calls_made=[str(item) for item in value.get("tool_calls_made") or []],
            pending_tool_call=dict(pending),
            remaining_tool_calls=[
                dict(item) for item in value.get("remaining_tool_calls") or []
                if isinstance(item, dict)
            ],
            pending_attempt=max(1, int(value.get("pending_attempt") or 1)),
            seen_tool_result_digests={
                str(key): str(item)
                for key, item in dict(value.get("seen_tool_result_digests") or {}).items()
            },
            disable_followup_tools=(
                bool(value.get("disable_followup_tools"))
                if schema_version >= 2
                else False
            ),
            tool_continuations=(
                {
                    str(key): dict(item)
                    for key, item in dict(value.get("tool_continuations") or {}).items()
                    if isinstance(item, dict)
                }
                if schema_version >= 2
                else {}
            ),
            pending_tool_result=(
                dict(value["pending_tool_result"])
                if schema_version >= 4
                and isinstance(value.get("pending_tool_result"), dict)
                and value["pending_tool_result"].get("id")
                else None
            ),
            final_response_evidence=(
                dict(value["final_response_evidence"])
                if isinstance(value.get("final_response_evidence"), dict) else {}
            ),
            tool_evidence=(dict(value["tool_evidence"]) if isinstance(value.get("tool_evidence"), dict) else {}),
            web_source_slices=[dict(item) for item in web_source_slices],
            # A checkpoint parked before the field existed: its discovery
            # loads from the ledger (RuntimeRunState.restore does the same).
            loaded_gateway_tools=_runtime_checkpoint_loaded_tools(value),
        )


def _runtime_checkpoint_loaded_tools(value: dict[str, Any]) -> list[str]:
    from packages.core.ai.runtime.run_state import RuntimeRunState

    return RuntimeRunState.restore({
        key: value[key] for key in ("loaded_gateway_tools", "tool_evidence") if key in value
    }).loaded_gateway_tools


def is_runtime_tool_suspension(value: Any) -> bool:
    return isinstance(value, RuntimeToolSuspension)


__all__ = [
    "HUMAN_INPUT_ANSWERED_REASON",
    "HUMAN_INPUT_SUSPENSION_KIND",
    "HUMAN_INPUT_SUSPENSION_REASON",
    "RuntimeAgentCheckpoint",
    "RuntimeToolSuspension",
    "is_human_input_suspension_control",
    "RuntimeTurnAborted",
    "is_runtime_tool_suspension",
    "runtime_owned_sequence_child_result",
    "runtime_rewrap_sequence_child_result",
    "runtime_sequence_child_owner",
    "runtime_terminal_uncertainty_control",
]
