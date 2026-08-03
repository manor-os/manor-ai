"""Runtime-owned approval governance service.

This module is the stable runtime boundary for approval-gated tool execution.
Legacy service imports should delegate here so AI entrypoints depend on the
Runtime Harness instead of patching workspace governance directly.

Since the unified-approval rewrite, the ONLY store for runtime tool approvals
is the ``HitlRequest`` table shared with the dispatcher step gate —
``resolve_approval`` makes the decision, the approval_token IS the request id,
and "Always approve" in a workspace conversation writes the workspace policy
auto-approve set that BOTH planes honor. Provider approvals live there too,
carrying their continuation in ``request.context``.

The legacy conversation-meta blob (``conv.meta.runtime_approvals``) is no
longer read or written for decisions; a click on a card minted before the
upgrade gets a tombstone reply telling the user to retry the action, which
mints a fresh unified request.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
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
from packages.core.ai.runtime.provider_approvals import (
    provider_approval_is_expired,
    provider_approval_runtime_metadata,
)

logger = logging.getLogger(__name__)

APPROVAL_TOKEN_IGNORED = "__runtime_approval_token_ignored__"

_RUNTIME_ORIGIN_KIND = ApprovalOriginKind.TOOL_CALL.value

async def _set_direct_chat_always_approve_preference(db, *, req, user_id: str) -> None:
    """Write direct chat's standing "Always".

    Direct chat has no workspace policy, so its standing store is the user
    preference, which ``grant_approval(standing=True)`` never touches. Same
    click, same answer, whichever surface the user is on.
    """
    await set_runtime_approval_preference(
        db,
        user_id=user_id,
        mode="always_approve",
        action_key=req.action_key,
        capability_id=req.capability_id,
    )


def _provider_supports_always_approve(provider: str | None) -> bool:
    """Can a standing "Always approve" be honored for this provider?

    Yes for every provider we can normalize. Whether the gate belongs to
    Manor's own Chrome extension or to a third party changes nothing: the
    authority being exercised is the operator's, and a normalized provider
    approval always carries a machine-answerable continuation
    (``confirmation_tool`` + ``retry_tool``), so Manor can answer the gate on
    their behalf. The provider keeps demanding per action — it just stops
    being the operator's problem.

    The grant itself is recorded per provider AND per action
    (``capability_id`` is ``"{provider}.action"``), so approving Chrome
    form-fills never authorizes, say, a LinkedIn publish.
    """
    return bool(str(provider or "").strip())


async def runtime_auto_confirm_provider_approval(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: str,
    execute: Any,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
) -> str:
    """Answer a Manor-owned provider's approval gate from a standing grant.

    The Chrome extension raises its own per-action confirmation. Approving in
    chat runs a deterministic continuation — confirm, then retry with the
    returned token injected. When the operator already chose "Always approve"
    for this action, run that same continuation right here instead of asking
    again. Two things follow: the model never receives an approval it cannot
    resolve, and it cannot improvise an unapproved second attempt (which is
    what turned one approval into an endless card loop in production).

    Returns the retried tool's result, or the original result untouched when
    anything about the fast path does not hold. It never invents permission:
    without a standing grant the normal card flow runs unchanged.
    """
    if not entity_id or not isinstance(result, str) or "approval_required" not in result:
        return result

    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval

    try:
        request = normalize_provider_approval(tool_name, arguments, result)
    except Exception:
        return result
    if not isinstance(request, dict):
        return result

    provider = str(request.get("provider") or "")
    if not _provider_supports_always_approve(provider):
        return result
    if provider_approval_is_expired(request):
        return result

    confirmation_tool = str(request.get("confirmation_tool") or "").strip()
    confirmation_arguments = request.get("confirmation_arguments")
    retry_tool = str(request.get("retry_tool") or "").strip()
    retry_arguments = request.get("retry_arguments")
    if (
        not confirmation_tool
        or not retry_tool
        or not isinstance(confirmation_arguments, dict)
        or not isinstance(retry_arguments, dict)
    ):
        return result

    action_key = str(request.get("action_key") or f"{provider}.action")
    capability_id = f"{provider}.action"

    from packages.core.database import async_session

    async with async_session() as db:
        granted = await _provider_standing_grant(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            action_key=action_key,
            capability_id=capability_id,
        )
    if not granted:
        return result

    try:
        confirmation = await execute(confirmation_tool, dict(confirmation_arguments))
        token = _provider_approval_token(confirmation)
        if not token:
            return result
        retried = await execute(
            retry_tool, {**retry_arguments, "approvalToken": token},
        )
    except Exception:
        logger.warning(
            "auto-confirm for %s failed; falling back to the approval card",
            tool_name, exc_info=True,
        )
        return result
    return retried if isinstance(retried, str) and retried else result


def _provider_approval_token(confirmation: Any) -> str | None:
    """Pull the single-use token out of a confirm_action result."""
    if not isinstance(confirmation, str):
        return None
    try:
        payload = json.loads(confirmation)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("approvalToken", "approval_token"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


async def _provider_standing_grant(
    db,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    action_key: str,
    capability_id: str,
) -> bool:
    """Did the operator already say "always" for this provider action?

    Reads the same two standing stores the rest of the runtime guard honors:
    the workspace policy auto-approve set for workspace chats, and the user
    preference for direct chat. Never fabricates permission — a hard
    never_allow still denies through the normal gate.
    """
    if workspace_id:
        from packages.core.governance.service import workspace_policy_auto_approves

        if await workspace_policy_auto_approves(
            db,
            workspace_id=workspace_id,
            action_key=action_key,
            capability_id=capability_id,
        ):
            return True
    return await runtime_approval_preference_mode(
        db,
        user_id=user_id,
        action_key=action_key,
        capability_id=capability_id,
    ) == "always_approve"


@dataclass(frozen=True)
class RuntimeApprovalResolution:
    message: str
    runtime_metadata: dict[str, Any] | None = None


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


# ── unified-store plumbing ─────────────────────────────────────────────


async def _load_runtime_request(
    db,
    *,
    request_id: str,
    entity_id: str,
    conversation_id: str | None,
):
    """Load a HitlRequest ONLY if it is a runtime tool-call request for
    THIS conversation. Step-origin requests (dispatcher cards) and other
    conversations' requests are invisible here — the chat resolver chain
    relies on returning None for foreign ids."""
    if not request_id or not conversation_id:
        return None
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    req = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == request_id,
            HitlRequest.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if req is None or req.origin_kind != _RUNTIME_ORIGIN_KIND:
        return None
    if req.origin_conversation_id != conversation_id:
        return None
    return req


def _runtime_request_item(req) -> dict[str, Any]:
    """Adapt a HitlRequest row to the legacy item-dict shape the message
    builders (retry / rejected) consume."""
    ctx = dict(req.context or {})
    return {
        "id": req.id,
        "tool": ctx.get("tool"),
        "action_key": req.action_key,
        "capability_id": req.capability_id,
        "risk_level": req.risk_level,
        "args_hash": ctx.get("args_hash"),
        "args_preview": ctx.get("args_preview"),
        "retry_args": ctx.get("retry_args"),
        "paths": ctx.get("paths"),
        "workspace": ctx.get("workspace"),
        "content": ctx.get("content"),
        "reason": req.reason,
        "matched_rule": req.matched_rule,
    }


async def _runtime_render_context(
    db,
    *,
    conversation_id: str | None,
    entity_id: str,
    user_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
) -> dict[str, Any]:
    """Everything the card and the retry message need, computed once and
    stored on the request's context at mint time."""
    args_preview = approval_preview_arguments(arguments)
    paths = approval_paths(action, tool_name, arguments)
    workspace = None
    if conversation_id:
        conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
        if conv is not None:
            workspace = await runtime_approval_workspace_context(db, conv)
    public_content = approval_public_content(args_preview)
    content_preview = approval_content_preview(public_content) if public_content else ""
    return {
        "tool": tool_name,
        "args_hash": approval_args_hash(arguments),
        "args_preview": args_preview,
        "retry_args": args_preview if not args_preview.get("truncated") else None,
        "paths": paths,
        "workspace": workspace,
        "content": content_preview,
        "requested_by": user_id,
    }


def _runtime_hitl_payload(
    *,
    request_id: str,
    action: RuntimeApprovalAction,
    tool_name: str,
    arguments: dict[str, Any],
    matched_rule: str | None,
    render: dict[str, Any],
) -> str:
    """The blocking __hitl__ tool result. Shape is a frontend contract —
    identical to the pre-rewrite payload, with approval_token = request id."""
    prompt = runtime_approval_prompt(action, tool_name, arguments)
    return json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": request_id,
        "hitl": {
            "id": request_id,
            "type": "approval",
            "prompt": prompt,
            "action": action.action_key,
            "capability_id": action.capability_id,
            "tool": tool_name,
            "workspace": render.get("workspace"),
            "paths": render.get("paths"),
            "content": render.get("content"),
            "args_preview": render.get("args_preview"),
            "options": approval_options(),
        },
        "message": (
            "Workspace governance requires approval before this action. "
            "Do not retry until the user approves. If approved, retry the same tool "
            f"call with approval_token='{request_id}'."
        ),
        "operation": {
            "tool": tool_name,
            "action_key": action.action_key,
            "capability_id": action.capability_id,
            "risk_level": action.risk_level,
            "matched_rule": matched_rule,
            "args_preview": render.get("args_preview"),
            "paths": render.get("paths"),
            "workspace": render.get("workspace"),
        },
    }, ensure_ascii=False)


async def _runtime_block_payload(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
    reason: str | None,
    matched_rule_hint: str | None,
) -> str | None:
    """Force-mint a fresh approval request and return its blocking payload.

    Used when a previously granted approval cannot cover the call (payload
    changed after approval) — the user must see the NEW content, so standing
    preferences are deliberately not consulted. Returns None when policy
    auto-approves the new payload outright."""
    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        resolve_approval,
    )

    render = await _runtime_render_context(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        tool_name=tool_name,
        arguments=arguments,
        action=action,
    )
    decision = await resolve_approval(
        db,
        subject=ApprovalSubject(
            entity_id=entity_id,
            workspace_id=workspace_id,
            action_key=action.action_key,
            capability_id=action.capability_id,
            resource_kind=action.resource_kind,
            risk_level=action.risk_level,
            kind=action.kind,
            requires_approval=True,
        ),
        origin=ApprovalOrigin(
            kind=_RUNTIME_ORIGIN_KIND,
            conversation_id=conversation_id,
            args_hash=approval_args_hash(arguments),
            context=render,
        ),
        reason=reason,
        intrinsic_rule=matched_rule_hint or "approval_payload_changed",
        intrinsic_reason=reason,
    )
    if decision.outcome == "allow":
        if decision.request is not None:
            from packages.core.governance.approvals import consume_approval

            await consume_approval(db, decision.request)
        return None
    if decision.request is None:
        return json.dumps({
            "error": "approval_required",
            "message": reason or "This action requires approval.",
            "action_key": action.action_key,
            "capability_id": action.capability_id,
            "tool": tool_name,
        }, ensure_ascii=False)
    return _runtime_hitl_payload(
        request_id=decision.request.id,
        action=action,
        tool_name=tool_name,
        arguments=arguments,
        matched_rule=decision.matched_rule,
        render=render,
    )


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
    """Return a blocking tool result, or ``None`` when execution may continue.

    One decision — ``resolve_approval`` — covers policy rules, standing
    workspace grants, and the direct-chat safety baseline (expressed as the
    subject's intrinsic requires_approval). The user-preference store is still
    consulted as a read fallback exactly where the legacy guard consulted it
    (workspace-less: before anything; workspace: only when a human would be
    asked) — that shim goes away in step 5.
    """
    action = classify_runtime_tool_action(
        name,
        arguments,
        entity_id=entity_id,
    )
    if action is None:
        return None

    approval_token = str(arguments.get("approval_token") or "").strip()
    # Platform-scoped actions (resource_kind="platform") are not workspace-
    # governed resources — no workspace policy can legitimately own or waive
    # them — so the workspace plane must never lift their baseline safety
    # requirement the way it can for ordinary workspace-owned capabilities.
    # This is enforced from two sides: `platform_scoped` here keeps
    # baseline_required True regardless of workspace_id, AND
    # governance/approvals.py's resolve_approval separately refuses to let
    # even an explicit workspace auto_approve_actions/capabilities rule
    # (including a wildcard "*") short-circuit past that requirement for a
    # resource_kind="platform" subject — so this claim is fully true, not
    # just "usually". Everything else keeps the existing behavior: baseline
    # only applies workspace-less, otherwise the workspace's own governance
    # rules decide.
    platform_scoped = getattr(action, "resource_kind", None) == "platform"
    baseline_required = (
        platform_scoped or not workspace_id
    ) and runtime_requires_baseline_approval(action)

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
            # Legacy parity: in direct chat the user preference is consulted
            # for EVERY classified action, before any other gate.
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

        from packages.core.governance.approvals import (
            ApprovalOrigin,
            ApprovalSubject,
            consume_approval,
            resolve_approval,
        )

        spent_credits_per_kind = None
        if workspace_id:
            from packages.core.budget import get_workspace_spent_credits_per_kind

            spent_credits_per_kind = await get_workspace_spent_credits_per_kind(db, workspace_id)

        render = await _runtime_render_context(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            tool_name=name,
            arguments=arguments,
            action=action,
        )
        decision = await resolve_approval(
            db,
            subject=ApprovalSubject(
                entity_id=entity_id,
                workspace_id=workspace_id,
                action_key=action.action_key,
                capability_id=action.capability_id,
                resource_kind=action.resource_kind,
                risk_level=action.risk_level,
                kind=action.kind,
                requires_approval=baseline_required,
            ),
            origin=ApprovalOrigin(
                kind=_RUNTIME_ORIGIN_KIND,
                conversation_id=conversation_id,
                # Thread the task so task-level runtime rules gate this plane
                # too, and so task-terminal cleanup can expire these requests.
                task_id=task_id,
                args_hash=approval_args_hash(arguments),
                context=render,
            ),
            spent_credits=spent_credits_per_kind,
            intrinsic_rule="direct_chat_baseline" if baseline_required else None,
            intrinsic_reason=(
                "Direct chat safety requires approval for destructive, publishing, "
                "sending, or automation actions."
            ) if baseline_required else None,
        )

        if decision.outcome == "allow":
            # If the allow rests on a one-time operator grant, spend it NOW —
            # this return IS the point of irreversible proceed for the runtime
            # plane (the caller executes the tool next). Without this, a
            # token-less identical retry keeps finding the granted row and
            # "approve once" silently becomes approve-forever.
            if decision.request is not None:
                await consume_approval(db, decision.request)
            await db.commit()
            return None

        if decision.outcome == "deny":
            # Hard governance block — never approvable.
            return json.dumps({
                "error": "blocked_by_governance",
                "message": decision.reason or "Workspace governance blocked this action.",
                "action_key": action.action_key,
                "capability_id": action.capability_id,
                "matched_rule": decision.matched_rule,
                "tool": name,
            }, ensure_ascii=False)

        # needs_human ─ a card is warranted. Workspace conversations have no
        # user-preference layer anymore: the ONE standing store there is the
        # workspace policy auto-approve set, which resolve_approval already
        # honored above. (Direct chat keeps the user preference — checked
        # before the resolve — because it has no workspace policy to write.)
        if not conversation_id:
            if workspace_id:
                return json.dumps({
                    "error": "approval_required",
                    "message": "Workspace governance requires approval, but this tool call has no conversation context to request it.",
                    "action_key": action.action_key,
                    "capability_id": action.capability_id,
                    "matched_rule": decision.matched_rule,
                }, ensure_ascii=False)
            return json.dumps({
                "error": "approval_required",
                "message": "This action requires approval, but there is no conversation context to request it.",
                "action_key": action.action_key,
                "capability_id": action.capability_id,
                "tool": name,
            }, ensure_ascii=False)

        if decision.request is None:
            # Shouldn't happen with a conversation surface — fail closed.
            return json.dumps({
                "error": "approval_required",
                "message": decision.reason or "This action requires approval.",
                "action_key": action.action_key,
                "capability_id": action.capability_id,
                "tool": name,
            }, ensure_ascii=False)

        payload = _runtime_hitl_payload(
            request_id=decision.request.id,
            action=action,
            tool_name=name,
            arguments=arguments,
            matched_rule=decision.matched_rule,
            render=render,
        )
        await db.commit()
        return payload


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
    resolution = await resolve_runtime_approval_turn(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action=action,
    )
    return resolution.message if resolution else None


async def resolve_runtime_approval_turn(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
) -> RuntimeApprovalResolution | None:
    """Resolve a runtime approval and expose any deterministic continuation."""
    # ── unified store first: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db, request_id=hitl_id, entity_id=entity_id, conversation_id=conversation_id,
    )
    if req is not None:
        item = _runtime_request_item(req)
        is_provider = (req.context or {}).get("kind") == "provider"
        if req.status != ApprovalStatus.PENDING:
            status_label = {"granted": "approved", "denied": "rejected"}.get(
                req.status, req.status
            )
            return RuntimeApprovalResolution(
                f"Runtime approval {hitl_id} is already {status_label}. "
                "Do not retry the blocked tool call."
            )
        normalized = normalize_approval_choice(action)
        if normalized in {APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_ALWAYS_APPROVE}:
            from packages.core.governance.approvals import grant_approval

            if is_provider:
                continuation = (req.context or {}).get("continuation") or {}
                if provider_approval_is_expired(continuation):
                    req.status = ApprovalStatus.EXPIRED.value
                    req.resolved_reason = "provider_approval_expired"
                    req.decided_at = datetime.now(timezone.utc)
                    await mark_runtime_hitl_request_resolved(
                        db,
                        conversation_id=conversation_id,
                        hitl_id=hitl_id,
                        choice="expired",
                    )
                    return RuntimeApprovalResolution(
                        "The provider approval expired before it was confirmed. "
                        "Do not retry the blocked tool call."
                    )
                provider_name = (req.context or {}).get("provider")
                # For a provider Manor owns, "Always approve" is honored: the
                # standing grant lands in the workspace policy auto-approve set
                # (workspace chats) or the user preference (direct chat), and
                # `register_provider_runtime_approval` auto-confirms future
                # gates for the same action instead of asking again.
                if (
                    normalized == APPROVAL_CHOICE_ALWAYS_APPROVE
                    and _provider_supports_always_approve(provider_name)
                ):
                    if req.workspace_id:
                        await grant_approval(
                            db, req, by_user_id=user_id, via="chat_card_always",
                            standing=True, changed_by=user_id,
                        )
                    else:
                        await _set_direct_chat_always_approve_preference(
                            db, req=req, user_id=user_id,
                        )
                        await grant_approval(
                            db, req, by_user_id=user_id, via="chat_card_always",
                        )
                else:
                    await grant_approval(db, req, by_user_id=user_id, via="chat_card")
                await mark_runtime_hitl_request_resolved(
                    db,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    choice=normalized,
                )
                provider_item = {
                    **item,
                    "kind": "provider",
                    "provider": (req.context or {}).get("provider"),
                    "provider_approval_id": (req.context or {}).get("provider_approval_id"),
                    "continuation": continuation,
                }
                return RuntimeApprovalResolution(
                    "[Runtime approval approved] Resume the exact provider action now.",
                    provider_approval_runtime_metadata(provider_item),
                )

            if normalized == APPROVAL_CHOICE_ALWAYS_APPROVE:
                if req.workspace_id:
                    # THE unified "Always": a standing grant in the workspace
                    # policy auto-approve set — the one store the dispatcher
                    # step gate and this runtime guard both honor.
                    await grant_approval(
                        db, req, by_user_id=user_id, via="chat_card_always",
                        standing=True, changed_by=user_id,
                    )
                else:
                    # No workspace to write policy into (direct chat) — the
                    # user-level preference remains the standing store there.
                    await _set_direct_chat_always_approve_preference(
                        db, req=req, user_id=user_id,
                    )
                    await grant_approval(
                        db, req, by_user_id=user_id, via="chat_card_always",
                    )
            else:
                await grant_approval(db, req, by_user_id=user_id, via="chat_card")
            await mark_runtime_hitl_request_resolved(
                db,
                conversation_id=conversation_id,
                hitl_id=hitl_id,
                choice=normalized,
            )
            return RuntimeApprovalResolution(
                runtime_approval_retry_message(item, hitl_id)
            )
        if normalized == APPROVAL_CHOICE_REJECT:
            from packages.core.governance.approvals import deny_approval

            await deny_approval(
                db, req, by_user_id=user_id, via="chat_card",
                reason="user rejected runtime approval",
            )
            await mark_runtime_hitl_request_resolved(
                db,
                conversation_id=conversation_id,
                hitl_id=hitl_id,
                choice=normalized,
            )
            return RuntimeApprovalResolution(runtime_approval_rejected_message(item))
        return None

    # ── tombstone: a card minted before the unified-store upgrade ──
    # The legacy conv.meta blob is no longer a decision store. Mark the old
    # item closed and tell the user to retry the action — the retry re-gates
    # through resolve_approval and mints a fresh card with a request id.
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return None
    approvals = runtime_approvals(conv)
    item = approvals.get(hitl_id)
    if not item:
        return None
    if item.get("status") == "pending":
        item.update({
            "status": "expired",
            "expired_at": runtime_approval_now_iso(),
            "expired_reason": "superseded_by_unified_approvals",
        })
        approvals[hitl_id] = item
        set_runtime_approvals(conv, approvals)
        await mark_runtime_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice="expired",
        )
    return RuntimeApprovalResolution(
        "This approval card predates the approval-system upgrade and can no "
        "longer be actioned. Ask the agent to retry the action — a fresh "
        "approval card will be issued."
    )


def _provider_hitl_data(
    hitl_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    continuation = item.get("continuation") or {}
    target = str(continuation.get("target_label") or "").strip()
    data_summary = str(continuation.get("data_summary") or "").strip()
    url = str(continuation.get("url") or "").strip()
    prompt_parts = ["Approve this external action"]
    if target:
        prompt_parts.append(f"on {target}")
    if url:
        prompt_parts.append(f"at {url}")
    prompt = " ".join(prompt_parts) + "?"
    if data_summary:
        prompt = f"{prompt} {data_summary}"
    operation = {
        "kind": "provider_approval",
        "provider": item.get("provider"),
        "provider_approval_id": item.get("provider_approval_id"),
        "tool": item.get("tool"),
        "action_key": item.get("action_key"),
        "args_preview": item.get("args_preview"),
        "url": url or None,
        "target_label": target or None,
        "data_summary": data_summary or None,
    }
    return {
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": hitl_id,
        "hitl": {
            "id": hitl_id,
            "type": "approval",
            "prompt": prompt,
            "action": item.get("action_key"),
            "capability_id": item.get("capability_id"),
            "tool": item.get("tool"),
            "content": data_summary or None,
            "args_preview": item.get("args_preview"),
            "options": (
                approval_options()
                if _provider_supports_always_approve(item.get("provider"))
                else [APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_REJECT]
            ),
        },
        "operation": operation,
        "message": (
            "This provider action requires approval. Do not retry until the "
            "standard HITL request is resolved."
        ),
    }


def _provider_item_from_request(req) -> dict[str, Any]:
    ctx = dict(req.context or {})
    return {
        "id": req.id,
        "kind": "provider",
        "provider": ctx.get("provider"),
        "provider_approval_id": ctx.get("provider_approval_id"),
        "tool": ctx.get("tool"),
        "action_key": req.action_key,
        "capability_id": req.capability_id,
        "args_preview": ctx.get("args_preview"),
        "continuation": ctx.get("continuation") or {},
    }


async def register_provider_runtime_approval(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """Persist one normalized provider approval in the unified store.

    Dedup by (provider, provider_approval_id): a still-pending registration
    returns the same card; an already-decided one returns None (the provider
    approval was consumed/decided — re-registering must not re-ask)."""
    if not isinstance(request, dict):
        return None
    provider = str(request.get("provider") or "").strip()
    provider_approval_id = str(
        request.get("provider_approval_id") or ""
    ).strip()
    confirmation_tool = str(request.get("confirmation_tool") or "").strip()
    confirmation_arguments = request.get("confirmation_arguments")
    retry_tool = str(request.get("retry_tool") or "").strip()
    retry_arguments = request.get("retry_arguments")
    if (
        not provider
        or not provider_approval_id
        or not confirmation_tool
        or not isinstance(confirmation_arguments, dict)
        or not retry_tool
        or not isinstance(retry_arguments, dict)
    ):
        return None
    conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
    if not conv:
        return None

    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        find_requests_by_dedup,
        mint_approval_request,
    )

    action_key = str(request.get("action_key") or f"{provider}.action")
    capability_id = f"{provider}.action"
    workspace_id = getattr(conv, "workspace_id", None)

    # Standing grant from a previous "Always approve" on this Manor-owned
    # provider: auto-confirm instead of asking the same question again. The
    # provider's own per-action gate still fires — Manor just answers it with
    # the decision the operator already made. Returning None means "no card";
    # the caller proceeds with the provider continuation.
    if _provider_supports_always_approve(provider) and await _provider_standing_grant(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        user_id=user_id,
        action_key=action_key,
        capability_id=capability_id,
    ):
        return None

    dedup_key = f"provider:{provider}:{provider_approval_id}"
    existing = await find_requests_by_dedup(
        db, entity_id=entity_id, dedup_key=dedup_key,
    )
    if existing:
        pending = next((r for r in existing if r.status == ApprovalStatus.PENDING), None)
        if pending is None:
            return None
        return _provider_hitl_data(pending.id, _provider_item_from_request(pending))

    req = await mint_approval_request(
        db,
        subject=ApprovalSubject(
            entity_id=entity_id,
            workspace_id=getattr(conv, "workspace_id", None),
            action_key=str(request.get("action_key") or f"{provider}.action"),
            capability_id=f"{provider}.action",
            risk_level="high",
            kind="action",
        ),
        origin=ApprovalOrigin(
            kind=_RUNTIME_ORIGIN_KIND,
            conversation_id=conversation_id,
            context={
                "kind": "provider",
                "provider": provider,
                "provider_approval_id": provider_approval_id,
                "tool": retry_tool,
                "args_hash": approval_args_hash(retry_arguments),
                "args_preview": approval_preview_arguments(retry_arguments),
                "requested_by": user_id,
                "continuation": dict(request),
            },
        ),
        dedup_key=dedup_key,
        reason=request.get("reason"),
        matched_rule="provider_required",
    )
    return _provider_hitl_data(req.id, _provider_item_from_request(req))


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
    wanted_ids = {
        str(item or "").strip()
        for item in (hitl_ids or [])
        if str(item or "").strip()
    }
    cancelled = 0
    cancelled_ids: list[str] = []

    # ── unified store: expire open tool-call requests for this conversation ──
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    rows = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
            HitlRequest.origin_kind == _RUNTIME_ORIGIN_KIND,
            HitlRequest.status == ApprovalStatus.PENDING,
        )
    )).scalars().all()
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        if wanted_ids and row.id not in wanted_ids:
            continue
        requested_by = (row.context or {}).get("requested_by")
        if user_id and requested_by and requested_by != user_id:
            continue
        row.status = ApprovalStatus.EXPIRED.value
        row.resolved_reason = reason
        row.decided_at = now_dt
        cancelled += 1
        cancelled_ids.append(row.id)

    if not cancelled:
        return 0
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
    resolution = await resolve_pending_runtime_approval_turn_from_reply(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message=message,
    )
    return resolution.message if resolution else None


async def resolve_pending_runtime_approval_turn_from_reply(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    message: str,
) -> RuntimeApprovalResolution | None:
    """Resolve one unambiguous pending runtime approval from a short reply."""
    try:
        from packages.core.services.ai_file_permissions import classify_file_approval_reply
    except Exception:
        classify_file_approval_reply = None
    choice = classify_file_approval_reply(message) if classify_file_approval_reply else None
    if choice not in {"approve", "always_approve", "reject"}:
        return None

    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    pending_ids = list((await db.execute(
        select(HitlRequest.id).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
            HitlRequest.origin_kind == _RUNTIME_ORIGIN_KIND,
            HitlRequest.status == ApprovalStatus.PENDING,
        )
    )).scalars().all())

    if not pending_ids:
        # A reply may target a card minted before the unified-store upgrade
        # (blob-only pending). Route it to the resolver so the user gets the
        # explicit tombstone notice instead of their "approve" silently
        # falling through to the model as free text.
        conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
        if conv:
            blob_pending = [
                hitl_id
                for hitl_id, item in runtime_approvals(conv).items()
                if isinstance(item, dict) and item.get("status") == "pending"
            ]
            if len(blob_pending) == 1:
                return await resolve_runtime_approval_turn(
                    db,
                    conversation_id=conversation_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    hitl_id=blob_pending[0],
                    action=choice,
                )

    if len(pending_ids) != 1:
        return None
    return await resolve_runtime_approval_turn(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=pending_ids[0],
        action=choice,
    )


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

    # ── unified store: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db, request_id=hitl_id, entity_id=entity_id, conversation_id=conversation_id,
    )
    if req is not None:
        ctx = dict(req.context or {})
        if ctx.get("tool") != tool_name or req.action_key != action.action_key:
            return APPROVAL_TOKEN_IGNORED
        if req.status != ApprovalStatus.GRANTED:
            if req.status == ApprovalStatus.CONSUMED:
                # A spent one-time grant: the identical retry already ran.
                return json.dumps({
                    "error": "approval_not_granted",
                    "status": "consumed",
                    "approval_token": hitl_id,
                })
            status_label = {"denied": "rejected"}.get(req.status, req.status)
            return json.dumps({
                "error": "approval_not_granted",
                "status": status_label,
                "approval_token": hitl_id,
            })
        if ctx.get("args_hash") not in _approval_args_hash_candidates(arguments):
            # Payload changed after approval — the grant covers other content.
            req.status = ApprovalStatus.EXPIRED.value
            req.resolved_reason = "payload_changed_after_approval"
            req.decided_at = datetime.now(timezone.utc)
            return await _runtime_block_payload(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=req.workspace_id,
                tool_name=tool_name,
                arguments=arguments,
                action=action,
                reason="The requested operation changed after approval. Please approve the updated content.",
                matched_rule_hint=req.matched_rule or "approval_payload_changed",
            )
        from packages.core.governance.approvals import consume_approval

        await consume_approval(db, req)
        return None

    # Token unknown — try a payload match among granted unified requests for
    # this conversation (the model may echo a stale/foreign token on the
    # correct approved payload).
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    accepted_hashes = set(_approval_args_hash_candidates(arguments))
    granted_rows = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
            HitlRequest.origin_kind == _RUNTIME_ORIGIN_KIND,
            HitlRequest.status == ApprovalStatus.GRANTED,
            HitlRequest.action_key == action.action_key,
        )
    )).scalars().all()
    matches = [
        row for row in granted_rows
        if (row.context or {}).get("tool") == tool_name
        and (row.context or {}).get("args_hash") in accepted_hashes
    ]
    if len(matches) == 1:
        from packages.core.governance.approvals import consume_approval

        await consume_approval(db, matches[0])
        return None

    # Unknown token, no payload match — ignore it and let the normal gate
    # decide (tokens minted before the unified-store upgrade land here: the
    # gate re-asks with a fresh card).
    return APPROVAL_TOKEN_IGNORED
