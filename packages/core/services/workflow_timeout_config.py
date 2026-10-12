"""Workflow node timeout configuration contract."""
from __future__ import annotations

import math
from typing import Any


_TIMEOUT_ERROR = "config.timeout must be a finite positive number or null"


def resolve_workflow_node_timeout(config: dict[str, Any]) -> float | None:
    """Return the optional outer node wall-clock timeout.

    Missing and null values intentionally leave node execution unbounded; node
    implementations retain their own request and sandbox limits.
    """
    value = config.get("timeout")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_TIMEOUT_ERROR)
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise ValueError(_TIMEOUT_ERROR) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(_TIMEOUT_ERROR)
    return timeout


def resolve_workflow_http_request_timeout(config: dict[str, Any]) -> float:
    """Keep HTTP's request-level default when no outer node timeout is set."""
    return resolve_workflow_node_timeout(config) or 30.0
