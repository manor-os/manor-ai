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
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Cap upload size — OpenAI's hard limit is 25 MB.
WHISPER_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# $0.006 per minute of audio.
_WHISPER_USD_PER_MINUTE = 0.006


class WhisperError(Exception):
    """Raised when the Whisper request fails for a known reason
    (missing key, oversized input, upstream HTTP error). The message
    is user-safe — callers can surface it directly."""


@dataclass
class WhisperResult:
    text: str
    duration_seconds: float
    model: str


async def transcribe_blob(
    blob: bytes,
    *,
    mime: str,
    filename: str = "audio.webm",
    language: Optional[str] = None,
    user_api_key: Optional[str] = None,
    resolved_model: Optional[str] = None,
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
        raise WhisperError(
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

    # Detect which API path to use based on the model ID
    # Chat-based models (gpt-4o-audio, gpt-audio-mini) use /chat/completions
    # Whisper models use /audio/transcriptions
    _CHAT_AUDIO_MODELS = {"openai/gpt-4o-audio-preview", "openai/gpt-audio-mini", "openai/gpt-audio"}
    use_chat_api = model in _CHAT_AUDIO_MODELS

    api_key = ""
    base_url = ""

    # 1. BYOK
    if user_api_key and user_api_key.strip():
        api_key = user_api_key.strip()
        if use_chat_api:
            if api_key.startswith("sk-or-"):
                base_url = "https://openrouter.ai/api/v1"
            elif api_key.startswith("sk-"):
                base_url = "https://api.openai.com/v1"
                if model.startswith("openai/"):
                    model = model.split("/", 1)[1]
            else:
                raise WhisperError("Chat-audio transcription requires an OpenRouter or OpenAI API key.")
        else:
            if api_key.startswith("gsk_"):
                base_url = "https://api.groq.com/openai/v1"
                if not model or model == "whisper-1":
                    model = "whisper-large-v3"
            else:
                base_url = "https://api.openai.com/v1"

    cloud_model_routing = os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"


    # Default model if still empty
    if not model:
        model = "openai/gpt-4o-audio-preview" if use_chat_api else "whisper-1"

    if not api_key:
        raise WhisperError(
            "Audio transcription is unavailable in self-hosted mode until a "
            "matching provider API key is saved for Speech-to-text."
        )

    if use_chat_api:
        # OpenRouter path: send audio as base64 in a chat completion
        # using gpt-4o-audio-preview which accepts audio input parts.
        import base64
        audio_b64 = base64.b64encode(blob).decode()
        # Map mime to format hint
        fmt = "wav"
        if "webm" in (mime or ""):
            fmt = "webm"
        elif "mp3" in (mime or "") or "mpeg" in (mime or ""):
            fmt = "mp3"
        elif "ogg" in (mime or ""):
            fmt = "ogg"

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
                            "text": "Transcribe the audio above. Return ONLY the transcript text, nothing else.",
                        },
                    ],
                }
            ],
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
        # Estimate duration from blob size (chat API doesn't return duration)
        duration = max(1.0, len(blob) / 4096)
        return WhisperResult(text=text, duration_seconds=duration, model=model)

    # Standard Whisper path: multipart file upload to /audio/transcriptions
    files = {"file": (filename or "audio.webm", blob, mime or "audio/webm")}
    data: dict = {
        "model": model,
        "response_format": "verbose_json",
    }
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

    return WhisperResult(text=text, duration_seconds=duration, model=model)


def whisper_cost_usd(duration_seconds: float) -> float:
    """Convert audio duration to provider USD cost. Caller multiplies
    through the standard margin via ``record_media_usage``."""
    return max(0.0, duration_seconds) / 60.0 * _WHISPER_USD_PER_MINUTE
