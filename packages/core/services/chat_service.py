"""Chat service — conversations, messages, SSE streaming."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.chat_logger import ChatTrace
from packages.core.ai.runtime.performance import runtime_performance_run_id
from packages.core.constants.hitl_envelope import parse_hitl_envelope
from packages.core.ai.runtime import (
    ChannelRuntimeContext,
    ChatSurface,
    runtime_context_meta,
    runtime_execute_chat_agent_loop,
    runtime_manual_skill_ids_from_refs,
    runtime_persist_chat_runtime_events,
    runtime_persist_chat_stream_runtime_events,
    runtime_release_billing_context,
    runtime_set_callback_settled_billing_context,
)
from packages.core.ai.llm_client import LLMRateLimited
from packages.core.ai.runtime.skill_forcing import (
    runtime_forced_tool_calls_for_turn,
    runtime_message_text_for_intent,
)
from packages.core.ai.runtime.control import RuntimeContextWriterFenced
from packages.core.ai.runtime.file_context import editor_document_round_processor
from packages.core.ai.runtime.turn_policy import runtime_turn_max_rounds
from packages.core.ai.runtime.output_policy import (
    BYOK_PROVIDER_ERROR_CODE,
    PUBLIC_REQUEST_FAILURE_MESSAGE,
    runtime_assistant_stream_error_content,
    runtime_assistant_result_meta,
    runtime_fallback_stream_final_summary,
    runtime_is_internal_failure_detail,
    runtime_public_failure_payload,
    runtime_public_transport_payload,
    runtime_public_tool_calls,
    runtime_sanitize_assistant_content_after_loop,
    runtime_visitor_assistant_message_content,
)
from packages.core.ai.runtime.streams import (
    RuntimeToolStreamSink,
    runtime_partial_json_object,
    runtime_tool_draft_preview,
    runtime_record_sub_agent_event_for_chat,
    runtime_record_tool_end_for_chat,
    runtime_record_tool_start_for_chat,
    runtime_should_flush_stream_text,
    runtime_tool_arguments_for_chat as tool_arguments_for_chat,
    runtime_tool_call_error,
    runtime_tool_call_outcome,
    runtime_tool_path_memory_outcome,
    runtime_tool_result_for_chat as tool_result_for_chat,
    runtime_tool_status_for_chat as tool_status_for_chat,
    runtime_tool_stream_sink_var,
)
from packages.core.ai.runtime.provider_approvals import (
    ProviderApprovalCollector,
    provider_tool_result_for_persistence,
)
from packages.core.services.conversation_messages import (
    ORIGIN_USER_MESSAGE_ID_META_KEY,
    add_message,
    assistant_message_origin_meta,
    resolve_author_subscription_id,
    save_assistant_stream_error_message,
    save_assistant_stream_interrupted_message,
    save_or_update_assistant_stream_message,
)
from packages.core.services.assistant_blocks import (
    AssistantBlocksBuilder,
    AssistantProcessLifecycle,
    assistant_tool_uses_structured_result,
    assistant_blocks_stream_payload,
)
from packages.core.services.chat_artifacts import chat_turn_attachments
from packages.core.services.file_refs import enforce_reply_file_links
from packages.core.services.chat_approvals import register_chat_provider_approval
from packages.core.services.chat_approvals import (
    chat_turn_cancellation_requested,
    get_chat_turn_cancel_generation,
)
from packages.core.services.hitl_requests import (
    hitl_requests_from_data,
    workspace_operation_pending_action_from_data,
)
from packages.core.services.model_resolver import (
    resolve_llm_metadata_from_context,
    resolve_model_from_context,
)
from packages.core.services.runtime_chat_context import resolve_runtime_chat_context
from packages.core.services.runtime_learning import (
    record_chat_runtime_learning,
    schedule_learning_candidate_applies,
)
from packages.core.services.sse_events import format_sse
from packages.core.services.usage_service import record_chat_llm_usage  # noqa: F401
from packages.core.services.usage_service import (
    build_durable_llm_billing_callbacks,
)
from packages.core.services.billing_payer import resolve_run_payer

from packages.core.services.conversation_transcript import TRANSCRIPT_TOOL_RESULT_MAX_CHARS  # noqa: E402

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 15  # seconds
STREAM_CHECKPOINT_INTERVAL = 2.0  # seconds
STREAM_CHECKPOINT_MIN_CHARS = 400
STREAM_CHECKPOINT_MIN_DURABLE_CHARS = 24
# Cadence for the WS snapshots that let a reloaded page follow a running turn.
# Deliberately looser than a token stream (it is a catch-up channel, not the
# primary one) and deliberately tighter than the DB checkpoint, whose 2s/400
# char + "durable text" gates exist to spare the database, not the reader.
STREAM_SNAPSHOT_INTERVAL = 0.8  # seconds
STREAM_SNAPSHOT_PUBLISH_TIMEOUT = 5.0  # seconds
# Surfaces whose transcript a browser tab renders from ``messages`` and can
# therefore resume from a snapshot. Public webchat and channel turns persist
# too, but their "user" is a customer, not the tab-owning account.
_SNAPSHOT_CHAT_SURFACES = frozenset({
    ChatSurface.GLOBAL_OWNER_CHAT.value,
    ChatSurface.AGENT_DM.value,
})
FINAL_RESPONSE_SENTINEL = "<manor-final-response>"
FINAL_RESPONSE_SENTINELS = (
    FINAL_RESPONSE_SENTINEL,
    "</manor-final-response>",
    "<final>",
    "</final>",
)


class _ChatPreparationCancelled(Exception):
    """Stop before the Agent loop exists, without turning cancellation into an error."""


async def _await_chat_preparation(awaitable: Awaitable, is_cancelled: Callable[[], Awaitable[bool]]):
    pending = asyncio.ensure_future(awaitable)
    try:
        while True:
            try:
                cancelled = await is_cancelled()
            except Exception:
                logger.warning("Chat preparation cancellation check failed", exc_info=True)
                cancelled = False
            if cancelled:
                raise _ChatPreparationCancelled()
            if pending.done():
                return pending.result()
            await asyncio.wait({pending}, timeout=0.25)
    finally:
        if not pending.done():
            pending.cancel()
        # Drain the preparation task and its DB context before terminal writes.
        await asyncio.gather(pending, return_exceptions=True)


async def _runtime_run_started_at_ms(runtime_run_id: str) -> int | None:
    """When a durable chat turn began (its run's creation), in epoch ms."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.runtime_run import RuntimeRun

    try:
        async with async_session() as db:
            created_at = await db.scalar(select(RuntimeRun.created_at).where(RuntimeRun.id == runtime_run_id))
    except Exception:  # noqa: BLE001 - the clock is display-only; fall back to this wake
        logger.debug("runtime run start lookup failed for %s", runtime_run_id, exc_info=True)
        return None
    if created_at is None:
        return None
    return min(int(created_at.timestamp() * 1000), int(time.time() * 1000))


_FINAL_SAVE_RETRY_DELAY_SECONDS = 0.25
_FINAL_SAVE_OPTIONAL_META = ("assistant_blocks", "assistant_blocks_schema", "sub_agent_events")


async def _save_final_assistant_message(**fields: Any) -> str | None:
    """Save the answer the user already has, shedding optional detail if needed.

    A failed final save used to replace a correct answer with a failure. Retry
    instead: first without raw tool results, then also without the process
    record, so the answer, its attachments and approvals still reach history.
    """
    saved_id = await save_or_update_assistant_stream_message(**fields)
    if saved_id:
        return saved_id
    meta = fields.get("meta") or {}
    lean_meta = {key: value for key, value in meta.items() if key not in _FINAL_SAVE_OPTIONAL_META}
    for detail, overrides in (
        ("tool_results", {"tool_calls": None}),
        ("tool_results+process", {"tool_calls": None, "meta": lean_meta or None}),
    ):
        await asyncio.sleep(_FINAL_SAVE_RETRY_DELAY_SECONDS)
        saved_id = await save_or_update_assistant_stream_message(**{**fields, **overrides})
        if saved_id:
            logger.warning(
                "final assistant message saved without %s conversation=%s message=%s",
                detail, fields.get("conversation_id"), saved_id,
            )
            return saved_id
    return None


async def _append_turn_transcript(ctx: Any, result: Any, *, run_id: str | None, assistant_message_id: str) -> None:
    """Append this run to the conversation transcript; never fails the turn."""
    scope = getattr(ctx, "conversation_transcript", None)
    if scope is None or not getattr(result, "messages", None) or getattr(result, "stop_reason", None) == "cancelled":
        return
    from packages.core.database import async_session
    from packages.core.services.conversation_transcript import append_run_transcript

    envelope_metadata = getattr(getattr(ctx, "runtime_envelope", None), "metadata", None)
    user_message_id = (
        envelope_metadata.get(ORIGIN_USER_MESSAGE_ID_META_KEY) if isinstance(envelope_metadata, dict) else None
    )
    try:
        async with async_session() as db:
            await append_run_transcript(
                db, scope, run_id=run_id,
                initial_messages=getattr(ctx, "transcript_initial_messages", None) or [],
                final_messages=result.messages,
                user_message_id=user_message_id if isinstance(user_message_id, str) else None,
                assistant_message_id=assistant_message_id,
            )
    except Exception:
        # Unwritten turns are picked up from the messages table next turn.
        logger.warning("conversation transcript append failed", exc_info=True)


def _runtime_assistant_origin_meta(ctx: Any) -> dict[str, str]:
    envelope = getattr(ctx, "runtime_envelope", None)
    metadata = getattr(envelope, "metadata", None)
    origin_user_message_id = (
        metadata.get(ORIGIN_USER_MESSAGE_ID_META_KEY)
        if isinstance(metadata, dict)
        else None
    )
    return assistant_message_origin_meta(origin_user_message_id)


# When credits stop a turn and the exception carried no message.
CREDIT_EXHAUSTED_FALLBACK = "AI credits ran out before this request finished. Add credits or upgrade your plan to continue."


_RESERVATION_SHORTFALL = re.compile(r"^insufficient credits: required (\d+), available (-?\d+)$")


def credit_exhausted_reply(result: Any) -> str:
    """The reply body of a turn that credits stopped: the limit's own message.

    The reservation shortfall keeps its machine form for the clients that
    parse it (Workflow failures); a chat reply reads it in plain words.
    """
    detail = getattr(result, "error_detail", None)
    message = str((detail.get("message") if isinstance(detail, dict) else None)
                  or getattr(result, "error", None) or "").strip()
    if shortfall := _RESERVATION_SHORTFALL.match(message):
        required, available = (int(group) for group in shortfall.groups())
        return (
            f"Not enough AI credits for this request: it needs {required:,} credits and your "
            f"balance is {max(available, 0):,}. Add credits or upgrade your plan to continue."
        )
    return message or CREDIT_EXHAUSTED_FALLBACK


def _workspace_final_response_contract(ctx: Any):
    from packages.core.ai.runtime.chat_evidence import chat_evidence_contract
    return chat_evidence_contract(ctx)


def _chat_assistant_result_meta(result: Any) -> dict[str, Any]:
    meta = runtime_assistant_result_meta(result) or {}
    evidence = (getattr(result, "control", None) or {}).get("execution_evidence")
    if evidence and not getattr(result, "error", None):
        # A historical observation, not authority to mark its Task complete.
        meta["execution_evidence"] = evidence
    return meta


def _chat_llm_billing_callbacks(
    *,
    trace: ChatTrace,
    entity_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    fallback_model: str | None,
    payer_entity_id: str | None = None,
):
    """Build per-turn durable billing callbacks for the agentic loop."""
    return build_durable_llm_billing_callbacks(
        source_prefix=f"chat:{trace.trace_id}",
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        fallback_model=fallback_model,
        payer_entity_id=payer_entity_id,
    )


def _snapshot_surface_allows_resume(runtime_surface: "ChatSurface | str | None") -> bool:
    value = getattr(runtime_surface, "value", runtime_surface)
    return str(value or "") in _SNAPSHOT_CHAT_SURFACES


def _chat_result_process_lifecycle(result, *, hitl_data=None) -> AssistantProcessLifecycle:
    """Project the actual loop outcome separately from individual tool errors."""
    if result.stop_reason == "cancelled":
        return "cancelled"
    if (
        result.stop_reason == "max_rounds" and not result.error
        and isinstance(getattr(result, "outcome", None), dict) and (result.content or "").strip()
    ):
        # The round budget ran out but the host accepted a reply: the turn
        # completed with what it got (its outcome says partial), not failed.
        return "completed"
    if result.error or result.stop_reason in {"error", "credit_exhausted", "max_rounds"}:
        return "failed"
    if hitl_data is not None or result.stop_reason in {
        "waiting_resource", "hitl_required", "needs_hitl",
    }:
        return "waiting"
    return "completed"


def _chat_result_visible_content(result, surface: ChatSurface | str | None, tool_calls=None) -> str:
    """Project visitor failures without changing the result kept for operators."""
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT and (
        result.error or result.stop_reason == "error"
    ):
        return runtime_visitor_assistant_message_content(
            result.content, runtime_assistant_result_meta(result), tool_calls,
        ) or ""
    if result.error or result.stop_reason == "error":
        from packages.core.ai.runtime.output_policy import runtime_public_assistant_message_content

        return runtime_public_assistant_message_content(result.content, runtime_assistant_result_meta(result), tool_calls) or ""
    return result.content or ""


def _chat_result_public_error(result, surface: ChatSurface | str | None, tool_calls=None) -> str | None:
    """The ``error`` field of a stream_end / response payload for this surface."""
    if not result.error:
        return None
    if result.error in {"sandbox_allocation_failed", "sandbox_queue_timeout", "sandbox_capacity_timeout", "sandbox_runner_unavailable", "sandbox_allocation_rejected"}:
        return _chat_result_visible_content(result, surface, tool_calls)
    if result.error == "final_response_validation_failed" and surface != ChatSurface.PUBLIC_CUSTOMER_CHAT:
        from packages.core.ai.runtime.output_policy import runtime_response_validation_failure_content

        return runtime_response_validation_failure_content(runtime_assistant_result_meta(result))
    if result.error != BYOK_PROVIDER_ERROR_CODE:
        return runtime_assistant_stream_error_content(_chat_result_visible_content(result, surface, tool_calls))
    if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
        return PUBLIC_REQUEST_FAILURE_MESSAGE
    # The owner-facing sentence with its structured detail lives in the
    # typed failure envelope; the bare code would lose the detail.
    detail = getattr(result, "error_detail", None)
    message = detail.get("message") if isinstance(detail, dict) else None
    return runtime_assistant_stream_error_content(str(message or result.error))


def _chat_result_public_failure_payload(result, surface: ChatSurface | str | None) -> Any:
    """``limit_detail`` for this surface: visitors never see the owner's provider."""
    detail = getattr(result, "error_detail", None)
    if (
        surface == ChatSurface.PUBLIC_CUSTOMER_CHAT
        and result.error == BYOK_PROVIDER_ERROR_CODE
        and isinstance(detail, dict)
    ):
        return {"code": BYOK_PROVIDER_ERROR_CODE, "message": PUBLIC_REQUEST_FAILURE_MESSAGE}
    return runtime_public_failure_payload(detail)


_VISITOR_HIDDEN_USAGE_KEYS = frozenset({"error", "error_public_detail"})


def _chat_result_public_usage(result, surface: ChatSurface | str | None) -> dict:
    """``usage`` for this surface: provider diagnostics are operator evidence."""
    usage = result.usage or {}
    if surface != ChatSurface.PUBLIC_CUSTOMER_CHAT or not isinstance(usage, dict):
        return usage
    return {key: value for key, value in usage.items() if key not in _VISITOR_HIDDEN_USAGE_KEYS}


def _normalize_workspace_recommendation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    action = str(value.get("action") or "").strip()
    reason = str(value.get("reason") or "").strip()
    request = str(value.get("request") or "").strip()
    workspace_id = str(value.get("workspace_id") or "").strip() or None
    if action not in {"create_new", "open_existing", "add_to_existing"}:
        return None
    if not reason or not request:
        return None
    if action == "create_new" and workspace_id is not None:
        return None
    if action != "create_new" and workspace_id is None:
        return None
    return {
        "action": action,
        "reason": reason,
        "request": request,
        "workspace_id": workspace_id,
        "workspace_name": str(value.get("workspace_name") or "").strip() or None,
    }


def _workspace_recommendation_from_context(ctx: Any) -> dict[str, Any] | None:
    envelope = getattr(ctx, "runtime_envelope", None)
    metadata = getattr(envelope, "metadata", None)
    value = metadata.get("workspace_recommendation") if isinstance(metadata, dict) else None
    return _normalize_workspace_recommendation(value)


# The recommendation classifier starts with the turn in the chat process and
# usually finishes long before the reply does; never hold the reply for long.
DEFERRED_WORKSPACE_RECOMMENDATION_WAIT_SECONDS = 3.0


class _TurnWorkspaceRecommendation:
    """The turn's Workspace recommendation, resolved once at the end."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._value = _workspace_recommendation_from_context(ctx)
        metadata = getattr(getattr(ctx, "runtime_envelope", None), "metadata", None)
        self._key = (
            metadata.get("deferred_chat_intent_key")
            if isinstance(metadata, dict) and self._value is None
            else None
        )
        self._resolved = self._key is None

    async def get(self) -> dict[str, Any] | None:
        if not self._resolved:
            self._resolved = True
            from packages.core.services.chat_intent_routing import wait_for_deferred_chat_intent

            published = await wait_for_deferred_chat_intent(
                self._key,
                timeout=DEFERRED_WORKSPACE_RECOMMENDATION_WAIT_SECONDS,
            )
            if isinstance(published, dict):
                self._value = _normalize_workspace_recommendation(
                    published.get("workspace_recommendation")
                )
        return self._value


def _tool_calls_for_snapshot(tool_events: list[dict]) -> list[dict]:
    """Tool cards for a broadcast, without the untruncated provider output.

    ``tool_results`` carries ``raw_result`` so the DB row can be replayed and
    HITL envelopes parsed; it is whole tool output and can be megabytes. The SSE
    payload drops it for the same reason (RuntimeToolStreamSink), and a Redis
    fan-out is a worse place to put it than a response body.
    """
    projected = runtime_public_tool_calls(
        [event for event in tool_events if isinstance(event, dict)],
        preserve_replay_result=False,
    )
    return projected if isinstance(projected, list) else []


def _assistant_blocks_skill_display_names(
    ctx: Any, manual_skill_refs: list[dict] | None = None,
) -> dict[str, str]:
    names: dict[str, str] = {}
    for descriptor in getattr(ctx, "runtime_skill_descriptors", None) or ():
        display_name = str(
            getattr(descriptor, "name", "") or getattr(descriptor, "slug", "") or ""
        ).strip()
        if not display_name:
            continue
        for key in (getattr(descriptor, "id", ""), getattr(descriptor, "slug", "")):
            normalized_key = str(key or "").strip().casefold()
            if normalized_key:
                names[normalized_key] = display_name
    # Manual selections are resolved server-side independently of the bounded
    # prompt catalog, which may omit this exact ID or contain a same-slug copy.
    for skill in manual_skill_refs or ():
        display_name = str(skill.get("display_name") or skill.get("name") or skill.get("slug") or "").strip()
        if not display_name:
            continue
        for key in (skill.get("id"), skill.get("slug")):
            if key:
                names[str(key).strip().casefold()] = display_name
    return names


# Snapshot publishes are fire-and-forget from sync tool callbacks, so they need
# the same strong reference _DETACHED_CHAT_TURNS holds: a bare create_task can
# be garbage-collected before it reaches the wire.
_SNAPSHOT_PUBLISH_TASKS: "set[asyncio.Task]" = set()


async def _publish_chat_stream_snapshot(
    user_id: str,
    entity_id: str,
    payload: dict,
) -> None:
    from packages.core.services.realtime import push_chat_stream_snapshot

    try:
        # A black-holed Redis has no socket timeout of its own, and this runs
        # inside a turn that may already be detached from its request.
        await asyncio.wait_for(
            push_chat_stream_snapshot(user_id, payload, entity_id=entity_id),
            timeout=STREAM_SNAPSHOT_PUBLISH_TIMEOUT,
        )
    except Exception:
        logger.debug("Chat stream snapshot publish failed", exc_info=True)


def _attach_raw_tool_result(tool_events: list[dict], name: str, result: str) -> None:
    persisted_result = provider_tool_result_for_persistence(name, result)
    for event in reversed(tool_events):
        if (
            isinstance(event, dict)
            and event.get("name") == name
            and event.get("status") != "pending"
            and "raw_result" not in event
        ):
            event["raw_result"] = persisted_result
            return


def _with_final_response_sentinel_guidance(system_prompt: str) -> str:
    prompt = str(system_prompt or "")
    return (
        f"{prompt.rstrip()}\n\n"
        "## Final Response Stream Marker\n"
        f"- After all required tool calls are complete, output `{FINAL_RESPONSE_SENTINEL}` "
        "immediately before your final user-facing answer.\n"
        "- Do not output this marker before progress notes or before any tool call.\n"
        "- Do not mention, quote, translate, wrap, or explain the marker."
    ).strip()


def _strip_final_response_sentinel(text: str | None) -> str:
    if not text:
        return ""
    stripped = str(text)
    marker_index = stripped.rfind(FINAL_RESPONSE_SENTINEL)
    if marker_index != -1:
        stripped = stripped[marker_index + len(FINAL_RESPONSE_SENTINEL):]
    for sentinel in FINAL_RESPONSE_SENTINELS:
        stripped = stripped.replace(sentinel, "")
    return stripped.strip()


def _stream_text_is_durable_checkpoint(content: str | None) -> bool:
    """Return whether a partial stream is complete enough to persist visibly."""

    text = _strip_final_response_sentinel(content).strip()
    if len(text) >= STREAM_CHECKPOINT_MIN_DURABLE_CHARS:
        return True
    return len(text) >= 8 and text.endswith((".", "!", "?", "。", "！", "？", "\n"))


# ---------------------------------------------------------------------------
# SSE Streaming chat (real AI engine)
# ---------------------------------------------------------------------------

# Chat-turn agentic loops that outlive their SSE connection. When the client
# disconnects AFTER a tool/skill has started, we let the loop run to completion
# and persist its result (rather than cancelling), so a long-running skill is
# not thrown away on navigate-away. Hold a strong reference here so the detached
# task is not garbage-collected before it finishes.
_DETACHED_CHAT_TURNS: "set[asyncio.Task]" = set()


def _detach_chat_turn(task: "asyncio.Task") -> None:
    _DETACHED_CHAT_TURNS.add(task)
    task.add_done_callback(_DETACHED_CHAT_TURNS.discard)


# Intent->tool-path memory (tool discovery v2, spec §A3) fire-and-forget
# recording tasks. Mirrors _DETACHED_CHAT_TURNS above: a bare
# asyncio.create_task() with no strong reference held elsewhere can be
# garbage-collected mid-DB-write (the task object itself is the only
# thing keeping the coroutine alive once this function returns).
_RECORDING_TASKS: "set[asyncio.Task]" = set()


def _schedule_tool_path_recording(
    *,
    tool_name: str,
    result: str,
    entity_id: str | None,
    user_id: str | None,
    message: "str | list[dict]",
    ctx: Any,
) -> None:
    """Fire-and-forget intent-path recording for one finished tool call.

    Classifies the raw result via runtime_tool_path_memory_outcome rather
    than the UI-facing tool_status_for_chat status string: approval-gate
    outcomes (waiting_human/rejected/blocked/cancelled/canceled) and raw
    __hitl__ interrupt payloads are skipped entirely — spec §A3 explicitly
    excludes approval denials/cancellations/availability blocks from
    failure recording ("those say nothing about whether the path fits the
    task"), and a HITL interrupt is not a terminal outcome yet (recording
    it as a success would be premature, and double-counts once the human
    actually resolves it).
    """
    outcome = runtime_tool_path_memory_outcome(result)
    if outcome is None:
        return
    from packages.core.ai.agentic_loop import _maybe_record_tool_path
    task = asyncio.create_task(_maybe_record_tool_path(
        tool_name=tool_name,
        success=(outcome == "success"),
        entity_id=entity_id,
        user_id=user_id,
        user_message=runtime_message_text_for_intent(message),
        hinted_tool_names=getattr(ctx, "hinted_tool_names", None),
    ))
    _RECORDING_TASKS.add(task)
    task.add_done_callback(_RECORDING_TASKS.discard)


async def stream_chat_response(
    message: "str | list[dict]",
    conversation_id: str | None,
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    db: AsyncSession | None = None,
    manual_skill_refs: list[dict] | None = None,
    assistant_message_id: str | None = None,
    disable_tools: bool = False,
    blocked_tools: list[str] | tuple[str, ...] | set[str] | str | None = None,
    editor_context: dict | None = None,
    channel_context: ChannelRuntimeContext | dict | None = None,
    runtime_metadata: dict | None = None,
    persist_messages: bool = True,
    runtime_surface: ChatSurface | str | None = None,
    runtime_run_id: str | None = None,
    runtime_checkpoint: dict | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream an AI response as SSE events with full multi-round tool execution.

    Events emitted:
      - stream_start   — {conversation_id} when persisted, otherwise ephemeral
      - text_delta     — {content: "..."}
      - tool_start     — {name, arguments, round}
      - tool_end       — {name, result_preview, round}
      - hitl_required  — {reason, data}  (human-in-the-loop)
      - stream_end     — {conversation_id, usage, rounds, tool_calls}
      - error          — {message}
    """
    turn_started_at_ms = int(time.time() * 1000)
    # ── Trace ──
    trace = ChatTrace(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    trace.log_request(message)
    trace.start_loop_probe()

    stream_completed = False
    hitl_data: dict | None = None
    # The turn parked durably (on an approval, a question or a queue): its
    # open request now belongs to the parked run, not to this stream.
    loop_parked = [False]
    persisted_message_id: list[str | None] = [
        assistant_message_id if persist_messages else None
    ]
    tool_stream_token = None

    def current_stream_message_id() -> str | None:
        return persisted_message_id[0] or assistant_message_id

    def scoped_payload(
        payload: dict | None = None,
        *,
        fallback_message_id: bool = True,
    ) -> dict:
        data = dict(payload or {})
        if not data.get("conversation_id"):
            data["conversation_id"] = conversation_id
        if fallback_message_id and not data.get("message_id"):
            data["message_id"] = current_stream_message_id()
        if runtime_run_id and not data.get("run_id"):
            data["run_id"] = runtime_run_id
        return data

    def scoped_sse(
        event: str,
        payload: dict | None = None,
        *,
        fallback_message_id: bool = True,
    ) -> str:
        return format_sse(
            event,
            scoped_payload(payload, fallback_message_id=fallback_message_id),
        )

    cancel_generation = 0
    if persist_messages and conversation_id and entity_id:
        from packages.core.database import async_session as _cancel_check_session_factory

        async with _cancel_check_session_factory() as cancel_check_db:
            cancel_generation = await get_chat_turn_cancel_generation(
                cancel_check_db,
                conversation_id=conversation_id,
                entity_id=entity_id,
            )
    trace.mark("cancel_check")

    async def is_cancelled() -> bool:
        if not (persist_messages and conversation_id and entity_id):
            return False
        async with _cancel_check_session_factory() as cancel_check_db:
            return await chat_turn_cancellation_requested(
                cancel_check_db, conversation_id=conversation_id,
                entity_id=entity_id, generation=cancel_generation,
            )

    async def runtime_cancel_requested() -> bool:
        if not runtime_run_id:
            return False
        from sqlalchemy import select as _select
        from packages.core.database import async_session as _runtime_session
        from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus

        async with _runtime_session() as runtime_db:
            state = (await runtime_db.execute(_select(
                RuntimeRun.status, RuntimeRun.cancel_requested_at,
            ).where(RuntimeRun.id == runtime_run_id))).one_or_none()
        return bool(state and (
            state.cancel_requested_at is not None
            or state.status in {
                RuntimeRunStatus.CANCEL_REQUESTED.value,
                RuntimeRunStatus.CANCELLING.value,
                RuntimeRunStatus.CANCELLED.value,
            }
        ))

    if runtime_run_id and runtime_checkpoint:
        # A durable turn resumes here after every park (a sandbox, an approval):
        # its process clock counts from when the turn started, not this wake.
        turn_started_at_ms = await _runtime_run_started_at_ms(runtime_run_id) or turn_started_at_ms

    yield scoped_sse("stream_start", {
        "ephemeral": not persist_messages,
        **assistant_blocks_stream_payload(AssistantBlocksBuilder(started_at_ms=turn_started_at_ms)),
    })

    try:
        # Use a fresh DB session for context resolution so the request-scoped
        # session (from Depends(get_db)) is released immediately.  This prevents
        # long-running SSE streams from holding a DB connection for minutes.
        from packages.core.database import async_session as _ctx_session_factory
        async def prepare_context():
            async with _ctx_session_factory() as ctx_db:
                return await resolve_runtime_chat_context(
                    ctx_db,
                    message,
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id if persist_messages else None,
                    workspace_id=workspace_id,
                    manual_skill_refs=manual_skill_refs,
                    trace=trace,
                    disable_tools=disable_tools,
                    blocked_tools=blocked_tools,
                    channel_context=channel_context,
                    editor_context=editor_context,
                    runtime_metadata=runtime_metadata,
                    runtime_surface=runtime_surface,
                    split_turn_context=True,
                )

        turn_cancel_requested = runtime_cancel_requested if runtime_run_id else is_cancelled
        system_prompt, tools, initial_messages, ctx = await _await_chat_preparation(
            prepare_context(), turn_cancel_requested,
        )
        # ctx_db is now closed — no DB connection held during streaming
        agent_id = getattr(ctx, "agent_id", None) or agent_id
        workspace_recommendation = _TurnWorkspaceRecommendation(ctx)
        turn_recommendation = None

        # SSE event queue — tools push events here, we yield them
        event_queue: asyncio.Queue = asyncio.Queue()
        round_counter = [0]
        loop_saved = [False]  # whether the background task already saved the message
        tools_started = [False]  # a tool/skill has begun executing this turn
        last_tool_args: dict[str, dict] = {}  # name -> args for current round
        tool_results: list[dict] = []  # [{name, result}] for DB storage
        sub_agent_events: list[dict] = []
        provider_approvals = ProviderApprovalCollector()
        assistant_blocks = AssistantBlocksBuilder(
            skill_display_names=_assistant_blocks_skill_display_names(ctx, manual_skill_refs),
            workspace_id=getattr(ctx, "workspace_id", None),
            started_at_ms=turn_started_at_ms,
        )
        text_buffer = [""]
        pending_post_tool_text = [""]
        pending_post_tool_chunks: list[str] = []
        has_emitted_text = [False]
        pending_text_reset = [False]
        reset_before_next_text = [False]
        summary_started = [False]
        sentinel_scan_buffer = [""]
        internal_tool_failure_seen = [False]
        # Plain-text replies stream post-tool text live as progress; the host
        # promotes the trailing progress to the final answer once the loop
        # accepts it (final_response_start), or withdraws it on a ledger gate.
        turn_reply_contract = _workspace_final_response_contract(ctx)
        live_post_tool_text = bool(getattr(turn_reply_contract, "streams_candidate_text", False))
        editor_round_processor = editor_document_round_processor(ctx.runtime_envelope, entity_id)

        def with_assistant_blocks(payload: dict) -> dict:
            return runtime_public_transport_payload({
                **payload,
                **assistant_blocks_stream_payload(assistant_blocks),
            })

        # ── Resume channel ──
        # Everything below keeps a reloaded page (or a second tab) in sync with
        # a turn whose SSE connection it does not own. The SSE body is still the
        # primary channel; this is the only one that survives the reload.
        snapshot_enabled = bool(
            persist_messages
            and conversation_id
            and user_id
            and entity_id
            # Workspace chat already has its own realtime path and its own
            # renderer; a second event would double-render it.
            and not ctx.workspace_id
            and _snapshot_surface_allows_resume(runtime_surface)
        )
        snapshot_state = {"seq": 0, "time": 0.0, "terminal": False}

        def publish_stream_snapshot(
            status: str = "streaming",
            *,
            force: bool = False,
            content: str | None = None,
        ) -> None:
            if not snapshot_enabled or not persisted_message_id[0]:
                return
            if snapshot_state["terminal"]:
                return
            now = time.time()
            if not force and now - float(snapshot_state["time"]) < STREAM_SNAPSHOT_INTERVAL:
                return
            snapshot_state["time"] = now
            snapshot_state["seq"] = int(snapshot_state["seq"]) + 1
            if status != "streaming":
                snapshot_state["terminal"] = True
            visible = content if content is not None else streamed_text_content[0]
            payload = runtime_public_transport_payload({
                "conversation_id": conversation_id,
                "message_id": persisted_message_id[0],
                # Publishes are fire-and-forget tasks and Redis pub/sub gives no
                # cross-connection ordering, so the reader drops anything that
                # does not move this forward.
                "seq": snapshot_state["seq"],
                "status": status,
                "content": _strip_final_response_sentinel(visible).strip(),
                "tool_calls": _tool_calls_for_snapshot(tool_results),
                **assistant_blocks.meta(),
            })
            task = asyncio.create_task(
                _publish_chat_stream_snapshot(user_id, entity_id, payload),
            )
            _SNAPSHOT_PUBLISH_TASKS.add(task)
            task.add_done_callback(_SNAPSHOT_PUBLISH_TASKS.discard)

        def assistant_blocks_has_process() -> bool:
            # A terminal prose-only process record is not a tool boundary.
            return any(block.get("type") == "process" and block.get("steps")
                       for block in assistant_blocks.blocks())

        def emit_visible_text_delta(visible_text: str) -> None:
            if not visible_text:
                return
            if has_emitted_text[0] and (pending_text_reset[0] or reset_before_next_text[0]):
                event_queue.put_nowait(scoped_sse("text_reset", {}))
            pending_text_reset[0] = False
            reset_before_next_text[0] = False
            event_queue.put_nowait(scoped_sse("text_delta", {"content": visible_text}))
            has_emitted_text[0] = True

        def pop_pending_post_tool_chunks() -> list[str]:
            chunks = pending_post_tool_chunks[:] or (
                [pending_post_tool_text[0]] if pending_post_tool_text[0] else []
            )
            pending_post_tool_text[0] = ""
            pending_post_tool_chunks.clear()
            return chunks

        def classify_pending_post_tool_text(phase: str) -> None:
            text = pending_post_tool_text[0]
            if not text:
                return
            emit_visible_text_delta(text)
            assistant_blocks.append_text(text, phase=phase)
            pending_post_tool_text[0] = ""
            pending_post_tool_chunks.clear()

        def record_nested_tool_event(event_type: str, data: dict) -> None:
            nonlocal hitl_data
            tool_call = data.get("tool_call") if isinstance(data, dict) else None
            if not isinstance(tool_call, dict):
                return
            name = str(tool_call.get("name") or "tool")
            args = tool_call.get("arguments")
            provider_approvals.capture_recorded_tool_call(tool_call)
            if event_type == "tool_start":
                runtime_record_tool_start_for_chat(tool_results, name, args)
                flush_text_buffer()
                classify_pending_post_tool_text("progress")
                assistant_blocks.start_tool(
                    name,
                    args,
                    now_ms=int(time.time() * 1000),
                )
            elif event_type == "tool_end":
                nested_result = tool_call.get("raw_result", tool_call.get("result"))
                if runtime_is_internal_failure_detail(nested_result):
                    internal_tool_failure_seen[0] = True
                runtime_record_tool_end_for_chat(
                    tool_results,
                    name,
                    args=args,
                    result=tool_call.get("result"),
                    status=tool_call.get("status"),
                    duration_ms=tool_call.get("duration_ms"),
                )
                persisted_result = tool_call.get("raw_result")
                if isinstance(persisted_result, str) and persisted_result:
                    _attach_raw_tool_result(tool_results, name, persisted_result)
                    # FIRST blocker wins, not last: the one that stopped the
                    # run is the one the user must answer. A later nested tool
                    # asking for its own approval must not replace it, or the
                    # user resolves the wrong gate and the original stays open.
                    if hitl_data is None:
                        hitl_data = parse_hitl_envelope(persisted_result)
                assistant_blocks.end_tool(
                    name,
                    arguments=args,
                    result=tool_call.get("result"),
                    structured_result=(
                        persisted_result
                        if assistant_tool_uses_structured_result(name, args)
                        else None
                    ),
                    status=tool_call.get("status"),
                    duration_ms=tool_call.get("duration_ms"),
                    now_ms=int(time.time() * 1000),
                )
            data.update(assistant_blocks_stream_payload(assistant_blocks))
            # Throttled, not forced: a skill can emit nested events in bursts.
            publish_stream_snapshot()

        def record_sub_agent_event(event: dict) -> None:
            runtime_record_sub_agent_event_for_chat(sub_agent_events, event)

        def record_nested_progress(data: dict) -> None:
            assistant_blocks.append_text(data["content"] + "\n\n", phase="progress")
            data.update(assistant_blocks_stream_payload(assistant_blocks))
            publish_stream_snapshot(force=True)

        tool_stream_token = runtime_tool_stream_sink_var.set(RuntimeToolStreamSink(
            event_queue=event_queue,
            record_tool_event=record_nested_tool_event,
            record_sub_agent_event=record_sub_agent_event,
            format_event=scoped_sse,
            format_tool_arguments=tool_arguments_for_chat,
            format_tool_result=tool_result_for_chat,
            resolve_tool_status=tool_status_for_chat,
            record_progress=record_nested_progress,
        ))

        def flush_text_buffer(*, phase: str | None = None) -> None:
            if not text_buffer[0]:
                return
            # Runtime owns response language, including explicit output overrides.
            visible_text = text_buffer[0]
            if not phase and live_post_tool_text and assistant_blocks_has_process() and not summary_started[0]:
                emit_visible_text_delta(visible_text)
                assistant_blocks.append_text(visible_text, phase="progress")
                text_buffer[0] = ""
                return
            if not phase and assistant_blocks_has_process() and not summary_started[0]:
                pending_post_tool_text[0] += visible_text
                pending_post_tool_chunks.append(visible_text)
            else:
                emit_visible_text_delta(visible_text)
            if phase:
                assistant_blocks.append_text(visible_text, phase=phase)
            elif not assistant_blocks_has_process() or summary_started[0]:
                assistant_blocks.append_text(visible_text, phase="final" if summary_started[0] else "opening")
            text_buffer[0] = ""

        def process_stream_text_for_summary_sentinel(token_text: str) -> None:
            if not token_text:
                return
            if summary_started[0]:
                combined = sentinel_scan_buffer[0] + token_text
                for sentinel in FINAL_RESPONSE_SENTINELS:
                    combined = combined.replace(sentinel, "")
                held_prefix = 0
                for sentinel in FINAL_RESPONSE_SENTINELS:
                    max_prefix = min(len(combined), len(sentinel) - 1)
                    for size in range(max_prefix, 0, -1):
                        if combined.endswith(sentinel[:size]):
                            held_prefix = max(held_prefix, size)
                            break
                if held_prefix:
                    visible = combined[:-held_prefix]
                    sentinel_scan_buffer[0] = combined[-held_prefix:]
                else:
                    visible = combined
                    sentinel_scan_buffer[0] = ""
                if visible:
                    emit_visible_text_delta(visible)
                    assistant_blocks.append_text(visible, phase="final")
                return

            combined = sentinel_scan_buffer[0] + token_text
            marker_match: tuple[int, str] | None = None
            for sentinel in FINAL_RESPONSE_SENTINELS:
                index = combined.find(sentinel)
                if index >= 0 and (marker_match is None or index < marker_match[0]):
                    marker_match = (index, sentinel)
            if marker_match is not None:
                marker_index, marker = marker_match
                before = combined[:marker_index]
                after = combined[marker_index + len(marker):]
                sentinel_scan_buffer[0] = ""
                if before:
                    text_buffer[0] += before
                    flush_text_buffer()
                if marker == FINAL_RESPONSE_SENTINEL and assistant_blocks_has_process():
                    emit_summary_start_once()
                if after:
                    text_buffer[0] += after
                    flush_text_buffer()
                return

            keep = max(len(sentinel) for sentinel in FINAL_RESPONSE_SENTINELS) - 1
            if len(combined) <= keep:
                sentinel_scan_buffer[0] = combined
                return

            releasable = combined[:-keep]
            sentinel_scan_buffer[0] = combined[-keep:]
            text_buffer[0] += releasable
            if runtime_should_flush_stream_text(text_buffer[0]):
                flush_text_buffer()

        def flush_sentinel_scan_buffer() -> None:
            if not sentinel_scan_buffer[0]:
                return
            text_buffer[0] += sentinel_scan_buffer[0]
            sentinel_scan_buffer[0] = ""
            if runtime_should_flush_stream_text(text_buffer[0]):
                flush_text_buffer()

        def replace_visible_text(
            content: str,
            *,
            final_summary: bool = False,
            clear: bool = False,
            preserve_progress: bool = False,
        ) -> None:
            text_buffer[0] = ""
            sentinel_scan_buffer[0] = ""
            if not content and not clear:
                return
            pending_text_reset[0] = False
            reset_before_next_text[0] = False
            if has_emitted_text[0] or clear:
                assistant_blocks.reset_text(preserve_progress=preserve_progress)
                pending_post_tool_text[0] = ""
                pending_post_tool_chunks.clear()
                event_queue.put_nowait(scoped_sse(
                    "text_reset",
                    with_assistant_blocks({"clear": True}) if clear else {},
                ))
            if not content:
                has_emitted_text[0] = False
                streamed_text_content[0] = ""
                return
            if final_summary:
                emit_summary_start_once()
            event_queue.put_nowait(scoped_sse("text_delta", {"content": content}))
            assistant_blocks.append_text(
                content,
                phase="final" if final_summary else "opening",
            )
            has_emitted_text[0] = True

        def emit_summary_start_once(*, emit_event: bool = True) -> None:
            if summary_started[0]:
                return
            summary_started[0] = True
            assistant_blocks.start_final_summary()
            if emit_event:
                event_queue.put_nowait(scoped_sse("summary_start", {}))

        def flush_final_summary_text(final_text: str = "") -> None:
            flush_sentinel_scan_buffer()
            if text_buffer[0] and (
                has_emitted_text[0]
                or (assistant_blocks_has_process() and not summary_started[0])
            ):
                flush_text_buffer()
            if summary_started[0]:
                if final_text:
                    assistant_blocks.set_final_text(_strip_final_response_sentinel(final_text))
                return
            if has_emitted_text[0] and not assistant_blocks_has_process():
                if final_text:
                    assistant_blocks.set_final_text(_strip_final_response_sentinel(final_text))
                return
            final_visible_text = _strip_final_response_sentinel(final_text) or pending_post_tool_text[0]
            if final_visible_text:
                # The final text supersedes any draft still held back for
                # flushing; a later flush must not emit that draft again.
                text_buffer[0] = ""
                sentinel_scan_buffer[0] = ""
                emit_summary_start_once()
                final_chunks = pending_post_tool_chunks[:]
                if not final_chunks or "".join(final_chunks).strip() != final_visible_text.strip():
                    final_chunks = [final_visible_text]
                pending_post_tool_text[0] = ""
                pending_post_tool_chunks.clear()
                for chunk in final_chunks:
                    emit_visible_text_delta(chunk)
                assistant_blocks.set_final_text(_strip_final_response_sentinel(final_text) or final_visible_text)

        def on_tool_start(name: str, args: dict) -> None:
            round_counter[0] += 1
            tools_started[0] = True
            last_tool_args[name] = args
            event_args = tool_arguments_for_chat(name, args)
            runtime_record_tool_start_for_chat(tool_results, name, event_args)
            flush_sentinel_scan_buffer()
            flush_text_buffer()
            classify_pending_post_tool_text("progress")
            assistant_blocks.start_tool(
                name,
                event_args,
                now_ms=int(time.time() * 1000),
            )
            # Send as tool_call with status=pending so frontend shows spinner
            event_queue.put_nowait(scoped_sse("tool_start", with_assistant_blocks({
                "tool_call": {
                    "name": name,
                    "arguments": event_args,
                    "status": "pending",
                },
            })))
            # Forced: a tool boundary is the visible proof that a turn is alive,
            # and a tool-only turn never reaches the text-growth publish below.
            publish_stream_snapshot(force=True)

        streamed_text = [False]  # track if we streamed tokens in real-time
        streamed_text_content = [""]  # durable fallback when providers stream but omit final content
        reset_stream_buffer = [False]
        last_stream_checkpoint = {"time": 0.0, "chars": 0}
        stream_checkpoint_lock = asyncio.Lock()

        async def replay_pending_post_tool_chunks(chunks: list[str]) -> None:
            for chunk in chunks:
                emit_visible_text_delta(chunk)
                await asyncio.sleep(0.015)

        async def checkpoint_stream_text(force: bool = False) -> None:
            if not persist_messages:
                return
            if loop_saved[0]:
                return
            content = _strip_final_response_sentinel(streamed_text_content[0]).strip()
            if not content or not persisted_message_id[0]:
                return
            if not force and not _stream_text_is_durable_checkpoint(content):
                return
            now = time.time()
            new_chars = len(content) - int(last_stream_checkpoint["chars"])
            if (
                not force
                and now - float(last_stream_checkpoint["time"]) < STREAM_CHECKPOINT_INTERVAL
                and new_chars < STREAM_CHECKPOINT_MIN_CHARS
            ):
                return
            async with stream_checkpoint_lock:
                if loop_saved[0]:
                    return
                content = _strip_final_response_sentinel(streamed_text_content[0]).strip()
                if not content:
                    return
                if not force and not _stream_text_is_durable_checkpoint(content):
                    return
                now = time.time()
                new_chars = len(content) - int(last_stream_checkpoint["chars"])
                if (
                    not force
                    and now - float(last_stream_checkpoint["time"]) < STREAM_CHECKPOINT_INTERVAL
                    and new_chars < STREAM_CHECKPOINT_MIN_CHARS
                ):
                    return
                saved_id = await save_or_update_assistant_stream_message(
                    conversation_id=conversation_id,
                    entity_id=entity_id,
                    workspace_id=ctx.workspace_id,
                    agent_id=agent_id,
                    message_id=persisted_message_id[0],
                    content=content,
                    tool_calls=tool_results if tool_results else None,
                    meta={
                        "stream_status": "streaming",
                        "stream_checkpoint": True,
                        "sub_agent_events": sub_agent_events,
                        **_runtime_assistant_origin_meta(ctx),
                        **assistant_blocks.meta(),
                    },
                )
                if saved_id:
                    persisted_message_id[0] = saved_id
                    last_stream_checkpoint["time"] = now
                    last_stream_checkpoint["chars"] = len(content)

        async def on_stream_event(event_type: str, data: dict) -> None:
            """Buffer tiny token deltas so tools start after coherent visible text."""
            if event_type == "text_delta":
                synthetic_text = bool(data.get("synthetic"))
                if not synthetic_text:
                    streamed_text[0] = True
                token = data.get("text_delta", data.get("token", data.get("content", "")))
                if token is None:
                    token = ""
                token_text = token if isinstance(token, str) else str(token)
                if reset_before_next_text[0]:
                    reset_before_next_text[0] = False
                    reset_stream_buffer[0] = True
                    if has_emitted_text[0] and not internal_tool_failure_seen[0]:
                        event_queue.put_nowait(scoped_sse("text_reset", {}))
                if reset_stream_buffer[0]:
                    if not synthetic_text:
                        streamed_text_content[0] = token_text
                    reset_stream_buffer[0] = False
                else:
                    if not synthetic_text:
                        streamed_text_content[0] += token_text
                if internal_tool_failure_seen[0]:
                    return
                process_stream_text_for_summary_sentinel(token_text)
                if not synthetic_text:
                    await checkpoint_stream_text()
                    # Its own cadence: checkpoint_stream_text is gated on
                    # "durable" text (>=24 chars) and 2s/400 chars, so hanging
                    # the snapshot off it would blank the first sentence.
                    publish_stream_snapshot()
            elif event_type == "text_commit":
                # Everything so far has been accepted. Send it now, so a later
                # reset can only withdraw text that came after this point. The
                # timeline keeps only the readable part; every later event
                # carries the timeline, and patch bodies would grow each one.
                from packages.core.services.chat_feedback import visible_assistant_text

                flush_sentinel_scan_buffer()
                flush_text_buffer()
                classify_pending_post_tool_text("progress")
                assistant_blocks.map_text(visible_assistant_text)
                event_queue.put_nowait(scoped_sse("text_commit", data))
            elif event_type == "text_reset":
                reset_stream_buffer[0] = True
                text_buffer[0] = ""
                sentinel_scan_buffer[0] = ""
                pending_post_tool_text[0] = ""
                pending_post_tool_chunks.clear()
                if data.get("discard_candidate"):
                    # A reply the host did not accept leaves the timeline; the
                    # blocks travel with the reset so every client drops it.
                    assistant_blocks.discard_candidate_text()
                    event_queue.put_nowait(scoped_sse("text_reset", with_assistant_blocks({})))
                    publish_stream_snapshot(force=True, content="")
                else:
                    event_queue.put_nowait(scoped_sse("text_reset", {}))
            elif event_type == "tool_call_draft":
                # A tool call the model is still writing: show what it is
                # about to do and the line it is writing, before the call runs.
                name = str(data.get("name") or "")
                if not name or internal_tool_failure_seen[0]:
                    return
                flush_sentinel_scan_buffer()
                flush_text_buffer()
                classify_pending_post_tool_text("progress")
                partial = runtime_partial_json_object(data.get("arguments_text"))
                assistant_blocks.draft_tool(
                    str(data.get("id") or data.get("index") or name), name,
                    tool_arguments_for_chat(name, partial),
                    preview=runtime_tool_draft_preview(partial),
                    now_ms=int(time.time() * 1000),
                )
                event_queue.put_nowait(scoped_sse("tool_draft", with_assistant_blocks({
                    "tool_call": {"name": name, "status": "drafting"},
                })))
                publish_stream_snapshot()
            elif event_type == "process_note":
                content = data.get("content", "")
                note = content if isinstance(content, str) else str(content or "")
                if note:
                    assistant_blocks.set_process_note(note)
                    event_queue.put_nowait(scoped_sse("process_note", with_assistant_blocks({
                        "content": note,
                        "synthetic": bool(data.get("synthetic")),
                    })))
            elif event_type == "final_response_start":
                if assistant_blocks_has_process():
                    flush_sentinel_scan_buffer()
                    flush_text_buffer()
                    if live_post_tool_text:
                        # The accepted reply already streamed as trailing
                        # progress: re-label it, never re-send its text. No
                        # summary_start event: clients reset their typing
                        # queue on it and would cut the live answer short.
                        # The terminal blocks carry the final phase.
                        assistant_blocks.promote_trailing_progress_to_final()
                        emit_summary_start_once(emit_event=False)
                    else:
                        emit_summary_start_once()
                    await replay_pending_post_tool_chunks(pop_pending_post_tool_chunks())

        def on_tool_end(name: str, result: str, duration_ms: float = 0, args: dict | None = None) -> None:
            nonlocal hitl_data
            provider_approvals.capture(name, args, result)
            if hitl_data is None:
                hitl_data = parse_hitl_envelope(result)
            # A read of the document returns the document's words, which may
            # quote an error message; that is not a failed tool.
            if runtime_is_internal_failure_detail(result) and not (
                editor_round_processor is not None and name in {"read_file", "grep_files"}
            ):
                internal_tool_failure_seen[0] = True

            preview = tool_result_for_chat(name, result)
            status = tool_status_for_chat(result)
            fallback_args = last_tool_args.pop(name, None)
            tool_args = args if isinstance(args, dict) else fallback_args
            event_args = tool_arguments_for_chat(name, tool_args)
            runtime_record_tool_end_for_chat(
                tool_results,
                name,
                args=event_args,
                result=preview,
                status=status,
                duration_ms=duration_ms,
            )
            _attach_raw_tool_result(tool_results, name, result)
            assistant_blocks.end_tool(
                name,
                arguments=event_args,
                result=preview,
                # Structured results supply bounded visualization or recovery
                # evidence only; other tools keep the redacted preview path.
                structured_result=(
                    result
                    if assistant_tool_uses_structured_result(name, event_args)
                    else None
                ),
                status=status,
                duration_ms=duration_ms,
                now_ms=int(time.time() * 1000),
            )
            reset_before_next_text[0] = True
            # Send as tool_call with result + inferred status so frontend updates the card.
            # ``duration_ms`` lets the frontend show "bash · 1.2s" inline.
            event_queue.put_nowait(scoped_sse("tool_end", with_assistant_blocks({
                "tool_call": {
                    "name": name,
                    "arguments": event_args,
                    "result": preview,
                    "status": status,
                    "duration_ms": int(duration_ms),
                },
            })))
            publish_stream_snapshot(force=True)
            # ``tool_call_logs`` is only useful for failure analysis if the
            # row records the real outcome — without these two the column
            # default made every persisted row say success=True.
            tool_error = runtime_tool_call_error(result)
            trace.log_tool_exec(
                round_num=round_counter[0],
                duration_ms=duration_ms,
                tool_name=name,
                result_length=len(result),
                result_preview=preview,
                success=tool_error is None,
                error=tool_error,
                tool_args=tool_args,
                outcome=runtime_tool_call_outcome(name, result),
            )
            # Intent->tool-path memory (tool discovery v2, spec §A3):
            # fire-and-forget, never delays the tool result. Classifies
            # the raw result (approval-gate / HITL outcomes are excluded,
            # not just the UI-facing status) and holds a strong task
            # reference so it can't be GC'd mid-write.
            _schedule_tool_path_recording(
                tool_name=name, result=result, entity_id=entity_id,
                user_id=user_id, message=message, ctx=ctx,
            )

        def on_llm_call(round_num: int, duration_ms: float, usage: dict, tool_calls: list, finish_reason: str) -> None:
            trace.log_llm_call(
                round_num=round_num,
                duration_ms=duration_ms,
                usage=usage,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                message_count=len(initial_messages or []) + 2 + round_num,
                tool_count=len(tools),
            )

        # Run agentic loop in background task
        loop_result = [None]
        loop_error = [None]
        loop_cancelled = [False]
        # Another worker took over this run: it owns the row and the stream.
        loop_fenced = [False]
        error_saved_id: list[str | None] = [None]
        resolved_model = resolve_model_from_context(ctx)
        manual_skill_ids = runtime_manual_skill_ids_from_refs(manual_skill_refs)
        # One payer for the whole turn: the durable callbacks charge it and
        # the bound billing context gates it. Own short session, so no
        # transaction stays open across the loop.
        turn_payer_entity_id = await resolve_run_payer(
            None, entity_id=entity_id, workspace_id=ctx.workspace_id,
        )
        reserve_llm_call, settle_llm_call = _chat_llm_billing_callbacks(
            trace=trace,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id if persist_messages else None,
            fallback_model=resolved_model,
            payer_entity_id=turn_payer_entity_id,
        )
        _reserve_llm_call = reserve_llm_call

        async def reserve_llm_call(round_num, model, messages, max_tokens):
            # The first provider call ends turn preparation.
            trace.mark("loop_setup")
            trace.log_phases("prepare")
            await _reserve_llm_call(round_num, model, messages, max_tokens)

        async def _run_loop():
            nonlocal hitl_data, turn_recommendation
            # Loop rounds settle through the durable callbacks; the calls around
            # them (tools, compaction, completions) keep their automatic record.
            _billing_handle = runtime_set_callback_settled_billing_context(
                entity_id=entity_id or "",
                workspace_id=ctx.workspace_id,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id if persist_messages else None,
                payer_entity_id=turn_payer_entity_id,
            )
            try:
                result = await runtime_execute_chat_agent_loop(
                    runtime_envelope=ctx.runtime_envelope,
                    prompt_context=ctx,
                    system_prompt=system_prompt,
                    user_message=message,
                    tools=tools,
                    entity_id=entity_id or "",
                    user_id=user_id,
                    agent_id=agent_id,
                    workspace_id=ctx.workspace_id,
                    conversation_id=conversation_id if persist_messages else None,
                    task_id=ctx.task_id,
                    active_user_message=runtime_message_text_for_intent(message),
                    manual_skill_selected=bool(manual_skill_refs),
                    manual_skill_ids=manual_skill_ids,
                    tool_profile=ctx.tool_profile,
                    allowed_tool_names=ctx.allowed_tool_names,
                    model=resolved_model,
                    temperature=getattr(ctx, "temperature", None),
                    max_tokens=getattr(ctx, "max_tokens", None),
                    initial_messages=initial_messages or None,
                    carry_previous_checkpoint=getattr(ctx, "conversation_transcript", None) is None,
                    turn_context=getattr(ctx, "runtime_turn_context", None) or None,
                    gateway_tool_schemas=list(getattr(ctx, "gateway_tool_schemas", None) or []) or None,
                    tool_result_max_chars=(
                        TRANSCRIPT_TOOL_RESULT_MAX_CHARS
                        if getattr(ctx, "conversation_transcript", None) is not None else None
                    ),
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_llm_call=on_llm_call,
                    on_llm_call_before=reserve_llm_call,
                    on_llm_usage_settled=settle_llm_call,
                    stream_handler=on_stream_event,
                    metadata=resolve_llm_metadata_from_context(ctx),
                    forced_tool_calls=runtime_forced_tool_calls_for_turn(ctx, manual_skill_refs, message),
                    is_cancelled=is_cancelled,
                    performance_run_id=runtime_performance_run_id(
                        runtime_run_id=runtime_run_id,
                        chat_trace_id=trace.trace_id,
                    ),
                    runtime_run_id=runtime_run_id,
                    runtime_checkpoint=runtime_checkpoint,
                    # Only an owned durable run can park a Chat question.
                    # Non-resumable entrypoints omit the tool in the harness.
                    human_input_suspension=bool(runtime_run_id),
                    runtime_cancel_checker=(
                        runtime_cancel_requested if runtime_run_id else None
                    ),
                    max_rounds=runtime_turn_max_rounds(ctx),
                    final_response_contract=turn_reply_contract,
                    round_text_processor=editor_round_processor,
                )
                loop_result[0] = result
                loop_ended_at_ms = int(time.time() * 1000)
                if result and result.stop_reason != "cancelled" and await turn_cancel_requested():
                    result.stop_reason = "cancelled"
                    result.error = None
                    result.content = ""
                    hitl_data = None

                if result and result.stop_reason == "waiting_resource" and runtime_run_id:
                    assistant_blocks.finish("waiting", now_ms=loop_ended_at_ms)
                    from packages.core.config import get_settings as _runtime_settings
                    from packages.core.database import async_session as _runtime_state_session
                    from packages.core.services.runtime_run_service import (
                        persist_runtime_run_suspension,
                        project_runtime_run_status,
                    )

                    async with _runtime_state_session() as runtime_db:
                        run = await persist_runtime_run_suspension(
                            runtime_db,
                            run_id=runtime_run_id,
                            control=dict(result.control or {}),
                        )
                        projection = await project_runtime_run_status(
                            runtime_db,
                            run,
                            poll_after_seconds=(
                                _runtime_settings().SANDBOX_QUEUE_POLL_SECONDS
                            ),
                        )
                        if projection.get("human_input"):
                            from packages.core.services.runtime_human_input import (
                                record_human_input_question_message,
                            )

                            await record_human_input_question_message(runtime_db, run=run)
                        elif persist_messages and conversation_id and hitl_data:
                            from packages.core.services.runtime_human_input import (
                                record_parked_approval_message,
                            )

                            await record_parked_approval_message(
                                runtime_db, run=run, hitl_requests=hitl_requests_from_data(hitl_data),
                            )
                        await runtime_db.commit()
                    loop_parked[0] = True
                    event_queue.put_nowait(
                        scoped_sse(
                            "runtime_waiting",
                            {
                                "status": projection["status"],
                                "status_reason": projection["status_reason"],
                                "queue": projection["queue"],
                                "human_input": projection.get("human_input"),
                                **assistant_blocks.meta(),
                            },
                        )
                    )
                    return

                if result and result.stop_reason != "cancelled":
                    try:
                        turn_recommendation = await _await_chat_preparation(
                            workspace_recommendation.get(), turn_cancel_requested,
                        )
                    except _ChatPreparationCancelled:
                        result.stop_reason = "cancelled"
                        result.error = None
                        result.content = ""
                        hitl_data = None

                # Save assistant message immediately after loop completes,
                # inside the task so it persists even if the SSE client
                # disconnects before the generator resumes.
                if result and result.stop_reason == "cancelled":
                    assistant_blocks.finish("cancelled", now_ms=loop_ended_at_ms)
                    try:
                        interrupted_message_id = await save_assistant_stream_interrupted_message(
                            conversation_id=conversation_id,
                            entity_id=entity_id,
                            workspace_id=ctx.workspace_id,
                            agent_id=agent_id,
                            tool_results=tool_results,
                            attachments=await chat_turn_attachments(tool_results, entity_id=entity_id) or None,
                            message_id=persisted_message_id[0],
                            meta=assistant_blocks.meta() or None,
                        )
                        if interrupted_message_id:
                            persisted_message_id[0] = interrupted_message_id
                            loop_saved[0] = True
                            publish_stream_snapshot("interrupted", force=True)
                    except Exception as save_err:
                        logger.error("Failed to save cancelled assistant message: %s", save_err)
                elif result:
                    if (
                        hitl_data is None
                        and persist_messages
                        and conversation_id
                        and (provider_request := provider_approvals.pending_request())
                    ):
                        from packages.core.database import async_session as _approval_session_factory

                        async with _approval_session_factory() as approval_db:
                            hitl_data = await register_chat_provider_approval(
                                approval_db,
                                conversation_id=conversation_id,
                                entity_id=entity_id,
                                user_id=user_id,
                                request=provider_request,
                                runtime_envelope=ctx.runtime_envelope,
                            )
                            await approval_db.commit()
                    streamed_fallback_content = streamed_text_content[0].strip()
                    # Some providers stream visible text but return an empty
                    # final assistant message. Persist the streamed content so
                    # refresh/reload does not make the reply disappear.
                    if not (result.content or "").strip() and streamed_fallback_content and not result.tool_calls_made:
                        result.content = streamed_fallback_content
                    if result.stop_reason == "credit_exhausted" and (result.content or "").strip() in {
                        "", str(result.error or "").strip(),
                        str((result.error_detail or {}).get("message") or "").strip(),
                    }:
                        # The turn stopped on credits, not on the model: say so,
                        # rather than leaving it empty (read as "empty response")
                        # or in the limit's machine form.
                        result.content = credit_exhausted_reply(result)
                    # Apply fallback if LLM returned nothing
                    if not (result.content or "").strip() and not result.tool_calls_made:
                        result.content = (
                            "I did not get an edit back for this request. Please try again with the exact change you want."
                            if disable_tools
                            else (
                                "Sorry, the model returned an empty response. Please try again; "
                                "if this repeats, switch the selected model or reset your API key."
                            )
                        )
                    if (
                        disable_tools
                        and result.error == "empty_llm_response"
                        and (result.content or "").startswith("Sorry, the model returned an empty response")
                    ):
                        result.content = "I did not get an edit back for this request. Please try again with the exact change you want."
                    generated_fallback_content = False
                    if (
                        not result.content
                        and result.tool_calls_made
                        and hitl_data is None
                        and result.stop_reason not in {"hitl_required", "needs_hitl"}
                    ):
                        if result.stop_reason == "cancelled":
                            result.content = ""
                        elif (
                            internal_tool_failure_seen[0]
                            or result.error
                            or result.stop_reason in {"error", "credit_exhausted"}
                        ):
                            result.content = PUBLIC_REQUEST_FAILURE_MESSAGE
                        else:
                            result.content = runtime_fallback_stream_final_summary(
                                result.tool_calls_made,
                                tool_results,
                            )
                        generated_fallback_content = bool(result.content)
                    if result.content:
                        result.content = _strip_final_response_sentinel(result.content)
                    sanitized_content = runtime_sanitize_assistant_content_after_loop(
                        result.content or "",
                        internal_failure_seen=internal_tool_failure_seen[0],
                    )
                    sanitized_content = await enforce_reply_file_links(
                        sanitized_content, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                    )
                    if internal_tool_failure_seen[0] and not sanitized_content.strip():
                        sanitized_content = PUBLIC_REQUEST_FAILURE_MESSAGE
                    if (
                        hitl_data is not None
                        and not result.error
                        and result.stop_reason not in {"error", "credit_exhausted", "cancelled"}
                        and sanitized_content == PUBLIC_REQUEST_FAILURE_MESSAGE
                    ):
                        # The turn recovered far enough to create an actionable
                        # human review.  The card is now the terminal state, so
                        # a generic failure sentence above it is contradictory.
                        sanitized_content = ""
                        replace_visible_text("", clear=True)
                    content_was_sanitized = sanitized_content != (result.content or "")
                    force_visible_replacement = bool(
                        result.content
                        and isinstance(getattr(result, "control", None), dict)
                        and result.control.get("replace_visible_text")
                    )
                    if content_was_sanitized:
                        result.content = sanitized_content
                    visible_content = _chat_result_visible_content(result, runtime_surface, tool_results)
                    process_lifecycle = _chat_result_process_lifecycle(result, hitl_data=hitl_data)
                    preserve_progress = not content_was_sanitized and process_lifecycle != "failed"
                    assistant_blocks.finish(
                        process_lifecycle,
                        now_ms=loop_ended_at_ms,
                        outcome=getattr(result, "outcome", None),
                    )
                    if visible_content != (result.content or ""):
                        # Reset partial text before emitting a known public failure.
                        # Keep the original result and stored body for diagnostics.
                        replace_visible_text(visible_content, final_summary=True)
                    elif internal_tool_failure_seen[0] and result.content:
                        # Replace suppressed final deltas, not earlier public
                        # progress from a turn that still reached a valid outcome.
                        replace_visible_text(result.content, final_summary=True, preserve_progress=preserve_progress)
                    elif content_was_sanitized:
                        if streamed_text[0] and sanitized_content:
                            # Only reset+re-emit when there is replacement content to show.
                            # If sanitized is empty we already streamed the best version;
                            # emitting a bare text_reset would wipe the user's visible text.
                            replace_visible_text(sanitized_content)
                    elif streamed_text[0] and result.content and (
                        force_visible_replacement or generated_fallback_content or pending_text_reset[0]
                    ):
                        # A canonical final reply supersedes candidate answer
                        # text, while confirmed public progress remains visible.
                        replace_visible_text(result.content, final_summary=True, preserve_progress=preserve_progress)
                    if result.error or result.stop_reason == "error":
                        flush_final_summary_text(visible_content)
                        assistant_blocks.set_final_text(visible_content)
                    if persist_messages and conversation_id:
                        try:
                            pending_action = (
                                workspace_operation_pending_action_from_data(hitl_data)
                                if ctx.workspace_id
                                else None
                            )
                            assistant_meta = _chat_assistant_result_meta(result)
                            attachments = await chat_turn_attachments(tool_results, entity_id=entity_id)
                            flush_final_summary_text(visible_content)
                            assistant_blocks.set_final_text(visible_content)
                            pending_post_tool_text[0] = ""
                            terminal_result_failed = bool(result.error) or result.stop_reason in {
                                "error",
                                "credit_exhausted",
                            }
                            assistant_meta.update(assistant_blocks.meta())
                            runtime_meta = runtime_context_meta(ctx)
                            if runtime_meta:
                                assistant_meta["runtime"] = runtime_meta
                            assistant_meta.update(
                                _runtime_assistant_origin_meta(ctx)
                            )
                            if turn_recommendation:
                                assistant_meta["workspace_recommendation"] = turn_recommendation
                            hitl_requests = hitl_requests_from_data(hitl_data)
                            if hitl_requests:
                                assistant_meta["hitl_requests"] = hitl_requests
                            if sub_agent_events:
                                assistant_meta["sub_agent_events"] = sub_agent_events
                            if attachments:
                                assistant_meta["attachments"] = attachments
                            saved_id = await _save_final_assistant_message(
                                conversation_id=conversation_id,
                                entity_id=entity_id,
                                workspace_id=ctx.workspace_id,
                                agent_id=agent_id,
                                message_id=persisted_message_id[0],
                                content=result.content or "",
                                tool_calls=tool_results if tool_results else None,
                                attachments=attachments if attachments else None,
                                token_usage=result.usage,
                                meta=assistant_meta or None,
                                message_kind="hitl_request" if (pending_action or hitl_requests) else "text",
                                pending_action=pending_action,
                            )
                            if saved_id:
                                persisted_message_id[0] = saved_id
                                loop_saved[0] = True
                                await _append_turn_transcript(
                                    ctx, result, run_id=runtime_run_id, assistant_message_id=saved_id,
                                )
                                # Followers need the finished answer and the
                                # "stopped working" edge; without this they sit
                                # on the last partial until they reload.
                                publish_stream_snapshot(
                                    "error" if terminal_result_failed else "done",
                                    force=True,
                                    content=visible_content,
                                )
                                await runtime_persist_chat_stream_runtime_events(
                                    ctx.runtime_envelope,
                                    message_id=saved_id,
                                    trace_id=trace.trace_id,
                                )
                            else:
                                # Every save attempt failed, so the database
                                # itself is unreachable. The user already has
                                # the answer: keep it on screen rather than
                                # replacing it with a failure. Stale-stream
                                # repair settles the placeholder row later.
                                logger.error(
                                    "final assistant message not saved after retries "
                                    "conversation=%s message=%s",
                                    conversation_id, persisted_message_id[0],
                                )
                                publish_stream_snapshot(
                                    "error" if terminal_result_failed else "done",
                                    force=True,
                                    content=visible_content,
                                )
                        except Exception as save_err:
                            logger.error("Failed to save assistant message: %s", save_err)
                    else:
                        loop_saved[0] = True
                        flush_final_summary_text(visible_content)

                    # Per-call token usage and credit ledger rows were already
                    # committed before the loop could continue. This transaction
                    # records only post-turn learning evidence.
                    try:
                        from packages.core.database import async_session as _sf2
                        async with _sf2() as usage_db:
                            queued_learning_ids = (
                                await record_chat_runtime_learning(
                                    usage_db,
                                    entity_id=entity_id,
                                    user_id=user_id,
                                    agent_id=agent_id,
                                    conversation_id=conversation_id,
                                    message_id=persisted_message_id[0],
                                    message=message,
                                    result=result,
                                    ctx=ctx,
                                    trace=trace,
                                    tool_results=tool_results,
                                )
                                if (
                                    persist_messages
                                    and conversation_id
                                    and loop_saved[0]
                                    and not loop_error[0]
                                )
                                else []
                            )
                            await usage_db.commit()
                            await schedule_learning_candidate_applies(
                                usage_db,
                                entity_id=entity_id,
                                candidate_ids=queued_learning_ids,
                                workspace_id=ctx.workspace_id,
                                user_id=user_id,
                            )
                    except Exception as usage_err:
                        logger.error("Failed to persist usage/runtime evidence: %s", usage_err)

            except LLMRateLimited as exc:
                # Background jobs use this BaseException signal to release a
                # lease and retry later. An interactive SSE turn must instead
                # close with the normal error and stream_end events.
                loop_error[0] = exc
                assistant_blocks.finish("failed")
            except asyncio.CancelledError:
                # The generator cancels us on disconnect-before-tools; it then
                # persists the interrupted row and publishes the terminal
                # snapshot itself, in that order. Publishing "done" from here
                # would tell followers an aborted turn finished cleanly.
                loop_cancelled[0] = True
                assistant_blocks.finish("cancelled")
                raise
            except RuntimeContextWriterFenced:
                # Not a failure: the new owner continues the turn. Write
                # nothing, or this stale copy overwrites its answer.
                loop_fenced[0] = True
                assistant_blocks.finish("cancelled")
                logger.warning(
                    "chat turn stopped: run taken over by another worker conversation=%s",
                    conversation_id,
                )
            except Exception as exc:
                loop_error[0] = exc
                assistant_blocks.finish("failed")
                # A detached turn has no generator left to persist its failure:
                # stream_chat_response's error handling sits after the yield
                # loop, unreachable once the client is gone. Without this the
                # row claims "streaming" until the next process restart, and the
                # refetch that our terminal snapshot triggers finds a healthy-
                # looking checkpoint contradicting the "error" status.
                if persist_messages and conversation_id and not loop_saved[0]:
                    try:
                        error_saved_id[0] = await save_assistant_stream_error_message(
                            conversation_id=conversation_id,
                            entity_id=entity_id,
                            workspace_id=ctx.workspace_id,
                            agent_id=agent_id,
                            error_message=str(exc) or "Unknown error",
                            message_id=persisted_message_id[0],
                            meta=assistant_blocks.meta() or None,
                        )
                        if error_saved_id[0]:
                            persisted_message_id[0] = error_saved_id[0]
                    except Exception:
                        logger.exception("Failed to persist detached turn error")
            finally:
                runtime_release_billing_context(_billing_handle)
                result = loop_result[0]
                if result and (result.content or text_buffer[0] or pending_post_tool_text[0]):
                    flush_final_summary_text(_chat_result_visible_content(result, runtime_surface, tool_results))
                else:
                    flush_text_buffer()
                event_queue.put_nowait(None)  # sentinel
                # Terminal edge for every non-cancel path the save above does
                # not cover. No-op once a "done" snapshot already went out.
                # Deliberately not awaited: this block also runs under
                # cancellation, where an await re-raises.
                if not loop_cancelled[0] and not loop_fenced[0] and not (
                    result and result.stop_reason == "cancelled"
                ):
                    publish_stream_snapshot(
                        "error" if loop_error[0] or (
                            result and (result.error or result.stop_reason == "error")
                        ) else "done",
                        force=True,
                        content=(_chat_result_visible_content(result, runtime_surface, tool_results) if result else None),
                    )

        if await turn_cancel_requested():
            raise _ChatPreparationCancelled()
        task = asyncio.create_task(_run_loop())

        # Yield events as they arrive + keepalive.
        # If the client disconnects (GeneratorExit is thrown at the yield point
        # by StreamingResponse.aclose()), the finally below decides what to do
        # with the still-running background agentic-loop task:
        #   - if a tool/skill has already started, let it finish and persist
        #     (it saves inside the task) so a long skill is not lost on
        #     navigate-away — detach it so it is not GC'd;
        #   - otherwise cancel it, so a still-idle response does not keep
        #     consuming LLM tokens / tool calls for a viewer who left.
        try:
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=KEEPALIVE_INTERVAL)
                    if event is None:
                        break  # loop done — fall through to await task below
                    yield event
                except asyncio.TimeoutError:
                    yield scoped_sse("keepalive", {})
        finally:
            if not task.done():
                if tools_started[0] and persist_messages and conversation_id:
                    # Substantive work underway — keep it running to completion;
                    # the loop persists its own result (loop_saved).
                    _detach_chat_turn(task)
                else:
                    # StreamingResponse cancels an AnyIO scope on disconnect;
                    # each cleanup await would be cancelled again without a
                    # shield. Bound cleanup so an unavailable DB cannot hang
                    # the disconnected request indefinitely.
                    with anyio.move_on_after(5, shield=True) as finalize_scope:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                        if persist_messages and conversation_id and not loop_saved[0]:
                            if _stream_text_is_durable_checkpoint(streamed_text_content[0]):
                                await checkpoint_stream_text(force=True)
                            interrupted_message_id = await save_assistant_stream_interrupted_message(
                                conversation_id=conversation_id,
                                entity_id=entity_id,
                                workspace_id=ctx.workspace_id,
                                agent_id=agent_id,
                                tool_results=tool_results,
                                attachments=await chat_turn_attachments(tool_results, entity_id=entity_id) or None,
                                message_id=persisted_message_id[0],
                                partial_content=(
                                    streamed_text_content[0].strip()
                                    if _stream_text_is_durable_checkpoint(streamed_text_content[0])
                                    else None
                                ),
                                meta=assistant_blocks.meta() or None,
                            )
                            if interrupted_message_id:
                                persisted_message_id[0] = interrupted_message_id
                                # Publish only after the interruption was saved.
                                publish_stream_snapshot("interrupted", force=True)
                    if finalize_scope.cancel_called:
                        logger.warning("Timed out finalizing interrupted chat stream")

        # Normal-exit path: task is already done, this is a fast no-op.
        await task
        if loop_fenced[0]:
            # The owning worker emits this turn's terminal events.
            stream_completed = True
            return

        terminal_error_id: str | None = None
        if loop_error[0]:
            error_message = str(loop_error[0]) or "Unknown error"
            public_error_message = runtime_assistant_stream_error_content(error_message)
            assistant_blocks.set_final_text(public_error_message)
            trace.log_error(error_message, phase="agentic_loop")
            # _run_loop already persisted the row (it must: on a detached turn
            # this code never runs). Re-saving here would double-broadcast.
            saved_error_id = error_saved_id[0] or (
                await save_assistant_stream_error_message(
                    conversation_id=conversation_id,
                    entity_id=entity_id,
                    workspace_id=ctx.workspace_id,
                    agent_id=agent_id,
                    error_message=error_message,
                    message_id=persisted_message_id[0],
                    meta=assistant_blocks.meta() or None,
                )
                if persist_messages and conversation_id
                else None
            )
            if saved_error_id:
                persisted_message_id[0] = saved_error_id
            terminal_error_id = saved_error_id
            if streamed_text[0] or has_emitted_text[0]:
                yield scoped_sse("text_reset", {})
                yield scoped_sse(
                    "text_delta",
                    {"content": public_error_message},
                )
            yield scoped_sse(
                "error",
                {
                    "message": public_error_message,
                    **assistant_blocks_stream_payload(assistant_blocks),
                    "persisted": bool(saved_error_id),
                    "message_id": saved_error_id,
                },
                fallback_message_id=False,
            )

        # HITL check
        if hitl_data:
            yield scoped_sse("hitl_required", hitl_data)

        result = loop_result[0]
        if result and result.stop_reason == "waiting_resource":
            trace.total_usage = result.usage or {}
            # Parked, not stopped: the run resumes once its request is answered.
            stream_completed = True
            return
        if result:
            trace.total_usage = result.usage or {}

            # Stream the final text only if no visible text was emitted. Tool-only
            # turns can publish their terminal summary through the internal event
            # queue without producing LLM tokens; checking ``streamed_text`` alone
            # re-emits that same summary here and duplicates it in the chat bubble.
            if result.content and not has_emitted_text[0]:
                yield scoped_sse("text_delta", {"content": _chat_result_visible_content(result, runtime_surface, tool_results)})

            trace.log_complete(result=result)

            stream_completed = True
            public_tool_events = _tool_calls_for_snapshot(tool_results)
            tool_call_names = [
                str(item.get("name"))
                for item in public_tool_events
                if isinstance(item, dict) and item.get("name")
            ]
            attachments = await chat_turn_attachments(tool_results, entity_id=entity_id)
            end_payload = {
                "conversation_id": conversation_id,
                "message_id": persisted_message_id[0],
                "persisted": bool(loop_saved[0] and persisted_message_id[0]),
                "usage": _chat_result_public_usage(result, runtime_surface),
                "rounds": result.rounds,
                "tool_calls": tool_call_names,
                "stop_reason": result.stop_reason,
            }
            if result.stop_reason == "cancelled":
                end_payload["status"] = "cancelled"
            elif result.error or result.stop_reason == "error":
                end_payload["status"] = "error"
            end_payload.update(assistant_blocks_stream_payload(assistant_blocks))
            if attachments:
                end_payload["attachments"] = attachments
            if turn_recommendation:
                end_payload["workspace_recommendation"] = turn_recommendation
            if result.error and result.stop_reason != "cancelled":
                end_payload["error"] = _chat_result_public_error(result, runtime_surface, tool_results)
                if result.error in {
                    "final_response_validation_failed",
                    "sandbox_allocation_failed",
                    "sandbox_queue_timeout",
                    "sandbox_capacity_timeout",
                    "sandbox_runner_unavailable",
                    "sandbox_allocation_rejected",
                    "llm_configuration_missing",
                    "llm_call_failed",
                    "llm_provider_unavailable",
                    BYOK_PROVIDER_ERROR_CODE,
                }:
                    end_payload["error_code"] = result.error
            if getattr(result, "error_detail", None):
                end_payload["limit_detail"] = _chat_result_public_failure_payload(
                    result, runtime_surface
                )
            yield scoped_sse(
                "stream_end",
                runtime_public_transport_payload(end_payload),
            )
        else:
            trace.log_complete()
            stream_completed = True
            terminal_message_id = (
                terminal_error_id if loop_error[0] else persisted_message_id[0]
            )
            terminal_payload = {
                "conversation_id": conversation_id,
                "usage": {},
                "message_id": terminal_message_id,
                "persisted": bool(terminal_message_id),
            }
            if not loop_error[0]:
                terminal_payload = with_assistant_blocks(terminal_payload)
            yield scoped_sse(
                "stream_end",
                terminal_payload,
                fallback_message_id=not bool(loop_error[0]),
            )

    except _ChatPreparationCancelled:
        blocks = AssistantBlocksBuilder(started_at_ms=turn_started_at_ms)
        blocks.finish("cancelled")
        saved_id = (
            await save_assistant_stream_interrupted_message(
                conversation_id=conversation_id, entity_id=entity_id,
                workspace_id=workspace_id, agent_id=agent_id,
                message_id=persisted_message_id[0], meta=blocks.meta(),
            ) if persist_messages and conversation_id else None
        )
        if saved_id:
            persisted_message_id[0] = saved_id
        stream_completed = True
        trace.log_complete()
        yield scoped_sse("stream_end", {
            "message_id": saved_id, "persisted": bool(saved_id),
            "status": "cancelled", "stop_reason": "cancelled", "usage": {},
            **assistant_blocks_stream_payload(blocks),
        })
    except Exception as exc:
        error_message = f"Internal error: {exc}"
        public_error_message = runtime_assistant_stream_error_content(error_message)
        trace.log_error(str(exc), phase="stream_chat_response")
        logger.error("stream_chat_response failed: %s", exc, exc_info=True)
        saved_error_id = (
            await save_assistant_stream_error_message(
                conversation_id=conversation_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                error_message=error_message,
                message_id=persisted_message_id[0],
            )
            if persist_messages and conversation_id
            else None
        )
        if saved_error_id:
            persisted_message_id[0] = saved_error_id
        yield scoped_sse(
            "error",
            {
                "message": public_error_message,
                "persisted": bool(saved_error_id),
                "message_id": saved_error_id,
            },
            fallback_message_id=False,
        )
        yield scoped_sse(
            "stream_end",
            {
                "conversation_id": conversation_id,
                "usage": {},
                "message_id": saved_error_id,
                "persisted": bool(saved_error_id),
            },
            fallback_message_id=False,
        )
    finally:
        if tool_stream_token is not None:
            try:
                runtime_tool_stream_sink_var.reset(tool_stream_token)
            except Exception:
                pass
        if (
            persist_messages and conversation_id and not stream_completed and hitl_data
            and not loop_parked[0]
        ):
            try:
                hitl = hitl_data.get("hitl") if isinstance(hitl_data, dict) else {}
                hitl_id = str((hitl or {}).get("id") or "").strip()
                from packages.core.database import async_session as _cancel_session_factory
                from packages.core.services.chat_approvals import cancel_chat_approvals
                async with _cancel_session_factory() as cancel_db:
                    cancelled = await cancel_chat_approvals(
                        cancel_db,
                        conversation_id=conversation_id,
                        entity_id=entity_id,
                        user_id=user_id,
                        hitl_ids=[hitl_id] if hitl_id else None,
                        reason="stream_stopped",
                    )
                    if cancelled.get("cancelled"):
                        await cancel_db.commit()
            except Exception:
                logger.debug("Failed to cancel stale HITL approvals for stopped stream", exc_info=True)


# ---------------------------------------------------------------------------
# Non-streaming chat (full agentic loop)
# ---------------------------------------------------------------------------

async def run_chat_message(
    message: str,
    conversation_id: str,
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    db: AsyncSession | None = None,
    manual_skill_refs: list[dict] | None = None,
    blocked_tools: list[str] | tuple[str, ...] | set[str] | str | None = None,
    editor_context: dict | None = None,
    runtime_metadata: dict | None = None,
    runtime_surface: ChatSurface | str | None = None,
) -> dict:
    """
    Non-streaming chat: runs the full agentic loop with multi-turn tool execution.
    Returns dict with {conversation_id, message_id, content, tool_calls_made, usage}.
    """
    turn_started_at_ms = int(time.time() * 1000)
    # ── Trace ──
    trace = ChatTrace(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    trace.log_request(message)

    _billing_handle = None
    _tool_stream_token = None
    try:
        system_prompt, tools, initial_messages, ctx = (
            await resolve_runtime_chat_context(
                db,
                message,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                manual_skill_refs=manual_skill_refs,
                trace=trace,
                blocked_tools=blocked_tools,
                editor_context=editor_context,
                runtime_metadata=runtime_metadata,
                runtime_surface=runtime_surface,
                split_turn_context=True,
            )
        )
        agent_id = getattr(ctx, "agent_id", None) or agent_id
        workspace_recommendation = _TurnWorkspaceRecommendation(ctx)

        # Per-round LLM call logging
        _round_counter = [0]
        _last_tool_args: dict[str, dict] = {}
        _tool_results: list[dict] = []
        sub_agent_events: list[dict] = []
        provider_approvals = ProviderApprovalCollector()
        hitl_data: dict | None = None

        def _record_nested_tool_event(event_type: str, data: dict) -> None:
            nonlocal hitl_data
            tool_call = data.get("tool_call") if isinstance(data, dict) else None
            if not isinstance(tool_call, dict):
                return
            name = str(tool_call.get("name") or "tool")
            args = tool_call.get("arguments")
            provider_approvals.capture_recorded_tool_call(tool_call)
            if event_type == "tool_start":
                runtime_record_tool_start_for_chat(_tool_results, name, args)
            elif event_type == "tool_end":
                runtime_record_tool_end_for_chat(
                    _tool_results,
                    name,
                    args=args,
                    result=tool_call.get("result"),
                    status=tool_call.get("status"),
                    duration_ms=tool_call.get("duration_ms"),
                )
                persisted_result = tool_call.get("raw_result")
                if isinstance(persisted_result, str) and persisted_result:
                    _attach_raw_tool_result(_tool_results, name, persisted_result)
                    # First blocker wins — see the streaming path above.
                    if hitl_data is None:
                        hitl_data = parse_hitl_envelope(persisted_result)

        def _record_sub_agent_event(event: dict) -> None:
            runtime_record_sub_agent_event_for_chat(sub_agent_events, event)

        _tool_stream_token = runtime_tool_stream_sink_var.set(RuntimeToolStreamSink(
            record_tool_event=_record_nested_tool_event,
            record_sub_agent_event=_record_sub_agent_event,
            format_tool_arguments=tool_arguments_for_chat,
            format_tool_result=tool_result_for_chat,
            resolve_tool_status=tool_status_for_chat,
        ))

        def _on_llm_call(round_num: int, duration_ms: float, usage: dict, tool_calls_req: list, finish_reason: str) -> None:
            trace.log_llm_call(
                round_num=round_num,
                duration_ms=duration_ms,
                usage=usage,
                tool_calls=tool_calls_req,
                finish_reason=finish_reason,
                message_count=len(initial_messages or []) + 2 + round_num,
                tool_count=len(tools),
            )

        def _on_tool_start(name: str, args: dict) -> None:
            _round_counter[0] += 1
            _last_tool_args[name] = args
            runtime_record_tool_start_for_chat(
                _tool_results,
                name,
                tool_arguments_for_chat(name, args),
            )

        def _on_tool_end(name: str, result: str, duration_ms: float = 0, args: dict | None = None) -> None:
            nonlocal hitl_data
            provider_approvals.capture(name, args, result)
            if hitl_data is None:
                hitl_data = parse_hitl_envelope(result)
            preview = tool_result_for_chat(name, result)
            status = tool_status_for_chat(result)
            fallback_args = _last_tool_args.pop(name, None)
            tool_args = args if isinstance(args, dict) else fallback_args
            runtime_record_tool_end_for_chat(
                _tool_results,
                name,
                args=tool_arguments_for_chat(name, tool_args),
                result=preview,
                status=status,
                duration_ms=duration_ms,
            )
            _attach_raw_tool_result(_tool_results, name, result)
            # See the identical comment in stream_chat_response's on_tool_end.
            tool_error = runtime_tool_call_error(result)
            trace.log_tool_exec(
                round_num=_round_counter[0],
                duration_ms=duration_ms,
                tool_name=name,
                result_length=len(result),
                result_preview=preview,
                success=tool_error is None,
                error=tool_error,
                tool_args=tool_args,
                outcome=runtime_tool_call_outcome(name, result),
            )
            # Intent->tool-path memory (tool discovery v2, spec §A3): see
            # the identical comment in stream_chat_response's on_tool_end.
            _schedule_tool_path_recording(
                tool_name=name, result=result, entity_id=entity_id,
                user_id=user_id, message=message, ctx=ctx,
            )

        # The context already applies the Agent override before tenant and
        # platform defaults, matching the streaming path.
        _resolved_model = resolve_model_from_context(ctx)
        _reserve_llm_call, _settle_llm_call = _chat_llm_billing_callbacks(
            trace=trace,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id,
            fallback_model=_resolved_model,
        )

        # Loop rounds settle through the durable callbacks; the calls around
        # them (tools, compaction, completions) keep their automatic record.
        _billing_handle = runtime_set_callback_settled_billing_context(
            entity_id=entity_id or "",
            workspace_id=ctx.workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        manual_skill_ids = runtime_manual_skill_ids_from_refs(manual_skill_refs)

        is_cancelled = None
        voice_origin_message_id = str(
            (runtime_metadata or {}).get("origin_user_message_id") or ""
        ).strip()
        if (
            (runtime_metadata or {}).get("voice_session_mode") == "chat_gateway"
            and voice_origin_message_id
        ):
            from packages.core.database import async_session as _voice_cancel_session
            from packages.core.services.voice.work_queue import (
                voice_work_was_interrupted,
            )

            async def voice_turn_cancelled() -> bool:
                async with _voice_cancel_session() as cancel_db:
                    return await voice_work_was_interrupted(
                        cancel_db,
                        message_id=voice_origin_message_id,
                        conversation_id=conversation_id,
                    )

            is_cancelled = voice_turn_cancelled

        result = await runtime_execute_chat_agent_loop(
            runtime_envelope=ctx.runtime_envelope,
            prompt_context=ctx,
            system_prompt=system_prompt,
            user_message=message,
            tools=tools,
            round_text_processor=editor_document_round_processor(ctx.runtime_envelope, entity_id),
            entity_id=entity_id or "",
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id,
            task_id=ctx.task_id,
            active_user_message=runtime_message_text_for_intent(message),
            manual_skill_selected=bool(manual_skill_refs),
            manual_skill_ids=manual_skill_ids,
            tool_profile=ctx.tool_profile,
            allowed_tool_names=ctx.allowed_tool_names,
            model=_resolved_model,
            temperature=getattr(ctx, "temperature", None),
            max_tokens=getattr(ctx, "max_tokens", None),
            initial_messages=initial_messages if initial_messages else None,
            carry_previous_checkpoint=getattr(ctx, "conversation_transcript", None) is None,
            turn_context=getattr(ctx, "runtime_turn_context", None) or None,
            gateway_tool_schemas=list(getattr(ctx, "gateway_tool_schemas", None) or []) or None,
            tool_result_max_chars=(
                TRANSCRIPT_TOOL_RESULT_MAX_CHARS
                if getattr(ctx, "conversation_transcript", None) is not None else None
            ),
            on_tool_start=_on_tool_start,
            on_tool_end=_on_tool_end,
            on_llm_call=_on_llm_call,
            on_llm_call_before=_reserve_llm_call,
            on_llm_usage_settled=_settle_llm_call,
            metadata=resolve_llm_metadata_from_context(ctx),
            forced_tool_calls=runtime_forced_tool_calls_for_turn(ctx, manual_skill_refs, message),
            performance_run_id=runtime_performance_run_id(
                chat_trace_id=trace.trace_id,
            ),
            max_rounds=runtime_turn_max_rounds(ctx),
            final_response_contract=_workspace_final_response_contract(ctx),
            is_cancelled=is_cancelled,
        )

        trace.total_usage = result.usage or {}
        trace.log_complete(result=result)

    except Exception as exc:
        trace.log_error(str(exc), phase="run_chat_message")
        raise
    finally:
        if _tool_stream_token is not None:
            try:
                runtime_tool_stream_sink_var.reset(_tool_stream_token)
            except Exception:
                pass
        runtime_release_billing_context(_billing_handle)

    turn_ended_at_ms = int(time.time() * 1000)
    if result:
        internal_failure_seen = any(
            isinstance(runtime_message, dict)
            and runtime_message.get("role") == "tool"
            and runtime_is_internal_failure_detail(runtime_message.get("content"))
            for runtime_message in (result.messages or [])
        )
        result.content = await enforce_reply_file_links(
            runtime_sanitize_assistant_content_after_loop(
                result.content or "",
                internal_failure_seen=internal_failure_seen,
            ),
            entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        )

    if (
        hitl_data is None
        and db
        and conversation_id
        and (provider_request := provider_approvals.pending_request())
    ):
        hitl_data = await register_chat_provider_approval(
            db,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            request=provider_request,
            runtime_envelope=ctx.runtime_envelope,
        )

    if (
        result
        and hitl_data is not None
        and not result.error
        and result.stop_reason not in {"error", "credit_exhausted", "cancelled"}
        and result.content == PUBLIC_REQUEST_FAILURE_MESSAGE
    ):
        result.content = ""

    # Save assistant message to DB
    message_id = None
    if db and (result.content or _tool_results):
        try:
            author_subscription_id = await resolve_author_subscription_id(
                db,
                entity_id=entity_id,
                workspace_id=ctx.workspace_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            pending_action = (
                workspace_operation_pending_action_from_data(hitl_data)
                if ctx.workspace_id
                else None
            )
            assistant_meta = _chat_assistant_result_meta(result)
            attachments = await chat_turn_attachments(_tool_results, entity_id=entity_id, db=db)
            runtime_meta = runtime_context_meta(ctx)
            if runtime_meta:
                assistant_meta["runtime"] = runtime_meta
            assistant_meta.update(_runtime_assistant_origin_meta(ctx))
            recommendation = await workspace_recommendation.get()
            if recommendation:
                assistant_meta["workspace_recommendation"] = recommendation
            hitl_requests = hitl_requests_from_data(hitl_data)
            if hitl_requests:
                assistant_meta["hitl_requests"] = hitl_requests
            if sub_agent_events:
                assistant_meta["sub_agent_events"] = sub_agent_events
            if attachments:
                assistant_meta["attachments"] = attachments
            assistant_blocks = AssistantBlocksBuilder(
                skill_display_names=_assistant_blocks_skill_display_names(ctx, manual_skill_refs),
                workspace_id=getattr(ctx, "workspace_id", None),
                started_at_ms=turn_started_at_ms,
            )
            for item in _tool_results:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "tool")
                args = item.get("arguments")
                assistant_blocks.start_tool(name, args)
                assistant_blocks.end_tool(
                    name,
                    arguments=args,
                    result=item.get("result"),
                    structured_result=(
                        item.get("raw_result")
                        if assistant_tool_uses_structured_result(name, args)
                        else None
                    ),
                    status=item.get("status"),
                    duration_ms=item.get("duration_ms"),
                )
            assistant_blocks.set_final_text(_chat_result_visible_content(result, getattr(ctx.runtime_envelope, "surface", None), _tool_results))
            assistant_blocks.finish(
                _chat_result_process_lifecycle(result, hitl_data=hitl_data),
                now_ms=turn_ended_at_ms,
                outcome=getattr(result, "outcome", None),
            )
            assistant_meta.update(assistant_blocks.meta())
            msg = await add_message(
                db, conversation_id,
                role="assistant",
                content=result.content or "",
                tool_calls=(
                    _tool_results if _tool_results else None
                ),
                attachments=attachments if attachments else None,
                token_usage=result.usage,
                author_subscription_id=author_subscription_id,
                meta=assistant_meta or None,
                message_kind="hitl_request" if (pending_action or hitl_requests) else "text",
                pending_action=pending_action,
            )
            message_id = msg.id
            queued_learning_ids = await record_chat_runtime_learning(
                db,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                message_id=message_id,
                message=message,
                result=result,
                ctx=ctx,
                trace=trace,
                tool_results=_tool_results,
            )
            await db.commit()
            await _append_turn_transcript(ctx, result, run_id=None, assistant_message_id=message_id)
            await runtime_persist_chat_runtime_events(
                ctx.runtime_envelope,
                message_id=message_id,
                trace_id=trace.trace_id,
            )
            await schedule_learning_candidate_applies(
                db,
                entity_id=entity_id,
                candidate_ids=queued_learning_ids,
                workspace_id=ctx.workspace_id,
                user_id=user_id,
            )
        except Exception as save_err:
            logger.error("Failed to save assistant message: %s", save_err)

    tool_call_names = [
        str(item.get("name"))
        for item in _tool_results
        if isinstance(item, dict) and item.get("name")
    ]
    attachments = await chat_turn_attachments(_tool_results, entity_id=entity_id, db=db)
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "content": _chat_result_visible_content(result, runtime_surface, _tool_results),
        "tool_calls_made": tool_call_names or result.tool_calls_made,
        "usage": _chat_result_public_usage(result, runtime_surface),
        "rounds": result.rounds,
        "stop_reason": result.stop_reason,
        "error": _chat_result_public_error(result, runtime_surface, _tool_results),
        "limit_detail": _chat_result_public_failure_payload(result, runtime_surface),
        "hitl_requests": hitl_requests_from_data(hitl_data),
        "attachments": attachments,
        "workspace_recommendation": await workspace_recommendation.get(),
    }
