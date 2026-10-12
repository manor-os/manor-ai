from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import file_tools


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fs_settings(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    previous = (
        settings.MANOR_FS_ENABLED,
        settings.MANOR_FS_ROOT,
        settings.DEPLOYMENT_MODE,
    )
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"
    yield settings
    (
        settings.MANOR_FS_ENABLED,
        settings.MANOR_FS_ROOT,
        settings.DEPLOYMENT_MODE,
    ) = previous


def test_replace_file_schema_requires_exact_source_and_target_versions() -> None:
    schema = file_tools.REPLACE_FILE_SCHEMA["function"]
    parameters = schema["parameters"]

    assert schema["name"] == "replace_file"
    assert parameters["required"] == [
        "source_path",
        "target_path",
        "expected_source_sha256",
        "expected_target_sha256",
    ]
    assert "approval_token" in parameters["properties"]
    assert "replace_file" in {
        tool_schema["function"]["name"]
        for tool_schema, _handler in file_tools.get_tools()
    }


def test_replace_file_is_a_target_scoped_workspace_modify_action() -> None:
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool_action,
    )
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalRequest,
        runtime_capability_id_for_action_key,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory,
    )
    from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
    from packages.core.ai.runtime.file_context import _tool_paths
    from packages.core.ai.runtime.tool_execution import RUNTIME_ENVELOPE_AWARE_TOOLS

    arguments = {
        "source_path": "candidates/new-character.png",
        "target_path": "brand/stickman-character.png",
        "expected_source_sha256": "a" * 64,
        "expected_target_sha256": "b" * 64,
    }
    action = classify_runtime_tool_action("replace_file", arguments)

    assert action is not None
    assert action.action_key == "workspace.file.modify"
    assert action.operation == "modify"
    assert action.resource_id == "brand/stickman-character.png"
    assert runtime_capability_id_for_action_key("replace_file") == "file.write"
    assert "replace_file" in CORE_CAPABILITIES["file.write"].tool_names
    assert "replace_file" in RUNTIME_ENVELOPE_AWARE_TOOLS
    assert _tool_paths("replace_file", arguments) == ("brand/stickman-character.png",)
    assert WorkspaceFileAuthorizationResourceFactory.supports_workspace_scope(
        "replace_file"
    )

    request = RuntimeApprovalRequest(
        tool_name="replace_file",
        arguments=arguments,
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
    )
    assert WorkspaceFileAuthorizationResourceFactory.create(
        request=request,
        action_key=action.action_key,
        resource_id=action.resource_id,
    )[0].resource_id == "brand/stickman-character.png"


@pytest.mark.asyncio
async def test_replace_file_copies_verified_bytes_without_changing_source(
    tmp_path,
    monkeypatch,
) -> None:
    entity_id = "entity-1"
    root = tmp_path / entity_id
    source = root / "candidates" / "new-character.png"
    target = root / "brand" / "stickman-character.png"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source_bytes = b"candidate-png"
    target_bytes = b"current-png"
    source.write_bytes(source_bytes)
    target.write_bytes(target_bytes)

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))
    access_calls: list[tuple[str, tuple[str, ...]]] = []

    async def allow_read(**kwargs):
        access_calls.append(("read", tuple(kwargs["paths"])))

    async def allow_resource(**kwargs):
        access_calls.append(("resource", tuple(kwargs["paths"])))

    async def allow_mutation(**kwargs):
        access_calls.append(("mutation", tuple(kwargs["paths"])))

    async def fake_commit(**kwargs):
        copied = Path(kwargs["source_abs_path"]).read_bytes()
        resolved_target = root / kwargs["rel_path"]
        resolved_target.write_bytes(copied)
        return (
            str(resolved_target),
            SimpleNamespace(synced=True, document_id="target-doc", reason=None),
            {
                "size": len(copied),
                "mtime_ns": 1,
                "source_sha256": _sha256(copied),
            },
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_read_access", allow_read)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_resource)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_mutation)
    monkeypatch.setattr(file_tools, "_commit_file_copy_projection", fake_commit)

    result = json.loads(await file_tools._replace_file(
        entity_id,
        source_path="candidates/new-character.png",
        target_path="brand/stickman-character.png",
        expected_source_sha256=_sha256(source_bytes),
        expected_target_sha256=_sha256(target_bytes),
    ))

    assert result == {
        "replaced": True,
        "unchanged": False,
        "source_path": "candidates/new-character.png",
        "target_path": "brand/stickman-character.png",
        "size": len(source_bytes),
        "source_sha256": _sha256(source_bytes),
        "target_sha256": _sha256(source_bytes),
        "mtime_ns": 1,
        "knowledge_synced": True,
        "document_id": "target-doc",
        "knowledge_sync_reason": None,
    }
    assert source.read_bytes() == source_bytes
    assert target.read_bytes() == source_bytes
    assert access_calls == [
        ("read", ("candidates/new-character.png",)),
        ("resource", ("brand/stickman-character.png",)),
        ("mutation", ("brand/stickman-character.png",)),
    ]


@pytest.mark.asyncio
async def test_replace_file_never_creates_a_missing_target(tmp_path, monkeypatch) -> None:
    root = tmp_path / "entity-1"
    source = root / "candidate.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"candidate")
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    result = json.loads(await file_tools._replace_file(
        "entity-1",
        source_path="candidate.png",
        target_path="missing.png",
        expected_source_sha256=_sha256(b"candidate"),
        expected_target_sha256=_sha256(b"old"),
    ))

    assert result["error"] == "target_missing"
    assert result["target_path"] == "missing.png"
    assert not (root / "missing.png").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stale_argument", "expected_error"),
    [
        ("expected_source_sha256", "source_changed"),
        ("expected_target_sha256", "target_changed"),
    ],
)
async def test_replace_file_rejects_stale_sha_guards_without_writing(
    tmp_path,
    monkeypatch,
    stale_argument,
    expected_error,
) -> None:
    root = tmp_path / "entity-1"
    source = root / "candidate.png"
    target = root / "canonical.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"candidate")
    target.write_bytes(b"current")
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow(**_kwargs):
        return None

    async def fail_commit(**_kwargs):
        raise AssertionError("stale replacement must not commit")

    monkeypatch.setattr(file_tools, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow)
    monkeypatch.setattr(file_tools, "_commit_file_copy_projection", fail_commit)
    arguments = {
        "source_path": "candidate.png",
        "target_path": "canonical.png",
        "expected_source_sha256": _sha256(b"candidate"),
        "expected_target_sha256": _sha256(b"current"),
    }
    arguments[stale_argument] = _sha256(b"stale")

    result = json.loads(await file_tools._replace_file("entity-1", **arguments))

    assert result["error"] == expected_error
    assert target.read_bytes() == b"current"


@pytest.mark.asyncio
async def test_replace_file_is_idempotent_when_target_already_matches_source(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "entity-1"
    source = root / "candidate.png"
    target = root / "canonical.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same-bytes")
    target.write_bytes(b"same-bytes")
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow(**_kwargs):
        return None

    async def fail_mutation(**_kwargs):
        raise AssertionError("idempotent replacement must not request a mutation")

    monkeypatch.setattr(file_tools, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", fail_mutation)
    digest = _sha256(b"same-bytes")

    result = json.loads(await file_tools._replace_file(
        "entity-1",
        source_path="candidate.png",
        target_path="canonical.png",
        expected_source_sha256=digest,
        expected_target_sha256=digest,
    ))

    assert result["replaced"] is False
    assert result["unchanged"] is True
    assert result["source_sha256"] == digest
    assert result["target_sha256"] == digest
    assert target.read_bytes() == b"same-bytes"


@pytest.mark.asyncio
async def test_replace_file_rejects_same_resolved_file(tmp_path, monkeypatch) -> None:
    root = tmp_path / "entity-1"
    target = root / "canonical.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same")
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))
    digest = _sha256(b"same")

    result = json.loads(await file_tools._replace_file(
        "entity-1",
        source_path="canonical.png",
        target_path="canonical.png",
        expected_source_sha256=digest,
        expected_target_sha256=digest,
    ))

    assert result["error"] == "source_matches_target"


@pytest.mark.asyncio
async def test_replace_file_preserves_target_knowledge_document_identity(
    fs_settings,
    db_session,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    entity_id = generate_ulid()
    root = Path(provision_entity_filesystem(entity_id))
    source = root / "candidate.txt"
    target = root / "canonical.txt"
    source.write_bytes(b"candidate bytes")
    target.write_bytes(b"current bytes")

    for path in (source, target):
        synced = await sync_file_to_knowledge(
            entity_id=entity_id,
            abs_path=str(path),
            entity_root=str(root),
            source="manual",
            created_by="user-1",
            user_id="user-1",
            db=db_session,
        )
        assert synced.synced is True
    await db_session.commit()
    original_target = (await db_session.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == "canonical.txt",
        )
    )).scalar_one()
    original_target_id = original_target.id

    async def allow(**_kwargs):
        return None

    monkeypatch.setattr(file_tools, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow)
    result = json.loads(await file_tools._replace_file(
        entity_id,
        user_id="user-1",
        source_path="candidate.txt",
        target_path="canonical.txt",
        expected_source_sha256=_sha256(b"candidate bytes"),
        expected_target_sha256=_sha256(b"current bytes"),
    ))

    assert result["replaced"] is True
    assert result["document_id"] == original_target_id
    assert source.read_bytes() == b"candidate bytes"
    assert target.read_bytes() == b"candidate bytes"
    db_session.expire_all()
    updated_target = (await db_session.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == "canonical.txt",
        )
    )).scalar_one()
    assert updated_target.id == original_target_id
    assert updated_target.fs_path == "canonical.txt"


@pytest.mark.asyncio
async def test_replace_file_restores_target_when_knowledge_projection_fails(
    fs_settings,
    monkeypatch,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.services import knowledge_sync
    from packages.core.services.entity_fs import provision_entity_filesystem

    entity_id = generate_ulid()
    root = Path(provision_entity_filesystem(entity_id))
    source = root / "candidate.txt"
    target = root / "canonical.txt"
    source.write_bytes(b"candidate bytes")
    target.write_bytes(b"current bytes")

    async def allow(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_read_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)
    result = json.loads(await file_tools._replace_file(
        entity_id,
        source_path="candidate.txt",
        target_path="canonical.txt",
        expected_source_sha256=_sha256(b"candidate bytes"),
        expected_target_sha256=_sha256(b"current bytes"),
    ))

    assert result["replaced"] is False
    assert result["knowledge_sync_reason"] == "storage_limit"
    assert source.read_bytes() == b"candidate bytes"
    assert target.read_bytes() == b"current bytes"
