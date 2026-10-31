"""API for the in-sandbox ``manor`` CLI (``/api/v1/sandbox-cli``).

Only the CLI calls this. Each request carries the short-lived token one bash
command was given (``Authorization: Bearer mcli_...``); the token's claims,
never the request body, decide who the caller is. Operations live in
``packages.core.services.sandbox_cli.operations`` and reuse the file tools'
ACL services. The token itself is never logged: only its SHA-256 is used.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from packages.core.services.sandbox_cli import operations
from packages.core.services.sandbox_cli.tokens import (
    SandboxCliClaims,
    resolve_sandbox_cli_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sandbox-cli", tags=["sandbox-cli"], include_in_schema=False)

_MAX_BODY_BYTES = 64 * 1024


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": message})


async def _claims(request: Request) -> SandboxCliClaims | JSONResponse:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return _error(401, "token_required", "A sandbox CLI token is required.")
    claims = await resolve_sandbox_cli_token(token.strip())
    if claims is None:
        return _error(
            401,
            "token_invalid",
            "The sandbox CLI token is unknown, expired or revoked. The manor CLI "
            "only works inside the bash command it was started from.",
        )
    from packages.core.ai.runtime.profiles import runtime_surface_is_internal

    if not runtime_surface_is_internal(claims.surface):
        return _error(403, "external_principal", "External principals cannot use the sandbox CLI.")
    return claims


async def _body(request: Request) -> dict[str, Any] | JSONResponse:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return _error(413, "request_too_large", "The CLI request body is too large.")
    if not raw:
        return {}
    import json

    try:
        payload = json.loads(raw)
    except ValueError:
        return _error(400, "invalid_request", "The CLI request body must be JSON.")
    if not isinstance(payload, dict):
        return _error(400, "invalid_request", "The CLI request body must be a JSON object.")
    return payload


async def _run(
    request: Request,
    handler: Callable[[SandboxCliClaims, dict[str, Any]], Awaitable[Any]],
    name: str,
) -> Response:
    claims = await _claims(request)
    if isinstance(claims, JSONResponse):
        return claims
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        result = await handler(claims, body)
    except operations.SandboxCliOperationError as exc:
        return _error(exc.status, exc.code, exc.message)
    except Exception:  # noqa: BLE001 - one opaque 500, details in the server log
        logger.exception(
            "[sandbox-cli] %s failed entity=%s sandbox=%s", name, claims.entity_id, claims.sandbox_id,
        )
        return _error(500, "internal_error", f"manor {name} failed on the server.")
    if isinstance(result, operations.PulledFile):
        return Response(
            content=result.data,
            media_type="application/octet-stream",
            headers={"X-Manor-Path": result.path, "X-Manor-Sha256": result.sha256},
        )
    return JSONResponse(content=result)


@router.post("/ls")
async def ls(request: Request) -> Response:
    return await _run(request, operations.cli_ls, "ls")


@router.post("/stat")
async def stat(request: Request) -> Response:
    return await _run(request, operations.cli_stat, "stat")


@router.post("/glob")
async def glob(request: Request) -> Response:
    return await _run(request, operations.cli_glob, "glob")


@router.post("/grep")
async def grep(request: Request) -> Response:
    return await _run(request, operations.cli_grep, "grep")


@router.post("/read")
async def read(request: Request) -> Response:
    return await _run(request, operations.cli_read, "cat")


@router.post("/search")
async def search(request: Request) -> Response:
    return await _run(request, operations.cli_search, "search")


@router.post("/pull")
async def pull(request: Request) -> Response:
    return await _run(request, operations.cli_pull, "pull")


@router.post("/ops")
async def queue_operation(request: Request) -> Response:
    return await _run(request, operations.cli_queue_operation, "file operation")


@router.post("/whoami")
async def whoami(request: Request) -> Response:
    async def handler(claims: SandboxCliClaims, _body: dict[str, Any]) -> dict[str, Any]:
        return operations.cli_whoami(claims)

    return await _run(request, handler, "whoami")
