"""Shared OpenAI/Vercel Realtime provider and Manor bridge contracts."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
import websockets


DEFAULT_REALTIME_MODEL = "openai/gpt-realtime"
DEFAULT_VERCEL_REALTIME_MODEL = "openai/gpt-realtime-mini"
VERCEL_REALTIME_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
SUPPORTED_REALTIME_MODELS = frozenset(
    {
        "openai/gpt-realtime",
        "openai/gpt-realtime-2025-08-28",
    }
)
SUPPORTED_VERCEL_REALTIME_MODELS = frozenset({DEFAULT_VERCEL_REALTIME_MODEL})
OPENAI_API_BASE_URL = "https://api.openai.com/v1"
VERCEL_GATEWAY_PROTOCOL_VERSION = "0.0.1"
VERCEL_REALTIME_SUBPROTOCOL = "ai-gateway-realtime.v1"
MANOR_BRIDGE_FUNCTION = "manor_agent_reply"
MAX_UTTERANCE_CHARS = 4000


@dataclass(frozen=True)
class RealtimeRoute:
    api_key: str
    base_url: str
    model: str
    byok: bool
    provider: str = "openai"
    auth_method: Literal["api-key", "oidc"] = "api-key"

    @property
    def wire_model(self) -> str:
        """Return the provider-native model name at the wire boundary."""

        return self.model.removeprefix("openai/")


@dataclass(frozen=True)
class BridgeCall:
    call_id: str | None
    utterance: str
    response_id: str


VoiceOutcomeStatus = Literal[
    "ok",
    "approval_required",
    "action_handled",
    "no_reply",
    "error",
]


@dataclass(frozen=True)
class VoiceAgentOutcome:
    status: VoiceOutcomeStatus
    spoken_reply: str
    conversation_id: str | None = None
    agent_id: str | None = None


def resolve_realtime_model() -> str:
    configured = str(
        os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_REALTIME_MODEL) or ""
    ).strip()
    if "/" not in configured:
        configured = f"openai/{configured}"
    if configured not in SUPPORTED_REALTIME_MODELS:
        raise RuntimeError(
            "Twilio Voice requires a supported native OpenAI gpt-realtime model"
        )
    return configured


def resolve_vercel_realtime_model() -> str:
    configured = str(
        os.getenv("VERCEL_REALTIME_MODEL", DEFAULT_VERCEL_REALTIME_MODEL) or ""
    ).strip()
    if "/" not in configured:
        configured = f"openai/{configured}"
    if configured not in SUPPORTED_VERCEL_REALTIME_MODELS:
        raise RuntimeError(
            "Twilio Voice through Vercel requires "
            "openai/gpt-realtime-mini"
        )
    return configured


def is_native_openai_base_url(base_url: str | None) -> bool:
    parsed = urlsplit(str(base_url or OPENAI_API_BASE_URL).strip().rstrip("/"))
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.openai.com"
        and parsed.port is None
        and parsed.path in {"", "/v1"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
    )


async def _resolve_realtime_byok(entity_id: str | None, user_id: str | None = None) -> dict | None:
    if not entity_id:
        return None
    from packages.core.services.model_gateway import provider_for_model
    from packages.core.services.model_resolver import (
        resolve_llm_metadata_for_user,
        resolve_model_for_user,
    )

    selected_model = await resolve_model_for_user(
        "primary",
        user_id=user_id,
        entity_id=entity_id,
    )
    if provider_for_model(selected_model) != "openai":
        return None
    return await resolve_llm_metadata_for_user(
        "primary",
        user_id=user_id,
        entity_id=entity_id,
    )


async def _has_audio_role_override(entity_id: str, user_id: str) -> bool:
    from packages.core.services.model_resolver import resolve_llm_metadata_for_user

    for role in ("voice", "stt"):
        metadata = await resolve_llm_metadata_for_user(
            role,
            user_id=user_id,
            entity_id=entity_id,
        )
        if str((metadata or {}).get("llm_api_key") or "").strip():
            return True
    return False


async def resolve_realtime_route(
    entity_id: str | None,
    *,
    user_id: str | None = None,
    required: bool = True,
    allow_managed: bool = True,
    respect_audio_role_overrides: bool = False,
) -> RealtimeRoute | None:
    model = resolve_realtime_model()
    if (
        respect_audio_role_overrides
        and entity_id
        and user_id
        and await _has_audio_role_override(entity_id, user_id)
    ):
        return None
    byok = await _resolve_realtime_byok(entity_id, user_id) if user_id else await _resolve_realtime_byok(entity_id)
    byok_key = str((byok or {}).get("llm_api_key") or "").strip()
    byok_base_url = str(
        (byok or {}).get("llm_base_url") or OPENAI_API_BASE_URL
    ).strip().rstrip("/")
    incompatible_byok = bool(byok_key and not is_native_openai_base_url(byok_base_url))
    if byok_key and not incompatible_byok:
        return RealtimeRoute(
            api_key=byok_key,
            base_url=OPENAI_API_BASE_URL,
            model=model,
            byok=True,
            provider="openai",
        )

    if not allow_managed:
        route = None
    else:
        from packages.core.services.model_gateway import resolve_official_model_route

        route = await resolve_official_model_route(
            model,
            reason="channel.voice.realtime.official_provider_key",
            provider_chain=("vercel", "openai"),
        )
    if route and route.api_key:
        provider = str(route.provider or "").strip().lower()
        route_base_url = str(route.base_url or "").strip().rstrip("/")
        if provider == "vercel":
            _vercel_gateway_origin(route_base_url)
            auth_method: Literal["api-key", "oidc"] = (
                "oidc"
                if str(getattr(route, "source_detail", "") or "").strip()
                == "VERCEL_OIDC_TOKEN"
                else "api-key"
            )
            return RealtimeRoute(
                api_key=str(route.api_key).strip(),
                base_url=route_base_url,
                model=resolve_vercel_realtime_model(),
                byok=False,
                provider="vercel",
                auth_method=auth_method,
            )
        if provider != "openai" or not is_native_openai_base_url(route_base_url):
            if not required:
                return None
            raise RuntimeError(
                "Twilio Voice Realtime route provider is not supported"
            )
        return RealtimeRoute(
            api_key=str(route.api_key).strip(),
            base_url=OPENAI_API_BASE_URL,
            model=model,
            byok=False,
            provider="openai",
        )

    if not required:
        return None
    if incompatible_byok:
        raise RuntimeError(
            "Twilio Voice Realtime requires native OpenAI BYOK; the configured "
            "OpenAI-compatible custom base URL cannot carry realtime audio, and "
            "no Admin Vercel or OpenAI Realtime credential is available"
        )
    raise RuntimeError(
        "Twilio Voice Realtime requires native OpenAI BYOK or an Admin Vercel/"
        "OpenAI credential; OpenRouter can answer the bound Agent but does not "
        "provide this Realtime WebSocket transport"
    )


def open_realtime_connection(route: RealtimeRoute):
    if route.provider == "vercel":
        return _VercelRealtimeConnection(route)
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=route.api_key, base_url=route.base_url)
    return client.realtime.connect(model=route.wire_model)


def _vercel_gateway_origin(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ai-gateway.vercel.sh"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "Twilio Voice requires the official Vercel Gateway URL"
        )
    return f"https://{parsed.netloc}"


def _vercel_session_update_event(session: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if session.get("instructions") is not None:
        config["instructions"] = session["instructions"]
    if session.get("output_modalities") is not None:
        config["outputModalities"] = session["output_modalities"]
    audio = session.get("audio")
    if isinstance(audio, dict):
        audio_input = audio.get("input")
        if isinstance(audio_input, dict):
            if isinstance(audio_input.get("format"), dict):
                config["inputAudioFormat"] = audio_input["format"]
            if isinstance(audio_input.get("transcription"), dict):
                config["inputAudioTranscription"] = audio_input["transcription"]
            turn_detection = audio_input.get("turn_detection")
            if isinstance(turn_detection, dict):
                turn_type = str(turn_detection.get("type") or "").replace("_", "-")
                normalized_turn: dict[str, Any] = {"type": turn_type}
                for source, target in (
                    ("threshold", "threshold"),
                    ("silence_duration_ms", "silenceDurationMs"),
                    ("prefix_padding_ms", "prefixPaddingMs"),
                ):
                    if turn_detection.get(source) is not None:
                        normalized_turn[target] = turn_detection[source]
                config["turnDetection"] = normalized_turn
        audio_output = audio.get("output")
        if isinstance(audio_output, dict):
            if isinstance(audio_output.get("format"), dict):
                config["outputAudioFormat"] = audio_output["format"]
            if audio_output.get("voice") is not None:
                config["voice"] = audio_output["voice"]
    if isinstance(session.get("tools"), list):
        config["tools"] = session["tools"]
    provider_options: dict[str, Any] = {}
    if session.get("tool_choice") is not None:
        provider_options["tool_choice"] = session["tool_choice"]
    if isinstance(audio, dict):
        # Keep OpenAI-specific VAD response/interruption flags that the
        # normalized AI SDK turn-detection shape does not expose.
        provider_options["audio"] = audio
    if provider_options:
        config["providerOptions"] = provider_options
    return {"type": "session-update", "config": config}


def _vercel_client_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = event.get("type")
    if event_type == "session.update":
        session = event.get("session")
        if not isinstance(session, dict):
            raise TypeError("Vercel Realtime session update is invalid")
        return [_vercel_session_update_event(session)]
    if event_type == "input_audio_buffer.append":
        return [{"type": "input-audio-append", "audio": event.get("audio")}]
    if event_type == "input_audio_buffer.clear":
        return [{"type": "input-audio-clear"}]
    if event_type == "response.cancel":
        return [{"type": "response-cancel"}]
    if event_type == "conversation.item.create":
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            raise TypeError("Vercel Realtime conversation item is not supported")
        return [
            {
                "type": "conversation-item-create",
                "item": {
                    "type": "function-call-output",
                    "callId": item.get("call_id"),
                    "name": MANOR_BRIDGE_FUNCTION,
                    "output": item.get("output"),
                },
            }
        ]
    if event_type == "conversation.item.truncate":
        return [
            {
                "type": "conversation-item-truncate",
                "itemId": event.get("item_id"),
                "contentIndex": event.get("content_index"),
                "audioEndMs": event.get("audio_end_ms"),
            }
        ]
    if event_type == "response.create":
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        options: dict[str, Any] = {}
        if response.get("output_modalities") is not None:
            options["modalities"] = response["output_modalities"]
        if response.get("instructions") is not None:
            options["instructions"] = response["instructions"]
        if response.get("metadata") is not None:
            options["metadata"] = response["metadata"]
        create_event: dict[str, Any] = {"type": "response-create"}
        if options:
            create_event["options"] = options
        return [create_event]
    raise TypeError(f"Unsupported Vercel Realtime client event: {event_type}")


class _VercelRealtimeConnection:
    def __init__(self, route: RealtimeRoute):
        self._route = route
        self._socket_manager: Any | None = None
        self._socket: Any | None = None
        self._send_lock = asyncio.Lock()
        self._session: dict[str, Any] = {}
        self._verified = False

    async def __aenter__(self):
        origin = _vercel_gateway_origin(self._route.base_url)
        headers = {
            "Authorization": f"Bearer {self._route.api_key}",
            "ai-gateway-protocol-version": VERCEL_GATEWAY_PROTOCOL_VERSION,
            "ai-gateway-auth-method": self._route.auth_method,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{origin}/v1/realtime/client-secrets",
                headers=headers,
                json={"model": self._route.model, "expiresIn": 60},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    "Vercel Realtime client-secret request failed with "
                    f"HTTP {exc.response.status_code}"
                ) from None
            payload = response.json()
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if not token.startswith("vcst_"):
            raise RuntimeError("Vercel Realtime returned no valid client secret")
        query = urlencode({"ai-model-id": self._route.model})
        websocket_url = f"wss://{urlsplit(origin).netloc}/v4/ai/realtime-model?{query}"
        self._socket_manager = websockets.connect(
            websocket_url,
            subprotocols=[
                VERCEL_REALTIME_SUBPROTOCOL,
                f"ai-gateway-auth.{token}",
            ],
            open_timeout=15,
            max_size=2 * 1024 * 1024,
        )
        self._socket = await self._socket_manager.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self._socket_manager is not None:
            return await self._socket_manager.__aexit__(exc_type, exc, traceback)
        return False

    async def send(self, event: dict[str, Any]) -> None:
        if self._socket is None:
            raise RuntimeError("Vercel Realtime connection is not open")
        event_type = event.get("type")
        if event_type == "session.update":
            session = event.get("session")
            if not isinstance(session, dict):
                raise TypeError("Vercel Realtime session update is invalid")
            self._session = dict(session)
        mapped: list[dict[str, Any]] = []
        if event_type == "response.create":
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            response_session = {
                **self._session,
                "tool_choice": response.get(
                    "tool_choice",
                    self._session.get("tool_choice", "none"),
                ),
            }
            mapped.append(_vercel_session_update_event(response_session))
        mapped.extend(_vercel_client_events(event))
        async with self._send_lock:
            for item in mapped:
                await self._socket.send(json.dumps(item, separators=(",", ":")))

    async def recv(self) -> dict[str, Any]:
        if self._socket is None:
            raise RuntimeError("Vercel Realtime connection is not open")
        payload = await self._socket.recv()
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        try:
            event = json.loads(payload)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("Vercel Realtime returned invalid JSON") from exc
        if not isinstance(event, dict):
            raise RuntimeError("Vercel Realtime returned an invalid event")
        event_type = event.get("type")
        if event_type in {"audio-delta", "audio-done"}:
            normalized = {
                "type": (
                    "response.output_audio.delta"
                    if event_type == "audio-delta"
                    else "response.output_audio.done"
                ),
                "response_id": event.get("responseId"),
                "item_id": event.get("itemId"),
            }
            if event_type == "audio-delta":
                normalized["delta"] = event.get("delta")
            return normalized
        raw = event.get("raw")
        if isinstance(raw, dict):
            self._verify_response_controls(raw)
            return raw
        if event_type == "error":
            error = {
                "message": str(event.get("message") or "Unknown error"),
            }
            if event.get("code") is not None:
                error["code"] = str(event["code"])
            return {
                "type": "error",
                "error": error,
            }
        raise RuntimeError("Vercel Realtime event is missing provider event data")

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        return await self.recv()

    def _verify_response_controls(self, event: dict[str, Any]) -> None:
        if self._verified or event.get("type") != "session.updated":
            return
        session = event.get("session")
        if not isinstance(session, dict):
            raise RuntimeError("Vercel realtime did not return its session controls")
        detection = ((session.get("audio") or {}).get("input") or {}).get(
            "turn_detection"
        ) or {}
        accepted_transcription_value = (
            ((session.get("audio") or {}).get("input") or {}).get(
                "transcription"
            )
            or {}
        )
        accepted_transcription = (
            accepted_transcription_value
            if isinstance(accepted_transcription_value, dict)
            else {}
        )
        expected_transcription_value = (
            ((self._session.get("audio") or {}).get("input") or {}).get(
                "transcription"
            )
            or {}
        )
        expected_transcription = (
            expected_transcription_value
            if isinstance(expected_transcription_value, dict)
            else {}
        )
        expected_tool_choice = self._session.get("tool_choice")
        if detection.get("create_response") is not False or (
            expected_tool_choice is not None
            and session.get("tool_choice") != expected_tool_choice
        ) or (
            expected_transcription
            and any(
                accepted_transcription.get(key) != value
                for key, value in expected_transcription.items()
            )
        ):
            raise RuntimeError(
                "Vercel realtime did not accept Manor's response controls"
            )
        self._verified = True


def event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    raise TypeError("Unsupported OpenAI Realtime event")


def parse_completed_bridge_call(
    event: Any,
    *,
    seen_event_ids: set[str],
    seen_call_ids: set[str],
) -> BridgeCall | None:
    data = event_dict(event)
    event_id = str(data.get("event_id") or "").strip()
    if event_id:
        if event_id in seen_event_ids:
            return None
        seen_event_ids.add(event_id)
    if data.get("type") != "response.done":
        return None

    response = data.get("response")
    if not isinstance(response, dict) or response.get("status") != "completed":
        return None
    response_id = str(response.get("id") or "").strip()
    if not response_id:
        return None
    output = response.get("output")
    if not isinstance(output, list):
        return None
    function_calls = [
        item
        for item in output
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    if len(function_calls) != 1:
        return None
    item = function_calls[0]
    if item.get("status") != "completed" or item.get("name") != MANOR_BRIDGE_FUNCTION:
        return None
    call_id = str(item.get("call_id") or "").strip()
    if not call_id or call_id in seen_call_ids:
        return None
    try:
        arguments = json.loads(str(item.get("arguments") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(arguments, dict) or set(arguments) != {"utterance"}:
        return None
    utterance_value = arguments.get("utterance")
    if not isinstance(utterance_value, str):
        return None
    utterance = utterance_value.strip()
    if not utterance or len(utterance) > MAX_UTTERANCE_CHARS:
        return None
    seen_call_ids.add(call_id)
    return BridgeCall(
        call_id=call_id,
        utterance=utterance,
        response_id=response_id,
    )


def parse_completed_input_transcription(
    event: Any,
    *,
    seen_item_ids: set[str],
) -> BridgeCall | None:
    data = event_dict(event)
    if data.get("type") != "conversation.item.input_audio_transcription.completed":
        return None
    item_id = str(data.get("item_id") or "").strip()
    if not item_id or item_id in seen_item_ids:
        return None
    transcript_value = data.get("transcript")
    if not isinstance(transcript_value, str):
        return None
    transcript = transcript_value.strip()
    if not transcript or len(transcript) > MAX_UTTERANCE_CHARS:
        return None
    seen_item_ids.add(item_id)
    return BridgeCall(
        call_id=None,
        utterance=transcript,
        response_id=item_id,
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def extract_realtime_usage(
    event: Any,
    *,
    model: str,
    byok: bool,
    provider: str = "openai",
) -> dict[str, Any] | None:
    data = event_dict(event)
    if data.get("type") != "response.done":
        return None
    response = data.get("response")
    if not isinstance(response, dict):
        return None
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, dict):
        return None

    input_tokens = _nonnegative_int(raw_usage.get("input_tokens"))
    output_tokens = _nonnegative_int(raw_usage.get("output_tokens"))
    if input_tokens == 0 and output_tokens == 0:
        return None
    input_details = raw_usage.get("input_token_details")
    if not isinstance(input_details, dict):
        input_details = {}
    output_details = raw_usage.get("output_token_details")
    if not isinstance(output_details, dict):
        output_details = {}
    cached_details = input_details.get("cached_tokens_details")
    if not isinstance(cached_details, dict):
        cached_details = {}

    cache_read = min(input_tokens, _nonnegative_int(input_details.get("cached_tokens")))
    audio_input = min(input_tokens, _nonnegative_int(input_details.get("audio_tokens")))
    audio_output = min(output_tokens, _nonnegative_int(output_details.get("audio_tokens")))
    cached_audio = min(
        cache_read,
        audio_input,
        _nonnegative_int(cached_details.get("audio_tokens")),
    )
    wire_model = str(model or DEFAULT_REALTIME_MODEL).strip()
    if wire_model.startswith("openai/"):
        wire_model = wire_model.split("/", 1)[1]
    billing_source = "byok" if byok else "platform"
    route_provider = str(provider or "openai").strip().lower()
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": max(
            input_tokens + output_tokens,
            _nonnegative_int(raw_usage.get("total_tokens")),
        ),
        "cache_read_input_tokens": cache_read,
        "audio_input_tokens": audio_input,
        "audio_output_tokens": audio_output,
        "cached_audio_input_tokens": cached_audio,
        "model": f"openai/{wire_model}",
        "provider": route_provider,
        "byok": byok,
        "billing_mode": billing_source,
        "api_key_source": billing_source,
        "pricing_source": (
            "byok" if byok else "vercel" if route_provider == "vercel" else "official"
        ),
    }


def extract_input_transcription_usage(
    event: Any,
    *,
    byok: bool,
    provider: str = "openai",
) -> dict[str, Any] | None:
    data = event_dict(event)
    if data.get("type") != "conversation.item.input_audio_transcription.completed":
        return None
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict) or raw_usage.get("type") != "tokens":
        return None
    input_tokens = _nonnegative_int(raw_usage.get("input_tokens"))
    output_tokens = _nonnegative_int(raw_usage.get("output_tokens"))
    if input_tokens == 0 and output_tokens == 0:
        return None
    input_details = raw_usage.get("input_token_details")
    if not isinstance(input_details, dict):
        input_details = {}
    route_provider = str(provider or "openai").strip().lower()
    billing_source = "byok" if byok else "platform"
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": max(
            input_tokens + output_tokens,
            _nonnegative_int(raw_usage.get("total_tokens")),
        ),
        "audio_input_tokens": min(
            input_tokens,
            _nonnegative_int(input_details.get("audio_tokens")),
        ),
        "model": f"openai/{VERCEL_REALTIME_TRANSCRIPTION_MODEL}",
        "provider": route_provider,
        "byok": byok,
        "billing_mode": billing_source,
        "api_key_source": billing_source,
        "pricing_source": (
            "byok" if byok else "vercel" if route_provider == "vercel" else "official"
        ),
    }


def build_realtime_session_update(
    *,
    model: str,
    voice: str = "alloy",
    input_transcription_model: str | None = None,
) -> dict[str, Any]:
    session: dict[str, Any] = {
        "type": "realtime",
        "model": model.removeprefix("openai/"),
        "output_modalities": ["text"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcmu"},
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": False,
                    "interrupt_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcmu"},
                "voice": voice,
            },
        },
    }
    if input_transcription_model:
        session["audio"]["input"]["transcription"] = {
            "model": input_transcription_model,
        }
    else:
        session["tools"] = [
            {
                "type": "function",
                "name": MANOR_BRIDGE_FUNCTION,
                "description": (
                    "Forward the caller's current utterance to the bound Manor Agent."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"utterance": {"type": "string"}},
                    "required": ["utterance"],
                    "additionalProperties": False,
                },
            }
        ]
        session["tool_choice"] = {
            "type": "function",
            "name": MANOR_BRIDGE_FUNCTION,
        }
    return {
        "type": "session.update",
        "session": session,
    }


def build_input_audio_event(payload: str) -> dict[str, Any]:
    return {
        "type": "input_audio_buffer.append",
        "audio": payload,
    }


def build_function_output_event(
    *,
    call_id: str,
    status: str,
    spoken_reply: str,
) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(
                {
                    "status": status,
                    "spoken_reply": spoken_reply,
                },
                ensure_ascii=True,
            ),
        },
    }


def build_spoken_response_event(
    *,
    spoken_reply: str,
    conversation: str = "auto",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "output_modalities": ["audio"],
        "tool_choice": "none",
        "instructions": (
            "Speak exactly this Manor response and nothing else: " + spoken_reply
        ),
    }
    if conversation != "auto":
        response["conversation"] = conversation
    return {"type": "response.create", "response": response}


@dataclass(frozen=True)
class RealtimeProviderEvent:
    data: dict[str, Any]
    bridge_call: BridgeCall | None = None
    duplicate_response: bool = False


class RealtimeVoiceEngine:
    """Provider-neutral Realtime controls shared by media adapters."""

    def __init__(
        self,
        *,
        route: RealtimeRoute,
        usage_scope: dict[str, Any],
        source: str,
        response_operation_prefix: str,
        transcription_operation_prefix: str | None = None,
        check_access: Callable[[], Awaitable[None]] | None = None,
        connection_factory: Callable[[RealtimeRoute], Any] = open_realtime_connection,
    ) -> None:
        self.route = route
        self.usage_scope = dict(usage_scope)
        self.source = source
        self.response_operation_prefix = response_operation_prefix
        self.transcription_operation_prefix = transcription_operation_prefix
        self.check_access = check_access
        self.connection_factory = connection_factory
        self.connection: Any | None = None
        self.seen_event_ids: set[str] = set()
        self.seen_call_ids: set[str] = set()
        self.seen_transcription_item_ids: set[str] = set()
        self.completed_response_ids: set[str] = set()
        self.billed_response_ids: set[str] = set()
        self.billed_transcription_ids: set[str] = set()

    @asynccontextmanager
    async def connect(self):
        manager = self.connection_factory(self.route)
        async with manager as connection:
            self.connection = connection
            try:
                yield connection
            finally:
                self.connection = None

    async def send(self, event: dict[str, Any]) -> None:
        if self.connection is None:
            raise RuntimeError("Realtime connection is not open")
        await self.connection.send(event)

    async def recv(self) -> dict[str, Any]:
        if self.connection is None:
            raise RuntimeError("Realtime connection is not open")
        return event_dict(await self.connection.recv())

    async def initialize_session(
        self,
        event: dict[str, Any],
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """Apply response controls before either media adapter starts I/O."""

        await self.send(event)
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    data = await self.recv()
                    event_type = str(data.get("type") or "")
                    if event_type == "session.updated":
                        return data
                    if event_type == "error":
                        error = data.get("error")
                        message = (
                            error.get("message") if isinstance(error, dict) else None
                        )
                        raise RuntimeError(
                            str(message or "Realtime provider rejected the session")
                        )
                    if event_type.startswith(("response.", "conversation.item.")):
                        raise RuntimeError(
                            "Realtime provider responded before accepting session controls"
                        )
        except TimeoutError as exc:
            raise RuntimeError(
                "Realtime provider did not accept session controls in time"
            ) from exc

    async def preflight_provider(
        self,
        *,
        allow_active_voice_reservation: bool = False,
    ) -> None:
        await self._check_access()
        if self.route.byok:
            return
        from packages.core.ai.runtime import (
            runtime_assert_credit_available,
            runtime_current_billing_context,
        )

        billing = runtime_current_billing_context()
        if billing is None or billing.suppress:
            raise RuntimeError("Platform Realtime requires a billing context")
        if allow_active_voice_reservation:
            from packages.core.services.voice.billing import (
                voice_call_has_active_reservation,
            )

            if voice_call_has_active_reservation():
                return
        await runtime_assert_credit_available(
            billing.entity_id,
            source=billing.source or self.source,
        )

    async def create_response(
        self,
        event: dict[str, Any],
        *,
        before_send: Callable[[], bool | None] | None = None,
    ) -> None:
        await self._check_access()
        if not self.route.byok:
            from packages.core.ai.runtime import (
                runtime_assert_credit_available,
                runtime_current_billing_context,
            )

            billing = runtime_current_billing_context()
            if billing is None or billing.suppress:
                raise RuntimeError("Platform Realtime response requires a billing context")
            from packages.core.services.voice.billing import (
                voice_call_has_active_reservation,
            )

            # Managed calls reserve credit before opening the provider socket.
            # Rechecking the same balance before every spoken response adds a
            # database round trip to each turn without changing admission.
            if not voice_call_has_active_reservation():
                await runtime_assert_credit_available(
                    billing.entity_id,
                    source=billing.source or self.source,
                )
        if before_send is not None and before_send() is False:
            return
        await self.send(event)

    async def inspect_provider_event(
        self,
        event: Any,
        *,
        conversation_id: str | None = None,
    ) -> RealtimeProviderEvent:
        data = event_dict(event)
        event_type = data.get("type")
        if event_type == "conversation.item.input_audio_transcription.completed":
            await self.settle_transcription(data, conversation_id=conversation_id)
            return RealtimeProviderEvent(
                data=data,
                bridge_call=parse_completed_input_transcription(
                    data,
                    seen_item_ids=self.seen_transcription_item_ids,
                ),
            )
        if event_type != "response.done":
            return RealtimeProviderEvent(data=data)

        await self.settle_response(data, conversation_id=conversation_id)
        response = data.get("response")
        response_id = (
            str(response.get("id") or "").strip()
            if isinstance(response, dict)
            else ""
        )
        if response_id and response_id in self.completed_response_ids:
            return RealtimeProviderEvent(data=data, duplicate_response=True)
        if response_id:
            self.completed_response_ids.add(response_id)
        return RealtimeProviderEvent(
            data=data,
            bridge_call=parse_completed_bridge_call(
                data,
                seen_event_ids=self.seen_event_ids,
                seen_call_ids=self.seen_call_ids,
            ),
        )

    async def send_function_outcome(
        self,
        call: BridgeCall,
        outcome: VoiceAgentOutcome,
    ) -> None:
        if not call.call_id:
            return
        await self.send(
            build_function_output_event(
                call_id=call.call_id,
                status=outcome.status,
                spoken_reply=outcome.spoken_reply,
            )
        )

    async def send_voice_outcome(
        self,
        call: BridgeCall,
        outcome: VoiceAgentOutcome,
        *,
        conversation: str = "auto",
    ) -> None:
        await self.send_function_outcome(call, outcome)
        if outcome.spoken_reply:
            await self.create_response(
                build_spoken_response_event(
                    spoken_reply=outcome.spoken_reply,
                    conversation=conversation,
                )
            )

    async def discard_bridge_call(self, call: BridgeCall) -> None:
        await self.send_function_outcome(
            call,
            VoiceAgentOutcome(status="no_reply", spoken_reply=""),
        )

    async def settle_response(
        self,
        event: Any,
        *,
        conversation_id: str | None = None,
    ) -> None:
        data = event_dict(event)
        usage = extract_realtime_usage(
            data,
            model=self.route.model,
            byok=self.route.byok,
            provider=self.route.provider,
        )
        if usage is None:
            return
        response = data.get("response")
        response_id = (
            str(response.get("id") or "").strip()
            if isinstance(response, dict)
            else ""
        )
        if not response_id:
            raise RuntimeError("Realtime usage has no response identity")
        if response_id in self.billed_response_ids:
            return
        await self._record_usage(
            usage=usage,
            operation_id=f"{self.response_operation_prefix}:{response_id}",
            conversation_id=conversation_id,
        )
        self.billed_response_ids.add(response_id)

    async def settle_transcription(
        self,
        event: Any,
        *,
        conversation_id: str | None = None,
    ) -> None:
        data = event_dict(event)
        usage = extract_input_transcription_usage(
            data,
            byok=self.route.byok,
            provider=self.route.provider,
        )
        if usage is None:
            return
        event_id = str(data.get("event_id") or "").strip()
        if not event_id:
            raise RuntimeError("Realtime transcription usage has no event identity")
        if event_id in self.billed_transcription_ids:
            return
        if not self.transcription_operation_prefix:
            raise RuntimeError("Realtime transcription usage has no operation prefix")
        await self._record_usage(
            usage=usage,
            operation_id=f"{self.transcription_operation_prefix}:{event_id}",
            conversation_id=conversation_id,
        )
        self.billed_transcription_ids.add(event_id)

    async def _check_access(self) -> None:
        if self.check_access is not None:
            await self.check_access()

    async def _record_usage(
        self,
        *,
        usage: dict[str, Any],
        operation_id: str,
        conversation_id: str | None,
    ) -> None:
        from packages.core.database import async_session
        from packages.core.services.usage_service import record_llm_usage

        scope = dict(self.usage_scope)
        if conversation_id is not None or "conversation_id" not in scope:
            scope["conversation_id"] = conversation_id
        async with async_session() as db:
            await record_llm_usage(
                db,
                **scope,
                usage=usage,
                source=self.source,
                operation_id=operation_id,
                strict=True,
            )
            await db.commit()
