"""Short-lived file transfer slots between a coding agent's shell and MCP tools.

Blueprint payloads reach megabytes, too large to pass through a model's tool
arguments or results. A tool instead returns a capability URL: the agent runs
``curl`` to download an export or upload a local file, and later tool calls refer
to the upload by its id. Slots live in Redis for 30 minutes and are bound to the
user and team that created them; the URL token is the only credential.
"""
from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Optional

from apps.api.mcp.registry import ToolError

TRANSFER_TTL_SECONDS = 30 * 60
MAX_TRANSFER_BYTES = 20 * 1024 * 1024
_KEY = "manor:mcp:transfer:"

DOWNLOAD = "download"
UPLOAD = "upload"


class TransferUnavailable(ToolError):
    """Redis is not reachable, so no transfer slot can be created or read."""


@dataclass
class Transfer:
    id: str
    kind: str
    user_id: str
    entity_id: str
    filename: str
    content_type: str
    data: Optional[bytes]


async def _redis():
    from packages.core.cache import _get_redis

    client = await _get_redis()
    if client is None:
        raise TransferUnavailable("file transfer needs Redis, which is unavailable")
    return client


def _encode(transfer: Transfer) -> str:
    return json.dumps({
        "kind": transfer.kind,
        "user_id": transfer.user_id,
        "entity_id": transfer.entity_id,
        "filename": transfer.filename,
        "content_type": transfer.content_type,
        "data": None if transfer.data is None else base64.b64encode(transfer.data).decode("ascii"),
    })


def _decode(transfer_id: str, raw: str) -> Transfer:
    value = json.loads(raw)
    data = value.get("data")
    return Transfer(
        id=transfer_id,
        kind=value["kind"],
        user_id=value["user_id"],
        entity_id=value["entity_id"],
        filename=value["filename"],
        content_type=value["content_type"],
        data=None if data is None else base64.b64decode(data),
    )


async def create(
    *, kind: str, user_id: str, entity_id: str, filename: str,
    content_type: str, data: Optional[bytes] = None,
) -> Transfer:
    if data is not None and len(data) > MAX_TRANSFER_BYTES:
        raise ToolError(f"file exceeds {MAX_TRANSFER_BYTES // (1024 * 1024)} MiB")
    transfer = Transfer(
        id=secrets.token_urlsafe(32), kind=kind, user_id=user_id, entity_id=entity_id,
        filename=filename, content_type=content_type, data=data,
    )
    await (await _redis()).set(_KEY + transfer.id, _encode(transfer), ex=TRANSFER_TTL_SECONDS)
    return transfer


async def load(transfer_id: str) -> Optional[Transfer]:
    if not transfer_id or len(transfer_id) > 128:
        return None
    raw = await (await _redis()).get(_KEY + transfer_id)
    return None if raw is None else _decode(transfer_id, raw)


async def store_upload(transfer: Transfer, data: bytes, content_type: Optional[str]) -> None:
    if len(data) > MAX_TRANSFER_BYTES:
        raise ValueError(f"file exceeds {MAX_TRANSFER_BYTES // (1024 * 1024)} MiB")
    transfer.data = data
    # curl --data-binary labels every body as a form post; keep the declared type then.
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/x-www-form-urlencoded":
        transfer.content_type = declared
    await (await _redis()).set(_KEY + transfer.id, _encode(transfer), ex=TRANSFER_TTL_SECONDS)


async def load_upload_for(transfer_id: str, *, user_id: str, entity_id: str) -> Transfer:
    """The uploaded bytes of a slot this caller created, or ValueError."""
    transfer = await load(transfer_id)
    if (transfer is None or transfer.kind != UPLOAD
            or transfer.user_id != user_id or transfer.entity_id != entity_id):
        raise ValueError("unknown or expired upload_id")
    if transfer.data is None:
        raise ValueError("nothing has been uploaded to this upload_id yet")
    return transfer
