"""API key management — entity-level LLM provider credentials."""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import api_key_service
from apps.api.deps import require_permission
from packages.core.permissions import Permission

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])


# ── Schemas ──

class ApiKeyCreateRequest(BaseModel):
    name: str
    provider: str  # openrouter, openai, anthropic, custom
    api_key: str
    base_url: str | None = None
    default_model: str | None = None
    is_default: bool = False


class ApiKeyUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    is_default: bool | None = None


class ApiKeyRotateRequest(BaseModel):
    new_api_key: str


class ApiKeyResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    provider: str
    key_prefix: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    is_default: bool
    status: str
    usage_count: int
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ApiKeyCreateResponse(ApiKeyResponse):
    """Returned only on create — includes the clear API key shown once."""
    api_key: str


class ResolvedConfigResponse(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    key_prefix: str | None = None
    key_id: str | None = None
    key_name: str | None = None
    source: str


# ── Helpers ──

def _to_response(key) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id,
        entity_id=key.entity_id,
        name=key.name,
        provider=key.provider,
        key_prefix=key.key_prefix,
        base_url=key.base_url,
        default_model=key.default_model,
        is_default=key.is_default,
        status=key.status,
        usage_count=key.usage_count,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


# ── Routes (fixed paths BEFORE parameterized paths) ──

@router.get("/resolve", response_model=ResolvedConfigResponse)
async def resolve_config(
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Show what LLM config would be used for this entity (no actual keys exposed)."""
    config = await api_key_service.resolve_llm_config(db, user.entity_id)
    return ResolvedConfigResponse(**config)


@router.get("", response_model=list[ApiKeyResponse])
async def list_keys(
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """List API keys for the current entity (key_hash never exposed)."""
    keys = await api_key_service.list_api_keys(db, user.entity_id)
    return [_to_response(k) for k in keys]


@router.post("", response_model=ApiKeyCreateResponse, status_code=201)
async def create_key(
    req: ApiKeyCreateRequest,
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Create an API key. The clear key is returned only once."""
    key, clear_key = await api_key_service.create_api_key(
        db,
        entity_id=user.entity_id,
        name=req.name,
        provider=req.provider,
        api_key=req.api_key,
        base_url=req.base_url,
        default_model=req.default_model,
        is_default=req.is_default,
    )
    await db.commit()
    resp = _to_response(key)
    return ApiKeyCreateResponse(**resp.model_dump(), api_key=clear_key)


@router.put("/{key_id}", response_model=ApiKeyResponse)
async def update_key(
    key_id: str,
    req: ApiKeyUpdateRequest,
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Update name, base_url, default_model, or is_default."""
    key = await api_key_service.update_api_key(
        db, key_id, user.entity_id,
        name=req.name,
        base_url=req.base_url,
        default_model=req.default_model,
        is_default=req.is_default,
    )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    await db.commit()
    return _to_response(key)


@router.delete("/{key_id}", status_code=204)
async def delete_key(
    key_id: str,
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key."""
    ok = await api_key_service.revoke_api_key(db, key_id, user.entity_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    await db.commit()


@router.post("/{key_id}/rotate", response_model=ApiKeyResponse)
async def rotate_key(
    key_id: str,
    req: ApiKeyRotateRequest,
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Rotate (replace) the secret for an API key."""
    key = await api_key_service.rotate_api_key(
        db, key_id, user.entity_id, req.new_api_key,
    )
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    await db.commit()
    return _to_response(key)


@router.post("/{key_id}/test", response_model=dict)
async def test_key(
    key_id: str,
    user: User = Depends(require_permission(Permission.ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_db),
):
    """Test an API key by making a simple completion call.

    Note: This endpoint validates the key exists and is active.
    A full live test would require decrypting the key, which we don't store.
    The key_hash is one-way (bcrypt), so we can only verify status.
    """
    key = await api_key_service.get_api_key_by_id(db, key_id, user.entity_id)
    if not key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if key.status != "active":
        return {"ok": False, "error": "Key is revoked"}
    return {
        "ok": True,
        "provider": key.provider,
        "key_prefix": key.key_prefix,
        "message": "Key is active. Provide the raw key in the request to perform a live test.",
    }
