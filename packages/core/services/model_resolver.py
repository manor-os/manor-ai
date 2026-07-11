"""Model resolver — single helper every caller uses to pick the right
LLM / image / voice / embedding model for a (user, entity, role) tuple.

Before this module existed, every call site repeated:

    from sqlalchemy import select
    from packages.core.models.user import Entity, User
    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    e = (await db.execute(select(Entity).where(Entity.id == entity_id))).scalar_one_or_none()
    user_prefs = u.preferences if u else None
    entity_settings = e.settings if e else None
    model = resolve_model_for_role(role, user_prefs, entity_settings)

…with subtly different mistakes in each copy (only reading the legacy
``user.llm_model`` column, forgetting one of the two layers, etc.). One
of them was even silently ignoring the Account-page picker.

This module collapses that into:

    from packages.core.services.model_resolver import resolve_model_for_user
    model = await resolve_model_for_user("image", user_id=..., entity_id=...)

Resolution order is tenant-scoped whenever an entity is known:

    entity.settings.models.{role}
      > legacy owner.preferences.models.{role}
      > env (LLM_MODEL / OPENROUTER_MODEL — primary only)
      > DEFAULTS[role]

Callers that already have ``User`` / ``Entity`` objects in hand can use
``resolve_model_from_objects`` to skip the DB round-trip.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.sql import or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.models import DEFAULTS, resolve_model_for_role

logger = logging.getLogger(__name__)


PRIMARY_BYOK_FALLBACK_LLM_ROLES: frozenset[str] = frozenset({
    "primary",
    "worker",
    "planner",
    "strategist",
    "plan_executor",
    "plan_supervisor",
    "task_runner",
    "chat",
    "chat_stream",
    "chat_extractor",
    "chat_insight_extraction",
    "conversation_summary",
    "briefing",
    "outcome_evaluation",
    "goal_measurement",
    "skill",
    "skill_generator",
    "skill_matcher",
    "subagent",
    "workflow",
    "workflow_runner",
    "workflow_service",
    "workspace_setup",
    "workspace_architect",
    "agentic_loop",
    "channel",
    "channel_hold",
    "runtime_completion",
    "prompt_preview",
    "document_ai_draft",
    "docgen",
    "extract_data_tool",
    "knowledge_gen",
    "memory",
    "internal_worker",
    "system",
})


def sanitize_llm_api_key(api_key: str, source: str = "API key") -> str:
    """Sanitize a user/provider API key through the model resolver boundary."""

    from packages.core.ai.llm_client import _sanitize_api_key

    return _sanitize_api_key(api_key, source)


def detect_llm_provider_from_key(api_key: str) -> str | None:
    """Detect the provider family for a raw API key."""

    from packages.core.ai.llm_client import detect_provider_from_key

    return detect_provider_from_key(api_key)


def llm_provider_from_model(model: str | None) -> str | None:
    """Return the provider family implied by a model id."""

    from packages.core.ai.llm_client import provider_for_model

    return provider_for_model(model)


def llm_provider_from_base_url(base_url: str) -> str | None:
    """Return the provider family implied by a custom base URL."""

    from packages.core.ai.llm_client import _provider_from_base_url

    return _provider_from_base_url(base_url)


def resolve_llm_provider_base_url(model: str, api_key: str, base_url: str | None = None) -> str:
    """Resolve the provider base URL for a model/API-key pair."""

    from packages.core.ai.llm_client import resolve_provider_base_url

    return resolve_provider_base_url(model, api_key, base_url)


def normalize_llm_model_for_provider(model: str, base_url: str) -> str:
    """Normalize a catalog model id for a provider-native endpoint."""

    from packages.core.ai.llm_client import normalize_model_for_provider

    return normalize_model_for_provider(model, base_url)


def default_llm_model() -> str:
    """Return the platform default LLM model through the resolver boundary."""

    from packages.core.ai.llm_client import get_llm_model

    return get_llm_model()


# ── DB-loading variant — most callers use this ──────────────────────────────

async def resolve_model_for_user(
    role: str,
    *,
    user_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> str:
    """Resolve the model id for a role, loading user prefs + entity
    settings as needed.

    ``db`` is optional — when omitted, this opens its own short-lived
    session. Pass ``db`` when you're already inside a transaction so
    everything stays on one connection.

    Always returns a non-empty model id (falls back through ``DEFAULTS``).
    Never raises — DB failures log and fall through to defaults.
    """
    user_prefs = None
    entity_settings = None
    owner_prefs = None

    if user_id or entity_id:
        try:
            if db is not None:
                user_prefs, entity_settings, _entity_plan_id, owner_prefs = await _load_prefs(
                    db, user_id, entity_id,
                )
            else:
                from packages.core.database import async_session
                async with async_session() as fresh:
                    user_prefs, entity_settings, _entity_plan_id, owner_prefs = await _load_prefs(
                        fresh, user_id, entity_id,
                    )
        except Exception as exc:
            logger.debug(
                "model_resolver: prefs lookup failed (role=%s user=%s entity=%s): %s",
                role, user_id, entity_id, exc,
            )

    from packages.core.services.model_settings import get_model_settings_cached
    platform_settings = await get_model_settings_cached(db)

    if entity_id:
        return (
            _resolve_entity_scoped_model(
                role,
                entity_settings=entity_settings,
                owner_prefs=owner_prefs,
                platform_settings=platform_settings,
            )
            or DEFAULTS.get(role, DEFAULTS["primary"])
        )

    return (
        resolve_model_for_role(role, user_prefs, entity_settings, platform_settings)
        or DEFAULTS.get(role, DEFAULTS["primary"])
    )


async def _load_prefs(
    db: AsyncSession,
    user_id: Optional[str],
    entity_id: Optional[str],
) -> tuple[Optional[dict], Optional[dict], Optional[str], Optional[dict]]:
    """Inner helper — loads (user.preferences, entity.settings,
    entity.plan_id) in one session. Returns ``None`` for missing rows.
    ``plan_id`` is needed so callers can apply the BYOK plan gate
    without a second round-trip."""
    # Imports kept local so this module can be imported during model
    # registration without circular issues.
    from packages.core.models.user import Entity, User, UserMembership

    user_prefs: Optional[dict] = None
    entity_settings: Optional[dict] = None
    entity_plan_id: Optional[str] = None
    owner_prefs: Optional[dict] = None

    if user_id:
        u = (await db.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if u:
            user_prefs = u.preferences
    if entity_id:
        e = (await db.execute(
            select(Entity).where(Entity.id == entity_id)
        )).scalar_one_or_none()
        if e:
            entity_settings = e.settings
            entity_plan_id = getattr(e, "plan_id", None)
        owner_prefs = await load_entity_owner_preferences(db, entity_id)

    return user_prefs, entity_settings, entity_plan_id, owner_prefs


async def load_entity_owner_preferences(db: AsyncSession, entity_id: str) -> dict | None:
    """Return the active owner preferences for legacy BYOK/model fallback."""
    from packages.core.models.user import User, UserMembership

    owner = (await db.execute(
        select(User)
        .outerjoin(
            UserMembership,
            (UserMembership.user_id == User.id)
            & (UserMembership.entity_id == entity_id)
            & (UserMembership.status == "active"),
        )
        .where(
            User.status == "active",
            or_(
                (User.entity_id == entity_id) & (User.role == "owner"),
                UserMembership.role == "owner",
            ),
        )
        .order_by(User.created_at.asc())
        .limit(1)
    )).scalar_one_or_none()
    return dict(owner.preferences or {}) if owner else None


def _model_disabled(role: str, model_id: str, platform_settings: dict | None) -> bool:
    disabled = set(((platform_settings or {}).get("disabled_models") or {}).get(role) or [])
    return model_id in disabled


def _configured_model(
    role: str,
    settings: dict | None,
    platform_settings: dict | None,
) -> str | None:
    model_id = str(((settings or {}).get("models") or {}).get(role) or "").strip()
    if model_id and not _model_disabled(role, model_id, platform_settings):
        return model_id
    return None


def _resolve_entity_scoped_model(
    role: str,
    *,
    entity_settings: dict | None,
    owner_prefs: dict | None,
    platform_settings: dict | None,
) -> str:
    return (
        _configured_model(role, entity_settings, platform_settings)
        or _configured_model(role, owner_prefs, platform_settings)
        or resolve_model_for_role(role, None, None, platform_settings)
        or DEFAULTS.get(role, DEFAULTS["primary"])
    )


async def resolve_llm_metadata_for_user(
    role: str,
    *,
    user_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> dict | None:
    """Resolve BYOK metadata for a role, loading preferences as needed.

    Applies the BYOK plan gate (``byok_allowed_for_plan``) so a user on
    the public Free plan has their stored API key silently ignored —
    Manor's gateway stays in the loop and the credit meter keeps
    counting."""

    if not user_id and not entity_id:
        return None
    try:
        if db is not None:
            user_prefs, entity_settings, entity_plan_id, owner_prefs = await _load_prefs(
                db, user_id, entity_id,
            )
        else:
            from packages.core.database import async_session
            async with async_session() as fresh:
                user_prefs, entity_settings, entity_plan_id, owner_prefs = await _load_prefs(
                    fresh, user_id, entity_id,
                )
    except Exception as exc:
        logger.debug(
            "model_resolver: llm metadata lookup failed (role=%s user=%s entity=%s): %s",
            role, user_id, entity_id, exc,
        )
        return None
    if entity_id and not byok_allowed_for_plan(entity_plan_id):
        if _settings_have_byok(entity_settings) or _settings_have_byok(owner_prefs):
            logger.info(
                "model_resolver: BYOK suppressed by plan gate "
                "(entity_plan=%s role=%s user=%s)",
                entity_plan_id, role, user_id,
            )
        return None
    if entity_id:
        return (
            resolve_llm_metadata_from_settings(entity_settings, role=role, source="entity")
            or resolve_llm_metadata_from_settings(owner_prefs, role=role, source="owner_legacy")
        )
    return resolve_llm_metadata_from_preferences(user_prefs, role=role)


# ── Object-loaded variant — for callers that already have rows ──────────────

def resolve_model_from_objects(
    role: str,
    *,
    user=None,
    entity=None,
) -> str:
    """Same logic as ``resolve_model_for_user`` but takes the already-
    loaded ``User`` / ``Entity`` SQLAlchemy objects directly. Use this
    when you've just queried them anyway — saves a round trip."""
    user_prefs = getattr(user, "preferences", None) if user is not None else None
    entity_settings = getattr(entity, "settings", None) if entity is not None else None
    # Sync path — uses the last-known platform settings cache (no refresh).
    from packages.core.services.model_settings import get_model_settings_sync
    platform_settings = get_model_settings_sync()
    if entity is not None:
        owner_prefs = user_prefs if getattr(user, "role", None) == "owner" else None
        return _resolve_entity_scoped_model(
            role,
            entity_settings=entity_settings,
            owner_prefs=owner_prefs,
            platform_settings=platform_settings,
        )
    return (
        resolve_model_for_role(role, user_prefs, entity_settings, platform_settings)
        or DEFAULTS.get(role, DEFAULTS["primary"])
    )


def resolve_model_from_context(ctx, role: str = "primary") -> str:
    """Resolve a model from a runtime/prompt context with loaded objects."""

    resolved_model = str(getattr(ctx, "model", "") or "").strip()
    if resolved_model:
        return resolved_model

    return resolve_model_from_objects(
        role,
        user=getattr(ctx, "user", None),
        entity=getattr(ctx, "entity", None),
    )


def resolve_llm_metadata_from_preferences(
    prefs: dict | None,
    role: str = "primary",
) -> dict | None:
    """Resolve BYOK metadata for an LLM role from user preferences.

    Returns ``None`` when the request should use platform-managed keys.
    OpenRouter keys are deliberately ignored here so BYOK cannot bypass Manor
    credit accounting.
    """

    return resolve_llm_metadata_from_settings(prefs, role=role, source="user")


def _settings_have_byok(settings: dict | None) -> bool:
    settings = settings or {}
    return bool(settings.get("llm_api_key") or settings.get("llm_api_keys"))


def resolve_llm_metadata_from_settings(
    settings: dict | None,
    role: str = "primary",
    *,
    source: str = "settings",
) -> dict | None:
    """Resolve native-provider BYOK metadata from a settings document."""

    settings = settings or {}
    role_keys = settings.get("llm_api_keys") or {}
    fallback_to_primary = role in PRIMARY_BYOK_FALLBACK_LLM_ROLES
    api_key = (
        role_keys.get(role)
        or (role_keys.get("primary") if fallback_to_primary else "")
        or (settings.get("llm_api_key", "") if fallback_to_primary else "")
    )
    if not api_key or not str(api_key).strip():
        return None
    key = sanitize_llm_api_key(str(api_key), f"{source}.{role}_api_key")
    if not key or key.startswith("sk-or-"):
        return None
    meta: dict = {"llm_api_key": key}
    role_urls = settings.get("llm_base_urls") or {}
    base_url = (
        role_urls.get(role)
        or (role_urls.get("primary") if fallback_to_primary else "")
        or (settings.get("llm_base_url", "") if fallback_to_primary else "")
    )
    if base_url and str(base_url).strip():
        meta["llm_base_url"] = str(base_url).strip()
    return meta


def resolve_llm_metadata_from_objects(
    role: str = "primary",
    *,
    user=None,
    entity=None,
) -> dict | None:
    """Resolve BYOK metadata from already-loaded user/entity objects.

    Applies the BYOK plan gate: if the entity is on the public Free
    plan (``plan_free``), BYOK is suppressed and Manor's gateway
    handles the request. Existing customers grandfathered onto
    ``plan_free_legacy`` keep BYOK access."""

    plan_id = getattr(entity, "plan_id", None) if entity is not None else None
    if not byok_allowed_for_plan(plan_id):
        return None
    entity_settings = getattr(entity, "settings", None) if entity is not None else None
    entity_metadata = resolve_llm_metadata_from_settings(entity_settings, role=role, source="entity")
    if entity_metadata:
        return entity_metadata
    prefs = getattr(user, "preferences", None) if user is not None else None
    if entity is not None and getattr(user, "role", None) != "owner":
        return None
    return resolve_llm_metadata_from_preferences(prefs, role=role)


# ── BYOK plan gate ──────────────────────────────────────────────────

# Plans on which BYOK (Bring Your Own Key) is suppressed. The set is
# intentionally a hard-coded denylist rather than an allowlist so that
# any new plan added through ``subscription_plans`` (including custom
# Enterprise rows) gets BYOK by default. The public Free tier is the
# only plan where suppressing BYOK matters — without it, Free users
# would route their own API key through Manor's orchestration layer
# and never have a reason to upgrade.
#
# Existing Free entities migrated to ``plan_free_legacy`` keep BYOK
# access intact (grandfathered).
BYOK_BLOCKED_PLANS: frozenset[str] = frozenset({"plan_free"})


def byok_allowed_for_plan(plan_id: Optional[str]) -> bool:
    """True iff the given plan permits BYOK keys to take effect.

    NULL / missing plan_id → True (we don't want to break callers
    that aren't yet plan-aware; the credit gate will still apply).
    """
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() != "cloud":
        return True
    if plan_id is None:
        return True
    return plan_id not in BYOK_BLOCKED_PLANS


def resolve_llm_metadata_from_context(ctx, role: str = "primary") -> dict | None:
    """Resolve BYOK metadata from a runtime/prompt context."""

    resolved_metadata = getattr(ctx, "llm_metadata", None)
    if isinstance(resolved_metadata, dict) and resolved_metadata:
        return dict(resolved_metadata)

    return resolve_llm_metadata_from_objects(
        role,
        user=getattr(ctx, "user", None),
        entity=getattr(ctx, "entity", None),
    )


__all__ = [
    "resolve_model_for_user",
    "resolve_model_from_objects",
    "resolve_model_from_context",
    "resolve_llm_metadata_for_user",
    "resolve_llm_metadata_from_objects",
    "resolve_llm_metadata_from_context",
    "resolve_llm_metadata_from_preferences",
    "resolve_llm_metadata_from_settings",
    "load_entity_owner_preferences",
    "PRIMARY_BYOK_FALLBACK_LLM_ROLES",
    "byok_allowed_for_plan",
    "BYOK_BLOCKED_PLANS",
]
