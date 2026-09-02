"""Delegation lifetime and separate view/download action regressions."""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import os
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import packages.core.database as db_module
import packages.core.services.document_access as access
from packages.core.models import (
    Document,
    DocumentFolder,
    Entity,
    ResourceGrant,
    Share,
    User,
    UserMembership,
)
from packages.core.models.base import generate_ulid
from packages.core.services.resource_grant_policy import ResourceGrantPolicyError, ResourceGrantPolicyFactory
from test_document_permissions import _auth, _invite_and_accept_member


def _patch_filesystem_settings(monkeypatch, root, *, enabled: bool) -> None:
    """Keep cached and router-held filesystem settings aligned in full-suite runs."""
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings

    seen: set[int] = set()
    for current in (get_settings(), documents_router.settings):
        if id(current) in seen:
            continue
        seen.add(id(current))
        monkeypatch.setattr(current, "MANOR_FS_ROOT", str(root))
        monkeypatch.setattr(current, "MANOR_FS_ENABLED", enabled)


async def _actors(client):
    headers = await _auth(client, "sharing_boundary_owner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member_headers, member = await _invite_and_accept_member(
        client,
        headers,
        "sharing_boundary_member@test.com",
    )
    return headers, owner, member_headers, member


async def _document(owner, classification="internal", *, in_folder=False):
    async with db_module.async_session() as session:
        folder = (
            DocumentFolder(
                entity_id=owner["entity_id"],
                name=f"Private folder {generate_ulid()}",
                owner_id=owner["id"],
                visibility="private",
                classification=classification,
            )
            if in_folder
            else None
        )
        if folder:
            session.add(folder)
            await session.flush()
        doc = Document(
            entity_id=owner["entity_id"],
            name="private.md",
            owner_id=owner["id"],
            created_by=owner["id"],
            visibility="private",
            classification=classification,
            file_type="md",
            mime_type="text/markdown",
            metadata_={"content": "private content"},
            folder_id=folder.id if folder else None,
        )
        session.add(doc)
        await session.commit()
        return doc.id, folder.id if folder else None


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["document", "folder"])
async def test_delegate_cannot_extend_own_or_descendant_grant(client, monkeypatch, resource):
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=resource == "folder")
    url = f"/api/v1/documents/{doc_id}/grants" if resource == "document" else f"/api/v1/folders/{folder_id}/grants"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"subject_id": member["user_id"], "capabilities": ["view", "share_internal"]}
    original = await client.post(url, headers=headers, json={**payload, "expires_at": expiry.isoformat()})
    assert original.status_code == 201, original.text
    too_long = await client.post(
        url, headers=member_headers, json={**payload, "expires_at": (expiry + timedelta(days=1)).isoformat()}
    )
    assert too_long.status_code == 403, too_long.text
    renewed = await client.post(url, headers=member_headers, json=payload)
    assert renewed.status_code == 201, renewed.text
    assert renewed.json()["expires_at"] == original.json()["expires_at"]
    # A folder-derived direct document grant must retain the same ceiling.
    direct = await client.post(f"/api/v1/documents/{doc_id}/grants", headers=member_headers, json=payload)
    assert direct.status_code == 201, direct.text
    assert direct.json()["expires_at"] == original.json()["expires_at"]

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (expiry + timedelta(seconds=1)).astimezone(tz)

    monkeypatch.setattr(access, "datetime", ExpiredClock)
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=member_headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["internal", "confidential", "restricted"])
async def test_view_only_cannot_download_but_can_preview(client, classification):
    headers, owner, member_headers, member = await _actors(client)
    doc_id, _ = await _document(owner, classification)
    base = f"/api/v1/documents/{doc_id}"
    assert (await client.get(f"{base}/preview/content", headers=member_headers)).status_code == 404
    grant = await client.post(
        f"{base}/grants", headers=headers, json={"subject_id": member["user_id"], "capabilities": ["view"]}
    )
    assert grant.status_code == 201, grant.text
    download = await client.get(f"{base}/download", headers=member_headers)
    assert download.status_code == 403, download.text
    preview = await client.get(f"{base}/preview/content", headers=member_headers)
    assert preview.status_code == 200, preview.text
    assert preview.text == "private content"
    assert preview.headers["content-disposition"] == "inline"
    assert preview.headers["content-security-policy"] == "sandbox"
    grant = await client.post(
        f"{base}/grants", headers=headers, json={"subject_id": member["user_id"], "capabilities": ["view", "download"]}
    )
    assert grant.status_code == 201, grant.text
    expected = 403 if classification == "restricted" else 200
    assert (await client.get(f"{base}/download", headers=member_headers)).status_code == expected
    assert (await client.get(f"{base}/download", headers=headers)).status_code == expected


def test_delegation_horizon_uses_alternatives_and_each_required_capability():
    now = datetime.now(timezone.utc)
    soon, later = now + timedelta(hours=1), now + timedelta(hours=2)

    def grant(caps, expiry):
        return ResourceGrant(capabilities=caps, expires_at=expiry, status="active")

    policy = ResourceGrantPolicyFactory.create("document")
    rows = [grant(["view", "edit"], soon), grant(["view"], None), grant(["share_internal"], later)]
    assert policy.delegated_expiry(["view"], grants=rows, expires_at=None) == later
    assert policy.delegated_expiry(["view", "edit"], grants=rows, expires_at=None) == soon
    rows.append(grant(["grant_access"], None))
    assert policy.delegated_expiry(["view"], grants=rows, expires_at=None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("in_folder", [False, True])
@pytest.mark.parametrize(
    "extra_capabilities",
    [
        [],
        ["manage_metadata"],
        ["comment", "edit", "download", "print", "share_internal", "share_external", "grant_access"],
    ],
    ids=["bare", "metadata", "actions"],
)
async def test_redacted_access_does_not_imply_full_content_access(
    client,
    monkeypatch,
    tmp_path,
    in_folder,
    extra_capabilities,
):
    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=in_folder)
    base = f"/api/v1/documents/{doc_id}"
    grant_base = f"/api/v1/folders/{folder_id}" if in_folder else base
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        doc.pii_detected = True
        await session.commit()
    capabilities = ["view_redacted", *extra_capabilities]
    grant = await client.post(
        f"{grant_base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": capabilities,
        },
    )
    assert grant.status_code == 201, grant.text
    metadata = await client.get(base, headers=member_headers)
    assert metadata.status_code == 200, metadata.text
    assert "view" not in metadata.json()["current_user_capabilities"]
    for action in (
        "preview/content",
        "content",
        "download",
        "thumbnail",
        "pages",
        "pages/0",
        "slides",
        "slides/0",
        "editable-file",
    ):
        response = await client.get(
            f"{base}/{action}",
            headers=member_headers,
            params={"version": "0" * 16},
        )
        assert response.status_code in {403, 404}, (action, response.text)
        assert "private content" not in response.text
    assert (await client.get(f"{base}/preview/content", headers=headers)).status_code == 200

    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        ctx = await access.DocumentAccessContext.load(
            session,
            entity_id=owner["entity_id"],
            user_id=member["user_id"],
            role="member",
        )
        await ctx.preload_documents(session, [doc])
        assert await ctx.can_read_document(session, doc)
        assert not await ctx.can_read_document(session, doc, allow_redacted=False)
        assert await ctx.effective_document_capabilities(session, doc) == set(capabilities)
        assert await access.effective_document_capabilities_for_user(
            session,
            document=doc,
            user_id=member["user_id"],
            role="member",
        ) == set(capabilities)
        if folder_id:
            folder = await session.get(DocumentFolder, folder_id)
            assert not await ctx.can_read_folder(session, folder, allow_redacted=False)
            assert not await access.user_can_read_folder(
                session,
                folder,
                entity_id=owner["entity_id"],
                user_id=member["user_id"],
                role="member",
                allow_redacted=False,
            )
        from packages.core.ai.runtime.rag import _runtime_rag_filter_to_visible_documents

        assert (
            await _runtime_rag_filter_to_visible_documents(
                session,
                [{"document_id": doc_id, "content": "private content"}],
                entity_id=owner["entity_id"],
                user_id=member["user_id"],
                workspace_id=None,
            )
            == []
        )
        from apps.api.routers.filesystem import _wiki_index_consistent

        monkeypatch.setattr(
            "packages.core.services.wiki_service.build_wiki_graph",
            lambda *_args, **_kwargs: {"pages": []},
        )
        graph = await _wiki_index_consistent(
            db=session,
            user=SimpleNamespace(entity_id=owner["entity_id"], id=member["user_id"], role="member"),
            net_id=None,
            group_id=None,
            workspace_id=None,
        )
        assert graph["pages"] == []
        doc.fs_path = "private.md"
        await session.commit()
        assert await access.unreadable_document_paths(
            session,
            entity_id=owner["entity_id"],
            rel_paths=["private.md"],
            user_id=member["user_id"],
            role="member",
        ) == {"private.md"}

        # An independent entity-wide visibility path still authorizes full view.
        doc.fs_path = None
        doc.visibility = "entity"
        if folder_id:
            (await session.get(DocumentFolder, folder_id)).visibility = "entity"
        await session.commit()
    response = await client.get(f"{base}/preview/content", headers=member_headers)
    assert response.status_code == 200, response.text
    assert response.text == "private content"


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["document", "folder"])
@pytest.mark.parametrize("visibility", ["entity", "workspace"])
async def test_external_share_accepts_independent_view_but_keeps_authority_expiry(
    client,
    resource,
    visibility,
):
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        folder = await session.get(DocumentFolder, folder_id)
        doc.visibility = folder.visibility = visibility
        if visibility == "workspace":
            workspace = Workspace(
                entity_id=owner["entity_id"],
                name="Share workspace",
                artifact_folder_id=folder_id,
                settings={"access_mode": "members_only"},
            )
            session.add(workspace)
            await session.flush()
            session.add(
                WorkspaceStaff(
                    workspace_id=workspace.id,
                    user_id=member["user_id"],
                    role="member",
                    status="active",
                )
            )
            doc.metadata_ = {**doc.metadata_, "workspace_id": workspace.id}
        await session.commit()
    preview = f"/api/v1/documents/{doc_id}/preview/content"
    assert (await client.get(preview, headers=member_headers)).status_code == 200
    base = f"/api/v1/documents/{doc_id}" if resource == "document" else f"/api/v1/folders/{folder_id}"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["share_external"],
            "expires_at": expiry.isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text
    for extra in ({}, {"expires_in_days": None}):
        shared = await client.post(
            f"{base}/shares",
            headers=member_headers,
            json={
                "capabilities": ["view"],
                **extra,
            },
        )
        assert shared.status_code == 201, shared.text
        assert datetime.fromisoformat(shared.json()["expires_at"]) == expiry
    for invalid in (
        {"capabilities": ["view", "download"], "allow_download": True},
        {"capabilities": ["view"], "expires_in_days": 1},
    ):
        rejected = await client.post(f"{base}/shares", headers=member_headers, json=invalid)
        assert rejected.status_code == 403, rejected.text
    revoked = await client.delete(f"{base}/grants/{granted.json()['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    assert (await client.get(preview, headers=member_headers)).status_code == 200
    assert (
        await client.post(
            f"{base}/shares",
            headers=member_headers,
            json={
                "capabilities": ["view"],
            },
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_external_share_does_not_treat_expiring_folder_view_as_implicit(client):
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        (await session.get(Document, doc_id)).visibility = "entity"
        await session.commit()
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    granted = await client.post(
        f"/api/v1/folders/{folder_id}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view"],
            "expires_at": expiry.isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text
    base = f"/api/v1/documents/{doc_id}"
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["share_external"],
        },
    )
    assert granted.status_code == 201, granted.text
    shared = await client.post(
        f"{base}/shares",
        headers=member_headers,
        json={
            "capabilities": ["view"],
            "expires_in_days": None,
        },
    )
    assert shared.status_code == 201, shared.text
    assert datetime.fromisoformat(shared.json()["expires_at"]) == expiry


@pytest.mark.asyncio
@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("action", ["content", "preview/content", "download"])
@pytest.mark.parametrize("cache_hit", [False, True])
async def test_file_read_rechecks_revocation_after_cache_wait(
    client,
    monkeypatch,
    tmp_path,
    physical,
    action,
    cache_hit,
):
    from apps.api.routers import documents
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob
    from test_folder_permissions import _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=physical)
    headers, owner, member_headers, member = await _actors(client)
    if physical:
        doc_id = (await _upload(client, headers, name="race.md"))["id"]
    else:
        doc_id, _ = await _document(owner)
    base = f"/api/v1/documents/{doc_id}"
    grant = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert grant.status_code == 201, grant.text
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def cache_barrier(document, kind="content"):
        assert document.id == doc_id and kind == ("content" if action == "content" else "download")
        ready.set()
        await proceed.wait()
        if action == "content":
            return "cached secret" if cache_hit else None
        return CachedKnowledgeBlob(b"cached secret", "text/markdown") if cache_hit else None

    cache_function = "get_cached_document_text" if action == "content" else "get_cached_document_blob"
    monkeypatch.setattr(documents, cache_function, cache_barrier)
    pending = asyncio.create_task(client.get(f"{base}/{action}", headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        revoked = await client.delete(f"{base}/grants/{grant.json()['id']}", headers=headers)
        assert revoked.status_code == 204, revoked.text
        saved = await client.put(
            f"{base}/content",
            headers=headers,
            json={
                "content": "SECRET-WRITTEN-AFTER-REVOCATION",
            },
        )
        assert saved.status_code == 200, saved.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 404, response.text
        assert "secret" not in response.text.lower()
        assert (await client.get(f"{base}/{action}", headers=member_headers)).status_code == 404
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["document", "folder"])
async def test_external_share_cannot_add_unheld_capability(client, resource):
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=resource == "folder")
    base = f"/api/v1/documents/{doc_id}" if resource == "document" else f"/api/v1/folders/{folder_id}"
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "share_external"],
        },
    )
    assert granted.status_code == 201, granted.text
    for capability in ("download", "comment"):
        shared = await client.post(
            f"{base}/shares",
            headers=member_headers,
            json={
                "capabilities": ["view", capability],
                "allow_download": True,
                "expires_in_days": None,
            },
        )
        assert shared.status_code == 403, shared.text
    assert (await client.get(f"{base}/shares", headers=headers)).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["metadata", "physical", "promoted"])
@pytest.mark.parametrize("action", ["content", "preview/content"])
async def test_preview_discards_cached_version_after_concurrent_save(
    client,
    monkeypatch,
    tmp_path,
    storage,
    action,
):
    from apps.api.routers import documents
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob
    from test_folder_permissions import _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=storage != "metadata")
    headers, owner, member_headers, member = await _actors(client)
    if storage == "physical":
        doc_id = (await _upload(client, headers, name="cache-version.md"))["id"]
    else:
        doc_id, _ = await _document(owner)
    base = f"/api/v1/documents/{doc_id}"
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view"],
        },
    )
    assert granted.status_code == 201, granted.text
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def cache_barrier(*_args):
        ready.set()
        await proceed.wait()
        if action == "content":
            return "OLD cached content"
        return CachedKnowledgeBlob(b"OLD cached content", "text/markdown")

    cache_function = "get_cached_document_text" if action == "content" else "get_cached_document_blob"
    monkeypatch.setattr(documents, cache_function, cache_barrier)
    pending = asyncio.create_task(client.get(f"{base}/{action}", headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        saved = await client.put(f"{base}/content", headers=headers, json={"content": "new authorized content"})
        assert saved.status_code == 200, saved.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        if storage == "promoted":
            assert response.status_code == 409, response.text

            async def miss(*_args):
                return None

            monkeypatch.setattr(documents, cache_function, miss)
            response = await client.get(f"{base}/{action}", headers=member_headers)
        assert response.status_code == 200, response.text
        content = response.json()["content"] if action == "content" else response.text
        assert content == "new authorized content"
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["preview/content", "download"])
@pytest.mark.parametrize("ranged", [False, True])
async def test_private_file_response_snapshots_before_releasing_read_lock(
    client,
    monkeypatch,
    tmp_path,
    action,
    ranged,
):
    from apps.api import file_responses
    from apps.api.routers import documents
    from packages.core.services.entity_fs import (
        EntityFilesystemBusyError,
        entity_filesystem_mutation_lock,
        get_entity_root,
    )
    from starlette.responses import FileResponse
    from test_folder_permissions import _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, member_headers, member = await _actors(client)
    doc_id = (await _upload(client, headers, name="snapshot.md"))["id"]
    base = f"/api/v1/documents/{doc_id}"
    grant = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert grant.status_code == 201, grant.text
    copying, copy_allowed = asyncio.Event(), asyncio.Event()
    streaming, stream_allowed = asyncio.Event(), asyncio.Event()
    original_snapshot = file_responses.EntitySnapshotFileResponse.__call__
    original_stream = FileResponse.__call__

    async def miss(*_args, **_kwargs):
        return None

    async def snapshot_barrier(response, scope, receive, send):
        copying.set()
        await copy_allowed.wait()
        return await original_snapshot(response, scope, receive, send)

    async def stream_barrier(response, scope, receive, send):
        streaming.set()
        await stream_allowed.wait()
        return await original_stream(response, scope, receive, send)

    monkeypatch.setattr(documents, "get_cached_document_blob", miss)
    monkeypatch.setattr(file_responses.EntitySnapshotFileResponse, "__call__", snapshot_barrier)
    monkeypatch.setattr(FileResponse, "__call__", stream_barrier)
    pending = asyncio.create_task(
        client.get(
            f"{base}/{action}",
            headers={**member_headers, **({"Range": "bytes=1-3"} if ranged else {})},
        )
    )
    try:
        await asyncio.wait_for(copying.wait(), timeout=60)
        # No writer may change the authorized source before its snapshot exists.
        with pytest.raises(EntityFilesystemBusyError):
            async with entity_filesystem_mutation_lock(get_entity_root(owner["entity_id"]), timeout_seconds=0):
                pytest.fail("Source was unlocked before its snapshot")
        copy_allowed.set()
        await asyncio.wait_for(streaming.wait(), timeout=60)
        revoked = await client.delete(f"{base}/grants/{grant.json()['id']}", headers=headers)
        assert revoked.status_code == 204, revoked.text
        saved = await client.put(f"{base}/content", headers=headers, json={"content": "new secret"})
        assert saved.status_code == 200, saved.text
        stream_allowed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == (206 if ranged else 200), response.text
        assert response.content == (b"ell" if ranged else b"hello")
        if ranged:
            assert response.headers["content-range"] == "bytes 1-3/5"
        assert (await client.get(f"{base}/{action}", headers=member_headers)).status_code == 404
    finally:
        copy_allowed.set()
        stream_allowed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_inflight_download_rechecks_download_capability_not_just_view(client, monkeypatch):
    from apps.api.routers import documents
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob

    headers, owner, member_headers, member = await _actors(client)
    doc_id, _ = await _document(owner)
    base = f"/api/v1/documents/{doc_id}"
    payload = {"subject_id": member["user_id"], "capabilities": ["view", "download"]}
    assert (await client.post(f"{base}/grants", headers=headers, json=payload)).status_code == 201
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def cache_barrier(*_args):
        ready.set()
        await proceed.wait()
        return CachedKnowledgeBlob(b"private content", "text/markdown")

    monkeypatch.setattr(documents, "get_cached_document_blob", cache_barrier)
    pending = asyncio.create_task(client.get(f"{base}/download", headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        changed = await client.post(f"{base}/grants", headers=headers, json={**payload, "capabilities": ["view"]})
        assert changed.status_code == 201, changed.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 403, response.text
        assert (await client.get(f"{base}/preview/content", headers=member_headers)).status_code == 200
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["document", "folder"])
async def test_external_share_inherits_grant_ceiling_and_expires(client, monkeypatch, resource):
    from apps.api.routers import document_permissions, folder_permissions

    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=resource == "folder")
    base = f"/api/v1/documents/{doc_id}" if resource == "document" else f"/api/v1/folders/{folder_id}"
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download", "share_external"],
            "expires_at": expiry.isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text
    payload = {"capabilities": ["view", "download"], "allow_download": True}
    excessive = await client.post(
        f"{base}/shares",
        headers=member_headers,
        json={
            **payload,
            "expires_in_days": 7,
        },
    )
    assert excessive.status_code == 403, excessive.text
    created = []
    for extra in ({}, {"expires_in_days": None}):
        shared = await client.post(f"{base}/shares", headers=member_headers, json={**payload, **extra})
        assert shared.status_code == 201, shared.text
        assert datetime.fromisoformat(shared.json()["expires_at"]) == expiry
        created.append(shared.json())
    unlimited = await client.post(
        f"{base}/shares",
        headers=headers,
        json={
            **payload,
            "expires_in_days": None,
        },
    )
    assert unlimited.status_code == 201, unlimited.text
    assert unlimited.json()["expires_at"] is None

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (expiry + timedelta(seconds=1)).astimezone(tz)

    monkeypatch.setattr(document_permissions, "datetime", ExpiredClock)
    monkeypatch.setattr(folder_permissions, "datetime", ExpiredClock)
    public = "shared-doc" if resource == "document" else "shared-folder"
    for shared in created:
        assert (await client.get(f"/api/v1/{public}/{shared['token']}")).status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["document", "folder"])
async def test_external_share_accepts_explicit_shorter_expiry(client, resource):
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=resource == "folder")
    base = f"/api/v1/documents/{doc_id}" if resource == "document" else f"/api/v1/folders/{folder_id}"
    now = datetime.now(timezone.utc)
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "share_external"],
            "expires_at": (now + timedelta(days=3)).isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text
    shared = await client.post(
        f"{base}/shares",
        headers=member_headers,
        json={
            "capabilities": ["view"],
            "expires_in_days": 1,
        },
    )
    assert shared.status_code == 201, shared.text
    assert now + timedelta(hours=23) < datetime.fromisoformat(shared.json()["expires_at"]) < now + timedelta(days=2)


def test_external_delegation_uses_only_external_authority_and_each_capability():
    now = datetime.now(timezone.utc)
    soon, later = now + timedelta(minutes=5), now + timedelta(hours=1)
    policy = ResourceGrantPolicyFactory.create("document")
    rows = [
        ResourceGrant(capabilities=["view", "share_internal"], status="active"),
        ResourceGrant(capabilities=["download"], status="active", expires_at=soon),
    ]
    kwargs = {"grants": rows, "expires_at": None, "authority_capabilities": ("share_external",)}
    with pytest.raises(ResourceGrantPolicyError):
        policy.delegated_expiry(["view"], **kwargs)
    rows.append(ResourceGrant(capabilities=["share_external"], status="active", expires_at=later))
    assert policy.delegated_expiry(["view"], **kwargs) == later
    assert policy.delegated_expiry(["view", "download"], **kwargs) == soon


def test_implicit_view_does_not_supply_external_delegation_authority():
    policy = ResourceGrantPolicyFactory.create("document")
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    kwargs = {
        "expires_at": None,
        "authority_capabilities": ("share_external",),
        "implicit_capabilities": ("view",),
    }
    with pytest.raises(ResourceGrantPolicyError):
        policy.delegated_expiry(["view"], grants=[], **kwargs)
    rows = [ResourceGrant(capabilities=["share_external"], status="active", expires_at=expiry)]
    assert policy.delegated_expiry(["view"], grants=rows, **kwargs) == expiry
    with pytest.raises(ResourceGrantPolicyError):
        policy.delegated_expiry(["view", "download"], grants=rows, **kwargs)


async def _folder_share(client, *, download=True, audience="anonymous"):
    from test_folder_permissions import _create_folder

    headers, owner, _member_headers, _member = await _actors(client)
    doc_id, root_id = await _document(owner, in_folder=True)
    child = await _create_folder(client, headers, "Nested", root_id)
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        doc.folder_id = child["id"]
        await session.commit()
    shared = await client.post(
        f"/api/v1/folders/{root_id}/shares",
        headers=headers,
        json={
            "audience_type": audience,
            "audience_value": "recipient@example.com" if audience == "email" else None,
            "capabilities": ["view", "download"] if download else ["view"],
            "allow_download": download,
            "require_otp": audience == "email",
        },
    )
    assert shared.status_code == 201, shared.text
    data = shared.json()
    return headers, owner, root_id, child["id"], doc_id, data


@pytest.mark.asyncio
async def test_folder_link_browses_previews_downloads_and_stays_inside_root(client):
    _headers, owner, root_id, child_id, doc_id, share = await _folder_share(client)
    base = f"/api/v1/shared-folder/{share['token']}"
    root = await client.get(base)
    assert root.status_code == 200, root.text
    assert root.json()["parent_id"] is None
    assert root.json()["subfolders"][0]["id"] == child_id
    nested = await client.get(base, params={"folder_id": child_id})
    assert nested.status_code == 200, nested.text
    assert nested.json()["parent_id"] == root_id
    assert nested.json()["documents"][0]["id"] == doc_id
    for action in ("content", "download"):
        response = await client.get(f"{base}/documents/{doc_id}/{action}")
        assert response.status_code == 200, response.text
        assert response.text == "private content"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["content-disposition"].startswith("inline" if action == "content" else "attachment")
    outside_id, outside_folder = await _document(owner, in_folder=True)
    assert (await client.get(base, params={"folder_id": outside_folder})).status_code == 404
    assert (await client.get(f"{base}/documents/{outside_id}/content")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("file_format", ["docx", "pptx", "xlsx", "doc"])
async def test_metadata_office_files_return_real_bytes_or_fail_explicitly(client, file_format):
    from apps.api.routers.documents import DOCX_MIME, PPTX_MIME, XLSX_MIME

    headers, _owner, _root_id, _child_id, doc_id, share = await _folder_share(client)
    source_mime = {"docx": DOCX_MIME, "pptx": PPTX_MIME, "xlsx": XLSX_MIME, "doc": "application/msword"}[file_format]
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        doc.name, doc.file_type, doc.mime_type = f"Report.{file_format}", file_format, source_mime
        doc.metadata_ = {"content": "<p>Metadata Office regression</p>"}
        await session.commit()
    urls = [
        (f"/api/v1/shared-folder/{share['token']}/documents/{doc_id}/{action}", {})
        for action in ("content", "download")
    ] + [(f"/api/v1/documents/{doc_id}/{action}", headers) for action in ("preview/content", "download")]
    for url, request_headers in urls:
        response = await client.get(url, headers=request_headers)
        if file_format == "xlsx":
            assert response.status_code == 415, response.text
            assert "Metadata Office regression" not in response.text
            continue
        assert response.status_code == 200, response.text
        expected_mime = PPTX_MIME if file_format == "pptx" else DOCX_MIME
        assert response.headers["content-type"] == expected_mime
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "[Content_Types].xml" in archive.namelist()
            content_xml = b"\n".join(archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
            assert b"Metadata Office regression" in content_xml
        if url.endswith("/download"):
            assert response.headers["content-disposition"].endswith(
                "Report.pptx" if file_format == "pptx" else "Report.docx"
            )
    async with db_module.async_session() as session:
        # Serving a synthesized representation must not rewrite the source format.
        doc = await session.get(Document, doc_id)
        assert doc.file_type == file_format
        assert doc.mime_type == source_mime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "revoked",
        "expired",
        "trashed",
        "quarantined",
        "confidential",
        "moved",
        "folder_moved",
        "foreign",
        "workspace_deleted",
    ],
)
async def test_folder_content_revalidates_authority_on_every_request(client, mutation):
    _headers, owner, _root_id, child_id, doc_id, share = await _folder_share(client)
    base = f"/api/v1/shared-folder/{share['token']}"
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 200
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        row = await session.get(Share, share["id"])
        if mutation == "revoked":
            row.status = "revoked"
        elif mutation == "expired":
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif mutation == "trashed":
            doc.is_trashed = True
        elif mutation == "quarantined":
            doc.quarantine_status = "quarantined"
        elif mutation == "confidential":
            (await session.get(DocumentFolder, child_id)).classification = "confidential"
        elif mutation == "moved":
            doc.folder_id = None
        elif mutation == "folder_moved":
            (await session.get(DocumentFolder, child_id)).parent_id = None
        elif mutation == "foreign":
            doc.entity_id = "another-entity"
        else:
            from packages.core.models.workspace import Workspace

            workspace = Workspace(
                entity_id=owner["entity_id"], name="Deleted owner", deleted_at=datetime.now(timezone.utc)
            )
            session.add(workspace)
            await session.flush()
            doc.metadata_ = {**doc.metadata_, "workspace_id": workspace.id}
        await session.commit()
    for action in ("content", "download"):
        response = await client.get(f"{base}/documents/{doc_id}/{action}")
        assert response.status_code in {404, 410}, response.text


@pytest.mark.asyncio
async def test_folder_view_only_link_and_counted_view_session(client):
    _headers, _owner, _root_id, child_id, doc_id, share = await _folder_share(client, download=False)
    base = f"/api/v1/shared-folder/{share['token']}"
    assert (await client.get(f"{base}/documents/{doc_id}/download")).status_code == 403
    async with db_module.async_session() as session:
        row = await session.get(Share, share["id"])
        row.max_uses, row.use_count = 1, 0
        await session.commit()
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 410
    assert (await client.get(base)).status_code == 200
    assert (await client.get(base, params={"folder_id": child_id})).status_code == 200
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 200
    async with db_module.async_session() as session:
        assert (await session.get(Share, share["id"])).use_count == 1
    client.cookies.clear()
    assert (await client.get(base)).status_code == 410
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 410


@pytest.mark.asyncio
async def test_folder_file_requires_otp_and_rechecks_changed_audience(client, monkeypatch):
    _headers, _owner, _root_id, _child_id, doc_id, share = await _folder_share(client, audience="email")
    base = f"/api/v1/shared-folder/{share['token']}"
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 401
    codes = []

    async def deliver(_email, code):
        codes.append(code)
        return True

    monkeypatch.setattr("packages.core.services.email_service.send_share_verification_email", deliver)
    assert (await client.post(f"{base}/request-otp", json={"email": "recipient@example.com"})).status_code == 200
    verified = await client.post(f"{base}/verify-otp", json={"email": "recipient@example.com", "code": codes[0]})
    assert verified.status_code == 200, verified.text
    assert (await client.get(f"{base}/documents/{doc_id}/content")).status_code == 200
    async with db_module.async_session() as session:
        (await session.get(Share, share["id"])).audience = "email:another@example.com"
        await session.commit()
    assert (await client.get(f"{base}/documents/{doc_id}/download")).status_code == 401


@pytest.mark.asyncio
async def test_folder_download_snapshots_real_file(client, tmp_path, monkeypatch):
    from test_folder_permissions import _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, _owner, _root_id, child_id, _doc_id, share = await _folder_share(client)
    doc = await _upload(client, headers, folder_id=child_id, name="actual.md")
    base = f"/api/v1/shared-folder/{share['token']}/documents/{doc['id']}"
    for action in ("content", "download"):
        response = await client.get(f"{base}/{action}")
        assert response.status_code == 200, response.text
        assert response.content == b"hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("ancestor_visibility", ["private", "workspace"])
async def test_folder_share_implicit_view_respects_ancestor_ceiling(
    client,
    monkeypatch,
    tmp_path,
    ancestor_visibility,
):
    from packages.core.models.workspace import Workspace
    from test_folder_permissions import _create_folder, _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, member_headers, member = await _actors(client)
    root = await _create_folder(client, headers, "Ancestor")
    changed = await client.post(
        f"/api/v1/folders/{root['id']}/properties", headers=headers, json={"visibility": "entity", "cascade": True}
    )
    assert changed.status_code == 200, changed.text
    middle = await _create_folder(client, headers, "Middle", root["id"])
    child = await _create_folder(client, headers, "Child", middle["id"])
    doc = await _upload(client, headers, folder_id=child["id"])
    preview = f"/api/v1/documents/{doc['id']}/preview/content"
    assert (await client.get(preview, headers=member_headers)).status_code == 200
    if ancestor_visibility == "workspace":
        async with db_module.async_session() as session:
            session.add(
                Workspace(
                    entity_id=owner["entity_id"],
                    name="Members only",
                    artifact_folder_id=root["id"],
                    settings={"access_mode": "members_only"},
                )
            )
            await session.commit()
    changed = await client.post(
        f"/api/v1/folders/{root['id']}/properties",
        headers=headers,
        json={"visibility": ancestor_visibility, "cascade": False},
    )
    assert changed.status_code == 200, changed.text
    base = f"/api/v1/folders/{child['id']}"
    granted = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["share_external"],
        },
    )
    assert granted.status_code == 201, granted.text
    assert (await client.get(preview, headers=member_headers)).status_code == 404
    shared = await client.post(f"{base}/shares", headers=member_headers, json={"capabilities": ["view"]})
    assert shared.status_code == 403, shared.text
    assert (await client.get(f"{base}/shares", headers=headers)).json() == []

    # Explicit ancestor VIEW still works, but must retain its finite horizon.
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    granted = await client.post(
        f"/api/v1/folders/{root['id']}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view"],
            "expires_at": expiry.isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text
    shared = await client.post(
        f"{base}/shares",
        headers=member_headers,
        json={
            "capabilities": ["view"],
            "expires_in_days": None,
        },
    )
    assert shared.status_code == 201, shared.text
    assert datetime.fromisoformat(shared.json()["expires_at"]) == expiry
    public = await client.get(f"/api/v1/shared-folder/{shared.json()['token']}/documents/{doc['id']}/content")
    assert public.status_code == 200, public.text
    assert public.content == b"hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "source"),
    [
        ("content", "cache"),
        ("content", "metadata"),
        ("preview/content", "cache"),
        ("preview/content", "metadata"),
        ("preview/content", "remote"),
        ("download", "cache"),
        ("download", "metadata"),
        ("download", "remote"),
    ],
)
async def test_authorized_content_fallback_does_not_require_mounted_filesystem(
    client,
    monkeypatch,
    tmp_path,
    action,
    source,
):
    from apps.api.routers import documents
    from fastapi.responses import Response
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, member_headers, member = await _actors(client)
    doc_id, _ = await _document(owner)
    base = f"/api/v1/documents/{doc_id}"
    async with db_module.async_session() as session:
        doc = await session.get(Document, doc_id)
        doc.fs_path = "unmounted.md"
        if source == "remote":
            doc.file_url = "https://example.invalid/remote.md"
        await session.commit()
    grant = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert grant.status_code == 201, grant.text
    _patch_filesystem_settings(monkeypatch, tmp_path / "missing-storage-mount", enabled=True)
    remote_calls = []

    async def cached_blob(*_args):
        return CachedKnowledgeBlob(b"cached content", "text/markdown") if source == "cache" else None

    async def cached_text(*_args):
        return "cached content" if source == "cache" else None

    async def remote(*_args, **_kwargs):
        remote_calls.append(True)
        return Response("remote content", media_type="text/markdown")

    def no_physical_lock(*_args):
        pytest.fail("A nonphysical response tried to acquire a filesystem lock")

    monkeypatch.setattr(documents, "get_cached_document_blob", cached_blob)
    monkeypatch.setattr(documents, "get_cached_document_text", cached_text)
    monkeypatch.setattr(documents, "_remote_document_stream_response", remote)
    monkeypatch.setattr(documents, "entity_filesystem_read_boundary", no_physical_lock)
    response = await client.get(f"{base}/{action}", headers=member_headers)
    assert response.status_code == 200, response.text
    content = response.json()["content"] if action == "content" else response.text
    assert content == {"cache": "cached content", "metadata": "private content", "remote": "remote content"}[source]
    assert len(remote_calls) == (1 if source == "remote" else 0)
    revoked = await client.delete(f"{base}/grants/{grant.json()['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    assert (await client.get(f"{base}/{action}", headers=member_headers)).status_code == 404
    assert len(remote_calls) == (1 if source == "remote" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["preview", "download", "shared_document", "shared_folder"])
async def test_snapshot_if_range_uses_source_validators(client, monkeypatch, tmp_path, surface):
    from test_document_permissions import _upload as upload_bytes
    from test_folder_permissions import _create_folder

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, _owner, _member_headers, _member = await _actors(client)
    body = b"x" * (1024 * 1024 + 1)  # Larger than Redis blob cache; no cache mocks.
    doc = await upload_bytes(client, headers, name="range.md", body=body)
    base = f"/api/v1/documents/{doc['id']}"
    request_headers = headers
    if surface == "shared_folder":
        folder = await _create_folder(client, headers, "Range folder")
        async with db_module.async_session() as session:
            (await session.get(Document, doc["id"])).folder_id = folder["id"]
            await session.commit()
        share_base = f"/api/v1/folders/{folder['id']}"
    else:
        share_base = base
    if surface.startswith("shared_"):
        shared = await client.post(
            f"{share_base}/shares",
            headers=headers,
            json={
                "capabilities": ["view", "download"],
                "allow_download": True,
            },
        )
        assert shared.status_code == 201, shared.text
        token = shared.json()["token"]
        url = (
            f"/api/v1/shared-folder/{token}/documents/{doc['id']}/download"
            if surface == "shared_folder"
            else f"/api/v1/shared-doc/{token}/download"
        )
        request_headers = {}
    else:
        url = f"{base}/{'preview/content' if surface == 'preview' else 'download'}"
    first = await client.get(url, headers=request_headers)
    assert first.status_code == 200, first.text[:200]
    assert first.content == body
    for validator in ("etag", "last-modified"):
        ranged = await client.get(
            url,
            headers={
                **request_headers,
                "Range": "bytes=1-3",
                "If-Range": first.headers[validator],
            },
        )
        assert ranged.status_code == 206, ranged.text[:200]
        assert ranged.content == b"xxx"
        assert ranged.headers["content-range"] == f"bytes 1-3/{len(body)}"
        assert ranged.headers["etag"] == first.headers["etag"]
        assert ranged.headers["last-modified"] == first.headers["last-modified"]
    saved = await client.put(f"{base}/content", headers=headers, json={"content": "updated content"})
    assert saved.status_code == 200, saved.text
    changed = await client.get(
        url,
        headers={
            **request_headers,
            "Range": "bytes=1-3",
            "If-Range": first.headers["etag"],
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.content == b"updated content"


@pytest.mark.asyncio
async def test_raw_content_keeps_physical_read_boundary_until_bytes_are_read(client, monkeypatch, tmp_path):
    from apps.api.routers import documents
    from packages.core.services.entity_fs import (
        EntityFilesystemBusyError,
        entity_filesystem_mutation_lock,
        get_entity_root,
    )
    from test_folder_permissions import _upload

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, _mh, _member = await _actors(client)
    doc_id = (await _upload(client, headers, name="raw-lock.md"))["id"]
    ready, proceed = asyncio.Event(), asyncio.Event()
    original = documents.get_document_content

    async def miss(*_args):
        return None

    async def read_barrier(*args, **kwargs):
        assert kwargs["allow_filesystem"] is True
        ready.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(documents, "get_cached_document_text", miss)
    monkeypatch.setattr(documents, "get_document_content", read_barrier)
    pending = asyncio.create_task(client.get(f"/api/v1/documents/{doc_id}/content", headers=headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        with pytest.raises(EntityFilesystemBusyError):
            async with entity_filesystem_mutation_lock(get_entity_root(owner["entity_id"]), timeout_seconds=0):
                pytest.fail("Raw content read lacked the physical boundary")
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        assert response.json() == {"content": "hello"}
        async with entity_filesystem_mutation_lock(get_entity_root(owner["entity_id"]), timeout_seconds=0):
            pass
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content", "preview/content", "download"])
async def test_live_ledger_read_does_not_reacquire_workspace_mutation_lock(
    client,
    monkeypatch,
    tmp_path,
    action,
):
    from packages.core.models import Workspace

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name="Live ledger",
            artifact_folder_id=folder_id,
        )
        session.add(workspace)
        await session.flush()
        document = await session.get(Document, doc_id)
        document.metadata_ = {
            "content": "stale ledger content",
            "blueprint_knowledge_pack_slug": "solo-stickman-studio-ops",
            "blueprint_starter_path": "topic-ledger/ledger.md",
            "origin": {"workspace_id": workspace.id},
        }
        await session.commit()
    base = f"/api/v1/documents/{doc_id}"
    grant = await client.post(
        f"{base}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert grant.status_code == 201, grant.text
    response = await asyncio.wait_for(client.get(f"{base}/{action}", headers=member_headers), timeout=60)
    assert response.status_code == 200, response.text
    content = response.json()["content"] if action == "content" else response.text
    assert "Topics created or reserved: 0" in content
    assert "stale ledger content" not in content
    assert not list(tmp_path.rglob("topic-ledger"))
    revoked = await client.delete(f"{base}/grants/{grant.json()['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    assert (await client.get(f"{base}/{action}", headers=member_headers)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content", "preview/content", "download"])
async def test_document_read_rechecks_legacy_role_after_cache_wait(
    client,
    monkeypatch,
    action,
):
    """A role snapshot captured by request auth must not survive an async wait."""
    from apps.api.routers import documents
    from test_document_permissions import _auth, _create_entity_user

    headers = await _auth(client, f"gate_legacy_owner_{action.replace('/', '_')}")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    reader = await _create_entity_user(
        owner["entity_id"],
        f"gate_legacy_reader_{action.replace('/', '_')}",
        role="admin",
    )
    doc_id, _ = await _document(owner)
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def cache_barrier(*_args):
        ready.set()
        await proceed.wait()
        return None

    cache_name = "get_cached_document_text" if action == "content" else "get_cached_document_blob"
    monkeypatch.setattr(documents, cache_name, cache_barrier)
    pending = asyncio.create_task(client.get(f"/api/v1/documents/{doc_id}/{action}", headers=reader["headers"]))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        demoted = await client.put(
            f"/api/v1/auth/users/{reader['id']}/role",
            headers=headers,
            json={"role": "member"},
        )
        assert demoted.status_code == 200, demoted.text
        secret = f"secret-after-role-demotion-{action}"
        saved = await client.put(
            f"/api/v1/documents/{doc_id}/content",
            headers=headers,
            json={"content": secret},
        )
        assert saved.status_code == 200, saved.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 404, response.text
        assert secret not in response.text
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content", "preview/content", "download"])
async def test_document_read_rechecks_staff_identity_after_policy_wait(
    client,
    monkeypatch,
    action,
):
    """A direct User grant cannot outlive the selected entity's Staff identity."""
    from apps.api.routers import documents

    headers, owner, member_headers, member = await _actors(client)
    doc_id, _ = await _document(owner)
    async with db_module.async_session() as session:
        personal = Entity(name=f"Gate reader personal {generate_ulid()}")
        session.add(personal)
        await session.flush()
        identity = await session.get(User, member["user_id"])
        identity.entity_id = personal.id
        identity.role = "owner"
        session.add(
            UserMembership(
                user_id=identity.id,
                entity_id=personal.id,
                role="owner",
                status="active",
            )
        )
        await session.commit()
    granted = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert granted.status_code == 201, granted.text
    ready, proceed = asyncio.Event(), asyncio.Event()
    original = documents.get_document_for_update

    async def policy_barrier(*args, **kwargs):
        ready.set()
        await proceed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(documents, "get_document_for_update", policy_barrier)
    pending = asyncio.create_task(client.get(f"/api/v1/documents/{doc_id}/{action}", headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        removed = await client.delete(
            f"/api/v1/staff/{member['staff_id']}",
            headers=headers,
        )
        assert removed.status_code == 204, removed.text
        secret = f"secret-after-staff-removal-{action}"
        saved = await client.put(
            f"/api/v1/documents/{doc_id}/content",
            headers=headers,
            json={"content": secret},
        )
        assert saved.status_code == 200, saved.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 404, response.text
        assert secret not in response.text
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["content", "preview/content", "download"])
async def test_document_read_rechecks_token_version_after_cache_wait(
    client,
    monkeypatch,
    action,
):
    """Logout during a cache wait revokes the already-admitted request."""
    from apps.api.routers import documents

    headers, owner, member_headers, member = await _actors(client)
    doc_id, _ = await _document(owner)
    granted = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view", "download"],
        },
    )
    assert granted.status_code == 201, granted.text
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def cache_barrier(*_args):
        ready.set()
        await proceed.wait()
        return None

    cache_name = "get_cached_document_text" if action == "content" else "get_cached_document_blob"
    monkeypatch.setattr(documents, cache_name, cache_barrier)
    pending = asyncio.create_task(client.get(f"/api/v1/documents/{doc_id}/{action}", headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        logged_out = await client.post("/api/v1/auth/logout", headers=member_headers)
        assert logged_out.status_code == 204, logged_out.text
        secret = f"secret-after-logout-{action}"
        saved = await client.put(
            f"/api/v1/documents/{doc_id}/content",
            headers=headers,
            json={"content": secret},
        )
        assert saved.status_code == 200, saved.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 404, response.text
        assert secret not in response.text
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_auth_read_does_not_reactivate_an_inactive_membership(client):
    headers, owner, member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        membership = (
            await session.execute(
                select(UserMembership).where(
                    UserMembership.user_id == member["user_id"],
                    UserMembership.entity_id == owner["entity_id"],
                )
            )
        ).scalar_one()
        membership.status = "inactive"
        await session.commit()

    denied = await client.get("/api/v1/auth/me", headers=member_headers)
    assert denied.status_code == 403, denied.text
    async with db_module.async_session() as session:
        membership = (
            await session.execute(
                select(UserMembership).where(
                    UserMembership.user_id == member["user_id"],
                    UserMembership.entity_id == owner["entity_id"],
                )
            )
        ).scalar_one()
        assert membership.status == "inactive"


@pytest.mark.asyncio
async def test_auth_read_does_not_create_a_legacy_membership(client):
    from test_document_permissions import _create_entity_user

    legacy = await _create_entity_user(
        generate_ulid(),
        "auth_read_only_legacy",
        role="member",
    )
    async with db_module.async_session() as session:
        session.add(Entity(id=legacy["entity_id"], name="Legacy identity entity"))
        await session.commit()

    current = await client.get("/api/v1/auth/me", headers=legacy["headers"])
    assert current.status_code == 200, current.text
    async with db_module.async_session() as session:
        membership = (
            await session.execute(
                select(UserMembership.id).where(
                    UserMembership.user_id == legacy["id"],
                    UserMembership.entity_id == legacy["entity_id"],
                )
            )
        ).scalar_one_or_none()
        assert membership is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["detail", "list"])
async def test_workspace_read_gate_rechecks_membership_before_response(
    client,
    monkeypatch,
    surface,
):
    from apps.api.routers import workspaces as workspace_router
    from packages.core.models.staff import Staff
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    headers, owner, member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        member_staff_id = (
            await session.execute(
                select(Staff.id).where(
                    Staff.entity_id == owner["entity_id"],
                    Staff.user_id == member["user_id"],
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name=f"Gate race {surface}",
            settings={"access_mode": "members_only"},
        )
        session.add(workspace)
        await session.flush()
        membership = WorkspaceStaff(
            workspace_id=workspace.id,
            staff_id=member_staff_id,
            user_id=member["user_id"],
            role="viewer",
            status="active",
        )
        session.add(membership)
        await session.commit()
        workspace_id = workspace.id
        membership_id = membership.id

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_blueprint_payloads = workspace_router._blueprint_payloads_for

    async def blueprint_barrier(db, rows):
        payloads = await original_blueprint_payloads(db, rows)
        if any(str(row.id) == workspace_id for row in rows):
            ready.set()
            await proceed.wait()
        return payloads

    monkeypatch.setattr(
        workspace_router,
        "_blueprint_payloads_for",
        blueprint_barrier,
    )
    path = f"/api/v1/workspaces/{workspace_id}" if surface == "detail" else "/api/v1/workspaces"
    pending = asyncio.create_task(client.get(path, headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            membership = await session.get(WorkspaceStaff, membership_id)
            membership.status = "inactive"
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        if surface == "detail":
            assert response.status_code == 404, response.text
        else:
            assert response.status_code == 200, response.text
            assert workspace_id not in {row["id"] for row in response.json()}
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["detail", "list"])
async def test_workspace_can_manage_uses_membership_after_final_read_gate(
    client,
    monkeypatch,
    surface,
):
    from packages.core.models.staff import Staff
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        member_staff_id = (
            await session.execute(
                select(Staff.id).where(
                    Staff.entity_id == owner["entity_id"],
                    Staff.user_id == member["user_id"],
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name=f"Manage projection race {surface}",
            settings={"access_mode": "members_only"},
        )
        session.add(workspace)
        await session.flush()
        membership = WorkspaceStaff(
            workspace_id=workspace.id,
            staff_id=member_staff_id,
            user_id=member["user_id"],
            role="owner",
            status="active",
        )
        session.add(membership)
        await session.commit()
        workspace_id = workspace.id
        membership_id = membership.id

    ready = asyncio.Event()
    proceed = asyncio.Event()
    method_name = (
        "authorize_workspace_read"
        if surface == "detail"
        else "authorize_workspace_batch_read"
    )
    original_gate = getattr(ResourcePermissionGate, method_name)
    call_count = 0

    async def gate_barrier(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            ready.set()
            await proceed.wait()
        return await original_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        method_name,
        staticmethod(gate_barrier),
    )
    path = f"/api/v1/workspaces/{workspace_id}" if surface == "detail" else "/api/v1/workspaces"
    pending = asyncio.create_task(client.get(path, headers=member_headers))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            membership = await session.get(WorkspaceStaff, membership_id)
            membership.role = "viewer"
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        payload = response.json()
        row = payload if surface == "detail" else next(
            item for item in payload if item["id"] == workspace_id
        )
        assert row["can_manage"] is False
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_folder_gate_rejects_identity_revoked_after_grant(client):
    from packages.core.services.actor_authorization import AuthenticatedUserCredential
    from packages.core.services.permission_gate import ResourcePermissionGate

    headers, owner, member_headers, member = await _actors(client)
    _doc_id, folder_id = await _document(owner, in_folder=True)
    granted = await client.post(
        f"/api/v1/folders/{folder_id}/grants",
        headers=headers,
        json={"subject_id": member["user_id"], "capabilities": ["view"]},
    )
    assert granted.status_code == 201, granted.text

    async with db_module.async_session() as session:
        user = await session.get(User, member["user_id"])
        folder = await session.get(DocumentFolder, folder_id)
        credential = AuthenticatedUserCredential.from_user(user)
        assert (
            await ResourcePermissionGate.authorize_folder_read(
                session,
                credential=credential,
                folder=folder,
            )
            is not None
        )
        user.token_version = int(user.token_version or 0) + 1
        await session.commit()

    async with db_module.async_session() as session:
        folder = await session.get(DocumentFolder, folder_id)
        assert (
            await ResourcePermissionGate.authorize_folder_read(
                session,
                credential=credential,
                folder=folder,
            )
            is None
        )
    assert (await client.get("/api/v1/auth/me", headers=member_headers)).status_code == 401


@pytest.mark.asyncio
async def test_folder_gate_enforces_ancestor_and_workspace_owner_boundaries(client):
    from packages.core.models.staff import Staff
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from packages.core.services.actor_authorization import AuthenticatedUserCredential
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, _member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        root = DocumentFolder(
            entity_id=owner["entity_id"],
            name="Gate ancestor",
            owner_id=owner["id"],
            visibility="private",
            classification="internal",
        )
        session.add(root)
        await session.flush()
        child = DocumentFolder(
            entity_id=owner["entity_id"],
            name="Gate child",
            parent_id=root.id,
            owner_id=owner["id"],
            visibility="entity",
            classification="internal",
        )
        session.add(child)
        await session.commit()
        child_id = child.id
        root_id = root.id

    async with db_module.async_session() as session:
        user = await session.get(User, member["user_id"])
        child = await session.get(DocumentFolder, child_id)
        credential = AuthenticatedUserCredential.from_user(user)
        assert (
            await ResourcePermissionGate.authorize_folder_read(
                session,
                credential=credential,
                folder=child,
            )
            is None
        )

        root = await session.get(DocumentFolder, root_id)
        root.visibility = "entity"
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name="Folder owner boundary",
            artifact_folder_id=root_id,
            settings={"access_mode": "members_only"},
        )
        session.add(workspace)
        await session.commit()
        workspace_id = workspace.id

    async with db_module.async_session() as session:
        child = await session.get(DocumentFolder, child_id)
        assert (
            await ResourcePermissionGate.authorize_folder_read(
                session,
                credential=credential,
                folder=child,
            )
            is None
        )
        staff_id = (
            await session.execute(
                select(Staff.id).where(
                    Staff.entity_id == owner["entity_id"],
                    Staff.user_id == member["user_id"],
                    Staff.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        session.add(
            WorkspaceStaff(
                workspace_id=workspace_id,
                staff_id=staff_id,
                user_id=member["user_id"],
                role="viewer",
                status="active",
            )
        )
        await session.commit()

    async with db_module.async_session() as session:
        child = await session.get(DocumentFolder, child_id)
        assert (
            await ResourcePermissionGate.authorize_folder_read(
                session,
                credential=credential,
                folder=child,
            )
            is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["folders", "folder-tree", "browse"])
async def test_folder_list_gate_rechecks_grant_before_response(
    client,
    monkeypatch,
    surface,
):
    from packages.core.services.permission_gate import ResourcePermissionGate

    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    granted = await client.post(
        f"/api/v1/folders/{folder_id}/grants",
        headers=headers,
        json={"subject_id": member["user_id"], "capabilities": ["view"]},
    )
    assert granted.status_code == 201, granted.text

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_folder_gate = ResourcePermissionGate.authorize_folder_batch_read
    gate_calls = 0
    final_gate_call = 3 if surface == "browse" else 2

    async def folder_gate_barrier(*args, **kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == final_gate_call:
            ready.set()
            await proceed.wait()
        return await original_folder_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_folder_batch_read",
        staticmethod(folder_gate_barrier),
    )

    pending = asyncio.create_task(
        client.get(f"/api/v1/documents/{surface}", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        revoked = await client.delete(
            f"/api/v1/folders/{folder_id}/grants/{granted.json()['id']}",
            headers=headers,
        )
        assert revoked.status_code == 204, revoked.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        payload = response.json()
        rows = payload["folders"] if surface == "browse" else payload
        assert folder_id not in {row["id"] for row in rows}
        if surface == "browse":
            assert doc_id not in {row["id"] for row in payload["items"]}
            assert payload["total"] == len(payload["items"])
            assert payload["total_files"] >= payload["total"]
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_browse_final_document_gate_reloads_current_policy(client, monkeypatch):
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, member_headers, _member = await _actors(client)
    doc_id, _folder_id = await _document(owner)
    async with db_module.async_session() as session:
        document = await session.get(Document, doc_id)
        document.visibility = "entity"
        await session.commit()

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_document_gate = ResourcePermissionGate.authorize_document_batch_read

    async def document_gate_barrier(*args, **kwargs):
        if any(str(document.id) == doc_id for document in kwargs["documents"]):
            ready.set()
            await proceed.wait()
        return await original_document_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_document_batch_read",
        staticmethod(document_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get("/api/v1/documents/browse?scope=all", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            document = await session.get(Document, doc_id)
            document.visibility = "private"
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        assert doc_id not in {row["id"] for row in response.json()["items"]}
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_browse_final_folder_gate_reloads_current_policy(client, monkeypatch):
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, member_headers, _member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        folder = await session.get(DocumentFolder, folder_id)
        folder.visibility = "entity"
        document = await session.get(Document, doc_id)
        document.visibility = "entity"
        await session.commit()

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_folder_gate = ResourcePermissionGate.authorize_folder_batch_read
    gate_calls = 0

    async def folder_gate_barrier(*args, **kwargs):
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 3:
            ready.set()
            await proceed.wait()
        return await original_folder_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_folder_batch_read",
        staticmethod(folder_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get("/api/v1/documents/browse", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            folder = await session.get(DocumentFolder, folder_id)
            folder.visibility = "private"
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert folder_id not in {row["id"] for row in payload["folders"]}
        assert doc_id not in {row["id"] for row in payload["items"]}
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["folders", "folder-tree"])
async def test_folder_counts_recheck_document_grant_before_response(
    client,
    monkeypatch,
    surface,
):
    from packages.core.services.permission_gate import ResourcePermissionGate

    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        folder = await session.get(DocumentFolder, folder_id)
        folder.visibility = "entity"
        await session.commit()
    granted = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=headers,
        json={"subject_id": member["user_id"], "capabilities": ["view"]},
    )
    assert granted.status_code == 201, granted.text

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_document_gate = ResourcePermissionGate.authorize_document_batch_read

    async def document_gate_barrier(*args, **kwargs):
        if any(str(document.id) == doc_id for document in kwargs["documents"]):
            ready.set()
            await proceed.wait()
        return await original_document_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_document_batch_read",
        staticmethod(document_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get(f"/api/v1/documents/{surface}", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        revoked = await client.delete(
            f"/api/v1/documents/{doc_id}/grants/{granted.json()['id']}",
            headers=headers,
        )
        assert revoked.status_code == 204, revoked.text
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        folder = next(row for row in response.json() if row["id"] == folder_id)
        assert folder["document_count"] == 0
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_folder_counts_reload_active_grant_expiry_before_response(
    client,
    monkeypatch,
):
    from packages.core.services.permission_gate import ResourcePermissionGate

    headers, owner, member_headers, member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        folder = await session.get(DocumentFolder, folder_id)
        folder.visibility = "entity"
        await session.commit()
    granted = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=headers,
        json={
            "subject_id": member["user_id"],
            "capabilities": ["view"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert granted.status_code == 201, granted.text

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_document_gate = ResourcePermissionGate.authorize_document_batch_read

    async def document_gate_barrier(*args, **kwargs):
        if any(str(document.id) == doc_id for document in kwargs["documents"]):
            ready.set()
            await proceed.wait()
        return await original_document_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_document_batch_read",
        staticmethod(document_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get("/api/v1/documents/folders", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            grant = await session.get(ResourceGrant, granted.json()["id"])
            grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        folder = next(row for row in response.json() if row["id"] == folder_id)
        assert folder["document_count"] == 0
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["folders", "folder-tree", "browse"])
async def test_folder_surfaces_gate_folders_after_document_refresh(
    client,
    monkeypatch,
    surface,
):
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, member_headers, _member = await _actors(client)
    doc_id, folder_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        folder = await session.get(DocumentFolder, folder_id)
        folder.visibility = "entity"
        document = await session.get(Document, doc_id)
        document.visibility = "entity"
        await session.commit()

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_document_gate = ResourcePermissionGate.authorize_document_batch_read
    blocked = False

    async def document_gate_barrier(*args, **kwargs):
        nonlocal blocked
        if not blocked and any(
            str(document.id) == doc_id for document in kwargs["documents"]
        ):
            blocked = True
            ready.set()
            await proceed.wait()
        return await original_document_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_document_batch_read",
        staticmethod(document_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get(f"/api/v1/documents/{surface}", headers=member_headers)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            folder = await session.get(DocumentFolder, folder_id)
            folder.visibility = "private"
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        payload = response.json()
        rows = payload["folders"] if surface == "browse" else payload
        assert folder_id not in {row["id"] for row in rows}
        if surface == "browse":
            assert doc_id not in {row["id"] for row in payload["items"]}
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_browse_reapplies_current_document_and_folder_scope(client, monkeypatch):
    from packages.core.services.permission_gate import ResourcePermissionGate

    _headers, owner, member_headers, _member = await _actors(client)
    doc_id, parent_id = await _document(owner, in_folder=True)
    async with db_module.async_session() as session:
        parent = await session.get(DocumentFolder, parent_id)
        parent.visibility = "entity"
        document = await session.get(Document, doc_id)
        document.visibility = "entity"
        child = DocumentFolder(
            entity_id=owner["entity_id"],
            name=f"Child {generate_ulid()}",
            parent_id=parent_id,
            owner_id=owner["id"],
            visibility="entity",
            classification="internal",
        )
        destination = DocumentFolder(
            entity_id=owner["entity_id"],
            name=f"Destination {generate_ulid()}",
            owner_id=owner["id"],
            visibility="entity",
            classification="internal",
        )
        session.add_all([child, destination])
        await session.commit()
        child_id = child.id
        destination_id = destination.id

    ready = asyncio.Event()
    proceed = asyncio.Event()
    original_document_gate = ResourcePermissionGate.authorize_document_batch_read

    async def document_gate_barrier(*args, **kwargs):
        if any(str(document.id) == doc_id for document in kwargs["documents"]):
            ready.set()
            await proceed.wait()
        return await original_document_gate(*args, **kwargs)

    monkeypatch.setattr(
        ResourcePermissionGate,
        "authorize_document_batch_read",
        staticmethod(document_gate_barrier),
    )
    pending = asyncio.create_task(
        client.get(
            "/api/v1/documents/browse",
            headers=member_headers,
            params={"folder_id": parent_id},
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
        async with db_module.async_session() as session:
            document = await session.get(Document, doc_id)
            document.folder_id = destination_id
            child = await session.get(DocumentFolder, child_id)
            child.parent_id = None
            await session.commit()
        proceed.set()
        response = await asyncio.wait_for(pending, timeout=60)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert doc_id not in {row["id"] for row in payload["items"]}
        assert child_id not in {row["id"] for row in payload["folders"]}
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    ("overrides", "filters"),
    [
        ({"fs_path": ".cache/hidden.md"}, {}),
        (
            {"source": "agent", "mime_type": "video/mp4", "file_type": "mp4"},
            {"include_generated_assets": False},
        ),
        (
            {"name": "renamed.md", "fs_path": "Knowledge/renamed.md", "metadata_": {}},
            {"search_query": "needle"},
        ),
        ({}, {"workspace_id": "workspace-2"}),
    ],
    ids=["hidden-path", "generated-media", "search", "workspace"],
)
def test_browse_filters_recheck_gate_refreshed_fields(overrides, filters):
    from apps.api.routers.documents import _document_matches_browse_filters

    fields = {
        "name": "needle.md",
        "fs_path": "Knowledge/needle.md",
        "file_type": "md",
        "mime_type": "text/markdown",
        "source": "upload",
        "metadata_": {"tag": "needle"},
    }
    fields.update(overrides)
    document = SimpleNamespace(**fields)
    access_context = SimpleNamespace(
        document_workspace_ids=lambda _document: {"workspace-1"},
    )
    request_filters = {
        "search_query": None,
        "workspace_id": None,
        "include_generated_assets": True,
    }
    request_filters.update(filters)

    assert not _document_matches_browse_filters(
        document,
        access_context=access_context,
        **request_filters,
    )


@pytest.mark.asyncio
async def test_actor_resolution_does_not_refresh_request_user_entity(client):
    from sqlalchemy.orm.attributes import set_committed_value

    from packages.core.services.actor_authorization import (
        AuthenticatedUserCredential,
        resolve_current_user_actor,
    )

    _headers, owner, _member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        personal = Entity(name=f"Actor snapshot {generate_ulid()}")
        session.add(personal)
        await session.flush()
        identity = await session.get(User, member["user_id"])
        identity.entity_id = personal.id
        identity.role = "owner"
        session.add(
            UserMembership(
                user_id=identity.id,
                entity_id=personal.id,
                role="owner",
                status="active",
            )
        )
        await session.commit()

    async with db_module.async_session() as session:
        identity = await session.get(User, member["user_id"])
        token_version = int(identity.token_version or 0)
        set_committed_value(identity, "entity_id", owner["entity_id"])
        actor = await resolve_current_user_actor(
            session,
            AuthenticatedUserCredential(
                user_id=str(identity.id),
                entity_id=owner["entity_id"],
                token_version=token_version,
            ),
        )
        assert actor is not None
        assert actor.entity_id == owner["entity_id"]
        assert identity.entity_id == owner["entity_id"]


@pytest.mark.asyncio
async def test_browse_never_switches_to_users_current_primary_entity(client):
    _headers, owner, member_headers, member = await _actors(client)
    async with db_module.async_session() as session:
        personal = Entity(name=f"Browse snapshot {generate_ulid()}")
        session.add(personal)
        await session.flush()
        identity = await session.get(User, member["user_id"])
        identity.entity_id = personal.id
        identity.role = "owner"
        session.add(
            UserMembership(
                user_id=identity.id,
                entity_id=personal.id,
                role="owner",
                status="active",
            )
        )
        secret = Document(
            entity_id=personal.id,
            name="other-entity-secret.md",
            owner_id=identity.id,
            created_by=identity.id,
            visibility="private",
            classification="internal",
            file_type="md",
            mime_type="text/markdown",
            metadata_={"content": "other entity secret"},
        )
        session.add(secret)
        await session.commit()
        secret_id = secret.id

    response = await client.get(
        "/api/v1/documents/browse?scope=all",
        headers=member_headers,
    )
    assert response.status_code == 200, response.text
    assert secret_id not in {row["id"] for row in response.json()["items"]}


@pytest.mark.asyncio
async def test_private_raw_file_rejects_revoked_bearer_token(
    client,
    monkeypatch,
    tmp_path,
):
    from packages.core.services.entity_fs import get_entity_root

    _patch_filesystem_settings(monkeypatch, tmp_path, enabled=True)
    headers = await _auth(client, "raw_file_revoked_token")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_root = get_entity_root(owner["entity_id"])
    os.makedirs(entity_root, exist_ok=True)
    with open(os.path.join(entity_root, "private-raw.md"), "w", encoding="utf-8") as file:
        file.write("raw secret")

    path = f"/api/v1/fs/{owner['entity_id']}/private-raw.md"
    allowed = await client.get(path, headers=headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.text == "raw secret"

    async with db_module.async_session() as session:
        user = await session.get(User, owner["id"])
        user.token_version = int(user.token_version or 0) + 1
        await session.commit()

    denied = await client.get(path, headers=headers)
    assert denied.status_code == 401, denied.text
    assert "raw secret" not in denied.text
