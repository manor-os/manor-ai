"""Provider approval adapters and provider-neutral continuation metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from packages.core.services.sensitive_data import sanitize_approval_credentials


_PROVIDER_RETRY_ARGUMENTS_MAX_BYTES = 16_000
_PROVIDER_CONTINUATION_MAX_BYTES = 64_000
_TOOL_CONTINUATION_KEY = "__manor_tool_continuation"
_CHROME_CONFIRMATION_MODES = {
    "always_action_time",
    "preapproval_allowed",
    "handoff_required",
    "no_confirmation",
}
_PROVIDER_TOOL_CONTRACTS = {
    "chrome": ("mcp__chrome__confirm_action", "mcp__chrome__"),
}


def provider_tool_result_for_persistence(tool_name: str, result: str) -> str:
    """Keep provider replay output while removing one-time action credentials."""

    normalized_name = str(tool_name or "").strip()
    if not any(
        normalized_name == confirmation_tool or normalized_name.startswith(tool_prefix)
        for confirmation_tool, tool_prefix in _PROVIDER_TOOL_CONTRACTS.values()
    ):
        return result
    sanitized = sanitize_approval_credentials(result)
    return sanitized if isinstance(sanitized, str) else result


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

    def _resolve(self, resolution: dict[str, Any] | None) -> None:
        if not isinstance(resolution, dict):
            return
        identity = (
            resolution.get("provider"),
            resolution.get("provider_approval_id"),
        )
        self.requests = [
            item
            for item in self.requests
            if (item.get("provider"), item.get("provider_approval_id")) != identity
        ]

    def capture(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        result: str | dict[str, Any],
    ) -> None:
        self._resolve(normalize_provider_approval_resolution(tool_name, arguments, result))
        self._append(normalize_provider_approval(tool_name, arguments, result))

    def capture_recorded_tool_call(self, tool_call: Any) -> None:
        if not isinstance(tool_call, dict):
            return
        resolution = tool_call.get("provider_approval_resolution")
        self._resolve(resolution if isinstance(resolution, dict) else None)
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
        return None
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
    if len(encoded.encode("utf-8")) > _PROVIDER_RETRY_ARGUMENTS_MAX_BYTES:
        return None
    return json.loads(encoded)


def freeze_provider_approval_request(
    request: Any,
) -> dict[str, Any] | None:
    """Freeze one provider gate for deterministic post-approval replay.

    Provider-native approvals use a two-call continuation (confirm, then retry
    with the returned provider token), but follow the same persistence contract
    as ordinary runtime tools: exact JSON types, a bounded payload, and no grant
    when a legacy continuation is incomplete.
    """

    if not isinstance(request, dict):
        return None
    try:
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > _PROVIDER_CONTINUATION_MAX_BYTES:
        return None
    frozen = json.loads(encoded)
    if not isinstance(frozen, dict):
        return None
    provider = str(frozen.get("provider") or "").strip()
    if not provider:
        return None
    provider_approval_id = str(frozen.get("provider_approval_id") or "").strip()
    if not provider_approval_id:
        return None
    confirmation_tool = str(frozen.get("confirmation_tool") or "").strip()
    if not confirmation_tool:
        return None
    confirmation_arguments = frozen.get("confirmation_arguments")
    if not isinstance(confirmation_arguments, dict):
        return None
    retry_tool = str(frozen.get("retry_tool") or "").strip()
    if not retry_tool:
        return None
    retry_arguments = frozen.get("retry_arguments")
    if not isinstance(retry_arguments, dict):
        return None
    tool_contract = _PROVIDER_TOOL_CONTRACTS.get(provider)
    if tool_contract is None:
        return None
    expected_confirmation_tool, tool_prefix = tool_contract
    if confirmation_tool != expected_confirmation_tool:
        return None
    if not retry_tool.startswith(tool_prefix) or retry_tool == confirmation_tool:
        return None
    confirmation_approval_id = str(
        confirmation_arguments.get("approvalId")
        or confirmation_arguments.get("approval_id")
        or ""
    ).strip()
    if confirmation_approval_id != provider_approval_id:
        return None
    if any(
        key in retry_arguments
        for key in ("approvalToken", "approval_token", _TOOL_CONTINUATION_KEY)
    ):
        return None
    recovery_tool_names = frozen.get("recovery_tool_names")
    if recovery_tool_names is not None and (
        not isinstance(recovery_tool_names, list)
        or any(
            not isinstance(tool_name, str)
            or not tool_name.strip().startswith(tool_prefix)
            for tool_name in recovery_tool_names
        )
    ):
        return None
    return frozen


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
    for source_key, target_key in (
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
            retry_arguments[target_key] = payload.get(source_key)

    confirmation_arguments: dict[str, Any] = {"approvalId": approval_id}
    confirmation_mode = str(payload.get("confirmation_mode") or "").strip()
    policy_category = str(payload.get("policy_category") or "").strip()
    if confirmation_mode in _CHROME_CONFIRMATION_MODES and policy_category:
        confirmation_arguments.update({
            "confirmation_mode": confirmation_mode,
            "policy_category": policy_category,
            "preapproved": payload.get("preapproved") is True,
        })

    request = {
        "version": 1,
        "provider": "chrome",
        "provider_approval_id": approval_id,
        "confirmation_tool": "mcp__chrome__confirm_action",
        "confirmation_arguments": confirmation_arguments,
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
    if confirmation_mode in _CHROME_CONFIRMATION_MODES and policy_category:
        request.update({
            "confirmation_mode": confirmation_mode,
            "policy_category": policy_category,
            "preapproved": payload.get("preapproved") is True,
        })
    resource_id = str(
        payload.get("resource_id") or payload.get("resourceId") or ""
    ).strip()
    if resource_id:
        request["resource_id"] = resource_id
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
            return freeze_provider_approval_request(request)
    return None


def provider_approval_confirmation_receipt(
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: str | dict[str, Any],
) -> dict[str, Any] | None:
    """Return the private receipt needed to resume a confirmed provider gate.

    The approval token is intentionally kept out of
    :func:`normalize_provider_approval_resolution`, whose value is copied into
    public tool-call history.  Runtime approval state may persist this private
    receipt so a worker restart does not confirm the same provider request
    again or lose the exact one-time retry token.
    """

    if tool_name != "mcp__chrome__confirm_action":
        return None
    payload = _json_payload(result)
    if payload.get("ok") is not True or str(payload.get("status") or "").strip() != "approved":
        return None
    approval_id = str(
        payload.get("approvalId")
        or payload.get("approval_id")
        or (arguments or {}).get("approvalId")
        or (arguments or {}).get("approval_id")
        or ""
    ).strip()
    approval_token = str(
        payload.get("approvalToken") or payload.get("approval_token") or ""
    ).strip()
    if not approval_id or not approval_token:
        return None
    return {
        "provider": "chrome",
        "provider_approval_id": approval_id,
        "approval_token": approval_token,
    }


def normalize_provider_approval_resolution(
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: str | dict[str, Any],
) -> dict[str, Any] | None:
    receipt = provider_approval_confirmation_receipt(tool_name, arguments, result)
    if receipt is None:
        return None
    return {
        "provider": receipt["provider"],
        "provider_approval_id": receipt["provider_approval_id"],
    }


def provider_approval_is_expired(request: Any) -> bool:
    if not isinstance(request, dict):
        return False
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

    continuation = freeze_provider_approval_request(item.get("continuation"))
    if continuation is None or provider_approval_is_expired(continuation):
        return None
    item_provider = str(item.get("provider") or "").strip()
    item_approval_id = str(item.get("provider_approval_id") or "").strip()
    item_tool = str(item.get("tool") or "").strip()
    if not item_provider or not item_approval_id or not item_tool:
        return None
    if item_provider and str(continuation.get("provider") or "").strip() != item_provider:
        return None
    if (
        item_approval_id
        and str(continuation.get("provider_approval_id") or "").strip()
        != item_approval_id
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
    if item_tool and retry_tool != item_tool:
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
                # Confirm + provider-token retry form one approved attempt.
                # After both calls, the model may summarize but cannot invent
                # a second side effect or another approval card.
                "disable_followup_tools": True,
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
        "resource_id": request.get("resource_id"),
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
