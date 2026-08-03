"""Provider approval adapters and provider-neutral continuation metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


_PROVIDER_RETRY_ARGUMENTS_MAX_CHARS = 16_000
_TOOL_CONTINUATION_KEY = "__manor_tool_continuation"


@dataclass
class ProviderApprovalCollector:
    """Collect normalized provider approvals without leaking them to UI events."""

    requests: list[dict[str, Any]] = field(default_factory=list)

    def _append(self, request: dict[str, Any] | None) -> None:
        if request is None:
            return
        identity = (
            request.get("provider"),
            request.get("provider_approval_id"),
        )
        if any(
            (item.get("provider"), item.get("provider_approval_id")) == identity
            for item in self.requests
        ):
            return
        self.requests.append(request)

    def capture(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        result: str | dict[str, Any],
    ) -> None:
        self._append(normalize_provider_approval(tool_name, arguments, result))

    def capture_recorded_tool_call(self, tool_call: Any) -> None:
        if not isinstance(tool_call, dict):
            return
        request = tool_call.get("provider_approval")
        self._append(request if isinstance(request, dict) else None)

    def pending_request(self) -> dict[str, Any] | None:
        return self.requests[0] if self.requests else None


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _retry_arguments(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return {}
    arguments = {
        str(key): item
        for key, item in value.items()
        if str(key)
        not in {
            "approvalToken",
            "approval_token",
            _TOOL_CONTINUATION_KEY,
        }
    }
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    if len(encoded) > _PROVIDER_RETRY_ARGUMENTS_MAX_CHARS:
        return None
    return json.loads(encoded)


def _normalize_chrome_approval(
    tool_name: str,
    arguments: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        not tool_name.startswith("mcp__chrome__")
        or tool_name == "mcp__chrome__confirm_action"
    ):
        return None
    status = str(payload.get("status") or "").strip()
    if not (
        payload.get("approval_required") is True
        or status == "approval_required"
    ):
        return None
    provider = str(payload.get("provider") or "chrome").strip().lower()
    if provider != "chrome":
        return None
    approval_id = str(
        payload.get("approvalId")
        or payload.get("approval_id")
        or payload.get("approval_token")
        or ""
    ).strip()
    if not approval_id:
        return None

    retry_action = (
        payload.get("retry_action")
        if isinstance(payload.get("retry_action"), dict)
        else {}
    )
    retry_tool = str(retry_action.get("name") or tool_name).strip()
    if retry_tool != tool_name:
        return None
    raw_retry_arguments = (
        retry_action.get("arguments")
        if isinstance(retry_action.get("arguments"), dict)
        else arguments
    )
    retry_arguments = _retry_arguments(raw_retry_arguments)
    if retry_arguments is None:
        return None

    request = {
        "version": 1,
        "provider": "chrome",
        "provider_approval_id": approval_id,
        "confirmation_tool": "mcp__chrome__confirm_action",
        "confirmation_arguments": {"approvalId": approval_id},
        "retry_tool": retry_tool,
        "retry_arguments": retry_arguments,
        "recovery_tool_names": ["mcp__chrome__read_page"],
        "expires_at": str(
            payload.get("expires_at") or payload.get("expiresAt") or ""
        ).strip()
        or None,
        "action_key": str(payload.get("action_key") or "chrome.action").strip(),
        "reason": str(
            payload.get("reason") or payload.get("matched_rule") or ""
        ).strip()
        or None,
        "target_label": str(payload.get("target_label") or "").strip() or None,
        "target_role": str(payload.get("target_role") or "").strip() or None,
        "url": str(payload.get("url") or "").strip() or None,
        "data_summary": str(payload.get("data_summary") or "").strip() or None,
    }
    for source_key, target_key in (
        ("next_required_tool", "next_required_tool"),
        ("groupId", "groupId"),
        ("group_id", "groupId"),
        ("tabId", "tabId"),
        ("tab_id", "tabId"),
        ("ref", "ref"),
        ("node_id", "ref"),
        ("selector", "selector"),
        ("snapshot_id", "snapshot_id"),
    ):
        if payload.get(source_key) is not None:
            request[target_key] = payload.get(source_key)
    return request


_PROVIDER_ADAPTERS: tuple[
    Callable[[str, dict[str, Any] | None, dict[str, Any]], dict[str, Any] | None],
    ...
] = (_normalize_chrome_approval,)


def normalize_provider_approval(
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: str | dict[str, Any],
) -> dict[str, Any] | None:
    """Normalize a provider-native approval result into a durable request."""

    payload = _json_payload(result)
    if not payload:
        return None
    for adapter in _PROVIDER_ADAPTERS:
        request = adapter(str(tool_name or "").strip(), arguments, payload)
        if request is not None:
            return request
    return None


def provider_approval_is_expired(request: dict[str, Any]) -> bool:
    value = str(request.get("expires_at") or "").strip()
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def provider_approval_runtime_metadata(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Build deterministic execution metadata from a stored provider request."""

    continuation = item.get("continuation")
    if not isinstance(continuation, dict) or provider_approval_is_expired(
        continuation
    ):
        return None
    confirmation_tool = str(continuation.get("confirmation_tool") or "").strip()
    retry_tool = str(continuation.get("retry_tool") or "").strip()
    confirmation_arguments = continuation.get("confirmation_arguments")
    retry_arguments = continuation.get("retry_arguments")
    if (
        not confirmation_tool
        or not retry_tool
        or not isinstance(confirmation_arguments, dict)
        or not isinstance(retry_arguments, dict)
    ):
        return None

    arguments = dict(confirmation_arguments)
    arguments[_TOOL_CONTINUATION_KEY] = {
        "kind": "retry_with_result_token",
        "tool": retry_tool,
        "arguments": dict(retry_arguments),
        "required_status": "approved",
        "result_token_keys": ["approvalToken", "approval_token"],
        "argument_token_key": "approvalToken",
    }
    recovery_tool_names = continuation.get("recovery_tool_names")
    extra_tool_names = {confirmation_tool, retry_tool}
    if isinstance(recovery_tool_names, list):
        extra_tool_names.update(
            tool_name.strip()
            for tool_name in recovery_tool_names
            if isinstance(tool_name, str) and tool_name.strip()
        )
    return {
        "extra_tool_names": sorted(extra_tool_names),
        "forced_tool_calls": [
            {
                "name": confirmation_tool,
                "arguments": arguments,
            }
        ],
        "approval_resume_guidance": (
            "Resume the approved provider action using the supplied forced "
            "tool continuation. Do not rediscover or alter the approved action."
        ),
    }


def provider_approval_runtime_event_data(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Project a normalized request into the public runtime event payload."""

    approval_id = request.get("provider_approval_id")
    data = {
        "tool_name": request.get("retry_tool"),
        "approval_token": approval_id,
        "approval_id": approval_id,
        "action_key": request.get("action_key"),
        "matched_rule": request.get("reason"),
        "provider": request.get("provider"),
    }
    for key in (
        "next_required_tool",
        "groupId",
        "tabId",
        "ref",
        "selector",
        "snapshot_id",
        "target_label",
        "target_role",
        "data_summary",
        "url",
    ):
        if request.get(key) is not None:
            data[key] = request.get(key)
    return data
