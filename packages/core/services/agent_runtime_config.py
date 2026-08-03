"""Typed runtime overrides stored on an Agent's JSON config.

An Agent override is intentionally the highest-priority runtime setting for
that Agent. Missing or invalid values remain ``None`` so each execution
surface can fall back to its existing workspace/account/platform defaults.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class AgentModelMode(str, Enum):
    INHERIT = "inherit"
    FIXED = "fixed"


class LegacyAgentModelSelection(str, Enum):
    """Pre-enum model selections accepted only at the config boundary."""

    INHERIT = "default"


@dataclass(frozen=True)
class AgentRuntimeConfig:
    model_mode: AgentModelMode = AgentModelMode.INHERIT
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


def _enum_value(enum_type: type[Enum], value: Any) -> Enum | None:
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _model_config(values: Mapping[str, Any]) -> tuple[AgentModelMode, str | None]:
    model = str(values.get("model") or "").strip()
    configured_mode = _enum_value(AgentModelMode, values.get("model_mode"))
    if configured_mode is AgentModelMode.INHERIT:
        return AgentModelMode.INHERIT, None
    if configured_mode is AgentModelMode.FIXED:
        return AgentModelMode.FIXED, model or None

    # Compatibility for rows and exported Blueprints created before
    # ``model_mode`` existed. New writes always persist AgentModelMode.
    legacy_selection = _enum_value(LegacyAgentModelSelection, model)
    if legacy_selection is LegacyAgentModelSelection.INHERIT:
        return AgentModelMode.INHERIT, None
    if model:
        return AgentModelMode.FIXED, model
    return AgentModelMode.INHERIT, None


def _temperature_override(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        return None
    return temperature


def _max_tokens_override(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        max_tokens = int(value)
    except (TypeError, ValueError):
        return None
    if max_tokens <= 0:
        return None
    return max_tokens


def agent_runtime_config(config: Mapping[str, Any] | None) -> AgentRuntimeConfig:
    """Return validated Agent overrides from a raw config mapping."""

    values = config if isinstance(config, Mapping) else {}
    model_mode, model = _model_config(values)
    return AgentRuntimeConfig(
        model_mode=model_mode,
        model=model,
        temperature=_temperature_override(values.get("temperature")),
        max_tokens=_max_tokens_override(values.get("max_tokens")),
    )


def normalize_agent_runtime_config(
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a storage-safe config using the explicit model mode enum."""

    normalized = dict(config) if isinstance(config, Mapping) else {}
    runtime_config = agent_runtime_config(normalized)
    normalized["model_mode"] = runtime_config.model_mode.value
    if runtime_config.model_mode is AgentModelMode.FIXED and runtime_config.model:
        normalized["model"] = runtime_config.model
    else:
        normalized.pop("model", None)
    return normalized


def agent_runtime_config_for(agent: Any) -> AgentRuntimeConfig:
    """Return validated overrides from an Agent ORM row or mapping."""

    if isinstance(agent, Mapping):
        raw_config = agent.get("config")
    else:
        raw_config = getattr(agent, "config", None)
    return agent_runtime_config(raw_config if isinstance(raw_config, Mapping) else None)


__all__ = [
    "AgentModelMode",
    "AgentRuntimeConfig",
    "agent_runtime_config",
    "agent_runtime_config_for",
    "normalize_agent_runtime_config",
]
