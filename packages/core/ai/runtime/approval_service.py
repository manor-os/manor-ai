"""Runtime-owned approval governance service.

This module is the stable runtime boundary for approval-gated tool execution.
Legacy service imports should delegate here so AI entrypoints depend on the
Runtime Harness instead of patching workspace governance directly.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
from packages.core.ai.runtime.approval_messages import (
    approval_args_hash,
    approval_content_preview,
    approval_paths,
    approval_preview_arguments,
    approval_public_content,
    runtime_approval_prompt,
    runtime_approval_rejected_message,
    runtime_approval_retry_message,
)
from packages.core.ai.runtime.approval_preferences import (
    runtime_approval_preference_mode,
    set_runtime_approval_preference,
)
from packages.core.ai.runtime.approval_store import (
    load_runtime_approval_conversation,
    mark_runtime_hitl_request_resolved,
    mark_runtime_hitl_requests_resolved,
    runtime_approval_now_iso,
    runtime_approval_workspace_context,
    runtime_approvals,
    set_runtime_approvals,
)
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
    approval_options,
    normalize_approval_choice,
)
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalAction,
    runtime_requires_baseline_approval,
)

APPROVAL_TOKEN_IGNORED = "__runtime_approval_token_ignored__"


def _boolish_confirmation_control(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
            "y",
            "n",
            "on",
            "off",
        }
    return False


def _approval_args_hash_candidates(arguments: dict[str, Any]) -> tuple[str, ...]:
    """Hashes accepted for an approval retry.

    `confirm` is a tool/runtime control flag on several external-action
    tools. The user-facing payload being approved is the target content, not
    that boolean switch, so a retry that only adds `confirm=true` must still
    consume the original approval token. Non-boolean `confirm` values remain
    payload-bearing and are not ignored.
    """

    hashes = [approval_args_hash(arguments)]
    if "confirm" in arguments and _boolish_confirmation_control(arguments.get("confirm")):
        without_confirm = dict(arguments)
        without_confirm.pop("confirm", None)
        stripped_hash = approval_args_hash(without_confirm)
        if stripped_hash not in hashes:
            hashes.append(stripped_hash)
    return tuple(hashes)


async def guard_runtime_tool_action(
    *,
    name: str,
    arguments: dict[str, Any],
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    conversation_id: str | None,
    task_id: str | None = None,
) -> str | None:
    """Return a blocking tool result, or ``None`` when execution may continue."""
    action = classify_runtime_tool_action(
        name,
        arguments,
        entity_id=entity_id,
    )
    if action is None:
        return None

    approval_token = str(arguments.get("approval_token") or "").strip()
    baseline_required = (not workspace_id) and runtime_requires_baseline_approval(action)

    from packages.core.database import async_session

    async with async_session() as db:
        if approval_token:
            result = await consume_runtime_approval(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                hitl_id=approval_token,
                tool_name=name,
                arguments=arguments,
                action=action,
            )
            await db.commit()
            if result is None:
                arguments.pop("approval_token", None)
                return None
            if result == APPROVAL_TOKEN_IGNORED:
                arguments.pop("approval_token", None)
                approval_token = ""
            else:
                return result

        if not workspace_id:
            preference = await runtime_approval_preference_mode(
                db,
                user_id=user_id,
                action_key=action.action_key,
                capability_id=action.capability_id,
            )
            if preference == "deny":
                return json.dumps({
                    "error": "blocked_by_user_policy",
                    "message": "This action is blocked by your approval preferences.",
                    "action_key": action.action_key,
                    "capability_id": action.capability_id,
                    "tool": name,
                }, ensure_ascii=False)
            if preference == "always_approve":
                return None
            if baseline_required:
                if not conversation_id:
                    return json.dumps({
                        "error": "approval_required",
                        "message": "This action requires approval, but there is no conversation context to request it.",
                        "action_key": action.action_key,
                        "capability_id": action.capability_id,
                        "tool": name,
                    }, ensure_ascii=False)
                payload = await create_runtime_approval(
                    db,
                    conversation_id=conversation_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    tool_name=name,
                    arguments=arguments,
                    action=action,
                    reason="Direct chat safety requires approval for destructive, publishing, sending, or automation actions.",
                    matched_rule="direct_chat_baseline",
                )
                await db.commit()
                return payload
            return None

        from packages.core.budget import get_workspace_spent_credits_per_kind
        from packages.core.governance import check_step_policy

        spent_credits_per_kind = await get_workspace_spent_credits_per_kind(db, workspace_id)

        decision = await check_step_policy(
            db,
            workspace_id=workspace_id,
            kind=action.kind,
            action_key=action.action_key,
            risk_level=action.risk_level,
            capability_id=action.capability_id,
            spent_credits_per_kind=spent_credits_per_kind,
            task_id=task_id,
        )
        if decision.allowed:
            return None

        if decision.pause_for_hitl:
            if not conversation_id:
                return json.dumps({
                    "error": "approval_required",
                    "message": "Workspace governance requires approval, but this tool call has no conversation context to request it.",
                    "action_key": action.action_key,
                    "capability_id": action.capability_id,
                    "matched_rule": decision.matched_rule,
                }, ensure_ascii=False)
            payload = await create_runtime_approval(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                tool_name=name,
                arguments=arguments,
                action=action,
                reason=decision.reason,
                matched_rule=decision.matched_rule,
            )
            await db.commit()
            return payload

        return json.dumps({
            "error": "blocked_by_governance",
            "message": decision.reason or "Workspace governance blocked this action.",
            "action_key": action.action_key,
            "capability_id": action.capability_id,
            "matched_rule": decision.matched_rule,
            "tool": name,
        }, ensure_ascii=False)


async def resolve_runtime_approval_message(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
) -> str | None:
    """Resolve an approval-card click for runtime tool approvals."""
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return None
    approvals = runtime_approvals(conv)
    item = approvals.get(hitl_id)
    if not item:
        return None
    if item.get("status") != "pending":
        return f"Runtime approval {hitl_id} is already {item.get('status')}. Do not retry the blocked tool call."

    normalized = normalize_approval_choice(action)
    now = runtime_approval_now_iso()
    if normalized in {APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_ALWAYS_APPROVE}:
        if normalized == APPROVAL_CHOICE_ALWAYS_APPROVE:
            await set_runtime_approval_preference(
                db,
                user_id=user_id,
                mode="always_approve",
                action_key=item.get("action_key"),
                capability_id=item.get("capability_id"),
                workspace_id=None,
            )
        item.update({"status": "approved", "approved_by": user_id, "approved_at": now})
        approvals[hitl_id] = item
        set_runtime_approvals(conv, approvals)
        await mark_runtime_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=normalized,
        )
        return runtime_approval_retry_message(item, hitl_id)
    if normalized == APPROVAL_CHOICE_REJECT:
        item.update({"status": "rejected", "rejected_by": user_id, "rejected_at": now})
        approvals[hitl_id] = item
        set_runtime_approvals(conv, approvals)
        await mark_runtime_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=normalized,
        )
        return runtime_approval_rejected_message(item)
    return None


async def cancel_pending_runtime_approvals(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str | None,
    hitl_ids: Iterable[str] | None = None,
    reason: str = "request_stopped",
) -> int:
    """Permanently close pending runtime approvals for a stopped request."""
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return 0
    approvals = runtime_approvals(conv)
    if not approvals:
        return 0

    wanted_ids = {
        str(item or "").strip()
        for item in (hitl_ids or [])
        if str(item or "").strip()
    }
    cancelled = 0
    cancelled_ids: list[str] = []
    now = runtime_approval_now_iso()
    for hitl_id, item in list(approvals.items()):
        if not isinstance(item, dict):
            continue
        if wanted_ids and hitl_id not in wanted_ids:
            continue
        if item.get("status") != "pending":
            continue
        requested_by = item.get("requested_by")
        if user_id and requested_by and requested_by != user_id:
            continue
        item.update({
            "status": "cancelled",
            "cancelled_by": user_id,
            "cancelled_at": now,
            "cancel_reason": reason,
        })
        approvals[hitl_id] = item
        cancelled += 1
        cancelled_ids.append(hitl_id)

    if not cancelled:
        return 0
    set_runtime_approvals(conv, approvals)
    await mark_runtime_hitl_requests_resolved(
        db,
        conversation_id=conversation_id,
        hitl_ids=cancelled_ids,
        choice="cancelled",
    )
    await db.flush()
    return cancelled


async def resolve_pending_runtime_approval_from_reply(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    message: str,
) -> str | None:
    """Resolve short yes/no replies when the UI card is not used."""
    try:
        from packages.core.services.ai_file_permissions import classify_file_approval_reply
    except Exception:
        classify_file_approval_reply = None
    choice = classify_file_approval_reply(message) if classify_file_approval_reply else None
    if choice not in {"approve", "always_approve", "reject"}:
        return None
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return None
    pending = [
        (hitl_id, item)
        for hitl_id, item in runtime_approvals(conv).items()
        if item.get("status") == "pending"
    ]
    if len(pending) != 1:
        return None
    hitl_id, _item = pending[0]
    return await resolve_runtime_approval_message(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action=choice,
    )


async def create_runtime_approval(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
    reason: str | None,
    matched_rule: str | None,
) -> str:
    from packages.core.models.base import generate_ulid

    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return json.dumps({"error": "conversation_not_found"})
    hitl_id = generate_ulid()
    approvals = runtime_approvals(conv)
    prompt = runtime_approval_prompt(action, tool_name, arguments)
    args_preview = approval_preview_arguments(arguments)
    paths = approval_paths(action, tool_name, arguments)
    workspace = await runtime_approval_workspace_context(db, conv)
    public_content = approval_public_content(args_preview)
    content_preview = approval_content_preview(public_content) if public_content else ""
    approvals[hitl_id] = {
        "id": hitl_id,
        "status": "pending",
        "tool": tool_name,
        "action_key": action.action_key,
        "capability_id": action.capability_id,
        "risk_level": action.risk_level,
        "args_hash": approval_args_hash(arguments),
        "args_preview": args_preview,
        "retry_args": args_preview if not args_preview.get("truncated") else None,
        "paths": paths,
        "workspace": workspace,
        "content": content_preview,
        "reason": reason,
        "matched_rule": matched_rule,
        "requested_by": user_id,
        "created_at": runtime_approval_now_iso(),
    }
    set_runtime_approvals(conv, approvals)
    return json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": hitl_id,
        "hitl": {
            "id": hitl_id,
            "type": "approval",
            "prompt": prompt,
            "action": action.action_key,
            "capability_id": action.capability_id,
            "tool": tool_name,
            "workspace": workspace,
            "paths": paths,
            "content": content_preview,
            "args_preview": args_preview,
            "options": approval_options(),
        },
        "message": (
            "Workspace governance requires approval before this action. "
            "Do not retry until the user approves. If approved, retry the same tool "
            f"call with approval_token='{hitl_id}'."
        ),
        "operation": {
            "tool": tool_name,
            "action_key": action.action_key,
            "capability_id": action.capability_id,
            "risk_level": action.risk_level,
            "matched_rule": matched_rule,
            "args_preview": approval_preview_arguments(arguments),
            "paths": paths,
            "workspace": workspace,
        },
    }, ensure_ascii=False)


async def consume_runtime_approval(
    db,
    *,
    conversation_id: str | None,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
) -> str | None:
    if not conversation_id:
        return json.dumps({"error": "approval_token_requires_conversation"})
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return json.dumps({"error": "conversation_not_found"})
    approvals = runtime_approvals(conv)
    item = approvals.get(hitl_id)
    if not item:
        fallback_id, fallback_item = find_approved_runtime_approval_by_payload(
            approvals,
            tool_name=tool_name,
            action_key=action.action_key,
            args_hashes=_approval_args_hash_candidates(arguments),
        )
        if not fallback_item:
            return APPROVAL_TOKEN_IGNORED
        hitl_id = fallback_id
        item = fallback_item
    if item.get("status") != "approved":
        return json.dumps({"error": "approval_not_granted", "status": item.get("status"), "approval_token": hitl_id})
    if item.get("tool") != tool_name or item.get("action_key") != action.action_key:
        return APPROVAL_TOKEN_IGNORED
    if item.get("args_hash") not in _approval_args_hash_candidates(arguments):
        item.update({
            "status": "superseded",
            "superseded_by": user_id,
            "superseded_at": runtime_approval_now_iso(),
            "superseded_reason": "payload_changed_after_approval",
        })
        approvals[hitl_id] = item
        set_runtime_approvals(conv, approvals)
        return await create_runtime_approval(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            action=action,
            reason="The requested operation changed after approval. Please approve the updated content.",
            matched_rule=item.get("matched_rule") or "approval_payload_changed",
        )
    item.update({"status": "consumed", "consumed_by": user_id, "consumed_at": runtime_approval_now_iso()})
    approvals[hitl_id] = item
    set_runtime_approvals(conv, approvals)
    return None


def find_approved_runtime_approval_by_payload(
    approvals: dict[str, dict[str, Any]],
    *,
    tool_name: str,
    action_key: str,
    args_hashes: Iterable[str],
) -> tuple[str, dict[str, Any] | None]:
    accepted_hashes = set(args_hashes)
    matches = [
        (hitl_id, item)
        for hitl_id, item in approvals.items()
        if isinstance(item, dict)
        and item.get("status") == "approved"
        and item.get("tool") == tool_name
        and item.get("action_key") == action_key
        and item.get("args_hash") in accepted_hashes
    ]
    if len(matches) == 1:
        return matches[0]
    return "", None
