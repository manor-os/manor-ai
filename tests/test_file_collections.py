"""File collection verification stays on the existing Document delivery path."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workflow import WorkflowRun
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.file_collections import (
    FileCollectionError,
    archive_file_collection,
    verify_file_collection,
)
from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
from packages.core.services.generated_media_naming import workspace_execution_artifact_dir
from packages.core.services.workspace_artifacts import workspace_artifact_storage_base


@pytest.fixture(autouse=True)
def local_files(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")


async def _seed(db, tmp_path):
    entity_id, workspace_id, user_id = [generate_ulid() for _ in range(3)]
    db.add_all([
        Entity(id=entity_id, name="Collection entity"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Collection workspace", artifact_folder_id=generate_ulid()),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.invalid", password_hash="unused", role="owner", status="active"),
        UserMembership(entity_id=entity_id, user_id=user_id, role="owner", status="active"),
    ])
    audio_id, cover_id = generate_ulid(), generate_ulid()
    root = tmp_path / entity_id
    root.mkdir()
    audio = b"RIFF" + (11 * 1024 * 1024).to_bytes(4, "little") + b"WAVE" + (b"\x00" * (11 * 1024 * 1024))
    cover = b"RIFF" + (128).to_bytes(4, "little") + b"WEBP" + (b"\x00" * 128)
    (root / "master.wav").write_bytes(audio)
    (root / "cover.webp").write_bytes(cover)
    db.add_all([
        Document(id=audio_id, entity_id=entity_id, name="Host master.wav", fs_path="master.wav", file_size=len(audio), file_type="wav", mime_type="audio/wav", owner_id=user_id, visibility="entity"),
        Document(id=cover_id, entity_id=entity_id, name="Host cover.webp", fs_path="cover.webp", file_size=len(cover), file_type="webp", mime_type="image/webp", owner_id=user_id, visibility="entity"),
    ])
    await db.commit()
    return {"entity_id": entity_id, "workspace_id": workspace_id, "user_id": user_id, "audio_id": audio_id, "cover_id": cover_id, "audio": audio, "cover": cover}


def _source_receipts(ids):
    return [
        {"document_id": ids["audio_id"], "role": "source_audio"},
        {"document_id": ids["cover_id"], "role": "source_cover"},
    ]


@pytest.mark.cloud
@pytest.mark.asyncio
async def test_release_package_checks_audio_before_preparing_materials(db_session, tmp_path):
    """An MP4 with an audio track must fail before the release's agents prepare files."""
    from packages.core.ai.workflow_runner import _resolve_structure
    from packages.core.blueprints.package import unpack

    package = Path(__file__).parents[1] / "docs/blueprints/jr-music-studio-campaign-final-review-2026-10-30.manor"
    payload, _ = unpack(package.read_bytes())
    workflow = next(item for item in payload["recipe"]["workflows"]
                    if item["slug"] == "jr-music-prepare-single-release")
    steps = {step["id"]: step for step in workflow["steps"]}
    audio_input = next(item for item in steps["start_release_preparation"]["config"]["run_inputs"]
                       if item["key"] == "audio_files")
    assert audio_input["schema"]["x-ui"]["accept"] == ["audio"]
    assert audio_input["schema"]["minItems"] == audio_input["schema"]["maxItems"] == 1
    assert next(item for item in workflow["run_inputs"] if item["key"] == "audio_files") == audio_input

    gate = steps[steps["claim_release_project"]["next"][0]]
    assert gate["type"] == "tool"
    assert gate["config"]["tool"] == "verify_file_collection"
    assert gate["next"] == ["pin_release_source_audio"]
    assert steps["pin_release_source_audio"]["next"] == ["intake_release_identity"]
    ids = await _seed(db_session, tmp_path)
    args = _resolve_structure(gate["config"]["args"], {
        "audio_files": [{"document_id": ids["audio_id"]}],
        "release_project": {"project_id": "release-project", "revision": 0},
    })
    assert args["required_roles"] == ["source_audio"]
    assert args["verification_scope"] == "preparation"
    assert args["expected_sources"] == []
    context = dict(entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"])
    result = await verify_file_collection(db_session, **context, **args)
    assert result["preparation_completed"] is True
    assert result["delivery_verified"] is False
    assert result["files"][0]["document_id"] == ids["audio_id"]

    document = await db_session.get(Document, ids["audio_id"])
    document.name = "captioned-video.mp4"
    document.file_type = "mp4"
    document.mime_type = "video/mp4"
    await db_session.flush()
    with pytest.raises(FileCollectionError) as rejected:
        await verify_file_collection(db_session, **context, **args)
    assert rejected.value.code == "source_media_type_invalid"


@pytest.mark.asyncio
async def test_collection_reads_explicitly_linked_sources_in_another_workspace(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    reader_id = generate_ulid()
    source = Workspace(
        entity_id=ids["entity_id"], name="Previous release",
        settings={"access_mode": "members_only"},
    )
    db_session.add_all([
        source,
        User(id=reader_id, entity_id=ids["entity_id"], email=f"{reader_id}@test.invalid",
             password_hash="unused", role="member", status="active"),
        UserMembership(entity_id=ids["entity_id"], user_id=reader_id, role="member", status="active"),
        WorkspaceStaff(workspace_id=ids["workspace_id"], user_id=reader_id, role="viewer", status="active"),
    ])
    await db_session.flush()
    folder = await ensure_workspace_artifact_folder(db_session, source)
    group = DocumentGroup(
        id=generate_ulid(), entity_id=ids["entity_id"], name="Selected originals",
        workspace_id=ids["workspace_id"],
    )
    db_session.add(group)
    original_paths = {}
    links = []
    for document_id in (ids["audio_id"], ids["cover_id"]):
        document = await db_session.get(Document, document_id)
        path = f"{workspace_artifact_storage_base(folder.id)}/workflow/previous/{document.fs_path}"
        original_file = tmp_path / ids["entity_id"] / path
        original_file.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ids["entity_id"] / document.fs_path).rename(original_file)
        document.fs_path = path
        document.folder_id = folder.id
        document.visibility = "workspace"
        document.metadata_ = {"origin": {"workspace_id": source.id}}
        link = DocumentGroupMember(group_id=group.id, document_id=document_id)
        db_session.add(link)
        links.append(link)
        original_paths[document_id] = path
    await db_session.commit()

    args = dict(
        entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=reader_id,
        files=[{**receipt, "group": "sources"} for receipt in _source_receipts(ids)],
        required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids),
        confirmed_version="v1", title="Linked sources", summary="Ready",
    )
    verified = await verify_file_collection(db_session, **args)
    assert verified["delivery_verified"] is True
    for receipt, content in zip(verified["files"], (ids["audio"], ids["cover"]), strict=True):
        document = await db_session.get(Document, receipt["document_id"])
        assert document.fs_path == original_paths[document.id]
        assert document.folder_id == folder.id
        assert receipt["source_sha256"] == hashlib.sha256(content).hexdigest()
        assert (tmp_path / ids["entity_id"] / document.fs_path).read_bytes() == content

    await db_session.delete(links[0])
    await db_session.commit()
    with pytest.raises(FileCollectionError, match="not currently readable"):
        await verify_file_collection(db_session, **args)


@pytest.mark.asyncio
async def test_preparation_stage_verifies_real_documents_without_release_sources(db_session, tmp_path, monkeypatch):
    import json
    from contextlib import asynccontextmanager

    from packages.core.ai.tools import file_delivery_tools
    from packages.core.ai.workflow_runner import _resolve_structure

    ids = await _seed(db_session, tmp_path)
    plan_id = generate_ulid()
    (tmp_path / ids["entity_id"] / "plan.md").write_text("# Release plan\nSaved content")
    db_session.add(Document(
        id=plan_id, entity_id=ids["entity_id"], name="plan.md", fs_path="plan.md",
        file_type="md", mime_type="text/markdown", owner_id=ids["user_id"], visibility="entity",
    ))
    await db_session.commit()
    step_args = {
        "files": ["{{release_plan.plan_document}}"],
        "required_roles": ["release_plan"],
        "expected_sources": [],
        "verification_scope": "preparation",
        "confirmed_version": "{{release_project.project_id}}:{{release_project.revision}}",
        "title": "Plan files",
        "summary": "Saved preparation files",
    }
    args = _resolve_structure(step_args, {
        "release_plan": {"plan_document": {"document_id": plan_id, "role": "release_plan", "group": "Release Materials"}},
        "release_project": {"project_id": "project", "revision": 1},
    })
    context = {key: ids[key] for key in ("entity_id", "workspace_id", "user_id")}
    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(file_delivery_tools, "async_session", session)
    result = json.loads(await file_delivery_tools._verify_file_collection(**context, **args))
    assert result["preparation_completed"] is True
    assert result["delivery_verified"] is False
    assert result["files"][0]["document_id"] == plan_id

    # Final delivery still requires the two fixed original source receipts.
    with pytest.raises(FileCollectionError, match="source_audio and source_cover"):
        await verify_file_collection(db_session, **context, **{**args, "verification_scope": "delivery"})
    missing = await verify_file_collection(db_session, **context, **{**args, "required_roles": ["release_plan", "credits_rights"]})
    assert missing["preparation_completed"] is False
    assert missing["missing_roles"] == ["credits_rights"]
    for invalid in (
        {"verification_scope": "invalid"},
        {"expected_sources": _source_receipts(ids)},
    ):
        with pytest.raises(FileCollectionError):
            await verify_file_collection(db_session, **context, **{**args, **invalid})
    (tmp_path / ids["entity_id"] / "plan.md").unlink()
    with pytest.raises(FileCollectionError):
        await verify_file_collection(db_session, **context, **args)


@pytest.mark.asyncio
async def test_archive_collection_copies_sources_to_the_workflow_lineage_and_reuses_them(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    lineage, run_id = generate_ulid(), generate_ulid()
    db_session.add(WorkflowRun(
        id=run_id, workflow_id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        lineage_root_run_id=lineage, variables={}, step_results={}, trigger_data={}, definition_snapshot={}, execution_trace=[],
    ))
    await db_session.commit()
    await ensure_workspace_artifact_folder(
        db_session, await db_session.get(Workspace, ids["workspace_id"]),
    )
    await db_session.commit()
    collection = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
        ], required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
    )

    archived = await archive_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        workflow_run_id=run_id, workflow_lineage_root_run_id=lineage, collection=collection,
    )
    assert archived["archive_lineage_root_run_id"] == lineage
    assert [item["document_id"] for item in archived["files"]] != [ids["audio_id"], ids["cover_id"]]
    assert [item["source_document_id"] for item in archived["files"]] == [ids["audio_id"], ids["cover_id"]]
    assert (tmp_path / ids["entity_id"] / archived["files"][0]["archive_path"]).read_bytes() == ids["audio"]
    assert (tmp_path / ids["entity_id"] / archived["files"][1]["archive_path"]).read_bytes() == ids["cover"]
    archived_audio = await db_session.get(Document, archived["files"][0]["document_id"])
    assert archived_audio is not None
    assert archived_audio.mime_type == "audio/wav"
    archived_receipts = [
        {"document_id": archived["files"][0]["document_id"], "role": "source_audio"},
        {"document_id": archived["files"][1]["document_id"], "role": "source_cover"},
    ]
    verified_archive = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[
            {"document_id": archived["files"][0]["document_id"], "role": "source_audio", "group": "sources"},
            {"document_id": archived["files"][1]["document_id"], "role": "source_cover", "group": "sources"},
        ],
        required_roles=["source_audio", "source_cover"], expected_sources=archived_receipts, confirmed_version="jr-v1", title="JR", summary="Ready",
    )
    assert verified_archive["files"][0]["mime_type"] == "audio/wav"

    retried = await archive_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        workflow_run_id=run_id, workflow_lineage_root_run_id=lineage, collection=archived,
    )
    assert [item["document_id"] for item in retried["files"]] == [item["document_id"] for item in archived["files"]]

    next_lineage, next_run = generate_ulid(), generate_ulid()
    db_session.add(WorkflowRun(
        id=next_run, workflow_id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        lineage_root_run_id=next_lineage, variables={}, step_results={}, trigger_data={}, definition_snapshot={}, execution_trace=[],
    ))
    await db_session.commit()
    reused_in_new_run = await archive_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        workflow_run_id=next_run, workflow_lineage_root_run_id=next_lineage, collection=archived,
    )
    assert [item["source_document_id"] for item in reused_in_new_run["files"]] == [
        item["document_id"] for item in archived["files"]
    ]

    # A run working on a WorkflowProject archives into the project's folder,
    # the folder its approval was computed for.
    project_run = generate_ulid()
    db_session.add(WorkflowRun(
        id=project_run, workflow_id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        lineage_root_run_id=project_run, variables={}, step_results={}, trigger_data={}, definition_snapshot={}, execution_trace=[],
    ))
    await db_session.commit()
    in_project = await archive_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        workflow_run_id=project_run, workflow_lineage_root_run_id=project_run,
        workflow_project_root="projects/first-release", collection=archived,
    )
    assert all("/projects/first-release/" in item["archive_path"] for item in in_project["files"])


@pytest.mark.asyncio
async def test_archive_collection_rejects_denial_changes_collisions_and_wrong_run_before_writing(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    lineage, run_id = generate_ulid(), generate_ulid()
    db_session.add(WorkflowRun(
        id=run_id, workflow_id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        lineage_root_run_id=lineage, variables={}, step_results={}, trigger_data={}, definition_snapshot={}, execution_trace=[],
    ))
    await db_session.commit()
    workspace = await db_session.get(Workspace, ids["workspace_id"])
    await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.commit()
    collection = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[{"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"}, {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"}],
        required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
    )

    async def deny(_paths):
        return "approval required"

    with pytest.raises(FileCollectionError, match="approval required"):
        await archive_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            workflow_run_id=run_id, workflow_lineage_root_run_id=lineage, collection=collection, write_guard=deny,
        )
    assert not list((tmp_path / ids["entity_id"] / "Workspaces").rglob("master.wav"))

    archive_dir = workspace_execution_artifact_dir(
        workspace_artifact_storage_base(workspace.artifact_folder_id), workflow_run_id=run_id,
        workflow_lineage_root_run_id=lineage, entity_root=str(tmp_path / ids["entity_id"]),
    )
    collision_path = f"{archive_dir}/master.wav"
    collision_file = tmp_path / ids["entity_id"] / collision_path
    collision_file.parent.mkdir(parents=True, exist_ok=True)
    collision_file.write_bytes(b"unrelated")
    db_session.add(Document(
        id=generate_ulid(), entity_id=ids["entity_id"], name="collision", fs_path=collision_path,
        file_size=9, file_type="wav", mime_type="audio/wav", owner_id=ids["user_id"], visibility="entity",
    ))
    await db_session.commit()
    with pytest.raises(FileCollectionError, match="already belongs"):
        await archive_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            workflow_run_id=run_id, workflow_lineage_root_run_id=lineage, collection=collection,
        )
    assert collision_file.read_bytes() == b"unrelated"

    with pytest.raises(FileCollectionError, match="context no longer matches"):
        await archive_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            workflow_run_id=run_id, workflow_lineage_root_run_id=generate_ulid(), collection=collection,
        )

    (tmp_path / ids["entity_id"] / "master.wav").write_bytes(b"changed")
    with pytest.raises(FileCollectionError, match="bytes are unavailable or changed"):
        await archive_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            workflow_run_id=run_id, workflow_lineage_root_run_id=lineage, collection=collection,
        )


@pytest.mark.asyncio
async def test_archive_tool_guard_uses_the_standard_save_file_action(monkeypatch):
    from packages.core.ai.runtime.file_contracts import FileMutationAction
    from packages.core.ai.tools import file_delivery_tools

    captured = {}

    class Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def fake_guard(**kwargs):
        captured.update(kwargs)
        return None

    async def fake_archive(_db, **kwargs):
        assert await kwargs["write_guard"](["Workspaces/_by_id/folder/workflow/run/master.wav"]) is None
        return {"delivery_verified": True}

    monkeypatch.setattr(file_delivery_tools, "async_session", lambda: Session())
    monkeypatch.setattr(file_delivery_tools, "runtime_guard_file_mutation", fake_guard)
    monkeypatch.setattr(file_delivery_tools, "archive_file_collection", fake_archive)

    await file_delivery_tools._archive_file_collection(
        entity_id="entity", user_id="user", workspace_id="workspace",
        _workflow_run_id_from_context="run", _workflow_lineage_root_run_id_from_context="lineage",
        collection={},
    )

    assert captured["action"] is FileMutationAction.SAVE_FILE
    assert captured["tool_name"] == "archive_file_collection"
    assert captured["workspace_id"] == "workspace"


@pytest.mark.asyncio
async def test_collection_uses_verified_receipts_for_large_wav_and_webp(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    result = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources", "name": "untrusted.mp3"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources", "name": "untrusted.jpg"},
        ],
        required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR release", summary="Ready",
    )
    assert result["delivery_verified"] is True
    assert result["preparation_completed"] is True
    assert result["confirmed_version"] == "jr-v1"
    assert result["files"][0] == {
        "document_id": ids["audio_id"], "name": "Host master.wav", "role": "source_audio", "group": "sources",
        "mime_type": "audio/wav", "source_sha256": hashlib.sha256(ids["audio"]).hexdigest(),
        "size": len(ids["audio"]), "viewer_url": f"/viewer/{ids['audio_id']}",
    }
    assert result["files"][1]["mime_type"] == "image/webp"


@pytest.mark.asyncio
async def test_collection_dedupes_same_role_but_rejects_conflicting_role(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    args = dict(entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"], required_roles=["source_audio"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready")
    result = await verify_file_collection(db_session, files=[
        {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
        {"document_id": ids["audio_id"], "role": "source_audio", "group": "duplicate"},
        {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
    ], **args)
    assert len(result["files"]) == 2
    with pytest.raises(FileCollectionError, match="cannot satisfy more than one"):
        await verify_file_collection(db_session, files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
            {"document_id": ids["audio_id"], "role": "source_cover", "group": "sources"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
        ], **args)


@pytest.mark.asyncio
async def test_collection_reports_missing_role_without_failing(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    result = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
        ],
        required_roles=["source_audio", "source_cover", "release_notes"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
    )
    assert result["missing_roles"] == ["release_notes"]
    assert result["delivery_verified"] is False
    assert result["preparation_completed"] is False


@pytest.mark.asyncio
async def test_collection_rejects_source_role_with_wrong_registered_media_type(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    document = await db_session.get(Document, ids["audio_id"])
    document.mime_type = "image/webp"
    await db_session.commit()
    with pytest.raises(FileCollectionError, match="matching registered MIME type"):
        await verify_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            files=[{"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"}, {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"}],
            required_roles=["source_audio"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
        )


@pytest.mark.asyncio
async def test_collection_accepts_wav_when_registered_mime_is_generic(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    document = await db_session.get(Document, ids["audio_id"])
    document.mime_type = "application/octet-stream"
    await db_session.commit()
    result = await verify_file_collection(
        db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
        files=[{"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"}, {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"}],
        required_roles=["source_audio"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
    )
    assert result["files"][0]["mime_type"] == "audio/wav"


@pytest.mark.asyncio
async def test_collection_rejects_a_readable_but_unselected_source_document(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    alternate_id = generate_ulid()
    root = tmp_path / ids["entity_id"]
    (root / "alternate.wav").write_bytes(b"RIFFalternate")
    db_session.add(Document(
        id=alternate_id, entity_id=ids["entity_id"], name="Alternate", fs_path="alternate.wav",
        mime_type="audio/wav", owner_id=ids["user_id"], visibility="entity",
    ))
    await db_session.commit()
    with pytest.raises(FileCollectionError, match="source receipt selected for this run"):
        await verify_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            files=[
                {"document_id": alternate_id, "role": "source_audio", "group": "sources"},
                {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
            ],
            required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
        )
    with pytest.raises(FileCollectionError, match="source receipt selected for this run"):
        await verify_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            files=[
                {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
                {"document_id": alternate_id, "role": "source_audio", "group": "sources"},
                {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
            ],
            required_roles=["source_audio", "source_cover"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready",
        )


@pytest.mark.asyncio
async def test_collection_honors_an_optional_prior_source_snapshot(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    expected_sources = _source_receipts(ids)
    expected_sources[0]["source_sha256"] = hashlib.sha256(ids["audio"]).hexdigest()
    (tmp_path / ids["entity_id"] / "master.wav").write_bytes(b"changed source")
    with pytest.raises(FileCollectionError, match="bytes are unavailable or changed"):
        await verify_file_collection(
            db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"],
            files=[
                {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
                {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
            ],
            required_roles=["source_audio", "source_cover"], expected_sources=expected_sources, confirmed_version="jr-v1", title="JR", summary="Ready",
        )


@pytest.mark.asyncio
async def test_collection_rejects_missing_or_unreadable_documents(db_session, tmp_path):
    ids = await _seed(db_session, tmp_path)
    args = dict(entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], user_id=ids["user_id"], required_roles=["source_audio"], expected_sources=_source_receipts(ids), confirmed_version="jr-v1", title="JR", summary="Ready")
    with pytest.raises(FileCollectionError, match="does not resolve"):
        await verify_file_collection(db_session, files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
            {"document_id": generate_ulid(), "role": "release_notes", "group": "delivery"},
        ], **args)
    other_user = generate_ulid()
    db_session.add(User(id=other_user, entity_id=ids["entity_id"], email=f"{other_user}@test.invalid", password_hash="unused", role="member", status="active"))
    await db_session.commit()
    with pytest.raises(FileCollectionError, match="not currently readable"):
        await verify_file_collection(db_session, user_id=other_user, files=[
            {"document_id": ids["audio_id"], "role": "source_audio", "group": "sources"},
            {"document_id": ids["cover_id"], "role": "source_cover", "group": "sources"},
        ], **{key: value for key, value in args.items() if key != "user_id"})
