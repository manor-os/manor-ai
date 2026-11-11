"""Readable email projection shared by the mail MCP servers.

Provider APIs return transport headers, encoded MIME parts and both a text
and an HTML copy of the body. A model needs the conversation, not the
envelope: these helpers reduce a message to sender, recipients, subject,
date, one decoded text body and an attachment list, and page long bodies
instead of cutting them.

A single message uses the loop's paged-text shape (``content`` + ``offset`` /
``next_offset`` / ``returned_chars``) so a context cut keeps the
continuation offset truthful.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Dict, List

from packages.core.services.web_text import html_to_text

BODY_PAGE_CHARS = 10_000
THREAD_BODY_BUDGET_CHARS = 30_000
THREAD_MIN_BODY_CHARS = 1_500

_QUOTE_START = re.compile(
    r"^(On .{4,300} wrote:|在.{2,200}写道[:：]|-{2,}\s*Original Message\s*-{2,}|"
    r"-{2,}\s*Forwarded message\s*-{2,}|From: .+\n(Sent|Date): )",
    re.IGNORECASE | re.MULTILINE,
)


def body_text(plain: str, html: str) -> str:
    """One readable body: the text part, else the HTML part as text."""
    if plain and plain.strip():
        return plain.strip()
    if html and html.strip():
        return html_to_text(html)
    return ""


def without_quoted_history(text: str, known_bodies: List[str]) -> tuple[str, bool]:
    """Drop a quoted tail only when another message contains the same body."""
    match = _QUOTE_START.search(text)
    if not match:
        return text, False

    def normalized(value: str) -> str:
        return " ".join(re.sub(r"^\s*>+\s?", "", line) for line in value.splitlines()).strip()

    tail = normalized(text[match.end():])
    if tail and any(tail == normalized(body) for body in known_bodies):
        return text[:match.start()].rstrip(), True
    return text, False


def page_offset(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("offset must be a non-negative integer")
    try:
        offset = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset must be a non-negative integer") from exc
    if offset < 0:
        raise ValueError("offset must be a non-negative integer")
    return offset


def paged_message(fields: Dict[str, Any], body: str, *, offset: int = 0) -> Dict[str, Any]:
    """A single message whose body is served one page at a time."""
    page = body[offset: offset + BODY_PAGE_CHARS]
    next_offset = offset + len(page)
    out = dict(fields)
    out.update({
        "content": page,
        "offset": offset,
        "returned_chars": len(page),
        "total_chars": len(body),
        "next_offset": next_offset if next_offset < len(body) else None,
    })
    if out["next_offset"] is not None:
        out["hint"] = (
            f"The body continues. Call this tool again with offset={next_offset} "
            "to read the rest before relying on this message."
        )
    return out


def thread_messages(items: List[tuple[Dict[str, Any], str]]) -> List[Dict[str, Any]]:
    """Messages of one conversation, each without the history it quotes.

    Only verified copies of another message are removed. Unique forwarded
    content and blockquotes remain. A body still longer than its share of
    the budget is cut and says how to read it whole.
    """
    share = max(THREAD_MIN_BODY_CHARS, THREAD_BODY_BUDGET_CHARS // max(len(items), 1))
    out: List[Dict[str, Any]] = []
    for index, (fields, body) in enumerate(items):
        own, quoted = without_quoted_history(body, [b for i, (_, b) in enumerate(items) if i != index])
        entry = dict(fields)
        entry["body"] = own[:share]
        if quoted:
            entry["quoted_history_omitted"] = True
        if len(own) > share:
            entry["body_truncated"] = True
            entry["hint"] = "Body cut to fit the thread; call get_message for this message to read it whole."
        out.append(entry)
    return out


class CallContext:
    """Runtime actor and Workspace bound to one in-process MCP call."""

    def __init__(self, name: str) -> None:
        self._var: ContextVar[Dict[str, Any]] = ContextVar(name, default={})

    def set(self, context: Dict[str, Any]) -> None:
        self._var.set(dict(context or {}))

    def clear(self) -> None:
        self._var.set({})

    def get(self) -> Dict[str, Any]:
        return self._var.get()


async def save_attachment(
    context: Dict[str, Any],
    *,
    data: bytes,
    filename: str,
    content_type: str,
    source: Dict[str, Any],
    tool_name: str,
) -> Dict[str, Any]:
    """Store one received attachment in the caller's Workspace and Knowledge."""
    from packages.core.services.email_attachments import (
        EmailAttachmentError,
        persist_workspace_email_attachment,
    )

    if not all(context.get(key) for key in ("entity_id", "user_id")):
        raise EmailAttachmentError(
            "Saving an email attachment requires the current entity and user context."
        )
    return await persist_workspace_email_attachment(
        entity_id=str(context.get("entity_id") or ""),
        workspace_id=str(context.get("workspace_id") or ""),
        user_id=str(context.get("user_id") or ""),
        data=data,
        filename=filename or "attachment",
        content_type=content_type or "application/octet-stream",
        source=source,
        task_id=str(context.get("task_id") or "") or None,
        conversation_id=str(context.get("conversation_id") or "") or None,
        tool_name=tool_name,
    )
