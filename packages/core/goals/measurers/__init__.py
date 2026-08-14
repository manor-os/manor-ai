"""Per-provider metric extractors.

Each measurer is a coroutine ``measure(integration, params, metric_key)
-> Decimal`` registered under a provider key. The measurement service
looks up the registry and dispatches; adding a new provider means a
new file + one ``register()`` call.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from packages.core.integrations.registry import (
    integration_specs,
    register_stat_measurer,
    stat_measurer_for,
)
from packages.core.models.document import Integration


class Measurer(Protocol):
    async def __call__(
        self,
        integration: Integration,
        params: dict,
        metric_key: str,
    ) -> Decimal: ...


def register(
    provider_key: str,
    fn: Measurer,
    *,
    metric_keys: set[str] | frozenset[str] = frozenset(),
) -> None:
    register_stat_measurer(provider_key, fn, metric_keys=metric_keys)


def get(provider_key: str) -> Measurer | None:
    return stat_measurer_for(provider_key)  # type: ignore[return-value]


def supported_providers() -> list[str]:
    return [spec.key for spec in integration_specs() if spec.stat_measurer is not None]


# Auto-register built-in measurers on import.
from packages.core.goals.measurers import twitter_x as _twitter_x  # noqa: E402,F401
