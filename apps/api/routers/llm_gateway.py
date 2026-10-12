"""OpenAI-compatible LLM gateway for downstream Manor apps.

Downstream products (Minutes, PMS, ...) that let people sign in with a Manor
account should not hold their own provider keys for those people, nor report
usage to Manor after the fact. They call this gateway instead:

    POST /api/v1/llm/chat/completions   OpenAI ``chat.completions`` wire format
    GET  /api/v1/llm/credit             preflight: 200 when the entity may spend

Cloud resolves the model route with Manor's platform credentials; OSS resolves
the billed entity's BYOK route. The gateway runs the call through
``chat_completion`` and records usage through the same ``llm_billing_context``
first-party chat uses, so credits land on the caller's entity ledger and the
credit gate is enforced before any provider tokens are spent.

Two ways to authenticate:

* ``Authorization: Bearer <Manor access token>`` — the JWT the first-party web
  app or the OAuth ``/token`` endpoint hands out. Usage is attributed to that
  user's entity.
* OAuth client credentials — for server-side callers (background workers)
  acting for a Manor user without a live user token::

      X-Manor-Client-Id:     <registered oauth_client_apps.client_id>
      X-Manor-Client-Secret: <its secret>
      X-Manor-Entity-Id:     <entity to bill>
      X-Manor-User-Id:       <user for attribution, optional>

  The client must be active. OSS machine clients must be explicitly restricted
  to the exact billed entity; a supplied user is optional but must be an active
  member of that entity. Cloud retains its existing client restriction rules.

``X-Manor-Business-Type`` (optional, either auth mode) labels the usage rows so
the Manor usage views can break spend down per product feature.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, security
from packages.core.ai.llm_client import (
    CreditCheckUnavailableError,
    CreditExhaustedError,
    LLMAuthConfigurationError,
    assert_credit_available,
    chat_completion,
    get_llm_model,
    llm_billing_context,
    resolve_llm_routing_for_model,
    _validate_llm_key_model_compatibility,
)
from packages.core.constants.plans import is_cloud
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.services.auth_service import get_user_by_id
from packages.core.services.oauth_provider_service import verify_client_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/llm", tags=["llm-gateway"])

DEFAULT_SOURCE = "llm_gateway"
_BUSINESS_TYPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


# ── Principal resolution ──


@dataclass(frozen=True)
class GatewayPrincipal:
    """Who is billed for a gateway call, and how the usage row is labelled."""

    entity_id: str
    user_id: str | None
    source: str
    client_id: str | None = None


@dataclass(frozen=True)
class GatewayModelRoute:
    model: str
    metadata: dict[str, Any] | None
    byok: bool


def _business_type(request: Request, fallback: str) -> str:
    raw = (request.headers.get("x-manor-business-type") or "").strip()
    if not raw:
        return fallback
    if not _BUSINESS_TYPE_RE.match(raw):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_business_type")
    return raw


async def resolve_gateway_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> GatewayPrincipal:
    client_id = (request.headers.get("x-manor-client-id") or "").strip()
    if client_id:
        return await _resolve_client_principal(request, db, client_id)

    user = await get_current_user(request, credentials, db)
    return GatewayPrincipal(
        entity_id=str(user.entity_id),
        user_id=str(user.id),
        source=_business_type(request, DEFAULT_SOURCE),
    )


async def _resolve_client_principal(
    request: Request, db: AsyncSession, client_id: str
) -> GatewayPrincipal:
    client_secret = (request.headers.get("x-manor-client-secret") or "").strip()
    client = await verify_client_credentials(db, client_id, client_secret)
    if client is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_client")

    entity_id = (request.headers.get("x-manor-entity-id") or "").strip()
    if not entity_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing_entity_id")
    if not is_cloud() and client.restricted_entity_id != entity_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "entity_restriction_required_for_client")
    if client.restricted_entity_id and client.restricted_entity_id != entity_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "entity_not_allowed_for_client")

    entity = (
        await db.execute(select(Entity).where(Entity.id == entity_id))
    ).scalar_one_or_none()
    if entity is None or getattr(entity, "deleted_at", None) is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown_entity")

    user_id: str | None = (request.headers.get("x-manor-user-id") or "").strip() or None
    if user_id:
        user = await get_user_by_id(db, user_id)
        if user is None or not await _client_user_has_active_entity_membership(db, user, entity_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user_not_in_entity")
        if user.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    return GatewayPrincipal(
        entity_id=entity_id,
        user_id=user_id,
        source=_business_type(request, client.client_id),
        client_id=client.client_id,
    )


async def _client_user_has_active_entity_membership(
    db: AsyncSession, user: Any, entity_id: str,
) -> bool:
    """Use the ordinary actor resolver for an optional client attribution user."""
    from packages.core.services.actor_authorization import (
        AuthenticatedUserCredential,
        resolve_current_user_actor,
    )

    actor = await resolve_current_user_actor(
        db,
        replace(AuthenticatedUserCredential.from_user(user), entity_id=entity_id),
    )
    return actor is not None and actor.entity_id == entity_id


# ── Wire models ──


class ChatCompletionRequest(BaseModel):
    """The subset of OpenAI ``chat.completions`` the gateway honours.

    Unknown fields (``n``, ``tools``, ``user``, ...) are ignored rather than
    rejected so stock OpenAI SDKs can talk to the gateway unchanged.
    """

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[dict[str, Any]] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    reasoning_effort: str | None = None


def _usage_block(usage: dict[str, Any]) -> dict[str, int]:
    prompt = int(usage.get("prompt") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total") or usage.get("total_tokens") or 0) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _resolved_model(usage: dict[str, Any], requested: str | None) -> str:
    return str(usage.get("model") or requested or get_llm_model())


def _finish_reason(usage: dict[str, Any]) -> str:
    return str(usage.get("finish_reason") or "stop")


def _completion_kwargs(body: ChatCompletionRequest, *, model: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model if model is not None else body.model or None,
        "max_tokens": body.max_tokens,
        "response_format": body.response_format,
        "reasoning_effort": body.reasoning_effort,
    }
    if body.temperature is not None:
        kwargs["temperature"] = body.temperature
    return kwargs


def _provider_failure(usage: dict[str, Any]) -> HTTPException:
    detail = str(usage.get("error") or "LLM call failed")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail)


async def _preflight(principal: GatewayPrincipal) -> None:
    """Map the credit gate to HTTP before any provider tokens are spent."""
    try:
        await assert_credit_available(
            principal.entity_id,
            user_id=principal.user_id,
            source=principal.source,
        )
    except CreditExhaustedError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except CreditCheckUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


async def _resolve_gateway_model_route(
    principal: GatewayPrincipal,
    requested_model: str | None,
) -> GatewayModelRoute:
    """Resolve the self-hosted entity's BYOK route before credit/provider work."""
    from packages.core.services.model_resolver import (
        resolve_llm_metadata_for_user,
        resolve_model_for_user,
    )

    model = await resolve_model_for_user(
        "primary",
        user_id=principal.user_id,
        entity_id=principal.entity_id,
    )
    if requested_model and requested_model != model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "requested_model_not_allowed")
    metadata = await resolve_llm_metadata_for_user(
        "primary",
        user_id=principal.user_id,
        entity_id=principal.entity_id,
    )
    routing = await resolve_llm_routing_for_model(model, metadata)
    if not routing.api_key or not routing.is_byok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "llm_byok_not_configured")
    try:
        _validate_llm_key_model_compatibility(routing.api_key, routing.base_url, model)
    except LLMAuthConfigurationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return GatewayModelRoute(model=model, metadata=metadata, byok=True)


def _billing_context_kwargs(principal: GatewayPrincipal, route: GatewayModelRoute) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_id": principal.user_id,
        "source": principal.source,
    }
    if route.byok:
        kwargs["byok"] = True
    return kwargs


# ── Endpoints ──


@router.get("/credit")
async def credit_preflight(
    principal: GatewayPrincipal = Depends(resolve_gateway_principal),
) -> dict[str, Any]:
    """200 when the billed entity may spend credits right now, else 402/503.

    Lets a downstream app refuse expensive work (an upload that will need
    summarising later) up front instead of failing after transcription.
    """
    await _preflight(principal)
    return {
        "ok": True,
        "entity_id": principal.entity_id,
        "user_id": principal.user_id,
    }


@router.post("/chat/completions")
async def create_chat_completion(
    body: ChatCompletionRequest,
    principal: GatewayPrincipal = Depends(resolve_gateway_principal),
):
    route = (
        await _resolve_gateway_model_route(principal, body.model)
        if not is_cloud()
        else GatewayModelRoute(model=body.model or get_llm_model(), metadata=None, byok=False)
    )
    await _preflight(principal)
    if body.stream:
        return StreamingResponse(
            _stream_completion(body, principal, route),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with llm_billing_context(
            principal.entity_id, **_billing_context_kwargs(principal, route),
        ):
            content, usage = await chat_completion(
                body.messages,
                metadata=route.metadata,
                **_completion_kwargs(body, model=route.model),
            )
    except CreditExhaustedError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except CreditCheckUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    if not content and usage.get("error"):
        raise _provider_failure(usage)

    return {
        "id": f"chatcmpl-{generate_ulid()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": _resolved_model(usage, route.model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": _finish_reason(usage),
            }
        ],
        "usage": _usage_block(usage),
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk(
    completion_id: str,
    created: int,
    model: str,
    *,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        out["usage"] = usage
    return out


async def _stream_completion(
    body: ChatCompletionRequest, principal: GatewayPrincipal, route: GatewayModelRoute,
) -> AsyncIterator[str]:
    """Bridge ``chat_completion``'s stream handler onto OpenAI SSE chunks.

    The provider call runs in a task; ``text_delta`` events are forwarded as
    they arrive, and the final chunk carries ``finish_reason`` plus usage so
    SDK callers that request ``stream_options.include_usage`` see real token
    counts. Errors are emitted as an OpenAI-style ``{"error": ...}`` event,
    which stock SDKs surface as an exception.
    """
    completion_id = f"chatcmpl-{generate_ulid()}"
    created = int(time.time())
    model_label = route.model
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def handler(event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "text_delta":
            text = str(payload.get("content") or "")
            if text:
                await queue.put(("delta", text))
        elif event_name == "text_reset":
            await queue.put(("reset", None))

    async def run() -> None:
        try:
            async with llm_billing_context(
                principal.entity_id, **_billing_context_kwargs(principal, route),
            ):
                result = await chat_completion(
                    body.messages,
                    stream_handler=handler,
                    metadata=route.metadata,
                    **_completion_kwargs(body, model=route.model),
                )
            await queue.put(("done", result))
        except BaseException as exc:  # noqa: BLE001 - forwarded to the SSE consumer
            await queue.put(("error", exc))

    task = asyncio.create_task(run())
    emitted = ""
    reset_seen = False
    try:
        yield _sse(_chunk(completion_id, created, model_label, delta={"role": "assistant"}))
        while True:
            kind, data = await queue.get()
            if kind == "delta":
                emitted += data
                yield _sse(_chunk(completion_id, created, model_label, delta={"content": data}))
            elif kind == "reset":
                # Streaming transport failed mid-way; chat_completion retries
                # buffered and returns the full text in the final result.
                reset_seen = True
            elif kind == "error":
                exc = data
                if isinstance(exc, CreditExhaustedError):
                    err = {"message": str(exc), "type": "insufficient_credit", "code": 402}
                elif isinstance(exc, CreditCheckUnavailableError):
                    err = {"message": str(exc), "type": "credit_check_unavailable", "code": 503}
                else:
                    logger.exception("llm gateway stream failed")
                    err = {"message": f"{type(exc).__name__}: {exc}", "type": "server_error", "code": 500}
                yield _sse({"error": err})
                break
            else:  # done
                content, usage = data
                if not content and usage.get("error"):
                    yield _sse({"error": {"message": str(usage["error"]), "type": "server_error", "code": 502}})
                    break
                remainder = ""
                if content.startswith(emitted):
                    remainder = content[len(emitted):]
                elif reset_seen:
                    remainder = content
                if remainder:
                    yield _sse(_chunk(completion_id, created, model_label, delta={"content": remainder}))
                yield _sse(
                    _chunk(
                        completion_id,
                        created,
                        _resolved_model(usage, route.model),
                        delta={},
                        finish_reason=_finish_reason(usage),
                        usage=_usage_block(usage),
                    )
                )
                break
        yield "data: [DONE]\n\n"
    finally:
        if not task.done():
            task.cancel()
