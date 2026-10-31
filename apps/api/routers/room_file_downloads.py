"""Bounded-memory, room-authorized downloads for browser download managers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import tempfile
import time
from urllib.parse import quote
from zipfile import ZIP_STORED, ZipFile

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import authenticated_user_credential_from_claims, get_current_user
from apps.api.file_responses import PinnedFileStreamingResponse, room_file_snapshot_budget
from packages.core.config import get_settings
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.actor_authorization import AuthenticatedUserCredential

router = APIRouter(prefix="/api/v1/chat/rooms", tags=["chat-rooms"])
ROOM_FILE_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024 * 1024
_budget = room_file_snapshot_budget
_ID = re.compile(r"[0-9A-Z]{26}\Z")


class RoomFileDownloadRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=100)
    archive: StrictBool = True


def _sign(payload: str) -> str:
    # Separate domain from all existing auth and entity-file bearer tokens.
    return hmac.new(get_settings().JWT_SECRET_KEY.encode(),
                    b"room-file-download-v1\x00" + payload.encode("ascii"), hashlib.sha256).hexdigest()


def _ticket(room_id: str, body: RoomFileDownloadRequest, credential: AuthenticatedUserCredential) -> str:
    expires = min(time.time() + 600, credential.expires_at_epoch or float("inf"))
    claims = {"room": room_id, "files": body.file_ids, "archive": body.archive,
              "sub": credential.user_id, "entity_id": credential.entity_id,
              "token_version": credential.token_version, "exp": expires}
    if credential.impersonation_session_id:
        claims.update(typ="impersonation", impersonation_session_id=credential.impersonation_session_id)
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    return encoded + "." + _sign(encoded)


def _read_ticket(token: str, room_id: str):
    try:
        encoded, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(encoded)):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if claims["room"] != room_id or claims["exp"] <= time.time():
            raise ValueError("expired or foreign scope")
        body = RoomFileDownloadRequest(file_ids=claims["files"], archive=claims["archive"])
        credential = authenticated_user_credential_from_claims(
            user_id=claims["sub"], entity_id=claims["entity_id"], claims=claims,
        )
        return body, credential
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(403, "Download link is invalid or expired") from None


async def _selection(db, room_id, body, credential):
    from apps.api.routers.chat_rooms import _authorized_room_download

    if (any(not _ID.fullmatch(file_id) for file_id in body.file_ids)
            or len(set(body.file_ids)) != len(body.file_ids)
            or (not body.archive and len(body.file_ids) != 1)):
        raise HTTPException(422, "Choose unique group files")
    rows = []
    total = 0
    for file_id in body.file_ids:
        row, _ = await _authorized_room_download(db, room_id, file_id, credential, metadata_only=True)
        total += row.size
        if total > ROOM_FILE_DOWNLOAD_MAX_BYTES:
            raise HTTPException(413, "Selected files must total 5 GiB or less")
        # Keep metadata only. Legacy inline content must not accumulate across
        # the entire selection in either Python or the SQLAlchemy identity map.
        rows.append((row.id, row.name, row.size, row.content_sha256, row.document_id))
        db.expunge(row)
    return rows, total


def _archive_names(rows):
    names = []
    for row in rows:
        name = row[1]
        name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", re.split(r"[\\/]", name)[-1]).rstrip(". ") or "file"
        if re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name):
            name = "_" + name
        names.append(name)
    reserved, used, result = {name.casefold() for name in names}, set(), []
    for name in names:
        candidate, suffix = name, 2
        stem, dot, extension = name.rpartition(".")
        if not stem:
            stem, dot, extension = name, "", ""
        while candidate.casefold() in used:
            candidate = f"{stem} ({suffix}){dot}{extension}"
            suffix += 1
            while candidate.casefold() in reserved:
                candidate = f"{stem} ({suffix}){dot}{extension}"
                suffix += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result


class _BudgetedDownload(PinnedFileStreamingResponse):
    def __init__(self, handle, reserved_size, **kwargs):
        super().__init__(handle, on_close=lambda: _budget.release(reserved_size), **kwargs)


@router.post("/{room_id}/file-downloads")
async def prepare_room_file_download(
    room_id: str, body: RoomFileDownloadRequest, response: Response,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    credential = AuthenticatedUserCredential.from_user(user)
    await _selection(db, room_id, body, credential)
    await db.commit()
    response.headers["Cache-Control"] = "private, no-store"
    return {"download_url": f"/api/v1/chat/rooms/{room_id}/file-downloads?ticket={_ticket(room_id, body, credential)}"}


@router.get("/{room_id}/file-downloads")
async def stream_room_file_download(
    room_id: str, request: Request, ticket: str = Query(max_length=7000), db: AsyncSession = Depends(get_db),
):
    from apps.api.routers.chat_rooms import _room_file_download_response

    body, credential = _read_ticket(ticket, room_id)
    rows, total = await _selection(db, room_id, body, credential)
    await db.commit()
    if not body.archive:
        return await _room_file_download_response(db, room_id, rows[0][0], credential)

    # One final ZIP plus one verified source snapshot. No multi-GiB ArrayBuffer
    # or BytesIO, and concurrent downloads cannot fill temporary disk unboundedly.
    reserved = total + 1024 * 1024
    if not await _budget.reserve(reserved, timeout=0):
        raise HTTPException(503, "File downloads are busy; retry shortly", headers={"Retry-After": "5"})
    output = None
    try:
        output = tempfile.TemporaryFile(mode="w+b")
        with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            for row, name in zip(rows, _archive_names(rows)):
                file_id, _, size = row[:3]
                if await request.is_disconnected():
                    raise HTTPException(499, "Download cancelled")
                response = await _room_file_download_response(db, room_id, file_id, credential)
                try:
                    def copy_entry():
                        copied = 0
                        with archive.open(name, "w", force_zip64=True) as entry:
                            if isinstance(response, PinnedFileStreamingResponse):
                                while chunk := response._handle.read(1024 * 1024):
                                    copied += len(chunk)
                                    if copied > size:
                                        raise HTTPException(409, "File changed after selection")
                                    entry.write(chunk)
                            else:
                                # Old inline rows are capped at 100 MiB.
                                data = memoryview(response.body)
                                copied = len(data)
                                if copied > size:
                                    raise HTTPException(409, "File changed after selection")
                                for offset in range(0, copied, 1024 * 1024):
                                    entry.write(data[offset:offset + 1024 * 1024])
                        if copied != size:
                            raise HTTPException(409, "File changed after selection")
                    await anyio.to_thread.run_sync(copy_entry)
                    if await request.is_disconnected():
                        raise HTTPException(499, "Download cancelled")
                finally:
                    if isinstance(response, PinnedFileStreamingResponse):
                        response.close()
        # Revocation during preparation must not release previously copied files.
        db.expire_all()
        current, _ = await _selection(db, room_id, body, credential)
        if current != rows:
            raise HTTPException(409, "Files changed after selection")
        await db.commit()
        length = output.tell()
        output.seek(0)
        return _BudgetedDownload(output, reserved, media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote('group-files.zip')}",
            "Content-Length": str(length), "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
            "Referrer-Policy": "no-referrer",
        })
    except BaseException:
        if output is not None:
            output.close()
        _budget.release(reserved)
        raise
