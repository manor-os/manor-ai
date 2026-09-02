from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.integration_skill_registry import (
    integration_provider_keys_for_skill,
)


_COMPANION_FIELDS = frozenset({"tool_names", "tool_prefixes"})


def _normalized_values(raw: Any, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"capability_companion.{field} must be an array")
    values = tuple(
        dict.fromkeys(str(value or "").strip() for value in raw if str(value or "").strip())
    )
    if len(values) != len(raw):
        raise ValueError(
            f"capability_companion.{field} must contain unique non-empty strings"
        )
    return values


@dataclass(frozen=True)
class SkillCapabilityCompanion:
    """Trusted binding from a built-in Skill to executable capabilities."""

    tool_names: tuple[str, ...] = ()
    tool_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, raw: Any) -> "SkillCapabilityCompanion":
        if not isinstance(raw, Mapping):
            raise ValueError("capability_companion must be an object")
        unknown_fields = set(raw).difference(_COMPANION_FIELDS)
        if unknown_fields:
            names = ", ".join(sorted(str(field) for field in unknown_fields))
            raise ValueError(f"capability_companion has unknown fields: {names}")

        companion = cls(
            tool_names=_normalized_values(raw.get("tool_names"), field="tool_names"),
            tool_prefixes=_normalized_values(
                raw.get("tool_prefixes"),
                field="tool_prefixes",
            ),
        )
        if not companion.tool_names and not companion.tool_prefixes:
            raise ValueError("capability_companion requires a tool name or prefix")
        if any(
            not prefix.startswith("mcp__") or not prefix.endswith("__")
            for prefix in companion.tool_prefixes
        ):
            raise ValueError(
                "capability_companion.tool_prefixes must use mcp__<server>__ prefixes"
            )
        return companion

    def to_dict(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if self.tool_names:
            result["tool_names"] = list(self.tool_names)
        if self.tool_prefixes:
            result["tool_prefixes"] = list(self.tool_prefixes)
        return result

    def matches(self, tool_name: str) -> bool:
        name = str(tool_name or "").strip()
        return bool(
            name
            and (
                name in self.tool_names
                or any(name.startswith(prefix) for prefix in self.tool_prefixes)
            )
        )


def _is_trusted_builtin_skill(skill: Any) -> bool:
    config = getattr(skill, "config", None)
    return not (
        getattr(skill, "entity_id", "missing") is not None
        or not isinstance(config, dict)
        or config.get("source") != "builtin"
    )


def trusted_integration_provider_keys(skill: Any) -> tuple[str, ...]:
    """Resolve provider ownership only for repository-controlled built-ins."""

    if not _is_trusted_builtin_skill(skill):
        return ()
    return integration_provider_keys_for_skill(
        getattr(skill, "slug", None),
        getattr(skill, "name", None),
    )


def trusted_skill_capability_companion(
    skill: Any,
) -> SkillCapabilityCompanion | None:
    """Return explicit Tool bindings plus derived Integration MCP bindings."""

    if not _is_trusted_builtin_skill(skill):
        return None
    config = getattr(skill, "config", None) or {}
    raw = config.get("capability_companion")
    explicit = None
    if raw is not None:
        try:
            explicit = SkillCapabilityCompanion.from_config(raw)
        except ValueError:
            return None

    integration_prefixes = tuple(
        f"mcp__{provider_key}__"
        for provider_key in trusted_integration_provider_keys(skill)
    )
    tool_names = explicit.tool_names if explicit is not None else ()
    tool_prefixes = tuple(
        dict.fromkeys(
            (
                *(explicit.tool_prefixes if explicit is not None else ()),
                *integration_prefixes,
            )
        )
    )
    if not tool_names and not tool_prefixes:
        return None
    return SkillCapabilityCompanion(
        tool_names=tool_names,
        tool_prefixes=tool_prefixes,
    )
