"""Producer-friendly helpers for posting chat messages.

PlanExecutor + measurement.py + Strategist call into these instead of
constructing Message rows directly. Each notifier encapsulates:
  * which conversation thread to use (main vs per-plan vs per-goal)
  * the message_kind enum value
  * the canonical body format
  * any pending_action card shape

Callers without a session retain the historical best-effort behavior. Step
terminal/HITL callers pass their transaction so execution state and its visible
receipt commit or roll back together.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.contracts.envelope import step_result_output_text
from packages.core.database import async_session
from packages.core.i18n import t
from packages.core.services.chat_feedback import ChatFeedbackTargetKind
from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    canonical_document_id,
    dedupe_generated_file_refs,
    entity_fs_open_url,
    generated_file_ref_url_keys,
)
from packages.core.workspace_chat import service as chat

logger = logging.getLogger(__name__)


# ── Plan lifecycle ────────────────────────────────────────────────────

# ── Step lifecycle ────────────────────────────────────────────────────

async def notify_step_done(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    kind: str,
    description: Optional[str],
    duration_seconds: Optional[float],
    cost_usd: Optional[float],
    subscription_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    result_summary: Optional[str] = None,
    result: Optional[dict] = None,
    locale: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    if not workspace_id:
        return
    # Human-readable step name
    label = description or step_key.replace("_", " ").title()
    # Agent attribution
    agent_part = t("task.execution.by_agent", locale=locale or "en", agent_name=agent_name) if agent_name else ""
    # Duration
    time_part = (
        t("task.execution.in_duration", locale=locale or "en", duration=_fmt_duration(duration_seconds))
        if duration_seconds and duration_seconds >= 1 else ""
    )

    summary = result_summary or summarize_result_for_chat(result, max_chars=1600, locale=locale)
    artifacts = extract_artifacts_for_chat(result)
    body = _render_step_completion_summary(
        label=label,
        agent_part=agent_part,
        time_part=time_part,
        summary=summary,
        artifacts=artifacts,
        max_summary_chars=1600,
        entity_id=entity_id,
        locale=locale,
    )
    attachments = _artifacts_as_attachments(artifacts, entity_id=entity_id)

    # 1. Detailed message in the plan thread (for drill-down)
    await _post_step_event(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
        attachments=attachments or None,
    )

    # 2. Human-readable receipt in the main workspace chat. Keep enough
    # detail here so the user does not have to open a hidden plan thread
    # just to understand what changed.
    main_body = _render_step_completion_summary(
        label=label,
        agent_part=agent_part,
        time_part=time_part,
        summary=summary,
        artifacts=artifacts,
        max_summary_chars=1000,
        entity_id=entity_id,
        locale=locale,
    )
    await _post_step_event(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=main_body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
        attachments=attachments or None,
    )


async def notify_step_failed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    error: Optional[dict],
    will_retry: bool,
    subscription_id: Optional[str] = None,
    locale: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    if not workspace_id:
        return
    msg = (error or {}).get("message") or t("task.execution.unknown_error", locale=locale or "en")
    suffix = t("task.execution.will_retry", locale=locale or "en") if will_retry else ""
    body = t("task.execution.step_failed", locale=locale or "en", step_key=step_key, error=msg, retry=suffix)

    # Plan thread (detailed)
    await _post_step_event(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )

    if will_retry:
        return

    # Main workspace chat (visible)
    await _post_step_event(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )


async def notify_step_needs_human(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    prompt: str,
    subscription_id: Optional[str] = None,
    pending_action: Optional[dict] = None,
    approval_request_id: Optional[str] = None,
    locale: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Lease-level HITL prompt — interactive message with a
    pending_action so the user can resolve it from the chat.

    ``pending_action`` is the optional structured payload from
    packages/core/ai/pending_action.py (kinds: ``needs_login``,
    ``needs_input``, ``needs_confirmation``). When provided, it's
    used verbatim and step_id / plan_id are merged in so the resolve
    endpoint can look up the originating step.

    When omitted, falls back to the historical free-form text-input
    card (``PendingActionKind.HUMAN_INPUT``) that asks the user to type
    a reply.

    ``approval_request_id`` is the unified HitlRequest row minted for
    this pause. It is merged into whichever card shape we end up with —
    the notifier is the single writer of that field — so answering the
    card can close the request instead of orphaning it.
    """
    if not workspace_id:
        return
    if pending_action and pending_action.get("kind"):
        # Caller provided a structured payload — merge step/plan refs
        # so the resolve endpoint can find the originating step.
        merged_action = {**pending_action, "step_id": step_id, "plan_id": plan_id}
        # Use the structured title as the message body when present;
        # otherwise fall back to the prompt text.
        title = merged_action.get("title") or prompt or t(
            "task.execution.needs_input_title", locale=locale or "en", step_key=step_key,
        )
        body = title
    else:
        merged_action = {
            "kind": PendingActionKind.HUMAN_INPUT.value,
            "step_id": step_id,
            "plan_id": plan_id,
            "input_schema": {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            },
        }
        body = t("task.execution.needs_input_body", locale=locale or "en", step_key=step_key, prompt=prompt)

    if approval_request_id:
        merged_action["approval_request_id"] = approval_request_id

    await _post_step_event(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=body,
        message_kind="hitl_request",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
        pending_action=merged_action,
    )


# ── Task lifecycle ────────────────────────────────────────────────────

async def notify_task_completed(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    task_title: Optional[str],
    summary: Optional[str],
    files: list[dict],
    subscription_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    locale: str | None = None,
) -> None:
    """Announce a completed Task in the Workspace chat, in the caller's transaction.

    One bubble from the agent carries the delivered files and the user's
    feedback on the result; one event row after it says who finished what.
    Both share a timestamp, so they are written in the order every client
    shows them.
    """
    subject = formatted_task_subject(task_title, task_id)
    headline = (
        t("task.execution.task_completed_by", locale=locale or "en", agent_name=agent_name, subject=subject)
        if agent_name
        else t("task.execution.task_completed", locale=locale or "en", subject=subject)
    )
    task_ref = {"type": "task", "id": task_id, **({"title": task_title} if task_title else {})}
    artifacts = [
        {"kind": "document", "value": ref["document_id"], "name": ref.get("name"), "fs_path": ref.get("fs_path")}
        if ref.get("document_id")
        else {"kind": "file", "value": ref.get("fs_path"), "name": ref.get("name")}
        for ref in files
        if isinstance(ref, dict) and (ref.get("document_id") or ref.get("fs_path"))
    ]
    attachments = _artifacts_as_attachments(artifacts, entity_id=entity_id)
    body = _clip_text(summary, 1600)
    if body or attachments:
        await _safe_post(
            db=db,
            entity_id=entity_id, workspace_id=workspace_id,
            body=body, message_kind="agent_update", author_kind="agent",
            author_subscription_id=subscription_id,
            refs=[task_ref],
            attachments=attachments or None,
            meta={"feedback_target_kind": ChatFeedbackTargetKind.TASK_COMPLETION.value},
        )
    await _safe_post(
        db=db,
        entity_id=entity_id, workspace_id=workspace_id,
        body=headline, message_kind="system", author_kind="system",
        refs=[task_ref],
    )


def formatted_task_subject(title: Optional[str], task_id: str) -> str:
    return _as_text(title) or f"#{task_id[-6:]}"


# ── Goal lifecycle ────────────────────────────────────────────────────

async def notify_goal_measured(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: Decimal,
    pace: Optional[str],
) -> None:
    """Quiet tick — only posted on non-trivial pace context. Frequent
    routine measurements (e.g. hourly) would spam the chat."""
    if not workspace_id:
        return
    if pace not in {"at_risk", "behind", "ahead"}:
        return
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"📊 {metric_key}: {value:g} ({pace})",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


async def notify_goal_pace_changed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: Decimal,
    prev_pace: Optional[str],
    new_pace: Optional[str],
) -> None:
    if not workspace_id:
        return
    icon = {"at_risk": "🔴", "behind": "🟡", "ahead": "🟢", "on_track": "🟢"}.get(new_pace or "", "ℹ")
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"{icon} Goal pace changed: {prev_pace or '?'} → {new_pace or '?'} (current {metric_key}={value:g})",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


async def notify_goal_achieved(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: Decimal,
) -> None:
    if not workspace_id:
        return
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"🎉 Goal achieved! {metric_key} reached {value:g}",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


# ── Internals ─────────────────────────────────────────────────────────

# ── Agent greetings ──────────────────────────────────────────────────

async def notify_agent_greeting(
    *,
    entity_id: str,
    workspace_id: str,
    subscription_id: str,
    greeting: str,
    sequence: int,
    total: int,
) -> None:
    """Post a greeting, then fan it out only after its transaction commits."""
    try:
        async with async_session() as db:
            message = await chat.post_message(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                body=greeting,
                message_kind="agent_update",
                author_kind="agent",
                author_subscription_id=subscription_id,
                meta={
                    "agent_greeting": True,
                    "agent_greeting_sequence": sequence,
                    "agent_greeting_total": total,
                },
                publish_event=False,
            )
            await db.commit()
            await chat.publish_workspace_chat_message_event(
                entity_id,
                workspace_id=workspace_id,
                message=message,
            )
    except Exception:
        logger.warning("workspace_chat post failed", exc_info=True)


def summarize_result_for_chat(
    result: Any, *, max_chars: int = 1200, locale: str | None = None,
) -> Optional[str]:
    """Best-effort user-facing summary from worker/LLM step output."""
    text = _extract_result_text(result, locale=locale)
    if not text:
        return None
    return _clip_text(_strip_boilerplate_completion_headings(text), max_chars)


def extract_artifacts_for_chat(result: Any) -> list[dict]:
    """Return user-visible files/URLs produced by a step result."""
    artifacts: list[dict] = []
    factory = ArtifactReferenceFactory()

    def add(kind: str, value: Any, *, name: Optional[str] = None, fs_path: str = "") -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value.startswith("data:"):
            return
        if kind == "document" and not canonical_document_id(value):
            return
        artifact = {"kind": kind, "value": value, "name": name}
        if kind == "document" and fs_path:
            artifact["fs_path"] = fs_path
        artifacts.append(artifact)

    def walk(obj: Any) -> None:
        if not isinstance(obj, dict):
            return

        def label_for(mapping: dict) -> Optional[str]:
            explicit = _as_text(mapping.get("name") or mapping.get("filename"))
            if explicit:
                return explicit
            location = _as_text(
                mapping.get("fs_path")
                or mapping.get("path")
                or mapping.get("file_path")
                or mapping.get("output_path")
            )
            if not location:
                return None
            return location.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

        identity = factory.inspect(obj)
        path_identity = factory.inspect({
            key: obj.get(key) for key in ("fs_path", "path", "file_path", "output_path")
        })
        doc = obj.get("document")
        if isinstance(doc, dict):
            child = {**doc, "document_id": doc.get("document_id") or doc.get("id")}
            child_identity = factory.inspect(child)
            if (
                child_identity.document_id
                and not child_identity.fs_path
                and path_identity.fs_path
                and identity.document_id in {"", child_identity.document_id}
            ):
                # The nested Document describes this parent's materialized file;
                # retain that explicit association, not a filename heuristic.
                child["fs_path"] = path_identity.fs_path
            walk(child)

        object_name = label_for(obj)
        if identity.document_id:
            add("document", identity.document_id, name=object_name, fs_path=identity.fs_path)
        elif path_identity.fs_path:
            # Address fields on one file record are alternatives, not outputs.
            # The shared factory selects fs_path before relative path aliases.
            add("file", path_identity.fs_path, name=object_name)

        reference_url_keys = generated_file_ref_url_keys(obj)
        for key, kind in (
            ("open_url", "url"),
            ("openUrl", "url"),
            ("viewer_url", "url"),
            ("previewUrl", "url"),
            ("preview_url", "url"),
            ("file_url", "url"),
            ("document_url", "url"),
            ("image_url", "url"),
            ("video_url", "url"),
            ("result_url", "url"),
            ("url", "url"),
            ("job_id", "job"),
        ):
            if key in {"previewUrl", "preview_url"} and key not in reference_url_keys:
                continue
            if kind == "url" and (
                identity.document_id
                or (path_identity.fs_path and key not in {"image_url", "video_url"})
            ):
                continue
            add(kind, obj.get(key), name=object_name)

        for key in ("files", "artifacts", "knowledge_artifacts", "outputs", "documents"):
            values = obj.get(key)
            if isinstance(values, list):
                for item in values:
                    walk(item)
            elif isinstance(values, dict):
                walk(values)

        for key in ("summary", "message", "text", "content", "output", "value"):
            nested = _parse_json_if_string(obj.get(key))
            if isinstance(nested, dict):
                walk(nested)

    parsed = _parse_json_if_string(result)
    walk(parsed)
    return _unique_artifacts(artifacts)[:8]


def _render_step_completion_summary(
    *,
    label: str,
    agent_part: str,
    time_part: str,
    summary: Optional[str],
    artifacts: list[dict],
    max_summary_chars: int,
    entity_id: str = "",
    locale: str | None = None,
) -> str:
    lines = [t("task.execution.step_completed", locale=locale or "en", label=label, agent_part=agent_part, time_part=time_part)]
    artifacts = _unique_artifacts(artifacts)

    clean_summary = _clip_text(_as_text(summary), max_summary_chars)
    if clean_summary:
        lines.append("")
        lines.append(t("task.execution.what_produced", locale=locale or "en"))
        for item in _summary_bullets(clean_summary):
            lines.append(f"- {item}")

    if artifacts:
        lines.append("")
        lines.append(t("task.execution.files_saved", locale=locale or "en"))
        lines.extend(
            f"- {_format_artifact(artifact, entity_id=entity_id, locale=locale)}"
            for artifact in artifacts[:6]
        )

    if not clean_summary and not artifacts:
        lines.append("")
        lines.append(t("task.execution.step_successful", locale=locale or "en"))

    return "\n".join(lines)


def _extract_result_text(result: Any, *, locale: str | None = None) -> Optional[str]:
    parsed = _parse_json_if_string(result)
    if isinstance(parsed, dict):
        outputs = parsed.get("outputs")
        if isinstance(outputs, dict) and isinstance(outputs.get("text"), str):
            text = step_result_output_text(parsed)
            if text:
                return text
        for key in ("summary", "message", "text", "content", "output", "value"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                nested = _parse_json_if_string(value)
                if isinstance(nested, dict) and nested is not value:
                    nested_text = _extract_result_text(nested, locale=locale)
                    if nested_text:
                        return nested_text
                return value.strip()
        if isinstance(parsed.get("document"), dict):
            doc = parsed["document"]
            name = doc.get("name") or doc.get("fs_path") or doc.get("id")
            if name:
                return t("task.execution.saved_document", locale=locale or "en", name=name)
        return None
    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip()
    return None


def _parse_json_if_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _summary_bullets(summary: str) -> list[str]:
    lines = [line.strip(" -•\t") for line in summary.splitlines()]
    lines = [line for line in lines if line and not _is_markdown_rule(line)]
    if len(lines) > 1:
        return lines[:8]
    return [summary]


def _strip_boilerplate_completion_headings(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        normalized = line.lower().strip("# -*✅✓")
        if normalized.startswith("task complete") or normalized.startswith("step complete"):
            continue
        if normalized.startswith("step ") and (
            "complete" in normalized[:80] or "delivered" in normalized[:80]
        ):
            continue
        if normalized.startswith("completed:") and len(normalized) < 80:
            continue
        lines.append(raw)
    return "\n".join(lines).strip() or text.strip()


def _format_artifact(artifact: dict, *, entity_id: str = "", locale: str | None = None) -> str:
    """Render one produced file as something the reader can open.

    This used to print the path inside backticks, which is inline code: a
    name, not an address. The UI only builds a file card from a reference it
    can actually resolve, so a real deliverable arrived as grey monospace
    text that led nowhere. Naming a file is not delivering it — the link has
    to be the address the step actually returned.
    """
    kind = artifact.get("kind") or "output"
    value = _as_text(artifact.get("value")) or "output"
    name = _as_text(artifact.get("name")) or value.rstrip("/").rsplit("/", 1)[-1] or value
    kind_label = (
        t(f"task.execution.artifact_{kind}", locale=locale or "en")
        if kind in {"document", "file", "url", "job", "output"} else kind.title()
    )

    href = ""
    if kind == "document" and canonical_document_id(value):
        href = f"/viewer/{value}"
    elif kind == "url":
        href = value
    elif kind == "file" and entity_id:
        href = entity_fs_open_url(entity_id, value)

    if href:
        return f"{kind_label}: [{name}]({href})"
    # No address to link — say the name plainly rather than dress a
    # non-address up as one.
    return f"{kind_label}: `{value}`"


def _artifacts_as_attachments(
    artifacts: Any,
    *,
    entity_id: str = "",
) -> list[dict]:
    """Persist produced files as the same structured chat contract used by direct turns."""
    attachments: list[dict] = []
    for artifact in _unique_artifacts(artifacts or []):
        kind = _as_text(artifact.get("kind")) or "output"
        value = _as_text(artifact.get("value"))
        if not value or kind not in {"file", "document", "url"}:
            continue
        if kind == "document" and not canonical_document_id(value):
            continue
        name = (
            _as_text(artifact.get("name"))
            or value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            or value
        )
        attachment = {"name": name, "type": "knowledge"}
        if "." in name:
            attachment.setdefault("fileType", name.rsplit(".", 1)[-1].lower()[:20])
        if kind == "document":
            attachment["document_id"] = value
            attachment["openUrl"] = f"/viewer/{value}"
        elif kind == "url":
            attachment["previewUrl"] = value
            attachment["openUrl"] = value
        path = value if kind == "file" else _as_text(artifact.get("fs_path"))
        if path:
            attachment["fsPath"] = path
            if address := entity_fs_open_url(entity_id, path):
                attachment["previewUrl"] = address
                attachment.setdefault("openUrl", address)
        attachments.append(attachment)
    def display_priority(attachment: dict) -> int:
        if attachment.get("fileType") not in {"mp4", "mov", "webm", "m4v", "avi", "mkv", "mpeg", "mpg"}:
            return 2
        preview = re.search(
            r"(?:^|[\s_.\/-])(?:preview|draft|raw|source|clean[-_ ]?master|clip|segment|shot|s\d{2})(?:$|[\s_.\/-])|预览|草稿|片段|素材",
            attachment["name"], re.IGNORECASE,
        )
        return 1 if preview else 0

    # Final videos must remain visible even when supporting files fill the cap.
    # Reorder only this output projection, never the saved execution artifacts.
    return sorted(attachments, key=display_priority)[:8]


def _unique_artifacts(artifacts: Any) -> list[dict]:
    """Use the same durable identities for step, plan, and attachment views."""
    refs: list[dict] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        kind = _as_text(artifact.get("kind")) or "output"
        value = _as_text(artifact.get("value"))
        if not value:
            continue
        ref = {**artifact, "kind": kind, "value": value}
        if kind == "document":
            if not canonical_document_id(value):
                continue
            ref["document_id"] = value
        elif kind == "file":
            ref["fs_path"] = value
        elif kind == "url":
            ref["url"] = value
        refs.append(ref)

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    factory = ArtifactReferenceFactory()
    for ref in dedupe_generated_file_refs(refs):
        identity = factory.inspect(ref)
        kind, value = ref["kind"], ref["value"]
        if identity.document_id:
            kind, value = "document", identity.document_id
        elif identity.fs_path and kind == "file":
            kind, value = "file", identity.fs_path
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        artifact = {"kind": kind, "value": value, "name": _as_text(ref.get("name"))}
        if kind == "document" and identity.fs_path:
            artifact["fs_path"] = identity.fs_path
        out.append(artifact)
    return out


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _clip_text(text: Optional[str], max_chars: int) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)].rstrip() + "…"


def _is_markdown_rule(line: str) -> bool:
    return set(line) <= {"-", "_", "*"} and len(line) >= 3


async def _safe_post(
    *,
    db: AsyncSession | None = None,
    **kwargs,
) -> None:
    """Post best-effort, or atomically when the caller supplies ``db``."""

    if db is not None:
        message = await chat.post_message(db, publish_event=False, **kwargs)
        chat.queue_workspace_chat_message_event_after_commit(
            db,
            entity_id=str(kwargs["entity_id"]),
            workspace_id=str(kwargs["workspace_id"]),
            message=message,
        )
        return
    try:
        async with async_session() as db:
            await chat.post_message(db, **kwargs)
            await db.commit()
    except Exception:
        logger.warning("workspace_chat post failed", exc_info=True)


async def _post_step_event(
    *,
    db: AsyncSession | None,
    **kwargs,
) -> None:
    """Persist dispatcher receipts in the caller's state transaction.

    Other notifier callers keep the historical best-effort own-session path.
    Dispatcher terminal/HITL transitions pass their session so the Message and
    lease/step state either commit together or roll back together. Realtime is
    queued only after that commit; clients can always recover from the durable
    row by refetching.
    """

    await _safe_post(db=db, **kwargs)


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
