"""Workspace-level defaults for generated speech and narration."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_WORKSPACE_AUDIO_LANGUAGE = "en-US"
_AUDIO_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_AUDIO_LANGUAGE_ALIASES = {
    "en": "en-US",
    "zh": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hant": "zh-TW",
    "es": "es-ES",
    "de": "de-DE",
    "fr": "fr-FR",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "pt": "pt-BR",
    "hi": "hi-IN",
}


def normalize_workspace_audio_language(
    value: Any,
    *,
    default: str = DEFAULT_WORKSPACE_AUDIO_LANGUAGE,
) -> str:
    """Return a stable BCP-47-style language tag for audio generation."""

    raw = str(value or "").strip().replace("_", "-")
    if not raw:
        return default
    alias = _AUDIO_LANGUAGE_ALIASES.get(raw.lower())
    if alias:
        return alias
    if not _AUDIO_LANGUAGE_RE.fullmatch(raw):
        return default
    parts = raw.split("-")
    normalized = [parts[0].lower()]
    normalized.extend(part.upper() if len(part) in {2, 3} else part for part in parts[1:])
    return "-".join(normalized)


def workspace_audio_language(settings: dict[str, Any] | None) -> str:
    """Resolve the effective language from ``Workspace.settings``."""

    root = settings if isinstance(settings, dict) else {}
    audio_defaults = root.get("audio_defaults")
    if not isinstance(audio_defaults, dict):
        return DEFAULT_WORKSPACE_AUDIO_LANGUAGE
    return normalize_workspace_audio_language(audio_defaults.get("language"))


def normalize_workspace_audio_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an explicit audio-defaults object without losing peer settings."""

    normalized = dict(settings) if isinstance(settings, dict) else {}
    audio_defaults = normalized.get("audio_defaults")
    if not isinstance(audio_defaults, dict):
        return normalized
    normalized["audio_defaults"] = {
        **audio_defaults,
        "language": normalize_workspace_audio_language(audio_defaults.get("language")),
    }
    return normalized
