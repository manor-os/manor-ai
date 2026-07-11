"""Per-provider metric extractors.

Each measurer is a coroutine ``measure(integration, params, metric_key)
-> Decimal`` registered under a provider key. The measurement service
looks up the registry and dispatches; adding a new provider means a
new file + one ``register()`` call.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Awaitable, Callable, Protocol

from packages.core.models.document import Integration


class Measurer(Protocol):
    async def __call__(
        self,
        integration: Integration,
        params: dict,
        metric_key: str,
    ) -> Decimal: ...


_REGISTRY: dict[str, Measurer] = {}


def register(provider_key: str, fn: Measurer) -> None:
    _REGISTRY[provider_key] = fn


def get(provider_key: str) -> Measurer | None:
    return _REGISTRY.get(provider_key)


def supported_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


# Auto-register built-in measurers on import.
from packages.core.goals.measurers import twitter_x as _twitter_x  # noqa: F401
