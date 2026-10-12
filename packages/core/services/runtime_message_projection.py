"""Reconcile Chat placeholders with the authoritative cancelled Runtime run."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from packages.core.ai.runtime.output_policy import runtime_assistant_stream_interrupted_content
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus
from packages.core.models.task import Message
from packages.core.services.assistant_blocks import project_failed_assistant_blocks
from packages.core.services.conversation_messages import (
    assistant_stream_interrupted_meta,
    assistant_stream_started_content,
)


def cancelled_runtime_message_snapshot(message: Message, run: RuntimeRun):
    """Return a read-only terminal snapshot without changing finished replies."""
    meta = message.meta or {}
    if (
        run.status != RuntimeRunStatus.CANCELLED.value
        or message.id != run.assistant_message_id
        or message.conversation_id != run.conversation_id
        or message.role != "assistant"
        or meta.get("stream_status") not in {"running", "streaming"}
    ):
        return message

    partial = message.content
    if partial == assistant_stream_started_content():
        partial = None
    content = runtime_assistant_stream_interrupted_content(partial)
    terminal_meta = assistant_stream_interrupted_meta(meta)
    terminal_meta["stop_reason"] = "cancelled"
    blocks = project_failed_assistant_blocks(
        meta.get("assistant_blocks"), content,
        tool_calls=message.tool_calls, lifecycle="cancelled",
    )
    if blocks:
        terminal_meta["assistant_blocks"] = blocks
    else:
        terminal_meta.pop("assistant_blocks", None)
        terminal_meta.pop("assistant_blocks_schema", None)
    return SimpleNamespace(**{
        **{attr.key: getattr(message, attr.key) for attr in Message.__mapper__.column_attrs},
        "content": content,
        "meta": terminal_meta,
    })


async def cancelled_runtime_message_snapshots(db, messages, *, entity_id: str):
    """Recover legacy cancelled placeholders in one actor-scoped read query."""
    candidates = [message for message in messages if (
        message.role == "assistant"
        and (message.meta or {}).get("stream_status") in {"running", "streaming"}
    )]
    if not candidates:
        return messages
    runs = (await db.execute(select(RuntimeRun).where(
        RuntimeRun.entity_id == entity_id,
        RuntimeRun.assistant_message_id.in_([message.id for message in candidates]),
        RuntimeRun.conversation_id.in_([message.conversation_id for message in candidates]),
        RuntimeRun.status == RuntimeRunStatus.CANCELLED.value,
    ))).scalars().all()
    by_message = {run.assistant_message_id: run for run in runs}
    return [
        cancelled_runtime_message_snapshot(message, by_message[message.id])
        if message.id in by_message else message
        for message in messages
    ]
