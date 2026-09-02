from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.output_policy import (
    PROVIDER_REASONING_META_KEY,
    runtime_strip_leaked_tool_activity,
)
from packages.core.ai.runtime import (
    runtime_execute_conversation_summary_completion,
    runtime_conversation_summary_text,
)
from packages.core.ai.runtime.streams import runtime_persisted_tool_calls_history_summary
from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text

logger = logging.getLogger(__name__)

HISTORY_TOKEN_BUDGET = 80_000
SUMMARY_TRIGGER = 10
MAX_HISTORY_ROWS = 200
CHAT_MODE_MARKER_RE = re.compile(r"^\[Mode:\s*.+?\]\s*$", re.IGNORECASE)
CHAT_MODE_SETTINGS_MARKER_RE = re.compile(r"^\[Mode settings:\s*\{.*\}\]\s*$", re.IGNORECASE)
ATTACHMENT_HISTORY_MARKER = "[Attached file context]"
ATTACHMENT_HISTORY_INSTRUCTION = (
    "Use these exact persisted attachment refs; do not search Knowledge by filename "
    "when document_id, path, or url is provided."
)
GENERATIVE_TOOL_HISTORY_NAMES = {
    "generate_file",
    "generate_image",
    "generate_video",
    "generate_audio",
}


class AttachmentReferenceKind(StrEnum):
    """Persisted attachment kinds that represent a Knowledge document."""

    KNOWLEDGE = "knowledge"
    KNOWLEDGE_DOCUMENT = "knowledge_document"
    CHAT_UPLOAD = "chat_upload"


_DOCUMENT_ATTACHMENT_KINDS = frozenset(kind.value for kind in AttachmentReferenceKind)


def is_running_stream_placeholder(message: Any) -> bool:
    meta = message.meta or {}
    return (
        message.role == "assistant"
        and meta.get("stream_status") in {"running", "streaming"}
    )


def strip_leaked_tool_activity(content: str) -> str:
    """Compatibility wrapper for runtime-owned output cleanup."""

    return runtime_strip_leaked_tool_activity(content)


def strip_chat_mode_history_markers(content: str) -> str:
    """Remove UI-only chat-mode labels before history is sent to the model."""

    lines = [
        line
        for line in str(content or "").splitlines()
        if not CHAT_MODE_MARKER_RE.match(line.strip())
        and not CHAT_MODE_SETTINGS_MARKER_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


def _attachment_identity(attachment: dict[str, Any]) -> tuple[str, str, str, str]:
    name = str(
        attachment.get("name")
        or attachment.get("filename")
        or attachment.get("title")
        or ""
    ).strip().lower()
    document_id = str(attachment.get("document_id") or "").strip().lower()
    path = str(
        attachment.get("path")
        or attachment.get("fs_path")
        or attachment.get("fsPath")
        or ""
    ).strip().lower()
    url = str(
        attachment.get("url")
        or attachment.get("file_url")
        or attachment.get("previewUrl")
        or attachment.get("preview_url")
        or attachment.get("openUrl")
        or attachment.get("open_url")
        or ""
    ).strip().lower()
    return document_id, path, url, name


def _attachment_document_id(attachment: dict[str, Any]) -> str:
    """Return an authoritative document id from a persisted attachment.

    ``document_id`` is canonical and self-describing. Historical aliases are
    accepted only when the attachment kind identifies a Knowledge-backed
    reference; a generic ``id`` may belong to any external attachment type.
    """
    document_id = str(attachment.get("document_id") or "").strip()
    if document_id:
        return document_id

    kind = str(
        attachment.get("kind") or attachment.get("type") or ""
    ).strip().casefold()
    if kind not in _DOCUMENT_ATTACHMENT_KINDS:
        return ""
    return str(
        attachment.get("documentId")
        or attachment.get("doc_id")
        or attachment.get("id")
        or ""
    ).strip()


def _attachment_context_line(attachment: dict[str, Any]) -> str | None:
    name = str(
        attachment.get("name")
        or attachment.get("filename")
        or attachment.get("title")
        or ""
    ).strip()
    if not name:
        return None

    kind = str(attachment.get("kind") or attachment.get("type") or "").strip()
    parts: list[str] = []
    if kind:
        parts.append(kind)
    document_id = str(attachment.get("document_id") or "").strip()
    if document_id:
        parts.append(f"document_id={document_id}")
    path = str(
        attachment.get("path")
        or attachment.get("fs_path")
        or attachment.get("fsPath")
        or ""
    ).strip()
    if path:
        parts.append(f"path={path}")
    url = str(
        attachment.get("url")
        or attachment.get("file_url")
        or attachment.get("previewUrl")
        or attachment.get("preview_url")
        or attachment.get("openUrl")
        or attachment.get("open_url")
        or ""
    ).strip()
    if url:
        parts.append(f"url={url}")
    if not parts:
        parts.append("attachment")
    return f"- {name} ({'; '.join(parts)})"


def _render_attachment_context(attachments: list[dict[str, Any]]) -> str | None:
    lines = [
        line
        for line in (
            _attachment_context_line(attachment)
            for attachment in attachments
        )
        if line
    ]
    if not lines:
        return None
    return f"{ATTACHMENT_HISTORY_MARKER}\n{ATTACHMENT_HISTORY_INSTRUCTION}\n" + "\n".join(lines)


def _message_content_with_attachment_context(message: Any) -> str:
    content = str(getattr(message, "content", "") or "").strip()
    attachment_refs = _normalize_message_attachment_refs(
        getattr(message, "attachments", None)
    )
    attachment_context = _render_attachment_context(attachment_refs)
    if attachment_context:
        return f"{content}\n\n{attachment_context}" if content else attachment_context
    return content


def _normalize_message_attachment_refs(attachments: Any) -> list[dict[str, Any]]:
    if isinstance(attachments, dict):
        raw_attachments = [attachments]
    elif isinstance(attachments, list):
        raw_attachments = attachments
    else:
        raw_attachments = []
    return [attachment for attachment in raw_attachments if isinstance(attachment, dict)]


async def _resolve_message_attachment_refs(
    db: AsyncSession,
    conversation: Any,
    message: Any,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add_ref(ref: dict[str, Any]) -> None:
        identity = _attachment_identity(ref)
        if identity in seen:
            return
        if not any(identity):
            return
        seen.add(identity)
        refs.append(ref)

    raw_attachments = _normalize_message_attachment_refs(
        getattr(message, "attachments", None)
    )
    if raw_attachments:
        for raw in raw_attachments:
            ref = dict(raw)
            document_id = _attachment_document_id(ref)
            if document_id:
                try:
                    from packages.core.models.document import Document

                    doc = (
                        await db.execute(
                            select(Document).where(
                                Document.id == document_id,
                                Document.entity_id == conversation.entity_id,
                                Document.is_trashed == False,  # noqa: E712
                            ).limit(1)
                        )
                    ).scalar_one_or_none()
                except Exception:
                    doc = None
                if doc:
                    ref = _document_attachment_ref(doc, entity_id=conversation.entity_id)
                else:
                    # An id-bearing attachment represents a Knowledge document,
                    # not an arbitrary filesystem hint. Do not replay stale or
                    # cross-entity paths when the authoritative row is absent.
                    continue
            add_ref(ref)

    return refs


def _document_attachment_ref(doc: Any, *, entity_id: str) -> dict[str, Any]:
    fs_path = str(getattr(doc, "fs_path", "") or "").strip() or None
    file_url = str(getattr(doc, "file_url", "") or "").strip() or None
    url = f"/api/v1/fs/{entity_id}/{fs_path}" if fs_path else file_url
    source = str(getattr(doc, "source", "") or "").strip().lower()
    kind = "chat_upload" if source == "chat_upload" else "knowledge_document"
    return {
        "kind": kind,
        "name": getattr(doc, "name", None),
        "document_id": getattr(doc, "id", None),
        "path": fs_path,
        "url": url,
        "mime": getattr(doc, "mime_type", None),
        "file_type": getattr(doc, "file_type", None),
    }


COMPLETED_TOOL_ACTIVITY_FRAME = (
    "[Completed tool activity from the previous turn — already done, "
    "do not re-run these actions; reuse their results as context]"
)


def should_include_previous_tool_activity_for_turn(
    latest_user_message: str | None,
    tool_summary: str | None,
    *,
    is_last_assistant_message: bool = False,
) -> bool:
    """Decide whether prior tool activity is safe to replay into this turn.

    Persisted tool summaries are operational traces, not user-visible memory.
    When a new user turn is being built, older tool traces are dropped so
    stale queued/search actions are not interpreted as active work. The most
    recent assistant turn's trace IS kept (framed as completed): dropping it
    forces the model to redo every lookup the previous turn just finished.
    """

    if not tool_summary:
        return False
    if not str(latest_user_message or "").strip():
        return True
    return is_last_assistant_message


def should_include_tool_history_summary(message: Any) -> bool:
    """Keep prior tool context, except generation calls that can re-trigger old intent."""

    raw = getattr(message, "tool_calls", None)
    if not raw:
        return False
    calls = raw if isinstance(raw, list) else [{"name": name, "result": result} for name, result in raw.items()] if isinstance(raw, dict) else []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if name in GENERATIVE_TOOL_HISTORY_NAMES:
            return False
    return True


async def load_conversation_history(
    db: AsyncSession,
    conversation_id: str,
    *,
    token_budget: int = HISTORY_TOKEN_BUDGET,
    latest_user_message: str | None = None,
) -> list[dict]:
    """Load conversation history within a token budget."""

    msgs = [
        m
        for m in await _list_messages_for_history(db, conversation_id, limit=MAX_HISTORY_ROWS)
        if not is_running_stream_placeholder(m)
    ]
    if not msgs:
        return []

    conv = await _get_conversation_for_history(db, conversation_id)

    used_tokens = 0
    selected: list[tuple[Any, list[dict[str, Any]], str | None]] = []

    for m in reversed(msgs):
        meta = m.meta or {}
        tool_summary = runtime_persisted_tool_calls_history_summary(m.tool_calls)
        attachment_refs = (
            await _resolve_message_attachment_refs(db, conv, m)
            if conv is not None
            else []
        )
        attachment_context = _render_attachment_context(attachment_refs)
        msg_tokens = (
            runtime_estimate_tokens_for_text(m.content or "")
            + runtime_estimate_tokens_for_text(tool_summary or "")
            + runtime_estimate_tokens_for_text(attachment_context or "")
            + runtime_estimate_tokens_for_text(
                str(meta.get(PROVIDER_REASONING_META_KEY) or "")
            )
        )
        if used_tokens + msg_tokens > token_budget and selected:
            break
        used_tokens += msg_tokens
        selected.append((m, attachment_refs, attachment_context))

    selected.reverse()

    history: list[dict] = []
    if len(selected) < len(msgs):
        if conv and conv.summary:
            history.append({
                "role": "system",
                "content": f"[Earlier conversation summary]\n{conv.summary}",
            })

        dropped = len(msgs) - len(selected)
        if dropped >= SUMMARY_TRIGGER:
            try:
                # Snapshot before scheduling: the task outlives this request,
                # so it must not touch the request-scoped session or its ORM rows.
                snapshots = [
                    SimpleNamespace(
                        role=m.role,
                        content=_message_content_with_attachment_context(m),
                    )
                    for m in msgs[:dropped]
                ]
                asyncio.create_task(
                    update_conversation_summary(conversation_id, snapshots)
                )
            except Exception:
                pass

    last_assistant_idx = next(
        (
            idx
            for idx in range(len(selected) - 1, -1, -1)
            if selected[idx][0].role == "assistant"
        ),
        None,
    )
    has_new_user_message = bool(str(latest_user_message or "").strip())

    for idx, (m, _attachment_refs, attachment_context) in enumerate(selected):
        content = strip_chat_mode_history_markers(
            strip_leaked_tool_activity(m.content or "")
        )
        if attachment_context:
            content = f"{content}\n\n{attachment_context}" if content else attachment_context
        tool_summary = (
            runtime_persisted_tool_calls_history_summary(m.tool_calls)
            if should_include_tool_history_summary(m)
            else None
        )
        if tool_summary and not should_include_previous_tool_activity_for_turn(
            latest_user_message,
            tool_summary,
            is_last_assistant_message=idx == last_assistant_idx,
        ):
            tool_summary = None
        if tool_summary:
            if has_new_user_message:
                tool_summary = f"{COMPLETED_TOOL_ACTIVITY_FRAME}\n{tool_summary}"
            content = f"{content}\n\n{tool_summary}" if content else tool_summary
        entry: dict = {
            "role": m.role,
            "content": content,
        }
        meta = m.meta or {}
        reasoning_content = meta.get(PROVIDER_REASONING_META_KEY)
        if (
            m.role == "assistant"
            and isinstance(reasoning_content, str)
            and reasoning_content.strip()
        ):
            entry["reasoning_content"] = reasoning_content
        history.append(entry)
    return history


async def update_conversation_summary(
    conversation_id: str,
    dropped_messages: list[Any],
) -> None:
    """Roll dropped messages into the stored conversation summary.

    Runs fire-and-forget after the originating request, so it opens its own
    session. The prior summary is merged into the new one — regenerating from
    only the latest dropped window used to silently discard everything the
    previous summary covered.
    """

    try:
        from packages.core.database import async_session

        text_block = runtime_conversation_summary_text(dropped_messages)
        if not text_block:
            return

        async with async_session() as db:
            conv_row = await _get_conversation_for_history(db, conversation_id)
            if conv_row is None:
                return
            prior_summary = (conv_row.summary or "").strip() or None

            completion = await runtime_execute_conversation_summary_completion(
                entity_id=conv_row.entity_id,
                workspace_id=conv_row.workspace_id,
                text_block=text_block,
                prior_summary=prior_summary,
            )
            summary = completion.content
            if summary and summary.strip():
                conv = await _get_conversation_for_history(db, conversation_id)
                if conv:
                    conv.summary = summary.strip()
                    await db.commit()
                    logger.info(
                        "Updated conversation summary for %s (%d chars)",
                        conversation_id,
                        len(conv.summary),
                    )
    except Exception:
        logger.warning("Failed to update conversation summary", exc_info=True)


async def _list_messages_for_history(
    db: AsyncSession,
    conversation_id: str,
    *,
    limit: int,
) -> list[Any]:
    from packages.core.models.task import Message

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    msgs = list(result.scalars().all())
    msgs.reverse()
    return msgs


async def _get_conversation_for_history(
    db: AsyncSession,
    conversation_id: str,
) -> Any | None:
    from packages.core.models.task import Conversation

    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    return result.scalar_one_or_none()
