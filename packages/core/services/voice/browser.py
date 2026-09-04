"""Duplex browser audio; Manor remains the authority for every spoken turn."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from packages.core.services.voice.latency import (
    VoiceTurnTiming,
    log_voice_latency,
    voice_latency_async_context,
    voice_latency_outcome,
    voice_latency_span,
)
from packages.core.services.voice.profiles import (
    DEFAULT_VOICE_PROFILE,
    VoiceProfile,
    normalize_voice_profile,
    openai_voice,
)
from packages.core.services.voice.realtime import (
    BridgeCall,
    RealtimeRoute,
    RealtimeVoiceEngine,
    VERCEL_REALTIME_TRANSCRIPTION_MODEL,
    VoiceAgentOutcome,
    build_realtime_session_update,
    build_spoken_response_event,
    extract_input_transcription_usage,
    extract_realtime_usage,
    open_realtime_connection,
)

SAMPLE_RATE = 24000
MAX_CALL_SECONDS = 1800
RESERVATION_KIND = "browser_voice_call"
# An accepted transcript may outlive the microphone connection, but a stuck
# Chat/tool request must not retain the call lease forever.
TURN_SETTLEMENT_TIMEOUT_SECONDS = 12 * 60
PROVIDER_SETTLEMENT_TIMEOUT_SECONDS = 3
# Keep the phone conversation responsive while the bound Chat Agent performs
# tools or other long-running work. Fast conversational turns still complete
# normally without an acknowledgement in front of them.
BACKGROUND_ACK_AFTER_SECONDS = 0.8
RECENT_WORK_STATUS_SECONDS = 30

logger = logging.getLogger(__name__)


@dataclass
class BrowserAgentWork:
    generation: int
    call: BridgeCall
    task: asyncio.Task[VoiceAgentOutcome]
    started_at: float
    acknowledged: bool = False


@dataclass(frozen=True)
class QueuedBrowserAgentCall:
    generation: int
    call: BridgeCall


@dataclass(frozen=True)
class BrowserSpeechRequest:
    request_id: str
    spoken_reply: str
    retry_silent_audio: bool
    retry_count: int = 0


_SPEECH_REQUEST_KEY = "_manor_speech_request"


def _voice_reply_language(text: str) -> str:
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return "zh"
    if any("\uac00" <= char <= "\ud7af" for char in text):
        return "ko"
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "ja"
    return "en"


def _voice_work_reply(text: str, kind: str) -> str:
    replies = {
        "zh": {
            "ack": "好的，我正在处理。你可以随时问我进度。",
            "progress": "还在处理中，完成后我会马上告诉你。",
            "busy": "我还在处理上一项工作，完成后就可以继续。",
            "queued": "好的，已排队。上一项完成后我会接着处理。",
            "completed": "刚刚已经完成，结果也发到对话里了。",
            "error": "处理时遇到了问题，请稍后再试。",
        },
        "ko": {
            "ack": "알겠습니다. 지금 처리하고 있어요. 언제든 진행 상황을 물어보세요.",
            "progress": "아직 처리 중이에요. 끝나는 대로 바로 알려드릴게요.",
            "busy": "이전 작업을 처리 중이에요. 끝나면 바로 이어갈게요.",
            "queued": "알겠습니다. 대기열에 추가했어요. 이전 작업이 끝나면 이어서 처리할게요.",
            "completed": "방금 완료했고 결과도 대화에 남겼어요.",
            "error": "처리 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
        },
        "ja": {
            "ack": "はい、今処理しています。いつでも進捗を聞いてください。",
            "progress": "まだ処理中です。終わり次第すぐにお知らせします。",
            "busy": "前の作業を処理中です。終わったら続けられます。",
            "queued": "承知しました。前の作業が終わり次第、続けて処理します。",
            "completed": "先ほど完了し、結果もチャットに保存しました。",
            "error": "処理中に問題が発生しました。少し後でもう一度お試しください。",
        },
        "en": {
            "ack": "Okay, I'm working on it. You can ask me for an update anytime.",
            "progress": "I'm still working on it. I'll tell you as soon as it's ready.",
            "busy": "I'm still handling the previous request. We can continue when it's done.",
            "queued": "Okay, I queued that. I'll handle it after the current request finishes.",
            "completed": "It just finished, and I added the result to the chat.",
            "error": "I ran into a problem while working on that. Please try again shortly.",
        },
    }
    return replies[_voice_reply_language(text)][kind]


def is_voice_progress_query(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("'", "").split())
    return any(
        phrase in normalized
        for phrase in (
            "进度",
            "怎么样了",
            "好了没",
            "好了吗",
            "完成了吗",
            "查到了吗",
            "到哪了",
            "结果呢",
            "有消息吗",
            "status",
            "progress",
            "any update",
            "how is it going",
            "hows it going",
            "are you done",
            "is it done",
            "finished yet",
            "still working",
            "진행 상황",
            "어떻게 됐",
            "끝났",
            "進捗",
            "終わった",
        )
    )


def browser_session_update(
    model: str,
    voice: VoiceProfile = DEFAULT_VOICE_PROFILE,
) -> dict:
    event = build_realtime_session_update(
        model=model,
        voice=openai_voice(voice),
        input_transcription_model=VERCEL_REALTIME_TRANSCRIPTION_MODEL,
    )
    session = event["session"]
    session["audio"]["input"]["format"] = {"type": "audio/pcm", "rate": SAMPLE_RATE}
    session["audio"]["output"]["format"] = {"type": "audio/pcm", "rate": SAMPLE_RATE}
    # The server gates each response on current authorization and credit. VAD
    # commits audio automatically, while dedicated transcription avoids a full
    # Realtime model turn before the authoritative Manor Agent can start.
    session["audio"]["input"]["turn_detection"].update(
        create_response=False,
        # Prefer a nearby speaker over a television or music in the room.
        # A shorter tail makes the assistant start sooner once speech ends.
        threshold=0.72,
        silence_duration_ms=420,
        prefix_padding_ms=240,
    )
    session["instructions"] = (
        "You are Manor's speech renderer. Speak only the exact assistant text supplied "
        "by the server. Never answer the user or perform actions yourself."
    )
    return event


def _has_explicit_zero_usage(raw_usage: Any, *, usage_type: str | None = None) -> bool:
    if not isinstance(raw_usage, dict):
        return False
    if usage_type is not None and raw_usage.get("type") != usage_type:
        return False
    return all(
        isinstance(raw_usage.get(key), int)
        and not isinstance(raw_usage[key], bool)
        and raw_usage[key] == 0
        for key in ("input_tokens", "output_tokens")
    )


class BrowserVoiceSession:
    def __init__(
        self,
        ws: WebSocket,
        *,
        route: RealtimeRoute,
        entity_id: str,
        user_id: str,
        conversation_id: str,
        workspace_id: str | None,
        agent_id: str | None,
        check_access: Callable[[], Awaitable[None]],
        agent: Callable[[str], Awaitable[VoiceAgentOutcome]],
        record_control_turn: Callable[[str, str], Awaitable[None]] | None = None,
        connection_factory: Callable = open_realtime_connection,
        on_ready: Callable[[], None] | None = None,
        voice: VoiceProfile = DEFAULT_VOICE_PROFILE,
    ):
        self.ws = ws
        self.call_id = uuid.uuid4().hex[:12]
        self.usage_scope = dict(
            entity_id=entity_id,
            user_id=user_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        self.check_access, self.agent = check_access, agent
        self.record_control_turn = record_control_turn
        self.engine = RealtimeVoiceEngine(
            route=route,
            usage_scope=self.usage_scope,
            source="chat_voice",
            response_operation_prefix="browser-realtime",
            transcription_operation_prefix="browser-transcription",
            check_access=check_access,
            connection_factory=connection_factory,
        )
        self.on_ready = on_ready
        self.voice = normalize_voice_profile(voice)
        self.voice_locked = False
        self.generation = 0
        self.response_generation = 0
        self.response_has_audio = False
        self.response_audio_bytes = 0
        self.active_speech_request: BrowserSpeechRequest | None = None
        self.response_idle = asyncio.Event()
        self.response_idle.set()
        self.requests: asyncio.Queue = asyncio.Queue(maxsize=8)
        self.turns: asyncio.Queue = asyncio.Queue(maxsize=8)
        self.transcription_generations: dict[str, int] = {}
        self.pending_transcriptions: set[str] = set()
        self.transcription_idle = asyncio.Event()
        self.transcription_idle.set()
        self.turn_timings: dict[int, VoiceTurnTiming] = {}
        self.run_started_at: float | None = None
        self.session_update_started_at: float | None = None
        self.playback: dict | None = None
        self.turn_idle = asyncio.Event()
        self.turn_idle.set()
        self.input_idle = asyncio.Event()
        self.input_idle.set()
        self.active_agent_work: BrowserAgentWork | None = None
        self.queued_agent_calls: deque[QueuedBrowserAgentCall] = deque()
        self.foreground_delivery_lock = asyncio.Lock()
        self.agent_completion_tasks: set[asyncio.Task] = set()
        self.last_work_completed_at: float | None = None
        self.last_work_status: str | None = None
        self.ready = False
        self.closing = False
        self.billing_failures: set[str] = set()
        self.muted = False
        self.started = time.monotonic()
        self.input_bytes = 0
        self.pending_client_messages: deque[dict[str, Any]] = deque()

    @property
    def route(self) -> RealtimeRoute:
        return self.engine.route

    @route.setter
    def route(self, value: RealtimeRoute) -> None:
        self.engine.route = value

    @property
    def connection(self) -> Any | None:
        return self.engine.connection

    @connection.setter
    def connection(self, value: Any | None) -> None:
        self.engine.connection = value

    @property
    def connection_factory(self) -> Callable:
        return self.engine.connection_factory

    @connection_factory.setter
    def connection_factory(self, value: Callable) -> None:
        self.engine.connection_factory = value

    @property
    def billing_failed(self) -> bool:
        return bool(self.billing_failures)

    def log_latency(
        self,
        stage: str,
        started_at: float,
        *,
        generation: int | str | None = None,
        outcome: str = "ok",
        segment: int = 0,
        ended_at: float | None = None,
    ) -> int:
        return log_voice_latency(
            logger,
            transport="native_realtime",
            provider=self.route.provider,
            model=self.route.model,
            call_id=self.call_id,
            turn=generation,
            stage=stage,
            started_at=started_at,
            outcome=outcome,
            segment=segment,
            ended_at=ended_at,
        )

    def finish_turn_timing(self, generation: int, *, outcome: str, ended_at: float | None = None) -> None:
        timing = self.turn_timings.pop(generation, None)
        if timing and timing.input_committed_at is not None:
            self.log_latency(
                "turn_complete",
                timing.input_committed_at,
                generation=timing.turn_id if timing.turn_id is not None else generation,
                outcome=outcome,
                ended_at=ended_at,
            )

    def finish_uncommitted_capture(
        self,
        generation: int,
        *,
        outcome: str,
        ended_at: float | None = None,
    ) -> None:
        timing = self.turn_timings.get(generation)
        if timing is None or timing.input_committed_at is not None:
            return
        self.turn_timings.pop(generation, None)
        if timing.speech_started_at is not None:
            self.log_latency(
                "speech_capture",
                timing.speech_started_at,
                generation=timing.turn_id if timing.turn_id is not None else generation,
                outcome=outcome,
                ended_at=ended_at,
            )

    async def preflight(self):
        await self.engine.preflight_provider(
            allow_active_voice_reservation=True,
        )

    async def run(self):
        from packages.core.ai.runtime import runtime_llm_billing_context
        from packages.core.services.voice.billing import (
            reserve_voice_call_credits,
            settle_voice_call_credits,
            voice_call_reservation_scope,
        )

        reservation_id = str(uuid.uuid4())
        reserved = provider_started = False
        billing = {k: v for k, v in self.usage_scope.items() if k != "conversation_id"}
        self.run_started_at = time.monotonic()
        async with runtime_llm_billing_context(**billing, source="chat_voice"):
            try:
                with voice_latency_span(self.log_latency, "access_preflight"):
                    await self.preflight()
                if not self.route.byok:
                    with voice_latency_span(self.log_latency, "credit_reservation"):
                        await reserve_voice_call_credits(
                            source_id=reservation_id,
                            source_kind=RESERVATION_KIND,
                            **billing,
                        )
                    reserved = True
                with voice_call_reservation_scope(
                    reservation_id if reserved else None
                ):
                    async with asyncio.timeout(MAX_CALL_SECONDS):
                        async with voice_latency_async_context(
                            self.engine.connect(),
                            self.log_latency,
                            "provider_connect",
                        ):
                            self.session_update_started_at = time.monotonic()
                            try:
                                ready_event = await self.wait_for_ready_or_hangup(
                                    browser_session_update(self.route.model, self.voice),
                                )
                            except BaseException as error:
                                outcome = voice_latency_outcome(error)
                                self.log_latency(
                                    "session_update",
                                    self.session_update_started_at,
                                    outcome=outcome,
                                )
                                self.session_update_started_at = None
                                if self.run_started_at is not None:
                                    self.log_latency(
                                        "call_ready",
                                        self.run_started_at,
                                        outcome=outcome,
                                    )
                                raise
                            if ready_event is None:
                                self.log_latency(
                                    "session_update",
                                    self.session_update_started_at,
                                    outcome="closed",
                                )
                                self.session_update_started_at = None
                                if self.run_started_at is not None:
                                    self.log_latency(
                                        "call_ready",
                                        self.run_started_at,
                                        outcome="closed",
                                    )
                                return
                            provider_started = True
                            await self.handle_provider(ready_event)
                            tasks = [
                                asyncio.create_task(fn())
                                for fn in (
                                    self.receive_client,
                                    self.receive_provider,
                                    self.generate_responses,
                                    self.run_turns,
                                    self.monitor_access,
                                )
                            ]
                            try:
                                done, _ = await asyncio.wait(
                                    tasks,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                for task in done:
                                    task.result()
                            finally:
                                self.closing = True
                                # A background Agent result may be waiting for
                                # the current microphone turn to finish. Hangup
                                # is also a terminal input boundary, so release
                                # that waiter before draining admitted work.
                                self.input_idle.set()
                                cleanup = asyncio.create_task(self.finish(tasks))
                                try:
                                    await asyncio.shield(cleanup)
                                except asyncio.CancelledError:
                                    # Preserve a Chat request whose transcript was
                                    # already shown even if the outer lease closes.
                                    await asyncio.shield(cleanup)
                                    raise
            finally:
                # Failed authoritative usage must keep its reservation active.
                # Consumed reservations do not reduce the available balance,
                # while active ones remain a durable hold for reconciliation.
                if reserved and not self.billing_failed:
                    await settle_voice_call_credits(
                        source_id=reservation_id,
                        source_kind=RESERVATION_KIND,
                        provider_started=provider_started,
                    )

    async def finish(self, tasks: list[asyncio.Task]) -> None:
        """Stop transport work and drain a transcript already admitted to Chat."""

        receive_client, receive_provider, generate_responses, run_turns, monitor_access = tasks
        for task in (receive_client, generate_responses, monitor_access):
            task.cancel()

        # Obtain authoritative usage for provider work cancelled or completed
        # around hangup before closing the Realtime connection.
        settlement_events: list[asyncio.Event] = []
        if not self.response_idle.is_set():
            if not receive_provider.done():
                try:
                    await self.engine.send({"type": "response.cancel"})
                except Exception:
                    pass
            settlement_events.append(self.response_idle)
        if not self.transcription_idle.is_set():
            settlement_events.append(self.transcription_idle)
        provider_settlement_timed_out = False
        if settlement_events and not receive_provider.done():
            try:
                with voice_latency_span(
                    self.log_latency,
                    "provider_settlement_wait",
                ):
                    await asyncio.wait_for(
                        asyncio.gather(*(event.wait() for event in settlement_events)),
                        timeout=PROVIDER_SETTLEMENT_TIMEOUT_SECONDS,
                    )
            except TimeoutError:
                provider_settlement_timed_out = True
        if (
            any(not event.is_set() for event in settlement_events)
            and not self.route.byok
        ):
            # The provider may have charged active work even though its
            # authoritative completion never reached us. Keep the reservation
            # active instead of silently clearing the hold.
            if not self.response_idle.is_set():
                self.billing_failures.add("response:incomplete")
                logger.warning("Browser voice usage settlement incomplete; preserving reservation")
            if not self.transcription_idle.is_set():
                self.billing_failures.add("transcription:incomplete")
                logger.warning(
                    "Browser voice transcription settlement incomplete; preserving reservation"
                )
        receive_provider.cancel()
        transport_results = await asyncio.gather(
            receive_client,
            receive_provider,
            generate_responses,
            monitor_access,
            return_exceptions=True,
        )

        # No transcript was exposed while the worker was idle, so it can stop
        # immediately. Otherwise wait only for the admitted Chat call; the
        # worker itself is intentionally infinite and may already be waiting
        # for another queue item when the current turn marks itself idle.
        timed_out = False
        if not self.turn_idle.is_set():
            try:
                async with asyncio.timeout(TURN_SETTLEMENT_TIMEOUT_SECONDS):
                    await self.turn_idle.wait()
            except TimeoutError:
                timed_out = True
                logger.error("Browser voice turn settlement exceeded its deadline")
        if not run_turns.done():
            run_turns.cancel()
        result = (await asyncio.gather(run_turns, return_exceptions=True))[0]
        if self.agent_completion_tasks:
            await asyncio.gather(
                *tuple(self.agent_completion_tasks),
                return_exceptions=True,
            )
        if (
            not timed_out
            and isinstance(result, BaseException)
            and not isinstance(result, asyncio.CancelledError)
        ):
            logger.error(
                "Accepted browser voice turn failed during hangup settlement",
                exc_info=(type(result), result, result.__traceback__),
            )
        final_outcome = (
            "timeout"
            if timed_out or provider_settlement_timed_out
            else "closed"
        )
        ended_at = time.monotonic()
        for generation in list(self.turn_timings):
            self.finish_uncommitted_capture(
                generation,
                outcome=final_outcome,
                ended_at=ended_at,
            )
            self.finish_turn_timing(
                generation,
                outcome=final_outcome,
                ended_at=ended_at,
            )
        provider_result = transport_results[1]
        if isinstance(provider_result, BaseException) and not isinstance(
            provider_result,
            asyncio.CancelledError,
        ):
            raise provider_result

    async def wait_for_ready_or_hangup(
        self,
        session_update: dict[str, Any],
    ) -> dict[str, Any] | None:
        initialize = asyncio.create_task(
            self.engine.initialize_session(session_update)
        )
        early_client = asyncio.create_task(self.receive_client_before_ready())
        try:
            done, _ = await asyncio.wait(
                (initialize, early_client),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if early_client in done:
                early_client.result()
                return None
            return initialize.result()
        finally:
            for task in (initialize, early_client):
                if not task.done():
                    task.cancel()
            await asyncio.gather(initialize, early_client, return_exceptions=True)

    async def receive_client_before_ready(self) -> None:
        while True:
            message = await self._receive_client_message()
            if message.get("type") == "end":
                return
            if message.get("type") in {"pong", "clip_done"}:
                continue
            if len(self.pending_client_messages) >= 8:
                raise ValueError("Too many voice frames before provider ready")
            self.pending_client_messages.append(message)

    async def _receive_client_message(self) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.receive_text(), timeout=35)
        if len(raw) > 16000:
            raise ValueError("Voice frame too large")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("Invalid voice frame")
        return message

    async def monitor_access(self):
        while True:
            await asyncio.sleep(20)
            await self.preflight()
            if not self.ready:
                raise TimeoutError("Voice provider did not become ready")
            await self.ws.send_json({"type": "ping"})

    async def receive_client(self):
        while True:
            message = (
                self.pending_client_messages.popleft()
                if self.pending_client_messages
                else await self._receive_client_message()
            )
            kind = message.get("type")
            if kind == "end":
                return
            if kind in {"pong", "clip_done"}:
                continue
            if kind == "voice":
                requested_voice = normalize_voice_profile(message.get("voice"))
                # Realtime providers reject session voice changes after they
                # start producing a response, sometimes before the first audio
                # delta reaches us. Only update an idle session to avoid that
                # race; the acknowledgement restores the browser selection.
                if (
                    not self.voice_locked
                    and self.response_idle.is_set()
                    and requested_voice != self.voice
                ):
                    self.voice = requested_voice
                    await self.engine.send(
                        browser_session_update(self.route.model, self.voice)
                    )
                await self.ws.send_json(
                    {"type": "voice", "voice": self.voice, "locked": self.voice_locked}
                )
                continue
            if kind == "mute" and isinstance(message.get("muted"), bool):
                self.muted = message["muted"]
                await self.engine.send({"type": "input_audio_buffer.clear"})
                if self.muted:
                    self.finish_uncommitted_capture(
                        self.generation,
                        outcome="cleared",
                    )
                continue
            if kind == "played":
                playback = self.playback
                if not playback or message.get("item_id") != playback["item_id"]:
                    continue
                # The browser may acknowledge only audio this server delivered.
                milliseconds = message.get("audio_end_ms")
                if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
                    raise ValueError("Invalid playback acknowledgement")
                if playback.get("interrupted"):
                    await self.engine.send(
                        {
                            "type": "conversation.item.truncate",
                            "item_id": playback["item_id"],
                            "content_index": playback["content_index"],
                            "audio_end_ms": max(0, min(milliseconds, playback["bytes"] // 48)),
                        }
                    )
                    self.playback = None
                continue
            if kind != "audio" or not isinstance(message.get("audio"), str):
                raise ValueError("Unsupported voice message")
            if not self.ready or self.muted:
                continue
            audio = base64.b64decode(message["audio"], validate=True)
            if not audio or len(audio) % 2 or len(audio) > 9600:
                raise ValueError("Invalid PCM audio frame")
            self.input_bytes += len(audio)
            if self.input_bytes > (time.monotonic() - self.started + 3) * SAMPLE_RATE * 2:
                raise ValueError("Audio arrived faster than realtime")
            await self.engine.send(
                {"type": "input_audio_buffer.append", "audio": message["audio"]}
            )

    async def generate_responses(self):
        while True:
            generation, queued_event = await self.requests.get()
            event = dict(queued_event)
            speech_request = event.pop(_SPEECH_REQUEST_KEY, None)
            await asyncio.wait_for(self.response_idle.wait(), timeout=90)
            if generation != self.generation:
                self.finish_turn_timing(generation, outcome="interrupted")
                continue
            access_started_at = time.monotonic()
            access_logged = False

            def response_started() -> bool | None:
                nonlocal access_logged
                now = time.monotonic()
                self.log_latency(
                    "tts_access_check",
                    access_started_at,
                    generation=generation,
                    ended_at=now,
                )
                access_logged = True
                if generation != self.generation:
                    self.finish_turn_timing(generation, outcome="interrupted", ended_at=now)
                    return False
                self.response_generation = generation
                self.active_speech_request = (
                    speech_request
                    if isinstance(speech_request, BrowserSpeechRequest)
                    else None
                )
                self.response_has_audio = (
                    event.get("response", {}).get("output_modalities") == ["audio"]
                )
                self.response_audio_bytes = 0
                timing = self.turn_timings.setdefault(
                    generation,
                    VoiceTurnTiming(turn_id=generation),
                )
                if timing.tts_queued_at is not None:
                    self.log_latency(
                        "tts_queue",
                        timing.tts_queued_at,
                        generation=generation,
                        segment=timing.tts_attempt + 1,
                        ended_at=now,
                    )
                timing.tts_attempt += 1
                timing.tts_started_at = now
                timing.first_audio_at = None
                self.response_idle.clear()

            try:
                await self.engine.create_response(event, before_send=response_started)
            except BaseException as error:
                if not access_logged:
                    self.log_latency(
                        "tts_access_check",
                        access_started_at,
                        generation=generation,
                        outcome=voice_latency_outcome(error),
                    )
                self.finish_turn_timing(
                    generation,
                    outcome=voice_latency_outcome(error),
                )
                raise

    def queue_latest_response(self, item: tuple[int, dict]) -> None:
        """Bound provider work without letting an obsolete response end the call."""

        generation, _ = item
        self.turn_timings.setdefault(
            generation,
            VoiceTurnTiming(turn_id=generation),
        ).tts_queued_at = time.monotonic()
        if self.requests.full():
            try:
                dropped_generation, _ = self.requests.get_nowait()
                self.requests.task_done()
                self.finish_turn_timing(dropped_generation, outcome="dropped")
            except asyncio.QueueEmpty:
                pass
        self.requests.put_nowait(item)

    def _queue_spoken_response(
        self,
        generation: int,
        spoken_reply: str,
        *,
        retry_silent_audio: bool,
        request_id: str | None = None,
        retry_count: int = 0,
    ) -> None:
        event = build_spoken_response_event(spoken_reply=spoken_reply)
        event[_SPEECH_REQUEST_KEY] = BrowserSpeechRequest(
            request_id=request_id or uuid.uuid4().hex,
            spoken_reply=spoken_reply,
            retry_silent_audio=retry_silent_audio,
            retry_count=retry_count,
        )
        self.queue_latest_response((generation, event))

    async def discard_bridge_call(self, call: BridgeCall) -> None:
        await self.engine.discard_bridge_call(call)

    async def queue_bridge_call(self, generation: int, call: BridgeCall) -> None:
        """Admit the newest turn and close any queued calls it supersedes."""

        discarded: list[BridgeCall] = []
        if self.turns.full():
            retained: list[tuple[int, BridgeCall]] = []
            while True:
                try:
                    queued_generation, queued_call = self.turns.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self.turns.task_done()
                if queued_generation == generation:
                    retained.append((queued_generation, queued_call))
                else:
                    discarded.append(queued_call)

            # Reserve one slot for the newest call. Duplicate provider calls in
            # the same generation are still bounded by evicting the oldest one.
            keep = max(0, self.turns.maxsize - 1)
            if keep == 0:
                discarded.extend(item[1] for item in retained)
                retained = []
            elif len(retained) > keep:
                discarded.extend(item[1] for item in retained[:-keep])
                retained = retained[-keep:]
            for item in retained:
                self.turns.put_nowait(item)

        self.turns.put_nowait((generation, call))
        for stale_call in discarded:
            await self.discard_bridge_call(stale_call)

    async def receive_provider(self):
        while True:
            await self.handle_provider(await self.engine.recv())

    async def handle_provider(self, event: dict):
        kind = event.get("type")
        if kind == "conversation.item.input_audio_transcription.completed":
            event_received_at = time.monotonic()
            item_id = str(event.get("item_id") or "").strip()
            generation = self.transcription_generations.get(item_id)
            if generation is None:
                await self.settle_input_transcription(event)
            else:
                with voice_latency_span(
                    self.log_latency,
                    "transcription_usage_settlement",
                    generation=generation,
                ):
                    await self.settle_input_transcription(event)
            inspected = await self.engine.inspect_provider_event(event)
            self.transcription_generations.pop(item_id, None)
            self.pending_transcriptions.discard(item_id)
            if not self.pending_transcriptions:
                self.transcription_idle.set()
            now = event_received_at
            timing = self.turn_timings.get(generation) if generation is not None else None
            if timing and timing.input_committed_at is not None:
                self.log_latency(
                    "transcription",
                    timing.input_committed_at,
                    generation=generation,
                    ended_at=now,
                )
                timing.transcription_completed_at = now
            if self.closing:
                return
            call = inspected.bridge_call
            if call is None or generation is None or generation != self.generation:
                if generation is not None:
                    self.finish_turn_timing(generation, outcome="interrupted", ended_at=now)
                    await self.ws.send_json(
                        {"type": "listening", "generation": generation}
                    )
                return
            await self.ws.send_json({"type": "thinking", "generation": generation})
            await self.queue_bridge_call(generation, call)
            return
        if self.closing and kind != "response.done":
            return
        if kind == "error":
            # Cancellation can race with a response that has just completed.
            if (event.get("error") or {}).get("code") == "response_cancel_not_active":
                return
            if not self.ready and self.session_update_started_at is not None:
                self.log_latency(
                    "session_update",
                    self.session_update_started_at,
                    outcome="error",
                )
                self.session_update_started_at = None
            raise RuntimeError("Voice provider rejected the session")
        if kind == "session.updated" and not self.ready:
            now = time.monotonic()
            if self.session_update_started_at is not None:
                self.log_latency("session_update", self.session_update_started_at, ended_at=now)
                self.session_update_started_at = None
            try:
                await self.ws.send_json(
                    {
                        "type": "ready",
                        "conversation_id": self.usage_scope["conversation_id"],
                        "transport_mode": "native_realtime",
                        "voice": self.voice,
                        "voice_locked": self.voice_locked,
                        "generation": self.generation,
                    }
                )
            except BaseException as error:
                if self.run_started_at is not None:
                    self.log_latency(
                        "call_ready",
                        self.run_started_at,
                        outcome=voice_latency_outcome(error),
                    )
                raise
            if self.run_started_at is not None:
                self.log_latency("call_ready", self.run_started_at)
            self.ready = True
            if self.on_ready:
                self.on_ready()
        elif kind == "input_audio_buffer.speech_started":
            self.input_idle.clear()
            self.finish_uncommitted_capture(
                self.generation,
                outcome="superseded",
            )
            self.generation += 1
            self.turn_timings[self.generation] = VoiceTurnTiming(
                turn_id=self.generation,
                speech_started_at=time.monotonic()
            )
            if self.playback:
                self.playback["interrupted"] = True
            await self.ws.send_json(
                {
                    "type": "interrupt",
                    "item_id": (self.playback or {}).get("item_id"),
                    "generation": self.generation,
                }
            )
        elif kind == "input_audio_buffer.committed":
            self.input_idle.set()
            if self.muted:
                return
            item_id = str(event.get("item_id") or "").strip()
            if not item_id:
                raise RuntimeError("Voice provider committed audio without an item identity")
            now = time.monotonic()
            timing = self.turn_timings.setdefault(
                self.generation,
                VoiceTurnTiming(turn_id=self.generation),
            )
            timing.input_committed_at = now
            if timing.speech_started_at is not None:
                self.log_latency(
                    "speech_capture",
                    timing.speech_started_at,
                    generation=self.generation,
                    ended_at=now,
                )
            self.transcription_generations[item_id] = self.generation
            self.pending_transcriptions.add(item_id)
            self.transcription_idle.clear()
            await self.ws.send_json(
                {"type": "transcribing", "generation": self.generation}
            )
        elif kind == "response.output_audio.delta" and self.response_generation == self.generation:
            self.voice_locked = True
            payload = event.get("delta") or ""
            audio = base64.b64decode(payload, validate=True)
            if self.response_audio_bytes == 0:
                now = time.monotonic()
                timing = self.turn_timings.get(self.response_generation)
                if timing:
                    timing.first_audio_at = now
                    if timing.tts_started_at is not None:
                        self.log_latency(
                            "tts_first_audio",
                            timing.tts_started_at,
                            generation=self.response_generation,
                            segment=timing.tts_attempt,
                            ended_at=now,
                        )
                    if timing.input_committed_at is not None:
                        self.log_latency(
                            "turn_first_audio",
                            timing.input_committed_at,
                            generation=self.response_generation,
                            ended_at=now,
                        )
            self.response_audio_bytes += len(audio)
            item_id = event["item_id"]
            if not self.playback or self.playback["item_id"] != item_id:
                self.playback = dict(item_id=item_id, content_index=event.get("content_index", 0), bytes=0)
            self.playback["bytes"] += len(audio)
            await self.ws.send_json({"type": "audio", "audio": payload, "item_id": item_id})
        elif kind == "response.output_audio_transcript.delta" and self.response_generation == self.generation:
            await self.ws.send_json(
                {"type": "caption", "delta": event.get("delta", ""), "item_id": event.get("item_id")}
            )
        elif kind == "response.output_audio.done" and self.response_generation == self.generation:
            await self.ws.send_json({"type": "audio_done", "item_id": event.get("item_id")})
        elif kind == "response.done":
            event_received_at = time.monotonic()
            response_generation = self.response_generation
            speech_request = self.active_speech_request
            response = event.get("response") or {}
            response_id = str(response.get("id") or "").strip()
            if response_id and response_id in self.engine.completed_response_ids:
                # Usage writes are independently idempotent. A redelivery may
                # repair an earlier completion that omitted authoritative usage.
                await self.settle_response(event)
                return
            try:
                with voice_latency_span(
                    self.log_latency,
                    "tts_usage_settlement",
                    generation=response_generation,
                ):
                    await self.settle_response(event)
            except BaseException as error:
                self.finish_turn_timing(
                    response_generation,
                    outcome=voice_latency_outcome(error),
                    ended_at=event_received_at,
                )
                self.active_speech_request = None
                self.response_idle.set()
                raise
            inspected = await self.engine.inspect_provider_event(event)
            if inspected.duplicate_response:
                return
            timing = self.turn_timings.get(response_generation)
            response_status = str(response.get("status") or "unknown")
            now = event_received_at
            if timing and timing.tts_started_at is not None:
                self.log_latency(
                    "tts_complete",
                    timing.tts_started_at,
                    generation=response_generation,
                    outcome=response_status,
                    segment=timing.tts_attempt,
                    ended_at=now,
                )
            if self.closing:
                self.finish_turn_timing(
                    response_generation,
                    outcome="closed",
                    ended_at=now,
                )
                self.active_speech_request = None
                self.response_idle.set()
                return
            call = inspected.bridge_call
            if call:
                if self.response_generation != self.generation:
                    await self.discard_bridge_call(call)
                else:
                    await self.queue_bridge_call(self.response_generation, call)
            elif response.get("status") not in {"completed", "cancelled"}:
                self.finish_turn_timing(
                    self.response_generation,
                    outcome="error",
                    ended_at=now,
                )
                raise RuntimeError("Voice response failed")
            elif not self.response_has_audio and response.get("status") == "completed":
                raise RuntimeError("Voice provider did not return a Manor turn")
            elif self.response_has_audio and self.response_generation != self.generation:
                self.finish_turn_timing(
                    self.response_generation,
                    outcome="interrupted",
                    ended_at=now,
                )
            elif (
                self.response_has_audio
                and response.get("status") == "completed"
                and self.response_generation == self.generation
                and self.response_audio_bytes == 0
            ):
                generation = self.response_generation
                if (
                    speech_request is not None
                    and speech_request.retry_silent_audio
                    and speech_request.retry_count == 0
                ):
                    # The Agent result is already saved. Retry only provider
                    # speech generation so actions and chat messages never run
                    # twice when this exact Realtime response completes without
                    # audio. Request-local state prevents an earlier response in
                    # the same microphone generation from consuming the retry.
                    self.active_speech_request = None
                    self.response_idle.set()
                    self._queue_spoken_response(
                        generation,
                        speech_request.spoken_reply,
                        retry_silent_audio=True,
                        request_id=speech_request.request_id,
                        retry_count=1,
                    )
                    return
                self.active_speech_request = None
                self.response_idle.set()
                self.finish_turn_timing(generation, outcome="no_audio", ended_at=now)
                raise RuntimeError("Voice provider returned no audio")
            elif (
                self.response_has_audio
                and response.get("status") == "completed"
                and self.response_generation == self.generation
                and self.response_audio_bytes > 0
            ):
                self.active_speech_request = None
                self.finish_turn_timing(
                    self.response_generation,
                    outcome="completed",
                    ended_at=now,
                )
            elif response_status == "cancelled":
                self.active_speech_request = None
                self.finish_turn_timing(
                    self.response_generation,
                    outcome="interrupted",
                    ended_at=now,
                )
            self.active_speech_request = None
            self.response_idle.set()

    async def _send_spoken_turn(
        self,
        generation: int,
        outcome: VoiceAgentOutcome,
        *,
        retry_silent_audio: bool,
    ) -> None:
        await self.ws.send_json(
            {
                "type": "turn",
                "conversation_id": outcome.conversation_id,
                "text": outcome.spoken_reply,
                "status": outcome.status,
                "generation": generation,
            }
        )
        if outcome.spoken_reply and generation == self.generation:
            await self.ws.send_json(
                {"type": "synthesizing", "generation": generation}
            )
            self._queue_spoken_response(
                generation,
                outcome.spoken_reply,
                retry_silent_audio=retry_silent_audio,
            )
        else:
            self.finish_turn_timing(
                generation,
                outcome=(
                    outcome.status if not outcome.spoken_reply else "interrupted"
                ),
            )

    async def _send_work_state(self, status: str) -> None:
        if not self.closing:
            await self.ws.send_json({"type": "work", "status": status})

    async def _record_control_turn(self, user_text: str, assistant_text: str) -> None:
        if self.record_control_turn is not None:
            await self.record_control_turn(user_text, assistant_text)

    async def _reply_about_work(
        self,
        generation: int,
        call: BridgeCall,
        *,
        kind: str,
        expected_work: BrowserAgentWork | None = None,
    ) -> None:
        async with self.foreground_delivery_lock:
            # Completion delivery uses the same lock. If it won the race, never
            # follow a completed result with a stale "still working" response.
            if expected_work is not None and self.active_agent_work is not expected_work:
                kind = "error" if self.last_work_status == "error" else "completed"
            reply = _voice_work_reply(call.utterance, kind)
            await self._record_control_turn(call.utterance, reply)
            outcome = VoiceAgentOutcome(
                status="action_handled",
                spoken_reply=reply,
                conversation_id=self.usage_scope["conversation_id"],
                agent_id=self.usage_scope["agent_id"],
            )
            await self.engine.send_function_outcome(call, outcome)
            await self._send_spoken_turn(
                generation,
                outcome,
                retry_silent_audio=True,
            )

    async def _queue_followup_agent_call(
        self,
        generation: int,
        call: BridgeCall,
    ) -> bool:
        """Acknowledge and retain a new instruction while another Agent runs."""

        async with self.foreground_delivery_lock:
            if self.active_agent_work is None:
                return False
            self.queued_agent_calls.append(
                QueuedBrowserAgentCall(generation=generation, call=call)
            )
            reply = _voice_work_reply(call.utterance, "queued")
            outcome = VoiceAgentOutcome(
                status="action_handled",
                spoken_reply=reply,
                conversation_id=self.usage_scope["conversation_id"],
                agent_id=self.usage_scope["agent_id"],
            )
            # Close the provider function call now. The queued Chat Agent result
            # is delivered as speech later and must not reuse the function id.
            await self.engine.send_function_outcome(call, outcome)
            await self._send_spoken_turn(
                generation,
                outcome,
                retry_silent_audio=True,
            )
            return True

    def _create_agent_work(
        self,
        generation: int,
        call: BridgeCall,
        *,
        acknowledged: bool = False,
        timing: VoiceTurnTiming | None = None,
    ) -> BrowserAgentWork:
        started_at = time.monotonic()
        if timing is None:
            timing = self.turn_timings.setdefault(
                generation,
                VoiceTurnTiming(turn_id=generation),
            )
        if timing.transcription_completed_at is not None:
            self.log_latency(
                "agent_queue",
                timing.transcription_completed_at,
                generation=generation,
                ended_at=started_at,
            )
        work = BrowserAgentWork(
            generation=generation,
            call=call,
            task=asyncio.create_task(self.agent(call.utterance)),
            started_at=started_at,
            acknowledged=acknowledged,
        )
        self.active_agent_work = work
        self.turn_idle.clear()
        return work

    async def _advance_agent_queue_locked(self, work: BrowserAgentWork) -> None:
        if self.active_agent_work is not work:
            return
        self.active_agent_work = None
        if not self.queued_agent_calls:
            self.turn_idle.set()
            return
        queued = self.queued_agent_calls.popleft()
        next_work = self._create_agent_work(
            queued.generation,
            queued.call,
            acknowledged=True,
        )
        completion = asyncio.create_task(self._deliver_agent_work(next_work))
        self._track_agent_completion(completion)
        await self._send_work_state("running")

    async def _deliver_agent_work(
        self,
        work: BrowserAgentWork,
        outcome: VoiceAgentOutcome | None = None,
        error: BaseException | None = None,
    ) -> None:
        advanced = False
        try:
            if outcome is None and error is None:
                try:
                    outcome = await work.task
                except BaseException as caught:
                    error = caught
            completed_at = time.monotonic()
            if error is not None:
                logger.error(
                    (
                        "Accepted browser voice turn failed during hangup settlement"
                        if self.closing
                        else "Browser Voice background Agent failed"
                    ),
                    exc_info=(type(error), error, error.__traceback__),
                )
                self.log_latency(
                    "agent",
                    work.started_at,
                    generation=work.generation,
                    outcome=voice_latency_outcome(error),
                    ended_at=completed_at,
                )
                outcome = VoiceAgentOutcome(
                    status="error",
                    spoken_reply=_voice_work_reply(work.call.utterance, "error"),
                    conversation_id=self.usage_scope["conversation_id"],
                    agent_id=self.usage_scope["agent_id"],
                )
            else:
                if not isinstance(outcome, VoiceAgentOutcome):
                    raise TypeError("Voice Agent returned an invalid outcome")
                self.log_latency(
                    "agent",
                    work.started_at,
                    generation=work.generation,
                    outcome=outcome.status,
                    ended_at=completed_at,
                )
            if outcome.conversation_id != self.usage_scope["conversation_id"]:
                raise RuntimeError("Voice conversation changed during call")
            self.last_work_completed_at = completed_at
            self.last_work_status = outcome.status
            if self.closing:
                self.finish_turn_timing(
                    work.generation,
                    outcome="closed",
                    ended_at=completed_at,
                )
            else:
                await self.input_idle.wait()
            async with self.foreground_delivery_lock:
                if not self.closing:
                    delivery_generation = self.generation
                    if work.acknowledged:
                        await self._send_work_state(
                            "failed" if outcome.status == "error" else "completed"
                        )
                    if not work.acknowledged:
                        await self.engine.send_function_outcome(work.call, outcome)
                    await self._send_spoken_turn(
                        delivery_generation,
                        outcome,
                        retry_silent_audio=True,
                    )
                await self._advance_agent_queue_locked(work)
                advanced = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Browser Voice background Agent delivery failed")
        finally:
            if not advanced:
                async with self.foreground_delivery_lock:
                    await self._advance_agent_queue_locked(work)

    def _track_agent_completion(self, task: asyncio.Task) -> None:
        self.agent_completion_tasks.add(task)
        task.add_done_callback(self.agent_completion_tasks.discard)

    async def _start_agent_work(
        self,
        generation: int,
        call: BridgeCall,
        timing: VoiceTurnTiming,
    ) -> None:
        work = self._create_agent_work(generation, call, timing=timing)
        started_at = work.started_at
        task = work.task
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=BACKGROUND_ACK_AFTER_SECONDS,
            )
        except TimeoutError:
            if task.done():
                try:
                    outcome = task.result()
                except BaseException as error:
                    await self._deliver_agent_work(work, error=error)
                else:
                    await self._deliver_agent_work(work, outcome=outcome)
                return
            work.acknowledged = True
            acknowledgement = VoiceAgentOutcome(
                status="action_handled",
                spoken_reply=_voice_work_reply(call.utterance, "ack"),
                conversation_id=self.usage_scope["conversation_id"],
                agent_id=self.usage_scope["agent_id"],
            )
            self.log_latency(
                "agent_ack",
                started_at,
                generation=generation,
            )
            await self._send_work_state("running")
            await self.engine.send_function_outcome(call, acknowledgement)
            await self._send_spoken_turn(
                generation,
                acknowledgement,
                retry_silent_audio=True,
            )
            completion = asyncio.create_task(self._deliver_agent_work(work))
            self._track_agent_completion(completion)
        except asyncio.CancelledError:
            # The bounded hangup path owns cancellation. asyncio.shield keeps
            # an admitted Chat request alive during normal transport teardown,
            # but once its settlement deadline expires the underlying task
            # must also stop and the infinite turn worker must stay cancelled.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.active_agent_work is work:
                self.active_agent_work = None
                self.turn_idle.set()
            raise
        except BaseException as error:
            await self._deliver_agent_work(work, error=error)
        else:
            await self._deliver_agent_work(work, outcome=outcome)

    async def run_turns(self):
        while True:
            generation, call = await self.turns.get()
            if self.closing:
                return
            if generation != self.generation:
                await self.discard_bridge_call(call)
                continue
            try:
                with voice_latency_span(
                    self.log_latency,
                    "agent_access_check",
                    generation=generation,
                ):
                    await self.preflight()
            except BaseException as error:
                self.finish_turn_timing(
                    generation,
                    outcome=voice_latency_outcome(error),
                )
                raise
            if self.closing:
                return
            if generation != self.generation:
                await self.discard_bridge_call(call)
                continue
            try:
                timing = self.turn_timings.setdefault(
                    generation,
                    VoiceTurnTiming(turn_id=generation),
                )
                await self.ws.send_json(
                    {
                        "type": "transcript",
                        "role": "user",
                        "text": call.utterance,
                        "generation": generation,
                    }
                )
                active_work = self.active_agent_work
                if active_work is not None:
                    if is_voice_progress_query(call.utterance):
                        await self._reply_about_work(
                            generation,
                            call,
                            kind="progress",
                            expected_work=active_work,
                        )
                        continue
                    if await self._queue_followup_agent_call(generation, call):
                        continue
                if (
                    is_voice_progress_query(call.utterance)
                    and self.last_work_completed_at is not None
                    and time.monotonic() - self.last_work_completed_at
                    <= RECENT_WORK_STATUS_SECONDS
                ):
                    self.turn_idle.clear()
                    try:
                        await self._reply_about_work(
                            generation,
                            call,
                            kind=(
                                "error"
                                if self.last_work_status == "error"
                                else "completed"
                            ),
                        )
                    finally:
                        self.turn_idle.set()
                    continue
                # The standard Chat Agent remains authoritative for messages,
                # approvals, tools and credit usage. Slow work is detached from
                # the foreground phone turn after a short acknowledgement.
                await self._start_agent_work(generation, call, timing)
            except BaseException as error:
                self.finish_turn_timing(
                    generation,
                    outcome=voice_latency_outcome(error),
                )
                raise

    async def settle_response(self, event: dict):
        response = event.get("response")
        response_id = (
            str(response.get("id") or "").strip()
            if isinstance(response, dict)
            else ""
        )
        failure_key = f"response:{response_id or 'unknown'}"
        usage = extract_realtime_usage(
            event,
            model=self.route.model,
            byok=self.route.byok,
            provider=self.route.provider,
        )
        if usage is None:
            raw_usage = response.get("usage") if isinstance(response, dict) else None
            if not self.route.byok and not _has_explicit_zero_usage(raw_usage):
                # response.done ends the provider request, but without valid
                # authoritative usage Manor cannot safely clear its credit hold.
                self.billing_failures.add(failure_key)
                logger.warning(
                    "Browser voice response omitted authoritative usage; preserving reservation"
                )
            else:
                self.billing_failures.discard(failure_key)
            return
        try:
            await self.engine.settle_response(event)
        except BaseException:
            # A managed provider request must retain its credit hold until its
            # authoritative usage has been durably written.
            if not self.route.byok:
                self.billing_failures.add(failure_key)
            raise
        else:
            self.billing_failures.discard(failure_key)

    async def settle_input_transcription(self, event: dict):
        item_id = str(event.get("item_id") or "").strip()
        failure_key = f"transcription:{item_id or 'unknown'}"
        usage = extract_input_transcription_usage(
            event,
            byok=self.route.byok,
            provider=self.route.provider,
        )
        if usage is None:
            raw_usage = event.get("usage")
            if not self.route.byok and not _has_explicit_zero_usage(
                raw_usage,
                usage_type="tokens",
            ):
                self.billing_failures.add(failure_key)
                logger.warning(
                    "Browser voice transcription omitted authoritative usage; "
                    "preserving reservation"
                )
            else:
                self.billing_failures.discard(failure_key)
            return
        try:
            await self.engine.settle_transcription(event)
        except BaseException:
            if not self.route.byok:
                self.billing_failures.add(failure_key)
            raise
        else:
            self.billing_failures.discard(failure_key)
