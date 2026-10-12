"""Twilio Media Streams to OpenAI Realtime call orchestration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict

from fastapi import WebSocket, WebSocketDisconnect

from packages.core.services.voice.realtime import (
    BridgeCall,
    RealtimeRoute,
    RealtimeVoiceEngine,
    VERCEL_REALTIME_TRANSCRIPTION_MODEL,
    VoiceAgentOutcome,
    build_input_audio_event,
    build_realtime_session_update,
    build_spoken_response_event,
    event_dict,
    open_realtime_connection,
)
from packages.core.services.voice.work_queue import VoiceWorkReceipt
from packages.core.services.voice.work_types import (
    VoiceWorkAction,
    VoiceWorkDecision,
)

logger = logging.getLogger(__name__)


_HOLD_AFTER_SECONDS = 10.0
_HOLD_MESSAGE_DEFAULT = "I'm working on it, give me a moment."
_MAX_QUEUED_TURNS = 8
_GENERIC_VOICE_ERROR = "Sorry, something went wrong. Try again in a moment."
_GENERIC_BUSY_REPLY = "I am still handling earlier requests. Please try again shortly."


@dataclass
class _CallState:
    stream_sid: str = ""
    call_sid: str = ""
    from_number: str = ""
    to_number: str = ""
    channel_config_id: str = ""
    entity_id: str = ""
    custom: Dict[str, str] = field(default_factory=dict)


@dataclass
class _PlaybackState:
    response_id: str
    item_id: str
    content_index: int
    started_at_twilio_ms: int
    sent_audio_ms: int = 0
    acknowledged_audio_ms: int = 0


@dataclass
class _DurableAgentWork:
    call: BridgeCall
    receipt: VoiceWorkReceipt
    task: asyncio.Task[VoiceAgentOutcome]
    suppress_delivery: bool = False


@dataclass(frozen=True)
class _QueuedDurableCall:
    call: BridgeCall
    receipt: VoiceWorkReceipt


class TwilioVoiceSession:
    """Own one authenticated Twilio stream and one OpenAI Realtime socket."""

    def __init__(
        self,
        ws: WebSocket,
        *,
        realtime_route: RealtimeRoute,
        agent_callable: Callable[..., Awaitable[VoiceAgentOutcome]],
        call_session_id: str | None,
        channel_config_id: str,
        entity_id: str,
        conversation_id: str | None = None,
        billing_user_id: str | None = None,
        billing_workspace_id: str | None = None,
        billing_agent_id: str | None = None,
        channel_binding_id: str | None = None,
        agent_subscription_id: str | None = None,
        on_connected: Callable[..., Awaitable[None]] | None = None,
        realtime_connection_factory: Callable[[RealtimeRoute], Any] | None = None,
        hold_message: str = _HOLD_MESSAGE_DEFAULT,
        admit_work: Callable[[str], Awaitable[VoiceWorkReceipt]] | None = None,
        execute_work: Callable[[VoiceWorkReceipt], Awaitable[VoiceAgentOutcome]] | None = None,
        route_followup: Callable[
            [str, VoiceWorkReceipt], Awaitable[VoiceWorkDecision]
        ] | None = None,
        cancel_work: Callable[
            [VoiceWorkReceipt, VoiceWorkReceipt | None], Awaitable[bool]
        ] | None = None,
        record_control_turn: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._ws = ws
        self._realtime_route = realtime_route
        self._agent = agent_callable
        self._admit_work = admit_work
        self._execute_work = execute_work
        self._route_followup = route_followup
        self._cancel_work = cancel_work
        self._record_control_turn = record_control_turn
        self._hold_message = hold_message
        self._state = _CallState(
            channel_config_id=channel_config_id,
            entity_id=entity_id,
        )
        self._on_connected = on_connected
        self._call_session_id = call_session_id
        self._billing_user_id = billing_user_id
        self._billing_workspace_id = billing_workspace_id
        self._billing_agent_id = billing_agent_id
        self._channel_binding_id = channel_binding_id
        self._agent_subscription_id = agent_subscription_id
        self._engine = RealtimeVoiceEngine(
            route=realtime_route,
            usage_scope={
                "entity_id": entity_id,
                "user_id": billing_user_id,
                "workspace_id": billing_workspace_id,
                "agent_id": billing_agent_id,
            },
            source="twilio_voice",
            response_operation_prefix="twilio-realtime",
            transcription_operation_prefix="twilio-transcription",
            connection_factory=(
                realtime_connection_factory or open_realtime_connection
            ),
        )
        self._bridge_calls: asyncio.Queue[BridgeCall | None] = asyncio.Queue(
            maxsize=_MAX_QUEUED_TURNS
        )
        self._detached_agent_tasks: set[asyncio.Task[Any]] = set()
        self._active_durable_work: _DurableAgentWork | None = None
        self._queued_durable_calls: deque[_QueuedDurableCall] = deque()
        self._durable_work_lock = asyncio.Lock()
        self._response_create_lock = asyncio.Lock()
        self._response_idle = asyncio.Event()
        self._response_idle.set()
        self._conversation_id = conversation_id
        self._latest_twilio_media_ms = 0
        self._twilio_media_frames = 0
        self._twilio_media_payload_chars = 0
        self._vad_started_count = 0
        self._vad_stopped_count = 0
        self._transcriptions_completed_count = 0
        self._responses_done_count = 0
        self._output_audio_frames = 0
        self._output_audio_bytes = 0
        self._playback: _PlaybackState | None = None
        self._pending_marks: dict[str, tuple[str, int]] = {}
        self._cancelled_audio_responses: set[str] = set()
        self._closed = False
        self._provider_started = False
        self.error_message: str | None = None

    @property
    def _realtime(self) -> Any | None:
        return self._engine.connection

    @_realtime.setter
    def _realtime(self, value: Any | None) -> None:
        self._engine.connection = value

    @property
    def _settled_response_ids(self) -> set[str]:
        return self._engine.billed_response_ids

    async def run(self) -> None:
        """Relay until Twilio stops or either transport fails."""
        from packages.core.ai.runtime import runtime_llm_billing_context
        from packages.core.services.voice.billing import (
            reserve_voice_call_credits,
            settle_voice_call_credits,
            voice_call_reservation_scope,
        )

        reservation_active = False
        relay_tasks: list[asyncio.Task] = []
        async with runtime_llm_billing_context(
            self._state.entity_id,
            user_id=self._billing_user_id,
            agent_id=self._billing_agent_id,
            workspace_id=self._billing_workspace_id,
            source="twilio_voice",
        ):
            try:
                if self._call_session_id and not self._realtime_route.byok:
                    await reserve_voice_call_credits(
                        source_id=self._call_session_id,
                        entity_id=self._state.entity_id,
                        workspace_id=self._billing_workspace_id,
                        user_id=self._billing_user_id,
                        agent_id=self._billing_agent_id,
                    )
                    reservation_active = True
                with voice_call_reservation_scope(
                    self._call_session_id if reservation_active else None
                ):
                    await self._preflight_realtime()
                    async with self._engine.connect():
                        await self._engine.initialize_session(
                            build_realtime_session_update(
                                model=self._realtime_route.model,
                                input_transcription_model=(
                                    VERCEL_REALTIME_TRANSCRIPTION_MODEL
                                    if self._realtime_route.provider == "vercel"
                                    else None
                                ),
                            )
                        )
                        self._provider_started = True
                        twilio_task = asyncio.create_task(
                            self._twilio_receive_loop(),
                            name="twilio-voice-receive",
                        )
                        provider_task = asyncio.create_task(
                            self._realtime_receive_loop(),
                            name="twilio-realtime-receive",
                        )
                        worker_task = asyncio.create_task(
                            self._agent_worker(),
                            name="twilio-voice-agent-fifo",
                        )
                        relay_tasks = [twilio_task, provider_task, worker_task]
                        done, _pending = await asyncio.wait(
                            relay_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in done:
                            exception = task.exception()
                            if exception is not None:
                                raise exception
                        if worker_task in done:
                            raise RuntimeError(
                                "Twilio Voice Agent worker stopped unexpectedly"
                            )
                        if provider_task in done and not self._closed:
                            raise RuntimeError(
                                "OpenAI Realtime connection closed unexpectedly"
                            )
            except WebSocketDisconnect:
                logger.info("Twilio Media Stream websocket disconnected")
            except Exception as exc:
                self.error_message = str(exc)
                logger.exception("Twilio Realtime voice session failed")
            finally:
                self._closed = True
                for task in relay_tasks:
                    if not task.done():
                        task.cancel()
                if relay_tasks:
                    await asyncio.gather(*relay_tasks, return_exceptions=True)
                if reservation_active and self._call_session_id:
                    try:
                        await settle_voice_call_credits(
                            source_id=self._call_session_id,
                            provider_started=self._provider_started,
                        )
                    except Exception as exc:
                        self.error_message = self.error_message or str(exc)
                        logger.exception(
                            "Twilio Voice credit reservation settlement failed"
                        )

    async def _preflight_realtime(self) -> None:
        await self._engine.preflight_provider(
            allow_active_voice_reservation=True,
        )

    async def _twilio_receive_loop(self) -> None:
        while not self._closed:
            raw = await self._ws.receive_text()
            message = json.loads(raw)
            await self._handle_twilio_message(message)
            if self._closed:
                return

    async def _handle_twilio_message(self, message: dict[str, Any]) -> None:
        event_type = message.get("event")
        if event_type == "start":
            await self._on_start(message.get("start") or {})
            return
        if event_type == "media":
            media = message.get("media") or {}
            try:
                timestamp_ms = max(0, int(media.get("timestamp") or 0))
            except (TypeError, ValueError, OverflowError):
                timestamp_ms = 0
            self._latest_twilio_media_ms = max(
                self._latest_twilio_media_ms,
                timestamp_ms,
            )
            payload = str(media.get("payload") or "").strip()
            if payload:
                self._twilio_media_frames += 1
                self._twilio_media_payload_chars += len(payload)
                await self._send_realtime(build_input_audio_event(payload))
            return
        if event_type == "mark":
            mark = message.get("mark")
            mark_name = str(mark.get("name") or "") if isinstance(mark, dict) else ""
            marked = self._pending_marks.pop(mark_name, None)
            if marked and self._playback and marked[0] == self._playback.response_id:
                self._playback.acknowledged_audio_ms = min(
                    self._playback.sent_audio_ms,
                    max(self._playback.acknowledged_audio_ms, marked[1]),
                )
                if (
                    self._playback.acknowledged_audio_ms
                    >= self._playback.sent_audio_ms
                ):
                    self._playback = None
            logger.debug("Twilio Voice mark received: %s", mark)
            return
        if event_type == "stop":
            logger.info(
                "Twilio stream stopped (streamSid=%s, media_frames=%s, "
                "media_payload_chars=%s, vad_started=%s, vad_stopped=%s, "
                "transcriptions_completed=%s, responses_done=%s, "
                "output_audio_frames=%s, "
                "output_audio_bytes=%s)",
                self._state.stream_sid,
                self._twilio_media_frames,
                self._twilio_media_payload_chars,
                self._vad_started_count,
                self._vad_stopped_count,
                self._transcriptions_completed_count,
                self._responses_done_count,
                self._output_audio_frames,
                self._output_audio_bytes,
            )
            self._closed = True

    async def _on_start(self, start: dict[str, Any]) -> None:
        self._state.stream_sid = str(start.get("streamSid") or "")
        self._state.call_sid = str(start.get("callSid") or "")
        custom = start.get("customParameters")
        self._state.custom = dict(custom) if isinstance(custom, dict) else {}
        self._state.from_number = str(self._state.custom.get("from") or "")
        self._state.to_number = str(self._state.custom.get("to") or "")
        if self._on_connected:
            await self._on_connected(
                stream_sid=self._state.stream_sid or None,
                call_sid=self._state.call_sid or None,
            )

    async def _realtime_receive_loop(self) -> None:
        while not self._closed:
            event = await self._engine.recv()
            await self._handle_realtime_event(event)

    async def _handle_realtime_event(self, event: Any) -> None:
        data = event_dict(event)
        event_type = data.get("type")
        if event_type == "error":
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(str(message or "OpenAI Realtime provider error"))
        if event_type == "response.output_audio.delta":
            response_id = str(data.get("response_id") or "").strip()
            if response_id in self._cancelled_audio_responses:
                return
            payload = str(data.get("delta") or "").strip()
            if payload and self._state.stream_sid:
                try:
                    audio_bytes = base64.b64decode(payload, validate=True)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("OpenAI Realtime returned invalid PCMU audio") from exc
                self._output_audio_frames += 1
                self._output_audio_bytes += len(audio_bytes)
                item_id = str(data.get("item_id") or "").strip()
                try:
                    content_index = max(0, int(data.get("content_index") or 0))
                except (TypeError, ValueError, OverflowError):
                    content_index = 0
                if (
                    self._playback is None
                    or self._playback.response_id != response_id
                    or self._playback.item_id != item_id
                ):
                    self._playback = _PlaybackState(
                        response_id=response_id,
                        item_id=item_id,
                        content_index=content_index,
                        started_at_twilio_ms=self._latest_twilio_media_ms,
                    )
                self._playback.sent_audio_ms += len(audio_bytes) // 8
                await self._send_twilio(
                    {
                        "event": "media",
                        "streamSid": self._state.stream_sid,
                        "media": {"payload": payload},
                    }
                )
            return
        if event_type == "response.output_audio.done":
            await self._mark_audio_complete(data)
            return
        if event_type == "input_audio_buffer.speech_started":
            self._vad_started_count += 1
            await self._interrupt_playback()
            return
        if event_type == "input_audio_buffer.speech_stopped":
            self._vad_stopped_count += 1
            if self._realtime_route.provider != "vercel":
                await self._create_realtime_response({"type": "response.create"})
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            self._transcriptions_completed_count += 1
            try:
                inspected = await self._engine.inspect_provider_event(
                    data,
                    conversation_id=self._conversation_id,
                )
            except Exception as exc:
                self.error_message = self.error_message or str(exc)
                raise
            call = inspected.bridge_call
            if call is not None:
                await self._queue_bridge_call(call)
            return
        if event_type != "response.done":
            return
        self._response_idle.set()
        self._responses_done_count += 1
        try:
            inspected = await self._engine.inspect_provider_event(
                data,
                conversation_id=self._conversation_id,
            )
        except Exception as exc:
            self.error_message = self.error_message or str(exc)
            raise
        if inspected.duplicate_response:
            return
        call = inspected.bridge_call
        if call is None:
            return
        await self._queue_bridge_call(call)

    async def _queue_bridge_call(self, call: BridgeCall) -> None:
        try:
            self._bridge_calls.put_nowait(call)
        except asyncio.QueueFull:
            logger.warning("Twilio Voice Agent turn queue is full")
            await self._send_voice_outcome(
                call,
                VoiceAgentOutcome(status="error", spoken_reply=_GENERIC_BUSY_REPLY),
            )

    async def _agent_worker(self) -> None:
        while True:
            call = await self._bridge_calls.get()
            try:
                if call is None:
                    return
                if self._admit_work is not None and self._execute_work is not None:
                    await self._start_durable_agent_turn(call)
                else:
                    await self._run_agent_turn(call)
            finally:
                self._bridge_calls.task_done()

    async def _start_durable_agent_turn(self, call: BridgeCall) -> None:
        async with self._durable_work_lock:
            active_work = self._active_durable_work
            decision = None
            if active_work is not None and self._route_followup is not None:
                decision = await self._route_followup(
                    call.utterance,
                    active_work.receipt,
                )
                if decision.action is not VoiceWorkAction.QUEUE:
                    await self._handle_durable_control_locked(
                        call,
                        active_work,
                        decision,
                    )
                    return

            receipt = await self._admit_work(call.utterance)
            if active_work is not None:
                self._queued_durable_calls.append(
                    _QueuedDurableCall(call=call, receipt=receipt)
                )
                await self._send_voice_outcome(
                    call,
                    VoiceAgentOutcome(
                        status="action_handled",
                        spoken_reply=(
                            decision.reply
                            if decision is not None and decision.reply
                            else "Got it. I'll handle that after the current request."
                        ),
                        conversation_id=self._conversation_id,
                        agent_id=self._billing_agent_id,
                    ),
                )
                return
            await self._launch_durable_agent_turn_locked(call, receipt)

    async def _handle_durable_control_locked(
        self,
        call: BridgeCall,
        active_work: _DurableAgentWork,
        decision: VoiceWorkDecision,
    ) -> None:
        replacement = None
        superseded_by = decision.superseded_by
        if decision.action is VoiceWorkAction.REPLACE:
            replacement = await self._admit_work(call.utterance)
        if decision.action in {VoiceWorkAction.CANCEL, VoiceWorkAction.REPLACE}:
            if self._cancel_work is None:
                raise RuntimeError("Twilio Voice cancellation is unavailable")
            interrupted = await self._cancel_work(
                active_work.receipt,
                replacement,
            )
            if interrupted:
                active_work.suppress_delivery = True
            elif self._route_followup is not None:
                decision = await self._route_followup(
                    call.utterance,
                    active_work.receipt,
                )
            if interrupted and superseded_by:
                self._queued_durable_calls = deque(
                    queued
                    for queued in self._queued_durable_calls
                    if queued.receipt.id != superseded_by
                )
            if replacement is not None:
                self._queued_durable_calls.append(
                    _QueuedDurableCall(call=call, receipt=replacement)
                )
        if self._record_control_turn is not None:
            await self._record_control_turn(
                "" if replacement is not None else call.utterance,
                decision.reply,
            )
        await self._send_voice_outcome(
            call,
            VoiceAgentOutcome(
                status="action_handled",
                spoken_reply=decision.reply,
                conversation_id=self._conversation_id,
                agent_id=self._billing_agent_id,
            ),
        )

    async def _launch_durable_agent_turn_locked(
        self,
        call: BridgeCall,
        receipt: VoiceWorkReceipt,
    ) -> None:
        work = _DurableAgentWork(
            call=call,
            receipt=receipt,
            task=asyncio.create_task(self._execute_work(receipt)),
        )
        self._active_durable_work = work
        try:
            await self._send_voice_outcome(
                call,
                VoiceAgentOutcome(
                    status="action_handled",
                    spoken_reply=self._hold_message,
                    conversation_id=self._conversation_id,
                    agent_id=self._billing_agent_id,
                ),
            )
        finally:
            completion = asyncio.create_task(
                self._complete_durable_agent_turn(work)
            )
            self._detached_agent_tasks.add(completion)
            completion.add_done_callback(self._observe_detached_agent_task)

    async def _complete_durable_agent_turn(
        self,
        work: _DurableAgentWork,
    ) -> None:
        try:
            outcome = await work.task
        except Exception:
            logger.exception("Twilio Voice durable Agent turn failed")
            outcome = VoiceAgentOutcome(
                status="error",
                spoken_reply=_GENERIC_VOICE_ERROR,
            )
        if outcome.conversation_id:
            self._conversation_id = outcome.conversation_id
        async with self._durable_work_lock:
            if self._active_durable_work is not work:
                return
            if not self._closed and not work.suppress_delivery:
                await self._send_voice_outcome(
                    BridgeCall(None, "", ""),
                    outcome,
                )
            self._active_durable_work = None
            if self._queued_durable_calls:
                queued = self._queued_durable_calls.popleft()
                await self._launch_durable_agent_turn_locked(
                    queued.call,
                    queued.receipt,
                )

    async def _run_agent_turn(self, call: BridgeCall) -> None:
        agent_task = asyncio.create_task(
            self._agent(
                entity_id=self._state.entity_id,
                channel_config_id=self._state.channel_config_id,
                channel_binding_id=self._channel_binding_id,
                agent_subscription_id=self._agent_subscription_id,
                agent_id=self._billing_agent_id,
                workspace_id=self._billing_workspace_id,
                call_sid=self._state.call_sid,
                from_number=self._state.from_number,
                text=call.utterance,
            )
        )
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(agent_task),
                timeout=_HOLD_AFTER_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Twilio Voice Agent exceeded %ss; %s",
                _HOLD_AFTER_SECONDS,
                (
                    "waiting without a hold response on Vercel Realtime"
                    if self._realtime_route.provider == "vercel"
                    else "playing hold response"
                ),
            )
            try:
                if self._realtime_route.provider != "vercel":
                    await self._create_realtime_response(
                        build_spoken_response_event(
                            spoken_reply=self._hold_message,
                            conversation="none",
                        )
                    )
                outcome = await agent_task
            except asyncio.CancelledError:
                self._detach_agent_task(agent_task)
                raise
            except Exception:
                logger.exception("Twilio Voice Agent failed after hold response")
                outcome = VoiceAgentOutcome(
                    status="error",
                    spoken_reply=_GENERIC_VOICE_ERROR,
                )
        except asyncio.CancelledError:
            self._detach_agent_task(agent_task)
            raise
        except Exception:
            logger.exception("Twilio Voice Agent turn failed")
            outcome = VoiceAgentOutcome(
                status="error",
                spoken_reply=_GENERIC_VOICE_ERROR,
            )
        if not isinstance(outcome, VoiceAgentOutcome):
            logger.error("Twilio Voice Agent returned an invalid outcome")
            outcome = VoiceAgentOutcome(
                status="error",
                spoken_reply=_GENERIC_VOICE_ERROR,
            )
        if outcome.conversation_id:
            self._conversation_id = outcome.conversation_id
        await self._send_voice_outcome(call, outcome)

    def _detach_agent_task(
        self,
        task: asyncio.Task[VoiceAgentOutcome],
    ) -> None:
        self._detached_agent_tasks.add(task)
        task.add_done_callback(self._observe_detached_agent_task)

    def _observe_detached_agent_task(
        self,
        task: asyncio.Task[Any],
    ) -> None:
        self._detached_agent_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Detached Twilio Voice Agent turn was cancelled")
        except Exception:
            logger.exception("Detached Twilio Voice Agent turn failed")

    async def _send_voice_outcome(
        self,
        call: BridgeCall,
        outcome: VoiceAgentOutcome,
    ) -> None:
        if self._closed:
            return
        await self._engine.send_function_outcome(call, outcome)
        if outcome.spoken_reply:
            await self._create_realtime_response(
                build_spoken_response_event(spoken_reply=outcome.spoken_reply)
            )

    async def _create_realtime_response(self, event: dict[str, Any]) -> None:
        async with self._response_create_lock:
            await self._response_idle.wait()
            self._response_idle.clear()
            try:
                await self._engine.create_response(event)
            except BaseException:
                self._response_idle.set()
                raise

    async def _send_realtime(self, event: dict[str, Any]) -> None:
        await self._engine.send(event)

    async def _send_twilio(self, event: dict[str, Any]) -> None:
        await self._ws.send_text(json.dumps(event))

    async def _mark_audio_complete(self, event: dict[str, Any]) -> None:
        if self._playback is None or not self._state.stream_sid:
            return
        response_id = str(event.get("response_id") or "").strip()
        item_id = str(event.get("item_id") or "").strip()
        if (
            response_id != self._playback.response_id
            or item_id != self._playback.item_id
        ):
            return
        mark_name = f"end-of-utterance-{uuid.uuid4().hex[:12]}"
        self._pending_marks[mark_name] = (
            self._playback.response_id,
            self._playback.sent_audio_ms,
        )
        await self._send_twilio(
            {
                "event": "mark",
                "streamSid": self._state.stream_sid,
                "mark": {"name": mark_name},
            }
        )

    async def _interrupt_playback(self) -> None:
        playback = self._playback
        if playback is None or not playback.item_id:
            return
        elapsed_ms = max(
            0,
            self._latest_twilio_media_ms - playback.started_at_twilio_ms,
        )
        played_ms = min(
            playback.sent_audio_ms,
            max(playback.acknowledged_audio_ms, elapsed_ms),
        )
        self._cancelled_audio_responses.add(playback.response_id)
        await self.clear()
        await self._send_realtime(
            {
                "type": "conversation.item.truncate",
                "item_id": playback.item_id,
                "content_index": playback.content_index,
                "audio_end_ms": played_ms,
            }
        )
        self._pending_marks = {
            name: marked
            for name, marked in self._pending_marks.items()
            if marked[0] != playback.response_id
        }
        self._playback = None

    async def clear(self) -> None:
        if not self._state.stream_sid:
            return
        await self._send_twilio(
            {
                "event": "clear",
                "streamSid": self._state.stream_sid,
            }
        )
