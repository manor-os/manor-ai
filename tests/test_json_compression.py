"""Only finished JSON is compressed; streams and files must keep their bytes."""
import gzip
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from apps.api.middleware.json_compression import JSONCompressionMiddleware

pytestmark = pytest.mark.asyncio

LARGE = {"items": ["workflow step"] * 500}


async def _large(_request):
    return JSONResponse(LARGE)


async def _small(_request):
    return JSONResponse({"ok": True})


async def _events(_request):
    async def stream():
        yield b"data: first\n\n"
        yield b"data: second\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


async def _video(_request):
    return Response(
        b"x" * 5000,
        status_code=206,
        media_type="video/mp4",
        headers={"Content-Range": "bytes 0-4999/9000"},
    )


async def _passthrough(request, call_next):
    return await call_next(request)


def _client() -> httpx.AsyncClient:
    app = Starlette(routes=[
        Route("/large", _large),
        Route("/small", _small),
        Route("/events", _events),
        Route("/video", _video),
    ])
    # Production has these inside it, and they split a body into several messages.
    app.add_middleware(BaseHTTPMiddleware, dispatch=_passthrough)
    app.add_middleware(JSONCompressionMiddleware)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get_raw(client: httpx.AsyncClient, path: str, encoding: str = "gzip") -> httpx.Response:
    async with client.stream("GET", path, headers={"Accept-Encoding": encoding}) as response:
        response.raw_body = b"".join([chunk async for chunk in response.aiter_raw()])
    return response


@pytest.mark.parametrize("encoding", ["gzip", "br, GZIP; q=0.5", "*;q=0.8"])
async def test_large_json_is_compressed_with_matching_headers(encoding):
    async with _client() as client:
        response = await _get_raw(client, "/large", encoding=encoding)
    assert response.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in response.headers["vary"].lower()
    assert int(response.headers["content-length"]) == len(response.raw_body)
    assert json.loads(gzip.decompress(response.raw_body)) == LARGE
    assert len(response.raw_body) < len(json.dumps(LARGE)) / 5


@pytest.mark.parametrize("encoding", ["identity", "gzip;q=0, *;q=1", "x-gzip-test"])
async def test_client_without_gzip_gets_plain_json(encoding):
    async with _client() as client:
        response = await _get_raw(client, "/large", encoding=encoding)
    assert "content-encoding" not in response.headers
    assert "accept-encoding" in response.headers["vary"].lower()
    assert json.loads(response.raw_body) == LARGE


async def test_small_json_stays_plain():
    async with _client() as client:
        response = await _get_raw(client, "/small")
    assert "content-encoding" not in response.headers
    assert json.loads(response.raw_body) == {"ok": True}


@pytest.mark.parametrize("path", ["/events", "/video"])
async def test_streams_and_media_keep_their_bytes(path):
    async with _client() as client:
        response = await _get_raw(client, path)
    assert "content-encoding" not in response.headers
    if path == "/video":
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 0-4999/9000"
        assert len(response.raw_body) == 5000
    else:
        assert response.raw_body == b"data: first\n\ndata: second\n\n"
