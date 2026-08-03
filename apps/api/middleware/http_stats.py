"""Per-request HTTP traffic counting — Redis hash increments only.

Every API request bumps one field in the current UTC hour's Redis hash
(key/field vocabulary owned by ``packages.core.services.http_stats``;
the ``metrics.http_flush`` Celery task syncs the hashes into the
``http_request_hourly`` table every 5 minutes). Three hard rules:

* NO per-request DB writes — one HINCRBY (+ EXPIRE) per request, done
  as a fire-and-forget asyncio task so the response never waits on
  Redis at all.
* Fail-open — any Redis error is swallowed and logged at *debug* (this
  fires per-request; a Redis outage must not spam warnings, let alone
  fail requests).
* Bounded cardinality — the field records the matched route *template*
  (``/items/{id}``), never the raw path; requests that matched no route
  (404 scans etc.) collapse into the single ``unmatched`` path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from packages.core.services.http_stats import (
    STATS_KEY_TTL_SECONDS,
    UNMATCHED_PATH,
    hour_key,
    stats_field,
)

logger = logging.getLogger(__name__)

# Same exclusion as middleware_core's _HEALTH_PATHS: infra probes fire
# every few seconds and would drown the counters without telling admins
# anything. There are no /metrics scrape endpoints or static mounts in
# this app (checked main.py), and the admin API is deliberately NOT
# excluded — admins want to see their own traffic too.
_EXCLUDED_PATHS = {"/health", "/health/"}

# Keep strong references to in-flight increment tasks — asyncio only
# holds weak refs to tasks, so a fire-and-forget task with no reference
# can be garbage-collected mid-flight.
_pending: set[asyncio.Task] = set()

# Redis-outage backoff. This middleware is the one per-request caller of
# the shared Redis client, and cache._get_redis logs a WARNING + does a
# fresh connect/ping every time Redis is unreachable — without a
# short-circuit, an outage would produce warning volume (and connect
# attempts) proportional to traffic. After any increment failure we skip
# scheduling entirely for _BACKOFF_SECONDS, then probe again with a
# single request's increment. Losing counter increments during a Redis
# outage is inherent to the design (the counters LIVE in Redis).
_BACKOFF_SECONDS = 30.0
_backoff_until: float = 0.0

# Belt to the backoff's suspenders: a black-holed Redis (connection that
# hangs rather than errors — the shared client has no socket timeout)
# would leave tasks parked in _pending until the first one fails and
# arms the backoff. Never let the set grow past this, whatever state
# Redis is in.
_MAX_PENDING = 1000


async def _increment(key: str, field: str) -> None:
    """One HINCRBY + EXPIRE against the shared async Redis client
    (``packages.core.cache`` — the same client/loop-affinity handling
    the rest of the API process uses). EXPIRE on every increment costs
    one extra Redis op but needs no per-process bookkeeping; the TTL
    only has to be set once per key for correctness, so re-setting it
    is harmlessly idempotent."""
    global _backoff_until
    try:
        from packages.core.cache import _get_redis

        r = await _get_redis()
        if r is None:
            # cache._get_redis already logged its warning; arm the
            # backoff so the next N seconds of requests don't repeat it.
            _backoff_until = time.monotonic() + _BACKOFF_SECONDS
            return
        await r.hincrby(key, field, 1)
        await r.expire(key, STATS_KEY_TTL_SECONDS)
    except Exception:
        _backoff_until = time.monotonic() + _BACKOFF_SECONDS
        logger.debug("http stats increment failed for %s %s", key, field, exc_info=True)


def _schedule_increment(key: str, field: str) -> None:
    """Fire-and-forget seam — tests monkeypatch this to capture calls."""
    if time.monotonic() < _backoff_until:
        return
    if len(_pending) > _MAX_PENDING:
        logger.debug("http stats increment skipped: %d tasks pending", len(_pending))
        return
    task = asyncio.create_task(_increment(key, field))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


class HttpStatsMiddleware(BaseHTTPMiddleware):
    """Count every request per (hour, method, route template, status class)."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            # Crash-500s: the catch-all generic_error_handler is mounted on
            # Starlette's ServerErrorMiddleware, which sits OUTSIDE the user
            # middleware stack — an uncaught route exception propagates
            # straight through here with no response object at all. Count it
            # as a 5xx (under its matched template — routing DID happen
            # before the endpoint raised), then re-raise unchanged so the
            # outer handler still produces the client's 500.
            self._record(request, 500)
            raise
        self._record(request, response.status_code)
        return response

    @staticmethod
    def _record(request: Request, status_code: int) -> None:
        try:
            if request.url.path in _EXCLUDED_PATHS:
                return
            # scope["route"] is set by the router AFTER matching, and the
            # scope dict is shared, so it's visible here on the way back
            # out. None means routing never matched (404 scans,
            # short-circuited rate-limit/degraded responses) — all
            # collapsed into one "unmatched" field instead of exploding
            # cardinality with raw scan paths. Streaming responses are
            # counted like any other: the status line exists as soon as
            # call_next returns, and the response object is not touched.
            route = request.scope.get("route")
            template = getattr(route, "path", None) or UNMATCHED_PATH
            _schedule_increment(
                hour_key(),
                stats_field(request.method, template, status_code),
            )
        except Exception:
            # Counting must never fail a request.
            logger.debug("http stats scheduling failed", exc_info=True)
