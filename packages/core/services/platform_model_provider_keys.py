"""Platform official model provider token service."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.base import generate_ulid
from packages.core.models.model_provider import PlatformModelProviderKey
from packages.core.services.model_provider_handlers import (
    detect_provider_from_key,
    handler_for_provider,
    official_env_key,
    provider_catalog,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfficialProviderCredential:
    provider: str
    api_key: str
    base_url: str
    source: str
    source_detail: str = ""


def _strip_api_key_wrappers(raw: str) -> str:
    key = (raw or "").strip()
    for _ in range(4):
        before = key
        if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
            key = key[1:-1].strip()
        lowered = key.lower()
        if lowered.startswith("authorization:"):
            key = key.split(":", 1)[1].strip()
            lowered = key.lower()
        if lowered.startswith("bearer "):
            key = key[7:].strip()
        if key == before:
            break
    return key


def sanitize_provider_api_key(value: str, *, source: str = "api_key") -> str:
    key = _strip_api_key_wrappers(value)
    if not key:
        return ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in key) or len(key) < 8:
        logger.warning("Invalid provider API key in %s; ignoring preview=%r", source, key[:6])
        return ""
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        logger.warning("Provider API key in %s contains non-ASCII characters", source)
        return ""
    return key


def mask_provider_api_key(value: str) -> str:
    key = str(value or "")
    if not key:
        return ""
    return key[:4] + "****" + key[-4:] if len(key) > 8 else "****"


def _validate_provider_key(provider: str, api_key: str) -> None:
    handler = handler_for_provider(provider)
    if not handler:
        raise ValueError(f"Unknown model provider: {provider}")
    detected = detect_provider_from_key(api_key)
    if provider == "openrouter":
        if detected != "openrouter":
            raise ValueError("OpenRouter official token must start with sk-or-.")
        return
    if detected == "openrouter":
        raise ValueError("Official native provider tokens cannot be OpenRouter keys.")
    if detected and detected != provider:
        if detected == "openai" and handler.generic_sk:
            return
        raise ValueError(
            f"Token prefix looks like {detected}, but provider is {provider}."
        )
    if handler.key_prefixes and not any(api_key.startswith(prefix) for prefix in handler.key_prefixes):
        # Providers like Mistral/Kling/Volcengine have no stable public prefix;
        # handlers with prefixes should be validated to catch obvious mistakes.
        raise ValueError(
            f"{handler.display_name} token must start with one of: {', '.join(handler.key_prefixes)}"
        )


async def _get_row(db: AsyncSession, provider: str) -> PlatformModelProviderKey | None:
    return (await db.execute(
        select(PlatformModelProviderKey).where(
            PlatformModelProviderKey.provider == provider,
        )
    )).scalar_one_or_none()


async def list_official_provider_key_statuses(db: AsyncSession) -> list[dict[str, Any]]:
    rows = {
        row.provider: row
        for row in (await db.execute(select(PlatformModelProviderKey))).scalars().all()
    }
    items: list[dict[str, Any]] = []
    for item in provider_catalog():
        provider = item["provider"]
        handler = handler_for_provider(provider)
        if not handler:
            continue
        row = rows.get(provider)
        env_value, env_name = official_env_key(provider)
        configured = bool((row and row.credential_ref and row.status == "active") or env_value)
        config = dict(row.config or {}) if row else {}
        items.append({
            **item,
            "configured": configured,
            "db_configured": bool(row and row.credential_ref and row.status == "active"),
            "db_key_present": bool(row and row.credential_ref),
            "env_configured": bool(env_value),
            "env_var": env_name or None,
            "status": row.status if row else ("env" if env_value else "missing"),
            "masked": config.get("masked") or (mask_provider_api_key(env_value) if env_value else ""),
            "base_url": config.get("base_url") or handler.base_url,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "last_rotated_at": row.last_rotated_at.isoformat() if row and row.last_rotated_at else None,
        })
    return items


async def upsert_official_provider_key(
    db: AsyncSession,
    *,
    provider: str,
    api_key: str,
    base_url: str | None = None,
    enabled: bool = True,
    actor_user_id: str | None = None,
) -> PlatformModelProviderKey:
    provider = str(provider or "").strip().lower()
    handler = handler_for_provider(provider)
    if not handler:
        raise ValueError(f"Unknown model provider: {provider}")
    key = sanitize_provider_api_key(api_key, source=f"{provider}.official_api_key")
    if not key:
        raise ValueError("API token is empty or malformed.")
    _validate_provider_key(provider, key)

    row = await _get_row(db, provider)
    now = datetime.now(timezone.utc)
    if row is None:
        row = PlatformModelProviderKey(
            id=generate_ulid(),
            provider=provider,
            display_name=handler.display_name,
            status="active" if enabled else "inactive",
            config={},
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        db.add(row)
        await db.flush()

    cfg = dict(row.config or {})
    resolved_base_url = str(base_url or "").strip().rstrip("/") or handler.base_url
    cfg["base_url"] = resolved_base_url
    cfg["masked"] = mask_provider_api_key(key)
    row.display_name = handler.display_name
    row.status = "active" if enabled else "inactive"
    row.config = cfg
    row.updated_by = actor_user_id
    row.last_rotated_at = now
    get_credential_service().store_model_provider_key(row, {"api_key": key})
    await db.flush()
    return row


async def set_official_provider_key_status(
    db: AsyncSession,
    *,
    provider: str,
    enabled: bool,
    actor_user_id: str | None = None,
) -> PlatformModelProviderKey:
    """Enable/disable the stored official token without re-entering it.

    Only valid for providers with a DB-stored token — env-configured
    providers have nothing to toggle here."""
    provider = str(provider or "").strip().lower()
    if not handler_for_provider(provider):
        raise ValueError(f"Unknown model provider: {provider}")
    row = await _get_row(db, provider)
    if not row or not row.credential_ref:
        raise ValueError("No stored token for this provider — set a token first.")
    row.status = "active" if enabled else "inactive"
    row.updated_by = actor_user_id
    await db.flush()
    return row


async def delete_official_provider_key(
    db: AsyncSession,
    *,
    provider: str,
    actor_user_id: str | None = None,
) -> PlatformModelProviderKey | None:
    provider = str(provider or "").strip().lower()
    row = await _get_row(db, provider)
    if not row:
        return None
    row.status = "inactive"
    row.credential_ref = None
    row.config = {
        key: value
        for key, value in dict(row.config or {}).items()
        if key not in {"masked"}
    }
    row.updated_by = actor_user_id
    await db.flush()
    return row


async def resolve_official_provider_credential(
    provider: str,
    *,
    reason: str = "model_provider.official_key",
) -> OfficialProviderCredential | None:
    """Resolve the platform official credential for a provider.

    DB-configured admin tokens win. Env vars remain a bootstrap/fallback path
    so deployments keep working before the admin page is configured.
    """

    provider = str(provider or "").strip().lower()
    handler = handler_for_provider(provider)
    if not handler:
        return None

    try:
        from packages.core.database import async_session

        async with async_session() as db:
            row = await _get_row(db, provider)
            if row and row.status == "active" and row.credential_ref:
                payload = get_credential_service().lease_model_provider_key(
                    row,
                    requester=Requester(kind="system", id=f"model_provider:{provider}"),
                    reason=reason,
                )
                key = sanitize_provider_api_key(
                    str(payload.get("api_key") or ""),
                    source=f"{provider}.official_db_key",
                )
                if key:
                    cfg = dict(row.config or {})
                    return OfficialProviderCredential(
                        provider=provider,
                        api_key=key,
                        base_url=str(cfg.get("base_url") or handler.base_url).rstrip("/"),
                        source="official",
                        source_detail="db",
                    )
    except SQLAlchemyError:
        logger.debug("Official model provider key DB lookup failed for %s", provider, exc_info=True)
    except Exception:
        logger.warning("Official model provider key lookup failed for %s", provider, exc_info=True)

    key, env_name = official_env_key(provider)
    key = sanitize_provider_api_key(key, source=env_name or f"{provider}.env")
    if key:
        return OfficialProviderCredential(
            provider=provider,
            api_key=key,
            base_url=handler.base_url,
            source="official",
            source_detail=env_name,
        )
    return None
