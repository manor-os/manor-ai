"""Pausing Workspace autonomy must preserve actor-authorized file operations."""

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from packages.core import database
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.governance.approval_policy_service import account_defaults
from tests.approval_settings_test_utils import (
    action_rule, initialize_test_approval_owners, set_workspace_test_approval,
)
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.ai_file_permissions import (
    _resource_file_acl_denial,
    guard_ai_file_mutation,
    guard_ai_file_read_access,
)
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_folder,
    workspace_artifact_storage_base,
)


@pytest_asyncio.fixture
async def paused_files(db_session, monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_AI_FILE_HITL_ENABLED", True)
    entity = Entity(id=generate_ulid(), name="Paused files",
        settings={"approval_defaults": account_defaults()})
    actor = User(
        entity_id=entity.id,
        email=f"paused-files-{generate_ulid()}@test.example",
        password_hash="unused",
        role="member",
        status="active",
    )
    workspace = Workspace(
        entity_id=entity.id,
        name="Paused website",
        status="paused",
        settings={"access_mode": "members_only"},
    )
    db_session.add_all([entity, actor, workspace])
    await db_session.flush()
    membership = WorkspaceStaff(
        workspace_id=workspace.id,
        user_id=actor.id,
        role="contributor",
        status="active",
    )
    db_session.add_all([
        UserMembership(
            user_id=actor.id,
            entity_id=entity.id,
            role="member",
            status="active",
            is_primary=True,
        ),
        membership,
    ])
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=actor.id)
    folder = await ensure_workspace_artifact_folder(db_session, workspace)
    base = workspace_artifact_storage_base(folder.id)
    existing_path = f"{base}/code/landing/index.html"
    document = Document(
        entity_id=entity.id,
        name="index.html",
        fs_path=existing_path,
        file_type="html",
        source="agent",
        visibility="workspace",
        owner_id=actor.id,
        folder_id=folder.id,
        metadata_={"origin": {"workspace_id": workspace.id}},
    )
    db_session.add(document)
    await db_session.flush()
    target = tmp_path / entity.id / existing_path
    target.parent.mkdir(parents=True)
    target.write_text("<html>Existing landing page</html>", encoding="utf-8")

    class ExistingSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(database, "async_session", ExistingSession)
    return SimpleNamespace(
        db=db_session,
        entity=entity,
        actor=actor,
        workspace=workspace,
        membership=membership,
        document=document,
        existing_path=existing_path,
        new_path=f"{base}/code/landing/terms.html",
    )


async def _write_denial(files, *, path=None, action="write", actor=True):
    return await _resource_file_acl_denial(
        files.db,
        runtime_envelope=None,
        entity_id=files.entity.id,
        user_id=files.actor.id if actor else None,
        workspace_id=files.workspace.id,
        action=action,
        tool_name="write_file",
        paths=[path or files.new_path],
    )


@pytest.mark.asyncio
async def test_paused_workspace_member_can_read_create_edit_and_delete_artifact(paused_files):
    files = paused_files
    assert await guard_ai_file_read_access(
        entity_id=files.entity.id,
        user_id=files.actor.id,
        workspace_id=files.workspace.id,
        tool_name="read_file",
        paths=[files.existing_path],
    ) is None
    assert await _write_denial(files) is None
    assert await _write_denial(files, path=files.existing_path) is None
    assert await _write_denial(files, path=files.existing_path, action="delete") is None
    assert files.workspace.status == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("role,status", [("viewer", "active"), ("contributor", "inactive")])
async def test_paused_workspace_does_not_grant_missing_write_membership(paused_files, role, status):
    files = paused_files
    files.membership.role = role
    files.membership.status = status
    await files.db.flush()

    denied = json.loads(await _write_denial(files))
    assert denied["mode"] == "entity_file_acl_denied"
    assert denied["reason"] == "Workspace artifact write access is required"


@pytest.mark.asyncio
async def test_paused_workspace_still_requires_document_edit_access(paused_files):
    files = paused_files
    files.document.owner_id = None
    files.document.visibility = "private"
    await files.db.flush()

    denied = json.loads(await _write_denial(files, path=files.existing_path))
    assert denied["reason"] == "Edit access is required to write this file"


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["deleted", "inactive", "missing_folder"])
async def test_unavailable_workspace_cannot_write_artifacts(paused_files, condition):
    files = paused_files
    if condition == "deleted":
        files.workspace.deleted_at = datetime.now(UTC)
    elif condition == "inactive":
        files.workspace.status = "archived"
    else:
        files.workspace.artifact_folder_id = None
    await files.db.flush()

    denied = json.loads(await _write_denial(files))
    assert denied["reason"] == "The Workspace artifact scope is unavailable"
    if condition == "deleted":
        assert await guard_ai_file_read_access(
            entity_id=files.entity.id,
            user_id=files.actor.id,
            workspace_id=files.workspace.id,
            tool_name="read_file",
            paths=[files.existing_path],
        ) is not None


@pytest.mark.asyncio
async def test_paused_workspace_cannot_write_another_workspace_root(paused_files):
    files = paused_files
    other = Workspace(entity_id=files.entity.id, name="Other", status="paused")
    files.db.add(other)
    await files.db.flush()
    folder = await ensure_workspace_artifact_folder(files.db, other)
    await files.db.flush()

    denied = json.loads(await _write_denial(
        files,
        path=f"{workspace_artifact_storage_base(folder.id)}/terms.html",
    ))
    assert denied["reason"] == "The path is outside the Workspace artifact scope"


@pytest.mark.asyncio
async def test_paused_workspace_does_not_expand_actorless_autonomous_exception(paused_files):
    denied = json.loads(await _write_denial(paused_files, actor=False))
    assert denied["mode"] == "entity_file_acl_denied"
    assert "An authenticated user is required" in denied["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deny", "approval"])
async def test_paused_workspace_still_applies_file_approval_policy(paused_files, mode):
    files = paused_files
    owner = User(id=generate_ulid(), entity_id=files.entity.id,
        email=f"file-policy-{generate_ulid()}@test.example", password_hash="fixture",
        role="owner", status="active")
    files.db.add_all([owner, UserMembership(user_id=owner.id, entity_id=files.entity.id,
        role="owner", status="active")])
    await files.db.flush()
    await set_workspace_test_approval(files.db, workspace_id=files.workspace.id,
        entity_id=files.entity.id, actor_id=owner.id,
        user_rules=[action_rule("workspace.file.create", effect="ask" if mode == "approval" else "deny")])

    denied = json.loads(await guard_ai_file_mutation(
        entity_id=files.entity.id,
        user_id=files.actor.id,
        conversation_id=None,
        execution_id=f"paused-file:{mode}",
        workspace_id=files.workspace.id,
        tool_name="write_file",
        action="write",
        paths=[files.new_path],
    ))
    assert denied["mode"] == mode
    if mode == "approval":
        assert "Approval is required" in denied["reason"]
