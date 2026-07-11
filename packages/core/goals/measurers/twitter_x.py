"""Twitter / X measurer.

Maps the canonical Goal metric_keys we care about onto the existing
twitter_x MCP adapter calls. Only ``followers_count`` is needed for the
Demo A v0 sprint; others added as new goals demand them.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from packages.core.ai.mcp import twitter_x as _adapter
from packages.core.credentials import Requester, get_credential_service
from packages.core.goals.measurers import register
from packages.core.models.document import Integration

logger = logging.getLogger(__name__)


_PROFILE_METRICS = {
    "followers_count",
    "following_count",
    "tweet_count",
    "listed_count",
}


async def measure(
    integration: Integration,
    params: dict,
    metric_key: str,
) -> Decimal:
    """Resolve credentials, call adapter, extract requested metric.

    Raises ValueError on missing creds or unsupported metric_key.
    """
    if metric_key in _PROFILE_METRICS:
        return await _measure_profile_metric(integration, metric_key)
    raise ValueError(
        f"twitter_x measurer doesn't know metric_key={metric_key!r}; "
        f"supported: {sorted(_PROFILE_METRICS)}"
    )


async def _measure_profile_metric(integration: Integration, metric_key: str) -> Decimal:
    creds = get_credential_service().lease_integration(
        integration,
        requester=Requester(kind="goal_measurement", id=integration.id),
        reason=f"measure:twitter_x.{metric_key}",
    )
    token = creds.get("access_token") or creds.get("bearer_token")
    if not token:
        raise ValueError(
            "twitter_x integration credentials missing access_token / bearer_token"
        )

    raw = await _adapter._get_me(token, {})
    payload = json.loads(raw)

    # X API v2: {"data": {"id": ..., "public_metrics": {"followers_count": N, ...}}}
    data = payload.get("data") or {}
    metrics = data.get("public_metrics") or {}
    if metric_key not in metrics:
        raise ValueError(
            f"twitter_x get_me response missing public_metrics.{metric_key}"
        )
    return Decimal(str(metrics[metric_key]))


register("twitter_x", measure)
