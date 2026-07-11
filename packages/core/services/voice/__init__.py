"""Voice streaming support for Twilio Media Streams.

Decomposition:
  - stt.py      : pluggable speech-to-text engines (Deepgram reference impl)
  - tts.py      : pluggable text-to-speech engines (OpenAI reference impl)
  - session.py  : TwilioVoiceSession — owns one Media Streams websocket, runs
                  the turn-taking loop (listen → transcribe → agent → reply),
                  handles the 10s "still working" hold message

Everything else in the channel gateway (Conversation upsert, Celery dispatch,
ChannelContact dedup) stays the same. This package just handles the audio
transport + realtime turn-taking that text channels don't need.
"""
from __future__ import annotations

from packages.core.services.voice.session import TwilioVoiceSession
from packages.core.services.voice.stt import STTEngine, get_stt_engine
from packages.core.services.voice.tts import TTSEngine, get_tts_engine

__all__ = [
    "TwilioVoiceSession",
    "STTEngine", "get_stt_engine",
    "TTSEngine", "get_tts_engine",
]
