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
from packages.core.models.document import Document
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.models.base import generate_ulid
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_folder,
    workspace_artifact_storage_base,
)

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


@pytest.mark.asyncio
async def test_empty_workspace_listing_never_falls_back_to_entity_root(fs_enabled, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    entity_id = "empty_workspace_listing"
    root = tmp_path / entity_id
    root.mkdir()
    (root / "outside.md").write_text("Outside this Workspace", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        AsyncMock(return_value="Workspaces/_by_id/EMPTY"),
    )
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths", AsyncMock(return_value=[]))
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths_batched", AsyncMock(return_value=[]))

    result = json.loads(await _list_files(entity_id, path="", workspace_id="workspace1"))

    assert result == {"error": "Directory not found: Workspaces/_by_id/EMPTY"}
    assert not (root / "Workspaces").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_has_folder", [False, True])
async def test_execution_archive_listing_finds_workspace_home_folders(
    fs_enabled, tmp_path, monkeypatch, archive_has_folder,
):
    """list_files must find what generate_file(storage_scope='workspace') wrote.

    Inside a Workflow run the resolved base is the run archive; read_file
    already falls back to the Workspace home, and listing the same logical
    folder must agree with it rather than report "Directory not found".
    """
    from unittest.mock import AsyncMock

    entity_id = "archive_home_listing"
    home = "Workspaces/_by_id/HOME"
    archive = f"{home}/workflow/run1"
    root = tmp_path / entity_id
    (root / home / "Videos" / "kinetic-quote-zh").mkdir(parents=True)
    (root / home / "Videos" / "kinetic-quote-zh" / "CANVAS.md").write_text("# Canvas", encoding="utf-8")
    (root / "Workspaces/_by_id/OTHER/Videos").mkdir(parents=True)
    (root / "Workspaces/_by_id/OTHER/Videos/private.md").write_text("other", encoding="utf-8")
    if archive_has_folder:
        (root / archive / "documents" / "Videos").mkdir(parents=True)
        (root / archive / "documents" / "Videos" / "run-only.md").write_text("run", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        AsyncMock(return_value=archive),
    )
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths", AsyncMock(return_value=[]))
    monkeypatch.setattr(file_tools_module, "_blocked_doc_paths_batched", AsyncMock(return_value=[]))

    listed = await _list_files(entity_id, path="Videos", recursive=True, workspace_id="workspace1")

    if archive_has_folder:
        # The run's own folder keeps precedence, exactly like read_file.
        assert f"{archive}/documents/Videos/run-only.md" in listed
        assert "CANVAS.md" not in listed
    else:
        assert f"{home}/Videos/kinetic-quote-zh/CANVAS.md" in listed
    assert "OTHER" not in listed and "private.md" not in listed

    missing = json.loads(await _list_files(entity_id, path="Nope", workspace_id="workspace1"))
    assert missing == {"error": f"Directory not found: {archive}/documents/Nope"}


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
        handbook = await create_document(
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
    # A path read names its Document, so the caller can link it; a denied read names nothing.
    assert r_shared["document_id"] == handbook.id
    assert "document_id" not in r

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
    generic_allowed = json.loads(await _read_file(
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
    assert generic_allowed["content"] == "workspace B generic only"
    assert "candidates" not in basename_miss
    assert other_path not in globbed["files"]


@pytest.mark.asyncio
async def test_userless_workspace_task_reads_its_bound_human_input_document(
    fs_enabled, tmp_path,
):
    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)
    (root / "asset-provenance.txt").write_text(
        "TEST_FIXTURE / NOT_FOR_DISTRIBUTION", encoding="utf-8",
    )

    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release entity"))
        workspace = Workspace(entity_id=entity_id, name="Release", status="active")
        db.add(workspace)
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        await create_document(
            db, entity_id, name="asset-provenance.txt",
            fs_path="asset-provenance.txt", folder_id=folder.id,
            source="upload", visibility="workspace",
            metadata={"origin": {
                "workspace_id": workspace.id,
                "tool_name": "hitl_upload",
            }},
        )
        workspace_id = workspace.id
        await db.commit()

    read = json.loads(await _read_file(
        entity_id, path="asset-provenance.txt", workspace_id=workspace_id,
    ))
    assert read["content"] == "TEST_FIXTURE / NOT_FOR_DISTRIBUTION"


@pytest.mark.asyncio
async def test_read_file_document_id_disambiguates_same_named_workspace_file(
    fs_enabled, tmp_path,
):
    assert "document_id" in file_tools_module.READ_FILE_SCHEMA["function"]["parameters"]["properties"]
    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)
    (root / "asset-provenance.txt").write_text("release A", encoding="utf-8")

    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release Workspace entity"))
        workspace = Workspace(entity_id=entity_id, name="Release Workspace", status="active")
        db.add(workspace)
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        root_document = await create_document(
            db, entity_id, name="asset-provenance.txt",
            fs_path="asset-provenance.txt", folder_id=folder.id,
            source="upload", visibility="workspace",
            metadata={"origin": {
                "workspace_id": workspace.id, "tool_name": "hitl_upload",
            }},
        )
        scoped_path = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/asset-provenance.txt"
        await create_document(
            db, entity_id, name="asset-provenance.txt",
            fs_path=scoped_path, folder_id=folder.id,
            source="ai_generated", visibility="workspace",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        workspace_id, document_id = workspace.id, root_document.id
        await db.commit()

    scoped_file = root / scoped_path
    scoped_file.parent.mkdir(parents=True)
    scoped_file.write_text("release B", encoding="utf-8")
    result = json.loads(await _read_file(
        entity_id, path="asset-provenance.txt", document_id=document_id,
        workspace_id=workspace_id,
    ))
    assert result["content"] == "release A"
    assert result["resolved_path"] == "asset-provenance.txt"
    mismatched = json.loads(await _read_file(
        entity_id, path=scoped_path, document_id=document_id,
        workspace_id=workspace_id,
    ))
    # The path is another readable file: an explicit conflict that names the
    # document's real fs_path, never a silent read of either file.
    assert mismatched["error"] == "identity_conflict"
    assert mismatched["document_id"] == document_id
    assert mismatched["fs_path"] == "asset-provenance.txt"
    assert "content" not in mismatched


@pytest.mark.asyncio
async def test_read_file_by_document_id_tolerates_a_display_path(fs_enabled, tmp_path):
    """FILES.md shows a display path first; agents pass it with the right id."""

    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)

    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Video Studio Eval entity"))
        workspace = Workspace(entity_id=entity_id, name="Video Studio Eval R1", status="active")
        db.add(workspace)
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        storage = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/launch-taglines.md"
        document = await create_document(
            db, entity_id, name="launch-taglines.md", fs_path=storage,
            folder_id=folder.id, visibility="workspace",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        private = await create_document(
            db, entity_id, name="private.md",
            fs_path=f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/private.md",
            folder_id=folder.id, visibility="private",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        workspace_id, document_id, private_id = workspace.id, document.id, private.id
        await db.commit()

    (root / storage).parent.mkdir(parents=True)
    (root / storage).write_text("Ride the night ferry.", encoding="utf-8")
    (root / storage).parent.joinpath("private.md").write_text("private note", encoding="utf-8")
    display = "Workspaces/Video Studio Eval R1/launch-taglines.md"

    read = json.loads(await _read_file(
        entity_id, document_id=document_id, path=display, workspace_id=workspace_id,
    ))
    assert read["content"] == "Ride the night ferry."
    assert read["document_id"] == document_id
    # The model learns the real storage path for later calls.
    assert read["path"] == storage and read["resolved_path"] == storage
    assert read["requested_path"] == display
    # The document's own access check still applies, and a refusal never
    # echoes the storage path of a document the caller cannot read.
    private_storage = f"{storage.rsplit('/', 1)[0]}/private.md"
    for path in ("Workspaces/Video Studio Eval R1/private.md", None):
        raw = await _read_file(
            entity_id, document_id=private_id, workspace_id=workspace_id,
            **({"path": path} if path else {}),
        )
        denied = json.loads(raw)
        assert denied.get("error") and "content" not in denied
        assert private_storage not in raw and "_by_id" not in raw
    # A document whose bytes are gone: not found, by the caller's own input.
    (root / storage).unlink()
    raw = await _read_file(entity_id, document_id=document_id, path=display, workspace_id=workspace_id)
    assert json.loads(raw).get("error") and storage not in raw and "_by_id" not in raw


@pytest.mark.asyncio
async def test_document_id_read_with_a_chat_upload_path_is_no_existence_oracle(fs_enabled, tmp_path, monkeypatch):
    from packages.core.services import ai_file_permissions

    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release entity"))
        workspace = Workspace(entity_id=entity_id, name="Release", status="active")
        db.add(workspace)
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        note = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/note.md"
        document = await create_document(
            db, entity_id, name="note.md", fs_path=note, folder_id=folder.id,
            source="upload", visibility="workspace",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        workspace_id, document_id = workspace.id, document.id
        await db.commit()
    (root / note).parent.mkdir(parents=True)
    (root / note).write_text("the note", encoding="utf-8")
    upload_path = "uploads/chat/0123456789abcdef.pdf"
    assert ai_file_permissions.is_chat_upload_path(upload_path)
    (root / upload_path).parent.mkdir(parents=True)
    (root / upload_path).write_bytes(b"%PDF secret")
    guarded = []

    async def deny(**kwargs):
        guarded.append(kwargs.get("paths"))
        return "blocked"

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", deny)
    raw = await _read_file(entity_id, document_id=document_id, path=upload_path, workspace_id=workspace_id)
    # The attachment someone else uploaded is never confirmed to exist: the
    # id decides and the read returns the document.
    assert json.loads(raw).get("content") == "the note", raw
    assert [upload_path] in guarded


@pytest.mark.asyncio
async def test_read_file_resolves_canonical_workspace_document_by_id_without_storage_path(
    fs_enabled, tmp_path,
):
    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)

    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release A entity"))
        workspace = Workspace(entity_id=entity_id, name="Release A", status="active")
        foreign = Workspace(entity_id=entity_id, name="Release B", status="active")
        db.add_all([workspace, foreign])
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        await ensure_workspace_artifact_folder(db, foreign)
        path = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/releases/night-ferry/release-state.md"
        document = await create_document(
            db, entity_id, name="release-state.md", fs_path=path,
            folder_id=folder.id, visibility="workspace",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        private = await create_document(
            db, entity_id, name="private.md", fs_path=f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/private.md",
            folder_id=folder.id, visibility="private",
            metadata={"origin": {"workspace_id": workspace.id}},
        )
        workspace_id, foreign_id, document_id, private_id = (
            workspace.id, foreign.id, document.id, private.id,
        )
        await db.commit()

    file = root / path
    file.parent.mkdir(parents=True)
    file.write_text("release_id: night-ferry\nstate_revision: 1", encoding="utf-8")
    (file.parent.parent.parent / "private.md").write_text("private release note", encoding="utf-8")

    schema = file_tools_module.READ_FILE_SCHEMA["function"]["parameters"]
    assert "path" not in schema["required"]
    read = json.loads(await _read_file(
        entity_id, document_id=document_id, workspace_id=workspace_id,
    ))
    assert read["content"] == "release_id: night-ferry\nstate_revision: 1"
    assert read["resolved_path"] == path
    foreign_read = json.loads(await _read_file(
        entity_id, document_id=document_id, workspace_id=foreign_id,
    ))
    assert foreign_read.get("error")
    assert "content" not in foreign_read
    private_read = json.loads(await _read_file(
        entity_id, document_id=private_id, workspace_id=workspace_id,
    ))
    assert private_read.get("error")
    assert "content" not in private_read
    missing_locator = json.loads(await _read_file(entity_id, workspace_id=workspace_id))
    assert missing_locator.get("error")


@pytest.mark.asyncio
async def test_read_file_returns_same_task_write_origin_only_for_unchanged_document(
    fs_enabled, tmp_path,
):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)

    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release A entity"))
        workspace = Workspace(entity_id=entity_id, name="Release A", status="active")
        db.add(workspace)
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        path = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/releases/song/eligibility.md"
        file = root / path
        file.parent.mkdir(parents=True)
        file.write_text("release_id: song\n", encoding="utf-8")
        document = await create_document(
            db, entity_id, name="eligibility.md", fs_path=path,
            folder_id=folder.id, source="ai_generated", visibility="workspace",
            file_size=file.stat().st_size,
            metadata={
                "origin": {
                    "workspace_id": workspace.id,
                    "task_id": task_id,
                    "tool_name": "generate_file",
                },
                "file_integrity": {
                    "status": "ok", "mtime_ns": file.stat().st_mtime_ns,
                },
            },
        )
        workspace_id, document_id = workspace.id, document.id
        await db.commit()

    read = json.loads(await _read_file(
        entity_id, document_id=document_id,
        workspace_id=workspace_id, task_id=task_id,
    ))
    assert read["content"] == "release_id: song\n"
    assert read["document_id"] == document_id
    assert read["write_receipt"] == {
        "origin_task_id": task_id,
        "origin_workspace_id": workspace_id,
        "tool_name": "generate_file",
        "source_sha256": read["source_sha256"],
    }

    other_task = json.loads(await _read_file(
        entity_id, document_id=document_id,
        workspace_id=workspace_id, task_id=generate_ulid(),
    ))
    assert "write_receipt" not in other_task
    path_only = json.loads(await _read_file(
        entity_id, path=path, workspace_id=workspace_id, task_id=task_id,
    ))
    assert "write_receipt" not in path_only

    async with db_module.async_session() as db:
        document = await db.get(Document, document_id)
        document.source = "upload"
        await db.commit()
    uploaded = json.loads(await _read_file(
        entity_id, document_id=document_id,
        workspace_id=workspace_id, task_id=task_id,
    ))
    assert "write_receipt" not in uploaded

    async with db_module.async_session() as db:
        document = await db.get(Document, document_id)
        document.source = "ai_generated"
        document.metadata_ = {**document.metadata_, "file_integrity": {"status": "missing"}}
        document.updated_at = document.created_at
        await db.commit()
    stale = json.loads(await _read_file(
        entity_id, document_id=document_id,
        workspace_id=workspace_id, task_id=task_id,
    ))
    assert "write_receipt" not in stale

    file.write_text("changed bytes", encoding="utf-8")
    changed = json.loads(await _read_file(
        entity_id, document_id=document_id,
        workspace_id=workspace_id, task_id=task_id,
    ))
    assert "write_receipt" not in changed


@pytest.mark.asyncio
async def test_userless_workspace_task_cannot_read_other_root_documents(
    fs_enabled, tmp_path,
):
    from packages.core.services.document_service import add_document_to_group, create_group

    entity_id = generate_ulid()
    root = tmp_path / entity_id
    root.mkdir(parents=True)
    names = (
        "unbound.txt", "foreign.txt", "private.txt", "restricted.txt",
        "quarantined.txt", "shared.txt", "trashed.txt",
    )
    for name in names:
        (root / name).write_text(f"secret: {name}", encoding="utf-8")

    document_ids = {}
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Release entity"))
        workspace = Workspace(entity_id=entity_id, name="Release A", status="active")
        foreign = Workspace(entity_id=entity_id, name="Release B", status="active")
        db.add_all([workspace, foreign])
        await db.flush()
        folder = await ensure_workspace_artifact_folder(db, workspace)
        await ensure_workspace_artifact_folder(db, foreign)
        shared_group = await create_group(
            db, entity_id, name="Shared B", workspace_id=foreign.id,
        )
        for name in names:
            doc = await create_document(
                db, entity_id, name=name, fs_path=name, folder_id=folder.id,
                source="upload",
                visibility="private" if name == "private.txt" else "workspace",
                classification="restricted" if name == "restricted.txt" else "internal",
                metadata={"origin": {
                    "workspace_id": foreign.id if name == "foreign.txt" else workspace.id,
                    "tool_name": "upload" if name == "unbound.txt" else "hitl_upload",
                }},
            )
            document_ids[name] = doc.id
            if name == "quarantined.txt":
                doc.quarantine_status = "quarantined"
            if name == "trashed.txt":
                doc.is_trashed = True
            if name == "shared.txt":
                await add_document_to_group(db, doc.id, shared_group.id, entity_id=entity_id)
        workspace_id = workspace.id
        await db.commit()

    for name in names:
        for kwargs in ({}, {"document_id": document_ids[name]}):
            result = json.loads(await _read_file(
                entity_id, path=name, workspace_id=workspace_id, **kwargs,
            ))
            assert result.get("error"), name
            assert "content" not in result, name

    listed = json.loads(await _list_files(
        entity_id, path="", workspace_id=workspace_id,
    ))
    assert not any(name in str(listed) for name in names)


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
