"""Regression: agent file tools must honor Document.visibility.

Knowledge documents are real files under the entity FS root. The agent
``read_file`` / ``list_files`` / ``glob`` / ``grep`` tools read that root with
only path-traversal + hidden-path filtering, so an agent acting for a member
could read another member's PRIVATE document by path. These tools now gate
Knowledge-document paths through ``user_can_read_document``.
"""

from __future__ import annotations

import json
import os

import pytest

import packages.core.database as db_module
import packages.core.ai.tools.file_tools as file_tools_module
from packages.core.ai.tools.file_tools import (
    _delete_file,
    _patch_file,
    _glob_files,
    _grep_files,
    _list_files,
    _read_file,
)
from packages.core.config import get_settings
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.models.base import generate_ulid
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document
from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

SECRET = "机密:董事会薪酬明细 BOARD-COMP-SECRET"


@pytest.fixture
def fs_enabled(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    try:
        yield settings
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


async def _make_user(entity_id: str, name: str, role: str) -> str:
    async with db_module.async_session() as db:
        if await db.get(Entity, entity_id) is None:
            db.add(Entity(id=entity_id, name=f"{name} test entity"))
            await db.flush()
        u = User(
            entity_id=entity_id, email=f"{name}@test.com", display_name=name,
            password_hash=hash_password("pass123"), role=role, status="active",
        )
        db.add(u)
        await db.flush()
        db.add(UserMembership(
            user_id=u.id,
            entity_id=entity_id,
            role=role,
            status="active",
            is_primary=True,
        ))
        uid = u.id
        await db.commit()
    return uid


@pytest.mark.parametrize(
    ("handler", "kwargs"),
    (
        (_patch_file, {"operations": [{"op": "text.replace", "old_text": "protected", "new_text": "changed"}]}),
        (_delete_file, {"expected_sha256": "stale"}),
    ),
)
@pytest.mark.asyncio
async def test_mutation_acl_runs_before_source_fingerprint(
    fs_enabled,
    tmp_path,
    monkeypatch,
    handler,
    kwargs,
):
    entity_id = "ent_ftools_mutation_preflight"
    root = tmp_path / entity_id
    root.mkdir(parents=True)
    (root / "protected.md").write_text("protected", encoding="utf-8")

    async def deny_resource(**_kwargs):
        return json.dumps({
            "error": "file_permission_denied",
            "mode": "entity_file_acl_denied",
        })

    async def fingerprint_must_not_run(**_kwargs):
        raise AssertionError("source fingerprint ran before resource ACL")

    monkeypatch.setattr(
        file_tools_module,
        "runtime_guard_file_resource_access",
        deny_resource,
    )
    monkeypatch.setattr(
        file_tools_module,
        "_guard_expected_source_sha",
        fingerprint_must_not_run,
    )

    result = json.loads(await handler(
        entity_id,
        path="protected.md",
        user_id="user_1",
        **kwargs,
    ))
    assert result["mode"] == "entity_file_acl_denied"


@pytest.mark.asyncio
async def test_file_tools_hide_private_doc_from_member(fs_enabled, tmp_path, client):
    entity_id = "ent_ftools_vis"
    owner_id = await _make_user(entity_id, "ft_owner", "owner")
    member_id = await _make_user(entity_id, "ft_member", "member")

    # Write the two files to the entity FS root.
    root = os.path.join(str(tmp_path), entity_id)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "board-comp.md"), "w", encoding="utf-8") as f:
        f.write(f"{SECRET}\n{SECRET}")
    with open(os.path.join(root, "handbook.md"), "w", encoding="utf-8") as f:
        f.write("team handbook body")
    with open(os.path.join(root, "restricted.md"), "w", encoding="utf-8") as f:
        f.write("restricted agent content")
    with open(os.path.join(root, "quarantined.md"), "w", encoding="utf-8") as f:
        f.write("quarantined agent content")

    async with db_module.async_session() as db:
        await create_document(
            db, entity_id, name="board-comp.md", fs_path="board-comp.md",
            file_type="md", source="upload", visibility="private", owner_id=owner_id,
        )
        await create_document(
            db, entity_id, name="handbook.md", fs_path="handbook.md",
            file_type="md", source="upload", visibility="entity", owner_id=owner_id,
        )
        await create_document(
            db, entity_id, name="restricted.md", fs_path="restricted.md",
            file_type="md", source="upload", visibility="entity",
            classification="restricted", owner_id=owner_id,
        )
        quarantined = await create_document(
            db, entity_id, name="quarantined.md", fs_path="quarantined.md",
            file_type="md", source="upload", visibility="entity", owner_id=owner_id,
        )
        quarantined.quarantine_status = "quarantined"
        await db.commit()

    # list_files as MEMBER: private doc absent, shared present.
    listed = json.loads(await _list_files(entity_id, path="", user_id=member_id))
    paths = {e["path"] for e in listed["entries"]}
    assert "board-comp.md" not in paths, f"private doc leaked to member: {paths}"
    assert "handbook.md" in paths
    assert listed["total"] == 1

    # Pagination is derived from the authorized candidate set. A private file
    # before the first visible result must not create an empty page/cursor.
    first_page = json.loads(await _list_files(
        entity_id,
        path="",
        user_id=member_id,
        limit=1,
    ))
    assert [entry["path"] for entry in first_page["entries"]] == ["handbook.md"]
    assert first_page["has_more"] is False
    assert first_page["next_offset"] is None
    assert first_page["total"] == 1

    first_glob = json.loads(await _glob_files(
        entity_id,
        pattern="*.md",
        user_id=member_id,
        limit=1,
    ))
    assert first_glob["files"] == ["handbook.md"]
    assert first_glob["has_more"] is False
    assert first_glob["next_offset"] is None

    hidden_glob = json.loads(await _glob_files(
        entity_id,
        pattern="board-*.md",
        user_id=member_id,
        limit=1,
    ))
    assert hidden_glob["files"] == []
    assert hidden_glob["has_more"] is False
    assert hidden_glob["next_offset"] is None

    # list_files as OWNER: sees both.
    listed_owner = json.loads(await _list_files(entity_id, path="", user_id=owner_id))
    owner_paths = {e["path"] for e in listed_owner["entries"]}
    assert {"board-comp.md", "handbook.md"} <= owner_paths
    assert "restricted.md" not in owner_paths
    assert "quarantined.md" not in owner_paths

    # read_file the private doc as MEMBER: denied (reported as not found).
    r = json.loads(await _read_file(entity_id, path="board-comp.md", user_id=member_id))
    assert r.get("error"), f"read_file leaked private doc: {r}"
    assert "content" not in r

    missing = json.loads(await _read_file(
        entity_id,
        path="missing/board-comp.md",
        user_id=member_id,
    ))
    assert "candidates" not in missing
    missing_edit = json.loads(
        await _patch_file(
            entity_id,
            path="missing/board-comp.md",
            content="replacement",
            user_id=member_id,
            operations=[{"op": "text.replace"}],
        )
    )
    assert "candidates" not in missing_edit
    missing_delete = json.loads(await _delete_file(
        entity_id,
        path="missing/board-comp.md",
        user_id=member_id,
    ))
    assert "candidates" not in missing_delete

    # read_file as OWNER: returns content.
    r_owner = json.loads(await _read_file(entity_id, path="board-comp.md", user_id=owner_id))
    assert SECRET.split()[0] in r_owner.get("content", ""), r_owner
    for hard_blocked_path in ("restricted.md", "quarantined.md"):
        blocked_owner = json.loads(await _read_file(
            entity_id,
            path=hard_blocked_path,
            user_id=owner_id,
        ))
        assert blocked_owner.get("error")
        assert "content" not in blocked_owner

    # read_file the entity-visible doc as MEMBER: allowed (no over-block).
    r_shared = json.loads(await _read_file(entity_id, path="handbook.md", user_id=member_id))
    assert "team handbook" in r_shared.get("content", ""), r_shared

    # grep as MEMBER must not surface private-doc content.
    g = json.loads(await _grep_files(
        entity_id,
        pattern="董事会薪酬",
        max_matches=1,
        user_id=member_id,
    ))
    hit_files = {m["file"] for m in g.get("matches", [])}
    assert "board-comp.md" not in hit_files, f"grep leaked private content: {g}"
    assert g["has_more"] is False
    assert g["next_offset"] is None
    assert g["files_scanned"] == 1
    assert "board-comp.md" not in json.dumps(g, ensure_ascii=False)


@pytest.mark.asyncio
async def test_file_tools_hide_internal_runtime_paths_from_entity_owner(
    fs_enabled,
    tmp_path,
    monkeypatch,
):
    entity_id = "ent_ftools_internal_visibility"
    root = tmp_path / entity_id
    secret = root / ".ai" / "workspaces" / "ws_other" / "memory" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("OTHER_WORKSPACE_MEMORY", encoding="utf-8")

    async def allow_unprojected(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths", allow_unprojected)
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths_batched", allow_unprojected)

    read = json.loads(await _read_file(
        entity_id,
        path=".ai/workspaces/ws_other/memory/secret.md",
    ))
    listed = json.loads(await _list_files(
        entity_id,
        path="",
        recursive=True,
    ))
    globbed = json.loads(await _glob_files(
        entity_id,
        pattern=".ai/**/*.md",
    ))
    grepped = json.loads(await _grep_files(
        entity_id,
        pattern="OTHER_WORKSPACE_MEMORY",
    ))
    deleted = json.loads(await _delete_file(
        entity_id,
        path=".ai/workspaces/ws_other/memory/secret.md",
    ))

    assert read.get("error")
    assert all(not entry["path"].startswith(".ai/") for entry in listed["entries"])
    assert globbed["files"] == []
    assert grepped["matches"] == []
    assert deleted.get("error")
    assert secret.is_file()


@pytest.mark.asyncio
async def test_knowledge_document_list_and_glob_agree_for_root_pdf(
    fs_enabled,
    tmp_path,
):
    """A visible root-level Knowledge PDF must be discoverable by glob too."""
    entity_id = "ent_ftools_pdf_consistency"
    owner_id = await _make_user(entity_id, "ft_pdf_owner", "owner")
    root = tmp_path / entity_id
    (root / "references").mkdir(parents=True)
    (root / "root-reference.pdf").write_bytes(b"root pdf")
    (root / "references" / "nested-reference.pdf").write_bytes(b"nested pdf")

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="root-reference.pdf",
            fs_path="root-reference.pdf",
            file_type="pdf",
            mime_type="application/pdf",
            source="upload",
            visibility="entity",
            owner_id=owner_id,
        )
        await create_document(
            db,
            entity_id,
            name="nested-reference.pdf",
            fs_path="references/nested-reference.pdf",
            file_type="pdf",
            mime_type="application/pdf",
            source="upload",
            visibility="entity",
            owner_id=owner_id,
        )
        await db.commit()

        from packages.core.services.document_access import list_visible_documents

        documents, total = await list_visible_documents(
            db,
            entity_id,
            user_id=owner_id,
            actor_type="agent",
        )

    listed_paths = {str(document.fs_path) for document in documents}
    globbed = json.loads(await _glob_files(
        entity_id,
        pattern="**/*.pdf",
        user_id=owner_id,
    ))

    assert total == 2
    assert listed_paths == {
        "root-reference.pdf",
        "references/nested-reference.pdf",
    }
    assert set(globbed["files"]) == listed_paths


@pytest.mark.asyncio
async def test_workspace_agent_cannot_read_another_workspace_artifact(
    fs_enabled,
    tmp_path,
):
    entity_id = "ent_ftools_workspace_scope"
    owner_id = await _make_user(entity_id, "ft_scope_owner", "owner")
    workspace_a = Workspace(
        entity_id=entity_id,
        name="Workspace A",
        artifact_folder_id=generate_ulid(),
        status="active",
    )
    workspace_b = Workspace(
        entity_id=entity_id,
        name="Workspace B",
        artifact_folder_id=generate_ulid(),
        status="active",
    )
    async with db_module.async_session() as db:
        db.add_all([workspace_a, workspace_b])
        await db.flush()
        workspace_a_id = workspace_a.id
        workspace_b_id = workspace_b.id
        workspace_b_folder_id = str(workspace_b.artifact_folder_id)
        other_path = (
            f"{workspace_artifact_storage_base(workspace_b_folder_id)}"
            "/tasks/task_b/documents/other-workspace-secret.md"
        )
        await create_document(
            db,
            entity_id,
            name="other-workspace-secret.md",
            fs_path=other_path,
            file_type="md",
            source="agent",
            visibility="entity",
            owner_id=owner_id,
            metadata={"origin": {"workspace_id": workspace_b_id}},
        )
        generic_other_path = "shared/workspace-b-generic-secret.md"
        await create_document(
            db,
            entity_id,
            name="workspace-b-generic-secret.md",
            fs_path=generic_other_path,
            file_type="md",
            source="upload",
            visibility="entity",
            owner_id=owner_id,
            metadata={"origin": {"workspace_id": workspace_b_id}},
        )
        await db.commit()

    full_path = tmp_path / entity_id / other_path
    full_path.parent.mkdir(parents=True)
    full_path.write_text("workspace B only", encoding="utf-8")
    generic_full_path = tmp_path / entity_id / generic_other_path
    generic_full_path.parent.mkdir(parents=True)
    generic_full_path.write_text("workspace B generic only", encoding="utf-8")

    denied = json.loads(await _read_file(
        entity_id,
        path=other_path,
        user_id=owner_id,
        workspace_id=workspace_a_id,
    ))
    generic_denied = json.loads(await _read_file(
        entity_id,
        path=generic_other_path,
        user_id=owner_id,
        workspace_id=workspace_a_id,
    ))
    basename_miss = json.loads(await _read_file(
        entity_id,
        path="other-workspace-secret.md",
        user_id=owner_id,
        workspace_id=workspace_a_id,
    ))
    globbed = json.loads(await _glob_files(
        entity_id,
        pattern="**/*secret.md",
        user_id=owner_id,
        workspace_id=workspace_a_id,
    ))

    assert denied.get("error")
    assert "content" not in denied
    assert generic_denied.get("error")
    assert "content" not in generic_denied
    assert "candidates" not in basename_miss
    assert other_path not in globbed["files"]


@pytest.mark.asyncio
async def test_recursive_list_and_glob_stop_after_one_authorized_page(
    fs_enabled,
    tmp_path,
    monkeypatch,
):
    entity_id = "ent_ftools_bounded_page"
    (tmp_path / entity_id).mkdir(parents=True)
    consumed = {"list": 0, "glob": 0}

    def bounded_list_scan(*_args, **_kwargs):
        for index in range(file_tools_module.FILE_SCAN_ACL_BATCH_SIZE + 1):
            consumed["list"] += 1
            if consumed["list"] > file_tools_module.FILE_SCAN_ACL_BATCH_SIZE:
                raise AssertionError("list_files consumed a second filesystem batch")
            yield {"path": f"file-{index:04d}.txt", "type": "file", "size": 1}

    def bounded_glob_scan(*_args, **_kwargs):
        for index in range(file_tools_module.FILE_SCAN_ACL_BATCH_SIZE + 1):
            consumed["glob"] += 1
            if consumed["glob"] > file_tools_module.FILE_SCAN_ACL_BATCH_SIZE:
                raise AssertionError("glob_files consumed a second filesystem batch")
            yield f"file-{index:04d}.txt"

    async def allow_batch(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(file_tools_module, "_scan_list_files", bounded_list_scan)
    monkeypatch.setattr(file_tools_module, "_scan_glob_files", bounded_glob_scan)
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths_batched", allow_batch)

    listed = json.loads(await _list_files(
        entity_id,
        path="",
        recursive=True,
        limit=1,
        user_id="user_1",
    ))
    globbed = json.loads(await _glob_files(
        entity_id,
        pattern="*.txt",
        limit=1,
        user_id="user_1",
    ))

    assert [entry["path"] for entry in listed["entries"]] == ["file-0000.txt"]
    assert listed["has_more"] is True
    assert globbed["files"] == ["file-0000.txt"]
    assert globbed["has_more"] is True
    assert consumed == {
        "list": file_tools_module.FILE_SCAN_ACL_BATCH_SIZE,
        "glob": file_tools_module.FILE_SCAN_ACL_BATCH_SIZE,
    }


def test_real_list_and_glob_scanners_do_not_materialize_directory_names(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "streaming-scanner"
    root.mkdir()
    for index in range(300):
        (root / f"file-{index:04d}.txt").write_text("x", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("scanner materialized the directory")

    monkeypatch.setattr(file_tools_module.os, "listdir", forbidden)
    monkeypatch.setattr(file_tools_module.os, "walk", forbidden)
    listed = file_tools_module._scan_list_files(str(root), str(root), False)
    globbed = file_tools_module._scan_glob_files(str(root), "*.txt")
    try:
        assert len([next(listed) for _ in range(3)]) == 3
        assert len([next(globbed) for _ in range(3)]) == 3
    finally:
        listed.close()
        globbed.close()


def test_globstar_matches_zero_or_more_directories(tmp_path):
    """``**/`` follows standard glob semantics, including the root level.

    Knowledge uploads without an explicit folder are stored directly under
    the entity root. A model's conventional ``**/*.pdf`` query must find
    those files as well as PDFs below Knowledge folders.
    """
    root = tmp_path / "globstar"
    root.mkdir()
    (root / "root.pdf").write_text("root", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "deep.pdf").write_text("nested", encoding="utf-8")
    (root / "nested" / "deeper").mkdir()
    (root / "nested" / "deeper" / "deepest.pdf").write_text(
        "deepest", encoding="utf-8",
    )

    assert list(file_tools_module._scan_glob_files(str(root), "**/*.pdf")) == [
        "root.pdf",
        "nested/deep.pdf",
        "nested/deeper/deepest.pdf",
    ]
    assert list(file_tools_module._scan_glob_files(str(root), "**/*/*.pdf")) == [
        "nested/deep.pdf",
        "nested/deeper/deepest.pdf",
    ]
