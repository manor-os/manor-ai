"""User-level activation for optional built-in Manor plugins.

The definitions in this module are credential-free product metadata.  A user's
choice is stored in ``User.preferences`` and enforced by the Skill runtime gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.user import User


BUILTIN_PLUGIN_PREFERENCE_KEY = "builtin_plugins"


@dataclass(frozen=True)
class BuiltinPluginSkill:
    slug: str
    display_name: str
    description: str


@dataclass(frozen=True)
class BuiltinPluginDefinition:
    key: str
    display_name: str
    default_enabled: bool
    skills: tuple[BuiltinPluginSkill, ...]
    sandbox_config: Mapping[str, Any] | None = None


REMOTION_PLUGIN_SKILLS = (
    BuiltinPluginSkill(
        "remotion-best-practices",
        "Remotion Best Practices",
        "Route Remotion tasks to the right workflow.",
    ),
    BuiltinPluginSkill(
        "remotion-captions",
        "Remotion Captions",
        "Transcribe, display, and animate captions.",
    ),
    BuiltinPluginSkill(
        "remotion-create",
        "Remotion Create",
        "Create a new Remotion video project.",
    ),
    BuiltinPluginSkill(
        "remotion-docs",
        "Remotion Docs",
        "Search current Remotion documentation.",
    ),
    BuiltinPluginSkill(
        "remotion-interactivity",
        "Remotion Interactivity",
        "Make compositions editable in Remotion Studio.",
    ),
    BuiltinPluginSkill(
        "remotion-maps",
        "Remotion Maps",
        "Create animated maps in Remotion videos.",
    ),
    BuiltinPluginSkill(
        "remotion-markup",
        "Remotion Markup",
        "Apply Remotion animation and effects practices.",
    ),
    BuiltinPluginSkill(
        "remotion-multimedia",
        "Remotion Multimedia",
        "Inspect browser-based audio and video media.",
    ),
    BuiltinPluginSkill(
        "remotion-render",
        "Remotion Render",
        "Export Remotion videos and stills.",
    ),
    BuiltinPluginSkill(
        "remotion-saas",
        "Remotion SaaS",
        "Build apps that preview and render videos.",
    ),
    BuiltinPluginSkill(
        "remotion-studio",
        "Remotion Studio",
        "Preview a Remotion video.",
    ),
    BuiltinPluginSkill(
        "remotion-upgrade",
        "Remotion Upgrade",
        "Upgrade Remotion and related dependencies.",
    ),
)


BUILTIN_PLUGINS: dict[str, BuiltinPluginDefinition] = {
    "remotion": BuiltinPluginDefinition(
        key="remotion",
        display_name="Remotion",
        default_enabled=False,
        skills=REMOTION_PLUGIN_SKILLS,
        # Remotion installs Chromium-facing Node dependencies into /skill and
        # needs materially more headroom than a small script-style skill.
        sandbox_config={
            "memory": "2g",
            "workdir_tmpfs_size": "1g",
            "pids_limit": 512,
        },
    ),
}


def builtin_plugin_definition(plugin_key: str) -> BuiltinPluginDefinition | None:
    return BUILTIN_PLUGINS.get(str(plugin_key or "").strip().lower())


def builtin_plugin_key_for_skill(skill: Any) -> str | None:
    config = getattr(skill, "config", None)
    if isinstance(config, dict):
        declared = str(config.get("plugin_key") or "").strip().lower()
        if declared in BUILTIN_PLUGINS:
            return declared
    slug = str(getattr(skill, "slug", None) or getattr(skill, "name", "") or "").strip()
    for definition in BUILTIN_PLUGINS.values():
        if any(item.slug == slug for item in definition.skills):
            return definition.key
    return None


def normalize_builtin_plugin_preferences(
    preferences: Mapping[str, Any] | None,
) -> dict[str, bool]:
    raw = (
        preferences.get(BUILTIN_PLUGIN_PREFERENCE_KEY)
        if isinstance(preferences, Mapping)
        else None
    )
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        key: bool(raw[key]) if key in raw else definition.default_enabled
        for key, definition in BUILTIN_PLUGINS.items()
    }


def builtin_plugin_is_enabled(
    plugin_key: str,
    preferences: Mapping[str, Any] | None,
) -> bool:
    definition = builtin_plugin_definition(plugin_key)
    if definition is None:
        return False
    if (
        isinstance(preferences, Mapping)
        and definition.key in preferences
        and isinstance(preferences[definition.key], bool)
    ):
        return bool(preferences[definition.key])
    return normalize_builtin_plugin_preferences(preferences)[definition.key]


async def load_user_builtin_plugin_preferences(
    db: AsyncSession,
    user_id: str,
) -> dict[str, bool]:
    preferences = (
        await db.execute(select(User.preferences).where(User.id == user_id))
    ).scalar_one_or_none()
    return normalize_builtin_plugin_preferences(preferences)


async def set_user_builtin_plugin_enabled(
    db: AsyncSession,
    *,
    user_id: str,
    plugin_key: str,
    enabled: bool,
) -> dict[str, bool]:
    definition = builtin_plugin_definition(plugin_key)
    if definition is None:
        raise KeyError(plugin_key)
    user = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None or user.status != "active":
        raise LookupError(user_id)
    preferences = dict(user.preferences or {})
    raw_plugin_preferences = preferences.get(BUILTIN_PLUGIN_PREFERENCE_KEY)
    plugin_preferences = (
        dict(raw_plugin_preferences)
        if isinstance(raw_plugin_preferences, Mapping)
        else {}
    )
    plugin_preferences[definition.key] = bool(enabled)
    preferences[BUILTIN_PLUGIN_PREFERENCE_KEY] = plugin_preferences
    user.preferences = preferences
    await db.flush()
    return normalize_builtin_plugin_preferences(preferences)
