"""A recording plays from a stream address while it downloads, as the person who opened it."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.document import Document
from packages.core.models.user import User


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com", "password": "pass123",
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _enable_filesystem(monkeypatch, root) -> None:
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings

    for current in {id(item): item for item in (get_settings(), documents_router.settings)}.values():
        monkeypatch.setattr(current, "MANOR_FS_ROOT", str(root))
        monkeypatch.setattr(current, "MANOR_FS_ENABLED", True)


@pytest.mark.asyncio
async def test_a_recording_streams_in_slices_and_stops_when_the_sign_in_is_revoked(
    client: AsyncClient, db_session, monkeypatch, tmp_path,
):
    from apps.api.routers import documents as documents_router

    headers = await _auth(client, "stream_viewer")
    user = await db_session.scalar(select(User).order_by(User.created_at.desc()).limit(1))
    _enable_filesystem(monkeypatch, tmp_path)
    monkeypatch.setattr(documents_router, "_STREAM_RANGE_SLICE_BYTES", 4)
    (tmp_path / user.entity_id / "media").mkdir(parents=True)
    (tmp_path / user.entity_id / "media" / "clip.mp4").write_bytes(b"0123456789")
    clip = Document(entity_id=user.entity_id, name="clip.mp4", fs_path="media/clip.mp4", file_type="mp4",
                    mime_type="video/mp4", owner_id=user.id)
    (tmp_path / user.entity_id / "media" / "other.mp4").write_bytes(b"other")
    other = Document(entity_id=user.entity_id, name="other.mp4", fs_path="media/other.mp4", file_type="mp4",
                     mime_type="video/mp4", owner_id=user.id)
    db_session.add_all([clip, other])
    await db_session.commit()

    issued = await client.get(f"/api/v1/documents/{clip.id}/stream-url", headers=headers)
    assert issued.status_code == 200, issued.text
    url = issued.json()["url"]
    assert url.startswith(f"/api/v1/documents/{clip.id}/stream?t=")

    # The player's open-ended first range gets the first slice; it asks again for the rest.
    first = await client.get(url, headers={"Range": "bytes=0-"})
    assert first.status_code == 206, first.text
    assert first.content == b"0123"
    assert first.headers["content-range"] == "bytes 0-3/10"
    assert first.headers["content-disposition"] == "inline"
    later = await client.get(url, headers={"Range": "bytes=8-"})
    assert (later.status_code, later.content, later.headers["content-range"]) == (206, b"89", "bytes 8-9/10")
    whole = await client.get(url)
    assert (whole.status_code, whole.content) == (200, b"0123456789")
    # The bearer read surface still answers a range in full.
    preview = await client.get(f"/api/v1/documents/{clip.id}/preview/content", headers={**headers, "Range": "bytes=0-"})
    assert (preview.status_code, preview.content) == (206, b"0123456789")

    # The address is for this Document only, and only as signed.
    token = url.split("t=", 1)[1]
    assert (await client.get(f"/api/v1/documents/{other.id}/stream?t={token}")).status_code == 401
    assert (await client.get(f"{url[:-2]}xx")).status_code == 401

    # Signing out everywhere (a new token version) stops an address already handed out.
    user.token_version = int(user.token_version or 0) + 1
    await db_session.commit()
    assert (await client.get(url, headers={"Range": "bytes=0-"})).status_code == 401
