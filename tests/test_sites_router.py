"""Sites router — host-routed public serving, tls-check, publish + domain management."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models import Site, SiteEvent
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Document
from packages.core.models.permission import (
    Capability,
    Classification,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.user import User
from packages.core.services.auth_service import create_access_token, hash_password
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceStaff

SITES_DOMAIN = "sites.test.local"


@pytest.fixture(autouse=True)
def sites_env(tmp_path, monkeypatch):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_SITES_DOMAIN", SITES_DOMAIN)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    return tmp_path


async def _auth(client: AsyncClient, username: str) -> tuple[dict, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["entity_id"]


def _write_bundle(fs_root, entity_id: str, folder: str = "s") -> None:
    root = fs_root / entity_id / folder
    (root / "css").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_bytes(
        b'<html><link href="css/style.css"><a href="about.html">about</a></html>'
    )
    (root / "about.html").write_bytes(b"<html>about</html>")
    (root / "css" / "style.css").write_bytes(b"body{}")


def _write_connected_bundle(fs_root, entity_id: str, folder: str = "workspace-site") -> None:
    root = fs_root / entity_id / folder
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        """<!doctype html>
<html><body>
  <a href="#contact" data-manor-event="contact_cta">Contact us</a>
  <section id="contact">
    <form data-manor-action="lead">
      <input name="email" type="email" required>
      <button type="submit">Request a demo</button>
      <span data-manor-status></span>
    </form>
    <form data-manor-action="subscription">
      <input name="email" type="email" required>
      <button type="submit">Subscribe</button>
      <span data-manor-status></span>
    </form>
  </section>
</body></html>""",
        encoding="utf-8",
    )


async def _login_headers(client: AsyncClient, username: str) -> dict:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "pass123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_entity_member(db_session, entity_id: str, username: str) -> tuple[User, dict]:
    member = User(
        entity_id=entity_id,
        email=f"{username}@test.com",
        display_name=username,
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(member)
    await db_session.flush()
    await db_session.commit()
    token = create_access_token(member.id, entity_id, member.role)
    return member, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def published(client, sites_env):
    """Register a user, write a bundle, publish it. Returns (headers, site dict)."""
    headers, entity_id = await _auth(client, "siteowner")
    _write_bundle(sites_env, entity_id)
    r = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={"path": "s", "name": "My site"},
    )
    assert r.status_code == 200, r.text
    return headers, r.json()


@pytest.mark.asyncio
async def test_publish_reports_url_and_revision(published):
    _, site = published
    assert site["revision"] == 1
    assert site["url"] == f"https://{site['slug']}.{SITES_DOMAIN}"
    assert site["excluded"] == []


@pytest.mark.asyncio
async def test_publish_rejects_confidential_and_restricted_documents(
    client, sites_env, db_session,
):
    headers, entity_id = await _auth(client, "siteclassification")
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    source = sites_env / entity_id / "classified.html"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<html>classified</html>", encoding="utf-8")
    document = Document(
        entity_id=entity_id,
        name="classified.html",
        fs_path="classified.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        classification=Classification.RESTRICTED,
    )
    db_session.add(document)
    await db_session.commit()

    restricted = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={"path": document.fs_path, "name": "Classified"},
    )
    assert restricted.status_code == 403, restricted.text
    assert "Restricted" in restricted.json()["detail"]

    document.classification = Classification.CONFIDENTIAL
    await db_session.commit()
    confidential = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={"path": document.fs_path, "name": "Classified"},
    )
    assert confidential.status_code == 403, confidential.text
    assert "approval flow" in confidential.json()["detail"]

    document.classification = Classification.INTERNAL
    await db_session.commit()
    allowed = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={"path": document.fs_path, "name": "Classified"},
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_publish_rejects_a_snapshot_changed_after_confirmation(
    client, sites_env, db_session,
):
    headers, entity_id = await _auth(client, "siteconfirmationhash")
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    source = sites_env / entity_id / "confirmed.html"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<html>before</html>", encoding="utf-8")
    document = Document(
        entity_id=entity_id,
        name="confirmed.html",
        fs_path="confirmed.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
    )
    db_session.add(document)
    await db_session.commit()

    preview = await client.get(
        "/api/v1/sites/for-path",
        headers=headers,
        params={"path": document.fs_path},
    )
    assert preview.status_code == 200, preview.text
    confirmed_hash = preview.json()["publish_snapshot_hash"]
    assert confirmed_hash

    source.write_text("<html>after</html>", encoding="utf-8")
    stale_publish = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={
            "path": document.fs_path,
            "name": "Confirmed",
            "expected_snapshot_hash": confirmed_hash,
        },
    )
    assert stale_publish.status_code == 409, stale_publish.text

    refreshed = await client.get(
        "/api/v1/sites/for-path",
        headers=headers,
        params={"path": document.fs_path},
    )
    refreshed_hash = refreshed.json()["publish_snapshot_hash"]
    assert refreshed_hash and refreshed_hash != confirmed_hash
    published = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={
            "path": document.fs_path,
            "name": "Confirmed",
            "expected_snapshot_hash": refreshed_hash,
        },
    )
    assert published.status_code == 200, published.text


@pytest.mark.asyncio
async def test_publish_requires_edit_and_external_share_access_to_the_source_documents(
    client, sites_env, db_session,
):
    owner_headers, entity_id = await _auth(client, "siteaclowner")
    _write_bundle(sites_env, entity_id, folder="protected-site")
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    entry = Document(
        entity_id=entity_id,
        name="index.html",
        fs_path="protected-site/index.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    db_session.add(entry)
    await db_session.flush()
    member, member_headers = await _create_entity_member(
        db_session,
        entity_id,
        "siteaclmember",
    )

    denied = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "protected-site", "name": "Protected site"},
    )
    assert denied.status_code == 403, denied.text

    entry_grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=entry.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.EDIT],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    )
    db_session.add(entry_grant)
    await db_session.commit()

    edit_only = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "protected-site", "name": "Protected site"},
    )
    assert edit_only.status_code == 403, edit_only.text
    assert "indexed" in edit_only.json()["detail"]

    about = Document(
        entity_id=entity_id,
        name="about.html",
        fs_path="protected-site/about.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    stylesheet = Document(
        entity_id=entity_id,
        name="style.css",
        fs_path="protected-site/css/style.css",
        file_type="css",
        mime_type="text/css",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    db_session.add_all([about, stylesheet])
    await db_session.flush()
    for document in (about, stylesheet):
        db_session.add(ResourceGrant(
            entity_id=entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=document.id,
            subject_type=SubjectType.USER,
            subject_id=member.id,
            capabilities=[Capability.EDIT, Capability.SHARE_EXTERNAL],
            granted_by=owner.id,
            granted_at=datetime.now(timezone.utc),
            status="active",
        ))
    await db_session.commit()

    external_share_denied = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "protected-site", "name": "Protected site"},
    )
    assert external_share_denied.status_code == 403, external_share_denied.text
    assert "External share access" in external_share_denied.json()["detail"]

    entry_grant.capabilities = [Capability.EDIT, Capability.SHARE_EXTERNAL]
    await db_session.commit()

    allowed = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "protected-site", "name": "Protected site"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["revision"] == 1
    durable_site = await db_session.get(Site, allowed.json()["id"])
    assert durable_site is not None
    assert durable_site.created_by_user_id == member.id

    editor_analytics = await client.get(
        f"/api/v1/sites/{allowed.json()['id']}/analytics",
        headers=member_headers,
    )
    assert editor_analytics.status_code == 200, editor_analytics.text

    taken_offline = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=member_headers,
        json={"status": "offline"},
    )
    assert taken_offline.status_code == 200, taken_offline.text
    entry_grant.capabilities = [Capability.EDIT]
    await db_session.commit()
    denied_reactivation = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=member_headers,
        json={"status": "active"},
    )
    assert denied_reactivation.status_code == 403, denied_reactivation.text
    assert "External share access" in denied_reactivation.json()["detail"]
    entry_grant.capabilities = [Capability.EDIT, Capability.SHARE_EXTERNAL]
    await db_session.commit()
    entry_path = sites_env / entity_id / "protected-site" / "index.html"
    entry_path.write_text(
        "<!doctype html><body>fresh authorized snapshot</body>",
        encoding="utf-8",
    )
    reactivated = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=member_headers,
        json={"status": "active"},
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["status"] == "active"
    assert reactivated.json()["revision"] == 2
    served = await client.get(
        "/api/v1/site-host/",
        headers={"host": f"{allowed.json()['slug']}.{SITES_DOMAIN}"},
    )
    assert served.status_code == 200, served.text
    assert "fresh authorized snapshot" in served.text

    # A source collaborator may publish their own site, but edit/share grants
    # on this source do not transfer management of the existing site.
    second_editor, second_editor_headers = await _create_entity_member(
        db_session,
        entity_id,
        "siteaclsecondeditor",
    )
    for document in (entry, about, stylesheet):
        db_session.add(ResourceGrant(
            entity_id=entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=document.id,
            subject_type=SubjectType.USER,
            subject_id=second_editor.id,
            capabilities=[Capability.EDIT, Capability.SHARE_EXTERNAL],
            granted_by=owner.id,
            granted_at=datetime.now(timezone.utc),
            status="active",
        ))
    await db_session.commit()
    denied_editor_management = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=second_editor_headers,
        json={"status": "offline"},
    )
    assert denied_editor_management.status_code == 403, denied_editor_management.text
    denied_editor_republish = await client.post(
        "/api/v1/sites/publish",
        headers=second_editor_headers,
        json={"path": "protected-site", "name": "Hijacked site"},
    )
    assert denied_editor_republish.status_code == 403, denied_editor_republish.text

    # Durable management survives the source entry projection being moved to
    # Trash.  Re-activation still separately requires a publishable source.
    entry.is_trashed = True
    entry.fs_path = None
    await db_session.commit()
    creator_analytics = await client.get(
        f"/api/v1/sites/{allowed.json()['id']}/analytics",
        headers=member_headers,
    )
    assert creator_analytics.status_code == 200, creator_analytics.text
    creator_offline = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=member_headers,
        json={"status": "offline"},
    )
    assert creator_offline.status_code == 200, creator_offline.text

    _, ungranted_headers = await _create_entity_member(
        db_session,
        entity_id,
        "siteaclungranted",
    )
    denied_connections = await client.get(
        f"/api/v1/sites/{allowed.json()['id']}/connections",
        headers=ungranted_headers,
    )
    assert denied_connections.status_code in {403, 404}, denied_connections.text
    denied_status = await client.post(
        f"/api/v1/sites/{allowed.json()['id']}/status",
        headers=ungranted_headers,
        json={"status": "offline"},
    )
    assert denied_status.status_code in {403, 404}, denied_status.text

    hidden_preview = await client.get(
        "/api/v1/sites/for-path",
        headers=ungranted_headers,
        params={"path": "protected-site"},
    )
    assert hidden_preview.status_code == 200, hidden_preview.text
    assert hidden_preview.json()["publishable"] is False
    assert hidden_preview.json()["site"] is None
    assert hidden_preview.json()["auto_connection_plan"] is None

    owner_view = await client.get(
        "/api/v1/sites/for-path",
        headers=owner_headers,
        params={"path": "protected-site"},
    )
    assert owner_view.status_code == 200
    assert owner_view.json()["site"]["id"] == allowed.json()["id"]


@pytest.mark.asyncio
async def test_publish_requires_edit_access_to_every_document_in_the_public_snapshot(
    client, sites_env, db_session,
):
    _, entity_id = await _auth(client, "sitesnapshotowner")
    _write_bundle(sites_env, entity_id, folder="snapshot-site")
    root = sites_env / entity_id / "snapshot-site"
    (root / "private-notes.md").write_text("not public", encoding="utf-8")
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    now = datetime.now(timezone.utc)
    entry = Document(
        entity_id=entity_id,
        name="index.html",
        fs_path="snapshot-site/index.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    protected_page = Document(
        entity_id=entity_id,
        name="about.html",
        fs_path="snapshot-site/about.html",
        file_type="html",
        mime_type="text/html",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="private",
    )
    stylesheet = Document(
        entity_id=entity_id,
        name="style.css",
        fs_path="snapshot-site/css/style.css",
        file_type="css",
        mime_type="text/css",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    excluded_notes = Document(
        entity_id=entity_id,
        name="private-notes.md",
        fs_path="snapshot-site/private-notes.md",
        file_type="markdown",
        mime_type="text/markdown",
        source="upload",
        created_by=owner.id,
        owner_id=owner.id,
        visibility="entity",
    )
    db_session.add_all([entry, protected_page, stylesheet, excluded_notes])
    await db_session.flush()
    member, member_headers = await _create_entity_member(
        db_session,
        entity_id,
        "sitesnapshotmember",
    )
    db_session.add(ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=entry.id,
        subject_type=SubjectType.USER,
        subject_id=member.id,
        capabilities=[Capability.EDIT, Capability.SHARE_EXTERNAL],
        granted_by=owner.id,
        granted_at=now,
        status="active",
    ))
    await db_session.commit()

    denied = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "snapshot-site", "name": "Snapshot site"},
    )
    assert denied.status_code == 403, denied.text

    for document in (protected_page, stylesheet):
        db_session.add(ResourceGrant(
            entity_id=entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=document.id,
            subject_type=SubjectType.USER,
            subject_id=member.id,
            capabilities=[Capability.EDIT, Capability.SHARE_EXTERNAL],
            granted_by=owner.id,
            granted_at=now,
            status="active",
        ))
    await db_session.commit()

    published = await client.post(
        "/api/v1/sites/publish",
        headers=member_headers,
        json={"path": "snapshot-site", "name": "Snapshot site"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["excluded"] == [
        {"path": "private-notes.md", "sensitive": False},
    ]


@pytest.mark.asyncio
async def test_publish_promotes_the_exact_snapshot_that_passed_access_check(
    client, sites_env, monkeypatch,
):
    from apps.api.routers import sites

    headers, entity_id = await _auth(client, "sitestagingowner")
    source = sites_env / entity_id / "race.html"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<!doctype html><body>authorized snapshot</body>", encoding="utf-8")
    original_check = sites._require_target_publish_access

    async def mutate_source_after_access_check(*args, **kwargs):
        await original_check(*args, **kwargs)
        source.write_text("<!doctype html><body>changed after access check</body>", encoding="utf-8")

    monkeypatch.setattr(sites, "_require_target_publish_access", mutate_source_after_access_check)

    published = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={"path": "race.html", "name": "Immutable snapshot"},
    )
    assert published.status_code == 200, published.text

    served = await client.get(
        "/api/v1/site-host/",
        headers={"host": f"{published.json()['slug']}.{SITES_DOMAIN}"},
    )
    assert served.status_code == 200, served.text
    assert "authorized snapshot" in served.text
    assert "changed after access check" not in served.text


@pytest.mark.asyncio
async def test_serve_by_slug_host(client, published):
    _, site = published
    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    r = await client.get("/api/v1/site-host/", headers=host)
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]

    r = await client.get("/api/v1/site-host/css/style.css", headers=host)
    assert r.status_code == 200
    assert "css" in r.headers["content-type"]


@pytest.mark.asyncio
async def test_published_html_injects_site_bridge_and_runtime(client, published):
    _, site = published
    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}

    page = await client.get("/api/v1/site-host/", headers=host)
    assert page.status_code == 200
    assert '<script defer src="/__manor/runtime.js"></script>' in page.text
    assert "script-src 'self' 'unsafe-inline' http: https:" in page.headers[
        "content-security-policy"
    ]
    assert "frame-src 'self' blob: http: https:" in page.headers[
        "content-security-policy"
    ]
    assert "default-src 'none'" not in page.headers["content-security-policy"]

    runtime = await client.get(
        "/api/v1/site-host/__manor/runtime.js", headers=host,
    )
    assert runtime.status_code == 200
    assert "application/javascript" in runtime.headers["content-type"]
    assert 'eventsUrl":"/__manor/events"' in runtime.text
    assert site["id"] not in runtime.text


@pytest.mark.asyncio
async def test_site_bridge_records_first_party_analytics_and_strips_secrets(
    client, published, db_session,
):
    headers, site = published
    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    event = await client.post(
        "/api/v1/site-host/__manor/events",
        headers=host,
        json={
            "event_type": "page_view",
            "session_id": "visitor-one",
            "path": "/pricing?utm_source=test&email=private@example.com#details",
            "properties": {
                "title": "Pricing",
                "utm_source": "test",
                "password": "must-not-be-stored",
                "api-token": "must-not-be-stored",
            },
        },
    )
    assert event.status_code == 202, event.text

    analytics = await client.get(
        f"/api/v1/sites/{site['id']}/analytics", headers=headers,
    )
    assert analytics.status_code == 200
    assert analytics.json()["page_views"] == 1
    assert analytics.json()["unique_sessions"] == 1
    assert analytics.json()["top_pages"] == [
        {"path": "/pricing?utm_source=test", "views": 1}
    ]

    stored = await db_session.scalar(
        select(SiteEvent).where(SiteEvent.site_id == site["id"])
    )
    assert stored is not None
    assert stored.properties == {"title": "Pricing", "utm_source": "test"}


@pytest.mark.asyncio
async def test_site_connections_route_webchat_and_lead_form_to_workspace(
    client, published, db_session, monkeypatch,
):
    headers, site = published
    # The authenticated entity id is already present on the published Site.
    stored_site = await db_session.get(Site, site["id"])
    assert stored_site is not None
    entity_id = stored_site.entity_id
    workspace = Workspace(entity_id=entity_id, name="Website Operations", status="active")
    db_session.add(workspace)
    await db_session.flush()
    channel_config = ChannelConfig(
        entity_id=entity_id,
        workspace_id=workspace.id,
        channel_type="webchat",
        provider="webchat",
        name="Website Support",
        config={"public_token": "public-site-chat-token"},
        credentials={},
        status="active",
    )
    db_session.add(channel_config)
    await db_session.flush()
    db_session.add(
        Channel(
            entity_id=entity_id,
            workspace_id=workspace.id,
            type="webchat",
            name="Website Support",
            config={"channel_config_id": channel_config.id},
            status="active",
        )
    )
    workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Capture website lead",
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "name": "Website form",
                "config": {},
                "next": ["capture"],
            },
            {
                "id": "capture",
                "type": "transform",
                "name": "Capture",
                "config": {"template": "{{email}}"},
                "next": [],
            },
        ],
        variables={},
        trigger_type="manual",
        status="active",
        is_active=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace.id,
        name="Website lead",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    db_session.add(binding)
    await db_session.commit()

    configured = await client.put(
        f"/api/v1/sites/{site['id']}/connections",
        headers=headers,
        json={
            "workspace_id": workspace.id,
            "customer_service_channel_config_id": channel_config.id,
            "subscription_workflow_binding_id": None,
            "lead_workflow_binding_id": binding.id,
            "analytics_enabled": True,
        },
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["workspace_id"] == workspace.id

    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    runtime = await client.get(
        "/api/v1/site-host/__manor/runtime.js", headers=host,
    )
    assert "public-site-chat-token/embed.js" in runtime.text
    assert workspace.id not in runtime.text

    queued: list[str] = []
    from packages.core.ai.workflow_runner import WorkflowRunner

    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id, delay_seconds=0: queued.append(run_id) or True),
    )
    submitted = await client.post(
        "/api/v1/site-host/__manor/events",
        headers=host,
        json={
            "event_type": "form_submit",
            "action": "lead",
            "session_id": "lead-one",
            "path": "/contact",
            "properties": {
                "name": "Ada",
                "email": "ada@example.com",
                "site_id": "visitor-cannot-override-this",
                "password": "not-stored",
            },
        },
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["workflow_started"] is True
    run = await db_session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.binding_id == binding.id)
        .order_by(WorkflowRun.created_at.desc())
    )
    assert run is not None
    assert queued == [run.id]
    assert run.workspace_id == workspace.id
    assert run.binding_id == binding.id
    assert run.trigger_source == "site_form"
    assert run.trigger_data["email"] == "ada@example.com"
    assert run.trigger_data["site_id"] == site["id"]
    assert "password" not in run.trigger_data


@pytest.mark.asyncio
async def test_workspace_document_editor_publishes_without_auto_connect_privileges(
    client, sites_env, db_session,
):
    _, entity_id = await _auth(client, "siteworkspaceowner")
    owner = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert owner is not None
    workspace = Workspace(
        entity_id=entity_id,
        name="Restricted Website Operations",
        status="active",
    )
    db_session.add(workspace)
    await db_session.flush()
    _write_connected_bundle(sites_env, entity_id, folder="restricted-workspace-site")
    entry = Document(
        entity_id=entity_id,
        name="index.html",
        fs_path="restricted-workspace-site/index.html",
        file_type="html",
        mime_type="text/html",
        source="ai_generated",
        metadata_={"origin": {"workspace_id": workspace.id}},
        created_by=owner.id,
        owner_id=owner.id,
        visibility="workspace",
    )
    db_session.add(entry)
    await db_session.flush()
    editor, editor_headers = await _create_entity_member(
        db_session,
        entity_id,
        "siteworkspaceeditor",
    )
    db_session.add(ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=entry.id,
        subject_type=SubjectType.USER,
        subject_id=editor.id,
        capabilities=[Capability.EDIT, Capability.SHARE_EXTERNAL],
        granted_by=owner.id,
        granted_at=datetime.now(timezone.utc),
        status="active",
    ))
    await db_session.commit()

    preview = await client.get(
        "/api/v1/sites/for-path",
        headers=editor_headers,
        params={"path": entry.fs_path},
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()["auto_connection_plan"]
    assert plan["eligible"] is True
    assert plan["can_auto_connect"] is False
    assert plan["workspace_id"] is None
    assert plan["workspace_name"] is None
    assert plan["actions"] == {
        "customer_service": "not_available",
        "lead_flow": "not_detected",
        "subscription_flow": "not_detected",
        "analytics": "enable",
    }
    assert plan["reason"] == "workspace_manage_required"

    forbidden_auto_connect = await client.post(
        "/api/v1/sites/publish",
        headers=editor_headers,
        json={
            "path": entry.fs_path,
            "name": "Restricted Workspace site",
            "auto_connect": True,
        },
    )
    assert forbidden_auto_connect.status_code == 403, forbidden_auto_connect.text

    published = await client.post(
        "/api/v1/sites/publish",
        headers=editor_headers,
        json={
            "path": entry.fs_path,
            "name": "Restricted Workspace site",
            "auto_connect": False,
        },
    )
    assert published.status_code == 200, published.text
    site = published.json()
    assert site["auto_connected"] is False
    assert site["workspace_id"] == workspace.id

    forbidden_connection = await client.put(
        f"/api/v1/sites/{site['id']}/connections",
        headers=editor_headers,
        json={
            "workspace_id": workspace.id,
            "customer_service_channel_config_id": None,
            "subscription_workflow_binding_id": None,
            "lead_workflow_binding_id": None,
            "analytics_enabled": True,
        },
    )
    assert forbidden_connection.status_code == 403, forbidden_connection.text

    forbidden_disconnect = await client.put(
        f"/api/v1/sites/{site['id']}/connections",
        headers=editor_headers,
        json={
            "workspace_id": None,
            "customer_service_channel_config_id": None,
            "subscription_workflow_binding_id": None,
            "lead_workflow_binding_id": None,
            "analytics_enabled": False,
        },
    )
    assert forbidden_disconnect.status_code == 403, forbidden_disconnect.text
    unchanged_connections = await client.get(
        f"/api/v1/sites/{site['id']}/connections",
        headers=editor_headers,
    )
    assert unchanged_connections.status_code == 200, unchanged_connections.text
    assert unchanged_connections.json()["workspace_id"] == workspace.id
    assert unchanged_connections.json()["analytics_enabled"] is True

    workspace_owner, workspace_owner_headers = await _create_entity_member(
        db_session,
        entity_id,
        "siteworkspaceoperator",
    )
    db_session.add(WorkspaceStaff(
        workspace_id=workspace.id,
        user_id=workspace_owner.id,
        role="owner",
        added_by=owner.id,
        status="active",
    ))
    await db_session.commit()
    owner_analytics = await client.get(
        f"/api/v1/sites/{site['id']}/analytics",
        headers=workspace_owner_headers,
    )
    assert owner_analytics.status_code == 200, owner_analytics.text


@pytest.mark.asyncio
async def test_workspace_site_publish_auto_connects_and_reuses_managed_resources(
    client, sites_env, db_session, monkeypatch,
):
    headers, entity_id = await _auth(client, "autositeowner")
    user = await db_session.scalar(select(User).where(User.entity_id == entity_id))
    assert user is not None
    workspace = Workspace(
        entity_id=entity_id,
        name="Website Operations",
        status="active",
    )
    db_session.add(workspace)
    await db_session.flush()
    workspace_id = workspace.id
    _write_connected_bundle(sites_env, entity_id)
    db_session.add(
        Document(
            entity_id=entity_id,
            name="index.html",
            fs_path="workspace-site/index.html",
            file_type="html",
            mime_type="text/html",
            source="ai_generated",
            metadata_={"origin": {"workspace_id": workspace_id}},
            created_by=user.id,
            owner_id=user.id,
            visibility="workspace",
        )
    )
    await db_session.commit()

    preview = await client.get(
        "/api/v1/sites/for-path",
        headers=headers,
        params={"path": "workspace-site/index.html"},
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()["auto_connection_plan"]
    assert plan["eligible"] is True
    assert plan["can_auto_connect"] is True
    assert plan["workspace_id"] == workspace_id
    assert plan["workspace_name"] == "Website Operations"
    assert plan["features"] == {
        "customer_service": True,
        "lead_forms": 1,
        "subscription_forms": 1,
        "tracked_interactions": 1,
        "analytics": True,
    }
    assert plan["actions"] == {
        "customer_service": "create",
        "lead_flow": "create",
        "subscription_flow": "create",
        "analytics": "enable",
    }

    published = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={
            "path": "workspace-site/index.html",
            "name": "Workspace website",
            "auto_connect": True,
        },
    )
    assert published.status_code == 200, published.text
    site = published.json()
    assert site["auto_connected"] is True
    assert site["workspace_id"] == workspace_id
    first_connections = site["connections"]
    assert first_connections["customer_service_channel_config_id"]
    assert first_connections["lead_workflow_binding_id"]
    assert first_connections["subscription_workflow_binding_id"]
    assert first_connections["analytics_enabled"] is True

    db_session.expire_all()
    agents = list((await db_session.scalars(
        select(Agent).where(
            Agent.entity_id == entity_id,
            Agent.workspace_id == workspace_id,
        )
    )).all())
    subscriptions = list((await db_session.scalars(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
        )
    )).all())
    channel_configs = list((await db_session.scalars(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == entity_id,
            ChannelConfig.workspace_id == workspace_id,
            ChannelConfig.channel_type == "webchat",
        )
    )).all())
    channels = list((await db_session.scalars(
        select(Channel).where(
            Channel.entity_id == entity_id,
            Channel.workspace_id == workspace_id,
            Channel.type == "webchat",
        )
    )).all())
    flows = list((await db_session.scalars(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.workspace_id == workspace_id,
        )
    )).all())
    bindings = list((await db_session.scalars(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
        )
    )).all())
    assert len(agents) == len(subscriptions) == len(channel_configs) == len(channels) == 1
    assert len(flows) == len(bindings) == 2
    assert agents[0].config["managed_by"] == "site_bridge"
    assert channel_configs[0].config["managed_by"] == "site_bridge"
    assert {binding.config["site_bridge_action"] for binding in bindings} == {
        "lead",
        "subscription",
    }

    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    runtime = await client.get("/api/v1/site-host/__manor/runtime.js", headers=host)
    assert runtime.status_code == 200
    assert f"{channel_configs[0].config['public_token']}/embed.js" in runtime.text

    embed = await client.get(
        f"/api/v1/public/chat/{channel_configs[0].config['public_token']}/embed.js"
    )
    assert embed.status_code == 200
    assert '"--manor-chat-accent", "--brand", "--primary"' in embed.text
    assert '"--manor-chat-surface", "--bg", "--background"' in embed.text
    assert "theme: theme.mode" in embed.text
    assert "font: theme.font" in embed.text

    queued: list[str] = []
    from packages.core.ai.workflow_runner import WorkflowRunner

    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id, delay_seconds=0: queued.append(run_id) or True),
    )
    submitted = await client.post(
        "/api/v1/site-host/__manor/events",
        headers=host,
        json={
            "event_type": "form_submit",
            "action": "lead",
            "session_id": "automatic-lead",
            "path": "/#contact",
            "properties": {"email": "lead@example.com"},
        },
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["workflow_started"] is True
    assert len(queued) == 1

    republished = await client.post(
        "/api/v1/sites/publish",
        headers=headers,
        json={
            "path": "workspace-site",
            "name": "Workspace website",
            "auto_connect": True,
        },
    )
    assert republished.status_code == 200, republished.text
    assert republished.json()["connections"] == first_connections
    assert republished.json()["revision"] == 2

    db_session.expire_all()
    assert len(list((await db_session.scalars(
        select(Agent).where(
            Agent.entity_id == entity_id,
            Agent.workspace_id == workspace_id,
        )
    )).all())) == 1
    assert len(list((await db_session.scalars(
        select(WorkflowBinding).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace_id,
        )
    )).all())) == 2


@pytest.mark.asyncio
async def test_spa_fallback_and_404(client, published):
    _, site = published
    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    r = await client.get("/api/v1/site-host/some/client/route", headers=host)
    assert r.status_code == 200  # extension-less -> SPA fallback to entry
    r = await client.get("/api/v1/site-host/missing.png", headers=host)
    assert r.status_code == 404  # has extension -> hard 404


@pytest.mark.asyncio
async def test_unknown_host_404(client):
    r = await client.get("/api/v1/site-host/", headers={"host": f"nope.{SITES_DOMAIN}"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_offline_site_404(client, published):
    headers, site = published
    r = await client.post(
        f"/api/v1/sites/{site['id']}/status", headers=headers, json={"status": "offline"}
    )
    assert r.status_code == 200
    r = await client.get(
        "/api/v1/site-host/", headers={"host": f"{site['slug']}.{SITES_DOMAIN}"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_path_traversal_blocked(client, published):
    _, site = published
    host = {"host": f"{site['slug']}.{SITES_DOMAIN}"}
    r = await client.get("/api/v1/site-host/..%2f..%2fsecret.txt", headers=host)
    assert r.status_code in (400, 404)


@pytest.mark.asyncio
async def test_tls_check(client, published):
    _, site = published
    ok = await client.get(f"/api/v1/sites/tls-check?domain={site['slug']}.{SITES_DOMAIN}")
    assert ok.status_code == 200
    base = await client.get(f"/api/v1/sites/tls-check?domain={SITES_DOMAIN}")
    assert base.status_code == 200  # CNAME target itself
    bad = await client.get("/api/v1/sites/tls-check?domain=evil.example.com")
    assert bad.status_code == 404


@pytest.mark.asyncio
async def test_for_path_and_unpublishable(client, published, sites_env):
    headers, site = published
    r = await client.get("/api/v1/sites/for-path", headers=headers, params={"path": "s"})
    assert r.status_code == 200
    body = r.json()
    assert body["publishable"] is True
    assert body["site"]["slug"] == site["slug"]

    r = await client.get(
        "/api/v1/sites/for-path", headers=headers, params={"path": "does-not-exist"}
    )
    assert r.status_code == 200
    assert r.json() == {
        "publishable": False,
        "target": None,
        "site": None,
        "hosting_configured": True,
        "auto_connection_plan": None,
        "publish_snapshot_hash": None,
    }


@pytest.mark.asyncio
async def test_publish_rejects_bad_target(client, sites_env):
    headers, entity_id = await _auth(client, "badowner")
    (sites_env / entity_id).mkdir(exist_ok=True)
    (sites_env / entity_id / "d.csv").write_bytes(b"a,b")
    r = await client.post(
        "/api/v1/sites/publish", headers=headers, json={"path": "d.csv", "name": "Bad"}
    )
    assert r.status_code == 422
    assert "index.html" in r.json()["detail"]


@pytest.mark.asyncio
async def test_other_entity_cannot_manage_site(client, published, sites_env):
    _, site = published
    other_headers, _ = await _auth(client, "intruder")
    r = await client.post(
        f"/api/v1/sites/{site['id']}/status", headers=other_headers, json={"status": "offline"}
    )
    assert r.status_code == 404


# ── Custom domains ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_domain_and_check(client, published, monkeypatch):
    from packages.core.services import site_publisher as sp

    async def fake_check(domain, expected_target):
        return True

    monkeypatch.setattr(sp, "dns_points_at_us", fake_check)

    headers, site = published
    r = await client.post(
        f"/api/v1/sites/{site['id']}/domain", headers=headers, json={"domain": "www.example.com"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["domain_status"] == "pending"

    r = await client.post(f"/api/v1/sites/{site['id']}/domain/check", headers=headers)
    assert r.status_code == 200
    assert r.json()["domain_status"] == "active"

    # active custom domain now serves the site
    r = await client.get("/api/v1/site-host/", headers={"host": "www.example.com"})
    assert r.status_code == 200

    r = await client.delete(f"/api/v1/sites/{site['id']}/domain", headers=headers)
    assert r.status_code == 200
    assert r.json()["custom_domain"] is None


@pytest.mark.asyncio
async def test_domain_conflict_409(client, published, sites_env):
    headers, site = published
    headers2, entity2 = await _auth(client, "owner2")
    _write_bundle(sites_env, entity2, folder="t")
    r2 = await client.post(
        "/api/v1/sites/publish", headers=headers2, json={"path": "t", "name": "Other"}
    )
    assert r2.status_code == 200
    site2 = r2.json()

    r = await client.post(
        f"/api/v1/sites/{site['id']}/domain", headers=headers, json={"domain": "www.example.com"}
    )
    assert r.status_code == 200
    r = await client.post(
        f"/api/v1/sites/{site2['id']}/domain", headers=headers2, json={"domain": "www.example.com"}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_platform_subdomain_rejected_as_custom(client, published):
    headers, site = published
    r = await client.post(
        f"/api/v1/sites/{site['id']}/domain",
        headers=headers,
        json={"domain": f"foo.{SITES_DOMAIN}"},
    )
    assert r.status_code == 422


# ── Hosting not configured (MANOR_SITES_DOMAIN unset) ────────────────────────


@pytest.fixture
def no_sites_domain(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_SITES_DOMAIN", "")


@pytest.mark.asyncio
async def test_publish_refused_when_hosting_unconfigured(client, sites_env, no_sites_domain):
    """A publish that cannot produce a reachable URL must fail loudly, not
    report success with an empty address."""
    headers, entity_id = await _auth(client, "unconfigured")
    _write_bundle(sites_env, entity_id)
    r = await client.post(
        "/api/v1/sites/publish", headers=headers, json={"path": "s", "name": "My site"}
    )
    assert r.status_code == 503
    assert "MANOR_SITES_DOMAIN" in r.json()["detail"]


@pytest.mark.asyncio
async def test_for_path_reports_hosting_unconfigured(client, sites_env, no_sites_domain):
    headers, entity_id = await _auth(client, "unconfigured2")
    _write_bundle(sites_env, entity_id)
    r = await client.get("/api/v1/sites/for-path", headers=headers, params={"path": "s"})
    assert r.status_code == 200
    body = r.json()
    assert body["publishable"] is True
    assert body["hosting_configured"] is False


@pytest.mark.asyncio
async def test_for_path_reports_hosting_configured(client, published):
    headers, site = published
    r = await client.get("/api/v1/sites/for-path", headers=headers, params={"path": "s"})
    assert r.json()["hosting_configured"] is True


@pytest.mark.asyncio
async def test_bind_domain_refused_when_hosting_unconfigured(
    client, published, monkeypatch
):
    """The CNAME target comes from the sites domain; without one the
    instructions would point nowhere."""
    from packages.core.config import get_settings

    headers, site = published
    monkeypatch.setattr(get_settings(), "MANOR_SITES_DOMAIN", "")
    r = await client.post(
        f"/api/v1/sites/{site['id']}/domain",
        headers=headers,
        json={"domain": "www.nowhere.com"},
    )
    assert r.status_code == 503
