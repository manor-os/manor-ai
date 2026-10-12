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

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from packages.core.ai.runtime.media_outputs import runtime_media_output_policy_resources
from packages.core.constants.approvals import (
    ApprovalConsumeOutcome,
    ApprovalOriginKind,
    ApprovalOutcome,
    ApprovalStatus,
)
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.ai.runtime.approval_classifier import (
    RuntimeToolClassification,
    RuntimeToolEffect,
    classify_runtime_tool,
)
from packages.core.ai.runtime.approval_messages import (
    CHROME_APPROVAL_CONTEXT_KEYS,
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
    one_time_approval_options,
    normalize_approval_choice,
)
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalAction,
)
from packages.core.ai.runtime.provider_approvals import (
    freeze_provider_approval_request,
    normalize_provider_approval,
    provider_approval_confirmation_receipt,
    provider_approval_is_expired,
    provider_approval_runtime_metadata,
)
from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
from packages.core.services.runtime_authorization import (
    AuthorizationRule,
    PermissionDecision,
)
from packages.core.services.task_comment_approval_origin import (
    TASK_COMMENT_ORIGIN_CONTEXT_KEY,
    TaskCommentApprovalOriginError,
    build_task_comment_origin,
    has_task_comment_origin,
    is_task_comment_runtime,
    lock_task_comment_workspace,
    task_comment_origin_matches_request,
    validate_task_comment_approval_origin,
)

logger = logging.getLogger(__name__)

APPROVAL_TOKEN_IGNORED = "__runtime_approval_token_ignored__"

_RUNTIME_ORIGIN_KIND = ApprovalOriginKind.TOOL_CALL.value
_TIKTOK_PUBLISH_TOOL = "mcp__tiktok__post_video"
_CHROME_PUBLICATION_ACTION = "chrome.publication.publish"


def _chrome_publication_review(arguments: dict[str, Any]) -> dict[str, Any] | None:
    intent = arguments.get("operation_intent")
    publication = intent.get("publication") if isinstance(intent, dict) else None
    if not isinstance(publication, dict):
        return None
    if not all(
        isinstance(publication.get(key), str) and publication[key].strip()
        for key in ("destination_url", "account")
    ) or not isinstance(publication.get("text"), str):
        return None
    document_ids = publication.get("document_ids")
    if not isinstance(document_ids, list) or any(
        not isinstance(document_id, str) or not document_id.strip()
        for document_id in document_ids
    ):
        return None
    return {key: publication[key] for key in ("destination_url", "account", "text", "document_ids")}


async def _chrome_publication_binding(
    db,
    *,
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    raise RuntimeApprovalContinuationError("Chrome publication is unavailable in this deployment.")


def _approval_consumption_blocked_result(
    request_id: str,
    outcome: ApprovalConsumeOutcome,
) -> str:
    """Stable fail-closed result for a lost one-time approval claim."""

    already_consumed = outcome is ApprovalConsumeOutcome.ALREADY_CONSUMED
    return json.dumps({
        "error": "approval_not_granted",
        "status": (
            ApprovalStatus.CONSUMED.value
            if already_consumed
            else ApprovalConsumeOutcome.NOT_GRANTED.value
        ),
        "approval_token": request_id,
        "message": (
            "This one-time approval was already used. No action was run."
            if already_consumed
            else "This approval is no longer granted. No action was run."
        ),
    }, ensure_ascii=False)


async def _set_direct_chat_always_approve_preference(db, *, req, user_id: str) -> None:
    raise ValueError("Always approval requires a versioned approval-settings command")


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


async def _automated_run_always_allowed(db, **kwargs: Any) -> bool:
    from packages.core.governance.service import automated_run_always_allowed

    return await automated_run_always_allowed(db, **kwargs)


def _is_automated_runtime_surface(runtime_surface: Any) -> bool:
    from packages.core.ai.runtime.surfaces import ChatSurface

    value = getattr(runtime_surface, "value", runtime_surface)
    return value in {
        ChatSurface.WORKFLOW_AGENT_STEP.value,
        ChatSurface.SCHEDULED_AGENT_RUN.value,
    }


def _is_workflow_runtime_surface(runtime_surface: Any) -> bool:
    from packages.core.ai.runtime.surfaces import ChatSurface

    value = getattr(runtime_surface, "value", runtime_surface)
    return value == ChatSurface.WORKFLOW_AGENT_STEP.value


_AUTOMATED_PROVIDER_HUMAN_BLOCKERS = frozenset({
    "browser_permission",
    "captcha",
    "login",
    "mfa",
    "password_change",
    "sensitive_data_transmission",
})


def _provider_requires_human(request: dict[str, Any]) -> bool:
    confirmation_mode = str(request.get("confirmation_mode") or "").strip().lower()
    policy_category = str(request.get("policy_category") or "").strip().lower()
    return bool(
        confirmation_mode == "handoff_required"
        or policy_category in _AUTOMATED_PROVIDER_HUMAN_BLOCKERS
    )


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
    runtime_surface: Any | None = None,
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
    workflow_scope_id = workflow_run_id if workspace_id and _is_workflow_runtime_surface(runtime_surface) else None
    if workspace_id and _is_workflow_runtime_surface(runtime_surface) and not workflow_scope_id:
        return result

    from packages.core.database import async_session
    from packages.core.governance.approvals import find_requests_by_dedup
    from packages.core.governance.service import check_step_policy
    async with async_session() as db:
        existing = await find_requests_by_dedup(db, entity_id=entity_id,
            dedup_key=f"provider:{provider}:{request.get('provider_approval_id')}")
        if existing or _provider_requires_human(request):
            return result
        current_policy = await check_step_policy(
            db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
            kind="action", action_key=action_key, resource_id=resource_id,
            capability_id=capability_id, risk_level="high", task_id=task_id,
            workflow_run_id=workflow_scope_id,
            # A normalized, machine-answerable provider gate represents an
            # external action; confirm/retry still cross runtime permissions.
            impact="external" if workflow_scope_id else None,
        )
        if not current_policy.allowed and not current_policy.pause_for_hitl:
            return result
        applicable = (current_policy.explanation or {}).get("target_decisions") or [current_policy.explanation or {}]
        if any(item.get("outcome") == "ask" and item.get("source") != "mode" for item in applicable):
            return result

    workflow_grant_id = str(workflow_action_grant_id or "").strip()
    if workflow_grant_id and _is_workflow_runtime_surface(runtime_surface):
        from packages.core.database import async_session
        from packages.core.models.workflow import WorkflowActionGrant
        from packages.core.services.workflow_action_grant_service import (
            YOUTUBE_PUBLICATION_GRANT_TYPE,
            WorkflowActionGrantDenied,
            consume_youtube_publication_grant,
            validate_youtube_publication_grant,
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
                        await validate_youtube_publication_grant(
                            db,
                            **workflow_scope,
                        )
                    )
                    if request_kind in {"publish", "save"}:
                        await consume_youtube_publication_grant(
                            db,
                            **workflow_scope,
                        )
                except WorkflowActionGrantDenied:
                    await db.rollback()
                    logger.warning(
                        "Workflow provider auto-confirm stopped: "
                        "stage=grant_denied tool=%s workflow_run_id=%s",
                        tool_name,
                        workflow_scope["workflow_run_id"],
                    )
                    return result

                try:
                    confirmation = await execute(
                        confirmation_tool,
                        dict(confirmation_arguments),
                    )
                    token = _provider_approval_token(confirmation)
                    if not token:
                        await db.rollback()
                        logger.warning(
                            "Workflow provider auto-confirm stopped: "
                            "stage=confirmation_token_missing tool=%s workflow_run_id=%s",
                            tool_name,
                            workflow_scope["workflow_run_id"],
                        )
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
                    logger.warning(
                        "Workflow provider auto-confirm stopped: "
                        "stage=retry_not_executed tool=%s workflow_run_id=%s",
                        tool_name,
                        workflow_scope["workflow_run_id"],
                    )
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
        from packages.core.governance.approvals import find_requests_by_dedup
        existing = await find_requests_by_dedup(db, entity_id=entity_id,
            dedup_key=f"provider:{provider}:{request.get('provider_approval_id')}")
        if existing or _provider_requires_human(request):
            return result
        granted = await _standing_grant(
            db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
            action_key=action_key, resource_id=resource_id, capability_id=capability_id,
            **({"workflow_run_id": workflow_scope_id} if workflow_scope_id else {}),
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
    db, *, entity_id: str, workspace_id: str | None, user_id: str | None,
    action_key: str, resource_id: str | None, capability_id: str,
    workflow_run_id: str | None = None,
) -> bool:
    from packages.core.governance.service import check_step_policy
    decision = await check_step_policy(
        db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
        kind="action", action_key=action_key, resource_id=resource_id,
        capability_id=capability_id, risk_level="high",
        workflow_run_id=workflow_run_id,
        impact="external" if workflow_run_id else None,
    )
    return decision.allowed


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


def _approval_args_hash_candidates(
    arguments: dict[str, Any], *, tool_name: str = "", context: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Hashes accepted for an approval retry.

    `confirm` is a tool/runtime control flag on several external-action
    tools. The user-facing payload being approved is the target content, not
    that boolean switch, so a retry that only adds `confirm=true` must still
    consume the original approval token. Non-boolean `confirm` values remain
    payload-bearing and are not ignored.
    """

    payloads = [arguments]
    if "confirm" in arguments and _boolish_confirmation_control(arguments.get("confirm")):
        payloads.append({key: value for key, value in arguments.items() if key != "confirm"})
    hashes = [approval_args_hash(payload, tool_name=tool_name) for payload in payloads]
    if tool_name.startswith("mcp__chrome__"):
        continuation = (context or {}).get("continuation")
        stored = continuation.get("arguments") if isinstance(continuation, dict) else None
        if (
            isinstance(stored, dict) and continuation.get("version") == 1
            and continuation.get("tool") == tool_name
        ):
            # Old cards hashed the previous lease. Reconstruct only those
            # Runtime-owned fields; all target/content values come from the
            # current call and must still match the persisted original hash.
            for payload in payloads:
                legacy = {key: value for key, value in payload.items() if key not in CHROME_APPROVAL_CONTEXT_KEYS}
                legacy.update({key: stored[key] for key in CHROME_APPROVAL_CONTEXT_KEYS if key in stored})
                hashes.append(approval_args_hash(legacy))
                # Cards minted before publication-level consent included the
                # old locator/expectation. Compare their frozen business input
                # before accepting that historical hash with a fresh locator.
                if approval_args_hash(stored, tool_name=tool_name) == approval_args_hash(payload, tool_name=tool_name):
                    hashes.extend((approval_args_hash(stored), approval_args_hash({
                        key: value for key, value in stored.items() if key not in CHROME_APPROVAL_CONTEXT_KEYS
                    })))
    return tuple(dict.fromkeys(hashes))


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


def _task_origin_conversation_matches(
    origin_task, *, conversation_id: str | None, task_id: str | None,
    runtime_envelope: Any | None = None,
) -> bool:
    """Return true when a tool call's conversation scope belongs to its origin Task.

    A foreground run presents the Task's own conversation.  A conversation-less
    worker run has no conversation row at all: its sandbox-bound tools present
    ``runtime_task_binding_key(task_id)`` as their authorization scope (see
    ``runtime_authorization_conversation_id``).  That key names the very Task
    this guard has already loaded and locked, so it is at least as tightly bound
    as a conversation match; any other foreign scope is still refused.
    """

    presented = str(conversation_id or "")
    if str(getattr(origin_task, "conversation_id", None) or "") == presented:
        return True
    if not presented and runtime_envelope is not None:
        from packages.core.ai.runtime.surfaces import ChatSurface

        surface = getattr(runtime_envelope, "surface", None)
        if (
            getattr(surface, "value", surface) == ChatSurface.SCHEDULED_AGENT_RUN.value
            and getattr(runtime_envelope, "conversation_id", None) is None
            and str(getattr(runtime_envelope, "task_id", "")) == str(task_id or "")
            and str(getattr(origin_task, "id", "")) == str(task_id or "")
        ):
            return True
    from packages.core.ai.runtime.sandbox import runtime_task_binding_key

    return bool(presented) and presented == runtime_task_binding_key(task_id or "")


def _runtime_request_matches_task_origin(req, task_id: str | None) -> bool:
    """Bind a one-time approval to exactly one execution plane.

    A direct-chat grant has no origin Task and may only be spent by direct
    chat.  A Task-origin grant may only be spent by that exact Task.  Keeping
    this check ahead of payload/status handling also prevents a foreign token
    from making the current transaction acquire (or fail) a second Task.
    """

    request_task_id = str(getattr(req, "origin_task_id", None) or "").strip()
    execution_task_id = str(task_id or "").strip()
    return request_task_id == execution_task_id


def _runtime_request_matches_step_origin(req, step_id: str | None) -> bool:
    """Prevent a Chat/other-step approval from being reused by this Step."""

    request_step_id = str(getattr(req, "origin_step_id", None) or "").strip()
    execution_step_id = str(step_id or "").strip()
    return request_step_id == execution_step_id


def _runtime_request_matches_comment_origin(req, origin) -> bool:
    if origin is None:
        return not has_task_comment_origin(req)
    context = req.context if isinstance(req.context, dict) else {}
    return (
        context.get(TASK_COMMENT_ORIGIN_CONTEXT_KEY) == origin
        and not any(key.startswith("task_runtime_") for key in context)
    )


async def _foreground_cannot_claim_worker_approval(db, *, request, runtime_envelope):
    if (
        not request.origin_task_id or has_task_comment_origin(request)
        or not is_task_comment_runtime(runtime_envelope)
    ):
        return False
    from packages.core.constants.task import TaskType
    from packages.core.models.task import Task

    # A valid signed worker checkpoint is still the wrong execution plane
    # for an ordinary foreground comment, even on the same canonical thread.
    return (await db.execute(select(Task.task_type).where(
        Task.id == request.origin_task_id, Task.entity_id == request.entity_id,
    ))).scalar_one_or_none() == TaskType.GENERAL.value


async def _stage_background_runtime_approval_resolution(db, *, request, **kwargs):
    from packages.core.services.workflow_runtime_approvals import workflow_approval_owned, stage_workflow_approval_resolution
    if workflow_approval_owned(request):
        return await stage_workflow_approval_resolution(db, request=request, **kwargs)
    if has_task_comment_origin(request):
        return None
    from packages.core.services.task_agent_loop_hitl import verified_request_tag

    if verified_request_tag(request) is not None:
        return await _stage_task_agent_loop_resolution(
            db, request=request, choice=kwargs["choice"],
        )
    from packages.core.services.task_runtime_approvals import (
        stage_task_runtime_approval_resolution,
    )
    return await stage_task_runtime_approval_resolution(db, request=request, **kwargs)


async def _stage_task_agent_loop_resolution(db, *, request, choice: str, reason: str | None = None):
    """Resume the Task agent loop parked on this approval (checked current earlier)."""

    from packages.core.services.task_agent_loop_hitl import (
        stage_task_agent_loop_approval_resolution,
    )
    from packages.core.services.task_runtime_approvals import TaskRuntimeApprovalError

    resolution = await stage_task_agent_loop_approval_resolution(
        db, request=request, choice=choice, reason=reason,
    )
    if resolution is None:
        raise TaskRuntimeApprovalError("Task agent loop is no longer parked on this approval")
    return resolution


def _runtime_approval_origin_mismatch_result(hitl_id: str) -> str:
    return json.dumps(
        {
            "error": "approval_origin_mismatch",
            "message": (
                "This approval belongs to a different execution origin. "
                "No action was run."
            ),
            "approval_token": hitl_id,
        },
        ensure_ascii=False,
    )


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
        "chrome_basic_approval_policy_v1": ctx.get("chrome_basic_approval_policy_v1"),
        "paths": ctx.get("paths"),
        "workspace": ctx.get("workspace"),
        "content": ctx.get("content"),
        "reason": req.reason,
        "matched_rule": req.matched_rule,
    }


def _delegated_runtime_approval_metadata(
    req,
    *,
    hitl_id: str,
    user_id: str,
    direct_metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """Wrap a validated child tool retry in its original service delegation."""

    context = dict(req.context or {})
    resume = context.get("delegation_resume")
    if resume is None:
        return direct_metadata
    if not isinstance(resume, dict) or resume.get("version") != 1:
        return None

    forced_calls = direct_metadata.get("forced_tool_calls")
    direct_call = direct_metadata.get("approved_tool_call") or (
        forced_calls[0]
        if isinstance(forced_calls, list)
        and len(forced_calls) == 1
        and isinstance(forced_calls[0], dict)
        else None
    )
    expected_scope = {
        "approval_token": hitl_id,
        "entity_id": req.entity_id,
        "workspace_id": req.workspace_id,
        "user_id": user_id,
        "conversation_id": req.origin_conversation_id,
    }
    tool_name = str(context.get("tool") or "").strip()
    if (
        direct_call is None
        or any(
            str(resume.get(key) or "") != str(value or "")
            for key, value in expected_scope.items()
        )
        or str(resume.get("tool_name") or "").strip() != tool_name
        or str(direct_call.get("name") or "").strip() != tool_name
        or not all(
            str(resume.get(key) or "").strip()
            for key in (
                "agent_subscription_id",
                "agent_id",
                "service_key",
                "prompt",
            )
        )
    ):
        return None

    return {
        "approval_kind": "runtime_delegation",
        "extra_tool_names": ["workspace_agent"],
        "forced_tool_calls": [
            {
                "name": "workspace_agent",
                "arguments": {
                    "action": "delegate_service",
                    "params": {
                        "agent_subscription_id": resume[
                            "agent_subscription_id"
                        ],
                        "approval_token": hitl_id,
                    },
                },
                "disable_followup_tools": True,
            }
        ],
        "approval_resume_guidance": (
            "Resume the approved delegated service call exactly once. The service "
            "Agent must execute the preserved tool operation and report its actual result."
        ),
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
    workspace_id: str | None = None,
    chrome_basic_approval_policy: dict[str, Any] | None = None,
    chrome_review_target_label: str | None = None,
) -> dict[str, Any]:
    """Everything the card and the retry message need, computed once and
    stored on the request's context at mint time."""
    from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_snapshot

    args_preview = approval_preview_arguments(arguments, tool_name=tool_name)
    media_plan = runtime_media_output_approval_snapshot()
    paths = media_plan["paths"] if media_plan else approval_paths(action, tool_name, arguments)
    workspace = None
    if conversation_id:
        conv = await load_runtime_approval_conversation(db, conversation_id, entity_id)
        if conv is not None:
            workspace = await runtime_approval_workspace_context(db, conv)
    public_content = chrome_review_target_label or approval_public_content(
        arguments if action.action_key == _CHROME_PUBLICATION_ACTION else args_preview
    )
    content_preview = approval_content_preview(public_content) if public_content else ""
    confirmation_receipt = _chrome_confirmation_receipt(tool_name, arguments)
    publish_review = {}
    if action.action_key == _CHROME_PUBLICATION_ACTION:
        review = _chrome_publication_review(arguments)
        binding = await _chrome_publication_binding(
            db, entity_id=entity_id, user_id=user_id,
            workspace_id=workspace_id, arguments=arguments,
        )
        publish_review = {
            "review": {**review, "documents": binding["documents"]},
            "chrome_publication_binding": binding,
        }
    if tool_name == _TIKTOK_PUBLISH_TOOL:
        from packages.core.services.tiktok_publish_review import prepare_tiktok_publish_review

        try:
            publish_review = await prepare_tiktok_publish_review(
                tool_name=tool_name,
                arguments=runtime_approval_continuation(tool_name, arguments)["arguments"],
                entity_id=entity_id,
                user_id=user_id, conversation_id=conversation_id,
                workspace_id=workspace_id,
            )
        except ValueError as exc:
            raise RuntimeApprovalContinuationError(str(exc)) from exc
    return {
        "tool": tool_name,
        "args_hash": approval_args_hash(arguments, tool_name=tool_name),
        "args_preview": args_preview,
        "retry_args": args_preview if not args_preview.get("truncated") else None,
        "continuation": runtime_approval_continuation(tool_name, arguments),
        "paths": paths,
        "workspace": workspace,
        "content": content_preview,
        **({"chrome_review_target_label": chrome_review_target_label}
           if chrome_review_target_label else {}),
        "requested_by": user_id,
        "media_output_plan": media_plan,
        **({"chrome_basic_approval_policy_v1": chrome_basic_approval_policy}
           if chrome_basic_approval_policy is not None else {}),
        **confirmation_receipt,
        **publish_review,
    }


def _chrome_basic_approval_policy_proof(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
    classification_arguments: dict[str, Any],
    runtime_metadata: Any,
) -> dict[str, Any] | None:
    """Persist server-derived ref facts without changing browser arguments."""
    expected_action = _chrome_ref_approval_action(action.action_key)
    if expected_action is None or not _same_chrome_ref_approval_action(action, expected_action):
        return None
    from packages.core.ai.runtime.chrome_tool_catalog import CHROME_TARGET_ACTIONS, normalize_chrome_call

    if normalize_chrome_call(tool_name, arguments)[0] not in CHROME_TARGET_ACTIONS:
        return None
    # chrome_policy_arguments only produces target semantics after binding the
    # ref to a current read_page snapshot. Do not mint a continuation proof
    # from labels supplied by the caller.
    if not isinstance(classification_arguments.get("target"), dict):
        return None
    ref = str(arguments.get("ref") or arguments.get("node_id") or "").strip()
    snapshot_id = str(arguments.get("snapshot_id") or arguments.get("snapshotId") or "").strip()
    tab_id = arguments.get("tabId")
    if not ref or not snapshot_id or not isinstance(tab_id, int) or isinstance(tab_id, bool):
        return None
    proof = {
        "version": 1,
        "tool_name": tool_name,
        "action_key": action.action_key,
        "operation": action.operation,
        "resource_kind": action.resource_kind,
        "capability_id": action.capability_id,
        "tab_id": tab_id,
        "snapshot_id": snapshot_id,
        "ref": ref,
    }
    state = (
        runtime_metadata.get("chrome_runtime_contract_v1")
        if isinstance(runtime_metadata, dict) else None
    )
    binding = state.get("latest_candidate_snapshot") if isinstance(state, dict) else None
    connection_id = str(binding.get("connection_id") or "").strip() if isinstance(binding, dict) else ""
    if connection_id:
        proof["connection_id"] = connection_id
    return proof


def _chrome_server_ref_target_label(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    classification_arguments: dict[str, Any],
    runtime_metadata: Any,
) -> str | None:
    """Project an observed Chrome ref label into review UI without mutating calls."""
    from packages.core.ai.runtime.chrome_tool_catalog import CHROME_TARGET_ACTIONS, normalize_chrome_call

    if normalize_chrome_call(tool_name, arguments)[0] not in CHROME_TARGET_ACTIONS:
        return None
    from packages.core.ai.runtime.chrome_routing import chrome_policy_arguments

    observed = chrome_policy_arguments(tool_name, arguments, runtime_metadata)
    target = classification_arguments.get("target")
    if not isinstance(target, dict) or observed.get("target") != target:
        return None
    label = str(target.get("label") or "").strip()
    return label[:240] or None


def _approved_chrome_basic_classification(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_metadata: Any,
) -> RuntimeToolClassification | None:
    """Recover an approved basic-ref classification in a fresh Runtime turn."""
    if not str(arguments.get("approval_token") or "").strip():
        return None
    from packages.core.ai.runtime.chrome_tool_catalog import CHROME_TARGET_ACTIONS, normalize_chrome_call

    if normalize_chrome_call(tool_name, arguments)[0] not in CHROME_TARGET_ACTIONS:
        return None
    metadata = runtime_metadata if isinstance(runtime_metadata, dict) else {}
    proof = metadata.get("chrome_basic_approval_policy_v1")
    if not isinstance(proof, dict) or proof.get("version") != 1:
        return None
    expected_action = _chrome_ref_approval_action(str(proof.get("action_key") or ""))
    if expected_action is None:
        return None
    expected = {
        "tool_name": tool_name,
        "action_key": expected_action.action_key,
        "operation": expected_action.operation,
        "resource_kind": expected_action.resource_kind,
        "capability_id": expected_action.capability_id,
        "tab_id": arguments.get("tabId"),
        "snapshot_id": str(arguments.get("snapshot_id") or arguments.get("snapshotId") or "").strip(),
        "ref": str(arguments.get("ref") or arguments.get("node_id") or "").strip(),
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        return None
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification

    return RuntimeToolClassification.action_call(expected_action)


async def _chrome_ref_approval_token_preflight_failed(
    db,
    *,
    approval_token: str,
    entity_id: str,
    conversation_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_metadata: Any,
) -> bool:
    """Fail closed before a stale ref token can take a lower-risk policy path."""
    request = await _load_runtime_request(
        db,
        request_id=approval_token,
        entity_id=entity_id,
        conversation_id=conversation_id,
    )
    context = request.context if request is not None and isinstance(request.context, dict) else {}
    stored_proof = context.get("chrome_basic_approval_policy_v1")
    if not isinstance(stored_proof, dict):
        return False
    if _chrome_ref_approval_action(str(stored_proof.get("action_key") or "")) is None:
        return False
    metadata = runtime_metadata if isinstance(runtime_metadata, dict) else {}
    if metadata.get("chrome_basic_approval_policy_v1") != stored_proof:
        return True
    if _approved_chrome_basic_classification(
        tool_name=tool_name,
        arguments=arguments,
        runtime_metadata=metadata,
    ) is None:
        return True
    return context.get("args_hash") not in _approval_args_hash_candidates(
        arguments,
        tool_name=tool_name,
        context=context,
    )


def _chrome_ref_approval_action(action_key: str) -> RuntimeApprovalAction | None:
    """Closed set of ref actions allowed to resume after a one-time review."""
    actions = {
        "chrome.browser.interact": RuntimeApprovalAction(
            "action", "chrome.browser.interact", "low", "interact with Chrome",
            "browser", "modify", capability_id="manor.composite",
        ),
        "chrome.ordinary_page_action.confirm": RuntimeApprovalAction(
            "action", "chrome.ordinary_page_action.confirm", "high", "confirm Chrome action",
            "external_account", "execute", capability_id="manor.composite",
        ),
        "chrome.representational_communication.confirm": RuntimeApprovalAction(
            "action", "chrome.representational_communication.confirm", "high", "confirm Chrome action",
            "external_account", "execute", capability_id="manor.composite",
        ),
    }
    return actions.get(action_key)


def _same_chrome_ref_approval_action(
    actual: RuntimeApprovalAction,
    expected: RuntimeApprovalAction,
) -> bool:
    return (
        actual.action_key == expected.action_key
        and actual.operation == expected.operation
        and actual.resource_kind == expected.resource_kind
        and actual.risk_level == expected.risk_level
        and actual.capability_id == expected.capability_id
    )


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
    chrome_target_label = str(render.get("chrome_review_target_label") or "").strip()
    prompt = runtime_approval_prompt(
        action,
        tool_name,
        {"title": chrome_target_label} if chrome_target_label else arguments,
    )
    confirmation_receipt = {
        key: render[key]
        for key in ("confirmation_mode", "policy_category", "preapproved")
        if key in render
    }
    publish_review = (
        {"review": render["review"]}
        if (tool_name == _TIKTOK_PUBLISH_TOOL or action.action_key == _CHROME_PUBLICATION_ACTION)
        and isinstance(render.get("review"), dict)
        else {}
    )
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
            "options": one_time_approval_options() if tool_name == _TIKTOK_PUBLISH_TOOL else approval_options(),
            **confirmation_receipt,
            **publish_review,
            **({"review_read_only": True} if action.action_key == _CHROME_PUBLICATION_ACTION else {}),
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
            **publish_review,
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
    operation_id: str | None = None,
    account_id: str | None = None,
    origin_task_id: str | None = None,
    origin_step_id: str | None = None,
    workflow_run_id: str | None = None,
    runtime_envelope: Any | None = None,
) -> str | None:
    """Re-evaluate an uncovered call without reopening a terminal operation.

    A new operation may require a fresh request; an existing terminal decision
    stays blocked and must not be projected as an actionable approval card.
    """
    from packages.core.governance.approvals import (
        ApprovalOrigin,
        ApprovalSubject,
        claim_approval_consumption,
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
            workspace_id=workspace_id,
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
                user_id=user_id, operation=action.operation, account_id=account_id,
                resource_ids=runtime_media_output_policy_resources(),
            ),
            origin=ApprovalOrigin(
                kind=_RUNTIME_ORIGIN_KIND,
                conversation_id=conversation_id,
                args_hash=approval_args_hash(arguments, tool_name=tool_name),
                operation_id=operation_id, task_id=origin_task_id, step_id=origin_step_id,
                workflow_run_id=workflow_run_id,
                context_loader=load_render_context,
            ),
            permission_decision=permission_decision,
            reason=reason,
            intrinsic_rule=matched_rule_hint or "approval_payload_changed",
            intrinsic_reason=reason,
            require_explicit_approval=(
                tool_name == _TIKTOK_PUBLISH_TOOL or action.action_key == _CHROME_PUBLICATION_ACTION
            ),
        )
    except RuntimeApprovalContinuationError as exc:
        return _runtime_continuation_error_result(
            tool_name=tool_name,
            error=exc,
        )
    if decision.outcome is ApprovalOutcome.ALLOW:
        if tool_name == _TIKTOK_PUBLISH_TOOL:
            if decision.request is None:
                return json.dumps({"error": "tiktok_publish_review_required"})
            return await consume_runtime_approval(
                db, conversation_id=conversation_id, entity_id=entity_id,
                user_id=user_id, hitl_id=decision.request.id, tool_name=tool_name,
                arguments=arguments, action=action, permission_decision=permission_decision,
                workflow_run_id=workflow_run_id, runtime_envelope=runtime_envelope,
            )
        if decision.request is not None:
            outcome = await claim_approval_consumption(db, decision.request)
            if outcome is not ApprovalConsumeOutcome.CONSUMED:
                return _approval_consumption_blocked_result(
                    decision.request.id,
                    outcome,
                )
        return None
    if decision.outcome is ApprovalOutcome.DENY:
        permission_denied = str(decision.matched_rule or "").startswith("permission.")
        denied = {
            "error": "blocked_by_permission" if permission_denied else "blocked_by_governance",
            "message": decision.reason or "This action is no longer authorized.",
            "action_key": action.action_key,
            "capability_id": action.capability_id,
            "matched_rule": decision.matched_rule,
            "tool": tool_name,
            "action_executed": False,
        }
        if decision.request is not None:
            denied.update(status=decision.request.status, approval_token=decision.request.id)
        return json.dumps(denied, ensure_ascii=False)
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
    step_id: str | None = None,
    runtime_envelope: Any | None = None,
    classification: RuntimeToolClassification | None = None,
    operation_id: str | None = None,
    account_id: str | None = None,
) -> str | None:
    """Return a blocking tool result, or ``None`` when execution may continue.

    Every classified effect first crosses the non-approvable infra permission
    boundary. Every business effect then uses the inherited policy; explicit
    human review and one-time continuations retain their own lifecycle checks.
    Approval tokens cannot survive role, membership, tenant-scope, or tool
    binding revocation.
    """
    from packages.core.ai.runtime.chrome_routing import chrome_policy_arguments

    if name == "search_tools":
        from packages.core.ai.runtime.tool_discovery import runtime_prepare_search_tools_request

        discovery = runtime_prepare_search_tools_request(**{
            key: arguments.get(key) for key in (
                "mode", "query", "queries", "provider_key", "exact_names", "max_results",
            )
        })
        if not discovery.ok:
            return json.dumps({
                "error": "tool_input_validation_failed", "message": discovery.error,
                "action_executed": False, "tool": name,
            })

    classification_arguments = chrome_policy_arguments(
        name, arguments, getattr(runtime_envelope, "metadata", None)
    )
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
            classification_arguments,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            workspace_file_scope=workspace_file_scope,
            declared_effect=(discovered.effect if discovered is not None else None),
        )
    unapproved_classification = classification
    approved_chrome_basic = _approved_chrome_basic_classification(
        tool_name=name,
        arguments=arguments,
        runtime_metadata=getattr(runtime_envelope, "metadata", None),
    )
    if approved_chrome_basic is not None:
        classification = approved_chrome_basic
    elif name.startswith("mcp__chrome__") and name not in {
        "mcp__chrome__run_sequence", "mcp__chrome__confirm_action", "mcp__chrome__upload",
    }:
        from packages.core.ai.runtime.approval_classifier import classify_chrome_call

        chrome_decision = classify_chrome_call(name, classification_arguments)
        if chrome_decision.kind in {"unsupported", "observation_required", "handoff"}:
            if str(arguments.get("approval_token") or "").strip():
                return json.dumps({
                    "error": "approval_payload_changed", "action_executed": False,
                    "message": "The approved Chrome call no longer has its exact bound target and effect.",
                })
            return json.dumps({
                "error": chrome_decision.reason or "chrome_handoff_required",
                "status": "read_page_required" if chrome_decision.kind == "observation_required" else "blocked",
                "action_executed": False,
                "message": (
                    "Read the current page and resolve the intended target before retrying. "
                    "Missing target evidence is not an approval request."
                    if chrome_decision.kind == "observation_required" else
                    "This Chrome action needs a supported operation or human interaction; approval cannot make it executable."
                ),
                **({"next_required_tool": "mcp__chrome__read_page"}
                   if chrome_decision.kind == "observation_required" else {}),
            })
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

        task_comment_origin = None
        if task_id:
            from sqlalchemy import select

            from packages.core.models.task import Task
            from packages.core.services.task_runtime_approvals import (
                lock_task_runtime_approval_boundary,
            )
            from packages.core.services.task_state_machine import TERMINAL_STATUSES

            if is_task_comment_runtime(runtime_envelope):
                try:
                    await lock_task_comment_workspace(
                        db, entity_id=entity_id, workspace_id=workspace_id,
                    )
                except TaskCommentApprovalOriginError:
                    return json.dumps({"error": "approval_origin_not_executable", "tool": name})
            await lock_task_runtime_approval_boundary(db, task_id=task_id)
            origin_task = (await db.execute(
                select(Task)
                .where(Task.id == task_id, Task.entity_id == entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if (
                origin_task is None
                or origin_task.status in TERMINAL_STATUSES
                or str(origin_task.workspace_id or "") != str(workspace_id or "")
            ):
                return json.dumps({
                    "error": "approval_origin_not_executable",
                    "message": (
                        "The origin Task is no longer executable. No action was run."
                    ),
                    "tool": name,
                }, ensure_ascii=False)
            from packages.core.constants.task import TaskType

            if (
                is_task_comment_runtime(runtime_envelope)
                and origin_task.task_type == TaskType.GENERAL.value
            ):
                try:
                    task_comment_origin = await build_task_comment_origin(
                        db, task=origin_task, conversation_id=conversation_id,
                        entity_id=entity_id, workspace_id=workspace_id,
                        user_id=user_id, runtime_envelope=runtime_envelope,
                    )
                except TaskCommentApprovalOriginError:
                    return json.dumps({"error": "approval_origin_not_executable", "tool": name})
            elif not _task_origin_conversation_matches(
                origin_task, conversation_id=conversation_id, task_id=task_id,
                runtime_envelope=runtime_envelope,
            ):
                return json.dumps({"error": "approval_origin_not_executable", "tool": name})

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
            # Deployment identity is server-owned runtime context, never a tool
            # argument. Retain it while rechecking current contextual grants.
            agent_subscription_id=(getattr(runtime_envelope, "metadata", None) or {}).get(
                "agent_subscription_id"
            ),
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

        workflow_execution = False
        workflow_run_id = None
        if _is_workflow_runtime_surface(runtime_surface):
            from packages.core.governance.service import workflow_run_is_executable

            workflow_run_id = arguments.get("_workflow_run_id_from_context")
            workflow_execution = await workflow_run_is_executable(
                db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                workflow_run_id=workflow_run_id,
            )
            if not workflow_execution:
                return json.dumps({
                    "error": "workflow_origin_not_executable", "action_executed": False,
                    "message": "The Workflow is missing, paused, stopped, or outside the current actor's scope.",
                    "tool": name,
                })

        if classification.effect is RuntimeToolEffect.ORCHESTRATOR:
            # This closed classification includes only invoke_skill and an
            # immutable leased action; both recheck each concrete child effect.
            return None
        if classification.effect is RuntimeToolEffect.CONTROL and authorization.action_key in {
            "runtime.control.submit_result", "runtime.control.sandbox_destroy",
        }:
            return None
        from packages.core.ai.runtime.approval_classifier import (
            runtime_policy_action_for_classification,
        )

        action, impact = runtime_policy_action_for_classification(classification)

        if not operation_id and not arguments.get("approval_token") and classification.effect is not RuntimeToolEffect.READ_ONLY:
            return json.dumps({"error": "approval_operation_identity_missing", "action_executed": False,
                "message": "This action requires a persisted runtime call identity.", "tool": name})

        provider_execution = _provider_runtime_execution_metadata(
            getattr(runtime_envelope, "metadata", None)
        )
        chrome_publication = (
            classification.action is not None
            and classification.action.action_key == _CHROME_PUBLICATION_ACTION
        )
        if chrome_publication and _chrome_publication_review(arguments) is None:
            return json.dumps({
                "error": "invalid_publication_intent",
                "message": "Final publication requires an exact destination, account, text and document_ids for review.",
                "tool": name,
            }, ensure_ascii=False)
        if (
            provider_execution is not None
            and name in {provider_execution[1], provider_execution[2]}
            and name != _TIKTOK_PUBLISH_TOOL
            and not chrome_publication
        ):
            from packages.core.governance.service import check_step_policy
            current_policy = await check_step_policy(
                db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                kind=action.kind, action_key=action.action_key,
                resource_kind=action.resource_kind, resource_id=action.resource_id,
                risk_level=action.risk_level, capability_id=action.capability_id,
                operation=action.operation, impact=impact, account_id=account_id, task_id=task_id,
                workflow_run_id=workflow_run_id,
            )
            if not current_policy.allowed and not current_policy.pause_for_hitl:
                return json.dumps({"error": "blocked_by_governance", "message": current_policy.reason})
            # Exact provider preflight still consumes its own continuation.
            return None

        if action is None:  # Defensive exhaustiveness for malformed plugins.
            return json.dumps({
                "error": "blocked_by_authorization_configuration",
                "message": "Action classification is missing approval metadata.",
                "matched_rule": "permission.tool.classification",
                "tool": name,
            }, ensure_ascii=False)

        approval_token = str(arguments.get("approval_token") or "").strip()
        if approval_token:
            if await _chrome_ref_approval_token_preflight_failed(
                db,
                approval_token=approval_token,
                entity_id=entity_id,
                conversation_id=conversation_id,
                tool_name=name,
                arguments=arguments,
                runtime_metadata=getattr(runtime_envelope, "metadata", None),
            ):
                return json.dumps({
                    "error": "approval_payload_changed",
                    "message": "The approved Chrome target changed. Review and approve again.",
                    "action_executed": False,
                }, ensure_ascii=False)
            from packages.core.governance.service import check_step_policy
            token_policy = await check_step_policy(
                db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                kind=action.kind, action_key=action.action_key, resource_id=action.resource_id,
                resource_kind=action.resource_kind, resource_ids=runtime_media_output_policy_resources(),
                risk_level=action.risk_level, capability_id=action.capability_id,
                operation=action.operation, impact=impact, account_id=account_id, task_id=task_id,
                workflow_run_id=workflow_run_id,
            )
            if not token_policy.allowed and not token_policy.pause_for_hitl:
                return json.dumps({"error": "blocked_by_governance", "tool": name,
                    "message": token_policy.reason, "matched_rule": token_policy.matched_rule})
            result = await consume_runtime_approval(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                task_id=task_id,
                hitl_id=approval_token,
                workflow_run_id=workflow_run_id,
                tool_name=name,
                arguments=arguments,
                action=action,
                impact=impact,
                permission_decision=permission_decision,
                runtime_envelope=runtime_envelope,
            )
            await db.commit()
            if result is None:
                arguments.pop("approval_token", None)
                return None
            if result == APPROVAL_TOKEN_IGNORED:
                arguments.pop("approval_token", None)
                approval_token = ""
                # A stale or mismatched token cannot retain the server-side
                # resume classification and fall through to browser default
                # allow. Re-enter ordinary policy with the conservative call.
                classification = unapproved_classification
                action, impact = runtime_policy_action_for_classification(classification)
                authorization = classification.authorization
                if authorization is None:
                    return json.dumps({
                        "error": "blocked_by_authorization_configuration",
                        "message": "Tool classification is missing hard-authorization metadata.",
                        "tool": name,
                    }, ensure_ascii=False)
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
                        if principal is not None else None
                    ),
                    tool_name=name,
                    bound_tool_names=bound_tool_names,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    runtime_surface=runtime_surface,
                    agent_subscription_id=(getattr(runtime_envelope, "metadata", None) or {}).get(
                        "agent_subscription_id"
                    ),
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
            else:
                return result

        from packages.core.governance.approvals import (
            ApprovalOrigin,
            ApprovalSubject,
            claim_approval_consumption,
            resolve_approval,
        )

        spent_credits_per_kind = None
        if workspace_id:
            from packages.core.budget import get_workspace_spent_credits_per_kind

            spent_credits_per_kind = await get_workspace_spent_credits_per_kind(db, workspace_id)

        async def load_render_context() -> dict[str, Any]:
            chrome_basic_approval_policy = _chrome_basic_approval_policy_proof(
                tool_name=name,
                arguments=arguments,
                action=action,
                classification_arguments=classification_arguments,
                runtime_metadata=getattr(runtime_envelope, "metadata", None),
            )
            chrome_review_target_label = _chrome_server_ref_target_label(
                tool_name=name,
                arguments=arguments,
                classification_arguments=classification_arguments,
                runtime_metadata=getattr(runtime_envelope, "metadata", None),
            )
            return await _runtime_render_context(
                db,
                conversation_id=conversation_id,
                entity_id=entity_id,
                user_id=user_id,
                tool_name=name,
                arguments=arguments,
                action=action,
                workspace_id=workspace_id,
                chrome_basic_approval_policy=chrome_basic_approval_policy,
                chrome_review_target_label=chrome_review_target_label,
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
                    requires_approval=False,
                    user_id=user_id, operation=action.operation, impact=impact, account_id=account_id,
                    resource_ids=runtime_media_output_policy_resources(),
                ),
                origin=ApprovalOrigin(
                    kind=_RUNTIME_ORIGIN_KIND,
                    conversation_id=conversation_id,
                    workflow_run_id=workflow_run_id,
                    # Thread the task so task-level runtime rules gate this plane
                    # too, and so task-terminal cleanup can expire these requests.
                    task_id=task_id,
                    step_id=step_id,
                    args_hash=approval_args_hash(arguments, tool_name=name),
                    operation_id=operation_id,
                    context=(
                        {TASK_COMMENT_ORIGIN_CONTEXT_KEY: task_comment_origin}
                        if task_comment_origin is not None else {}
                    ),
                    context_loader=load_render_context,
                ),
                permission_decision=permission_decision,
                spent_credits=spent_credits_per_kind,
                require_explicit_approval=name == _TIKTOK_PUBLISH_TOOL,

            )
        except RuntimeApprovalContinuationError as exc:
            return _runtime_continuation_error_result(
                tool_name=name,
                error=exc,
            )

        if decision.request is not None and (
            not _runtime_request_matches_task_origin(decision.request, task_id)
            or not _runtime_request_matches_step_origin(decision.request, step_id)
            or not _runtime_request_matches_comment_origin(
                decision.request, task_comment_origin,
            )
        ):
            return _runtime_approval_origin_mismatch_result(decision.request.id)

        if decision.outcome is ApprovalOutcome.ALLOW:
            if chrome_publication and decision.request is None:
                from packages.core.ai.runtime.authorization_receipts import (
                    ChromePublicationBinding, runtime_record_chrome_publication_binding,
                )

                try:
                    binding = await _chrome_publication_binding(
                        db, entity_id=entity_id, user_id=user_id,
                        workspace_id=workspace_id, arguments=arguments,
                    )
                except RuntimeApprovalContinuationError as exc:
                    return _runtime_continuation_error_result(tool_name=name, error=exc)
                runtime_record_chrome_publication_binding(ChromePublicationBinding(
                    connection_id=binding["connection_id"],
                    credential_revision=binding["credential_revision"],
                    connection_epoch=binding["connection_epoch"],
                ))
                return None
            if name == _TIKTOK_PUBLISH_TOOL or chrome_publication or (
                decision.request is not None and runtime_media_output_policy_resources()
            ):
                # Tokenless grants must revalidate operator consent or the
                # entire output plan at the same one-time consumption boundary.
                if decision.request is None:
                    return json.dumps({"error": "tiktok_publish_review_required"})
                result = await consume_runtime_approval(
                    db, conversation_id=conversation_id, entity_id=entity_id,
                    user_id=user_id, task_id=task_id, hitl_id=decision.request.id,
                    tool_name=name, arguments=arguments, action=action,
                    permission_decision=permission_decision, runtime_envelope=runtime_envelope,
                    workflow_run_id=workflow_run_id,
                    impact=impact,
                )
                await db.commit()
                return result
            # If the allow rests on a one-time operator grant, spend it NOW —
            # this return IS the point of irreversible proceed for the runtime
            # plane (the caller executes the tool next). Without this, a
            # token-less identical retry keeps finding the granted row and
            # "approve once" silently becomes approve-forever.
            if decision.request is not None:
                outcome = await claim_approval_consumption(db, decision.request)
                if outcome is not ApprovalConsumeOutcome.CONSUMED:
                    await db.commit()
                    return _approval_consumption_blocked_result(
                        decision.request.id,
                        outcome,
                    )
            await db.commit()
            return None

        if decision.outcome is ApprovalOutcome.DENY:
            permission_denied = str(decision.matched_rule or "").startswith(
                "permission."
            )
            denied: dict[str, Any] = {
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
            }
            if not permission_denied and workspace_id:
                # A governance block is a rule the operator can change. Name
                # it and hand back the exact soften patch, so the agent can
                # offer the fix instead of only reporting "rules forbid this".
                from packages.core.governance.hard_block_provenance import (
                    load_hard_block_denial_context,
                )

                try:
                    context = await load_hard_block_denial_context(
                        db,
                        workspace_id=workspace_id,
                        action_key=action.action_key,
                        capability_id=action.capability_id,
                        matched_rule=decision.matched_rule,
                    )
                except Exception:  # noqa: BLE001 — provenance is advisory
                    logger.warning("hard-block provenance lookup failed", exc_info=True)
                    context = {}
                if context:
                    denied["blocking_rules"] = context["blocking_rules"]
                    denied["fix"] = context["fix"]
                    denied["message"] = f"{denied['message']} {context['message']}"
            return json.dumps(denied, ensure_ascii=False)

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


async def _stage_task_runtime_no_action(
    db,
    *,
    request,
    user_id: str,
    conversation_id: str,
    hitl_id: str,
    reason: str,
    card_choice: str,
) -> dict[str, Any] | None:
    """Close a valid Task checkpoint through its normal non-execution resume."""

    from packages.core.services.workflow_runtime_approvals import workflow_approval_owned
    workflow_owned = workflow_approval_owned(request)
    if not workflow_owned and (not getattr(request, "origin_task_id", None) or has_task_comment_origin(request)):
        return None
    if request.status != ApprovalStatus.PENDING:
        return None
    from packages.core.governance.approvals import deny_approval
    from packages.core.services.task_runtime_approvals import (
        stage_task_runtime_approval_resolution,
    )

    await deny_approval(
        db,
        request,
        by_user_id=user_id,
        via="chat_card",
        reason=reason,
    )
    request.resolved_reason = reason
    await mark_runtime_hitl_request_resolved(
        db,
        conversation_id=conversation_id,
        hitl_id=hitl_id,
        choice=card_choice,
    )
    from packages.core.services.task_agent_loop_hitl import verified_request_tag

    if workflow_owned:
        from packages.core.services.workflow_runtime_approvals import stage_workflow_approval_resolution
        return await stage_workflow_approval_resolution(db, request=request, choice=APPROVAL_CHOICE_REJECT)
    if verified_request_tag(request) is not None:
        # The parked loop hears that the action did not run, and why.
        return await _stage_task_agent_loop_resolution(
            db, request=request, choice=APPROVAL_CHOICE_REJECT, reason=reason,
        )
    return await stage_task_runtime_approval_resolution(
        db,
        request=request,
        choice=APPROVAL_CHOICE_REJECT,
        runtime_metadata=None,
    )


async def resolve_runtime_approval_turn(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
    revision_request: str | None = None,
    submitted_review: Any = None,
) -> RuntimeApprovalResolution | None:
    """Resolve a runtime approval and expose any deterministic continuation."""
    # ── unified store first: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=False,
    )
    if req is not None:
        from packages.core.governance.approvals import (
            coordinator_outer_approval_id,
            is_coordinator_owned_nested_approval_durable,
        )

        if await is_coordinator_owned_nested_approval_durable(db, req):
            outer_id = coordinator_outer_approval_id(req)
            return RuntimeApprovalResolution(
                "This delegated action is controlled by its Task approval. "
                "Resolve the Task approval card so the exact child continuation "
                "and parent lease advance together.",
                {
                    "approval_kind": "coordinator_parent",
                    "approval_outcome": "pending",
                    "approval_request_id": outer_id,
                },
            )
    if req is not None and has_task_comment_origin(req):
        try:
            await lock_task_comment_workspace(
                db, entity_id=req.entity_id, workspace_id=req.workspace_id,
            )
        except TaskCommentApprovalOriginError:
            return RuntimeApprovalResolution(
                "This Task comment approval is no longer executable. No action was run.",
                {"approval_kind": "runtime_terminal", "approval_outcome": "blocked"},
            )
    if req is not None and getattr(req, "origin_task_id", None):
        from packages.core.services.task_runtime_approvals import (
            lock_task_runtime_approval_boundary,
        )

        await lock_task_runtime_approval_boundary(
            db,
            task_id=str(req.origin_task_id),
        )
    if req is not None:
        req = await _load_runtime_request(
            db,
            request_id=hitl_id,
            entity_id=entity_id,
            conversation_id=conversation_id,
            for_update=True,
        )
        # The coordinator marker may have been committed after the initial
        # compatibility lookup but before this locked refresh.  Recheck under
        # the row lock so a stale, already-rendered nested Chat card cannot win
        # the pre-link race and resolve independently of the parent Task card.
        if (
            req is not None
            and await is_coordinator_owned_nested_approval_durable(db, req)
        ):
            outer_id = coordinator_outer_approval_id(req)
            return RuntimeApprovalResolution(
                "This delegated action is controlled by its Task approval. "
                "Resolve the Task approval card so the exact child continuation "
                "and parent lease advance together.",
                {
                    "approval_kind": "coordinator_parent",
                    "approval_outcome": "pending",
                    "approval_request_id": outer_id,
                },
            )
    if req is not None:
        item = _runtime_request_item(req)
        is_provider = (req.context or {}).get("kind") == "provider"
        normalized = normalize_approval_choice(action)
        from packages.core.services.task_runtime_approvals import (
            existing_task_runtime_approval_resolution,
        )

        existing_task_resolution = existing_task_runtime_approval_resolution(req)
        if existing_task_resolution is None:
            from packages.core.services.task_agent_loop_hitl import (
                existing_task_agent_loop_resolution,
            )

            existing_task_resolution = existing_task_agent_loop_resolution(req)
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
            from packages.core.services.workflow_runtime_approvals import (
                WorkflowRuntimeApprovalError, workflow_approval_origin, ensure_workflow_approval_current,
            )
            workflow_owned = await workflow_approval_origin(db, req)
            if workflow_owned and req.status == ApprovalStatus.PENDING:
                try:
                    await ensure_workflow_approval_current(db, request=req)
                except WorkflowRuntimeApprovalError:
                    req.status = ApprovalStatus.EXPIRED.value
                    req.resolved_reason = "workflow_approval_not_current"
                    req.decided_at = datetime.now(timezone.utc)
                    await mark_runtime_hitl_request_resolved(db, conversation_id=conversation_id, hitl_id=hitl_id, choice="expired")
                    return RuntimeApprovalResolution(
                        "This Workflow approval is no longer current. No action was run.",
                        {"approval_kind": "runtime_terminal", "approval_outcome": "expired"},
                    )
            if has_task_comment_origin(req):
                try:
                    await validate_task_comment_approval_origin(db, request=req)
                except TaskCommentApprovalOriginError:
                    from packages.core.services.task_runtime_approvals import (
                        expire_task_runtime_approval_request,
                    )
                    await expire_task_runtime_approval_request(
                        db, request=req, reason="task_comment_origin_invalid",
                    )
                    return RuntimeApprovalResolution(
                        "This Task comment approval is no longer executable. No action was run.",
                        {"approval_kind": "runtime_terminal", "approval_outcome": "expired"},
                    )
            if existing_task_resolution is not None:
                existing_choice = existing_task_resolution["task_runtime_resume"][
                    "choice"
                ]
                return RuntimeApprovalResolution(
                    "This scheduled-agent action is already "
                    f"{existing_choice}; its prior decision was not changed.",
                    existing_task_resolution,
                )
            agent_loop_owned = False
            if not workflow_owned and getattr(req, "origin_task_id", None) and not has_task_comment_origin(req):
                from packages.core.services.task_agent_loop_hitl import (
                    task_agent_loop_approval_is_current,
                    task_agent_loop_owns_request,
                )

                agent_loop_owned = await task_agent_loop_owns_request(db, request=req)
            if agent_loop_owned:
                # A Task agent loop resumes only from the exact park this card
                # was shown for. Anything else (a stale card, a request the
                # loop never parked on) closes without touching the Task.
                if not await task_agent_loop_approval_is_current(db, request=req):
                    from packages.core.services.task_runtime_approvals import (
                        expire_task_runtime_approval_request,
                    )

                    await expire_task_runtime_approval_request(
                        db, request=req, reason="agent_loop_approval_not_current",
                    )
                    return RuntimeApprovalResolution(
                        "This Task approval is no longer current: the Task is not "
                        "waiting on it. No action was run.",
                        {
                            "approval_kind": "runtime_terminal",
                            "approval_outcome": "expired",
                        },
                    )
            elif not workflow_owned and getattr(req, "origin_task_id", None) and not has_task_comment_origin(req):
                from packages.core.services.task_runtime_approvals import (
                    TaskRuntimeApprovalError,
                    ensure_task_runtime_approval_resolvable,
                    fail_task_runtime_approval_origin,
                )

                try:
                    await ensure_task_runtime_approval_resolvable(
                        db,
                        request=req,
                    )
                except TaskRuntimeApprovalError:
                    await fail_task_runtime_approval_origin(
                        db,
                        request=req,
                        reason="runtime_approval_checkpoint_invalid",
                    )
                    return RuntimeApprovalResolution(
                        "This scheduled-agent approval is no longer attached "
                        "to a valid waiting Task. No action was run.",
                        {
                            "approval_kind": "runtime_terminal",
                            "approval_outcome": "expired",
                        },
                    )
        if req.status != ApprovalStatus.PENDING and not actionable_provider_grant:
            status_label = {"granted": "approved", "denied": "rejected"}.get(
                req.status, req.status
            )
            return RuntimeApprovalResolution(
                f"Runtime approval {hitl_id} is already {status_label}. "
                "Do not retry the blocked tool call."
            )
        if (req.context or {}).get("tool") == _TIKTOK_PUBLISH_TOOL:
            if normalized not in {APPROVAL_CHOICE_APPROVE, APPROVAL_CHOICE_REJECT}:
                return RuntimeApprovalResolution(
                    "TikTok publishing requires review of this one post. Choose approve or reject.",
                    {"approval_kind": "runtime_terminal", "approval_outcome": "pending"},
                )
            if normalized == APPROVAL_CHOICE_APPROVE:
                context = dict(req.context or {})
                if (
                    context.get("requested_by") != user_id
                    or context.get("tiktok_review_refreshed") is not True
                    or not isinstance(submitted_review, dict)
                    or not isinstance(context.get("tiktok_publish_evidence"), dict)
                ):
                    return RuntimeApprovalResolution(
                        "Open the TikTok review card, choose the publishing settings, "
                        "and confirm there. This request remains pending; no post was sent.",
                        {"approval_kind": "runtime_terminal", "approval_outcome": "pending"},
                    )
                from packages.core.services.tiktok_publish_review import (
                    _arguments_hash, apply_tiktok_publish_review,
                )

                try:
                    original = (context.get("continuation") or {}).get("arguments")
                    evidence = context["tiktok_publish_evidence"]
                    review = context.get("review")
                    if (
                        not isinstance(original, dict) or not isinstance(review, dict)
                        or evidence.get("original_args_sha256") != _arguments_hash(original)
                        or evidence.get("review_id") != review.get("review_id")
                    ):
                        raise ValueError("The original media or account changed; refresh this review")
                    final_arguments = apply_tiktok_publish_review(
                        review, submitted_review, original,
                    )
                    continuation = runtime_approval_continuation(_TIKTOK_PUBLISH_TOOL, final_arguments)
                except (ValueError, TypeError, RuntimeApprovalContinuationError) as exc:
                    return RuntimeApprovalResolution(
                        f"TikTok review was not accepted: {exc}. This request remains pending.",
                        {"approval_kind": "runtime_terminal", "approval_outcome": "pending"},
                    )
                context.update({
                    "continuation": continuation,
                    "args_hash": approval_args_hash(final_arguments),
                    "args_preview": approval_preview_arguments(final_arguments),
                    "retry_args": continuation["arguments"],
                    "tiktok_publish_consent": {
                        "version": 1,
                        "schema_version": 1,
                        "approval_id": req.id,
                        "evidence": context["tiktok_publish_evidence"],
                        "fields": continuation["arguments"],
                        "submitted_review": json.loads(json.dumps(submitted_review, allow_nan=False)),
                    },
                })
                req.context = context
                item = _runtime_request_item(req)
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
            task_resolution = await _stage_task_runtime_no_action(
                db,
                request=req,
                user_id=user_id,
                conversation_id=conversation_id,
                hitl_id=hitl_id,
                reason="revision_requested",
                card_choice=normalized,
            )
            if task_resolution is not None:
                return RuntimeApprovalResolution(
                    "Revision requested. The blocked scheduled action will not run; "
                    "start a new run after updating its instructions.",
                    task_resolution,
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
                    task_resolution = await _stage_task_runtime_no_action(
                        db,
                        request=req,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        hitl_id=hitl_id,
                        reason="provider_approval_expired",
                        card_choice="expired",
                    )
                    if task_resolution is not None:
                        return RuntimeApprovalResolution(
                            "The provider approval expired. The scheduled action "
                            "will not run.",
                            task_resolution,
                        )
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
                    task_resolution = await _stage_task_runtime_no_action(
                        db,
                        request=req,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        hitl_id=hitl_id,
                        reason="approval_continuation_unavailable",
                        card_choice="expired",
                    )
                    if task_resolution is not None:
                        return RuntimeApprovalResolution(
                            "The provider continuation is unavailable. The "
                            "scheduled action will not run.",
                            task_resolution,
                        )
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
                task_resolution = await _stage_background_runtime_approval_resolution(
                    db,
                    request=req,
                    choice=normalized,
                    runtime_metadata=provider_runtime_metadata,
                )
                if task_resolution is not None:
                    await mark_runtime_hitl_request_resolved(
                        db,
                        conversation_id=conversation_id,
                        hitl_id=hitl_id,
                        choice=normalized,
                    )
                    return RuntimeApprovalResolution(
                        "Approved. The scheduled agent will resume the exact "
                        "blocked action.",
                        task_resolution,
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
                task_resolution = await _stage_task_runtime_no_action(
                    db,
                    request=req,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    reason="approval_continuation_unavailable",
                    card_choice="expired",
                )
                if task_resolution is not None:
                    return RuntimeApprovalResolution(
                        "The stored continuation is unavailable. The scheduled "
                        "action will not run.",
                        task_resolution,
                    )
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

            forced_call = runtime_metadata.get("approved_tool_call") or runtime_metadata["forced_tool_calls"][0]
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
                task_resolution = await _stage_task_runtime_no_action(
                    db,
                    request=req,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    reason="approved_tool_schema_unavailable",
                    card_choice="expired",
                )
                if task_resolution is not None:
                    return RuntimeApprovalResolution(
                        "The tool schema is unavailable. The scheduled action "
                        "will not run.",
                        task_resolution,
                    )
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
                task_resolution = await _stage_task_runtime_no_action(
                    db,
                    request=req,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    reason="approved_tool_input_invalid",
                    card_choice="expired",
                )
                if task_resolution is not None:
                    return RuntimeApprovalResolution(
                        "The stored tool input is invalid. The scheduled action "
                        "will not run.",
                        task_resolution,
                    )
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

            runtime_metadata = _delegated_runtime_approval_metadata(
                req,
                hitl_id=hitl_id,
                user_id=user_id,
                direct_metadata=runtime_metadata,
            )
            if runtime_metadata is None:
                task_resolution = await _stage_task_runtime_no_action(
                    db,
                    request=req,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    reason="delegated_approval_continuation_unavailable",
                    card_choice="expired",
                )
                if task_resolution is not None:
                    return RuntimeApprovalResolution(
                        "The delegated continuation is unavailable. The scheduled "
                        "action will not run.",
                        task_resolution,
                    )
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = (
                    "delegated_approval_continuation_unavailable"
                )
                req.decided_at = datetime.now(UTC)
                await mark_runtime_hitl_request_resolved(
                    db,
                    conversation_id=conversation_id,
                    hitl_id=hitl_id,
                    choice="expired",
                )
                return RuntimeApprovalResolution(
                    "The approved action could not be resumed because its delegated "
                    "continuation is invalid. Please issue the action again."
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
            task_resolution = await _stage_background_runtime_approval_resolution(
                db,
                request=req,
                choice=normalized,
                runtime_metadata=runtime_metadata,
            )
            if task_resolution is not None:
                return RuntimeApprovalResolution(
                    "Approved. The scheduled agent will resume the exact "
                    "blocked action.",
                    task_resolution,
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
            task_resolution = await _stage_background_runtime_approval_resolution(
                db,
                request=req,
                choice=normalized,
                runtime_metadata=None,
            )
            if task_resolution is not None:
                return RuntimeApprovalResolution(
                    "Rejected. The scheduled agent will not run the blocked action.",
                    task_resolution,
                )
            return RuntimeApprovalResolution(
                runtime_approval_rejected_message(item),
                {
                    "approval_kind": "runtime_terminal",
                    "approval_outcome": "rejected",
                },
            )
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


def _provider_retry_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return provider arguments without Runtime-owned execution context."""

    return {
        key: value
        for key, value in arguments.items()
        if key not in RUNTIME_TOOL_CONTEXT_KEYS
        and key not in {"control_epoch", "controlEpoch"}
    }


def _provider_private_value_hash(value: Any) -> str:
    """Hash private diagnostic material without exposing the original value."""

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


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
    task_id: str | None = None,
    tool_name: str,
    arguments: dict[str, Any] | None,
    runtime_envelope=None,
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

    if task_id and is_task_comment_runtime(runtime_envelope):
        try:
            await lock_task_comment_workspace(
                db, entity_id=entity_id, workspace_id=runtime_envelope.workspace_id,
            )
        except TaskCommentApprovalOriginError:
            return ProviderRuntimeApprovalExecutionPreflight(
                result=_provider_execution_blocked_result(
                    hitl_id=hitl_id, status="invalid",
                    reason="The Task comment Workspace is no longer available.",
                )
            )
    if task_id:
        from packages.core.services.task_runtime_approvals import (
            lock_task_runtime_approval_boundary,
        )

        await lock_task_runtime_approval_boundary(db, task_id=task_id)

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
    if not _runtime_request_matches_task_origin(req, task_id):
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="invalid",
                reason=(
                    "This provider approval belongs to a different execution "
                    "origin. No external action was executed."
                ),
            )
        )

    if await _foreground_cannot_claim_worker_approval(
        db, request=req, runtime_envelope=runtime_envelope,
    ):
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id, status="invalid",
                reason="This provider approval belongs to background Task execution.",
            )
        )
    if has_task_comment_origin(req):
        try:
            await validate_task_comment_approval_origin(
                db, request=req, runtime_envelope=runtime_envelope, require_runtime=True,
            )
        except TaskCommentApprovalOriginError:
            return ProviderRuntimeApprovalExecutionPreflight(
                result=_provider_execution_blocked_result(
                    hitl_id=hitl_id, status="invalid",
                    reason="The Task comment approval origin is no longer executable.",
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

    from packages.core.governance.approvals import _consent_requirements
    from packages.core.governance.service import check_step_policy
    current_policy = await check_step_policy(
        db, entity_id=entity_id, workspace_id=req.workspace_id, user_id=context.get("requested_by"),
        kind="action", action_key=req.action_key, resource_id=req.resource_id,
        resource_kind=req.resource_kind, capability_id=req.capability_id, risk_level=req.risk_level,
        account_id=context.get("approval_account_id"), task_id=task_id,
    )
    if ((not current_policy.allowed and not current_policy.pause_for_hitl)
        or not set(_consent_requirements(current_policy)).issubset(set(context.get("approval_requirements") or []))):
        await _expire_provider_runtime_approval(db, req, "approval_requirements_changed")
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(hitl_id=hitl_id, status="expired",
                reason="Approval requirements changed. Start a new action for review before executing."),
            changed=True,
        )

    task_marker_changed = False
    if getattr(req, "origin_task_id", None) and not has_task_comment_origin(req):
        from packages.core.services.task_runtime_approvals import (
            TaskRuntimeApprovalError,
            claim_task_runtime_approval_execution,
            expire_task_runtime_approval_request,
        )

        try:
            task_marker_changed = await claim_task_runtime_approval_execution(
                db,
                request=req,
                task_id=task_id,
            )
        except TaskRuntimeApprovalError as exc:
            changed = await expire_task_runtime_approval_request(
                db,
                request=req,
                reason="runtime_approval_origin_not_executable",
            )
            return ProviderRuntimeApprovalExecutionPreflight(
                result=_provider_execution_blocked_result(
                    hitl_id=hitl_id,
                    status="expired",
                    reason=f"{exc}. No external action was executed.",
                ),
                changed=changed,
            )

        # ``claim_task_runtime_approval_execution`` adds the signed durable
        # standard-action attempt to request.context.  Continue from that
        # refreshed mapping so provider receipt/claim writes cannot clobber it.
        context = dict(req.context or {})

    receipt = context.get("provider_confirmation_receipt")
    receipt_matches = _provider_confirmation_receipt_matches(
        receipt,
        provider=context.get("provider"),
        provider_approval_id=context.get("provider_approval_id"),
    )
    if tool_name == confirmation_tool:
        if not receipt_matches:
            return ProviderRuntimeApprovalExecutionPreflight(
                changed=task_marker_changed
            )
        return ProviderRuntimeApprovalExecutionPreflight(
            result=json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "approvalId": receipt["provider_approval_id"],
                    "approvalToken": receipt["approval_token"],
                },
                ensure_ascii=False,
            ),
            changed=task_marker_changed,
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
    expected_token = str(receipt.get("approval_token") or "").strip()
    if supplied_token != expected_token:
        logger.warning(
            "Provider retry token mismatch: approval_id=%s "
            "supplied_token_hash=%s expected_token_hash=%s",
            hitl_id,
            _provider_private_value_hash(supplied_token),
            _provider_private_value_hash(expected_token),
        )
        await _expire_provider_runtime_approval(
            db,
            req,
            "provider_retry_token_mismatch",
        )
        return ProviderRuntimeApprovalExecutionPreflight(
            result=_provider_execution_blocked_result(
                hitl_id=hitl_id,
                status="expired",
                reason="The provider retry no longer matches the approved action.",
            ),
            changed=True,
        )

    supplied_payload = _provider_retry_payload(supplied_arguments)
    expected_payload = (
        _provider_retry_payload(expected_arguments)
        if isinstance(expected_arguments, dict)
        else {}
    )
    if not isinstance(expected_arguments, dict) or supplied_payload != expected_payload:
        logger.warning(
            "Provider retry payload mismatch: approval_id=%s "
            "supplied_args_hash=%s expected_args_hash=%s",
            hitl_id,
            approval_args_hash(supplied_payload),
            approval_args_hash(expected_payload),
        )
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
    task_id: str | None = None,
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: Any,
    runtime_envelope=None,
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

    if task_id and is_task_comment_runtime(runtime_envelope):
        try:
            await lock_task_comment_workspace(
                db, entity_id=entity_id, workspace_id=runtime_envelope.workspace_id,
            )
        except TaskCommentApprovalOriginError:
            return False
    if task_id:
        from packages.core.services.task_runtime_approvals import (
            lock_task_runtime_approval_boundary,
        )

        await lock_task_runtime_approval_boundary(db, task_id=task_id)

    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
    )
    if req is None or (req.context or {}).get("kind") != "provider":
        return False
    if not _runtime_request_matches_task_origin(req, task_id):
        return False
    if await _foreground_cannot_claim_worker_approval(
        db, request=req, runtime_envelope=runtime_envelope,
    ):
        return False
    if has_task_comment_origin(req):
        try:
            await validate_task_comment_approval_origin(
                db, request=req, runtime_envelope=runtime_envelope, require_runtime=True,
            )
        except TaskCommentApprovalOriginError:
            return False
    blocking_hitl = parse_hitl_envelope(result)
    provider_gate = normalize_provider_approval(tool_name, arguments, result)
    if blocking_hitl is not None or provider_gate is not None:
        if req.status != ApprovalStatus.GRANTED:
            return False
        context = dict(req.context or {})
        same_request = (
            blocking_hitl is not None
            and str(blocking_hitl.get("approval_token") or "") == req.id
        ) or (
            provider_gate is not None
            and provider_gate.get("provider") == context.get("provider")
            and provider_gate.get("provider_approval_id") == context.get("provider_approval_id")
            and approval_args_hash(provider_gate.get("retry_arguments")) == context.get("args_hash")
        )
        if same_request:
            # The provider proved this attempt is still gated, not ambiguous
            # completion. Keep the original card recoverable, but do not replay
            # its rejected confirmation token or retain an in-flight claim.
            context.pop("provider_action_claim", None)
            context.pop("provider_confirmation_receipt", None)
            req.context = context
            req.consumed_at = None
            req.resolved_reason = "provider_action_waiting_approval"
        else:
            # A different gate owns the next decision. Retire the old one-time
            # grant without consuming it; the new request remains pending.
            await _expire_provider_runtime_approval(
                db, req, "provider_action_requires_new_approval",
            )
        return True
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
    task_id: str | None = None,
    runtime_envelope=None,
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
    task_comment_origin = None
    if task_id and is_task_comment_runtime(runtime_envelope):
        from packages.core.models.task import Task

        await lock_task_comment_workspace(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )
        task = (await db.execute(select(Task).where(
            Task.id == task_id, Task.entity_id == entity_id,
        ))).scalar_one_or_none()
        from packages.core.constants.task import TaskType

        if task is None:
            raise TaskCommentApprovalOriginError("Task comment origin is unavailable")
        if task.task_type == TaskType.GENERAL.value:
            task_comment_origin = await build_task_comment_origin(
                db, task=task, conversation_id=conversation_id, entity_id=entity_id,
                workspace_id=workspace_id, user_id=user_id, runtime_envelope=runtime_envelope,
            )

    from packages.core.governance.approvals import _consent_requirements
    from packages.core.governance.service import check_step_policy
    current_policy = await check_step_policy(
        db, entity_id=entity_id, workspace_id=workspace_id, user_id=user_id, kind="action",
        action_key=action_key, resource_id=resource_id, capability_id=capability_id,
        risk_level="high", task_id=task_id,
    )
    if not current_policy.allowed and not current_policy.pause_for_hitl:
        return None

    dedup_key = f"provider:{provider}:{provider_approval_id}"
    existing = await find_requests_by_dedup(
        db, entity_id=entity_id, dedup_key=dedup_key,
    )
    if existing:
        if any(
            row.origin_conversation_id != conversation_id
            or not _runtime_request_matches_task_origin(row, task_id)
            or not _runtime_request_matches_comment_origin(row, task_comment_origin)
            for row in existing
        ):
            return None
        pending = next((r for r in existing if r.status == ApprovalStatus.PENDING), None)
        if pending is None:
            return None
        return _provider_hitl_data(pending.id, _provider_item_from_request(pending))

    if not _provider_requires_human(request) and _provider_supports_always_approve(provider) and current_policy.allowed:
        return None

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
            task_id=task_id,
            operation_id=dedup_key,
            args_hash=approval_args_hash(retry_arguments),
            context={
                "approval_requirements": _consent_requirements(current_policy),
                "policy_evaluation": current_policy.explanation,
                **(
                    {TASK_COMMENT_ORIGIN_CONTEXT_KEY: task_comment_origin}
                    if task_comment_origin is not None else {}
                ),
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
    if (
        req.origin_conversation_id != conversation_id
        or not _runtime_request_matches_task_origin(req, task_id)
        or not _runtime_request_matches_comment_origin(req, task_comment_origin)
    ):
        return None
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
    """Close direct-Chat and foreground-comment approvals for a stopped turn.

    Background Task approvals have an independent durable continuation and Task
    lifecycle. A conversation-wide Chat stop must never revoke those rows.
    Task cancellation goes through the Task runtime-approval boundary instead.
    """
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

    candidates = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
            HitlRequest.origin_kind == _RUNTIME_ORIGIN_KIND,
            HitlRequest.status.in_((
                ApprovalStatus.PENDING.value,
                ApprovalStatus.GRANTED.value,
            )),
        )
    )).scalars().all())
    foreground = [row for row in candidates if task_comment_origin_matches_request(row)]
    if foreground:
        from packages.core.services.task_runtime_approvals import (
            lock_task_runtime_approval_boundary,
        )

        for workspace_id in sorted({row.workspace_id for row in foreground}):
            try:
                await lock_task_comment_workspace(
                    db, entity_id=entity_id, workspace_id=workspace_id,
                )
            except TaskCommentApprovalOriginError:
                # Lifecycle is already closed; this Chat operation need not
                # acquire the worker's Task locks or reopen any authority.
                return 0
        for task_id in sorted({row.origin_task_id for row in foreground}):
            await lock_task_runtime_approval_boundary(db, task_id=task_id)
    eligible_ids = [
        row.id for row in candidates
        if not row.origin_task_id or task_comment_origin_matches_request(row)
    ]
    rows = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id.in_(eligible_ids),
            HitlRequest.status.in_((ApprovalStatus.PENDING.value, ApprovalStatus.GRANTED.value)),
        ).with_for_update().execution_options(populate_existing=True)
    )).scalars().all()
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        if row.origin_task_id and not task_comment_origin_matches_request(row):
            continue
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


async def restore_unexecuted_chrome_publication_approval(db, *, receipt, result: Any) -> bool:
    """Keep one human decision when the browser proves no action occurred.

    Consumption still claims the attempt before dispatch. A late/duplicate
    response cannot release a later attempt because consumed_at must match.
    Workflow authorization has no human grant to restore.
    """
    binding = receipt.chrome_publication_binding
    if binding is None or not binding.approval_id or binding.consumed_at is None:
        return False
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("action_executed") is not False:
        return False
    req = await _load_runtime_request(
        db, request_id=binding.approval_id, entity_id=receipt.entity_id,
        conversation_id=receipt.conversation_id, for_update=True,
    )
    if (
        req is None or req.status != ApprovalStatus.CONSUMED
        or req.consumed_at != binding.consumed_at
        or req.action_key != _CHROME_PUBLICATION_ACTION
        or (req.context or {}).get("requested_by") != receipt.user_id
    ):
        return False
    req.status = ApprovalStatus.GRANTED.value
    req.consumed_at = None
    req.resolved_reason = "chrome_publication_not_executed"
    return True


async def consume_runtime_approval(
    db,
    *,
    conversation_id: str | None,
    entity_id: str,
    user_id: str,
    task_id: str | None = None,
    hitl_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    action: RuntimeApprovalAction,
    permission_decision: PermissionDecision,
    runtime_envelope=None,
    workflow_run_id: str | None = None,
    impact: str | None = None,
) -> str | None:
    if not conversation_id:
        return json.dumps({"error": "approval_token_requires_conversation"})

    # The caller passes its host-validated identity separately from tool args.
    # Recheck it at consumption, including compatibility/tokenless paths.
    if workflow_run_id:
        from packages.core.governance.service import workflow_run_is_executable

        if not _is_workflow_runtime_surface(getattr(runtime_envelope, "surface", None)) or not await workflow_run_is_executable(
            db, entity_id=entity_id, workspace_id=getattr(runtime_envelope, "workspace_id", None),
            user_id=user_id, workflow_run_id=workflow_run_id,
        ):
            return json.dumps({"error": "workflow_origin_not_executable", "action_executed": False})

    if task_id and is_task_comment_runtime(runtime_envelope):
        try:
            await lock_task_comment_workspace(
                db, entity_id=entity_id, workspace_id=runtime_envelope.workspace_id,
            )
        except TaskCommentApprovalOriginError:
            return json.dumps({"error": "approval_origin_not_executable"})
    if task_id:
        from packages.core.services.task_runtime_approvals import (
            lock_task_runtime_approval_boundary,
        )

        await lock_task_runtime_approval_boundary(db, task_id=task_id)

    # ── unified store: the token is a HitlRequest id ──
    req = await _load_runtime_request(
        db,
        request_id=hitl_id,
        entity_id=entity_id,
        conversation_id=conversation_id,
        for_update=True,
    )
    if req is not None:
        if not _runtime_request_matches_task_origin(req, task_id):
            return _runtime_approval_origin_mismatch_result(hitl_id)
        if await _foreground_cannot_claim_worker_approval(
            db, request=req, runtime_envelope=runtime_envelope,
        ):
            return _runtime_approval_origin_mismatch_result(hitl_id)
        if has_task_comment_origin(req):
            try:
                await validate_task_comment_approval_origin(
                    db, request=req, runtime_envelope=runtime_envelope, require_runtime=True,
                )
            except TaskCommentApprovalOriginError:
                return json.dumps({"error": "approval_origin_not_executable"})
        from packages.core.services.workflow_runtime_approvals import (
            WorkflowRuntimeApprovalError, workflow_approval_origin, validate_workflow_approval_consumption,
        )
        workflow_owned = await workflow_approval_origin(db, req)
        if workflow_owned:
            try:
                await validate_workflow_approval_consumption(db, request=req, arguments=arguments, runtime_envelope=runtime_envelope)
            except WorkflowRuntimeApprovalError:
                return json.dumps({"error": "workflow_approval_origin_not_executable", "action_executed": False})
        ctx = dict(req.context or {})
        if ctx.get("tool") != tool_name or req.action_key != action.action_key:
            return APPROVAL_TOKEN_IGNORED
        stored_basic_proof = ctx.get("chrome_basic_approval_policy_v1")
        if isinstance(stored_basic_proof, dict):
            envelope_metadata = getattr(runtime_envelope, "metadata", None)
            presented_basic_proof = (
                envelope_metadata.get("chrome_basic_approval_policy_v1")
                if isinstance(envelope_metadata, dict) else None
            )
            if presented_basic_proof != stored_basic_proof:
                return APPROVAL_TOKEN_IGNORED
            approved_connection_id = str(stored_basic_proof.get("connection_id") or "").strip()
            if approved_connection_id:
                chrome_state = (
                    envelope_metadata.get("chrome_runtime_contract_v1")
                    if isinstance(envelope_metadata, dict) else None
                )
                current_connection_id = str(
                    chrome_state.get("chrome_policy_connection_id") or ""
                ).strip() if isinstance(chrome_state, dict) else ""
                if current_connection_id != approved_connection_id:
                    req.status = ApprovalStatus.EXPIRED.value
                    req.resolved_reason = "chrome_basic_approval_connection_changed"
                    return json.dumps({
                        "error": "approval_payload_changed",
                        "message": "The Chrome connection changed. Review and approve again.",
                        "action_executed": False,
                    }, ensure_ascii=False)
        if action.action_key == _CHROME_PUBLICATION_ACTION:
            try:
                current_binding = await _chrome_publication_binding(
                    db, entity_id=entity_id, user_id=user_id,
                    workspace_id=getattr(runtime_envelope, "workspace_id", None),
                    arguments=arguments,
                )
            except RuntimeApprovalContinuationError:
                current_binding = None
            if not ctx.get("chrome_publication_binding") or current_binding != ctx["chrome_publication_binding"]:
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = "chrome_publication_binding_changed"
                return json.dumps({
                    "error": "approval_payload_changed",
                    "message": "The browser connection or publication attachment changed. Review and approve again.",
                })
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
        from packages.core.governance.approvals import _consent_requirements
        from packages.core.governance.service import check_step_policy
        current_policy = await check_step_policy(
            db, entity_id=entity_id, workspace_id=req.workspace_id, user_id=user_id,
            kind=action.kind, action_key=action.action_key, resource_id=action.resource_id,
            resource_kind=action.resource_kind, resource_ids=runtime_media_output_policy_resources(),
            risk_level=action.risk_level, capability_id=action.capability_id,
            operation=action.operation, impact=impact, account_id=ctx.get("approval_account_id"), task_id=task_id,
            workflow_run_id=workflow_run_id,
        )
        if not current_policy.allowed and not current_policy.pause_for_hitl:
            return json.dumps({"error": "blocked_by_governance", "message": current_policy.reason})


        publish_consent = None
        if tool_name == _TIKTOK_PUBLISH_TOOL:
            from packages.core.ai.runtime.authorization_receipts import TikTokPublishConsent
            from packages.core.services.tiktok_publish_review import (
                _arguments_hash, apply_tiktok_publish_review,
            )

            consent = ctx.get("tiktok_publish_consent")
            evidence = consent.get("evidence") if isinstance(consent, dict) else None
            fields = consent.get("fields") if isinstance(consent, dict) else None
            declarations_match = False
            if isinstance(consent, dict) and isinstance(evidence, dict) and isinstance(fields, dict):
                submitted = consent.get("submitted_review")
                try:
                    # Reapplying the declarations to the frozen final arguments
                    # must reproduce them exactly; no model reconstruction occurs.
                    reapplied = apply_tiktok_publish_review(ctx.get("review"), submitted, fields)
                    declarations_match = (
                        type(consent.get("schema_version")) is int
                        and consent["schema_version"] == 1
                        and submitted.get("review_id") == evidence.get("review_id")
                        and _arguments_hash(reapplied) == _arguments_hash(fields)
                    )
                except (ValueError, TypeError):
                    pass
            if (
                not isinstance(consent, dict)
                or consent.get("version") != 1
                or consent.get("approval_id") != req.id
                or not isinstance(evidence, dict)
                or not isinstance(fields, dict)
                or ctx.get("tiktok_review_refreshed") is not True
                or ctx.get("args_hash") != approval_args_hash(fields)
                or not declarations_match
            ):
                req.status = ApprovalStatus.EXPIRED.value
                req.resolved_reason = "tiktok_publish_consent_missing"
                return await _runtime_block_payload(
                    db, conversation_id=conversation_id, entity_id=entity_id,
                    user_id=user_id, workspace_id=req.workspace_id,
                    tool_name=tool_name, arguments=arguments, action=action,
                    permission_decision=permission_decision,
                    reason="TikTok requires a fresh review of this specific post.",
                    matched_rule_hint="tiktok_publish_review",
                    operation_id=ctx.get("operation_id"), account_id=ctx.get("approval_account_id"),
                    origin_task_id=req.origin_task_id, origin_step_id=req.origin_step_id,
                    workflow_run_id=workflow_run_id, runtime_envelope=runtime_envelope,
                )
            if (
                not permission_decision.allowed
                or ctx.get("requested_by") != user_id
                or evidence.get("entity_id") != entity_id
                or evidence.get("user_id") != user_id
                or evidence.get("conversation_id") != conversation_id
                or str(evidence.get("workspace_id") or "") != str(req.workspace_id or "")
                or evidence.get("tool_name") != tool_name
            ):
                return json.dumps({"error": "tiktok_publish_consent_scope_mismatch"})
            publish_consent = TikTokPublishConsent(
                approval_id=req.id,
                evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                fields_json=json.dumps(fields, sort_keys=True, separators=(",", ":")),
            )
        from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_matches

        if (
            ctx.get("args_hash") not in _approval_args_hash_candidates(arguments, tool_name=tool_name, context=ctx)
            or not runtime_media_output_approval_matches(ctx)
        ):
            # Payload changed after approval — the grant covers other content.
            if has_task_comment_origin(req):
                from packages.core.services.task_runtime_approvals import (
                    expire_task_runtime_approval_request,
                )
                await expire_task_runtime_approval_request(
                    db, request=req, reason="payload_changed_after_approval",
                )
                return json.dumps({"error": "approval_payload_changed", "approval_token": hitl_id})
            from packages.core.services.task_agent_loop_hitl import (
                task_agent_loop_owns_request,
                verified_request_tag,
            )

            if verified_request_tag(req) is not None or await task_agent_loop_owns_request(db, request=req):
                # A Task agent loop continues: the changed action did not run
                # and the loop may ask for approval of the new one. Never the
                # TaskRunner fail path, even for a tag that no longer verifies.
                from packages.core.services.task_runtime_approvals import (
                    expire_task_runtime_approval_request,
                )

                await expire_task_runtime_approval_request(
                    db, request=req, reason="payload_changed_after_approval",
                )
                return json.dumps({
                    "error": "approval_payload_changed",
                    "message": (
                        "The action changed after approval, so it was not run. "
                        "Request approval for the new action."
                    ),
                    "approval_token": hitl_id,
                }, ensure_ascii=False)
            if getattr(req, "origin_task_id", None):
                from packages.core.services.task_runtime_approvals import (
                    fail_task_runtime_approval_origin,
                )

                await fail_task_runtime_approval_origin(
                    db,
                    request=req,
                    reason="payload_changed_after_approval",
                )
                return json.dumps({
                    "error": "approval_payload_changed",
                    "message": (
                        "The scheduled action changed after approval. The origin "
                        "Task was stopped and no action was run."
                    ),
                    "approval_token": hitl_id,
                }, ensure_ascii=False)
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
                operation_id=ctx.get("operation_id"), account_id=ctx.get("approval_account_id"),
                origin_task_id=req.origin_task_id, origin_step_id=req.origin_step_id,
                workflow_run_id=workflow_run_id, runtime_envelope=runtime_envelope,
            )
        # First invalidate changed content/physical output bindings. An old
        # grant must not remain executable merely because the changed target
        # also introduces a new policy requirement.
        if not set(_consent_requirements(current_policy)).issubset(set(ctx.get("approval_requirements") or [])):
            return await _runtime_block_payload(
                db, conversation_id=conversation_id, entity_id=entity_id, user_id=user_id,
                workspace_id=req.workspace_id, tool_name=tool_name, arguments=arguments,
                action=action, permission_decision=permission_decision,
                reason="The action has a new approval requirement. Review it before execution.",
                matched_rule_hint="approval_requirements_changed",
                operation_id=ctx.get("operation_id"), origin_task_id=req.origin_task_id,
                origin_step_id=req.origin_step_id, account_id=ctx.get("approval_account_id"),
                workflow_run_id=workflow_run_id, runtime_envelope=runtime_envelope,
            )
        if not workflow_owned and getattr(req, "origin_task_id", None) and not has_task_comment_origin(req):
            from packages.core.services.task_runtime_approvals import (
                TaskRuntimeApprovalError,
                claim_task_runtime_approval_execution,
                expire_task_runtime_approval_request,
            )

            from packages.core.services.task_agent_loop_hitl import verified_request_tag

            # A Task agent loop's approval is spent by the re-executed call
            # itself; it has no TaskRunner checkpoint or execution attempt.
            claim_scheduled_task = verified_request_tag(req) is None
            if claim_scheduled_task:
                try:
                    await claim_task_runtime_approval_execution(
                        db,
                        request=req,
                        task_id=task_id,
                    )
                except TaskRuntimeApprovalError as exc:
                    await expire_task_runtime_approval_request(
                        db,
                        request=req,
                        reason="runtime_approval_origin_not_executable",
                    )
                    return json.dumps({
                        "error": "approval_origin_not_executable",
                        "message": f"{exc}. No action was run.",
                        "approval_token": hitl_id,
                    }, ensure_ascii=False)
        from packages.core.governance.approvals import claim_approval_consumption

        outcome = await claim_approval_consumption(db, req)
        if outcome is not ApprovalConsumeOutcome.CONSUMED:
            return _approval_consumption_blocked_result(req.id, outcome)
        if action.action_key == _CHROME_PUBLICATION_ACTION:
            from packages.core.ai.runtime.authorization_receipts import (
                ChromePublicationBinding, runtime_record_chrome_publication_binding,
            )

            binding = ctx["chrome_publication_binding"]
            runtime_record_chrome_publication_binding(ChromePublicationBinding(
                connection_id=binding["connection_id"],
                credential_revision=binding["credential_revision"],
                connection_epoch=binding["connection_epoch"],
                approval_id=req.id,
                consumed_at=req.consumed_at,
            ))
        if publish_consent is not None:
            from packages.core.ai.runtime.authorization_receipts import runtime_record_tiktok_publish_consent

            runtime_record_tiktok_publish_consent(publish_consent)
        return None

    if task_id or tool_name == _TIKTOK_PUBLISH_TOOL:
        # Task continuations must spend the exact signed request id.  Falling
        # back to a same-payload direct-chat grant would bypass the Task
        # checkpoint/execution-attempt protocol.
        return json.dumps(
            {
                "error": "approval_not_granted",
                "status": "not_found",
                "message": (
                    "The Task approval token is missing or no longer valid. "
                    "No action was run."
                ),
                "approval_token": hitl_id,
            },
            ensure_ascii=False,
        )

    # Token unknown — try a payload match among granted unified requests for
    # this conversation (the model may echo a stale/foreign token on the
    # correct approved payload).
    from sqlalchemy import select

    from packages.core.models.hitl_request import HitlRequest

    accepted_hashes = set(_approval_args_hash_candidates(arguments))
    from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_matches

    granted_rows = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
            HitlRequest.origin_kind == _RUNTIME_ORIGIN_KIND,
            HitlRequest.origin_task_id.is_(None),
            HitlRequest.status == ApprovalStatus.GRANTED,
            HitlRequest.action_key == action.action_key,
        )
    )).scalars().all()
    matches = [
        row for row in granted_rows
        if (row.context or {}).get("tool") == tool_name
        and (row.context or {}).get("args_hash") in accepted_hashes
        and runtime_media_output_approval_matches(row.context or {})
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
            and runtime_media_output_approval_matches(matched_context)
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
        from packages.core.governance.approvals import claim_approval_consumption

        outcome = await claim_approval_consumption(db, matched)
        if outcome is not ApprovalConsumeOutcome.CONSUMED:
            return _approval_consumption_blocked_result(matched.id, outcome)
        return None

    # Unknown token, no payload match — ignore it and let the normal gate
    # decide (tokens minted before the unified-store upgrade land here: the
    # gate re-asks with a fresh card).
    return APPROVAL_TOKEN_IGNORED
