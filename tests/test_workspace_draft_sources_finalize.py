"""Finalize carries draft sources into knowledge nets and grounds starter docs."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid


def _session(entity_id, sources, attachment):
    from packages.core.services.workspace_setup_service import WorkspaceSetupSession

    return WorkspaceSetupSession(
        entity_id=entity_id,
        fields={
            "name": "Method Workspace",
            "kind": "project",
            "operating_context": "ctx",
            "primary_work": "work",
            "services": [],
            "agent_mappings": [],
            "goals": [],
            "knowledge_attachments": [attachment],
            "channel_config": {},
            "sources": sources,
        },
        messages=[],
        ready=True,
        missing=[],
    )


@pytest.mark.asyncio
async def test_finalize_adds_document_sources_and_writes_link_sources(db_session, monkeypatch):
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services import knowledge_starter
    from packages.core.services.workspace_setup_service import finalize_setup

    entity_id = generate_ulid()
    doc_id = generate_ulid()
    db_session.add(Document(id=doc_id, entity_id=entity_id, name="chapter7.docx", file_type="docx", vector_status="ready", source="upload"))
    await db_session.flush()

    written: list[dict] = []

    async def fake_write(db, *, entity_id, name, content, source, metadata):
        written.append({"name": name, "content": content, "source": source})
        doc = Document(id=generate_ulid(), entity_id=entity_id, name=name, file_type="md", vector_status="pending", source=source, metadata_=metadata)
        db.add(doc)
        await db.flush()
        return doc

    monkeypatch.setattr(knowledge_starter, "write_markdown_document", fake_write)

    sources = [
        {"id": "S1", "kind": "document", "name": "chapter7.docx", "document_id": doc_id, "status": "ready", "chars": 5, "text": "第7章 拆开工作", "turn_index": 0},
        {"id": "S2", "kind": "link", "name": "https://example.com/method", "url": "https://example.com/method", "status": "ready", "chars": 4, "text": "page", "turn_index": 0},
    ]
    session = _session(entity_id, sources, {
        "name": "Method KB", "purpose": "the method", "mode": "create_new",
        "generate_starter_doc": True, "source_ids": ["S1", "S2"],
    })

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()

    group = (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id))).scalar_one()
    member_ids = set((await db_session.execute(select(DocumentGroupMember.document_id).where(DocumentGroupMember.group_id == group.id))).scalars().all())
    assert doc_id in member_ids
    assert len(member_ids) == 2  # document source + link markdown doc
    assert written and written[0]["source"] == "workspace_draft_link"
    assert "page" in written[0]["content"] and "https://example.com/method" in written[0]["content"]
    starter = group.settings["starter_document"]
    assert starter["status"] == "scheduled"
    assert starter["source_excerpt"].startswith("## chapter7.docx\n第7章")
    assert "## https://example.com/method\npage" in starter["source_excerpt"]


@pytest.mark.asyncio
async def test_unstored_link_source_leaves_no_empty_document_and_reaches_ops(db_session, monkeypatch, tmp_path):
    from packages.core.config import get_settings
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.workspace import Workspace
    from packages.core.services import entity_fs, platform_faults
    from packages.core.services.workspace_setup_service import dispatch_workspace_post_commit, finalize_setup
    from packages.core.tasks import ai_tasks

    def unavailable():
        raise entity_fs.EntityFilesystemError("Entity filesystem root is not mounted: /mnt/manor")

    faults: list[dict] = []

    async def report(**kwargs):
        faults.append(kwargs)
        return True

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", unavailable)
    monkeypatch.setattr(platform_faults, "report_platform_fault", report)
    monkeypatch.setattr(ai_tasks.generate_knowledge_content, "delay", lambda *_: None)

    entity_id = generate_ulid()
    session = _session(entity_id, [
        {"id": "S1", "kind": "link", "name": "https://example.com/method", "url": "https://example.com/method", "status": "ready", "chars": 4, "text": "page", "turn_index": 0},
    ], {"name": "Method KB", "purpose": "the method", "mode": "create_new", "generate_starter_doc": True, "source_ids": ["S1"]})

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()

    group = (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id))).scalar_one()
    assert group.settings["unstored_source_ids"] == ["S1"]
    assert group.settings["starter_document"]["source_excerpt"].endswith("page")
    assert (await db_session.execute(select(DocumentGroupMember).where(DocumentGroupMember.group_id == group.id))).scalars().all() == []
    assert (await db_session.execute(select(Document).where(Document.entity_id == entity_id))).scalars().all() == []

    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = "active"
    await db_session.commit()
    await dispatch_workspace_post_commit(db_session, workspace_id=workspace_id, entity_id=entity_id)

    assert [fault["code"] for fault in faults] == ["knowledge_draft_source_not_stored"]
    assert faults[0]["context"]["source_ids"] == ["S1"]


@pytest.mark.asyncio
async def test_post_commit_dispatch_passes_source_excerpt_positionally(db_session, monkeypatch):
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_setup_service import dispatch_workspace_post_commit, finalize_setup
    from packages.core.tasks import ai_tasks

    dispatched: list[tuple] = []
    monkeypatch.setattr(ai_tasks.generate_knowledge_content, "delay", lambda *args: dispatched.append(args))

    entity_id = generate_ulid()
    session = _session(entity_id, [
        {"id": "S1", "kind": "document", "name": "n", "status": "ready", "chars": 3, "text": "abc", "turn_index": 0},
    ], {"name": "KB", "purpose": "p", "mode": "create_new", "generate_starter_doc": True, "source_ids": ["S1"]})
    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    # A workspace without services finalizes as needs_setup, which skips post-commit
    # dispatch by design; force it active the way the other dispatch tests do.
    workspace.status = "active"
    await db_session.commit()

    await dispatch_workspace_post_commit(
        db_session, workspace_id=workspace.id, entity_id=workspace.entity_id,
    )

    starter_calls = [args for args in dispatched if len(args) >= 10]
    assert starter_calls, dispatched
    assert starter_calls[0][9] == "## n\nabc"
