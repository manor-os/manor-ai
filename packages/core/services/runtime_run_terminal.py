"""The one terminal transition for Runtime runs that no live loop will finish.

A Chat turn that ends inside its agent loop is finalized by that loop: the
assistant Message is saved with its reply or failure, the terminal SSE frames
go out, and ``complete_runtime_run_execution`` records the run's outcome.

A run parked outside the loop (on a Sandbox ticket, a question, or a queued
start) can also end without any loop running: its ticket fails or expires, its
question times out, a cancel arrives, the worker that held it dies for good.
Each of those writers used to set ``run.status`` and stop there, leaving the
assistant Message ``streaming`` and the client waiting on a queue label
forever. Every such writer must go through :func:`finish_runtime_run_out_of_loop`,
which in one transaction

* moves the run to its terminal status (status, reason, completion time,
  lease, version, error),
* finalizes the assistant Message with a user-visible outcome, and
* stages a durable ``runtime.run_terminal_notice`` outbox row. After commit the
  outbox dispatcher appends the terminal frames to the run's event stream and
  pushes the realtime refresh (:func:`announce_runtime_run_terminal`).

``tests/test_runtime_run_terminal_guard.py`` keeps every other module from
writing a terminal ``RuntimeRunStatus`` directly.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
)

logger = logging.getLogger(__name__)

RUNTIME_RUN_TERMINAL_NOTICE_EVENT = "runtime.run_terminal_notice"

_STATUS_REASON_MAX_CHARS = 80
_ANNOUNCE_TIMEOUT_SECONDS = 5.0

_SANDBOX_START_FAILED_MESSAGE = (
    "The work environment for this request could not be started, so the "
    "request stopped before it finished. Please try again."
)
_SANDBOX_QUEUE_TIMEOUT_MESSAGE = (
    "No work environment became available in time, so the request stopped "
    "before it finished. Please try again."
)
_HUMAN_INPUT_TIMEOUT_MESSAGE = (
    "The question in this request was not answered in time, so the request "
    "stopped. Send a new message to continue."
)
_WORKER_LOST_MESSAGE = (
    "The worker running this request stopped unexpectedly, so the request "
    "did not finish. Please try again."
)


def runtime_run_failure_message(reason: str | None) -> str:
    """User-visible text for a run that failed outside its agent loop.

    Only the reason *code* is consulted; raw errors never reach the reader.
    """

    from packages.core.ai.runtime.output_policy import PUBLIC_REQUEST_FAILURE_MESSAGE

    code = str(reason or "")
    if code == "sandbox_queue_timeout":
        return _SANDBOX_QUEUE_TIMEOUT_MESSAGE
    if code.startswith("sandbox_"):
        return _SANDBOX_START_FAILED_MESSAGE
    if code == "human_input_timeout":
        return _HUMAN_INPUT_TIMEOUT_MESSAGE
    if code == "runtime_recovery_exhausted":
        return _WORKER_LOST_MESSAGE
    return PUBLIC_REQUEST_FAILURE_MESSAGE


async def finish_runtime_run_out_of_loop(
    db: AsyncSession,
    run: RuntimeRun,
    *,
    status: RuntimeRunStatus | str,
    reason: str,
    now: datetime | None = None,
    error: dict[str, Any] | None = None,
) -> bool:
    """Terminally fail or cancel ``run`` and settle what the user sees.

    The caller holds the run's row lock and commits. Returns ``False`` (and
    changes nothing) when the run is already terminal, so concurrent sweeps
    and repeated deliveries stay idempotent. ``COMPLETED`` is reserved for the
    loop's own finalizer, which owns the reply.
    """

    target = RuntimeRunStatus(status).value
    if target not in {RuntimeRunStatus.FAILED.value, RuntimeRunStatus.CANCELLED.value}:
        raise ValueError(f"an out-of-loop transition cannot end a run as {target!r}")
    if run.status in RuntimeRunStatus.terminal():
        return False
    current_time = now or datetime.now(timezone.utc)
    code = str(reason or "runtime_execution_failed")[:_STATUS_REASON_MAX_CHARS]
    run.status = target
    run.status_reason = code
    run.completed_at = current_time
    run.lease_owner = None
    run.lease_expires_at = None
    if target == RuntimeRunStatus.FAILED.value:
        run.error = dict(error) if error else {
            "code": code,
            "message": runtime_run_failure_message(code),
        }
    run.version += 1
    await settle_terminal_runtime_run_message(db, run)
    stage_runtime_run_terminal_notice(db, run, now=current_time)
    return True


async def settle_terminal_runtime_run_message(
    db: AsyncSession,
    run: RuntimeRun,
) -> bool:
    """Finalize a terminal run's still-streaming assistant Message in place.

    A reply that already finished (or a failure the loop already saved) is
    left alone. Returns whether the Message was changed.
    """

    if (
        run.status not in {RuntimeRunStatus.FAILED.value, RuntimeRunStatus.CANCELLED.value}
        or not run.assistant_message_id
    ):
        return False
    from packages.core.models.task import Message

    message = await db.scalar(
        select(Message)
        .where(
            Message.id == run.assistant_message_id,
            Message.conversation_id == run.conversation_id,
            Message.role == "assistant",
        )
        .with_for_update()
    )
    if message is None:
        return False
    if run.status == RuntimeRunStatus.CANCELLED.value:
        from packages.core.services.runtime_message_projection import (
            cancelled_runtime_message_snapshot,
        )

        snapshot = cancelled_runtime_message_snapshot(message, run)
        if snapshot is message:
            return False
        message.content = snapshot.content
        message.meta = snapshot.meta
        return True

    meta = dict(message.meta or {})
    if meta.get("stream_status") not in {"running", "streaming"}:
        return False
    from packages.core.services.assistant_blocks import project_failed_assistant_blocks

    content = runtime_run_failure_message(run.status_reason)
    blocks = project_failed_assistant_blocks(
        meta.get("assistant_blocks"), content,
        tool_calls=message.tool_calls, lifecycle="failed",
    )
    if blocks:
        meta["assistant_blocks"] = blocks
    else:
        meta.pop("assistant_blocks", None)
        meta.pop("assistant_blocks_schema", None)
    meta.update({
        "stream_status": "error",
        "stream_error": True,
        "error_message": content,
        "stop_reason": "error",
        # The code is for operators and clients that branch on it; the
        # visible body above is the only text the reader gets.
        "runtime_failure": {
            "run_id": run.id,
            "status": run.status,
            "reason": run.status_reason,
        },
    })
    message.content = content
    message.meta = meta
    return True


def stage_runtime_run_terminal_notice(
    db: AsyncSession,
    run: RuntimeRun,
    *,
    now: datetime | None = None,
) -> None:
    """Queue the client notification for a run that just became terminal."""

    current_time = now or datetime.now(timezone.utc)
    db.add(
        RuntimeOutboxEvent(
            event_type=RUNTIME_RUN_TERMINAL_NOTICE_EVENT,
            aggregate_id=run.id,
            dedupe_key=f"{RUNTIME_RUN_TERMINAL_NOTICE_EVENT}:{run.id}:{run.version}",
            payload={"run_id": run.id},
            available_at=current_time,
            created_at=current_time,
        )
    )


def runtime_run_terminal_frames(run: RuntimeRun, message: Any | None) -> list[str]:
    """SSE frames that tell a connected client how a terminal run ended.

    They mirror what the loop itself emits when a turn fails or is cancelled
    (``error`` then ``stream_end``), preceded by the authoritative
    ``runtime_status`` so a client parked on a queue label clears it.
    """

    from packages.core.services.sse_events import format_sse

    frames = [
        format_sse(
            "runtime_status",
            {"run_id": run.id, "status": run.status, "status_reason": run.status_reason},
        )
    ]
    if run.status not in {RuntimeRunStatus.FAILED.value, RuntimeRunStatus.CANCELLED.value}:
        return frames
    meta = dict(getattr(message, "meta", None) or {}) if message is not None else {}
    settled = message is not None and meta.get("stream_status") not in {"running", "streaming"}
    message_id = run.assistant_message_id if message is not None else None
    blocks: dict[str, Any] = {}
    if isinstance(meta.get("assistant_blocks"), list):
        blocks["assistant_blocks"] = meta["assistant_blocks"]
        if meta.get("assistant_blocks_schema") is not None:
            blocks["assistant_blocks_schema"] = meta["assistant_blocks_schema"]
    base = {"conversation_id": run.conversation_id, "message_id": message_id}
    if run.status == RuntimeRunStatus.FAILED.value:
        text = (
            str(message.content)
            if settled and meta.get("stream_error") and message.content
            else runtime_run_failure_message(run.status_reason)
        )
        frames.append(format_sse("error", {
            **base,
            "message": text,
            "persisted": bool(settled),
            "error_code": run.status_reason,
            **blocks,
        }))
        frames.append(format_sse("stream_end", {
            **base,
            "persisted": bool(settled),
            "usage": {},
            "status": "error",
            "stop_reason": "error",
            **blocks,
        }))
    else:
        frames.append(format_sse("stream_end", {
            **base,
            "persisted": bool(settled),
            "usage": {},
            "status": "cancelled",
            "stop_reason": "cancelled",
            **blocks,
        }))
    return frames


async def announce_runtime_run_terminal(db: AsyncSession, run_id: str) -> bool:
    """Publish a committed terminal transition to every client surface.

    Called by the outbox dispatcher after the transition committed. Raises
    when the event stream cannot be written so the outbox retries; realtime
    pushes are best-effort refreshes on top of the durable Message.
    """

    from packages.core.models.task import Conversation, Message
    from packages.core.services.runtime_event_stream import append_runtime_sse_event

    run = await db.get(RuntimeRun, run_id)
    if run is None or run.status not in RuntimeRunStatus.terminal():
        return False
    message = None
    if run.assistant_message_id:
        message = await db.scalar(select(Message).where(
            Message.id == run.assistant_message_id,
            Message.conversation_id == run.conversation_id,
        ))
    for frame in runtime_run_terminal_frames(run, message):
        await asyncio.wait_for(
            append_runtime_sse_event(run.id, frame),
            timeout=_ANNOUNCE_TIMEOUT_SECONDS,
        )
    if message is None or run.parent_run_id is not None:
        return True
    conversation = await db.get(Conversation, run.conversation_id)
    try:
        await asyncio.wait_for(
            _push_message_refresh(run, message, conversation),
            timeout=_ANNOUNCE_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — the Message row is already final
        logger.warning("Runtime terminal realtime push failed run=%s", run.id, exc_info=True)
    return True


async def _push_message_refresh(run: RuntimeRun, message: Any, conversation: Any) -> None:
    workspace_id = getattr(conversation, "workspace_id", None)
    if workspace_id:
        from packages.core.workspace_chat.service import publish_workspace_chat_message_event

        await publish_workspace_chat_message_event(
            conversation.entity_id, workspace_id=workspace_id, message=message,
        )
        return
    from packages.core.services.realtime import (
        push_chat_stream_snapshot,
        push_conversation_message,
    )

    meta = dict(message.meta or {})
    surface = (run.execution_payload or {}).get("surface")
    from packages.core.services.chat_service import _snapshot_surface_allows_resume

    if _snapshot_surface_allows_resume(surface):
        # Tabs following this turn render from the snapshot; a terminal one
        # ends their "still working" state and makes them refetch the row.
        await push_chat_stream_snapshot(
            run.user_id,
            {
                "conversation_id": run.conversation_id,
                "message_id": message.id,
                "status": "error" if run.status == RuntimeRunStatus.FAILED.value else "interrupted",
                "content": str(message.content or ""),
                **({"assistant_blocks": meta["assistant_blocks"]}
                   if isinstance(meta.get("assistant_blocks"), list) else {}),
            },
            entity_id=run.entity_id,
        )
    await push_conversation_message(
        run.user_id,
        {
            "conversation_id": run.conversation_id,
            "message_id": message.id,
            "message_kind": message.message_kind,
            "author_kind": message.author_kind,
            "has_pending_action": False,
        },
        entity_id=run.entity_id,
    )


__all__ = [
    "RUNTIME_RUN_TERMINAL_NOTICE_EVENT",
    "announce_runtime_run_terminal",
    "finish_runtime_run_out_of_loop",
    "runtime_run_failure_message",
    "runtime_run_terminal_frames",
    "settle_terminal_runtime_run_message",
    "stage_runtime_run_terminal_notice",
]
