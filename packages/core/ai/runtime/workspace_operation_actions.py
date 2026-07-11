"""Runtime-owned workspace operation tool action facade."""

from __future__ import annotations

import json
import logging
from typing import Any

from packages.core.services.hitl_options import approval_options

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


def _operation_review_payload(
    draft_data: dict[str, Any],
    *,
    prompt: str,
    content: Any,
) -> dict[str, Any]:
    diff = draft_data.get("diff") if isinstance(draft_data.get("diff"), dict) else {}
    validation = draft_data.get("validation") if isinstance(draft_data.get("validation"), dict) else {}
    changed_keys = _as_clean_list((diff or {}).get("changed_keys"))
    operation = {
        "kind": "workspace_operation_review",
        "draft_id": draft_data.get("id"),
        "workspace_id": draft_data.get("workspace_id"),
        "base_revision": draft_data.get("base_revision"),
        "status": draft_data.get("status"),
        "changed_keys": changed_keys,
        "summary": f"Review workspace runtime changes: {', '.join(changed_keys) if changed_keys else 'workspace runtime'}.",
        "diff": diff,
        "validation": validation,
        "patches": draft_data.get("patches") or [],
    }
    return {
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": draft_data.get("id"),
        "hitl": {
            "id": draft_data.get("id"),
            "type": "approval",
            "prompt": prompt,
            "action": "workspace.operation.apply",
            "tool": "workspace_operation",
            "content": content,
            "operation": operation,
            "options": approval_options(),
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
            return [dict(item) for item in nested if isinstance(item, dict)]
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


def _normalise_workspace_operation_action(action: Any) -> str:
    text = str(action or "").strip().lower().replace("-", "_")
    return _WORKSPACE_OPERATION_ACTION_ALIASES.get(text, text)


async def runtime_workspace_operation_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Run workspace operation draft actions through the Runtime boundary."""

    if not workspace_id:
        return _dumps({"error": "workspace_id is required; use this tool only inside workspace chat"})
    raw_params = dict(params or {})
    action = _normalise_workspace_operation_action(raw_params.get("action"))
    if not action:
        return _dumps({"error": "action is required"})

    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        OperationValidationError,
    )

    try:
        from packages.core.database import async_session
        from packages.core.services.workspace_operation_service import (
            apply_operation_draft,
            create_operation_draft,
            discard_operation_draft,
            draft_to_dict,
            get_current_operation_state,
            get_operation_draft,
            patch_operation_draft,
            preview_operation_diff,
            validate_operation_draft,
        )

        async with async_session() as db:
            if action == "get_current":
                state = await get_current_operation_state(db, workspace_id, entity_id)
                if state is None:
                    return _dumps({"error": "workspace not found"})
                return _dumps({"workspace_id": workspace_id, "state": state})

            patches = _normalise_workspace_operation_patches(raw_params.get("patches"))
            if patches is None:
                return _dumps({
                    "error": (
                        "patches must be an array of patch objects, a single "
                        "patch object, or an object map of op -> payload"
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
                payload = _operation_review_payload(
                    draft_data,
                    prompt="Apply these workspace runtime changes?",
                    content={
                        "patches": draft_data.get("patches") or [],
                        "effect": (
                            "These workspace-wide changes will affect future "
                            "goals, services, rules, tools, skills, channels, "
                            "or strategist behavior after approval."
                        ),
                    },
                )
                payload["draft"] = draft_data
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
                payload = {"draft": draft_to_dict(draft)}
                await db.commit()
                return _dumps(payload)

            draft = await get_operation_draft(db, draft_id, entity_id, workspace_id)
            if not draft:
                return _dumps({"error": "operation draft not found"})

            if action == "validate_draft":
                validation = await validate_operation_draft(db, draft)
                await db.commit()
                return _dumps({"draft_id": draft.id, "validation": validation})

            if action == "preview_diff":
                diff = await preview_operation_diff(db, draft)
                await db.commit()
                return _dumps({"draft_id": draft.id, "diff": diff})

            if action == "apply_draft":
                result = await apply_operation_draft(
                    db,
                    draft.id,
                    entity_id,
                    workspace_id,
                    user_id=user_id or None,
                    user_confirmation=bool(raw_params.get("user_confirmation")),
                )
                await db.commit()
                return _dumps(result or {"error": "operation draft not found"})

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
            apply_operation_draft,
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
            if not _truthy(raw_params.get("user_confirmation") or raw_params.get("apply_immediately")):
                payload = _operation_review_payload(
                    draft_data,
                    prompt=f"Apply this workspace rule?\n\n{description}",
                    content={
                        "rule": rule,
                        "effect": (
                            "Future matching workspace tool calls will follow this rule "
                            "after the draft is approved."
                        ),
                    },
                )
                await db.commit()
                return _dumps(payload)

            result = await apply_operation_draft(
                db,
                draft.id,
                entity_id,
                workspace_id,
                user_id=user_id or None,
                user_confirmation=True,
            )
            await db.commit()

        applied_rules = (((result or {}).get("draft") or {}).get("current_state") or {}).get("rules") or []
        return _dumps({
            "updated": True,
            "rule": applied_rules[-1] if applied_rules else rule,
            "draft_id": draft.id,
            "governance_synced": True,
            "workspace_id": workspace_id,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace_add_rule failed")
        return _dumps({"error": f"failed to add workspace rule: {exc}"})
