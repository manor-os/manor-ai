"""Contract tests for the backend half of the error-i18n contract.

Locks in the shape of `CodedError`'s response body so the frontend can
rely on `{detail: {code, message, vars?}}`. If anyone changes the shape,
these tests catch it and the frontend translator stops working silently.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.errors import CodedError


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()

    @app.get("/owner-only")
    async def _owner_only() -> None:
        raise CodedError(
            403,
            code="permissions.error.doc.share_external_owner_only",
            message="Only the document owner or an admin can share externally",
        )

    @app.get("/with-vars")
    async def _with_vars() -> None:
        raise CodedError(
            400,
            code="permissions.error.share.unknown_caps",
            message="Unknown capabilities: ['frobnicate']",
            vars={"caps": "frobnicate"},
        )

    @app.get("/legacy-passthrough")
    async def _legacy() -> None:
        # CodedError must remain a true HTTPException so existing FastAPI
        # error handlers continue to work (logging middleware, sentry, etc).
        raise CodedError(
            410,
            code="permissions.error.share.expired",
            message="link expired",
        )

    return app


@pytest.mark.asyncio
async def test_coded_error_response_shape(app: FastAPI) -> None:
    """Body must be `{detail: {code, message}}` so the frontend can translate."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/owner-only")
    assert r.status_code == 403
    body = r.json()
    assert "detail" in body
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "permissions.error.doc.share_external_owner_only"
    assert "owner or an admin" in detail["message"]
    # vars omitted entirely (not null) when none supplied — keeps payload small
    assert "vars" not in detail


@pytest.mark.asyncio
async def test_coded_error_with_vars(app: FastAPI) -> None:
    """`vars` survives the round trip so the frontend can interpolate `{name}`."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/with-vars")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["vars"] == {"caps": "frobnicate"}


@pytest.mark.asyncio
async def test_coded_error_is_httpexception(app: FastAPI) -> None:
    """CodedError must still be an HTTPException — existing middleware (logging,
    sentry, plan-gate) expects FastAPI's standard error shape."""
    from fastapi import HTTPException

    err = CodedError(
        500,
        code="permissions.error.generic",
        message="boom",
    )
    assert isinstance(err, HTTPException)
    assert err.status_code == 500
