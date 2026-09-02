"""Cancellation boundary for shared speech I/O owned by a live call."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class SpeechRequestState:
    submitted: bool = False
    stopped: bool = False


_current_request: ContextVar[SpeechRequestState | None] = ContextVar("voice_speech_request", default=None)


@contextmanager
def speech_request_scope(state: SpeechRequestState):
    token = _current_request.set(state)
    try:
        yield
    finally:
        _current_request.reset(token)


def begin_speech_provider_request():
    """Call immediately before each HTTP submission, including retries.

    Other audio callers have no live-call scope and retain their usual behavior.
    CancelledError intentionally bypasses provider fallback/error handlers.
    """
    state = _current_request.get()
    if state is not None:
        if state.stopped:
            raise asyncio.CancelledError("Voice call ended before this provider request")
        state.submitted = True
