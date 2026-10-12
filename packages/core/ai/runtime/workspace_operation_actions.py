"""Runtime-owned workspace operation tool action facade."""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.services.hitl_options import one_time_approval_options

logger = logging.getLogger(__name__)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _as_clean_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _rule_key(description: str) -> str:
    import re

    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(description or "").lower()).strip("_")
    return (text[:48] or "workspace_rule").strip("_")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "start", "run", "execute", "now"}


def _pending_hitl_id(pending_action: dict[str, Any]) -> str:
    operation = (
        pending_action.get("operation")
        if isinstance(pending_action.get("operation"), dict)
        else {}
    )
    return str(
        pending_action.get("approval_token")
        or pending_action.get("approval_request_id")
        or pending_action.get("commitment_id")
        or pending_action.get("draft_id")
        or pending_action.get("review_id")
        or pending_action.get("step_id")
        or operation.get("draft_id")
        or ""
    ).strip()


def _normalise_hitl_action(action: Any) -> str:
    raw = str(action or "").strip().lower()
    if raw in {"approve", "approved", "yes", "accept", "confirm", "confirmed", "ok"}:
        return "approve"
    if raw in {
        "reject",
        "rejected",
        "no",
        "deny",
        "decline",
        "cancel",
        "cancelled",
        "canceled",
        "stop",
    }:
        return "reject"
    return raw


async def runtime_workspace_resolve_hitl_action(
    *,
    entity_id: str,
    workspace_id: str,
    conversation_id: str,
    user_id: str | None,
    params: dict[str, Any] | None = None,
    origin_task_id: str | None = None,
) -> str:
    """Relay a user's answer or decision to the exact persisted Workspace card.

    ``origin_task_id``: the calling Task run, when a caller relays for it.
    """

    from packages.core.ai.runtime.execution_origin import runtime_origin_task_id

    raw_params = dict(params or {})
    from packages.core.ai.runtime.tool_execution import _runtime_workspace_action_preflight_error
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    validation = _runtime_workspace_action_preflight_error("workspace_resolve_hitl", raw_params)
    if validation is not None:
        return RuntimePreflightValidationError(validation.to_tool_result(tool_name="workspace_resolve_hitl"))
    message_id = str(raw_params.get("message_id") or "").strip()
    hitl_id = str(raw_params.get("hitl_id") or "").strip()
    action = _normalise_hitl_action(raw_params.get("action"))
    note = str(raw_params.get("note") or "").strip()
    if not conversation_id or not workspace_id or not entity_id:
        return _dumps({
            "error": "workspace_resolve_hitl requires workspace conversation context",
        })
    if not user_id:
        return _dumps({"error": "workspace_resolve_hitl requires a user_id"})
    if action == "respond":
        return await _runtime_workspace_respond_hitl(
            entity_id=entity_id, workspace_id=workspace_id, conversation_id=conversation_id,
            user_id=user_id, params=raw_params,
        )

    from sqlalchemy import select

    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.database import async_session
    from packages.core.models.task import Message
    from packages.core.services.workspace_operation_service import (
        resolve_workspace_operation_review,
    )
    from packages.core.workspace_chat import service as workspace_chat_service

    async with async_session() as db:
        rows = list((await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.pending_action.isnot(None),
                Message.resolved_at.is_(None),
            )
            .order_by(Message.created_at.desc())
            .limit(25)
        )).scalars().all())
        candidates = [
            row
            for row in rows
            if isinstance(row.pending_action, dict)
            and row.pending_action.get("kind")
            == PendingActionKind.WORKSPACE_OPERATION_REVIEW
        ]
        if not candidates:
            return _dumps({
                "resolved": False,
                "reason": "no_open_workspace_operation_review",
            })

        primary = next((row for row in candidates if row.id == message_id), None)
        if primary is None and hitl_id and not message_id:
            matches = [row for row in candidates if _pending_hitl_id(row.pending_action or {}) == hitl_id]
            if not matches:
                matches = [
                    row for row in candidates
                    if str((row.pending_action or {}).get("draft_id") or "") == hitl_id
                ]
                if len(matches) > 1:
                    return _dumps({
                        "resolved": False,
                        "reason": "ambiguous_review",
                        "message": "Choose the exact review card by message_id or its versioned hitl_id.",
                    })
            primary = matches[0] if matches else None
        if primary is None:
            return _dumps({
                "resolved": False,
                "reason": "target_not_found",
                "available": [
                    {
                        "message_id": row.id,
                        "hitl_id": _pending_hitl_id(row.pending_action or {}),
                    }
                    for row in candidates[:10]
                ],
            })

        primary_hitl_id = _pending_hitl_id(primary.pending_action or {})
        if action == "approve":
            # The agent may carry the user's verdict back to a card the user
            # has seen; it may not approve a card it raised in this very turn.
            # "Seen" means a user message arrived after the card was posted.
            seen = (await db.execute(
                select(Message.id)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.author_kind == "user",
                    Message.created_at > primary.created_at,
                )
                .limit(1)
            )).scalar_one_or_none()
            if seen is None:
                return _dumps({
                    "resolved": False,
                    "reason": "review_not_seen_by_user",
                    "message": (
                        "This review card has not been answered by the user yet. "
                        "Show the card, end your turn, and call resolve_hitl "
                        "only to relay the verdict the user gives in chat."
                    ),
                    "message_id": primary.id,
                    "hitl_id": primary_hitl_id,
                })
        targets = [
            row
            for row in candidates
            if primary_hitl_id
            and _pending_hitl_id(row.pending_action or {}) == primary_hitl_id
        ] or [primary]

        result = await resolve_workspace_operation_review(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            hitl_id=primary_hitl_id,
            action=action,
            expected_review_fingerprint=(primary.pending_action.get("operation") or {}).get("review_fingerprint"),
            # Tasks the applied draft creates link to the Task run (if any).
            origin_task_id=origin_task_id or runtime_origin_task_id(raw_params, entity_id=entity_id),
        )
        if result is None:
            await db.rollback()
            return _dumps({
                "resolved": False,
                "reason": "workspace_operation_review_rejected_by_service",
            })
        if result.get("status") in {"conflict", "validation_failed", "review_required", "not_found"}:
            await db.rollback()
            return _dumps({
                "resolved": False,
                "reason": result["status"],
                "message": result.get("llm_message") or result.get("message"),
            })

        resolution = {"choice": str(result.get("action") or action)}
        if note:
            resolution["note"] = note
        from packages.core.services.hitl_requests import mark_hitl_request_resolved

        await mark_hitl_request_resolved(
            db, conversation_id=conversation_id, hitl_id=primary_hitl_id,
            choice=resolution["choice"],
        )
        resolved_message_ids: list[str] = []
        for target in targets:
            resolved = await workspace_chat_service.resolve_pending_action(
                db,
                message_id=target.id,
                user_id=user_id,
                resolution=resolution,
            )
            if resolved is not None:
                resolved_message_ids.append(target.id)

        db.add(Message(
            conversation_id=conversation_id,
            role="system",
            content=str(
                result.get("message")
                or "Workspace operation review resolved."
            ),
            author_kind="system",
            message_kind="system",
            refs=[
                {"type": "message", "id": primary.id},
                {
                    "type": "workspace_operation_draft",
                    "id": result.get("draft_id"),
                },
            ],
        ))
        await db.commit()
        return _dumps({
            "resolved": True,
            "kind": PendingActionKind.WORKSPACE_OPERATION_REVIEW.value,
            "action": resolution["choice"],
            "draft_id": result.get("draft_id") or primary_hitl_id,
            "message_ids": resolved_message_ids,
            "message": result.get("llm_message") or result.get("message"),
        })


async def _runtime_workspace_respond_hitl(
    *, entity_id: str, workspace_id: str, conversation_id: str,
    user_id: str, params: dict[str, Any],
) -> str:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import set_committed_value

    from packages.core.database import async_session
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User
    from packages.core.services.actor_authorization import AuthenticatedUserCredential, resolve_current_user_actor
    from packages.core.services.workspace_input_resolution import (
        WorkspaceInputResolutionError, dispatch_workspace_input_resolution, resolve_workspace_input_message,
    )

    async with async_session() as db:
        user = await db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
        actor = await resolve_current_user_actor(db, AuthenticatedUserCredential(
            user_id=user_id, entity_id=entity_id, token_version=user.token_version,
        )) if user else None
        if actor is None:
            return _dumps({"resolved": False, "reason": "permission_denied", "message": "Current user access is no longer available."})
        set_committed_value(user, "entity_id", actor.entity_id)
        set_committed_value(user, "role", actor.display_role)
        query = select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Message.conversation_id == conversation_id, Conversation.workspace_id == workspace_id,
            Conversation.entity_id == entity_id,
        )
        message_id = str(params.get("message_id") or "").strip()
        hitl_id = str(params.get("hitl_id") or "").strip()
        if message_id:
            message = await db.scalar(query.where(Message.id == message_id))
            if message is not None and hitl_id and _pending_hitl_id(message.pending_action or {}) != hitl_id:
                message = None
        else:
            # Exact identifiers only. Never answer the newest/arbitrary card.
            from sqlalchemy import or_
            matching = list((await db.scalars(query.where(
                Message.resolved_at.is_(None),
                or_(*(Message.pending_action[key].as_string() == hitl_id for key in (
                    "approval_request_id", "commitment_id", "step_id",
                ))),
            ).limit(2))).all()) if hitl_id else []
            message = matching[0] if len(matching) == 1 else None
        if message is None:
            return _dumps({"resolved": False, "reason": "target_not_found_or_ambiguous", "message": "Use the exact message_id of the input request in this conversation."})
        try:
            result = await resolve_workspace_input_message(
                db, workspace_id=workspace_id, conversation_id=conversation_id,
                message_id=message.id, user=user, response=str(params.get("response") or ""),
                payload=params.get("payload"), require_user_reply=True,
            )
        except WorkspaceInputResolutionError as exc:
            await db.rollback()
            return _dumps({"resolved": False, "reason": "input_not_resolved", "message": exc.detail})
        await db.commit()
        await dispatch_workspace_input_resolution(db, result)
        return _dumps({
            "resolved": True, "already_resolved": not result.changed, "action": "respond",
            "message_id": result.message.id, "message": "Response saved to the original input request." if result.changed else "This input request was already answered; no action was repeated.",
        })


def _draft_validation_errors(draft_data: dict[str, Any]) -> list[dict[str, Any]]:
    validation = draft_data.get("validation")
    if not isinstance(validation, dict):
        return []
    errors = [
        error for error in (validation.get("errors") or [])
        if isinstance(error, dict)
    ] if isinstance(validation.get("errors"), list) else []
    if validation.get("valid") is False and not errors:
        errors = [{"path": "draft", "message": "draft failed validation"}]
    return errors


def _draft_needs_fix_payload(draft_data: dict[str, Any]) -> dict[str, Any]:
    """What the model gets instead of a review card when the draft is invalid.

    Until this existed an invalid draft still produced the approval card, with
    Approve disabled by the validation errors and the model told to "wait for
    approval". Nobody could move: the operator could only reject, and the model
    never saw the errors it needed to fix. Prod card "发布 X post" sat on
    ``capability_bindings[8].owner_scope binding must be owned by …`` that way.
    Now the errors go back to the model, the draft stays open, and the card is
    produced by ``patch_draft`` once the draft validates.
    """
    payload = tool_error_payload(
        tool_name="workspace_operation", code="operation_validation_failed",
        message="The draft did not pass validation; no review card was shown.",
        details={"draft_id": draft_data.get("id"), "validation_errors": _draft_validation_errors(draft_data)},
    )
    payload["status"] = "needs_fix"
    return payload


def _operation_review_or_needs_fix(
    draft_data: dict[str, Any],
    *,
    prompt: str,
    content: Any,
) -> dict[str, Any]:
    if _draft_validation_errors(draft_data):
        return _draft_needs_fix_payload(draft_data)
    payload = _operation_review_payload(draft_data, prompt=prompt, content=content)
    payload["draft"] = draft_data
    return payload


def _operation_draft_review_content(draft_data: dict[str, Any]) -> dict[str, Any]:
    patches = draft_data.get("patches") or []
    if patches and all(isinstance(patch, dict) and patch.get("op") == "app_view.update" for patch in patches):
        return {
            "patches": patches,
            "effect": "Update this Workspace page's presentation and selected action targets. Workflow and Agent execution retain their normal permissions and approvals.",
        }
    return {
        "patches": patches,
        "effect": (
            "These workspace-wide changes will affect future "
            "goals, services, rules, tools, skills, channels, "
            "or strategist behavior after approval."
        ),
    }


def _operation_review_payload(
    draft_data: dict[str, Any],
    *,
    prompt: str,
    content: Any,
) -> dict[str, Any]:
    from packages.core.constants.approvals import HitlType
    from packages.core.services.workspace_operation_service import (
        operation_draft_review_fingerprint,
        operation_review_hitl_payload,
    )

    diff = draft_data.get("diff") if isinstance(draft_data.get("diff"), dict) else {}
    validation = draft_data.get("validation") if isinstance(draft_data.get("validation"), dict) else {}
    changed_keys = _as_clean_list((diff or {}).get("changed_keys"))
    patches = draft_data.get("patches") or []
    app_view_only = bool(patches) and all(
        isinstance(patch, dict) and patch.get("op") == "app_view.update"
        for patch in patches
    )
    if app_view_only:
        prompt = "Apply these page changes?"
    # A REVIEW of this draft's contents, not an authorization of a capability.
    # The typed payload is what lets the card say a hard block is being
    # removed; without it the card said only "Apply this workspace operation
    # draft?" while the draft rebuilt never_allow underneath it.
    review_payload = operation_review_hitl_payload(draft_data)
    review_diff = review_payload["diff"]
    fingerprint = operation_draft_review_fingerprint(draft_data)
    review_id = f"workspace_operation:{draft_data.get('id')}:{fingerprint}"
    operation = {
        "kind": "workspace_operation_review",
        "draft_id": draft_data.get("id"),
        "workspace_id": draft_data.get("workspace_id"),
        "base_revision": draft_data.get("base_revision"),
        "review_fingerprint": fingerprint,
        "status": draft_data.get("status"),
        "changed_keys": changed_keys,
        "summary": "Review changes to this Workspace page." if app_view_only else f"Review workspace runtime changes: {', '.join(changed_keys) if changed_keys else 'workspace runtime'}.",
        "diff": diff,
        "validation": validation,
        "patches": patches,
        # Surfaced on the operation blob too: the card reads the operation for
        # everything else it renders, and a warning the renderer has to go
        # looking for in a second place is a warning that gets missed.
        "removed_hard_blocks": review_diff.get("removed_hard_blocks") or [],
    }
    return {
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": review_id,
        "hitl": {
            "id": review_id,
            "type": "approval",
            "hitl_type": HitlType.REVIEW.value,
            "payload": review_payload,
            "prompt": prompt,
            "action": "workspace.operation.apply",
            "tool": "workspace_operation",
            "content": content,
            "operation": operation,
            # No "Always" on a review: the answer is a verdict on THIS diff.
            # A standing grant over "whatever the next draft happens to say"
            # is not a thing a person can consent to.
            "options": one_time_approval_options(),
        },
        "operation": operation,
        "message": (
            "Workspace operation draft is ready. Show the user this review card "
            "and wait for approval before applying."
        ),
    }


def _normalise_workspace_operation_patches(raw: Any) -> list[dict[str, Any]] | None:
    """Accept strict patch arrays plus common single-patch LLM shapes."""

    if raw is None:
        return []
    if isinstance(raw, list):
        patches = [dict(item) for item in raw if isinstance(item, dict)]
        return patches if len(patches) == len(raw) else None
    if isinstance(raw, dict):
        nested = raw.get("patches")
        if isinstance(nested, list):
            return _normalise_workspace_operation_patches(nested)
        if any(key in raw for key in ("op", "operation", "type")):
            return [dict(raw)]
        if raw and all(isinstance(value, dict) for value in raw.values()):
            return [
                {"op": str(op), "payload": dict(payload)}
                for op, payload in raw.items()
            ]
    return None


_WORKSPACE_OPERATION_ACTION_ALIASES = {
    "current": "get_current",
    "get": "get_current",
    "state": "get_current",
    "create": "create_draft",
    "draft": "create_draft",
    "patch": "patch_draft",
    "update": "patch_draft",
    "validate": "validate_draft",
    "preview": "preview_diff",
    "diff": "preview_diff",
    "apply": "apply_draft",
    "approve": "apply_draft",
    "approved": "apply_draft",
    "confirm": "apply_draft",
    "discard": "discard_draft",
    "reject": "discard_draft",
    "cancel": "discard_draft",
}


WORKSPACE_OPERATION_ACTIONS: tuple[str, ...] = (
    "get_current",
    "create_draft",
    "patch_draft",
    "validate_draft",
    "preview_diff",
    "apply_draft",
    "discard_draft",
)


_WORKSPACE_OPERATION_APP_VIEW_GUIDANCE = (
    "The Workspace App connects directly to Agents, active Workflow bindings, and Automations. "
    "It does not require a Service. When asked to add an App action, first call get_current. "
    "Use app_view.update with entries:{'action:<unique entry id>':{title:<name>}} and "
    "targets:{'action:<same id>':'agent:<id>'|'agent:default'|'workflow:<binding_id>'|'automation:<job_id>'}. "
    "Multiple command entries may use the same Agent. Configure Agent instructions with "
    "prompts:{<action key>:<command text>} and input fields with forms. Remove a custom entry with "
    "entries:{<action key>:null}; this removes only that App entry and its display settings, never its target resource. "
    "For saved Workflows use attachable_workflow_options and workflow_binding.attach with action_key. "
    "For existing actions, including legacy Service entries, use their returned exact keys. To edit page text, "
    "rename, describe, reorder, hide, or feature an existing panel action, "
    "first call get_current and use an exact app_view.available_actions key, then "
    "create_draft with app_view.update and payload.app_view={eyebrow:<short label>, title:<page title>, "
    "description:<intro>, hero:<key>, order:[<keys>], hidden:[<keys>], "
    "labels:{<key>:<display name>}, descriptions:{<key>:<short description>}, "
    "forms:{<action key>:[{key:<id>,label:<question>,type:'text'|'long_text'|'file'|'number'|'select',required:true|false,placeholder:<hint>,options:[<choices>]}]}, "
    "entries:{'action:<id>':{title:<name>}}, prompts:{<action key>:<Agent command>}, "
    "targets:{<action key>:'agent:<id>'|'agent:default'|'workflow:<binding_id>'|'automation:<job_id>'}, "
    "visual:'auto'|'image'|'motion'|'plain', layout:'feature'|'cover'|'list'}. "
    "Use the returned workflow_options, agent_options and automation_options for exact target IDs. "
    "Choose a Workflow for the main action. If no App configuration exists, the page defaults to the Workspace name and description, "
    "and uses the first available bound Workflow as its main action. If no Workflow is available, keep the main action empty and help select or create a Workflow. "
    "Any App entry can select Agent, Flow, or Automation. An entry connected to 'workflow:<binding_id>' uses that Workflow's run_inputs instead of its chat form. "
    "To attach a saved active Workflow listed in attachable_workflow_options, use "
    "workflow_binding.attach with payload={workflow_id:<id>,action_key:<optional existing App action key>,primary:<optional boolean>}. "
    "It creates a manual Workspace binding on approved apply and optionally connects that App entry. "
    "Set primary=true to make the attached Workflow the App's main action, including in a Workspace with no App configuration or services. "
    "It never runs the Workflow or creates a definition. Re-read get_current after apply to find the binding_id. "
    "Only include fields the user asked to change. app_view.update edits presentation and target selection; "
    "it never creates a runnable Flow or changes an Agent's tools. "
)

_WORKSPACE_OPERATION_COMMON_GUIDANCE = (
    "operating_model.update merges top-level keys only: each supplied collection "
    "replaces that entire collection. services, goals, rules, automations, processes, "
    "agent_mappings, capability_bindings, and skill_bindings must be arrays of objects, "
    "never objects keyed by name or natural-language instructions. Prefer targeted "
    "upsert/remove patches to preserve unrelated configuration. "
    "process.upsert (alias workflow.upsert) stores process metadata only; it does "
    "not create a runnable WorkflowDefinition or WorkflowBinding. Editing "
    "operating_model.automations in a post-creation operation draft changes "
    "descriptive configuration, not live ScheduledJob schedules. Initial Workspace "
    "provisioning has a separate installer for declared automations. For runnable "
    "automation, use the Manor "
    "create_scheduled_job action (or its list/query, toggle, run-now, or cancel "
    "actions). Use discovered Workflow creation tools for runnable workflows. "
    "Adding an Agent requires agent_mapping.upsert; service_role.upsert only "
    "defines service metadata and cannot provision an Agent or bind skills. "
    "First use operation action=get_current to inspect services and agent_mappings. "
    "For an additional Agent, choose a new service_key and include both "
    "service_role.upsert with payload.service={key:<new service_key>,name:<name>} "
    "and agent_mapping.upsert in the same draft. Reusing an existing service_key "
    "replaces that service's Agent; do so only when replacement was requested. "
    "For an existing Agent, use payload.mapping={service_key,agent_id}. For a "
    "new custom Agent, use payload.mapping={service_key,strategy:'create_custom',"
    "create_agent_draft:{agent_name,system_prompt,tool_bindings:[<tool name>],"
    "skill_bindings:[<skill id or slug>]}} with a complete specialist system_prompt. "
    "Discover real Agent, tool, and Skill identities before binding them. "
    "For Workspace-only skills, add skill_binding.upsert with "
    "payload.binding={owner_scope:'service',owner_service_key:<service_key>,"
    "skill_id:<discovered skill id>}. Workspace-only tools use capability_binding.upsert "
    "with payload.binding={owner_scope:'service',owner_service_key:<service_key>,"
    "capability_type:'tool',tool_name:<discovered tool name>}. "
    "Do not put agent/skill/tool provisioning fields inside a service payload. "
    "Integrations: inspect actor connections with Manor list_integrations. "
    "integration.remove stops Workspace use and requirements; integration.enable "
    "restores a disabled provider. Add only connected providers with "
    "capability_binding.upsert, capability_type:'mcp', integration_key:<server_key>. "
    "Then immediately create_draft with the exact requested add/remove/restore "
    "patch. Its review card requests confirmation; prose is insufficient. "
    "Apply only after approval. "
    "After approval, re-read get_current and verify the requested agent_mappings "
    "and bindings before claiming completion; a services-only diff does not add an Agent. "
)

WORKSPACE_OPERATION_CONFIGURATION_GUIDANCE = (
    _WORKSPACE_OPERATION_APP_VIEW_GUIDANCE + _WORKSPACE_OPERATION_COMMON_GUIDANCE
)

WORKSPACE_OPERATION_EAGER_CONFIGURATION_GUIDANCE = (
    "For App edits, first call get_current before Workspace/Knowledge search; "
    "empty search hits do not prove runners absent. Use returned available_actions, "
    "workflow_options, agent_options, automation_options and attachable_workflow_options. "
    "app_view.update payload.app_view: entries:{'action:<id>':{title}}, "
    "targets:{<key>:'agent:<id>'|'agent:default'|'workflow:<binding_id>'|'automation:<job_id>'}, "
    "prompts and forms. No Service needed; commands may share an Agent. entries:{<key>:null} removes only the entry. "
    "workflow_binding.attach payload={workflow_id,action_key:<optional existing App action key>,primary:<boolean>} creates a manual "
    "binding only on approved apply and never runs the Workflow. Workflow actions use run_inputs. "
    "Default main action: first bound Workflow; if none, leave empty and help select/create. "
    + _WORKSPACE_OPERATION_COMMON_GUIDANCE
)


def _normalise_workspace_operation_action(action: Any) -> str:
    text = str(action or "").strip().lower().replace("-", "_")
    return _WORKSPACE_OPERATION_ACTION_ALIASES.get(text, text)


def _resolve_workspace_operation_action(raw_params: dict[str, Any]) -> str:
    """Read the draft action from ``action`` or, as LLMs often write it, ``op``/``operation``."""

    for key in ("action", "op", "operation"):
        value = raw_params.get(key)
        if value:
            return _normalise_workspace_operation_action(value)
    return ""


def _operation_action_vocabulary() -> str:
    return "valid actions: " + ", ".join(WORKSPACE_OPERATION_ACTIONS)


_PATCHES_KEYS: tuple[str, ...] = ("patches", "operations", "ops")


def describe_operation_patch_ops() -> str:
    """Runtime-boundary re-export of the implementation-derived op vocabulary.

    Thin AI tools must not import business layers directly; they read the
    vocabulary through here.
    """

    from packages.core.services.workspace_operation_service import (
        describe_operation_patch_ops as _describe,
    )

    return _describe()


def supported_operation_patch_ops() -> tuple[str, ...]:
    """Return the same op vocabulary used by the Workspace patch dispatcher."""

    from packages.core.services.workspace_operation_service import (
        SUPPORTED_OPERATION_PATCH_OPS,
    )

    return SUPPORTED_OPERATION_PATCH_OPS


def _resolve_workspace_operation_patches(raw_params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read patches from ``patches`` or the aliases LLMs reach for (``operations``, ``ops``)."""

    for key in _PATCHES_KEYS:
        if key in raw_params:
            return _normalise_workspace_operation_patches(raw_params.get(key))
    return []


async def runtime_workspace_operation_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Run workspace operation draft actions through the Runtime boundary."""

    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})
    raw_params = dict(params or {})
    action = _resolve_workspace_operation_action(raw_params)
    if not action:
        return _dumps({"error": f"action is required; {_operation_action_vocabulary()}"})
    if action not in WORKSPACE_OPERATION_ACTIONS:
        return _dumps({
            "error": (
                f"unsupported workspace_operation action: {action}; "
                f"{_operation_action_vocabulary()}"
            )
        })

    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        OperationValidationError,
    )

    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_operation_service import (
            create_operation_draft,
            discard_operation_draft,
            draft_to_dict,
            get_current_operation_state,
            get_operation_draft,
            patch_operation_draft,
            refresh_operation_draft_review,
        )

        async with async_session() as db:
            if action == "get_current":
                state = await get_current_operation_state(db, workspace_id, entity_id)
                if state is None:
                    return _dumps({"error": "workspace not found"})
                from packages.core.models.workspace import Workspace
                from packages.core.services.workspace_app_view import load_workspace_app_view
                from packages.core.models.user import User

                workspace = await db.get(Workspace, workspace_id)
                user = await db.get(User, user_id) if user_id else None
                if user is None or user.entity_id != entity_id:
                    return _dumps({"error": "an authenticated Workspace actor is required"})
                app_view = await load_workspace_app_view(db, workspace, user=user) if workspace else None
                return _dumps({"workspace_id": workspace_id, "state": state, "app_view": app_view})

            patches = _resolve_workspace_operation_patches(raw_params)
            if patches is None:
                return _dumps({
                    "error": (
                        "patches must be an array of patch objects, a single "
                        "patch object, or an object map of op -> payload"
                    )
                })

            if action == "create_draft" and not patches:
                # An empty draft only produces an empty review card. The
                # caller meant to change something; tell it how to say so.
                from packages.core.services.workspace_operation_service import (
                    describe_operation_patch_ops,
                )

                return _dumps({
                    "error": (
                        "create_draft requires patches as [{op, payload}] "
                        "(keys accepted: patches, operations, ops). "
                        + describe_operation_patch_ops()
                    )
                })

            if action == "create_draft":
                draft = await create_operation_draft(
                    db,
                    workspace_id,
                    entity_id,
                    user_id=user_id or None,
                    source_event_id=str(raw_params.get("source_event_id") or "workspace_agent.operation"),
                    initial_patches=patches,
                )
                if not draft:
                    return _dumps({"error": "workspace not found"})
                draft_data = draft_to_dict(draft)
                payload = _operation_review_or_needs_fix(
                    draft_data,
                    prompt="Apply these workspace runtime changes?",
                    content=_operation_draft_review_content(draft_data),
                )
                await db.commit()
                return _dumps(payload)

            draft_id = str(raw_params.get("draft_id") or "").strip()
            if not draft_id:
                return _dumps({"error": "draft_id is required for this action"})

            if action == "patch_draft":
                draft = await patch_operation_draft(
                    db,
                    draft_id,
                    entity_id,
                    workspace_id,
                    patches,
                )
                if not draft:
                    return _dumps({"error": "operation draft not found"})
                # A patch is how an invalid draft gets fixed, so this is where
                # the review card has to come from once the draft validates —
                # otherwise the model has no action that shows the card again.
                draft_data = draft_to_dict(draft)
                payload = _operation_review_or_needs_fix(
                    draft_data,
                    prompt="Apply these workspace runtime changes?",
                    content=_operation_draft_review_content(draft_data),
                )
                await db.commit()
                return _dumps(payload)

            draft = (
                await refresh_operation_draft_review(db, draft_id, entity_id, workspace_id)
                if action in {"validate_draft", "preview_diff"}
                else await get_operation_draft(db, draft_id, entity_id, workspace_id)
            )
            if not draft:
                return _dumps({"error": "operation draft not found"})

            if action == "validate_draft":
                validation = draft.validation
                await db.commit()
                return _dumps({"draft_id": draft.id, "validation": validation})

            if action == "preview_diff":
                diff = draft.diff
                await db.commit()
                return _dumps({"draft_id": draft.id, "diff": diff})

            if action == "apply_draft":
                if not _truthy(raw_params.get("user_confirmation")):
                    # "Apply" from the model without the user's confirmation
                    # means "ask the user": hand back the review card (or the
                    # errors that stand in its way) rather than an error the
                    # model can only relay as text.
                    draft_data = draft_to_dict(draft)
                    payload = _operation_review_or_needs_fix(
                        draft_data,
                        prompt="Apply these workspace runtime changes?",
                        content=_operation_draft_review_content(draft_data),
                    )
                    await db.commit()
                    return _dumps(payload)
                # Model-supplied confirmation cannot stand in for the durable
                # review the user actually saw and answered.
                await db.rollback()
                from packages.core.ai.runtime.execution_origin import runtime_origin_task_id

                return await runtime_workspace_resolve_hitl_action(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id or "",
                    user_id=user_id,
                    params={"hitl_id": draft_id, "action": "approve"},
                    origin_task_id=runtime_origin_task_id(raw_params, entity_id=entity_id),
                )

            if action == "discard_draft":
                discarded = await discard_operation_draft(
                    db,
                    draft.id,
                    entity_id,
                    workspace_id,
                    user_id=user_id or None,
                )
                payload = {"draft": draft_to_dict(discarded)}
                await db.commit()
                return _dumps(payload)

        return _dumps({"error": f"unsupported workspace_operation action: {action}"})
    except OperationConflictError as exc:
        return _dumps({"error": str(exc), "code": "operation_conflict"})
    except OperationValidationError as exc:
        return _dumps({"error": "operation validation failed", "validation": exc.validation})
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_operation failed")
        return _dumps({"error": f"failed to run workspace operation: {exc}"})


async def runtime_workspace_add_rule_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Create or apply a workspace rule through the operation draft Runtime boundary."""

    raw_params = dict(params or {})
    description = str(raw_params.get("description") or "").strip()
    if not description:
        return _dumps({"error": "description is required"})
    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})

    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_operation_service import (
            create_operation_draft,
            draft_to_dict,
        )

        rule: dict[str, Any] = {
            "rule_key": str(raw_params.get("rule_key") or "").strip() or _rule_key(description),
            "description": description,
            "source": "workspace_agent",
        }
        for key in ("rule_type", "severity", "notes"):
            value = str(raw_params.get(key) or "").strip()
            if value:
                rule[key] = value
        action_patterns = _as_clean_list(raw_params.get("action_patterns"))
        if action_patterns:
            rule["action_patterns"] = action_patterns
        capability_patterns = _as_clean_list(raw_params.get("capability_patterns"))
        if capability_patterns:
            rule["capability_patterns"] = capability_patterns
        runtime_enforceable = bool(
            rule.get("rule_type")
            and (rule.get("action_patterns") or rule.get("capability_patterns"))
        )
        if conversation_id:
            rule["created_from_conversation_id"] = conversation_id
        if user_id:
            rule["created_by_user_id"] = user_id

        async with async_session() as db:
            draft = await create_operation_draft(
                db,
                workspace_id,
                entity_id,
                user_id=user_id or None,
                source_event_id="workspace_agent.add_rule",
                initial_patches=[{"op": "rule.add", "payload": {"rule": rule}}],
            )
            if not draft:
                return _dumps({"error": "workspace not found"})
            draft_data = draft_to_dict(draft)
            # A newly created rule always needs its own visible review.
            if _draft_validation_errors(draft_data):
                payload = _draft_needs_fix_payload(draft_data)
                await db.commit()
                return _dumps(payload)
            payload = _operation_review_payload(
                draft_data,
                prompt=f"Apply this workspace rule?\n\n{description}",
                content={
                    "rule": rule,
                    "effect": (
                        "Future matching workspace tool calls will enforce this structured rule "
                        "after approval."
                        if runtime_enforceable
                        else "This remains agent-visible operating guidance; it is not converted into a runtime policy."
                    ),
                },
            )
            await db.commit()
            return _dumps(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_add_rule failed")
        return _dumps({"error": f"failed to add workspace rule: {exc}"})
