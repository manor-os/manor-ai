"""publish_site agent tool."""
from __future__ import annotations

import json

import pytest

from packages.core.ai.tools import site_tools


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


@pytest.mark.asyncio
async def test_publish_site_handler(entity_fs, tool_db):
    root, eid = entity_fs
    (root / "s").mkdir()
    (root / "s" / "index.html").write_bytes(b"<html>hi</html>")
    (root / "s" / "notes.py").write_bytes(b"print()")
    _, handler = site_tools.get_tools()[0]
    out = json.loads(await handler(entity_id=eid, user_id="u1", path="s", name="Demo"))
    assert out["published"] is True
    assert out["url"] == f"https://{out['slug']}.sites.test.local"
    assert out["revision"] == 1
    assert out["excluded_files"] == ["notes.py"]


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
