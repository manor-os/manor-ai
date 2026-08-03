"""Whisper transcription — blob-in / text-out helper.

Single source of truth for OpenAI ``/audio/transcriptions`` calls. Used
by:
  * ``apps/api/routers/audio.py`` for the floating-chat mic button
  * ``packages/core/services/file_context.py`` for chat audio attachments

Both callers feed in raw bytes; this module owns the HTTP call, error
normalisation, and credit billing. The returned text + duration let the
caller decide how to present it (UI label vs inline transcript).
"""
from __future__ import annotations

import logging
import os
import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Cap upload size — OpenAI's hard limit is 25 MB.
WHISPER_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# OpenAI whisper-1: $0.006 per minute of audio.
_WHISPER_USD_PER_MINUTE = 0.006
# Groq whisper-large-v3: $0.111 per audio hour ≈ $0.00185/min.
_GROQ_WHISPER_USD_PER_MINUTE = 0.111 / 60.0


class WhisperError(Exception):
    """Raised when the Whisper request fails for a known reason
    (missing key, oversized input, upstream HTTP error). The message
    is user-safe — callers can surface it directly."""


class WhisperTimestampError(WhisperError):
    """Raised when a transcription route cannot return measured segments."""


class WhisperUploadTooLargeError(WhisperError):
    """Raised before an oversized audio upload is sent to a provider."""


@dataclass
class WhisperResult:
    text: str
    duration_seconds: float
    model: str
    # Per-segment timing from the native Whisper ``verbose_json`` response.
    # Each dict: {"start": float, "end": float, "text": str}. ``None`` when
    # timestamps are unavailable (e.g. the OpenRouter chat-audio fallback).
    segments: list[dict] | None = None
    # Word-level timing from native verbose transcription responses.
    words: list[dict] | None = None


async def transcribe_blob(
    blob: bytes,
    *,
    mime: str,
    filename: str = "audio.webm",
    language: Optional[str] = None,
    user_api_key: Optional[str] = None,
    user_base_url: Optional[str] = None,
    resolved_model: Optional[str] = None,
    require_timestamps: bool = False,
    reference_transcript: Optional[str] = None,
) -> WhisperResult:
    """Send ``blob`` to OpenAI Whisper, return the transcript + duration.

    Does NOT bill — caller is responsible for that step (audio router
    bills against the requesting user; file_context bills against the
    workspace's entity). Decoupling lets the same call serve both
    paths without forcing one billing identity on the other.

    Raises ``WhisperError`` for any failure that should be reported to
    the user. The exception message is safe to surface verbatim.
    """
    if not blob:
        raise WhisperError("Empty audio upload")
    if len(blob) > WHISPER_MAX_UPLOAD_BYTES:
        raise WhisperUploadTooLargeError(
            f"Audio too large: {len(blob) / 1024 / 1024:.1f} MB > "
            f"{WHISPER_MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
            "Whisper's hard limit is 25 MB; record a shorter clip."
        )

    # Resolve API key + base URL for Whisper transcription.
    #
    # Whisper is an audio endpoint — NOT a chat completion. OpenRouter
    # does NOT proxy /audio/transcriptions. So we need a provider that
    # actually supports this endpoint:
    #   - OpenAI (api.openai.com) — the canonical Whisper host
    #   - Groq (api.groq.com) — fast Whisper, same API shape
    #   - Any OpenAI-compatible endpoint via WHISPER_BASE_URL
    #
    # Key priority:
    #   1. User's BYOK key — assumed OpenAI-compatible
    #   2. Cloud-only platform official provider tokens

    # Use resolved model from user/entity preferences (Account page picker)
    # or fall back to env / defaults.
    model = resolved_model or os.getenv("WHISPER_MODEL", "")
    reported_model = model

    # Detect which API path to use based on the model ID
    # Chat-based models (gpt-4o-audio, gpt-audio-mini) use /chat/completions
    # Whisper models use /audio/transcriptions
    _CHAT_AUDIO_MODELS = {"openai/gpt-4o-audio-preview", "openai/gpt-audio-mini", "openai/gpt-audio"}
    use_chat_api = model in _CHAT_AUDIO_MODELS
    selected_provider = model.split("/", 1)[0].lower() if "/" in model else ""

    api_key = ""
    base_url = ""
    requested_base_url = str(user_base_url or "").strip().rstrip("/")
    if requested_base_url and not str(user_api_key or "").strip():
        raise WhisperError("A custom STT base URL requires a matching user API key.")

    # 1. BYOK
    if user_api_key and user_api_key.strip():
        from packages.core.services.model_gateway import (
            detect_provider_from_key,
            provider_from_base_url,
        )

        api_key = user_api_key.strip()
        key_provider = detect_provider_from_key(api_key)
        base_provider = provider_from_base_url(requested_base_url) if requested_base_url else None
        if selected_provider in {"openai", "groq"} and key_provider in {"openai", "groq"}:
            if selected_provider != key_provider:
                raise WhisperError(
                    f"The selected STT model uses {selected_provider}, but the saved BYOK key is for {key_provider}."
                )
        if base_provider in {"openai", "groq"} and selected_provider:
            if base_provider != selected_provider:
                raise WhisperError(
                    f"The custom STT base URL routes to {base_provider}, but the selected model uses "
                    f"{selected_provider}."
                )
        if base_provider in {"openai", "groq"} and key_provider in {"openai", "groq"}:
            if base_provider != key_provider:
                raise WhisperError(
                    f"The custom STT base URL routes to {base_provider}, but the saved BYOK key is for "
                    f"{key_provider}."
                )
        if requested_base_url:
            if base_provider == "openrouter" and not use_chat_api:
                error_type = WhisperTimestampError if require_timestamps else WhisperError
                raise error_type(
                    "Native Whisper transcription requires a timestamp-capable OpenAI-compatible "
                    "audio transcription endpoint; OpenRouter does not proxy /audio/transcriptions."
                )
            base_url = requested_base_url
            if selected_provider and model.startswith(f"{selected_provider}/"):
                model = model.split("/", 1)[1]
        elif use_chat_api:
            if api_key.startswith("sk-or-"):
                base_url = "https://openrouter.ai/api/v1"
            elif api_key.startswith("sk-"):
                base_url = "https://api.openai.com/v1"
                if model.startswith("openai/"):
                    model = model.split("/", 1)[1]
            else:
                raise WhisperError("Chat-audio transcription requires an OpenRouter or OpenAI API key.")
        else:
            if api_key.startswith("sk-or-"):
                raise WhisperError(
                    "Native Whisper transcription requires an OpenAI or Groq key; "
                    "OpenRouter does not proxy /audio/transcriptions."
                )
            if api_key.startswith("gsk_"):
                if selected_provider and selected_provider != "groq":
                    raise WhisperError(
                        f"The selected STT model uses {selected_provider}, but the saved BYOK key is for Groq."
                    )
                base_url = "https://api.groq.com/openai/v1"
                if not model or model == "whisper-1":
                    model = "whisper-large-v3"
                elif model.startswith("groq/"):
                    model = model.split("/", 1)[1]
            else:
                if selected_provider and selected_provider != "openai":
                    raise WhisperError(
                        f"The selected STT model uses {selected_provider}, but the saved BYOK key is for OpenAI."
                    )
                base_url = "https://api.openai.com/v1"
                if model.startswith("openai/"):
                    model = model.split("/", 1)[1]

    cloud_model_routing = os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"


    if (
        require_timestamps
        and str(reference_transcript or "").strip()
        and (
            api_key.startswith("sk-or-")
            or "openrouter.ai" in str(base_url or "").lower()
        )
    ):
        model = (
            os.getenv("OPENROUTER_AUDIO_TRANSCRIPTION_MODEL")
            or "google/gemini-3.1-flash-lite"
        ).strip()
        reported_model = model
        use_chat_api = True

    # Default model if still empty
    if not model:
        model = "openai/gpt-4o-audio-preview" if use_chat_api else "whisper-1"
        reported_model = model

    if not api_key:
        raise WhisperError(
            "Audio transcription is unavailable in self-hosted mode until a "
            "matching provider API key is saved for Speech-to-text."
        )

    if use_chat_api and require_timestamps and not str(reference_transcript or "").strip():
        raise WhisperTimestampError(
            "Measured subtitle alignment requires a timestamp-capable STT model; "
            "the selected chat-audio route needs the canonical transcript to return "
            "reference-aligned segment timestamps."
        )

    if use_chat_api:
        # OpenRouter path: send audio as base64 in a chat completion
        # using a model which accepts audio input parts.
        audio_b64 = base64.b64encode(blob).decode()
        # Map mime to format hint
        fmt = "wav"
        if "webm" in (mime or ""):
            fmt = "webm"
        elif "mp3" in (mime or "") or "mpeg" in (mime or ""):
            fmt = "mp3"
        elif "ogg" in (mime or ""):
            fmt = "ogg"

        prompt = "Transcribe the audio above. Return ONLY the transcript text, nothing else."
        if require_timestamps:
            prompt = (
                "The supplied audio was generated verbatim from the canonical transcript below. "
                "Return ordered, subtitle-ready segments as JSON. Copy every segment text verbatim "
                "from the canonical transcript, preserve the complete text and order, and measure "
                "each start/end time from the supplied audio. Use audible speech and pause boundaries; "
                "do not estimate timestamps from text length. Keep each segment short enough for at "
                "most two subtitle lines.\n\nCanonical transcript:\n"
                f"{str(reference_transcript or '').strip()}"
            )
        chat_body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_b64,
                                "format": fmt,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        }
        if require_timestamps:
            chat_body["temperature"] = 0
            chat_body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "timed_transcript",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "segments": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "start": {"type": "number"},
                                        "end": {"type": "number"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["start", "end", "text"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["segments"],
                        "additionalProperties": False,
                    },
                },
            }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=chat_body,
                )
        except httpx.HTTPError as exc:
            logger.warning("Audio chat transcription failed: %s", exc)
            raise WhisperError(f"Transcription provider unreachable: {exc}")

        if resp.status_code != 200:
            body = resp.text[:500]
            logger.warning("Audio chat API error %s: %s", resp.status_code, body)
            raise WhisperError(f"Audio transcription error {resp.status_code}: {body}")

        payload = resp.json()
        text = ""
        try:
            text = (payload.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except Exception:
            pass
        if require_timestamps:
            return _reference_aligned_chat_result(
                content=text,
                reference_transcript=str(reference_transcript or "").strip(),
                model=reported_model or model,
            )
        # Estimate duration from blob size (chat API doesn't return duration)
        duration = max(1.0, len(blob) / 4096)
        return WhisperResult(
            text=text,
            duration_seconds=duration,
            model=reported_model or model,
        )

    # Standard Whisper path: multipart file upload to /audio/transcriptions
    files = {"file": (filename or "audio.webm", blob, mime or "audio/webm")}
    data: dict = {
        "model": model,
        "response_format": "verbose_json",
    }
    if require_timestamps:
        data["timestamp_granularities[]"] = ["word", "segment"]
    if language:
        data["language"] = language

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
            )
    except httpx.HTTPError as exc:
        logger.warning("Whisper request failed: %s", exc)
        raise WhisperError(f"Transcription provider unreachable: {exc}")

    if resp.status_code != 200:
        body = resp.text[:500]
        logger.warning("Whisper API error %s: %s", resp.status_code, body)
        raise WhisperError(
            f"Whisper API error {resp.status_code}: {body}"
            if resp.status_code >= 500
            else f"Whisper rejected the audio: {body}"
        )

    payload = resp.json()
    text = (payload.get("text") or "").strip()
    duration = float(payload.get("duration") or 0.0)
    if duration <= 0:
        duration = max(1.0, len(blob) / 4096)

    # ``verbose_json`` includes a per-segment array with start/end timings.
    # Map it to a compact shape callers can turn into subtitle cues.
    segments: list[dict] | None = None
    raw_segments = payload.get("segments")
    if isinstance(raw_segments, list):
        segments = []
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            seg_text = (seg.get("text") or "").strip()
            segments.append(
                {
                    "start": float(seg.get("start") or 0.0),
                    "end": float(seg.get("end") or 0.0),
                    "text": seg_text,
                }
            )

    words: list[dict] | None = None
    raw_words = payload.get("words")
    if isinstance(raw_words, list):
        words = []
        for word in raw_words:
            if not isinstance(word, dict):
                continue
            word_text = str(word.get("word") or word.get("text") or "").strip()
            words.append(
                {
                    "start": float(word.get("start") or 0.0),
                    "end": float(word.get("end") or 0.0),
                    "text": word_text,
                }
            )

    if require_timestamps and not segments:
        raise WhisperTimestampError(
            "The selected timestamp-capable STT route did not return segment timestamps."
        )

    return WhisperResult(
        text=text,
        duration_seconds=duration,
        model=reported_model or model,
        segments=segments,
        words=words,
    )


def _canonical_transcript_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _reference_aligned_chat_result(
    *,
    content: str,
    reference_transcript: str,
    model: str,
) -> WhisperResult:
    raw = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WhisperTimestampError(
            "The OpenRouter audio alignment route did not return valid timestamp JSON."
        ) from exc

    raw_segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(raw_segments, list) or not raw_segments:
        raise WhisperTimestampError(
            "The OpenRouter audio alignment route returned no timestamp segments."
        )

    segments: list[dict] = []
    prior_end = 0.0
    for item in raw_segments:
        if not isinstance(item, dict):
            raise WhisperTimestampError(
                "The OpenRouter audio alignment route returned an invalid timestamp segment."
            )
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WhisperTimestampError(
                "The OpenRouter audio alignment route returned an invalid timestamp segment."
            ) from exc
        segment_text = str(item.get("text") or "").strip()
        if not segment_text or start < 0 or end <= start or start < prior_end:
            raise WhisperTimestampError(
                "The OpenRouter audio alignment route returned non-monotonic timestamp segments."
            )
        segments.append(
            {
                "start": start,
                "end": end,
                "text": segment_text,
                "timing_source": "measured_openrouter_audio_segments",
            }
        )
        prior_end = end

    aligned_text = "".join(segment["text"] for segment in segments)
    if _canonical_transcript_text(aligned_text) != _canonical_transcript_text(reference_transcript):
        raise WhisperTimestampError(
            "The OpenRouter audio alignment segments do not match the canonical transcript."
        )
    return WhisperResult(
        text=reference_transcript,
        duration_seconds=prior_end,
        model=model,
        segments=segments,
    )


def whisper_cost_usd(duration_seconds: float, model: str = "") -> float:
    """Convert audio duration to provider USD cost. Caller multiplies
    through the standard margin via ``record_media_usage``."""
    lowered = str(model or "").lower()
    per_minute = (
        _GROQ_WHISPER_USD_PER_MINUTE
        if ("groq/" in lowered or "large-v3" in lowered)
        else _WHISPER_USD_PER_MINUTE
    )
    return max(0.0, duration_seconds) / 60.0 * per_minute
