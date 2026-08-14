"""Provider key normalization helpers shared by integrations/workspaces.

Most MCP servers use their canonical ``server_key`` as the provider key
(``twitter_x``, ``linkedin``, ...), but OAuth bridges can surface vendor
keys such as ``twitter`` or ``x``. Keep those aliases in one place so
setup resolution, capability displays, and runtime credential lookup agree.
"""
from __future__ import annotations

from packages.core.integrations.registry import (
    canonical_integration_key,
    integration_key_aliases,
    normalize_integration_key,
)


def normalize_provider_key(provider: object) -> str:
    return normalize_integration_key(provider)


def canonical_provider_key(provider: object) -> str:
    return canonical_integration_key(provider)


def provider_key_aliases(provider: object) -> set[str]:
    return integration_key_aliases(provider)


def provider_keys_match(left: object, right: object) -> bool:
    return canonical_provider_key(left) == canonical_provider_key(right)
