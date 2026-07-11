"""Temporal client singleton + feature-flag helpers.

Lazy-imports temporalio so the rest of Manor stays runnable even when
temporalio isn't installed (it's a heavy dep). ``is_temporal_enabled()``
is the canonical check — call sites use it to decide whether to take
the Temporal path or fall back to Celery PlanExecutor.
"""
from __future__ import annotations

import logging
from typing import Optional

from packages.core.config import get_settings

logger = logging.getLogger(__name__)


_client: Optional["object"] = None
"""Cached temporalio.client.Client. Type-annotated as object to avoid
importing temporalio at module load time (heavy dep, optional)."""


def is_temporal_enabled() -> bool:
    """True when ``TEMPORAL_ENABLED=true`` env var is set AND
    temporalio is importable. Safe to call from any code path; never
    raises."""
    if not get_settings().TEMPORAL_ENABLED:
        return False
    try:
        import temporalio  # noqa: F401
    except ImportError:
        logger.warning(
            "TEMPORAL_ENABLED=true but temporalio not installed — "
            "falling back to Celery PlanExecutor"
        )
        return False
    return True


async def get_temporal_client():
    """Return a connected temporalio Client, or None when disabled.

    The same Client instance is reused across calls. Reconnecting per
    request would defeat the connection pooling Temporal does
    internally."""
    global _client
    if not is_temporal_enabled():
        return None
    if _client is not None:
        return _client

    from temporalio.client import Client, TLSConfig

    settings = get_settings()
    tls = TLSConfig() if settings.TEMPORAL_TLS else False
    _client = await Client.connect(
        settings.TEMPORAL_HOST,
        namespace=settings.TEMPORAL_NAMESPACE,
        tls=tls,
    )
    logger.info(
        "Temporal client connected: host=%s namespace=%s",
        settings.TEMPORAL_HOST, settings.TEMPORAL_NAMESPACE,
    )
    return _client


def workflow_id_for_plan(plan_id: str) -> str:
    """Stable, deterministic workflow id derived from the plan id.

    Lets us reuse the same workflow id on retries (Temporal's reuse
    policy can be ALLOW_DUPLICATE_FAILED_ONLY) without coordination."""
    return f"plan-{plan_id}"


def reset_for_test(client_override=None) -> None:
    """Test helper — inject a Client (e.g. WorkflowEnvironment.client)
    or clear the cache."""
    global _client
    _client = client_override
