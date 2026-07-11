"""LLM-driven conversational workspace setup service.

Implements a multi-turn conversation where the LLM collects information to
build a workspace operating model.  State is tracked in a WorkspaceSetupSession
dataclass that is passed in and out of each turn (stateless API).

Ported from manor-multi-agent operation_create.py / operation_create_prompt.py.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.capability_bindings import (
    normalize_workspace_custom_agent_tool_bindings,
)
from packages.core.ai.runtime import (
    runtime_execute_workspace_setup_agent_mapping_completion,
    runtime_execute_workspace_setup_auto_model_completion,
    runtime_execute_workspace_setup_turn_completion,
    runtime_workspace_setup_system_prompt,
    runtime_workspace_setup_user_message,
)
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceActivity,
)
from packages.core.models.goal import Goal
from packages.core.services.knowledge_starter import with_starter_document_settings
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.workspace_access import (
    ensure_workspace_owner_membership,
    settings_with_default_workspace_access,
)
# Task import previously used by the seeded starter task; removed
# along with the seed in finalize_setup. Add back if any task creation
# moves into setup again.

logger = logging.getLogger(__name__)

_PUBLIC_TEMPLATE_AGENT_SOURCE = "template"

# ---------------------------------------------------------------------------
# Required fields before a workspace can be finalized
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {
    "kind",
    "name",
    "operating_context",
    "primary_work",
    "services",
    "agent_mappings",
    "channel_config",
}

# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------

DEFAULT_FIELDS: Dict[str, Any] = {
    "kind": "",
    "name": "",
    "operating_context": "",
    "primary_work": "",
    "services": [],
    "agent_mappings": [],
    "goals": [],
    "rules": [],
    "automations": [],
    "evaluation": {
        "enabled": True,
        "cadence": "",
        "scorecard": [],
        "target_score": None,
        "warning_score": None,
        "notes": "",
    },
    "budget_policy": {
        "monthly_budget_credits": None,
        "auto_pause_on_budget": True,
        "notes": "",
    },
    "channel_config": {
        "channels": [],
        "notes": "",
    },
    "notes": "",
}


@dataclass
class WorkspaceSetupSession:
    """Holds conversation state for workspace creation.

    Designed to be serializable (e.g. to JSON via dataclasses.asdict)
    so it can be stored in a cache or passed through an API boundary.
    """

    entity_id: str
    fields: Dict[str, Any]
    messages: List[Dict[str, str]]  # conversation history
    ready: bool = False
    missing: List[str] = field(default_factory=list)
    user_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "fields": self.fields,
            "messages": self.messages,
            "ready": self.ready,
            "missing": list(self.missing),
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkspaceSetupSession":
        return cls(
            entity_id=data["entity_id"],
            fields=data.get("fields", copy.deepcopy(DEFAULT_FIELDS)),
            messages=data.get("messages", []),
            ready=data.get("ready", False),
            missing=data.get("missing", list(REQUIRED_FIELDS)),
            user_id=data.get("user_id"),
        )


# ---------------------------------------------------------------------------
# System prompt for setup wizard
# ---------------------------------------------------------------------------

SETUP_SYSTEM_PROMPT = runtime_workspace_setup_system_prompt(
    default_fields=DEFAULT_FIELDS,
    required_fields=REQUIRED_FIELDS,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_setup(entity_id: str) -> WorkspaceSetupSession:
    """Initialize a new workspace setup session."""
    return WorkspaceSetupSession(
        entity_id=entity_id,
        fields=copy.deepcopy(DEFAULT_FIELDS),
        messages=[],
        ready=False,
        missing=sorted(REQUIRED_FIELDS),
    )


async def process_setup_turn(
    session: WorkspaceSetupSession,
    user_message: str,
    db: AsyncSession,
    *,
    metadata: Optional[Dict[str, Any]] = None,
    stream_handler: Optional[Any] = None,
) -> Tuple[str, WorkspaceSetupSession]:
    """Process one turn of the setup conversation.

    Uses the Runtime completion boundary with a system prompt that guides the LLM to:
    1. Ask about workspace kind/name first
    2. Then operating context and primary work
    3. Then auto-suggest services based on the above
    4. Then resolve agent mappings (match services to available agents)
    5. Then channel configuration

    Returns (assistant_response, updated_session). Pass ``stream_handler``
    to receive per-token events (signature: ``(event_name, payload)``);
    when set, the visible reply still comes back as the return value but
    tokens stream out incrementally in parallel.
    """
    # Build context block with available agents for the entity
    context = await _build_setup_context(session.entity_id, db)
    enriched_message = runtime_workspace_setup_user_message(
        user_message=user_message,
        context=context,
    )

    # Append user message to conversation history
    session.messages.append({"role": "user", "content": enriched_message})

    # Call LLM (optionally streaming)
    completion = await runtime_execute_workspace_setup_turn_completion(
        entity_id=session.entity_id,
        system_prompt=SETUP_SYSTEM_PROMPT,
        session_messages=session.messages,
        metadata=metadata,
        stream_handler=stream_handler,
    )
    response_text = completion.content

    # Parse status block from response
    status = _extract_status_block(response_text)
    if status:
        session.fields = status.get("fields", session.fields)
        session.ready = status.get("ready", False)
        session.missing = status.get("missing", [])

    # Strip the status block from the visible response
    visible_response = _strip_status_block(response_text).strip()

    # Append assistant message (with full text for context continuity)
    session.messages.append({"role": "assistant", "content": response_text})

    return visible_response, session


async def auto_generate_model(
    session: WorkspaceSetupSession,
    db: AsyncSession,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """After basic fields collected, auto-generate full operating model.

    Uses LLM to suggest: services, goals, rules, automations, evaluation
    scorecard.  Based on: kind, name, operating_context, primary_work.

    Returns the generated operating model dict.
    """
    completion = await runtime_execute_workspace_setup_auto_model_completion(
        entity_id=session.entity_id,
        fields=session.fields,
        metadata=metadata,
    )
    response_text = completion.content

    try:
        model = json.loads(response_text.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        json_match = re.search(r"\{[\s\S]+\}", response_text)
        if json_match:
            try:
                model = json.loads(json_match.group())
            except json.JSONDecodeError:
                logger.warning("Failed to parse auto-generated model JSON")
                model = {}
        else:
            model = {}

    return model


async def resolve_agent_mappings(
    session: WorkspaceSetupSession,
    db: AsyncSession,
    *,
    metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Match workspace services to available agents.

    Strategy order:
    1. Exact name/category match in entity's agents
    2. Semantic match via LLM
    3. Suggest marketplace agents
    4. Suggest creating custom agent

    Returns list of {service_key, agent_id, strategy, reason} dicts.
    """
    services = session.fields.get("services", [])
    if not services:
        return []

    # Fetch entity agents
    entity_agents = await _fetch_entity_agents(session.entity_id, db)
    marketplace_agents = await _fetch_marketplace_agents(db)

    all_agents = entity_agents + marketplace_agents

    if not all_agents:
        # No agents available -- suggest creating custom agents for everything
        return [
            {
                "service_key": svc.get("service_key", ""),
                "agent_id": None,
                "strategy": "create_custom",
                "reason": "No agents available; suggest creating a custom agent.",
                "create_agent_draft": {
                    "agent_name": f"{svc.get('service_key', 'service').replace('_', ' ').title()} Agent",
                    "category": session.fields.get("kind", "general"),
                },
            }
            for svc in services
        ]

    # Use LLM for semantic matching
    agent_descriptions = [
        {
            "id": a["id"],
            "name": a["name"],
            "description": a.get("description", ""),
            "category": a.get("category", ""),
            "tools": a.get("tools", []),
            "skills": a.get("skills", []),
            "integrations": a.get("integrations", []),
            "source": a.get("source", "entity"),
        }
        for a in all_agents
    ]

    service_descriptions = [
        {
            "service_key": svc.get("service_key", ""),
            "description": svc.get("description", ""),
        }
        for svc in services
    ]

    completion = await runtime_execute_workspace_setup_agent_mapping_completion(
        entity_id=session.entity_id,
        service_descriptions=service_descriptions,
        agent_descriptions=agent_descriptions,
        metadata=metadata,
    )
    response_text = completion.content

    try:
        mappings = json.loads(response_text.strip())
    except json.JSONDecodeError:
        json_match = re.search(r"\[[\s\S]+\]", response_text)
        if json_match:
            try:
                mappings = json.loads(json_match.group())
            except json.JSONDecodeError:
                mappings = []
        else:
            mappings = []

    if not isinstance(mappings, list):
        mappings = []

    # Ensure every service has a mapping
    mapped_keys = {m.get("service_key") for m in mappings}
    for svc in services:
        key = svc.get("service_key", "")
        if key and key not in mapped_keys:
            mappings.append({
                "service_key": key,
                "agent_id": None,
                "strategy": "create_custom",
                "reason": "No suitable agent found.",
                "create_agent_draft": {
                    "agent_name": f"{key.replace('_', ' ').title()} Agent",
                    "category": session.fields.get("kind", "general"),
                },
            })

    return mappings


def _coerce_goal_number(value: Any) -> float:
    """Convert loose goal targets like "10,000" or "5%" into a DB number."""
    if value is None:
        return 0
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text)
    if not match:
        return 0
    return float(match.group(0).replace(",", ""))


def _coerce_positive_int(value: Any) -> int | None:
    """Convert loose numeric input into a positive integer, or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


async def _resolve_workspace_flagged_integrations(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    flagged: list[Any],
) -> list[dict[str, Any]]:
    from packages.core.services.integration_resolution import resolve_missing_integration_flags

    return await resolve_missing_integration_flags(
        db,
        entity_id=entity_id,
        user_id=user_id,
        flagged=flagged,
    )


_WEEKDAY_CRON: dict[str, str] = {
    "sunday": "0",
    "sun": "0",
    "monday": "1",
    "mon": "1",
    "tuesday": "2",
    "tue": "2",
    "tues": "2",
    "wednesday": "3",
    "wed": "3",
    "thursday": "4",
    "thu": "4",
    "thurs": "4",
    "friday": "5",
    "fri": "5",
    "saturday": "6",
    "sat": "6",
}


def _automation_slug(value: Any, fallback: str = "automation") -> str:
    text = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (slug or fallback)[:60]


def _automation_display_name(automation: dict[str, Any], index: int) -> str:
    raw = (
        automation.get("title")
        or automation.get("name")
        or automation.get("automation_type")
        or automation.get("automation_key")
        or automation.get("service_key")
        or f"Workspace automation {index + 1}"
    )
    return str(raw).replace("_", " ").replace("-", " ").strip().title()


def _automation_time(text: str) -> tuple[int, int]:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.I)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return hour % 24, max(0, min(minute, 59))

    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if match:
        return int(match.group(1)), int(match.group(2))

    if "afternoon" in text:
        return 13, 0
    if "evening" in text:
        return 17, 0
    return 9, 0


def _looks_like_cron_expr(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.strip().split()
    if len(parts) != 5:
        return False
    field_re = re.compile(r"^(\*|\*/\d+|\d{1,2}(?:-\d{1,2})?(?:,\d{1,2}(?:-\d{1,2})?)*)$")
    return all(field_re.match(part) for part in parts)


def _find_cron_expr(text: str) -> str | None:
    if _looks_like_cron_expr(text):
        return text.strip()
    tokens = text.split()
    for i in range(0, max(0, len(tokens) - 4)):
        candidate = " ".join(tokens[i:i + 5])
        if _looks_like_cron_expr(candidate):
            return candidate
    return None


def _automation_schedule(automation: dict[str, Any]) -> tuple[str, str, str | None, float | None]:
    """Translate loose workspace-draft automation cadence into ScheduledJob fields."""
    direct_cron = automation.get("cron_expr") or automation.get("cron")
    if _looks_like_cron_expr(direct_cron):
        return "cron", "cron", direct_cron.strip(), None

    text = " ".join(
        str(automation.get(key) or "")
        for key in ("schedule", "cadence", "trigger", "description")
    ).strip()
    lowered = text.lower()

    cron_expr = _find_cron_expr(text)
    if cron_expr:
        return "cron", "cron", cron_expr, None

    every_match = re.search(
        r"\bevery\s+(\d+)\s*(minute|minutes|min|mins|m|hour|hours|hr|hrs|h|day|days|d)\b",
        lowered,
    )
    if every_match:
        amount = int(every_match.group(1))
        unit = every_match.group(2)
        if unit.startswith(("minute", "min")) or unit == "m":
            return "interval", "every", None, float(amount * 60)
        if unit.startswith(("hour", "hr")) or unit == "h":
            return "interval", "every", None, float(amount * 3600)
        return "interval", "every", None, float(amount * 86400)

    if "hourly" in lowered or "every hour" in lowered:
        return "interval", "every", None, 3600.0

    hour, minute = _automation_time(lowered)
    weekday_hits = [
        cron
        for name, cron in _WEEKDAY_CRON.items()
        if re.search(rf"\b{name}\b", lowered)
    ]
    if "weekday" in lowered or "business day" in lowered:
        return "cron", "cron", f"{minute} {hour} * * 1-5", None
    if weekday_hits:
        days = ",".join(sorted(set(weekday_hits), key=int))
        return "cron", "cron", f"{minute} {hour} * * {days}", None
    if "weekly" in lowered or "each week" in lowered:
        return "cron", "cron", f"{minute} {hour} * * 1", None
    if "monthly" in lowered or "each month" in lowered:
        return "cron", "cron", f"{minute} {hour} 1 * *", None

    return "cron", "cron", f"{minute} {hour} * * *", None


async def _install_workspace_draft_automations(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    automations: list[Any],
    sub_by_service: dict[str, dict[str, str]],
    created_subs: list[dict[str, str]],
    user_id: str | None,
) -> int:
    """Turn setup-draft automations into real workspace-scoped scheduled jobs."""
    if not automations:
        return 0

    from packages.core.models.scheduler import ScheduledJob
    from packages.core.services.scheduler_service import create_scheduled_job

    created = 0
    fallback_agent_id = created_subs[0].get("agent_id") if created_subs else None
    for idx, raw in enumerate(automations):
        if not isinstance(raw, dict):
            continue

        key = _automation_slug(
            raw.get("automation_key")
            or raw.get("automation_type")
            or raw.get("name")
            or raw.get("title")
            or f"automation-{idx + 1}",
        )
        job_id = f"wa:{workspace_id}:{key}"[:100]
        existing = (await db.execute(
            select(ScheduledJob).where(ScheduledJob.job_id == job_id)
        )).scalar_one_or_none()
        if existing:
            continue

        service_key = str(raw.get("service_key") or "").strip()
        matched_sub = sub_by_service.get(service_key) if service_key else None
        agent_id = (matched_sub or {}).get("agent_id") or fallback_agent_id
        job_type, schedule_kind, cron_expr, every_seconds = _automation_schedule(raw)
        name = _automation_display_name(raw, idx)
        trigger = str(raw.get("trigger") or raw.get("schedule") or "").strip()
        description = str(raw.get("description") or "").strip()
        prompt_parts = [
            f"Run the workspace automation: {name}.",
            f"Workspace ID: {workspace_id}.",
        ]
        if service_key:
            prompt_parts.append(f"Responsible service: {service_key}.")
        if trigger:
            prompt_parts.append(f"Original trigger/cadence: {trigger}.")
        if description:
            prompt_parts.append(f"Automation description: {description}.")
        prompt_parts.append(
            "Complete the work using workspace context, then report the concrete outcome, "
            "files created, messages sent, or blockers."
        )

        await create_scheduled_job(
            db,
            entity_id,
            job_id,
            name,
            job_type=job_type,
            schedule_kind=schedule_kind,
            cron_expr=cron_expr,
            every_seconds=every_seconds,
            timezone_str="UTC",
            payload_message="\n".join(prompt_parts),
            agent_id=agent_id,
            execution_type="agent",
            workspace_id=workspace_id,
            user_id=user_id,
        )
        created += 1

    return created


def _should_generate_starter_doc(attachment: dict[str, Any], mode: str) -> bool:
    explicit = _optional_bool(attachment.get("generate_starter_doc"))
    if explicit is not None:
        return explicit
    # New proposed groups keep the existing behavior. Template/existing
    # bindings can still opt in explicitly when a starter doc is useful.
    return mode == "create_new"


_EXTERNAL_PUBLISH_ACTIONS = [
    "social_post.publish",
    "email.send",
    "external_message.send",
]
_DESTRUCTIVE_EXTERNAL_ACTIONS = ["social_post.delete", "email.delete"]
_GOVERNANCE_POLICY_KEYS = {
    "never_allow_actions",
    "hitl_required_actions",
    "auto_approve_actions",
    "never_allow_capabilities",
    "hitl_required_capabilities",
    "auto_approve_capabilities",
    "max_risk_level",
    "budget_caps_per_kind",
}
_GOVERNANCE_ACTION_TO_CAPABILITY_FIELD = {
    "never_allow_actions": "never_allow_capabilities",
    "hitl_required_actions": "hitl_required_capabilities",
    "auto_approve_actions": "auto_approve_capabilities",
}


def _unique_strings(values: Any) -> list[str]:
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


def _rule_text(rule: dict[str, Any]) -> str:
    return " ".join(
        str(rule.get(key) or "")
        for key in ("description", "rule_key", "scope", "notes")
    ).strip()


def _explicit_rule_patterns(rule: dict[str, Any]) -> list[str]:
    for key in ("action_patterns", "actions", "patterns"):
        patterns = _unique_strings(rule.get(key))
        if patterns:
            return patterns
    return []


def _explicit_rule_capability_patterns(rule: dict[str, Any]) -> list[str]:
    for key in ("capability_patterns", "capabilities", "capability_ids"):
        patterns = _unique_strings(rule.get(key))
        if patterns:
            return patterns
    return []


def _capability_patterns_for_action_patterns(patterns: list[str]) -> list[str]:
    from packages.core.ai.runtime import runtime_capability_id_for_action_key

    capability_patterns: list[str] = []
    for pattern in patterns:
        capability_id = runtime_capability_id_for_action_key(pattern)
        if capability_id:
            capability_patterns.append(capability_id)
    return _unique_strings(capability_patterns)


def _infer_rule_patterns(text: str) -> list[str]:
    lower = text.lower()
    patterns: list[str] = []
    fileish = bool(re.search(r"workspace.*file|file|document|doc|knowledge|知识库|文件|文档|资料", lower))
    create_only = bool(re.search(r"只能.*(添加|新增|创建)|只.*(添加|新增|创建)|only\s+(add|create)|add[- ]?only|create[- ]?only", lower))
    if fileish:
        if create_only:
            patterns.extend(["workspace.file.modify", "workspace.file.delete", "workspace.file.write"])
        else:
            if re.search(r"改|修改|编辑|更新|覆盖|写入|变更|modify|edit|update|overwrite|write|change", lower):
                patterns.append("workspace.file.modify")
            if re.search(r"删除|移除|delete|remove|destroy", lower):
                patterns.append("workspace.file.delete")
            if re.search(r"添加|新增|创建|add|create", lower):
                patterns.append("workspace.file.create")
    if re.search(r"post|发帖|发\s*post|社媒|social|linkedin|twitter|tweet|\bx\b|xhs|小红书|facebook|instagram|发布", lower):
        patterns.append("social_post.publish")
    if re.search(r"email|e-mail|邮件|gmail|outlook", lower):
        patterns.append("email.send")
    if re.search(r"message|消息|wechat|微信|messenger|\bdm\b|私信", lower):
        patterns.append("external_message.send")
    if re.search(r"对外|external|public|公开|发送|send|publish|发布", lower) and not patterns:
        patterns.extend(_EXTERNAL_PUBLISH_ACTIONS)
    if re.search(r"删除|delete|remove|destroy", lower) and (
        not fileish
        or re.search(r"post|发帖|发\s*post|社媒|social|linkedin|twitter|tweet|\bx\b|xhs|小红书|facebook|instagram|发布", lower)
        or re.search(r"email|e-mail|邮件|gmail|outlook", lower)
        or re.search(r"message|消息|wechat|微信|messenger|\bdm\b|私信", lower)
        or re.search(r"对外|external|public|公开|发送|send|publish|发布", lower)
    ):
        patterns.extend(_DESTRUCTIVE_EXTERNAL_ACTIONS)
    return _unique_strings(patterns)


def _is_conditional_rule(text: str) -> bool:
    """The simple policy matcher has no conditional language runtime.

    Keep conditional rules agent-visible, but avoid turning "never post on
    Sundays" into an unconditional block of every social post.
    """
    lower = text.lower()
    return bool(re.search(
        r"\b(if|when|unless|except|only when|only if|on sunday|on monday|"
        r"on tuesday|on wednesday|on thursday|on friday|on saturday|"
        r"weekend|weekday)\b|如果|当|除非|超过|大于|少于|小于|周[一二三四五六日天末]|星期|礼拜",
        lower,
    ))


def _is_platform_specific_social_deny(text: str, patterns: list[str]) -> bool:
    """Avoid turning a platform-specific publish rule into a global ban.

    The policy matcher currently sees only normalized action keys such as
    ``social_post.publish``; it cannot distinguish X from Xiaohongshu. When the
    user names one platform without explicit action patterns, enforcing
    ``social_post.publish`` would also block unrelated social publishing. Keep
    those rules agent-visible until provider-scoped policy matching exists.
    """
    if not any(pattern.startswith("social_post.") for pattern in patterns):
        return False
    lower = text.lower()
    has_platform = bool(re.search(
        r"\b(xhs|xiaohongshu|rednote|twitter|tweet|linkedin|facebook|instagram)\b|"
        r"小红书|推特|领英|微博|抖音|微信",
        lower,
    ))
    if not has_platform:
        return False
    has_broad_scope = bool(re.search(
        r"\b(all|any|every|social|social media|all platforms|public social)\b|"
        r"所有|全部|任意|任何|社媒|全平台|公开社交",
        lower,
    ))
    return not has_broad_scope


def _infer_rule_enforcement(rule: dict[str, Any]) -> dict[str, Any] | None:
    text = _rule_text(rule)
    explicit_capability_patterns = _explicit_rule_capability_patterns(rule)
    explicit_patterns = _explicit_rule_patterns(rule)
    if not text and not explicit_patterns and not explicit_capability_patterns:
        return None

    patterns = explicit_patterns or _infer_rule_patterns(text)
    capability_patterns = explicit_capability_patterns or _capability_patterns_for_action_patterns(patterns)
    if not patterns and not capability_patterns:
        return None

    lower = text.lower()
    rule_type = str(rule.get("rule_type") or "").strip().lower()
    severity = str(rule.get("severity") or "").strip().lower()
    approvalish = bool(re.search(
        r"审核|审批|批准|同意|确认|给用户|用户同意|人工|human|review|approve|approval|"
        r"consent|confirm|permission",
        lower,
    ))
    create_only = bool(re.search(
        r"只能.*(添加|新增|创建)|只.*(添加|新增|创建)|only\s+(add|create)|add[- ]?only|create[- ]?only",
        lower,
    ))
    denyish = bool(re.search(
        r"禁止|不要|不得|不准|不允许|不能|只生成草稿|草稿|never|deny|block|don't|do not|draft[- ]?only",
        lower,
    )) or create_only

    def _result(field: str, rule_type_value: str) -> dict[str, Any]:
        return {
            "field": field,
            "rule_type": rule_type_value,
            "patterns": patterns,
            "capability_field": _GOVERNANCE_ACTION_TO_CAPABILITY_FIELD[field],
            "capability_patterns": capability_patterns,
        }

    inferred_social_publish_only = (
        not explicit_patterns
        and patterns == ["social_post.publish"]
        and capability_patterns == ["external.social"]
    )

    if rule_type in {"auto_approve", "allow", "exception"}:
        return _result("auto_approve_actions", "auto_approve")
    if rule_type in {"approval_required", "hitl_required", "require_approval", "review_required"}:
        return _result("hitl_required_actions", "approval_required")
    if rule_type in {"deny", "never_allow", "block", "draft_only"}:
        if not explicit_patterns and _is_platform_specific_social_deny(text, patterns):
            return None
        if inferred_social_publish_only:
            return _result("hitl_required_actions", "approval_required")
        return _result("never_allow_actions", rule_type)

    # Approval language wins over deny words such as "不得发布未经审核内容".
    if approvalish:
        return _result("hitl_required_actions", "approval_required")
    if denyish or severity in {"block", "deny"}:
        if not explicit_patterns and _is_conditional_rule(text):
            return None
        if not explicit_patterns and _is_platform_specific_social_deny(text, patterns):
            return None
        if inferred_social_publish_only:
            return _result("hitl_required_actions", "approval_required")
        return _result("never_allow_actions", "deny")
    return None


def _enrich_operating_rules(rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        return []
    enriched: list[dict[str, Any]] = []
    for idx, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            continue
        rule = dict(raw_rule)
        rule.setdefault("rule_key", f"rule_{idx + 1}")
        enforcement = _infer_rule_enforcement(rule)
        if enforcement:
            rule.setdefault("rule_type", enforcement["rule_type"])
            if enforcement["patterns"]:
                rule.setdefault("action_patterns", enforcement["patterns"])
            if enforcement["capability_patterns"]:
                rule.setdefault("capability_patterns", enforcement["capability_patterns"])
            rule["runtime_enforced"] = True
        enriched.append(rule)
    return enriched


def _build_governance_policy_from_rules(
    rules: list[dict[str, Any]],
    raw_policy: Any,
):
    from packages.core.governance import WorkspacePolicy

    raw = raw_policy if isinstance(raw_policy, dict) else {}
    policy = WorkspacePolicy()
    policy.never_allow_actions = _unique_strings(raw.get("never_allow_actions"))
    policy.hitl_required_actions = _unique_strings(raw.get("hitl_required_actions"))
    policy.auto_approve_actions = _unique_strings(raw.get("auto_approve_actions"))
    policy.never_allow_capabilities = _unique_strings(raw.get("never_allow_capabilities"))
    policy.hitl_required_capabilities = _unique_strings(raw.get("hitl_required_capabilities"))
    policy.auto_approve_capabilities = _unique_strings(raw.get("auto_approve_capabilities"))
    if raw.get("max_risk_level") in {"low", "medium", "high"}:
        policy.max_risk_level = raw["max_risk_level"]
    caps = raw.get("budget_caps_per_kind")
    if isinstance(caps, dict):
        policy.budget_caps_per_kind = {
            str(k): int(v) for k, v in caps.items()
            if isinstance(k, str) and isinstance(v, int) and v >= 0
        }

    inferred_any = False
    for rule in rules:
        enforcement = _infer_rule_enforcement(rule)
        if not enforcement:
            continue
        field = enforcement["field"]
        merged = _unique_strings(getattr(policy, field) + enforcement["patterns"])
        setattr(policy, field, merged)
        capability_field = enforcement["capability_field"]
        capability_merged = _unique_strings(
            getattr(policy, capability_field) + enforcement["capability_patterns"]
        )
        setattr(policy, capability_field, capability_merged)
        inferred_any = True

    has_raw_policy = any(key in raw for key in _GOVERNANCE_POLICY_KEYS)
    if not has_raw_policy and not inferred_any:
        return None
    return policy


def _build_knowledge_policy(
    fields: dict[str, Any],
    materialized_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = fields.get("knowledge_policy") or fields.get("knowledge") or {}
    if not isinstance(raw, dict):
        raw = {}

    materialized_ids = [g["group_id"] for g in materialized_groups if g.get("group_id")]
    default_ids = _unique_strings(raw.get("default_group_ids") or materialized_ids)
    if not default_ids:
        default_ids = _unique_strings(materialized_ids)

    group_purposes: dict[str, str] = {
        str(group["group_id"]): str(group.get("purpose") or "").strip()
        for group in materialized_groups
        if group.get("group_id") and str(group.get("purpose") or "").strip()
    }
    raw_purposes = raw.get("group_purposes")
    if isinstance(raw_purposes, dict):
        for group_id, purpose in raw_purposes.items():
            if str(group_id).strip() and str(purpose or "").strip():
                group_purposes[str(group_id)] = str(purpose).strip()

    retrieval_mode = str(raw.get("retrieval_mode") or "auto").strip().lower()
    if retrieval_mode not in {"auto", "manual", "strict"}:
        retrieval_mode = "auto"

    return {
        "auto_search": bool(raw.get("auto_search", True)),
        "retrieval_mode": retrieval_mode,
        "citation_required": bool(raw.get("citation_required", True)),
        "strict_mode": bool(raw.get("strict_mode", False)),
        "default_group_ids": default_ids,
        "group_purposes": group_purposes,
    }


async def finalize_setup(
    session: WorkspaceSetupSession,
    db: AsyncSession,
    *,
    progress: Optional[Any] = None,
) -> str:
    """Create the workspace from the completed session.

    - Creates Workspace record with full operating_model
    - Creates AgentSubscription records for each mapping
    - Seeds default automation skills
    - Records creation activity

    ``progress`` is an optional callback invoked at each major
    checkpoint with ``(step_key: str, payload: dict)`` so a streaming
    consumer can render real-time finalize progress. Errors raised by
    the callback are swallowed -- progress reporting must never block
    the actual provisioning.

    Returns workspace_id.
    """
    from packages.core.services.default_workspace_skills import seed_workspace_skills

    def _report(step: str, **payload: Any) -> None:
        if progress is None:
            return
        try:
            progress(step, payload)
        except Exception:
            pass

    fields = session.fields

    operating_rules = _enrich_operating_rules(fields.get("rules", []))
    raw_budget_policy = fields.get("budget_policy") if isinstance(fields.get("budget_policy"), dict) else {}
    monthly_budget_credits = _coerce_positive_int(raw_budget_policy.get("monthly_budget_credits"))
    auto_pause_setting = _optional_bool(raw_budget_policy.get("auto_pause_on_budget"))
    auto_pause_on_budget = True if auto_pause_setting is None else auto_pause_setting
    monthly_budget_usd: Decimal | None = None
    if monthly_budget_credits:
        from packages.core.services.credit_service import credits_to_usd
        monthly_budget_usd = Decimal(str(credits_to_usd(monthly_budget_credits)))
    budget_policy = {
        "monthly_budget_credits": monthly_budget_credits,
        "auto_pause_on_budget": auto_pause_on_budget,
        "notes": raw_budget_policy.get("notes", "") or "",
    }

    # Build operating model
    operating_model: Dict[str, Any] = {
        "services": fields.get("services", []),
        "goals": fields.get("goals", []),
        "rules": operating_rules,
        "automations": fields.get("automations", []),
        "evaluation": fields.get("evaluation", {}),
        "budget_policy": budget_policy,
        "channel_config": fields.get("channel_config", {}),
        "agent_mappings": fields.get("agent_mappings", []),
    }

    # Create Workspace record
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=session.entity_id,
        name=fields.get("name", "Unnamed Workspace"),
        kind=fields.get("kind"),
        operating_context=fields.get("operating_context"),
        primary_work=fields.get("primary_work"),
        operating_model=operating_model,
        status="active",
        monthly_budget_usd=monthly_budget_usd,
        auto_pause_on_budget=auto_pause_on_budget,
        budget_alert_state="normal",
        settings=settings_with_default_workspace_access(
            {"created_by_user_id": session.user_id} if session.user_id else None
        ),
    )
    db.add(workspace)
    await db.flush()
    await ensure_workspace_owner_membership(
        db,
        entity_id=session.entity_id,
        workspace_id=workspace_id,
        user_id=session.user_id,
        added_by=session.user_id,
    )
    _report("workspace_created", workspace_id=workspace_id, name=workspace.name)

    # Create AgentSubscription records for each resolved mapping.
    #
    # Three branches:
    #  1. mapping.agent_id is set                     → just subscribe.
    #  2. mapping.recommended_agent_id is set         → subscribe to it.
    #  3. mapping.strategy == "create_custom"         → provision the
    #     full custom agent (Agent row + tool / skill / mcp bindings +
    #     auto-create missing skills) and subscribe.
    #
    # Path 3 is what makes "auto agent creation" actually useful — the
    # custom agent ships with everything it needs to do real work the
    # moment the user clicks Create Workspace.
    agent_mappings = fields.get("agent_mappings", [])
    created_subs: list[dict] = []  # for greeting dispatch
    # Dedupe: track custom agents by name so we don't create duplicates
    # when multiple services map to the same recommended_agent_name
    custom_agent_cache: dict[str, tuple[str, str]] = {}  # name → (agent_id, agent_name)
    total_agents = len(agent_mappings)
    custom_count = sum(1 for m in agent_mappings if m.get("strategy") == "create_custom")
    _report("provisioning_agents_started", total=total_agents, custom=custom_count)
    for idx, mapping in enumerate(agent_mappings):
        agent_id = (
            mapping.get("agent_id")
            or mapping.get("recommended_agent_id")
        )
        agent_name = mapping.get("agent_name") or mapping.get("recommended_agent_name") or "Agent"

        # Dedupe custom agents: if same name was already created, reuse it
        if not agent_id and mapping.get("strategy") == "create_custom":
            cache_key = agent_name.strip().lower()
            if cache_key in custom_agent_cache:
                agent_id, agent_name = custom_agent_cache[cache_key]

        if not agent_id and mapping.get("strategy") == "create_custom":
            try:
                agent_id, agent_name = await _provision_custom_agent_for_mapping(
                    db,
                    entity_id=session.entity_id,
                    workspace_id=workspace_id,
                    workspace_name=fields.get("name", "Workspace"),
                    operating_context=fields.get("operating_context") or "",
                    primary_work=fields.get("primary_work") or "",
                    mapping=mapping,
                )
                # Cache so duplicate names reuse the same agent
                custom_agent_cache[agent_name.strip().lower()] = (agent_id, agent_name)
            except Exception:
                logger.exception(
                    "Failed to provision custom agent for service %s",
                    mapping.get("service_key"),
                )
                continue

        if not agent_id:
            continue

        # AgentSubscription carries the workspace-specific framing so
        # the Agent itself stays a general worker. The custom_prompt
        # layer gives this subscription's runtime knowledge of the
        # current workspace + service without polluting the Agent's
        # base prompt (which other workspaces also use).
        service_key = mapping.get("service_key") or ""
        ws_framing_parts = [
            f"You are subscribed to the \"{fields.get('name', 'this')}\" workspace as the '{service_key}' service handler."
            if service_key else f"You are subscribed to the \"{fields.get('name', 'this')}\" workspace.",
        ]
        if fields.get("primary_work"):
            ws_framing_parts.append(f"Workspace primary work: {fields['primary_work']}")
        if fields.get("operating_context"):
            ws_framing_parts.append(f"Operating context: {fields['operating_context']}")
        rationale = (mapping.get("rationale") or "").strip()
        if rationale:
            ws_framing_parts.append(f"Why you were assigned: {rationale}")

        sub_id = generate_ulid()
        sub = AgentSubscription(
            id=sub_id,
            entity_id=session.entity_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            service_key=service_key or None,
            custom_prompt="\n\n".join(p for p in ws_framing_parts if p) or None,
            status="active",
        )
        db.add(sub)
        created_subs.append({
            "subscription_id": sub_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "service_key": mapping.get("service_key", "general"),
            "system_prompt": mapping.get("system_prompt", ""),
        })
        _report(
            "agent_provisioned",
            index=idx + 1,
            total=total_agents,
            agent_name=agent_name,
            service_key=mapping.get("service_key"),
            strategy=mapping.get("strategy") or "match",
        )

    await db.flush()

    # Ensure entity has an InternalWorker, then bind all subscriptions to it.
    # Without this, the Dispatcher can't assign plan steps to any executor.
    if created_subs:
        try:
            from packages.core.models.worker import Worker, SubscriptionWorker
            worker = (await db.execute(
                select(Worker).where(
                    Worker.entity_id == session.entity_id,
                    Worker.kind == "internal",
                    Worker.status == "active",
                ).limit(1)
            )).scalar_one_or_none()

            # Auto-create internal worker if none exists for this entity
            if not worker:
                worker = Worker(
                    id=generate_ulid(),
                    entity_id=session.entity_id,
                    kind="internal",
                    display_name="Manor Internal Worker",
                    status="active",
                    capabilities={
                        "supported_kinds": ["llm", "action", "subagent", "sleep", "human"],
                        "max_risk_level": "high",
                        "supported_providers": None,
                        "max_concurrent_leases": 4,
                        "protocol_version": 1,
                        "deployment": "local",
                        "uses_manor_credentials": True,
                    },
                )
                db.add(worker)
                await db.flush()
                logger.info("Auto-created internal worker %s for entity %s", worker.id, session.entity_id)

            for sub_data in created_subs:
                db.add(SubscriptionWorker(
                    worker_id=worker.id,
                    subscription_id=sub_data["subscription_id"],
                ))
            await db.flush()
        except Exception:
            logger.warning("Failed to bind subscriptions to worker for workspace %s", workspace_id, exc_info=True)

    _report("agents_done", count=len(created_subs))

    _report("provisioning_team_and_knowledge")

    # ── Staff assignments ──
    # The architect's `ws_assign_staff` calls dropped rows into
    # draft.fields.staff_assignments; turn each into a real
    # WorkspaceStaff binding so the operator's team is wired in from
    # day one.
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.models.staff import Staff
    valid_staff_ids = set((await db.execute(
        select(Staff.id).where(
            Staff.entity_id == session.entity_id,
            Staff.deleted_at.is_(None),
        )
    )).scalars().all())
    for assignment in (fields.get("staff_assignments") or []):
        sid = (assignment or {}).get("staff_id")
        if not sid or sid not in valid_staff_ids:
            continue
        db.add(WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=workspace_id,
            staff_id=sid,
            role=(assignment.get("role") or "member"),
        ))

    # ── Knowledge groups ──
    # Create groups unless the user explicitly opted out (approved=false).
    # This matches the draft UI, where newly suggested knowledge starts selected.
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    starter_doc_requests: list[dict] = []
    materialized_knowledge: list[dict] = []
    for ka in (fields.get("knowledge_attachments") or []):
        if not isinstance(ka, dict):
            continue

        # Skip only when the user explicitly opted out.
        if ka.get("approved", True) is False:
            continue  # User didn't approve -- skip, keep as suggestion

        mode = ka.get("mode", "create_new")
        generate_starter_doc = _should_generate_starter_doc(ka, mode)
        if mode == "link_existing" and ka.get("existing_group_id"):
            existing = (await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.id == ka["existing_group_id"],
                    DocumentGroup.entity_id == session.entity_id,
                )
            )).scalar_one_or_none()
            if not existing or (existing.settings or {}).get("workspace_file_bucket"):
                continue
            if existing.workspace_id == workspace_id:
                purpose = ka.get("purpose") or (existing.settings or {}).get("purpose", "")
                if generate_starter_doc:
                    existing.settings = with_starter_document_settings(
                        existing.settings,
                        group_name=existing.name,
                        status="scheduled",
                    )
                materialized_knowledge.append({
                    "group_id": existing.id,
                    "name": existing.name,
                    "purpose": purpose,
                })
                if generate_starter_doc:
                    starter_doc_requests.append({
                        "group_id": existing.id,
                        "name": existing.name,
                        "purpose": purpose,
                        "task_key": (existing.settings or {}).get("starter_document", {}).get("task_key"),
                    })
            else:
                # Do not reassign global/entity knowledge groups into this workspace.
                # A workspace binding is a separate collection that references the
                # same documents, so the source group remains usable elsewhere.
                group_id = generate_ulid()
                settings = dict(existing.settings or {})
                settings.pop("workspace_file_bucket", None)
                settings.pop("default_collection", None)
                settings["kind"] = "knowledge_net"
                settings["purpose"] = ka.get("purpose") or settings.get("purpose", "")
                settings["linked_service_keys"] = list(ka.get("linked_service_keys") or settings.get("linked_service_keys") or [])
                settings["auto_created"] = True
                settings["user_manageable"] = True
                settings["generate_starter_doc"] = generate_starter_doc
                settings["source_existing_group_id"] = existing.id
                name = (ka.get("name") or existing.name or "Workspace Knowledge").strip()
                if generate_starter_doc:
                    settings = with_starter_document_settings(
                        settings,
                        group_name=name,
                        status="scheduled",
                    )
                db.add(DocumentGroup(
                    id=group_id,
                    entity_id=session.entity_id,
                    workspace_id=workspace_id,
                    name=name,
                    settings=settings,
                ))
                member_rows = (await db.execute(
                    select(DocumentGroupMember.document_id)
                    .join(Document, Document.id == DocumentGroupMember.document_id)
                    .where(
                        DocumentGroupMember.group_id == existing.id,
                        Document.entity_id == session.entity_id,
                        Document.is_trashed == False,  # noqa: E712
                    )
                )).scalars().all()
                for document_id in member_rows:
                    db.add(DocumentGroupMember(document_id=document_id, group_id=group_id))
                materialized_knowledge.append({
                    "group_id": group_id,
                    "name": name,
                    "purpose": settings.get("purpose", ""),
                })
                if generate_starter_doc:
                    starter_doc_requests.append({
                        "group_id": group_id,
                        "name": name,
                        "purpose": settings.get("purpose", ""),
                        "task_key": settings.get("starter_document", {}).get("task_key"),
                    })
            continue

        kname = (ka.get("name") or "").strip()
        if not kname:
            continue
        group_id = generate_ulid()

        template_group_id: str | None = None
        template_settings: dict[str, Any] = {}
        if mode == "clone_template" and ka.get("template_group_id"):
            template = (await db.execute(
                select(DocumentGroup).where(
                    DocumentGroup.id == ka["template_group_id"],
                    DocumentGroup.entity_id == session.entity_id,
                )
            )).scalar_one_or_none()
            if template and not (template.settings or {}).get("workspace_file_bucket"):
                template_group_id = template.id
                template_settings = dict(template.settings or {})
                template_settings.pop("workspace_file_bucket", None)
                template_settings.pop("default_collection", None)

        group_settings = {
            **template_settings,
            "kind": "knowledge_net",
            "purpose": ka.get("purpose", ""),
            "linked_service_keys": list(ka.get("linked_service_keys") or []),
            "auto_created": True,
            "user_manageable": True,
            "generate_starter_doc": generate_starter_doc,
            "source_template_group_id": template_group_id,
        }
        if generate_starter_doc:
            group_settings = with_starter_document_settings(
                group_settings,
                group_name=kname,
                status="scheduled",
            )

        db.add(DocumentGroup(
            id=group_id,
            entity_id=session.entity_id,
            workspace_id=workspace_id,
            name=kname,
            settings=group_settings,
        ))

        if template_group_id:
            member_rows = (await db.execute(
                select(DocumentGroupMember.document_id)
                .join(Document, Document.id == DocumentGroupMember.document_id)
                .where(
                    DocumentGroupMember.group_id == template_group_id,
                    Document.entity_id == session.entity_id,
                )
            )).scalars().all()
            for document_id in member_rows:
                db.add(DocumentGroupMember(document_id=document_id, group_id=group_id))

        if generate_starter_doc:
            starter_doc_requests.append({
                "group_id": group_id,
                "name": kname,
                "purpose": ka.get("purpose", ""),
                "task_key": group_settings.get("starter_document", {}).get("task_key"),
            })
        materialized_knowledge.append({
            "group_id": group_id,
            "name": kname,
            "purpose": ka.get("purpose", ""),
        })

    operating_model["knowledge"] = _build_knowledge_policy(fields, materialized_knowledge)
    workspace.operating_model = dict(operating_model)

    governance_policy = _build_governance_policy_from_rules(
        operating_rules,
        fields.get("governance_policy") or fields.get("governance"),
    )
    if governance_policy is not None:
        try:
            from packages.core.governance import update_policy

            await update_policy(
                db,
                entity_id=session.entity_id,
                workspace_id=workspace_id,
                policy=governance_policy,
                changed_by=session.user_id,
                change_summary="Workspace draft guardrails",
            )
            _report(
                "governance_policy_created",
                hitl=len(governance_policy.hitl_required_actions),
                blocked=len(governance_policy.never_allow_actions),
            )
        except Exception:
            logger.exception("Failed to create governance policy for workspace %s", workspace_id)
            raise

    # ── Channels ──
    # Only create channels that have configured integrations.
    # Channels without credentials are flagged as suggestions.
    from packages.core.models.channel import ChannelConfig
    from packages.core.services.integration_resolution import (
        connected_integration_provider_keys,
        resolve_missing_integration_provider_key,
        supported_integration_provider_keys,
    )

    supported_providers = await supported_integration_provider_keys(db)
    configured_providers = await connected_integration_provider_keys(
        db,
        entity_id=session.entity_id,
        user_id=session.user_id,
    )

    flagged = list(fields.get("flagged_integrations") or [])
    flagged_providers = {(f or {}).get("provider") for f in flagged}
    cc = (fields.get("channel_config") or {})

    # Index subscriptions by service_key so we can bind channels → agents
    sub_by_service: Dict[str, Dict[str, str]] = {}
    for sd in created_subs:
        sk = sd.get("service_key")
        if sk:
            sub_by_service[sk] = sd

    # Only allow channel types that have a registered adapter in the platform.
    # The LLM architect may suggest arbitrary types — silently skip those.
    from packages.core.services.channels.base import registered_channel_types
    SUPPORTED_CHANNEL_TYPES = set(registered_channel_types())
    # Built-in channel types that don't need external integrations
    BUILTIN_PROVIDERS = {"internal_chat", "webchat", "in_app"}

    created_channel_configs: list[tuple[str, str, str, str]] = []
    # (channel_config_id, ch_type, linked_service_key, public_token_or_empty)

    def _mk_channel(role: str, block: Dict[str, Any]) -> None:
        ch_type = (block or {}).get("channel_type", "").strip()
        if not ch_type:
            return

        # Reject channel types that have no adapter implementation
        if ch_type not in SUPPORTED_CHANNEL_TYPES:
            logger.info(
                "Skipping unsupported channel type %r suggested by architect (supported: %s)",
                ch_type, ", ".join(sorted(SUPPORTED_CHANNEL_TYPES)),
            )
            return

        provider = (block or {}).get("provider") or ch_type

        # Skip channels without configured integrations (except built-ins)
        if canonical_provider_key(provider) not in configured_providers and provider not in BUILTIN_PROVIDERS:
            resolved_provider = resolve_missing_integration_provider_key(
                provider,
                supported_provider_keys=supported_providers,
                connected_provider_keys=configured_providers,
            )
            if resolved_provider is None:
                return
            if resolved_provider.provider not in flagged_providers:
                flag = {
                    "provider": resolved_provider.provider,
                    "purpose": block.get("purpose", "") or f"{role} channel",
                    "required": True,
                    "source": "channel_setup",
                }
                if resolved_provider.covered_provider:
                    flag["covered_provider"] = resolved_provider.covered_provider
                flagged.append(flag)
                flagged_providers.add(resolved_provider.provider)
            return  # Don't create — just flag

        cc_id = generate_ulid()
        linked_service_key = block.get("linked_service_key") or ""

        # Webchat channels get a public access token for QR/link sharing
        import secrets
        public_token = secrets.token_urlsafe(24) if ch_type == "webchat" else ""

        channel_config = {
            "role": role,
            "purpose": block.get("purpose", ""),
            "login_required": bool(block.get("login_required", False)),
            "linked_service_key": linked_service_key,
            "notes": block.get("notes", ""),
        }
        if public_token:
            channel_config["public_token"] = public_token

        db.add(ChannelConfig(
            id=cc_id,
            entity_id=session.entity_id,
            workspace_id=workspace_id,
            channel_type=ch_type,
            provider=provider,
            name=block.get("name") or f"{role}: {ch_type}",
            config=channel_config,
        ))
        created_channel_configs.append((cc_id, ch_type, linked_service_key, public_token))

        # If the architect didn't already flag this provider AND it's
        # not the always-internal chat, surface it as needs-setup so
        # the operator knows credentials are missing.
        if (
            ch_type not in {"internal_chat"}
            and provider not in flagged_providers
            and bool(block.get("login_required", False))
        ):
            flagged.append({
                "provider": provider,
                "purpose": block.get("purpose", "") or f"{role} channel",
                "required": True,
                "linked_service_keys": [linked_service_key] if linked_service_key else [],
                "source": "channel_setup",
            })
            flagged_providers.add(provider)

    # New format: channels is a flat list
    for ch in (cc.get("channels") or []):
        _mk_channel(ch.get("role", "channel"), ch)
    # Legacy format support
    if cc.get("primary_external_channel"):
        _mk_channel("primary_external", cc["primary_external_channel"])
    if cc.get("internal_channel") and cc["internal_channel"].get("channel_type") != "internal_chat":
        _mk_channel("internal", cc["internal_channel"])
    for sec in (cc.get("secondary_external_channels") or []):
        _mk_channel("secondary_external", sec)
    if flagged:
        # Sync any newly-discovered missing creds back to fields so the
        # workspace.settings persistence step below picks them up.
        fields["flagged_integrations"] = flagged

    await db.flush()

    # ── Create Channel binding rows ──
    # Channel rows are the gateway's dispatch target: they bind a
    # ChannelConfig to an AgentSubscription so inbound messages route
    # to the right agent. Without these, the channel_gateway has no
    # binding and drops inbound messages.
    from packages.core.models.document import Channel
    for cc_id, ch_type, linked_sk, public_token in created_channel_configs:
        # Find the subscription that handles this channel's linked service
        matched_sub = sub_by_service.get(linked_sk) if linked_sk else None
        # Fallback: use the first subscription (primary agent)
        if not matched_sub and created_subs:
            matched_sub = created_subs[0]

        channel_config_data: Dict[str, Any] = {
            "channel_config_id": cc_id,
        }
        if public_token:
            channel_config_data["public_token"] = public_token

        db.add(Channel(
            id=generate_ulid(),
            entity_id=session.entity_id,
            workspace_id=workspace_id,
            type=ch_type,
            name=ch_type,
            agent_id=matched_sub["agent_id"] if matched_sub else None,
            agent_subscription_id=matched_sub["subscription_id"] if matched_sub else None,
            config=channel_config_data,
            status="active",
        ))

    await db.flush()

    automations_created = 0
    try:
        automations_created = await _install_workspace_draft_automations(
            db,
            entity_id=session.entity_id,
            workspace_id=workspace_id,
            automations=list(fields.get("automations") or []),
            sub_by_service=sub_by_service,
            created_subs=created_subs,
            user_id=session.user_id,
        )
    except Exception:
        logger.exception("Failed to install workspace draft automations for %s", workspace_id)
    if automations_created:
        _report("automations_scheduled", count=automations_created)

    _report(
        "team_and_knowledge_done",
        staff=len(fields.get("staff_assignments") or []),
        knowledge=len(fields.get("knowledge_attachments") or []),
        channels=(
            (1 if (fields.get("channel_config") or {}).get("primary_external_channel") else 0)
            + (1 if (fields.get("channel_config") or {}).get("internal_channel") else 0)
            + len((fields.get("channel_config") or {}).get("secondary_external_channels") or [])
        ),
    )

    # Seed default automation skills
    try:
        await seed_workspace_skills(db, session.entity_id, workspace_id)
    except Exception:
        logger.exception("Failed to seed default workspace skills for %s", workspace_id)
    _report("default_skills_seeded")

    # Seed the workspace memory directory layout + initial guidance
    # entries so Strategist and Planner have context from day 1.
    try:
        from packages.core.memory.repo import ensure_workspace_memory_dirs
        from packages.core.memory.seed import seed_workspace_memory
        from packages.core.memory.canonical import ensure_workspace_memory_docs
        from packages.core.services.entity_fs import provision_entity_filesystem

        # Run memory seeding inside a savepoint so a failure doesn't
        # poison the main transaction (which already holds the workspace
        # + agent subscription rows).
        async with db.begin_nested():
            provision_entity_filesystem(session.entity_id)
            ensure_workspace_memory_dirs(session.entity_id, workspace_id)
            ensure_workspace_memory_docs(
                session.entity_id,
                workspace_id,
                workspace_name=fields.get("name", "Unnamed Workspace"),
                workspace_kind=fields.get("kind"),
            )
            await seed_workspace_memory(
                db,
                entity_id=session.entity_id,
                workspace_id=workspace_id,
                workspace_name=fields.get("name", "Unnamed Workspace"),
                workspace_kind=fields.get("kind"),
                services=fields.get("services", []),
            )
    except Exception:
        logger.exception(
            "Failed to seed workspace memory layout for %s", workspace_id,
        )

    _report("memory_seeded")

    # ── Materialize Goal DB rows from operating_model.goals[] ──────────
    # Without real Goal rows, the Strategist has nothing to reason about.
    goals_data = fields.get("goals") or operating_model.get("goals", [])
    goals_created = 0
    for g in goals_data:
        title = g.get("title") or g.get("goal_key", "Untitled Goal")
        metric_key = (
            g.get("metric_key")
            or g.get("goal_key", title).lower().replace(" ", "_").replace("-", "_")
        )
        target_val = _coerce_goal_number(
            g.get("target_value") if g.get("target_value") is not None else g.get("target")
        )
        baseline_val = None
        if g.get("baseline_value") is not None:
            baseline_val = _coerce_goal_number(g["baseline_value"])
        from packages.core.goals.scheduling import (
            default_workspace_measurement_source,
            is_workspace_internal_measurement_source,
        )

        measurement_source = default_workspace_measurement_source(
            g.get("measurement_source"),
            workspace_id=workspace_id,
        )
        measurement_cadence = g.get("measurement_cadence") or g.get("cadence") or "daily"
        if measurement_source and is_workspace_internal_measurement_source(measurement_source):
            baseline_val = baseline_val if baseline_val is not None else Decimal("0")
            measurement_cadence = measurement_cadence or "daily"

        goal = Goal(
            id=generate_ulid(),
            entity_id=session.entity_id,
            workspace_id=workspace_id,
            title=title,
            description=g.get("description"),
            metric_key=metric_key,
            target_value=target_val,
            baseline_value=baseline_val,
            measurement_source=measurement_source,
            measurement_cadence=measurement_cadence,
            priority=int(g.get("priority", 3)),
            status="active",
            pace_status="unknown",
        )
        db.add(goal)
        await db.flush()
        goals_created += 1

        # Schedule periodic measurement only for automatic sources. Manual
        # goals are updated by explicit user/tool measurements.
        try:
            from packages.core.goals.scheduling import (
                install_measurement_schedule,
                should_install_measurement_schedule,
            )
            if should_install_measurement_schedule(goal):
                await install_measurement_schedule(db, goal)
        except Exception:
            logger.exception(
                "Failed to install measurement schedule for goal %s",
                goal.id,
            )

    # ── Persist any flagged integrations on the workspace ──
    # The architect surfaces missing integrations (e.g. user hasn't
    # connected Twitter yet) so the UI can render a "needs setup" panel.
    flagged = await _resolve_workspace_flagged_integrations(
        db,
        entity_id=session.entity_id,
        user_id=session.user_id,
        flagged=list(fields.get("flagged_integrations") or []),
    )
    if flagged:
        ws_settings = dict(workspace.settings or {})
        ws_settings["flagged_integrations"] = flagged
        workspace.settings = ws_settings

    # Seed generated workspace state/file caches after goals, agents, knowledge,
    # and integration hints exist so the first Strategist run has a useful map.
    try:
        from packages.core.services.workspace_state_files import refresh_workspace_state_files

        await refresh_workspace_state_files(db, workspace)
        _report("state_files_seeded")
    except Exception:
        logger.exception("Failed to seed workspace state/file caches for %s", workspace_id)

    # ── Enable heartbeat — AI decides cadence, default daily ──────────
    # The heartbeat wakes the Strategist to propose tasks autonomously.
    heartbeat_cadence = (
        fields.get("heartbeat_cadence")
        or operating_model.get("heartbeat_cadence")
        or "0 9 * * *"  # daily at 9am
    )
    workspace.heartbeat_enabled = True
    workspace.heartbeat_cadence = heartbeat_cadence

    # Install built-in runtime schedules. Without this the cadence is
    # set on the Workspace row but the recurring review/evolution loops
    # never actually fire.
    try:
        from packages.core.services.workspace_runtime import install_workspace_runtime_schedules
        await install_workspace_runtime_schedules(db, workspace, cadence=heartbeat_cadence)
    except Exception:
        logger.exception(
            "Failed to install workspace runtime schedules for %s",
            workspace_id,
        )

    _report("runtime_scheduled", heartbeat_cadence=heartbeat_cadence)

    # NOTE: previously this seeded a "Review and configure {workspace}"
    # starter task to nudge the user. Removed — the wizard already walks
    # goals / agent mappings / channels, so nothing's left to review.
    # An empty Kanban on a fresh workspace is the correct signal.

    await db.flush()

    # Record creation activity
    activity = WorkspaceActivity(
        id=generate_ulid(),
        workspace_id=workspace_id,
        entity_id=session.entity_id,
        event_type="workspace_created",
        summary=f"Workspace '{fields.get('name', '')}' created via setup wizard",
        details={
            "kind": fields.get("kind"),
            "services_count": len(fields.get("services", [])),
            "agent_mappings_count": len(agent_mappings),
            "goals_created": goals_created,
            "heartbeat_cadence": heartbeat_cadence,
        },
    )
    db.add(activity)
    await db.flush()

    # ── Dispatch agent greetings (async, don't block finalization) ─────
    logger.info(
        "finalize_setup: workspace=%s agent_mappings=%d created_subs=%d",
        workspace_id, len(agent_mappings), len(created_subs),
    )
    if created_subs:
        try:
            from packages.core.tasks.ai_tasks import send_agent_greetings
            send_agent_greetings.delay(
                session.entity_id,
                workspace_id,
                fields.get("name", "Unnamed Workspace"),
                fields.get("kind", ""),
                created_subs,
            )
            logger.info("Dispatched agent greetings for workspace %s (%d agents)", workspace_id, len(created_subs))
        except Exception:
            logger.warning("Failed to dispatch agent greetings for %s", workspace_id, exc_info=True)
    else:
        logger.warning("No agents subscribed to workspace %s — skipping greetings", workspace_id)

    # ── Dispatch explicit starter content generation for approved knowledge bases ──
    if starter_doc_requests:
        try:
            from packages.core.tasks.ai_tasks import generate_knowledge_content
            for kg in starter_doc_requests:
                generate_knowledge_content.delay(
                    session.entity_id, workspace_id,
                    kg["group_id"], kg["name"], kg["purpose"],
                    fields.get("name", "Workspace"),
                    fields.get("kind", ""),
                    fields.get("primary_work", ""),
                    kg.get("task_key"),
                )
        except Exception:
            logger.warning("Failed to dispatch knowledge content generation for %s", workspace_id, exc_info=True)

    # ── Dispatch first Strategist review (delayed so greetings land first) ──
    strategist_eta_s = 20
    try:
        from packages.core.tasks.ai_tasks import run_strategist_review
        run_strategist_review.apply_async(
            args=[workspace_id, "workspace_created"],
            countdown=strategist_eta_s,
        )
    except Exception:
        logger.warning("Failed to dispatch first strategist review for %s", workspace_id)

    _report("strategist_dispatched", eta_seconds=strategist_eta_s)
    _report("complete", workspace_id=workspace_id, strategist_eta_seconds=strategist_eta_s)
    return workspace_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(
    r"<workspace_setup>\s*(.*?)\s*</workspace_setup>",
    re.DOTALL,
)


def _extract_status_block(text: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON status block from LLM response."""
    match = _STATUS_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Failed to parse workspace_setup status block")
        return None


def _strip_status_block(text: str) -> str:
    """Remove the status block from the visible response."""
    return _STATUS_RE.sub("", text)


async def _build_setup_context(
    entity_id: str, db: AsyncSession
) -> Dict[str, Any]:
    """Build hidden context with available agents, channels, and integrations."""
    entity_agents = await _fetch_entity_agents(entity_id, db)
    marketplace_agents = await _fetch_marketplace_agents(db)

    # ── Integration inventory (reusable service function) ──
    from packages.core.services.integration_service import get_integration_inventory
    inventory = await get_integration_inventory(db, entity_id)
    channel_providers = inventory["channels"]
    configured_integrations = inventory["integrations"]

    # Existing knowledge bases (document groups + doc counts)
    existing_knowledge = []
    try:
        from packages.core.models.document import DocumentGroup, Document, DocumentGroupMember
        from sqlalchemy import func as sqlfunc
        rows = (await db.execute(
            select(
                DocumentGroup.id, DocumentGroup.name,
                DocumentGroup.workspace_id,
                sqlfunc.count(Document.id).label("doc_count"),
            )
            .outerjoin(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
            .outerjoin(
                Document,
                (Document.id == DocumentGroupMember.document_id)
                & (Document.entity_id == DocumentGroup.entity_id),
            )
            .where(DocumentGroup.entity_id == entity_id)
            .group_by(DocumentGroup.id)
        )).all()
        existing_knowledge = [
            {"id": r.id, "name": r.name, "workspace_id": r.workspace_id, "doc_count": r.doc_count}
            for r in rows
        ]
    except Exception:
        pass

    return {
        "entity_agents": entity_agents,
        "marketplace_agents": marketplace_agents,
        "available_channels": channel_providers,
        "configured_integrations": configured_integrations,
        "existing_knowledge": existing_knowledge,
    }


async def _fetch_entity_agents(
    entity_id: str, db: AsyncSession
) -> List[Dict[str, Any]]:
    """Fetch agents owned by the entity, including their tool bindings."""
    result = await db.execute(
        select(Agent).where(
            Agent.entity_id == entity_id,
            Agent.deleted_at.is_(None),
            Agent.status == "active",
        )
    )
    agents = result.scalars().all()
    agent_ids = [a.id for a in agents]

    agent_tools: Dict[str, List[str]] = {agent_id: [] for agent_id in agent_ids}
    agent_skills: Dict[str, List[str]] = {agent_id: [] for agent_id in agent_ids}
    agent_integrations: Dict[str, List[str]] = {agent_id: [] for agent_id in agent_ids}
    if agent_ids:
        try:
            from packages.core.models.workspace import AgentToolBinding, ToolDefinition

            tool_rows = (await db.execute(
                select(AgentToolBinding.agent_id, ToolDefinition.name)
                .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
                .where(
                    AgentToolBinding.agent_id.in_(agent_ids),
                    ToolDefinition.status == "active",
                )
            )).all()
            for agent_id, tool_name in tool_rows:
                if tool_name:
                    agent_tools.setdefault(agent_id, []).append(tool_name)
        except Exception:
            pass

        try:
            from sqlalchemy import or_
            from packages.core.models.skill import AgentSkillBinding, Skill

            skill_rows = (await db.execute(
                select(AgentSkillBinding.agent_id, Skill)
                .join(Skill, Skill.id == AgentSkillBinding.skill_id)
                .where(
                    AgentSkillBinding.agent_id.in_(agent_ids),
                    AgentSkillBinding.status == "active",
                    Skill.status == "active",
                    or_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),
                )
            )).all()
            for agent_id, skill in skill_rows:
                ref = skill.slug or skill.name or skill.id
                if ref:
                    agent_skills.setdefault(agent_id, []).append(ref)
        except Exception:
            pass

    try:
        from packages.core.models.mcp import AgentMCPBinding, MCPServer
        if agent_ids:
            rows = (await db.execute(
                select(AgentMCPBinding.agent_id, MCPServer.server_key)
                .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
                .where(
                    AgentMCPBinding.agent_id.in_(agent_ids),
                    AgentMCPBinding.status == "active",
                    MCPServer.status == "active",
                )
            )).all()
            for agent_id, server_key in rows:
                if server_key:
                    agent_integrations.setdefault(agent_id, []).append(server_key)
    except Exception:
        pass

    return [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description or "",
            "category": a.category or "",
            "tags": a.tags or [],
            "tools": sorted(set(agent_tools.get(a.id, []))),
            "skills": sorted(set(agent_skills.get(a.id, []))),
            "integrations": sorted(set(agent_integrations.get(a.id, []))),
            "source": "entity",
        }
        for a in agents
    ]


async def _fetch_marketplace_agents(db: AsyncSession) -> List[Dict[str, Any]]:
    """Fetch public marketplace template agents."""
    result = await db.execute(
        select(Agent).where(
            Agent.is_template == True,  # noqa: E712
            Agent.is_public == True,  # noqa: E712
            Agent.deleted_at.is_(None),
        ).limit(50)
    )
    agents = result.scalars().all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description or "",
            "category": a.category or "",
            "tags": a.tags or [],
            "tools": [],
            "skills": [],
            "integrations": [],
            "source": _PUBLIC_TEMPLATE_AGENT_SOURCE,
        }
        for a in agents
    ]


# ---------------------------------------------------------------------------
# Custom-agent provisioning (called from finalize_setup for create_custom)
# ---------------------------------------------------------------------------

async def _provision_custom_agent_for_mapping(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str = "",
    workspace_name: str,
    operating_context: str,
    primary_work: str,
    mapping: Dict[str, Any],
) -> Tuple[str, str]:
    """Thin adapter from a workspace-architect mapping to the public
    ``agent_provisioning_service.provision_custom_agent`` function.

    The provisioning logic itself (Agent row + tool/skill/mcp bindings +
    auto-create missing skills) lives in the dedicated service module
    so any caller -- finalize, retroactive auto-map button, an operator
    CLI -- can reuse it without depending on workspace_setup_service.
    """
    from packages.core.services.agent_provisioning_service import (
        provision_custom_agent, spec_from_create_agent_draft,
    )

    create_draft = dict(mapping.get("create_agent_draft") or {})
    _ensure_workspace_custom_agent_tool_bindings(create_draft)
    spec = spec_from_create_agent_draft(
        create_draft,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        operating_context=operating_context,
        primary_work=primary_work,
        service_key=mapping.get("service_key", ""),
    )
    result = await provision_custom_agent(db, entity_id=entity_id, spec=spec)
    return result.agent_id, result.agent_name


def _ensure_workspace_custom_agent_tool_bindings(create_draft: Dict[str, Any]) -> None:
    """Normalize capability-first bindings for workspace-created custom agents.

    ``create_draft`` comes from an LLM-authored workspace setup proposal. Newer
    proposals may include ``business_capabilities``; older resolver paths only
    know raw ``tool_bindings`` or that a custom agent is needed. Runtime owns
    the merge/expansion so setup and provisioning don't drift.
    """
    has_skills = bool(
        create_draft.get("skill_bindings")
        or create_draft.get("missing_skill_specs")
    )
    create_draft["tool_bindings"] = list(
        normalize_workspace_custom_agent_tool_bindings(
            create_draft.get("tool_bindings") or [],
            business_capability_ids=create_draft.get("business_capabilities") or [],
            has_skills=has_skills,
        )
    )
