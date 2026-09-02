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

The old conversation-meta blob is no longer part of the runtime approval
decision path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from packages.core.constants.approvals import (
    ApprovalOriginKind,
    ApprovalOutcome,
    ApprovalStatus,
)
from packages.core.ai.runtime.approval_classifier import (
    RuntimeToolClassification,
    RuntimeToolEffect,
    classify_runtime_tool,
)
from packages.core.ai.runtime.approval_messages import (
    RuntimeApprovalContinuationError,
    approval_args_hash,
    approval_content_preview,
    approval_paths,
    approval_preview_arguments,
    approval_public_content,
    runtime_approval_prompt,
    runtime_approval_continuation,
    runtime_approval_rejected_message,
    runtime_approval_runtime_metadata,
    runtime_approval_retry_message,
)
from packages.core.ai.runtime.approval_preferences import (
    runtime_approval_preference_mode,
    set_runtime_approval_preference,
)
from packages.core.ai.runtime.approval_store import (
    load_runtime_approval_conversation,
    mark_runtime_hitl_request_resolved,
    runtime_approval_workspace_context,
)
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
    APPROVAL_CHOICE_REVISE,
    approval_options,
    normalize_approval_choice,
)
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalAction,
    runtime_requires_baseline_approval,
)
from packages.core.ai.runtime.provider_approvals import (
    freeze_provider_approval_request,
    provider_approval_confirmation_receipt,
    provider_approval_is_expired,
    provider_approval_runtime_metadata,
)
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
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
        resource_id=req.resource_id,
        capability_id=req.capability_id if not req.action_key else None,
    )


async def activate_blueprint_provider_scopes_for_proposal(
    db,
    *,
    workspace_id: str,
    task_ids: Iterable[str],
    user_id: str | None,
) -> int:
    """Promote only Blueprint-declared provider scopes for a proposal.

    Proposal-level ``Always approve`` must remain bounded to the services and
    providers declared by the workspace Blueprint; it must not create a
    general provider grant for unrelated tasks.
    """
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace

    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return 0
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )

    workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=workspace.entity_id,
    )
    if workspace is None or workspace.deleted_at is not None:
        return 0
    settings = dict(workspace.settings or {})
    scopes = settings.get("runtime_approval_scopes")
    if not isinstance(scopes, list):
        return 0
    ids = [str(value).strip() for value in task_ids if str(value).strip()]
    if not ids:
        return 0

    owner_keys = set((await db.execute(
        select(Task.owner_service_key).where(
            Task.id.in_(ids),
            Task.workspace_id == workspace_id,
        )
    )).scalars().all())
    grants = [
        dict(value)
        for value in (settings.get("runtime_approval_scope_grants") or [])
        if isinstance(value, dict)
    ]
    changed = 0
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        service_key = str(scope.get("owner_service_key") or "").strip()
        if not service_key:
            continue
        proposal_keys = scope.get("proposal_service_keys") or [service_key]
        if isinstance(proposal_keys, str):
            proposal_keys = [proposal_keys]
        if not owner_keys.intersection({str(value).strip() for value in proposal_keys}):
            continue
        if any(
            str(grant.get("owner_service_key") or "").strip() == service_key
            and str(grant.get("provider") or "").strip().lower()
            == str(scope.get("provider") or "").strip().lower()
            for grant in grants
        ):
            continue
        grant = dict(scope)
        grant["enabled"] = True
        grant["source"] = "proposal_always_approve"
        grant["granted_by"] = user_id
        grants.append(grant)
        changed += 1
    if changed:
        settings["runtime_approval_scope_grants"] = grants
        workspace.settings = settings
        await db.flush()
    return changed


def _provider_supports_always_approve(provider: str | None) -> bool:
    """Can a standing "Always approve" be honored for this provider?

    Yes for every provider we can normalize. Whether the gate belongs to
    Manor's own Chrome extension or to a third party changes nothing: the
    authority being exercised is the operator's, and a normalized provider
    approval always carries a machine-answerable continuation
    (``confirmation_tool`` + ``retry_tool``), so Manor can answer the gate on
    their behalf. The provider keeps demanding per action — it just stops
    being the operator's problem.

    The grant itself is recorded on the canonical action scope, optionally
    narrowed by a concrete resource id. That keeps Chrome form-fills,
    uploads, and other provider-backed actions on the same semantic axis as
    workspace approvals instead of inventing a separate provider namespace.
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
    task_id: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    workflow_run_id: str | None = None,
    workflow_lineage_root_run_id: str | None = None,
    workflow_action_grant_id: str | None = None,
    workflow_step_id: str | None = None,
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
    resource_id = str(request.get("resource_id") or "").strip() or None
    capability_id = f"{provider}.action"

    workflow_grant_id = str(workflow_action_grant_id or "").strip()
    if workflow_grant_id:
        from packages.core.database import async_session
        from packages.core.models.workflow import WorkflowActionGrant
        from packages.core.services.workflow_action_grant_service import (
            YOUTUBE_PUBLICATION_GRANT_TYPE,
            WorkflowActionGrantDenied,
            consume_proposal_youtube_publication_grant,
            validate_proposal_youtube_publication_grant,
        )

        async with async_session() as db:
            stored_grant = await db.get(WorkflowActionGrant, workflow_grant_id)
            if (
                stored_grant is not None
                and stored_grant.grant_type == YOUTUBE_PUBLICATION_GRANT_TYPE
            ):
                workflow_scope = {
                    "grant_id": workflow_grant_id,
                    "entity_id": entity_id,
                    "user_id": str(user_id or ""),
                    "workspace_id": str(workspace_id or ""),
                    "workflow_run_id": str(workflow_run_id or ""),
                    "workflow_lineage_root_run_id": str(
                        workflow_lineage_root_run_id or ""
                    ),
                    "workflow_step_id": str(workflow_step_id or ""),
                    "provider_request": request,
                }
                try:
                    _grant, request_kind = (
                        await validate_proposal_youtube_publication_grant(
                            db,
                            **workflow_scope,
                        )
                    )
                    if request_kind == "publish":
                        await consume_proposal_youtube_publication_grant(
                            db,
                            **workflow_scope,
                        )
                except WorkflowActionGrantDenied:
                    await db.rollback()
                    return result

                try:
                    confirmation = await execute(
                        confirmation_tool,
                        dict(confirmation_arguments),
                    )
                    token = _provider_approval_token(confirmation)
                    if not token:
                        await db.rollback()
                        return result
                    retried = await execute(
                        retry_tool,
                        {**retry_arguments, "approvalToken": token},
                    )
                except Exception:
                    await db.rollback()
                    logger.warning(
                        "Proposal Workflow auto-confirm failed before execution was proven",
                        exc_info=True,
                    )
                    return result
                if not _provider_action_was_executed(retried):
                    await db.rollback()
                    return retried if isinstance(retried, str) and retried else result
                await db.commit()
                return retried if isinstance(retried, str) and retried else result

    proposal_authorization = (
        runtime_metadata.get("proposal_external_authorization")
        if isinstance(runtime_metadata, dict)
        else None
    )
    if isinstance(proposal_authorization, dict):
        from packages.core.proposals.external_authorization import (
            proposal_youtube_public_request_matches,
            proposal_youtube_public_upload_entry_request_matches,
            proposal_youtube_public_upload_transfer_request_matches,
        )

        proposal_request_kind = None
        if proposal_youtube_public_request_matches(
            proposal_authorization,
            request,
            task_id=task_id,
            workspace_id=workspace_id,
        ):
            proposal_request_kind = "publish"
        elif proposal_youtube_public_upload_entry_request_matches(
            proposal_authorization,
            request,
            task_id=task_id,
            workspace_id=workspace_id,
        ):
            proposal_request_kind = "upload_entry"
        elif proposal_youtube_public_upload_transfer_request_matches(
            proposal_authorization,
            request,
            task_id=task_id,
            workspace_id=workspace_id,
        ):
            proposal_request_kind = "upload_transfer"
        else:
            logger.info(
                "proposal external authorization did not match provider approval: "
                "task_id=%s workspace_id=%s tool=%s confirmation_mode=%s "
                "policy_category=%s target_label=%s url=%s",
                task_id,
                workspace_id,
                request.get("retry_tool"),
                request.get("confirmation_mode"),
                request.get("policy_category"),
                request.get("target_label"),
                request.get("url"),
            )
            # A Proposal-scoped run fails closed on any target mismatch. It
            # must not silently fall through to a broader standing grant.
            return result

    from packages.core.database import async_session

    if isinstance(proposal_authorization, dict) and task_id and workspace_id:
        async with async_session() as db:
            if proposal_request_kind == "upload_entry":
                from packages.core.proposals.external_authorization import (
                    begin_proposal_youtube_public_authorization,
                )

                granted = await begin_proposal_youtube_public_authorization(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    runtime_authorization=proposal_authorization,
                    provider_request=request,
                )
            elif proposal_request_kind == "upload_transfer":
                from packages.core.proposals.external_authorization import (
                    record_proposal_youtube_public_upload_transfer,
                )

                granted = await record_proposal_youtube_public_upload_transfer(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    runtime_authorization=proposal_authorization,
                    provider_request=request,
                )
            else:
                from packages.core.proposals.external_authorization import (
                    consume_proposal_youtube_public_authorization,
                )

                granted = await consume_proposal_youtube_public_authorization(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    runtime_authorization=proposal_authorization,
                    provider_request=request,
                )
            if not granted:
                return result

            try:
                confirmation = await execute(
                    confirmation_tool,
                    dict(confirmation_arguments),
                )
                token = _provider_approval_token(confirmation)
                logger.info(
                    "proposal provider confirmation completed: task_id=%s "
                    "request_kind=%s status=%s token_present=%s",
                    task_id,
                    proposal_request_kind,
                    _provider_result_field(confirmation, "status"),
                    bool(token),
                )
                if not token:
                    await db.rollback()
                    return result
                retried = await execute(
                    retry_tool,
                    {**retry_arguments, "approvalToken": token},
                )
            except Exception:
                await db.rollback()
                logger.warning(
                    "proposal auto-confirm for %s failed before execution was proven; "
                    "authorization was not recorded",
                    tool_name,
                    exc_info=True,
                )
                return result

            action_executed = _provider_action_was_executed(retried)
            logger.info(
                "proposal provider retry completed: task_id=%s request_kind=%s "
                "status=%s reason=%s action_executed=%s",
                task_id,
                proposal_request_kind,
                _provider_result_field(retried, "status"),
                _provider_result_field(retried, "reason"),
                action_executed,
            )
            if not action_executed:
                await db.rollback()
                return retried if isinstance(retried, str) and retried else result

            try:
                await db.commit()
            except Exception:
                logger.exception(
                    "provider action executed but Proposal authorization persistence "
                    "failed: task_id=%s request_kind=%s",
                    task_id,
                    proposal_request_kind,
                )
            return retried if isinstance(retried, str) and retried else result

    async with async_session() as db:
        granted = await _standing_grant(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            action_key=action_key,
            resource_id=resource_id,
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


def _provider_result_field(value: Any, field: str) -> Any:
    if not isinstance(value, str):
        return None
    try:
        payload = json.loads(value)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get(field)


def _provider_action_was_executed(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        payload = json.loads(value)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if "action_executed" in payload:
        return payload.get("action_executed") is True
    post_action_state = payload.get("post_action_page_state")
    return bool(
        payload.get("ok") is True
        and isinstance(post_action_state, dict)
        and post_action_state.get("state_verified") is True
    )


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


async def _standing_grant(
    db,
    *,
    workspace_id: str | None,
    user_id: str | None,
    action_key: str,
    resource_id: str | None,
    capability_id: str,
) -> bool:
    """Did the operator already say "always" for this canonical action?

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
            resource_id=resource_id,
            capability_id=capability_id,
        ):
            return True
    return await runtime_approval_preference_mode(
        db,
        user_id=user_id,
        action_key=action_key,
        resource_id=resource_id,
        capability_id=capability_id,
    ) == "always_approve"


@dataclass(frozen=True)
class RuntimeApprovalResolution:
    message: str
    runtime_metadata: dict[str, Any] | None = None


def _chrome_confirmation_receipt(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if tool_name != "mcp__chrome__confirm_action":
        return {}
    mode = str(arguments.get("confirmation_mode") or "").strip()
    category = str(arguments.get("policy_category") or "").strip()
    if mode not in {
        "always_action_time",
        "preapproval_allowed",
        "handoff_required",
        "no_confirmation",
    } or not category:
        return {}
    return {
        "confirmation_mode": mode,
        "policy_category": category,
        "preapproved": arguments.get("preapproved") is True,
    }


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
    for_update: bool = False,
):
    """Load a HitlRequest ONLY if it is a runtime tool-call request for
    THIS conversation. Step-origin requests (dispatcher cards) and other
    conversations' requests are invisible here — the chat resolver chain
    relies on returning None for foreign ids."""
    if not request_id or not conversation_id:
        return None
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    query = select(HitlRequest).where(
        HitlRequest.id == request_id,
        HitlRequest.entity_id == entity_id,
    )
    if for_update:
        # A compatibility lookup may already have loaded this row into the
        # session identity map before we acquire the lock. Refresh from the
        # locked database version so a concurrent consumer cannot keep using
        # its stale in-memory GRANTED snapshot after the first transaction
        # commits CONSUMED.
        query = query.with_for_update().execution_options(populate_existing=True)
    req = (await db.execute(query)).scalar_one_or_none()
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
        "continuation": ctx.get("continuation"),
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
    confirmation_receipt = _chrome_confirmation_receipt(tool_name, arguments)
    return {
        "tool": tool_name,
        "args_hash": approval_args_hash(arguments),
        "args_preview": args_preview,
        "retry_args": args_preview if not args_preview.get("truncated") else None,
        "continuation": runtime_approval_continuation(tool_name, arguments),
        "paths": paths,
        "workspace": workspace,
        "content": content_preview,
        "requested_by": user_id,
        **confirmation_receipt,
    }


def _runtime_continuation_error_result(
    *,
    tool_name: str,
    error: RuntimeApprovalContinuationError,
) -> str:
    return json.dumps(
        {
            "error": "approval_continuation_unavailable",
            "message": (
                f"{error} Save large content as a Workspace document and retry "
                "with its canonical reference."
            ),
            "tool": tool_name,
            "retryable": True,
        },
        ensure_ascii=False,
    )


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
    confirmation_receipt = {
        key: render[key]
        for key in ("confirmation_mode", "policy_category", "preapproved")
        if key in render
    }
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
            **confirmation_receipt,
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
            **confirmation_receipt,
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
    permission_decision: PermissionDecision,
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

    async def load_render_context() -> dict[str, Any]:
        return await _runtime_render_context(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            tool_name=tool_name,
            arguments=arguments,
            action=action,
        )

    try:
        decision = await resolve_approval(
            db,
            subject=ApprovalSubject(
                entity_id=entity_id,
                workspace_id=workspace_id,
                action_key=action.action_key,
                resource_id=action.resource_id,
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
                context_loader=load_render_context,
            ),
            permission_decision=permission_decision,
            reason=reason,
            intrinsic_rule=matched_rule_hint or "approval_payload_changed",
            intrinsic_reason=reason,
        )
    except RuntimeApprovalContinuationError as exc:
        return _runtime_continuation_error_result(
            tool_name=tool_name,
            error=exc,
        )
    if decision.outcome is ApprovalOutcome.ALLOW:
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
        render=dict(decision.request.context or {}),
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
    runtime_envelope: Any | None = None,
    classification: RuntimeToolClassification | None = None,
) -> str | None:
    """Return a blocking tool result, or ``None`` when execution may continue.

    Every classified effect first crosses the non-approvable infra permission
    boundary. READ/USE/CONTROL return after that decision; ACTION then enters
    ``resolve_approval`` for policy rules, standing grants, direct-chat safety,
    and HITL. Approval tokens and standing preferences therefore cannot survive
    a role, membership, tenant-scope, or tool-binding revocation.
    """
    if classification is None:
        from packages.core.ai.runtime.approval_classifier import (
            resolve_runtime_workspace_file_scope,
        )

        workspace_file_scope = await resolve_runtime_workspace_file_scope(
            tool_name=name,
            arguments=arguments,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        discovered_grants = getattr(
            runtime_envelope,
            "discovered_tool_grants",
            None,
        )
        discovered = (
            discovered_grants.metadata(name)
            if discovered_grants is not None
            else None
        )
        classification = classify_runtime_tool(
            name,
            arguments,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            workspace_file_scope=workspace_file_scope,
            declared_effect=(discovered.effect if discovered is not None else None),
        )
    if classification.effect is RuntimeToolEffect.UNKNOWN:
        return json.dumps({
            "error": "blocked_by_authorization_configuration",
            "message": classification.reason or (
                "This tool has no declared execution-effect classification."
            ),
            "matched_rule": "permission.tool.classification",
            "tool": name,
        }, ensure_ascii=False)
    authorization = classification.authorization
    if authorization is None:
        return json.dumps({
            "error": "blocked_by_authorization_configuration",
            "message": "Tool classification is missing hard-authorization metadata.",
            "matched_rule": "permission.tool.classification",
            "tool": name,
        }, ensure_ascii=False)

    from packages.core.database import async_session

    async with async_session() as db:
        from packages.core.services.runtime_authorization import (
            authorize_runtime_action,
        )

        principal = getattr(runtime_envelope, "principal", None)
        raw_kind = getattr(principal, "kind", None) if principal is not None else None
        principal_kind = getattr(raw_kind, "value", raw_kind)
        runtime_surface = getattr(runtime_envelope, "surface", None)
        from packages.core.ai.runtime.envelope import runtime_effective_tool_scope

        effective_scope = runtime_effective_tool_scope(
            runtime_envelope=runtime_envelope,
        )
        bound_tool_names = effective_scope.searchable_tool_names() or set()
        permission_decision = await authorize_runtime_action(
            db,
            entity_id=entity_id,
            user_id=user_id or None,
            workspace_id=workspace_id,
            action_key=authorization.action_key,
            capability_id=authorization.capability_id,
            access=authorization.access,
            principal_kind=principal_kind,
            principal_agent_id=(
                getattr(principal, "agent_id", None) if principal is not None else None
            ),
            principal_execution_user_id=(
                getattr(principal, "execution_user_id", None)
                if principal is not None
                else None
            ),
            tool_name=name,
            bound_tool_names=bound_tool_names,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_surface=runtime_surface,
        )
        if not permission_decision.allowed:
            return json.dumps({
                "error": "blocked_by_permission",
                "message": permission_decision.reason or "Runtime permission denied.",
                "action_key": authorization.action_key,
                "capability_id": authorization.capability_id,
                "matched_rule": (
                    permission_decision.matched_rule or AuthorizationRule.DENIED.value
                ),
                "tool": name,
            }, ensure_ascii=False)

        # Hard authorization is mandatory for every executable effect. Only
        # calls with a direct side effect continue into governance/HITL.
        if classification.effect in {
            RuntimeToolEffect.READ_ONLY,
            RuntimeToolEffect.ORCHESTRATOR,
            RuntimeToolEffect.CONTROL,
        }:
            return None

        action = classification.action
        if action is None:  # Defensive exhaustiveness for malformed plugins.
            return json.dumps({
                "error": "blocked_by_authorization_configuration",
                "message": "Action classification is missing approval metadata.",
                "matched_rule": "permission.tool.classification",
                "tool": name,
            }, ensure_ascii=False)

        approval_token = str(arguments.get("approval_token") or "").strip()
        platform_scoped = getattr(action, "resource_kind", None) == "platform"
        external_channel_reply = bool(
            getattr(principal, "is_external", False)
            and action.action_key == "channel.reply"
        )
        baseline_required = (
            (platform_scoped or not workspace_id)
            and not external_channel_reply
            and runtime_requires_baseline_approval(action)
        )

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
                permission_decision=permission_decision,
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

        if not workspace_id and not external_channel_reply:
            # Legacy parity: in direct chat the user preference is consulted
            # for every classified action after the infra permission boundary
            # and before governance policy / HITL consent.
            preference = await runtime_approval_preference_mode(
                db,
                user_id=user_id,
                action_key=action.action_key,
                resource_id=action.resource_id,
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

        async def load_render_context() -> dict[str, Any]:
            return await _runtime_render_context(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                tool_name=name,
                arguments=arguments,
                action=action,
            )

        try:
            decision = await resolve_approval(
                db,
                subject=ApprovalSubject(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    action_key=action.action_key,
                    resource_id=action.resource_id,
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
                    context_loader=load_render_context,
                ),
                permission_decision=permission_decision,
                spent_credits=spent_credits_per_kind,
                intrinsic_rule="direct_chat_baseline" if baseline_required else None,
                intrinsic_reason=(
                    "Direct chat safety requires approval for destructive, publishing, "
                    "sending, or automation actions."
                ) if baseline_required else None,
            )
        except RuntimeApprovalContinuationError as exc:
            return _runtime_continuation_error_result(
                tool_name=name,
                error=exc,
            )

        if decision.outcome is ApprovalOutcome.ALLOW:
            # If the allow rests on a one-time operator grant, spend it NOW —
            # this return IS the point of irreversible proceed for the runtime
            # plane (the caller executes the tool next). Without this, a
            # token-less identical retry keeps finding the granted row and
            # "approve once" silently becomes approve-forever.
            if decision.request is not None:
                await consume_approval(db, decision.request)
            await db.commit()
            return None

        if decision.outcome is ApprovalOutcome.DENY:
            permission_denied = str(decision.matched_rule or "").startswith(
                "permission."
            )
            return json.dumps({
                "error": (
                    "blocked_by_permission"
                    if permission_denied
                    else "blocked_by_governance"
                ),
                "message": decision.reason or (
                    "Runtime permission denied."
                    if permission_denied
                    else "Workspace governance blocked this action."
                ),
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
            render=dict(decision.request.context or {}),
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
    revision_request: str | None = None,
) -> RuntimeApprovalResolution | None:
    """Resolve a runtime approval and expose any deterministic continuation."""
    # ── unified store first: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
    )
    if req is not None:
        item = _runtime_request_item(req)
        is_provider = (req.context or {}).get("kind") == "provider"
        normalized = normalize_approval_choice(action)
        actionable_provider_grant = (
            is_provider
            and req.status == ApprovalStatus.GRANTED
            and (
                normalized in {
                    APPROVAL_CHOICE_APPROVE,
                    APPROVAL_CHOICE_ALWAYS_APPROVE,
                }
                or (
                    normalized == APPROVAL_CHOICE_REJECT
                    and not isinstance(
                        (req.context or {}).get("provider_action_claim"), dict
                    )
                )
            )
        )
        if req.status != ApprovalStatus.PENDING and not actionable_provider_grant:
            status_label = {"granted": "approved", "denied": "rejected"}.get(
                req.status, req.status
            )
            return RuntimeApprovalResolution(
                f"Runtime approval {hitl_id} is already {status_label}. "
                "Do not retry the blocked tool call."
            )
        if normalized in {
            APPROVAL_CHOICE_APPROVE,
            APPROVAL_CHOICE_ALWAYS_APPROVE,
            APPROVAL_CHOICE_REJECT,
            APPROVAL_CHOICE_REVISE,
        }:
            from packages.core.services.runtime_authorization import (
                authorize_hitl_resolution,
            )

            authority = await authorize_hitl_resolution(
                db,
                request=req,
                by_user_id=user_id,
                standing=normalized == APPROVAL_CHOICE_ALWAYS_APPROVE,
            )
            if not authority.allowed:
                return RuntimeApprovalResolution(
                    authority.reason
                    or "You do not have permission to resolve this approval."
                )
        if normalized == APPROVAL_CHOICE_REVISE and is_provider:
            if not _provider_approval_supports_revision(
                _provider_item_from_request(req)
            ):
                return RuntimeApprovalResolution(
                    "This provider approval cannot be revised. Choose approve or reject."
                )
            revision = str(revision_request or "").strip()
            if not revision:
                return RuntimeApprovalResolution(
                    "Revision instructions are required before requesting changes."
                )
            req.status = ApprovalStatus.EXPIRED.value
            req.decided_by_user_id = user_id
            req.decided_at = datetime.now(timezone.utc)
            req.decided_via = "chat_card_revise"
            req.resolved_reason = "revision_requested"
            await mark_runtime_hitl_request_resolved(
                db,
                conversation_id=conversation_id,
                hitl_id=hitl_id,
                choice=normalized,
            )
            return RuntimeApprovalResolution(
                "[Runtime approval revision requested] Do not confirm or retry "
                "the blocked provider action. Revise the proposed content as "
                "requested, then prepare the new final action for approval.\n\n"
                f"Revision request:\n{revision[:4000]}",
                {
                    "approval_kind": "provider_revision",
                    "approval_resume_guidance": (
                        "Revise the proposed content according to the user's "
                        "instructions. Do not confirm or reuse the superseded "
                        "provider approval; request approval again only for the "
                        "new final external action."
                    ),
                },
            )
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
                provider_item = {
                    **item,
                    "kind": "provider",
                    "provider": (req.context or {}).get("provider"),
                    "provider_approval_id": (req.context or {}).get(
                        "provider_approval_id"
                    ),
                    "continuation": continuation,
                }
                provider_runtime_metadata = provider_approval_runtime_metadata(
                    provider_item
                )
                if provider_runtime_metadata is None:
                    req.status = ApprovalStatus.EXPIRED.value
                    req.resolved_reason = "approval_continuation_unavailable"
                    req.decided_at = datetime.now(timezone.utc)
                    await mark_runtime_hitl_request_resolved(
                        db,
                        conversation_id=conversation_id,
                        hitl_id=hitl_id,
                        choice="expired",
                    )
                    return RuntimeApprovalResolution(
                        "The provider action could not be resumed because its "
                        "exact continuation was not preserved. Please issue "
                        "the action again."
                    )
                provider_runtime_metadata["provider_approval_execution"] = {
                    "hitl_id": hitl_id,
                    "confirmation_tool": str(
                        continuation.get("confirmation_tool") or ""
                    ).strip(),
                    "retry_tool": str(
                        continuation.get("retry_tool") or ""
                    ).strip(),
                }
                provider_name = (req.context or {}).get("provider")
                # For a provider Manor owns, "Always approve" is honored: the
                # standing grant lands in the workspace policy auto-approve set
                # (workspace chats) or the user preference (direct chat), and
                # `register_provider_runtime_approval` auto-confirms future
                # gates for the same action instead of asking again.
                wants_standing_grant = (
                    normalized == APPROVAL_CHOICE_ALWAYS_APPROVE
                    and _provider_supports_always_approve(provider_name)
                )
                if wants_standing_grant:
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
                elif req.status == ApprovalStatus.PENDING:
                    await grant_approval(
                        db, req, by_user_id=user_id, via="chat_card",
                    )
                # Keep the card actionable until the exact provider attempt
                # reaches a durable terminal state. If storage fails before
                # execution, another click can resume this same frozen grant;
                # a persisted provider-action claim prevents duplicate I/O.
                return RuntimeApprovalResolution(
                    "[Runtime approval approved] Resume the exact provider action now.",
                    provider_runtime_metadata,
                )

            runtime_metadata = runtime_approval_runtime_metadata(item, hitl_id)
            if runtime_metadata is None:
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = "approval_continuation_unavailable"
                req.decided_at = datetime.now(timezone.utc)
                await mark_runtime_hitl_request_resolved(
                    db,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    choice="expired",
                )
                return RuntimeApprovalResolution(
                    "The approved action could not be resumed because its exact "
                    "tool arguments were not preserved. Please issue the action again."
                )

            forced_call = runtime_metadata["forced_tool_calls"][0]
            from packages.core.ai.runtime.tool_input_validation import (
                validate_runtime_tool_arguments,
            )
            from packages.core.ai.runtime.tool_registry import (
                runtime_tool_schema_for_actor,
            )

            registered_schema = await runtime_tool_schema_for_actor(
                str(forced_call["name"]),
                entity_id=entity_id,
                user_id=user_id,
            )
            if registered_schema is None:
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = "approved_tool_schema_unavailable"
                req.decided_at = datetime.now(timezone.utc)
                await mark_runtime_hitl_request_resolved(
                    db,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    choice="expired",
                )
                return RuntimeApprovalResolution(
                    "The action was not executed because its current tool schema "
                    "is unavailable for this account. Please issue it again."
                )
            validation_failure = (
                validate_runtime_tool_arguments(
                    arguments=dict(forced_call["arguments"]),
                    tool_schema=registered_schema,
                )
            )
            if validation_failure is not None:
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = "approved_tool_input_invalid"
                req.decided_at = datetime.now(timezone.utc)
                await mark_runtime_hitl_request_resolved(
                    db,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    choice="expired",
                )
                return RuntimeApprovalResolution(
                    "The action was not executed because its stored tool input is "
                    f"invalid: {validation_failure.message} Please issue it again."
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
                runtime_approval_retry_message(item, hitl_id),
                runtime_metadata,
            )
        if normalized == APPROVAL_CHOICE_REJECT:
            if is_provider and req.status == ApprovalStatus.GRANTED:
                await _reject_unclaimed_provider_runtime_approval(
                    db,
                    req,
                    user_id=user_id,
                )
            else:
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

    return None


@dataclass(frozen=True)
class ProviderRuntimeApprovalExecutionPreflight:
    """Durable decision made immediately before provider execution."""

    result: str | None = None
    changed: bool = False


def _provider_runtime_execution_metadata(
    runtime_metadata: dict[str, Any] | None,
) -> tuple[str, str, str] | None:
    execution = (
        runtime_metadata.get("provider_approval_execution")
        if isinstance(runtime_metadata, dict)
        else None
    )
    if not isinstance(execution, dict):
        return None
    hitl_id = str(execution.get("hitl_id") or "").strip()
    confirmation_tool = str(execution.get("confirmation_tool") or "").strip()
    retry_tool = str(execution.get("retry_tool") or "").strip()
    if not hitl_id or not confirmation_tool or not retry_tool:
        return None
    return hitl_id, confirmation_tool, retry_tool


def _provider_execution_blocked_result(
    *,
    hitl_id: str,
    status: str,
    reason: str,
) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": "provider_approval_execution_blocked",
            "status": status,
            "approval_id": hitl_id,
            "reason": reason,
        },
        ensure_ascii=False,
    )


async def _expire_provider_runtime_approval(
    db,
    req,
    reason: str,
    *,
    card_choice: str = "expired",
) -> None:
    req.status = ApprovalStatus.EXPIRED.value
    req.resolved_reason = reason
    req.decided_at = datetime.now(timezone.utc)
    req.consumed_at = None
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(
        db,
        req,
        ledger_et.APPROVAL_EXPIRED,
    )
    await _resolve_provider_runtime_approval_card(
        db,
        req,
        choice=card_choice,
    )


async def _reject_unclaimed_provider_runtime_approval(
    db,
    req,
    *,
    user_id: str,
) -> None:
    req.status = ApprovalStatus.DENIED.value
    req.decided_by_user_id = user_id
    req.decided_at = datetime.now(timezone.utc)
    req.decided_via = "chat_card"
    req.resolved_reason = "provider_approval_revoked"
    req.consumed_at = None
    req.reason = "user rejected runtime approval"
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.ledger import event_types as ledger_et

    await ledger_adapters.record_approval_event(
        db,
        req,
        ledger_et.APPROVAL_DENIED,
        actor_id=user_id,
    )
    await _resolve_provider_runtime_approval_card(
        db,
        req,
        choice=APPROVAL_CHOICE_REJECT,
    )


async def _resolve_provider_runtime_approval_card(
    db,
    req,
    *,
    choice: str,
) -> None:
    """Resolve the provider card in the same transaction as terminal state."""

    conversation_id = str(req.origin_conversation_id or "").strip()
    if not conversation_id:
        return
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    await mark_hitl_request_resolved(
        db,
        conversation_id=conversation_id,
        hitl_id=req.id,
        choice=choice,
    )


def _provider_confirmation_receipt_matches(
    receipt: Any,
    *,
    provider: Any,
    provider_approval_id: Any,
) -> bool:
    return bool(
        isinstance(receipt, dict)
        and str(receipt.get("provider") or "").strip()
        == str(provider or "").strip()
        and str(receipt.get("provider_approval_id") or "").strip()
        == str(provider_approval_id or "").strip()
        and str(receipt.get("approval_token") or "").strip()
    )


async def prepare_provider_runtime_approval_execution(
    db,
    *,
    runtime_metadata: dict[str, Any] | None,
    entity_id: str,
    conversation_id: str | None,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> ProviderRuntimeApprovalExecutionPreflight:
    """Persist or replay the exact provider continuation before provider I/O.

    Confirmation receipts are durable, so a worker restart can reconstruct the
    one-time provider token without confirming twice.  The external retry is
    claimed under a row lock before its handler runs.  If the worker crashes
    after provider I/O, that claim remains and all automatic replays fail
    closed as an ambiguous outcome instead of repeating the side effect.
    """

    metadata = _provider_runtime_execution_metadata(runtime_metadata)
    if metadata is None:
        return ProviderRuntimeApprovalExecutionPreflight()
    hitl_id, confirmation_tool, retry_tool = metadata
    if tool_name not in {confirmation_tool, retry_tool}:
        return ProviderRuntimeApprovalExecutionPreflight()

    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
    )
    if req is None or (req.context or {}).get("kind") != "provider":
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="invalid",
                reason="The provider approval no longer exists in this conversation.",
            )
        )

    context = dict(req.context or {})
    continuation = freeze_provider_approval_request(context.get("continuation"))
    if (
        continuation is None
        or str(continuation.get("confirmation_tool") or "").strip()
        != confirmation_tool
        or str(continuation.get("retry_tool") or "").strip() != retry_tool
        or str(continuation.get("provider") or "").strip()
        != str(context.get("provider") or "").strip()
        or str(continuation.get("provider_approval_id") or "").strip()
        != str(context.get("provider_approval_id") or "").strip()
    ):
        changed = False
        if req.status == ApprovalStatus.GRANTED:
            await _expire_provider_runtime_approval(
                db,
                req,
                "provider_continuation_invalid_before_execution",
            )
            changed = True
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="expired",
                reason="The stored provider continuation is no longer valid.",
            ),
            changed=changed,
        )

    if req.status != ApprovalStatus.GRANTED:
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status=str(req.status),
                reason="This provider approval is no longer executable.",
            )
        )

    receipt = context.get("provider_confirmation_receipt")
    receipt_matches = _provider_confirmation_receipt_matches(
        receipt,
        provider=context.get("provider"),
        provider_approval_id=context.get("provider_approval_id"),
    )
    if tool_name == confirmation_tool:
        if not receipt_matches:
            return ProviderRuntimeApprovalExecutionPreflight()
        return ProviderRuntimeApprovalExecutionPreflight(
            result=json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "approvalId": receipt["provider_approval_id"],
                    "approvalToken": receipt["approval_token"],
                },
                ensure_ascii=False,
            )
        )

    if not receipt_matches:
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="unconfirmed",
                reason="The provider confirmation receipt was not durably recorded.",
            )
        )

    supplied_arguments = dict(arguments or {})
    supplied_token = str(
        supplied_arguments.pop("approvalToken", None)
        or supplied_arguments.pop("approval_token", None)
        or ""
    ).strip()
    expected_arguments = continuation.get("retry_arguments")
    if (
        supplied_token != str(receipt.get("approval_token") or "").strip()
        or supplied_arguments != expected_arguments
    ):
        await _expire_provider_runtime_approval(
            db,
            req,
            "provider_retry_payload_changed",
        )
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="expired",
                reason="The provider retry no longer matches the approved action.",
            ),
            changed=True,
        )

    if isinstance(context.get("provider_action_claim"), dict):
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="ambiguous",
                reason=(
                    "This provider action was already started. Its outcome must be "
                    "reconciled before another attempt."
                ),
            )
        )

    context["provider_action_claim"] = {
        "tool": retry_tool,
        "args_hash": approval_args_hash(supplied_arguments),
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    req.context = context
    req.resolved_reason = "provider_action_claimed"
    return ProviderRuntimeApprovalExecutionPreflight(changed=True)


async def settle_provider_runtime_approval_execution(
    db,
    *,
    runtime_metadata: dict[str, Any] | None,
    entity_id: str,
    conversation_id: str | None,
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: Any,
) -> bool:
    """Settle a provider HITL row at the Runtime tool boundary.

    Provider-native approval is a two-call continuation: confirmation followed
    by the exact external action.  The chat-card click grants consent, but the
    grant is not spent until that second call actually completes.  A failed
    confirmation or failed retry closes the one-time request so a stale grant
    cannot be replayed.
    """

    metadata = _provider_runtime_execution_metadata(runtime_metadata)
    if metadata is None:
        return False
    hitl_id, confirmation_tool, retry_tool = metadata
    if tool_name not in {confirmation_tool, retry_tool}:
        return False

    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
    )
    if req is None or (req.context or {}).get("kind") != "provider":
        return False
    from packages.core.ai.runtime.streams import runtime_tool_status_for_chat

    parsed_result = result if isinstance(result, dict) else None
    result_text = (
        result
        if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, default=str)
    )
    failed = runtime_tool_status_for_chat(result_text) == "error"
    if parsed_result is None:
        try:
            parsed_result = json.loads(result_text)
        except (TypeError, ValueError):
            parsed_result = None
    if isinstance(parsed_result, dict) and parsed_result.get("ok") is False:
        failed = True

    if tool_name == confirmation_tool:
        if req.status != ApprovalStatus.GRANTED:
            return False
        receipt = provider_approval_confirmation_receipt(
            tool_name,
            arguments,
            result,
        )
        context = dict(req.context or {})
        if receipt is not None and not failed and _provider_confirmation_receipt_matches(
            receipt,
            provider=context.get("provider"),
            provider_approval_id=context.get("provider_approval_id"),
        ):
            context["provider_confirmation_receipt"] = {
                **receipt,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
            req.context = context
            req.resolved_reason = "provider_confirmation_completed"
            return True
        await _expire_provider_runtime_approval(
            db,
            req,
            "provider_confirmation_failed",
        )
        return True

    if req.status != ApprovalStatus.GRANTED:
        return False
    context = dict(req.context or {})
    if not isinstance(context.get("provider_action_claim"), dict):
        if failed:
            # Authorization, binding, or input validation can reject the
            # continuation before the pre-I/O claim is created. Close that
            # now-unresumable grant explicitly; no provider side effect ran.
            await _expire_provider_runtime_approval(
                db,
                req,
                "provider_action_blocked_before_execution",
            )
            return True
        logger.error(
            "Refusing to settle unclaimed provider action %s for approval %s",
            tool_name,
            hitl_id,
        )
        return False
    if failed:
        await _expire_provider_runtime_approval(db, req, "provider_action_failed")
        return True

    from packages.core.governance.approvals import consume_approval

    await consume_approval(db, req)
    req.resolved_reason = "provider_action_completed"
    await _resolve_provider_runtime_approval_card(
        db,
        req,
        choice=(
            APPROVAL_CHOICE_ALWAYS_APPROVE
            if req.decided_via == "chat_card_always"
            else APPROVAL_CHOICE_APPROVE
        ),
    )
    return True


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
    options = (
        approval_options()
        if _provider_supports_always_approve(item.get("provider"))
        else [APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_REJECT]
    )
    if _provider_approval_supports_revision(item):
        options.insert(-1, APPROVAL_CHOICE_REVISE)
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
            "options": options,
        },
        "operation": operation,
        "message": (
            "This provider action requires approval. Do not retry until the "
            "standard HITL request is resolved."
        ),
    }


def _provider_approval_supports_revision(item: dict[str, Any]) -> bool:
    """Only proposed external communications have a meaningful draft to revise."""

    continuation = item.get("continuation") or {}
    return (
        isinstance(continuation, dict)
        and str(continuation.get("policy_category") or "").strip()
        == "representational_communication"
    )


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
    request = freeze_provider_approval_request(request)
    if request is None:
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
    resource_id = str(request.get("resource_id") or "").strip() or None
    capability_id = f"{provider}.action"
    workspace_id = getattr(conv, "workspace_id", None)

    # Standing grant from a previous "Always approve" on this Manor-owned
    # provider action: auto-confirm instead of asking the same question again.
    # The provider's own per-action gate still fires — Manor just answers it
    # with the decision the operator already made. Returning None means "no
    # card"; the caller proceeds with the provider continuation.
    if _provider_supports_always_approve(provider) and await _standing_grant(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        action_key=action_key,
        resource_id=resource_id,
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
            resource_id=resource_id,
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
    """Close pending approvals and unclaimed provider grants for a stopped request."""
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
            HitlRequest.status.in_((
                ApprovalStatus.PENDING.value,
                ApprovalStatus.GRANTED.value,
            )),
        ).with_for_update()
    )).scalars().all()
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        if wanted_ids and row.id not in wanted_ids:
            continue
        unclaimed_provider_grant = False
        if row.status == ApprovalStatus.GRANTED:
            context = dict(row.context or {})
            if (
                context.get("kind") != "provider"
                or isinstance(context.get("provider_action_claim"), dict)
            ):
                continue
            unclaimed_provider_grant = True
        requested_by = (row.context or {}).get("requested_by")
        if user_id and requested_by and requested_by != user_id:
            continue
        if unclaimed_provider_grant:
            # A provider grant is still reversible until execution claims it.
            # Reuse the strict terminal path so the ledger and visible card
            # cannot disagree with the authorization state.
            await _expire_provider_runtime_approval(
                db,
                row,
                reason,
                card_choice="cancelled",
            )
        else:
            row.status = ApprovalStatus.EXPIRED.value
            row.resolved_reason = reason
            row.decided_at = now_dt
            cancelled_ids.append(row.id)
        cancelled += 1

    if not cancelled:
        return 0
    from packages.core.services.hitl_requests import mark_hitl_request_resolved

    for hitl_id in cancelled_ids:
        await mark_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
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
    permission_decision: PermissionDecision,
) -> str | None:
    if not conversation_id:
        return json.dumps({"error": "approval_token_requires_conversation"})

    # ── unified store: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
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
            if req.status == ApprovalStatus.PENDING:
                # Still waiting on a person — nothing has gone wrong, the model
                # simply retried early. Returning a bare `approval_not_granted`
                # here produced the second half of the email incident: it is not
                # a `__hitl__` envelope, so no card was recorded, and the model
                # told the user "已重试，新的审批卡已经生成。请在新的 approval
                # card 上点 Approve" — a card that did not exist. Re-emit THIS
                # request's own envelope (same id, same stored render context,
                # so no duplicate request is minted) and the sentence becomes
                # true: the card is back, and answering it resolves the one
                # blocker rather than a fresh one.
                return _runtime_hitl_payload(
                    request_id=req.id,
                    action=action,
                    tool_name=tool_name,
                    arguments=arguments,
                    matched_rule=req.matched_rule,
                    render=ctx,
                )
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
                permission_decision=permission_decision,
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
        # The compatibility lookup above deliberately avoids locking every
        # granted request in the conversation. Lock the one exact candidate
        # now and re-check it after the lock: two workers may have observed the
        # same GRANTED snapshot, but only the first may spend it.
        matched = await _load_runtime_request(
            db,
            request_id=matches[0].id,
            entity_id=entity_id,
            conversation_id=conversation_id,
            for_update=True,
        )
        if matched is None:
            return APPROVAL_TOKEN_IGNORED
        matched_context = dict(matched.context or {})
        payload_still_matches = (
            matched.action_key == action.action_key
            and matched_context.get("tool") == tool_name
            and matched_context.get("args_hash") in accepted_hashes
        )
        if not payload_still_matches:
            return APPROVAL_TOKEN_IGNORED
        if matched.status != ApprovalStatus.GRANTED:
            if matched.status == ApprovalStatus.CONSUMED:
                return json.dumps({
                    "error": "approval_not_granted",
                    "status": "consumed",
                    "approval_token": matched.id,
                })
            return APPROVAL_TOKEN_IGNORED
        from packages.core.governance.approvals import consume_approval

        await consume_approval(db, matched)
        return None

    # Unknown token, no payload match — ignore it and let the normal gate
    # decide (tokens minted before the unified-store upgrade land here: the
    # gate re-asks with a fresh card).
    return APPROVAL_TOKEN_IGNORED
