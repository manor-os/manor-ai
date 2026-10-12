"""Owner-equivalent resource grants retain delegation and classification gates."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from pypdf import PdfWriter

import packages.core.database as db_module
from packages.core.models import Document, DocumentFolder
from packages.core.models.permission import ResourceGrantPending, Share
from packages.core.services.resource_grant_policy import ResourceGrantPolicyFactory
from test_document_permissions import (
    _auth, _connect_signature_mailbox, _create_entity_user, _invite_and_accept_member, _upload,
)


async def _actors(client):
    owner_headers = await _auth(client, "coowner_source")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    colleague_headers, colleague = await _invite_and_accept_member(client, owner_headers, "coowner_colleague@test.com")
    return owner_headers, owner, colleague_headers, colleague


async def _grant(client, base, owner_headers, colleague, *, expiry=None):
    response = await client.post(f"{base}/grants", headers=owner_headers, json={
        "subject_id": colleague["user_id"],
        "capabilities": sorted(ResourceGrantPolicyFactory.create("document").allowed_capabilities),
        "expires_at": expiry.isoformat() if expiry else None,
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_co_owner_has_owner_operations_and_revocation_removes_them(client):
    headers, owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private")
    base = f"/api/v1/documents/{doc['id']}"
    manager = await client.post(f"{base}/grants", headers=headers, json={
        "subject_id": colleague["user_id"],
        "capabilities": ["view", "comment", "edit", "manage_metadata", "grant_access", "share_internal"],
    })
    assert manager.status_code == 201, manager.text
    for path in ["access-log", "access-requests", "signature-requests", "signature-senders"]:
        assert (await client.get(f"{base}/{path}", headers=colleague_headers)).status_code == 403
    grant = await _grant(client, base, headers, colleague)
    assert grant["id"] == manager.json()["id"]
    original = await client.get(base, headers=headers)
    delegated = await client.get(base, headers=colleague_headers)
    assert delegated.status_code == 200, delegated.text
    assert delegated.json()["owner_id"] == owner["id"]
    assert set(delegated.json()["current_user_capabilities"]) == set(original.json()["current_user_capabilities"])
    for path in ["grants", "access-log", "access-requests", "signature-requests", "signature-senders", "share-approvals"]:
        response = await client.get(f"{base}/{path}", headers=colleague_headers)
        assert response.status_code == 200, (path, response.text)
    shared = await client.post(f"{base}/shares", headers=colleague_headers, json={"audience_type": "anonymous", "capabilities": ["view"]})
    assert shared.status_code == 201, shared.text
    renamed = await client.put(base, headers=colleague_headers, json={"name": "coowner-renamed.md"})
    assert renamed.status_code == 200, renamed.text
    trashed = await client.post(f"{base}/trash", headers=colleague_headers)
    assert trashed.status_code == 200, trashed.text
    # Trash revokes every document sharing grant and link, including Co-owner.
    # Restoration must not silently resurrect the revoked delegation.
    restored = await client.post(f"{base}/restore", headers=colleague_headers)
    assert restored.status_code == 403, restored.text
    restored = await client.post(f"{base}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"{base}/signature-requests", headers=colleague_headers)).status_code == 403
    grant = await _grant(client, base, headers, colleague)
    revoked = await client.delete(f"{base}/grants/{grant['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    for path in ["access-log", "access-requests", "signature-requests", "signature-senders"]:
        assert (await client.get(f"{base}/{path}", headers=colleague_headers)).status_code == 403


@pytest.mark.asyncio
async def test_temporary_co_owner_approves_access_without_extending_delegation(client):
    headers, owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private")
    base = f"/api/v1/documents/{doc['id']}"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    await _grant(client, base, headers, colleague, expiry=expiry)
    recipient = await _create_entity_user(owner["entity_id"], "coowner_recipient")
    pending = await client.post("/api/v1/permissions/access-requests", headers=recipient["headers"], json={
        "resource_type": "document", "resource_id": doc["id"], "requested_capabilities": ["view", "comment"],
    })
    assert pending.status_code == 200, pending.text
    approved = await client.post(f"{base}/access-requests/{pending.json()['id']}/decision", headers=colleague_headers, json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    grants = (await client.get(f"{base}/grants", headers=headers)).json()
    delegated = next(row for row in grants if row["subject_id"] == recipient["id"])
    assert datetime.fromisoformat(delegated["expires_at"]) == expiry
    escalation = await client.post(f"{base}/grants", headers=colleague_headers, json={
        "subject_id": recipient["id"], "capabilities": ["view"], "expires_at": (expiry + timedelta(days=1)).isoformat(),
    })
    assert escalation.status_code == 403, escalation.text


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["confidential", "restricted"])
async def test_co_owner_grant_keeps_protected_document_policy(client, classification):
    headers, _owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private", classification=classification)
    base = f"/api/v1/documents/{doc['id']}"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    grant = await _grant(client, base, headers, colleague, expiry=expiry)
    shared = await client.post(f"{base}/shares", headers=colleague_headers, json={"audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"]})
    assert shared.status_code == (409 if classification == "confidential" else 400), shared.text
    requested = await client.post(f"{base}/share-approvals", headers=colleague_headers, json={
        "audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"], "reason": "Review this document",
    })
    if classification == "restricted":
        assert requested.status_code == 400, requested.text
        return
    assert requested.status_code == 201, requested.text
    approved = await client.post(f"{base}/share-approvals/{requested.json()['id']}/decision", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    shares = (await client.get(f"{base}/shares", headers=colleague_headers)).json()
    assert datetime.fromisoformat(shares[0]["expires_at"]) == expiry
    requested = await client.post(f"{base}/share-approvals", headers=colleague_headers, json={
        "audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"], "reason": "Another review",
    })
    assert requested.status_code == 201, requested.text
    assert (await client.delete(f"{base}/grants/{grant['id']}", headers=headers)).status_code == 204
    approved = await client.post(f"{base}/share-approvals/{requested.json()['id']}/decision", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 403, approved.text


@pytest.mark.asyncio
async def test_renewed_co_owner_cannot_reuse_expired_share_approval_authority(client):
    headers, _owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private", classification="confidential")
    base = f"/api/v1/documents/{doc['id']}"
    await _grant(client, base, headers, colleague, expiry=datetime.now(timezone.utc) + timedelta(hours=1))
    requested = await client.post(f"{base}/share-approvals", headers=colleague_headers, json={
        "audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"], "reason": "Review",
    })
    assert requested.status_code == 201, requested.text
    # The current grant is live, but the authority snapshotted by an older
    # request has already expired. Renewing access must require a new request.
    async with db_module.async_session() as session:
        pending = await session.get(ResourceGrantPending, requested.json()["id"])
        pending.metadata_ = {**pending.metadata_, "delegated_expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
        await session.commit()
    approved = await client.post(f"{base}/share-approvals/{requested.json()['id']}/decision", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 403, approved.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("grant_days", "share_days"), [(None, 1), (10, 1), (None, None)])
async def test_co_owner_share_lifetime_starts_on_approval(client, monkeypatch, grant_days, share_days):
    from apps.api.routers import document_permissions

    headers, owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private", classification="confidential")
    base = f"/api/v1/documents/{doc['id']}"
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=grant_days) if grant_days else None
    await _grant(client, base, headers, colleague, expiry=expiry)
    config = {
        "audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"],
        "expires_in_days": share_days, "require_otp": True, "reason": "Review",
    }
    requests = []
    for requester_headers in (colleague_headers, headers):
        requested = await client.post(f"{base}/share-approvals", headers=requester_headers, json=config)
        assert requested.status_code == 201, requested.text
        requests.append(requested.json()["id"])
    async with db_module.async_session() as session:
        pending = await session.get(ResourceGrantPending, requests[0])
        assert pending.metadata_["delegated_expires_at"] == (expiry.isoformat() if expiry else None)

    approved_at = now + timedelta(days=2)

    class ApprovalDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return approved_at if tz else approved_at.replace(tzinfo=None)

    monkeypatch.setattr(document_permissions, "datetime", ApprovalDatetime)
    expected_expiry = approved_at + timedelta(days=share_days) if share_days else None
    for request_id, creator_id in zip(requests, (colleague["user_id"], owner["id"])):
        approved = await client.post(f"{base}/share-approvals/{request_id}/decision", headers=headers, json={"decision": "approve"})
        assert approved.status_code == 200, approved.text
        async with db_module.async_session() as session:
            share = await session.get(Share, approved.json()["approval"]["approved_share_id"])
            assert share.created_at == approved_at
            assert share.expires_at == expected_expiry
            assert share.created_by == creator_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("original_hours", "current_hours"), [(2, 24), (24, 2)])
async def test_co_owner_share_approval_keeps_original_and_current_authority_bounds(client, original_hours, current_hours):
    headers, _owner, colleague_headers, colleague = await _actors(client)
    doc = await _upload(client, headers, visibility="private", classification="confidential")
    base = f"/api/v1/documents/{doc['id']}"
    now = datetime.now(timezone.utc)
    original_expiry = now + timedelta(hours=original_hours)
    await _grant(client, base, headers, colleague, expiry=original_expiry)
    requested = await client.post(f"{base}/share-approvals", headers=colleague_headers, json={
        "audience_type": "email", "audience_value": "recipient@example.test", "capabilities": ["view"],
        "expires_in_days": 7, "reason": "Review",
    })
    assert requested.status_code == 201, requested.text
    current_expiry = now + timedelta(hours=current_hours)
    await _grant(client, base, headers, colleague, expiry=current_expiry)
    approved = await client.post(f"{base}/share-approvals/{requested.json()['id']}/decision", headers=headers, json={"decision": "approve"})
    assert approved.status_code == 200, approved.text
    async with db_module.async_session() as session:
        share = await session.get(Share, approved.json()["approval"]["approved_share_id"])
        assert share.expires_at == min(original_expiry, current_expiry)


@pytest.mark.asyncio
async def test_co_owner_sends_signature_from_own_mailbox_with_bounded_expiry(client, monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    headers, _owner, colleague_headers, colleague = await _actors(client)
    mailbox = await _connect_signature_mailbox(client, colleague_headers, address="colleague@example.test")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    uploaded = await client.post("/api/v1/documents/upload?visibility=private", headers=headers, files={"file": ("coowner.pdf", buffer.getvalue(), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    base = f"/api/v1/documents/{uploaded.json()['id']}"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    await _grant(client, base, headers, colleague, expiry=expiry)
    deliveries = []

    async def capture(credentials, **kwargs):
        deliveries.append(kwargs)
        return True

    monkeypatch.setattr("packages.core.services.email_service.send_connected_email", capture)
    created = await client.post(f"{base}/signature-requests", headers=colleague_headers, json={
        "recipient_name": "Test recipient", "recipient_email": "recipient@example.test", "sender_integration_id": mailbox,
        "recipient_emails": ["recipient@example.test", "alternate@example.test"],
        "fields": [{"id": "signature", "type": "signature", "label": "Signature", "required": True, "page": 1, "x": .1, "y": .2, "width": .3, "height": .1}],
    })
    assert created.status_code == 201, created.text
    assert datetime.fromisoformat(created.json()["expires_at"]) == expiry
    assert len(deliveries) == 2
    assert all(delivery["expected_sender"] == "colleague@example.test" for delivery in deliveries)


@pytest.mark.asyncio
async def test_folder_co_owner_can_manage_properties_and_inherits_full_document_access(client):
    headers, owner, colleague_headers, colleague = await _actors(client)
    async with db_module.async_session() as session:
        folder = DocumentFolder(entity_id=owner["entity_id"], owner_id=owner["id"], name="Co-owner folder", visibility="private", classification="internal")
        session.add(folder)
        await session.flush()
        doc = Document(entity_id=owner["entity_id"], owner_id=owner["id"], created_by=owner["id"], name="inherited.md", visibility="private", classification="internal", folder_id=folder.id, metadata_={"content": "test"})
        session.add(doc)
        await session.commit()
        folder_id, doc_id = folder.id, doc.id
    granted = await client.post(f"/api/v1/folders/{folder_id}/grants", headers=headers, json={
        "subject_id": colleague["user_id"], "capabilities": sorted(ResourceGrantPolicyFactory.create("document_folder").allowed_capabilities),
    })
    assert granted.status_code == 201, granted.text
    changed = await client.post(f"/api/v1/folders/{folder_id}/properties", headers=colleague_headers, json={"visibility": "private"})
    assert changed.status_code == 200, changed.text
    renamed = await client.put(f"/api/v1/documents/folders/{folder_id}", headers=colleague_headers, json={"name": "Renamed co-owner folder"})
    assert renamed.status_code == 200, renamed.text
    log = await client.get(f"/api/v1/documents/{doc_id}/access-log", headers=colleague_headers)
    assert log.status_code == 200, log.text
    visible = await client.get(f"/api/v1/documents/{doc_id}", headers=colleague_headers)
    assert visible.status_code == 200, visible.text
    assert ResourceGrantPolicyFactory.create("document").allowed_capabilities.issubset(visible.json()["current_user_capabilities"])
    deleted = await client.delete(f"/api/v1/documents/folders/{folder_id}", headers=colleague_headers)
    assert deleted.status_code == 204, deleted.text


@pytest.mark.asyncio
async def test_folder_co_owner_cannot_rename_workspace_without_workspace_management(client):
    from packages.core.ai.runtime.manor_actions import runtime_manor_rename_document_folder

    headers, _owner, colleague_headers, colleague = await _actors(client)
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Owner Workspace"})
    assert created.status_code == 201, created.text
    workspace = created.json()
    folder_id = workspace["artifact_folder_id"]
    child = await client.post("/api/v1/documents/folders", headers=headers, json={"name": "Child folder", "parent_id": folder_id})
    assert child.status_code == 201, child.text
    granted = await client.post(f"/api/v1/folders/{folder_id}/grants", headers=headers, json={
        "subject_id": colleague["user_id"],
        "capabilities": sorted(ResourceGrantPolicyFactory.create("document_folder").allowed_capabilities),
    })
    assert granted.status_code == 201, granted.text
    direct = await client.put(f"/api/v1/workspaces/{workspace['id']}", headers=colleague_headers, json={"name": "Blocked rename"})
    assert direct.status_code in (403, 404), direct.text
    root_rename = await client.put(f"/api/v1/documents/folders/{folder_id}", headers=colleague_headers, json={"name": "Blocked rename"})
    assert root_rename.status_code == 403, root_rename.text
    async with db_module.async_session() as session:
        tool_rename = json.loads(await runtime_manor_rename_document_folder(
            session, entity_id=workspace["entity_id"], user_id=colleague["user_id"],
            params={"folder_id": folder_id, "name": "Blocked tool rename"},
        ))
        assert tool_rename.get("error") == "Only Workspace owners/admins can rename this Workspace folder", tool_rename
    unchanged = await client.get(f"/api/v1/workspaces/{workspace['id']}", headers=headers)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["name"] == "Owner Workspace"
    child_rename = await client.put(f"/api/v1/documents/folders/{child.json()['id']}", headers=colleague_headers, json={"name": "Renamed child"})
    assert child_rename.status_code == 200, child_rename.text
    async with db_module.async_session() as session:
        tool_child_rename = json.loads(await runtime_manor_rename_document_folder(
            session, entity_id=workspace["entity_id"], user_id=colleague["user_id"],
            params={"folder_id": child.json()["id"], "name": "Tool renamed child"},
        ))
        assert tool_child_rename.get("updated") is True, tool_child_rename
    owner_rename = await client.put(f"/api/v1/documents/folders/{folder_id}", headers=headers, json={"name": "Owner renamed Workspace"})
    assert owner_rename.status_code == 200, owner_rename.text
    updated = await client.get(f"/api/v1/workspaces/{workspace['id']}", headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Owner renamed Workspace"
