"""Runtime evidence ledger and reviewable agent-learning candidates.

The service intentionally separates *observing* from *changing behavior*:
runtime evidence is append-only, while learning candidates remain proposed
until a review flow applies them as memory, skills, rules, or profile patches.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import RUNTIME_CHAT_SOURCE
from packages.core.ai.runtime.skill_forcing import runtime_message_text_for_intent
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
from packages.core.models.workspace import Agent, Workspace

logger = logging.getLogger(__name__)

_MEMORY_CUE_RE = re.compile(
    r"(记住|以后|下次|一直|总是|必须|不要|不能|偏好|规则|remember|always|never|prefer|must|should)",
    re.IGNORECASE,
)
_AGENT_PROFILE_CUE_RE = re.compile(
    r"(你是|你的职责|你负责|作为.*agent|作为.*智能体|lease consultant|leasing consultant|"
    r"role|responsibilit(?:y|ies)|capabilit(?:y|ies)|speciali[sz]e|agent profile|"
    r"work style|工作方式|职责|定位|身份|专门负责)",
    re.IGNORECASE,
)
_LOW_SIGNAL_TOOLS = {"search_tools", "workspace_search", "rag"}
_MAX_EXCERPT_CHARS = 700
_MAX_TOOL_RESULTS = 12
_RECENT_PATTERN_WINDOW = 25
_AUTO_APPLY_CANDIDATE_TYPES = {"agent_profile_patch"}
_AGENT_FILE_MAX_APPEND_CHARS = 1200
_AGENT_FILE_SOFT_LIMIT_CHARS = 12_000
_RUNTIME_REF_ID_MAX_CHARS = 26
_RUNTIME_TRACE_ID_MAX_CHARS = 64
_SYSTEM_EVIDENCE_TYPES = {"workspace_evaluation_snapshot"}
_RUNTIME_LEARNING_BLOCK_RE = re.compile(
    r"\n?<!-- runtime-learning:[^>]+ -->\n.*?(?:\n<!-- /runtime-learning -->|(?=\n<!-- runtime-learning:|\Z))",
    re.DOTALL,
)
_RUNTIME_LEARNING_SUMMARY_RE = re.compile(
    r"\n?<!-- runtime-learning-summary -->\n.*?\n<!-- /runtime-learning-summary -->",
    re.DOTALL,
)

_DISABLED_SETTING_VALUES = {"0", "false", "off", "disabled", "no"}


@dataclass(frozen=True)
class LearningCandidateDraft:
    """Pure-data draft before DB persistence/deduping."""

    candidate_type: str
    scope: str
    title: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    confidence: float = 0.5
    dedupe_key: str | None = None


def runtime_status_from_stop_reason(stop_reason: str | None, *, has_content: bool = False) -> str:
    """Normalize agentic-loop stop reasons into evidence status."""
    reason = (stop_reason or "completed").strip().lower()
    if reason == "completed":
        return "succeeded"
    if reason in {"credit_exhausted", "blocked", "hitl_required"}:
        return "blocked"
    if has_content:
        return "partial"
    return "failed"


def _setting_explicitly_disabled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in _DISABLED_SETTING_VALUES
    return False


def _setting_explicitly_enabled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _DISABLED_SETTING_VALUES
    return False


def _runtime_learning_enabled_from_config(config: Any) -> bool:
    """Default-on learning toggle shared by workspace.settings and agent.config."""
    if not isinstance(config, dict):
        return True

    runtime_learning = config.get("runtime_learning")
    if isinstance(runtime_learning, dict) and _setting_explicitly_disabled(runtime_learning.get("enabled")):
        return False

    learning = config.get("learning")
    if isinstance(learning, dict) and _setting_explicitly_disabled(learning.get("enabled")):
        return False

    if _setting_explicitly_disabled(config.get("runtime_learning_enabled")):
        return False
    if _setting_explicitly_disabled(config.get("learning_enabled")):
        return False
    if _setting_explicitly_enabled(config.get("disable_runtime_learning")):
        return False

    return True


async def runtime_learning_enabled(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None = None,
    agent_id: str | None = None,
) -> bool:
    """Return whether runtime learning should record new evidence/candidates.

    Missing settings are treated as enabled so existing workspaces and agents
    keep learning until a user explicitly turns it off.
    """
    if workspace_id:
        workspace_settings = (
            await db.execute(
                select(Workspace.settings).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if workspace_settings is not None and not _runtime_learning_enabled_from_config(workspace_settings):
            return False

    if agent_id:
        agent_config = (
            await db.execute(
                select(Agent.config).where(
                    Agent.id == agent_id,
                    Agent.deleted_at.is_(None),
                    or_(Agent.entity_id == entity_id, Agent.entity_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if agent_config is not None and not _runtime_learning_enabled_from_config(agent_config):
            return False

    return True


def _normalize_runtime_reference_id(
    value: str | None,
    *,
    field: str,
    max_chars: int,
    overflow: dict[str, str],
    hash_long: bool = False,
) -> str | None:
    """Keep runtime evidence writes resilient to external/non-ULID ids."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    overflow[field] = text
    if hash_long:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"sha256:{digest[: max_chars - len('sha256:')]}"
    return None


def build_chat_learning_candidate_drafts(
    *,
    user_message: str,
    assistant_content: str,
    tool_calls_made: list[str] | None,
    status: str,
    stop_reason: str | None,
    repeated_tool_pattern_count: int = 0,
    repeated_failure_count: int = 0,
) -> list[LearningCandidateDraft]:
    """Derive conservative learning candidates from a chat-run summary.

    This is deterministic and side-effect-free so it can be tested without a
    database or LLM. More ambitious extraction can layer on top later.
    """
    drafts: list[LearningCandidateDraft] = []
    message = _compact_text(user_message)
    content = _compact_text(assistant_content)
    tools = _normalize_tool_names(tool_calls_made)
    tool_pattern = _tool_pattern_key(tools)

    if message and _MEMORY_CUE_RE.search(message):
        memory_type = "instruction" if _looks_like_instruction(message) else "preference"
        drafts.append(LearningCandidateDraft(
            candidate_type="memory",
            scope="user",
            title="Remember user guidance",
            summary=(
                "The user gave guidance that may need to persist for future runs. "
                "Review before applying as agent memory."
            ),
            payload={
                "memory_type": memory_type,
                "content": message,
                "source": "runtime_evidence",
                "apply_target": "agent_memory",
            },
            risk_level="low",
            confidence=0.72,
            dedupe_key=f"memory:{_stable_hash(message.lower())}",
        ))

    if message and _looks_like_agent_profile_update(message):
        drafts.append(LearningCandidateDraft(
            candidate_type="agent_profile_patch",
            scope="agent",
            title="Update agent profile from user guidance",
            summary=(
                "The user described a durable role, responsibility, or work-style update "
                "that should shape how this agent presents itself and handles future work."
            ),
            payload={
                "profile_update": message,
                "source": "runtime_evidence",
                "apply_target": "AGENT.md",
                "auto_apply_eligible": True,
            },
            risk_level="low",
            confidence=0.68,
            dedupe_key=f"agent_profile:{_stable_hash(message.lower())}",
        ))

    meaningful_tools = [t for t in tools if t not in _LOW_SIGNAL_TOOLS]
    if status == "succeeded" and meaningful_tools:
        primary = ", ".join(meaningful_tools[:4])
        drafts.append(LearningCandidateDraft(
            candidate_type="tool_experience",
            scope="agent",
            title=f"Tool pattern worked: {primary}",
            summary=(
                "This run completed successfully with a reusable tool pattern. "
                "Keep as evidence for future routing and skill extraction."
            ),
            payload={
                "tools": tools,
                "meaningful_tools": meaningful_tools,
                "tool_pattern_key": tool_pattern,
                "user_message_excerpt": message,
                "assistant_excerpt": content,
                "apply_target": "agent_tool_experience",
            },
            risk_level="low",
            confidence=0.58,
            dedupe_key=f"tool_experience:{tool_pattern}",
        ))

        if repeated_tool_pattern_count >= 3:
            drafts.append(LearningCandidateDraft(
                candidate_type="skill",
                scope="agent",
                title=f"Consider extracting a reusable skill for {primary}",
                summary=(
                    f"A similar tool pattern has succeeded {repeated_tool_pattern_count} times. "
                    "Review whether this should become a named skill with inputs, outputs, and guardrails."
                ),
                payload={
                    "suggested_tools": meaningful_tools,
                    "tool_pattern_key": tool_pattern,
                    "observed_successes": repeated_tool_pattern_count,
                    "seed_prompt": _skill_seed_prompt(message, meaningful_tools),
                    "apply_target": "skill_candidate",
                },
                risk_level="medium",
                confidence=0.64,
                dedupe_key=f"skill:{tool_pattern}",
            ))

    if status in {"failed", "blocked", "partial"}:
        reason = (stop_reason or status or "unknown").strip().lower()
        if repeated_failure_count >= 2 or reason == "credit_exhausted":
            title = "Adjust agent behavior after repeated runtime issue"
            if reason == "credit_exhausted":
                title = "Add budget-aware execution guidance"
            drafts.append(LearningCandidateDraft(
                candidate_type="profile_patch",
                scope="agent",
                title=title,
                summary=(
                    f"Recent runs hit `{reason}` more than once. Review whether the agent's "
                    "RULES.md or TOOLS.md should be updated to avoid repeating this failure."
                ),
                payload={
                    "target_files": ["RULES.md", "TOOLS.md"],
                    "stop_reason": reason,
                    "recent_failure_count": repeated_failure_count,
                    "user_message_excerpt": message,
                    "assistant_excerpt": content,
                    "apply_target": "agent_profile_patch",
                },
                risk_level="medium",
                confidence=0.62 if repeated_failure_count >= 2 else 0.55,
                dedupe_key=f"profile_patch:{reason}",
            ))

    return drafts


async def record_runtime_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    evidence_type: str,
    summary: str,
    source: str = "runtime",
    status: str = "succeeded",
    workspace_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    details: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> RuntimeEvidence | None:
    if not await runtime_learning_enabled(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
    ):
        return None

    safe_details = dict(details or {})
    overflow_refs: dict[str, str] = {}
    conversation_id = _normalize_runtime_reference_id(
        conversation_id,
        field="conversation_id",
        max_chars=_RUNTIME_REF_ID_MAX_CHARS,
        overflow=overflow_refs,
    )
    message_id = _normalize_runtime_reference_id(
        message_id,
        field="message_id",
        max_chars=_RUNTIME_REF_ID_MAX_CHARS,
        overflow=overflow_refs,
    )
    task_id = _normalize_runtime_reference_id(
        task_id,
        field="task_id",
        max_chars=_RUNTIME_REF_ID_MAX_CHARS,
        overflow=overflow_refs,
    )
    trace_id = _normalize_runtime_reference_id(
        trace_id,
        field="trace_id",
        max_chars=_RUNTIME_TRACE_ID_MAX_CHARS,
        overflow=overflow_refs,
        hash_long=True,
    )
    if overflow_refs:
        safe_details["external_reference_ids"] = {
            **(safe_details.get("external_reference_ids") or {}),
            **overflow_refs,
        }
    row = RuntimeEvidence(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task_id,
        trace_id=trace_id,
        evidence_type=evidence_type,
        source=source,
        status=status,
        summary=(summary or "Runtime event")[:2000],
        details=_json_safe(safe_details),
        metrics=_json_safe(metrics or {}),
    )
    db.add(row)
    await db.flush()
    return row


async def record_user_signal_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    evidence_type: str,
    summary: str,
    source: str,
    status: str = "succeeded",
    workspace_id: str | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    details: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    guidance_text: str | None = None,
) -> tuple[RuntimeEvidence | None, list[AgentLearningCandidate]]:
    """Record a user-driven runtime event and derive durable learning candidates.

    This is intentionally conservative: every user signal becomes append-only
    evidence, while only rule/profile-looking text produces reviewable or
    auto-queued learning candidates. It lets task comments, approval notes, and
    workspace-chat resolutions share the same evolution path as normal chat.
    """
    evidence = await record_runtime_evidence(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task_id,
        trace_id=trace_id,
        evidence_type=evidence_type,
        source=source,
        status=status,
        summary=summary,
        details=details,
        metrics=metrics,
    )
    if evidence is None:
        return None, []

    text = _compact_text(guidance_text or "")
    if not text or status not in {"succeeded", "partial"}:
        return evidence, []

    drafts = build_chat_learning_candidate_drafts(
        user_message=text,
        assistant_content="",
        tool_calls_made=[],
        status="succeeded",
        stop_reason="completed",
    )
    candidates: list[AgentLearningCandidate] = []
    new_candidate_count = 0
    auto_queued_count = 0
    for draft in drafts:
        candidate, created = await _upsert_learning_candidate(
            db,
            draft,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            evidence_id=evidence.id,
        )
        if candidate is not None:
            candidates.append(candidate)
        if candidate is not None and created and _should_auto_apply_candidate(candidate):
            await _mark_learning_candidate_apply_queued(
                candidate,
                user_id=user_id,
                approval_mode="auto",
            )
            auto_queued_count += 1
            new_candidate_count += 1
        elif created:
            new_candidate_count += 1

    if workspace_id and new_candidate_count:
        await _record_learning_activity(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            candidate_count=new_candidate_count,
            evidence_id=evidence.id,
        )
    if workspace_id and auto_queued_count:
        logger.info(
            "queued %s auto-apply learning candidate(s) from user signal workspace=%s agent=%s",
            auto_queued_count,
            workspace_id,
            agent_id,
        )
    return evidence, candidates


async def record_chat_run_evidence(
    db: AsyncSession,
    *,
    entity_id: str | None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    user_message: str = "",
    assistant_content: str = "",
    tool_calls_made: list[str] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    rounds: int | None = None,
    stop_reason: str | None = None,
    error: str | None = None,
    runtime_profile: str | None = None,
    allowed_tool_names: list[str] | None = None,
) -> tuple[RuntimeEvidence | None, list[AgentLearningCandidate]]:
    """Persist one chat-run evidence row and candidate learnings.

    Best-effort callers should catch exceptions around this function. It does
    not commit; the caller owns transaction boundaries.
    """
    if not entity_id:
        return None, []

    tools = _normalize_tool_names(tool_calls_made)
    status = runtime_status_from_stop_reason(stop_reason, has_content=bool((assistant_content or "").strip()))
    tool_pattern = _tool_pattern_key(tools)
    usage = usage or {}
    metrics = _usage_metrics(usage)
    metrics.update({
        "rounds": int(rounds or 0),
        "tool_call_count": len(tools),
    })
    if error:
        metrics["error"] = str(error)[:500]

    details = {
        "user_message_excerpt": _compact_text(user_message),
        "assistant_excerpt": _compact_text(assistant_content),
        "tool_calls_made": tools,
        "tool_pattern_key": tool_pattern,
        "tool_results": _compact_tool_results(tool_results),
        "stop_reason": stop_reason or "completed",
        "runtime_profile": runtime_profile,
        "allowed_tool_names": list((allowed_tool_names or [])[:80]),
    }
    summary = _chat_run_summary(status=status, tools=tools, stop_reason=stop_reason, error=error)
    evidence = await record_runtime_evidence(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task_id,
        trace_id=trace_id,
        evidence_type="chat_run",
        source=RUNTIME_CHAT_SOURCE,
        status=status,
        summary=summary,
        details=details,
        metrics=metrics,
    )
    if evidence is None:
        return None, []

    repeated_tool_count = 0
    repeated_failure_count = 0
    try:
        repeated_tool_count = await _recent_tool_pattern_count(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            tool_pattern_key=tool_pattern,
        )
        repeated_failure_count = await _recent_failure_count(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            stop_reason=stop_reason or status,
        )
    except Exception:
        logger.debug("runtime learning pattern counts skipped", exc_info=True)

    drafts = build_chat_learning_candidate_drafts(
        user_message=user_message,
        assistant_content=assistant_content,
        tool_calls_made=tools,
        status=status,
        stop_reason=stop_reason,
        repeated_tool_pattern_count=repeated_tool_count,
        repeated_failure_count=repeated_failure_count,
    )
    candidates: list[AgentLearningCandidate] = []
    new_candidate_count = 0
    auto_queued_count = 0
    for draft in drafts:
        candidate, created = await _upsert_learning_candidate(
            db,
            draft,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            evidence_id=evidence.id,
        )
        if candidate is not None:
            candidates.append(candidate)
        if candidate is not None and created and _should_auto_apply_candidate(candidate):
            await _mark_learning_candidate_apply_queued(
                candidate,
                user_id=user_id,
                approval_mode="auto",
            )
            auto_queued_count += 1
            new_candidate_count += 1
        elif created:
            new_candidate_count += 1

    if workspace_id and new_candidate_count:
        await _record_learning_activity(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            candidate_count=new_candidate_count,
            evidence_id=evidence.id,
        )
    if workspace_id and auto_queued_count:
        logger.info(
            "queued %s auto-apply learning candidate(s) workspace=%s agent=%s",
            auto_queued_count,
            workspace_id,
            agent_id,
        )
    return evidence, candidates


async def record_task_execution_evidence(
    db: AsyncSession,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    task_id: str | None,
    plan_id: str | None,
    task_status: str | None,
    plan_status: str | None,
    task_title: str = "",
    task_description: str = "",
    owner_service_key: str | None = None,
    delegate_service_keys: list[str] | None = None,
    agent_id: str | None = None,
    steps: list[Any] | None = None,
    actual_output: dict[str, Any] | None = None,
    cost_tracking: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    source: str = "plan_executor",
) -> tuple[RuntimeEvidence | None, list[RuntimeEvidence], list[AgentLearningCandidate]]:
    """Record task/plan execution evidence and conservative learnings.

    Chat evidence tells us how the agent reasoned. This captures what actually
    happened in workspace execution: task status, plan steps, artifacts, costs,
    and repeated tool/failure patterns.
    """
    if not entity_id:
        return None, [], []

    steps = list(steps or [])
    tool_names = _execution_step_tool_names(steps)
    tool_pattern = _tool_pattern_key(tool_names)
    step_snapshot = _compact_execution_steps(steps)
    counts = _execution_step_counts(steps)
    first_error = _first_execution_error(steps)
    duration_s = None
    if started_at and completed_at:
        duration_s = max(0.0, (completed_at - started_at).total_seconds())
    artifact_count = max(_artifact_count(actual_output), _artifact_count_from_steps(steps))
    metrics = {
        **counts,
        "tool_call_count": len(tool_names),
        "artifact_count": artifact_count,
    }
    if duration_s is not None:
        metrics["duration_s"] = duration_s
    try:
        cost_usd = (cost_tracking or {}).get("usd")
        if cost_usd is not None:
            metrics["cost_usd"] = float(cost_usd)
    except (TypeError, ValueError):
        pass

    status = _coerce_task_runtime_status(
        task_status=task_status,
        plan_status=plan_status,
        counts=counts,
        artifact_count=artifact_count,
    )
    stop_reason = _task_stop_reason(status, task_status=task_status, plan_status=plan_status, first_error=first_error)
    title = _compact_text(task_title or task_id or "Workspace task", max_chars=220)
    summary_status = {
        "succeeded": "completed",
        "failed": "failed",
        "blocked": "blocked",
        "partial": "partial",
    }.get(status, task_status or status)
    summary = f"Task {summary_status}: {title}"
    if first_error:
        summary += f" ({_compact_text(first_error.get('type') or 'error', max_chars=80)})"
    evidence = await record_runtime_evidence(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        task_id=task_id,
        trace_id=plan_id,
        evidence_type="task_run",
        source=source,
        status=status,
        summary=summary,
        details={
            "task_id": task_id,
            "plan_id": plan_id,
            "task_title": title,
            "task_description_excerpt": _compact_text(task_description, max_chars=600),
            "task_status": task_status,
            "plan_status": plan_status,
            "owner_service_key": owner_service_key,
            "delegate_service_keys": list(delegate_service_keys or [])[:12],
            "tool_calls_made": tool_names,
            "tool_pattern_key": tool_pattern,
            "steps": step_snapshot,
            "stop_reason": stop_reason,
            "first_error": first_error,
            "actual_output_excerpt": _compact_text(actual_output, max_chars=900),
        },
        metrics=metrics,
    )
    if evidence is None:
        return None, [], []

    step_evidence = await _record_step_summary_evidence(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        plan_id=plan_id,
        source=source,
        steps=steps,
    )

    candidates: list[AgentLearningCandidate] = []
    repeated_tool_count = 0
    repeated_failure_count = 0
    try:
        repeated_tool_count = await _recent_task_tool_pattern_count(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            tool_pattern_key=tool_pattern,
        )
        repeated_failure_count = await _recent_task_failure_count(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            stop_reason=stop_reason,
        )
    except Exception:
        logger.debug("task runtime learning pattern counts skipped", exc_info=True)

    drafts = build_chat_learning_candidate_drafts(
        user_message=task_title or task_description or "",
        assistant_content=_task_candidate_content(actual_output, steps),
        tool_calls_made=tool_names,
        status=status,
        stop_reason=stop_reason,
        repeated_tool_pattern_count=repeated_tool_count,
        repeated_failure_count=repeated_failure_count,
    )
    new_candidate_count = 0
    for draft in drafts:
        if draft.candidate_type == "memory":
            continue
        candidate, created = await _upsert_learning_candidate(
            db,
            draft,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            user_id=None,
            evidence_id=evidence.id,
        )
        if candidate is not None:
            candidates.append(candidate)
        if created:
            new_candidate_count += 1

    if workspace_id and new_candidate_count:
        await _record_learning_activity(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            candidate_count=new_candidate_count,
            evidence_id=evidence.id,
        )
    return evidence, step_evidence, candidates


async def list_runtime_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    evidence_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[RuntimeEvidence]:
    stmt = select(RuntimeEvidence).where(RuntimeEvidence.entity_id == entity_id)
    if workspace_id is not None:
        stmt = stmt.where(RuntimeEvidence.workspace_id == workspace_id)
    if agent_id is not None:
        stmt = stmt.where(RuntimeEvidence.agent_id == agent_id)
    if task_id is not None:
        stmt = stmt.where(RuntimeEvidence.task_id == task_id)
    if evidence_type:
        stmt = stmt.where(RuntimeEvidence.evidence_type == evidence_type)
    else:
        # Scorecard snapshots have a dedicated history surface. Keeping them
        # out of the default runtime stream prevents Strategist/Learning prompts
        # from treating bookkeeping as operational evidence.
        stmt = stmt.where(RuntimeEvidence.evidence_type.notin_(_SYSTEM_EVIDENCE_TYPES))
    if status:
        stmt = stmt.where(RuntimeEvidence.status == status)
    rows = await db.execute(stmt.order_by(desc(RuntimeEvidence.created_at)).limit(max(1, min(limit, 100))))
    return list(rows.scalars().all())


async def list_learning_candidates(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    status: str | None = "proposed",
    candidate_type: str | None = None,
    limit: int = 20,
) -> list[AgentLearningCandidate]:
    stmt = select(AgentLearningCandidate).where(AgentLearningCandidate.entity_id == entity_id)
    if workspace_id is not None:
        stmt = stmt.where(AgentLearningCandidate.workspace_id == workspace_id)
    if agent_id is not None:
        stmt = stmt.where(AgentLearningCandidate.agent_id == agent_id)
    if status:
        stmt = stmt.where(AgentLearningCandidate.status == status)
    if candidate_type:
        stmt = stmt.where(AgentLearningCandidate.candidate_type == candidate_type)
    rows = await db.execute(stmt.order_by(desc(AgentLearningCandidate.created_at)).limit(max(1, min(limit, 100))))
    return list(rows.scalars().all())


async def resolve_learning_candidate(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    status: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    note: str | None = None,
) -> AgentLearningCandidate | None:
    """Mark a candidate reviewed without silently applying behavior changes."""
    normalized = (status or "").strip().lower()
    if normalized not in {"proposed", "accepted", "rejected", "archived"}:
        raise ValueError("status must be proposed, accepted, rejected, or archived")
    stmt = select(AgentLearningCandidate).where(
        AgentLearningCandidate.id == candidate_id,
        AgentLearningCandidate.entity_id == entity_id,
    )
    if workspace_id is not None:
        stmt = stmt.where(AgentLearningCandidate.workspace_id == workspace_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        return None
    row.status = normalized
    row.resolved_by_user_id = user_id
    row.resolution = {
        **(row.resolution or {}),
        "status": normalized,
        "note": (note or "").strip(),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.flush()
    return row


async def apply_learning_candidate(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> AgentLearningCandidate | None:
    """Queue an accepted candidate for asynchronous application.

    Applying a candidate can write Markdown files, create skills, mirror
    memory, or embed content. Keep those behavior-changing writes out of the
    chat/API request path; a worker calls ``apply_queued_learning_candidate``.
    """
    return await queue_learning_candidate_apply(
        db,
        entity_id=entity_id,
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        user_id=user_id,
        approval_mode="manual",
    )


async def queue_learning_candidate_apply(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    approval_mode: str = "manual",
) -> AgentLearningCandidate | None:
    """Mark a candidate as queued for a background apply worker."""
    row = await _get_learning_candidate(
        db,
        entity_id=entity_id,
        candidate_id=candidate_id,
        workspace_id=workspace_id,
    )
    if not row:
        return None
    if row.status == "applied":
        return row
    mode = (approval_mode or "manual").strip().lower()
    if mode == "auto":
        if not _should_auto_apply_candidate(row):
            raise ValueError("Learning candidate is not eligible for auto apply")
    elif row.status != "accepted":
        raise ValueError("Learning candidate must be accepted before apply")
    await _mark_learning_candidate_apply_queued(
        row,
        user_id=user_id,
        approval_mode=mode,
    )
    await db.flush()
    return row


async def apply_queued_learning_candidate(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
) -> AgentLearningCandidate | None:
    """Worker entry point: apply a previously queued candidate."""
    row = await _get_learning_candidate(
        db,
        entity_id=entity_id,
        candidate_id=candidate_id,
        workspace_id=workspace_id,
    )
    if not row:
        return None
    if row.status == "applied":
        return row
    if row.status != "accepted":
        raise ValueError("Learning candidate must be accepted before apply")
    resolution = row.resolution or {}
    apply_status = str(resolution.get("apply_status") or "queued").lower()
    if apply_status not in {"queued", "retry", "failed"}:
        raise ValueError("Learning candidate is not queued for apply")
    approval_mode = str(resolution.get("approval_mode") or "manual").strip().lower()
    await _apply_learning_candidate_row(
        db,
        row,
        entity_id=entity_id,
        user_id=user_id or row.resolved_by_user_id,
        approval_mode=approval_mode,
    )
    return row


async def enqueue_learning_candidate_apply(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
    user_id: str | None = None,
    countdown: int = 1,
) -> AgentLearningCandidate | None:
    """Enqueue the worker; return a failed row when enqueue itself fails.

    The caller still owns transaction boundaries. Keeping this helper in the
    runtime-learning service prevents API and chat paths from diverging.
    """
    try:
        from packages.core.tasks.ai_tasks import apply_learning_candidate_async

        apply_learning_candidate_async.apply_async(
            args=[entity_id, candidate_id],
            kwargs={"workspace_id": workspace_id, "user_id": user_id},
            countdown=countdown,
        )
        return None
    except Exception as exc:
        logger.warning("Failed to enqueue learning candidate apply", exc_info=True)
        return await mark_learning_candidate_apply_failed(
            db,
            entity_id=entity_id,
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            error=f"enqueue failed: {exc}",
        )


async def mark_learning_candidate_apply_failed(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
    error: str,
) -> AgentLearningCandidate | None:
    """Persist terminal worker failure while keeping the candidate retryable."""
    row = await _get_learning_candidate(
        db,
        entity_id=entity_id,
        candidate_id=candidate_id,
        workspace_id=workspace_id,
    )
    if not row:
        return None
    if row.status != "applied":
        row.status = "accepted"
        row.resolution = {
            **(row.resolution or {}),
            "status": "accepted",
            "apply_status": "failed",
            "apply_failed_at": datetime.now(timezone.utc).isoformat(),
            "apply_error": _compact_text(error, max_chars=700),
        }
        await db.flush()
    return row


def queued_learning_candidate_ids(candidates: list[AgentLearningCandidate]) -> list[str]:
    """Return candidate ids whose apply worker should be scheduled after commit."""
    ids: list[str] = []
    for candidate in candidates or []:
        resolution = candidate.resolution or {}
        if candidate.status == "accepted" and resolution.get("apply_status") == "queued":
            ids.append(candidate.id)
    return ids


async def record_chat_runtime_learning(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    message_id: str | None,
    message: str | list[dict],
    result: Any,
    ctx: Any,
    trace: Any,
    tool_results: list[dict] | None,
) -> list[str]:
    """Best-effort bridge from chat turns into the runtime learning ledger."""
    if not db or not entity_id or not result:
        return []
    try:
        _evidence, candidates = await record_chat_run_evidence(
            db,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=getattr(ctx, "workspace_id", None),
            conversation_id=conversation_id,
            message_id=message_id,
            task_id=getattr(ctx, "task_id", None),
            trace_id=getattr(trace, "trace_id", None),
            user_message=runtime_message_text_for_intent(message),
            assistant_content=getattr(result, "content", None) or "",
            tool_calls_made=list(getattr(result, "tool_calls_made", None) or []),
            tool_results=tool_results or [],
            usage=getattr(result, "usage", None) or {},
            rounds=getattr(result, "rounds", None),
            stop_reason=getattr(result, "stop_reason", None),
            error=getattr(result, "error", None),
            runtime_profile=getattr(ctx, "runtime_profile", None),
            allowed_tool_names=sorted(getattr(ctx, "allowed_tool_names", None) or []),
        )
        return queued_learning_candidate_ids(candidates)
    except Exception:
        logger.warning("Failed to persist runtime learning evidence", exc_info=True)
        return []


async def schedule_learning_candidate_applies(
    db: AsyncSession,
    *,
    entity_id: str | None,
    candidate_ids: list[str],
    workspace_id: str | None,
    user_id: str | None,
) -> None:
    """Schedule queued learning applies after the evidence transaction commits."""
    if not entity_id or not candidate_ids:
        return

    has_enqueue_failure = False
    for candidate_id in candidate_ids:
        failed_row = await enqueue_learning_candidate_apply(
            db,
            entity_id=entity_id,
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        has_enqueue_failure = has_enqueue_failure or failed_row is not None
    if has_enqueue_failure:
        await db.commit()


async def _apply_learning_candidate_row(
    db: AsyncSession,
    row: AgentLearningCandidate,
    *,
    entity_id: str,
    user_id: str | None,
    approval_mode: str,
) -> AgentLearningCandidate:
    await _ensure_agent_access(db, entity_id=entity_id, agent_id=row.agent_id)
    applied_result = await _apply_candidate_payload(db, row, entity_id=entity_id, user_id=user_id)
    applied_at = datetime.now(timezone.utc)
    row.status = "applied"
    row.applied_at = applied_at
    row.resolved_by_user_id = user_id
    resolution = dict(row.resolution or {})
    # A retry can succeed after an earlier queue/worker failure. Keep the
    # durable success result authoritative so UI/evaluation code does not read
    # stale failure metadata as the current state.
    resolution.pop("apply_error", None)
    resolution.pop("apply_failed_at", None)
    row.resolution = {
        **resolution,
        "status": "applied",
        "apply_status": "applied",
        "applied_at": applied_at.isoformat(),
        "applied_by_user_id": user_id,
        "approval_mode": approval_mode,
        "applied_result": applied_result,
    }
    await db.flush()
    if row.workspace_id:
        await record_runtime_evidence(
            db,
            entity_id=entity_id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            user_id=user_id,
            evidence_type="learning_apply",
            source="learning",
            status="succeeded",
            summary=f"Applied learning candidate: {row.title[:180]}",
            details={
                "candidate_id": row.id,
                "candidate_type": row.candidate_type,
                "approval_mode": approval_mode,
                "applied_result": applied_result,
            },
            metrics={},
        )
        await _record_learning_apply_activity(
            db,
            entity_id=entity_id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            candidate=row,
            applied_result=applied_result,
        )
    return row


async def _get_learning_candidate(
    db: AsyncSession,
    *,
    entity_id: str,
    candidate_id: str,
    workspace_id: str | None = None,
) -> AgentLearningCandidate | None:
    stmt = select(AgentLearningCandidate).where(
        AgentLearningCandidate.id == candidate_id,
        AgentLearningCandidate.entity_id == entity_id,
    )
    if workspace_id is not None:
        stmt = stmt.where(AgentLearningCandidate.workspace_id == workspace_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _mark_learning_candidate_apply_queued(
    row: AgentLearningCandidate,
    *,
    user_id: str | None,
    approval_mode: str,
) -> None:
    queued_at = datetime.now(timezone.utc)
    row.status = "accepted"
    row.resolved_by_user_id = user_id
    row.resolution = {
        **(row.resolution or {}),
        "status": "accepted",
        "apply_status": "queued",
        "apply_queued_at": queued_at.isoformat(),
        "approval_mode": (approval_mode or "manual").strip().lower(),
        "applied_by_user_id": user_id,
    }


def format_runtime_learning_context(
    *,
    evidence: list[RuntimeEvidence] | None = None,
    candidates: list[AgentLearningCandidate] | None = None,
    max_items: int = 8,
) -> str:
    """Human/LLM-readable snapshot for workspace_search and Strategist."""
    lines: list[str] = []
    candidates = list(candidates or [])[:max_items]
    evidence = list(evidence or [])[:max_items]
    if candidates:
        lines.append("## Learning Candidates")
        for c in candidates:
            lines.append(
                f"- candidate_id={c.id} [{c.status}] {c.candidate_type}/{c.scope} "
                f"risk={c.risk_level} conf={c.confidence:.2f}: {c.title}"
            )
            if c.summary:
                lines.append(f"  {c.summary[:220]}")
    if evidence:
        lines.append("## Recent Runtime Evidence")
        for ev in evidence:
            metrics = ev.metrics or {}
            tools = (ev.details or {}).get("tool_calls_made") or []
            cost = metrics.get("cost_usd")
            cost_part = f" cost=${float(cost):.4f}" if cost else ""
            lines.append(
                f"- evidence_id={ev.id} [{ev.status}] {ev.evidence_type}{cost_part} "
                f"tools={len(tools)}: {ev.summary[:220]}"
            )
    return "\n".join(lines)


async def _upsert_learning_candidate(
    db: AsyncSession,
    draft: LearningCandidateDraft,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    user_id: str | None,
    evidence_id: str,
) -> tuple[AgentLearningCandidate | None, bool]:
    dedupe_key = draft.dedupe_key
    scope, payload = _contextualize_candidate_draft(
        draft,
        workspace_id=workspace_id,
        agent_id=agent_id,
    )
    existing: AgentLearningCandidate | None = None
    if dedupe_key:
        stmt = select(AgentLearningCandidate).where(
            AgentLearningCandidate.entity_id == entity_id,
            AgentLearningCandidate.dedupe_key == dedupe_key,
            AgentLearningCandidate.status.in_(["proposed", "accepted", "applied"]),
        )
        stmt = stmt.where(
            AgentLearningCandidate.workspace_id == workspace_id
            if workspace_id is not None
            else AgentLearningCandidate.workspace_id.is_(None)
        )
        stmt = stmt.where(
            AgentLearningCandidate.agent_id == agent_id
            if agent_id is not None
            else AgentLearningCandidate.agent_id.is_(None)
        )
        stmt = stmt.where(
            AgentLearningCandidate.user_id == user_id
            if user_id is not None
            else AgentLearningCandidate.user_id.is_(None)
        )
        existing = (await db.execute(
            stmt.order_by(desc(AgentLearningCandidate.created_at)).limit(1)
        )).scalar_one_or_none()
    if existing:
        evidence_ids = list(existing.evidence_ids or [])
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
            existing.evidence_ids = evidence_ids[-20:]
        existing.confidence = max(float(existing.confidence or 0), float(draft.confidence or 0))
        existing.payload = _merge_candidate_payload(existing.payload or {}, payload, evidence_count=len(evidence_ids))
        await db.flush()
        return existing, False

    row = AgentLearningCandidate(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        user_id=user_id,
        candidate_type=draft.candidate_type,
        scope=scope,
        title=draft.title[:255],
        summary=draft.summary,
        payload=_json_safe(payload),
        evidence_ids=[evidence_id],
        dedupe_key=dedupe_key,
        risk_level=draft.risk_level,
        status="proposed",
        confidence=max(0.0, min(float(draft.confidence), 1.0)),
        created_by="runtime",
    )
    db.add(row)
    await db.flush()
    return row, True


def _contextualize_candidate_draft(
    draft: LearningCandidateDraft,
    *,
    workspace_id: str | None,
    agent_id: str | None,
) -> tuple[str, dict[str, Any]]:
    """Attach runtime scope/target hints before DB persistence.

    The draft extractor is intentionally context-free for easy tests. This
    router is where a workspace chat becomes workspace-local memory instead of
    accidentally updating global agent behavior.
    """
    payload = dict(draft.payload or {})
    scope = draft.scope
    if not workspace_id:
        return scope, payload

    ctype = (draft.candidate_type or "").strip().lower()
    if ctype == "memory":
        scope = "workspace"
        memory_type = str(payload.get("memory_type") or "context")
        payload["apply_target"] = _workspace_memory_filename(memory_type, payload.get("content"))
        payload["target_scope"] = "workspace"
    elif ctype in {"agent_profile_patch", "tool_experience", "profile_patch", "rule"}:
        payload["target_scope"] = "workspace_agent" if agent_id or ctype == "agent_profile_patch" else "workspace"
        payload["apply_target"] = {
            "agent_profile_patch": "workspace_agent:AGENT.md",
            "tool_experience": "workspace_agent:TOOLS.md" if agent_id else "workspace:TOOLS.md",
            "profile_patch": "workspace_agent:RULES.md" if agent_id else "workspace:RULES.md",
            "rule": "workspace:RULES.md",
        }.get(ctype, payload.get("apply_target"))
    elif ctype == "skill":
        payload["target_scope"] = "agent"
    return scope, payload


def _should_target_workspace_agent_file(candidate: AgentLearningCandidate) -> bool:
    payload = candidate.payload or {}
    target_scope = str(payload.get("target_scope") or "").strip().lower()
    if target_scope == "workspace":
        return False
    if target_scope == "workspace_agent":
        return True
    ctype = (candidate.candidate_type or "").strip().lower()
    if ctype == "agent_profile_patch":
        return True
    return bool(candidate.agent_id) and ctype in {"tool_experience", "profile_patch"}


def _workspace_memory_scope(memory_type: str) -> str:
    mt = (memory_type or "").strip().lower()
    if mt == "preference":
        return "preference"
    if mt == "fact":
        return "fact"
    if mt == "learning":
        return "learning"
    return "guidance"


def _workspace_memory_filename(memory_type: str, content: str | None = None) -> str:
    mt = (memory_type or "").strip().lower()
    text = _compact_text(content or "", max_chars=600).lower()
    if mt == "instruction" or _looks_rule_like(text):
        return "RULES.md"
    if mt == "learning":
        return "LEARNINGS.md"
    return "MEMORY.md"


def _looks_rule_like(text: str) -> bool:
    if not text:
        return False
    return any(token in text for token in [
        "必须",
        "不要",
        "不能",
        "不准",
        "审核",
        "批准",
        "同意",
        "must",
        "never",
        "do not",
        "approval",
        "approve",
        "permission",
    ])


async def _record_step_summary_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    task_id: str | None,
    plan_id: str | None,
    source: str,
    steps: list[Any],
) -> list[RuntimeEvidence]:
    rows: list[RuntimeEvidence] = []
    for step in _interesting_steps_for_evidence(steps)[:12]:
        step_key = str(_step_value(step, "step_key") or _step_value(step, "id") or "step")
        kind = str(_step_value(step, "kind") or "step")
        step_status = str(_step_value(step, "step_status") or "unknown")
        tool_name = _execution_step_tool_name(step)
        error = _compact_error(_step_value(step, "error"))
        result = _step_value(step, "result")
        cost = _step_value(step, "cost") or {}
        metrics: dict[str, Any] = {}
        if isinstance(cost, dict):
            for key in ("usd", "llm_tokens_input", "llm_tokens_output", "api_calls"):
                if cost.get(key) is not None:
                    metrics[key if key != "usd" else "cost_usd"] = cost.get(key)
        row = await record_runtime_evidence(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=_step_value(step, "resolved_agent_id"),
            task_id=task_id,
            trace_id=plan_id,
            evidence_type="tool_summary",
            source=source,
            status=_step_runtime_status(step_status),
            summary=f"Step {step_status}: {step_key} ({tool_name or kind})",
            details={
                "plan_id": plan_id,
                "step_id": _step_value(step, "id"),
                "step_key": step_key,
                "kind": kind,
                "service_key": _step_value(step, "service_key"),
                "provider": _step_value(step, "provider"),
                "action_key": _step_value(step, "action_key"),
                "tool_name": tool_name,
                "step_status": step_status,
                "result_excerpt": _compact_text(result, max_chars=700),
                "error": error,
                "evidence_refs": list(_step_value(step, "evidence_refs") or [])[:12],
            },
            metrics=metrics,
        )
        if row is not None:
            rows.append(row)
    return rows


def _task_runtime_status(*, task_status: str | None, plan_status: str | None) -> str:
    status = str(task_status or plan_status or "").strip().lower()
    if status in {"completed", "done", "succeeded", "success"}:
        return "succeeded"
    if status in {"waiting_on_customer", "waiting_human", "blocked", "paused", "needs_attention"}:
        return "blocked"
    if status in {"cancelled", "canceled"}:
        return "blocked"
    if status in {"failed", "error"}:
        return "failed"
    return "partial"


def _coerce_task_runtime_status(
    *,
    task_status: str | None,
    plan_status: str | None,
    counts: dict[str, int],
    artifact_count: int,
) -> str:
    """Keep learning evidence aligned with what actually happened.

    A supervisor may mark a task completed when a failed plan still produced
    the core deliverable. That is valid, but a failed plan with no successful
    steps is not a success signal and should not train future behavior.
    """
    status = _task_runtime_status(task_status=task_status, plan_status=plan_status)
    plan = str(plan_status or "").strip().lower()
    task = str(task_status or "").strip().lower()
    failed_steps = int(counts.get("failed_steps") or 0)
    blocked_steps = int(counts.get("blocked_steps") or 0)
    done_steps = int(counts.get("done_steps") or 0)
    if (
        plan in {"completed", "done", "succeeded", "success"}
        and task in {"waiting_on_customer", "waiting_human", "blocked", "paused", "needs_attention"}
        and failed_steps == 0
        and blocked_steps == 0
        and done_steps > 0
        and artifact_count > 0
    ):
        return "succeeded"
    if status != "succeeded" or plan not in {"failed", "error"}:
        return status

    if failed_steps <= 0:
        return status
    if done_steps == 0:
        return "failed"
    if artifact_count <= 0:
        return "partial"
    return status


def _step_runtime_status(step_status: str | None) -> str:
    status = str(step_status or "").strip().lower()
    if status == "done":
        return "succeeded"
    if status in {"waiting_human", "blocked"}:
        return "blocked"
    if status in {"failed", "skipped", "cancelled", "canceled"}:
        return "failed"
    return "partial"


def _task_stop_reason(
    status: str,
    *,
    task_status: str | None,
    plan_status: str | None,
    first_error: dict[str, Any] | None,
) -> str:
    if first_error:
        return str(first_error.get("type") or "step_failed").strip().lower()
    if status == "succeeded":
        return "completed"
    return str(task_status or plan_status or status or "unknown").strip().lower()


def _execution_step_counts(steps: list[Any]) -> dict[str, int]:
    counts = {"step_count": len(steps), "done_steps": 0, "failed_steps": 0, "blocked_steps": 0}
    for step in steps:
        status = str(_step_value(step, "step_status") or "").strip().lower()
        if status == "done":
            counts["done_steps"] += 1
        elif status in {"failed", "skipped", "cancelled", "canceled"}:
            counts["failed_steps"] += 1
        elif status in {"waiting_human", "blocked"}:
            counts["blocked_steps"] += 1
    return counts


def _compact_execution_steps(steps: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in steps[:20]:
        item = {
            "id": _step_value(step, "id"),
            "key": _step_value(step, "step_key"),
            "kind": _step_value(step, "kind"),
            "status": _step_value(step, "step_status"),
            "service_key": _step_value(step, "service_key"),
            "tool_name": _execution_step_tool_name(step),
        }
        error = _compact_error(_step_value(step, "error"))
        if error:
            item["error"] = error
        result = _step_value(step, "result")
        if result:
            item["result_excerpt"] = _compact_text(result, max_chars=300)
        out.append({k: v for k, v in item.items() if v not in (None, "", [])})
    return out


def _execution_step_tool_names(steps: list[Any]) -> list[str]:
    names: list[str] = []
    for step in steps:
        name = _execution_step_tool_name(step)
        if name:
            names.append(name)
    return _normalize_tool_names(names)


def _execution_step_tool_name(step: Any) -> str:
    kind = str(_step_value(step, "kind") or "").strip()
    provider = str(_step_value(step, "provider") or "").strip()
    action_key = str(_step_value(step, "action_key") or "").strip()
    service_key = str(_step_value(step, "service_key") or "").strip()
    if action_key and provider:
        return f"{provider}.{action_key}"
    if action_key:
        return action_key
    if kind in {"llm", "code"}:
        return kind
    if kind == "subagent":
        return f"subagent:{service_key}" if service_key else "subagent"
    return ""


def _interesting_steps_for_evidence(steps: list[Any]) -> list[Any]:
    interesting: list[Any] = []
    for step in steps:
        kind = str(_step_value(step, "kind") or "").strip()
        status = str(_step_value(step, "step_status") or "").strip()
        if kind in {"action", "llm", "subagent", "code"} or status in {"failed", "waiting_human"}:
            interesting.append(step)
    return interesting


def _first_execution_error(steps: list[Any]) -> dict[str, Any] | None:
    for step in steps:
        error = _compact_error(_step_value(step, "error"))
        if error:
            return error
    return None


def _compact_error(error: Any) -> dict[str, Any] | None:
    if not error:
        return None
    if isinstance(error, dict):
        return {
            "type": _compact_text(error.get("type") or "error", max_chars=120),
            "message": _compact_text(error.get("message") or error, max_chars=500),
        }
    return {"type": "error", "message": _compact_text(error, max_chars=500)}


def _artifact_count(actual_output: dict[str, Any] | None) -> int:
    if not isinstance(actual_output, dict):
        return 0
    count = 0
    files = actual_output.get("files")
    if isinstance(files, list):
        count += len([f for f in files if f])
    for step in actual_output.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("files"), list):
            count += len([f for f in step["files"] if f])
    return count


def _artifact_count_from_steps(steps: list[Any]) -> int:
    count = 0
    for step in steps:
        result = _step_value(step, "result")
        if not isinstance(result, dict):
            continue
        for key in ("files", "artifacts", "documents", "images"):
            values = result.get(key)
            if isinstance(values, list):
                count += len([item for item in values if item])
        for key in ("fs_path", "file_url", "document_url", "image_url", "video_url", "result_url"):
            if result.get(key):
                count += 1
    return count


def _task_candidate_content(actual_output: dict[str, Any] | None, steps: list[Any]) -> str:
    if isinstance(actual_output, dict):
        bits: list[str] = []
        for step in actual_output.get("steps") or []:
            if isinstance(step, dict) and step.get("result_summary"):
                bits.append(str(step["result_summary"]))
        if bits:
            return "\n".join(bits[:5])
    for step in steps:
        result = _step_value(step, "result")
        if result:
            return _compact_text(result, max_chars=1000)
    return ""


def _step_value(step: Any, name: str) -> Any:
    if isinstance(step, dict):
        return step.get(name)
    return getattr(step, name, None)


async def _recent_tool_pattern_count(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    tool_pattern_key: str,
) -> int:
    if not tool_pattern_key:
        return 0
    rows = await _recent_chat_evidence(db, entity_id=entity_id, workspace_id=workspace_id, agent_id=agent_id)
    return sum(
        1
        for ev in rows
        if ev.status == "succeeded" and (ev.details or {}).get("tool_pattern_key") == tool_pattern_key
    )


async def _recent_failure_count(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    stop_reason: str,
) -> int:
    reason = (stop_reason or "").strip().lower()
    rows = await _recent_chat_evidence(db, entity_id=entity_id, workspace_id=workspace_id, agent_id=agent_id)
    return sum(
        1
        for ev in rows
        if ev.status in {"failed", "blocked", "partial"}
        and str((ev.details or {}).get("stop_reason") or ev.status).strip().lower() == reason
    )


async def _recent_task_tool_pattern_count(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    tool_pattern_key: str,
) -> int:
    if not tool_pattern_key:
        return 0
    rows = await _recent_task_evidence(db, entity_id=entity_id, workspace_id=workspace_id, agent_id=agent_id)
    return sum(
        1
        for ev in rows
        if ev.status == "succeeded" and (ev.details or {}).get("tool_pattern_key") == tool_pattern_key
    )


async def _recent_task_failure_count(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    stop_reason: str,
) -> int:
    reason = (stop_reason or "").strip().lower()
    rows = await _recent_task_evidence(db, entity_id=entity_id, workspace_id=workspace_id, agent_id=agent_id)
    return sum(
        1
        for ev in rows
        if ev.status in {"failed", "blocked", "partial"}
        and str((ev.details or {}).get("stop_reason") or ev.status).strip().lower() == reason
    )


async def _recent_chat_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
) -> list[RuntimeEvidence]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = select(RuntimeEvidence).where(
        RuntimeEvidence.entity_id == entity_id,
        RuntimeEvidence.evidence_type == "chat_run",
        RuntimeEvidence.created_at >= cutoff,
    )
    if workspace_id is not None:
        stmt = stmt.where(RuntimeEvidence.workspace_id == workspace_id)
    if agent_id is not None:
        stmt = stmt.where(RuntimeEvidence.agent_id == agent_id)
    rows = await db.execute(stmt.order_by(desc(RuntimeEvidence.created_at)).limit(_RECENT_PATTERN_WINDOW))
    return list(rows.scalars().all())


async def _recent_task_evidence(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    agent_id: str | None,
) -> list[RuntimeEvidence]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = select(RuntimeEvidence).where(
        RuntimeEvidence.entity_id == entity_id,
        RuntimeEvidence.evidence_type == "task_run",
        RuntimeEvidence.created_at >= cutoff,
    )
    if workspace_id is not None:
        stmt = stmt.where(RuntimeEvidence.workspace_id == workspace_id)
    if agent_id is not None:
        stmt = stmt.where(RuntimeEvidence.agent_id == agent_id)
    rows = await db.execute(stmt.order_by(desc(RuntimeEvidence.created_at)).limit(_RECENT_PATTERN_WINDOW))
    return list(rows.scalars().all())


async def _record_learning_activity(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    agent_id: str | None,
    candidate_count: int,
    evidence_id: str,
) -> None:
    try:
        from packages.core.models.workspace import WorkspaceActivity

        db.add(WorkspaceActivity(
            id=generate_ulid(),
            workspace_id=workspace_id,
            entity_id=entity_id,
            event_type="agent_learning_candidate",
            summary=f"Agent runtime created {candidate_count} learning candidate(s)",
            details={"candidate_count": candidate_count, "evidence_id": evidence_id},
            agent_id=agent_id,
        ))
        await db.flush()
    except Exception:
        logger.debug("learning activity log skipped", exc_info=True)


def _should_auto_apply_candidate(candidate: AgentLearningCandidate) -> bool:
    if (candidate.candidate_type or "").strip().lower() not in _AUTO_APPLY_CANDIDATE_TYPES:
        return False
    if (candidate.risk_level or "low").strip().lower() not in {"low"}:
        return False
    payload = candidate.payload or {}
    if payload.get("auto_apply_eligible") is False:
        return False
    content = _compact_text(payload.get("profile_update") or payload.get("content") or candidate.summary)
    return bool(content)


async def _record_learning_apply_activity(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    agent_id: str | None,
    candidate: AgentLearningCandidate,
    applied_result: dict[str, Any],
) -> None:
    try:
        from packages.core.models.workspace import WorkspaceActivity

        label = applied_result.get("kind") or candidate.candidate_type
        db.add(WorkspaceActivity(
            id=generate_ulid(),
            workspace_id=workspace_id,
            entity_id=entity_id,
            event_type="agent_learning_applied",
            summary=f"Applied agent learning candidate: {candidate.title[:140]}",
            details={
                "candidate_id": candidate.id,
                "candidate_type": candidate.candidate_type,
                "applied_kind": label,
            },
            agent_id=agent_id,
        ))
        await db.flush()
    except Exception:
        logger.debug("learning apply activity log skipped", exc_info=True)


async def _ensure_agent_access(
    db: AsyncSession,
    *,
    entity_id: str,
    agent_id: str | None,
) -> None:
    if not agent_id:
        return
    from packages.core.models.workspace import Agent

    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if not agent or (agent.entity_id is not None and agent.entity_id != entity_id):
        raise ValueError("Learning candidate references an inaccessible agent")


async def _apply_candidate_payload(
    db: AsyncSession,
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    user_id: str | None,
) -> dict[str, Any]:
    ctype = (candidate.candidate_type or "").strip().lower()
    if ctype == "memory":
        return await _apply_memory_candidate(db, candidate, entity_id=entity_id, user_id=user_id)
    if ctype == "skill":
        return await _apply_skill_candidate(db, candidate, entity_id=entity_id)
    if ctype == "tool_experience":
        return _apply_runtime_file_candidate(candidate, entity_id=entity_id, filename="TOOLS.md")
    if ctype == "agent_profile_patch":
        return _apply_runtime_file_candidate(candidate, entity_id=entity_id, filename="AGENT.md")
    if ctype in {"profile_patch", "rule"}:
        return _apply_runtime_file_candidate(candidate, entity_id=entity_id, filename="RULES.md")
    raise ValueError(f"Unsupported learning candidate type: {candidate.candidate_type}")


async def _apply_memory_candidate(
    db: AsyncSession,
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    user_id: str | None,
) -> dict[str, Any]:
    from packages.core.services.memory_service import add_memory

    payload = candidate.payload or {}
    content = _compact_text(payload.get("content") or payload.get("seed_prompt") or candidate.summary)
    if not content:
        raise ValueError("Memory candidate has no content to apply")
    memory_type = str(payload.get("memory_type") or "context").strip() or "context"
    if candidate.workspace_id:
        return await _apply_workspace_memory_candidate(
            db,
            candidate,
            entity_id=entity_id,
            content=content,
            memory_type=memory_type,
        )
    memory = await add_memory(
        db,
        entity_id,
        content,
        memory_type=memory_type,
        agent_id=candidate.agent_id,
        user_id=candidate.user_id or user_id,
        importance=7,
        source=f"learning_candidate:{candidate.id}",
        metadata={
            "learning_candidate_id": candidate.id,
            "workspace_id": candidate.workspace_id,
            "evidence_ids": list(candidate.evidence_ids or []),
            "confidence": candidate.confidence,
        },
    )
    memory.confidence = max(0.0, min(float(candidate.confidence or 0), 1.0))
    await db.flush()
    return {
        "kind": "memory",
        "memory_id": memory.id,
        "memory_type": memory.memory_type,
        "agent_id": memory.agent_id,
        "user_id": memory.user_id,
    }


async def _apply_workspace_memory_candidate(
    db: AsyncSession,
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    content: str,
    memory_type: str,
) -> dict[str, Any]:
    from packages.core.memory.service import record_memory

    workspace_id = candidate.workspace_id
    if not workspace_id:
        raise ValueError("Workspace memory candidate requires workspace_id")
    scope = _workspace_memory_scope(memory_type)
    filename = _workspace_memory_filename(memory_type, content)
    entry = await record_memory(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        scope=scope,
        title=candidate.title,
        body=content,
        tags=["runtime-learning", candidate.candidate_type],
        confidence=max(0.0, min(float(candidate.confidence or 0), 1.0)),
        source=f"learning_candidate:{candidate.id}",
        slug=f"learning-{candidate.id.lower()}",
        importance=7,
        mirror_to_canonical=False,
    )
    file_result = _apply_workspace_file_candidate(
        candidate,
        entity_id=entity_id,
        filename=filename,
    )
    return {
        "kind": "workspace_memory",
        "memory_id": entry.frontmatter.id,
        "scope": scope,
        "filename": filename,
        "file_result": file_result,
    }


async def _apply_skill_candidate(
    db: AsyncSession,
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
) -> dict[str, Any]:
    from packages.core.services.skill_service import bind_skill_to_agent, create_skill

    payload = candidate.payload or {}
    tools = _normalize_tool_names(
        payload.get("suggested_tools")
        or payload.get("meaningful_tools")
        or payload.get("tools")
        or []
    )
    prompt = _compact_text(payload.get("seed_prompt") or candidate.summary, max_chars=4000)
    if not prompt:
        raise ValueError("Skill candidate has no prompt to apply")
    skill = await create_skill(
        db,
        entity_id=entity_id,
        name=_skill_name(candidate.title),
        system_prompt=prompt,
        description=candidate.summary,
        tools=tools,
        input_schema={},
        output_format="text",
        category="runtime_learning",
        tags=["runtime-learning", candidate.candidate_type],
        config={
            "created_from_learning_candidate": candidate.id,
            "workspace_id": candidate.workspace_id,
            "evidence_ids": list(candidate.evidence_ids or []),
            "confidence": candidate.confidence,
        },
    )
    binding_id = None
    if candidate.agent_id:
        binding = await bind_skill_to_agent(
            db,
            candidate.agent_id,
            skill.id,
            entity_id,
            config={
                "binding_type": "agent_skill_binding",
                "source": "runtime_learning",
                "agent_id": candidate.agent_id,
                "workspace_id": candidate.workspace_id,
                "learning_candidate_id": candidate.id,
                "match": {
                    "type": "learned_skill_candidate",
                    "confidence": candidate.confidence,
                },
            },
        )
        binding_id = binding.id if binding else None
    return {
        "kind": "skill",
        "skill_id": skill.id,
        "skill_name": skill.name,
        "bound_agent_id": candidate.agent_id,
        "binding_id": binding_id,
    }


def _apply_agent_file_candidate(
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    filename: str,
) -> dict[str, Any]:
    from packages.core.services.agent_files import (
        effective_agent_id,
        ensure_agent_workspace,
        read_agent_file,
        write_agent_file,
    )

    agent_id = effective_agent_id(candidate.agent_id)
    ensure_agent_workspace(entity_id, agent_id)
    existing = read_agent_file(entity_id, agent_id, filename) or ""
    marker = f"runtime-learning:{candidate.id}"
    if marker in existing:
        return {"kind": "agent_file", "agent_id": agent_id, "filename": filename, "already_present": True}

    compacted_existing, compaction = _compact_agent_file_content(existing, filename=filename)
    compacted = compaction["compacted_blocks"] > 0 or len(compacted_existing) > _AGENT_FILE_SOFT_LIMIT_CHARS
    section = _agent_file_learning_section(
        candidate,
        filename=filename,
        marker=marker,
        compacted=compacted,
    )
    next_content = f"{compacted_existing.rstrip()}\n\n{section}\n" if compacted_existing.strip() else f"{section}\n"
    write_agent_file(entity_id, agent_id, filename, next_content)
    return {
        "kind": "agent_file",
        "agent_id": agent_id,
        "filename": filename,
        "compacted": compacted,
        "compacted_blocks": compaction["compacted_blocks"],
        "file_size_chars": len(next_content),
        "over_soft_limit": len(next_content) > _AGENT_FILE_SOFT_LIMIT_CHARS,
    }


def _apply_runtime_file_candidate(
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    filename: str,
) -> dict[str, Any]:
    """Route file-backed learning to the narrowest safe operating surface."""
    if candidate.workspace_id:
        if _should_target_workspace_agent_file(candidate):
            return _apply_workspace_agent_file_candidate(candidate, entity_id=entity_id, filename=filename)
        return _apply_workspace_file_candidate(candidate, entity_id=entity_id, filename=filename)
    return _apply_agent_file_candidate(candidate, entity_id=entity_id, filename=filename)


def _apply_workspace_file_candidate(
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    filename: str,
) -> dict[str, Any]:
    from packages.core.memory.canonical import append_workspace_memory_block

    workspace_id = candidate.workspace_id
    if not workspace_id:
        raise ValueError("Workspace file candidate requires workspace_id")
    marker = f"runtime-learning:{candidate.id}"
    section = _agent_file_learning_section(
        candidate,
        filename=filename,
        marker=marker,
        compacted=False,
    )
    result = append_workspace_memory_block(
        entity_id,
        workspace_id,
        filename,
        section,
        marker=marker,
    )
    result.update({
        "workspace_id": workspace_id,
        "target_scope": "workspace",
    })
    return result


def _apply_workspace_agent_file_candidate(
    candidate: AgentLearningCandidate,
    *,
    entity_id: str,
    filename: str,
) -> dict[str, Any]:
    from packages.core.memory.canonical import append_workspace_agent_memory_block
    from packages.core.services.agent_files import effective_agent_id

    workspace_id = candidate.workspace_id
    if not workspace_id:
        raise ValueError("Workspace-agent file candidate requires workspace_id")
    agent_key = effective_agent_id(candidate.agent_id)
    marker = f"runtime-learning:{candidate.id}"
    section = _agent_file_learning_section(
        candidate,
        filename=filename,
        marker=marker,
        compacted=False,
    )
    result = append_workspace_agent_memory_block(
        entity_id,
        workspace_id,
        agent_key,
        filename,
        section,
        marker=marker,
    )
    result.update({
        "workspace_id": workspace_id,
        "agent_key": agent_key,
        "target_scope": "workspace_agent",
    })
    return result


def _agent_file_learning_section(
    candidate: AgentLearningCandidate,
    *,
    filename: str,
    marker: str,
    compacted: bool = False,
) -> str:
    payload = candidate.payload or {}
    title = _compact_text(candidate.title, max_chars=180)
    summary = _compact_text(candidate.summary, max_chars=500)
    evidence = ", ".join(list(candidate.evidence_ids or [])[:5]) or "none"
    lines = [
        f"<!-- {marker} -->",
        f"## Runtime Learning: {title}",
        f"- Status: applied from learning candidate `{candidate.id}`.",
        f"- Evidence: {evidence}.",
    ]
    if summary:
        lines.append(f"- Summary: {summary}")
    if filename == "AGENT.md":
        profile_update = _compact_text(
            payload.get("profile_update") or payload.get("content") or candidate.summary,
            max_chars=600 if compacted else _AGENT_FILE_MAX_APPEND_CHARS,
        )
        if profile_update:
            lines.append(f"- Agent profile update: {profile_update}")
        lines.append("- Use this as durable identity/responsibility guidance in future workspace runs.")
        if compacted:
            lines.append("- Note: existing AGENT.md is long, so this update was compacted. See runtime evidence for full context.")
    elif filename == "TOOLS.md":
        tools = _normalize_tool_names(
            payload.get("meaningful_tools")
            or payload.get("suggested_tools")
            or payload.get("tools")
            or []
        )
        if tools:
            lines.append(f"- Tool pattern that worked: {', '.join(tools)}.")
        excerpt = _compact_text(payload.get("user_message_excerpt") or "", max_chars=260)
        if excerpt:
            lines.append(f"- Use this pattern for similar requests like: {excerpt}")
    elif filename == "MEMORY.md":
        memory = _compact_text(
            payload.get("content") or payload.get("profile_update") or candidate.summary,
            max_chars=600 if compacted else _AGENT_FILE_MAX_APPEND_CHARS,
        )
        if memory:
            lines.append(f"- Workspace memory: {memory}")
        lines.append("- Treat this as durable workspace context for future planning and execution.")
    elif filename == "LEARNINGS.md":
        learning = _compact_text(
            payload.get("content") or payload.get("assistant_excerpt") or candidate.summary,
            max_chars=600 if compacted else _AGENT_FILE_MAX_APPEND_CHARS,
        )
        if learning:
            lines.append(f"- Learning: {learning}")
        lines.append("- Use this calibration before repeating similar work.")
    elif filename == "RULES.md":
        rule = _compact_text(
            payload.get("content")
            or payload.get("profile_update")
            or payload.get("stop_reason")
            or candidate.summary,
            max_chars=600 if compacted else _AGENT_FILE_MAX_APPEND_CHARS,
        )
        if rule:
            lines.append(f"- Rule/guidance: {rule}")
        lines.append("- Apply this only at the file's scope; do not promote it globally without separate evidence.")
    else:
        reason = _compact_text(payload.get("stop_reason") or payload.get("content") or "", max_chars=240)
        if reason:
            lines.append(f"- Trigger: {reason}.")
        lines.append("- Before repeating similar work, check constraints, budget, required approvals, and missing configuration.")
    lines.append("<!-- /runtime-learning -->")
    return "\n".join(lines)


def _compact_agent_file_content(existing: str, *, filename: str) -> tuple[str, dict[str, Any]]:
    """Compact Manor-managed runtime-learning blocks without touching user prose."""
    text = existing or ""
    if len(text) <= _AGENT_FILE_SOFT_LIMIT_CHARS:
        return text, {"compacted_blocks": 0}

    blocks = list(_RUNTIME_LEARNING_BLOCK_RE.finditer(text))
    if not blocks:
        return text, {"compacted_blocks": 0}

    without_summary = _RUNTIME_LEARNING_SUMMARY_RE.sub("", text)
    blocks = list(_RUNTIME_LEARNING_BLOCK_RE.finditer(without_summary))
    managed_summaries = [_summarize_runtime_learning_block(match.group(0)) for match in blocks]
    managed_summaries = [s for s in managed_summaries if s]
    user_authored = _RUNTIME_LEARNING_BLOCK_RE.sub("", without_summary).rstrip()
    summary = _runtime_learning_summary_block(
        managed_summaries,
        compacted_count=len(blocks),
        filename=filename,
    )
    next_text = f"{user_authored}\n\n{summary}\n" if user_authored else f"{summary}\n"
    return next_text, {"compacted_blocks": len(blocks)}


def _summarize_runtime_learning_block(block: str) -> str:
    title = ""
    detail = ""
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("## Runtime Learning:"):
            title = line.replace("## Runtime Learning:", "", 1).strip()
        elif line.startswith("- Agent profile update:"):
            detail = line.replace("- Agent profile update:", "", 1).strip()
        elif line.startswith("- Tool pattern that worked:") and not detail:
            detail = line.replace("- Tool pattern that worked:", "", 1).strip()
        elif line.startswith("- Summary:") and not detail:
            detail = line.replace("- Summary:", "", 1).strip()
    text = detail or title
    if not text:
        return ""
    return _compact_text(text, max_chars=220)


def _runtime_learning_summary_block(
    summaries: list[str],
    *,
    compacted_count: int,
    filename: str,
) -> str:
    kept = summaries[-12:]
    lines = [
        "<!-- runtime-learning-summary -->",
        "## Runtime Learning Summary",
        (
            f"- Manor compacted {compacted_count} managed runtime-learning entries in `{filename}` "
            "to keep the hot agent profile bounded."
        ),
        "- Full original context remains available in runtime evidence and learning candidate records.",
    ]
    for item in kept:
        lines.append(f"- {item}")
    if compacted_count > len(kept):
        lines.append(f"- ... {compacted_count - len(kept)} older managed update(s) omitted from this hot profile.")
    lines.append("<!-- /runtime-learning-summary -->")
    return "\n".join(lines)


def _skill_name(title: str) -> str:
    base = re.sub(r"\s+", " ", (title or "Runtime learned skill").strip())
    base = re.sub(r"^Consider extracting a reusable skill for\s+", "", base, flags=re.IGNORECASE)
    return base[:90] or "Runtime learned skill"


def _chat_run_summary(
    *,
    status: str,
    tools: list[str],
    stop_reason: str | None,
    error: str | None,
) -> str:
    if tools:
        tool_text = ", ".join(tools[:6])
        if len(tools) > 6:
            tool_text += f", +{len(tools) - 6} more"
    else:
        tool_text = "no tools"
    summary = f"Chat run {status} using {tool_text}."
    reason = (stop_reason or "completed").strip()
    if reason and reason != "completed":
        summary += f" stop_reason={reason}."
    if error:
        summary += f" error={str(error)[:180]}"
    return summary


def _usage_metrics(usage: dict[str, Any]) -> dict[str, Any]:
    def _int(*keys: str) -> int:
        for key in keys:
            if usage.get(key) is not None:
                try:
                    return int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    metrics: dict[str, Any] = {
        "prompt_tokens": _int("prompt_tokens", "prompt"),
        "completion_tokens": _int("completion_tokens", "completion"),
        "total_tokens": _int("total_tokens", "total"),
    }
    if usage.get("cost_usd") is not None:
        try:
            metrics["cost_usd"] = float(usage.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    if usage.get("model"):
        metrics["model"] = str(usage.get("model"))
    if usage.get("provider"):
        metrics["provider"] = str(usage.get("provider"))
    return metrics


def _compact_tool_results(tool_results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in (tool_results or [])[:_MAX_TOOL_RESULTS]:
        if not isinstance(item, dict):
            continue
        compact.append({
            "name": str(item.get("name") or "")[:120],
            "result": _compact_text(item.get("result"), max_chars=500),
        })
    return compact


def _normalize_tool_names(tool_calls_made: list[str] | None) -> list[str]:
    out: list[str] = []
    for name in tool_calls_made or []:
        text = str(name or "").strip()
        if text:
            out.append(text[:160])
    return out


def _tool_pattern_key(tools: list[str]) -> str:
    meaningful = [t for t in tools if t not in _LOW_SIGNAL_TOOLS]
    base = ",".join(sorted(set(meaningful or tools)))
    return _stable_hash(base or "no_tools")


def _stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _compact_text(value: Any, *, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _looks_like_instruction(message: str) -> bool:
    lower = message.lower()
    return any(token in lower for token in ("必须", "不要", "不能", "always", "never", "must", "should"))


def _looks_like_agent_profile_update(message: str) -> bool:
    if not _AGENT_PROFILE_CUE_RE.search(message):
        return False
    lower = message.lower()
    rule_only_markers = ("审核", "批准", "approval", "approve", "不要", "不能", "never", "禁止")
    role_markers = (
        "你是",
        "职责",
        "负责",
        "作为",
        "lease consultant",
        "leasing consultant",
        "role",
        "responsibility",
        "agent profile",
        "工作方式",
        "定位",
        "身份",
    )
    if any(marker in lower for marker in role_markers):
        return True
    return not any(marker in lower for marker in rule_only_markers)


def _skill_seed_prompt(message: str, tools: list[str]) -> str:
    return (
        "Create a reusable skill for runs like this. Include when to use it, "
        f"required inputs, expected outputs, guardrails, and tool order. Tools observed: {', '.join(tools)}. "
        f"Example request: {_compact_text(message, max_chars=300)}"
    )


def _merge_candidate_payload(existing: dict[str, Any], incoming: dict[str, Any], *, evidence_count: int) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key not in merged or not merged.get(key):
            merged[key] = value
    merged["evidence_count"] = evidence_count
    return _json_safe(merged)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
