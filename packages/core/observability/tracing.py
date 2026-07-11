"""OpenTelemetry tracing — opt-in, lazy.

Toggled by ``OTEL_ENABLED=true``. Endpoint defaults to
``OTEL_EXPORTER_OTLP_ENDPOINT`` (the standard env var) or to
``http://localhost:4318`` (the Jaeger all-in-one OTLP/HTTP port that
ships with the M11 docker-compose profile).

We deliberately do NOT pull in the auto-instrumentation packages
(opentelemetry-instrumentation-fastapi, etc.) because:

  * they monkey-patch on import, which conflicts with our test isolation
  * the value of "every HTTP request gets a span" is low compared to
    the value of hand-picked spans on Dispatcher / lease flow / worker
    heartbeats — those are the paths an operator actually debugs
  * keeps the dep list short

Hand-rolled spans live in the call sites under ``async with span(...)``.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Module state ──────────────────────────────────────────────────────

_INITIALIZED = False
_TRACER: Optional[Any] = None
_PROVIDER: Optional[Any] = None


def is_enabled() -> bool:
    """True after a successful ``init_tracing`` call. Cheap — call from
    hot paths instead of re-reading env."""
    return _TRACER is not None


# ── Public API ────────────────────────────────────────────────────────

def init_tracing(
    service_name: str,
    *,
    endpoint: Optional[str] = None,
    sample_rate: Optional[float] = None,
) -> bool:
    """Wire up OTLP/HTTP export. Idempotent.

    Returns True if tracing is now active, False if disabled (env not
    set, SDK not installed, or init failed). Failures log a warning
    and fall back to no-op so a misconfigured collector doesn't take
    Manor down.
    """
    global _INITIALIZED, _TRACER, _PROVIDER
    if _INITIALIZED:
        return _TRACER is not None
    _INITIALIZED = True

    if os.getenv("OTEL_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry SDK not installed (%s) — "
            "tracing disabled. Install with: pip install manor[otel]",
            exc,
        )
        return False

    try:
        resource = Resource.create({"service.name": service_name})
        if sample_rate is None:
            sample_rate = float(os.getenv("OTEL_SAMPLE_RATE", "1.0"))
        sampler = ParentBased(TraceIdRatioBased(sample_rate))
        provider = TracerProvider(resource=resource, sampler=sampler)

        ep = endpoint or os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        )
        # OTLP/HTTP traces endpoint conventionally suffixes /v1/traces.
        traces_url = (
            ep.rstrip("/") + "/v1/traces"
            if not ep.endswith("/v1/traces")
            else ep
        )
        exporter = OTLPSpanExporter(endpoint=traces_url)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _PROVIDER = provider
        _TRACER = trace.get_tracer("manor")
        logger.info(
            "OTEL tracing initialised: service=%s endpoint=%s sample=%.2f",
            service_name, traces_url, sample_rate,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — init must never crash boot
        logger.warning("OTEL init failed: %s — falling back to no-op", exc)
        _TRACER = None
        return False


def shutdown_tracing() -> None:
    """Flush and tear down. Call from process-shutdown hooks so spans
    aren't dropped on the ground."""
    global _TRACER, _PROVIDER, _INITIALIZED
    if _PROVIDER is not None:
        try:
            _PROVIDER.shutdown()
        except Exception:
            logger.warning("OTEL shutdown failed", exc_info=True)
    _TRACER = None
    _PROVIDER = None
    _INITIALIZED = False


@contextlib.asynccontextmanager
async def span(
    name: str,
    *,
    attributes: Optional[dict[str, Any]] = None,
) -> AsyncIterator[Any]:
    """Async context manager — wraps the block in a trace span.

    No-op when tracing is disabled. Exceptions inside the block are
    recorded on the span before re-raising, so a Jaeger trace shows
    where the failure originated.
    """
    if _TRACER is None:
        yield None
        return

    with _TRACER.start_as_current_span(name) as sp:
        if attributes:
            for k, v in attributes.items():
                if v is None:
                    continue
                # OTEL only accepts str/bool/int/float/sequence; coerce.
                sp.set_attribute(k, _coerce(v))
        try:
            yield sp
        except Exception as exc:  # noqa: BLE001
            try:
                sp.record_exception(exc)
                sp.set_status(_error_status(exc))
            except Exception:
                pass
            raise


def trace_function(name: Optional[str] = None) -> Callable[
    [Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]],
]:
    """Decorator — wrap an async function in a span. Equivalent to
    ``async with span(name): return await fn(...)``."""

    def _wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            async with span(span_name):
                return await fn(*args, **kwargs)

        return inner

    return _wrap


# ── Internals ────────────────────────────────────────────────────────

def _coerce(v: Any) -> Any:
    """OTEL attributes accept primitives + flat sequences only. Stringify
    anything else so set_attribute doesn't reject the whole batch."""
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)) and all(
        isinstance(x, (str, bool, int, float)) for x in v
    ):
        return list(v)
    return str(v)


def _error_status(exc: BaseException) -> Any:
    """Build a Status(StatusCode.ERROR, msg) without forcing the SDK
    import in the no-op path."""
    from opentelemetry.trace import Status, StatusCode
    return Status(StatusCode.ERROR, str(exc))
