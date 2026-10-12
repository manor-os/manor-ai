"""Workspace operation draft runtime.

This service is the single staging/apply layer for changes that affect how a
workspace runs: services, rules, knowledge policy, tool/skill/integration
scope, budget, heartbeat, strategist settings, and task waves.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.capability_bindings import (
    apply_runtime_binding_owner_scope_default,
    normalize_runtime_capability_binding,
    runtime_capability_binding_identity,
    validate_runtime_capability_binding,
)
from packages.core.constants.goals import GoalStatus
from packages.core.constants.agents import is_master_agent
from packages.core.models.base import generate_ulid
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceOperationDraft,
    WorkspaceWorkBatch,
)
from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    workspace_autonomy_state,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_references,
    reusable_skill_ids_from_runtime_state,
)
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.workspace_app_view import _ACTION_KEY
from packages.core.services.workspace_operation_validation import (
    partition_new_validation_errors,
    validate_operation_channel_references,
    validate_operation_domain_values,
)


class OperationConflictError(ValueError):
    """The workspace changed after the draft was created."""


class OperationValidationError(ValueError):
    """The draft cannot be safely applied."""

    def __init__(self, validation: dict[str, Any]):
        # str(exc) is what a model or API caller reads first: name the paths,
        # otherwise it can only guess what to fix (R2-4).
        errors = [
            error for error in _as_list((validation or {}).get("errors"))
            if isinstance(error, dict) and (error.get("path") or error.get("message"))
        ]
        detail = "; ".join(
            f"{error.get('path') or 'draft'}: {error.get('message') or error.get('code') or 'invalid'}"
            for error in errors[:_VALIDATION_ERROR_SUMMARY_LIMIT]
        )
        if len(errors) > _VALIDATION_ERROR_SUMMARY_LIMIT:
            detail += f"; and {len(errors) - _VALIDATION_ERROR_SUMMARY_LIMIT} more"
        super().__init__(
            "workspace operation draft has validation errors" + (f": {detail}" if detail else "")
        )
        self.validation = validation


# A model-facing message, not the full list: the rest stay in ``.validation``.
_VALIDATION_ERROR_SUMMARY_LIMIT = 3
_PRE_EXISTING_ISSUE_RESULT_LIMIT = 10


def _rows_without_inherited_errors(
    rows: Any, validation: dict[str, Any] | None, section: str,
) -> list[Any]:
    """Drop the rows of ``section`` that carry an inherited validation error.

    Apply must not materialize a row that is invalid and that this draft did
    not change (e.g. the second of two Goals sharing one goal_key, which would
    overwrite the first). Inherited issue paths index the draft's state.
    """
    pattern = re.compile(rf"^{re.escape(section)}\[(\d+)\]")
    skipped = {
        int(match.group(1))
        for issue in _as_list((validation or {}).get("pre_existing_issues"))
        if isinstance(issue, dict) and (match := pattern.match(str(issue.get("path") or "")))
    }
    return [row for index, row in enumerate(_as_list(rows)) if index not in skipped]


def _pre_existing_issue_summary(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The inherited problems an applied change carried, compact for a tool result."""
    return [
        {key: issue[key] for key in ("path", "code", "message") if issue.get(key)}
        for issue in _as_list((validation or {}).get("pre_existing_issues"))[:_PRE_EXISTING_ISSUE_RESULT_LIMIT]
        if isinstance(issue, dict)
    ]


_OPERATION_APPROVE_ACTIONS = {
    "approve",
    "approved",
    "yes",
    "accept",
    "confirm",
    "confirmed",
    "apply",
    "applied",
    "ok",
}
_OPERATION_REJECT_ACTIONS = {
    "reject",
    "rejected",
    "no",
    "deny",
    "decline",
    "cancel",
    "cancelled",
    "canceled",
    "stop",
    "stopped",
    "discard",
    "discarded",
}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _copy_json(value: Any) -> Any:
    return copy.deepcopy(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


_BINDING_STRUCTURAL_KEYS = frozenset({"op", "operation", "type", "payload", "binding", "bindings"})


def _binding_from_upsert_patch(raw_patch: Any) -> dict[str, Any]:
    """Resolve a single capability/skill binding from an upsert patch.

    Models place the binding fields at any of three depths, under alias names:
    inside ``payload.binding`` (the documented shape), at the ``payload`` top
    level as siblings of ``binding`` (prod card 01KY0XQG — four Chrome bindings
    collapsed into one), or at the PATCH top level as siblings of ``payload``
    (prod card 01KY1GERJK — ``_normalise_patch`` keeps only ``payload`` and would
    otherwise drop ``scope`` / ``skill_key`` / ``service_key`` /
    ``capability_type`` entirely). Merge all three levels — patch < payload <
    payload.binding, most-specific wins — and normalize the ``scope`` ->
    ``owner_scope`` and ``purpose`` -> ``description`` aliases, so the binding is
    complete no matter where the model put the fields."""
    raw_patch = _as_dict(raw_patch)
    merged: dict[str, Any] = {
        k: v for k, v in raw_patch.items() if k not in _BINDING_STRUCTURAL_KEYS
    }
    payload = _as_dict(raw_patch.get("payload"))
    merged.update({k: v for k, v in payload.items() if k not in ("binding", "bindings")})
    nested = payload.get("binding")
    if isinstance(nested, dict):
        merged.update(nested)
    if not merged.get("owner_scope") and merged.get("scope"):
        merged["owner_scope"] = merged.pop("scope")
    if not merged.get("description") and merged.get("purpose"):
        merged["description"] = merged["purpose"]
    return merged


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _normalise_operation_review_action(action: Any) -> str | None:
    text = str(action or "").strip().lower().replace("-", "_")
    if text in _OPERATION_APPROVE_ACTIONS:
        return "approve"
    if text in _OPERATION_REJECT_ACTIONS:
        return "reject"
    return None


def _normalise_operation_review_draft_id(hitl_id: Any) -> str:
    text = str(hitl_id or "").strip()
    if text.startswith("workspace_operation:"):
        return text.split(":", 2)[1].strip()
    if text.startswith("op:"):
        return text.split(":", 1)[1].strip()
    return text


def _changed_key_summary(diff: dict[str, Any] | None) -> str:
    keys = _unique_list((diff or {}).get("changed_keys"))
    return ", ".join(keys) if keys else "no changes"


def _parse_goal_deadline(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _coerce_goal_priority(value: Any, default: int = 3) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, priority))


def _workspace_subscription_prompt(
    workspace: Workspace,
    service_key: str,
    mapping: dict[str, Any],
) -> str:
    parts = [
        f'You are subscribed to the "{workspace.name}" workspace as the "{service_key}" service handler.'
        if service_key else f'You are subscribed to the "{workspace.name}" workspace.',
    ]
    if workspace.primary_work:
        parts.append(f"Workspace primary work: {workspace.primary_work}")
    if workspace.operating_context:
        parts.append(f"Operating context: {workspace.operating_context}")
    rationale = str(mapping.get("rationale") or mapping.get("reason") or "").strip()
    if rationale:
        parts.append(f"Why you were assigned: {rationale}")
    return "\n\n".join(parts)


def _is_auto_framing_prompt(value: Any) -> bool:
    """Heuristic: does this custom_prompt look auto-generated by us?

    Auto framings always start with ``You are subscribed to the "<workspace>"``.
    Operator-written prompts won't begin with that exact phrasing, so we use
    this signature to decide whether it's safe to regenerate when the
    workspace's name / context / primary_work drifts.
    """
    if not isinstance(value, str):
        return False
    return value.lstrip().startswith('You are subscribed to the "')


async def refresh_workspace_subscription_framings(
    db: AsyncSession,
    workspace: Workspace,
) -> int:
    """Regenerate auto-generated AgentSubscription framings for a workspace.

    Call this after the workspace's name, primary_work, or operating_context
    changes so subscriptions don't keep referring to stale identity. Hand-
    written custom prompts (anything that doesn't match the auto-framing
    signature) are left untouched.

    Returns the number of subscriptions that were rewritten.
    """
    rows = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    rewritten = 0
    for sub in rows:
        if not _is_auto_framing_prompt(sub.custom_prompt):
            continue
        new_prompt = _workspace_subscription_prompt(
            workspace,
            str(sub.service_key or "").strip(),
            {},
        )
        if new_prompt != sub.custom_prompt:
            sub.custom_prompt = new_prompt
            rewritten += 1
    if rewritten:
        await db.flush()
    return rewritten


def _unique_list(values: Any) -> list[str]:
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


async def _load_workspace(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    for_update: bool = False,
) -> Workspace | None:
    query = select(Workspace).where(
        Workspace.id == workspace_id,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def _snapshot_workspace_state(
    db: AsyncSession,
    workspace: Workspace,
    *,
    lock_governance: bool = False,
) -> dict[str, Any]:
    from packages.core.budget import get_budget_status
    from packages.core.governance import DEFAULT_POLICY, policy_from_dict, policy_to_dict
    from packages.core.models.governance import GovernancePolicy
    from packages.core.services.workspace_service import get_workspace_agent_mappings

    operating_model = _as_dict(workspace.operating_model)
    # Operation previews/approval compare durable state, not a runtime cache.
    # This matters for repairing policy drift and for the locked apply recheck.
    policy_query = select(GovernancePolicy.policy).where(
        GovernancePolicy.workspace_id == workspace.id,
        GovernancePolicy.entity_id == workspace.entity_id,
    )
    if lock_governance:
        # Apply already holds the Workspace lock. Keep this existing policy
        # row locked from its authoritative read through the eventual write,
        # so a concurrent Governance API edit cannot be lost in between.
        policy_query = policy_query.with_for_update()
    raw_policy = (await db.execute(policy_query)).scalar_one_or_none()
    governance_policy = policy_to_dict(
        policy_from_dict(raw_policy) if raw_policy is not None else DEFAULT_POLICY
    )
    budget = await get_budget_status(db, workspace.id)
    agent_mappings = await get_workspace_agent_mappings(
        db,
        workspace.id,
        workspace.entity_id,
    )
    knowledge_policy = _as_dict(
        operating_model.get("knowledge_policy")
        or operating_model.get("knowledge")
    )
    state = {
        "workspace_id": workspace.id,
        "entity_id": workspace.entity_id,
        "operation_revision": int(workspace.operation_revision or 0),
        "operating_model": operating_model,
        "services": _as_list(operating_model.get("services")),
        "goals": _as_list(operating_model.get("goals")),
        "rules": _as_list(operating_model.get("rules")),
        "automations": _as_list(operating_model.get("automations")),
        "evaluation": _as_dict(operating_model.get("evaluation")),
        "knowledge_policy": knowledge_policy,
        "capability_bindings": _as_list(operating_model.get("capability_bindings")),
        "skill_bindings": _as_list(operating_model.get("skill_bindings")),
        "integration_scope": _as_dict(operating_model.get("integration_scope")),
        "channel_config": _as_dict(operating_model.get("channel_config")),
        "processes": _as_list(operating_model.get("processes")),
        "strategist": _as_dict(operating_model.get("strategist")),
        "agent_mappings": agent_mappings,
        "governance_policy": governance_policy,
        "budget_policy": {
            "monthly_budget_credits": budget.monthly_budget_credits if budget else None,
            "monthly_budget_usd": budget.monthly_budget_usd if budget else None,
            "auto_pause_on_budget": budget.auto_pause_on_budget if budget else bool(workspace.auto_pause_on_budget),
            "reset_alert_state": True,
        },
        "heartbeat_policy": {
            "enabled": bool(workspace.heartbeat_enabled),
            "cadence": workspace.heartbeat_cadence or "daily",
        },
        "immediate_tasks": [],
        "workflow_attachments": [],
    }
    return _sync_operating_model_from_state(state)


def _sync_operating_model_from_state(state: dict[str, Any]) -> dict[str, Any]:
    om = _as_dict(state.get("operating_model"))
    for key in (
        "services",
        "goals",
        "rules",
        "automations",
        "evaluation",
        "capability_bindings",
        "skill_bindings",
        "integration_scope",
        "channel_config",
        "processes",
        "strategist",
        "agent_mappings",
    ):
        if key in state:
            om[key] = _copy_json(state[key])
    if "knowledge_policy" in state:
        om["knowledge"] = _copy_json(state.get("knowledge_policy") or {})
    if "governance_policy" in state:
        om["governance"] = _copy_json(state.get("governance_policy") or {})
    state["operating_model"] = om
    return state


def _merge_by_key(items: Any, item: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _as_list(items) if isinstance(row, dict)]
    key_name = next((key for key in keys if item.get(key)), keys[0])
    key_value = str(item.get(key_name) or "").strip()
    if not key_value:
        rows.append(item)
        return rows
    out: list[dict[str, Any]] = []
    replaced = False
    for row in rows:
        if str(row.get(key_name) or "").strip() == key_value:
            merged = dict(row)
            merged.update(item)
            out.append(merged)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(item)
    return out


def _remove_by_key(items: Any, key_name: str, key_value: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _as_list(items)
        if isinstance(row, dict) and str(row.get(key_name) or "") != str(key_value)
    ]


_GOAL_IDENTITY_KEYS = ("id", "goal_id", "goal_key", "key", "metric_key", "title", "goal")


def _normalise_identity(value: Any) -> str:
    return str(value or "").strip().lower()


def _goal_update_matches(goal: dict[str, Any], update: dict[str, Any]) -> bool:
    """Match one Goal without conflating a shared metric with Goal identity."""
    update_goal_key = str(update.get("goal_key") or update.get("key") or "").strip()
    if update_goal_key:
        return update_goal_key == str(
            goal.get("goal_key") or goal.get("key") or ""
        ).strip()

    update_goal_id = str(update.get("goal_id") or update.get("id") or "").strip()
    if update_goal_id:
        return update_goal_id == str(
            goal.get("goal_id") or goal.get("id") or ""
        ).strip()

    # Legacy/friendly fallbacks are intentionally lower priority. A metric-only
    # update remains a bulk update for all Goals that measure that metric.
    update_metric = _normalise_identity(update.get("metric_key"))
    if update_metric:
        return update_metric == _normalise_identity(goal.get("metric_key"))
    update_title = _normalise_identity(update.get("title") or update.get("goal"))
    return bool(update_title) and update_title == _normalise_identity(
        goal.get("title") or goal.get("goal")
    )


def _goal_measurement_updates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_updates = (
        payload.get("updates")
        or payload.get("measurement_sources")
        or payload.get("sources")
        or payload.get("goals")
    )
    updates: list[dict[str, Any]] = []
    if isinstance(raw_updates, list):
        return [dict(row) for row in raw_updates if isinstance(row, dict)]
    if isinstance(raw_updates, dict):
        for key, raw in raw_updates.items():
            if isinstance(raw, dict):
                update = dict(raw)
            else:
                update = {"measurement_source": raw}
            update.setdefault("goal_key", key)
            updates.append(update)
        return updates
    if any(key in payload for key in (*_GOAL_IDENTITY_KEYS, "measurement_source", "source", "source_type")):
        return [dict(payload)]
    return []


def _measurement_source_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    source = _as_dict(update.get("measurement_source") or update.get("source"))
    source_type = str(
        update.get("source_type")
        or source.get("provider")
        or update.get("provider")
        or ""
    ).strip().lower()
    metric_key = str(update.get("metric_key") or "").strip()

    if not source:
        if "twitter_x" in source_type or source_type in {"x", "twitter", "twitterx"}:
            action = str(update.get("action") or "").strip() or (
                "get_me" if metric_key == "follower_count" else "get_tweet_metrics"
            )
            source = {
                "provider": "twitter_x",
                "action": action,
                "params": {"read_only": True},
            }
        elif source_type in {"manual", "manual_entry"} or source_type.startswith("manual"):
            source = {
                "provider": "manual",
                "params": {
                    "mode": "manual_entry",
                    "preserve_workspace_manual": True,
                },
            }

    if not source:
        return None

    provider = str(source.get("provider") or "").strip().lower()
    if provider in {"twitter_x_read_only", "x_read_only"}:
        source["provider"] = "twitter_x"
        source.setdefault("action", "get_me" if metric_key == "follower_count" else "get_tweet_metrics")
        params = _as_dict(source.get("params"))
        params["read_only"] = True
        source["params"] = params
    elif provider == "manual" or provider.startswith("manual_"):
        source["provider"] = "manual"
        params = _as_dict(source.get("params"))
        params.setdefault("mode", "manual_entry")
        params["preserve_workspace_manual"] = True
        source["params"] = params

    return source


def _apply_goal_measurement_updates(
    goals: Any,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in _as_list(goals) if isinstance(row, dict)]
    for update in _goal_measurement_updates(payload):
        if not any(_normalise_identity(update.get(key)) for key in _GOAL_IDENTITY_KEYS):
            continue

        fields: dict[str, Any] = {}
        measurement_source = _measurement_source_from_update(update)
        if measurement_source is not None:
            fields["measurement_source"] = measurement_source

        cadence = (
            update.get("measurement_cadence")
            or update.get("cadence")
            or update.get("refresh_cadence")
        )
        if cadence is not None:
            fields["measurement_cadence"] = str(cadence or "").strip() or None

        if update.get("baseline_value") is not None or update.get("baseline") is not None:
            fields["baseline_value"] = update.get("baseline_value", update.get("baseline"))
        if update.get("current_value") is not None or update.get("current") is not None:
            fields["current_value"] = update.get("current_value", update.get("current"))

        for key in ("metric_key", "target_value", "target", "status", "deadline", "description", "priority"):
            if key in update and update[key] is not None:
                fields["target_value" if key == "target" else key] = update[key]

        if not fields:
            continue

        matched = False
        for idx, goal in enumerate(rows):
            if _goal_update_matches(goal, update):
                merged = dict(goal)
                merged.update(fields)
                rows[idx] = merged
                matched = True

        if not matched and str(update.get("title") or update.get("goal") or "").strip():
            goal = {
                "goal_key": update.get("goal_key") or update.get("key") or update.get("metric_key"),
                "title": update.get("title") or update.get("goal"),
                "metric_key": update.get("metric_key") or update.get("goal_key") or update.get("key"),
                "target_value": update.get("target_value") or update.get("target") or 1,
                **fields,
            }
            rows.append({key: value for key, value in goal.items() if value is not None})
    return rows


def _normalise_patch(raw_patch: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw_patch, dict):
        raise ValueError("operation patch must be an object")
    op = str(
        raw_patch.get("op")
        or raw_patch.get("operation")
        or raw_patch.get("type")
        or ""
    ).strip()
    if not op:
        raise ValueError("operation patch requires op")
    payload = raw_patch.get("payload")
    if payload is None:
        payload = {
            key: value
            for key, value in raw_patch.items()
            if key not in {"op", "operation", "type"}
        }
    if not isinstance(payload, dict):
        raise ValueError("operation patch payload must be an object; use {op, payload}")
    return op, _as_dict(payload)


_MODEL_ARRAY_SECTIONS = frozenset({
    "services", "goals", "rules", "automations", "capability_bindings",
    "skill_bindings", "processes", "agent_mappings",
})
_MODEL_OBJECT_SECTIONS = frozenset({
    "evaluation", "knowledge", "knowledge_policy", "integration_scope",
    "channel_config", "strategist", "governance", "governance_policy",
})
_SERVICE_PROVISIONING_FIELDS = frozenset({
    "agent", "agent_id", "recommended_agent_id", "create_agent_draft",
    "skills", "skill_bindings", "tool_bindings", "mcp_bindings",
})


def _validate_service_shape(service: Any, *, path: str) -> None:
    if not isinstance(service, dict):
        raise ValueError(f"{path} must be an object")
    misplaced = sorted(set(service) & _SERVICE_PROVISIONING_FIELDS)
    if misplaced:
        raise ValueError(
            f"{path} contains unsupported Agent provisioning fields: {', '.join(misplaced)}. "
            "Services describe roles only. Use agent_mapping.upsert with service_key and "
            "agent_id, or strategy=create_custom plus create_agent_draft. "
            "Use skill_binding.upsert/capability_binding.upsert for Workspace bindings."
        )


def _validate_operation_patch_payload(op: str, payload: dict[str, Any]) -> None:
    """Reject malformed requested state before permissive snapshot coercion."""
    array_fields = {
        "rules.replace": ("rules",), "guardrails.replace": ("rules",),
        "goals.replace": ("goals",), "goal.replace": ("goals",),
        "agent_mappings.replace": ("agent_mappings", "mappings", "agent_bindings"),
        "agent_mapping.replace": ("agent_mappings", "mappings", "agent_bindings"),
        "capability_binding.upsert": ("bindings",), "tool_scope.update": ("bindings",),
    }.get(op, ())
    if op.endswith(".replace") and array_fields and not any(key in payload for key in array_fields):
        raise ValueError(f"{op} requires an explicit {array_fields[0]} array; use [] only to clear the collection")
    for key in array_fields:
        if key not in payload:
            continue
        if not isinstance(payload[key], list):
            raise ValueError(f"{op}.{key} must be an array of objects")
        for idx, row in enumerate(payload[key]):
            if not isinstance(row, dict):
                raise ValueError(f"{op}.{key}[{idx}] must be an object")
    wrapper_fields = {
        "evaluation.update": ("evaluation",), "scorecard.update": ("evaluation",),
        "governance_policy.update": ("governance_policy", "governance"),
        "governance.update": ("governance_policy", "governance"),
        "rule.add": ("rule",), "guardrail.add": ("rule",),
        "rule.update": ("rule",), "guardrail.update": ("rule",),
        "goal.upsert": ("goal",), "goal.add": ("goal",),
        "capability_binding.upsert": ("binding",), "tool_scope.update": ("binding",),
        "skill_binding.upsert": ("binding",), "skill_scope.update": ("binding",),
        "channel.upsert": ("channel",), "channel_config.upsert": ("channel",),
        "process.upsert": ("process",), "workflow.upsert": ("process",),
        "task.prepare": ("task",), "immediate_task.add": ("task",),
    }.get(op, ())
    for key in wrapper_fields:
        if key in payload and not isinstance(payload[key], dict):
            raise ValueError(f"{op}.{key} must be an object")
    if op in {"operating_model.update", "workspace_model.update", "operating_model.replace", "workspace_model.replace"}:
        model = payload.get("operating_model", payload)
        if not isinstance(model, dict):
            raise ValueError("operating_model must be an object")
        if "app_view" in model:
            from packages.core.services.workspace_app_view import normalize_app_view_config
            normalize_app_view_config(model["app_view"])
        for key in sorted(set(model) & _MODEL_ARRAY_SECTIONS):
            if not isinstance(model[key], list):
                message = f"operating_model.{key} must be an array of objects"
                if key == "services":
                    message += "; use service_role.upsert and agent_mapping.upsert to add an Agent without replacing existing services"
                raise ValueError(message)
            for idx, row in enumerate(model[key]):
                if not isinstance(row, dict):
                    raise ValueError(f"operating_model.{key}[{idx}] must be an object")
                if key == "services":
                    _validate_service_shape(row, path=f"operating_model.services[{idx}]")
        for key in sorted(set(model) & _MODEL_OBJECT_SECTIONS):
            if not isinstance(model[key], dict):
                raise ValueError(f"operating_model.{key} must be an object")
    elif op in {"service_role.upsert", "service.upsert"}:
        _validate_service_shape(payload.get("service", payload), path="service")
    elif op in {"agent_binding.upsert", "agent_mapping.upsert"}:
        mapping = payload.get("mapping", payload)
        if not isinstance(mapping, dict):
            raise ValueError("agent mapping must be an object")
        if not str(mapping.get("service_key") or "").strip():
            raise ValueError("agent_mapping.upsert requires service_key")


# ── operation patch registry ──────────────────────────────────────────────
#
# Every patch op the dispatcher understands is registered here, and the
# agent-visible vocabulary (tool schemas, error messages, guidance) is derived
# from this registry. There is deliberately no second list to keep in sync:
# an op that is not registered cannot be dispatched, and an op that is
# registered is always advertised.


@dataclass(frozen=True)
class OperationPatchOp:
    canonical: str
    aliases: tuple[str, ...]
    summary: str
    touches_rules: bool = False
    touches_skill_bindings: bool = False
    payload_schema: dict[str, Any] | None = None


_PATCH_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]] = {}
_PATCH_CATALOG: list[OperationPatchOp] = []


def _patch_op(
    canonical: str, *aliases: str, summary: str,
    touches_rules: bool = False, touches_skill_bindings: bool = False,
    payload_schema: dict[str, Any] | None = None,
):
    def register(fn):
        entry = OperationPatchOp(canonical, tuple(aliases), summary, touches_rules, touches_skill_bindings, payload_schema)
        _PATCH_CATALOG.append(entry)
        for name in (canonical, *aliases):
            if name in _PATCH_HANDLERS:
                raise RuntimeError(f"duplicate workspace operation patch op: {name}")
            _PATCH_HANDLERS[name] = fn
        return fn
    return register


def _enrich_rules(rules: Any) -> list[dict[str, Any]]:
    from packages.core.services.workspace_setup_service import _enrich_operating_rules

    return _enrich_operating_rules(rules)


_INTEGRATION_REMOVE_EXAMPLE = (
    'Use patches=[{"op":"integration.remove","payload":{"provider":"elevenlabs"}}] '
    "for a workspace-wide removal. The current workspace is already supplied by the runtime; "
    "omit scope, owner_scope, service_key, agent_id, task_id, and binding_key. "
    "This preserves shared account credentials and Agent definitions automatically."
)


# The dispatcher, public tool schema and guide catalog share these shapes.
_INTEGRATION_PROVIDER_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "integration_key": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "owner_scope": {"type": "string", "pattern": r"^\s*(workspace|workspace_agent)?\s*$"},
        "scope": {"type": "string", "pattern": r"^\s*(workspace|workspace_agent)?\s*$"},
    },
    "anyOf": [{"required": ["provider"]}, {"required": ["integration_key"]}],
    "additionalProperties": False,
}
_INTEGRATION_SCOPE_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "disabled_providers": {
            "type": "array", "items": {"type": "string", "minLength": 1, "pattern": r"\S"},
        },
    },
    "additionalProperties": False,
}


def _validated_integration_scope(raw: Any) -> dict[str, Any]:
    # Never turn malformed policy into {}, which would silently restore every
    # disabled integration during a whole-model update/replace.
    if not isinstance(raw, dict):
        raise ValueError("integration_scope must be an object")
    unknown = sorted(set(raw) - _INTEGRATION_SCOPE_PAYLOAD_SCHEMA["properties"].keys())
    if unknown:
        raise ValueError(
            f"integration_scope contains unsupported fields: {', '.join(unknown)}. "
            "Only disabled_providers (an array of provider keys) changes executable scope. "
            + _INTEGRATION_REMOVE_EXAMPLE
        )
    scope = _copy_json(raw)
    if "disabled_providers" in scope:
        disabled = scope["disabled_providers"]
        if not isinstance(disabled, list) or any(
            not isinstance(value, str) or not canonical_provider_key(value)
            for value in disabled
        ):
            raise ValueError("integration_scope.disabled_providers must be a list of non-empty provider keys")
        scope["disabled_providers"] = sorted({canonical_provider_key(value) for value in disabled})
    return scope


@_patch_op(
    "operating_model.replace",
    "workspace_model.replace",
    summary="replace the whole operating model",
    touches_rules=True,
    touches_skill_bindings=True,
)
def _op_operating_model_replace(state, payload, raw_patch):
    raw_model = payload.get("operating_model", payload)
    if not isinstance(raw_model, dict):
        raise ValueError("operating_model must be an object")
    om = _as_dict(raw_model)
    if "integration_scope" in om:
        om["integration_scope"] = _validated_integration_scope(om["integration_scope"])
    state["operating_model"] = om
    state["services"] = _as_list(om.get("services"))
    state["goals"] = _as_list(om.get("goals"))
    state["rules"] = _enrich_rules(om.get("rules") or [])
    state["automations"] = _as_list(om.get("automations"))
    state["evaluation"] = _as_dict(om.get("evaluation"))
    state["knowledge_policy"] = _as_dict(om.get("knowledge") or om.get("knowledge_policy"))
    state["capability_bindings"] = _as_list(om.get("capability_bindings"))
    state["skill_bindings"] = _as_list(om.get("skill_bindings"))
    state["integration_scope"] = _as_dict(om.get("integration_scope"))
    state["channel_config"] = _as_dict(om.get("channel_config"))
    state["processes"] = _as_list(om.get("processes"))
    state["strategist"] = _as_dict(om.get("strategist"))
    state["agent_mappings"] = _as_list(om.get("agent_mappings"))
    return _sync_operating_model_from_state(state)


@_patch_op("operating_model.update", "workspace_model.update", summary="merge top-level operating model keys", touches_skill_bindings=True)
def _op_operating_model_update(state, payload, raw_patch):
    raw_model = payload.get("operating_model", payload)
    if not isinstance(raw_model, dict):
        raise ValueError("operating_model must be an object")
    om_update = _as_dict(raw_model)
    if "integration_scope" in om_update:
        om_update["integration_scope"] = _validated_integration_scope(om_update["integration_scope"])
    om = _as_dict(state.get("operating_model"))
    om.update(_copy_json(om_update))
    state["operating_model"] = om
    for key in (
        "services",
        "goals",
        "rules",
        "automations",
        "evaluation",
        "capability_bindings",
        "skill_bindings",
        "integration_scope",
        "channel_config",
        "processes",
        "strategist",
        "agent_mappings",
    ):
        if key in om_update:
            state[key] = _as_dict(om.get(key)) if key in {
                "evaluation",
                "integration_scope",
                "channel_config",
                "strategist",
            } else _as_list(om.get(key))
    if "knowledge" in om_update or "knowledge_policy" in om_update:
        state["knowledge_policy"] = _as_dict(om.get("knowledge") or om.get("knowledge_policy"))
    if "governance" in om_update or "governance_policy" in om_update:
        governance = _as_dict(om.get("governance") or om.get("governance_policy"))
        current_approval = _as_dict(state.get("governance_policy")).get("approval")
        forbidden = set(governance) - {"max_risk_level", "budget_caps_per_kind"}
        if forbidden:
            raise ValueError("Approval policy is edited through Execution & approvals settings")
        if current_approval is not None:
            governance["approval"] = current_approval
        state["governance_policy"] = governance
    return _sync_operating_model_from_state(state)


@_patch_op("app_view.update", summary="change the Workspace app page's text, action targets, order, or appearance")
def _op_app_view_update(state, payload, raw_patch):
    from packages.core.services.workspace_app_view import normalize_app_view_config

    update = payload.get("app_view", payload)
    if not isinstance(update, dict):
        raise ValueError("app_view.update requires an app_view object")
    unknown = set(update) - {"eyebrow", "title", "description", "hero", "order", "hidden", "labels", "descriptions", "forms", "targets", "entries", "prompts", "visual", "layout", "motion_cover_url", "motion_cover_source_url"}
    if unknown:
        raise ValueError(f"unsupported app_view update fields: {', '.join(sorted(unknown))}")
    model = _as_dict(state.get("operating_model"))
    config = normalize_app_view_config(model.get("app_view"))
    for key in ("eyebrow", "title", "description", "hero", "order", "hidden", "visual", "layout", "motion_cover_url", "motion_cover_source_url"):
        if key in update:
            config[key] = update[key]
    if "labels" in update:
        if not isinstance(update["labels"], dict):
            raise ValueError("app_view.labels must be an object")
        labels = dict(config["labels"])
        for key, value in update["labels"].items():
            if value is None:
                labels.pop(key, None)
            else:
                labels[key] = value
        config["labels"] = labels
    if "descriptions" in update:
        if not isinstance(update["descriptions"], dict):
            raise ValueError("app_view.descriptions must be an object")
        descriptions = dict(config["descriptions"])
        for key, value in update["descriptions"].items():
            if value is None:
                descriptions.pop(key, None)
            else:
                descriptions[key] = value
        config["descriptions"] = descriptions
    if "forms" in update:
        if not isinstance(update["forms"], dict):
            raise ValueError("app_view.forms must be an object")
        forms = dict(config["forms"])
        for key, fields in update["forms"].items():
            if fields is None:
                forms.pop(key, None)
            else:
                forms[key] = fields
        config["forms"] = forms
    if "targets" in update:
        if not isinstance(update["targets"], dict):
            raise ValueError("app_view.targets must be an object")
        targets = dict(config["targets"])
        for key, target in update["targets"].items():
            if target is None:
                targets.pop(key, None)
            else:
                targets[key] = target
        config["targets"] = targets
    for field in ("entries", "prompts"):
        if field not in update:
            continue
        if not isinstance(update[field], dict):
            raise ValueError(f"app_view.{field} must be an object")
        for key, value in update[field].items():
            if field == "entries":
                from packages.core.services.workspace_app_view import _ACTION_KEY
                if not isinstance(key, str) or not key.startswith("action:") or not _ACTION_KEY.fullmatch(key):
                    raise ValueError("app_view.entries keys must be action:<entry id>")
            if value is None:
                config[field].pop(key, None)
                if field == "entries":
                    for mapping in ("labels", "descriptions", "forms", "targets", "prompts"):
                        config[mapping].pop(key, None)
                    for collection in ("order", "hidden"):
                        config[collection] = [item for item in config[collection] if item != key]
                    if config["hero"] == key:
                        config["hero"] = None
            else:
                config[field][key] = value
    model["app_view"] = normalize_app_view_config(config)
    state["operating_model"] = model
    return _sync_operating_model_from_state(state)


_WORKFLOW_ATTACH_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "workflow_id": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "service_key": {"type": ["string", "null"], "minLength": 1, "pattern": r"\S"},
        "action_key": {"type": ["string", "null"], "pattern": _ACTION_KEY.pattern},
        "primary": {"type": "boolean"},
    },
    "required": ["workflow_id"],
    "additionalProperties": False,
    "not": {"properties": {"action_key": {"type": "string"}, "service_key": {"type": "string"}},
            "required": ["action_key", "service_key"]},
}


@_patch_op("workflow_binding.attach", summary="attach a saved Workflow and optionally make it the App's main action or an entry target",
           payload_schema=_WORKFLOW_ATTACH_PAYLOAD_SCHEMA)
def _op_workflow_binding_attach(state, payload, raw_patch):
    if set(payload) - _WORKFLOW_ATTACH_PAYLOAD_SCHEMA["properties"].keys():
        raise ValueError("workflow_binding.attach accepts workflow_id, optional action_key, service_key and primary")
    workflow_id = payload.get("workflow_id")
    service_key = payload.get("service_key")
    action_key = payload.get("action_key")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("workflow_binding.attach requires a saved workflow_id")
    if service_key is not None and (not isinstance(service_key, str) or not service_key.strip()):
        raise ValueError("workflow_binding.attach service_key must name an existing service")
    if action_key is not None:
        from packages.core.services.workspace_app_view import _ACTION_KEY
        if not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key):
            raise ValueError("workflow_binding.attach action_key must name an existing App entry")
        if service_key is not None:
            raise ValueError("workflow_binding.attach accepts either action_key or service_key")
    if "primary" in payload and not isinstance(payload["primary"], bool):
        raise ValueError("workflow_binding.attach primary must be boolean")
    row = {"workflow_id": workflow_id.strip()}
    if service_key is not None:
        row["service_key"] = service_key.strip()
    if action_key is not None:
        row["action_key"] = action_key
    if payload.get("primary"):
        row["primary"] = True
    def entry_key(item):
        return item.get("action_key") or (f"service:{item['service_key']}" if item.get("service_key") else None)
    rows = [item for item in state.get("workflow_attachments") or [] if not (
        entry_key(item) == entry_key(row)
        and (entry_key(row) or item.get("workflow_id") == row["workflow_id"])
    )]
    rows.append(row)
    state["workflow_attachments"] = rows
    return state


@_patch_op("evaluation.update", "scorecard.update", summary="merge evaluation/scorecard fields")
def _op_evaluation_update(state, payload, raw_patch):
    evaluation = _as_dict(state.get("evaluation"))
    evaluation.update(_as_dict(payload.get("evaluation") or payload))
    state["evaluation"] = evaluation
    return _sync_operating_model_from_state(state)


@_patch_op("budget_policy.update", "budget.update", summary="merge budget policy (monthly_budget_credits/usd)")
def _op_budget_policy_update(state, payload, raw_patch):
    budget = _as_dict(state.get("budget_policy"))
    budget.update(payload)
    # Budget snapshots expose both the persisted USD value and a derived
    # credit value. If a later patch updates only one representation, the
    # stale counterpart must not keep winning during apply.
    if "monthly_budget_usd" in payload and "monthly_budget_credits" not in payload:
        if payload.get("monthly_budget_usd") is None:
            budget["monthly_budget_credits"] = 0
        else:
            budget.pop("monthly_budget_credits", None)
    if "monthly_budget_credits" in payload and "monthly_budget_usd" not in payload:
        if payload.get("monthly_budget_credits") is None:
            budget["monthly_budget_credits"] = 0
        budget.pop("monthly_budget_usd", None)
    state["budget_policy"] = budget
    return _sync_operating_model_from_state(state)


@_patch_op("heartbeat_policy.update", "heartbeat.update", summary="merge heartbeat policy (enabled, cadence)")
def _op_heartbeat_policy_update(state, payload, raw_patch):
    heartbeat = _as_dict(state.get("heartbeat_policy"))
    heartbeat.update(payload)
    state["heartbeat_policy"] = heartbeat
    return _sync_operating_model_from_state(state)


@_patch_op(
    "governance_policy.update",
    "governance.update",
    summary="merge Workspace risk ceilings and per-kind budgets",
)
def _op_governance_policy_update(state, payload, raw_patch):
    governance = _as_dict(state.get("governance_policy"))
    governance_update = _as_dict(
        payload.get("governance_policy") or payload.get("governance") or payload
    )
    if set(governance_update) - {"max_risk_level", "budget_caps_per_kind"}:
        raise ValueError("Use Execution & approvals settings to edit approval policy")
    governance.update(governance_update)
    state["governance_policy"] = governance
    return _sync_operating_model_from_state(state)


@_patch_op("rule.add", "guardrail.add", summary="add one rule (description, rule_type, action_patterns/capability_patterns)", touches_rules=True)
def _op_rule_add(state, payload, raw_patch):
    rule = _as_dict(payload.get("rule") or payload)
    rules = _as_list(state.get("rules"))
    enriched = _enrich_rules(rules + [rule])
    state["rules"] = _dedupe_rule_dicts(enriched)
    return _sync_operating_model_from_state(state)


def _rule_identity_fields(payload: dict[str, Any]) -> tuple[str, str]:
    rule_key = str(payload.get("rule_key") or payload.get("key") or "").strip()
    description = str(
        payload.get("match_description")
        or (payload.get("description") if not rule_key else "")
        or ""
    ).strip()
    return rule_key, description


def _find_rule_index(rules: list[dict[str, Any]], rule_key: str, description: str) -> int:
    for idx, rule in enumerate(rules):
        if rule_key and str(rule.get("rule_key") or "").strip() == rule_key:
            return idx
    if description:
        wanted = description.casefold()
        for idx, rule in enumerate(rules):
            if str(rule.get("description") or "").strip().casefold() == wanted:
                return idx
    return -1


def _no_rule_matches(rules: list[dict[str, Any]], rule_key: str, description: str) -> ValueError:
    known = [str(rule.get("rule_key") or "") for rule in rules if rule.get("rule_key")]
    wanted = rule_key or description or "<empty>"
    return ValueError(
        f"no workspace rule matches {wanted!r}; known rule_keys: {', '.join(known) or '(none)'}"
    )


@_patch_op("rule.remove", "guardrail.remove", summary="remove one rule by rule_key (or exact description)", touches_rules=True)
def _op_rule_remove(state, payload, raw_patch):
    rules = [dict(row) for row in _as_list(state.get("rules")) if isinstance(row, dict)]
    rule_key, description = _rule_identity_fields(payload)
    idx = _find_rule_index(rules, rule_key, description)
    if idx < 0:
        raise _no_rule_matches(rules, rule_key, description)
    del rules[idx]
    state["rules"] = rules
    return _sync_operating_model_from_state(state)


@_patch_op(
    "rule.update",
    "guardrail.update",
    summary="merge fields into one rule by rule_key (e.g. rule_type draft_only -> approval_required)",
    touches_rules=True,
)
def _op_rule_update(state, payload, raw_patch):
    rules = [dict(row) for row in _as_list(state.get("rules")) if isinstance(row, dict)]
    rule_key, description = _rule_identity_fields(payload)
    idx = _find_rule_index(rules, rule_key, description)
    if idx < 0:
        raise _no_rule_matches(rules, rule_key, description)
    fields = _as_dict(payload.get("rule"))
    if not fields:
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"rule_key", "key", "match_description"}
        }
    merged = {**rules[idx], **_copy_json(fields)}
    merged.setdefault("rule_key", rules[idx].get("rule_key"))
    # Enforcement is re-derived from the (possibly retyped) rule.
    merged.pop("runtime_enforced", None)
    rules[idx] = merged
    state["rules"] = _enrich_rules(rules)
    return _sync_operating_model_from_state(state)


@_patch_op("rules.replace", "guardrails.replace", summary="replace the full rule list", touches_rules=True)
def _op_rules_replace(state, payload, raw_patch):
    state["rules"] = _enrich_rules(payload.get("rules") or [])
    return _sync_operating_model_from_state(state)


@_patch_op("goals.replace", "goal.replace", summary="replace the full goal list")
def _op_goals_replace(state, payload, raw_patch):
    state["goals"] = _as_list(payload.get("goals") or [])
    return _sync_operating_model_from_state(state)


@_patch_op("goal.upsert", "goal.add", summary="insert or merge one goal by goal_key")
def _op_goal_upsert(state, payload, raw_patch):
    goal = _as_dict(payload.get("goal") or payload)
    state["goals"] = _merge_by_key(
        state.get("goals"),
        goal,
        "goal_key",
        "key",
        "goal_id",
        "id",
        "metric_key",  # legacy payloads only
    )
    return _sync_operating_model_from_state(state)


@_patch_op(
    "goal.measurement_sources.update",
    "goal.measurement_source.update",
    "goals.measurement_sources.update",
    "measurement_sources.update",
    "goal_sources.update",
    summary="attach measurement_source/cadence/baseline/current_value to goals",
)
def _op_goal_measurement_sources_update(state, payload, raw_patch):
    state["goals"] = _apply_goal_measurement_updates(state.get("goals"), payload)
    return _sync_operating_model_from_state(state)


@_patch_op("service_role.upsert", "service.upsert", summary="insert or merge one service by key")
def _op_service_role_upsert(state, payload, raw_patch):
    service = _as_dict(payload.get("service") or payload)
    if service.get("service_key") and not service.get("key"):
        service["key"] = service["service_key"]
    state["services"] = _merge_by_key(state.get("services"), service, "key", "service_key")
    return _sync_operating_model_from_state(state)


@_patch_op("service_role.remove", "service.remove", summary="remove one service (and its agent mappings) by key")
def _op_service_role_remove(state, payload, raw_patch):
    key = str(payload.get("key") or payload.get("service_key") or "").strip()
    if key:
        state["services"] = _remove_by_key(state.get("services"), "key", key)
        state["agent_mappings"] = [
            dict(row)
            for row in _as_list(state.get("agent_mappings"))
            if isinstance(row, dict) and str(row.get("service_key") or "").strip() != key
        ]
    return _sync_operating_model_from_state(state)


@_patch_op("knowledge_policy.update", "knowledge.update", summary="merge knowledge retrieval policy")
def _op_knowledge_policy_update(state, payload, raw_patch):
    policy = _as_dict(state.get("knowledge_policy"))
    policy.update(payload)
    state["knowledge_policy"] = policy
    return _sync_operating_model_from_state(state)


@_patch_op(
    "capability_binding.upsert",
    "tool_scope.update",
    summary="bind capability/mcp/tool/action to workspace_agent/service/agent",
)
def _op_capability_binding_upsert(state, payload, raw_patch):
    bindings = payload.get("bindings") or raw_patch.get("bindings")
    if isinstance(bindings, list):
        for binding in bindings:
            if isinstance(binding, dict):
                state["capability_bindings"] = _upsert_capability_binding(
                    state.get("capability_bindings"),
                    binding,
                )
    else:
        binding = _binding_from_upsert_patch(raw_patch)
        state["capability_bindings"] = _upsert_capability_binding(
            state.get("capability_bindings"),
            binding,
        )
    return _sync_operating_model_from_state(state)


@_patch_op("capability_binding.remove", "tool_scope.remove", summary="remove one capability binding by binding_key")
def _op_capability_binding_remove(state, payload, raw_patch):
    binding_key = str(payload.get("binding_key") or payload.get("key") or "").strip()
    bindings = _as_list(state.get("capability_bindings"))
    if not binding_key or not any(
        isinstance(row, dict) and str(row.get("binding_key") or "") == binding_key
        for row in bindings
    ):
        known = ", ".join(
            str(row["binding_key"])
            for row in bindings
            if isinstance(row, dict) and row.get("binding_key")
        )
        raise ValueError(
            "capability_binding.remove requires an existing binding_key; "
            f"received {binding_key!r}. Known binding keys: {known or '(none)'}. "
            "To stop requiring and using an integration throughout this workspace, "
            "use integration.remove with payload.provider (or integration_key). "
            "This does not disconnect the account or edit shared Agent bindings."
        )
    state["capability_bindings"] = _remove_by_key(bindings, "binding_key", binding_key)
    return _sync_operating_model_from_state(state)


@_patch_op("skill_binding.upsert", "skill_scope.update", summary="bind a skill to workspace_agent/service/agent", touches_skill_bindings=True)
def _op_skill_binding_upsert(state, payload, raw_patch):
    binding = _binding_from_upsert_patch(raw_patch)
    state["skill_bindings"] = _upsert_capability_binding(
        state.get("skill_bindings"),
        binding,
        default_type="skill",
    )
    return _sync_operating_model_from_state(state)


@_patch_op(
    "integration_scope.update", "integration.update",
    summary="set disabled_providers:string[] only; prefer integration.remove/enable for one provider; all other fields are rejected",
    payload_schema=_INTEGRATION_SCOPE_PAYLOAD_SCHEMA,
)
def _op_integration_scope_update(state, payload, raw_patch):
    updates = _validated_integration_scope(payload)
    scope = _validated_integration_scope(state.get("integration_scope", {}))
    scope.update(updates)
    state["integration_scope"] = _validated_integration_scope(scope)
    return _sync_operating_model_from_state(state)


def _integration_patch_provider(payload: dict[str, Any]) -> str:
    # This operation is deliberately workspace-wide, not a mutation of an
    # account connection or an Agent that other workspaces may share.
    unknown = sorted(set(payload) - _INTEGRATION_PROVIDER_PAYLOAD_SCHEMA["properties"].keys())
    scopes = [payload[key] for key in ("owner_scope", "scope") if key in payload]
    if unknown or any(
        not isinstance(scope, str) or scope.strip() not in {"", "workspace", "workspace_agent"}
        for scope in scopes
    ):
        raise ValueError(
            "integration.remove/enable is workspace-wide and does not accept extra or narrower-scope fields. "
            + (f"Unsupported fields: {', '.join(unknown)}. " if unknown else "")
            + _INTEGRATION_REMOVE_EXAMPLE
        )
    raw_providers = [payload[key] for key in ("provider", "integration_key") if key in payload]
    if not raw_providers or any(not isinstance(raw, str) or not canonical_provider_key(raw) for raw in raw_providers):
        raise ValueError("integration.remove/enable requires payload.provider (or integration_key). " + _INTEGRATION_REMOVE_EXAMPLE)
    providers = {canonical_provider_key(raw) for raw in raw_providers}
    if len(providers) != 1:
        raise ValueError("integration.remove/enable provider and integration_key conflict; specify one integration. " + _INTEGRATION_REMOVE_EXAMPLE)
    return providers.pop()


@_patch_op(
    "integration.remove",
    summary="disable a provider/integration_key for this workspace's requirements and execution; keep account credentials and shared Agent bindings",
    payload_schema=_INTEGRATION_PROVIDER_PAYLOAD_SCHEMA,
)
def _op_integration_remove(state, payload, raw_patch):
    provider = _integration_patch_provider(payload)
    scope = _validated_integration_scope(state.get("integration_scope", {}))
    disabled = {canonical_provider_key(value) for value in _as_list(scope.get("disabled_providers"))}
    scope["disabled_providers"] = sorted((disabled | {provider}) - {""})
    state["integration_scope"] = scope
    return _sync_operating_model_from_state(state)


@_patch_op(
    "integration.enable",
    summary="undo integration.remove for one provider/integration_key; restores inherited requirements without adding bindings or connecting an account",
    payload_schema=_INTEGRATION_PROVIDER_PAYLOAD_SCHEMA,
)
def _op_integration_enable(state, payload, raw_patch):
    provider = _integration_patch_provider(payload)
    scope = _validated_integration_scope(state.get("integration_scope", {}))
    disabled = {canonical_provider_key(value) for value in _as_list(scope.get("disabled_providers"))}
    if provider not in disabled:
        raise ValueError(f"integration {provider!r} is not disabled in this workspace")
    scope["disabled_providers"] = sorted(disabled - {provider, ""})
    state["integration_scope"] = scope
    return _sync_operating_model_from_state(state)


@_patch_op("channel.upsert", "channel_config.upsert", summary="insert or merge one channel by channel_id/channel_type")
def _op_channel_upsert(state, payload, raw_patch):
    channel = _as_dict(payload.get("channel") or payload)
    channel_config = _as_dict(state.get("channel_config"))
    channels = _merge_by_key(
        channel_config.get("channels"),
        channel,
        "channel_id",
        "channel_type",
    )
    channel_config["channels"] = channels
    state["channel_config"] = channel_config
    return _sync_operating_model_from_state(state)


@_patch_op("agent_binding.upsert", "agent_mapping.upsert", summary="map an agent to a service by service_key")
def _op_agent_binding_upsert(state, payload, raw_patch):
    mapping = _as_dict(payload.get("mapping") or payload)
    rows = _as_list(state.get("agent_mappings"))
    if mapping.get("agent_id") or mapping.get("recommended_agent_id") or mapping.get("strategy") == "create_custom":
        # An explicit selection replaces the previous selection, while keeping
        # this deployment's prompt/config. Otherwise its old agent_id wins over
        # a new recommended_agent_id or create_custom design during apply.
        rows = [
            {
                key: value for key, value in row.items()
                if key not in {"agent_id", "recommended_agent_id", "strategy", "create_agent_draft"}
            }
            if isinstance(row, dict) and row.get("service_key") == mapping.get("service_key") else row
            for row in rows
        ]
    state["agent_mappings"] = _merge_by_key(
        rows,
        mapping,
        "service_key",
        "agent_subscription_id",
        "agent_id",
    )
    return _sync_operating_model_from_state(state)


@_patch_op("agent_binding.remove", "agent_mapping.remove", summary="unmap agents from a service by service_key")
def _op_agent_binding_remove(state, payload, raw_patch):
    key = str(payload.get("service_key") or payload.get("key") or "").strip()
    if key:
        state["agent_mappings"] = [
            dict(row)
            for row in _as_list(state.get("agent_mappings"))
            if isinstance(row, dict) and str(row.get("service_key") or "").strip() != key
        ]
    return _sync_operating_model_from_state(state)


@_patch_op("agent_mappings.replace", "agent_mapping.replace", summary="replace all agent↔service mappings")
def _op_agent_mappings_replace(state, payload, raw_patch):
    state["agent_mappings"] = _as_list(
        payload.get("agent_mappings")
        or payload.get("mappings")
        or payload.get("agent_bindings")
        or []
    )
    return _sync_operating_model_from_state(state)


@_patch_op("process.upsert", "workflow.upsert", summary="insert or merge descriptive process metadata by process_key; does not create a runnable Workflow")
def _op_process_upsert(state, payload, raw_patch):
    process = _as_dict(payload.get("process") or payload)
    key = "process_key" if process.get("process_key") else "key"
    state["processes"] = _merge_by_key(state.get("processes"), process, key)
    return _sync_operating_model_from_state(state)


@_patch_op("strategist.update", "strategist_policy.update", summary="merge strategist policy")
def _op_strategist_update(state, payload, raw_patch):
    strategist = _as_dict(state.get("strategist"))
    strategist.update(payload)
    state["strategist"] = strategist
    return _sync_operating_model_from_state(state)


@_patch_op("task.prepare", "immediate_task.add", summary="queue an immediate task to create on apply")
def _op_task_prepare(state, payload, raw_patch):
    task = _as_dict(payload.get("task") or payload)
    state["immediate_tasks"] = _as_list(state.get("immediate_tasks")) + [task]
    return _sync_operating_model_from_state(state)


OPERATION_PATCH_OP_CATALOG: tuple[OperationPatchOp, ...] = tuple(_PATCH_CATALOG)
SUPPORTED_OPERATION_PATCH_OPS: tuple[str, ...] = tuple(_PATCH_HANDLERS)
_SKILL_BINDING_PATCH_OPS: frozenset[str] = frozenset(
    name
    for entry in OPERATION_PATCH_OP_CATALOG
    if entry.touches_skill_bindings
    for name in (entry.canonical, *entry.aliases)
)


def operation_patch_payload_schema(op: str) -> dict[str, Any] | None:
    for entry in OPERATION_PATCH_OP_CATALOG:
        if op in (entry.canonical, *entry.aliases) and entry.payload_schema is not None:
            return _copy_json(entry.payload_schema)
    return None


def operation_patch_item_schema() -> dict[str, Any]:
    """Keep typed patch payloads tied to the executable dispatcher catalog."""
    typed_names = set()
    branches = []
    for entry in OPERATION_PATCH_OP_CATALOG:
        if entry.payload_schema is None:
            continue
        names = [entry.canonical, *entry.aliases]
        typed_names.update(names)
        branches.append({
            "properties": {"op": {"enum": names}, "payload": _copy_json(entry.payload_schema)},
            "required": ["op", "payload"],
        })
    branches.append({"properties": {"op": {"enum": sorted(set(SUPPORTED_OPERATION_PATCH_OPS) - typed_names)}}})
    return {
        "type": "object",
        "properties": {"op": {"type": "string", "enum": list(SUPPORTED_OPERATION_PATCH_OPS)},
                       "payload": {"type": "object"}},
        "required": ["op"],
        "oneOf": branches,
    }


def operation_patches_require_lifecycle_lock(patches: list[dict[str, Any]]) -> bool:
    """Fence draft Skill and Workflow references before Workspace/Draft locks.

    Ordinary integration edits do not add reusable definitions and can proceed
    independently in separate Workspaces within the same entity.
    """
    return any(_normalise_patch(patch)[0] in (_SKILL_BINDING_PATCH_OPS | {"workflow_binding.attach"}) for patch in patches)


_RULE_TOUCHING_OPS: frozenset[str] = frozenset(
    name
    for entry in OPERATION_PATCH_OP_CATALOG
    if entry.touches_rules
    for name in (entry.canonical, *entry.aliases)
)
_GOVERNANCE_PATCH_OPS: frozenset[str] = frozenset(
    name
    for entry in OPERATION_PATCH_OP_CATALOG
    if entry.canonical == "governance_policy.update"
    for name in (entry.canonical, *entry.aliases)
)

_RULE_DERIVED_GUARDRAIL_NOTE = (
    "Guardrail lists (never_allow / hitl_required / auto_approve, actions and "
    "capabilities) are derived from rules that carry rule_type + "
    "action_patterns/capability_patterns and are re-derived on apply. To lift "
    "or soften a gate, change the rule with rule.remove / rule.update "
    "(e.g. rule_type draft_only → approval_required); governance_policy.update "
    "alone is overwritten by a surviving rule."
)


def describe_operation_patch_ops() -> str:
    """Compact, implementation-derived vocabulary for prompts and errors."""

    lines = [f"{entry.canonical} ({entry.summary})" for entry in OPERATION_PATCH_OP_CATALOG]
    return (
        "Patch ops: " + "; ".join(lines) + ". " + _RULE_DERIVED_GUARDRAIL_NOTE
        + " Integration removal is a configuration change: use integration.remove "
        "with provider/integration_key when the user no longer needs a platform in "
        "this workspace. rule.add only records instructions; it does not remove "
        "integration requirements or bindings. capability_binding.remove only "
        "removes one existing operation binding_key, not inherited Agent bindings. "
        "Use integration.enable alone to undo a workspace integration removal. "
        "Inherited connection requirements return automatically; do not add capability bindings "
        "or recreate requirements unless the user separately requests new configuration."
        ' The exact create_draft argument shape is {"action":"create_draft",'
        '"patches":[{"op":"integration.remove","payload":{"provider":"elevenlabs"}}]}. '
        + _INTEGRATION_REMOVE_EXAMPLE
        + " integration_scope.update accepts only disabled_providers:string[]; fields such as "
        "remove_bindings, remove_required_connections, preserve_integrations, credentials, "
        "rules, and automations are invalid, not instructions that the runtime can execute."
    )


def _unsupported_patch_op_error(op: str) -> ValueError:
    canonical = [entry.canonical for entry in OPERATION_PATCH_OP_CATALOG]
    operation_family = op.lower().split(".", 1)[0].replace("-", "_")
    same_family_ops = [
        candidate
        for candidate in SUPPORTED_OPERATION_PATCH_OPS
        if candidate.lower().split(".", 1)[0].replace("-", "_")
        == operation_family
    ]
    close = difflib.get_close_matches(op, same_family_ops, n=1, cutoff=0.6)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return ValueError(
        f"unsupported workspace operation patch: {op}.{hint} "
        f"Supported ops: {', '.join(canonical)}. {_RULE_DERIVED_GUARDRAIL_NOTE}"
    )


def _apply_single_patch(
    state: dict[str, Any],
    raw_patch: dict[str, Any],
) -> dict[str, Any]:
    op, payload = _normalise_patch(raw_patch)
    handler = _PATCH_HANDLERS.get(op)
    if handler is None:
        raise _unsupported_patch_op_error(op)
    _validate_operation_patch_payload(op, payload)
    return handler(state, payload, raw_patch)


def _dedupe_rule_dicts(rules: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            continue
        rule = dict(raw_rule)
        explicit_key = str(rule.get("rule_key") or "").strip()
        description = str(rule.get("description") or "").strip()
        dedupe = explicit_key or description
        if dedupe and dedupe in seen:
            continue
        if dedupe:
            seen.add(dedupe)
        rule["rule_key"] = explicit_key or f"rule_{idx + 1}"
        out.append(rule)
    return out


def _binding_identity(binding: dict[str, Any]) -> str:
    return runtime_capability_binding_identity(binding)


def _upsert_capability_binding(
    items: Any,
    raw_binding: dict[str, Any],
    *,
    default_type: str = "tool",
) -> list[dict[str, Any]]:
    # Owner scope first: ``binding_key`` is derived from it, and a binding the
    # model drafted without an owner must not be keyed ``unscoped`` and then
    # rejected as unowned when the intent (the workspace agent) is plain.
    binding = normalize_runtime_capability_binding(
        apply_runtime_binding_owner_scope_default(raw_binding),
        default_type=default_type,
    )
    rows = [dict(row) for row in _as_list(items) if isinstance(row, dict)]
    return _merge_by_key(rows, binding, "binding_key")


def _state_diff(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "services",
        "goals",
        "rules",
        "automations",
        "evaluation",
        "knowledge_policy",
        "capability_bindings",
        "skill_bindings",
        "integration_scope",
        "channel_config",
        "processes",
        "strategist",
        "agent_mappings",
        "governance_policy",
        "budget_policy",
        "heartbeat_policy",
        "immediate_tasks",
        "workflow_attachments",
    )
    changed: dict[str, Any] = {}
    for key in keys:
        before = base.get(key)
        after = target.get(key)
        if key == "rules" and _enrich_rules(before) == _enrich_rules(after):
            # Historical rules may omit derived capability/runtime metadata.
            # Filling it in does not change the rule's executable meaning.
            continue
        if key == "integration_scope":
            try:
                before_scope = _validated_integration_scope({} if before is None else before)
                after_scope = _validated_integration_scope({} if after is None else after)
                if before_scope.get("disabled_providers", []) == after_scope.get("disabled_providers", []):
                    continue
            except ValueError:
                # Invalid new state is rejected by validation. Removing legacy
                # unsupported metadata remains an explicit, visible repair.
                pass
        if before != after:
            changed[key] = {"before": _json_safe(before), "after": _json_safe(after)}
    # Whole-model operations also persist fields outside the projected lists.
    # Include those in the approval diff, otherwise a real edit can appear to
    # be a no-op (and an unreviewed model change can hide behind another patch).
    projected = set(keys) | {"knowledge", "governance"}
    before_extra = {key: value for key, value in _as_dict(base.get("operating_model")).items() if key not in projected}
    after_extra = {key: value for key, value in _as_dict(target.get("operating_model")).items() if key not in projected}
    if before_extra != after_extra:
        changed["operating_model"] = {"before": _json_safe(before_extra), "after": _json_safe(after_extra)}
    return {
        "changed_keys": list(changed.keys()),
        "changes": changed,
    }


#: The policy lists ``rules.replace`` rebuilds from scratch. Everything else on
#: the policy (risk ceiling, budget caps) survives the rebuild untouched.
_REBUILT_GOVERNANCE_POLICY_KEYS: frozenset[str] = frozenset({"approval"})

def _resolved_governance_policy(
    state: dict[str, Any],
    *,
    operating_rules: list[dict[str, Any]],
    rules_touched: bool,
    edited_governance_keys: set[str],
    previous_operating_rules: list[dict[str, Any]] | None = None,
):
    """The governance policy that applying this draft WOULD write.

    Shared by ``apply_operation_draft`` and the review card, and that sharing
    is the point. ``rules.replace`` rebuilds ``never_allow`` from the draft's
    rules — an agent can drop its own hard blocks that way — while the draft's
    stored ``governance_policy`` still shows the OLD lists, because the rebuild
    only happens at apply time. A review card computing its own version of
    this would be showing the user a second opinion, not the change.

    Returns ``None`` when the draft implies no policy at all (unchanged
    behaviour from ``_build_governance_policy_from_rules``).
    """
    from packages.core.services.workspace_setup_service import (
        _build_governance_policy_from_rules,
    )

    operating_model = _as_dict(state.get("operating_model"))
    raw_governance_policy = _as_dict(
        state.get("governance_policy")
        if "governance_policy" in state else operating_model.get("governance")
    )
    # Recompile source-owned rules only. User exceptions and the local mode
    # survive both a single-rule edit and replacement of the source list.
    return _build_governance_policy_from_rules(operating_rules, raw_governance_policy)


def _draft_rules_touched(patches: Any) -> bool:
    for patch in _as_list(patches):
        if not isinstance(patch, dict):
            continue
        op, payload = _normalise_patch(patch)
        if op in _RULE_TOUCHING_OPS:
            return True
        if op in {"operating_model.update", "workspace_model.update"}:
            operating_model = _as_dict(payload.get("operating_model") or payload)
            if "rules" in operating_model:
                return True
    return False


def _draft_patch_ops(patches: Any) -> set[str]:
    return {
        _normalise_patch(patch)[0]
        for patch in _as_list(patches)
        if isinstance(patch, dict)
    }


_OPERATING_MODEL_UPDATE_OPS: frozenset[str] = frozenset({"operating_model.update", "workspace_model.update"})


def _draft_edited_governance_keys(patches: Any) -> set[str]:
    """Governance keys the draft itself sets.

    Only ``governance_policy.update`` (and aliases) or an
    ``operating_model.update`` carrying a ``governance`` key edit governance.
    ``operating_model.replace`` payloads usually echo the workspace's
    ``governance`` block back, but the replace handler ignores it, so it is
    not an edit. Diffing draft state against the live workspace cannot answer
    this question: live edits made after the draft was created show up there
    as "changes" too.
    """

    keys: set[str] = set()
    for patch in _as_list(patches):
        if not isinstance(patch, dict):
            continue
        op, payload = _normalise_patch(patch)
        if op in _GOVERNANCE_PATCH_OPS:
            keys.update(
                _as_dict(payload.get("governance_policy") or payload.get("governance") or payload)
            )
        elif op in _OPERATING_MODEL_UPDATE_OPS:
            om_update = _as_dict(payload.get("operating_model") or payload)
            if "governance" in om_update or "governance_policy" in om_update:
                keys.update(_as_dict(om_update.get("governance") or om_update.get("governance_policy")))
    return keys


def _effective_operation_state(current: dict[str, Any], draft: WorkspaceOperationDraft) -> dict[str, Any]:
    """Resolve the same post-apply state for validation, review, and apply.

    Explicitly replacing the rules can repair stale inferred governance. Conversely,
    a draft must not claim changes to an old policy snapshot that apply preserves
    from the live workspace. Only explicitly edited policy keys come from the
    draft; all other policy values, including live operator consent, stay live.
    """
    from packages.core.governance import policy_to_dict

    state = _copy_json(_as_dict(draft.current_state))
    rules_changed = _enrich_rules(current.get("rules")) != _enrich_rules(state.get("rules"))
    edited_governance_keys = _draft_edited_governance_keys(draft.patches)
    draft_policy = _as_dict(state.get("governance_policy"))
    policy_values = _copy_json(_as_dict(current.get("governance_policy")))
    for key in edited_governance_keys & {"max_risk_level", "budget_caps_per_kind"}:
        if key in draft_policy:
            policy_values[key] = _copy_json(draft_policy[key])
    state["governance_policy"] = policy_values
    rules_touched = _draft_rules_touched(draft.patches)
    rules_replaced = bool(_draft_patch_ops(draft.patches) & {"rules.replace", "guardrails.replace"})
    # Repeating a single-rule update is a no-op, not a request to repair or
    # rewrite unrelated governance drift. Only explicit full-list replacement
    # rebuilds inferred policy even when the rule list itself is unchanged.
    if rules_replaced or rules_changed or policy_values != _as_dict(current.get("governance_policy")):
        policy = _resolved_governance_policy(
            state,
            operating_rules=_enrich_rules(_as_dict(state.get("operating_model")).get("rules") or state.get("rules") or []),
            rules_touched=rules_touched,
            edited_governance_keys=edited_governance_keys,
            previous_operating_rules=(
                _enrich_rules(current.get("rules") or [])
                if rules_touched and not rules_replaced else None
            ),
        )
        if policy is not None:
            state["governance_policy"] = policy_to_dict(policy)
    return _sync_operating_model_from_state(state)


def _operation_state_diff(current: dict[str, Any], draft: WorkspaceOperationDraft) -> dict[str, Any]:
    effective_state = _effective_operation_state(current, draft)
    diff = _state_diff(current, effective_state)
    # Review needs current policy evidence even when it did not change. The
    # draft snapshot can predate a separate Governance API edit, so absence
    # from changed_keys must not send a fresh review through legacy fallback.
    diff["effective_governance_policy"] = {
        "before": _copy_json(_as_dict(current.get("governance_policy"))),
        "after": _copy_json(_as_dict(effective_state.get("governance_policy"))),
    }
    return diff


def _policy_list(policy: Any, field: str) -> list[str]:
    if policy is None:
        return []
    if isinstance(policy, dict):
        return _unique_list(policy.get(field))
    return _unique_list(getattr(policy, field, None))


def operation_review_diff(draft_data: dict[str, Any]) -> dict[str, Any]:
    """What this draft actually changes, in terms a person can act on.

    The workspace-operation review card used to say, in full: "Apply this
    workspace operation draft?" — a card demanding approval without saying
    what for. That is the same defect as the incident this phase exists to
    fix, and it is worse here than elsewhere: ``rules.replace`` REBUILDS the
    ``never_allow`` list, so a draft can quietly delete the workspace's hard
    blocks (``billing.*``, ``payments.refund``, ``*.delete_*``) — the one tier
    of governance with no approval path at all — and the operator would have
    read nothing about it before pressing Approve.

    So the diff is mandatory (``HITL_REQUIRED_PAYLOAD_FIELDS["review"]``) and
    carries ``removed_hard_blocks`` computed from the policy the apply would
    really write, not from the draft's stale stored copy.
    """
    state = _as_dict(draft_data.get("current_state"))
    stored_diff = _as_dict(draft_data.get("diff"))
    changes = _as_dict(stored_diff.get("changes"))
    changed_keys = _unique_list(stored_diff.get("changed_keys"))

    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    for key in changed_keys:
        change = _as_dict(changes.get(key))
        before, after = change.get("before"), change.get("after")
        before_empty = before in (None, [], {}, "")
        after_empty = after in (None, [], {}, "")
        if before_empty and not after_empty:
            added.append(key)
        elif after_empty and not before_empty:
            removed.append(key)
        else:
            modified.append(key)

    # The hard-block delta. ``before`` is what governs the workspace right
    # now; ``after`` is what an apply would install.
    governance_evidence = (
        _as_dict(stored_diff.get("effective_governance_policy"))
        or _as_dict(changes.get("governance_policy"))
    )
    before_policy = (
        _as_dict(governance_evidence.get("before"))
        if governance_evidence
        else _as_dict(state.get("governance_policy"))
    )
    from packages.core.services.workspace_setup_service import (
        _enrich_operating_rules,
    )

    operating_rules = _enrich_operating_rules(
        _as_dict(state.get("operating_model")).get("rules")
        or _as_list(state.get("rules"))
    )
    rules_touched = _draft_rules_touched(draft_data.get("patches"))
    rules_replaced = bool(_draft_patch_ops(draft_data.get("patches")) & {"rules.replace", "guardrails.replace"})
    # Fresh previews already resolve the exact post-apply policy against live
    # state. Re-deriving from the draft's older snapshot could invent removals
    # of hard blocks that apply actually preserves. Legacy cards lack this.
    after_policy = (
        _as_dict(governance_evidence["after"])
        if "after" in governance_evidence
        else _resolved_governance_policy(
            state,
            operating_rules=operating_rules,
            rules_touched=rules_touched,
            edited_governance_keys=_draft_edited_governance_keys(draft_data.get("patches")),
            # Old single-rule cards may lack a resolved policy diff. Remove
            # only gates evidenced by their old rules; missing before-state
            # cannot prove that an independent hard block will be lifted.
            previous_operating_rules=(
                _enrich_operating_rules(_as_dict(changes.get("rules")).get("before"))
                if rules_touched and not rules_replaced else None
            ),
        )
    )

    from packages.core.governance.approval_projection import policy_override, selector_label
    before_denials = {selector_label(rule) for rule in policy_override(before_policy).rules if rule.effect == "deny"}
    after_denials = {selector_label(rule) for rule in policy_override(after_policy).rules if rule.effect == "deny"}
    removed_hard_blocks = sorted(before_denials - after_denials)

    integration_change = _as_dict(changes.get("integration_scope"))
    before_disabled = {
        canonical_provider_key(value)
        for value in _as_list(_as_dict(integration_change.get("before")).get("disabled_providers"))
    }
    after_disabled = {
        canonical_provider_key(value)
        for value in _as_list(_as_dict(integration_change.get("after")).get("disabled_providers"))
    }

    return {
        "changed_keys": changed_keys,
        "added": added,
        "removed": removed,
        "modified": modified,
        # Named for what it means to the reader, not for the column it came
        # from: these are the gates that stop being gates.
        "removed_hard_blocks": _unique_list(removed_hard_blocks),
        "disabled_integrations": sorted(after_disabled - before_disabled - {""}),
        "enabled_integrations": sorted(before_disabled - after_disabled - {""}),
    }


def operation_draft_review_fingerprint(
    draft: WorkspaceOperationDraft | dict[str, Any],
) -> str:
    """Bind a review to the exact draft contents, independent of JSON key order."""
    data = draft if isinstance(draft, dict) else {
        key: getattr(draft, key)
        for key in ("id", "workspace_id", "base_revision", "current_state", "patches", "diff")
    }
    reviewed = {
        key: data.get(key)
        for key in ("id", "workspace_id", "base_revision", "current_state", "patches", "diff")
    }
    return hashlib.sha256(json.dumps(
        reviewed, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()


def operation_review_hitl_payload(
    draft_data: dict[str, Any],
    *,
    why: str | None = None,
) -> dict[str, Any]:
    """The ``HitlType.REVIEW`` payload for a workspace-operation draft.

    Runs through the record layer's own validator even though this card is not
    (yet) backed by an ``HitlRequest`` row: "a review must carry a diff" is
    a rule of the record layer, and a rule enforced only on the path nothing
    takes is not a rule.
    """
    from packages.core.constants.approvals import HitlType
    from packages.core.governance.approvals import validate_hitl_payload

    diff = operation_review_diff(draft_data)
    removed_hard_blocks = diff.get("removed_hard_blocks") or []
    if removed_hard_blocks:
        reason = (
            "This draft REMOVES hard blocks that currently cannot be approved "
            "around at all: "
            + ", ".join(removed_hard_blocks)
            + ". After applying, these hard blocks are removed; any remaining "
            "approval requirements and workspace rules still apply."
        )
    else:
        changed = ", ".join(diff.get("changed_keys") or []) or "workspace runtime"
        reason = (
            f"These changes take effect workspace-wide once applied: {changed}."
        )
    disabled = diff.get("disabled_integrations") or []
    enabled = diff.get("enabled_integrations") or []
    if disabled:
        reason += (
            " Stop requiring and using these integrations in this workspace: "
            + ", ".join(disabled)
            + ". Account connections and shared Agent bindings remain unchanged."
        )
    if enabled:
        reason += (
            " Restore this workspace's inherited integration requirements and access: "
            + ", ".join(enabled)
            + ". Missing account setup may be required again."
        )
    if set(diff.get("changed_keys") or []) == {"rules"}:
        reason += " Rules alone do not modify integration bindings or connection requirements."
    recovery = _as_dict(_as_dict(draft_data.get("current_state")).get("task_capability_recovery"))
    if recovery.get("task_id"):
        reason += (
            " Configure this Agent deployment: "
            + str(recovery.get("agent_name") or "selected Agent")
            + "; tools: " + ", ".join(_unique_list(recovery.get("tool_names"))) + "."
            " After applying, continue the existing task: "
            + str(recovery.get("task_title") or recovery["task_id"])
            + ". It will resume only if its Agent deployment and stopped execution still match this review."
        )
        diff["resume_task_id"] = recovery["task_id"]
    changes = _as_dict(_as_dict(draft_data.get("diff")).get("changes"))
    for section in ("services", "agent_mappings"):
        change = _as_dict(changes.get(section))
        if not isinstance(change.get("before"), list) or not isinstance(change.get("after"), list):
            continue
        snapshots = [
            {
                str(row.get("service_key") or row.get("key")): row
                for row in change[side]
                if isinstance(row, dict) and (row.get("service_key") or row.get("key"))
            }
            for side in ("before", "after")
        ]
        before, after = snapshots
        subject = "services" if section == "services" else "Agent bindings for services"
        for verb, source, other in (("Add", after, before), ("Remove", before, after)):
            labels = [
                str(row.get("name") or key) if section == "services" else key
                for key, row in source.items() if key not in other
            ]
            if labels:
                reason += f" {verb} {subject}: {', '.join(labels)}."
        if section == "agent_mappings":
            replaced = [
                key for key, row in after.items()
                if key in before and (
                    row.get("agent_id") or row.get("recommended_agent_id") or row.get("create_agent_draft")
                ) != (
                    before[key].get("agent_id") or before[key].get("recommended_agent_id")
                    or before[key].get("create_agent_draft")
                )
            ]
            if replaced:
                reason += f" Replace Agents for services: {', '.join(replaced)}."

    return validate_hitl_payload(
        HitlType.REVIEW.value,
        {"diff": diff, "why": why or reason},
    )


async def get_current_operation_state(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
) -> dict[str, Any] | None:
    workspace = await _load_workspace(db, workspace_id, entity_id)
    if workspace is None:
        return None
    return await _snapshot_workspace_state(db, workspace)


async def create_operation_draft(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    user_id: str | None = None,
    source_event_id: str | None = None,
    initial_patches: list[dict[str, Any]] | None = None,
) -> WorkspaceOperationDraft | None:
    # Match purge/apply order before taking a snapshot or adding references.
    if operation_patches_require_lifecycle_lock(initial_patches or []):
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace = await _load_workspace(db, workspace_id, entity_id, for_update=True)
    if workspace is None:
        return None
    current = await _snapshot_workspace_state(db, workspace)
    state = _copy_json(current)
    base_skill_ids = reusable_skill_ids_from_runtime_state(state)
    patches: list[dict[str, Any]] = []
    for patch in initial_patches or []:
        state = _apply_single_patch(state, patch)
        patches.append(_json_safe(patch))
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        skill_ids=(
            reusable_skill_ids_from_runtime_state(state) - base_skill_ids
        ),
        workflow_ids={row["workflow_id"] for row in state.get("workflow_attachments") or []},
    )
    draft = WorkspaceOperationDraft(
        id=generate_ulid(),
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
        created_by_user_id=user_id,
        source_event_id=source_event_id,
        base_revision=int(workspace.operation_revision or 0),
        status=WorkspaceOperationDraftStatus.OPEN,
        current_state=_json_safe(state),
        patches=patches,
    )
    db.add(draft)
    await db.flush()
    draft.validation = await validate_operation_draft(db, draft, current_state=current)
    draft.diff = await preview_operation_diff(db, draft, current_state=current)
    await db.flush()
    return draft


async def get_operation_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    workspace_id: str | None = None,
    *,
    for_update: bool = False,
) -> WorkspaceOperationDraft | None:
    filters = [
        WorkspaceOperationDraft.id == draft_id,
        WorkspaceOperationDraft.entity_id == entity_id,
    ]
    if workspace_id:
        filters.append(WorkspaceOperationDraft.workspace_id == workspace_id)
    query = select(WorkspaceOperationDraft).where(*filters)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def patch_operation_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    workspace_id: str,
    patches: list[dict[str, Any]],
) -> WorkspaceOperationDraft | None:
    if operation_patches_require_lifecycle_lock(patches):
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace = await _load_workspace(db, workspace_id, entity_id, for_update=True)
    if workspace is None:
        return None
    draft = await get_operation_draft(db, draft_id, entity_id, workspace_id, for_update=True)
    if draft is None:
        return None
    if draft.status != WorkspaceOperationDraftStatus.OPEN:
        raise OperationConflictError(f"cannot patch {draft.status} operation draft")
    state = _copy_json(draft.current_state or {})
    base_skill_ids = reusable_skill_ids_from_runtime_state(state)
    appended = _as_list(draft.patches)
    for patch in patches:
        state = _apply_single_patch(state, patch)
        appended.append(_json_safe(patch))
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        skill_ids=(
            reusable_skill_ids_from_runtime_state(state) - base_skill_ids
        ),
        workflow_ids={row["workflow_id"] for row in state.get("workflow_attachments") or []},
    )
    draft.current_state = _json_safe(state)
    draft.patches = appended
    current = await _snapshot_workspace_state(db, workspace)
    draft.validation = await validate_operation_draft(db, draft, current_state=current)
    draft.diff = await preview_operation_diff(db, draft, current_state=current)
    await db.flush()
    return draft


async def refresh_operation_draft_review(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    workspace_id: str,
) -> WorkspaceOperationDraft | None:
    """Refresh open review metadata without racing edits or terminal receipts."""
    workspace = await _load_workspace(db, workspace_id, entity_id, for_update=True)
    if workspace is None:
        return None
    draft = await get_operation_draft(db, draft_id, entity_id, workspace_id, for_update=True)
    if draft is not None and draft.status == WorkspaceOperationDraftStatus.OPEN:
        current = await _snapshot_workspace_state(db, workspace)
        draft.validation = await validate_operation_draft(db, draft, current_state=current)
        draft.diff = await preview_operation_diff(db, draft, current_state=current)
        await db.flush()
    return draft


def rule_implied_governance_conflicts(
    rules: list[dict[str, Any]],
    governance_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Explain governance edits that a surviving rule will undo."""

    from packages.core.governance.approval_projection import compile_workspace_rules, policy_override

    present = {r.id: r for r in policy_override(governance_policy).rules}
    warnings = []
    for rule in compile_workspace_rules(rules):
        if present.get(rule.id) != rule:
            warnings.append({
                "path": "governance_policy.approval",
                "message": f"Approval rule {rule.source_id!r} is owned by its Workspace source; edit that source instead.",
            })
    return warnings


def _enforcement_conflict_warnings(
    rules: list[dict[str, Any]],
    governance_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Hard block + approval on the same pattern: the block wins silently."""

    from packages.core.services.workspace_setup_service import (
        describe_rule_enforcement_conflict,
        rule_enforcement_conflicts,
    )

    warnings: list[dict[str, Any]] = [
        {
            "path": f"rules.{conflict['pattern']}",
            "message": describe_rule_enforcement_conflict(conflict),
        }
        for conflict in rule_enforcement_conflicts(rules)
    ]
    from packages.core.governance.approval_projection import policy_override
    effects: dict[str, set[str]] = {}
    for rule in policy_override(governance_policy).rules:
        key = json.dumps(rule.selector.model_dump(), sort_keys=True)
        effects.setdefault(key, set()).add(rule.effect)
    if any({"deny", "ask"} <= values for values in effects.values()):
        warnings.append({"path": "governance_policy.approval", "message": "A prohibition and approval rule apply to the same selector; the prohibition wins."})
    return warnings


async def validate_operation_draft(
    db: AsyncSession,
    draft: WorkspaceOperationDraft,
    *,
    current_state: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Validate what this draft changes, not everything the Workspace holds.

    ``errors`` (which block review and apply) are only the problems the draft
    introduces. Problems the live Workspace already had are returned as
    ``pre_existing_issues`` and repeated in ``warnings``: an older install or
    edit cannot veto every later, unrelated change (R2-4), and is never
    silently dropped either. Identity is path shape + code + message counted
    as a multiset, so shifted indexes stay inherited and a second copy is new.
    """
    current = (
        current_state
        if current_state is not None
        else await get_current_operation_state(db, draft.workspace_id, draft.entity_id)
    )
    after_errors, warnings, missing_setup = await _collect_operation_draft_issues(
        db, draft, current, user_id=user_id,
    )
    errors, pre_existing = after_errors, []
    if after_errors and current is not None:
        # The same checks over the unchanged live state. A no-op draft takes
        # no change-scoped branch (domain values, readiness, channels, patch
        # shapes), so this is exactly what the Workspace already carried.
        baseline = SimpleNamespace(
            id=draft.id, entity_id=draft.entity_id, workspace_id=draft.workspace_id,
            created_by_user_id=draft.created_by_user_id,
            current_state=_copy_json(current), patches=[],
        )
        baseline_errors, _, _ = await _collect_operation_draft_issues(
            db, baseline, current, user_id=user_id,
        )
        errors, pre_existing = partition_new_validation_errors(
            baseline_errors, after_errors,
            before_state=current, after_state=_as_dict(draft.current_state),
        )
        warnings = [*warnings, *(
            {
                **issue,
                "code": "pre_existing_issue",
                "original_code": issue.get("code"),
                "message": f"Existing issue, not introduced by this change: {issue.get('message')}",
            }
            for issue in pre_existing
        )]

    # The effective policy diff reads typed values too; malformed drafts must
    # retain structured validation errors instead of entering normalization.
    if not errors and current is not None:
        try:
            effective_state = _effective_operation_state(current, draft)
        except (TypeError, ValueError):
            # Only inherited errors remain, and an inherited malformed value can
            # still fail normalization; the raw draft state answers "no change".
            effective_state = _as_dict(draft.current_state)
        if not _state_diff(current, effective_state)["changed_keys"]:
            errors.append({
                "path": "patches",
                "code": "no_changes",
                "message": (
                    "This draft makes no changes to the current workspace. "
                    "It cannot be approved or applied; inspect the current state and "
                    "target an existing binding_key, or use integration.remove with "
                    "provider/integration_key to disable a workspace integration."
                ),
            })

    validation = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "pre_existing_issues": pre_existing,
        "missing_setup": missing_setup,
        "budget_impact": _budget_impact(current or {}, _as_dict(draft.current_state)),
        "checked_at": _utcnow().isoformat(),
    }
    draft.validation = _json_safe(validation)
    return draft.validation


async def _collect_operation_draft_issues(
    db: AsyncSession,
    draft: Any,
    current: dict[str, Any] | None,
    *,
    user_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Every problem of the draft's resulting state: (errors, warnings, missing_setup)."""
    state = _as_dict(draft.current_state)
    changed_domains = set(_state_diff(current or {}, state)["changed_keys"])
    errors: list[dict[str, Any]] = list(validate_operation_domain_values(
        state, changed_keys=changed_domains,
    ))
    warnings: list[dict[str, Any]] = []
    missing_setup: list[str] = []

    current_app_view = _as_dict(_as_dict((current or {}).get("operating_model")).get("app_view"))
    requested_app_view = _as_dict(_as_dict(state.get("operating_model")).get("app_view"))
    available_app_actions: set[str] = set()
    if current_app_view != requested_app_view or any(row.get("action_key") for row in state.get("workflow_attachments") or []):
        from packages.core.services.workspace_app_view import normalize_app_view_config, app_target_kind_and_id
        from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints

        def references(config: dict[str, Any]) -> set[str]:
            return set(config["entries"]) | set(config["prompts"]) | set(config["order"]) | set(config["hidden"]) | set(config["labels"]) | set(config["descriptions"]) | set(config["forms"]) | set(config["targets"]) | (
                {config["hero"]} if config["hero"] else set()
            )

        try:
            requested_config = normalize_app_view_config(requested_app_view)
            previous_config = normalize_app_view_config(current_app_view)
        except ValueError as exc:
            errors.append({"path": "app_view", "code": "invalid_app_view", "message": str(exc)})
        else:
            from packages.core.models.user import User
            actor_id = user_id or draft.created_by_user_id
            actor = await db.get(User, actor_id) if actor_id else None
            entrypoints = await list_workspace_chat_entrypoints(
                db, entity_id=draft.entity_id, workspace_id=draft.workspace_id,
                user=actor, require_control=True, include_unpublished=True,
            )
            from packages.core.models.scheduler import ScheduledJob
            job_rows = (await db.execute(select(ScheduledJob).where(
                ScheduledJob.entity_id == draft.entity_id,
                ScheduledJob.workspace_id == draft.workspace_id,
                ScheduledJob.agent_initiative_id.is_(None),
            ))).scalars().all()
            job_ids = {
                job.job_id for job in job_rows
                if (job.execution_target or {}).get("managed_by") != "agent_initiative"
            }
            agent_ids = set((await db.execute(select(AgentSubscription.agent_id).join(
                Agent, Agent.id == AgentSubscription.agent_id,
            ).where(
                AgentSubscription.entity_id == draft.entity_id,
                AgentSubscription.workspace_id == draft.workspace_id,
                AgentSubscription.status == "active",
                Agent.deleted_at.is_(None),
            ))).scalars())
            binding_ids = {entry.binding_id for entry in entrypoints}
            available_app_actions = {
                f"workflow:{entry.binding_id}" for entry in entrypoints
            } | {
                f"service:{service.get('key') or service.get('service_key')}"
                for service in _as_list(state.get("services"))
                if isinstance(service, dict) and (service.get("key") or service.get("service_key"))
            } | {f"automation:{job_id}" for job_id in job_ids} | set(requested_config["entries"])
            pending_entry_targets = {row.get("action_key") for row in state.get("workflow_attachments") or []}
            for key in requested_config["entries"]:
                if key not in requested_config["targets"] and key not in pending_entry_targets:
                    errors.append({
                        "path": f"app_view.entries.{key}", "code": "missing_app_target",
                        "message": f"App entry must select an Agent, Workflow, or Automation: {key}",
                    })
            unknown = sorted(references(requested_config) - references(previous_config) - available_app_actions)
            if unknown:
                errors.append({
                    "path": "app_view", "code": "unknown_app_action",
                    "message": f"App View references unavailable actions: {', '.join(unknown)}",
                })
            for key, target in requested_config["targets"].items():
                if previous_config["targets"].get(key) == target:
                    continue
                kind, target_id = app_target_kind_and_id(key, target)
                allowed = {"workflow": binding_ids, "chat": agent_ids | {"default"}, "automation": job_ids}[kind]
                if target_id not in allowed:
                    errors.append({
                        "path": f"app_view.targets.{key}", "code": "unavailable_app_target",
                        "message": f"App action target is unavailable: {key}",
                    })

    # Open drafts created by an older runtime may already contain a coerced
    # snapshot. Recheck their original requested shapes before allowing apply.
    for idx, patch in enumerate(_as_list(draft.patches)):
        try:
            op, payload = _normalise_patch(patch)
            _validate_operation_patch_payload(op, payload)
        except ValueError as exc:
            errors.append({
                "path": f"patches[{idx}]", "code": "invalid_patch_shape", "message": str(exc),
            })

    # Resolve valid drafts against live policy, but keep malformed requested
    # values on the structured validation path instead of normalizing them.
    effective_state = _effective_operation_state(current or {}, draft) if not errors else state

    services = [row for row in _as_list(state.get("services")) if isinstance(row, dict)]
    service_keys = {
        str(row.get("key") or row.get("service_key") or "").strip()
        for row in services
        if str(row.get("key") or row.get("service_key") or "").strip()
    }

    for idx, attachment in enumerate(state.get("workflow_attachments") or []):
        path = f"workflow_attachments[{idx}]"
        if attachment.get("service_key") and attachment["service_key"] not in service_keys:
            errors.append({"path": path, "message": "Workflow attachment references an unknown service"})
            continue
        if attachment.get("action_key") and attachment["action_key"] not in available_app_actions:
            errors.append({"path": path, "code": "unknown_app_action", "message": "Workflow attachment references an unavailable App entry"})
            continue
        try:
            await _resolve_workflow_attachment(
                db, attachment, entity_id=draft.entity_id, workspace_id=draft.workspace_id,
                user_id=user_id or draft.created_by_user_id,
            )
        except ValueError as exc:
            errors.append({"path": path, "code": "unavailable_workflow", "message": str(exc)})

    for idx, service in enumerate(services):
        key = str(service.get("key") or service.get("service_key") or "").strip()
        if not key:
            errors.append({"path": f"services[{idx}].key", "message": "service requires key/service_key"})

    seen_goal_keys: dict[str, int] = {}
    for idx, goal in enumerate(_as_list(state.get("goals"))):
        if not isinstance(goal, dict):
            errors.append({"path": f"goals[{idx}]", "message": "goal must be an object"})
            continue
        goal_key = str(
            goal.get("goal_key")
            or goal.get("key")
            or goal.get("metric_key")  # legacy single-goal identity
            or ""
        ).strip()
        if not goal_key:
            errors.append({
                "path": f"goals[{idx}].goal_key",
                "message": "goal requires goal_key (or legacy metric_key)",
            })
            continue
        previous_idx = seen_goal_keys.get(goal_key)
        if previous_idx is not None:
            errors.append({
                "path": f"goals[{idx}].goal_key",
                "message": f"goal_key duplicates goals[{previous_idx}]",
            })
        else:
            seen_goal_keys[goal_key] = idx

    current_mappings = _as_list((current or {}).get("agent_mappings"))
    live_deployments_per_service: dict[str, int] = {}
    for row in current_mappings:
        key = str(row.get("service_key") or "").strip() if isinstance(row, dict) else ""
        if key:
            live_deployments_per_service[key] = live_deployments_per_service.get(key, 0) + 1
    seen_service_keys: set[str] = set()
    for idx, mapping in enumerate(_as_list(state.get("agent_mappings"))):
        if not isinstance(mapping, dict):
            errors.append({"path": f"agent_mappings[{idx}]", "message": "agent mapping must be an object"})
            continue
        service_key = str(mapping.get("service_key") or "").strip()
        if (
            service_key and mapping not in current_mappings
            and live_deployments_per_service.get(service_key, 0) > 1
        ):
            # Apply cannot pick which of the live deployments to rewrite; say
            # so on the review instead of letting it fail at apply.
            errors.append({
                "path": f"agent_mappings[{idx}].service_key",
                "code": "duplicate_service_change",
                "message": (
                    f"service {service_key!r} has more than one active Agent; "
                    "resolve that duplicate before changing its mapping"
                ),
            })
        if not service_key and mapping not in current_mappings:
            errors.append({"path": f"agent_mappings[{idx}].service_key", "message": "agent mapping requires service_key"})
        if service_key and service_key in seen_service_keys:
            errors.append({"path": f"agent_mappings[{idx}].service_key", "message": "only one Agent may be mapped to each service_key"})
        seen_service_keys.add(service_key)
        if service_key and service_keys and service_key not in service_keys:
            errors.append({
                "path": f"agent_mappings[{idx}].service_key",
                "message": f"agent mapping references unknown service {service_key!r}",
            })
        agent_id = str(mapping.get("agent_id") or mapping.get("recommended_agent_id") or "").strip()
        if not agent_id and mapping.get("strategy") != "create_custom":
            errors.append({
                "path": f"agent_mappings[{idx}].agent_id",
                "message": "agent mapping requires agent_id/recommended_agent_id or strategy=create_custom with create_agent_draft",
            })
        if "config" in mapping and not isinstance(mapping["config"], dict):
            errors.append({"path": f"agent_mappings[{idx}].config", "message": "subscription config must be an object"})
        if "create_agent_draft" in mapping and not isinstance(mapping["create_agent_draft"], dict):
            errors.append({"path": f"agent_mappings[{idx}].create_agent_draft", "message": "create_agent_draft must be an object"})
        create_draft = _as_dict(mapping.get("create_agent_draft"))
        for field in ("agent_name", "agent_description", "system_prompt", "system_prompt_seed", "agent_slug"):
            if field in create_draft and not isinstance(create_draft[field], str):
                errors.append({
                    "path": f"agent_mappings[{idx}].create_agent_draft.{field}",
                    "message": f"{field} must be a string",
                })
        for field in ("tool_bindings", "skill_bindings", "mcp_bindings", "business_capabilities", "skill_binding_refs", "missing_skill_specs"):
            if field not in create_draft:
                continue
            values = create_draft[field]
            item_type = dict if field in {"skill_binding_refs", "missing_skill_specs"} else str
            if not isinstance(values, list) or any(not isinstance(value, item_type) for value in values):
                errors.append({
                    "path": f"agent_mappings[{idx}].create_agent_draft.{field}",
                    "message": f"{field} must be an array of {'objects' if item_type is dict else 'strings'}",
                })
        if agent_id and mapping.get("strategy") == "create_custom":
            errors.append({
                "path": f"agent_mappings[{idx}].agent_id",
                "message": "choose an existing agent_id or strategy=create_custom, not both",
            })

    agent_ids_by_idx: dict[int, str] = {}
    service_by_agent_id: dict[str, str] = {}
    for idx, mapping in enumerate(_as_list(state.get("agent_mappings"))):
        if not isinstance(mapping, dict):
            continue
        agent_id = str(
            mapping.get("agent_id")
            or mapping.get("recommended_agent_id")
            or ""
        ).strip()
        if agent_id:
            agent_ids_by_idx[idx] = agent_id
            service_key = str(mapping.get("service_key") or "").strip()
            previous_service = service_by_agent_id.get(agent_id)
            if previous_service is not None and previous_service != service_key:
                errors.append({
                    "path": f"agent_mappings[{idx}].agent_id",
                    "message": (
                        f"agent is already mapped to service {previous_service!r}; "
                        "one Agent can have only one deployment per Workspace. "
                        "Create a distinct Agent to add another service."
                    ),
                })
            service_by_agent_id[agent_id] = service_key
    if agent_ids_by_idx:
        rows = list((await db.execute(
            select(Agent).where(
                Agent.id.in_(set(agent_ids_by_idx.values())),
                Agent.deleted_at.is_(None),
                Agent.status == "active",
            )
        )).scalars().all())
        agents_by_id = {agent.id: agent for agent in rows}
        for idx, agent_id in agent_ids_by_idx.items():
            agent = agents_by_id.get(agent_id)
            if agent is None:
                errors.append({
                    "path": f"agent_mappings[{idx}].agent_id",
                    "message": f"agent {agent_id!r} was not found",
                })
                continue
            if agent.entity_id and agent.entity_id != draft.entity_id and not agent.is_template:
                errors.append({
                    "path": f"agent_mappings[{idx}].agent_id",
                    "code": "foreign_entity_agent",
                    "message": "agent belongs to another entity",
                })

    _validate_capability_bindings(
        state.get("capability_bindings"),
        service_keys,
        errors,
        prefix="capability_bindings",
    )
    _validate_new_task_owned_bindings(draft, current or {}, errors)
    _validate_capability_bindings(
        state.get("skill_bindings"),
        service_keys,
        errors,
        prefix="skill_bindings",
    )
    # A Workspace Chat request may grant MCP tools only through an integration
    # already usable by this actor. Recheck at approval/apply time as well as
    # at draft creation, since connections can be removed while a card is open.
    def mcp_binding_provider(binding: Any) -> str:
        if not isinstance(binding, dict):
            return ""
        from packages.core.services.workspace_integration_scope import integration_provider_for_tool

        tool_provider = integration_provider_for_tool(
            binding.get("tool_name") or binding.get("capability") or binding.get("capability_id")
        )
        if tool_provider:
            return tool_provider
        if binding.get("capability_type") not in {"mcp", "integration"}:
            return ""
        return canonical_provider_key(
            binding.get("mcp_server_key") or binding.get("integration_key")
            or binding.get("capability_id") or binding.get("capability")
        )

    current_bindings = {
        str(row.get("binding_key") or ""): row
        for row in _as_list((current or {}).get("capability_bindings"))
        if isinstance(row, dict)
    }
    requested_integrations = {
        mcp_binding_provider(row)
        for row in _as_list(state.get("capability_bindings"))
        if isinstance(row, dict)
        and row != current_bindings.get(str(row.get("binding_key") or ""))
        and mcp_binding_provider(row)
    }
    before_disabled = {
        canonical_provider_key(value)
        for value in _as_list(_as_dict((current or {}).get("integration_scope")).get("disabled_providers"))
        if isinstance(value, str) and canonical_provider_key(value)
    }
    after_disabled = {
        canonical_provider_key(value)
        for value in _as_list(_as_dict(state.get("integration_scope")).get("disabled_providers"))
        if isinstance(value, str) and canonical_provider_key(value)
    }
    # Internal Blueprint restore may intentionally reinstate a live setup
    # requirement before the account exists. User-authored Chat changes may not.
    if user_id or draft.created_by_user_id:
        requested_integrations.update(before_disabled - after_disabled)
    if requested_integrations:
        from packages.core.services.integration_resolution import integration_provider_readiness

        readiness = await integration_provider_readiness(
            db,
            entity_id=draft.entity_id,
            user_id=user_id or draft.created_by_user_id,
            provider_keys=sorted(requested_integrations),
        )
        for provider in sorted(requested_integrations):
            if not readiness.get(provider) or not readiness[provider].ready:
                errors.append({
                    "path": "capability_bindings" if provider not in before_disabled else "integration_scope",
                    "code": "integration_not_connected",
                    "message": f"Integration {provider!r} is not an existing connection available to this user.",
                })
    scope_candidates = [("integration_scope", state)]
    if isinstance(state.get("operating_model"), dict):
        scope_candidates.append(("operating_model.integration_scope", state["operating_model"]))
    for path, container in scope_candidates:
        if "integration_scope" not in container:
            continue
        try:
            _validated_integration_scope(container["integration_scope"])
        except ValueError as exc:
            errors.append({
                "path": path,
                "code": "invalid_integration_scope",
                "message": str(exc),
            })

    budget = _as_dict(state.get("budget_policy"))
    for key in ("monthly_budget_credits", "monthly_budget_usd"):
        value = budget.get(key)
        if value is not None:
            try:
                if float(value) < 0:
                    errors.append({"path": f"budget_policy.{key}", "message": f"{key} cannot be negative"})
            except (TypeError, ValueError, OverflowError):
                errors.append({"path": f"budget_policy.{key}", "message": f"{key} must be numeric"})

    heartbeat = _as_dict(state.get("heartbeat_policy"))
    cadence = str(heartbeat.get("cadence") or "").strip()
    if heartbeat.get("enabled") and not cadence:
        errors.append({"path": "heartbeat_policy.cadence", "message": "enabled heartbeat requires cadence"})
    if cadence and cadence not in {"hourly", "daily", "weekly", "biweekly"} and " " not in cadence:
        errors.append({
            "path": "heartbeat_policy.cadence",
            "message": "cadence must be hourly/daily/weekly/biweekly or a cron expression",
        })

    rules = [row for row in _as_list(state.get("rules")) if isinstance(row, dict)]
    for idx, rule in enumerate(rules):
        if not rule.get("description") and not rule.get("rule_key"):
            warnings.append({"path": f"rules[{idx}]", "message": "rule has no description"})
        if (
            rule.get("runtime_enforced")
            and not rule.get("action_patterns")
            and not rule.get("capability_patterns")
            and not rule.get("approval_selectors")
        ):
            warnings.append({
                "path": f"rules[{idx}].capability_patterns",
                "message": "runtime rule has no action or capability patterns",
            })

    edited_governance_keys = _draft_edited_governance_keys(draft.patches)
    edited_rule_policy_keys = edited_governance_keys & _REBUILT_GOVERNANCE_POLICY_KEYS
    if edited_rule_policy_keys:
        implied = rule_implied_governance_conflicts(
            rules, _as_dict(state.get("governance_policy")),
        )
        errors.extend(
            {**warning, "code": "rule_implied_governance"}
            for warning in implied
            if str(warning.get("path") or "").rsplit(".", 1)[-1]
            in edited_rule_policy_keys
        )
    warnings.extend(
        _enforcement_conflict_warnings(rules, _as_dict(effective_state.get("governance_policy")))
    )

    for idx, task in enumerate(_as_list(state.get("immediate_tasks"))):
        if not isinstance(task, dict):
            errors.append({"path": f"immediate_tasks[{idx}]", "message": "task draft must be an object"})
            continue
        if not str(task.get("title") or "").strip():
            errors.append({"path": f"immediate_tasks[{idx}].title", "message": "task draft requires title"})

    if not services:
        missing_setup.append("services")
    if not rules:
        warnings.append({"path": "rules", "message": "workspace has no persistent runtime rules"})

    if "channel_config" in changed_domains:
        errors.extend(await validate_operation_channel_references(
            db, state, entity_id=draft.entity_id, workspace_id=draft.workspace_id,
            user_id=user_id or draft.created_by_user_id,
        ))

    return errors, warnings, missing_setup


def _validate_capability_bindings(
    bindings: Any,
    service_keys: set[str],
    errors: list[dict[str, Any]],
    *,
    prefix: str,
) -> None:
    for idx, raw_binding in enumerate(_as_list(bindings)):
        if not isinstance(raw_binding, dict):
            errors.append({"path": f"{prefix}[{idx}]", "message": "binding must be an object"})
            continue
        if raw_binding.get("enabled") is False:
            continue
        binding_errors = validate_runtime_capability_binding(
            raw_binding,
            path=f"{prefix}[{idx}]",
            service_keys=service_keys,
        )
        errors.extend(binding_errors)


def _validate_new_task_owned_bindings(
    draft: WorkspaceOperationDraft,
    current_state: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    """Reject newly requested Task-owned grants that Runtime cannot execute.

    Compare effective grant fields across every patch route, while allowing
    unrelated edits to coexist with historical Task-owned bindings.
    """
    def grant_identity(raw_binding: dict[str, Any]) -> str | None:
        if raw_binding.get("owner_scope") != "task" or raw_binding.get("enabled") is False:
            return None
        grant = {
            key: value for key, value in raw_binding.items()
            if key not in {"binding_key", "description", "purpose"}
        }
        grant["enabled"] = True
        return json.dumps(grant, sort_keys=True, ensure_ascii=False, default=str)

    for section in ("capability_bindings", "skill_bindings"):
        existing = {
            identity
            for row in _as_list(current_state.get(section))
            if isinstance(row, dict) and (identity := grant_identity(row)) is not None
        }
        for idx, row in enumerate(_as_list(_as_dict(draft.current_state).get(section))):
            if not isinstance(row, dict):
                continue
            identity = grant_identity(row)
            if identity is not None and identity not in existing:
                errors.append({
                    "path": f"{section}[{idx}].owner_scope",
                    "code": "task_scoped_binding_unsupported",
                    "message": (
                        "Task-scoped execution bindings are not supported. "
                        "Update this Agent's configuration or its deployment "
                        "binding in the current Workspace through user review. "
                        "Connect any required account separately."
                    ),
                })


def _budget_impact(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    before = _as_dict(base.get("budget_policy"))
    after = _as_dict(target.get("budget_policy"))
    return {
        "before": {
            "monthly_budget_credits": before.get("monthly_budget_credits"),
            "monthly_budget_usd": before.get("monthly_budget_usd"),
            "auto_pause_on_budget": before.get("auto_pause_on_budget"),
        },
        "after": {
            "monthly_budget_credits": after.get("monthly_budget_credits"),
            "monthly_budget_usd": after.get("monthly_budget_usd"),
            "auto_pause_on_budget": after.get("auto_pause_on_budget"),
        },
        "changed": before != after,
    }


async def preview_operation_diff(
    db: AsyncSession,
    draft: WorkspaceOperationDraft,
    *,
    current_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = (
        current_state
        if current_state is not None
        else await get_current_operation_state(db, draft.workspace_id, draft.entity_id)
    )
    state = _as_dict(draft.current_state)
    raw_diff = _state_diff(current or {}, state)
    # Invalid drafts remain editable. Show their requested values without
    # passing them through the typed policy resolver used by a valid apply.
    # Recheck primitives here as legacy stored validation may be stale.
    invalid_values = validate_operation_domain_values(
        state, changed_keys=set(raw_diff["changed_keys"]),
    )
    # A no-op is a validly shaped request, not an invalid value. It still
    # needs live effective-policy evidence instead of a stale snapshot diff.
    invalid_request = any(
        not isinstance(error, dict) or error.get("code") != "no_changes"
        for error in _as_list(_as_dict(draft.validation).get("errors"))
    )
    diff = (
        raw_diff
        if invalid_request or invalid_values
        else _operation_state_diff(current or {}, draft)
    )
    draft.diff = _json_safe(diff)
    return draft.diff


async def _sync_goals_from_operation_state(
    db: AsyncSession,
    workspace: Workspace,
    raw_goals: Any,
    *,
    deactivate_missing: bool,
) -> dict[str, int]:
    """Materialize operation goals into the runtime Goal table.

    ``operating_model.goals`` is the editable contract, but Strategist and
    measurement jobs read Goal rows. Keep those two layers aligned when a user
    applies an operation draft.
    """
    from packages.core.goals.scheduling import (
        install_measurement_schedule,
        remove_measurement_schedule,
        should_install_measurement_schedule,
    )
    from packages.core.goals.locking import lock_workspace_for_goal_mutation
    from packages.core.goals.service import (
        create_goal,
        sync_workspace_goal_mode,
        update_goal,
    )
    from packages.core.models.goal import Goal
    from packages.core.services.workspace_setup_service import _coerce_goal_number

    await lock_workspace_for_goal_mutation(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
    )

    existing_goals = list((await db.execute(
        select(Goal).where(
            Goal.entity_id == workspace.entity_id,
            Goal.workspace_id == workspace.id,
        )
    )).scalars().all())
    by_goal_key = {g.goal_key: g for g in existing_goals if g.goal_key}

    created = 0
    updated = 0
    paused = 0
    scheduled = 0
    desired_goal_keys: set[str] = set()

    def _merged_measurement_source(existing_source: Any, incoming_source: Any) -> dict | None:
        if not isinstance(incoming_source, dict) or not incoming_source:
            return None
        if not isinstance(existing_source, dict) or not existing_source:
            return incoming_source

        same_provider = existing_source.get("provider") == incoming_source.get("provider")
        same_action = existing_source.get("action") == incoming_source.get("action")
        if same_provider and same_action:
            # Drafts often carry the concise public contract
            # ({provider, action}) while runtime rows contain private routing
            # hints such as params/_sandbox. Preserve those hints unless the
            # draft explicitly replaces them.
            return {**existing_source, **incoming_source}
        return incoming_source

    for raw in _as_list(raw_goals):
        if not isinstance(raw, dict):
            continue
        goal_key = str(
            raw.get("goal_key")
            or raw.get("key")
            or raw.get("metric_key")  # legacy operation contract
            or ""
        ).strip()
        if not goal_key:
            continue
        metric_key = str(
            raw.get("metric_key")
            or raw.get("goal_key")
            or raw.get("key")
            or raw.get("title")
            or raw.get("goal")
            or "untitled_goal"
        ).strip()
        if not metric_key:
            continue
        desired_goal_keys.add(goal_key)

        existing = by_goal_key.get(goal_key)
        title = str(
            raw.get("title")
            or raw.get("goal")
            or (existing.title if existing else None)
            or raw.get("goal_key")
            or raw.get("key")
            or "Untitled Goal"
        ).strip() or "Untitled Goal"
        target_raw = raw.get("target_value") if raw.get("target_value") is not None else raw.get("target")
        target_value = (
            _coerce_goal_number(target_raw)
            if target_raw is not None or existing is None
            else existing.target_value
        )
        baseline_value = (
            _coerce_goal_number(raw["baseline_value"])
            if raw.get("baseline_value") is not None
            else (existing.baseline_value if existing else None)
        )
        current_value = (
            _coerce_goal_number(raw["current_value"])
            if raw.get("current_value") is not None
            else (existing.current_value if existing else None)
        )
        if "measurement_source" in raw:
            measurement_source = _merged_measurement_source(
                existing.measurement_source if existing else None,
                raw.get("measurement_source"),
            )
        else:
            measurement_source = existing.measurement_source if existing else None
        if "measurement_cadence" in raw or "cadence" in raw:
            measurement_cadence = str(
                raw.get("measurement_cadence")
                or raw.get("cadence")
                or ""
            ).strip() or None
        else:
            measurement_cadence = existing.measurement_cadence if existing else None
        status_value = str(
            raw.get("status") or (existing.status if existing else GoalStatus.ACTIVE.value)
        ).strip().lower()
        try:
            status = GoalStatus(status_value).value
        except ValueError:
            status = GoalStatus.ACTIVE.value
        if status == GoalStatus.ACHIEVED.value and existing is None:
            # Achievement is runtime evidence, never portable configuration.
            status = GoalStatus.ACTIVE.value
        deadline = (
            _parse_goal_deadline(raw.get("deadline"))
            if "deadline" in raw
            else (existing.deadline if existing else None)
        )
        description = raw.get("description") if "description" in raw else (existing.description if existing else None)
        priority = _coerce_goal_priority(raw.get("priority")) if "priority" in raw else (
            existing.priority if existing else _coerce_goal_priority(None)
        )

        if existing:
            goal = await update_goal(
                db,
                existing.id,
                workspace.entity_id,
                sync_contract=False,
                title=title,
                description=description,
                metric_key=metric_key,
                target_value=target_value,
                baseline_value=baseline_value,
                current_value=current_value,
                deadline=deadline,
                measurement_source=measurement_source,
                measurement_cadence=measurement_cadence,
                priority=priority,
                **(
                    {"status": status}
                    if status != GoalStatus.ACHIEVED.value
                    else {}
                ),
            )
            updated += 1 if goal else 0
        else:
            goal = await create_goal(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                title=title,
                goal_key=goal_key,
                description=description,
                metric_key=metric_key,
                target_value=target_value,
                baseline_value=baseline_value,
                deadline=deadline,
                measurement_source=measurement_source,
                measurement_cadence=measurement_cadence,
                priority=priority,
                install_schedule=False,
                sync_contract=False,
            )
            if status not in {
                GoalStatus.ACTIVE.value,
                GoalStatus.ACHIEVED.value,
            }:
                goal.status = status
            if current_value is not None:
                goal.current_value = current_value
            created += 1

        if not goal:
            continue
        await db.flush()
        if (
            workspace_autonomy_state(workspace) is WorkspaceAutonomyState.RUNNING
            and goal.status == GoalStatus.ACTIVE.value
            and should_install_measurement_schedule(goal)
        ):
            await install_measurement_schedule(db, goal)
            scheduled += 1
        else:
            await remove_measurement_schedule(db, goal)

    if deactivate_missing:
        for goal in existing_goals:
            if (
                goal.goal_key in desired_goal_keys
                or goal.status != GoalStatus.ACTIVE.value
            ):
                continue
            goal.status = GoalStatus.PAUSED.value
            await remove_measurement_schedule(db, goal)
            paused += 1

    await db.flush()
    await sync_workspace_goal_mode(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
        bump_operation_revision=False,
    )
    return {
        "created": created,
        "updated": updated,
        "paused": paused,
        "scheduled": scheduled,
    }


async def _sync_agent_mappings_from_operation_state(
    db: AsyncSession,
    workspace: Workspace,
    raw_mappings: Any,
    *,
    deactivate_missing: bool,
    requester_user_id: str | None = None,
    preserve_existing_deployments: bool = False,
    unchanged_mappings: list[Any] | None = None,
) -> dict[str, int]:
    """Materialize service->agent mappings into AgentSubscription rows.

    ``unchanged_mappings`` (the live snapshot rows) limits an operation apply
    to the rows its draft changed. Snapshot rows *are* the live subscriptions,
    and some may carry validation errors the draft inherited (an undeclared
    service, a duplicate service_key); re-materializing them could rewire a
    deployment nobody asked to change.
    """
    from packages.core.services.workspace_service import map_agent_to_service
    from packages.core.services.workspace_setup_service import _provision_custom_agent_for_mapping

    existing_subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    existing_by_service = {
        str(sub.service_key or "").strip(): sub
        for sub in existing_subs
        if str(sub.service_key or "").strip()
    }

    created = 0
    updated = 0
    unchanged = 0
    deactivated = 0
    custom_agents = 0
    reused_existing_agents = 0
    skipped = 0
    desired_service_keys: set[str] = set()
    custom_agent_cache: dict[str, tuple[str, str]] = {}
    untouched = [row for row in _as_list(unchanged_mappings) if isinstance(row, dict)]
    active_per_service: dict[str, int] = {}
    for sub in existing_subs:
        key = str(sub.service_key or "").strip()
        active_per_service[key] = active_per_service.get(key, 0) + 1

    existing_auto_agents = list((await db.execute(
        select(Agent).where(
            Agent.entity_id == workspace.entity_id,
            Agent.source == "auto_workspace_setup",
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
    )).scalars().all())
    existing_auto_agent_by_name: dict[str, Agent] = {}
    for agent in existing_auto_agents:
        name_key = str(agent.name or "").strip().lower()
        if name_key and name_key not in existing_auto_agent_by_name:
            existing_auto_agent_by_name[name_key] = agent

    for raw in _as_list(raw_mappings):
        if not isinstance(raw, dict):
            continue
        mapping = dict(raw)
        service_key = str(mapping.get("service_key") or "").strip()
        if mapping in untouched:
            untouched.remove(mapping)
            if service_key:
                desired_service_keys.add(service_key)
            unchanged += 1
            continue
        if not service_key:
            skipped += 1
            continue
        desired_service_keys.add(service_key)
        if active_per_service.get(service_key, 0) > 1:
            raise OperationConflictError(
                f"Service {service_key!r} has more than one active Agent; "
                "resolve that duplicate before changing its mapping"
            )

        agent_id = str(
            mapping.get("agent_id")
            or mapping.get("recommended_agent_id")
            or ""
        ).strip()
        if not agent_id and preserve_existing_deployments and service_key in existing_by_service:
            agent_id = existing_by_service[service_key].agent_id
        if not agent_id and mapping.get("strategy") == "create_custom":
            create_draft = _as_dict(mapping.get("create_agent_draft"))
            agent_name = str(
                create_draft.get("agent_name")
                or mapping.get("agent_name")
                or mapping.get("recommended_agent_name")
                or service_key.replace("_", " ").title()
            ).strip()
            cache_key = json.dumps(
                {"service_key": service_key, "agent_name": agent_name.lower(), "create_agent_draft": create_draft},
                sort_keys=True, ensure_ascii=False,
            )
            if cache_key in custom_agent_cache:
                agent_id, _agent_name = custom_agent_cache[cache_key]
            elif agent_name.lower() in existing_auto_agent_by_name and not any(
                value for key, value in create_draft.items() if key != "agent_name"
            ) and not any(
                sub.agent_id == existing_auto_agent_by_name[agent_name.lower()].id
                and str(sub.service_key or "").strip() != service_key
                for sub in existing_by_service.values()
            ):
                # An earlier apply (or finalize_setup) already auto-provisioned
                # an agent with this exact name for this entity. Reuse it
                # instead of creating duplicates that pile up across applies.
                # Only a legacy name-only request can reuse by display name.
                # A requested prompt/tool/Skill design must actually provision;
                # another Agent with that name is not proof of those bindings.
                existing_agent = existing_auto_agent_by_name[agent_name.lower()]
                agent_id = existing_agent.id
                custom_agent_cache[cache_key] = (agent_id, existing_agent.name)
                reused_existing_agents += 1
            else:
                agent_id, _agent_name = await _provision_custom_agent_for_mapping(
                    db,
                    entity_id=workspace.entity_id,
                    workspace_id=workspace.id,
                    workspace_name=workspace.name,
                    operating_context=workspace.operating_context or "",
                    primary_work=workspace.primary_work or "",
                    mapping=mapping,
                    requester_user_id=requester_user_id,
                )
                custom_agent_cache[cache_key] = (agent_id, _agent_name)
                custom_agents += 1
        if not agent_id:
            raise ValueError(f"Service {service_key!r} has no materializable Agent")

        existing = existing_by_service.get(service_key)
        before_agent_id = existing.agent_id if existing else None
        before_custom_prompt = existing.custom_prompt if existing else None
        before_config = _copy_json(existing.config) if existing else {}
        requested_prompt = mapping.get("custom_prompt")
        if requested_prompt is not None:
            custom_prompt = str(requested_prompt)
        elif existing and existing.custom_prompt and not _is_auto_framing_prompt(existing.custom_prompt):
            # Operator wrote this prompt by hand — leave it alone.
            custom_prompt = existing.custom_prompt
        else:
            # No existing prompt, or the existing one is our own auto framing
            # (which may now reference a stale workspace name / context /
            # primary work). Regenerate from current workspace metadata.
            custom_prompt = _workspace_subscription_prompt(workspace, service_key, mapping)
        sub = await map_agent_to_service(
            db,
            workspace.id,
            workspace.entity_id,
            service_key,
            agent_id,
            custom_prompt=custom_prompt,
        )
        if "config" in mapping:
            if not isinstance(mapping["config"], dict):
                raise ValueError("subscription config must be an object")
            sub.config = _json_safe(mapping["config"])
        if existing is None:
            created += 1
        elif before_agent_id != agent_id or before_custom_prompt != custom_prompt or before_config != sub.config:
            updated += 1
        else:
            unchanged += 1
        existing_by_service[service_key] = sub

    if deactivate_missing:
        for sub in existing_subs:
            service_key = str(sub.service_key or "").strip()
            if service_key and service_key not in desired_service_keys:
                sub.status = "inactive"
                deactivated += 1

    await db.flush()
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "deactivated": deactivated,
        "custom_agents": custom_agents,
        "reused_existing_agents": reused_existing_agents,
        "skipped": skipped,
    }


def _operation_channel_blocks(channel_config: Any) -> list[tuple[str, dict[str, Any]]]:
    config = _as_dict(channel_config)
    out: list[tuple[str, dict[str, Any]]] = []
    for raw in _as_list(config.get("channels")):
        if isinstance(raw, dict):
            role = str(raw.get("role") or "channel").strip() or "channel"
            out.append((role, dict(raw)))
    legacy = (
        ("primary_external", config.get("primary_external_channel")),
        ("internal", config.get("internal_channel")),
    )
    for role, raw in legacy:
        if isinstance(raw, dict) and (raw.get("channel_type") or raw.get("type")):
            out.append((role, dict(raw)))
    for raw in _as_list(config.get("secondary_external_channels")):
        if isinstance(raw, dict):
            out.append(("secondary_external", dict(raw)))
    return out


def _channel_config_match(
    configs: list[Any],
    *,
    channel_type: str,
    role: str,
    linked_service_key: str,
) -> Any | None:
    fallback_matches: list[Any] = []
    for config in configs:
        cfg = _as_dict(getattr(config, "config", None))
        if (
            getattr(config, "channel_type", None) == channel_type
            and str(cfg.get("role") or "") == role
            and str(cfg.get("linked_service_key") or "") == linked_service_key
        ):
            return config
        if (
            getattr(config, "channel_type", None) == channel_type
            and not str(cfg.get("role") or "").strip()
            and not str(cfg.get("linked_service_key") or "").strip()
        ):
            fallback_matches.append(config)
    if len(fallback_matches) == 1:
        return fallback_matches[0]
    return None


async def _sync_channels_from_operation_state(
    db: AsyncSession,
    workspace: Workspace,
    channel_config_state: Any,
    *,
    user_id: str | None = None,
) -> dict[str, int]:
    """Materialize operation channels into ChannelConfig + Channel rows."""
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    builtins = {"webchat", "internal_chat", "in_app"}
    subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())
    sub_by_service = {
        str(sub.service_key or "").strip(): sub
        for sub in subs
        if str(sub.service_key or "").strip()
    }
    first_sub = subs[0] if subs else None
    existing_configs = list((await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == workspace.entity_id,
            ChannelConfig.status == "active",
            ChannelConfig.workspace_id == workspace.id,
        )
    )).scalars().all())
    existing_channels = list((await db.execute(
        select(Channel).where(
            Channel.entity_id == workspace.entity_id,
            Channel.workspace_id == workspace.id,
        )
    )).scalars().all())

    created_configs = 0
    updated_configs = 0
    created_bindings = 0
    updated_bindings = 0
    skipped = 0
    created_config_ids: set[str] = set()

    def _binding_for_config(config_id: str) -> Any | None:
        for row in existing_channels:
            if str(_as_dict(row.config).get("channel_config_id") or "") == config_id:
                return row
        return None

    for role, block in _operation_channel_blocks(channel_config_state):
        channel_type = str(block.get("channel_type") or block.get("type") or "").strip()
        if not channel_type:
            skipped += 1
            continue
        provider = str(block.get("provider") or channel_type).strip()
        linked_service_key = str(block.get("linked_service_key") or block.get("service_key") or "").strip()
        sub = sub_by_service.get(linked_service_key) if linked_service_key else None
        if not sub:
            sub = first_sub
        resolved_agent_id = sub.agent_id if sub else block.get("agent_id")

        config_row = None
        channel_config_id = str(block.get("channel_config_id") or "").strip()
        if channel_config_id:
            config_row = (await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.id == channel_config_id,
                    ChannelConfig.entity_id == workspace.entity_id,
                    ChannelConfig.status == "active",
                )
            )).scalar_one_or_none()
            if not config_row:
                skipped += 1
                continue
            # ChannelConfig rows are workspace-scoped. Refuse to bind an
            # inbound channel of THIS workspace to a config row that belongs
            # to a different workspace — that would silently route messages
            # across workspace boundaries.
            existing_workspace_id = getattr(config_row, "workspace_id", None)
            if existing_workspace_id and existing_workspace_id != workspace.id:
                skipped += 1
                continue
        else:
            if provider not in builtins and channel_type not in builtins:
                skipped += 1
                continue
            config_row = _channel_config_match(
                existing_configs,
                channel_type=channel_type,
                role=role,
                linked_service_key=linked_service_key,
            )
            if not config_row:
                config_row = ChannelConfig(
                    id=generate_ulid(),
                    entity_id=workspace.entity_id,
                    owner_user_id=user_id if channel_type == "webchat" else None,
                    workspace_id=workspace.id,
                    channel_type=channel_type,
                    provider=provider,
                    credentials={},
                    status="active",
                )
                db.add(config_row)
                existing_configs.append(config_row)
                created_configs += 1
                created_config_ids.add(config_row.id)

        provider = str(block.get("provider") or getattr(config_row, "provider", None) or channel_type).strip()
        previous_channel_type = str(
            getattr(config_row, "channel_type", "") or ""
        )
        config_payload = _as_dict(getattr(config_row, "config", None))
        config_payload.update(_as_dict(block.get("config")))
        public_token = str(block.get("public_token") or config_payload.get("public_token") or "").strip()
        if channel_type == "webchat" and not public_token:
            public_token = generate_ulid()
        config_payload.update({
            "role": role,
            "purpose": block.get("purpose") or config_payload.get("purpose") or "",
            "linked_service_key": linked_service_key,
            "login_required": bool(block.get("login_required", config_payload.get("login_required", False))),
            "notes": block.get("notes") or config_payload.get("notes") or "",
        })
        if public_token:
            config_payload["public_token"] = public_token

        before_config = _json_safe(getattr(config_row, "config", {}) or {})
        before_name = getattr(config_row, "name", None)
        config_row.workspace_id = workspace.id
        config_row.channel_type = channel_type
        config_row.provider = provider
        config_row.name = block.get("name") or config_row.name or f"{role}: {channel_type}"
        config_row.config = _json_safe(config_payload)
        if config_row.id not in created_config_ids and (
            before_config != config_row.config or before_name != config_row.name
        ):
            updated_configs += 1

        binding_config = {
            "channel_config_id": config_row.id,
            "role": role,
            "purpose": config_payload.get("purpose") or "",
            "linked_service_key": linked_service_key,
        }
        if public_token:
            binding_config["public_token"] = public_token

        binding = _binding_for_config(config_row.id)
        if (
            resolved_agent_id
            and (binding is None or resolved_agent_id != binding.agent_id)
            and not is_master_agent(resolved_agent_id)
        ):
            await lock_reusable_resource_references(
                db,
                entity_id=workspace.entity_id,
                agent_ids=(resolved_agent_id,),
            )
        if binding:
            target_subscription_id = (
                sub.id if sub else block.get("agent_subscription_id")
            )
            before_binding = (
                binding.agent_id,
                binding.agent_subscription_id,
                getattr(binding, "user_id", None),
                _json_safe(binding.config or {}),
                binding.name,
                binding.status,
            )
            if (
                previous_channel_type == "twilio_voice"
                and (
                    binding.agent_id != resolved_agent_id
                    or binding.agent_subscription_id != target_subscription_id
                    or binding.workspace_id != workspace.id
                    or binding.type != channel_type
                    or binding.status != "active"
                )
            ):
                from packages.core.services.voice.call_sessions import (
                    cancel_unconnected_call_sessions_for_binding,
                )

                await cancel_unconnected_call_sessions_for_binding(
                    db,
                    channel_config_id=config_row.id,
                    channel_binding_id=binding.id,
                    reason=(
                        "Twilio Voice Agent binding changed before the call connected."
                    ),
                )
            binding.agent_id = resolved_agent_id
            binding.agent_subscription_id = target_subscription_id
            binding.user_id = getattr(config_row, "owner_user_id", None)
            binding.name = block.get("name") or binding.name or config_row.name or channel_type
            binding.type = channel_type
            binding.config = _json_safe(binding_config)
            binding.status = "active"
            after_binding = (
                binding.agent_id,
                binding.agent_subscription_id,
                getattr(binding, "user_id", None),
                _json_safe(binding.config or {}),
                binding.name,
                binding.status,
            )
            if before_binding != after_binding:
                updated_bindings += 1
        else:
            binding = Channel(
                id=generate_ulid(),
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                user_id=getattr(config_row, "owner_user_id", None),
                type=channel_type,
                name=block.get("name") or config_row.name or channel_type,
                agent_id=resolved_agent_id,
                agent_subscription_id=sub.id if sub else block.get("agent_subscription_id"),
                config=_json_safe(binding_config),
                status="active",
            )
            db.add(binding)
            existing_channels.append(binding)
            created_bindings += 1

    await db.flush()
    return {
        "created_configs": created_configs,
        "updated_configs": updated_configs,
        "created_bindings": created_bindings,
        "updated_bindings": updated_bindings,
        "skipped": skipped,
    }




async def _resolve_workflow_attachment(db, attachment, *, entity_id, workspace_id, user_id):
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.models.permission import Capability, ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, user_can_access_resource
    from packages.core.services import workflow_service

    user = await db.get(User, user_id) if user_id else None
    workflow = await db.get(WorkflowDefinition, attachment["workflow_id"])
    if user is None or user.entity_id != entity_id or workflow is None or workflow.entity_id != entity_id:
        raise ValueError("Workflow is unavailable to this Workspace actor")
    if not await user_can_access_resource(
        db, descriptor=ResourceDescriptor.from_row(workflow, ResourceType.WORKFLOW),
        entity_id=entity_id, user_id=user.id, role=user.role, capability=Capability.VIEW,
    ):
        raise ValueError("Workflow is unavailable to this Workspace actor")
    if not workflow.is_active or workflow.status != "active" or workflow.trigger_type == "internal":
        raise ValueError("Workflow must be active before attaching it")
    if workflow.workspace_id:
        owner = await db.get(Workspace, workflow.workspace_id)
        if owner is None or owner.entity_id != entity_id or owner.deleted_at is not None:
            raise ValueError("Workflow's source Workspace is unavailable")
    if not workflow_service.validate_workflow_definition(workflow)["valid"]:
        raise ValueError("Workflow must be valid before attaching it")
    bindings = await workflow_service.list_bindings(db, entity_id, workspace_id=workspace_id, workflow_id=workflow.id)
    existing = next((row for row in bindings if row.trigger_type == "manual"), None)
    if existing is not None and (not existing.enabled or existing.status != "active"):
        raise ValueError("The existing Workspace Workflow binding is inactive; enable it before connecting it")
    return workflow, existing


async def _materialize_workflow_attachments(db, workspace, attachments, *, user_id):
    from packages.core.services.workflow_service import create_workflow_binding
    from packages.core.services.workspace_app_view import normalize_app_view_config

    bindings = []
    model = _copy_json(_as_dict(workspace.operating_model))
    for attachment in attachments or []:
        workflow, binding = await _resolve_workflow_attachment(
            db, attachment, entity_id=workspace.entity_id, workspace_id=workspace.id, user_id=user_id,
        )
        if binding is None:
            binding = await create_workflow_binding(
                db, workspace.entity_id, workflow.id, workspace_id=workspace.id,
                name=workflow.name, trigger_type="manual",
                config={"workspace_attached": True, "chat_entrypoint": {"enabled": True}},
            )
        action_key = attachment.get("action_key") or (f"service:{attachment['service_key']}" if attachment.get("service_key") else None)
        if action_key or attachment.get("primary"):
            config = normalize_app_view_config(model.get("app_view"))
            if action_key:
                config["targets"][action_key] = f"workflow:{binding.id}"
            if attachment.get("primary"):
                config["hero"] = action_key or f"workflow:{binding.id}"
                if not action_key:
                    config["targets"].pop(config["hero"], None)
                config["hidden"] = [key for key in config["hidden"] if key != config["hero"]]
            model["app_view"] = config
        bindings.append({"binding_id": binding.id, "workflow_id": workflow.id,
                         "service_key": attachment.get("service_key"),
                         **({"action_key": action_key} if attachment.get("action_key") else {})})
    if attachments:
        workspace.operating_model = _json_safe(model)
    return bindings


async def apply_operation_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    workspace_id: str,
    *,
    user_id: str | None = None,
    user_confirmation: bool = False,
    expected_review_fingerprint: str | None = None,
    origin_task_id: str | None = None,
) -> dict[str, Any] | None:
    """``origin_task_id``: the Task run applying the draft (host provenance).

    Known gap: a draft a person applies from the UI (REST or the chat card)
    has no origin, even when a Task run authored it. Recording that would
    need a draft-origin column written when the draft is created.
    """
    if not user_confirmation:
        raise OperationValidationError({
            "valid": False,
            "errors": [{"path": "user_confirmation", "message": "user confirmation is required before applying"}],
            "warnings": [],
        })

    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace = await _load_workspace(db, workspace_id, entity_id, for_update=True)
    if workspace is None:
        return None
    draft = await get_operation_draft(db, draft_id, entity_id, workspace_id, for_update=True)
    if draft is None:
        return None
    if draft.status != WorkspaceOperationDraftStatus.OPEN:
        raise OperationConflictError(f"cannot apply {draft.status} operation draft")
    if int(draft.base_revision or 0) != int(workspace.operation_revision or 0):
        raise OperationConflictError(
            "workspace operation draft is stale; create a new draft before applying"
        )

    # Snapshot the live workspace state once under the for_update lock and
    # reuse it for validation + diff so neither path re-loads workspace,
    # budget, governance, and agent mappings independently.
    current_state = await _snapshot_workspace_state(db, workspace, lock_governance=True)
    validation = await validate_operation_draft(db, draft, current_state=current_state, user_id=user_id)
    if validation.get("errors"):
        raise OperationValidationError(validation)
    applied_diff = await preview_operation_diff(db, draft, current_state=current_state)
    # Governance has its own revision. Compare the reviewed effects only after
    # reloading that live policy under its lock and recomputing the actual diff.
    if (
        expected_review_fingerprint is not None
        and expected_review_fingerprint != operation_draft_review_fingerprint(draft)
    ):
        raise OperationConflictError("workspace operation draft changed; review the updated draft before applying")
    changed_keys = set((applied_diff or {}).get("changed_keys") or [])
    from packages.core.services.task_capability_recovery import recovery_task_for_operation

    recovery_task = await recovery_task_for_operation(db, draft)
    if recovery_task is not None and not expected_review_fingerprint:
        raise OperationConflictError("Review the task configuration request before applying and continuing it.")

    state = _effective_operation_state(current_state, draft)
    operating_model = _as_dict(state.get("operating_model"))

    from packages.core.budget import set_workspace_budget
    from packages.core.governance import policy_from_dict, update_policy
    from packages.core.services.workspace_runtime import sync_workspace_runtime_schedules
    from packages.core.services.workspace_service import get_workspace_agent_mappings, record_activity
    from packages.core.services.workspace_setup_service import (
        _enrich_operating_rules,
    )
    from packages.core.workspace_chat.context import invalidate

    operating_rules = _enrich_operating_rules(operating_model.get("rules") or [])
    operating_model["rules"] = operating_rules
    workspace.operating_model = _json_safe(operating_model)

    workflow_attachments = await _materialize_workflow_attachments(
        db, workspace, state.get("workflow_attachments"), user_id=user_id or draft.created_by_user_id,
    )
    operating_model = _as_dict(workspace.operating_model)

    if "budget_policy" in changed_keys:
        budget = _as_dict(state.get("budget_policy"))
        await set_workspace_budget(
            db,
            workspace.id,
            monthly_budget_credits=budget.get("monthly_budget_credits"),
            monthly_budget_usd=budget.get("monthly_budget_usd"),
            auto_pause_on_budget=budget.get("auto_pause_on_budget"),
            reset_alert_state=bool(budget.get("reset_alert_state", True)),
        )

    heartbeat = _as_dict(state.get("heartbeat_policy"))
    if "enabled" in heartbeat:
        workspace.heartbeat_enabled = bool(heartbeat.get("enabled"))
    cadence = str(heartbeat.get("cadence") or "").strip()
    if cadence:
        workspace.heartbeat_cadence = cadence
    elif workspace.heartbeat_enabled and not workspace.heartbeat_cadence:
        workspace.heartbeat_cadence = "daily"
    await sync_workspace_runtime_schedules(db, workspace, user_id=user_id)

    if {"rules", "governance_policy"} & changed_keys:
        policy_row = await update_policy(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            policy=policy_from_dict(state.get("governance_policy")),
            changed_by=user_id,
            change_summary="Workspace operation draft applied",
            preserve_automated_run_consent=True,
        )
        operating_model["governance"] = dict(policy_row.policy or {})
        workspace.operating_model = _json_safe(operating_model)

    replace_goal_set = any(
        _as_dict(patch).get("op") in {"goals.replace", "goal.replace", "operating_model.replace", "workspace_model.replace"}
        for patch in _as_list(draft.patches)
    )
    replace_agent_mapping_set = any(
        _as_dict(patch).get("op") in {
            "agent_mappings.replace",
            "agent_mapping.replace",
            "agent_mapping.remove",
            "agent_binding.remove",
            "service_role.remove",
            "service.remove",
            "operating_model.replace",
            "workspace_model.replace",
        }
        for patch in _as_list(draft.patches)
    )
    goal_result = {"created": 0, "updated": 0, "paused": 0, "scheduled": 0}
    if "goals" in changed_keys:
        goal_result = await _sync_goals_from_operation_state(
            db,
            workspace,
            _rows_without_inherited_errors(state.get("goals"), validation, "goals"),
            deactivate_missing=replace_goal_set,
        )
    agent_mapping_result = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "deactivated": 0,
        "custom_agents": 0,
        "reused_existing_agents": 0,
        "skipped": 0,
    }
    if "agent_mappings" in changed_keys:
        agent_mapping_result = await _sync_agent_mappings_from_operation_state(
            db,
            workspace,
            state.get("agent_mappings"),
            deactivate_missing=replace_agent_mapping_set,
            requester_user_id=user_id,
            unchanged_mappings=current_state.get("agent_mappings"),
        )
        operating_model["agent_mappings"] = await get_workspace_agent_mappings(db, workspace.id, workspace.entity_id)
        workspace.operating_model = _json_safe(operating_model)
    channel_result = {
        "created_configs": 0,
        "updated_configs": 0,
        "created_bindings": 0,
        "updated_bindings": 0,
        "skipped": 0,
    }
    if "channel_config" in changed_keys:
        channel_result = await _sync_channels_from_operation_state(
            db,
            workspace,
            state.get("channel_config"),
            user_id=user_id,
        )
        if channel_result["skipped"]:
            raise OperationValidationError({
                "valid": False,
                "errors": [{"path": "channel_config", "message": "a requested channel could not be bound; refresh the channel configuration before applying"}],
                "warnings": [],
            })

    task_result = await _materialize_immediate_tasks(
        db,
        workspace,
        state.get("immediate_tasks"),
        draft_id=draft.id,
        user_id=user_id,
        origin_task_id=origin_task_id,
    )

    workspace.operation_revision = int(workspace.operation_revision or 0) + 1
    draft.status = WorkspaceOperationDraftStatus.APPLIED
    draft.applied_at = _utcnow()
    draft.validation = validation
    draft.diff = applied_diff
    invalidate(workspace.id)
    if recovery_task is not None:
        from packages.core.services.task_retry_service import TaskRetryError, prepare_task_retry

        await db.flush()
        try:
            await prepare_task_retry(
                db, task=recovery_task, user_id=user_id or draft.created_by_user_id,
                user_label="Configuration review",
                note="Continue the same task after its reviewed capability configuration was applied.",
            )
        except TaskRetryError as exc:
            raise OperationConflictError(exc.detail) from exc

    await record_activity(
        db,
        workspace.id,
        workspace.entity_id,
        event_type="workspace_operation.applied",
        summary="Workspace operation draft applied",
        details={
            "draft_id": draft.id,
            "base_revision": draft.base_revision,
            "new_revision": workspace.operation_revision,
            "changed_keys": (draft.diff or {}).get("changed_keys") or [],
            "goals": goal_result,
            "agent_mappings": agent_mapping_result,
            "channels": channel_result,
            "workflow_attachments": workflow_attachments,
            "tasks_created": task_result["task_ids"],
            "batch_id": task_result.get("batch_id"),
        },
        user_id=user_id,
    )
    await db.flush()
    return {
        "draft": draft_to_dict(draft),
        "workspace_revision": workspace.operation_revision,
        "pre_existing_issues": _pre_existing_issue_summary(validation),
        "tasks_created": task_result["tasks_created"],
        "goals": goal_result,
        "agent_mappings": agent_mapping_result,
        "channels": channel_result,
        "workflow_attachments": workflow_attachments,
        "batch": task_result.get("batch"),
    }


async def repair_workspace_operation_runtime(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Idempotently repair runtime rows from the persisted operating model.

    This is for older workspaces that already have operation JSON but were
    created before goals, subscriptions, and channels were materialized into
    their runtime tables. Repair is additive and does not deactivate rows that
    are missing from JSON. Configuration or deployment changes invalidate older
    operation drafts; an unchanged repair keeps the same operation_revision.
    """
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    workspace = await _load_workspace(db, workspace_id, entity_id, for_update=True)
    if workspace is None:
        return None

    from packages.core.governance import update_policy
    from packages.core.services.workspace_runtime import sync_workspace_runtime_schedules
    from packages.core.services.workspace_service import get_workspace_agent_mappings, record_activity
    from packages.core.services.workspace_setup_service import (
        _build_governance_policy_from_rules,
        _enrich_operating_rules,
    )
    from packages.core.workspace_chat.context import invalidate

    previous_operating_model = _copy_json(workspace.operating_model or {})
    previous_agent_mappings = await get_workspace_agent_mappings(db, workspace.id, workspace.entity_id)
    operating_model = _as_dict(workspace.operating_model)
    state = _sync_operating_model_from_state({
        "operating_model": operating_model,
        "services": _as_list(operating_model.get("services")),
        "goals": _as_list(operating_model.get("goals")),
        "rules": _as_list(operating_model.get("rules")),
        "knowledge_policy": _as_dict(operating_model.get("knowledge") or operating_model.get("knowledge_policy")),
        "capability_bindings": _as_list(operating_model.get("capability_bindings")),
        "skill_bindings": _as_list(operating_model.get("skill_bindings")),
        "integration_scope": _as_dict(operating_model.get("integration_scope")),
        "channel_config": _as_dict(operating_model.get("channel_config")),
        "processes": _as_list(operating_model.get("processes")),
        "strategist": _as_dict(operating_model.get("strategist")),
        "agent_mappings": _as_list(operating_model.get("agent_mappings")),
    })

    operating_rules = _enrich_operating_rules(state.get("rules") or [])
    operating_model["rules"] = operating_rules
    workspace.operating_model = _json_safe(operating_model)

    await sync_workspace_runtime_schedules(db, workspace, user_id=user_id)

    governance_policy = _build_governance_policy_from_rules(
        operating_rules,
        operating_model.get("governance"),
    )
    governance_result = {"updated": False}
    if governance_policy is not None and operating_rules:
        await update_policy(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            policy=governance_policy,
            changed_by=user_id,
            change_summary="Workspace operation runtime repair",
        )
        governance_result["updated"] = True

    goal_result = await _sync_goals_from_operation_state(
        db,
        workspace,
        state.get("goals"),
        deactivate_missing=False,
    )
    agent_mapping_result = await _sync_agent_mappings_from_operation_state(
        db,
        workspace,
        state.get("agent_mappings"),
        deactivate_missing=False,
        requester_user_id=user_id,
        preserve_existing_deployments=True,
    )
    operating_model["agent_mappings"] = await get_workspace_agent_mappings(db, workspace.id, workspace.entity_id)
    workspace.operating_model = _json_safe(operating_model)
    channel_result = await _sync_channels_from_operation_state(
        db,
        workspace,
        state.get("channel_config"),
        user_id=user_id,
    )

    if (
        previous_operating_model != workspace.operating_model
        or previous_agent_mappings != operating_model["agent_mappings"]
    ):
        workspace.operation_revision = int(workspace.operation_revision or 0) + 1

    invalidate(workspace.id)
    result = {
        "workspace_id": workspace.id,
        "workspace_revision": int(workspace.operation_revision or 0),
        "goals": goal_result,
        "agent_mappings": agent_mapping_result,
        "channels": channel_result,
        "governance": governance_result,
    }
    await record_activity(
        db,
        workspace.id,
        workspace.entity_id,
        event_type="workspace_operation.runtime_repaired",
        summary="Workspace operation runtime repaired",
        details=result,
        user_id=user_id,
    )
    await db.flush()
    return result


async def _materialize_immediate_tasks(
    db: AsyncSession,
    workspace: Workspace,
    raw_tasks: Any,
    *,
    draft_id: str,
    user_id: str | None,
    origin_task_id: str | None = None,
) -> dict[str, Any]:
    from packages.core.services import task_service

    task_payloads = [row for row in _as_list(raw_tasks) if isinstance(row, dict)]
    if not task_payloads:
        return {"tasks_created": [], "task_ids": [], "batch": None, "batch_id": None}

    tasks_created: list[Task] = []
    for payload in task_payloads:
        details = _as_dict(payload.get("details"))
        details.update({
            "workspace_operation_draft_id": draft_id,
            "runtime_context": _as_dict(payload.get("runtime_context")),
        })
        task = await task_service.create_task(
            db,
            workspace.entity_id,
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or ""),
            priority=int(payload.get("priority") or 3),
            task_type=str(payload.get("task_type") or "general"),
            workspace_id=workspace.id,
            assignee_id=payload.get("assignee_id"),
            agent_id=payload.get("agent_id"),
            agent_type=payload.get("agent_type"),
            creator_id=user_id,
            origin_task_id=origin_task_id,
            owner_service_key=(
                str(payload["owner_service_key"])
                if payload.get("owner_service_key")
                else None
            ),
            owner_subscription_id=(
                str(payload["owner_subscription_id"])
                if payload.get("owner_subscription_id")
                else None
            ),
            delegate_service_keys=(
                _unique_list(payload.get("delegate_service_keys"))
                if isinstance(payload.get("delegate_service_keys"), list)
                else None
            ),
            details=details,
            deadline=payload.get("deadline"),
        )
        tasks_created.append(task)

    batch = await create_work_batch(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
        task_ids=[task.id for task in tasks_created],
        created_by_user_id=user_id,
        source_draft_id=draft_id,
        source_kind="workspace_operation",
        summary=f"Workspace operation task wave ({len(tasks_created)} tasks)",
    )
    for task in tasks_created:
        details = _as_dict(task.details)
        details["workspace_work_batch_id"] = batch.id
        task.details = details
    await db.flush()

    return {
        "tasks_created": [_task_to_dict(task) for task in tasks_created],
        "task_ids": [task.id for task in tasks_created],
        "batch": work_batch_to_dict(batch),
        "batch_id": batch.id,
    }


async def create_work_batch(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    task_ids: list[str],
    created_by_user_id: str | None = None,
    source_draft_id: str | None = None,
    source_kind: str | None = None,
    summary: str | None = None,
    details: dict[str, Any] | None = None,
) -> WorkspaceWorkBatch:
    batch = WorkspaceWorkBatch(
        id=generate_ulid(),
        workspace_id=workspace_id,
        entity_id=entity_id,
        created_by_user_id=created_by_user_id,
        source_draft_id=source_draft_id,
        source_kind=source_kind,
        summary=summary,
        status="active",
        task_ids=_json_safe(task_ids),
        details=_json_safe(details or {}),
    )
    db.add(batch)
    await db.flush()
    return batch


async def check_work_batch_completion(
    db: AsyncSession,
    task: Task,
    *,
    trigger_source: str = "task_status_changed",
) -> WorkspaceWorkBatch | None:
    """Complete a task wave once all tasks in it reach a terminal state.

    Strategist should react to a whole wave finishing, not to every individual
    task. This keeps workspace autonomy useful without creating review spam.
    """
    from packages.core.services.task_state_machine import TERMINAL_STATUSES
    from packages.core.services.workspace_service import record_activity

    if task.status in TERMINAL_STATUSES:
        try:
            from packages.core.services.task_dependencies import release_or_block_dependents

            await release_or_block_dependents(
                db,
                task,
                trigger_source=trigger_source,
            )
        except Exception:
            pass

    details = _as_dict(task.details)
    batch_id = str(details.get("workspace_work_batch_id") or "").strip()
    if not batch_id or not task.workspace_id:
        return None
    if task.status not in TERMINAL_STATUSES:
        return None

    batch = (await db.execute(
        select(WorkspaceWorkBatch)
        .where(
            WorkspaceWorkBatch.id == batch_id,
            WorkspaceWorkBatch.workspace_id == task.workspace_id,
            WorkspaceWorkBatch.entity_id == task.entity_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if batch is None or batch.status != "active":
        return None

    task_ids = _unique_list(batch.task_ids)
    if not task_ids:
        batch.status = "completed"
        batch.completed_at = _utcnow()
        await db.flush()
        return batch

    rows = list((await db.execute(
        select(Task.id, Task.status)
        .where(
            Task.id.in_(task_ids),
            Task.workspace_id == task.workspace_id,
            Task.entity_id == task.entity_id,
        )
    )).all())
    statuses = {task_id: status for task_id, status in rows}
    missing = [task_id for task_id in task_ids if task_id not in statuses]
    open_ids = [
        task_id
        for task_id in task_ids
        if task_id in statuses and statuses.get(task_id) not in TERMINAL_STATUSES
    ]

    batch_details = _as_dict(batch.details)
    batch_details["progress"] = {
        "total": len(task_ids),
        "terminal": len(task_ids) - len(open_ids) - len(missing),
        "open_task_ids": open_ids,
        "missing_task_ids": missing,
        "statuses": statuses,
        "last_checked_at": _utcnow().isoformat(),
        "trigger_source": trigger_source,
    }
    batch.details = _json_safe(batch_details)

    if open_ids or missing:
        await db.flush()
        return None

    now = _utcnow()
    batch.status = "completed"
    batch.completed_at = now
    batch_details["completed_at"] = now.isoformat()
    batch.details = _json_safe(batch_details)

    try:
        await record_activity(
            db,
            task.workspace_id,
            task.entity_id,
            event_type="workspace_work_batch.completed",
            summary=f"Workspace task wave completed: {batch.summary or batch.id}",
            details={"batch_id": batch.id, "task_ids": task_ids, "trigger_source": trigger_source},
            user_id=batch.created_by_user_id,
        )
    except Exception:
        pass

    try:
        from packages.core.tasks.ai_tasks import run_strategist_review

        from packages.core.strategist import ReviewTrigger, ReviewTriggerKind

        run_strategist_review.apply_async(
            args=[task.workspace_id],
            kwargs={
                **ReviewTrigger(
                    kind=ReviewTriggerKind.EVENT,
                    detail=f"work batch completed: {batch.id}",
                ).celery_kwargs(),
                "user_id": batch.created_by_user_id,
            },
            countdown=1,
        )
        batch_details["strategist_triggered_at"] = now.isoformat()
        batch.details = _json_safe(batch_details)
    except Exception:
        batch_details["strategist_trigger_error"] = "dispatch_failed"
        batch.details = _json_safe(batch_details)

    await db.flush()
    return batch


async def discard_operation_draft(
    db: AsyncSession,
    draft_id: str,
    entity_id: str,
    workspace_id: str,
    *,
    user_id: str | None = None,
) -> WorkspaceOperationDraft | None:
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    if await _load_workspace(db, workspace_id, entity_id, for_update=True) is None:
        return None
    draft = await get_operation_draft(db, draft_id, entity_id, workspace_id, for_update=True)
    if draft is None:
        return None
    if draft.status == WorkspaceOperationDraftStatus.OPEN:
        draft.status = WorkspaceOperationDraftStatus.DISCARDED
        draft.discarded_at = _utcnow()
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            workspace_id,
            entity_id,
            event_type="workspace_operation.discarded",
            summary="Workspace operation draft discarded",
            details={"draft_id": draft.id},
            user_id=user_id,
        )
        await db.flush()
    return draft


async def resolve_workspace_operation_review(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
    workspace_id: str | None = None,
    expected_review_fingerprint: str | None = None,
    origin_task_id: str | None = None,
) -> dict[str, Any] | None:
    """Apply or discard a workspace operation draft from a chat approval card.

    Callers pass the fingerprint from the durable card the user answered.
    Legacy cards can still cancel a draft, but cannot authorize unseen content.
    """
    draft_id = _normalise_operation_review_draft_id(hitl_id)
    normalised_action = _normalise_operation_review_action(action)
    if not draft_id or normalised_action is None:
        return None

    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
    )).scalar_one_or_none()
    if conv is None:
        return None

    if workspace_id and conv.workspace_id != workspace_id:
        return None
    scoped_workspace_id = workspace_id or conv.workspace_id
    draft = await get_operation_draft(db, draft_id, entity_id, scoped_workspace_id)
    if draft is None and scoped_workspace_id is None:
        draft = await get_operation_draft(db, draft_id, entity_id)
    if draft is None:
        return None
    if scoped_workspace_id and draft.workspace_id != scoped_workspace_id:
        return None

    if normalised_action == "approve":
        if draft.status == WorkspaceOperationDraftStatus.OPEN:
            if not expected_review_fingerprint:
                return {
                    "status": "review_required",
                    "draft_id": draft.id,
                    "message": "This review card is out of date. Review a fresh card before applying.",
                    "llm_message": (
                        "[Workspace operation not applied]\n"
                        "This card does not identify the reviewed draft contents. "
                        "Show a fresh review card and wait for the user's approval."
                    ),
                }
            review_draft_id, review_workspace_id = draft.id, draft.workspace_id
            try:
                # A conflict becomes a readable Chat result whose caller
                # may still commit the conversation. Roll back the whole
                # apply if continuation validation rejects its final state.
                async with db.begin_nested():
                    result = await apply_operation_draft(
                        db,
                        review_draft_id,
                        entity_id,
                        review_workspace_id,
                        user_id=user_id or None,
                        user_confirmation=True,
                        expected_review_fingerprint=expected_review_fingerprint,
                        origin_task_id=origin_task_id,
                    )
            except OperationConflictError as exc:
                return {
                    "status": "conflict",
                    "draft_id": review_draft_id,
                    "message": str(exc),
                    "llm_message": (
                        "[Workspace operation not applied]\n"
                        f"{exc}\n"
                        "Tell the user this draft is stale and ask whether to create a fresh operation draft."
                    ),
                }
            except OperationValidationError as exc:
                return {
                    "status": "validation_failed",
                    "draft_id": review_draft_id,
                    "validation": exc.validation,
                    "message": "Workspace operation validation failed.",
                    "llm_message": (
                        "[Workspace operation not applied]\n"
                        f"Validation failed: {_json_safe(exc.validation)}\n"
                        "Tell the user what must be fixed before applying."
                    ),
                }
            if result is None:
                return {
                    "status": "not_found",
                    "draft_id": draft.id,
                    "message": "The workspace or operation draft is no longer available. Nothing was applied.",
                    "llm_message": "[Workspace operation not applied]\nThe workspace or draft is no longer available.",
                }
            applied_draft = (result or {}).get("draft") or draft_to_dict(draft)
            changed = _changed_key_summary(applied_draft.get("diff") or {})
            revision = (result or {}).get("workspace_revision")
            message = f"Workspace operation approved and applied. Changed: {changed}."
            if revision is not None:
                message += f" Workspace revision is now {revision}."
            pre_existing = (result or {}).get("pre_existing_issues") or []
            inherited = (
                " The Workspace still has existing issues this change did not cause: "
                + "; ".join(f"{issue.get('path')}: {issue.get('message')}" for issue in pre_existing[:3])
                + ". Mention them so the user can fix them."
            ) if pre_existing else ""
            return {
                "status": "applied",
                "action": "approve",
                "draft_id": draft.id,
                "draft": applied_draft,
                "workspace_revision": revision,
                "pre_existing_issues": pre_existing,
                "message": message,
                "llm_message": (
                    "[Workspace operation approved]\n"
                    f"Applied draft {draft.id}. Changed: {changed}. "
                    "Do not re-apply this draft. Briefly confirm the result to the user."
                    + inherited
                ),
            }
        changed = _changed_key_summary(_as_dict(draft.diff))
        return {
            "status": f"already_{draft.status}",
            "action": "approve" if draft.status == WorkspaceOperationDraftStatus.APPLIED else "reject",
            "draft_id": draft.id,
            "draft": draft_to_dict(draft),
            "message": f"Workspace operation draft is already {draft.status}.",
            "llm_message": (
                "[Workspace operation already resolved]\n"
                f"Draft {draft.id} is already {draft.status}. Changed: {changed}. "
                "Briefly tell the user no new apply was performed."
            ),
        }

    if draft.status == WorkspaceOperationDraftStatus.OPEN:
        discarded = await discard_operation_draft(
            db,
            draft.id,
            entity_id,
            draft.workspace_id,
            user_id=user_id or None,
        )
        if discarded is None:
            return {
                "status": "not_found",
                "draft_id": draft.id,
                "message": "The workspace or operation draft is no longer available.",
                "llm_message": "[Workspace operation not applied]\nThe workspace or draft is no longer available.",
            }
        if discarded.status != WorkspaceOperationDraftStatus.DISCARDED:
            return {
                "status": f"already_{discarded.status}",
                "action": "approve" if discarded.status == WorkspaceOperationDraftStatus.APPLIED else "reject",
                "draft_id": discarded.id,
                "draft": draft_to_dict(discarded),
                "message": f"Workspace operation draft is already {discarded.status}. No discard was performed.",
                "llm_message": (
                    "[Workspace operation already resolved]\n"
                    f"Draft {discarded.id} is already {discarded.status}. No discard was performed."
                ),
            }
        return {
            "status": "discarded",
            "action": "reject",
            "draft_id": draft.id,
            "draft": draft_to_dict(discarded or draft),
            "message": "Workspace operation rejected and discarded.",
            "llm_message": (
                "[Workspace operation rejected]\n"
                f"Discarded draft {draft.id}. Do not apply this operation. "
                "Briefly confirm cancellation to the user."
            ),
        }
    return {
        "status": f"already_{draft.status}",
        "action": "approve" if draft.status == WorkspaceOperationDraftStatus.APPLIED else "reject",
        "draft_id": draft.id,
        "draft": draft_to_dict(draft),
        "message": f"Workspace operation draft is already {draft.status}.",
        "llm_message": (
            "[Workspace operation already resolved]\n"
            f"Draft {draft.id} is already {draft.status}. Briefly tell the user no new discard was performed."
        ),
    }


async def resolve_workspace_operation_review_message(
    db: AsyncSession,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
) -> str | None:
    expected_review_fingerprint = await _persisted_operation_review_fingerprint(
        db, conversation_id=conversation_id, hitl_id=hitl_id,
    )
    result = await resolve_workspace_operation_review(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action=action,
        expected_review_fingerprint=expected_review_fingerprint,
    )
    if not result:
        return None
    if result.get("status") in {"conflict", "validation_failed", "review_required", "not_found"}:
        return str(result.get("llm_message") or result.get("message") or "").strip() or None
    choice = str(result.get("action") or action or "")
    try:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved
        await mark_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=choice,
        )
    except Exception:
        pass
    try:
        await _resolve_operation_review_pending_actions(
            db,
            conversation_id=conversation_id,
            draft_id=_normalise_operation_review_draft_id(hitl_id),
            user_id=user_id,
            choice=choice,
        )
    except Exception:
        pass
    return str(result.get("llm_message") or result.get("message") or "").strip() or None


async def _persisted_operation_review_fingerprint(
    db: AsyncSession, *, conversation_id: str, hitl_id: str,
) -> str | None:
    """Read the clicked version, never substitute the newest card for its draft."""
    from sqlalchemy import or_

    rows = (await db.execute(select(Message).where(
        Message.conversation_id == conversation_id,
        or_(
            Message.meta.contains({"hitl_requests": [{"id": hitl_id}]}),
            Message.pending_action["approval_token"].astext == hitl_id,
            Message.pending_action["draft_id"].astext == hitl_id,
        ),
    ))).scalars().all()
    fingerprints: set[str] = set()
    for row in rows:
        cards = [
            item for item in _as_list(_as_dict(row.meta).get("hitl_requests"))
            if isinstance(item, dict) and str(item.get("id") or "") == hitl_id
        ]
        pending = _as_dict(row.pending_action)
        if str(pending.get("approval_token") or pending.get("draft_id") or "") == hitl_id:
            cards.append(pending)
        for card in cards:
            operation = _as_dict(card.get("operation"))
            if operation.get("kind") != PendingActionKind.WORKSPACE_OPERATION_REVIEW.value:
                continue
            fingerprints.add(str(operation.get("review_fingerprint") or ""))
    # Old cards without evidence and ambiguous old draft-id tokens must be
    # re-presented; a hash supplied in an arbitrary request is not approval.
    return next(iter(fingerprints)) if len(fingerprints) == 1 and "" not in fingerprints else None


async def _resolve_operation_review_pending_actions(
    db: AsyncSession,
    *,
    conversation_id: str,
    draft_id: str,
    user_id: str,
    choice: str,
) -> int:
    """Close the durable ``pending_action`` card(s) for an already-resolved draft.

    One workspace-operation review is persisted twice on the same message:
    ``meta.hitl_requests`` for the personal-chat surfaces and
    ``pending_action`` for workspace chat and the runtime's own resolver. A
    click on the hitl copy applies the draft; without this the workspace-chat
    card stayed open, offering Approve on a draft that was already applied.
    """
    if not draft_id:
        return 0
    from packages.core.workspace_chat import service as workspace_chat_service

    rows = list((await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
            Message.resolved_at.is_(None),
        )
    )).scalars().all())
    resolved = 0
    for row in rows:
        action = row.pending_action if isinstance(row.pending_action, dict) else {}
        if action.get("kind") != PendingActionKind.WORKSPACE_OPERATION_REVIEW.value:
            continue
        row_draft_id = _normalise_operation_review_draft_id(
            action.get("draft_id") or action.get("approval_token") or ""
        )
        if row_draft_id != draft_id:
            continue
        # The hitl path already persisted the user's choice and the assistant
        # continuation; a second receipt here would show the outcome twice.
        await workspace_chat_service.resolve_pending_action(
            db,
            message_id=row.id,
            user_id=user_id,
            resolution={"choice": choice},
            emit_followup=False,
        )
        resolved += 1
    return resolved


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "task_type": task.task_type,
        "owner_service_key": task.owner_service_key,
        "owner_subscription_id": task.owner_subscription_id,
        "delegate_service_keys": task.delegate_service_keys or [],
        "details": task.details or {},
    }


def draft_to_dict(draft: WorkspaceOperationDraft) -> dict[str, Any]:
    return {
        "id": draft.id,
        "workspace_id": draft.workspace_id,
        "entity_id": draft.entity_id,
        "created_by_user_id": draft.created_by_user_id,
        "source_event_id": draft.source_event_id,
        "base_revision": draft.base_revision,
        "status": draft.status,
        "current_state": _json_safe(draft.current_state or {}),
        "patches": _json_safe(draft.patches or []),
        "validation": _json_safe(draft.validation or {}),
        "diff": _json_safe(draft.diff or {}),
        "created_at": _iso_loaded(draft, "created_at"),
        "updated_at": _iso_loaded(draft, "updated_at"),
        "applied_at": _iso_loaded(draft, "applied_at"),
        "discarded_at": _iso_loaded(draft, "discarded_at"),
    }


def work_batch_to_dict(batch: WorkspaceWorkBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "workspace_id": batch.workspace_id,
        "entity_id": batch.entity_id,
        "created_by_user_id": batch.created_by_user_id,
        "source_draft_id": batch.source_draft_id,
        "source_kind": batch.source_kind,
        "summary": batch.summary,
        "status": batch.status,
        "task_ids": _json_safe(batch.task_ids or []),
        "details": _json_safe(batch.details or {}),
        "completed_at": _iso_loaded(batch, "completed_at"),
        "created_at": _iso_loaded(batch, "created_at"),
        "updated_at": _iso_loaded(batch, "updated_at"),
    }


def _iso_loaded(obj: Any, attr: str) -> str | None:
    value = getattr(obj, "__dict__", {}).get(attr)
    return value.isoformat() if value else None
