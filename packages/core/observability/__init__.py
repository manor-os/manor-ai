"""Observability — OpenTelemetry tracing for Manor's hot paths.

Designed to be **truly optional**. If ``OTEL_ENABLED`` is not set, the
helpers in this package short-circuit to no-ops and the otel SDK is
never imported — so the production binary stays lean for users who
don't run a collector.

Usage at process boot:

    from packages.core.observability import init_tracing
    init_tracing(service_name="manor-api")

Usage in hot paths:

    from packages.core.observability import span

    async with span("dispatcher.checkout", attributes={"worker_id": w.id}):
        ...

The span helper falls through transparently when tracing is disabled,
so callers don't branch.
"""
from packages.core.observability.tracing import (
    init_tracing,
    is_enabled,
    shutdown_tracing,
    span,
    trace_function,
)

__all__ = [
    "init_tracing",
    "is_enabled",
    "shutdown_tracing",
    "span",
    "trace_function",
]
