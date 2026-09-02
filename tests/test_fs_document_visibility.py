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
from packages.core.services.file_access_tokens import create_file_access_token
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


@pytest.mark.asyncio
async def test_unprojected_file_is_not_a_raw_fs_acl_bypass(client: AsyncClient, fs_enabled):
    owner = await _auth(client, "fsvis_unprojected_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    entity_id = me["entity_id"]
    folder = await client.post(
        "/api/v1/documents/folders",
        headers=owner,
        json={"name": "Readable Folder"},
    )
    assert folder.status_code == 201, folder.text
    loose_file = (
        __import__("pathlib").Path(fs_enabled.MANOR_FS_ROOT)
        / entity_id
        / "unprojected-secret.md"
    )
    loose_file.parent.mkdir(parents=True, exist_ok=True)
    loose_file.write_text("not projected", encoding="utf-8")
    nested_loose_file = loose_file.parent / "Readable Folder" / "nested-secret.md"
    nested_loose_file.parent.mkdir(parents=True, exist_ok=True)
    nested_loose_file.write_text("also not projected", encoding="utf-8")
    member, _ = await _invite_and_accept_member(
        client, owner, "fsvis.unprojected@test.com"
    )

    # Entity admins can diagnose loose bytes, but ordinary members cannot use
    # an absent Document row as an authorization bypass.
    assert (
        await client.get("/api/v1/fs/read", headers=owner, params={"path": loose_file.name})
    ).status_code == 200
    denied = await client.get(
        "/api/v1/fs/read", headers=member, params={"path": loose_file.name}
    )
    assert denied.status_code == 404
    nested_denied = await client.get(
        "/api/v1/fs/read",
        headers=member,
        params={"path": "Readable Folder/nested-secret.md"},
    )
    assert nested_denied.status_code == 404
    listed = await client.get("/api/v1/fs/list", headers=member, params={"path": "."})
    assert loose_file.name not in {item["name"] for item in listed.json()["items"]}
    readable_folder = next(
        item for item in listed.json()["items"] if item["name"] == "Readable Folder"
    )
    assert readable_folder["item_count"] in {None, 0}
    nested_listed = await client.get(
        "/api/v1/fs/list", headers=member, params={"path": "Readable Folder"}
    )
    assert nested_listed.status_code == 200, nested_listed.text
    assert "nested-secret.md" not in {
        item["name"] for item in nested_listed.json()["items"]
    }
    folder_info = await client.get(
        "/api/v1/fs/info", headers=member, params={"path": "Readable Folder"}
    )
    assert folder_info.status_code == 200, folder_info.text
    assert folder_info.json()["item_count"] in {None, 0}


@pytest.mark.asyncio
async def test_actor_bound_signed_url_revalidates_current_document_acl(
    client: AsyncClient,
    fs_enabled,
):
    owner = await _auth(client, "fsvis_signed_owner")
    doc = await _upload_private(client, owner, "signed-private.md")
    member_headers, member = await _invite_and_accept_member(
        client, owner, "fsvis.signed.member@test.com"
    )
    owner_me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    owner_id = owner_me.get("user_id") or owner_me["id"]

    member_token = create_file_access_token(
        entity_id=owner_me["entity_id"],
        rel_path=doc["fs_path"],
        user_id=member.get("user_id") or member["id"],
    )
    owner_token = create_file_access_token(
        entity_id=owner_me["entity_id"],
        rel_path=doc["fs_path"],
        user_id=owner_id,
    )
    assert (await client.get(f"/api/v1/fs/public/{member_token}")).status_code == 404
    allowed = await client.get(f"/api/v1/fs/public/{owner_token}")
    assert allowed.status_code == 200
    assert SECRET.encode("utf-8") in allowed.content

    from sqlalchemy import update

    import packages.core.database as db_module
    from packages.core.models.user import User

    async with db_module.async_session() as db:
        await db.execute(
            update(User).where(User.id == owner_id).values(status="inactive")
        )
        await db.commit()

    assert (await client.get(f"/api/v1/fs/public/{owner_token}")).status_code == 404


@pytest.mark.asyncio
async def test_fs_acl_batches_large_tree_candidate_sets(monkeypatch):
    from types import SimpleNamespace

    import apps.api.routers.filesystem as filesystem_router

    calls = []

    async def fake_authorize(_db, **kwargs):
        files = list(kwargs["rel_paths"])
        directories = list(kwargs.get("directory_paths") or [])
        calls.append((files, directories))
        return SimpleNamespace(unreadable_paths=frozenset())

    monkeypatch.setattr(
        filesystem_router.ResourcePermissionGate,
        "authorize_filesystem_path_batch",
        fake_authorize,
    )
    files = [f"files/file-{index}.txt" for index in range(1001)]
    directories = [f"folders/folder-{index}" for index in range(801)]
    blocked = await filesystem_router._unreadable_doc_paths(
        object(),
        "ent_1",
        files,
        SimpleNamespace(id="user_1", entity_id="ent_1", role="member"),
        directory_paths=directories,
    )

    assert blocked == set()
    assert max(len(file_batch) for file_batch, _dirs in calls) <= 400
    assert max(len(dir_batch) for _files, dir_batch in calls) <= 400
    assert sum(len(file_batch) for file_batch, _dirs in calls) == len(files)
    assert sum(len(dir_batch) for _files, dir_batch in calls) == len(directories)
