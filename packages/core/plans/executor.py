"""PlanExecutor — plan-level orchestration only.

Post-M3 split:

  PlanExecutor          owns the plan DAG: which steps are runnable,
                        ref interpolation, sleep / human inline kinds,
                        and overall plan status transitions.

  Dispatcher            issues leases for action / llm / subagent / code
                        steps via SELECT FOR UPDATE SKIP LOCKED + manages
                        their lifecycle (complete / fail / expire).

  InternalWorker        runs leased steps in-process via
                        ``execute_lease`` Celery task. External workers
                        do the equivalent via HTTP heartbeat (M3.6).

The cycle pattern is unchanged: Celery ``run_plan`` fires
``run_cycle``, which advances state, returns a re-enqueue hint, and
the next cycle picks up worker-completed step results from the DB.

What ``run_cycle`` does each tick:
  1. Load plan + all step rows.
  2. Terminal checks — completed / failed → finalise + emit chat.
  3. ``pending_approval`` → wait for resume.
  4. ``draft`` → ``running`` + announce.
  5. For each pending step whose deps are all done:
       * ``sleep``               handled inline; mark done; re-enqueue
                                 the cycle with countdown=seconds.
       * ``human``               handled inline; mark waiting_human;
                                 chat surfaces a HITL prompt; cycle
                                 returns wait.
       * ``action`` / ``llm`` /  resolve ${{ refs }} → write back to
         ``subagent`` / ``code`` step.params; the Dispatcher takes it
                                 from here and a worker executes it.
       * ``parallel_fanout`` /   reserved for M5+ — marked failed for now.
         ``gather``
  6. Re-enqueue self every ``CYCLE_TICK_SECONDS`` while there are still
     active leases or pending steps not yet picked.

The cycle never blocks on a worker — it just orchestrates.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType, TaskStatus, plan_terminal_log_type
from packages.core.constants.execution import (
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_CHILD_WAKE_AT_PARAM_KEY,
    COORDINATOR_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    DEFAULT_PLAN_MAX_RUNTIME_SECONDS,
    DISPATCH_SKIP_KEY,
    DISPATCH_SKIPPED_ERROR_TYPE,
    NO_EXTERNAL_ROUTE_ERROR_TYPE,
    PLAN_DEADLINE_ERROR_TYPE,
    PLAN_TERMINAL_STATUSES,
    PREFLIGHT_SKIPPED_ERROR_TYPE,
    STEP_UNDISPATCHABLE_ERROR_TYPE,
    SUPERVISOR_FEEDBACK_PARAM_KEY,
    UNDISPATCHABLE_SOFT_GRACE_SECONDS,
    UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS,
    UPSTREAM_PARTIAL_RESULT_ERROR_TYPE,
    DispatchSkipReason,
    ExecutionPlanStatus,
    ExecutionStepStatus,
    is_coordinator_child_params,
)
from packages.core.constants.approvals import HitlType
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.services.hitl_options import review_card_options
from packages.core.constants.supervisor import (
    MAX_EVIDENCE_CHARS,
    SUPERVISOR_STEP_RETRY_FLAG,
    SUPERVISOR_VERDICT_LOG_TYPE,
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.constants.task_actors import TaskActor
from packages.core.contracts.artifact_delivery import artifact_result_schema
from packages.core.contracts.envelope import (
    StepResultStatus,
    normalize_step_result_status,
    step_result_output_text,
)
from packages.core.database import async_session
from packages.core.plans.response_language import resolve_plan_response_locale
from packages.core.i18n import t
from packages.core.ai.runtime.outcome import attempt_forbids_replay
from packages.core.ai.runtime import (
    RUNTIME_PLAN_EXECUTOR_SOURCE,
    approval_args_hash,
    approval_stable_target_hash,
    runtime_emit_plan_executor_task_event,
    runtime_ensure_plan_executor_billing_context,
    runtime_ensure_task_billing_context,
    runtime_execute_plan_supervisor_completion,
    runtime_parse_plan_supervisor_decision,
    runtime_record_plan_executor_task_evidence,
)
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.media_job import MediaJobStatus
from packages.core.plans.refs import ReferenceError, resolve_refs
from packages.core.plans.step_reset import (
    lock_plan_leases_then_steps,
    reset_never_ran_downstream_steps,
    reset_step_to_retry_ready,
    revoke_leases_before_cancelling_steps,
)
from packages.core.workspace_chat import notifiers as chat_notify

if TYPE_CHECKING:
    from packages.core.plans.wakeup import PlanCycleWake

logger = logging.getLogger(__name__)


class _SupersededTaskPlan(RuntimeError):
    """A stale Plan cycle no longer owns Task execution."""


class _TaskEffectHistoryBlocked(RuntimeError):
    """A predecessor Plan recorded an effect this cycle may not replay."""


class _SupervisorReviewInProgress(RuntimeError):
    """Another executor owns the current durable supervisor generation."""


class _StaleSupervisorVerdict(RuntimeError):
    """The execution generation changed while the supervisor model was running."""

    def __init__(self, *, plan_status: str | None, reason: str) -> None:
        super().__init__(reason)
        self.plan_status = plan_status


@dataclass(frozen=True)
class _SupervisorReviewGeneration:
    """Immutable fence around one provider-side supervisor review."""

    token: str
    fingerprint: str
    plan_id: str
    task_id: str
    plan_status: str
    task_status: str
    candidate_status: str
    locale: str


CYCLE_TICK_SECONDS = 2
"""Re-enqueue delay for a cycle that found another cycle running
(``cycle_busy``). The durable Plan-cycle generation folds duplicates."""

PENDING_POLL_SECONDS = 10
"""Re-enqueue cadence while steps are PENDING. A worker's lease transition
wakes the cycle the moment it happens; this is only the safety net."""

RUNNING_POLL_SECONDS = 30
"""Re-enqueue cadence while every open step is RUNNING on a worker."""

SUPERVISOR_LLM_TIMEOUT_SECONDS = 120
"""Ceiling on the supervisor's verdict call. A hung provider must not hold
the executor cycle; past it the plan's own status decides (FALLBACK)."""

_SUPERVISOR_REVIEW_CLAIM_KEY = "_supervisor_review_claim"
_SUPERVISOR_REVIEW_CLAIM_GRACE_SECONDS = 30


def _human_step_pending_action(params: dict[str, Any] | None, *, locale: str = "en") -> dict[str, Any] | None:
    """Route inline review steps through the typed governance HITL surface.

    ``hitl_type`` describes what the person is doing; ``pending_action.kind``
    describes which execution plane resumes. A Plan review is semantically the
    same ``review`` HITL as a Workflow approval, but it resumes an execution
    step, so its wire kind is ``governance_approval`` rather than
    ``workflow_approval``.
    """

    values = params if isinstance(params, dict) else {}
    explicit = values.get("pending_action")
    if isinstance(explicit, dict) and explicit.get("kind"):
        return dict(explicit)

    review = values.get("review")
    review_artifacts = values.get("review_artifacts")
    if review_artifacts is None:
        review_artifacts = values.get("artifacts_for_review")

    if review is None and review_artifacts is None:
        return None

    if review_artifacts is None:
        review_packet = review
    elif isinstance(review, dict):
        review_packet = dict(review)
        review_packet.setdefault("artifacts", review_artifacts)
    elif review is not None:
        review_packet = {"content": review, "artifacts": review_artifacts}
    else:
        review_packet = {"artifacts": review_artifacts}

    review_title = (
        values.get("review_title") or values.get("title") or t("plan.hitl.review_required", locale=locale)
    )
    prompt = values.get("prompt") or values.get("question") or ""
    return {
        "kind": PendingActionKind.GOVERNANCE_APPROVAL.value,
        "hitl_type": HitlType.REVIEW.value,
        "title": review_title,
        "review_title": review_title,
        "prompt": prompt,
        "action": values.get("action") or "content.review",
        "tool": values.get("tool") or "plan",
        "review": review_packet,
        "payload": values.get("payload") or {
            "diff": review_packet,
            "why": prompt or t("plan.hitl.review_verdict", locale=locale, title=review_title),
        },
        # A review is a verdict on this exact material, never a standing grant.
        "options": review_card_options(values.get("options")),
    }


_PLAN_FINALIZABLE_TASK_STATUSES = {"pending", "in_progress", "waiting_on_customer"}
"""Task states where the current plan result is still allowed to close the task.

This intentionally excludes scheduled/on_hold/blocked/proposed states, which
carry stronger user or scheduling semantics and should not be auto-overwritten
by a stale plan completion event.
"""


_ARTIFACT_RESULT_KEYS: dict[str, str] = {
    "artifact_url": "artifact",
    "artifact_path": "file",
    "download_url": "file",
    "file_url": "file",
    "file_path": "file",
    "document_url": "document",
    "image_url": "image",
    "video_url": "video",
    "audio_url": "audio",
    "media_url": "media",
    "output_url": "file",
    "output_path": "file",
    "public_url": "url",
    "result_url": "result",
    "url": "url",
    "fs_path": "file",
    "mp4_fs_path": "video",
    "path": "file",
    "local_path": "file",
    "saved_to": "file",
    "document_id": "document",
}
_ARTIFACT_COLLECTION_KEYS = ("files", "artifacts", "documents", "images", "image_urls")
_ARTIFACT_CREATION_FLAGS = {
    "created", "written", "edited", "saved", "generated", "uploaded",
    "downloaded", "exported",
}
_REFERENCE_ONLY_KEYS = {
    "context", "sources", "source_count", "scope", "groups", "knowledge_nets",
    "entries", "matches", "evidence_mode", "content_evidence_available",
}
_MATERIALIZED_ARTIFACT_SCHEMA_KEYS = {
    key
    for key in _ARTIFACT_RESULT_KEYS
    if key not in {"url", "path", "result_url", "public_url"}
} | {
    "files",
    "artifacts",
    "images",
    "image_urls",
    "download_url",
}


def _is_external_artifact_url(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("http://", "https://", "blob:", "data:"))


def _looks_like_json_pointer_path(value: Any) -> bool:
    text = str(value or "").strip()
    return text.startswith("$.") or text.startswith("$[")


def _looks_like_relative_artifact_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text or _is_external_artifact_url(text) or _looks_like_json_pointer_path(text):
        return False
    lowered = text.lstrip("/").lower()
    if lowered.startswith(("api/", "documents/", "viewer/", "editor/")):
        return False
    name = lowered.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    has_file_extension = bool(suffix) and suffix != name and len(suffix) <= 12
    return "/" in lowered or has_file_extension


def _artifact_value_is_path(source_key: str, value: Any) -> bool:
    if _looks_like_json_pointer_path(value):
        return False
    return (
        "path" in source_key
        or source_key in {"fs_path", "saved_to", "local_path"}
        or (
            (source_key.endswith("_url") or source_key in {"url", "files", "documents", "artifacts"})
            and _looks_like_relative_artifact_path(value)
        )
    )

_ARTIFACT_DELIVERABLE_TYPES = {
    "artifact",
    "file",
    "image",
    "visual",
    "document",
    "pdf",
    "word_document",
    "docx",
    "presentation",
    "slides",
    "deck",
    "spreadsheet",
    "csv",
    "audio",
    "video",
}
_ARTIFACT_NEGATION_TERMS = (
    "不需要图片", "无需图片", "不要图片", "不生成图片", "不用图片",
    "不需要图纸", "无需图纸", "不要图纸", "不生成图纸",
    "不需要文件", "无需文件", "不要文件", "不生成文件", "不用文件",
    "只需要文字", "文字即可", "文字方案", "text only", "no image",
    "no images", "no file", "no files", "no attachment",
)
_TEXT_ONLY_DELIVERABLE_TERMS = (
    "plain text",
    "text only",
    "text-only",
    "memo_text",
    "internal memo",
    "文字即可",
    "只需要文字",
    "文字方案",
)
_EXPLICIT_FILE_DELIVERABLE_TERMS = (
    "pdf", "docx", "pptx", "xlsx", "csv", "mp4", "download", "attachment",
    "saved file", "file link", "file path", "export as", "save as",
    "as a file", "report file", "document file", "markdown file",
    "导出", "下载", "附件", "保存为", "保存到",
)
_MEDIA_ARTIFACT_ACTION_TERMS = (
    "generate", "create", "produce", "render", "draw", "make",
    "生成", "创建", "制作", "绘制", "渲染", "出图",
)
_MEDIA_ARTIFACT_TARGET_TERMS = (
    "image", "picture", "photo", "poster", "cover", "mockup", "diagram",
    "render", "video", "audio",
    "document", "documents", "presentation", "slides", "deck",
    "spreadsheet", "workbook", "excel",
    "图片", "图像", "照片", "海报", "封面", "样图", "效果图", "渲染图",
    "视频", "音频", "文档", "演示文稿", "幻灯片", "表格",
)
_MEDIA_TEXT_DELIVERABLE_TERMS = (
    "script", "storyboard", "outline", "plan", "idea", "ideas",
    "candidate", "candidates", "caption", "copy", "brief", "analysis",
    "report", "recommendation", "recommendations", "summary", "memo",
    "notes", "draft",
    "脚本", "分镜", "大纲", "计划", "方案", "候选", "文案", "分析", "报告",
    "总结", "摘要", "备忘录", "笔记", "草稿",
)

def _task_status_from_event(task_event: Optional[dict]) -> Optional[str]:
    """The task status _finalize actually committed, if it reported one."""
    if not isinstance(task_event, dict):
        return None
    payload = task_event.get("payload")
    if not isinstance(payload, dict):
        return None
    status = payload.get("task_status")
    return str(status) if status else None


#: Task statuses that mean the supervisor did NOT accept the run, even though
#: the plan's steps all reached a terminal "done". Announcing completion for
#: these is the UI lying about the outcome.
#: Summaries that describe the hand-off rather than the work. Showing one
#: of these to an operator says nothing about what happened.
_NON_INFORMATIVE_SUMMARIES = {"Result submitted.", "Result submitted", ""}

_SUPERVISOR_HELD_TASK_STATUSES = frozenset({
    "blocked",
    "failed",
    "waiting_human",
    "waiting_on_customer",
})

#: Free-form statuses that only appear on CUSTOM (non-envelope) schemas.
#: Canonical outcomes are decided by ``StepResultStatus`` before this set is
#: consulted — listing them here too would mean two judgements of one word.
#: "blocked" / "error" / "failure" / "incomplete" are NOT here: they normalize
#: onto the enum, so the enum branch owns them.
_STRUCTURED_BLOCKER_STATUSES = {
    "needs_attention",
    "needs_confirmation",
    "needs_human",
    "needs_input",
    "requires_confirmation",
    "requires_human",
    "requires_input",
    "waiting_human",
}
_STRUCTURED_STATUS_KEYS = {
    "completion_status",
    "outcome",
    "result_status",
    "state",
    "status",
}
_STRUCTURED_FALSE_KEYS = {
    "complete",
    "completed",
    "done",
    "ok",
    "success",
    "succeeded",
}
_STRUCTURED_PENDING_KEYS = {
    "_pending_action",
    "human_action",
    "human_input",
    "pending_action",
    "required_action",
}
_STRUCTURED_NESTED_RESULT_KEYS = {
    "hitl",
    "meta",
    "metadata",
    "output",
    "response",
    "result",
}


def _jsonish_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _schema_mentions_artifact(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    keys = {str(k).lower() for k in schema}
    if keys & _MATERIALIZED_ARTIFACT_SCHEMA_KEYS:
        return True
    props = schema.get("properties")
    if isinstance(props, dict):
        prop_keys = {str(k).lower() for k in props}
        if prop_keys & _MATERIALIZED_ARTIFACT_SCHEMA_KEYS:
            return True
        for value in props.values():
            if _schema_mentions_artifact(value):
                return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_mentions_artifact(items):
        return True
    return False


def _text_explicitly_requests_artifact(text: str) -> bool:
    if any(term in text for term in _EXPLICIT_FILE_DELIVERABLE_TERMS):
        return True
    if not (
        any(term in text for term in _MEDIA_ARTIFACT_ACTION_TERMS)
        and any(term in text for term in _MEDIA_ARTIFACT_TARGET_TERMS)
    ):
        return False
    if any(term in text for term in _MEDIA_TEXT_DELIVERABLE_TERMS):
        return False
    return True


def _task_requires_artifact(task: Any | None) -> bool:
    if task is None:
        return False

    details = getattr(task, "details", None) or {}
    from packages.core.plans.task_constraints import (
        binding_constraints_forbid_artifact_writes,
    )

    # The runtime_context block is the user's verbatim task contract. It must
    # win over an inferred or stale machine-authored artifact requirement;
    # otherwise missing-artifact recovery creates an impossible write step and
    # retries it until the Plan budget is exhausted.
    if binding_constraints_forbid_artifact_writes(details):
        return False
    expected = getattr(task, "expected_output", None) or {}
    if isinstance(details, dict):
        if details.get("requires_artifact") is True:
            return True
        deliverable_type = str(
            details.get("deliverable_type") or details.get("artifact_type") or details.get("kind") or ""
        ).lower()
        if deliverable_type in _ARTIFACT_DELIVERABLE_TYPES:
            return True
    if isinstance(expected, dict):
        if expected.get("requires_artifact") is True or expected.get("artifact_required") is True:
            return True
        if str(expected.get("kind") or expected.get("artifact_type") or "").lower() in _ARTIFACT_DELIVERABLE_TYPES:
            return True
        if _schema_mentions_artifact(expected):
            return True

    title = getattr(task, "title", "") or ""
    description = getattr(task, "description", "") or ""
    text = f"{title}\n{description}\n{_jsonish_text(expected)}\n{_jsonish_text(details)}".lower()
    if (
        any(term in text for term in _TEXT_ONLY_DELIVERABLE_TERMS)
        and not any(term in text for term in _EXPLICIT_FILE_DELIVERABLE_TERMS)
    ):
        return False
    if any(term in text for term in _ARTIFACT_NEGATION_TERMS):
        return False
    return _text_explicitly_requests_artifact(text)


def _required_artifact_extensions(task: Any | None) -> set[str]:
    if task is None:
        return set()
    text = "\n".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            _jsonish_text(getattr(task, "expected_output", None)),
            _jsonish_text(getattr(task, "details", None)),
        ]
    ).lower()
    return {".mp4"} if re.search(r"(?<![a-z0-9])\.?mp4(?![a-z0-9])", text) else set()


def _artifact_refs_from_result(result: Any, *, step_key: str | None = None, output_schema: dict | None = None) -> list[dict]:
    if output_schema is not None:
        from packages.core.services.artifact_delivery import ArtifactDeliveryError, submitted_artifact_refs

        try:
            refs = submitted_artifact_refs(result, output_schema=output_schema)
        except ArtifactDeliveryError:
            # Rendering cannot choose between conflicting declarations. The
            # inspection contract carries the structured verification error.
            return []
        return _dedupe_task_artifact_refs([
            {"type": "file", **ref, **({"step": step_key} if step_key else {})}
            for ref in refs
        ])
    if not isinstance(result, dict):
        return []
    if _is_reference_only_payload(result):
        return []

    refs: list[dict] = []

    outputs = result.get("outputs")
    if isinstance(outputs, dict):
        nested_outputs = dict(outputs)
        if isinstance(nested_outputs.get("files"), list):
            nested_outputs["artifact_materialized"] = True
        refs.extend(_artifact_refs_from_result(nested_outputs, step_key=step_key))

    def add_ref(
        ref_type: str,
        value: Any,
        *,
        source_key: str = "",
        name: Any = None,
        document_id: Any = None,
    ) -> None:
        if not value or _looks_like_json_pointer_path(value):
            return
        ref: dict[str, Any] = {"type": ref_type}
        if step_key:
            ref["step"] = step_key
        if source_key:
            ref["source"] = source_key
        if name and source_key != "path":
            ref["name"] = str(name)
        if source_key == "document_id" or ref_type == "document_id":
            ref["document_id"] = value
        elif _artifact_value_is_path(source_key, value):
            ref["fs_path"] = value
        else:
            ref["url"] = value
        if document_id and not ref.get("document_id"):
            ref["document_id"] = document_id
        refs.append(ref)

    for key, ref_type in _ARTIFACT_RESULT_KEYS.items():
        add_ref(ref_type, result.get(key), source_key=key, document_id=result.get("document_id"))

    # Async media jobs carry the produced file on each job, not at the root.
    for item in result.get("jobs") or []:
        if not isinstance(item, dict) or not MediaJobStatus.is_completed(item.get("status")):
            continue
        for value_key in ("fs_path", "document_id", "result_url"):
            if item.get(value_key):
                add_ref(
                    str(item.get("kind") or "media"),
                    item[value_key],
                    source_key=value_key,
                    document_id=item.get("document_id"),
                )
                break

    # Canonical Knowledge refs go first so path-based duplicates collected
    # from the producer payload cannot discard the enriched document_id.
    for key in ("knowledge_artifacts", "files", "artifacts", "documents", "images"):
        values = result.get(key)
        if key in {"documents", "files"} and not _has_artifact_creation_signal(result):
            continue
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str):
                add_ref(key.rstrip("s") or "file", item, source_key=key)
                continue
            if not isinstance(item, dict):
                continue
            ref_type = str(item.get("type") or item.get("mime") or key.rstrip("s") or "file")
            item_name = item.get("name") or item.get("filename") or item.get("original_name") or item.get("title")
            for value_key in (
                "artifact_url", "download_url", "file_url", "document_url",
                "image_url", "video_url", "audio_url", "media_url",
                "result_url", "output_url", "public_url", "url",
                "fs_path", "artifact_path", "file_path", "output_path",
                "path", "local_path", "saved_to", "document_id",
            ):
                if item.get(value_key):
                    add_ref(
                        ref_type,
                        item[value_key],
                        source_key=value_key,
                        name=item_name,
                        document_id=item.get("document_id"),
                    )
                    break

    # The default StepResult envelope keeps worker-specific output under
    # ``outputs.data``. Promote that payload before applying the same artifact
    # rules used for legacy root-level results, so paths such as
    # ``mp4_fs_path`` remain usable by downstream steps and the supervisor.
    outputs = result.get("outputs")
    if isinstance(outputs, dict):
        data = outputs.get("data")
        if isinstance(data, dict):
            refs.extend(_artifact_refs_from_result(data, step_key=step_key))
        files = outputs.get("files")
        if isinstance(files, list):
            # ``outputs.files`` is an envelope-defined artifact slot, so it
            # is creation evidence even when the worker did not add a legacy
            # ``saved``/``generated`` flag.
            refs.extend(_artifact_refs_from_result(
                {"files": files, "generated": True},
                step_key=step_key,
            ))

    image_urls = result.get("image_urls")
    if isinstance(image_urls, list):
        for url in image_urls:
            add_ref("image", url, source_key="image_urls")

    # Merge the raw filesystem alias into the canonical document reference.
    # Keeping both produced two cards for one file and let the path-only copy
    # bypass the fixed viewer/Markdown address contract.
    return _dedupe_task_artifact_refs(_dedupe_artifact_refs(refs))


def _step_result_summary(result: Any, *, limit: int = 4000) -> str:
    """Canonical text view of a step result.

    Owned here because the executor is the canonical write path; the
    read-time reconciler imports it so task output and replan context
    describe a result the same way.
    """
    return step_result_output_text(result, limit=limit)


def _dedupe_artifact_refs(refs: list[dict]) -> list[dict]:
    from packages.core.services.generated_file_refs import generated_file_ref_aliases

    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for ref in refs:
        step = str(ref.get("step") or "")
        ref_type = str(ref.get("type") or "")
        aliases = generated_file_ref_aliases(ref)
        identities = aliases or {_artifact_ref_identity(ref)}
        keys = {(step, ref_type, identity) for identity in identities}
        if seen & keys:
            continue
        seen.update(keys)
        out.append(ref)
    return out


def _artifact_ref_identity(ref: dict) -> str:
    from packages.core.services.generated_file_refs import generated_file_ref_aliases

    aliases = generated_file_ref_aliases(ref)
    return sorted(aliases)[0] if aliases else str(ref)


async def resolve_config_versions(
    db: AsyncSession, *, agent_id: str | None, skill_id: str | None = None,
) -> Optional[dict]:
    """``{"agent_revision": N, "skill_revision": N}`` of the config that ran (M11).

    Shared by Plan finalization and the plan-free Task agent loop. Raises on
    lookup failure; callers stamp best-effort.
    """
    versions: dict = {}
    if agent_id:
        from packages.core.models.workspace import Agent
        revision = (await db.execute(
            select(Agent.revision).where(Agent.id == agent_id)
        )).scalar_one_or_none()
        if revision is not None:
            versions["agent_revision"] = int(revision)
    if skill_id:
        from packages.core.models.skill import Skill
        revision = (await db.execute(
            select(Skill.revision).where(Skill.id == skill_id)
        )).scalar_one_or_none()
        if revision is not None:
            versions["skill_revision"] = int(revision)
    return versions or None


def _dedupe_task_artifact_refs(refs: list[dict], *, entity_id: str | None = None) -> list[dict]:
    """Task-level file lists should show each generated artifact once."""
    from packages.core.services.generated_file_refs import dedupe_generated_file_refs

    return dedupe_generated_file_refs(refs, entity_id=entity_id)


def _has_artifact_collection_payload(payload: dict[str, Any]) -> bool:
    """Detect generated artifact lists without misclassifying search refs."""
    if any(key in payload for key in _REFERENCE_ONLY_KEYS):
        return False
    if any(key in payload for key in ("summary", "draft_count", "artifact_materialized")):
        return True
    for key in ("files", "artifacts", "documents", "images"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            if item.get("fs_path") or item.get("saved_to"):
                return True
    return False


def _has_artifact_creation_signal(payload: dict[str, Any]) -> bool:
    if any(bool(payload.get(key)) for key in _ARTIFACT_CREATION_FLAGS):
        return True
    if _has_artifact_collection_payload(payload):
        return True
    return any(bool(payload.get(key)) for key in set(_ARTIFACT_RESULT_KEYS) - {"document_id"})


def _is_reference_only_payload(payload: dict[str, Any]) -> bool:
    if _has_artifact_creation_signal(payload):
        return False
    if any(key in payload for key in _REFERENCE_ONLY_KEYS):
        return True
    if "documents" in payload:
        return True
    return False


def _artifact_ref_matches_extensions(ref: dict, extensions: set[str]) -> bool:
    ref_type = str(ref.get("type") or "").strip().lower()
    if ".mp4" in extensions and ref_type in {"mp4", "video/mp4"}:
        return True
    for key in ("name", "filename", "fs_path", "url", "path", "file_url"):
        value = str(ref.get(key) or "").strip().lower()
        path = value.split("?", 1)[0].split("#", 1)[0]
        if any(path.endswith(extension) for extension in extensions):
            return True
    return False


def _has_artifact_result(
    steps: list[ExecutionStep],
    *,
    required_extensions: set[str] | None = None,
) -> bool:
    refs = [
        ref
        for step in steps
        if step.step_status == ExecutionStepStatus.DONE
        for ref in _artifact_refs_from_result(step.result, step_key=step.step_key, output_schema=artifact_result_schema(step))
    ]
    if not required_extensions:
        return bool(refs)
    return any(
        _artifact_ref_matches_extensions(ref, required_extensions)
        for ref in refs
    )


# ── Replan context budget ─────────────────────────────────────────────
# ``_replan_context`` is dumped verbatim into the planner prompt as part of
# the task Details JSON, so the completed-step digest has to stay
# prompt-sized. Worst case with these caps is roughly
#   12 steps x (1500 summary chars + 6 refs x ~200 chars) capped by a
#   12000-char global summary budget  ≈ 12000 + ~14k ref chars < 30k chars
# and in practice far less, because most steps carry either text or files,
# not both at full size. 1500 chars (~375 tokens) is the smallest cap that
# still lets a downstream step consume a prior step's output directly
# instead of only recognising it — 200 chars was a label, not content.
_REPLAN_MAX_SUCCEEDED_STEPS = 12
_REPLAN_MAX_ARTIFACTS_PER_STEP = 6
_REPLAN_STEP_SUMMARY_CHARS = 1500
_REPLAN_SUMMARY_TOTAL_CHARS = 12000


def _succeeded_step_contexts(steps: list[ExecutionStep]) -> list[dict[str, Any]]:
    """Describe already-completed steps so a replan can REUSE their work.

    A truncated text blurb is a label, not a handle: it hides generated
    files entirely, cuts usable text down to nothing, and — when the step
    produced no output at all — hides the fact that the step ever ran, so
    the planner re-runs it. Each entry therefore carries the step identity,
    a bounded but usable text summary, the same structured artifact refs the
    executor already mines for evidence, and an explicit ``no_output`` flag.
    """
    done = [s for s in steps if s.step_status == ExecutionStepStatus.DONE]
    # Keep the tail: later steps are the ones a follow-up plan consumes,
    # and upstream work is usually already folded into their output.
    if len(done) > _REPLAN_MAX_SUCCEEDED_STEPS:
        done = done[-_REPLAN_MAX_SUCCEEDED_STEPS:]

    remaining_summary_chars = _REPLAN_SUMMARY_TOTAL_CHARS
    contexts: list[dict[str, Any]] = []
    for step in done:
        entry: dict[str, Any] = {"step_key": step.step_key, "kind": step.kind}

        summary = _step_result_summary(step.result, limit=_REPLAN_STEP_SUMMARY_CHARS)
        if summary and remaining_summary_chars > 0:
            entry["result_summary"] = summary[:remaining_summary_chars]
            remaining_summary_chars -= len(entry["result_summary"])

        if getattr(step, "evidence_refs", None):
            entry["evidence_refs"] = step.evidence_refs[:12]
        refs = _artifact_refs_from_result(step.result, step_key=step.step_key, output_schema=artifact_result_schema(step))
        if refs:
            entry["artifacts"] = refs[:_REPLAN_MAX_ARTIFACTS_PER_STEP]

        if isinstance(step.result, dict):
            for key in ("document_id", "fs_path"):
                if step.result.get(key):
                    entry[key] = step.result[key]

        if not step.result:
            # Succeeded with no payload (pure side effect). Say so rather
            # than omitting the step, which reads as "never ran".
            entry["no_output"] = True

        contexts.append(entry)
    return contexts


def _merged_replan_succeeded_steps(
    previous: Any,
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    combined = [
        row for row in (previous if isinstance(previous, list) else []) + current
        if isinstance(row, dict) and row.get("step_key")
    ]
    seen: set[str] = set()
    newest: list[dict[str, Any]] = []
    for row in reversed(combined):
        key = str(row["step_key"])
        if key not in seen:
            seen.add(key)
            newest.append(dict(row))
        if len(newest) >= _REPLAN_MAX_SUCCEEDED_STEPS:
            break
    remaining = _REPLAN_SUMMARY_TOTAL_CHARS
    for row in reversed(newest):
        if summary := row.get("result_summary"):
            row["result_summary"] = str(summary)[:min(remaining, _REPLAN_STEP_SUMMARY_CHARS)]
            remaining -= len(row["result_summary"])
    return list(reversed(newest))


def _unmet_expects_failure(
    plan: ExecutionPlan, steps: list[ExecutionStep], *,
    verified_artifacts: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[str, str] | None:
    """Deterministic completion check (envelope part ③): a done step whose
    plan declared ``expects`` must have CAPTURED evidence for each
    expectation — model claims never count, only step.evidence_refs mined
    from successful tool results (and, for files, tool-proven artifact
    fields already merged into the result)."""
    dag_steps = (plan.plan_dag or {}).get("steps") or []
    declared_by_key = {
        str(ds.get("key")): ds
        for ds in dag_steps
        if isinstance(ds, dict)
    }
    for step in steps:
        if step.step_status != ExecutionStepStatus.DONE:
            continue
        declared = declared_by_key.get(step.step_key) or {}
        expects = declared.get("expects") or []
        evidence = [e for e in (step.evidence_refs or []) if isinstance(e, dict)]
        if declared.get("kind") == "action" and getattr(plan, "execution_mode", "live") == "live":
            if not any(
                e.get("kind") == "tool_effect" and e.get("effect") == "action"
                and isinstance(e.get("fields"), dict)
                and e["fields"].get("provider") == declared.get("provider")
                and e["fields"].get("action_key") == declared.get("action_key")
                and (not declared.get("integration_id")
                     or e["fields"].get("integration_id") == declared.get("integration_id"))
                for e in evidence
            ):
                return "external_action_receipt_missing", (
                    f"Step {step.step_key!r} has no captured receipt for its planned action. "
                    "Verify the external state before executing it again; a done status "
                    "or model claim is not proof of execution."
                )
        if {"publish", "send"}.intersection(expects) and not any(
            e.get("kind") == "tool_effect" and e.get("effect") == "publish"
            and isinstance(e.get("fields"), dict)
            and e["fields"].get("delivery_confirmed") is True
            for e in evidence
        ):
            return "external_publish_receipt_missing", (
                f"Step {step.step_key!r} expects a confirmed external publish, but no "
                "successful publish tool result was captured as evidence. The model's "
                "own claim does not count. Verify the external state before publishing "
                "again, then capture a successful integration tool receipt."
            )
        file_evidence = (
            bool(verified_artifacts.get(str(step.id))) if verified_artifacts is not None
            else any(e.get("kind") == "artifact" for e in evidence)
        )
        if "files" in expects and not file_evidence:
            return "artifact_evidence_missing", (
                f"Step {step.step_key!r} expects a saved file/artifact, but no artifact "
                "evidence was captured. Replan: save the deliverable and return "
                "artifact evidence (fs_path, document_id, file_url, or files)."
            )
    return None


def _unmet_expects_issue(
    plan: ExecutionPlan, steps: list[ExecutionStep], *,
    verified_artifacts: dict[str, list[dict[str, Any]]] | None = None,
) -> str | None:
    """Compatibility view for diagnostics; the gate uses the typed cause."""
    failure = _unmet_expects_failure(
        plan, steps, verified_artifacts=verified_artifacts,
    )
    return failure[1] if failure is not None else None


def _missing_artifact_issue(
    task: Any | None, steps: list[ExecutionStep], *,
    verified_artifacts: dict[str, list[dict[str, Any]]] | None = None,
) -> str | None:
    if not _task_requires_artifact(task):
        return None
    required_extensions = _required_artifact_extensions(task)
    if verified_artifacts is None:
        has_artifact = _has_artifact_result(steps, required_extensions=required_extensions)
    else:
        refs = [ref for items in verified_artifacts.values() for ref in items]
        has_artifact = any(
            not required_extensions or _artifact_ref_matches_extensions(ref, required_extensions)
            for ref in refs
        )
    if has_artifact:
        return None
    format_requirement = ""
    if required_extensions:
        expected = ", ".join(sorted(required_extensions))
        format_requirement = f" matching the required format ({expected})"
    if getattr(task, "workspace_id", None):
        return (
            "This workspace task needs a saved file/media/document deliverable, "
            f"but no saved file link or path{format_requirement} was recorded. "
            "Replan and save the "
            "deliverable under this workspace's default artifact folder, then "
            "return artifact evidence such as fs_path, document_id, file_url, "
            "image_url, video_url, or files. Do not ask the user for a save "
            "location unless they explicitly requested no saved file."
        )
    return (
        "This task needs a saved file/media/document deliverable, but no "
        f"saved file link or path{format_requirement} was recorded. Replan and save the deliverable "
        "to a user-visible file, then return artifact evidence such as fs_path, "
        "document_id, file_url, image_url, video_url, or files."
    )


def _task_output_contract_issue(
    task: Any | None,
    steps: list[ExecutionStep],
) -> str | None:
    """Detect a plan that failed to bind its structured task deliverable.

    Bound steps are already hard-validated at the dispatcher boundary.  This
    deterministic supervisor gate covers plan-shape gaps (for example multiple
    terminal agent steps, so no unique producer could be selected) instead of
    asking the model supervisor to infer JSON Schema compliance from prose.
    """
    from packages.core.contracts.task_output import (
        OutputContractKind,
        output_contract_for_schema,
        terminal_agent_step_key,
        task_expected_output_json_schema,
    )

    expected = task_expected_output_json_schema(getattr(task, "expected_output", None) if task is not None else None)
    if expected is None:
        return None

    # Coordinator children are durable implementation rows for one declared
    # parent Plan step.  They are not independent terminal producers in the
    # logical DAG and must not make an otherwise unique task-output binding
    # appear ambiguous.
    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    terminal_key = terminal_agent_step_key(logical_steps)
    bound = [
        step
        for step in logical_steps
        if step.step_key == terminal_key
        if output_contract_for_schema(step.expected_output_schema).kind
        is OutputContractKind.TASK_ENVELOPE
    ]
    if len(bound) != 1:
        return (
            "The task declares a structured expected_output, but the plan did not "
            "bind it to exactly one terminal llm/subagent deliverable step. Replan "
            "with one terminal structured-output step."
        )
    step = bound[0]
    if step.step_status != ExecutionStepStatus.DONE:
        return f"The structured deliverable step {step.step_key!r} did not complete."
    outputs = step.result.get("outputs") if isinstance(step.result, dict) else None
    if not isinstance(outputs, dict) or "data" not in outputs:
        return f"The structured deliverable step {step.step_key!r} completed without outputs.data."
    return None


def _execution_output_contract_issue(
    plan: ExecutionPlan,
    steps: list[ExecutionStep],
    task: Any | None = None,
) -> str | None:
    """Prove Plan declarations, materialized Step contracts, and results agree.

    The Dispatcher is the primary lease-boundary validator.  This second,
    deterministic gate protects finalization from legacy rows, manual data
    repair, and materialization drift: the model supervisor is never asked to
    infer JSON Schema compatibility from result prose.
    """
    from packages.core.contracts.task_output import (
        task_output_envelope_schema,
        terminal_agent_step_key,
    )
    from packages.core.dispatcher.validation import (
        SchemaError,
        output_schema_is_advisory,
        validate_step_output,
    )
    from packages.core.plans.schema import Plan
    from packages.core.plans.service import (
        _expected_output_schema_for_plan_step,
        _resolve_output_shapes,
    )

    # Nested coordinator children deliberately share the ExecutionStep table
    # so their transcript, receipts and identity survive a process loss.  The
    # Planner did not declare them as top-level DAG nodes; their persisted
    # source contracts are revalidated at the coordinator handoff boundary.
    # Final Plan drift/schema checks therefore operate on logical DAG rows.
    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    materialized_by_key = {step.step_key: step for step in logical_steps}
    try:
        declared_plan = Plan.model_validate(plan.plan_dag or {})
    except Exception:  # Legacy Plans may predate the typed DAG contract.
        declared_plan = None

    if declared_plan is not None:
        declared_keys = {step.key for step in declared_plan.steps}
        unexpected = sorted(set(materialized_by_key) - declared_keys)
        if unexpected:
            return (
                "Materialized execution contains Step(s) not declared by the Plan: "
                + ", ".join(unexpected)
            )

        shape_by_key = _resolve_output_shapes(declared_plan.topo_order())
        task_contract = task_output_envelope_schema(
            getattr(task, "expected_output", None) if task is not None else None
        )
        terminal_key = (
            terminal_agent_step_key(declared_plan.steps)
            if task_contract is not None
            else None
        )
        for declared in declared_plan.topo_order():
            materialized = materialized_by_key.get(declared.key)
            if materialized is None:
                return f"Plan Step {declared.key!r} has no materialized execution Step."
            expected = _expected_output_schema_for_plan_step(
                declared,
                output_shape=shape_by_key.get(declared.key),
                task_output_contract_schema=(
                    task_contract if declared.key == terminal_key else None
                ),
            )
            # A provider may hydrate a previously undeclared action contract
            # at dispatch time.  That is an additional runtime guarantee, not
            # drift.  Any contract the Plan did declare must remain identical.
            if expected is not None and materialized.expected_output_schema != expected:
                return (
                    f"Step {declared.key!r} materialized output contract differs "
                    "from the Plan contract."
                )

    for step in logical_steps:
        schema = getattr(step, "expected_output_schema", None)
        if (
            step.step_status != ExecutionStepStatus.DONE
            or schema is None
            or output_schema_is_advisory(step.kind, schema)
        ):
            continue
        try:
            validate_step_output(step, step.result)
        except SchemaError as exc:
            detail = exc.errors[0]["message"] if exc.errors else str(exc)
            return (
                f"Step {step.step_key!r} delivered result does not satisfy its "
                f"output contract: {detail}"
            )
    return None


def _validated_terminal_task_output(
    task: Any | None,
    steps: list[ExecutionStep],
) -> tuple[str, Any] | None:
    """Return the one schema-valid terminal Task deliverable candidate.

    This proves execution facts only: the unique terminal producer is DONE,
    carries the current Task-authored envelope, did not report a failed
    control status, and its stored result still validates.  The caller must
    separately decide whether the Supervisor accepted that candidate as the
    Task's formal result.
    """
    from packages.core.contracts.envelope import envelope_indicates_failure
    from packages.core.contracts.task_output import (
        task_output_envelope_schema,
        task_output_payload,
        terminal_agent_step_key,
    )
    from packages.core.dispatcher.validation import SchemaError, validate_step_output

    authoritative_schema = task_output_envelope_schema(
        getattr(task, "expected_output", None) if task is not None else None
    )
    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    terminal_key = terminal_agent_step_key(logical_steps)
    if authoritative_schema is None or terminal_key is None:
        return None

    candidates = [step for step in logical_steps if step.step_key == terminal_key]
    if len(candidates) != 1:
        return None
    step = candidates[0]
    if (
        step.step_status != ExecutionStepStatus.DONE
        or step.expected_output_schema != authoritative_schema
        or envelope_indicates_failure(step.result)
    ):
        return None
    try:
        validate_step_output(step, step.result)
    except SchemaError:
        return None
    return step.step_key, task_output_payload(step.result, step.expected_output_schema)


def _accepted_terminal_text_summary(
    task: Any,
    steps: list[ExecutionStep],
    *,
    supervisor_accepted: bool,
) -> str | None:
    """Project text from one accepted terminal agent Step, never an arbitrary leaf."""
    from packages.core.contracts.task_output import (
        task_expected_output_declares_schema,
        terminal_agent_step_key,
    )

    if not supervisor_accepted or task_expected_output_declares_schema(
        getattr(task, "expected_output", None)
    ):
        return None
    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    terminal_key = terminal_agent_step_key(logical_steps)
    if terminal_key is None:
        return None
    candidates = [step for step in logical_steps if step.step_key == terminal_key]
    if len(candidates) != 1 or candidates[0].step_status != ExecutionStepStatus.DONE:
        return None
    result = candidates[0].result
    text = step_result_output_text(result)
    return text or None


def _accepted_terminal_output(
    task: Any,
    plan: ExecutionPlan,
    steps: list[ExecutionStep],
    *,
    supervisor_accepted: bool,
) -> tuple[str, Any, str] | None:
    """Project an accepted deliverable without guessing its field names.

    Task-authored contracts stay authoritative, including invalid declarations:
    a Plan payload cannot substitute for a missing/invalid Task envelope. Only
    explicit marked Plan contracts participate in the fallback; legacy worker
    results retain their historical summary-only projection.
    """
    from packages.core.contracts.task_output import (
        TASK_OUTPUT_CONTRACT_SOURCE,
        TASK_OUTPUT_CONTRACT_SOURCE_KEY,
        output_contract_for_schema,
        task_expected_output_declares_schema,
        terminal_agent_step_key,
    )
    if not supervisor_accepted:
        return None
    if task_expected_output_declares_schema(getattr(task, "expected_output", None)):
        candidate = _validated_terminal_task_output(task, steps)
        return (*candidate, TASK_OUTPUT_CONTRACT_SOURCE) if candidate is not None else None

    if plan.status != ExecutionPlanStatus.COMPLETED:
        return None

    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    terminal_key = terminal_agent_step_key(logical_steps)
    if terminal_key is None:
        return None
    candidates = [step for step in logical_steps if step.step_key == terminal_key]
    if len(candidates) != 1:
        return None
    step = candidates[0]
    contract = output_contract_for_schema(step.expected_output_schema)
    if step.step_status != ExecutionStepStatus.DONE or not contract.is_bare_payload:
        return None
    if _execution_output_contract_issue(plan, steps, task) is not None:
        return None
    # A bare payload's "status" is domain data, not envelope failure control.
    # Step state and the exact accepted contract provide the execution facts.
    return step.step_key, step.result, contract.schema[TASK_OUTPUT_CONTRACT_SOURCE_KEY]


def _structured_status_value(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _structured_result_blocker(
    result: Any,
    *,
    artifact_required: bool,
    depth: int = 0,
) -> str | None:
    """Detect machine-readable blockers without scanning free-form text."""
    if depth > 4:
        return None
    if isinstance(result, list):
        for item in result:
            issue = _structured_result_blocker(
                item,
                artifact_required=artifact_required,
                depth=depth + 1,
            )
            if issue:
                return issue
        return None
    if not isinstance(result, dict):
        return None

    if result.get("__hitl__") is True:
        return "step emitted a structured HITL request"

    for key in _STRUCTURED_PENDING_KEYS:
        pending = result.get(key)
        if isinstance(pending, dict) and any(
            pending.get(field) for field in ("kind", "prompt", "title", "action")
        ):
            return f"step emitted structured {key}"
        if pending is True:
            return f"step emitted structured {key}=true"

    for key in _STRUCTURED_FALSE_KEYS:
        if result.get(key) is False:
            return f"step reported {key}=false"

    if artifact_required and result.get("artifact_materialized") is False:
        return "step reported artifact_materialized=false"

    for key in _STRUCTURED_STATUS_KEYS:
        raw_status = result.get(key)
        # Enum first: when the value is a member of the canonical vocabulary
        # (or a word that normalizes onto one), the enum decides — SUCCEEDED
        # is never a blocker, PARTIAL/FAILED always are. Keyword matching only
        # covers statuses from custom, non-envelope schemas.
        declared = normalize_step_result_status(raw_status)
        if declared is not None:
            if declared is StepResultStatus.SUCCEEDED:
                continue
            if (
                declared is StepResultStatus.PARTIAL
                and artifact_required
                and _has_verified_artifact_payload(result)
            ):
                # A verified file with an unavailable optional receipt (for
                # example a checksum tool) is still safe for a dependent
                # upload step. Preserve blocking for ordinary partial output.
                continue
            return f"step reported {key}={declared.value}"
        status = _structured_status_value(raw_status)
        if status in _STRUCTURED_BLOCKER_STATUSES:
            return f"step reported {key}={status}"

    if result.get("error"):
        return "step returned a structured error payload"
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return "step returned structured validation errors"

    for key in _STRUCTURED_NESTED_RESULT_KEYS:
        value = result.get(key)
        if isinstance(value, (dict, list)):
            issue = _structured_result_blocker(
                value,
                artifact_required=artifact_required,
                depth=depth + 1,
            )
            if issue:
                return issue
    return None


def _has_verified_artifact_payload(result: dict[str, Any]) -> bool:
    """Whether an envelope carries a verified materialized artifact."""
    payload: Any = result
    outputs = result.get("outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get("data"), dict):
        payload = outputs["data"]
    if not isinstance(payload, dict):
        return False
    verification = str(payload.get("verification_status") or "").strip().lower()
    if verification != "verified":
        return False
    return bool(_artifact_refs_from_result(payload))


def _agent_summaries(steps: list[ExecutionStep], *, limit: int = 3) -> list[str]:
    """The agents' own account of what happened, newest first.

    A step's envelope summary is usually the most informative sentence
    anywhere in the run — one staging step said plainly "未完成最终 MP4
    交付;已启动 6 个场景片段生成,但尚未取得可保存的成片 artifact" while the
    operator was shown "step reported status=partial".
    """
    out: list[str] = []
    for step in reversed(steps):
        result = step.result if isinstance(step.result, dict) else None
        if not result:
            continue
        summary = str(result.get("summary") or "").strip()
        # Bookkeeping notices are not an account of the work.
        if not summary or summary in _NON_INFORMATIVE_SUMMARIES:
            continue
        label = (step.step_key or "step").replace("_", " ")
        out.append(f"{label}: {summary[:300]}")
        if len(out) >= limit:
            break
    return out


def _produced_artifact_labels(steps: list[ExecutionStep], *, limit: int = 6) -> list[str]:
    """Filenames the run actually produced, for the "what you already have"
    half of the message."""
    labels: list[str] = []
    seen: set[str] = set()
    for step in steps:
        for ref in _artifact_refs_from_result(step.result, output_schema=artifact_result_schema(step)) or []:
            if not isinstance(ref, dict):
                continue
            value = str(ref.get("fs_path") or ref.get("url") or ref.get("document_id") or "")
            if not value:
                continue
            label = value.rsplit("/", 1)[-1] or value
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= limit:
                return labels
    return labels


def _plain_language_blocker(issue: str, *, locale: str = "en") -> str:
    """Rewrite a machine blocker into something an operator can act on.

    ``_structured_result_blocker`` returns strings like
    "render finished video: step reported status=partial" — precise for a
    log, meaningless in a UI.
    """
    text = str(issue or "").strip()
    label, _, detail = text.partition(": ")
    detail = detail.strip() or text
    step = label.strip() if _ else ""
    for needle, phrasing in (
        ("status=partial", "partial"),
        ("status=failed", "failed"),
        ("status=blocked", "blocked"),
        ("artifact_materialized=false", "unsaved"),
        ("=false", "not_done"),
        ("HITL request", "human"),
        ("pending", "pending"),
    ):
        if needle in detail:
            return t(
                "plan.blocker.named" if step else "plan.blocker.unnamed",
                locale=locale, step=step, reason=t(f"plan.blocker.{phrasing}", locale=locale),
            )
    return t(
        "plan.blocker.named" if step else "plan.blocker.unnamed",
        locale=locale, step=step, reason=t("plan.blocker.incomplete", locale=locale),
    )


def _user_facing_step_issue(issue: str, steps: list[ExecutionStep]) -> str:
    """Keep the failure detail while replacing known internal step identifiers."""
    for step in steps:
        key = step.step_key
        if key and "_" in key:
            issue = re.sub(
                rf"(?<![\w/.-]){re.escape(key)}(?![\w/.-])",
                lambda _match: key.replace("_", " "),
                issue,
            )
    return issue


def _failed_step_issue(steps: list[ExecutionStep]) -> str | None:
    """Keep the execution failure ahead of consequential missing-output checks."""
    failed = [step for step in steps if step.step_status == ExecutionStepStatus.FAILED]
    if not failed:
        return None
    human_issue = _human_required_failure_issue(failed)
    if human_issue:
        return human_issue
    return "; ".join(
        f"{(step.step_key or 'step').replace('_', ' ')}: "
        f"{str((step.error or {}).get('message') or (step.error or {}).get('type') or 'failed')[:500]}"
        for step in failed[:3]
    )


def _hitl_request_message(
    task: Any | None,
    steps: list[ExecutionStep],
    *,
    structured_issue: str | None,
    artifact_issue: str | None,
    failed_steps: list[ExecutionStep],
    locale: str = "en",
) -> str:
    """Explain the hold in terms an operator can act on.

    This message used to render an internal status word verbatim —
    "recover or render finished video: step reported status=partial" — with
    no statement of what was produced, what is missing, or what the operator
    could do about it. Everything below is assembled from data the run
    already recorded.
    """
    lines: list[str] = [t("plan.hitl.stopped", locale=locale)]

    produced = _produced_artifact_labels(steps)
    if produced:
        lines.append("")
        lines.append(t("plan.hitl.produced", locale=locale, files=", ".join(produced)))

    failure_issue = _failed_step_issue(failed_steps)
    if failure_issue:
        lines.append("")
        lines.append(t("plan.hitl.failed", locale=locale, issue=failure_issue))
    elif structured_issue:
        lines.append("")
        lines.append(
            t("plan.hitl.incomplete", locale=locale, issue=_plain_language_blocker(structured_issue, locale=locale))
        )
    elif not artifact_issue:
        lines.append("")
        lines.append(
            t("plan.hitl.unverified", locale=locale)
        )

    if artifact_issue:
        lines.append("")
        lines.append(
            t("plan.hitl.missing", locale=locale)
        )

    said = _agent_summaries(steps)
    if said:
        lines.append("")
        lines.append(t("plan.hitl.agent_report", locale=locale))
        lines.extend(f"- {item}" for item in said)

    lines.append("")
    lines.append(t("plan.hitl.actions", locale=locale))
    lines.append(t("plan.hitl.retry", locale=locale))
    lines.append(t("plan.hitl.guidance", locale=locale))
    lines.append(t("plan.hitl.accept", locale=locale))
    return "\n".join(lines)


def _step_expects_external_publish(step: ExecutionStep, plan_dag: Any) -> bool:
    """Whether the step commits to an external side effect (publish / send).

    One detector for the partial-upstream gate and the plan pre-flight:
    declared ``expects`` / ``PublishResult`` shape, an ``external.*``
    capability, or an action key the governance mapper classifies as external.
    """
    from packages.core.plans.external_effects import step_external_effect

    return step_external_effect(step, plan_dag) is not None


def _partial_envelope_summary(step: ExecutionStep) -> str | None:
    """Summary of a DONE step whose StepResult envelope landed ``partial``.

    Reads ``status`` only on envelope-shaped rows — the same rule the
    dispatcher's success gate follows — so a provider receipt that carries
    ``status`` as payload data is never mistaken for the control signal."""
    from packages.core.contracts.task_output import output_contract_for_schema

    if step.step_status != ExecutionStepStatus.DONE or not isinstance(step.result, dict):
        return None
    if not output_contract_for_schema(step.expected_output_schema).is_envelope:
        return None
    if normalize_step_result_status(step.result.get("status")) is not StepResultStatus.PARTIAL:
        return None
    return str(step.result.get("summary") or "").strip() or "partial result, no summary"


def partial_upstream_publish_issue(
    step: ExecutionStep,
    plan_dag: Any,
    steps_by_key: dict[str, ExecutionStep],
) -> dict[str, Any] | None:
    """Hold an external publish whose dependency delivered only a partial result.

    ``partial`` is usable output for ordinary consumers and stays that way.
    The one consumer that must not run on it silently is a step the plan
    declares as an external publish (``expects: ["publish"]`` or
    ``output_shape: PublishResult``): publishing a placeholder or half a
    draft is irreversible and public. Instead of letting the step run, fail
    it at the boundary — like a ``ReferenceError`` on its input — with a
    failure that names the partial upstream step, so the Task can revise the
    upstream work before any irreversible publish (prod task FQ2881,
    2026-09-14).

    An operator note on this step (``human_input_response`` from a retry
    with guidance) is the explicit "publish the partial content anyway" and
    lifts the hold; retrying the upstream step revives this one instead.
    """
    if step.human_input_response:
        return None
    if not _step_expects_external_publish(step, plan_dag):
        return None
    for dep_key in step.depends_on or []:
        dep = steps_by_key.get(str(dep_key))
        if dep is None:
            continue
        summary = _partial_envelope_summary(dep)
        if summary is None:
            continue
        label = dep.step_key
        reason = (
            f"Step {label!r} delivered only a partial result ({summary}); an external "
            "publish or send must not run on partial content. Complete the upstream "
            "content before publishing; an explicit user review can separately approve "
            "publishing a partial result."
        )
        return {
            "message": reason,
            "failure": {
                "reason": reason,
                "blockers": [f"{label}: {summary}"],
                "retryable": False,
                "requires_human": False,
            },
        }
    return None


def _is_coordinator_child(step: Any) -> bool:
    """A step a coordinator's ``delegate_to_service`` call created."""
    params = getattr(step, "params", None)
    return (
        is_coordinator_child_params(params)
        and bool(str(params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) or "").strip())
    )


def _coordinator_parent_key(step: Any) -> str | None:
    params = getattr(step, "params", None)
    value = params.get(COORDINATOR_PARENT_STEP_PARAM_KEY) if isinstance(params, dict) else None
    return str(value) if value else None


def _is_released_typed_coordinator_child(step: Any) -> bool:
    params = getattr(step, "params", None)
    return (
        _is_coordinator_child(step)
        and isinstance(params, dict)
        and params.get(COORDINATOR_TYPED_CHILD_PARAM_KEY) is True
        and params.get(COORDINATOR_CHILD_RELEASED_PARAM_KEY) is True
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _undispatchable_sentence(
    step: Any, reason: str, *, skip: dict[str, Any], since: datetime, now: datetime,
) -> str:
    """One plain sentence per skip reason — what the person reads on the card."""
    service = getattr(step, "service_key", None) or "this service"
    if reason == DispatchSkipReason.SUBSCRIPTION_UNRESOLVED:
        return (
            f"No active agent is subscribed to service '{service}' in this workspace, "
            "so nothing can run this step."
        )
    if reason == DispatchSkipReason.WORKER_NOT_BOUND:
        return f"The agent for '{service}' is not bound to any worker."
    if reason == DispatchSkipReason.CAPABILITY_UNSUPPORTED:
        capability = (
            getattr(step, "capability_id", None) or getattr(step, "kind", None) or "this step"
        )
        return f"No worker supports capability '{capability}'."
    if reason == DispatchSkipReason.RISK_EXCEEDS_WORKER:
        level = getattr(step, "risk_level", None) or "declared"
        return f"No worker accepts the '{level}' risk level of this step."
    if reason == DispatchSkipReason.LOCAL_ACTION_UNSUPPORTED:
        action = ".".join(
            part for part in (getattr(step, "provider", None), getattr(step, "action_key", None)) if part
        ) or "this action"
        return f"No worker can execute the local action '{action}'."
    if reason == DispatchSkipReason.PROVIDER_CONNECTION_NOT_READY:
        provider = getattr(step, "provider", None) or "provider"
        return f"The '{provider}' connection this step needs is not ready."
    if reason == DispatchSkipReason.WORKSPACE_BUDGET:
        return "The workspace monthly budget is exhausted."
    if reason == DispatchSkipReason.AWAITING_WORKER:
        hours = max(1, int((now - since).total_seconds() // 3600))
        return f"No worker picked this step up for {hours}h."
    detail = str(skip.get("message") or "").strip()
    suffix = f": {detail}" if detail else ""
    return f"This step could not be dispatched ({reason}){suffix}."


def undispatchable_step_failure(step: Any, *, now: datetime) -> dict[str, Any] | None:
    """Watchdog for a runnable step nobody leases.

    The dispatcher stamps why it declined a step under
    ``step.error[DISPATCH_SKIP_KEY]`` (Section D); a step it never even saw
    carries nothing, so the executor stamps ``awaiting_worker`` itself on the
    first pass. Past the grace for the recorded reason — short for structural
    ones (no subscription, no bound worker, unsupported capability), long for
    soft ones (budget, backlog) — the step ends as an internal failure
    that says, in one sentence, what nobody could do. Returns that terminal
    error, or None while the wait is still within grace. A real prior
    execution error without a skip record is left untouched.
    """
    if step.error is None:
        message = "Waiting for a worker to pick this step up."
        step.error = {
            "type": DISPATCH_SKIPPED_ERROR_TYPE,
            "message": message,
            DISPATCH_SKIP_KEY: {
                "reason": DispatchSkipReason.AWAITING_WORKER.value,
                "message": message,
                "since": now.isoformat(),
                "structural": False,
            },
        }
        return None
    if not isinstance(step.error, dict):
        return None
    skip = step.error.get(DISPATCH_SKIP_KEY)
    if not isinstance(skip, dict):
        return None
    since = _parse_iso_datetime(skip.get("since"))
    if since is None:
        # A skip record without a clock starts one now (copy: JSONB change detection).
        stamped = dict(step.error)
        stamped[DISPATCH_SKIP_KEY] = {**skip, "since": now.isoformat()}
        step.error = stamped
        return None
    structural = bool(skip.get("structural"))
    grace = UNDISPATCHABLE_STRUCTURAL_GRACE_SECONDS if structural else UNDISPATCHABLE_SOFT_GRACE_SECONDS
    if (now - since).total_seconds() <= grace:
        return None
    reason = str(skip.get("reason") or DispatchSkipReason.AWAITING_WORKER.value)
    sentence = _undispatchable_sentence(step, reason, skip=skip, since=since, now=now)
    return {
        "type": STEP_UNDISPATCHABLE_ERROR_TYPE,
        "message": sentence,
        "reason": reason,
        "since": since.isoformat(),
        "failure": {
            "reason": sentence,
            "retryable": False,
            "requires_human": False,
            "blockers": [reason],
        },
    }


def _positive_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0:  # NaN or non-positive
        return None
    return seconds


def resolve_plan_max_runtime_seconds(workspace_settings: Any, plan_dag: Any) -> float:
    """Total runtime budget for one plan, in seconds.

    Resolution order matches ``services.retry_policy``: the workspace's
    ``settings.execution_policy.max_plan_runtime_seconds``, then the plan's
    ``plan_dag.metadata.max_plan_runtime_seconds`` (later wins), else
    ``DEFAULT_PLAN_MAX_RUNTIME_SECONDS``. Never below 60 seconds.
    """
    budget: float | None = None
    settings = workspace_settings if isinstance(workspace_settings, dict) else {}
    policy = settings.get("execution_policy")
    if isinstance(policy, dict):
        budget = _positive_seconds(policy.get("max_plan_runtime_seconds")) or budget
    dag = plan_dag if isinstance(plan_dag, dict) else {}
    metadata = dag.get("metadata")
    if isinstance(metadata, dict):
        budget = _positive_seconds(metadata.get("max_plan_runtime_seconds")) or budget
    if budget is None:
        budget = float(DEFAULT_PLAN_MAX_RUNTIME_SECONDS)
    return max(60.0, budget)


def _demote_replan_on_completed_plan(
    decision: SupervisorDecision, plan_status: str, steps: list[ExecutionStep],
) -> SupervisorDecision:
    """On a plan that mechanically COMPLETED, "replan" / "failed" from the
    model becomes one step re-run carrying the verdict as feedback.

    At finalize time a replan almost always just fails the task (the budget
    is spent, or the planner produces the same DAG), and "failed" throws
    away work that only needs one more pass. The newest agent step that has
    not had its supervisor re-run is sent back with the evidence attached
    (the worker renders it as guidance); when no such step is left, the
    original verdict follows the normal bounded replan/failure path. FAILED
    plans keep the model's verdict.
    """
    if decision.source is not SupervisorDecisionSource.MODEL:
        return decision
    if plan_status != ExecutionPlanStatus.COMPLETED:
        return decision
    if decision.verdict not in (SupervisorVerdict.NEEDS_REPLAN, SupervisorVerdict.FAILED):
        return decision
    feedback_step = next(
        (
            s for s in reversed(steps)
            if s.step_status == ExecutionStepStatus.DONE
            and s.kind in ("llm", "subagent")
            and not (s.params or {}).get(SUPERVISOR_STEP_RETRY_FLAG)
            and not _is_coordinator_child(s)
        ),
        None,
    )
    if feedback_step is None:
        suffix = "the deliverable was reviewed and found insufficient; no step is left to re-run"
        evidence = f"{decision.evidence} — {suffix}" if decision.evidence else suffix
        return SupervisorDecision(
            verdict=decision.verdict,
            evidence=evidence[:MAX_EVIDENCE_CHARS],
            source=SupervisorDecisionSource.MODEL,
        )
    return SupervisorDecision(
        verdict=SupervisorVerdict.RETRY_STEP,
        evidence=decision.evidence,
        source=SupervisorDecisionSource.MODEL,
        step_key=feedback_step.step_key,
    )


def _structured_blocking_issue(task: Any | None, steps: list[ExecutionStep]) -> str | None:
    artifact_required = _task_requires_artifact(task)
    for step in steps:
        if step.step_status != ExecutionStepStatus.DONE or not step.result:
            continue
        issue = _structured_result_blocker(
            step.result,
            artifact_required=artifact_required,
        )
        if issue:
            label = (getattr(step, "step_key", None) or "step").replace("_", " ")
            return f"{label}: {issue}"
    return None


TOOL_ERROR_NOT_HUMAN_MARKERS = (
    "unsupported_operation_for_file_type",
    "authorization_receipt_mismatch",
    "path traversal detected",
    "unsupported_binary_edit",
)


def tool_error_failure_is_not_human_required(haystack: str) -> bool:
    text = (haystack or "").lower()
    return any(marker in text for marker in TOOL_ERROR_NOT_HUMAN_MARKERS)


def _human_required_failure_issue(steps: list[ExecutionStep]) -> str | None:
    """Return host-typed access/account blockers, not a model's boolean claim."""
    human_error_types = {
        "CreditExhaustedError",
        "PermissionError",
        "AuthenticationError",
        NO_EXTERNAL_ROUTE_ERROR_TYPE,
    }
    for step in steps:
        if step.step_status != ExecutionStepStatus.FAILED:
            continue
        error = step.error if isinstance(step.error, dict) else {}
        failure = error.get("failure")
        if not isinstance(failure, dict) and isinstance(step.result, dict):
            failure = step.result.get("failure")
        if str(error.get("type") or "") not in human_error_types:
            continue
        reason = (
            str(failure.get("reason") or "").strip()
            if isinstance(failure, dict)
            else ""
        )
        reason = reason or str(error.get("message") or "").strip()
        reason = reason or "the run requires a permission, credential, or human decision"
        blockers = failure.get("blockers") if isinstance(failure, dict) else None
        blocker_text = ", ".join(
            str(value).strip()
            for value in blockers or []
            if str(value or "").strip()
        )
        if isinstance(failure, dict) and failure.get("requires_human") is True:
            haystack = " ".join(
                [
                    reason,
                    blocker_text,
                    str(error.get("message") or ""),
                ]
            ).lower()
            if tool_error_failure_is_not_human_required(haystack):
                continue
        label = (getattr(step, "step_key", None) or "step").replace("_", " ")
        suffix = f" (blocked by: {blocker_text})" if blocker_text else ""
        return f"{label}: {reason}{suffix}"
    return None


def _internal_file_recovery_failure(steps: list[ExecutionStep]) -> dict | None:
    from packages.core.dispatcher.service import INTERNAL_FILE_RECOVERY_FAILED

    return next((
        step.error for step in steps
        if step.step_status == ExecutionStepStatus.FAILED.value
        and isinstance(step.error, dict)
        and step.error.get("type") == INTERNAL_FILE_RECOVERY_FAILED
    ), None)


async def _fresh_unknown_effect_reconciliation(
    db: AsyncSession, plan: ExecutionPlan, steps: list[ExecutionStep],
) -> dict[str, Any] | None:
    from packages.core.dispatcher.effect_safety import (
        ambiguous_effect_reconciliation_snapshot,
    )

    snapshot = await ambiguous_effect_reconciliation_snapshot(
        db, steps, plan_id=plan.id,
    )
    return snapshot if isinstance(snapshot, dict) and snapshot.get("effects") else None


async def evaluate_task_completion_gates(
    db: AsyncSession,
    *,
    task: Any | None,
    plan: ExecutionPlan,
    steps: list[ExecutionStep],
    plan_status: str,
    locale: str,
    verified_artifacts: dict[str, list[dict[str, Any]]] | None = None,
    review_existing_delivery: bool = False,
) -> SupervisorDecision | None:
    """Check server-owned Task acceptance facts before any semantic review.

    The caller owns the Task/Plan/Lease/Step review locks. This boundary may
    hydrate durable artifact references, but does not call a model, commit, or
    change Task/Plan status. ``None`` means no hard gate fired, not that the
    Task's free-form deliverable was semantically accepted.
    """
    from packages.core.constants.supervisor import MAX_EVIDENCE_CHARS
    from packages.core.plans.completion_contracts import (
        completion_contract_issue,
        file_deliverable_contract_issue,
        hydrate_completion_artifacts,
    )

    logical_steps = [step for step in steps if not _is_coordinator_child(step)]
    done_count = sum(
        1 for step in logical_steps if step.step_status == ExecutionStepStatus.DONE
    )
    failed_count = sum(
        1 for step in logical_steps if step.step_status == ExecutionStepStatus.FAILED
    )

    def gate(verdict: SupervisorVerdict, evidence: str) -> SupervisorDecision:
        return SupervisorDecision(
            verdict=verdict,
            evidence=str(evidence)[:MAX_EVIDENCE_CHARS],
            source=SupervisorDecisionSource.GATE,
        )

    plan_error = plan.last_error if isinstance(plan.last_error, dict) else {}
    file_recovery_failure = _internal_file_recovery_failure(steps)
    if file_recovery_failure:
        return gate(SupervisorVerdict.FAILED, file_recovery_failure["message"])
    if plan_error.get("type") == PLAN_DEADLINE_ERROR_TYPE:
        reconciliation = await _fresh_unknown_effect_reconciliation(db, plan, steps)
        if reconciliation:
            plan.last_error = {**plan_error, "reconciliation": reconciliation}
            return gate(
                SupervisorVerdict.NEEDS_HUMAN,
                "A timed-out external action has an unknown outcome; reconcile its exact receipt before retrying.",
            )
        return gate(
            SupervisorVerdict.FAILED,
            str(plan_error.get("message") or "The Plan exceeded its runtime budget."),
        )
    if plan_error.get("type") == "ActionOutcomeReconciliationRequired" and not (
        review_existing_delivery and verified_artifacts is not None
    ):
        reconciliation = await _fresh_unknown_effect_reconciliation(db, plan, steps)
        if reconciliation:
            plan.last_error = {**plan_error, "reconciliation": reconciliation}
            return gate(
                SupervisorVerdict.NEEDS_HUMAN,
                str(
                    plan_error.get("message")
                    or "An external action outcome requires manual reconciliation."
                ),
            )
        # A persisted marker is not proof of a *current* unknown effect.
        # Continue through ordinary completion/evidence gates and review.

    # Runtime completion evidence belongs to the authored producer. A
    # later-created owner-service child must not steal hydration from its
    # coordinator parent and make the parent's ``expects`` look unmet.
    if verified_artifacts is None:
        await hydrate_completion_artifacts(db, task, logical_steps)
    task_completion_issue = await completion_contract_issue(db, task)
    if task_completion_issue and plan_status == ExecutionPlanStatus.COMPLETED:
        logger.info(
            "Supervisor requested replan for plan %s completion contract gap: %s",
            plan.id,
            task_completion_issue,
        )
        return gate(SupervisorVerdict.NEEDS_REPLAN, task_completion_issue)

    execution_contract_issue = _execution_output_contract_issue(plan, steps, task)
    if execution_contract_issue and plan_status == ExecutionPlanStatus.COMPLETED:
        logger.info(
            "Supervisor requested replan for plan %s execution contract gap: %s",
            plan.id,
            execution_contract_issue,
        )
        return gate(SupervisorVerdict.NEEDS_REPLAN, execution_contract_issue)
    task_output_issue = _task_output_contract_issue(task, steps)
    if task_output_issue and plan_status == ExecutionPlanStatus.COMPLETED:
        logger.info(
            "Supervisor requested replan for plan %s task-output contract gap: %s",
            plan.id,
            task_output_issue,
        )
        return gate(SupervisorVerdict.NEEDS_REPLAN, task_output_issue)
    if plan_status == ExecutionPlanStatus.COMPLETED:
        candidate = _validated_terminal_task_output(task, steps)
        file_issue = await file_deliverable_contract_issue(
            db, task, candidate[1] if candidate is not None else None,
        )
        if file_issue:
            return gate(SupervisorVerdict.NEEDS_REPLAN, file_issue)
    structured_issue = _structured_blocking_issue(task, steps)
    if structured_issue and plan_status == ExecutionPlanStatus.COMPLETED:
        logger.info(
            "Supervisor held plan %s for structured blocker: %s",
            plan.id, structured_issue,
        )
        # A Step result is not a durable request for input. Even a structured
        # needs_input claim must be repaired through the Agent/Step flow;
        # only a real pending request or approval may pause on a person.
        return gate(SupervisorVerdict.NEEDS_REPLAN, structured_issue)
    for step in steps:
        if (
            step.step_status == ExecutionStepStatus.FAILED
            and (step.error or {}).get("type") == "RequiredSkillBlocked"
        ):
            return gate(
                SupervisorVerdict.FAILED,
                str(step.error.get("message") or "Required Skill is blocked."),
            )
    artifact_issue = _missing_artifact_issue(task, steps, verified_artifacts=verified_artifacts)
    if artifact_issue and plan_status == ExecutionPlanStatus.COMPLETED:
        logger.info(
            "Supervisor requested replan for plan %s missing artifact evidence: %s",
            plan.id, artifact_issue,
        )
        return gate(SupervisorVerdict.NEEDS_REPLAN, artifact_issue)
    expects_failure = _unmet_expects_failure(
        plan, steps, verified_artifacts=verified_artifacts,
    )
    if expects_failure:
        cause, expects_issue = expects_failure
        logger.info(
            "Supervisor held plan %s with unmet step evidence: %s",
            plan.id, expects_issue,
        )
        # A missing external receipt does not itself prove an unknown effect
        # or authorize a customer request. Nor may the host infer that the
        # action was NOT applied and blindly replay it under a new Plan.
        # Missing file references are ordinary delivery-contract failures.
        verdict = (
            SupervisorVerdict.NEEDS_REPLAN
            if cause == "artifact_evidence_missing"
            else SupervisorVerdict.FAILED
        )
        return gate(verdict, expects_issue)

    # Do not let the LLM supervisor turn a totally failed execution into a
    # completed task. Replanning is attempted before finalization; once we
    # are here, a failed plan with zero successful steps has no core
    # deliverable to accept.
    if plan_status == ExecutionPlanStatus.FAILED and failed_count > 0 and done_count == 0:
        failure_detail = _failed_step_issue(logical_steps)
        summary = t("plan.log.all_steps_failed", locale=locale, count=failed_count)
        return gate(
            SupervisorVerdict.FAILED,
            f"{summary}: {failure_detail}" if failure_detail else summary,
        )

    # Cancelled/blocked: pass through directly.
    if plan_status in (SupervisorVerdict.CANCELLED, SupervisorVerdict.BLOCKED):
        return gate(
            SupervisorVerdict(plan_status),
            t("plan.log.terminal_evidence", locale=locale, status=t(f"task.status.{plan_status}", locale=locale)),
        )
    return None


def _supervisor_attempt_infos(
    prior_plans: list, steps_by_plan: dict[str, list],
) -> list[dict]:
    """Compact summaries of this task's earlier plans, oldest first.

    The supervisor's scope is the TASK: when the current plan is a replan
    (or a resumed retry), what the earlier attempts did and how they ended
    is part of what it is judging. Without this it re-litigates each plan
    from scratch and can keep prescribing what was already tried.
    """
    infos: list[dict] = []
    for prior in prior_plans:
        # Coordinator children are durable implementation rows, not logical
        # plan attempts.  Showing them here double-counts one nested run and
        # can make an earlier failed parent look partially successful.
        steps = [
            step
            for step in (steps_by_plan.get(prior.id) or [])
            if not _is_coordinator_child(step)
        ]
        infos.append({
            "status": prior.status,
            "steps": [
                {
                    "key": s.step_key,
                    "status": s.step_status,
                    "error": (
                        f"{(s.error or {}).get('type', '')}: {(s.error or {}).get('message', '')}".strip(": ")
                        if s.error else ""
                    ),
                }
                for s in steps
            ],
        })
    return infos


def _supervisor_review_infos(verdict_logs: list) -> list[dict]:
    """The supervisor's own earlier decisions on this task, oldest first.

    Sourced from the ai_supervisor_verdict task logs it writes — the same
    record a person reads. A supervisor that cannot see its own last review
    will happily retry the same step against the same result again; the
    once-per-step budget stops the loop mechanically, but the model should
    also be able to REASON about it.
    """
    infos: list[dict] = []
    for log in verdict_logs:
        meta = getattr(log, "meta", None) or {}
        if not meta.get("verdict"):
            continue
        infos.append({
            "verdict": meta.get("verdict"),
            "evidence": meta.get("evidence") or "",
            "step_key": meta.get("step_key"),
            "plan_id": meta.get("plan_id"),
        })
    return infos


def _supervisor_actual_result(
    steps: list[Any],
    *,
    acceptance_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only step evidence explicitly named by task acceptance criteria."""
    criteria = (
        acceptance_contract.get("criteria")
        if isinstance(acceptance_contract, dict)
        else None
    )
    criteria = [item for item in (criteria or []) if isinstance(item, dict)]
    evidence_keys: list[str] = []
    criteria_evidence: list[dict[str, Any]] = []
    for criterion in criteria:
        keys = [
            str(key) for key in (criterion.get("evidence_step_keys") or [])
            if str(key or "").strip()
        ]
        for key in keys:
            if key not in evidence_keys:
                evidence_keys.append(key)
        criteria_evidence.append({
            "criterion_key": criterion.get("key"),
            "deliverable_name": criterion.get("deliverable_name"),
            "description": criterion.get("description"),
            "evidence_step_keys": keys,
        })

    by_key = {str(getattr(step, "step_key", "")): step for step in steps}
    selected_steps = []
    for key in evidence_keys:
        step = by_key.get(key)
        if step is None:
            continue
        selected_steps.append({
            "key": key,
            "step_key": key,
            "kind": getattr(step, "kind", None),
            "status": getattr(step, "step_status", None),
            "result": getattr(step, "result", None),
            "evidence_refs": getattr(step, "evidence_refs", None) or [],
            "error": getattr(step, "error", None),
        })
    return {
        "criteria_evidence": criteria_evidence,
        "steps": selected_steps,
    }


def _supervisor_step_infos(plan: ExecutionPlan, steps: list[ExecutionStep]) -> list[dict]:
    """What the supervisor gets to see about each step.

    The old view was one line per step with 150 characters of result — no
    instruction, no artifacts — so the supervisor was judging deliverables
    it could not observe. The production misjudgements were structural:
    task 01KWRR5VGHYHQD3A116TZ8ET0W's step SAID it appended the entry, and
    the supervisor had no artifact evidence in view to notice nothing was
    written.

    Everything here is data the system already records: the plan_dag's
    per-step description and prompt (what the step was ASKED to do), the
    step's own result preview, and the artifact refs extracted from its
    result — the same refs actual_output aggregates. Rendering is runtime's
    job; this only extracts.
    """
    dag_steps: dict[str, dict] = {}
    for entry in (plan.plan_dag or {}).get("steps") or []:
        if isinstance(entry, dict) and entry.get("key"):
            dag_steps[str(entry["key"])] = entry

    from packages.core.contracts.task_output import output_contract_for_schema
    from packages.core.dispatcher.validation import output_schema_is_advisory

    infos: list[dict] = []
    for s in steps:
        dag = dag_steps.get(s.step_key, {})
        instruction_parts = [str(dag.get("description") or "").strip()]
        prompt = (s.params or {}).get("prompt") or (dag.get("params") or {}).get("prompt") or ""
        if str(prompt).strip():
            instruction_parts.append(str(prompt).strip())
        artifacts: list[str] = []
        for ref in _artifact_refs_from_result(s.result, step_key=s.step_key, output_schema=artifact_result_schema(s)):
            label = ref.get("fs_path") or ref.get("document_id") or ref.get("url") or ref.get("file_url")
            if label and str(label) not in artifacts:
                artifacts.append(str(label))
        error = ""
        if s.error:
            error = f"{s.error.get('type', '')}: {s.error.get('message', '')}".strip(": ")
        contract = output_contract_for_schema(
            getattr(s, "expected_output_schema", None)
        )
        infos.append({
            "key": s.step_key,
            "kind": s.kind,
            "owner": s.service_key or s.action_key or "",
            "status": s.step_status,
            "attempts": s.attempt_count,
            "instruction": " — ".join(part for part in instruction_parts if part),
            "result": _supervisor_result_preview(s.result, max_chars=4000) if s.result else "",
            "output_contract": contract.payload_schema or contract.schema,
            "contract_check": (
                "passed"
                if (
                    s.step_status == ExecutionStepStatus.DONE
                    and contract.has_schema
                    and not output_schema_is_advisory(s.kind, contract.schema)
                )
                else ("advisory" if contract.has_schema else "not_declared")
            ),
            "artifacts": artifacts,
            "expects": dag.get("expects") or [],
            "evidence_refs": list(getattr(s, "evidence_refs", None) or []),
            "error": error,
        })
    return infos


def _supervisor_result_preview(result: Any, *, max_chars: int = 1200) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        outputs = result.get("outputs")
        if isinstance(outputs, dict):
            if "data" in outputs:
                # ``outputs.data`` is the canonical home of a task-authored
                # structured deliverable.  Falling through to the envelope's
                # short summary hides that payload from the supervisor and can
                # turn a valid result into a retry/replan verdict.
                data_preview = _jsonish_text(outputs.get("data")).strip()
                if data_preview:
                    summary = str(result.get("summary") or "").strip()
                    parts = [part for part in (summary, data_preview) if part]
                    return "\n".join(parts)[:max_chars]
            output_text = outputs.get("text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text.strip()[:max_chars]
        priority_keys = (
            "result_summary", "summary", "message", "text", "value",
            "content", "answer", "output", "result", "error", "errors",
        )
        parts: list[str] = []
        for key in priority_keys:
            if key in result:
                parts.append(_supervisor_result_preview(result.get(key), max_chars=max_chars))
        if parts:
            return "\n".join(part for part in parts if part)[:max_chars]
        return _jsonish_text(result)[:max_chars]
    if isinstance(result, list):
        parts = [_supervisor_result_preview(item, max_chars=max_chars) for item in result[:8]]
        return "\n".join(part for part in parts if part)[:max_chars]
    return str(result)[:max_chars]


def _supervisor_plan_state(plan: ExecutionPlan) -> dict[str, Any]:
    state = {
        column.key: getattr(plan, column.key, None)
        for column in ExecutionPlan.__table__.columns
        # Persisting the claim itself advances this server-managed timestamp.
        # Every verdict-relevant Plan field is fingerprinted separately.
        if column.key != "updated_at"
    }
    dispatcher_state = dict(plan.dispatcher_state or {})
    dispatcher_state.pop(_SUPERVISOR_REVIEW_CLAIM_KEY, None)
    state["dispatcher_state"] = dispatcher_state
    return state


def _supervisor_task_state(task: Any) -> dict[str, Any]:
    return {
        column.key: getattr(task, column.key, None)
        for column in task.__table__.columns
    }


def _supervisor_step_state(step: ExecutionStep) -> dict[str, Any]:
    return {
        column.key: getattr(step, column.key, None)
        for column in ExecutionStep.__table__.columns
    }


def _supervisor_lease_state(lease: Any) -> dict[str, Any]:
    return {
        column.key: getattr(lease, column.key, None)
        for column in lease.__table__.columns
    }


async def _lock_supervisor_review_state(
    db: AsyncSession,
    plan: ExecutionPlan,
) -> tuple[Any, ExecutionPlan, list[ExecutionPlan], list[Any], list[ExecutionStep]]:
    """Lock Task -> Plans -> Leases -> Steps for one review generation."""

    from packages.core.models.task import Task
    from packages.core.models.worker import WorkLease

    if not plan.task_id:
        raise _StaleSupervisorVerdict(
            plan_status=str(plan.status),
            reason=f"plan {plan.id} no longer has a Task supervisor origin",
        )
    task = (await db.execute(
        select(Task)
        .where(Task.id == plan.task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None:
        raise _StaleSupervisorVerdict(
            plan_status=None,
            reason=f"task {plan.task_id} disappeared before supervisor review",
        )
    task_plans = list((await db.execute(
        select(ExecutionPlan)
        .where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.entity_id == task.entity_id,
        )
        .order_by(ExecutionPlan.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    locked_plan = next((candidate for candidate in task_plans if candidate.id == plan.id), None)
    if locked_plan is None:
        raise _StaleSupervisorVerdict(
            plan_status=None,
            reason=f"plan {plan.id} disappeared before supervisor review",
        )
    if (
        locked_plan.entity_id != task.entity_id
        or locked_plan.workspace_id != task.workspace_id
        or locked_plan.task_id != task.id
    ):
        raise _StaleSupervisorVerdict(
            plan_status=str(locked_plan.status),
            reason=f"plan {plan.id} changed ownership before supervisor review",
        )
    plan_ids = [candidate.id for candidate in task_plans]
    leases = list((await db.execute(
        select(WorkLease)
        .where(WorkLease.plan_id.in_(plan_ids))
        .order_by(WorkLease.plan_id, WorkLease.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    task_steps = list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id.in_(plan_ids))
        .order_by(ExecutionStep.plan_id, ExecutionStep.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all())
    steps = [step for step in task_steps if step.plan_id == locked_plan.id]
    return task, locked_plan, task_plans, leases, steps


async def _supervisor_state_payload(
    db: AsyncSession,
    *,
    task: Any,
    plan: ExecutionPlan,
    task_plans: list[ExecutionPlan],
    leases: list[Any],
    candidate_status: str,
    locale: str,
) -> dict[str, Any]:
    """Collect every durable input that can change a supervisor verdict."""

    from packages.core.models.task import TaskLog
    from packages.core.models.worker import WorkLease

    plan_ids = [candidate.id for candidate in task_plans]
    all_steps = list((await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id.in_(plan_ids))
        .order_by(ExecutionStep.plan_id, ExecutionStep.created_at, ExecutionStep.id)
    )).scalars().all()) if plan_ids else []
    all_leases = list((await db.execute(
        select(WorkLease)
        .where(WorkLease.plan_id.in_(plan_ids))
        .order_by(WorkLease.plan_id, WorkLease.id)
    )).scalars().all()) if plan_ids else leases
    verdict_logs = list((await db.execute(
        select(TaskLog)
        .where(
            TaskLog.task_id == task.id,
            TaskLog.log_type == SUPERVISOR_VERDICT_LOG_TYPE,
        )
        .order_by(TaskLog.created_at, TaskLog.id)
    )).scalars().all())
    return {
        "version": 1,
        "candidate_status": candidate_status,
        "locale": locale,
        "task": _supervisor_task_state(task),
        "plans": [_supervisor_plan_state(candidate) for candidate in task_plans],
        "steps": [_supervisor_step_state(step) for step in all_steps],
        "leases": [_supervisor_lease_state(lease) for lease in all_leases],
        "reviews": [
            {
                "id": log.id,
                "content": log.content,
                "metadata": log.meta,
                "created_at": log.created_at,
            }
            for log in verdict_logs
        ],
    }


async def _supervisor_state_fingerprint(
    db: AsyncSession,
    *,
    task: Any,
    plan: ExecutionPlan,
    task_plans: list[ExecutionPlan],
    leases: list[Any],
    candidate_status: str,
    locale: str,
) -> str:
    payload = await _supervisor_state_payload(
        db,
        task=task,
        plan=plan,
        task_plans=task_plans,
        leases=leases,
        candidate_status=candidate_status,
        locale=locale,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _supervisor_review_claim(plan: ExecutionPlan) -> dict[str, Any] | None:
    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    claim = state.get(_SUPERVISOR_REVIEW_CLAIM_KEY)
    return dict(claim) if isinstance(claim, dict) else None


def _clear_supervisor_review_claim(plan: ExecutionPlan, *, token: str) -> bool:
    claim = _supervisor_review_claim(plan)
    if not claim or str(claim.get("token") or "") != token:
        return False
    state = dict(plan.dispatcher_state or {})
    state.pop(_SUPERVISOR_REVIEW_CLAIM_KEY, None)
    plan.dispatcher_state = state
    return True


async def _claim_supervisor_review_generation(
    db: AsyncSession,
    *,
    task: Any,
    plan: ExecutionPlan,
    task_plans: list[ExecutionPlan],
    leases: list[Any],
    candidate_status: str,
    locale: str,
) -> _SupervisorReviewGeneration:
    """Persist one bounded model-review owner before releasing row locks."""

    from packages.core.models.base import generate_ulid

    now = datetime.now(timezone.utc)
    existing = _supervisor_review_claim(plan)
    if existing:
        try:
            expires_at = float(existing.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if str(existing.get("token") or "") and expires_at > now.timestamp():
            raise _SupervisorReviewInProgress(
                f"plan {plan.id} already has a live supervisor review"
            )

    fingerprint = await _supervisor_state_fingerprint(
        db,
        task=task,
        plan=plan,
        task_plans=task_plans,
        leases=leases,
        candidate_status=candidate_status,
        locale=locale,
    )
    token = generate_ulid()
    state = dict(plan.dispatcher_state or {})
    state[_SUPERVISOR_REVIEW_CLAIM_KEY] = {
        "version": 1,
        "token": token,
        "fingerprint": fingerprint,
        "candidate_status": candidate_status,
        "claimed_at": now.timestamp(),
        "expires_at": now.timestamp()
        + SUPERVISOR_LLM_TIMEOUT_SECONDS
        + _SUPERVISOR_REVIEW_CLAIM_GRACE_SECONDS,
    }
    plan.dispatcher_state = state
    await db.flush()
    return _SupervisorReviewGeneration(
        token=token,
        fingerprint=fingerprint,
        plan_id=plan.id,
        task_id=task.id,
        plan_status=str(plan.status),
        task_status=str(task.status),
        candidate_status=str(candidate_status),
        locale=locale,
    )


async def _revalidate_supervisor_review_generation(
    db: AsyncSession,
    generation: _SupervisorReviewGeneration,
    plan: ExecutionPlan,
) -> None:
    """Reacquire canonical locks and discard a verdict from stale inputs."""

    try:
        task, locked_plan, task_plans, leases, steps = await _lock_supervisor_review_state(
            db, plan
        )
    except _StaleSupervisorVerdict:
        await db.rollback()
        raise

    claim = _supervisor_review_claim(locked_plan)
    if not claim or str(claim.get("token") or "") != generation.token:
        await db.rollback()
        raise _StaleSupervisorVerdict(
            plan_status=str(locked_plan.status),
            reason=f"plan {locked_plan.id} supervisor generation was replaced",
        )

    current_terminal = PlanExecutor._terminal_summary(steps)
    current_fingerprint = await _supervisor_state_fingerprint(
        db,
        task=task,
        plan=locked_plan,
        task_plans=task_plans,
        leases=leases,
        candidate_status=generation.candidate_status,
        locale=await resolve_plan_response_locale(db, locked_plan),
    )
    unchanged = (
        locked_plan.status == generation.plan_status
        and task.status == generation.task_status
        and current_terminal == generation.candidate_status
        and current_fingerprint == generation.fingerprint
    )
    if not unchanged:
        _clear_supervisor_review_claim(locked_plan, token=generation.token)
        await db.commit()
        raise _StaleSupervisorVerdict(
            plan_status=str(locked_plan.status),
            reason=(
                f"plan {locked_plan.id} changed while supervisor I/O was in flight "
                f"(plan_status={locked_plan.status == generation.plan_status}, "
                f"task_status={task.status == generation.task_status}, "
                f"terminal={current_terminal == generation.candidate_status}, "
                f"fingerprint={current_fingerprint == generation.fingerprint})"
            ),
        )
    _clear_supervisor_review_claim(locked_plan, token=generation.token)
    await db.flush()


class PlanExecutor:
    """Plan-level DAG orchestrator.

    Stateless — canonical state lives in execution_plans /
    execution_steps. Concurrent run_cycle calls for the same plan are
    serialized per Plan until the cycle's state is committed. The
    Dispatcher's atomic checkout separately prevents double-leasing.
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or async_session

    async def run_cycle(self, plan_id: str) -> dict:
        """One pass over the plan. Returns ``{status, next_action,
        delay_seconds}`` so the caller (Celery ``run_plan`` task) can
        decide whether to re-enqueue and after how long."""
        announce_started = False
        progress_locale = "en"
        sleep_seconds: float = 0.0
        chat_events: list[dict] = []
        workspace_settings: dict | None = None

        async with self._session_factory() as db:
            claimed = await self._try_claim_cycle(db, plan_id)
            if not claimed:
                return {
                    "plan_id": plan_id,
                    "status": "cycle_busy",
                    "next_action": "schedule_self",
                    "delay_seconds": CYCLE_TICK_SECONDS,
                }
            plan = await self._load_plan(db, plan_id)
            if plan is None:
                return {"plan_id": plan_id, "status": "not_found", "next_action": "stop"}
            task_title = await self._load_task_title(db, plan)

            if plan.status in (
                ExecutionPlanStatus.COMPLETED,
                ExecutionPlanStatus.FAILED,
                ExecutionPlanStatus.CANCELLED,
                ExecutionPlanStatus.REPLANNED,
            ):
                # Terminal approval/card cleanup deliberately runs in a new
                # transaction.  Approval resolvers lock HitlRequest first and
                # may then touch their Task continuation; doing the cleanup
                # while this cycle still owns Task/Plan/Lease rows creates the
                # inverse lock order and can deadlock a simultaneous decision.
                terminal_status = plan.status
                terminal_task_id = plan.task_id
                await db.commit()
                await self._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan.id,
                    task_id=terminal_task_id,
                    reason=(
                        "plan_replanned"
                        if terminal_status == ExecutionPlanStatus.REPLANNED.value
                        else "plan_terminal"
                    ),
                )
                return {"plan_id": plan_id, "status": plan.status, "next_action": "stop"}

            if plan.status == ExecutionPlanStatus.PENDING_APPROVAL:
                return {
                    "plan_id": plan_id,
                    "status": "pending_approval",
                    "next_action": "wait_for_approval",
                }

            # Every mutating Plan cycle owns Task before Plan.  User
            # cancel/retry takes Workspace -> Task -> Plan; this shorter
            # compatible prefix never waits on Workspace and prevents
            # finalization's Plan autoflush from racing Task acquisition in
            # the inverse order.
            try:
                plan = await self._lock_cycle_mutation_origin(db, plan)
            except _SupersededTaskPlan as exc:
                await db.rollback()
                authoritative_wake = await self._persist_superseded_task_plan(
                    db,
                    plan_id=plan_id,
                    detail=str(exc),
                )
                if authoritative_wake is not None:
                    from packages.core.plans.wakeup import (
                        dispatch_plan_cycle_wake_hint,
                    )

                    dispatch_plan_cycle_wake_hint(
                        authoritative_wake,
                        reason="superseded_plan_authoritative_post_commit_hint",
                    )
                return {
                    "plan_id": plan_id,
                    "status": "superseded_task_plan",
                    "next_action": "stop",
                    "detail": str(exc),
                }
            except _TaskEffectHistoryBlocked as exc:
                await db.rollback()
                await self._persist_task_effect_history_block(
                    db,
                    plan_id=plan_id,
                    detail=str(exc),
                )
                return {
                    "plan_id": plan_id,
                    "status": "task_effect_history_blocked",
                    "next_action": "stop",
                    "detail": str(exc),
                }
            if plan.status in (
                ExecutionPlanStatus.COMPLETED,
                ExecutionPlanStatus.FAILED,
                ExecutionPlanStatus.CANCELLED,
                ExecutionPlanStatus.REPLANNED,
            ):
                terminal_status = plan.status
                terminal_task_id = plan.task_id
                await db.commit()
                await self._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan.id,
                    task_id=terminal_task_id,
                    reason=(
                        "plan_replanned"
                        if terminal_status == ExecutionPlanStatus.REPLANNED.value
                        else "plan_terminal"
                    ),
                )
                return {"plan_id": plan_id, "status": plan.status, "next_action": "stop"}
            if plan.status == ExecutionPlanStatus.PENDING_APPROVAL:
                await db.rollback()
                return {
                    "plan_id": plan_id,
                    "status": "pending_approval",
                    "next_action": "wait_for_approval",
                }

            if plan.workspace_id:
                from packages.core.models.workspace import Workspace
                from packages.core.models.task import Task
                from packages.core.services.task_execution_origin import plan_has_manual_execution

                workspace = await db.get(Workspace, plan.workspace_id)
                if workspace is not None and isinstance(workspace.settings, dict):
                    workspace_settings = workspace.settings
                if workspace is not None and workspace.status == "paused":
                    task = await db.get(Task, plan.task_id) if plan.task_id else None
                    if not plan_has_manual_execution(plan, task):
                        result = {
                            "plan_id": plan_id,
                            "status": "workspace_autonomy_paused",
                            "next_action": "schedule_self",
                            "delay_seconds": 60,
                        }
                        await self._stage_schedule_self(
                            db, plan_id, result,
                            reason="workspace_autonomy_paused",
                        )
                        await db.commit()
                        return result
                from packages.core.services.workspace_readiness import (
                    evaluate_current_workspace_blocking_setup,
                    task_is_authorized_workspace_setup,
                )

                setup_status = await evaluate_current_workspace_blocking_setup(
                    db,
                    workspace_id=str(plan.workspace_id),
                    entity_id=str(plan.entity_id) if plan.entity_id else None,
                )
                setup_task = await db.get(Task, plan.task_id) if plan.task_id else None
                authorized_setup = bool(
                    setup_task is not None
                    and setup_task.workspace_id == plan.workspace_id
                    and setup_task.entity_id == plan.entity_id
                    and await task_is_authorized_workspace_setup(db, setup_task)
                ) if setup_status is not None and setup_status.blocks_work else False
                if setup_status is not None and setup_status.blocks_work and not authorized_setup:
                    if setup_status.missing_setup_key == "workspace_unavailable":
                        return {
                            "plan_id": plan_id,
                            "status": "workspace_unavailable",
                            "next_action": "stop",
                        }
                    result = {
                        "plan_id": plan_id,
                        "status": "blocked_setup",
                        "next_action": "schedule_self",
                        "delay_seconds": 30,
                    }
                    await self._stage_schedule_self(
                        db, plan_id, result,
                        reason="workspace_setup_blocked",
                    )
                    await db.commit()
                    return result

            if plan.task_id:
                billing_scope = await runtime_ensure_task_billing_context(
                    db,
                    plan.task_id,
                    source=RUNTIME_PLAN_EXECUTOR_SOURCE,
                    model_role="worker",
                )
            else:
                billing_scope = runtime_ensure_plan_executor_billing_context(plan)

            progress_locale = await resolve_plan_response_locale(
                db, plan, user_id=getattr(billing_scope, "user_id", None),
            )
            if plan.status == ExecutionPlanStatus.DRAFT:
                plan.status = ExecutionPlanStatus.RUNNING.value
                plan.started_at = datetime.now(timezone.utc)
                announce_started = True
                await db.flush()
                # Log plan start to task
                if plan.task_id:
                    steps = await self._all_steps(db, plan_id)
                    from packages.core.workspace_chat.notifiers import _render_dag
                    dag_text = _render_dag(
                        self._snapshot_steps(steps, locale=progress_locale), entity_id=plan.entity_id or "",
                        locale=progress_locale,
                    )
                    await self._task_log(db, plan, "plan_started",
                        t("task.execution.plan_started", locale=progress_locale, step_count=len(steps)) + f"\n\n{dag_text}",
                        {"plan_id": plan.id, "step_count": len(steps), "execution_mode": plan.execution_mode})

            chat_ws = plan.workspace_id
            chat_entity = plan.entity_id
            chat_plan_id = plan.id
            chat_task_id = plan.task_id
            chat_mode = plan.execution_mode

            # Initial state load + early terminal check.
            steps = await self._all_steps(db, plan_id)
            if announce_started:
                # A plan that has just started is checked for what it cannot
                # do in this workspace BEFORE any step runs: an external
                # effect the governance forbids, or one no connected route
                # can carry, parks the task now with the fix named.
                if await self._preflight_external_effects(
                    db, plan, steps,
                    user_id=getattr(billing_scope, "user_id", None),
                ):
                    await db.flush()
                    steps = await self._all_steps(db, plan_id)
            now = datetime.now(timezone.utc)
            if await self._cancel_orphaned_coordinator_children(
                db,
                steps,
                now=now,
            ):
                await db.flush()
            if self._wake_waiting_coordinator_parents(steps):
                await db.flush()
            if await self._enforce_plan_deadline(
                db, plan, steps,
                workspace_settings=workspace_settings, now=now, chat_events=chat_events,
            ):
                await db.flush()
            task_coordinator_handoff = await self._handoff_task_coordinator_plan(
                db, plan, steps,
            )
            if task_coordinator_handoff is not None:
                await db.commit()
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started,
                    step_count=len(steps),
                    execution_mode=chat_mode,
                    chat_events=chat_events,
                    plan_done=None,
                    plan_started_at=None,
                    plan_completed_at=None,
                    plan_cost=None,
                    plan_error=None,
                    task_title=task_title,
                    locale=progress_locale,
                    step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
                )
                return task_coordinator_handoff
            terminal = self._terminal_summary(steps)
            if terminal == "completed":
                replanned = await self._maybe_replan_for_missing_artifact(db, plan, steps)
                if replanned:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event, supervisor_deferred = await self._finalize_for_cycle(
                    db, plan, "completed"
                )
                if supervisor_deferred is not None:
                    return supervisor_deferred
                if plan.status == ExecutionPlanStatus.REPLANNED:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                await self._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan.id,
                    task_id=plan.task_id,
                    reason="plan_terminal",
                )
                await self._emit_task_event(task_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started, step_count=len(steps),
                    execution_mode=chat_mode, chat_events=chat_events,
                    plan_done="completed",
                    plan_started_at=plan.started_at,
                    plan_completed_at=plan.completed_at,
                    plan_cost=(plan.cost_tracking or {}).get("usd"),
                    plan_error=None,
                    task_title=task_title,
                    locale=progress_locale,
                    step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
                    task_status=_task_status_from_event(task_event),
                    task_issue=(
                        ((task_event or {}).get("payload") or {}).get("issue")
                        or ((task_event or {}).get("payload") or {}).get("prompt")
                    ),
                )
                return {"plan_id": plan_id, "status": "completed", "next_action": "stop"}
            if terminal == "failed":
                # Try replanning before giving up
                replanned = await self._maybe_replan(db, plan, steps)
                if replanned:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event, supervisor_deferred = await self._finalize_for_cycle(
                    db, plan, "failed"
                )
                if supervisor_deferred is not None:
                    return supervisor_deferred
                if plan.status == ExecutionPlanStatus.REPLANNED:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                await self._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan.id,
                    task_id=plan.task_id,
                    reason="plan_terminal",
                )
                await self._emit_task_event(task_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started, step_count=len(steps),
                    execution_mode=chat_mode, chat_events=chat_events,
                    plan_done="failed",
                    plan_started_at=plan.started_at,
                    plan_completed_at=plan.completed_at,
                    plan_cost=None,
                    plan_error=plan.last_error,
                    task_title=task_title,
                    locale=progress_locale,
                    step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
                    task_status=((task_event or {}).get("payload") or {}).get("task_status"),
                    task_issue=(
                        ((task_event or {}).get("payload") or {}).get("issue")
                        or ((task_event or {}).get("payload") or {}).get("prompt")
                    ),
                )
                return {"plan_id": plan_id, "status": "failed", "next_action": "stop"}

            steps_by_key = {s.step_key: s for s in steps}
            prior_results = self._collect_prior_results(steps)
            from packages.core.plans.coordinator import (
                TASK_COORDINATOR_PLAN_VERSION,
                TASK_COORDINATOR_PLAN_VERSION_KEY,
            )

            plan_state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
            task_coordinator_v3 = (
                plan_state.get(TASK_COORDINATOR_PLAN_VERSION_KEY)
                == TASK_COORDINATOR_PLAN_VERSION
            )
            # Decide what to do with each runnable step.
            for step in self._pick_runnable(steps, steps_by_key):
                if step.kind == "sleep":
                    seconds = self._sleep_seconds(step.params or {})
                    if task_coordinator_v3 and seconds > 0:
                        sleep_seconds = (
                            min(sleep_seconds, seconds)
                            if sleep_seconds > 0 else seconds
                        )
                    else:
                        sleep_seconds = max(sleep_seconds, seconds)
                    if (
                        (_is_released_typed_coordinator_child(step) or task_coordinator_v3)
                        and seconds > 0
                    ):
                        # The durable wake timestamp remains on the pending
                        # child. No worker or parent lease is occupied while
                        # the next Plan cycle is delayed until it is due.
                        continue
                    self._mark_done(step, {"slept": seconds}, None)

                elif step.kind == "human":
                    # Review steps can bind directly to an upstream
                    # deliverable (especially ``outputs.files``). Resolve the
                    # binding before posting the HITL card so the UI receives
                    # real document ids/paths, not a literal `${{ ... }}`.
                    try:
                        step.params = resolve_refs(
                            step.params or {}, prior_results,
                            strict=_step_expects_external_publish(step, plan.plan_dag),
                        )
                    except ReferenceError as exc:
                        self._mark_failed(step, {
                            "type": "ReferenceError", "message": str(exc),
                        })
                        chat_events.append({
                            "kind": "step_failed", "step": step,
                            "error": {"type": "ReferenceError", "message": str(exc)},
                            "will_retry": False,
                        })
                        continue
                    if step.human_input_response is not None:
                        self._mark_done(step, step.human_input_response, None)
                        step.human_input_prompt = None
                    else:
                        self._mark_waiting_human(step, str((step.params or {}).get("prompt") or ""))
                        chat_events.append({
                            "kind": "step_needs_human",
                            "step": step,
                            "pending_action": _human_step_pending_action(step.params, locale=progress_locale),
                        })
                        # M9.2 — surface the wait as a HumanCommitment so the
                        # human-queue / consolidator can see the blocking input.
                        # Best-effort like the ledger adapters: never break the
                        # executor. open_commitment dedupes per waiting step.
                        if chat_ws:
                            try:
                                from packages.core.humans import open_commitment
                                await open_commitment(
                                    db,
                                    entity_id=chat_entity,
                                    workspace_id=chat_ws,
                                    request_kind="input",
                                    source_kind="execution_step",
                                    source_id=step.id,
                                    expected_input=(
                                        step.human_input_prompt or task_title or None
                                    ),
                                    blocking_execution_ids=[
                                        chat_task_id or chat_plan_id
                                    ],
                                )
                            except Exception:  # noqa: BLE001 — never fatal
                                logger.warning(
                                    "human commitment open failed for step %s (ignored)",
                                    step.id, exc_info=True,
                                )

                elif step.kind in ("action", "llm", "subagent"):
                    # Resolve refs into step.params so the dispatcher
                    # hands the worker a self-contained payload.
                    try:
                        params = step.params if isinstance(step.params, dict) else {}
                        if params.get(COORDINATOR_PARAM_KEY) is not True:
                            step.params = resolve_refs(
                                params,
                                prior_results,
                                strict=_step_expects_external_publish(
                                    step, plan.plan_dag
                                ),
                            )
                    except ReferenceError as exc:
                        self._mark_failed(step, {
                            "type": "ReferenceError", "message": str(exc),
                        })
                        chat_events.append({
                            "kind": "step_failed", "step": step,
                            "error": {"type": "ReferenceError", "message": str(exc)},
                            "will_retry": False,
                        })
                        continue
                    # An external publish must not run on a partial upstream
                    # result: fail it at the boundary (its input is not a
                    # deliverable) so the upstream work can be repaired
                    # without sending incomplete public content.
                    hold = partial_upstream_publish_issue(step, plan.plan_dag, steps_by_key)
                    if hold:
                        err = {
                            "type": UPSTREAM_PARTIAL_RESULT_ERROR_TYPE,
                            "message": hold["message"],
                            "failure": hold["failure"],
                        }
                        self._mark_failed(step, err)
                        chat_events.append({
                            "kind": "step_failed", "step": step,
                            "error": err, "will_retry": False,
                        })
                        continue
                    # Watchdog: a step the dispatcher keeps declining, or that
                    # no worker pass ever leased, must end somewhere visible
                    # instead of sitting PENDING forever.
                    escalation = undispatchable_step_failure(step, now=datetime.now(timezone.utc))
                    if escalation:
                        self._mark_failed(step, escalation)
                        chat_events.append({
                            "kind": "step_failed", "step": step,
                            "error": escalation, "will_retry": False,
                        })
                        continue
                    # Dispatcher will pick this up on next checkout.
                    # No state change here — step stays pending.

                elif step.kind in ("parallel_fanout", "gather", "code"):
                    # ``code`` used to fall into the dispatch branch above and
                    # sit pending forever: no worker advertises it, so the
                    # dispatcher could never lease it. Fail loudly instead —
                    # a visible error beats a plan that silently never moves.
                    err = {
                        "type": "NotImplemented",
                        "message": f"step kind {step.kind!r} not in Demo A v0 scope",
                    }
                    self._mark_failed(step, err)
                    chat_events.append({
                        "kind": "step_failed", "step": step,
                        "error": err, "will_retry": False,
                    })

                else:
                    err = {"type": "UnknownKind", "message": step.kind}
                    self._mark_failed(step, err)
                    chat_events.append({
                        "kind": "step_failed", "step": step,
                        "error": err, "will_retry": False,
                    })

            self._wake_waiting_coordinator_parents(steps)
            await db.flush()

            # Re-evaluate terminal after the inline transitions.
            steps = await self._all_steps(db, plan_id)
            task_coordinator_handoff = await self._handoff_task_coordinator_plan(
                db, plan, steps,
            )
            if task_coordinator_handoff is not None:
                inline_hitl_event = self._build_inline_hitl_event(plan, chat_events)
                await db.commit()
                await self._emit_task_event(inline_hitl_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started,
                    step_count=len(steps),
                    execution_mode=chat_mode,
                    chat_events=chat_events,
                    plan_done=None,
                    plan_started_at=None,
                    plan_completed_at=None,
                    plan_cost=None,
                    plan_error=None,
                    task_title=task_title,
                    locale=progress_locale,
                    step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
                )
                return task_coordinator_handoff
            terminal = self._terminal_summary(steps)
            if terminal == "failed":
                replanned = await self._maybe_replan(db, plan, steps)
                if replanned:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
            if terminal in ("completed", "failed"):
                if terminal == "completed":
                    replanned = await self._maybe_replan_for_missing_artifact(db, plan, steps)
                    if replanned:
                        await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                        return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event, supervisor_deferred = await self._finalize_for_cycle(
                    db, plan, terminal
                )
                if supervisor_deferred is not None:
                    return supervisor_deferred
                if plan.status == ExecutionPlanStatus.REPLANNED:
                    await self._commit_and_dispatch_replan(db, plan.task_id, plan.id)
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                await self._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan.id,
                    task_id=plan.task_id,
                    reason="plan_terminal",
                )
                await self._emit_task_event(task_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started, step_count=len(steps),
                    execution_mode=chat_mode, chat_events=chat_events,
                    plan_done=terminal,
                    plan_started_at=plan.started_at,
                    plan_completed_at=plan.completed_at,
                    plan_cost=(plan.cost_tracking or {}).get("usd"),
                    plan_error=plan.last_error,
                    task_title=task_title,
                    locale=progress_locale,
                    step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
                    task_status=_task_status_from_event(task_event),
                    task_issue=(
                        ((task_event or {}).get("payload") or {}).get("issue")
                        or ((task_event or {}).get("payload") or {}).get("prompt")
                    ),
                )
                return {"plan_id": plan_id, "status": terminal, "next_action": "stop"}

            # Decide re-enqueue cadence.
            if any(s.step_status == ExecutionStepStatus.WAITING_HUMAN for s in steps):
                # Plan is paused on operator input. Don't burn a cycle
                # slot — chat resolve_pending_action will wake us.
                next_action = "wait"
                delay = 0
            else:
                next_action = "schedule_self"
                any_pending = any(s.step_status == ExecutionStepStatus.PENDING for s in steps)
                any_running = any(s.step_status == ExecutionStepStatus.RUNNING for s in steps)
                if any_pending:
                    # A lease grant / transition wakes the cycle by itself;
                    # this is the safety net, not the trigger.
                    if task_coordinator_v3 and sleep_seconds > 0:
                        delay = max(1, min(
                            PENDING_POLL_SECONDS,
                            int(sleep_seconds + 0.999),
                        ))
                    else:
                        delay = max(int(sleep_seconds), PENDING_POLL_SECONDS)
                elif any_running:
                    delay = RUNNING_POLL_SECONDS
                else:
                    delay = max(int(sleep_seconds), CYCLE_TICK_SECONDS)

            result = {
                "plan_id": plan_id,
                "status": "running",
                "next_action": next_action,
                "delay_seconds": delay,
            }
            await self._stage_schedule_self(
                db,
                plan_id,
                result,
                reason="cycle_schedule_self",
            )
            inline_hitl_event = self._build_inline_hitl_event(plan, chat_events)
            # The next generation is committed atomically with every Step/
            # Plan mutation from this cycle. If this worker dies before its
            # outer delivery ACK, the intended timer is still durable.
            await db.commit()
            await self._emit_task_event(inline_hitl_event)

        # Chat announcements outside the DB session.
        await self._announce(
            chat_entity, chat_ws, chat_plan_id,
            task_id=chat_task_id,
            started=announce_started, step_count=len(steps),
            execution_mode=chat_mode, chat_events=chat_events,
            plan_done=None,
            plan_started_at=None, plan_completed_at=None,
            plan_cost=None, plan_error=None,
            task_title=task_title,
            locale=progress_locale,
            step_snapshots=self._snapshot_steps(steps, locale=progress_locale),
        )

        return result

    # ── State helpers ────────────────────────────────────────────────

    @staticmethod
    async def _stage_schedule_self(
        db: AsyncSession,
        plan_id: str,
        result: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Persist a requested next cycle in the same transaction as state.

        The Celery task may fail after ``run_cycle`` commits, so the returned
        delay cannot be the only copy of a timer. Replacing the current wake
        generation here makes broker/ACK failure a delivery concern rather
        than a lost Plan continuation.
        """

        if result.get("next_action") != "schedule_self":
            return
        from packages.core.plans.wakeup import stage_plan_cycle_wake

        wake = await stage_plan_cycle_wake(
            db,
            plan_id,
            delay_seconds=float(result.get("delay_seconds") or 0),
            reason=reason,
        )
        if wake is not None:
            result["continuation_staged"] = True

    @staticmethod
    def _sleep_seconds(params: dict) -> float:
        if COORDINATOR_CHILD_WAKE_AT_PARAM_KEY in params:
            target = _parse_iso_datetime(
                params.get(COORDINATOR_CHILD_WAKE_AT_PARAM_KEY)
            )
            if target is None:
                return 0.0
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        if "seconds" in params:
            return float(params["seconds"])
        if "until" in params:
            target = params["until"]
            if isinstance(target, str):
                target = datetime.fromisoformat(target)
            now = datetime.now(timezone.utc)
            return max(0.0, (target - now).total_seconds())
        return 0.0

    @staticmethod
    def _snapshot_steps(steps: list[ExecutionStep], *, locale: str = "en") -> list[dict]:
        """Snapshot step state for DAG rendering in chat/logs."""
        return [
            {
                "key": s.step_key,
                "kind": s.kind,
                "service_key": s.service_key,
                "provider": s.provider,
                "action_key": s.action_key,
                "capability_id": s.capability_id,
                "description": getattr(s, "description", None) or s.step_key.replace("_", " "),
                "depends_on": s.depends_on or [],
                "status": s.step_status,
                "result_summary": chat_notify.summarize_result_for_chat(s.result, max_chars=1200, locale=locale)
                    if s.result else None,
                "artifacts": chat_notify.extract_artifacts_for_chat(s.result)
                    if s.result else [],
                "error": {
                    "type": (s.error or {}).get("type", "unknown"),
                    "message": str((s.error or {}).get("message", ""))[:150],
                } if s.error else None,
            }
            for s in steps
        ]

    @staticmethod
    def _mark_done(step: ExecutionStep, result: Any, cost: Optional[dict]) -> None:
        step.step_status = ExecutionStepStatus.DONE.value
        # Explicit PlanStep contracts are bare payloads. Keep their native
        # JSON value (including null) so downstream whole-result refs see the
        # same value that passed dispatcher validation; schema-less and
        # envelope rows keep the historical value wrapper.
        from packages.core.contracts.task_output import output_contract_for_schema

        contract = output_contract_for_schema(step.expected_output_schema)
        step.result = (
            result
            if contract.preserves_native_payload
            else (result if isinstance(result, dict) else {"value": result})
        )
        if cost:
            step.cost = cost
        step.finished_at = datetime.now(timezone.utc)
        step.error = None
        step.runtime_checkpoint = None

    async def _preflight_external_effects(
        self,
        db: AsyncSession,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        user_id: str | None,
    ) -> bool:
        """Fail a fresh plan up front when an external effect cannot happen.

        Two deterministic checks, run once when a plan starts and every step
        is still pending: (1) the workspace's governance hard-blocks the
        effect — fail that step with the rule named and the fix attached;
        (2) no connected integration or paired Chrome can carry the effect —
        fail it with the setup links. Either way the remaining steps are
        skipped so the task fails on the blocker instead of spending credits
        on work it cannot finish (prod task FQ2881, 2026-09-14). Never lets an
        exception stop the cycle: pre-flight is a guard, not a gate keeper.
        """
        if not steps or any(s.step_status != ExecutionStepStatus.PENDING for s in steps):
            return False
        try:
            from packages.core.governance.hard_block_provenance import governance_denial_step_error
            from packages.core.governance.service import check_step_policy
            from packages.core.plans.external_effects import (
                external_effect_subject,
                step_external_effect,
            )
            from packages.core.services.external_effect_readiness import (
                external_effect_route_readiness,
            )

            for step in steps:
                effect = step_external_effect(step, plan.plan_dag)
                if not effect:
                    continue
                action_key, capability_id = external_effect_subject(step, effect)
                error: dict[str, Any] | None = None
                if plan.workspace_id:
                    decision = await check_step_policy(
                        db,
                        workspace_id=plan.workspace_id,
                        kind=step.kind,
                        action_key=action_key,
                        risk_level=step.risk_level or "high",
                        capability_id=capability_id,
                        task_id=plan.task_id,
                    )
                    if not decision.allowed and not decision.pause_for_hitl:
                        error = await governance_denial_step_error(
                            db,
                            workspace_id=plan.workspace_id,
                            action_key=action_key,
                            capability_id=capability_id,
                            matched_rule=decision.matched_rule,
                            reason=decision.reason,
                        )
                if error is None:
                    route = await external_effect_route_readiness(
                        db,
                        entity_id=plan.entity_id,
                        user_id=user_id,
                        effect=effect,
                        declared_provider=getattr(step, "provider", None),
                    )
                    if route is not None and not route.ready:
                        error = {
                            "type": NO_EXTERNAL_ROUTE_ERROR_TYPE,
                            "message": route.message,
                            "effect": effect,
                            "checked_providers": route.checked_providers,
                            "setup_links": route.setup_links,
                            "failure": {
                                "reason": route.message,
                                "blockers": [
                                    f"{provider}: {reason}" for provider, reason in route.reasons.items()
                                ] or [route.message],
                                "retryable": False,
                                "requires_human": True,
                            },
                        }
                if error is None:
                    continue
                self._mark_failed(step, error)
                now = datetime.now(timezone.utc)
                for other in steps:
                    if other is step or other.step_status != ExecutionStepStatus.PENDING:
                        continue
                    other.step_status = ExecutionStepStatus.SKIPPED.value
                    other.finished_at = now
                    other.error = {
                        "type": PREFLIGHT_SKIPPED_ERROR_TYPE,
                        "message": (
                            f"Not started: step {step.step_key!r} cannot run in this "
                            "workspace yet. Fix that first, then retry the task."
                        ),
                    }
                logger.info(
                    "plan %s pre-flight held step %s (%s)",
                    plan.id, step.step_key, error.get("type"),
                )
                return True
        except Exception:  # noqa: BLE001 — a guard must never stop the cycle
            logger.warning("plan %s pre-flight failed; continuing", plan.id, exc_info=True)
        return False

    @staticmethod
    def _mark_failed(step: ExecutionStep, error: dict) -> None:
        # PlanExecutor only marks failed for inline kinds (sleep/human
        # don't fail; ref errors always terminal). Worker-driven kinds
        # use Dispatcher.fail_lease which honours retries.
        step.step_status = ExecutionStepStatus.FAILED.value
        step.error = error
        step.finished_at = datetime.now(timezone.utc)
        if not any(
            isinstance(ref, dict) and ref.get("kind") == "tool_effect"
            for ref in (step.evidence_refs or [])
        ):
            step.runtime_checkpoint = None

    @staticmethod
    def _mark_waiting_human(step: ExecutionStep, prompt: Optional[str]) -> None:
        step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
        step.human_input_prompt = prompt

    # ── Reads ────────────────────────────────────────────────────────

    @staticmethod
    async def _cancel_orphaned_coordinator_children(
        db: AsyncSession,
        steps: list[ExecutionStep],
        *,
        now: datetime,
    ) -> bool:
        """Cancel delegated children only after their parent is truly terminal.

        A coordinator's ``delegate_to_service`` call creates the child RUNNING
        and settles it itself. A retryable parent lease expiry resets the parent
        to PENDING; its successor must keep and resume the child's durable
        transcript. Treating PENDING as orphaned discarded that checkpoint and
        made the whole coordinator unrecoverable after an ordinary worker loss.
        """
        if not steps:
            return False
        plan_ids = {step.plan_id for step in steps}
        if len(plan_ids) != 1:
            raise RuntimeError("coordinator orphan cleanup requires exactly one Plan")
        plan_id = next(iter(plan_ids))
        leases, locked_steps = await lock_plan_leases_then_steps(
            db,
            plan_id=plan_id,
        )
        steps[:] = locked_steps
        by_key = {s.step_key: s for s in locked_steps}
        orphan_ids: set[str] = set()
        for step in locked_steps:
            if not _is_coordinator_child(step):
                continue
            if step.step_status not in {
                ExecutionStepStatus.RUNNING.value,
                ExecutionStepStatus.PENDING.value,
                ExecutionStepStatus.WAITING_HUMAN.value,
                ExecutionStepStatus.PAUSED.value,
            }:
                continue
            parent = by_key.get(_coordinator_parent_key(step) or "")
            if parent is not None and parent.step_status in {
                ExecutionStepStatus.RUNNING.value,
                ExecutionStepStatus.PENDING.value,
                ExecutionStepStatus.WAITING_HUMAN.value,
                ExecutionStepStatus.PAUSED.value,
            }:
                continue
            orphan_ids.add(step.id)

        cancelled = revoke_leases_before_cancelling_steps(
            db,
            leases=leases,
            steps=locked_steps,
            step_ids=orphan_ids,
            reason="coordinator parent is terminal",
            now=now,
            clear_runtime_checkpoint=True,
        )
        for step in cancelled:
            logger.info(
                "plan %s: cancelled orphaned coordinator child %s (parent %s not running)",
                step.plan_id, step.step_key, _coordinator_parent_key(step),
            )
        return bool(cancelled)

    @staticmethod
    def _wake_waiting_coordinator_parents(steps: list[ExecutionStep]) -> bool:
        """Requeue a parked root only when its exact child is terminal."""

        by_id = {step.id: step for step in steps}
        changed = False
        terminal = {
            ExecutionStepStatus.DONE.value,
            ExecutionStepStatus.FAILED.value,
            ExecutionStepStatus.SKIPPED.value,
            ExecutionStepStatus.CANCELLED.value,
        }
        for parent in steps:
            if parent.step_status != ExecutionStepStatus.PAUSED.value:
                continue
            params = parent.params if isinstance(parent.params, dict) else {}
            if params.get(COORDINATOR_PARAM_KEY) is not True:
                continue
            waiting = params.get(COORDINATOR_WAITING_CHILD_PARAM_KEY)
            if not isinstance(waiting, dict):
                continue
            child = by_id.get(str(waiting.get("child_step_id") or ""))
            if child is None or child.step_status not in terminal:
                continue
            child_params = child.params if isinstance(child.params, dict) else {}
            if (
                child.plan_id != parent.plan_id
                or not _is_released_typed_coordinator_child(child)
                or _coordinator_parent_key(child) != parent.step_key
                or str(child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY) or "")
                != str(waiting.get("checklist_item_key") or "")
            ):
                continue
            # Keep the waiting marker until the resumed pending tool call has
            # replayed the exact terminal row and removed it transactionally.
            parent.step_status = ExecutionStepStatus.PENDING.value
            parent.current_lease_id = None
            changed = True
        return changed

    @staticmethod
    async def _enforce_plan_deadline(
        db: AsyncSession,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        workspace_settings: dict | None,
        now: datetime,
        chat_events: list[dict],
    ) -> bool:
        """Stop a RUNNING plan that outlived its runtime budget.

        Active leases are revoked and every open step (PENDING / RUNNING /
        WAITING_HUMAN) fails with ``PlanDeadlineExceeded``. A timeout is not
        evidence that a person can supply missing facts; the normal bounded
        failure path decides whether safe replanning is possible.
        Returns True when it acted.
        """
        if plan.status != ExecutionPlanStatus.RUNNING or not plan.started_at:
            return False
        open_steps = [
            s for s in steps
            if s.step_status in (
                ExecutionStepStatus.PENDING,
                ExecutionStepStatus.RUNNING,
                ExecutionStepStatus.WAITING_HUMAN,
            )
        ]
        if not open_steps:
            return False
        started = plan.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        budget = resolve_plan_max_runtime_seconds(
            workspace_settings, getattr(plan, "plan_dag", None),
        )
        if started + timedelta(seconds=budget) >= now:
            return False

        # Global mutation order for Task execution is Task -> Plan -> Lease ->
        # Step.  Approval resolution uses that same prefix before it revokes a
        # lease.  Taking these ownership locks now prevents the deadline path
        # from holding a lease while a concurrent approval holds the plan.
        if plan.task_id:
            from packages.core.models.task import Task

            await db.execute(
                select(Task)
                .where(Task.id == plan.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        locked_plan = (await db.execute(
            select(ExecutionPlan)
            .where(ExecutionPlan.id == plan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (
            locked_plan is None
            or locked_plan.status != ExecutionPlanStatus.RUNNING.value
            or not locked_plan.started_at
        ):
            return False
        locked_started = locked_plan.started_at
        if locked_started.tzinfo is None:
            locked_started = locked_started.replace(tzinfo=timezone.utc)
        if locked_started + timedelta(seconds=budget) >= now:
            return False
        plan = locked_plan

        elapsed_hours = (now - started).total_seconds() / 3600
        budget_hours = budget / 3600
        message = (
            f"The plan ran for {elapsed_hours:.1f}h, past its {budget_hours:.1f}h budget, "
            "and was stopped. Review what finished, then retry the task."
        )
        reason = f"plan {plan.id} exceeded its runtime budget of {int(budget)}s"
        try:
            from packages.core.dispatcher.service import Dispatcher

            revoke = getattr(Dispatcher(), "revoke_active_leases", None)
            if callable(revoke):
                await revoke(db, plan_id=plan.id, reason=reason)
            else:
                logger.warning(
                    "plan %s deadline: Dispatcher.revoke_active_leases unavailable; "
                    "open leases will expire on their own",
                    plan.id,
                )
        except Exception:  # noqa: BLE001 — the steps still fail below
            logger.warning(
                "plan %s deadline: lease revocation failed", plan.id, exc_info=True,
            )
        # Lease ownership is now closed. Lock/refresh the step rows afterwards
        # so a straggling worker's Lease -> Step report cannot form the inverse
        # half of a deadlock and cannot overwrite the deadline result.
        steps[:] = list((await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        open_steps = [
            step
            for step in steps
            if step.step_status
            in {
                ExecutionStepStatus.PENDING.value,
                ExecutionStepStatus.RUNNING.value,
                ExecutionStepStatus.WAITING_HUMAN.value,
            }
        ]
        for step in open_steps:
            error = {
                "type": PLAN_DEADLINE_ERROR_TYPE,
                "message": message,
                "budget_seconds": int(budget),
                "failure": {
                    "reason": message,
                    "retryable": False,
                    "requires_human": False,
                    "blockers": ["plan_deadline"],
                },
            }
            PlanExecutor._mark_failed(step, error)
            chat_events.append({
                "kind": "step_failed", "step": step,
                "error": error, "will_retry": False,
            })
        plan.last_error = {"type": PLAN_DEADLINE_ERROR_TYPE, "message": message}
        logger.warning(
            "plan %s exceeded its runtime budget (%.0fs); failed %d open step(s)",
            plan.id, budget, len(open_steps),
        )
        return True

    @staticmethod
    def _reset_step_for_rerun(
        step: ExecutionStep,
        *,
        preserve_result: bool = False,
    ) -> None:
        """Reset a step exactly the way a manual retry resets it."""
        reset_step_to_retry_ready(step)
        step.error = None
        if not preserve_result:
            step.result = None
        step.finished_at = None
        step.started_at = None
        step.attempt_count = 0
        step.human_input_prompt = None

    @staticmethod
    async def _try_claim_cycle(db: AsyncSession, plan_id: str) -> bool:
        """Avoid duplicate supervision and terminal writes in concurrent ticks."""
        from sqlalchemy import text
        from packages.core.constants.advisory_locks import AdvisoryLockNamespace

        if getattr(getattr(getattr(db, "bind", None), "dialect", None), "name", None) != "postgresql":
            return True
        return bool(await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:namespace, hashtext(:plan_id))"),
            {"namespace": int(AdvisoryLockNamespace.PLAN_EXECUTION_CYCLE), "plan_id": plan_id},
        ))

    @staticmethod
    async def _load_plan(db: AsyncSession, plan_id: str) -> Optional[ExecutionPlan]:
        return (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one_or_none()

    @staticmethod
    async def _lock_cycle_mutation_origin(
        db: AsyncSession,
        plan: ExecutionPlan,
    ) -> ExecutionPlan:
        """Lock Task -> Plan before this cycle can dirty either execution row."""

        if plan.task_id:
            from packages.core.models.task import Task

            task = (await db.execute(
                select(Task)
                .where(Task.id == plan.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if task is not None and (
                task.entity_id != plan.entity_id
                or task.workspace_id != plan.workspace_id
            ):
                raise RuntimeError(
                    f"plan {plan.id} crosses its task ownership boundary"
                )
            task_plans = list((await db.execute(
                select(ExecutionPlan)
                .where(
                    ExecutionPlan.task_id == plan.task_id,
                    ExecutionPlan.entity_id == plan.entity_id,
                )
                .order_by(ExecutionPlan.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all())
            locked = next((item for item in task_plans if item.id == plan.id), None)
            latest = max(
                task_plans,
                key=lambda item: (item.created_at, item.id),
                default=None,
            )
            active = [
                item
                for item in task_plans
                if item.status not in {
                    status.value for status in PLAN_TERMINAL_STATUSES
                }
            ]
            if (
                locked is None
                or latest is None
                or latest.id != plan.id
                or len(active) != 1
                or active[0].id != plan.id
            ):
                raise _SupersededTaskPlan(
                    f"plan {plan.id} is not the Task's unique latest active Plan"
                )
            from packages.core.dispatcher.effect_safety import (
                task_execution_effect_block_detail,
            )

            effect_block = await task_execution_effect_block_detail(
                db,
                plan.task_id,
                exclude_plan_id=plan.id,
            )
            if effect_block:
                raise _TaskEffectHistoryBlocked(effect_block)
        else:
            locked = (await db.execute(
                select(ExecutionPlan)
                .where(
                    ExecutionPlan.id == plan.id,
                    ExecutionPlan.entity_id == plan.entity_id,
                    ExecutionPlan.workspace_id == plan.workspace_id,
                    ExecutionPlan.task_id.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
        if locked is None:
            raise RuntimeError(f"plan {plan.id} ownership changed while locking")
        return locked

    @staticmethod
    async def _persist_superseded_task_plan(
        db: AsyncSession,
        *,
        plan_id: str,
        detail: str,
    ) -> PlanCycleWake | None:
        """Collapse duplicate Task Plans to one durable authoritative owner."""

        from packages.core.constants.execution import WorkLeaseStatus
        from packages.core.models.task import Task
        from packages.core.models.worker import WorkLease

        preview = await db.get(ExecutionPlan, plan_id)
        if preview is None or not preview.task_id:
            await db.rollback()
            return None
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == preview.task_id,
                Task.entity_id == preview.entity_id,
                Task.workspace_id == preview.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            await db.rollback()
            return None
        task_plans = list((await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.entity_id == task.entity_id,
            )
            .order_by(ExecutionPlan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        latest = max(
            task_plans,
            key=lambda candidate: (candidate.created_at, candidate.id),
            default=None,
        )
        if latest is None:
            await db.rollback()
            return None
        terminal = {status.value for status in PLAN_TERMINAL_STATUSES}
        stale = [
            candidate
            for candidate in task_plans
            if candidate.id != latest.id and candidate.status not in terminal
        ]
        stale_ids = {candidate.id for candidate in stale}
        leases = []
        steps = []
        if stale_ids:
            leases = list((await db.execute(
                select(WorkLease)
                .where(WorkLease.plan_id.in_(sorted(stale_ids)))
                .order_by(WorkLease.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all())
            steps = list((await db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.plan_id.in_(sorted(stale_ids)))
                .order_by(ExecutionStep.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalars().all())
        now = datetime.now(timezone.utc)
        error = {
            "type": "SupersededTaskPlan",
            "message": detail,
            "authoritative_plan_id": latest.id,
        }
        for candidate in stale:
            candidate.status = ExecutionPlanStatus.REPLANNED.value
            candidate.completed_at = now
            candidate.last_error = dict(error)
        for lease in leases:
            if lease.status in {
                WorkLeaseStatus.ACTIVE.value,
                WorkLeaseStatus.NEEDS_HUMAN.value,
            }:
                lease.status = WorkLeaseStatus.CANCELLED.value
                lease.credential_leases = []
                lease.execution_claim_id = None
                lease.error = dict(error)
        for step in steps:
            if step.step_status in {
                ExecutionStepStatus.PENDING.value,
                ExecutionStepStatus.RUNNING.value,
                ExecutionStepStatus.WAITING_HUMAN.value,
                ExecutionStepStatus.PAUSED.value,
            }:
                step.step_status = ExecutionStepStatus.CANCELLED.value
                step.current_lease_id = None
                step.error = dict(error)
                step.last_execution_error = dict(error)
                step.finished_at = now
        authoritative_wake = None
        if latest.status not in terminal:
            from packages.core.plans.wakeup import stage_plan_cycle_wake

            # Cancelling stale siblings and waking the surviving owner form
            # one durable boundary. A broker outage after this commit may add
            # latency, but cannot strand the authoritative Plan.
            authoritative_wake = await stage_plan_cycle_wake(
                db,
                latest.id,
                reason="superseded_plan_authoritative",
            )
        await db.commit()
        return authoritative_wake

    @staticmethod
    async def _persist_task_effect_history_block(
        db: AsyncSession,
        *,
        plan_id: str,
        detail: str,
    ) -> None:
        """Keep a cross-Plan replay fence without inventing a customer question."""

        from packages.core.models.task import Task
        from packages.core.models.worker import WorkLease
        from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl
        from packages.core.services.task_state_machine import apply_task_status_transition
        from packages.core.dispatcher.effect_safety import (
            ambiguous_effect_reconciliation_snapshot,
        )

        preview = await db.get(ExecutionPlan, plan_id)
        if preview is None or not preview.task_id:
            raise RuntimeError(detail)
        task = (await db.execute(
            select(Task)
            .where(
                Task.id == preview.task_id,
                Task.entity_id == preview.entity_id,
                Task.workspace_id == preview.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if task is None:
            raise RuntimeError(detail)
        task_plans = list((await db.execute(
            select(ExecutionPlan)
            .where(
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.entity_id == task.entity_id,
            )
            .order_by(ExecutionPlan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        target = next((candidate for candidate in task_plans if candidate.id == plan_id), None)
        if target is None:
            raise RuntimeError(detail)
        plan_ids = [candidate.id for candidate in task_plans]
        await db.execute(
            select(WorkLease)
            .where(WorkLease.plan_id.in_(plan_ids))
            .order_by(WorkLease.id)
            .with_for_update()
        )
        steps = list((await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id.in_(plan_ids))
            .order_by(ExecutionStep.plan_id, ExecutionStep.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        reconciliation = None
        reconciliation_plan_id: str | None = None
        for candidate in reversed(task_plans):
            if candidate.id == target.id:
                continue
            candidate_steps = [step for step in steps if step.plan_id == candidate.id]
            snapshot = await ambiguous_effect_reconciliation_snapshot(
                db,
                candidate_steps,
                plan_id=candidate.id,
            )
            if snapshot is not None:
                reconciliation = snapshot
                reconciliation_plan_id = candidate.id
                break
        needs_reconciliation = bool(
            isinstance(reconciliation, dict) and reconciliation.get("effects")
        )
        target.status = ExecutionPlanStatus.NEEDS_ATTENTION.value
        target.completed_at = None
        target.last_error = {
            "type": "TaskEffectHistoryBlocked",
            "message": detail,
            "failure": {
                "reason": detail,
                "retryable": False,
                "requires_human": needs_reconciliation,
                "blockers": [
                    "external_action_reconciliation"
                    if needs_reconciliation else "plan_effect_identity"
                ],
            },
            **({"reconciliation": reconciliation} if reconciliation else {}),
        }
        await apply_task_status_transition(
            task,
            (
                TaskStatus.WAITING_ON_CUSTOMER.value
                if needs_reconciliation else TaskStatus.BLOCKED.value
            ),
            db=db,
            actor_kind=TaskActor.SYSTEM.value,
        )
        recovery_message = (
            await ensure_task_recovery_hitl(
                db,
                task,
                plan_id=reconciliation_plan_id or target.id,
                prompt="Execution paused to protect a previously recorded external action.",
                issue=detail,
                reconciliation=reconciliation,
            )
            if needs_reconciliation else None
        )
        if (
            recovery_message is not None
            and isinstance(recovery_message.pending_action, dict)
            and recovery_message.pending_action.get("approval_request_id")
        ):
            preserve_owner = next(
                (
                    candidate
                    for candidate in task_plans
                    if candidate.id == (reconciliation_plan_id or target.id)
                ),
                target,
            )
            state = dict(preserve_owner.dispatcher_state or {})
            state["task_recovery_request_id"] = str(
                recovery_message.pending_action["approval_request_id"]
            )
            preserve_owner.dispatcher_state = state
        await db.commit()

    @staticmethod
    async def _load_task_title(db: AsyncSession, plan: ExecutionPlan) -> Optional[str]:
        if not plan.task_id:
            return None
        try:
            from packages.core.models.task import Task
            return (await db.execute(
                select(Task.title).where(Task.id == plan.task_id)
            )).scalar_one_or_none()
        except Exception:
            return None

    @staticmethod
    async def _all_steps(db: AsyncSession, plan_id: str) -> list[ExecutionStep]:
        return list((await db.execute(
            select(ExecutionStep)
            .where(ExecutionStep.plan_id == plan_id)
            .order_by(ExecutionStep.created_at)
        )).scalars().all())

    @staticmethod
    async def _handoff_task_coordinator_plan(
        db: AsyncSession,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
    ) -> dict[str, Any] | None:
        """Return a settled V3 Plan to its Task loop, not the V2 Supervisor.

        The Task coordinator owns the decision to release another step,
        repair/replan, or submit.  This cycle still owns Task -> Plan before
        locking the run; a later exact Step release stages the next Plan wake.
        An active Step remains with the ordinary Dispatcher/Plan-cycle path.
        """

        from packages.core.plans.coordinator import (
            TASK_COORDINATOR_PLAN_VERSION,
            TASK_COORDINATOR_PLAN_VERSION_KEY,
            TASK_COORDINATOR_RUN_ID_KEY,
        )

        state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
        if state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION:
            return None
        if not plan.task_id or not state.get(TASK_COORDINATOR_RUN_ID_KEY):
            raise RuntimeError("Task coordinator Plan has no Task/run identity")
        if any(
            step.step_status in (ExecutionStepStatus.PENDING, ExecutionStepStatus.RUNNING)
            for step in steps
        ):
            return None
        if any(step.step_status == ExecutionStepStatus.WAITING_HUMAN for step in steps):
            return {
                "plan_id": plan.id,
                "status": "waiting_human",
                "next_action": "wait",
            }

        from packages.core.models.task_coordinator import (
            TaskCoordinatorPhase,
            TaskCoordinatorRun,
        )
        from packages.core.services.task_coordinator_wakeup import (
            stage_task_coordinator_wake,
        )

        run = (await db.execute(
            select(TaskCoordinatorRun)
            .where(TaskCoordinatorRun.id == state[TASK_COORDINATOR_RUN_ID_KEY])
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (
            run is None
            or run.task_id != plan.task_id
            or run.entity_id != plan.entity_id
            or run.workspace_id != plan.workspace_id
            or run.current_plan_id != plan.id
        ):
            raise RuntimeError("Task coordinator Plan/run lineage changed")
        if (
            run.phase == TaskCoordinatorPhase.WAITING_CHILD.value
            and (
                run.wake_generation is None
                or run.wake_generation == run.claim_generation
            )
        ):
            # A very fast child may finish before the parent has ACKed the
            # wake that released it. Rotate that old generation rather than
            # letting its late ACK erase the only terminal-child wake. A
            # successor generation already pending is left untouched.
            await stage_task_coordinator_wake(
                db,
                task_id=run.task_id,
                reason="plan_step_settled",
            )
        return {
            "plan_id": plan.id,
            "status": "task_coordinator_handoff",
            "next_action": "stop",
        }

    @staticmethod
    def _pick_runnable(
        steps: list[ExecutionStep], by_key: dict[str, ExecutionStep],
    ) -> list[ExecutionStep]:
        """Pending steps whose deps are done. ``running`` steps mean a
        worker is mid-flight — leave them. ``waiting_human`` steps are
        excluded too (they wake via chat resolve)."""
        runnable: list[ExecutionStep] = []
        for s in steps:
            if s.step_status != ExecutionStepStatus.PENDING:
                continue
            if _is_coordinator_child(s) and not _is_released_typed_coordinator_child(s):
                # Legacy children are driven inside the parent call. V2 typed
                # children join the ordinary executor only after exact release.
                continue
            deps = s.depends_on or []
            if not all(by_key[d].step_status == ExecutionStepStatus.DONE for d in deps if d in by_key):
                # Mark blocked-by-failure dependents as skipped so the
                # plan can terminate. Otherwise just wait.
                if any(by_key[d].step_status in (ExecutionStepStatus.FAILED, ExecutionStepStatus.CANCELLED, ExecutionStepStatus.SKIPPED)
                       for d in deps if d in by_key):
                    s.step_status = ExecutionStepStatus.SKIPPED.value
                    s.finished_at = datetime.now(timezone.utc)
                continue
            runnable.append(s)
        return runnable

    @staticmethod
    def _collect_prior_results(steps: list[ExecutionStep]) -> dict[str, Any]:
        from packages.core.contracts.task_output import output_contract_for_schema

        prior: dict[str, Any] = {}
        for step in steps:
            if step.step_status != ExecutionStepStatus.DONE:
                continue
            # A nullable bare contract's successful result is intentionally
            # ``None``; omitting it would turn a valid downstream ref into a
            # spurious ReferenceError. Legacy/no-schema null rows remain
            # omitted for backwards-compatible semantics.
            contract = output_contract_for_schema(step.expected_output_schema)
            # Provider/action schemas can be unmarked legacy rows but are
            # still native payload contracts at runtime.  Preserve an
            # explicit JSON ``null`` for those rows just like marked PlanStep
            # payloads; only schema-less/envelope rows retain the historical
            # omission behavior.
            if step.result is not None or contract.preserves_native_payload:
                prior[step.step_key] = step.result
        return prior

    @staticmethod
    def _terminal_summary(steps: list[ExecutionStep]) -> Optional[str]:
        # Coordinator children are durable implementation details of their
        # authored parent step.  A coordinator may recover from one failed
        # child by delegating a replacement, so historical child state must
        # not independently keep the Plan open or turn a successful parent
        # into a failed Plan.  The parent step owns the logical outcome.
        logical_steps = [step for step in steps if not _is_coordinator_child(step)]
        any_pending = any(
            s.step_status == ExecutionStepStatus.PENDING for s in logical_steps
        )
        any_running = any(
            s.step_status == ExecutionStepStatus.RUNNING for s in logical_steps
        )
        any_waiting = any(
            s.step_status == ExecutionStepStatus.WAITING_HUMAN for s in logical_steps
        )
        any_paused = any(
            s.step_status == ExecutionStepStatus.PAUSED for s in logical_steps
        )
        any_failed = any(
            s.step_status == ExecutionStepStatus.FAILED for s in logical_steps
        )

        if any_pending or any_running or any_waiting or any_paused:
            return None
        if any_failed:
            return "failed"
        return "completed"

    MAX_REPLANS = 2

    @staticmethod
    async def _cleanup_terminal_approvals_after_commit(
        db: AsyncSession,
        *,
        plan_id: str,
        task_id: str | None,
        reason: str,
    ) -> None:
        """Close terminal approval rows/cards without holding execution locks.

        This is intentionally a post-commit transaction.  The interactive
        approval path owns HitlRequest before it locks the Task/Plan
        continuation, while execution/finalization owns Task/Plan first.
        Mixing cleanup into finalization therefore creates a real lock-order
        cycle.  A later terminal cycle retries this idempotent cleanup, so a
        transient failure remains repairable without blocking the outcome.
        """

        try:
            from packages.core.governance.approvals import resolve_origin_requests
            from packages.core.governance.service import resolve_stale_hitl_cards
            from packages.core.models.task import Task
            from packages.core.services.task_state_machine import TERMINAL_STATUSES

            terminal_task_id: str | None = None
            preserve_request_ids: set[str] = set()
            if task_id:
                task_status = (await db.execute(
                    select(Task.status).where(Task.id == task_id)
                )).scalar_one_or_none()
                if task_status in TERMINAL_STATUSES:
                    terminal_task_id = task_id
                else:
                    dispatcher_state = (await db.execute(
                        select(ExecutionPlan.dispatcher_state).where(
                            ExecutionPlan.id == plan_id
                        )
                    )).scalar_one_or_none()
                    if isinstance(dispatcher_state, dict):
                        recovery_request_id = str(
                            dispatcher_state.get("task_recovery_request_id") or ""
                        ).strip()
                        if recovery_request_id:
                            preserve_request_ids.add(recovery_request_id)

            await resolve_origin_requests(
                db,
                plan_id=plan_id,
                task_id=terminal_task_id,
                reason=reason,
                exclude_request_ids=preserve_request_ids,
            )
            await resolve_stale_hitl_cards(
                db,
                plan_id=plan_id,
                task_id=terminal_task_id,
                reason=reason,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "post-commit approval cleanup failed for plan %s; caller "
                "must retry or leave a durable cleanup backstop",
                plan_id,
            )
            raise

    @staticmethod
    async def _commit_and_dispatch_replan(
        db: AsyncSession,
        task_id: str | None,
        plan_id: str | None = None,
    ) -> bool:
        """Atomically persist a replan and its durable planning intent.

        The old implementation committed the replan first and then published
        directly to the broker.  A process death in that gap left the Task
        permanently ``in_progress`` with no replacement planner delivery.
        The Task outbox row now commits with the Plan/Task changes; its
        publisher owns the later broker handoff.
        """
        if not task_id:
            await db.commit()
            if plan_id:
                await PlanExecutor._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan_id,
                    task_id=None,
                    reason="plan_replanned",
                )
            return False

        from packages.core.constants.task import TaskRuntimeKind
        from packages.core.models.task import Task
        from packages.core.services.task_execution import (
            resolve_task_execution,
            stage_task_execution_intent,
        )
        from packages.core.services.task_service import add_task_log
        from packages.core.services.task_state_machine import (
            TERMINAL_STATUSES,
            apply_task_status_transition,
        )

        staged = False
        terminal_task = False
        staging_error: Exception | None = None
        try:
            # A savepoint lets a deterministic admission failure become a
            # visible recovery state without discarding the already-built
            # replan context in the surrounding transaction.  Successful
            # intent insertion still commits atomically with that context.
            async with db.begin_nested():
                task_for_dispatch = (await db.execute(
                    select(Task)
                    .where(Task.id == task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
                if task_for_dispatch is None:
                    raise RuntimeError("replacement Task no longer exists")
                if task_for_dispatch.status in TERMINAL_STATUSES:
                    terminal_task = True
                else:
                    execution = await resolve_task_execution(db, task_for_dispatch)
                    if (
                        execution.runtime_kind
                        is not TaskRuntimeKind.COORDINATED_PLAN
                    ):
                        raise RuntimeError(
                            "replacement Task no longer has an executable "
                            "coordinated Workspace service scope"
                        )
                    await stage_task_execution_intent(
                        db,
                        task_for_dispatch,
                        execution,
                    )
                    staged = True
        except Exception as exc:  # noqa: BLE001 - persisted recovery below
            staging_error = exc

        if staging_error is None:
            await db.commit()
            if plan_id:
                await PlanExecutor._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan_id,
                    task_id=task_id,
                    reason=(
                        "replan_dispatch_terminal"
                        if terminal_task
                        else "plan_replanned"
                    ),
                )
            return staged

        logger.warning(
            "Replan intent staging failed for task %s",
            task_id,
            exc_info=(
                type(staging_error),
                staging_error,
                staging_error.__traceback__,
            ),
        )
        try:
            plan_snapshot = None
            if plan_id:
                plan_snapshot = (await db.execute(
                    select(ExecutionPlan).where(
                        ExecutionPlan.id == plan_id,
                        ExecutionPlan.task_id == task_id,
                    )
                )).scalar_one_or_none()
            # Task-bound recovery always owns Task before Plan. This is the
            # same order used by Chat and manual Retry.
            task_stmt = select(Task).where(Task.id == task_id)
            if plan_snapshot is not None:
                task_stmt = task_stmt.where(
                    Task.entity_id == plan_snapshot.entity_id,
                )
            task = (await db.execute(
                task_stmt
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if task is not None and task.status in TERMINAL_STATUSES:
                await db.commit()
                if plan_id:
                    await PlanExecutor._cleanup_terminal_approvals_after_commit(
                        db,
                        plan_id=plan_id,
                        task_id=task_id,
                        reason="replan_dispatch_terminal",
                    )
                return False
            plan = None
            if plan_id:
                plan_stmt = select(ExecutionPlan).where(
                    ExecutionPlan.id == plan_id,
                    ExecutionPlan.task_id == task_id,
                )
                if task is not None:
                    plan_stmt = plan_stmt.where(
                        ExecutionPlan.entity_id == task.entity_id,
                    )
                plan = (await db.execute(
                    plan_stmt
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
            error_message = (
                str(staging_error)[:1000]
                or staging_error.__class__.__name__
            )
            if plan is not None:
                plan.last_error = {
                    "type": "ReplanDispatchFailed",
                    "message": error_message,
                }
            if task is not None:
                details = dict(task.details or {})
                context = details.get("_replan_context")
                context = dict(context) if isinstance(context, dict) else {}
                context["dispatch_status"] = "failed"
                context["dispatch_error"] = error_message
                context["dispatch_plan_id"] = plan_id
                details["_replan_context"] = context
                task.details = details
                await apply_task_status_transition(
                    task,
                    TaskStatus.BLOCKED.value,
                    db=db,
                    actor_kind=TaskActor.SYSTEM.value,
                )
                if plan is not None:
                    locale = await resolve_plan_response_locale(db, plan)
                else:
                    from packages.core.services.task_requester_identity import (
                        TaskRequesterIdentityError,
                        resolve_task_execution_user_id,
                    )
                    from packages.core.services.task_service import _task_log_locale

                    try:
                        user_id = await resolve_task_execution_user_id(db, task)
                    except TaskRequesterIdentityError:
                        user_id = None
                    locale = await _task_log_locale(db, user_id)
                await add_task_log(
                    db,
                    task.id,
                    TaskLogType.AI_EXECUTION_FAILED,
                    t("plan.log.replacement_queue_failed", locale=locale),
                    actor=TaskActor.SYSTEM,
                    created_by="system",
                    metadata={
                        "plan_id": plan_id,
                        "error": error_message,
                        "mode": "replan_dispatch",
                        "requires_human": False,
                        "recovery_kind": "internal_dispatch_failure",
                    },
                )
            await db.commit()
            if plan_id:
                await PlanExecutor._cleanup_terminal_approvals_after_commit(
                    db,
                    plan_id=plan_id,
                    task_id=task_id,
                    reason="replan_dispatch_failed",
                )
        except Exception:
            await db.rollback()
            logger.error(
                "Could not persist replan dispatch failure for task %s",
                task_id,
                exc_info=True,
            )
        return False

    @staticmethod
    async def _maybe_replan_for_missing_artifact(
        db: AsyncSession,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
    ) -> bool:
        """Retry artifact-producing tasks before asking the user for help."""
        if not plan.task_id:
            return False
        from packages.core.models.task import Task

        task = (await db.execute(
            select(Task).where(Task.id == plan.task_id)
        )).scalar_one_or_none()
        issue = _missing_artifact_issue(task, steps)
        if not issue:
            return False
        logger.info(
            "Plan %s completed without required artifact evidence; attempting replan",
            plan.id,
        )
        return await PlanExecutor._maybe_replan(
            db,
            plan,
            steps,
            reason="missing_artifact",
            issue=issue,
        )

    @staticmethod
    async def _maybe_replan(
        db: AsyncSession,
        plan: ExecutionPlan,
        steps: list[ExecutionStep],
        *,
        reason: str = "step_failure",
        issue: str | None = None,
    ) -> bool:
        """Decide whether to replan or truly stop after an actionable issue.

        Checks:
          1. Replan budget not exhausted (max 2 replans per task)
          2. Failure is actionable (not a credit/permission issue)

        If replanning: records the replacement context and returns True.
        Caller commits that state and dispatches the next planning cycle;
        it should NOT finalize the current plan as "failed" — instead mark
        it "replanned".

        Returns False if replanning is not possible/advisable.
        """
        if not plan.task_id:
            return False

        deadline_failure = next((
            step.error for step in steps
            if step.step_status == ExecutionStepStatus.FAILED
            and isinstance(step.error, dict)
            and step.error.get("type") == PLAN_DEADLINE_ERROR_TYPE
        ), None)
        if deadline_failure is not None:
            # The Plan runtime cap is a hard stop. A revoked worker may have
            # an unknown effect; do not create another execution identity.
            plan.last_error = dict(deadline_failure)
            return False

        if any(
            step.step_status == ExecutionStepStatus.FAILED
            and (step.error or {}).get("type") == "RequiredSkillBlocked"
            for step in steps
        ):
            # The next Strategist review can arrange missing prerequisites;
            # replaying this same Task cannot supply a missing upstream artifact.
            return False

        file_recovery_failure = _internal_file_recovery_failure(steps)
        if file_recovery_failure:
            # The system already exhausted exact same-Step recovery. A new
            # Plan or customer confirmation cannot reconstruct its lost state.
            plan.last_error = dict(file_recovery_failure)
            return False

        from packages.core.dispatcher.effect_safety import (
            ambiguous_effect_reconciliation_snapshot,
            validated_new_plan_effect_block_detail,
        )

        effect_block = await validated_new_plan_effect_block_detail(db, steps)
        if effect_block:
            logger.info("Plan %s will not auto-replan: %s", plan.id, effect_block)
            reconciliation = await ambiguous_effect_reconciliation_snapshot(
                db, steps, plan_id=plan.id,
            )
            outcome_unknown = bool(
                isinstance(reconciliation, dict) and reconciliation.get("effects")
            )
            plan.last_error = {
                "type": (
                    "ActionOutcomeReconciliationRequired"
                    if outcome_unknown else "PlanReplacementWouldRepeatEffects"
                ),
                "message": effect_block,
                "failure": {
                    "reason": effect_block,
                    "retryable": False,
                    "requires_human": outcome_unknown,
                    "blockers": [
                        "external_action_reconciliation"
                        if outcome_unknown else "plan_effect_identity"
                    ],
                },
                "reconciliation": reconciliation,
            }
            return False

        # Count prior plans for this task
        prior_count = (await db.execute(
            select(ExecutionPlan.id).where(
                ExecutionPlan.task_id == plan.task_id,
                ExecutionPlan.id != plan.id,
                # Only attempts that ended in failure spend the budget: a
                # completed or cancelled earlier plan is not a failed replan.
                ExecutionPlan.status.in_((ExecutionPlanStatus.FAILED, ExecutionPlanStatus.REPLANNED,)),
            )
        )).scalars().all()
        if len(prior_count) >= PlanExecutor.MAX_REPLANS:
            logger.info("Replan budget exhausted for task %s (%d prior plans)", plan.task_id, len(prior_count))
            return False

        # A new plan cannot grant a denied policy or connect a missing route.
        # Keep these host-typed blockers with the customer instead of spending
        # a replan attempt and leaving the Task in progress.
        failed_steps = [s for s in steps if s.step_status == ExecutionStepStatus.FAILED]
        for fs in failed_steps:
            err_type = (fs.error or {}).get("type", "")
            if err_type in (
                "CreditExhaustedError", "PermissionError", "AuthenticationError",
                "GovernancePolicy", NO_EXTERNAL_ROUTE_ERROR_TYPE,
            ):
                return False

        # Build failure context for the planner
        failure_context = []
        if issue:
            failure_context.append({
                "step_key": "supervisor",
                "kind": "supervisor",
                "error": {
                    "type": "MissingArtifactEvidence" if reason == "missing_artifact" else "SupervisorNeedsReplan",
                    "message": issue,
                },
                "params_summary": None,
            })
        for fs in failed_steps:
            failure_context.append({
                "step_key": fs.step_key,
                "kind": fs.kind,
                "action_key": getattr(fs, "action_key", None),
                "capability_id": getattr(fs, "capability_id", None),
                "requires_approval": bool(getattr(fs, "requires_approval", False)),
                "error": fs.error,
                "params_summary": str(fs.params)[:300] if fs.params else None,
            })

        # Collect what succeeded, with artifact handles the planner can
        # actually reuse instead of regenerating.
        succeeded = _succeeded_step_contexts(steps)

        try:
            from packages.core.models.task import Task
            task = (await db.execute(
                select(Task).where(Task.id == plan.task_id)
            )).scalar_one_or_none()
            if not task:
                return False

            # Append replan context to task details so planner sees it
            details = dict(task.details or {})
            prior_replan_context = details.get("_replan_context")
            prior_approval_constraints = (
                prior_replan_context.get("approval_constraints")
                if isinstance(prior_replan_context, dict)
                else None
            )
            succeeded = _merged_replan_succeeded_steps(
                prior_replan_context.get("succeeded_steps")
                if isinstance(prior_replan_context, dict) else None,
                succeeded,
            )
            details["_replan_context"] = {
                "prior_plan_id": plan.id,
                "reason": reason,
                "issue": issue,
                "failed_steps": failure_context,
                "succeeded_steps": succeeded,
                "attempt": len(prior_count) + 1,
            }
            # A human denial/change request is not an ordinary tool failure.
            # The replacement plan may change the approach, but it must not
            # silently turn the same rejected/reviewed operation into an
            # un-gated step. The planner consumes the prose/error context;
            # planner.py consumes this structural boundary after normalizing
            # planner-authored approval guesses.
            approval_constraints = [
                dict(row)
                for row in (prior_approval_constraints or [])
                if isinstance(row, dict)
            ]
            for fs in failed_steps:
                step_error = fs.error if isinstance(fs.error, dict) else {}
                if step_error.get("type") not in {
                    "UserDeniedApproval",
                    "UserRequestedChanges",
                }:
                    continue
                constraint = {
                    "step_key": fs.step_key,
                    "kind": fs.kind,
                    "provider": getattr(fs, "provider", None),
                    "action_key": getattr(fs, "action_key", None),
                    "capability_id": getattr(fs, "capability_id", None),
                    "integration_id": getattr(fs, "integration_id", None),
                    "params_fingerprint": approval_args_hash(fs.params or {}),
                    "target_fingerprint": approval_stable_target_hash(fs.params or {}),
                    "decision": step_error.get("human_decision"),
                }
                if constraint not in approval_constraints:
                    approval_constraints.append(constraint)
            if approval_constraints:
                details["_replan_context"]["approval_constraints"] = approval_constraints
            if reason == "missing_artifact":
                details["_replan_context"]["artifact_recovery"] = {
                    "default_action": "materialize_saved_workspace_file",
                    "save_location_policy": (
                        "Use the current workspace's default artifact folder. "
                        "Do not ask the user for a path unless the task explicitly "
                        "says not to save a file."
                    ),
                    "required_evidence": [
                        "fs_path",
                        "document_id",
                        "file_url",
                        "image_url",
                        "video_url",
                        "files",
                    ],
                }
            task.details = details

            # Mark current plan as replanned (not failed)
            plan.status = ExecutionPlanStatus.REPLANNED.value
            plan.completed_at = datetime.now(timezone.utc)
            # Approval/card cleanup is intentionally deferred until the caller
            # commits this terminal plan state.  See
            # ``_cleanup_terminal_approvals_after_commit`` for the lock-order
            # invariant.
            await db.flush()

            logger.info(
                "Replanning task %s (attempt %d, reason=%s) — %d steps failed",
                plan.task_id, len(prior_count) + 1, reason, len(failed_steps),
            )
            return True
        except Exception:
            logger.warning("Replan attempt failed for plan %s", plan.id, exc_info=True)
            return False

    @staticmethod
    async def _log_supervisor_verdict(
        db: AsyncSession, task, plan: ExecutionPlan,
        decision: SupervisorDecision, *, note: str = "",
    ) -> None:
        """Record every supervisor decision on the task, with its evidence.

        Task 01KWRR5VGHYHQD3A116TZ8ET0W ended as failed with seven task_logs,
        every one of them reporting success — the transition came from
        ``apply_task_status_transition`` alone, which writes no task_log, so
        the only record of WHY was a single verdict word that was never
        stored. Now the decision itself is the record: the verdict, the
        evidence behind it (the model must cite a step result; deterministic
        gates state their finding), which mechanism produced it, and what
        the code did with it. Best-effort: a logging failure must never
        block the status transition it explains.
        """
        try:
            from packages.core.services.task_service import add_task_log

            steps = list((await db.execute(
                select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
                .order_by(ExecutionStep.created_at)
            )).scalars().all())
            done = [s for s in steps if s.step_status == ExecutionStepStatus.DONE]
            failed = [s for s in steps if s.step_status == ExecutionStepStatus.FAILED]

            locale = await resolve_plan_response_locale(db, plan)
            source_label = t(f"plan.log.source.{decision.source.value}", locale=locale)
            lines = [t(
                "plan.log.verdict", locale=locale,
                verdict=t(f"plan.verdict.{decision.verdict.value}", locale=locale),
                source=source_label,
            )]
            if decision.evidence:
                lines.append(t("plan.log.evidence", locale=locale, evidence=decision.evidence))
            if decision.step_key:
                lines.append(t("plan.log.step", locale=locale, step=decision.step_key))

            if failed:
                names = ", ".join(s.step_key for s in failed[:5])
                lines.append(t("plan.log.failed_steps", locale=locale, count=len(failed), names=names))
                if done:
                    lines.append(t("plan.log.prior_completed_steps", locale=locale, count=len(done)))
            elif done and decision.verdict in (
                SupervisorVerdict.FAILED,
                SupervisorVerdict.NEEDS_REPLAN,
                SupervisorVerdict.CANCELLED,
                SupervisorVerdict.BLOCKED,
            ):
                lines.append(
                    t("plan.log.overridden_success", locale=locale, count=len(done))
                )
            if note:
                lines.append(note)

            await add_task_log(
                db, task.id, SUPERVISOR_VERDICT_LOG_TYPE, "\n".join(lines),
                actor=TaskActor.SUPERVISOR,
                created_by="AI Supervisor",
                metadata={
                    "verdict": decision.verdict.value,
                    "evidence": decision.evidence,
                    "source": decision.source.value,
                    "step_key": decision.step_key,
                    "plan_id": plan.id,
                    "done_count": len(done),
                    "failed_count": len(failed),
                },
            )
        except Exception:
            logger.warning(
                "supervisor-verdict log write failed for plan %s (status transition still applies)",
                plan.id, exc_info=True,
            )

    @staticmethod
    async def _apply_supervisor_step_retry(
        db: AsyncSession, task, plan: ExecutionPlan, decision: SupervisorDecision,
    ) -> bool:
        """Send one step back for a re-run instead of finalizing the task.

        The named step was validated against the plan's real steps at parse
        time; here it is validated again against the database, and against
        its once-per-plan budget — the SUPERVISOR_STEP_RETRY_FLAG in the
        step's params. Without that budget the supervisor could retry the
        same step against the same result forever (the shape of the provider
        approval loop, in a new place).

        On success the step is reset exactly the way a manual retry resets
        it, the plan goes back to running, and a fresh executor cycle is
        scheduled. Returns False when the retry cannot apply, so the caller
        downgrades the decision rather than silently dropping it.
        """
        if not decision.step_key:
            return False
        step = (await db.execute(
            select(ExecutionStep).where(
                ExecutionStep.plan_id == plan.id,
                ExecutionStep.step_key == decision.step_key,
            )
        )).scalar_one_or_none()
        if step is None or _is_coordinator_child(step):
            # A delegated child is settled by its coordinator, never re-run alone.
            return False
        siblings = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.created_at)
        )).scalars().all())
        owned_children = [
            sibling
            for sibling in siblings
            if _is_coordinator_child(sibling)
            and _coordinator_parent_key(sibling) == step.step_key
            and sibling.step_status in {
                ExecutionStepStatus.FAILED.value,
                ExecutionStepStatus.SKIPPED.value,
                ExecutionStepStatus.PAUSED.value,
                ExecutionStepStatus.CANCELLED.value,
            }
        ]
        from packages.core.dispatcher.effect_safety import (
            ambiguous_effect_reconciliation_snapshot,
            retry_effect_block_detail,
        )

        effect_block = await retry_effect_block_detail(db, [step, *owned_children])
        if effect_block:
            # A model verdict is not authority to repeat an uncertain external
            # action. Leave the durable row untouched for human reconciliation.
            plan.last_error = {
                "type": "ActionOutcomeReconciliationRequired",
                "message": effect_block,
                "reconciliation": await ambiguous_effect_reconciliation_snapshot(
                    db,
                    [step, *owned_children],
                    plan_id=plan.id,
                ),
            }
            return False
        params = dict(step.params or {})
        if params.get(SUPERVISOR_STEP_RETRY_FLAG):
            return False
        params[SUPERVISOR_STEP_RETRY_FLAG] = True
        if decision.evidence:
            # The worker renders this as guidance for the re-run.
            params[SUPERVISOR_FEEDBACK_PARAM_KEY] = decision.evidence
        step.params = params  # reassignment marks the JSON column dirty
        PlanExecutor._reset_step_for_rerun(step)
        for child in owned_children:
            # Preserve result/checkpoint/evidence. The parent releases this
            # same stable row again; the child cannot enter the Dispatcher
            # queue until that exact server-owned transition happens.
            PlanExecutor._reset_step_for_rerun(child, preserve_result=True)

        # Dependents that never ran because this step's earlier result held
        # them (skipped, or failed at the partial-upstream boundary) come
        # back with it — the same rule as a manual retry.
        reset_never_ran_downstream_steps(
            siblings,
            retried_step_keys={step.step_key},
            reset_step=PlanExecutor._reset_step_for_rerun,
        )

        plan.status = ExecutionPlanStatus.RUNNING.value
        plan.completed_at = None
        plan.last_error = None

        await PlanExecutor._log_supervisor_verdict(
            db, task, plan, decision,
            note=t("plan.log.step_retried", locale=await resolve_plan_response_locale(db, plan), step=decision.step_key),
        )

        from packages.core.plans.wakeup import stage_plan_cycle_wake

        dispatched = await stage_plan_cycle_wake(
            db,
            plan.id,
            reason="supervisor_step_retry",
        ) is not None
        logger.info(
            "Supervisor step retry applied: plan=%s step=%s dispatched=%s",
            plan.id, decision.step_key, dispatched,
        )
        return True

    @staticmethod
    async def _supervise_outcome(
        db: AsyncSession, plan: ExecutionPlan, plan_status: str,
    ) -> SupervisorDecision:
        """Lightweight supervisor: reviews all step results after plan
        finishes and decides the task ticket status.

        Returns a SupervisorDecision — verdict, evidence, and which
        mechanism produced it. Deterministic gates run first and state their
        own findings as evidence; the model is asked only when no gate
        fires, and must cite the step result that justifies its verdict.
        """
        task, plan, task_plans, leases, steps = await _lock_supervisor_review_state(
            db, plan
        )
        # A coordinator's child rows remain available to evidence/HITL gates,
        # but they are not independent logical plan steps.  Counts and the
        # model-facing review must describe the authored DAG only; otherwise
        # one successful child can mask a failed coordinator parent.
        logical_steps = [step for step in steps if not _is_coordinator_child(step)]

        locale = await resolve_plan_response_locale(db, plan)
        done_count = sum(
            1 for s in logical_steps if s.step_status == ExecutionStepStatus.DONE
        )
        failed_count = sum(
            1 for s in logical_steps if s.step_status == ExecutionStepStatus.FAILED
        )
        skipped_count = sum(
            1 for s in logical_steps if s.step_status == ExecutionStepStatus.SKIPPED
        )

        task_title = task.title if task else "Unknown"
        # The supervisor reviews a task a handful of times over its whole
        # life — the full description costs nothing next to the wrong
        # verdict a truncated one produces.
        task_desc = (task.description or "")[:2000] if task else ""

        from packages.core.contracts.task_output import task_expected_output_json_schema
        gate_decision = await evaluate_task_completion_gates(
            db,
            task=task,
            plan=plan,
            steps=steps,
            plan_status=plan_status,
            locale=locale,
        )
        if gate_decision is not None:
            return gate_decision

        # Freeze the provider request while Task -> Plans -> Leases -> Steps
        # are locked. The durable generation claim is committed before model
        # I/O, then the same state is locked and fingerprinted again before a
        # verdict can change Task/Plan state.
        try:
            step_infos = _supervisor_step_infos(plan, logical_steps)
            acceptance_contract = (
                ((plan.plan_dag or {}).get("metadata") or {}).get(
                    "acceptance_contract"
                )
            )
            actual_result = (
                _supervisor_actual_result(
                    logical_steps,
                    acceptance_contract=acceptance_contract,
                )
                if isinstance(acceptance_contract, dict)
                else None
            )
            if actual_result is not None:
                evidence_keys = {
                    str(item.get("step_key") or "")
                    for item in actual_result.get("steps", [])
                    if isinstance(item, dict)
                }
                step_infos = [
                    info for info in step_infos
                    if str(info.get("key") or "") in evidence_keys
                ]
            plan_rationale = str(
                ((plan.plan_dag or {}).get("metadata") or {}).get("rationale") or ""
            )

            # The supervisor oversees the TASK, not one plan: earlier
            # attempts and its own earlier reviews are part of the picture.
            prior_attempts: list[dict] = []
            prior_reviews: list[dict] = []
            if plan.task_id:
                prior_plans = list((await db.execute(
                    select(ExecutionPlan).where(
                        ExecutionPlan.task_id == plan.task_id,
                        ExecutionPlan.id != plan.id,
                    ).order_by(ExecutionPlan.created_at)
                )).scalars().all())[-5:]
                if prior_plans:
                    prior_steps = list((await db.execute(
                        select(ExecutionStep).where(
                            ExecutionStep.plan_id.in_([p.id for p in prior_plans])
                        ).order_by(ExecutionStep.created_at)
                    )).scalars().all())
                    steps_by_plan: dict[str, list] = {}
                    for prior_step in prior_steps:
                        steps_by_plan.setdefault(prior_step.plan_id, []).append(prior_step)
                    prior_attempts = _supervisor_attempt_infos(prior_plans, steps_by_plan)

                from packages.core.models.task import TaskLog
                verdict_logs = list((await db.execute(
                    select(TaskLog).where(
                        TaskLog.task_id == plan.task_id,
                        TaskLog.log_type == SUPERVISOR_VERDICT_LOG_TYPE,
                    ).order_by(TaskLog.created_at)
                )).scalars().all())
                prior_reviews = _supervisor_review_infos(verdict_logs)

            # A step may be offered for a supervisor re-run once per plan;
            # a supervisor retrying the same step against the same result
            # forever is a loop, not a review.
            retryable_step_keys = [
                s.step_key for s in logical_steps
                if s.step_status in (ExecutionStepStatus.DONE, ExecutionStepStatus.FAILED)
                and not (s.params or {}).get(SUPERVISOR_STEP_RETRY_FLAG)
            ]
            model_request = json.loads(json.dumps(dict(
                task_title=task_title,
                task_description=task_desc,
                task_output_contract=(
                    task_expected_output_json_schema(getattr(task, "expected_output", None))
                    if task is not None
                    else None
                ),
                acceptance_contract=acceptance_contract,
                actual_result=actual_result,
                done_count=done_count,
                failed_count=failed_count,
                skipped_count=skipped_count,
                steps=step_infos,
                entity_id=plan.entity_id,
                workspace_id=getattr(plan, "workspace_id", None),
                retryable_step_keys=retryable_step_keys,
                plan_rationale=plan_rationale,
                is_replan=bool(getattr(plan, "parent_plan_id", None)),
                prior_attempts=prior_attempts,
                prior_reviews=prior_reviews,
            ), ensure_ascii=False, default=str))
        except Exception:
            logger.warning(
                "Supervisor input freeze failed for plan %s, falling back to plan_status",
                plan.id,
                exc_info=True,
            )
            return SupervisorDecision(
                verdict=SupervisorVerdict(plan_status),
                evidence=t("plan.log.supervisor_unavailable", locale=locale),
                source=SupervisorDecisionSource.FALLBACK,
            )

        generation = await _claim_supervisor_review_generation(
            db,
            task=task,
            plan=plan,
            task_plans=task_plans,
            leases=leases,
            candidate_status=getattr(plan_status, "value", plan_status),
            locale=locale,
        )
        await db.commit()

        decision: SupervisorDecision | None = None
        try:
            # Bounded provider I/O runs with no database transaction or row
            # lock held. ``model_request`` is a detached JSON snapshot.
            completion = await asyncio.wait_for(
                runtime_execute_plan_supervisor_completion(**model_request),
                timeout=SUPERVISOR_LLM_TIMEOUT_SECONDS,
            )
            decision = runtime_parse_plan_supervisor_decision(
                completion.content, retryable_step_keys=retryable_step_keys,
            )
            if decision is not None:
                if decision.verdict is SupervisorVerdict.NEEDS_HUMAN:
                    # A review model can identify missing work, but cannot
                    # mint an input/approval request by choosing a verdict.
                    # Real waits were persisted before this terminal review;
                    # unknown external effects were caught by the host gate.
                    decision = decision.downgraded(
                        SupervisorVerdict.NEEDS_REPLAN,
                        "no durable input or approval request was recorded",
                    )
                decision = _demote_replan_on_completed_plan(
                    decision, plan_status, logical_steps
                )
        except Exception:
            logger.warning("Supervisor LLM call failed for plan %s, falling back to plan_status", plan.id, exc_info=True)

        if decision is None:
            decision = SupervisorDecision(
                verdict=SupervisorVerdict(plan_status),
                evidence=t("plan.log.supervisor_unavailable", locale=locale),
                source=SupervisorDecisionSource.FALLBACK,
            )
        await _revalidate_supervisor_review_generation(db, generation, plan)
        logger.info(
            "Supervisor verdict for plan %s: %s (%s)",
            plan.id,
            decision.verdict.value,
            decision.evidence[:120],
        )
        return decision

    @staticmethod
    async def _resolved_config_versions(
        db: AsyncSession, plan: ExecutionPlan, task,
    ) -> Optional[dict]:
        """Best-effort ``{"agent_revision": N, "skill_revision": N}`` for the
        config that produced this plan (M11 ledger stamping).

        Cheapest correct join: the LAST done step that resolved an agent
        (``ExecutionStep.resolved_agent_id``) — falling back to
        ``task.agent_id`` — then one lookup of that ``Agent.revision``.

        Skill revision has no cheap source: ``ExecutionStep`` carries no
        skill column (skills are invoked in-loop through ``invoke_skill``,
        not modelled as steps). It is stamped only when a step explicitly
        recorded a ``skill_id`` in its params/result; otherwise the stamp
        carries ``agent_revision`` alone.

        Never raises — a stamping failure must not break finalize.
        """
        try:
            steps = list((await db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.plan_id == plan.id)
                .order_by(ExecutionStep.created_at)
            )).scalars().all())

            agent_id = next(
                (
                    s.resolved_agent_id
                    for s in reversed(steps)
                    if s.step_status == ExecutionStepStatus.DONE and s.resolved_agent_id
                ),
                None,
            ) or next(
                (s.resolved_agent_id for s in reversed(steps) if s.resolved_agent_id),
                None,
            ) or getattr(task, "agent_id", None)

            skill_id = None
            for s in reversed(steps):
                for blob in (s.params, s.result):
                    if isinstance(blob, dict) and blob.get("skill_id"):
                        skill_id = str(blob["skill_id"])
                        break
                if skill_id:
                    break

            return await resolve_config_versions(db, agent_id=agent_id, skill_id=skill_id)
        except Exception:
            logger.debug("config_versions stamping skipped", exc_info=True)
            return None

    @staticmethod
    async def _finalize_for_cycle(
        db: AsyncSession,
        plan: ExecutionPlan,
        status: str,
    ) -> tuple[Optional[dict], dict[str, Any] | None]:
        """Apply a verdict or reschedule when another generation must win."""

        try:
            return await PlanExecutor._finalize(db, plan, status), None
        except _SupervisorReviewInProgress as exc:
            await db.rollback()
            return None, {
                "plan_id": plan.id,
                "status": "supervisor_review_in_progress",
                "next_action": "schedule_self",
                "delay_seconds": CYCLE_TICK_SECONDS,
                "detail": str(exc),
            }
        except _StaleSupervisorVerdict as exc:
            await db.rollback()
            return None, {
                "plan_id": plan.id,
                "status": "supervisor_verdict_stale",
                "observed_plan_status": exc.plan_status,
                "next_action": "schedule_self",
                "delay_seconds": CYCLE_TICK_SECONDS,
                "detail": str(exc),
            }

    @staticmethod
    async def _finalize(
        db: AsyncSession, plan: ExecutionPlan, status: str,
        *, server_decision: SupervisorDecision | None = None,
    ) -> Optional[dict]:
        locale = await resolve_plan_response_locale(db, plan)
        completed_at = datetime.now(timezone.utc)
        task_event: Optional[dict] = None
        replan_dispatch_task_id: str | None = None

        # Approval/card cleanup must run only after the terminal Task/Plan
        # transaction commits; otherwise approval decisions and finalization
        # acquire HitlRequest and Task/Plan in opposite orders.

        # Auto-update the parent task status + aggregate output.
        # A lightweight supervisor reviews the step results and decides
        # the final task status: completed, failed, or needs_replan.
        if plan.task_id:
            from packages.core.models.task import Task
            from packages.core.services.task_service import add_task_log
            from packages.core.services.task_state_machine import TERMINAL_STATUSES, apply_task_status_transition
            attention_issue: Optional[str] = None
            supervisor_decision: Optional[SupervisorDecision] = None
            result = await db.execute(
                select(Task).where(Task.id == plan.task_id)
            )
            task = result.scalar_one_or_none()
            # Ledger (M11): stamp the execution-config revisions that actually
            # produced this run onto the terminal execution_* event, so
            # outcome analysis can attribute the result to the exact agent /
            # skill content. Best-effort — never blocks finalize.
            config_versions = await PlanExecutor._resolved_config_versions(db, plan, task)
            if task and task.status in _PLAN_FINALIZABLE_TASK_STATUSES:
                # A V3 Task coordinator submits only after a separate,
                # locked server acceptance gate. Its own loop has already
                # inspected the Step results; do not start a second model
                # controller during projection. Legacy Plans retain the
                # existing Supervisor path unchanged.
                decision = server_decision or await PlanExecutor._supervise_outcome(
                    db, plan, status,
                )
                # Do not expose a terminal Plan before model review. The
                # supervisor commits only its generation claim before I/O,
                # then returns with canonical locks reacquired and its
                # fingerprint revalidated. A concurrent cancel therefore
                # wins instead of being overwritten by this verdict.
                plan.status = status
                plan.completed_at = completed_at

                if decision.verdict is SupervisorVerdict.RETRY_STEP:
                    # The supervisor may send ONE step back for a re-run
                    # instead of failing the whole task. If it applies, the
                    # plan is running again and nothing here is terminal.
                    if await PlanExecutor._apply_supervisor_step_retry(db, task, plan, decision):
                        return None
                    plan_error = (
                        plan.last_error if isinstance(plan.last_error, dict) else {}
                    )
                    if plan_error.get("type") == "ActionOutcomeReconciliationRequired":
                        retry_steps = list((await db.execute(
                            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
                            .order_by(ExecutionStep.created_at)
                        )).scalars().all())
                        reconciliation = await _fresh_unknown_effect_reconciliation(
                            db, plan, retry_steps,
                        )
                        if reconciliation:
                            plan.last_error = {**plan_error, "reconciliation": reconciliation}
                            decision = decision.downgraded(
                                SupervisorVerdict.NEEDS_HUMAN,
                                str(plan_error.get("message") or ""),
                            )
                        else:
                            decision = decision.downgraded(
                                SupervisorVerdict.NEEDS_REPLAN,
                                t("plan.log.retry_unavailable", locale=locale),
                            )
                    else:
                        decision = decision.downgraded(
                            SupervisorVerdict.NEEDS_REPLAN,
                            t("plan.log.retry_unavailable", locale=locale),
                        )

                # A model/gate replan verdict is produced inside finalization,
                # after the normal pre-finalize failure/artifact checks.  It
                # therefore must spend the real replan budget here instead of
                # being treated as proof that the budget was already spent.
                supervisor_replanned = False
                note = ""
                if decision.verdict is SupervisorVerdict.NEEDS_REPLAN:
                    replan_steps = list((await db.execute(
                        select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
                        .order_by(ExecutionStep.created_at)
                    )).scalars().all())
                    supervisor_replanned = await PlanExecutor._maybe_replan(
                        db,
                        plan,
                        replan_steps,
                        reason="supervisor_review",
                        issue=decision.evidence,
                    )
                    if supervisor_replanned:
                        replan_dispatch_task_id = plan.task_id
                    plan_error = (
                        plan.last_error if isinstance(plan.last_error, dict) else {}
                    )
                    if (
                        not supervisor_replanned
                        and plan_error.get("type")
                        == "ActionOutcomeReconciliationRequired"
                    ):
                        reconciliation = await _fresh_unknown_effect_reconciliation(
                            db, plan, replan_steps,
                        )
                        if reconciliation:
                            plan.last_error = {**plan_error, "reconciliation": reconciliation}
                            decision = decision.downgraded(
                                SupervisorVerdict.NEEDS_HUMAN,
                                str(plan_error.get("message") or ""),
                            )
                            note = str(plan_error.get("message") or "")
                        else:
                            note = t("plan.log.replan_failed", locale=locale)
                    else:
                        note = t(
                            "plan.log.replan_started"
                            if supervisor_replanned
                            else "plan.log.replan_failed",
                            locale=locale,
                        )

                # Every verdict says why — evidence from the model or the
                # gate, plus what the code did with it.
                await PlanExecutor._log_supervisor_verdict(db, task, plan, decision, note=note)
                supervisor_decision = decision

                if decision.verdict is SupervisorVerdict.COMPLETED:
                    await apply_task_status_transition(
                        task, "completed", db=db, config_versions=config_versions,
                    )
                elif decision.verdict is SupervisorVerdict.NEEDS_REPLAN:
                    if not supervisor_replanned:
                        await apply_task_status_transition(
                            task, "failed", db=db, config_versions=config_versions,
                        )
                elif decision.verdict is SupervisorVerdict.NEEDS_HUMAN:
                    await apply_task_status_transition(
                        task, "waiting_on_customer", db=db,
                        config_versions=config_versions,
                    )
                    # Notify workspace chat so user sees the HITL request
                    try:
                        steps_for_issue = list((await db.execute(
                            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
                            .order_by(ExecutionStep.created_at)
                        )).scalars().all())
                        artifact_issue = _missing_artifact_issue(task, steps_for_issue)
                        structured_issue = _structured_blocking_issue(task, steps_for_issue)
                        failed_steps = [s for s in (await db.execute(
                            select(ExecutionStep).where(
                                ExecutionStep.plan_id == plan.id,
                                ExecutionStep.step_status == ExecutionStepStatus.FAILED,
                            )
                        )).scalars().all()]
                        message = _hitl_request_message(
                            task, steps_for_issue,
                            structured_issue=structured_issue,
                            artifact_issue=artifact_issue,
                            failed_steps=failed_steps,
                            locale=locale,
                        )
                        attention_issue = (
                            decision.evidence
                            if (
                                isinstance(plan.last_error, dict)
                                and plan.last_error.get("type")
                                == "ActionOutcomeReconciliationRequired"
                            )
                            else _failed_step_issue(failed_steps)
                            or structured_issue
                            or artifact_issue
                            or decision.evidence
                            or message
                        )
                        attention_issue = _user_facing_step_issue(attention_issue, steps_for_issue)
                        await add_task_log(db, task.id, TaskLogType.AI_HITL_REQUESTED,
                            message,
                            actor=TaskActor.SUPERVISOR,
                            created_by="AI Supervisor",
                            metadata={
                                "verdict": decision.verdict.value,
                                "evidence": decision.evidence,
                                "source": decision.source.value,
                                "plan_id": plan.id,
                                "artifact_required": bool(artifact_issue),
                                "structured_blocker": bool(structured_issue),
                            })
                        from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

                        recovery_message = await ensure_task_recovery_hitl(
                            db,
                            task,
                            plan_id=plan.id,
                            prompt=message,
                            issue=attention_issue,
                            reconciliation=(
                                plan.last_error.get("reconciliation")
                                if isinstance(plan.last_error, dict)
                                else None
                            ),
                        )
                        if (
                            recovery_message is not None
                            and isinstance(recovery_message.pending_action, dict)
                            and recovery_message.pending_action.get("approval_request_id")
                        ):
                            dispatcher_state = dict(plan.dispatcher_state or {})
                            dispatcher_state["task_recovery_request_id"] = str(
                                recovery_message.pending_action["approval_request_id"]
                            )
                            plan.dispatcher_state = dispatcher_state
                    except Exception:
                        logger.warning(
                            "task %s: failed to project recovery HITL into workspace chat",
                            task.id,
                            exc_info=True,
                        )
                else:
                    # FAILED / CANCELLED / BLOCKED — the decision is a member
                    # of a closed enum, so there is no "unknown" branch left:
                    # _supervise_outcome already folds an unparseable reply
                    # into a FALLBACK decision on the plan's own status.
                    await apply_task_status_transition(
                        task, decision.verdict.value, db=db,
                        config_versions=config_versions,
                    )
            else:
                plan.status = status
                plan.completed_at = completed_at

            # Aggregate step results into task.actual_output so the
            # Strategist can learn from what the task actually produced.
            if task:
                steps = list((await db.execute(
                    select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
                    .order_by(ExecutionStep.created_at)
                )).scalars().all())
                # Build step summaries with file/document references
                step_summaries = []
                all_files: list[dict] = []
                from packages.core.contracts.task_output import (
                    OutputContractKind,
                    output_contract_for_schema,
                )

                previous_actual_output = (
                    task.actual_output if isinstance(task.actual_output, dict) else {}
                )
                previous_supervisor_verdict = previous_actual_output.get(
                    "supervisor_verdict"
                )
                supervisor_accepted = (
                    supervisor_decision is not None
                    and supervisor_decision.verdict is SupervisorVerdict.COMPLETED
                ) or (
                    supervisor_decision is None
                    and task.status == TaskStatus.COMPLETED
                    and previous_actual_output.get("plan_id") == plan.id
                    and previous_supervisor_verdict
                    == SupervisorVerdict.COMPLETED.value
                )
                accepted_task_output = _accepted_terminal_output(
                    task, plan, steps, supervisor_accepted=supervisor_accepted,
                )
                from packages.core.contracts.artifact_delivery import task_artifact_refs_for_output
                verified_files = task_artifact_refs_for_output(plan, steps)

                for s in steps:
                    entry: dict = {
                        "key": s.step_key,
                        "kind": s.kind,
                        "status": s.step_status,
                    }
                    if s.result and isinstance(s.result, dict):
                        entry["result_summary"] = _step_result_summary(s.result)
                        # Capture file/document references from step results
                        refs = _dedupe_task_artifact_refs(
                            verified_files.get(str(s.id), []) if verified_files is not None
                            else _artifact_refs_from_result(s.result, step_key=s.step_key, output_schema=artifact_result_schema(s)),
                            entity_id=plan.entity_id,
                        )
                        if refs:
                            entry["files"] = refs
                            all_files.extend(refs)
                        if verified_files is None and s.result.get("document_id"):
                            entry["document_id"] = s.result["document_id"]
                        if verified_files is None and s.result.get("fs_path"):
                            entry["fs_path"] = s.result["fs_path"]
                    elif s.result:
                        entry["result_summary"] = str(s.result)[:500]
                    if accepted_task_output is not None and s.step_key == accepted_task_output[0]:
                        entry["data"] = accepted_task_output[1]
                    if s.error:
                        entry["error"] = {
                            "type": s.error.get("type", "unknown"),
                            "message": str(s.error.get("message", ""))[
                                :1200 if s.error.get("type") == "RequiredSkillBlocked" else 300
                            ],
                        }
                    step_summaries.append(entry)

                actual_output = {
                    "plan_id": plan.id,
                    "plan_status": status,
                    "steps": step_summaries,
                    "files": _dedupe_task_artifact_refs(
                        all_files,
                        entity_id=plan.entity_id,
                    ) if all_files else None,
                }
                if accepted_task_output is not None:
                    # Preserve the exact machine-readable deliverable.  The
                    # old aggregate kept only a 500-char summary and URLs,
                    # which made a structurally correct worker result disappear
                    # before downstream tasks or the UI could consume it.
                    actual_output["result"] = accepted_task_output[1]
                    actual_output["result_step_key"] = accepted_task_output[0]
                    actual_output["result_contract_source"] = accepted_task_output[2]
                terminal_text_summary = _accepted_terminal_text_summary(
                    task, steps, supervisor_accepted=supervisor_accepted,
                )
                if terminal_text_summary:
                    actual_output["summary"] = terminal_text_summary
                if supervisor_decision is not None:
                    actual_output["supervisor_verdict"] = supervisor_decision.verdict.value
                    actual_output["supervisor_evidence"] = supervisor_decision.evidence
                    if supervisor_decision.verdict is SupervisorVerdict.NEEDS_HUMAN:
                        actual_output["needs_input"] = True
                task.actual_output = actual_output
                # Ledger (M1): one artifact_created per produced artifact ref.
                from packages.core.ledger.adapters import record_task_artifacts
                await record_task_artifacts(db, task, actual_output.get("files") or [])
                try:
                    from packages.core.models.workspace import Workspace
                    from packages.core.services.workspace_state_files import refresh_workspace_state_files

                    workspace_id = plan.workspace_id or task.workspace_id
                    if workspace_id:
                        workspace = (await db.execute(
                            select(Workspace).where(
                                Workspace.id == workspace_id,
                                Workspace.entity_id == plan.entity_id,
                                Workspace.deleted_at.is_(None),
                            )
                        )).scalar_one_or_none()
                        if workspace is not None:
                            await refresh_workspace_state_files(db, workspace)
                except Exception:
                    logger.debug("PlanExecutor: workspace state/file cache refresh skipped", exc_info=True)
                try:
                    resolved_agent_ids = {
                        str(s.resolved_agent_id)
                        for s in steps
                        if getattr(s, "resolved_agent_id", None)
                    }
                    learning_agent_id = task.agent_id
                    if not learning_agent_id and len(resolved_agent_ids) == 1:
                        learning_agent_id = next(iter(resolved_agent_ids))
                    await runtime_record_plan_executor_task_evidence(
                        db,
                        entity_id=plan.entity_id,
                        workspace_id=plan.workspace_id or task.workspace_id,
                        task_id=task.id,
                        plan_id=plan.id,
                        task_status=task.status,
                        plan_status=status,
                        task_title=task.title,
                        task_description=task.description or "",
                        owner_service_key=task.owner_service_key,
                        delegate_service_keys=task.delegate_service_keys or [],
                        agent_id=learning_agent_id,
                        steps=steps,
                        actual_output=task.actual_output or {},
                        cost_tracking=plan.cost_tracking or {},
                        started_at=plan.started_at,
                        completed_at=plan.completed_at,
                    )
                except Exception:
                    logger.debug("PlanExecutor: runtime evidence recording skipped", exc_info=True)

            # Log plan completion with DAG visualization to task
            if task:
                duration = None
                if plan.started_at and plan.completed_at:
                    duration = (plan.completed_at - plan.started_at).total_seconds()
                cost_usd = (plan.cost_tracking or {}).get("usd")
                icon = "✓" if status == ExecutionPlanStatus.COMPLETED else "✗"
                status_key = "plan.status.replanned" if status == ExecutionPlanStatus.REPLANNED else f"task.status.{status}"
                msg = t("plan.log.terminal", locale=locale, icon=icon, status=t(status_key, locale=locale))
                if duration is not None:
                    msg += t("plan.log.duration", locale=locale, seconds=f"{duration:.1f}")
                if cost_usd:
                    msg += f" · ${cost_usd:.4f}"
                # Append DAG rendering
                from packages.core.workspace_chat.notifiers import _render_dag
                step_snaps = PlanExecutor._snapshot_steps(steps, locale=locale)
                if step_snaps:
                    msg += "\n\n" + _render_dag(step_snaps, entity_id=plan.entity_id or "", locale=locale)
                try:
                    failed_steps_for_meta = [s for s in steps if s.step_status in (ExecutionStepStatus.FAILED, ExecutionStepStatus.SKIPPED, ExecutionStepStatus.CANCELLED,)]
                    first_error = next((s.error for s in failed_steps_for_meta if s.error), None)
                    await add_task_log(db, task.id,
                        plan_terminal_log_type(status), msg,
                        actor=TaskActor.SYSTEM,
                        created_by="system",
                        metadata={
                            "plan_id": plan.id,
                            "duration_s": duration,
                            "cost_usd": cost_usd,
                            "step_ids": [s.id for s in steps],
                            "failed_step_ids": [s.id for s in failed_steps_for_meta],
                            "error_type": (first_error or {}).get("type"),
                            "error_message": (first_error or {}).get("message"),
                        })
                except Exception:
                    pass  # best-effort

                # Post a human-readable summary of what the task produced.
                #
                # The plan reaching ``completed`` is not the same claim as the
                # TASK being done: a step can report done while saying, in its
                # own text, that it delivered nothing. That is what the
                # supervisor is for, and it had already ruled NEEDS_HUMAN on
                # task 01KZ8A43NZSZCYNTD46A2G8C24 — in the same transaction,
                # microseconds earlier — when this block posted "## Task
                # Completed" over the top of it. The thread then said both
                # "Task Completed" and "This task stopped before it finished".
                #
                # So the headline follows the supervisor's verdict, not the
                # plan's mechanical status. The deliverables are still posted
                # when a verdict withheld completion — the work that did happen
                # is worth reading — but under a heading that does not contradict
                # the card directly above it.
                accepted = (
                    supervisor_decision is None
                    or supervisor_decision.verdict is SupervisorVerdict.COMPLETED
                )
                if status == ExecutionPlanStatus.COMPLETED and steps:
                    # Collect final deliverables from step results
                    deliverables = []
                    for s in steps:
                        if s.step_status == ExecutionStepStatus.DONE and s.result:
                            text = ""
                            if isinstance(s.result, dict):
                                text = step_result_output_text(s.result)
                                if (
                                    output_contract_for_schema(s.expected_output_schema).kind
                                    is OutputContractKind.TASK_ENVELOPE
                                ):
                                    outputs = s.result.get("outputs")
                                    if isinstance(outputs, dict) and "data" in outputs:
                                        text = json.dumps(
                                            outputs["data"],
                                            ensure_ascii=False,
                                            indent=2,
                                            default=str,
                                        )
                            if isinstance(text, str) and text.strip():
                                step_label = getattr(s, "description", None) or s.step_key.replace("_", " ").title()
                                deliverables.append(f"### {step_label}\n\n{text.strip()}")
                    if deliverables:
                        heading = (
                            t("plan.log.completed_heading" if accepted else "plan.log.partial_heading", locale=locale)
                        )
                        summary = f"{heading}\n\n" + "\n\n---\n\n".join(deliverables)
                        try:
                            # The deliverables came from this task's agent;
                            # the executor only assembled them.
                            from packages.core.services.task_service import (
                                agent_log_authorship,
                            )

                            author, author_meta, author_actor = await agent_log_authorship(
                                db, task.agent_id,
                            )
                            await add_task_log(db, task.id,
                                TaskLogType.COMMENT, summary,
                                actor=author_actor,
                                created_by=author,
                                metadata={"auto_summary": True, **(author_meta or {})})
                        except Exception:
                            pass

                event_type = None
                event_steps = steps
                if task.status == TaskStatus.COMPLETED:
                    event_type = "task.succeeded"
                    event_steps = [s for s in steps if s.step_status == ExecutionStepStatus.DONE]
                elif task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED,):
                    event_type = "task.failed"
                    event_steps = [s for s in steps if s.step_status in (ExecutionStepStatus.FAILED, ExecutionStepStatus.SKIPPED, ExecutionStepStatus.CANCELLED,)]
                elif task.status == TaskStatus.WAITING_ON_CUSTOMER:
                    event_type = "task.hitl_requested"
                    event_steps = [s for s in steps if s.step_status in (ExecutionStepStatus.WAITING_HUMAN, ExecutionStepStatus.FAILED,)]

                if event_type:
                    first_error = next((s.error for s in event_steps if s.error), None)
                    first_prompt = next((s.human_input_prompt for s in event_steps if s.human_input_prompt), None)
                    task_event = {
                        "entity_id": plan.entity_id,
                        "event_type": event_type,
                        "payload": {
                            "task_id": task.id,
                            "title": task.title,
                            "workspace_id": plan.workspace_id,
                            "plan_id": plan.id,
                            "plan_status": status,
                            "task_status": task.status,
                            "step_ids": [s.id for s in event_steps],
                            "error_type": (first_error or {}).get("type"),
                            "error_message": (first_error or {}).get("message"),
                            "prompt": first_prompt or attention_issue,
                            "issue": attention_issue,
                        },
                    }

                if task.status in TERMINAL_STATUSES:
                    try:
                        from packages.core.services.workspace_operation_service import check_work_batch_completion

                        await check_work_batch_completion(
                            db,
                            task,
                            trigger_source="plans.executor.finalize",
                        )
                    except Exception:
                        logger.warning(
                            "plan %s task %s: failed to evaluate workspace work batch completion",
                            plan.id,
                            task.id,
                            exc_info=True,
                        )

        else:
            plan.status = status
            plan.completed_at = completed_at

        if replan_dispatch_task_id:
            await PlanExecutor._commit_and_dispatch_replan(
                db,
                replan_dispatch_task_id,
                plan.id,
            )
        return task_event

    @staticmethod
    def _build_inline_hitl_event(plan: ExecutionPlan, chat_events: list[dict]) -> Optional[dict]:
        if not plan.task_id:
            return None
        hitl_events = [evt for evt in chat_events if evt.get("kind") == "step_needs_human"]
        if not hitl_events:
            return None
        steps = [evt["step"] for evt in hitl_events]
        return {
            "entity_id": plan.entity_id,
            "event_type": "task.hitl_requested",
            "payload": {
                "task_id": plan.task_id,
                "workspace_id": plan.workspace_id,
                "plan_id": plan.id,
                "plan_status": plan.status,
                "task_status": "in_progress",
                "step_ids": [s.id for s in steps],
                "prompt": hitl_events[0].get("prompt") or steps[0].human_input_prompt,
            },
        }

    @staticmethod
    async def _emit_task_event(task_event: Optional[dict]) -> None:
        if not task_event:
            return
        try:
            runtime_emit_plan_executor_task_event(task_event)
        except Exception:
            logger.debug("PlanExecutor: task event emit failed", exc_info=True)
        payload = task_event.get("payload") or {}
        task_id = payload.get("task_id")
        if task_id:
            from packages.core.services.realtime import broadcast_task_runtime_update

            await broadcast_task_runtime_update(
                task_event.get("entity_id") or "",
                task_id=str(task_id),
                workspace_id=(
                    str(payload["workspace_id"])
                    if payload.get("workspace_id")
                    else None
                ),
                plan_id=str(payload["plan_id"]) if payload.get("plan_id") else None,
                status=str(payload["task_status"]) if payload.get("task_status") else None,
                title=str(payload["title"]) if payload.get("title") else None,
                event=str(task_event.get("event_type") or "runtime_updated"),
            )

    @staticmethod
    async def _task_log(db: AsyncSession, plan: ExecutionPlan, log_type: str, content: str, metadata: dict | None = None) -> None:
        """Write task log entry. Best-effort."""
        if not plan.task_id:
            return
        try:
            from packages.core.services.task_service import add_task_log
            await add_task_log(
                db, plan.task_id, log_type, content,
                actor=TaskActor.SYSTEM, created_by="system", metadata=metadata,
            )
        except Exception:
            pass

    @staticmethod
    async def _announce(
        entity_id: str,
        workspace_id: Optional[str],
        plan_id: str,
        *,
        task_id: Optional[str] = None,
        started: bool,
        step_count: int,
        execution_mode: str,
        chat_events: list[dict],
        plan_done: Optional[str],
        plan_started_at,
        plan_completed_at,
        plan_cost: Optional[float],
        plan_error: Optional[dict],
        task_title: Optional[str] = None,
        step_snapshots: Optional[list[dict]] = None,
        task_status: Optional[str] = None,
        task_issue: Optional[str] = None,
        locale: str | None = None,
    ) -> None:
        """Best-effort chat notifications for plan-level events.

        Step-level chat events (done / failed / needs_human) now fire
        from Dispatcher.complete_lease / fail_lease / lease_needs_human
        so external workers get the same surface as InternalWorker
        without each worker having to re-implement the chat hook.
        Inline kinds (sleep, human) handled by PlanExecutor still post
        from here through the same notifiers.
        """
        if started:
            await chat_notify.notify_plan_started(
                entity_id=entity_id, workspace_id=workspace_id,
                plan_id=plan_id, task_id=task_id, task_title=task_title,
                step_count=step_count, execution_mode=execution_mode,
                steps=step_snapshots,
                locale=locale,
            )

        for evt in chat_events:
            step = evt["step"]
            sub_id = step.resolved_subscription_id
            if evt["kind"] == "step_needs_human":
                await chat_notify.notify_step_needs_human(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, step_id=step.id, step_key=step.step_key,
                    prompt=evt.get("prompt") or step.human_input_prompt or "",
                    subscription_id=sub_id,
                    locale=locale,
                    pending_action=evt.get("pending_action"),
                )
            elif evt["kind"] == "step_failed":
                await chat_notify.notify_step_failed(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, step_id=step.id, step_key=step.step_key,
                    error=evt.get("error"),
                    will_retry=evt.get("will_retry", False),
                    subscription_id=sub_id,
                    locale=locale,
                )

        if plan_done == "completed":
            if task_status in _SUPERVISOR_HELD_TASK_STATUSES:
                await chat_notify.notify_plan_needs_attention(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, task_id=task_id,
                    task_title=task_title, issue=task_issue,
                    steps=step_snapshots,
                    locale=locale,
                )
            else:
                duration = None
                if plan_started_at and plan_completed_at:
                    duration = (plan_completed_at - plan_started_at).total_seconds()
                await chat_notify.notify_plan_completed(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, task_id=task_id,
                    duration_seconds=duration, cost_usd=plan_cost,
                    task_title=task_title,
                    steps=step_snapshots,
                    locale=locale,
                )
        elif plan_done == "failed":
            await chat_notify.notify_plan_failed(
                entity_id=entity_id, workspace_id=workspace_id,
                plan_id=plan_id, task_id=task_id, error=plan_error,
                task_title=task_title,
                steps=step_snapshots,
                locale=locale,
            )


async def _prepare_task_coordinator_submission(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: Any,
    inspection: bool = False,
) -> tuple[Any | None, SupervisorDecision | None]:
    """Freeze a V3 submission only through locked, server-owned facts.

    A Task AgentLoop may ask to finish, but it cannot mark a Step, Plan, or
    Task successful. The caller commits this transaction and emits the
    returned event after commit. Legacy V2 Supervisor finalization is not
    routed through this function.
    """

    from packages.core.constants.execution import WorkLeaseStatus
    from packages.core.models.task_coordinator import (
        TaskCoordinatorPhase,
        TaskCoordinatorRun,
    )
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.dispatcher.effect_safety import has_ambiguous_external_effect
    from packages.core.services.task_coordinator_fence import lock_task_coordinator_admission
    from packages.core.services.task_execution import task_execution_intent_generation

    _, selection = await lock_task_coordinator_admission(db, task_id=task_id)
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    )).scalar_one_or_none()
    if plan is None or plan.task_id != task_id:
        raise ValueError("Task coordinator submission Plan is unavailable")
    task, plan, task_plans, leases, steps = await _lock_supervisor_review_state(
        db, plan,
    )
    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    if (
        state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION
        or str(state.get(TASK_COORDINATOR_RUN_ID_KEY) or "") != claim.run_id
    ):
        raise ValueError("Task coordinator submission is not a V3 Plan")
    run = (await db.execute(
        select(TaskCoordinatorRun)
        .where(TaskCoordinatorRun.id == claim.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    expiry = run.claim_expires_at if run is not None else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (
        run is None
        or run.task_id != task_id
        or run.entity_id != task.entity_id
        or run.workspace_id != task.workspace_id
        or run.root_subscription_id != selection.root.subscription_id
        or run.root_agent_id != selection.root.agent_id
        or run.selection_fingerprint != selection.stable_fingerprint()
        or run.lifecycle_generation != task_execution_intent_generation(task)
        or run.current_plan_id != plan.id
        or run.claim_token != claim.claim_token
        or run.claim_generation != claim.wake_generation
        or run.wake_generation != claim.wake_generation
        or expiry is None
        or expiry <= datetime.now(timezone.utc)
        or run.phase in TaskCoordinatorPhase.terminal()
    ):
        raise ValueError("Task coordinator submission lost its current claim")
    if (
        task.status != TaskStatus.IN_PROGRESS.value
        or (
            isinstance(run.checkpoint, dict)
            and run.checkpoint.get("task_coordinator_control") is not None
        )
        or run.phase == TaskCoordinatorPhase.WAITING_CHILD.value
        or plan.status not in {
            ExecutionPlanStatus.DRAFT.value,
            ExecutionPlanStatus.RUNNING.value,
        }
        or len([
            candidate for candidate in task_plans
            if candidate.status not in {
                status.value for status in PLAN_TERMINAL_STATUSES
            }
        ]) != 1
        or any(step.step_status != ExecutionStepStatus.DONE.value for step in steps)
        or any(lease.status == WorkLeaseStatus.ACTIVE.value for lease in leases)
    ):
        return None, SupervisorDecision(
            verdict=SupervisorVerdict.NEEDS_REPLAN,
            evidence="The Plan still has unsettled execution; inspect its Steps before submitting.",
            source=SupervisorDecisionSource.GATE,
        )
    for step in steps:
        if await has_ambiguous_external_effect(
            db, step.evidence_refs, owner_step=step,
        ):
            return None, SupervisorDecision(
                verdict=SupervisorVerdict.NEEDS_HUMAN,
                evidence=f"Step {step.step_key!r} has an unreconciled external effect.",
                source=SupervisorDecisionSource.GATE,
            )
    from packages.core.services.task_acceptance_review import build_task_acceptance_snapshot

    # One read-only verification feeds the gate, review and final projection.
    # Keep typed evidence errors in the current coordinator loop; they are
    # not a declaration that completed execution must be repeated.
    snapshot = await build_task_acceptance_snapshot(
        db, task=task, plan=plan, steps=steps, claim=claim, inspection=inspection,
    )
    if inspection and snapshot.payload.get("verification", {}).get("issues"):
        return (task, plan, run, snapshot), None
    verified_artifacts = {
        item["step_id"]: item["refs"]
        for item in snapshot.payload["artifact_manifest"]["steps"]
    }
    locale = await resolve_plan_response_locale(db, plan)
    gate = await evaluate_task_completion_gates(
        db,
        task=task,
        plan=plan,
        steps=steps,
        plan_status=ExecutionPlanStatus.COMPLETED.value,
        locale=locale,
        verified_artifacts=verified_artifacts,
    )
    if gate is not None:
        if inspection:
            from packages.core.services.task_acceptance_review import acceptance_snapshot

            snapshot = acceptance_snapshot({
                **snapshot.payload,
                "verification": {"status": "unverified", "issues": [{
                    "code": "completion_gate_failed", "failure_scope": "task_acceptance",
                    "message": gate.evidence, "verdict": gate.verdict.value,
                    "action_executed": False, "execution_replay": "not_authorized_by_inspection",
                }]},
            })
            return (task, plan, run, snapshot), None
        return None, gate

    return (task, plan, run, snapshot), None


async def prepare_task_coordinator_submission(
    db: AsyncSession, *, task_id: str, plan_id: str, claim: Any, inspection: bool = False,
) -> tuple[Any | None, SupervisorDecision | None]:
    """Caller commits/releases this short transaction before content review."""
    prepared, gate = await _prepare_task_coordinator_submission(
        db, task_id=task_id, plan_id=plan_id, claim=claim, inspection=inspection,
    )
    return (prepared[3] if prepared is not None else None), gate


async def finalize_task_coordinator_submission(
    db: AsyncSession, *, task_id: str, plan_id: str, claim: Any,
    acceptance_review: Any = None,
) -> tuple[bool, SupervisorDecision | None, dict[str, Any] | None]:
    """The sole V3 terminal writer: hard gates plus exact content acceptance."""
    from packages.core.models.task_coordinator import TaskCoordinatorPhase
    from packages.core.services.task_acceptance_review import (
        TaskAcceptanceError,
        TaskAcceptanceReview,
        TaskAcceptanceVerdict,
    )
    from packages.core.services.task_coordinator_wakeup import (
        release_task_coordinator_claim,
        settle_task_coordinator_wake,
    )

    prepared, gate = await _prepare_task_coordinator_submission(
        db, task_id=task_id, plan_id=plan_id, claim=claim,
    )
    if prepared is None:
        return False, gate, None
    task, plan, run, snapshot = prepared
    if (
        not isinstance(acceptance_review, TaskAcceptanceReview)
        or acceptance_review.snapshot_digest != snapshot.digest
        or acceptance_review.verdict is not TaskAcceptanceVerdict.ACCEPTED
        or acceptance_review.issues
        or not acceptance_review.evidence.strip()
        or not acceptance_review.checks
    ):
        raise TaskAcceptanceError(
            "Task content acceptance is missing, rejected, or stale; review the current deliverables before submitting.",
            code="invalid_assessment",
            details={"next_action": "Re-inspect and correct the assessment in this loop; do not repeat completed Steps."},
        )

    plan.dispatcher_state = {
        **(plan.dispatcher_state or {}),
        "task_content_acceptance": {
            "snapshot_digest": snapshot.digest,
            "artifact_manifest": snapshot.payload["artifact_manifest"],
            "verdict": acceptance_review.verdict.value,
            "evidence": acceptance_review.evidence,
            "checks": [
                {"criterion": check.criterion, "pointer": check.pointer, "quote": check.quote}
                for check in acceptance_review.checks
            ],
        },
    }
    decision = SupervisorDecision(
        verdict=SupervisorVerdict.COMPLETED,
        evidence=acceptance_review.evidence,
        source=SupervisorDecisionSource.GATE,
    )
    task_event = await PlanExecutor._finalize(
        db, plan, ExecutionPlanStatus.COMPLETED.value,
        server_decision=decision,
    )
    settled = await settle_task_coordinator_wake(
        db,
        task_id=task_id,
        event_id=claim.event_id,
        wake_generation=claim.wake_generation,
        claim_token=claim.claim_token,
    )
    if not settled:
        raise RuntimeError("Task coordinator completion could not settle its wake")
    await release_task_coordinator_claim(
        db,
        task_id=task_id,
        claim_token=claim.claim_token,
        claim_generation=claim.wake_generation,
    )
    run.phase = TaskCoordinatorPhase.COMPLETED.value
    run.state_version += 1
    return True, None, task_event


class TaskCoordinatorReplanOutcome(str, Enum):
    REPAIRED = "repaired"
    REPLANNED = "replanned"
    BLOCKED = "blocked"
    WAITING_HUMAN = "waiting_human"
    FAILED = "failed"


async def resume_task_coordinator_delivery_review(
    db: AsyncSession, *, task_id: str, plan_id: str,
    expected_state_version: int, actor_user_id: str,
) -> dict[str, Any]:
    """Explicit recovery of an old replan hold, never retry its DONE writers.

    The authenticated API caller must authorize Task mutation and commit this
    transaction. Only a stopped, unclaimed current owner can be resumed; the
    staged outbox wake acquires a fresh normal claim for semantic acceptance.
    This function does not approve content, clear effects or run any Step.
    """
    from types import SimpleNamespace

    from packages.core.constants.execution import WorkLeaseStatus
    from packages.core.contracts.artifact_delivery import artifact_submission_sha256
    from packages.core.dispatcher.effect_safety import has_ambiguous_external_effect
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION, TASK_COORDINATOR_PLAN_VERSION_KEY, TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.services.task_acceptance_review import TaskAcceptanceError, build_task_acceptance_snapshot
    from packages.core.services.task_coordinator_fence import lock_task_coordinator_admission
    from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake
    from packages.core.services.task_execution import task_execution_intent_generation
    from packages.core.services.task_state_machine import apply_task_status_transition

    if not actor_user_id or not isinstance(expected_state_version, int) or isinstance(expected_state_version, bool):
        raise ValueError("Delivery review resume needs an actor and exact state version")
    _, selection = await lock_task_coordinator_admission(db, task_id=task_id)
    plan = (await db.execute(select(ExecutionPlan).where(ExecutionPlan.id == plan_id))).scalar_one_or_none()
    if plan is None or plan.task_id != task_id:
        raise ValueError("Delivery review Plan is unavailable")
    task, plan, task_plans, leases, steps = await _lock_supervisor_review_state(db, plan)
    state = dict(plan.dispatcher_state or {})
    run = (await db.execute(
        select(TaskCoordinatorRun).where(TaskCoordinatorRun.task_id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    latest = max(task_plans, key=lambda item: (item.created_at, item.id))
    if (
        run is None or run.state_version != expected_state_version
        or run.phase != TaskCoordinatorPhase.WAITING_HUMAN.value
        or run.claim_token is not None or run.claim_generation is not None or run.claim_expires_at is not None
        or run.wake_generation is not None
        or run.current_plan_id != plan.id or latest.id != plan.id
        or run.entity_id != task.entity_id or run.workspace_id != task.workspace_id
        or run.root_subscription_id != selection.root.subscription_id
        or run.root_agent_id != selection.root.agent_id
        or run.selection_fingerprint != selection.stable_fingerprint()
        or state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION
        or state.get(TASK_COORDINATOR_RUN_ID_KEY) != run.id
        or task.status not in {TaskStatus.WAITING_ON_CUSTOMER.value, TaskStatus.ON_HOLD.value}
        or plan.status != ExecutionPlanStatus.FAILED.value
        or any(candidate.status not in {status.value for status in PLAN_TERMINAL_STATUSES} for candidate in task_plans)
        or not steps or any(step.step_status != ExecutionStepStatus.DONE.value for step in steps)
        or any(lease.status == WorkLeaseStatus.ACTIVE.value for lease in leases)
        or (isinstance(run.checkpoint, dict) and run.checkpoint.get("task_coordinator_control") is not None)
    ):
        raise ValueError("Delivery review resume lost its stopped current owner or state version")
    prior_error = plan.last_error if isinstance(plan.last_error, dict) else {}
    if prior_error.get("type") != "ActionOutcomeReconciliationRequired":
        raise ValueError("Only an existing replan reconciliation hold can use delivery review resume")
    for step in steps:
        if await has_ambiguous_external_effect(db, step.evidence_refs, owner_step=step):
            raise ValueError("An unknown external effect must be reconciled before delivery review can resume")
    snapshot = await build_task_acceptance_snapshot(
        db, task=task, plan=plan, steps=steps,
        claim=SimpleNamespace(run_id=run.id, wake_generation=None, claim_token=None),
    )
    verified = {item["step_id"]: item["refs"] for item in snapshot.payload["artifact_manifest"]["steps"]}
    if not any(verified.values()):
        raise TaskAcceptanceError("No verified saved delivery is available for same-Plan recovery.", code="artifact_reference_missing")
    gate = await evaluate_task_completion_gates(
        db, task=task, plan=plan, steps=steps, plan_status=ExecutionPlanStatus.COMPLETED.value,
        locale=await resolve_plan_response_locale(db, plan), verified_artifacts=verified, review_existing_delivery=True,
    )
    if gate is not None:
        raise TaskAcceptanceError(gate.evidence, code="delivery_recovery_blocked")
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
    from packages.core.models.hitl_request import HitlRequest

    pending = list((await db.execute(select(HitlRequest).where(
        HitlRequest.entity_id == task.entity_id,
        HitlRequest.origin_task_id == task.id,
        HitlRequest.status == ApprovalStatus.PENDING.value,
    ))).scalars().all())
    rendered_hold_reason = _user_facing_step_issue(str(prior_error.get("message") or ""), steps)
    obsolete = [request for request in pending if (
        request.origin_kind == ApprovalOriginKind.TASK.value
        and request.origin_plan_id == plan.id and request.action_key == "task.recover"
        and request.hitl_type == HitlType.ERROR.value and request.matched_rule == "task.needs_recovery"
        and request.reason == rendered_hold_reason
    )]
    if len(obsolete) != len(pending):
        raise ValueError("A separate user decision is still pending; delivery review cannot bypass it")
    state["task_delivery_review_resume"] = {
        "actor_user_id": actor_user_id, "resumed_at": datetime.now(timezone.utc).isoformat(),
        "prior_error": prior_error, "prior_state_version": run.state_version,
        "prior_lifecycle_generation": run.lifecycle_generation,
        "prior_checkpoint_sha256": artifact_submission_sha256(run.checkpoint),
        "verified_evidence_digest": snapshot.evidence_digest,
        "obsolete_hitl_request_ids": [request.id for request in obsolete],
        "obsolete_hitl_reason": rendered_hold_reason,
    }
    state.pop("task_content_acceptance", None)
    plan.dispatcher_state = state
    plan.last_error = None
    plan.status = ExecutionPlanStatus.RUNNING.value
    plan.completed_at = None
    await apply_task_status_transition(task, TaskStatus.IN_PROGRESS.value, db=db, actor_kind="user", actor_id=actor_user_id)
    # The explicit status transition starts a new review-owner occurrence,
    # not a new Plan/Step attempt or external-effect identity.
    run.lifecycle_generation = task_execution_intent_generation(task)
    if isinstance(task.actual_output, dict):
        task.actual_output = {key: value for key, value in task.actual_output.items()
                              if key not in {"supervisor_verdict", "supervisor_evidence", "needs_input"}}
        task.actual_output = {**task.actual_output, "plan_status": ExecutionPlanStatus.RUNNING.value}
    # Drop the obsolete rejection transcript, not the Steps or their receipts.
    # Reuse the existing read-only observation retry entry point for first I/O.
    run.checkpoint = {"task_delivery_read_retry": {
        "version": 1, "plan_id": plan.id, "tool_name": "inspect_task_delivery",
        "arguments": {"kind": "manifest"}, "attempts": 0,
    }}
    run.phase = TaskCoordinatorPhase.READY.value
    run.terminal_cleanup_at = None
    wake = await stage_task_coordinator_wake(
        db, task_id=task_id, reason="task_delivery_review_resume", expected_state_version=expected_state_version,
    )
    if wake is None:
        raise ValueError("Delivery review recovery could not stage its owner wake")
    return {"task_id": task_id, "plan_id": plan_id, "run_id": run.id,
            "state_version": run.state_version, "event_id": wake.event_id, "wake_generation": wake.wake_generation}


async def resolve_resumed_delivery_review_hitl(
    db: AsyncSession, *, task_id: str, plan_id: str, actor_user_id: str,
) -> int:
    """After resume commits, expire only its exact obsolete error card.

    Approval locks intentionally live in a separate transaction from the
    Workspace/Task/Plan locks. Existing user decisions and logs are preserved;
    this expires an obsolete error, never manufactures a permission grant.
    """
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Message
    from packages.core.ledger import adapters, event_types

    plan = (await db.execute(select(ExecutionPlan).where(
        ExecutionPlan.id == plan_id, ExecutionPlan.task_id == task_id,
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    recovery = (plan.dispatcher_state or {}).get("task_delivery_review_resume") if plan else None
    if (not isinstance(recovery, dict) or recovery.get("actor_user_id") != actor_user_id
            or plan.status not in {ExecutionPlanStatus.RUNNING.value, ExecutionPlanStatus.COMPLETED.value}
            or plan.last_error):
        return 0
    ids = recovery.get("obsolete_hitl_request_ids") or []
    if not ids:
        return 0
    requests = list((await db.execute(select(HitlRequest).where(
        HitlRequest.id.in_(ids), HitlRequest.entity_id == plan.entity_id,
        HitlRequest.origin_task_id == task_id, HitlRequest.origin_plan_id == plan_id,
        HitlRequest.origin_kind == ApprovalOriginKind.TASK.value,
        HitlRequest.action_key == "task.recover", HitlRequest.hitl_type == HitlType.ERROR.value,
        HitlRequest.matched_rule == "task.needs_recovery",
        HitlRequest.reason == recovery.get("obsolete_hitl_reason"),
        HitlRequest.status == ApprovalStatus.PENDING.value,
    ).order_by(HitlRequest.id).with_for_update().execution_options(populate_existing=True))).scalars().all())
    now = datetime.now(timezone.utc)
    for request in requests:
        request.status = ApprovalStatus.EXPIRED.value
        request.resolved_reason = "delivery_review_resumed"
        request.decided_by_user_id = actor_user_id
        request.decided_at = now
        request.decided_via = "delivery_review_resume"
        await adapters.record_approval_event(db, request, event_types.APPROVAL_EXPIRED)
    resolved_ids = [request.id for request in requests]
    if resolved_ids:
        messages = list((await db.execute(select(Message).where(
            Message.pending_action["approval_request_id"].as_string().in_(resolved_ids),
            Message.resolved_at.is_(None),
        ).order_by(Message.id).with_for_update().execution_options(populate_existing=True))).scalars().all())
        for message in messages:
            message.resolved_at = now
            message.resolved_by_user_id = actor_user_id
            message.resolution = {"choice": "superseded", "reason": "delivery_review_resumed", "plan_id": plan_id}
    return len(requests)


_TASK_COORDINATOR_REPAIR_KEY = "task_coordinator_failed_branch_repair"


async def _repair_task_coordinator_failed_branches(
    db: AsyncSession, *, task: Any, plan: ExecutionPlan,
    steps: list[ExecutionStep], leases: list[Any], issue: str, tool_call_id: str,
) -> bool:
    """Grant one bounded same-Plan repair without replaying completed writers.

    Only already-attempted, effect-free Agent failures qualify. Structural
    changes, failed writers with receipts, and legacy rows without pinned
    inputs still use the existing replan/reconciliation path. The caller owns
    the Task/Plan/Lease/Step locks and the acknowledged V3 coordinator claim.
    """
    from packages.core.dispatcher.effect_safety import (
        has_ambiguous_external_effect,
        retry_effect_block_detail,
    )
    from packages.core.plans.step_reset import never_ran_downstream
    from packages.core.services.retry_policy import step_retry_policy_config
    from packages.core.services.task_coordinator_steps import (
        TASK_COORDINATOR_STEP_INPUTS_KEY,
        task_coordinator_step_input_digest,
        verified_task_coordinator_dependency_artifacts,
    )

    state = dict(plan.dispatcher_state or {})
    if not tool_call_id or state.get(_TASK_COORDINATOR_REPAIR_KEY) is not None:
        return False
    failed = [
        step for step in steps
        if step.step_status == ExecutionStepStatus.FAILED.value
        and not never_ran_downstream(step)
    ]
    if not failed:
        return False
    targets = {step.step_key for step in failed}
    affected = set(targets)
    while True:
        descendants = {step.step_key for step in steps if affected.intersection(step.depends_on or [])}
        if descendants <= affected:
            break
        affected.update(descendants)
    for step in steps:
        if step.step_key not in targets:
            if step.step_status == ExecutionStepStatus.CANCELLED.value:
                return False
            if step.step_key in affected and (
                step.attempt_count or step.runtime_checkpoint or step.evidence_refs
                or step.step_status == ExecutionStepStatus.DONE.value
            ):
                return False
            if (
                step.step_status in {ExecutionStepStatus.SKIPPED.value, ExecutionStepStatus.FAILED.value}
                and step.step_key not in affected
            ):
                return False
            continue
        failure = (step.error or {}).get("failure") or {}
        if (
            step.kind not in {"llm", "subagent"}
            or _is_coordinator_child(step)
            or not 0 < step.attempt_count < 10
            or _human_required_failure_issue([step]) is not None
            or failure.get("blockers")
            or failure.get("retryable") is False
            or attempt_forbids_replay(failure)
            or any(isinstance(ref, dict) and ref.get("kind") == "tool_effect" for ref in (step.evidence_refs or []))
            or any(
                lease.step_id == step.id and (
                    lease.execution_claim_id
                    or any(isinstance(ref, dict) and ref.get("kind") == "tool_effect" for ref in (lease.evidence_refs or []))
                )
                for lease in leases
            )
        ):
            return False
    # Includes completed siblings: an unresolved intent anywhere must not be
    # hidden by repairing a different branch.
    if await retry_effect_block_detail(db, steps):
        return False
    by_id = {step.id: step for step in steps}
    for lease in leases:
        if lease.step_id in by_id and await has_ambiguous_external_effect(
            db, lease.evidence_refs, owner_step=by_id[lease.step_id],
        ):
            return False
    digests = state.get(TASK_COORDINATOR_STEP_INPUTS_KEY) or {}
    for step in steps:
        if step in failed or step.step_status == ExecutionStepStatus.DONE.value:
            digest = task_coordinator_step_input_digest(task, plan, step, steps)
            if not digest or digests.get(step.id) != digest:
                return False
    await verified_task_coordinator_dependency_artifacts(
        db, task=task, plan=plan, steps=steps, selected=failed,
    )
    now = datetime.now(timezone.utc)
    repairs = []
    for step in failed:
        repairs.append({
            "step_id": step.id, "step_key": step.step_key,
            "attempt_count": step.attempt_count, "prior_max_attempts": step.max_attempts,
            "error": step.error, "result": step.result,
        })
        params = dict(step.params or {})
        params[SUPERVISOR_FEEDBACK_PARAM_KEY] = issue
        params["retry_policy"] = {
            **step_retry_policy_config(step), "max_attempts": step.attempt_count + 1,
        }
        step.params = params
        step.max_attempts = step.attempt_count + 1
        # There are no recorded actions to resume. Start a fresh Agent attempt
        # with the concrete repair issue, retaining cumulative attempts and all
        # prior lease/log rows. Do not reuse the generic reset (it clears count
        # and releases ordinary Steps directly to PENDING).
        step.step_status = ExecutionStepStatus.PAUSED.value
        step.current_lease_id = None
        step.error = None
        step.result = None
        step.runtime_checkpoint = None
        step.started_at = None
        step.finished_at = None

    revived = []
    for step in steps:
        if step.step_key in affected and never_ran_downstream(step):
            # Unlike the generic PENDING reset, V3 can revive a fan-in whose
            # other dependency is still PAUSED: only the coordinator may
            # release it once every dependency has actually completed.
            step.step_status = ExecutionStepStatus.PAUSED.value
            step.current_lease_id = None
            step.error = None
            step.finished_at = None
            revived.append(step.id)
    state[_TASK_COORDINATOR_REPAIR_KEY] = {
        "tool_call_id": tool_call_id, "issue": issue,
        "repaired_at": now.isoformat(), "steps": repairs,
        "revived_step_ids": revived,
    }
    plan.dispatcher_state = state
    plan.last_error = None
    return True


async def prepare_task_coordinator_replan(
    db: AsyncSession,
    *,
    task_id: str,
    plan_id: str,
    claim: Any,
    issue: str,
    expected_state_version: int | None = None,
    tool_call_id: str | None = None,
) -> tuple[TaskCoordinatorReplanOutcome, str | dict[str, Any], dict[str, Any] | None]:
    """Repair a settled failed branch, or prepare a bounded Plan replacement.

    This is a bounded, deterministic mutation only. The Task AgentLoop
    decides when to request it; the Planner still validates and persists a
    replacement under the same pinned claim in a later transaction.
    """

    from packages.core.constants.execution import WorkLeaseStatus
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
    from packages.core.plans.coordinator import (
        TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_PLAN_VERSION_KEY,
        TASK_COORDINATOR_RUN_ID_KEY,
    )
    from packages.core.services.task_execution import task_execution_intent_generation
    from packages.core.services.task_coordinator_fence import lock_task_coordinator_admission

    _, selection = await lock_task_coordinator_admission(db, task_id=task_id)
    plan = (await db.execute(
        select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
    )).scalar_one_or_none()
    if plan is None or plan.task_id != task_id:
        raise ValueError("Task coordinator replan Plan is unavailable")
    task, plan, task_plans, leases, steps = await _lock_supervisor_review_state(
        db, plan,
    )
    state = plan.dispatcher_state if isinstance(plan.dispatcher_state, dict) else {}
    if (
        state.get(TASK_COORDINATOR_PLAN_VERSION_KEY) != TASK_COORDINATOR_PLAN_VERSION
        or str(state.get(TASK_COORDINATOR_RUN_ID_KEY) or "") != claim.run_id
    ):
        raise ValueError("Task coordinator replan is not a V3 Plan")
    run = (await db.execute(
        select(TaskCoordinatorRun)
        .where(TaskCoordinatorRun.id == claim.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    expiry = run.claim_expires_at if run is not None else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if (
        run is None
        or claim.task_id != task_id
        or run.task_id != task_id
        or run.entity_id != task.entity_id
        or run.workspace_id != task.workspace_id
        or run.lifecycle_generation != task_execution_intent_generation(task)
        or run.root_subscription_id != selection.root.subscription_id
        or run.root_agent_id != selection.root.agent_id
        or run.selection_fingerprint != selection.stable_fingerprint()
        or run.current_plan_id != plan.id
        or run.claim_token != claim.claim_token
        or run.claim_generation != claim.wake_generation
        or run.wake_generation != claim.wake_generation
        or expiry is None
        or expiry <= datetime.now(timezone.utc)
        or run.phase in TaskCoordinatorPhase.terminal()
        or task.status != TaskStatus.IN_PROGRESS.value
        or plan.status not in {
            ExecutionPlanStatus.DRAFT.value,
            ExecutionPlanStatus.RUNNING.value,
        }
    ):
        raise ValueError("Task coordinator replan lost its current claim")
    if len([
        candidate for candidate in task_plans
        if candidate.status not in {
            status.value for status in PLAN_TERMINAL_STATUSES
        }
    ]) != 1:
        return TaskCoordinatorReplanOutcome.BLOCKED, "The Task no longer has one authoritative active Plan.", None
    repair = state.get(_TASK_COORDINATOR_REPAIR_KEY)
    if tool_call_id and isinstance(repair, dict) and repair.get("tool_call_id") == tool_call_id:
        # Observe the exact committed control result after a worker crash;
        # never grant a second attempt because tool-result checkpointing lost.
        return TaskCoordinatorReplanOutcome.REPAIRED, plan.id, None
    if expected_state_version is not None and run.state_version != expected_state_version:
        raise ValueError("Task coordinator replan lost its state version")
    if (
        run.phase == TaskCoordinatorPhase.WAITING_CHILD.value
        or (
            isinstance(run.checkpoint, dict)
            and run.checkpoint.get("task_coordinator_control") is not None
        )
    ):
        return TaskCoordinatorReplanOutcome.BLOCKED, "A released child batch has not been checkpointed and acknowledged.", None
    if any(lease.status == WorkLeaseStatus.ACTIVE.value for lease in leases) or any(
        step.step_status in {
            ExecutionStepStatus.PENDING.value,
            ExecutionStepStatus.RUNNING.value,
            ExecutionStepStatus.WAITING_HUMAN.value,
        }
        for step in steps
    ):
        return TaskCoordinatorReplanOutcome.BLOCKED, "Wait for active Steps and approvals before revising the Plan.", None
    normalized_issue = str(issue or "").strip()[:2000]
    if not normalized_issue:
        return TaskCoordinatorReplanOutcome.BLOCKED, "A concrete execution or deliverable issue is required.", None

    from packages.core.services.artifact_delivery import ArtifactDeliveryError

    try:
        repaired = expected_state_version is not None and await _repair_task_coordinator_failed_branches(
            db, task=task, plan=plan, steps=steps, leases=leases,
            issue=normalized_issue, tool_call_id=str(tool_call_id or ""),
        )
    except ArtifactDeliveryError as exc:
        return TaskCoordinatorReplanOutcome.BLOCKED, str(exc), None
    if repaired:
        run.phase = TaskCoordinatorPhase.RUNNING.value
        run.state_version += 1
        return TaskCoordinatorReplanOutcome.REPAIRED, plan.id, None

    from packages.core.ai.runtime.tool_error_contract import tool_error_payload
    from packages.core.contracts.tool_effects import ToolEffectEvidenceKind
    from packages.core.dispatcher.effect_safety import (
        has_ambiguous_external_effect,
        validated_new_plan_effect_block_detail,
    )

    missing_expectation = _unmet_expects_failure(plan, steps)
    if missing_expectation and missing_expectation[0] in {
        "external_action_receipt_missing", "external_publish_receipt_missing",
    }:
        return TaskCoordinatorReplanOutcome.BLOCKED, tool_error_payload(
            tool_name="replan_task", code="external_receipt_missing",
            message=(
                "This Plan has no captured proof of the external action outcome. "
                "Its action must not be repeated under a new Plan identity."
            ),
            details={
                "plan_id": plan.id, "cause": missing_expectation[1],
                "action_executed": False, "requires_human": False,
                "do_not_replay": True, "available_actions": ["inspect_task_delivery"],
                "unavailable_actions": ["replace_plan", "repeat_action"],
            },
        ), None

    # Replacing a Plan changes effect identity even when the original result
    # is known. That rejects this control operation; it is not evidence that a
    # customer must determine whether a successful write happened. Preserve
    # the original Plan and let the coordinator inspect its recorded delivery.
    effect_block = await validated_new_plan_effect_block_detail(db, steps)
    if effect_block:
        by_id = {step.id: step for step in steps}
        evidence_sources = [
            (step, "step", step.evidence_refs or []) for step in steps
        ] + [
            (by_id[lease.step_id], "lease", lease.evidence_refs or [])
            for lease in leases if lease.step_id in by_id
        ]
        ambiguous = False
        receipts = []
        receipt_count = 0
        for step, source, evidence in evidence_sources:
            if await has_ambiguous_external_effect(db, evidence, owner_step=step):
                ambiguous = True
                break
            for ref in evidence:
                if not isinstance(ref, dict) or ref.get("kind") != ToolEffectEvidenceKind.TOOL_EFFECT.value:
                    continue
                receipt_count += 1
                if len(receipts) < 32:
                    fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
                    receipts.append({
                        "step_id": step.id, "step_key": step.step_key,
                        "step_status": step.step_status, "source": source,
                        "effect": ref.get("effect"),
                        **{key: str(fields[key])[:200] for key in (
                            "tool", "tool_call_id", "effect_id", "intent_state",
                        ) if fields.get(key) is not None},
                    })
        if not ambiguous:
            return TaskCoordinatorReplanOutcome.BLOCKED, tool_error_payload(
                tool_name="replan_task", code="plan_replacement_would_repeat_effects",
                message=(
                    "The recorded operation outcomes are settled, but replacing this Plan "
                    "would change their execution identities. The Plan was not replaced. "
                    "Its existing Step results and delivery evidence remain available for inspection."
                ),
                details={
                    "plan_id": plan.id, "requested_issue": normalized_issue,
                    "operation": "replace_plan", "action_executed": False,
                    "effect_state": "settled", "do_not_replay": True,
                    "requires_human": False, "receipts": receipts,
                    "receipt_count": receipt_count,
                    "omitted_receipt_count": max(0, receipt_count - len(receipts)),
                    "available_actions": ["inspect_task_delivery"],
                    "unavailable_actions": ["replace_plan", "repeat_completed_steps"],
                    "delivery_accepted": False,
                },
            ), None

    accepted = await PlanExecutor._maybe_replan(
        db, plan, steps,
        reason="task_coordinator_review",
        issue=normalized_issue,
    )
    if not accepted:
        plan_error = plan.last_error if isinstance(plan.last_error, dict) else {}
        detail = str(plan_error.get("message") or normalized_issue)[:2000]
        from packages.core.dispatcher.effect_safety import (
            ambiguous_effect_reconciliation_snapshot,
        )

        # A failed internal repair or an exhausted replan budget is not a
        # customer question. Only a fresh, exact unknown-effect snapshot can
        # cross the reconciliation boundary; never infer that from the
        # planner's prose or a Step's requires_human boolean.
        reconciliation = await ambiguous_effect_reconciliation_snapshot(
            db, steps, plan_id=plan.id,
        )
        needs_reconciliation = bool(
            isinstance(reconciliation, dict) and reconciliation.get("effects")
        )
        if needs_reconciliation:
            plan.last_error = {**plan_error, "reconciliation": reconciliation}
            task_event = await PlanExecutor._finalize(
                db, plan, ExecutionPlanStatus.FAILED.value,
                server_decision=SupervisorDecision(
                    verdict=SupervisorVerdict.NEEDS_HUMAN,
                    evidence=detail,
                    source=SupervisorDecisionSource.GATE,
                ),
            )
        else:
            from packages.core.services.task_service import add_task_log
            from packages.core.services.task_state_machine import apply_task_status_transition

            # Keep the original V3 Plan and Step identities intact. A generic
            # Plan retry must never reset these rows through the V2 executor;
            # only an explicitly admitted Task retry may reopen this loop.
            await apply_task_status_transition(
                task, TaskStatus.BLOCKED.value, db=db, actor_kind=TaskActor.SYSTEM.value,
            )
            plan.last_error = plan_error or {
                "type": "TaskCoordinatorReplanUnavailable", "message": detail,
            }
            task_event = None
        from packages.core.services.task_coordinator_wakeup import (
            release_task_coordinator_claim,
            settle_task_coordinator_wake,
        )

        settled = await settle_task_coordinator_wake(
            db,
            task_id=task_id,
            event_id=claim.event_id,
            wake_generation=claim.wake_generation,
            claim_token=claim.claim_token,
        )
        if not settled:
            raise RuntimeError("Task coordinator hold could not settle its wake")
        await release_task_coordinator_claim(
            db,
            task_id=task_id,
            claim_token=claim.claim_token,
            claim_generation=claim.wake_generation,
        )
        run.phase = (
            TaskCoordinatorPhase.WAITING_HUMAN.value
            if needs_reconciliation else TaskCoordinatorPhase.FAILED.value
        )
        run.state_version += 1
        if needs_reconciliation:
            return TaskCoordinatorReplanOutcome.WAITING_HUMAN, detail, task_event
        outcome = tool_error_payload(
            tool_name="replan_task", code="task_replan_unavailable",
            message="The Task stopped after bounded automatic repair; no user approval is being requested.",
            details={
                "plan_id": plan.id, "cause": detail,
                "plan_error": plan_error.get("type"),
                "task_status": TaskStatus.BLOCKED.value,
                "plan_status": plan.status,
                "run_phase": TaskCoordinatorPhase.FAILED.value,
                "requires_human": False,
                "action_executed": False,
                "do_not_replay_completed_steps": True,
            },
        )
        run.checkpoint = {**(run.checkpoint or {}), "coordinator_outcome": outcome}
        await add_task_log(
            db, task.id, TaskLogType.AI_EXECUTION_FAILED,
            outcome["message"], actor=TaskActor.SYSTEM,
            metadata={"plan_id": plan.id, "error": outcome["error"],
                      "coordinator_outcome": outcome},
        )
        return TaskCoordinatorReplanOutcome.FAILED, outcome, task_event
    now = datetime.now(timezone.utc)
    unstarted = []
    for step in steps:
        if step.step_status == ExecutionStepStatus.PAUSED.value:
            unstarted.append(step.step_key)
            step.step_status = ExecutionStepStatus.SKIPPED.value
            step.finished_at = now
            step.error = {
                "type": "TaskCoordinatorReplanned",
                "message": "The Task coordinator revised the Plan before this Step ran.",
            }
    if unstarted:
        details = dict(task.details or {})
        context = dict(details.get("_replan_context") or {})
        context["unstarted_steps"] = unstarted
        details["_replan_context"] = context
        task.details = details
    run.phase = TaskCoordinatorPhase.RUNNING.value
    run.state_version += 1
    return TaskCoordinatorReplanOutcome.REPLANNED, plan.id, None
