"""Stable identities for integrations supported by the Stats library.

Integration rows may use provider aliases (for example ``x`` or ``twitter``),
but Stats definitions must not depend on those storage-layer spellings.  A
``StatIntegrationKey`` is the portable identity written into collector config,
Blueprints, and API responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Optional

from packages.core.integrations.registry import (
    canonical_integration_key,
    get_integration_spec,
    integration_key_aliases,
)


@unique
class StatIntegrationKey(StrEnum):
    TWITTER_X = "twitter_x"


@dataclass(frozen=True)
class StatIntegrationSpec:
    key: StatIntegrationKey
    provider_key: str
    measurer_key: str


def get_stat_integration_spec(
    key: StatIntegrationKey | object,
) -> Optional[StatIntegrationSpec]:
    try:
        normalized = key if isinstance(key, StatIntegrationKey) else StatIntegrationKey(str(key))
    except (TypeError, ValueError):
        return None
    spec = get_integration_spec(normalized.value)
    if spec is None or spec.stat_measurer_key is None:
        return None
    return StatIntegrationSpec(
        key=normalized,
        provider_key=spec.key,
        measurer_key=spec.stat_measurer_key,
    )


def stat_integration_key_for_provider(provider: object) -> Optional[StatIntegrationKey]:
    try:
        key = StatIntegrationKey(canonical_integration_key(provider))
    except ValueError:
        return None
    return key if get_stat_integration_spec(key) else None


def provider_aliases_for_stat_integration(
    key: StatIntegrationKey | object,
) -> set[str]:
    spec = get_stat_integration_spec(key)
    return integration_key_aliases(spec.provider_key) if spec else set()
