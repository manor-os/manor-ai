"""Canonical Integration identities and runtime capabilities.

External systems enter Manor through several paths (built-in MCP servers,
OAuth, Nango, entity credentials, and extensions).  Persisting a database
enum would make those extension points brittle, so integrations use stable,
normalized string keys instead.  This registry is the single place where a
canonical key gains aliases and optional runtime capabilities.

Callers may still pass an unregistered provider key.  It is normalized and
remains a valid canonical key, while registered aliases (for example ``x``)
resolve to their permanent identity (``twitter_x``).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable, Optional


HealthChecker = Callable[..., Awaitable[dict[str, Any]]]
StatMeasurer = Callable[[Any, dict[str, Any], str], Awaitable[Any]]


@dataclass(frozen=True)
class IntegrationSpec:
    """Capabilities attached to one permanent Integration key."""

    key: str
    aliases: frozenset[str] = frozenset()
    health_checker: Optional[HealthChecker] = None
    stat_measurer: Optional[StatMeasurer] = None
    stat_measurer_key: Optional[str] = None
    stat_metric_keys: frozenset[str] = frozenset()


_SPECS: dict[str, IntegrationSpec] = {}
_ALIASES: dict[str, str] = {}


def normalize_integration_key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def canonical_integration_key(value: object) -> str:
    """Return the stable key for a raw key or provider alias."""

    normalized = normalize_integration_key(value)
    return _ALIASES.get(normalized, normalized)


def register_integration(
    key: object,
    *,
    aliases: Iterable[object] = (),
    health_checker: Optional[HealthChecker] = None,
    stat_measurer: Optional[StatMeasurer] = None,
    stat_measurer_key: Optional[str] = None,
    stat_metric_keys: Iterable[object] = (),
) -> IntegrationSpec:
    """Register or enrich an Integration specification idempotently."""

    normalized_key = normalize_integration_key(key)
    if not normalized_key:
        raise ValueError("integration key cannot be empty")

    known_canonical = _ALIASES.get(normalized_key)
    if known_canonical and known_canonical != normalized_key:
        raise ValueError(
            f"integration key {normalized_key!r} is already an alias for {known_canonical!r}"
        )

    normalized_aliases = {
        normalized
        for value in aliases
        if (normalized := normalize_integration_key(value))
    }
    normalized_aliases.discard(normalized_key)
    for alias in normalized_aliases:
        owner = _ALIASES.get(alias)
        if owner and owner != normalized_key:
            raise ValueError(
                f"integration alias {alias!r} is already registered for {owner!r}"
            )

    existing = _SPECS.get(normalized_key) or IntegrationSpec(key=normalized_key)
    metric_keys = {
        normalized
        for value in stat_metric_keys
        if (normalized := normalize_integration_key(value))
    }
    resolved_measurer_key = (
        normalize_integration_key(stat_measurer_key)
        if stat_measurer_key is not None
        else existing.stat_measurer_key
    )
    spec = replace(
        existing,
        aliases=existing.aliases | frozenset(normalized_aliases),
        health_checker=health_checker or existing.health_checker,
        stat_measurer=stat_measurer or existing.stat_measurer,
        stat_measurer_key=resolved_measurer_key,
        stat_metric_keys=existing.stat_metric_keys | frozenset(metric_keys),
    )
    _SPECS[normalized_key] = spec
    _ALIASES[normalized_key] = normalized_key
    for alias in spec.aliases:
        _ALIASES[alias] = normalized_key
    return spec


def get_integration_spec(key: object) -> Optional[IntegrationSpec]:
    return _SPECS.get(canonical_integration_key(key))


def integration_key_aliases(key: object) -> set[str]:
    """Return every accepted storage spelling for an Integration key."""

    raw = normalize_integration_key(key)
    canonical = canonical_integration_key(raw)
    spec = _SPECS.get(canonical)
    aliases = {raw, canonical}
    if spec:
        aliases.update(spec.aliases)
    return {alias for alias in aliases if alias}


def register_health_checker(key: object, checker: HealthChecker) -> IntegrationSpec:
    return register_integration(key, health_checker=checker)


def health_checker_for(key: object) -> Optional[HealthChecker]:
    spec = get_integration_spec(key)
    return spec.health_checker if spec else None


def register_stat_measurer(
    key: object,
    measurer: StatMeasurer,
    *,
    metric_keys: Iterable[object] = (),
) -> IntegrationSpec:
    return register_integration(
        key,
        stat_measurer=measurer,
        stat_measurer_key=canonical_integration_key(key),
        stat_metric_keys=metric_keys,
    )


def stat_measurer_for(key: object) -> Optional[StatMeasurer]:
    spec = get_integration_spec(key)
    return spec.stat_measurer if spec else None


def integration_specs() -> tuple[IntegrationSpec, ...]:
    return tuple(_SPECS[key] for key in sorted(_SPECS))


# Stable aliases and declared measurement capability for the first
# integration exposed by the Workspace Stats library.  Runtime functions are
# attached by their owning modules to avoid registry import cycles.
register_integration(
    "twitter_x",
    aliases=("twitter", "twitterx", "x", "x_twitter"),
    stat_measurer_key="twitter_x",
)

# ``wechat`` was the original storage key before the product split the
# personal ClawBot and Official Account integrations. Keep legacy rows
# addressable as the Official Account provider until operators re-save them.
register_integration("wechat_official", aliases=("wechat",))
