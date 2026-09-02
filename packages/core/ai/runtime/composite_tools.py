"""Shared contracts for first-party composite Runtime tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SandboxToolAction(StrEnum):
    CREATE = "create"
    EXEC = "exec"
    STATUS = "status"
    RESPOND = "respond"
    CANCEL = "cancel"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    SAVE_RESULT = "save_result"
    DESTROY = "destroy"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


class SandboxToolErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_ACTION = "unsupported_action"
    SKILL_NOT_FOUND = "sandbox_skill_not_found"
    SKILL_REQUIRES_INVOKE = "sandbox_skill_requires_invoke"
    ACCESS_DENIED = "sandbox_access_denied"
    CAPACITY_FULL = "sandbox_capacity_full"
    CREATE_FAILED = "sandbox_creation_failed"
    GENERIC_UNAVAILABLE = "sandbox_generic_unavailable"
    ALREADY_ACTIVE = "sandbox_already_active"
    EXEC_TIMEOUT = "sandbox_execution_timeout"
    EXEC_FAILED = "sandbox_execution_failed"
    EXECUTION_NOT_FOUND = "sandbox_execution_not_found"
    EXECUTION_EVENT_NOT_FOUND = "sandbox_execution_event_not_found"
    RESPOND_FAILED = "sandbox_response_failed"
    CANCEL_FAILED = "sandbox_cancel_failed"
    READ_FAILED = "sandbox_read_failed"
    WRITE_FAILED = "sandbox_write_failed"
    WORKSPACE_FILE_NOT_FOUND = "workspace_file_not_found"
    QUALITY_GATE_BLOCKED = "sandbox_quality_gate_blocked"
    DOWNLOAD_FAILED = "sandbox_download_failed"
    EMPTY_RESULT = "sandbox_empty_result"
    FILESYSTEM_UNAVAILABLE = "entity_filesystem_unavailable"
    WORKSPACE_SCOPE_UNAVAILABLE = "workspace_artifact_scope_unavailable"
    DOCUMENT_SYNC_FAILED = "document_sync_failed"
    DESTROY_FAILED = "sandbox_destroy_failed"


class WorkspaceToolAction(StrEnum):
    SEARCH = "search"
    CREATE_TASK = "create_task"
    UPDATE_TASK_RUNTIME = "update_task_runtime"
    LIST_KNOWLEDGE = "list_knowledge"
    CREATE_KNOWLEDGE_FOLDER = "create_knowledge_folder"
    ADD_KNOWLEDGE_DOCUMENTS = "add_knowledge_documents"
    REMOVE_KNOWLEDGE_DOCUMENT = "remove_knowledge_document"
    UPDATE_KNOWLEDGE_POLICY = "update_knowledge_policy"
    ADD_RULE = "add_rule"
    DELEGATE_SERVICE = "delegate_service"
    GET_GOAL_STATUS = "get_goal_status"
    UPDATE_GOAL_VALUE = "update_goal_value"
    OPERATION = "operation"
    REQUEST_STRATEGIST_REVIEW = "request_strategist_review"
    RESOLVE_HITL = "resolve_hitl"
    ANSWER_TASK_BLOCKER = "answer_task_blocker"
    VISUALIZE_LEDGERS = "visualize_ledgers"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


SANDBOX_LEGACY_TOOL_BY_ACTION = {
    SandboxToolAction.CREATE: "sandbox_create",
    SandboxToolAction.EXEC: "sandbox_exec",
    SandboxToolAction.STATUS: "sandbox_status",
    SandboxToolAction.RESPOND: "sandbox_respond",
    SandboxToolAction.CANCEL: "sandbox_cancel",
    SandboxToolAction.READ_FILE: "sandbox_read_file",
    SandboxToolAction.WRITE_FILE: "sandbox_write_file",
    SandboxToolAction.SAVE_RESULT: "sandbox_save_result",
    SandboxToolAction.DESTROY: "sandbox_destroy",
}


@dataclass(frozen=True)
class RuntimeCompositeToolCall:
    tool_name: str
    arguments: dict[str, Any]


class RuntimeCompositeToolCallFactory:
    """Normalize a gateway call to its governed execution contract."""

    @staticmethod
    def _params(arguments: dict[str, Any]) -> dict[str, Any]:
        raw_params = arguments.get("params")
        return dict(raw_params) if isinstance(raw_params, dict) else {}

    @classmethod
    def sandbox_params(cls, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize nested and provider-flattened Sandbox action params.

        Some tool-calling providers emit action-specific fields beside
        ``action`` even though the composite schema documents a nested
        ``params`` object.  Normalize that compatibility shape here so
        execution, approval classification, and audit receipts all see the
        same arguments.  Explicit nested params win over flattened values.
        """

        args = dict(arguments or {})
        flattened = {
            key: value
            for key, value in args.items()
            if key not in {"action", "params"}
        }
        flattened.update(cls._params(args))
        return flattened

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> RuntimeCompositeToolCall:
        name = str(tool_name or "").strip()
        args = dict(arguments or {})
        params = cls._params(args)

        if name == "sandbox":
            try:
                action = SandboxToolAction(str(args.get("action") or "").strip())
            except ValueError:
                return RuntimeCompositeToolCall(name, args)
            return RuntimeCompositeToolCall(
                SANDBOX_LEGACY_TOOL_BY_ACTION[action],
                cls.sandbox_params(args),
            )

        if name == "manor" and str(args.get("action") or "").strip() == "workspace":
            workspace_action = str(params.get("action") or "").strip()
            raw_workspace_params = params.get("params")
            workspace_params = (
                dict(raw_workspace_params)
                if isinstance(raw_workspace_params, dict)
                else {
                    key: value
                    for key, value in params.items()
                    if key not in {"action", "params"}
                }
            )
            return RuntimeCompositeToolCall(
                "workspace_agent",
                {"action": workspace_action, "params": workspace_params},
            )

        return RuntimeCompositeToolCall(name, args)


__all__ = [
    "RuntimeCompositeToolCall",
    "RuntimeCompositeToolCallFactory",
    "SANDBOX_LEGACY_TOOL_BY_ACTION",
    "SandboxToolErrorCode",
    "SandboxToolAction",
    "WorkspaceToolAction",
]
