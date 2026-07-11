"""Chat service — conversations, messages, SSE streaming."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.chat_logger import ChatTrace
from packages.core.ai.runtime import (
    ChannelRuntimeContext,
    ChatSurface,
    runtime_context_meta,
    runtime_execute_chat_agent_loop,
    runtime_manual_skill_ids_from_refs,
    runtime_persist_chat_runtime_events,
    runtime_persist_chat_stream_runtime_events,
    runtime_release_billing_context,
    runtime_set_suppressed_billing_context,
)
from packages.core.ai.runtime.skill_forcing import (
    runtime_forced_tool_calls_for_turn,
    runtime_message_text_for_intent,
)
from packages.core.ai.runtime.output_policy import (
    runtime_assistant_stream_error_content,
    runtime_assistant_result_meta,
    runtime_coerce_visible_text_language,
    runtime_fallback_stream_final_summary,
    runtime_prefers_chinese,
    runtime_sanitize_assistant_content_after_loop,
)
from packages.core.ai.runtime.streams import (
    RuntimeToolStreamSink,
    runtime_record_tool_end_for_chat,
    runtime_record_tool_start_for_chat,
    runtime_should_flush_stream_text,
    runtime_tool_arguments_for_chat as tool_arguments_for_chat,
    runtime_tool_result_for_chat as tool_result_for_chat,
    runtime_tool_status_for_chat as tool_status_for_chat,
    runtime_tool_stream_sink_var,
)
from packages.core.services.conversation_messages import (
    add_message,
    resolve_author_subscription_id,
    save_assistant_stream_error_message,
    save_assistant_stream_interrupted_message,
    save_or_update_assistant_stream_message,
)
from packages.core.services.assistant_blocks import (
    AssistantBlocksBuilder,
    assistant_blocks_stream_payload,
)
from packages.core.services.chat_artifacts import chat_attachments_from_tool_results
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
from packages.core.services.usage_service import record_chat_llm_usage

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 15  # seconds
STREAM_CHECKPOINT_INTERVAL = 2.0  # seconds
STREAM_CHECKPOINT_MIN_CHARS = 400
STREAM_CHECKPOINT_MIN_DURABLE_CHARS = 24
FINAL_RESPONSE_SENTINEL = "<manor-final-response>"
FINAL_RESPONSE_SENTINELS = (
    FINAL_RESPONSE_SENTINEL,
    "</manor-final-response>",
)


def _attach_raw_tool_result(tool_events: list[dict], name: str, result: str) -> None:
    for event in reversed(tool_events):
        if (
            isinstance(event, dict)
            and event.get("name") == name
            and event.get("status") != "pending"
            and "raw_result" not in event
        ):
            event["raw_result"] = result
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
    # ── Trace ──
    trace = ChatTrace(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
    )
    trace.log_request(message)

    stream_completed = False
    hitl_data: dict | None = None
    persisted_message_id: list[str | None] = [
        assistant_message_id if persist_messages else None
    ]
    tool_stream_token = None

    def current_stream_message_id() -> str | None:
        return persisted_message_id[0] or assistant_message_id

    def scoped_payload(payload: dict | None = None) -> dict:
        data = dict(payload or {})
        if not data.get("conversation_id"):
            data["conversation_id"] = conversation_id
        if not data.get("message_id"):
            data["message_id"] = current_stream_message_id()
        return data

    def scoped_sse(event: str, payload: dict | None = None) -> str:
        return format_sse(event, scoped_payload(payload))

    yield scoped_sse("stream_start", {
        "ephemeral": not persist_messages,
    })

    try:
        # Use a fresh DB session for context resolution so the request-scoped
        # session (from Depends(get_db)) is released immediately.  This prevents
        # long-running SSE streams from holding a DB connection for minutes.
        from packages.core.database import async_session as _ctx_session_factory
        async with _ctx_session_factory() as ctx_db:
            system_prompt, tools, initial_messages, ctx = (
                await resolve_runtime_chat_context(
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
                )
            )
        # ctx_db is now closed — no DB connection held during streaming

        # SSE event queue — tools push events here, we yield them
        event_queue: asyncio.Queue = asyncio.Queue()
        round_counter = [0]
        loop_saved = [False]  # whether the background task already saved the message
        tools_started = [False]  # a tool/skill has begun executing this turn
        last_tool_args: dict[str, dict] = {}  # name -> args for current round
        tool_results: list[dict] = []  # [{name, result}] for DB storage
        assistant_blocks = AssistantBlocksBuilder()
        text_buffer = [""]
        pending_post_tool_text = [""]
        pending_post_tool_chunks: list[str] = []
        has_emitted_text = [False]
        pending_text_reset = [False]
        reset_before_next_text = [False]
        summary_started = [False]
        sentinel_scan_buffer = [""]
        user_prefers_chinese = runtime_prefers_chinese(message)

        def with_assistant_blocks(payload: dict) -> dict:
            return {
                **payload,
                **assistant_blocks_stream_payload(assistant_blocks),
            }

        def assistant_blocks_has_process() -> bool:
            return any(block.get("type") == "process" for block in assistant_blocks.blocks())

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
            tool_call = data.get("tool_call") if isinstance(data, dict) else None
            if not isinstance(tool_call, dict):
                return
            name = str(tool_call.get("name") or "tool")
            args = tool_call.get("arguments")
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
                runtime_record_tool_end_for_chat(
                    tool_results,
                    name,
                    args=args,
                    result=tool_call.get("result"),
                    status=tool_call.get("status"),
                    duration_ms=tool_call.get("duration_ms"),
                )
                assistant_blocks.end_tool(
                    name,
                    arguments=args,
                    result=tool_call.get("result"),
                    status=tool_call.get("status"),
                    duration_ms=tool_call.get("duration_ms"),
                    now_ms=int(time.time() * 1000),
                )
            data.update(assistant_blocks_stream_payload(assistant_blocks))

        tool_stream_token = runtime_tool_stream_sink_var.set(RuntimeToolStreamSink(
            event_queue=event_queue,
            record_tool_event=record_nested_tool_event,
            format_event=scoped_sse,
            format_tool_arguments=tool_arguments_for_chat,
            format_tool_result=tool_result_for_chat,
            resolve_tool_status=tool_status_for_chat,
        ))

        def flush_text_buffer(*, phase: str | None = None) -> None:
            if not text_buffer[0]:
                return
            visible_text = runtime_coerce_visible_text_language(
                text_buffer[0],
                prefers_chinese=user_prefers_chinese,
            )
            if not visible_text:
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
                emit_visible_text_delta(token_text)
                assistant_blocks.append_text(token_text, phase="final")
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

        def replace_visible_text(content: str) -> None:
            text_buffer[0] = ""
            sentinel_scan_buffer[0] = ""
            content = runtime_coerce_visible_text_language(
                content,
                prefers_chinese=user_prefers_chinese,
            )
            if not content:
                return
            pending_text_reset[0] = False
            reset_before_next_text[0] = False
            if has_emitted_text[0]:
                event_queue.put_nowait(scoped_sse("text_reset", {}))
                assistant_blocks.reset_text()
                pending_post_tool_text[0] = ""
                pending_post_tool_chunks.clear()
            event_queue.put_nowait(scoped_sse("text_delta", {"content": content}))
            assistant_blocks.append_text(content)
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
            if text_buffer[0] and assistant_blocks_has_process() and not summary_started[0]:
                flush_text_buffer()
            if summary_started[0]:
                if final_text:
                    assistant_blocks.set_final_text(_strip_final_response_sentinel(final_text))
                return
            if has_emitted_text[0] and not assistant_blocks_has_process():
                if final_text:
                    assistant_blocks.set_final_text(_strip_final_response_sentinel(final_text))
                return
            final_visible_text = runtime_coerce_visible_text_language(
                _strip_final_response_sentinel(final_text),
                prefers_chinese=user_prefers_chinese,
            ) or pending_post_tool_text[0]
            if final_visible_text:
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
                    if has_emitted_text[0]:
                        event_queue.put_nowait(scoped_sse("text_reset", {}))
                if reset_stream_buffer[0]:
                    if not synthetic_text:
                        streamed_text_content[0] = token_text
                    reset_stream_buffer[0] = False
                else:
                    if not synthetic_text:
                        streamed_text_content[0] += token_text
                process_stream_text_for_summary_sentinel(token_text)
                if not synthetic_text:
                    await checkpoint_stream_text()
            elif event_type == "text_reset":
                reset_stream_buffer[0] = True
                text_buffer[0] = ""
                sentinel_scan_buffer[0] = ""
                pending_post_tool_text[0] = ""
                pending_post_tool_chunks.clear()
                event_queue.put_nowait(scoped_sse("text_reset", {}))
            elif event_type == "process_note":
                content = data.get("content", "")
                note = content if isinstance(content, str) else str(content or "")
                note = runtime_coerce_visible_text_language(
                    note,
                    prefers_chinese=user_prefers_chinese,
                )
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
                    emit_summary_start_once()
                    await replay_pending_post_tool_chunks(pop_pending_post_tool_chunks())

        def on_tool_end(name: str, result: str, duration_ms: float = 0, args: dict | None = None) -> None:
            nonlocal hitl_data
            if result.strip().startswith('{"__hitl__":'):
                try:
                    hitl_data = json.loads(result)
                except Exception:
                    pass

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
            trace.log_tool_exec(
                round_num=round_counter[0],
                duration_ms=duration_ms,
                tool_name=name,
                result_length=len(result),
                result_preview=preview,
                tool_args=tool_args,
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
        resolved_model = resolve_model_from_context(ctx)
        manual_skill_slugs = runtime_manual_skill_ids_from_refs(manual_skill_refs)

        async def _run_loop():
            # Suppress auto-billing — chat handles it via record_chat_llm_usage after the loop
            _billing_handle = runtime_set_suppressed_billing_context(
                entity_id=entity_id or "",
                workspace_id=ctx.workspace_id,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conversation_id if persist_messages else None,
            )
            try:
                result = await runtime_execute_chat_agent_loop(
                    runtime_envelope=ctx.runtime_envelope,
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
                    manual_skill_slugs=manual_skill_slugs,
                    legacy_tool_profile=ctx.legacy_runtime_profile,
                    allowed_tool_names=ctx.allowed_tool_names,
                    model=resolved_model,
                    initial_messages=initial_messages or None,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_llm_call=on_llm_call,
                    stream_handler=on_stream_event,
                    metadata=resolve_llm_metadata_from_context(ctx),
                    forced_tool_calls=runtime_forced_tool_calls_for_turn(ctx, manual_skill_refs, message),
                )
                loop_result[0] = result

                # Save assistant message immediately after loop completes,
                # inside the task so it persists even if the SSE client
                # disconnects before the generator resumes.
                if result:
                    streamed_fallback_content = streamed_text_content[0].strip()
                    # Some providers stream visible text but return an empty
                    # final assistant message. Persist the streamed content so
                    # refresh/reload does not make the reply disappear.
                    if not (result.content or "").strip() and streamed_fallback_content and not result.tool_calls_made:
                        result.content = streamed_fallback_content
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
                    if not result.content and result.tool_calls_made:
                        result.content = runtime_fallback_stream_final_summary(
                            result.tool_calls_made,
                            tool_results,
                        )
                        generated_fallback_content = bool(result.content)
                    if result.content:
                        result.content = _strip_final_response_sentinel(result.content)
                    sanitized_content = runtime_sanitize_assistant_content_after_loop(
                        result.content or "",
                        result.tool_calls_made,
                    )
                    force_visible_replacement = bool(
                        result.content
                        and isinstance(getattr(result, "control", None), dict)
                        and result.control.get("replace_visible_text")
                    )
                    if sanitized_content != (result.content or ""):
                        result.content = sanitized_content
                        if streamed_text[0] and sanitized_content:
                            # Only reset+re-emit when there is replacement content to show.
                            # If sanitized is empty we already streamed the best version;
                            # emitting a bare text_reset would wipe the user's visible text.
                            replace_visible_text(sanitized_content)
                    elif streamed_text[0] and force_visible_replacement:
                        replace_visible_text(result.content)
                    elif streamed_text[0] and generated_fallback_content and result.content:
                        replace_visible_text(result.content)
                    elif streamed_text[0] and pending_text_reset[0] and result.content:
                        replace_visible_text(result.content)
                    if persist_messages and conversation_id:
                        try:
                            pending_action = (
                                workspace_operation_pending_action_from_data(hitl_data)
                                if ctx.workspace_id
                                else None
                            )
                            assistant_meta = runtime_assistant_result_meta(result) or {}
                            attachments = chat_attachments_from_tool_results(tool_results)
                            flush_final_summary_text(result.content or "")
                            assistant_blocks.set_final_text(result.content or "")
                            pending_post_tool_text[0] = ""
                            assistant_meta.update(assistant_blocks.meta())
                            runtime_meta = runtime_context_meta(ctx)
                            if runtime_meta:
                                assistant_meta["runtime"] = runtime_meta
                            hitl_requests = hitl_requests_from_data(hitl_data)
                            if hitl_requests:
                                assistant_meta["hitl_requests"] = hitl_requests
                            if attachments:
                                assistant_meta["attachments"] = attachments
                            saved_id = await save_or_update_assistant_stream_message(
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
                                await runtime_persist_chat_stream_runtime_events(
                                    ctx.runtime_envelope,
                                    message_id=saved_id,
                                    trace_id=trace.trace_id,
                                )
                        except Exception as save_err:
                            logger.error("Failed to save assistant message: %s", save_err)
                    else:
                        loop_saved[0] = True
                        flush_final_summary_text(result.content or "")

                    # Persist token usage
                    elapsed_ms = int((time.time() - trace.started_at) * 1000)
                    try:
                        from packages.core.database import async_session as _sf2
                        async with _sf2() as usage_db:
                            await record_chat_llm_usage(
                                usage_db,
                                entity_id=entity_id,
                                user_id=user_id,
                                agent_id=agent_id,
                                workspace_id=ctx.workspace_id,
                                conversation_id=conversation_id if persist_messages else None,
                                usage=result.usage or {},
                                duration_ms=elapsed_ms,
                                fallback_model=resolved_model,
                            )
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
                                if persist_messages and conversation_id
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

            except Exception as exc:
                loop_error[0] = exc
            finally:
                runtime_release_billing_context(_billing_handle)
                result = loop_result[0]
                if result and (result.content or text_buffer[0] or pending_post_tool_text[0]):
                    flush_final_summary_text(result.content or "")
                else:
                    flush_text_buffer()
                event_queue.put_nowait(None)  # sentinel

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
                            attachments=chat_attachments_from_tool_results(tool_results) or None,
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

        # Normal-exit path: task is already done, this is a fast no-op.
        await task

        if loop_error[0]:
            error_message = str(loop_error[0]) or "Unknown error"
            trace.log_error(error_message, phase="agentic_loop")
            saved_error_id = (
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
            if streamed_text[0]:
                yield scoped_sse("text_reset", {})
                yield scoped_sse(
                    "text_delta",
                    {"content": runtime_assistant_stream_error_content(error_message)},
                )
            yield scoped_sse("error", {
                "message": error_message,
                "persisted": bool(saved_error_id),
                "message_id": saved_error_id,
            })

        # HITL check
        if hitl_data:
            yield scoped_sse("hitl_required", hitl_data)

        result = loop_result[0]
        if result:
            trace.total_usage = result.usage or {}

            # Stream the final text only if we didn't already stream it token-by-token
            # (fallback content is already set by _run_loop if LLM returned nothing)
            if result.content and not streamed_text[0]:
                yield scoped_sse("text_delta", {"content": result.content})

            trace.log_complete(result=result)

            stream_completed = True
            tool_call_names = [
                str(item.get("name"))
                for item in tool_results
                if isinstance(item, dict) and item.get("name")
            ]
            attachments = chat_attachments_from_tool_results(tool_results)
            end_payload = {
                "conversation_id": conversation_id,
                "message_id": persisted_message_id[0],
                "persisted": bool(loop_saved[0] and persisted_message_id[0]),
                "usage": result.usage or {},
                "rounds": result.rounds,
                "tool_calls": tool_call_names or result.tool_calls_made,
                "stop_reason": result.stop_reason,
                **assistant_blocks_stream_payload(assistant_blocks),
            }
            if attachments:
                end_payload["attachments"] = attachments
            if result.error:
                end_payload["error"] = result.error
            if getattr(result, "error_detail", None):
                end_payload["limit_detail"] = result.error_detail
            yield scoped_sse("stream_end", end_payload)
        else:
            trace.log_complete()
            stream_completed = True
            yield scoped_sse("stream_end", with_assistant_blocks({
                "conversation_id": conversation_id,
                "usage": {},
                "message_id": persisted_message_id[0],
                "persisted": bool(persisted_message_id[0]),
            }))

    except Exception as exc:
        error_message = f"Internal error: {exc}"
        trace.log_error(str(exc), phase="stream_chat_response")
        logger.error("stream_chat_response failed: %s", exc, exc_info=True)
        error_assistant_blocks_meta = {}
        try:
            error_assistant_blocks_meta = assistant_blocks.meta()
        except Exception:
            error_assistant_blocks_meta = {}
        saved_error_id = (
            await save_assistant_stream_error_message(
                conversation_id=conversation_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                error_message=error_message,
                message_id=persisted_message_id[0],
                meta=error_assistant_blocks_meta or None,
            )
            if persist_messages and conversation_id
            else None
        )
        if saved_error_id:
            persisted_message_id[0] = saved_error_id
        yield scoped_sse("error", {
            "message": error_message,
            "persisted": bool(saved_error_id),
            "message_id": saved_error_id,
        })
        yield scoped_sse("stream_end", {
            "conversation_id": conversation_id,
            "usage": {},
            "message_id": persisted_message_id[0],
            "persisted": bool(persisted_message_id[0]),
            **error_assistant_blocks_meta,
        })
    finally:
        if tool_stream_token is not None:
            try:
                runtime_tool_stream_sink_var.reset(tool_stream_token)
            except Exception:
                pass
        if persist_messages and conversation_id and not stream_completed and hitl_data:
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
            )
        )

        # Per-round LLM call logging
        _round_counter = [0]
        _last_tool_args: dict[str, dict] = {}
        _tool_results: list[dict] = []
        hitl_data: dict | None = None

        def _record_nested_tool_event(event_type: str, data: dict) -> None:
            tool_call = data.get("tool_call") if isinstance(data, dict) else None
            if not isinstance(tool_call, dict):
                return
            name = str(tool_call.get("name") or "tool")
            args = tool_call.get("arguments")
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

        _tool_stream_token = runtime_tool_stream_sink_var.set(RuntimeToolStreamSink(
            record_tool_event=_record_nested_tool_event,
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
            if result.strip().startswith('{"__hitl__":'):
                try:
                    hitl_data = json.loads(result)
                except Exception:
                    pass
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
            trace.log_tool_exec(
                round_num=_round_counter[0],
                duration_ms=duration_ms,
                tool_name=name,
                result_length=len(result),
                result_preview=preview,
                tool_args=tool_args,
            )

        # Resolve model via the shared resolver — honours the entity's
        # Primary AI picker just like the streaming path does.
        from packages.core.services.model_resolver import resolve_model_for_user
        _resolved_model = await resolve_model_for_user(
            "primary",
            user_id=user_id, entity_id=entity_id, db=db,
        )

        # Suppress auto-billing — chat handles it via record_chat_llm_usage
        _billing_handle = runtime_set_suppressed_billing_context(
            entity_id=entity_id or "",
            workspace_id=ctx.workspace_id,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        manual_skill_slugs = runtime_manual_skill_ids_from_refs(manual_skill_refs)

        result = await runtime_execute_chat_agent_loop(
            runtime_envelope=ctx.runtime_envelope,
            system_prompt=system_prompt,
            user_message=message,
            tools=tools,
            entity_id=entity_id or "",
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id,
            task_id=ctx.task_id,
            active_user_message=runtime_message_text_for_intent(message),
            manual_skill_selected=bool(manual_skill_refs),
            manual_skill_slugs=manual_skill_slugs,
            legacy_tool_profile=ctx.legacy_runtime_profile,
            allowed_tool_names=ctx.allowed_tool_names,
            model=_resolved_model,
            initial_messages=initial_messages if initial_messages else None,
            on_tool_start=_on_tool_start,
            on_tool_end=_on_tool_end,
            on_llm_call=_on_llm_call,
            metadata=resolve_llm_metadata_from_context(ctx),
            forced_tool_calls=runtime_forced_tool_calls_for_turn(ctx, manual_skill_refs, message),
        )

        trace.total_usage = result.usage or {}
        trace.log_complete(result=result)

        # Persist token usage to DB
        elapsed_ms = int((time.time() - trace.started_at) * 1000)
        await record_chat_llm_usage(
            db,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=agent_id,
            workspace_id=ctx.workspace_id,
            conversation_id=conversation_id,
            usage=result.usage or {},
            duration_ms=elapsed_ms,
            fallback_model=_resolved_model,
        )
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

    if result:
        result.content = runtime_sanitize_assistant_content_after_loop(
            result.content or "",
            result.tool_calls_made,
        )

    # Save assistant message to DB
    message_id = None
    if db and (result.content or _tool_results):
        try:
            author_subscription_id = await resolve_author_subscription_id(
                db,
                entity_id=entity_id,
                workspace_id=ctx.workspace_id,
                agent_id=agent_id,
            )
            pending_action = (
                workspace_operation_pending_action_from_data(hitl_data)
                if ctx.workspace_id
                else None
            )
            assistant_meta = runtime_assistant_result_meta(result) or {}
            attachments = chat_attachments_from_tool_results(_tool_results)
            runtime_meta = runtime_context_meta(ctx)
            if runtime_meta:
                assistant_meta["runtime"] = runtime_meta
            hitl_requests = hitl_requests_from_data(hitl_data)
            if hitl_requests:
                assistant_meta["hitl_requests"] = hitl_requests
            if attachments:
                assistant_meta["attachments"] = attachments
            assistant_blocks = AssistantBlocksBuilder()
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
                    status=item.get("status"),
                    duration_ms=item.get("duration_ms"),
                )
            assistant_blocks.set_final_text(result.content or "")
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
    attachments = chat_attachments_from_tool_results(_tool_results)
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "content": result.content,
        "tool_calls_made": tool_call_names or result.tool_calls_made,
        "usage": result.usage,
        "rounds": result.rounds,
        "stop_reason": result.stop_reason,
        "error": result.error,
        "limit_detail": getattr(result, "error_detail", None),
        "hitl_requests": hitl_requests_from_data(hitl_data),
        "attachments": attachments,
    }
