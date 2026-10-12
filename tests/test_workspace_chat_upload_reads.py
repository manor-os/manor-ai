"""Projected Workspace chat uploads retain their normal document read surface."""

import io

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _registered_user(db) -> User:
    return await db.scalar(select(User).order_by(User.created_at.desc()).limit(1))


def _enable_filesystem(monkeypatch, root) -> None:
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings

    seen: set[int] = set()
    for current in (get_settings(), documents_router.settings):
        if id(current) in seen:
            continue
        seen.add(id(current))
        monkeypatch.setattr(current, "MANOR_FS_ROOT", str(root))
        monkeypatch.setattr(current, "MANOR_FS_ENABLED", True)


async def _projected_chat_upload(
    db,
    *,
    entity_id: str,
    user_id: str,
    deleted: bool = False,
    visibility: str = "workspace",
    name: str = "cover.png",
    mime_type: str = "image/png",
    content: str = "projected upload text",
):
    workspace = Workspace(entity_id=entity_id, name="Workspace uploads")
    db.add(workspace)
    await db.flush()
    document = Document(
        entity_id=entity_id,
        name=name,
        fs_path=f"uploads/chat/{name}",
        file_type=name.rsplit(".", 1)[-1],
        mime_type=mime_type,
        source="chat_upload",
        owner_id=user_id,
        visibility=visibility,
        metadata_={
            "content": content,
            "origin": {"workspace_id": workspace.id, "user_id": user_id},
        },
    )
    group = DocumentGroup(
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Workspace uploads",
    )
    if deleted:
        from datetime import datetime, timezone

        workspace.deleted_at = datetime.now(timezone.utc)
    db.add_all([document, group])
    await db.flush()
    db.add(DocumentGroupMember(document_id=document.id, group_id=group.id))
    await db.commit()
    return document


@pytest.mark.asyncio
async def test_projected_workspace_chat_upload_reads_cached_preview_bytes_and_text(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob

    headers = await _auth(client, "projected_upload_reader")
    user = await _registered_user(db_session)
    document = await _projected_chat_upload(
        db_session, entity_id=user.entity_id, user_id=user.id,
    )

    async def cached_blob(*_args, **_kwargs):
        return CachedKnowledgeBlob(data=b"\x89PNG\r\nprojected", media_type="image/png")

    monkeypatch.setattr(documents_router, "get_cached_document_blob", cached_blob)

    preview = await client.get(
        f"/api/v1/documents/{document.id}/preview/content", headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.content == b"\x89PNG\r\nprojected"

    content = await client.get(f"/api/v1/documents/{document.id}/content", headers=headers)
    assert content.status_code == 200, content.text
    assert content.json() == {"content": "projected upload text"}


@pytest.mark.asyncio
async def test_projected_workspace_chat_upload_reads_original_preview_download_and_text(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    from PIL import Image

    headers = await _auth(client, "projected_upload_file_reader")
    user = await _registered_user(db_session)
    _enable_filesystem(monkeypatch, tmp_path)
    image_buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (12, 34, 56)).save(image_buffer, "PNG")
    image = image_buffer.getvalue()
    Image.open(io.BytesIO(image)).verify()
    text = "projected filesystem text"
    image_document = await _projected_chat_upload(
        db_session, entity_id=user.entity_id, user_id=user.id,
    )
    text_document = await _projected_chat_upload(
        db_session,
        entity_id=user.entity_id,
        user_id=user.id,
        name="notes.txt",
        mime_type="text/plain",
        content="stale metadata text",
    )
    upload_root = tmp_path / user.entity_id / "uploads" / "chat"
    upload_root.mkdir(parents=True)
    (upload_root / "cover.png").write_bytes(image)
    (upload_root / "notes.txt").write_text(text)

    metadata = await client.get(f"/api/v1/documents/{image_document.id}", headers=headers)
    assert metadata.status_code == 200, metadata.text
    preview = await client.get(
        f"/api/v1/documents/{image_document.id}/preview/content", headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.content == image
    download = await client.get(f"/api/v1/documents/{image_document.id}/download", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == image
    content = await client.get(f"/api/v1/documents/{text_document.id}/content", headers=headers)
    assert content.status_code == 200, content.text
    assert content.json() == {"content": text}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "projected,deleted,foreign",
    [(False, False, False), (True, False, True)],
)
@pytest.mark.parametrize("action", ["preview/content", "download", "content"])
async def test_unprojected_or_foreign_workspace_chat_upload_remains_hidden(
    client: AsyncClient,
    db_session,
    projected: bool,
    deleted: bool,
    foreign: bool,
    action: str,
):
    headers = await _auth(client, f"hidden_upload_{projected}_{deleted}_{foreign}")
    user = await _registered_user(db_session)
    entity_id = user.entity_id
    if foreign:
        other = Entity(name="Foreign upload entity")
        db_session.add(other)
        await db_session.flush()
        entity_id = other.id
    if projected:
        document = await _projected_chat_upload(
            db_session,
            entity_id=entity_id,
            user_id=user.id,
            deleted=deleted,
            visibility="workspace",
        )
    else:
        document = Document(
            entity_id=entity_id,
            name="unprojected.png",
            fs_path="uploads/chat/unprojected.png",
            file_type="png",
            mime_type="image/png",
            source="chat_upload",
            owner_id=user.id,
            visibility="workspace",
        )
        db_session.add(document)
        await db_session.commit()

    response = await client.get(
        f"/api/v1/documents/{document.id}/{action}", headers=headers,
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["preview/content", "download", "content"])
async def test_projected_deleted_workspace_chat_upload_remains_readable_to_owner(
    client: AsyncClient,
    db_session,
    monkeypatch,
    action: str,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob

    headers = await _auth(client, f"retained_upload_{action.replace('/', '_')}")
    user = await _registered_user(db_session)
    document = await _projected_chat_upload(
        db_session,
        entity_id=user.entity_id,
        user_id=user.id,
        deleted=True,
        visibility="workspace",
    )

    async def cached_blob(*_args, **_kwargs):
        return CachedKnowledgeBlob(data=b"retained upload", media_type="image/png")

    monkeypatch.setattr(documents_router, "get_cached_document_blob", cached_blob)
    response = await client.get(
        f"/api/v1/documents/{document.id}/{action}", headers=headers,
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_projected_private_workspace_chat_upload_remains_hidden_to_member(
    client: AsyncClient,
    db_session,
):
    from test_document_permissions import _invite_and_accept_member

    owner_headers = await _auth(client, "private_projected_upload_owner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    document = await _projected_chat_upload(
        db_session,
        entity_id=owner["entity_id"],
        user_id=owner["id"],
        visibility="private",
    )
    member_headers, _member = await _invite_and_accept_member(
        client,
        owner_headers,
        "private_projected_upload_member@test.com",
    )

    for action in ("preview/content", "download", "content"):
        response = await client.get(
            f"/api/v1/documents/{document.id}/{action}", headers=member_headers,
        )
        assert response.status_code == 404, response.text
