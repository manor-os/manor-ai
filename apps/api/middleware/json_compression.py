"""Gzip finished JSON responses, and leave every other response exactly as it is."""
from __future__ import annotations

import asyncio
import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MINIMUM_SIZE = 1024
COMPRESS_LEVEL = 5
# Bigger bodies take long enough to hold up other requests on the event loop.
THREAD_SIZE = 64 * 1024


class JSONCompressionMiddleware:
    """Compress only JSON whose length is known up front.

    Streams (SSE, exports) have no Content-Length, and files and media must keep
    their bytes so Range requests stay valid, so both pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        accepts_gzip = _accepts_gzip(Headers(scope=scope).getlist("accept-encoding"))
        held_start: Message | None = None
        chunks: list[bytes] = []

        async def compressing_send(message: Message) -> None:
            nonlocal held_start
            if message["type"] == "http.response.start":
                if _should_compress(Headers(raw=message["headers"])):
                    MutableHeaders(raw=message["headers"]).add_vary_header("Accept-Encoding")
                    if accepts_gzip:
                        held_start = message
                        return
                await send(message)
                return
            if held_start is None or message["type"] != "http.response.body":
                await send(message)
                return

            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            body = b"".join(chunks)
            if body:
                body = await _compress(body)
                headers = MutableHeaders(raw=held_start["headers"])
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(body))
            await send(held_start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, compressing_send)


def _accepts_gzip(values: list[str]) -> bool:
    wildcard = False
    for token in ",".join(values).split(","):
        encoding, separator, weight = token.lower().partition(";")
        encoding = encoding.strip()
        if encoding not in {"gzip", "*"}:
            continue
        allowed = not separator
        if separator:
            name, equals, quality = weight.partition("=")
            if name.strip() == "q" and equals:
                try:
                    allowed = 0 < float(quality) <= 1
                except ValueError:
                    pass
        if encoding == "gzip":
            return allowed
        wildcard = allowed
    return wildcard


def _should_compress(headers: Headers) -> bool:
    if "content-encoding" in headers or "content-range" in headers:
        return False
    if not headers.get("content-type", "").lower().startswith("application/json"):
        return False
    try:
        return int(headers.get("content-length", "")) >= MINIMUM_SIZE
    except ValueError:
        return False


async def _compress(body: bytes) -> bytes:
    if len(body) < THREAD_SIZE:
        return gzip.compress(body, COMPRESS_LEVEL, mtime=0)
    return await asyncio.to_thread(gzip.compress, body, COMPRESS_LEVEL, mtime=0)
