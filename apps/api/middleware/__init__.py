"""Middleware package — re-exports setup_middleware from the core module.

The core middleware (request ID, logging, global rate limiting, error handlers)
lives in ``middleware_core.py``.  Sub-modules in this package add specialised
middleware (e.g. chat rate limiting).

``from apps.api.middleware import setup_middleware`` continues to work.
"""
from __future__ import annotations

from fastapi import FastAPI

from apps.api.middleware_core import (  # noqa: F401
    request_id_var,
    setup_middleware as _setup_core_middleware,
)
from apps.api.middleware.degraded import DegradedModeMiddleware
from apps.api.middleware.rate_limit import ChatRateLimitMiddleware


def setup_middleware(app: FastAPI) -> None:
    """Register all middleware on the app (called from main.py)."""
    # Core middleware (request ID, logging, global rate limit, error handlers)
    _setup_core_middleware(app)

    # Chat-specific rate limiter (runs after global rate limit)
    app.add_middleware(ChatRateLimitMiddleware)

    # Emergency high-cost route shedding. Added last so it runs first and can
    # short-circuit before auth/DB work when an operator enables DEGRADED_MODE.
    app.add_middleware(DegradedModeMiddleware)
