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

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType, TaskStatus, plan_terminal_log_type
from packages.core.constants.execution import (
    ExecutionPlanStatus,
    ExecutionStepStatus,
)
from packages.core.constants.supervisor import (
    SUPERVISOR_STEP_RETRY_FLAG,
    SUPERVISOR_VERDICT_LOG_TYPE,
    SupervisorDecision,
    SupervisorDecisionSource,
    SupervisorVerdict,
)
from packages.core.constants.task_actors import TaskActor
from packages.core.contracts.envelope import (
    StepResultStatus,
    normalize_step_result_status,
)
from packages.core.database import async_session
from packages.core.ai.runtime import (
    RUNTIME_PLAN_EXECUTOR_SOURCE,
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
from packages.core.workspace_chat import notifiers as chat_notify

logger = logging.getLogger(__name__)


CYCLE_TICK_SECONDS = 2
"""How often run_plan re-enqueues itself while waiting on workers.
Trade-off: lower → faster end-to-end, higher Celery load."""


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


def _looks_like_relative_artifact_path(value: Any) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if not text or _is_external_artifact_url(text):
        return False
    lowered = text.lstrip("/").lower()
    if lowered.startswith(("api/", "documents/", "viewer/", "editor/")):
        return False
    name = lowered.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    has_file_extension = bool(suffix) and suffix != name and len(suffix) <= 12
    return "/" in lowered or has_file_extension


def _artifact_value_is_path(source_key: str, value: Any) -> bool:
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
    "pdf", "docx", "pptx", "xlsx", "csv", "download", "attachment",
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


def _artifact_refs_from_result(result: Any, *, step_key: str | None = None) -> list[dict]:
    if not isinstance(result, dict):
        return []
    if _is_reference_only_payload(result):
        return []

    refs: list[dict] = []

    def add_ref(
        ref_type: str,
        value: Any,
        *,
        source_key: str = "",
        name: Any = None,
        document_id: Any = None,
    ) -> None:
        if not value:
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

    for key in ("files", "artifacts", "documents", "images"):
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

    image_urls = result.get("image_urls")
    if isinstance(image_urls, list):
        for url in image_urls:
            add_ref("image", url, source_key="image_urls")

    return _dedupe_artifact_refs(refs)


def _step_result_summary(result: Any, *, limit: int = 500) -> str:
    """Canonical text view of a step result.

    Owned here because the executor is the canonical write path; the
    read-time reconciler imports it so task output and replan context
    describe a result the same way.
    """
    if not isinstance(result, dict):
        return str(result or "")[:limit]
    return str(result.get("text") or result.get("value") or result.get("summary") or "")[:limit]


def _dedupe_artifact_refs(refs: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for ref in refs:
        identity = _artifact_ref_identity(ref)
        key = (str(ref.get("step") or ""), str(ref.get("type") or ""), identity)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _artifact_ref_identity(ref: dict) -> str:
    return str(
        ref.get("fs_path")
        or ref.get("document_id")
        or ref.get("url")
        or ref.get("path")
        or ref.get("file_url")
        or ref.get("name")
        or ref.get("filename")
        or ref
    )


def _dedupe_task_artifact_refs(refs: list[dict]) -> list[dict]:
    """Task-level file lists should show each generated artifact once."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for ref in refs:
        key = (str(ref.get("type") or ""), _artifact_ref_identity(ref))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


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


def _has_artifact_result(steps: list[ExecutionStep]) -> bool:
    return any(
        _artifact_refs_from_result(s.result, step_key=s.step_key)
        for s in steps
        if s.step_status == ExecutionStepStatus.DONE
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

        refs = _artifact_refs_from_result(step.result, step_key=step.step_key)
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


def _unmet_expects_issue(plan: ExecutionPlan, steps: list[ExecutionStep]) -> str | None:
    """Deterministic completion check (envelope part ③): a done step whose
    plan declared ``expects`` must have CAPTURED evidence for each
    expectation — model claims never count, only step.evidence_refs mined
    from successful tool results (and, for files, tool-proven artifact
    fields already merged into the result)."""
    dag_steps = (plan.plan_dag or {}).get("steps") or []
    expects_by_key = {
        str(ds.get("key")): [str(e) for e in (ds.get("expects") or []) if str(e or "").strip()]
        for ds in dag_steps
        if isinstance(ds, dict)
    }
    for step in steps:
        if step.step_status != ExecutionStepStatus.DONE:
            continue
        expects = expects_by_key.get(step.step_key) or []
        if not expects:
            continue
        evidence = [e for e in (step.evidence_refs or []) if isinstance(e, dict)]
        if "publish" in expects and not any(
            e.get("kind") == "tool_effect" and e.get("effect") == "publish"
            for e in evidence
        ):
            return (
                f"Step {step.step_key!r} expects a confirmed external publish, but no "
                "successful publish tool result was captured as evidence. The model's "
                "own claim does not count. Replan: actually perform the publish via an "
                "integration tool so the effect is evidenced."
            )
        if "files" in expects and not (
            any(e.get("kind") == "artifact" for e in evidence)
            or _has_artifact_result([step])
        ):
            return (
                f"Step {step.step_key!r} expects a saved file/artifact, but no artifact "
                "evidence was captured. Replan: save the deliverable and return "
                "artifact evidence (fs_path, document_id, file_url, or files)."
            )
    return None


def _missing_artifact_issue(task: Any | None, steps: list[ExecutionStep]) -> str | None:
    if not _task_requires_artifact(task):
        return None
    if _has_artifact_result(steps):
        return None
    if getattr(task, "workspace_id", None):
        return (
            "This workspace task needs a saved file/media/document deliverable, "
            "but no saved file link or path was recorded. Replan and save the "
            "deliverable under this workspace's default artifact folder, then "
            "return artifact evidence such as fs_path, document_id, file_url, "
            "image_url, video_url, or files. Do not ask the user for a save "
            "location unless they explicitly requested no saved file."
        )
    return (
        "This task needs a saved file/media/document deliverable, but no "
        "saved file link or path was recorded. Replan and save the deliverable "
        "to a user-visible file, then return artifact evidence such as fs_path, "
        "document_id, file_url, image_url, video_url, or files."
    )


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
        for ref in _artifact_refs_from_result(step.result) or []:
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


def _plain_language_blocker(issue: str) -> str:
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
        ("status=partial", "reported only partial progress"),
        ("status=failed", "reported that it failed"),
        ("status=blocked", "reported that it could not proceed"),
        ("artifact_materialized=false", "did not save the file it produced"),
        ("=false", "reported the work as not done"),
        ("HITL request", "asked for a human decision"),
        ("pending", "left a pending request unanswered"),
    ):
        if needle in detail:
            return f"the “{step}” step {phrasing}." if step else f"a step {phrasing}."
    return f"the “{step}” step did not complete." if step else "a step did not complete."


def _hitl_request_message(
    task: Any | None,
    steps: list[ExecutionStep],
    *,
    structured_issue: str | None,
    artifact_issue: str | None,
    failed_steps: list[ExecutionStep],
) -> str:
    """Explain the hold in terms an operator can act on.

    This message used to render an internal status word verbatim —
    "recover or render finished video: step reported status=partial" — with
    no statement of what was produced, what is missing, or what the operator
    could do about it. Everything below is assembled from data the run
    already recorded.
    """
    lines: list[str] = ["**This task stopped before it finished.**"]

    produced = _produced_artifact_labels(steps)
    if produced:
        lines.append("")
        lines.append("**Produced so far:** " + ", ".join(produced))

    if artifact_issue:
        lines.append("")
        lines.append(
            "**Missing:** the deliverable file this task was asked to produce. "
            "No saved file path or document was recorded by any step."
        )
    elif structured_issue:
        lines.append("")
        lines.append(
            "**Incomplete:** "
            f"{_plain_language_blocker(structured_issue)}"
        )
    elif failed_steps:
        detail = "; ".join(
            f"{(fs.step_key or 'step').replace('_', ' ')}: "
            f"{(fs.error or {}).get('message', 'failed')[:120]}"
            for fs in failed_steps[:3]
        )
        lines.append("")
        lines.append(f"**Failed:** {detail}")
    else:
        lines.append("")
        lines.append(
            "**Unverified:** every step reported done, but the supervisor "
            "could not confirm the task objective was met."
        )

    said = _agent_summaries(steps)
    if said:
        lines.append("")
        lines.append("**What the agent reported:**")
        lines.extend(f"- {item}" for item in said)

    lines.append("")
    lines.append("**You can:**")
    lines.append("- Retry the task — it will re-plan from what already exists.")
    lines.append("- Reply below with guidance (a different approach, a narrower goal).")
    lines.append("- Mark the task complete if what was produced is good enough.")
    return "\n".join(lines)


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
        steps = steps_by_plan.get(prior.id) or []
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

    infos: list[dict] = []
    for s in steps:
        dag = dag_steps.get(s.step_key, {})
        instruction_parts = [str(dag.get("description") or "").strip()]
        prompt = (s.params or {}).get("prompt") or (dag.get("params") or {}).get("prompt") or ""
        if str(prompt).strip():
            instruction_parts.append(str(prompt).strip())
        artifacts: list[str] = []
        for ref in _artifact_refs_from_result(s.result, step_key=s.step_key):
            label = ref.get("fs_path") or ref.get("document_id") or ref.get("url") or ref.get("file_url")
            if label and str(label) not in artifacts:
                artifacts.append(str(label))
        error = ""
        if s.error:
            error = f"{s.error.get('type', '')}: {s.error.get('message', '')}".strip(": ")
        infos.append({
            "key": s.step_key,
            "kind": s.kind,
            "owner": s.service_key or s.action_key or "",
            "status": s.step_status,
            "attempts": s.attempt_count,
            "instruction": " — ".join(part for part in instruction_parts if part),
            "result": _supervisor_result_preview(s.result, max_chars=4000) if s.result else "",
            "artifacts": artifacts,
            "error": error,
        })
    return infos


def _supervisor_result_preview(result: Any, *, max_chars: int = 1200) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
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


class PlanExecutor:
    """Plan-level DAG orchestrator.

    Stateless — canonical state lives in execution_plans /
    execution_steps. Concurrent run_cycle calls for the same plan are
    safe: each cycle reads fresh state, the only mutations are
    idempotent (resolve refs into pending steps, mark sleep/human steps
    done) and the Dispatcher's atomic checkout prevents double-leasing.
    """

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or async_session

    async def run_cycle(self, plan_id: str) -> dict:
        """One pass over the plan. Returns ``{status, next_action,
        delay_seconds}`` so the caller (Celery ``run_plan`` task) can
        decide whether to re-enqueue and after how long."""
        announce_started = False
        sleep_seconds: float = 0.0
        chat_events: list[dict] = []

        async with self._session_factory() as db:
            plan = await self._load_plan(db, plan_id)
            if plan is None:
                return {"plan_id": plan_id, "status": "not_found", "next_action": "stop"}
            task_title = await self._load_task_title(db, plan)

            if plan.task_id:
                await runtime_ensure_task_billing_context(
                    db,
                    plan.task_id,
                    source=RUNTIME_PLAN_EXECUTOR_SOURCE,
                    model_role="worker",
                )
            else:
                runtime_ensure_plan_executor_billing_context(plan)

            if plan.status in (ExecutionPlanStatus.COMPLETED, ExecutionPlanStatus.FAILED, ExecutionPlanStatus.CANCELLED,):
                return {"plan_id": plan_id, "status": plan.status, "next_action": "stop"}

            if plan.status == ExecutionPlanStatus.PENDING_APPROVAL:
                return {
                    "plan_id": plan_id,
                    "status": "pending_approval",
                    "next_action": "wait_for_approval",
                }

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
                        self._snapshot_steps(steps), entity_id=plan.entity_id or "",
                    )
                    await self._task_log(db, plan, "plan_started",
                        f"▶ Execution plan started — {len(steps)} step(s)\n\n{dag_text}",
                        {"plan_id": plan.id, "step_count": len(steps), "execution_mode": plan.execution_mode})

            chat_ws = plan.workspace_id
            chat_entity = plan.entity_id
            chat_plan_id = plan.id
            chat_task_id = plan.task_id
            chat_mode = plan.execution_mode

            # Initial state load + early terminal check.
            steps = await self._all_steps(db, plan_id)
            terminal = self._terminal_summary(steps)
            if terminal == "completed":
                replanned = await self._maybe_replan_for_missing_artifact(db, plan, steps)
                if replanned:
                    await db.commit()
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event = await self._finalize(db, plan, "completed")
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                self._emit_task_event(task_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started, step_count=len(steps),
                    execution_mode=chat_mode, chat_events=[],
                    plan_done="completed",
                    plan_started_at=plan.started_at,
                    plan_completed_at=plan.completed_at,
                    plan_cost=(plan.cost_tracking or {}).get("usd"),
                    plan_error=None,
                    task_title=task_title,
                    step_snapshots=self._snapshot_steps(steps),
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
                    await db.commit()
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event = await self._finalize(db, plan, "failed")
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                self._emit_task_event(task_event)
                await self._announce(
                    chat_entity, chat_ws, chat_plan_id,
                    task_id=chat_task_id,
                    started=announce_started, step_count=len(steps),
                    execution_mode=chat_mode, chat_events=[],
                    plan_done="failed",
                    plan_started_at=plan.started_at,
                    plan_completed_at=plan.completed_at,
                    plan_cost=None,
                    plan_error=plan.last_error,
                    task_title=task_title,
                    step_snapshots=self._snapshot_steps(steps),
                    task_status=((task_event or {}).get("payload") or {}).get("task_status"),
                    task_issue=(
                        ((task_event or {}).get("payload") or {}).get("issue")
                        or ((task_event or {}).get("payload") or {}).get("prompt")
                    ),
                )
                return {"plan_id": plan_id, "status": "failed", "next_action": "stop"}

            steps_by_key = {s.step_key: s for s in steps}
            prior_results = self._collect_prior_results(steps)
            # Decide what to do with each runnable step.
            for step in self._pick_runnable(steps, steps_by_key):
                if step.kind == "sleep":
                    seconds = self._sleep_seconds(step.params or {})
                    sleep_seconds = max(sleep_seconds, seconds)
                    self._mark_done(step, {"slept": seconds}, None)

                elif step.kind == "human":
                    if step.human_input_response is not None:
                        self._mark_done(step, step.human_input_response, None)
                        step.human_input_prompt = None
                    else:
                        self._mark_waiting_human(step, str((step.params or {}).get("prompt") or ""))
                        chat_events.append({"kind": "step_needs_human", "step": step})
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
                        step.params = resolve_refs(step.params or {}, prior_results)
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

            await db.flush()

            # Re-evaluate terminal after the inline transitions.
            steps = await self._all_steps(db, plan_id)
            terminal = self._terminal_summary(steps)
            if terminal == "failed":
                replanned = await self._maybe_replan(db, plan, steps)
                if replanned:
                    await db.commit()
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
            if terminal in ("completed", "failed"):
                if terminal == "completed":
                    replanned = await self._maybe_replan_for_missing_artifact(db, plan, steps)
                    if replanned:
                        await db.commit()
                        return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event = await self._finalize(db, plan, terminal)
                if plan.status == ExecutionPlanStatus.RUNNING:
                    # The supervisor sent a step back for a re-run — the plan
                    # is live again, so nothing terminal gets announced here.
                    await db.commit()
                    return {"plan_id": plan_id, "status": "supervisor_step_retry", "next_action": "stop"}
                await db.commit()
                self._emit_task_event(task_event)
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
                    step_snapshots=self._snapshot_steps(steps),
                    task_status=_task_status_from_event(task_event),
                    task_issue=(
                        ((task_event or {}).get("payload") or {}).get("issue")
                        or ((task_event or {}).get("payload") or {}).get("prompt")
                    ),
                )
                return {"plan_id": plan_id, "status": terminal, "next_action": "stop"}

            inline_hitl_event = self._build_inline_hitl_event(plan, chat_events)
            await db.commit()
            self._emit_task_event(inline_hitl_event)

            # Decide re-enqueue cadence.
            if any(s.step_status == ExecutionStepStatus.WAITING_HUMAN for s in steps):
                # Plan is paused on operator input. Don't burn a cycle
                # slot — chat resolve_pending_action will wake us.
                next_action = "wait"
                delay = 0
            else:
                next_action = "schedule_self"
                delay = max(int(sleep_seconds), CYCLE_TICK_SECONDS)

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
            step_snapshots=self._snapshot_steps(steps),
        )

        return {
            "plan_id": plan_id,
            "status": "running",
            "next_action": next_action,
            "delay_seconds": delay,
        }

    # ── State helpers ────────────────────────────────────────────────

    @staticmethod
    def _sleep_seconds(params: dict) -> float:
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
    def _snapshot_steps(steps: list[ExecutionStep]) -> list[dict]:
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
                "result_summary": chat_notify.summarize_result_for_chat(s.result, max_chars=1200)
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
        step.result = result if isinstance(result, dict) else {"value": result}
        if cost:
            step.cost = cost
        step.finished_at = datetime.now(timezone.utc)
        step.error = None

    @staticmethod
    def _mark_failed(step: ExecutionStep, error: dict) -> None:
        # PlanExecutor only marks failed for inline kinds (sleep/human
        # don't fail; ref errors always terminal). Worker-driven kinds
        # use Dispatcher.fail_lease which honours retries.
        step.step_status = ExecutionStepStatus.FAILED.value
        step.error = error
        step.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _mark_waiting_human(step: ExecutionStep, prompt: Optional[str]) -> None:
        step.step_status = ExecutionStepStatus.WAITING_HUMAN.value
        step.human_input_prompt = prompt

    # ── Reads ────────────────────────────────────────────────────────

    @staticmethod
    async def _load_plan(db: AsyncSession, plan_id: str) -> Optional[ExecutionPlan]:
        return (await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.id == plan_id)
        )).scalar_one_or_none()

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
        return {
            s.step_key: s.result
            for s in steps
            if s.step_status == ExecutionStepStatus.DONE and s.result is not None
        }

    @staticmethod
    def _terminal_summary(steps: list[ExecutionStep]) -> Optional[str]:
        any_pending = any(s.step_status == ExecutionStepStatus.PENDING for s in steps)
        any_running = any(s.step_status == ExecutionStepStatus.RUNNING for s in steps)
        any_waiting = any(s.step_status == ExecutionStepStatus.WAITING_HUMAN for s in steps)
        any_paused = any(s.step_status == ExecutionStepStatus.PAUSED for s in steps)
        any_failed = any(s.step_status == ExecutionStepStatus.FAILED for s in steps)

        if any_pending or any_running or any_waiting or any_paused:
            return None
        if any_failed:
            return "failed"
        return "completed"

    MAX_REPLANS = 2

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

        If replanning: creates a new ExecutionPlan with parent_plan_id,
        dispatches run_plan, and returns True. Caller should NOT finalize
        the current plan as "failed" — instead mark it "replanned".

        Returns False if replanning is not possible/advisable.
        """
        if not plan.task_id:
            return False

        # Count prior plans for this task
        prior_count = (await db.execute(
            select(ExecutionPlan.id).where(
                ExecutionPlan.task_id == plan.task_id,
                ExecutionPlan.id != plan.id,
                ExecutionPlan.status.in_((ExecutionPlanStatus.COMPLETED, ExecutionPlanStatus.FAILED, ExecutionPlanStatus.CANCELLED, ExecutionPlanStatus.REPLANNED,)),
            )
        )).scalars().all()
        if len(prior_count) >= PlanExecutor.MAX_REPLANS:
            logger.info("Replan budget exhausted for task %s (%d prior plans)", plan.task_id, len(prior_count))
            return False

        # Don't replan on non-actionable errors (credits, permissions)
        failed_steps = [s for s in steps if s.step_status == ExecutionStepStatus.FAILED]
        for fs in failed_steps:
            err_type = (fs.error or {}).get("type", "")
            if err_type in ("CreditExhaustedError", "PermissionError", "AuthenticationError"):
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
            details["_replan_context"] = {
                "prior_plan_id": plan.id,
                "reason": reason,
                "issue": issue,
                "failed_steps": failure_context,
                "succeeded_steps": succeeded,
                "attempt": len(prior_count) + 1,
            }
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
            await db.flush()

            # Dispatch new planning cycle
            from packages.core.tasks.ai_tasks import plan_and_run_task
            plan_and_run_task.delay(plan.task_id)

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

            source_label = {
                SupervisorDecisionSource.GATE: "deterministic check",
                SupervisorDecisionSource.MODEL: "supervisor review",
                SupervisorDecisionSource.FALLBACK: "fallback — no review ran",
            }[decision.source]
            lines = [f"**Verdict:** {decision.verdict.value} ({source_label})"]
            if decision.evidence:
                lines.append(f"**Evidence:** {decision.evidence}")
            if decision.step_key:
                lines.append(f"**Step:** {decision.step_key}")

            if failed:
                names = ", ".join(s.step_key for s in failed[:5])
                lines.append(f"{len(failed)} step(s) failed: {names}.")
                if done:
                    lines.append(f"{len(done)} step(s) completed before that.")
            elif done and decision.verdict in (
                SupervisorVerdict.FAILED,
                SupervisorVerdict.NEEDS_REPLAN,
                SupervisorVerdict.CANCELLED,
                SupervisorVerdict.BLOCKED,
            ):
                lines.append(
                    f"All {len(done)} step(s) in this plan completed with no "
                    "error — this verdict overrides a mechanically successful run."
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
        if step is None:
            return False
        params = dict(step.params or {})
        if params.get(SUPERVISOR_STEP_RETRY_FLAG):
            return False
        params[SUPERVISOR_STEP_RETRY_FLAG] = True
        step.params = params  # reassignment marks the JSON column dirty

        step.step_status = ExecutionStepStatus.PENDING.value
        step.current_lease_id = None
        step.error = None
        step.result = None
        step.finished_at = None
        step.started_at = None
        step.attempt_count = 0
        step.human_input_prompt = None

        plan.status = ExecutionPlanStatus.RUNNING.value
        plan.completed_at = None
        plan.last_error = None

        await PlanExecutor._log_supervisor_verdict(
            db, task, plan, decision,
            note=f"Step '{decision.step_key}' was sent back for one re-run; the plan resumed.",
        )

        dispatched = False
        try:
            from packages.core.tasks.ai_tasks import run_plan
            run_plan.delay(plan.id)
            dispatched = True
        except Exception:
            logger.warning(
                "supervisor step retry: dispatch failed for plan %s (monitor will pick it up)",
                plan.id, exc_info=True,
            )
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
        steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.created_at)
        )).scalars().all())

        done_count = sum(1 for s in steps if s.step_status == ExecutionStepStatus.DONE)
        failed_count = sum(1 for s in steps if s.step_status == ExecutionStepStatus.FAILED)
        skipped_count = sum(1 for s in steps if s.step_status == ExecutionStepStatus.SKIPPED)

        # Load task before the fast path so artifact-bearing deliverables
        # cannot be marked complete just because every step returned text.
        from packages.core.models.task import Task
        task = (await db.execute(select(Task).where(Task.id == plan.task_id))).scalar_one_or_none()
        task_title = task.title if task else "Unknown"
        # The supervisor reviews a task a handful of times over its whole
        # life — the full description costs nothing next to the wrong
        # verdict a truncated one produces.
        task_desc = (task.description or "")[:2000] if task else ""

        from packages.core.constants.supervisor import MAX_EVIDENCE_CHARS

        def gate(verdict: SupervisorVerdict, evidence: str) -> SupervisorDecision:
            return SupervisorDecision(
                verdict=verdict,
                evidence=str(evidence)[:MAX_EVIDENCE_CHARS],
                source=SupervisorDecisionSource.GATE,
            )

        structured_issue = _structured_blocking_issue(task, steps)
        if structured_issue and plan_status == ExecutionPlanStatus.COMPLETED:
            logger.info(
                "Supervisor held plan %s for structured blocker: %s",
                plan.id, structured_issue,
            )
            return gate(SupervisorVerdict.NEEDS_HUMAN, structured_issue)
        artifact_issue = _missing_artifact_issue(task, steps)
        if artifact_issue and plan_status == ExecutionPlanStatus.COMPLETED:
            logger.info(
                "Supervisor requested replan for plan %s missing artifact evidence: %s",
                plan.id, artifact_issue,
            )
            return gate(SupervisorVerdict.NEEDS_REPLAN, artifact_issue)
        expects_issue = _unmet_expects_issue(plan, steps)
        if expects_issue and plan_status == ExecutionPlanStatus.COMPLETED:
            logger.info(
                "Supervisor requested replan for plan %s with unmet step expects: %s",
                plan.id, expects_issue,
            )
            return gate(SupervisorVerdict.NEEDS_REPLAN, expects_issue)

        # Do not let the LLM supervisor turn a totally failed execution into a
        # completed task. Replanning is attempted before finalization; once we
        # are here, a failed plan with zero successful steps has no core
        # deliverable to accept.
        if plan_status == ExecutionPlanStatus.FAILED and failed_count > 0 and done_count == 0:
            return gate(
                SupervisorVerdict.FAILED,
                f"all {failed_count} executed step(s) failed; none completed",
            )

        # Cancelled/blocked: pass through directly
        if plan_status in (SupervisorVerdict.CANCELLED, SupervisorVerdict.BLOCKED):
            return gate(
                SupervisorVerdict(plan_status), f"the plan was {plan_status}",
            )

        # Ask the supervisor before mapping a finished plan onto the parent
        # task. A plan can be mechanically complete while the worker result
        # says the actual task goal was not achieved; the supervisor judges the
        # result in context before the task status changes.
        try:
            step_infos = _supervisor_step_infos(plan, steps)
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
                s.step_key for s in steps
                if s.step_status in (ExecutionStepStatus.DONE, ExecutionStepStatus.FAILED)
                and not (s.params or {}).get(SUPERVISOR_STEP_RETRY_FLAG)
            ]

            completion = await runtime_execute_plan_supervisor_completion(
                task_title=task_title,
                task_description=task_desc,
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
            )
            decision = runtime_parse_plan_supervisor_decision(
                completion.content, retryable_step_keys=retryable_step_keys,
            )
            if decision is not None:
                logger.info(
                    "Supervisor verdict for plan %s: %s (%s)",
                    plan.id, decision.verdict.value, decision.evidence[:120],
                )
                return decision

        except Exception:
            logger.warning("Supervisor LLM call failed for plan %s, falling back to plan_status", plan.id, exc_info=True)

        # Fallback: use the raw plan status
        return SupervisorDecision(
            verdict=SupervisorVerdict(plan_status),
            evidence="the supervisor was unavailable or unparseable; the plan's own status was used",
            source=SupervisorDecisionSource.FALLBACK,
        )

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
        except Exception:
            logger.debug("config_versions stamping skipped", exc_info=True)
            return None

    @staticmethod
    async def _finalize(
        db: AsyncSession, plan: ExecutionPlan, status: str,
    ) -> Optional[dict]:
        plan.status = status
        plan.completed_at = datetime.now(timezone.utc)
        task_event: Optional[dict] = None

        # The plan is now terminal — no step will resume to consume its approval
        # request — so expire any still-open HitlRequest attached to it.
        # Without this, a step that was waiting_human when the plan was
        # cancelled/failed/replanned leaves an orphaned "no longer attached to a
        # waiting step" card. Best-effort: cleanup never blocks finalization.
        try:
            from packages.core.governance.approvals import resolve_origin_requests
            from packages.core.governance.service import resolve_stale_hitl_cards
            await resolve_origin_requests(db, plan_id=plan.id, reason="plan_terminal")
            # ... and close the chat cards that rendered those requests, so
            # they stop counting toward the sidebar pending-action badge.
            await resolve_stale_hitl_cards(db, plan_id=plan.id, reason="plan_terminal")
        except Exception:
            logger.warning("approval-request cleanup on finalize failed", exc_info=True)

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
                decision = await PlanExecutor._supervise_outcome(db, plan, status)

                if decision.verdict is SupervisorVerdict.RETRY_STEP:
                    # The supervisor may send ONE step back for a re-run
                    # instead of failing the whole task. If it applies, the
                    # plan is running again and nothing here is terminal.
                    if await PlanExecutor._apply_supervisor_step_retry(db, task, plan, decision):
                        return None
                    decision = decision.downgraded(
                        SupervisorVerdict.NEEDS_REPLAN,
                        "the named step could not be re-run (already retried once, or no longer present)",
                    )

                # Every verdict says why — evidence from the model or the
                # gate, plus what the code did with it.
                note = ""
                if decision.verdict is SupervisorVerdict.NEEDS_REPLAN:
                    note = (
                        "the replan budget for this task is exhausted, so the "
                        "requested replan did not run and the task lands as failed"
                    )
                await PlanExecutor._log_supervisor_verdict(db, task, plan, decision, note=note)
                supervisor_decision = decision

                if decision.verdict is SupervisorVerdict.COMPLETED:
                    await apply_task_status_transition(
                        task, "completed", db=db, config_versions=config_versions,
                    )
                elif decision.verdict is SupervisorVerdict.NEEDS_REPLAN:
                    # Replan was already attempted before _finalize.
                    # If we're here, budget is exhausted → fall to failed.
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
                        )
                        attention_issue = structured_issue or artifact_issue or message
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
                    except Exception:
                        pass
                else:
                    # FAILED / CANCELLED / BLOCKED — the decision is a member
                    # of a closed enum, so there is no "unknown" branch left:
                    # _supervise_outcome already folds an unparseable reply
                    # into a FALLBACK decision on the plan's own status.
                    await apply_task_status_transition(
                        task, decision.verdict.value, db=db,
                        config_versions=config_versions,
                    )

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
                for s in steps:
                    entry: dict = {
                        "key": s.step_key,
                        "kind": s.kind,
                        "status": s.step_status,
                    }
                    if s.result and isinstance(s.result, dict):
                        entry["result_summary"] = str(
                            s.result.get("text")
                            or s.result.get("memo_text")
                            or s.result.get("value")
                            or s.result.get("summary")
                            or s.result.get("result_summary")
                            or s.result.get("message")
                            or ""
                        )[:500]
                        # Capture file/document references from step results
                        refs = _artifact_refs_from_result(s.result, step_key=s.step_key)
                        if refs:
                            entry["files"] = refs
                            all_files.extend(refs)
                        if s.result.get("document_id"):
                            entry["document_id"] = s.result["document_id"]
                        if s.result.get("fs_path"):
                            entry["fs_path"] = s.result["fs_path"]
                    elif s.result:
                        entry["result_summary"] = str(s.result)[:500]
                    if s.error:
                        entry["error"] = {
                            "type": s.error.get("type", "unknown"),
                            "message": str(s.error.get("message", ""))[:300],
                        }
                    step_summaries.append(entry)

                actual_output = {
                    "plan_id": plan.id,
                    "plan_status": status,
                    "steps": step_summaries,
                    "files": _dedupe_task_artifact_refs(all_files) if all_files else None,
                }
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
                icon = "✓" if status == "completed" else "✗"
                msg = f"{icon} Plan {status}"
                if duration is not None:
                    msg += f" in {duration:.1f}s"
                if cost_usd:
                    msg += f" · ${cost_usd:.4f}"
                # Append DAG rendering
                from packages.core.workspace_chat.notifiers import _render_dag
                step_snaps = PlanExecutor._snapshot_steps(steps)
                if step_snaps:
                    msg += "\n\n" + _render_dag(step_snaps, entity_id=plan.entity_id or "")
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

                # Post a human-readable summary of what the task produced
                if status == "completed" and steps:
                    # Collect final deliverables from step results
                    deliverables = []
                    for s in steps:
                        if s.step_status == ExecutionStepStatus.DONE and s.result:
                            text = ""
                            if isinstance(s.result, dict):
                                text = s.result.get("text") or s.result.get("value") or ""
                            if isinstance(text, str) and text.strip():
                                step_label = getattr(s, "description", None) or s.step_key.replace("_", " ").title()
                                deliverables.append(f"### {step_label}\n\n{text.strip()}")
                    if deliverables:
                        summary = "## Task Completed\n\n" + "\n\n---\n\n".join(deliverables)
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
                "plan_id": plan.id,
                "plan_status": plan.status,
                "task_status": "in_progress",
                "step_ids": [s.id for s in steps],
                "prompt": hitl_events[0].get("prompt") or steps[0].human_input_prompt,
            },
        }

    @staticmethod
    def _emit_task_event(task_event: Optional[dict]) -> None:
        try:
            runtime_emit_plan_executor_task_event(task_event)
        except Exception:
            logger.debug("PlanExecutor: task event emit failed", exc_info=True)

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
                )
            elif evt["kind"] == "step_failed":
                await chat_notify.notify_step_failed(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, step_id=step.id, step_key=step.step_key,
                    error=evt.get("error"),
                    will_retry=evt.get("will_retry", False),
                    subscription_id=sub_id,
                )

        if plan_done == "completed":
            if task_status in _SUPERVISOR_HELD_TASK_STATUSES:
                await chat_notify.notify_plan_needs_attention(
                    entity_id=entity_id, workspace_id=workspace_id,
                    plan_id=plan_id, task_id=task_id,
                    task_title=task_title, issue=task_issue,
                    steps=step_snapshots,
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
                )
        elif plan_done == "failed":
            await chat_notify.notify_plan_failed(
                entity_id=entity_id, workspace_id=workspace_id,
                plan_id=plan_id, task_id=task_id, error=plan_error,
                task_title=task_title,
                steps=step_snapshots,
            )
