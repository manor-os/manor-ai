from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.core.ai.runtime.approval_catalog import direct_chat_default_approval_mode
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.events import RuntimeEventType


_ACTION_KEY_CAPABILITY_ALIASES: dict[str, str] = {
    "workspace_agent": "workspace.operate",
    "workspace_operation": "workspace.operate",
    "workspace_create_task": "workspace.task",
    "workspace_update_task_runtime": "workspace.task",
    "workspace_create_knowledge_folder": "workspace.knowledge",
    "workspace_add_knowledge_documents": "workspace.knowledge",
    "workspace_remove_knowledge_document": "workspace.knowledge",
    "workspace_update_knowledge_policy": "workspace.knowledge",
    "workspace_add_rule": "workspace.governance",
    "workspace_request_strategist_review": "workspace.governance",
    "workspace_search": "workspace.search",
    "workspace_list_knowledge": "workspace.search",
    "rag": "workspace.search",
    "web_search": "web.safe_search",
    "web_fetch": "web.safe_search",
    "browse_web": "web.safe_search",
    "write_file": "file.write",
    "edit_file": "file.write",
    "delete_file": "file.write",
    "generate_file": "file.write",
    "sandbox_create": "sandbox.execute",
    "sandbox_exec": "sandbox.execute",
    "sandbox_read_file": "sandbox.execute",
    "sandbox_write_file": "sandbox.execute",
    "sandbox_save_result": "sandbox.execute",
    "sandbox_destroy": "sandbox.execute",
    "create_scheduled_job": "automation.manage",
    "list_scheduled_jobs": "automation.manage",
    "cancel_scheduled_job": "automation.manage",
    "toggle_scheduled_job": "automation.manage",
    "run_scheduled_job_now": "automation.manage",
    "bash": "cli.execute",
    "code.run": "cli.execute",
    "code.path_check": "cli.execute",
}
_SOCIAL_PROVIDER_MARKERS = (
    "twitter",
    "x_",
    "x.",
    "facebook",
    "instagram",
    "linkedin",
    "xiaohongshu",
    "rednote",
    "tiktok",
    "youtube",
)
_SOCIAL_MUTATION_MARKERS = (
    "publish",
    "post",
    "draft",
    "schedule",
    "comment",
    "reply",
    "like",
    "follow",
    "delete",
    "update",
    "create",
    "send",
)
_EMAIL_PROVIDER_MARKERS = ("gmail", "outlook", "email", "imap", "smtp", "mail")
_EMAIL_MUTATION_MARKERS = ("send", "delete", "draft", "update", "create", "reply", "forward")
_MESSAGE_PROVIDER_MARKERS = (
    "whatsapp",
    "telegram",
    "slack",
    "discord",
    "sms",
    "twilio",
    "wechat",
    "line",
    "messenger",
)
_MESSAGE_MUTATION_MARKERS = ("send", "reply", "message", "dm", "delete")


@dataclass(frozen=True)
class RuntimeToolBlockEvent:
    type: RuntimeEventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeApprovalRequest:
    tool_name: str
    arguments: dict[str, Any]
    entity_id: str
    user_id: str
    workspace_id: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    envelope: RuntimeEnvelope | None = None


@dataclass(frozen=True)
class RuntimeApprovalAction:
    kind: str
    action_key: str
    risk_level: str
    title: str
    resource_kind: str | None = None
    operation: str | None = None
    resource_id: str | None = None
    capability_id: str | None = None

    def __post_init__(self) -> None:
        if self.capability_id:
            return
        object.__setattr__(
            self,
            "capability_id",
            runtime_capability_id_for_action_key(
                self.action_key,
                resource_kind=self.resource_kind,
            ),
        )


def runtime_capability_id_for_action_key(
    action_key: str | None,
    *,
    provider: str | None = None,
    resource_kind: str | None = None,
) -> str | None:
    """Map approval action keys to Runtime BusinessCapability ids."""
    key = str(action_key or "").strip()
    provider_key = str(provider or "").strip().lower()
    resource = str(resource_kind or "").strip().lower()
    if not key:
        return None
    if key in _ACTION_KEY_CAPABILITY_ALIASES:
        return _ACTION_KEY_CAPABILITY_ALIASES[key]
    if key.startswith("workspace.task."):
        return "workspace.task"
    if key.startswith("workspace.knowledge."):
        return "workspace.knowledge"
    if key.startswith(("workspace.rule.", "workspace.strategist.", "workspace.operation.")):
        return "workspace.governance"
    if key.startswith("workspace.file."):
        return "file.write"
    if key.startswith("workspace.automation."):
        return "automation.manage"
    if key.startswith("sandbox."):
        return "sandbox.execute"
    if key.startswith(("social_post.", "social.")):
        return "external.social"
    if key.startswith("email."):
        return "external.email"
    if key.startswith("external_message.") or key == "channel.reply":
        return "external.message"
    if key == "cli.exec":
        return "cli.execute"
    if provider_key in {"chrome", "browser_mcp", "local_browser", "browser"}:
        return "manor.composite"
    if resource in {"file", "workspace_file"}:
        return "file.write"
    if resource == "sandbox_file":
        return "sandbox.execute"
    if key.startswith("workspace.") or resource in {
        "client",
        "order",
        "skill",
        "workspace",
        "workspace_task",
        "workspace_operation",
    }:
        return "manor.composite"
    lower_key = key.lower()
    if provider_key:
        if any(marker in provider_key for marker in _SOCIAL_PROVIDER_MARKERS) and any(
            marker in lower_key for marker in _SOCIAL_MUTATION_MARKERS
        ):
            return "external.social"
        if any(marker in provider_key for marker in _EMAIL_PROVIDER_MARKERS) and any(
            marker in lower_key for marker in _EMAIL_MUTATION_MARKERS
        ):
            return "external.email"
        if any(marker in provider_key for marker in _MESSAGE_PROVIDER_MARKERS) and any(
            marker in lower_key for marker in _MESSAGE_MUTATION_MARKERS
        ):
            return "external.message"
    return None


def runtime_requires_baseline_approval(action: RuntimeApprovalAction) -> bool:
    """Baseline safety for direct/non-workspace runtime tool calls."""
    return direct_chat_default_approval_mode(
        action_key=action.action_key,
        capability_id=action.capability_id,
        operation=action.operation,
        risk_level=action.risk_level,
    ) == "approval"


@dataclass(frozen=True)
class RuntimeApprovalDecision:
    allowed: bool
    blocked_result: str | None = None
    event: RuntimeToolBlockEvent | None = None

    @classmethod
    def allow(cls) -> "RuntimeApprovalDecision":
        return cls(allowed=True)

    @classmethod
    def block(cls, tool_name: str, blocked_result: str) -> "RuntimeApprovalDecision":
        return cls(
            allowed=False,
            blocked_result=blocked_result,
            event=runtime_event_from_tool_block_result(tool_name, blocked_result),
        )


class RuntimeApprovalPolicyAdapter(Protocol):
    def classify_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> RuntimeApprovalAction | None:
        ...

    async def guard_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> str | None:
        ...


@dataclass(frozen=True)
class LegacyWorkspaceApprovalPolicyAdapter:
    """Compatibility adapter for the runtime approval governance service."""

    def classify_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> RuntimeApprovalAction | None:
        from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action

        action = classify_runtime_tool_action(
            request.tool_name,
            request.arguments,
            entity_id=request.entity_id,
        )
        return action

    async def guard_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> str | None:
        from packages.core.ai.runtime.approval_service import guard_runtime_tool_action

        return await guard_runtime_tool_action(
            name=request.tool_name,
            arguments=request.arguments,
            entity_id=request.entity_id,
            user_id=request.user_id,
            workspace_id=request.workspace_id,
            conversation_id=request.conversation_id,
            task_id=request.task_id,
        )


@dataclass(frozen=True)
class RuntimeApprovalMiddleware:
    """Runtime middleware facade for approval-related execution events."""

    name: str = "approval"
    policy_adapter: RuntimeApprovalPolicyAdapter = field(
        default_factory=LegacyWorkspaceApprovalPolicyAdapter,
    )

    def apply_sync(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        return envelope

    async def guard_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> RuntimeApprovalDecision:
        """Run approval policy and return a structured runtime decision."""
        blocked = await self.policy_adapter.guard_request(request)
        if blocked:
            return RuntimeApprovalDecision.block(request.tool_name, blocked)
        return RuntimeApprovalDecision.allow()

    def classify_request(
        self,
        request: RuntimeApprovalRequest,
    ) -> RuntimeApprovalAction | None:
        return self.policy_adapter.classify_request(request)

    async def guard_tool_action(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        entity_id: str,
        user_id: str,
        workspace_id: str | None,
        conversation_id: str | None,
        task_id: str | None = None,
    ) -> str | None:
        """Run the Manor runtime approval gate for a concrete tool call.

        This delegates to the existing workspace governance implementation
        while making ApprovalMiddleware the stable runtime boundary.
        """
        decision = await self.guard_request(RuntimeApprovalRequest(
            tool_name=tool_name,
            arguments=arguments,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
        ))
        return decision.blocked_result

    def tool_block_event(
        self,
        tool_name: str,
        result: str | dict[str, Any],
    ) -> RuntimeToolBlockEvent | None:
        return runtime_event_from_tool_block_result(tool_name, result)


def _payload_from_tool_result(result: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.strip():
        try:
            parsed = json.loads(result)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def runtime_event_from_tool_block_result(
    tool_name: str,
    result: str | dict[str, Any],
) -> RuntimeToolBlockEvent | None:
    """Translate a blocking tool payload into a standard runtime event."""
    payload = _payload_from_tool_result(result)
    error = str(payload.get("error") or "").strip()
    if error == "approval_required":
        hitl = payload.get("hitl") if isinstance(payload.get("hitl"), dict) else {}
        operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else {}
        data = {
            "tool_name": tool_name,
            "approval_token": payload.get("approval_token") or hitl.get("id"),
            "action_key": payload.get("action_key") or hitl.get("action") or operation.get("action_key"),
            "matched_rule": payload.get("matched_rule") or operation.get("matched_rule"),
        }
        capability_id = (
            payload.get("capability_id")
            or hitl.get("capability_id")
            or operation.get("capability_id")
        )
        if capability_id:
            data["capability_id"] = capability_id
        return RuntimeToolBlockEvent(
            type="approval_required",
            data=data,
        )
    if error:
        return RuntimeToolBlockEvent(
            type="tool_denied",
            data={
                "tool_name": tool_name,
                "code": error,
                "reason": payload.get("message"),
            },
        )
    return None
