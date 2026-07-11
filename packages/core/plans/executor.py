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

from packages.core.database import async_session
from packages.core.ai.runtime import (
    RUNTIME_PLAN_EXECUTOR_SOURCE,
    runtime_emit_plan_executor_task_event,
    runtime_ensure_plan_executor_billing_context,
    runtime_ensure_task_billing_context,
    runtime_execute_plan_supervisor_completion,
    runtime_parse_plan_supervisor_verdict,
    runtime_record_plan_executor_task_evidence,
)
from packages.core.models.execution import ExecutionPlan, ExecutionStep
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
    "entries", "matches",
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

_STRUCTURED_BLOCKER_STATUSES = {
    "blocked",
    "error",
    "failed",
    "failure",
    "incomplete",
    "needs_attention",
    "needs_confirmation",
    "needs_human",
    "needs_input",
    "partial",
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
        if s.step_status == "done"
    )


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
        status = _structured_status_value(result.get(key))
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


def _structured_blocking_issue(task: Any | None, steps: list[ExecutionStep]) -> str | None:
    artifact_required = _task_requires_artifact(task)
    for step in steps:
        if step.step_status != "done" or not step.result:
            continue
        issue = _structured_result_blocker(
            step.result,
            artifact_required=artifact_required,
        )
        if issue:
            label = (getattr(step, "step_key", None) or "step").replace("_", " ")
            return f"{label}: {issue}"
    return None


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

            if plan.status in ("completed", "failed", "cancelled"):
                return {"plan_id": plan_id, "status": plan.status, "next_action": "stop"}

            if plan.status == "pending_approval":
                return {
                    "plan_id": plan_id,
                    "status": "pending_approval",
                    "next_action": "wait_for_approval",
                }

            if plan.status == "draft":
                plan.status = "running"
                plan.started_at = datetime.now(timezone.utc)
                announce_started = True
                await db.flush()
                # Log plan start to task
                if plan.task_id:
                    steps = await self._all_steps(db, plan_id)
                    from packages.core.workspace_chat.notifiers import _render_dag
                    dag_text = _render_dag(self._snapshot_steps(steps))
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
                )
                return {"plan_id": plan_id, "status": "completed", "next_action": "stop"}
            if terminal == "failed":
                # Try replanning before giving up
                replanned = await self._maybe_replan(db, plan, steps)
                if replanned:
                    await db.commit()
                    return {"plan_id": plan_id, "status": "replanned", "next_action": "stop"}
                task_event = await self._finalize(db, plan, "failed")
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

                elif step.kind in ("action", "llm", "subagent", "code"):
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

                elif step.kind in ("parallel_fanout", "gather"):
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
                )
                return {"plan_id": plan_id, "status": terminal, "next_action": "stop"}

            inline_hitl_event = self._build_inline_hitl_event(plan, chat_events)
            await db.commit()
            self._emit_task_event(inline_hitl_event)

            # Decide re-enqueue cadence.
            if any(s.step_status == "waiting_human" for s in steps):
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
        step.step_status = "done"
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
        step.step_status = "failed"
        step.error = error
        step.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def _mark_waiting_human(step: ExecutionStep, prompt: Optional[str]) -> None:
        step.step_status = "waiting_human"
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
            if s.step_status != "pending":
                continue
            deps = s.depends_on or []
            if not all(by_key[d].step_status == "done" for d in deps if d in by_key):
                # Mark blocked-by-failure dependents as skipped so the
                # plan can terminate. Otherwise just wait.
                if any(by_key[d].step_status in ("failed", "cancelled", "skipped")
                       for d in deps if d in by_key):
                    s.step_status = "skipped"
                    s.finished_at = datetime.now(timezone.utc)
                continue
            runnable.append(s)
        return runnable

    @staticmethod
    def _collect_prior_results(steps: list[ExecutionStep]) -> dict[str, Any]:
        return {
            s.step_key: s.result
            for s in steps
            if s.step_status == "done" and s.result is not None
        }

    @staticmethod
    def _terminal_summary(steps: list[ExecutionStep]) -> Optional[str]:
        any_pending = any(s.step_status == "pending" for s in steps)
        any_running = any(s.step_status == "running" for s in steps)
        any_waiting = any(s.step_status == "waiting_human" for s in steps)
        any_paused = any(s.step_status == "paused" for s in steps)
        any_failed = any(s.step_status == "failed" for s in steps)

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
                ExecutionPlan.status.in_(["completed", "failed", "cancelled", "replanned"]),
            )
        )).scalars().all()
        if len(prior_count) >= PlanExecutor.MAX_REPLANS:
            logger.info("Replan budget exhausted for task %s (%d prior plans)", plan.task_id, len(prior_count))
            return False

        # Don't replan on non-actionable errors (credits, permissions)
        failed_steps = [s for s in steps if s.step_status == "failed"]
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

        # Collect what succeeded (planner can reuse)
        succeeded = []
        for s in steps:
            if s.step_status == "done" and s.result:
                succeeded.append({
                    "step_key": s.step_key,
                    "result_summary": str(s.result.get("text", s.result.get("value", "")))[:200]
                        if isinstance(s.result, dict) else str(s.result)[:200],
                })

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
            plan.status = "replanned"
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
    async def _supervise_outcome(
        db: AsyncSession, plan: ExecutionPlan, plan_status: str,
    ) -> str:
        """Lightweight supervisor: reviews all step results after plan
        finishes and decides the task ticket status.

        Returns one of: completed, failed, needs_replan, needs_human.

        Deterministic gates handle structured blockers first. The supervisor
        then validates finished plan output before the parent task status is
        changed, including mechanically completed plans with all steps done.
        """
        steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
            .order_by(ExecutionStep.created_at)
        )).scalars().all())

        done_count = sum(1 for s in steps if s.step_status == "done")
        failed_count = sum(1 for s in steps if s.step_status == "failed")
        skipped_count = sum(1 for s in steps if s.step_status == "skipped")

        # Load task before the fast path so artifact-bearing deliverables
        # cannot be marked complete just because every step returned text.
        from packages.core.models.task import Task
        task = (await db.execute(select(Task).where(Task.id == plan.task_id))).scalar_one_or_none()
        task_title = task.title if task else "Unknown"
        task_desc = (task.description or "")[:300] if task else ""

        structured_issue = _structured_blocking_issue(task, steps)
        if structured_issue and plan_status == "completed":
            logger.info(
                "Supervisor held plan %s for structured blocker: %s",
                plan.id, structured_issue,
            )
            return "needs_human"
        artifact_issue = _missing_artifact_issue(task, steps)
        if artifact_issue and plan_status == "completed":
            logger.info(
                "Supervisor requested replan for plan %s missing artifact evidence: %s",
                plan.id, artifact_issue,
            )
            return "needs_replan"

        # Do not let the LLM supervisor turn a totally failed execution into a
        # completed task. Replanning is attempted before finalization; once we
        # are here, a failed plan with zero successful steps has no core
        # deliverable to accept.
        if plan_status == "failed" and failed_count > 0 and done_count == 0:
            return "failed"

        # Cancelled/blocked: pass through directly
        if plan_status in ("cancelled", "blocked"):
            return plan_status

        # Ask the supervisor before mapping a finished plan onto the parent
        # task. A plan can be mechanically "completed" while the worker result
        # says the actual task goal was not achieved; the supervisor judges the
        # result in context before the task status changes.
        try:
            # Build step summary for the supervisor
            step_lines = []
            for s in steps:
                line = f"- {s.step_key} ({s.kind}): {s.step_status}"
                if s.step_status == "done" and s.result:
                    text = _supervisor_result_preview(s.result)
                    line += f" — {str(text)[:150]}"
                if s.step_status == "failed" and s.error:
                    line += f" — ERROR: {s.error.get('type', '')}: {s.error.get('message', '')[:150]}"
                step_lines.append(line)

            completion = await runtime_execute_plan_supervisor_completion(
                task_title=task_title,
                task_description=task_desc,
                done_count=done_count,
                failed_count=failed_count,
                skipped_count=skipped_count,
                step_lines=step_lines,
                entity_id=plan.entity_id,
                workspace_id=getattr(plan, "workspace_id", None),
            )
            verdict_raw = completion.content

            verdict = runtime_parse_plan_supervisor_verdict(verdict_raw)
            if verdict in ("completed", "needs_replan", "needs_human", "failed"):
                logger.info("Supervisor verdict for plan %s: %s", plan.id, verdict)
                return verdict

        except Exception:
            logger.warning("Supervisor LLM call failed for plan %s, falling back to plan_status", plan.id, exc_info=True)

        # Fallback: use the raw plan status
        return plan_status

    @staticmethod
    async def _finalize(
        db: AsyncSession, plan: ExecutionPlan, status: str,
    ) -> Optional[dict]:
        plan.status = status
        plan.completed_at = datetime.now(timezone.utc)
        task_event: Optional[dict] = None

        # Auto-update the parent task status + aggregate output.
        # A lightweight supervisor reviews the step results and decides
        # the final task status: completed, failed, or needs_replan.
        if plan.task_id:
            from packages.core.models.task import Task
            from packages.core.services.task_service import add_task_log
            from packages.core.services.task_state_machine import TERMINAL_STATUSES, apply_task_status_transition
            verdict: Optional[str] = None
            result = await db.execute(
                select(Task).where(Task.id == plan.task_id)
            )
            task = result.scalar_one_or_none()
            if task and task.status in _PLAN_FINALIZABLE_TASK_STATUSES:
                verdict = await PlanExecutor._supervise_outcome(db, plan, status)
                if verdict == "completed":
                    apply_task_status_transition(task, "completed")
                elif verdict == "needs_replan":
                    # Replan was already attempted before _finalize.
                    # If we're here, budget is exhausted → fall to failed.
                    apply_task_status_transition(task, "failed")
                elif verdict == "needs_human":
                    apply_task_status_transition(task, "waiting_on_customer")
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
                                ExecutionStep.step_status == "failed",
                            )
                        )).scalars().all()]
                        issues = structured_issue or artifact_issue or "; ".join(
                            f"{fs.step_key}: {(fs.error or {}).get('message', 'failed')[:100]}"
                            for fs in failed_steps[:3]
                        ) or (
                            "The supervisor could not verify that the completed "
                            "plan actually satisfied the task objective."
                        )
                        await add_task_log(db, task.id, "ai_hitl_requested",
                            f"The plan ran into issues and needs your input:\n\n{issues}\n\n"
                            f"Please add a comment with guidance, or change the task status.",
                            created_by="AI Supervisor",
                            metadata={
                                "verdict": "needs_human",
                                "plan_id": plan.id,
                                "artifact_required": bool(artifact_issue),
                                "structured_blocker": bool(structured_issue),
                            })
                    except Exception:
                        pass
                elif verdict in ("failed", "cancelled", "blocked"):
                    apply_task_status_transition(task, verdict)
                else:
                    # Unknown verdict — fall back to plan status
                    if status == "completed":
                        apply_task_status_transition(task, "completed")
                    elif status == "failed":
                        apply_task_status_transition(task, "failed")

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
                if verdict:
                    actual_output["supervisor_verdict"] = verdict
                    if verdict == "needs_human":
                        actual_output["needs_input"] = True
                task.actual_output = actual_output
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
                    msg += "\n\n" + _render_dag(step_snaps)
                try:
                    failed_steps_for_meta = [s for s in steps if s.step_status in ("failed", "skipped", "cancelled")]
                    first_error = next((s.error for s in failed_steps_for_meta if s.error), None)
                    await add_task_log(db, task.id,
                        f"plan_{status}", msg,
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
                        if s.step_status == "done" and s.result:
                            text = ""
                            if isinstance(s.result, dict):
                                text = s.result.get("text") or s.result.get("value") or ""
                            if isinstance(text, str) and text.strip():
                                step_label = getattr(s, "description", None) or s.step_key.replace("_", " ").title()
                                deliverables.append(f"### {step_label}\n\n{text.strip()}")
                    if deliverables:
                        summary = "## Task Completed\n\n" + "\n\n---\n\n".join(deliverables)
                        try:
                            await add_task_log(db, task.id,
                                "comment", summary,
                                created_by="AI Agent",
                                metadata={"auto_summary": True})
                        except Exception:
                            pass

                event_type = None
                event_steps = steps
                if task.status == "completed":
                    event_type = "task.succeeded"
                    event_steps = [s for s in steps if s.step_status == "done"]
                elif task.status in ("failed", "cancelled", "blocked"):
                    event_type = "task.failed"
                    event_steps = [s for s in steps if s.step_status in ("failed", "skipped", "cancelled")]
                elif task.status == "waiting_on_customer":
                    event_type = "task.hitl_requested"
                    event_steps = [s for s in steps if s.step_status in ("waiting_human", "failed")]

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
                            "prompt": first_prompt,
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
            await add_task_log(db, plan.task_id, log_type, content, created_by="system", metadata=metadata)
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
