"""publish_site agent tool."""
from __future__ import annotations

import json
import threading

import pytest

from packages.core.ai.tools import site_tools
from packages.core.models.document import Document
from packages.core.models.user import Entity, User, UserMembership


@pytest.fixture
def entity_fs(tmp_path, monkeypatch):
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_SITES_DOMAIN", "sites.test.local")
    eid = generate_ulid()
    (tmp_path / eid).mkdir()
    return tmp_path / eid, eid


@pytest.fixture
def tool_db(db_session, monkeypatch):
    """Point the tool's async_session at the test DB engine."""
    import packages.core.database as db_module
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_module, "async_session", factory)
    return factory


def test_get_tools_shape():
    tools = site_tools.get_tools()
    assert len(tools) == 1
    schema, handler = tools[0]
    assert schema["function"]["name"] == "publish_site"
    assert callable(handler)


async def _create_tool_user(tool_db, entity_id: str, *, role: str) -> User:
    async with tool_db() as session:
        if await session.get(Entity, entity_id) is None:
            session.add(Entity(id=entity_id, name=f"Site tool {role} entity"))
            await session.flush()
        user = User(
            entity_id=entity_id,
            email=f"site-tool-{role}-{entity_id}@test.com",
            display_name=f"site-tool-{role}",
            password_hash="not-used",
            role=role,
            status="active",
        )
        session.add(user)
        await session.flush()
        session.add(UserMembership(
            user_id=user.id,
            entity_id=entity_id,
            role=role,
            status="active",
            is_primary=True,
        ))
        await session.commit()
        await session.refresh(user)
        return user


@pytest.mark.asyncio
async def test_publish_site_handler(entity_fs, tool_db):
    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "s").mkdir()
    (root / "s" / "index.html").write_bytes(b"<html>hi</html>")
    (root / "s" / "notes.py").write_bytes(b"print()")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(
        await handler(entity_id=eid, user_id=owner.id, path="s", name="Demo")
    )
    assert out["published"] is True
    assert out["url"] == f"https://{out['slug']}.sites.test.local"
    assert out["name"] == "Demo"
    assert out["revision"] == 1
    assert out["excluded_files"] == ["notes.py"]


@pytest.mark.asyncio
async def test_publish_site_handler_prepares_snapshot_off_event_loop_thread(
    entity_fs,
    tool_db,
    monkeypatch,
):
    from packages.core.services import site_publisher as sp

    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "threaded").mkdir()
    (root / "threaded" / "index.html").write_bytes(b"<html>threaded</html>")
    event_loop_thread = threading.get_ident()
    prepare_threads: list[int] = []
    original_prepare = sp.prepare_publication

    def tracked_prepare(*args, **kwargs):
        prepare_threads.append(threading.get_ident())
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(sp, "prepare_publication", tracked_prepare)
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(
        entity_id=eid,
        user_id=owner.id,
        path="threaded",
        name="Threaded",
    ))

    assert out["published"] is True
    assert prepare_threads
    assert prepare_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_publish_site_handler_returns_filesystem_errors(
    entity_fs,
    tool_db,
    monkeypatch,
):
    from packages.core.services import site_publisher as sp

    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "broken").mkdir()
    (root / "broken" / "index.html").write_bytes(b"<html>broken</html>")

    def fail_prepare(*_args, **_kwargs):
        raise OSError("snapshot storage unavailable")

    monkeypatch.setattr(sp, "prepare_publication", fail_prepare)
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(
        entity_id=eid,
        user_id=owner.id,
        path="broken",
        name="Broken",
    ))

    assert out == {
        "published": False,
        "error": "snapshot storage unavailable",
    }


@pytest.mark.asyncio
async def test_publish_site_handler_does_not_persist_membership_as_current_entity(
    entity_fs,
    tool_db,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import UserMembership

    root, target_entity_id = entity_fs
    primary_entity_id = generate_ulid()
    async with tool_db() as session:
        session.add_all([
            Entity(id=primary_entity_id, name="Primary site-tool entity"),
            Entity(id=target_entity_id, name="Membership site-tool entity"),
        ])
        user = User(
            entity_id=primary_entity_id,
            email=f"site-tool-membership-{target_entity_id}@test.com",
            display_name="site-tool-membership",
            password_hash="not-used",
            role="member",
            status="active",
        )
        session.add(user)
        await session.flush()
        session.add(UserMembership(
            user_id=user.id,
            entity_id=target_entity_id,
            role="owner",
            status="active",
            is_primary=False,
        ))
        session.add(Document(
            entity_id=target_entity_id,
            name="index.html",
            fs_path="member-site/index.html",
            file_type="html",
            mime_type="text/html",
            source="upload",
            created_by=user.id,
            owner_id=user.id,
        ))
        await session.commit()
        user_id = user.id

    (root / "member-site").mkdir()
    (root / "member-site" / "index.html").write_bytes(b"<html>member</html>")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(
        entity_id=target_entity_id,
        user_id=user_id,
        path="member-site",
        name="Member site",
    ))
    assert out["published"] is True

    async with tool_db() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.entity_id == primary_entity_id
        assert user.role == "member"


@pytest.mark.asyncio
async def test_publish_site_handler_rejects_unindexed_member_snapshot_file(entity_fs, tool_db):
    root, eid = entity_fs
    member = await _create_tool_user(tool_db, eid, role="member")
    (root / "private").mkdir()
    (root / "private" / "index.html").write_bytes(b"<html>private</html>")
    (root / "private" / "private.json").write_bytes(b'{"secret": true}')
    async with tool_db() as session:
        session.add(Document(
            entity_id=eid,
            name="index.html",
            fs_path="private/index.html",
            file_type="html",
            mime_type="text/html",
            source="upload",
            created_by=member.id,
            owner_id=member.id,
        ))
        await session.commit()
    _, handler = site_tools.get_tools()[0]

    out = json.loads(
        await handler(
            entity_id=eid,
            user_id=member.id,
            path="private",
            name="Private",
        )
    )

    assert out["published"] is False
    assert "must be indexed" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_handler_unpublishable(entity_fs, tool_db):
    root, eid = entity_fs
    (root / "d.csv").write_bytes(b"a,b")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id="u1", path="d.csv", name="Bad"))
    assert out["published"] is False
    assert "index.html" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_handler_missing_path(entity_fs, tool_db):
    root, eid = entity_fs
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id="u1", name="NoPath"))
    assert out["published"] is False
    assert "path" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_handler_hosting_unconfigured(entity_fs, tool_db, monkeypatch):
    """No sites domain means no reachable URL — the tool must say so rather
    than return published=true with an empty url."""
    from packages.core.config import get_settings

    root, eid = entity_fs
    monkeypatch.setattr(get_settings(), "MANOR_SITES_DOMAIN", "")
    (root / "s").mkdir()
    (root / "s" / "index.html").write_bytes(b"<html>hi</html>")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id="u1", path="s", name="Demo"))
    assert out["published"] is False
    assert "MANOR_SITES_DOMAIN" in out["error"]


async def _create_project(tool_db, eid: str, root_path: str, *, profile="static_site", workspace_id=None):
    from packages.core.models.code_project import CodeProject

    async with tool_db() as session:
        project = CodeProject(
            entity_id=eid, name="Proj", root_path=root_path, profile=profile, workspace_id=workspace_id,
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project


@pytest.mark.asyncio
async def test_publish_site_by_project_id_keeps_one_url_across_republishes(entity_fs, tool_db):
    from packages.core.models.site import Site

    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "Code/shop").mkdir(parents=True)
    (root / "Code/shop/index.html").write_bytes(b"<html>v1</html>")
    project = await _create_project(tool_db, eid, "Code/shop", workspace_id="ws-source")
    _, handler = site_tools.get_tools()[0]

    first = json.loads(await handler(entity_id=eid, user_id=owner.id, project_id=project.id, name="Shop"))
    assert first["published"] is True, first
    assert first["project_id"] == project.id
    # The project's Workspace is only offered, never applied.
    assert first["workspace_id"] is None
    assert first["connectable_workspace_id"] == "ws-source"

    (root / "Code/shop/about.html").write_bytes(b"<html>about</html>")
    second = json.loads(await handler(entity_id=eid, user_id=owner.id, project_id=project.id, name="Shop"))
    assert second["slug"] == first["slug"]
    assert second["revision"] == 2

    async with tool_db() as session:
        site = await session.get(Site, first["site_id"])
        assert site.project_id == project.id
        assert site.workspace_id is None


@pytest.mark.asyncio
async def test_publish_site_by_path_links_an_existing_project(entity_fs, tool_db):
    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "Code/blog").mkdir(parents=True)
    (root / "Code/blog/index.html").write_bytes(b"<html>blog</html>")
    project = await _create_project(tool_db, eid, "Code/blog")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id=owner.id, path="Code/blog", name="Blog"))
    assert out["published"] is True
    assert out["project_id"] == project.id


@pytest.mark.asyncio
async def test_publish_site_refuses_projects_without_a_site_target(entity_fs, tool_db):
    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "Code/data").mkdir(parents=True)
    (root / "Code/data/index.html").write_bytes(b"<html></html>")
    project = await _create_project(tool_db, eid, "Code/data", profile="generic")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id=owner.id, project_id=project.id, name="Data"))
    assert out["published"] is False
    assert "cannot be published as a site" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_workspace_connection_requires_workspace_management(entity_fs, tool_db):
    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "Code/w").mkdir(parents=True)
    (root / "Code/w/index.html").write_bytes(b"<html></html>")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(
        entity_id=eid, user_id=owner.id, path="Code/w", name="W", workspace_id="ws-missing",
    ))
    assert out["published"] is False
    assert "Workspace owner" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_needs_exactly_one_target(entity_fs, tool_db):
    _, eid = entity_fs
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id="u1", path="a", project_id="p", name="X"))
    assert out["published"] is False
    assert "exactly one" in out["error"]


@pytest.mark.asyncio
async def test_publish_site_connects_a_workspace_only_when_asked(entity_fs, tool_db):
    from packages.core.models.site import Site
    from packages.core.models.workspace import Workspace

    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    async with tool_db() as session:
        workspace = Workspace(entity_id=eid, name="Web Ops", status="active")
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)
    (root / "Code/c").mkdir(parents=True)
    (root / "Code/c/index.html").write_bytes(b"<html></html>")
    _, handler = site_tools.get_tools()[0]

    connected = json.loads(await handler(
        entity_id=eid, user_id=owner.id, path="Code/c", name="C", workspace_id=workspace.id,
    ))
    assert connected["published"] is True, connected
    assert connected["workspace_id"] == workspace.id

    # Republishing without workspace_id leaves the connection alone.
    again = json.loads(await handler(entity_id=eid, user_id=owner.id, path="Code/c", name="C"))
    assert again["workspace_id"] == workspace.id
    async with tool_db() as session:
        assert (await session.get(Site, again["site_id"])).workspace_id == workspace.id


_LEAD_FORM = (
    b"<!doctype html><form data-manor-action='lead'><input name='email'>"
    b"<button type='submit'>Send</button></form>"
)


@pytest.mark.asyncio
async def test_agent_workspace_connection_includes_chat_and_form_flows(entity_fs, tool_db):
    from packages.core.models.site import Site
    from packages.core.models.workspace import Workspace

    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    async with tool_db() as session:
        workspace = Workspace(entity_id=eid, name="Web Ops", status="active")
        session.add(workspace)
        await session.commit()
        await session.refresh(workspace)
    (root / "Code/f").mkdir(parents=True)
    (root / "Code/f/index.html").write_bytes(_LEAD_FORM)
    _, handler = site_tools.get_tools()[0]

    first = json.loads(await handler(
        entity_id=eid, user_id=owner.id, path="Code/f", name="F", workspace_id=workspace.id,
    ))
    assert first["published"] is True, first
    assert first["connections"] == {
        "analytics": True,
        "customer_service_chat": True,
        "lead_form_flow": True,
        "subscription_form_flow": False,
    }

    # A form added later gets its flow on republish, without passing workspace_id again,
    # and the chat and lead flow are reused rather than duplicated.
    (root / "Code/f/news.html").write_bytes(
        b"<!doctype html><form data-manor-action='subscription'><input name='email'>"
        b"<button type='submit'>Join</button></form>"
    )
    second = json.loads(await handler(entity_id=eid, user_id=owner.id, path="Code/f", name="F"))
    assert second["connections"]["subscription_form_flow"] is True
    async with tool_db() as session:
        site = await session.get(Site, first["site_id"])
        assert site.workspace_id == workspace.id
        chat_id = site.connections["customer_service_channel_config_id"]
    third = json.loads(await handler(entity_id=eid, user_id=owner.id, path="Code/f", name="F"))
    async with tool_db() as session:
        site = await session.get(Site, third["site_id"])
        assert site.connections["customer_service_channel_config_id"] == chat_id


@pytest.mark.asyncio
async def test_unconnected_sites_get_analytics_but_no_chat(entity_fs, tool_db):
    root, eid = entity_fs
    owner = await _create_tool_user(tool_db, eid, role="owner")
    (root / "Code/u").mkdir(parents=True)
    (root / "Code/u/index.html").write_bytes(b"<!doctype html><h1>hi</h1>")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id=owner.id, path="Code/u", name="U"))
    assert out["connections"] == {
        "analytics": True,
        "customer_service_chat": False,
        "lead_form_flow": False,
        "subscription_form_flow": False,
    }
