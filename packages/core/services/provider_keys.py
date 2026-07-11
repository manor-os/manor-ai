"""Provider key normalization helpers shared by integrations/workspaces.

Most MCP servers use their canonical ``server_key`` as the provider key
(``twitter_x``, ``linkedin``, ...), but OAuth bridges can surface vendor
keys such as ``twitter`` or ``x``. Keep those aliases in one place so
setup resolution, capability displays, and runtime credential lookup agree.
"""
from __future__ import annotations


_CANONICAL_PROVIDER_ALIASES: dict[str, str] = {
    "twitter": "twitter_x",
    "twitterx": "twitter_x",
    "x": "twitter_x",
    "x_twitter": "twitter_x",
}


def normalize_provider_key(provider: object) -> str:
    return str(provider or "").strip().lower().replace("-", "_").replace(" ", "_")


def canonical_provider_key(provider: object) -> str:
    key = normalize_provider_key(provider)
    return _CANONICAL_PROVIDER_ALIASES.get(key, key)


def provider_key_aliases(provider: object) -> set[str]:
    key = normalize_provider_key(provider)
    canonical = canonical_provider_key(key)
    aliases = {key, canonical}
    aliases.update(
        alias
        for alias, target in _CANONICAL_PROVIDER_ALIASES.items()
        if target == canonical
    )
    return {alias for alias in aliases if alias}


def provider_keys_match(left: object, right: object) -> bool:
    return canonical_provider_key(left) == canonical_provider_key(right)
