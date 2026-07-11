"""Platform-level model catalog controls.

Lets platform admins shape the model catalog without a deploy:

  * disable individual catalog models per role — they disappear from
    the user-facing picker, can't be saved as a preference, and any
    previously-saved preference pointing at them falls through to the
    platform default at resolution time;
  * override the platform default model per role — applied after user
    and entity preferences but before the hard-coded ``DEFAULTS``.

Storage is one ``platform_settings`` row (key ``model_catalog``)::

    {
      "disabled_models":  {"primary": ["openai/gpt-4o", ...], ...},
      "default_overrides": {"worker": "google/gemini-2.5-flash", ...},
      "platform_backend_model": "anthropic/claude-sonnet-4.6"
    }

``platform_backend_model`` selects the model for platform-backend AI
calls (``entity_id=None`` Runtime completions, e.g. announcement
drafting). Unset means the env-configured global default. Choices come
from the ``primary`` catalog; it is intentionally NOT a catalog role so
it never leaks into the user-facing catalog payloads.

Reads on hot paths go through a 60s in-process cache, mirroring
``feature_flags``. Admin mutations clear the cache in this process;
other workers converge within the TTL.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.models import CATALOG, DEFAULTS
from packages.core.models.base import generate_ulid
from packages.core.models.platform_setting import PlatformSetting

logger = logging.getLogger(__name__)

MODEL_SETTINGS_KEY = "model_catalog"

_CACHE_TTL_SECONDS = 60.0
_cache: Optional[dict[str, Any]] = None
_cache_at: float = 0.0


def invalidate_model_settings_cache() -> None:
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def _normalize(value: dict | None) -> dict[str, Any]:
    """Coerce the stored JSONB document into a predictable shape."""
    value = value or {}
    disabled: dict[str, list[str]] = {}
    for role, ids in (value.get("disabled_models") or {}).items():
        if role in DEFAULTS and isinstance(ids, list):
            cleaned = sorted({str(m).strip() for m in ids if str(m).strip()})
            if cleaned:
                disabled[role] = cleaned
    overrides: dict[str, str] = {}
    for role, model_id in (value.get("default_overrides") or {}).items():
        if role in DEFAULTS and str(model_id or "").strip():
            overrides[role] = str(model_id).strip()
    backend = str(value.get("platform_backend_model") or "").strip()
    return {
        "disabled_models": disabled,
        "default_overrides": overrides,
        "platform_backend_model": backend or None,
    }


# ── Reads ────────────────────────────────────────────────────────────

async def get_model_settings(db: AsyncSession) -> dict[str, Any]:
    """Load the raw settings document (uncached). Never raises — a
    missing row or query failure returns the empty document."""
    try:
        row = (await db.execute(
            select(PlatformSetting).where(PlatformSetting.key == MODEL_SETTINGS_KEY)
        )).scalar_one_or_none()
        return _normalize(row.value if row else None)
    except Exception as exc:
        logger.debug("model_settings: load failed: %s", exc)
        return _normalize(None)


async def get_model_settings_cached(db: AsyncSession | None = None) -> dict[str, Any]:
    """TTL-cached variant for hot paths. Opens a short-lived session
    when ``db`` is omitted."""
    global _cache, _cache_at
    if _cache is not None and (time.time() - _cache_at) < _CACHE_TTL_SECONDS:
        return _cache
    if db is not None:
        settings = await get_model_settings(db)
    else:
        try:
            from packages.core.database import async_session
            async with async_session() as fresh:
                settings = await get_model_settings(fresh)
        except Exception as exc:
            logger.debug("model_settings: cached load failed: %s", exc)
            return _cache or _normalize(None)
    _cache = settings
    _cache_at = time.time()
    return settings


def get_model_settings_sync() -> dict[str, Any]:
    """Last-known cached settings for sync callers (no refresh). Empty
    document until an async path has populated the cache."""
    return _cache or _normalize(None)


def effective_catalog(settings: dict[str, Any]) -> dict[str, list[dict]]:
    """CATALOG with admin-disabled models removed."""
    disabled = settings.get("disabled_models") or {}
    out: dict[str, list[dict]] = {}
    for role, items in CATALOG.items():
        blocked = set(disabled.get(role) or [])
        out[role] = [dict(item) for item in items if str(item.get("id")) not in blocked]
    return out


def effective_defaults(settings: dict[str, Any]) -> dict[str, str]:
    """DEFAULTS with admin overrides applied."""
    overrides = settings.get("default_overrides") or {}
    return {role: overrides.get(role) or model_id for role, model_id in DEFAULTS.items()}


def is_model_disabled(settings: dict[str, Any], role: str, model_id: str) -> bool:
    return model_id in set((settings.get("disabled_models") or {}).get(role) or [])


def platform_backend_model(settings: dict[str, Any]) -> Optional[str]:
    """Configured model for platform-backend AI calls, or ``None`` when
    the env-configured global default should apply."""
    return settings.get("platform_backend_model") or None


# ── Admin mutations ──────────────────────────────────────────────────

async def _get_or_create_row(db: AsyncSession) -> PlatformSetting:
    row = (await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == MODEL_SETTINGS_KEY)
    )).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(id=generate_ulid(), key=MODEL_SETTINGS_KEY, value={})
        db.add(row)
    return row


def _catalog_ids(role: str) -> set[str]:
    return {str(item.get("id")) for item in CATALOG.get(role, []) if item.get("id")}


def _validate_role_model(role: str, model_id: str) -> None:
    if role not in DEFAULTS:
        raise ValueError(f"Unknown model role: {role}")
    if model_id not in _catalog_ids(role):
        raise ValueError(f"Model {model_id} is not in the {role} catalog")


async def set_model_enabled(
    db: AsyncSession,
    *,
    role: str,
    model_id: str,
    enabled: bool,
    actor_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Enable/disable one catalog model for a role. The role's effective
    default can never be disabled — change the default first."""
    _validate_role_model(role, model_id)
    row = await _get_or_create_row(db)
    settings = _normalize(row.value)
    if not enabled:
        if effective_defaults(settings).get(role) == model_id:
            raise ValueError(
                f"{model_id} is the current default for {role} — "
                "set a different default before disabling it"
            )
        disabled = set(settings["disabled_models"].get(role) or [])
        disabled.add(model_id)
        settings["disabled_models"][role] = sorted(disabled)
    else:
        disabled = set(settings["disabled_models"].get(role) or [])
        disabled.discard(model_id)
        if disabled:
            settings["disabled_models"][role] = sorted(disabled)
        else:
            settings["disabled_models"].pop(role, None)
    row.value = settings
    row.updated_by = actor_user_id
    await db.flush()
    invalidate_model_settings_cache()
    return settings


async def set_platform_backend_model(
    db: AsyncSession,
    *,
    model_id: Optional[str],
    actor_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Set the model for platform-backend AI calls; ``None`` clears it
    (back to the env-configured global default). Validated against the
    ``primary`` catalog since backend drafting is a primary-tier task."""
    row = await _get_or_create_row(db)
    settings = _normalize(row.value)
    if model_id:
        _validate_role_model("primary", model_id)
        if is_model_disabled(settings, "primary", model_id):
            raise ValueError(
                f"{model_id} is disabled — enable it before using it "
                "for the platform backend"
            )
        settings["platform_backend_model"] = model_id
    else:
        settings["platform_backend_model"] = None
    row.value = settings
    row.updated_by = actor_user_id
    await db.flush()
    invalidate_model_settings_cache()
    return settings


async def set_default_override(
    db: AsyncSession,
    *,
    role: str,
    model_id: Optional[str],
    actor_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Override the platform default model for a role; ``None`` clears
    the override (back to the factory default)."""
    if role not in DEFAULTS:
        raise ValueError(f"Unknown model role: {role}")
    row = await _get_or_create_row(db)
    settings = _normalize(row.value)
    if model_id:
        _validate_role_model(role, model_id)
        if is_model_disabled(settings, role, model_id):
            raise ValueError(f"{model_id} is disabled — enable it before making it the default")
        settings["default_overrides"][role] = model_id
    else:
        settings["default_overrides"].pop(role, None)
        factory = DEFAULTS.get(role)
        if factory and is_model_disabled(settings, role, factory):
            raise ValueError(
                f"Cannot clear the {role} override while the factory default "
                f"({factory}) is disabled"
            )
    row.value = settings
    row.updated_by = actor_user_id
    await db.flush()
    invalidate_model_settings_cache()
    return settings
