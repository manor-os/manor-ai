"""Knowledge file read/write/edit consistency (architecture fix).

One logical file has three representations — FS bytes, the Document projection
(+ metadata.content_text), and pgvector chunks — and two path resolvers.
These tests pin the unified behavior:

  * every byte change invalidates the derived representations (vector_status
    back to pending, content_text fork dropped) so RAG cannot serve stale
    content — the fundraising_ops.md "saved but reads stale" incident;
  * read/edit locate a workspace-scoped file by its logical name, so write and
    read address the SAME file (no write-A-read-B fork);
  * a genuine miss returns same-basename candidates instead of dead-ending.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select

from packages.core.ai.tools import file_tools
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, VectorStatus
from packages.core.models.event import EventLog
from packages.core.models.workspace import Workspace


@pytest.fixture
def fs_enabled(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old = (settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT, settings.DEPLOYMENT_MODE)
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    async def allow_file_mutation(**_kwargs):
        return None

    async def allow_file_resource_access(**_kwargs):
        return None

    async def allow_test_paths(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_mutation)
    monkeypatch.setattr(
        file_tools,
        "runtime_guard_file_resource_access",
        allow_file_resource_access,
    )
    monkeypatch.setattr(file_tools, "_blocked_doc_paths", allow_test_paths)
    try:
        import packages.core.services.knowledge_sync as ks
        monkeypatch.setattr(ks, "_schedule_document_reembed", lambda _id: None)
    except Exception:
        pass
    yield
    settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT, settings.DEPLOYMENT_MODE = old


async def _seed_workspace(db_session, entity_id, workspace_id):
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Ops WS", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
    await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.commit()
    from packages.core.services.entity_fs import provision_entity_filesystem
    provision_entity_filesystem(entity_id)


async def _doc_for(db_session, entity_id, name):
    db_session.expire_all()
    return (await db_session.execute(
        select(Document).where(Document.entity_id == entity_id, Document.name == name)
    )).scalar_one_or_none()


@pytest.mark.asyncio
async def test_runtime_projection_delivers_document_event_after_commit(
    db_session,
    fs_enabled,
    monkeypatch,
):
    from packages.core.services import event_emitter
    from packages.core.services.entity_fs import provision_entity_filesystem

    entity_id = generate_ulid()
    provision_entity_filesystem(entity_id)
    deliveries: list[tuple[str, dict]] = []
    delivery_complete = asyncio.Event()

    async def capture_webhook(_entity_id, event_type, payload):
        assert _entity_id == entity_id
        deliveries.append((f"webhook:{event_type}", dict(payload)))

    async def capture_external(_entity_id, event_type, payload, **_delivery_context):
        assert _entity_id == entity_id
        deliveries.append((f"external:{event_type}", dict(payload)))
        delivery_complete.set()

    monkeypatch.setattr(event_emitter, "deliver_webhook_event", capture_webhook)
    monkeypatch.setattr(
        event_emitter,
        "deliver_task_external_event",
        capture_external,
    )

    result = json.loads(await file_tools._write_file(
        entity_id,
        path="event-report.md",
        content="# Report\n",
    ))

    assert result["written"] is True
    await asyncio.wait_for(delivery_complete.wait(), timeout=1)
    events = list((await db_session.scalars(
        select(EventLog).where(
            EventLog.entity_id == entity_id,
            EventLog.event_type == "document.uploaded",
        ),
    )).all())
    assert len(events) == 1
    assert events[0].payload["document_id"] == result["document_id"]
    assert deliveries == [
        (
            "webhook:document.uploaded",
            {"document_id": result["document_id"], "name": "event-report.md"},
        ),
        (
            "external:document.uploaded",
            {"document_id": result["document_id"], "name": "event-report.md"},
        ),
    ]


@pytest.mark.asyncio
async def test_filesystem_projection_queues_reembed_only_after_commit(
    db_session,
    fs_enabled,
    monkeypatch,
):
    from packages.core.services import knowledge_sync
    from packages.core.services.entity_fs import provision_entity_filesystem, resolve_path

    entity_id = generate_ulid()
    provision_entity_filesystem(entity_id)
    abs_path = resolve_path(entity_id, "commit-ordered.md")
    Path(abs_path).write_text("committed bytes\n", encoding="utf-8")
    order: list[str] = []

    def record_commit(_session):
        order.append("commit")

    event.listen(db_session.sync_session, "after_commit", record_commit)
    monkeypatch.setattr(
        knowledge_sync,
        "_schedule_document_reembed",
        lambda _document_id: order.append("schedule"),
    )
    try:
        result = await knowledge_sync.sync_file_to_knowledge(
            entity_id=entity_id,
            abs_path=abs_path,
            entity_root=str(Path(abs_path).parent),
            source="manual",
            db=db_session,
            commit=True,
        )
    finally:
        event.remove(db_session.sync_session, "after_commit", record_commit)

    assert result.synced is True
    assert order[-1] == "schedule"
    assert order[:-1]
    assert set(order[:-1]) == {"commit"}


@pytest.mark.asyncio
async def test_filesystem_projection_does_not_queue_reembed_when_commit_fails(
    db_session,
    fs_enabled,
    monkeypatch,
):
    from packages.core.services import knowledge_sync
    from packages.core.services.entity_fs import provision_entity_filesystem, resolve_path

    entity_id = generate_ulid()
    provision_entity_filesystem(entity_id)
    abs_path = resolve_path(entity_id, "commit-failed.md")
    Path(abs_path).write_text("uncommitted bytes\n", encoding="utf-8")
    scheduled: list[str] = []

    def reject_commit(_session):
        raise RuntimeError("commit failed")

    event.listen(db_session.sync_session, "before_commit", reject_commit)
    monkeypatch.setattr(
        knowledge_sync,
        "_schedule_document_reembed",
        lambda document_id: scheduled.append(document_id),
    )
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            await knowledge_sync.sync_file_to_knowledge(
                entity_id=entity_id,
                abs_path=abs_path,
                entity_root=str(Path(abs_path).parent),
                source="manual",
                db=db_session,
                commit=True,
            )
    finally:
        event.remove(db_session.sync_session, "before_commit", reject_commit)
        await db_session.rollback()

    assert scheduled == []


@pytest.mark.asyncio
async def test_edit_invalidates_stale_embedding_and_content_text(db_session, fs_enabled):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)

    w = json.loads(await file_tools._write_file(
        entity_id, path="fundraising_ops.md",
        content="| Investor | URL |\n| Garry Tan | https://www.ycombinator.com/people/garry tan |\n",
        save_to_knowledge=True, workspace_id=workspace_id,
    ))
    assert w["written"] is True
    await db_session.commit()

    doc = await _doc_for(db_session, entity_id, "fundraising_ops.md")
    assert doc is not None
    # simulate the indexer having embedded it + a legacy content_text fork
    doc.vector_status = VectorStatus.READY
    doc.metadata_ = {**(doc.metadata_ or {}), "content_text": "STALE bad url garry tan"}
    await db_session.commit()

    e = json.loads(await file_tools._edit_file(
        entity_id, path=w["path"],
        old_text="https://www.ycombinator.com/people/garry tan",
        new_text="https://www.ycombinator.com/people/garry-tan",
        workspace_id=workspace_id,
    ))
    assert e.get("edited") or e.get("replacements") or "error" not in e, e
    await db_session.commit()

    doc = await _doc_for(db_session, entity_id, "fundraising_ops.md")
    # derived representations invalidated so RAG re-derives from fresh bytes
    assert doc.vector_status == VectorStatus.PENDING
    assert "content_text" not in (doc.metadata_ or {})


@pytest.mark.asyncio
async def test_agent_office_overwrite_invalidates_current_preview_marker(
    db_session,
    fs_enabled,
):
    from packages.core.services.slide_renderer import publish_current_preview_version

    entity_id = generate_ulid()
    from packages.core.services.entity_fs import provision_entity_filesystem

    entity_root = file_tools._get_entity_root(entity_id)
    provision_entity_filesystem(entity_id)
    result = json.loads(await file_tools._write_file(
        entity_id,
        path="deck.pptx",
        content="# First version\n",
    ))
    assert result["written"] is True
    await db_session.commit()

    doc = await _doc_for(db_session, entity_id, "deck.pptx")
    assert doc is not None and doc.fs_path
    source_path = Path(entity_root, doc.fs_path)
    cache_dir = Path(entity_root, ".slide-cache", doc.id)
    version_dir = cache_dir / "0123456789abcdef"
    version_dir.mkdir(parents=True)
    (version_dir / "slide-1.png").write_bytes(b"first-preview")
    (version_dir / ".complete").write_text("1", encoding="utf-8")
    publish_current_preview_version(
        str(cache_dir),
        version_dir.name,
        str(source_path),
    )
    assert (cache_dir / ".current").is_file()

    overwritten = json.loads(await file_tools._write_file(
        entity_id,
        path=doc.fs_path,
        content="# Second version with changed bytes\n",
    ))
    assert overwritten["written"] is True
    await db_session.commit()

    assert not (cache_dir / ".current").exists()


@pytest.mark.asyncio
async def test_write_scoped_then_read_and_edit_by_logical_name(db_session, fs_enabled):
    """A workspace write reroutes a new file under the artifact folder; reading
    and editing it by its bare logical name must still find it."""
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)

    w = json.loads(await file_tools._write_file(
        entity_id, path="fundraising_ops.md", content="row one\n",
        save_to_knowledge=True, workspace_id=workspace_id,
    ))
    await db_session.commit()
    # write rerouted the new file away from the bare name
    assert w["path"] != "fundraising_ops.md"

    r = json.loads(await file_tools._read_file(
        entity_id, path="fundraising_ops.md", workspace_id=workspace_id,
    ))
    assert "error" not in r, r
    assert "row one" in r.get("content", "")

    e = json.loads(await file_tools._edit_file(
        entity_id, path="fundraising_ops.md",
        old_text="row one", new_text="row edited", workspace_id=workspace_id,
    ))
    assert "error" not in e, e

    r2 = json.loads(await file_tools._read_file(
        entity_id, path="fundraising_ops.md", workspace_id=workspace_id,
    ))
    assert "row edited" in r2.get("content", "")


@pytest.mark.asyncio
async def test_workspace_logical_name_prefers_scoped_file_over_entity_collision(
    db_session,
    fs_enabled,
):
    """Workspace reads, edits, and deletes must target the path used by writes."""
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)

    root_write = json.loads(await file_tools._write_file(
        entity_id,
        path="shared_name.md",
        content="entity root\n",
    ))
    workspace_write = json.loads(await file_tools._write_file(
        entity_id,
        path="shared_name.md",
        content="workspace copy\n",
        workspace_id=workspace_id,
    ))
    assert root_write["path"] == "shared_name.md"
    assert workspace_write["path"] != root_write["path"]

    workspace_read = json.loads(await file_tools._read_file(
        entity_id,
        path="shared_name.md",
        workspace_id=workspace_id,
    ))
    assert workspace_read.get("content") == "workspace copy\n"

    workspace_edit = json.loads(await file_tools._edit_file(
        entity_id,
        path="shared_name.md",
        old_text="workspace copy",
        new_text="workspace edited",
        workspace_id=workspace_id,
    ))
    assert "error" not in workspace_edit, workspace_edit

    workspace_delete = json.loads(await file_tools._delete_file(
        entity_id,
        path="shared_name.md",
        workspace_id=workspace_id,
    ))
    assert workspace_delete.get("deleted") is True

    entity_read = json.loads(await file_tools._read_file(
        entity_id,
        path="shared_name.md",
    ))
    assert entity_read.get("content") == "entity root\n"


@pytest.mark.asyncio
async def test_not_found_lists_same_basename_candidates(db_session, fs_enabled):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)
    # a file exists at a nested path but the model guesses the bare name
    await file_tools._write_file(
        entity_id, path="dev/reports/tracker.md", content="x\n",
        workspace_id=None,
    )

    r = json.loads(await file_tools._read_file(entity_id, path="tracker.md"))
    assert r["error"].startswith("File not found")
    assert "dev/reports/tracker.md" in r.get("candidates", [])


def test_same_basename_candidates_hide_runtime_internal_paths(tmp_path):
    hidden = tmp_path / "uploads"
    visible = tmp_path / "reports"
    hidden.mkdir()
    visible.mkdir()
    (hidden / "tracker.md").write_text("internal", encoding="utf-8")
    (visible / "tracker.md").write_text("visible", encoding="utf-8")

    assert file_tools._same_basename_candidates(
        str(tmp_path),
        "tracker.md",
    ) == ["reports/tracker.md"]


@pytest.mark.asyncio
async def test_unchanged_resync_does_not_reset_vector_status(db_session, fs_enabled, monkeypatch):
    """A reconcile/resync of a byte-identical file must NOT churn embeddings."""
    from packages.core.services import knowledge_sync

    entity_id = generate_ulid()
    from packages.core.services.entity_fs import provision_entity_filesystem, resolve_path
    provision_entity_filesystem(entity_id)

    await file_tools._write_file(entity_id, path="notes.md", content="hello\n")
    await db_session.commit()
    doc = await _doc_for(db_session, entity_id, "notes.md")
    doc.vector_status = VectorStatus.READY
    await db_session.commit()

    # re-sync the SAME bytes (no write) → must stay ready
    abs_path = resolve_path(entity_id, "notes.md")
    root = file_tools._get_entity_root(entity_id)
    await knowledge_sync.sync_file_to_knowledge(
        entity_id=entity_id, abs_path=abs_path, entity_root=root,
        source="filesystem_reconcile",
    )
    await db_session.commit()
    doc = await _doc_for(db_session, entity_id, "notes.md")
    assert doc.vector_status == VectorStatus.READY
