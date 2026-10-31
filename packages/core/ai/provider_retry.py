"""One classification and one retry budget for model-provider failures.

Every model call goes through ``llm_client``; this module is the policy it
applies. A provider failure is either *transient* (the same request can
succeed if sent again: no response, a dropped stream, 429, 5xx) or
*permanent* (it cannot: bad key, unknown model, invalid request, context
length, content policy). Only transient failures are retried, and only while
nothing from the failed attempt has reached the caller.

Both retry sites share the state below: the call-level loop that re-runs a
failed stream, and the request-level loop inside the buffered POST. They
draw from one attempt cap and one time budget, so the two never multiply.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, TypeVar

import httpx

#: Statuses that mean "the provider (or a proxy in front of it) could not
#: serve this request right now". 408/425 are request-timing, 429 is rate
#: limiting, 52x are Cloudflare origin failures, 529 is Anthropic overload.
TRANSIENT_HTTP_STATUSES = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)
#: Cloudflare 524 means the origin ran past the proxy's 100 s limit. A
#: buffered generation that long fails the same way every time, so it gets
#: one retry, not the whole budget.
_SINGLE_RETRY_STATUSES = frozenset({524})

#: Monotonic clock for the budget; a seam so tests can advance it with sleeps.
_clock = time.monotonic

#: Measured from the start of the first attempt, so it includes the time
#: failed attempts spent (a stalled stream runs to its deadline first). No
#: retry starts once it is spent; the call then ends within one more attempt.
DEFAULT_RETRY_BUDGET_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 30.0

# Provider error codes carried inside an SSE error frame. OpenAI-compatible
# gateways put an HTTP-like integer or a string type in ``error.code``;
# Anthropic puts the type in ``error.type``.
_TRANSIENT_ERROR_TYPES = frozenset({
    "overloaded_error", "api_error", "server_error", "rate_limit_error",
    "rate_limit_exceeded", "timeout", "timeout_error", "service_unavailable",
    "internal_server_error", "upstream_error", "provider_error",
})
_PERMANENT_ERROR_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error",
    "not_found_error", "context_length_exceeded", "content_filter",
    "content_policy_violation", "invalid_api_key", "model_not_found",
    "insufficient_quota", "billing_error", "request_too_large",
})
# An SSE error frame without a usable code is classified by its text. These
# phrases describe a request that would fail again unchanged.
_PERMANENT_MESSAGE_RE = re.compile(
    r"context[ _-]?(?:length|window)|maximum context|too many tokens|prompt is too long"
    r"|invalid[ _-](?:request|api[ _-]?key|model|argument|parameter|schema)"
    r"|does not support|not supported|unsupported"
    r"|unauthori[sz]ed|authentication|api[ _-]?key|permission|forbidden"
    r"|model[^.]{0,40}(?:not found|does not exist|unavailable for)"
    r"|not[ _-]found|content[ _-](?:policy|filter|management)|safety|moderation"
    r"|insufficient[ _-](?:quota|credits?|balance)",
    re.IGNORECASE,
)


class LLMStreamProviderError(RuntimeError):
    """The provider answered the stream with an error frame instead of chunks."""

    def __init__(self, message: str, *, code: Any = None, error_type: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.error_type = error_type


class LLMEmptyResponseError(RuntimeError):
    """The provider closed a response without content, finish reason, or usage."""


class LLMStreamStalled(TimeoutError):
    """A streaming response stopped making model progress within its deadline.

    ``phase`` names the deadline that ran out: ``first_token`` (no model
    output since the request was sent), ``idle`` (no model output since the
    last chunk; keep-alives do not count) or ``max_duration`` (the stream as
    a whole ran past its ceiling).
    """

    def __init__(self, message: str, *, phase: str, elapsed: float, bytes_received: int | None) -> None:
        super().__init__(message)
        self.phase = phase
        self.elapsed = elapsed
        self.bytes_received = bytes_received


class LLMRequestDeadlineExceeded(httpx.TimeoutException):
    """A buffered model request did not complete within its wall-clock deadline.

    httpx's own timeouts bound each socket operation, so a request body that
    uploads slowly or a response that trickles keep-alive bytes never trips
    them. This is the bound on the request as a whole; it is an
    ``httpx.TimeoutException`` so every transport-error path treats it as one.
    """


@dataclass(frozen=True)
class ProviderFailure:
    transient: bool
    #: Structured public label: ``HTTP <status>`` or ``no response``.
    label: str
    status: int | None = None
    retry_after: float | None = None
    #: Internal cause for logs (``stalled_stream``, ``request_deadline``);
    #: never part of the public label.
    reason: str | None = None


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = str(response.headers.get("Retry-After") or "").strip()
    if raw and raw.replace(".", "", 1).isdigit():
        return float(raw)
    return None


def _status_failure(status: int, retry_after: float | None = None) -> ProviderFailure:
    return ProviderFailure(
        transient=status in TRANSIENT_HTTP_STATUSES,
        label=f"HTTP {status}",
        status=status,
        retry_after=retry_after if status == 429 else None,
    )


def classify_response(response: httpx.Response) -> ProviderFailure:
    """Classify an HTTP error response before it is raised."""
    return _status_failure(response.status_code, _retry_after_seconds(response))


def _stream_error_failure(exc: LLMStreamProviderError) -> ProviderFailure:
    code = exc.code
    status: int | None = None
    if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
        status = int(code)
    if status is not None and 100 <= status <= 599:
        failure = _status_failure(status)
        # The stream itself opened with 200; the code is the provider's
        # verdict for this generation, which is what classification needs.
        return ProviderFailure(failure.transient, "no response", None, None)
    for value in (exc.error_type, code):
        kind = str(value or "").strip().lower()
        if kind in _TRANSIENT_ERROR_TYPES:
            return ProviderFailure(True, "no response")
        if kind in _PERMANENT_ERROR_TYPES:
            return ProviderFailure(False, "no response")
    if _PERMANENT_MESSAGE_RE.search(str(exc)):
        return ProviderFailure(False, "no response")
    # A mid-stream frame without a recognisable cause ("Upstream HTTP/2
    # stream failed", "Our servers are currently overloaded") is the
    # provider losing the generation, not rejecting the request.
    return ProviderFailure(True, "no response")


def classify_provider_failure(exc: BaseException) -> ProviderFailure:
    """Classify any exception raised while calling a model provider."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return _status_failure(exc.response.status_code, _retry_after_seconds(exc.response))
    if isinstance(exc, LLMStreamProviderError):
        return _stream_error_failure(exc)
    if isinstance(exc, LLMEmptyResponseError):
        return ProviderFailure(True, "no response")
    if isinstance(exc, LLMStreamStalled):
        return ProviderFailure(True, "no response", reason="stalled_stream")
    if isinstance(exc, LLMRequestDeadlineExceeded):
        return ProviderFailure(True, "no response", reason="request_deadline")
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        # A malformed base URL or a request we built wrong: sending it again
        # changes nothing.
        return ProviderFailure(False, "no response")
    if isinstance(exc, httpx.TransportError):
        # Connect/read/write/pool timeouts, refused or reset connections and
        # "peer closed connection without sending complete message body".
        return ProviderFailure(True, "no response")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        # Stream did not start, or stalled without a chunk.
        return ProviderFailure(True, "no response")
    if isinstance(exc, json.JSONDecodeError):
        # A truncated SSE frame.
        return ProviderFailure(True, "no response")
    return ProviderFailure(False, "no response")


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class ProviderRetryState:
    """Attempts and time spent on one model call, shared by both retry sites."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    budget_seconds: float = field(
        default_factory=lambda: _env_float("LLM_TRANSIENT_RETRY_BUDGET_SECONDS", DEFAULT_RETRY_BUDGET_SECONDS)
    )
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS
    #: Start of the first attempt. The budget runs from here, not from the
    #: first failure, so a stalled attempt spends it and total wall time is
    #: bounded by the budget plus one attempt's own deadline.
    started_at: float = field(default_factory=lambda: _clock())
    retries: int = 0
    #: Something from a failed attempt reached a consumer (a streamed token
    #: or a tool-call draft the stream handler does not withhold, see
    #: :func:`withhold_stream_events`). Re-running the call would show it twice, so the
    #: call-level loop stops; a buffered POST that returns nothing until it
    #: succeeds may still retry its own request.
    output_committed: bool = False
    #: Last failure the attempt returned; ``None`` once an attempt succeeds.
    failure: ProviderFailure | None = None
    _status_retries: dict[int, int] = field(default_factory=dict)

    @property
    def attempts(self) -> int:
        return self.retries + 1

    def elapsed_seconds(self) -> float:
        return max(_clock() - self.started_at, 0.0)

    def retry_delay(self, failure: ProviderFailure) -> float | None:
        """Seconds to wait before retrying, or ``None`` when this must surface."""
        if not failure.transient:
            return None
        if self.attempts >= self.max_attempts:
            return None
        if failure.status in _SINGLE_RETRY_STATUSES and self._status_retries.get(failure.status, 0) >= 1:
            return None
        if failure.retry_after is not None and failure.status not in _SINGLE_RETRY_STATUSES:
            delay = failure.retry_after
        else:
            ceiling = min(self.base_delay * (2 ** self.retries), self.max_delay)
            # Equal jitter: never retry in lockstep with other workers that
            # saw the same outage, never wait less than half the backoff.
            delay = ceiling / 2 + random.uniform(0, ceiling / 2)
        if self.elapsed_seconds() + delay > self.budget_seconds:
            return None
        return delay

    async def wait(self, delay: float, failure: ProviderFailure) -> None:
        self.retries += 1
        if failure.status is not None:
            self._status_retries[failure.status] = self._status_retries.get(failure.status, 0) + 1
        await asyncio.sleep(delay)

    def summary(self) -> str:
        attempts = self.attempts
        noun = "attempt" if attempts == 1 else "attempts"
        return f"after {attempts} {noun} over {int(round(self.elapsed_seconds()))}s"


_state: contextvars.ContextVar[Optional[ProviderRetryState]] = contextvars.ContextVar(
    "llm_provider_retry_state", default=None,
)


def current_retry_state() -> ProviderRetryState | None:
    return _state.get()


def enter_retry_state(state: ProviderRetryState) -> contextvars.Token:
    return _state.set(state)


def exit_retry_state(token: contextvars.Token) -> None:
    _state.reset(token)


def note_provider_failure(exc: BaseException) -> ProviderFailure:
    """Record the failure an attempt is about to return, for the call-level loop."""
    failure = classify_provider_failure(exc)
    state = _state.get()
    if state is not None:
        state.failure = failure
    return failure


def mark_output_committed() -> None:
    state = _state.get()
    if state is not None:
        state.output_committed = True


#: Stream event names a handler declares it shows no one while the call is in
#: flight. ``ALL_STREAM_EVENTS`` covers every event.
_WITHHELD_STREAM_EVENTS_ATTR = "withheld_stream_events"
ALL_STREAM_EVENTS = "*"

_Handler = TypeVar("_Handler", bound=Callable[..., Any])


def withhold_stream_events(handler: _Handler, events: Iterable[str] | None = None) -> _Handler:
    """Declare that ``handler`` shows no one ``events`` (every event when ``None``).

    Output is committed only once it reaches a consumer. A handler that only
    exists so the call streams (and gets streaming deadlines), or one that
    holds text until the round is confirmed and clears it on ``text_reset``,
    receives tokens nobody saw: a transient mid-stream failure must stay
    retryable, because re-running the call shows nothing twice.
    """
    withheld = frozenset({ALL_STREAM_EVENTS}) if events is None else frozenset(events)
    setattr(handler, _WITHHELD_STREAM_EVENTS_ATTR, withheld)
    return handler


def withheld_stream_events(handler: Any) -> frozenset[str]:
    """The events ``handler`` declared it withholds (see :func:`withhold_stream_events`)."""
    return frozenset(getattr(handler, _WITHHELD_STREAM_EVENTS_ATTR, None) or ())


def stream_event_reaches_consumer(handler: Any, event_name: str) -> bool:
    """Whether ``event_name`` sent to ``handler`` is output someone has seen."""
    if handler is None:
        return False
    withheld = withheld_stream_events(handler)
    return ALL_STREAM_EVENTS not in withheld and event_name not in withheld
