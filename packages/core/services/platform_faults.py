"""Report a platform fault no customer can resolve.

The one public entry point for "tell the operators". Self-hosted deployments
log it (the operator reads the server logs); Manor cloud also emails the ops
recipients through the cloud-only ``ops_alerts`` module. Callers never import
``ops_alerts`` directly, so public code stays free of cloud-only imports.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def report_platform_fault(
    *, target: str, code: str, summary: str, context: dict[str, Any] | None = None,
) -> bool:
    """Returns whether a report was sent beyond the log. Never raises."""
    logger.warning(
        "platform_fault target=%s code=%s summary=%s context=%s",
        target, code, summary[:500], context or {},
    )
    return False
