"""Regression: the /fs/* router must honor Document.visibility.

Knowledge documents live as real files under the entity FS root. The raw
``/api/v1/fs/*`` surface used to authorize only on entity_id + hidden-path,
never on ``Document.visibility`` — so a same-entity member could read a
private document's bytes/content/name via /fs/read, /fs/list, /fs/search,
/fs/tree, /fs/info, and the raw serve URL, even though the Knowledge Base UI
(/documents/*) correctly hid it.

These tests set up FS, have an owner upload a PRIVATE doc, add a plain member,
and assert the member is denied on every /fs read surface while the owner is
allowed.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from packages.core.config import get_settings
from tests.test_document_permissions import _auth, _invite_and_accept_member


@pytest.fixture
def fs_enabled(tmp_path):
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    try:
        yield settings
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


SECRET = "机密:董事会薪酬明细 CEO-PACKAGE-XYZ"


async def _upload_private(client: AsyncClient, headers: dict, name: str) -> dict:
    resp = await client.post(
        "/api/v1/documents/upload?visibility=private",
        headers=headers,
        files={"file": (name, SECRET.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["visibility"] == "private", body
    assert body.get("fs_path"), "expected the private doc to have an fs_path"
    return body


@pytest.mark.asyncio
async def test_member_cannot_read_private_doc_via_fs(client: AsyncClient, fs_enabled):
    owner = await _auth(client, "fsvis_owner")
    doc = await _upload_private(client, owner, "薪酬机密.md")
    fs_path = doc["fs_path"]

    member, _ = await _invite_and_accept_member(
        client, owner, "fsvis.member@test.com"
    )

    # Control: owner CAN read it via /fs.
    r = await client.get(
        "/api/v1/fs/read", headers=owner, params={"path": fs_path}
    )
    assert r.status_code == 200, r.text
    assert SECRET.split()[0] in r.json().get("content", "")

    # Member is DENIED on every read surface.
    r = await client.get(
        "/api/v1/fs/read", headers=member, params={"path": fs_path}
    )
    assert r.status_code == 404, f"/fs/read leaked private doc: {r.status_code} {r.text}"

    r = await client.get(
        "/api/v1/fs/info", headers=member, params={"path": fs_path}
    )
    assert r.status_code == 404, f"/fs/info leaked private doc: {r.status_code}"

    r = await client.get("/api/v1/fs/list", headers=member, params={"path": "."})
    assert r.status_code == 200, r.text
    names = {i["name"] for i in r.json()["items"]}
    assert "薪酬机密.md" not in names, f"/fs/list leaked private doc: {names}"

    r = await client.get("/api/v1/fs/tree", headers=member)
    assert r.status_code == 200, r.text

    def _tree_paths(nodes):
        out = []
        for n in nodes:
            if n["type"] == "directory":
                out += _tree_paths(n.get("children", []))
            else:
                out.append(n["path"])
        return out

    assert fs_path not in _tree_paths(r.json()["tree"]), "/fs/tree leaked private doc"

    # Content search must not return snippets of the private doc.
    r = await client.get(
        "/api/v1/fs/search", headers=member, params={"query": "董事会薪酬"}
    )
    assert r.status_code == 200, r.text
    hit_paths = {res["path"] for res in r.json()["results"]}
    assert fs_path not in hit_paths, f"/fs/search leaked private content: {r.json()}"


@pytest.mark.asyncio
async def test_member_can_read_entity_doc_via_fs(client: AsyncClient, fs_enabled):
    """The gate must NOT over-block: an entity-visible doc stays readable."""
    owner = await _auth(client, "fsvis_owner2")
    resp = await client.post(
        "/api/v1/documents/upload?visibility=entity",
        headers=owner,
        files={"file": ("shared-handbook.md", b"team handbook body", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    fs_path = resp.json()["fs_path"]

    member, _ = await _invite_and_accept_member(
        client, owner, "fsvis.member2@test.com"
    )
    r = await client.get(
        "/api/v1/fs/read", headers=member, params={"path": fs_path}
    )
    assert r.status_code == 200, f"entity-visible doc wrongly blocked: {r.status_code}"
    assert "team handbook" in r.json().get("content", "")

    r = await client.get("/api/v1/fs/list", headers=member, params={"path": "."})
    names = {i["name"] for i in r.json()["items"]}
    assert "shared-handbook.md" in names
