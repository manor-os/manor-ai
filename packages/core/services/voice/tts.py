"""Text-to-speech engines for Twilio Media Streams.

Twilio expects μ-law 8 kHz audio going the other way too. Most TTS APIs
return MP3 or WAV — this module transcodes to mulaw 8 kHz via audioop
(stdlib, lossless for this conversion).

Reference implementation: OpenAI's /audio/speech endpoint (cheap, low
latency, good voices). Swap via STT_PROVIDER-style env var or
``register_tts_engine``.
"""
from __future__ import annotations

import abc
import asyncio
import io
import logging
import os
import wave
from typing import Callable, Optional

try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+ removed audioop from stdlib.
    audioop = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class TTSEngine(abc.ABC):
    """Non-streaming TTS. Given text, return μ-law 8 kHz bytes ready to
    feed into Twilio Media Streams.

    Streaming TTS is a later optimisation — first goal is correctness.
    """

    @abc.abstractmethod
    async def synthesize(self, text: str) -> bytes:
        ...


# ── Helpers ────────────────────────────────────────────────────────────────

def _require_audioop():
    if audioop is None:
        raise RuntimeError(
            "TTS audio transcoding requires audioop, which is not available in this Python runtime"
        )
    return audioop


def pcm16_to_mulaw(pcm: bytes, rate_from: int) -> bytes:
    """Convert 16-bit PCM at ``rate_from`` Hz to μ-law 8 kHz for Twilio."""
    audio = _require_audioop()
    if rate_from != 8000:
        pcm, _ = audio.ratecv(pcm, 2, 1, rate_from, 8000, None)
    return audio.lin2ulaw(pcm, 2)


def wav_to_mulaw_8k(wav_bytes: bytes) -> bytes:
    """Decode a WAV container to μ-law 8 kHz mono."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    audio = _require_audioop()
    if sample_width != 2:
        pcm = audio.lin2lin(pcm, sample_width, 2)
    if n_channels == 2:
        pcm = audio.tomono(pcm, 2, 0.5, 0.5)
    return pcm16_to_mulaw(pcm, rate)


# ── OpenAI reference implementation ────────────────────────────────────────

class OpenAITTS(TTSEngine):
    """OpenAI /audio/speech — fast and good-quality. Requests WAV so the
    resample path is deterministic; MP3 would save bandwidth but force
    a heavier dependency."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "tts-1",
        voice: str = "alloy",
    ):
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url or os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1",
        )
        self._model = model
        self._voice = voice

    async def synthesize(self, text: str) -> bytes:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY not set — can't synthesize speech")
        try:
            from packages.core.ai.runtime import runtime_current_billing_context
            from packages.core.ai.runtime import runtime_assert_credit_available
            billing = runtime_current_billing_context()
            if billing and not billing.suppress and not billing.byok:
                await runtime_assert_credit_available(billing.entity_id, source=billing.source or "tts")
        except Exception:
            # If billing context isn't available, keep current behaviour.
            pass
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("httpx required — pip install httpx") from e

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self._base_url.rstrip('/')}/audio/speech",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "voice": self._voice,
                    "input": text,
                    "response_format": "wav",
                },
            )
        if not resp.is_success:
            raise RuntimeError(f"TTS API error {resp.status_code}: {resp.text[:200]}")

        # Bill against the active LLM billing context. OpenAI charges
        # per character: tts-1 = $15/M chars, tts-1-hd = $30/M chars.
        await _bill_tts(model=self._model, char_count=len(text))

        # Offload the sync audioop transcode to a thread
        return await asyncio.to_thread(wav_to_mulaw_8k, resp.content)


# ── Factory ─────────────────────────────────────────────────────────────────

_FACTORY: dict[str, Callable[..., TTSEngine]] = {
    "openai": lambda **kw: OpenAITTS(**kw),
}


def register_tts_engine(name: str, factory: Callable[..., TTSEngine]) -> None:
    _FACTORY[name.lower()] = factory


async def resolve_voice_model(entity_id: Optional[str]) -> Optional[str]:
    """Look up the entity's chosen voice model from Account → AI Models.
    Returns the bare model id with any provider prefix stripped
    (``openai/tts-1`` → ``tts-1`` for the OpenAI ``/audio/speech``
    endpoint), or ``None`` if no preference / lookup failed — caller
    should treat that as "use the engine's default".
    """
    if not entity_id:
        return None
    try:
        from packages.core.services.model_resolver import resolve_model_for_user
        picked = await resolve_model_for_user(
            "voice", user_id=None, entity_id=entity_id,
        )
        if not picked:
            return None
        return picked.split("/", 1)[1] if "/" in picked else picked
    except Exception:
        return None


def get_tts_engine(
    name: Optional[str] = None, *, model: Optional[str] = None,
) -> TTSEngine:
    """Build a TTS engine, optionally pinning the model.

    Pass ``model`` when you've already resolved the entity's preference
    via ``resolve_voice_model()``; the engine falls back to its own
    default when this is ``None``.
    """
    provider = (name or os.getenv("TTS_PROVIDER", "openai")).lower()
    factory = _FACTORY.get(provider)
    if not factory:
        raise RuntimeError(
            f"No TTS engine registered under '{provider}'. "
            f"Available: {list(_FACTORY)}"
        )
    return factory(**({"model": model} if model else {}))


# ── Billing ──────────────────────────────────────────────────────────

_TTS_PRICING_PER_M_CHARS = {
    "tts-1": 15.0,
    "tts-1-hd": 30.0,
}


async def _bill_tts(*, model: str, char_count: int) -> None:
    """Record one TTS synthesis against the active billing context.
    Skips silently when no billing context is set."""
    if char_count <= 0:
        return
    try:
        from packages.core.ai.runtime import runtime_current_billing_context
        billing = runtime_current_billing_context()
    except Exception:
        billing = None
    if billing is None or billing.suppress:
        return

    rate = _TTS_PRICING_PER_M_CHARS.get(model, 15.0)
    cost_usd = char_count / 1_000_000 * rate
    if cost_usd <= 0:
        return

    try:
        from packages.core.database import async_session
        from packages.core.services.usage_service import record_media_usage
        async with async_session() as db:
            await record_media_usage(
                db,
                entity_id=billing.entity_id,
                kind="tts",
                model=model,
                cost_usd=cost_usd,
                units=char_count,
                workspace_id=billing.workspace_id,
                user_id=billing.user_id,
                agent_id=billing.agent_id,
                conversation_id=billing.conversation_id,
                source=billing.source or "tts",
                byok=billing.byok,
            )
            await db.commit()
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "tts billing failed (best-effort)", exc_info=True,
        )
