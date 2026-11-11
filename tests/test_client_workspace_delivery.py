"""Private delivery must preserve customer ownership without a provider Staff grant."""

from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.client_delivery import ClientContact, ClientWorkspaceDelivery
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.user_email_proof import mark_email_verified


async def register(client, db, name, *, verified=True):
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": name, "email": f"{name}@delivery.test", "password": "DeliveryFixture123!", "entity_name": name},
    )
    assert response.status_code in (200, 201), response.text
    user = (await db.execute(select(User).where(User.email == f"{name}@delivery.test"))).scalar_one()
    if verified:
        mark_email_verified(user, user.email)  # fixture-only proof, never a production bypass
        await db.commit()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}, user


async def prepared(client, db, *, phase="trial", suffix=""):
    provider_headers, provider = await register(client, db, f"provider{suffix}")
    recipient_headers, recipient = await register(client, db, f"recipient{suffix}")
    c = await client.post("/api/v1/clients", headers=provider_headers, json={"name": "Customer"})
    assert c.status_code == 201, c.text
    cid = c.json()["id"]
    contact = await client.post(
        f"/api/v1/clients/{cid}/contacts",
        headers=provider_headers,
        json={"name": "Marketing owner", "email": recipient.email},
    )
    assert contact.status_code == 201, contact.text
    ws = await client.post(
        "/api/v1/workspaces",
        headers=provider_headers,
        json={"name": "Private marketing", "kind": "campaign", "primary_work": "Make bilingual campaigns"},
    )
    assert ws.status_code == 201, ws.text
    create = await client.post(
        f"/api/v1/clients/{cid}/deliveries",
        headers=provider_headers,
        json={"workspace_id": ws.json()["id"], "contact_id": contact.json()["id"], "phase": phase},
    )
    assert create.status_code == 201, create.text
    token = create.json()["invite_path"].rsplit("/", 1)[-1]
    return provider_headers, provider, recipient_headers, recipient, cid, ws.json()["id"], create.json(), token


@pytest.mark.asyncio
async def test_accept_is_idempotent_and_customer_owned_without_staff_or_marketplace(client, db_session):
    # Catches: a repeat acceptance issuing a second trial grant or seat; the
    # provider's trial access becoming Staff or a real company membership; the
    # provider's home-company token reaching the customer's Workspace.
    from packages.core.models.workspace_collaboration import WorkspaceCollaboration

    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    before = (await db_session.execute(select(func.count()).select_from(Staff).where(Staff.user_id == r.id))).scalar()
    preview = await client.get(f"/api/v1/client-deliveries/invites/{token}", headers=rh)
    assert preview.status_code == 200, preview.text
    assert "payload" not in preview.json()
    a = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert a.status_code == 200, a.text
    b = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert b.status_code == 200, b.text
    target = a.json()["target_workspace_id"]
    assert target == b.json()["target_workspace_id"] and target != source
    ws = await db_session.get(Workspace, target)
    assert ws.entity_id == r.entity_id and ws.status == "paused"
    owners = (
        (await db_session.execute(select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target))).scalars().all()
    )
    # The trial provider co-works through one seat derived from one grant.
    grants = (await db_session.scalars(select(WorkspaceCollaboration))).all()
    assert [(g.role, g.status, g.user_id, g.workspace_id, g.expires_at) for g in grants] == [
        ("provider_trial", "active", p.id, target, None)
    ]
    assert sorted((m.user_id, m.role, m.staff_id, m.collaboration_id) for m in owners) == sorted(
        [(r.id, "owner", None, None), (p.id, "owner", None, grants[0].id)]
    )
    provider_membership = (
        await db_session.scalars(
            select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == r.entity_id)
        )
    ).all()
    assert [(m.role, m.status, m.is_primary) for m in provider_membership] == [("collaborator", "active", False)]
    assert (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)).json()[0]["provider_access"] == "trial"
    assert (
        await db_session.execute(select(func.count()).select_from(Staff).where(Staff.user_id == r.id))
    ).scalar() == before
    assert not (await db_session.scalars(select(Staff).where(Staff.user_id == p.id, Staff.entity_id == r.entity_id))).all()
    assert (
        not (await db_session.execute(select(Staff).where(Staff.entity_id == p.entity_id, Staff.user_id == r.id)))
        .scalars()
        .all()
    )
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{source}", headers=ph)).status_code == 200
    assert (await db_session.execute(select(func.count()).select_from(WorkspaceBlueprint))).scalar() == 0
    contact = (await db_session.execute(select(ClientContact).where(ClientContact.client_id == cid))).scalar_one()
    assert contact.user_id == r.id
    pinned = await db_session.get(ClientWorkspaceDelivery, receipt["id"])
    roundtrip = await export_workspace(
        db_session,
        target,
        title=pinned.payload["manifest"]["title"],
        context=ExportContext(knowledge_document_ids=frozenset()),
    )
    for section in ("subscriptions", "workflows", "scheduled_jobs", "stats", "goals"):
        assert roundtrip["recipe"][section] == pinned.payload["recipe"][section]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "wrong_email",
        "unverified",
        "expired",
        "revoked",
        "deleted_source",
        "revoked_author",
        "member_recipient",
        "deleted_company",
        "deleted_client",
    ],
)
async def test_pending_invitation_fails_closed(client, db_session, failure):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    row = await db_session.get(ClientWorkspaceDelivery, receipt["id"])
    if failure == "wrong_email":
        rh, _ = await register(client, db_session, "outsider")
    elif failure == "unverified":
        r.verified_email = None
    elif failure == "expired":
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif failure == "revoked":
        row.status = "revoked"
    elif failure == "deleted_source":
        ws = await db_session.get(Workspace, source)
        ws.deleted_at = datetime.now(timezone.utc)
    elif failure == "deleted_company":
        from packages.core.models.user import Entity

        source_entity = await db_session.get(Entity, p.entity_id)
        source_entity.deleted_at = datetime.now(timezone.utc)
    elif failure == "deleted_client":
        from packages.core.models.people import Client

        source_client = await db_session.get(Client, cid)
        source_client.deleted_at = datetime.now(timezone.utc)
    elif failure == "revoked_author":
        membership = (
            await db_session.execute(select(UserMembership).where(UserMembership.user_id == p.id))
        ).scalar_one()
        membership.status = "inactive"
    else:
        membership = (
            await db_session.execute(select(UserMembership).where(UserMembership.user_id == r.id))
        ).scalar_one()
        membership.role = "member"
        r.role = "member"
    await db_session.commit()
    result = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert result.status_code in (403, 404, 410), result.text
    await db_session.refresh(row)
    assert row.target_workspace_id is None
    assert (
        await db_session.execute(select(func.count()).select_from(Workspace).where(Workspace.entity_id == r.entity_id))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_rotation_invalidates_old_link_and_revoke_keeps_source(client, db_session):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    changed = await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/rotate", headers=ph)
    assert changed.status_code == 200, changed.text
    new = changed.json()["invite_path"].rsplit("/", 1)[-1]
    assert new != token
    assert (await client.get(f"/api/v1/client-deliveries/invites/{token}", headers=rh)).status_code == 404
    assert (await client.get(f"/api/v1/client-deliveries/invites/{new}", headers=rh)).status_code == 200
    assert (
        await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/revoke", headers=ph)
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/client-deliveries/invites/{new}/accept", headers=rh, json={})
    ).status_code == 410
    assert (await client.get(f"/api/v1/workspaces/{source}", headers=ph)).status_code == 200


@pytest.mark.asyncio
async def test_contacts_and_clients_are_company_private_and_create_no_staff(client, db_session):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    count = (await db_session.execute(select(func.count()).select_from(Staff))).scalar()
    duplicate = await client.post(
        f"/api/v1/clients/{cid}/contacts", headers=ph, json={"name": "Recipient", "email": r.email.upper()}
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == receipt["contact_id"]
    assert (await db_session.execute(select(func.count()).select_from(Staff))).scalar() == count
    assert (await client.get(f"/api/v1/clients/{cid}/contacts", headers=rh)).status_code == 404
    assert (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=rh)).status_code == 404
    member = (await db_session.execute(select(UserMembership).where(UserMembership.user_id == p.id))).scalar_one()
    member.role = "member"
    p.role = "member"
    role = StaffRole(entity_id=p.entity_id, name="Delivery viewer", permissions=[])
    db_session.add(role)
    await db_session.flush()
    staff = (await db_session.execute(select(Staff).where(Staff.user_id == p.id))).scalar_one()
    staff.role_id = role.id
    await db_session.commit()
    assert (await client.get("/api/v1/clients", headers=ph)).status_code == 200
    assert (await client.get(f"/api/v1/clients/{cid}/contacts", headers=ph)).status_code == 200


@pytest.mark.asyncio
async def test_delivery_rejects_unavailable_document_selection(client, db_session):
    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    response = await client.post(
        f"/api/v1/clients/{cid}/deliveries",
        headers=ph,
        json={
            "workspace_id": source,
            "contact_id": receipt["contact_id"],
            "knowledge_document_ids": ["private-document"],
        },
    )
    assert response.status_code == 422
    assert "unavailable or ineligible" in response.json()["detail"]
    assert (await db_session.execute(select(func.count()).select_from(ClientWorkspaceDelivery))).scalar() == 1


async def _template_delivery_source(client, db_session, monkeypatch, *, initial_files=False):
    from packages.core.blueprints.installer import InstallMode, install_blueprint
    from test_blueprint_knowledge_workflow_refs import payload

    monkeypatch.setenv("EMAIL_ENABLED", "false")
    parties = await prepared(client, db_session)
    provider = parties[1]
    data = payload()
    starters = data["embedded"]["knowledge_packs"][0]["starter_documents"]
    starters.extend([
        {"key": "guide", "path": "research/guide.md", "body_md": "# Research guide\nAdd your own creators and evidence.\n"},
        {"key": "output", "path": "research/generated-output.md", "body_md": "Private run evidence must not be copied automatically.\n"},
    ])
    if initial_files:
        for starter in starters[:2]:
            starter["template"] = {"id": starter["key"], "mode": "initial_file", "version": 1}
    steps = data["recipe"]["workflows"][0]["steps"]
    steps[0]["next"] = ["read-guide"]
    steps.insert(1, {"id": "read-guide", "type": "tool", "config": {"tool": "read_file", "arguments": {"knowledge_document_key": "guide"}}, "next": ["done"]})
    installed = await install_blueprint(db_session, entity_id=provider.entity_id, payload=data, mode=InstallMode.LIVE, user_id=provider.id)
    await db_session.commit()
    docs = list((await db_session.scalars(
        select(Document).join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(DocumentGroup.workspace_id == installed.workspace_id)
    )).all())
    by_name = {document.name: document for document in docs}
    by_name["generated-output.md"].source = "workflow"
    await db_session.commit()
    return parties, installed.workspace_id, by_name


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_empty", [False, True])
async def test_delivery_without_selection_never_copies_eligible_knowledge(
    client, db_session, monkeypatch, explicit_empty,
):
    parties, source, _docs = await _template_delivery_source(client, db_session, monkeypatch)
    ph, _provider, _rh, _recipient, cid, _old_source, receipt, _old_token = parties
    workflow = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == source))).one()
    workflow.steps = [{"id": "done", "type": "end", "config": {}, "next": []}]
    await db_session.commit()
    body = {"workspace_id": source, "contact_id": receipt["contact_id"]}
    if explicit_empty:
        body["knowledge_document_ids"] = []
    response = await client.post(f"/api/v1/clients/{cid}/deliveries", headers=ph, json=body)
    assert response.status_code == 201, response.text
    frozen = await db_session.get(ClientWorkspaceDelivery, response.json()["id"])
    assert all(pack["mode"] == "skeleton" and not pack["starter_documents"] for pack in frozen.payload["embedded"]["knowledge_packs"])
    assert "Private run evidence" not in json.dumps(frozen.payload)


@pytest.mark.asyncio
async def test_delivery_selected_starters_install_native_files_and_preserve_app(
    client, db_session, monkeypatch, tmp_path, isolated_test_redis,
):
    from packages.core.config import get_settings
    import packages.core.database as database
    from packages.core.ai.tools.file_tools import _read_file
    from packages.core.services import email_service
    from packages.core.services.entity_fs import write_entity_file_atomic

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    parties, source, docs = await _template_delivery_source(client, db_session, monkeypatch, initial_files=True)
    ph, provider, rh, recipient, cid, _old_source, receipt, _old_token = parties
    current_bodies = {
        "watchlist.csv": "creator_key,handle,status\nprivate-creator,@existing-customer,tracked\n",
        "guide.md": "# Research guide\nPrivate research collected during a source run.\n",
    }
    for name, current in current_bodies.items():
        document = docs[name]
        document.metadata_ = {
            **document.metadata_, "content_text": current,
            "artifact": {"storage_scope": "artifact", "workspace_id": source},
        }
        write_entity_file_atomic(provider.entity_id, document.fs_path, current.encode())
    await db_session.commit()
    source_metadata = {name: copy.deepcopy(docs[name].metadata_) for name in current_bodies}
    source_bytes = {name: Path(tmp_path, provider.entity_id, docs[name].fs_path).read_bytes() for name in current_bodies}
    mail = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    monkeypatch.setattr(email_service, "send_client_delivery_email", mail)
    monkeypatch.setenv("APP_URL", "https://manor.example.test")
    selected = [docs["watchlist.csv"].id, docs["guide.md"].id]
    response = await client.post(f"/api/v1/clients/{cid}/deliveries", headers=ph, json={
        "workspace_id": source, "contact_id": receipt["contact_id"], "phase": "trial",
        "knowledge_document_ids": [f" {selected[0]} ", selected[1], selected[0]],
    })
    assert response.status_code == 201, response.text
    invitation = response.json()
    assert invitation["phase"] == "trial" and invitation["email_delivery"]["status"] == "sent"
    mail.assert_awaited_once()
    frozen = await db_session.get(ClientWorkspaceDelivery, invitation["id"])
    starters = [doc for pack in frozen.payload["embedded"]["knowledge_packs"] for doc in pack["starter_documents"]]
    assert {doc["path"] for doc in starters} == {"research/watchlist.csv", "research/guide.md"}
    assert "Private run evidence" not in json.dumps(frozen.payload)
    assert "private-creator" not in json.dumps(frozen.payload)
    assert "Private research collected" not in json.dumps(frozen.payload)
    assert not any(doc.id in json.dumps(frozen.payload) for doc in docs.values())
    for name in current_bodies:
        await db_session.refresh(docs[name])
        assert docs[name].metadata_ == source_metadata[name]
        assert Path(tmp_path, provider.entity_id, docs[name].fs_path).read_bytes() == source_bytes[name]
    # Acceptance uses the reviewed snapshot, not the source's subsequent edits.
    docs["guide.md"].metadata_ = {**docs["guide.md"].metadata_, "content_text": "A later source edit."}
    write_entity_file_atomic(provider.entity_id, docs["guide.md"].fs_path, b"A later source edit.")
    await db_session.commit()
    source_metadata["guide.md"] = copy.deepcopy(docs["guide.md"].metadata_)
    source_bytes["guide.md"] = b"A later source edit."
    token = invitation["invite_path"].rsplit("/", 1)[-1]
    accepted = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert accepted.status_code == 200, accepted.text
    target = accepted.json()["target_workspace_id"]
    assert target != source
    ws = await db_session.get(Workspace, target)
    assert ws.entity_id == recipient.entity_id and ws.status == "paused"
    owners = list((await db_session.scalars(select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target))).all())
    # The trial provider's seat is derived from the grant; the customer still owns the Workspace.
    assert sorted((member.user_id, member.role, member.staff_id) for member in owners if member.collaboration_id is None) == [
        (recipient.id, "owner", None)
    ]
    workflow = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == target))).one()
    expected_by_step = {"read": "creator_key,handle,status\n", "read-guide": "# Research guide\nAdd your own creators and evidence.\n"}
    for step_id, expected in expected_by_step.items():
        step = next(step for step in workflow.steps if step["id"] == step_id)
        args = step["config"]["arguments"]
        assert args["document_id"] not in selected
        doc = await db_session.get(Document, args["document_id"])
        assert Path(tmp_path, recipient.entity_id, doc.fs_path).read_text() == expected
        read = json.loads(await _read_file(recipient.entity_id, workspace_id=target, **args))
        assert read["content"] == expected and read["document_id"] == doc.id
        assert doc.metadata_["blueprint_initial_body_md"] == expected
        customer_body = expected + "Customer-only operational data.\n"
        doc.metadata_ = {**doc.metadata_, "content_text": customer_body}
        write_entity_file_atomic(recipient.entity_id, doc.fs_path, customer_body.encode())
    await db_session.commit()
    roundtrip = await export_workspace(db_session, target, title=ws.name, context=ExportContext(knowledge_pack_mode="inline_text"))
    assert roundtrip["recipe"]["workflows"] == frozen.payload["recipe"]["workflows"]
    assert roundtrip["recipe"]["operating_model"]["app_view"] == frozen.payload["recipe"]["operating_model"]["app_view"]
    assert roundtrip["embedded"]["knowledge_packs"] == frozen.payload["embedded"]["knowledge_packs"]
    assert "Customer-only operational data" not in json.dumps(roundtrip)
    for name in current_bodies:
        await db_session.refresh(docs[name])
        assert docs[name].metadata_ == source_metadata[name]
        assert Path(tmp_path, provider.entity_id, docs[name].fs_path).read_bytes() == source_bytes[name]
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=ph)).status_code == 404
    assert provider.entity_id != recipient.entity_id


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["omitted", "empty", "missing_dependency", "private", "quarantined", "foreign_workspace", "foreign_entity", "oversized", "too_many", "blank"])
async def test_delivery_starter_selection_fails_before_receipt_or_email(
    client, db_session, monkeypatch, invalid,
):
    from packages.core.constants.blueprints import BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES, BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS
    from packages.core.services import email_service

    parties, source, docs = await _template_delivery_source(client, db_session, monkeypatch)
    ph, provider, _rh, recipient, cid, _old_source, receipt, _old_token = parties
    mail = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "is_email_delivery_enabled", lambda: True)
    monkeypatch.setattr(email_service, "send_client_delivery_email", mail)
    monkeypatch.setenv("APP_URL", "https://manor.example.test")
    selected = [docs["watchlist.csv"].id, docs["guide.md"].id]
    if invalid == "private":
        docs["watchlist.csv"].visibility = "private"
    elif invalid == "quarantined":
        docs["watchlist.csv"].quarantine_status = "quarantined"
    elif invalid == "foreign_entity":
        docs["watchlist.csv"].entity_id = recipient.entity_id
    elif invalid == "foreign_workspace":
        other = Workspace(entity_id=provider.entity_id, name="Different source")
        db_session.add(other)
        await db_session.flush()
        group = (await db_session.scalars(select(DocumentGroup).where(DocumentGroup.workspace_id == source))).one()
        group.workspace_id = other.id
    elif invalid == "oversized":
        docs["watchlist.csv"].metadata_ = {"content_text": "x" * (BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES + 1)}
    elif invalid == "empty":
        selected = []
    elif invalid == "missing_dependency":
        selected = selected[:1]
    elif invalid == "too_many":
        selected = selected * BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS
    elif invalid == "blank":
        selected = [" "]
    await db_session.commit()
    body = {"workspace_id": source, "contact_id": receipt["contact_id"]}
    if invalid != "omitted":
        body["knowledge_document_ids"] = selected
    response = await client.post(f"/api/v1/clients/{cid}/deliveries", headers=ph, json=body)
    assert response.status_code == 422, response.text
    assert (await db_session.scalar(select(func.count()).select_from(ClientWorkspaceDelivery))) == 1
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_secondary_company_owner_membership_requires_active_company_membership(client, db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.services.workspace_access import ensure_workspace_owner_membership

    _, user = await register(client, db_session, "secondary")
    entity = Entity(id=generate_ulid(), name="Secondary company")
    db_session.add(entity)
    await db_session.flush()
    member = UserMembership(user_id=user.id, entity_id=entity.id, role="owner", status="active")
    db_session.add(member)
    source = Workspace(entity_id=entity.id, name="Secondary workspace")
    db_session.add(source)
    await db_session.flush()
    owner = await ensure_workspace_owner_membership(
        db_session, entity_id=entity.id, workspace_id=source.id, user_id=user.id
    )
    assert owner is not None and owner.staff_id is None and owner.role == "owner"
    member.status = "inactive"
    second = Workspace(entity_id=entity.id, name="After revocation")
    db_session.add(second)
    await db_session.flush()
    assert (
        await ensure_workspace_owner_membership(
            db_session, entity_id=entity.id, workspace_id=second.id, user_id=user.id
        )
        is None
    )
    primary = (
        await db_session.execute(
            select(UserMembership).where(UserMembership.user_id == user.id, UserMembership.entity_id == user.entity_id)
        )
    ).scalar_one()
    primary.status = "inactive"
    assert (
        await ensure_workspace_owner_membership(
            db_session, entity_id=user.entity_id, workspace_id=source.id, user_id=user.id
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_client_delivery_migration_upgrades_legacy_membership_and_repeats(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from tests.migration_test_helpers import script_directory

    revision = script_directory().get_revision("20261001_02").module
    crm_revision = script_directory().get_revision("20261001_03").module

    def migrate(sync_session):
        connection = sync_session.connection()
        # Entire migration runs in a test-only schema inside the test transaction.
        connection.execute(text("CREATE SCHEMA client_delivery_migration_test"))
        connection.execute(text("SET LOCAL search_path TO client_delivery_migration_test"))
        connection.execute(text("CREATE TABLE workspace_staff (staff_id VARCHAR(26) NOT NULL)"))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            crm_revision.upgrade()
            assert inspect(connection).get_columns("workspace_staff")[0]["nullable"]
            for model in (ClientContact, ClientWorkspaceDelivery):
                columns = {column["name"]: column for column in inspect(connection).get_columns(model.__tablename__)}
                later = (
                    {"phase", "acceptance", "email_delivery", "content_release"}
                    if model is ClientWorkspaceDelivery else {"locale"}
                )
                assert set(columns) == set(model.__table__.columns.keys()) - later
                for expected in model.__table__.columns:
                    if expected.name in columns:
                        assert columns[expected.name]["nullable"] == expected.nullable
            connection.execute(text("INSERT INTO workspace_staff (staff_id) VALUES (NULL)"))
            crm_revision.downgrade()
            revision.downgrade()
            assert inspect(connection).get_columns("workspace_staff")[0]["nullable"]
            revision.upgrade()
            crm_revision.upgrade()
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA client_delivery_migration_test CASCADE"))

    await db_session.run_sync(migrate)


@pytest.mark.asyncio
async def test_simultaneous_acceptance_installs_one_customer_workspace(client, db_session):
    import asyncio

    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    results = await asyncio.gather(
        *[client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={}) for _ in range(3)]
    )
    assert all(response.status_code == 200 for response in results), [response.text for response in results]
    assert len({response.json()["target_workspace_id"] for response in results}) == 1
    assert (
        await db_session.execute(select(func.count()).select_from(Workspace).where(Workspace.entity_id == r.entity_id))
    ).scalar() == 1


@pytest.mark.asyncio
async def test_one_providers_concurrent_trial_acceptances_both_grant_access(client, db_session):
    # Catches: two customers accepting the same provider's trials at once
    # deadlocking, as each held SHARE on the provider's User row and upgraded
    # it to grant trial access.
    import asyncio

    from packages.core.models.workspace_collaboration import WorkspaceCollaboration

    ph, p, rh, r, cid, source, receipt, token = await prepared(client, db_session)
    other_headers, other = await register(client, db_session, "second-recipient")
    c = await client.post("/api/v1/clients", headers=ph, json={"name": "Second customer"})
    contact = await client.post(
        f"/api/v1/clients/{c.json()['id']}/contacts", headers=ph, json={"name": "Owner", "email": other.email}
    )
    ws = await client.post(
        "/api/v1/workspaces", headers=ph, json={"name": "Second source", "kind": "campaign", "primary_work": "Ads"}
    )
    second = await client.post(
        f"/api/v1/clients/{c.json()['id']}/deliveries",
        headers=ph,
        json={"workspace_id": ws.json()["id"], "contact_id": contact.json()["id"], "phase": "trial"},
    )
    assert second.status_code == 201, second.text
    other_token = second.json()["invite_path"].rsplit("/", 1)[-1]
    results = await asyncio.gather(
        client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={}),
        client.post(f"/api/v1/client-deliveries/invites/{other_token}/accept", headers=other_headers, json={}),
    )
    assert [response.status_code for response in results] == [200, 200], [response.text for response in results]
    grants = (await db_session.scalars(select(WorkspaceCollaboration))).all()
    assert sorted((g.role, g.status, g.user_id, g.entity_id) for g in grants) == sorted(
        [("provider_trial", "active", p.id, r.entity_id), ("provider_trial", "active", p.id, other.entity_id)]
    )


async def restrict_company_role(db, user, role):
    membership = (
        await db.execute(select(UserMembership).where(UserMembership.user_id == user.id))
    ).scalar_one()
    membership.role = role
    user.role = role
    staff = (await db.execute(select(Staff).where(Staff.user_id == user.id))).scalar_one_or_none()
    if staff is not None:
        staff_role = StaffRole(entity_id=user.entity_id, name="No administrative permissions", permissions=[])
        db.add(staff_role)
        await db.flush()
        staff.role_id = staff_role.id
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_all_active_users_can_manage_company_clients_without_admin_permissions(client, db_session, role):
    headers, user = await register(client, db_session, "ordinary")
    outsider_headers, _ = await register(client, db_session, "other-company")
    await restrict_company_role(db_session, user, role)
    created = await client.post("/api/v1/clients", headers=headers, json={"name": "Customer"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert (await client.get("/api/v1/clients", headers=headers)).json()["total"] == 1
    assert (await client.get(f"/api/v1/clients/{cid}", headers=headers)).status_code == 200
    assert (await client.put(f"/api/v1/clients/{cid}", headers=headers, json={"name": "Updated"})).status_code == 200
    contact = await client.post(
        f"/api/v1/clients/{cid}/contacts", headers=headers,
        json={"name": "Marketing owner", "email": "marketing@customer.test"},
    )
    assert contact.status_code == 201, contact.text
    assert (await client.get(f"/api/v1/clients/{cid}/contacts", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/clients/{cid}/deliveries", headers=headers)).json() == []
    assert (await client.get("/api/v1/clients", headers=outsider_headers)).json()["total"] == 0
    assert (await client.get(f"/api/v1/clients/{cid}", headers=outsider_headers)).status_code == 404
    assert (await client.put(f"/api/v1/clients/{cid}", headers=outsider_headers, json={"name": "Wrong company"})).status_code == 404
    assert (await client.get(f"/api/v1/clients/{cid}/contacts", headers=outsider_headers)).status_code == 404
    # Client access grants no employee/team administration authority.
    assert (await client.post("/api/v1/staff", headers=headers, json={"name": "Employee", "role": "admin"})).status_code == 403
    assert (await client.delete(f"/api/v1/clients/{cid}", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/clients", headers=headers)).json()["total"] == 0


@pytest.mark.asyncio
async def test_member_workspace_owner_can_prepare_rotate_and_deliver(client, db_session):
    ph, provider, rh, recipient, cid, source, receipt, token = await prepared(client, db_session)
    await restrict_company_role(db_session, provider, "member")
    created = await client.post(
        f"/api/v1/clients/{cid}/deliveries", headers=ph,
        json={"workspace_id": source, "contact_id": receipt["contact_id"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["can_manage"] is True
    deliveries = await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)
    assert all(row["can_manage"] for row in deliveries.json())
    rotated = await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/rotate", headers=ph)
    assert rotated.status_code == 200, rotated.text
    new_token = rotated.json()["invite_path"].rsplit("/", 1)[-1]
    assert (await client.get(f"/api/v1/client-deliveries/invites/{new_token}", headers=rh)).status_code == 200
    accepted = await client.post(f"/api/v1/client-deliveries/invites/{new_token}/accept", headers=rh, json={})
    assert accepted.status_code == 200, accepted.text
    ws = await db_session.get(Workspace, accepted.json()["target_workspace_id"])
    assert ws.entity_id == recipient.entity_id and ws.status == "paused"


@pytest.mark.asyncio
async def test_client_access_does_not_allow_managing_another_workspace_invitation(client, db_session):
    ph, provider, rh, recipient, cid, source, receipt, token = await prepared(client, db_session)
    await restrict_company_role(db_session, provider, "member")
    owner = (
        await db_session.execute(select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == source, WorkspaceStaff.user_id == provider.id,
        ))
    ).scalar_one()
    owner.status = "inactive"
    await db_session.commit()
    listing = await client.get(f"/api/v1/clients/{cid}/deliveries", headers=ph)
    assert listing.status_code == 200 and listing.json()[0]["can_manage"] is False
    for action in ("rotate", "revoke"):
        assert (await client.post(f"/api/v1/clients/{cid}/deliveries/{receipt['id']}/{action}", headers=ph)).status_code == 403
    created = await client.post(
        f"/api/v1/clients/{cid}/deliveries", headers=ph,
        json={"workspace_id": source, "contact_id": receipt["contact_id"]},
    )
    assert created.status_code == 403
    assert (await client.get(f"/api/v1/client-deliveries/invites/{token}", headers=rh)).status_code == 410


@pytest.mark.asyncio
async def test_inactive_company_membership_cannot_use_client_management(client, db_session):
    headers, user = await register(client, db_session, "inactive")
    membership = (
        await db_session.execute(select(UserMembership).where(UserMembership.user_id == user.id))
    ).scalar_one()
    membership.status = "inactive"
    await db_session.commit()
    assert (await client.get("/api/v1/clients", headers=headers)).status_code == 403
    assert (await client.post("/api/v1/clients", headers=headers, json={"name": "Blocked"})).status_code == 403
