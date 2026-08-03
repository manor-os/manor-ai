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

import json

import pytest
from sqlalchemy import select

from packages.core.ai.tools import file_tools
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, VectorStatus
from packages.core.models.workspace import Workspace


@pytest.fixture
def fs_enabled(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old = (settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT, settings.DEPLOYMENT_MODE)
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"
    # keep the re-embed trigger from touching a broker in tests
    monkeypatch.setattr(
        file_tools, "runtime_sync_entity_file_to_knowledge",
        file_tools.runtime_sync_entity_file_to_knowledge,
    )
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
