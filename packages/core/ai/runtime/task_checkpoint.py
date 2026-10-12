"""Durable TaskRunner state for a runtime approval boundary.

The scheduled-agent loop is intentionally separate from interactive Chat, but
an approval must have the same continuation guarantees in both places.  This
module owns the JSON-only checkpoint contract so a worker can stop, release
its lease, and later resume from the exact transcript without asking the model
to reconstruct an approved action.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from packages.core.ai.engine import ChatMessage


RUNTIME_TASK_HITL_CHECKPOINT_SCHEMA_VERSION = 1
RUNTIME_TASK_HITL_CHECKPOINT_MAX_BYTES = 512 * 1024
_CHECKPOINT_SIGNATURE_DOMAIN = b"manor.task-runtime-hitl-checkpoint.v1\0"


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _validate_checkpoint_size(value: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Task runtime HITL checkpoint is not JSON serializable") from exc
    if len(encoded) > RUNTIME_TASK_HITL_CHECKPOINT_MAX_BYTES:
        raise ValueError("Task runtime HITL checkpoint is too large")


def _validated_checkpoint_payload(value: Any) -> dict[str, Any]:
    """Return one canonical, executable checkpoint payload.

    Both persistence and restore pass through this boundary.  Validating only
    on restore would still allow malformed in-memory state to become durable
    and strand a scheduled Task at the human gate.
    """

    if not isinstance(value, dict):
        raise ValueError("Task runtime HITL checkpoint must be an object")
    _validate_checkpoint_size(value)
    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version != RUNTIME_TASK_HITL_CHECKPOINT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported Task runtime HITL checkpoint schema")
    task_id = value.get("task_id")
    entity_id = value.get("entity_id")
    workspace_id = value.get("workspace_id")
    execution_user_id = value.get("execution_user_id")
    agent_id = value.get("agent_id")
    agent_subscription_id = value.get("agent_subscription_id")
    scheduled_run_id = value.get("scheduled_run_id")
    scheduled_job_id = value.get("scheduled_job_id")
    conversation_id = value.get("conversation_id")
    hitl_id = value.get("hitl_id")
    approval_binding_digest = value.get("approval_binding_digest")
    generation = value.get("generation")
    messages = value.get("messages")
    usage = value.get("usage", {})
    turns_used = value.get("turns_used", 0)
    tool_calls_made = value.get("tool_calls_made", [])
    remaining = value.get("remaining_tool_calls", [])
    evidence = value.get("supervisor_evidence", [])
    disable_followup_tools = value.get("disable_followup_tools", False)
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or not isinstance(entity_id, str)
        or not entity_id.strip()
        or not isinstance(execution_user_id, str)
        or not execution_user_id.strip()
        or not isinstance(conversation_id, str)
        or not conversation_id.strip()
        or not isinstance(hitl_id, str)
        or not hitl_id.strip()
        or not isinstance(approval_binding_digest, str)
        or len(approval_binding_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in approval_binding_digest)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(messages, list)
    ):
        raise ValueError("Task runtime HITL checkpoint identity is invalid")
    optional_ids = {
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "agent_subscription_id": agent_subscription_id,
        "scheduled_run_id": scheduled_run_id,
        "scheduled_job_id": scheduled_job_id,
    }
    if any(
        value is not None
        and (not isinstance(value, str) or not value.strip())
        for value in optional_ids.values()
    ):
        raise ValueError("Task runtime HITL checkpoint scope is invalid")
    if not isinstance(usage, dict):
        raise ValueError("Task runtime HITL usage is invalid")
    if (
        isinstance(turns_used, bool)
        or not isinstance(turns_used, int)
        or turns_used < 0
    ):
        raise ValueError("Task runtime HITL turn count is invalid")
    if not isinstance(tool_calls_made, list) or not all(
        isinstance(name, str) and name.strip() for name in tool_calls_made
    ):
        raise ValueError("Task runtime HITL tool history is invalid")
    if not isinstance(disable_followup_tools, bool):
        raise ValueError("Task runtime HITL follow-up policy is invalid")
    # Validate the transcript before accepting the checkpoint as executable.
    runtime_deserialize_task_messages(messages)
    if not isinstance(remaining, list) or not all(
        isinstance(call, dict) for call in remaining
    ):
        raise ValueError("Task runtime HITL remaining calls are invalid")
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) for item in evidence
    ):
        raise ValueError("Task runtime HITL evidence is invalid")
    return {
        "schema_version": schema_version,
        "task_id": task_id.strip(),
        "entity_id": entity_id.strip(),
        "workspace_id": workspace_id.strip() if workspace_id else None,
        "execution_user_id": execution_user_id.strip(),
        "agent_id": agent_id.strip() if agent_id else None,
        "agent_subscription_id": (
            agent_subscription_id.strip() if agent_subscription_id else None
        ),
        "scheduled_run_id": scheduled_run_id.strip() if scheduled_run_id else None,
        "scheduled_job_id": scheduled_job_id.strip() if scheduled_job_id else None,
        "conversation_id": conversation_id.strip(),
        "hitl_id": hitl_id.strip(),
        "approval_binding_digest": approval_binding_digest,
        "generation": generation,
        "messages": _json_copy(messages),
        "usage": _json_copy(usage),
        "turns_used": turns_used,
        "tool_calls_made": list(tool_calls_made),
        "supervisor_evidence": _json_copy(evidence),
        "remaining_tool_calls": _json_copy(remaining),
        "disable_followup_tools": disable_followup_tools,
    }


def runtime_serialize_task_messages(
    messages: Iterable[ChatMessage],
) -> list[dict[str, Any]]:
    """Serialize the private model transcript without provider wire rewrites."""

    serialized: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {
            "role": str(message.role or ""),
            "content": str(message.content or ""),
        }
        if message.tool_calls:
            item["tool_calls"] = _json_copy(message.tool_calls)
        if message.tool_call_id:
            item["tool_call_id"] = str(message.tool_call_id)
        serialized.append(item)
    return serialized


def runtime_deserialize_task_messages(
    messages: Iterable[dict[str, Any]],
) -> list[ChatMessage]:
    """Restore a validated TaskRunner transcript from its JSON checkpoint."""

    restored: list[ChatMessage] = []
    for raw in messages:
        if not isinstance(raw, dict):
            raise ValueError("Task runtime checkpoint contains an invalid message")
        role = str(raw.get("role") or "").strip()
        if role not in {"user", "assistant", "tool", "system"}:
            raise ValueError("Task runtime checkpoint contains an invalid role")
        tool_calls = raw.get("tool_calls")
        if tool_calls is not None and (
            not isinstance(tool_calls, list)
            or not all(isinstance(call, dict) for call in tool_calls)
        ):
            raise ValueError("Task runtime checkpoint contains invalid tool calls")
        restored.append(
            ChatMessage(
                role=role,
                content=str(raw.get("content") or ""),
                tool_calls=_json_copy(tool_calls) if tool_calls else None,
                tool_call_id=(
                    str(raw.get("tool_call_id"))
                    if raw.get("tool_call_id")
                    else None
                ),
            )
        )
    return restored


@dataclass(frozen=True)
class RuntimeTaskHitlCheckpoint:
    """The exact scheduled-agent state owned by one ``HitlRequest``."""

    task_id: str
    entity_id: str
    workspace_id: str | None
    execution_user_id: str
    agent_id: str | None
    agent_subscription_id: str | None
    scheduled_run_id: str | None
    scheduled_job_id: str | None
    conversation_id: str
    hitl_id: str
    approval_binding_digest: str
    generation: int
    messages: list[dict[str, Any]]
    usage: dict[str, Any]
    turns_used: int
    tool_calls_made: list[str] = field(default_factory=list)
    supervisor_evidence: list[dict[str, str]] = field(default_factory=list)
    remaining_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    disable_followup_tools: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _validated_checkpoint_payload({
            "schema_version": RUNTIME_TASK_HITL_CHECKPOINT_SCHEMA_VERSION,
            "task_id": self.task_id,
            "entity_id": self.entity_id,
            "workspace_id": self.workspace_id,
            "execution_user_id": self.execution_user_id,
            "agent_id": self.agent_id,
            "agent_subscription_id": self.agent_subscription_id,
            "scheduled_run_id": self.scheduled_run_id,
            "scheduled_job_id": self.scheduled_job_id,
            "conversation_id": self.conversation_id,
            "hitl_id": self.hitl_id,
            "approval_binding_digest": self.approval_binding_digest,
            "generation": self.generation,
            "messages": _json_copy(self.messages),
            "usage": _json_copy(self.usage),
            "turns_used": self.turns_used,
            "tool_calls_made": self.tool_calls_made,
            "supervisor_evidence": _json_copy(self.supervisor_evidence),
            "remaining_tool_calls": _json_copy(self.remaining_tool_calls),
            "disable_followup_tools": self.disable_followup_tools,
        })

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def signature(self) -> str:
        """Authenticate the full checkpoint against stale/internal JSON writers."""

        from packages.core.config import get_settings

        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        secret = get_settings().JWT_SECRET_KEY.encode("utf-8")
        return hmac.new(
            secret,
            _CHECKPOINT_SIGNATURE_DOMAIN + encoded,
            hashlib.sha256,
        ).hexdigest()

    def verify_signature(self, signature: Any) -> bool:
        return bool(
            isinstance(signature, str)
            and len(signature) == 64
            and hmac.compare_digest(signature, self.signature())
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeTaskHitlCheckpoint":
        payload = _validated_checkpoint_payload(value)
        return cls(
            task_id=payload["task_id"],
            entity_id=payload["entity_id"],
            workspace_id=payload["workspace_id"],
            execution_user_id=payload["execution_user_id"],
            agent_id=payload["agent_id"],
            agent_subscription_id=payload["agent_subscription_id"],
            scheduled_run_id=payload["scheduled_run_id"],
            scheduled_job_id=payload["scheduled_job_id"],
            conversation_id=payload["conversation_id"],
            hitl_id=payload["hitl_id"],
            approval_binding_digest=payload["approval_binding_digest"],
            generation=payload["generation"],
            messages=payload["messages"],
            usage=payload["usage"],
            turns_used=payload["turns_used"],
            tool_calls_made=payload["tool_calls_made"],
            supervisor_evidence=payload["supervisor_evidence"],
            remaining_tool_calls=payload["remaining_tool_calls"],
            disable_followup_tools=payload["disable_followup_tools"],
        )


__all__ = [
    "RUNTIME_TASK_HITL_CHECKPOINT_MAX_BYTES",
    "RUNTIME_TASK_HITL_CHECKPOINT_SCHEMA_VERSION",
    "RuntimeTaskHitlCheckpoint",
    "runtime_deserialize_task_messages",
    "runtime_serialize_task_messages",
]
