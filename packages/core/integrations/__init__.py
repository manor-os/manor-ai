"""Integration identities, capabilities, and external-system bridges.

The central registry gives every integration one canonical string key while
remaining extensible for dynamically installed providers.

``sessions/`` contains browser-session capture for sites with no usable API.
"""

from packages.core.integrations.registry import (
    IntegrationSpec,
    canonical_integration_key,
    get_integration_spec,
    integration_key_aliases,
)

__all__ = [
    "IntegrationSpec",
    "canonical_integration_key",
    "get_integration_spec",
    "integration_key_aliases",
]
