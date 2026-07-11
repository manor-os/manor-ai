from __future__ import annotations

import logging
from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.sources import (
    RUNTIME_CHANNEL_HOLD_SOURCE,
    RUNTIME_CHANNEL_SOURCE,
    RUNTIME_CHAT_SOURCE,
    RUNTIME_CHAT_STREAM_SOURCE,
    RUNTIME_INTERNAL_WORKER_SOURCE,
    RUNTIME_WORKFLOW_RUNNER_SOURCE,
    RUNTIME_WORKFLOW_SERVICE_SOURCE,
)

logger = logging.getLogger(__name__)
RUNTIME_CHAT_EVENT_SOURCE = RUNTIME_CHAT_SOURCE
RUNTIME_CHAT_STREAM_EVENT_SOURCE = RUNTIME_CHAT_STREAM_SOURCE
RUNTIME_CHANNEL_EVENT_SOURCE = RUNTIME_CHANNEL_SOURCE
RUNTIME_CHANNEL_HOLD_EVENT_SOURCE = RUNTIME_CHANNEL_HOLD_SOURCE
RUNTIME_INTERNAL_WORKER_EVENT_SOURCE = RUNTIME_INTERNAL_WORKER_SOURCE
RUNTIME_WORKFLOW_RUNNER_EVENT_SOURCE = RUNTIME_WORKFLOW_RUNNER_SOURCE
RUNTIME_WORKFLOW_SERVICE_EVENT_SOURCE = RUNTIME_WORKFLOW_SERVICE_SOURCE


def runtime_envelope_meta(envelope: RuntimeEnvelope | None) -> dict[str, Any] | None:
    """Return the safe, persisted metadata view for a runtime envelope."""
    if envelope is None:
        return None
    try:
        return envelope.to_message_meta()
    except Exception:
        logger.debug("Runtime envelope metadata serialization failed", exc_info=True)
        return None


def runtime_context_meta(ctx: Any) -> dict[str, Any] | None:
    """Extract persisted runtime metadata from any context with an envelope."""
    return runtime_envelope_meta(getattr(ctx, "runtime_envelope", None))


def runtime_events(envelope: RuntimeEnvelope | None) -> tuple[dict[str, Any], ...]:
    """Return normalized runtime events from an envelope metadata payload."""
    if envelope is None:
        return ()
    raw_events = envelope.metadata.get("runtime_events")
    if not isinstance(raw_events, list):
        return ()
    events: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        event_type = str(raw.get("type") or "").strip()
        if not event_type:
            continue
        event = dict(raw)
        event["type"] = event_type
        events.append(event)
    return tuple(events)


def runtime_event_summary(envelope: RuntimeEnvelope | None) -> dict[str, Any] | None:
    """Return a compact, log-safe summary of runtime events."""
    events = runtime_events(envelope)
    if not events:
        return None

    by_type: dict[str, int] = {}
    tool_names: set[str] = set()
    denied_tools: set[str] = set()
    error_count = 0
    for event in events:
        event_type = str(event.get("type") or "")
        by_type[event_type] = by_type.get(event_type, 0) + 1
        tool_name = str(event.get("tool_name") or "").strip()
        if tool_name:
            tool_names.add(tool_name)
        if event_type == "tool_denied" and tool_name:
            denied_tools.add(tool_name)
        if event_type == "error":
            error_count += 1

    return {
        "total": len(events),
        "by_type": dict(sorted(by_type.items())),
        "tool_names": tuple(sorted(tool_names)),
        "denied_tool_names": tuple(sorted(denied_tools)),
        "error_count": error_count,
    }


def runtime_execution_metadata(envelope: RuntimeEnvelope | None) -> dict[str, Any] | None:
    """Return runtime metadata plus a compact event summary for logs/results."""
    meta = runtime_envelope_meta(envelope)
    if not meta:
        return None
    payload: dict[str, Any] = {"runtime": meta}
    event_summary = runtime_event_summary(envelope)
    if event_summary:
        payload["runtime_event_summary"] = event_summary
    return payload


def runtime_metadata_from_context(ctx: Any) -> dict[str, Any] | None:
    """Return a worker/log metadata payload shaped as ``{\"runtime\": ...}``."""
    return runtime_execution_metadata(getattr(ctx, "runtime_envelope", None))


async def _runtime_persist_events_best_effort(
    envelope: RuntimeEnvelope | None,
    *,
    source: str,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    from packages.core.services.runtime_event_service import persist_runtime_events_best_effort

    metadata = {}
    if message_id is not None:
        metadata["message_id"] = message_id
    if trace_id is not None:
        metadata["trace_id"] = trace_id
    return await persist_runtime_events_best_effort(envelope, source=source, **metadata)


async def runtime_persist_chat_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist non-stream chat Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_CHAT_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


async def runtime_persist_chat_stream_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist streaming chat Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_CHAT_STREAM_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


async def runtime_persist_channel_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist delivered channel Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_CHANNEL_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


async def runtime_persist_channel_hold_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist held channel Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_CHANNEL_HOLD_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


async def runtime_persist_internal_worker_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist InternalWorker Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_INTERNAL_WORKER_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


def attach_runtime_meta(
    payload: dict[str, Any],
    envelope: RuntimeEnvelope | None,
    *,
    key: str = "runtime",
) -> dict[str, Any]:
    """Attach safe runtime metadata to a result dict without changing core fields."""
    meta = runtime_envelope_meta(envelope)
    if meta:
        payload[key] = meta
    return payload


async def runtime_attach_and_persist_workflow_runner_result(
    payload: dict[str, Any],
    envelope: RuntimeEnvelope | None,
) -> dict[str, Any]:
    """Attach runtime metadata and persist workflow-runner events."""

    attach_runtime_meta(payload, envelope)
    await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_WORKFLOW_RUNNER_EVENT_SOURCE,
    )
    return payload


async def runtime_persist_workflow_service_runtime_events(
    envelope: RuntimeEnvelope | None,
    *,
    message_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Persist legacy workflow-service Runtime events with Runtime-owned source."""

    return await _runtime_persist_events_best_effort(
        envelope,
        source=RUNTIME_WORKFLOW_SERVICE_EVENT_SOURCE,
        message_id=message_id,
        trace_id=trace_id,
    )


def merge_runtime_meta(
    metadata: dict[str, Any] | None,
    envelope: RuntimeEnvelope | None,
    *,
    key: str = "runtime",
) -> dict[str, Any]:
    """Merge safe runtime metadata into an existing metadata dict."""
    merged = dict(metadata or {})
    meta = runtime_envelope_meta(envelope)
    if meta:
        merged[key] = meta
    return merged
