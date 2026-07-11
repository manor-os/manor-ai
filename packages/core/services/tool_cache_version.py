"""Version stamps for AI tool result caches.

Read-heavy tools such as ``list_documents`` and ``list_staff`` can safely use
longer TTLs when their cache key includes a per-entity version. Mutations bump
the version, so old result keys become unreachable immediately and expire in
Redis naturally.
"""
from __future__ import annotations

import os

_VERSION_TTL_SECONDS = int(os.getenv("TOOL_CACHE_VERSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))


def _version_key(entity_id: str, namespace: str) -> str:
    safe_namespace = (namespace or "default").replace(":", "_")
    return f"tool_cache_version:{entity_id}:{safe_namespace}"


async def get_tool_cache_version(entity_id: str, namespace: str) -> int:
    if not entity_id:
        return 0
    try:
        from packages.core.cache import cache
        value = await cache.get(_version_key(entity_id, namespace))
        return int(value or 0)
    except Exception:
        return 0


async def bump_tool_cache_version(entity_id: str, *namespaces: str) -> None:
    if not entity_id:
        return
    try:
        from packages.core.cache import cache
        for namespace in namespaces or ("default",):
            await cache.incr(_version_key(entity_id, namespace), ttl=_VERSION_TTL_SECONDS)
    except Exception:
        return
