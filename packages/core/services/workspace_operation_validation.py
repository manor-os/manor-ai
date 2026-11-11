"""Validate operation values before a review can promise runtime changes."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.cron import validate_cron_expression
from packages.core.governance.policy import PolicyError, policy_from_dict
from packages.core.models.channel import ChannelConfig


def _channel_entries(config: dict[str, Any]) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for field in ("channels", "secondary_external_channels"):
        if isinstance(config.get(field), list):
            entries.extend((f"channel_config.{field}[{idx}]", row) for idx, row in enumerate(config[field]))
    for field in ("primary_external_channel", "internal_channel"):
        if config.get(field) is not None:
            entries.append((f"channel_config.{field}", config[field]))
    return entries


def validate_operation_domain_values(
    state: dict[str, Any],
    *,
    changed_keys: set[str],
) -> list[dict[str, str]]:
    """Keep legacy, unchanged domains from blocking an unrelated operation."""
    errors: list[dict[str, str]] = []

    def error(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    def booleans(values: dict[str, Any], fields: tuple[str, ...], path: str) -> None:
        for field in fields:
            if field in values and not isinstance(values[field], bool):
                error(f"{path}.{field}", f"{field} must be a JSON boolean (true or false)")

    if "budget_policy" in changed_keys:
        budget = state.get("budget_policy") or {}
        for field in ("monthly_budget_credits", "monthly_budget_usd"):
            value = budget.get(field)
            if value is None:
                continue
            try:
                valid_number = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0
            except OverflowError:
                valid_number = False
            if not valid_number:
                error(f"budget_policy.{field}", f"{field} must be a finite, nonnegative JSON number or null")
        booleans(budget, ("auto_pause_on_budget", "reset_alert_state"), "budget_policy")

    if "heartbeat_policy" in changed_keys:
        heartbeat = state.get("heartbeat_policy") or {}
        booleans(heartbeat, ("enabled",), "heartbeat_policy")
        cadence = heartbeat.get("cadence")
        if cadence is not None:
            named_cadences = {"hourly", "daily", "weekly", "biweekly"}
            invalid_cadence = not isinstance(cadence, str)
            if isinstance(cadence, str) and cadence.strip() and cadence.strip() not in named_cadences:
                try:
                    validate_cron_expression(cadence.strip())
                except ValueError:
                    invalid_cadence = True
            if invalid_cadence:
                error("heartbeat_policy.cadence", "cadence must be hourly/daily/weekly/biweekly or a valid cron expression")

    if "knowledge_policy" in changed_keys:
        knowledge = state.get("knowledge_policy") or {}
        booleans(knowledge, ("auto_search", "citation_required", "strict_mode"), "knowledge_policy")
        if "retrieval_mode" in knowledge and (
            not isinstance(knowledge["retrieval_mode"], str)
            or knowledge["retrieval_mode"] not in {"auto", "manual", "strict"}
        ):
            error("knowledge_policy.retrieval_mode", "retrieval_mode must be auto, manual, or strict")

    if "governance_policy" in changed_keys:
        try:
            policy_from_dict(state.get("governance_policy"))
        except (PolicyError, TypeError) as exc:
            error("governance_policy", str(exc))

    if "immediate_tasks" in changed_keys:
        for idx, task in enumerate(state.get("immediate_tasks") or []):
            if not isinstance(task, dict):
                continue  # The operation service reports the structural error.
            if "priority" in task:
                priority = task["priority"]
                if type(priority) is not int or not 1 <= priority <= 5:
                    error(f"immediate_tasks[{idx}].priority", "priority must be an integer from 1 to 5")
            for field in ("details", "runtime_context"):
                if field in task and not isinstance(task[field], dict):
                    error(f"immediate_tasks[{idx}].{field}", f"{field} must be an object")
            if "delegate_service_keys" in task and (
                not isinstance(task["delegate_service_keys"], list)
                or any(not isinstance(value, str) or not value.strip() for value in task["delegate_service_keys"])
            ):
                error(f"immediate_tasks[{idx}].delegate_service_keys", "delegate_service_keys must be an array of nonempty service keys")

    if "channel_config" in changed_keys:
        config = state.get("channel_config") or {}
        for field in ("channels", "secondary_external_channels"):
            if field not in config:
                continue
            if not isinstance(config[field], list):
                error(f"channel_config.{field}", f"{field} must be an array of channel objects")
                continue
        services = {
            str(row.get("key") or row.get("service_key") or "").strip()
            for row in state.get("services") or [] if isinstance(row, dict)
        }
        mapped_services = {
            str(row.get("service_key") or "").strip()
            for row in state.get("agent_mappings") or [] if isinstance(row, dict)
        }
        for path, channel in _channel_entries(config):
            if not isinstance(channel, dict):
                error(path, "channel must be an object")
                continue
            channel_type = channel.get("channel_type") or channel.get("type")
            if not isinstance(channel_type, str) or not channel_type.strip():
                error(f"{path}.channel_type", "channel requires a nonempty channel_type")
                continue
            booleans(channel, ("login_required",), path)
            for field in ("agent_id", "agent_subscription_id"):
                if channel.get(field):
                    error(f"{path}.{field}", "target a Workspace Agent with linked_service_key; direct Agent or subscription IDs are not supported by channel operations")
            if "config" in channel and not isinstance(channel["config"], dict):
                error(f"{path}.config", "channel config must be an object")
            linked_service = channel.get("linked_service_key") or channel.get("service_key")
            if linked_service is not None:
                if not isinstance(linked_service, str) or linked_service not in services:
                    error(f"{path}.linked_service_key", "channel references an unknown service")
                elif linked_service not in mapped_services:
                    error(f"{path}.linked_service_key", "channel service requires an Agent mapping in this draft")
            if channel_type not in {"webchat", "internal_chat", "in_app"}:
                config_id = channel.get("channel_config_id")
                if not isinstance(config_id, str) or not config_id.strip():
                    error(f"{path}.channel_config_id", "external channels require an existing channel_config_id; connect the channel account before binding it")

    return errors


async def validate_operation_channel_references(
    db: AsyncSession,
    state: dict[str, Any],
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
) -> list[dict[str, str]]:
    references = [
        (path, channel)
        for path, channel in _channel_entries(state.get("channel_config") or {})
        if isinstance(channel, dict)
        and isinstance(channel.get("channel_config_id"), str)
        and channel["channel_config_id"].strip()
    ]
    if not references:
        return []
    # Match the Workspace Channel API's account-owner boundary. Select no
    # credentials, and keep these rows locked through the apply transaction.
    rows = (await db.execute(
        select(ChannelConfig.id, ChannelConfig.channel_type, ChannelConfig.provider)
        .where(
            ChannelConfig.id.in_({channel["channel_config_id"] for _, channel in references}),
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.status == "active",
            or_(ChannelConfig.workspace_id.is_(None), ChannelConfig.workspace_id == workspace_id),
            or_(
                and_(ChannelConfig.owner_user_id == user_id, ChannelConfig.owner_user_id.is_not(None)),
                and_(ChannelConfig.owner_user_id.is_(None), ChannelConfig.workspace_id == workspace_id),
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).all()
    configs = {row.id: row for row in rows}
    errors: list[dict[str, str]] = []
    for path, channel in references:
        config = configs.get(channel["channel_config_id"])
        if config is None:
            errors.append({
                "path": f"{path}.channel_config_id",
                "code": "unavailable_channel_config",
                "message": "channel config is unavailable to this user and workspace; select an active channel account you own",
            })
            continue
        requested_type = channel.get("channel_type") or channel.get("type")
        if requested_type != config.channel_type:
            errors.append({"path": f"{path}.channel_type", "message": "channel_type must match the selected channel config"})
        if channel.get("provider") and channel["provider"] != config.provider:
            errors.append({"path": f"{path}.provider", "message": "provider must match the selected channel config"})
    return errors


#: Warning code for an integration a draft adds or restores that its author
#: has not connected yet. Never an error: the draft applies with it pending
#: and the Workspace asks to connect it (``ask_to_connect_pending_integrations``).
INTEGRATION_PENDING_CONNECTION = "integration_pending_connection"

_ARRAY_INDEX = re.compile(r"\[\d+\]")
_PATH_SEGMENT = re.compile(r"^([^\[]*)((?:\[\d+\])*)$")

# Tenant, access and authorization boundaries. A Workspace that already
# crosses one is never a reason to let another change through: these block
# every draft until fixed, whether or not this draft introduced them.
NEVER_INHERITED_ERROR_CODES = frozenset({
    "foreign_entity_agent",
    "unavailable_workflow",
    "unavailable_app_target",
    "unavailable_channel_config",
    "task_scoped_binding_unsupported",
    "rule_implied_governance",
    "invalid_patch_shape",
    # Not a boundary, but apply cannot proceed: it would rewrite one of two
    # live deployments of the same service.
    "duplicate_service_change",
})


def _offending_element(state: Any, path: str) -> Any:
    """The array row a path points into (``agent_mappings[3].x`` -> that row)."""
    node = state
    for segment in path.split("."):
        match = _PATH_SEGMENT.match(segment)
        if match is None or not isinstance(node, dict):
            return None
        node = node.get(match.group(1))
        indexes = [int(value) for value in re.findall(r"\d+", match.group(2))]
        if indexes:
            for index in indexes:
                if not isinstance(node, list) or index >= len(node):
                    return None
                node = node[index]
            return node
    return None


def validation_issue_identity(issue: dict[str, Any], state: Any = None) -> tuple[str, ...]:
    """What a problem is, independent of where an array put it.

    A patch that removes an earlier row shifts every later index; the problem
    ``agent_mappings[3]`` had is the same problem at ``agent_mappings[2]``.
    The offending row's content is part of the identity, so an error is
    inherited only while the same row carries it: fixing one row and breaking
    another with the same message is a new error.
    """
    path = str(issue.get("path") or "")
    element = _offending_element(state, path) if state is not None else None
    content = (
        json.dumps(element, sort_keys=True, ensure_ascii=False, default=str)
        if element is not None else ""
    )
    return (
        *(_ARRAY_INDEX.sub("[]", str(issue.get(field) or "")) for field in ("path", "code", "message")),
        content,
    )


def partition_new_validation_errors(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    before_state: Any = None,
    after_state: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``after`` into errors a change introduced and errors it inherited.

    Counted as a multiset: a second copy of an existing problem is new.
    Boundary codes (``NEVER_INHERITED_ERROR_CODES``) are always introduced.
    """
    remaining = Counter(
        validation_issue_identity(issue, before_state) for issue in before if isinstance(issue, dict)
    )
    introduced: list[dict[str, Any]] = []
    inherited: list[dict[str, Any]] = []
    for issue in after:
        if not isinstance(issue, dict) or issue.get("code") in NEVER_INHERITED_ERROR_CODES:
            introduced.append(issue)
            continue
        identity = validation_issue_identity(issue, after_state)
        if remaining[identity] > 0:
            remaining[identity] -= 1
            inherited.append(issue)
        else:
            introduced.append(issue)
    return introduced, inherited
