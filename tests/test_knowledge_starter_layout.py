from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.entity_fs import EntityFilesystemError
from packages.core.services.knowledge_starter import write_markdown_document


@pytest.fixture(autouse=True)
def entity_filesystem(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))


@pytest.mark.asyncio
async def test_workspace_starter_uses_workspace_root_and_basename(db_session, tmp_path):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Launch")
    db_session.add(workspace)
    await db_session.flush()

    document = await write_markdown_document(
        db_session,
        entity_id=entity_id,
        name="nested/Starter Brief",
        content="# Brief",
        source="workspace_setup",
        metadata={"origin": {"workspace_id": workspace_id}},
    )

    await db_session.refresh(workspace)
    assert document.name == "Starter Brief.md"
    assert document.folder_id == workspace.artifact_folder_id
    assert document.fs_path == f"Workspaces/_by_id/{workspace.artifact_folder_id}/Starter Brief.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Brief"

    folders = list((await db_session.scalars(
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
    )).all())
    assert {folder.name for folder in folders} == {"Workspaces", "Launch"}


@pytest.mark.asyncio
async def test_entity_starter_without_workspace_uses_entity_root(db_session, tmp_path):
    entity_id = generate_ulid()

    document = await write_markdown_document(
        db_session,
        entity_id=entity_id,
        name="Entity Brief",
        content="# Entity",
        source="workspace_setup",
        metadata={"origin": {}},
    )

    assert document.folder_id is None
    assert document.fs_path == "Entity Brief.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Entity"


@pytest.mark.asyncio
async def test_unknown_workspace_starter_never_falls_back_to_entity_root(db_session, tmp_path):
    entity_id = generate_ulid()
    filename = "Unknown Workspace.md"

    with pytest.raises(ValueError, match="Workspace"):
        await write_markdown_document(
            db_session,
            entity_id=entity_id,
            name=filename,
            content="# Invalid",
            source="workspace_setup",
            metadata={"origin": {"workspace_id": generate_ulid()}},
        )

    assert not (tmp_path / entity_id / filename).exists()
    assert (await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id)
    )).all() == []


def _storage_unavailable(*_args, **_kwargs):
    raise EntityFilesystemError("Entity filesystem root is not mounted: /mnt/manor")


@pytest.mark.parametrize("failure", ["filesystem_not_ready", "write_failed"])
@pytest.mark.asyncio
async def test_failed_write_leaves_no_row_without_content(db_session, monkeypatch, failure):
    from packages.core.services import entity_fs

    target = (
        "assert_entity_filesystem_ready" if failure == "filesystem_not_ready"
        else "write_entity_file_atomic"
    )
    monkeypatch.setattr(entity_fs, target, _storage_unavailable)
    entity_id = generate_ulid()

    with pytest.raises(EntityFilesystemError):
        await write_markdown_document(
            db_session,
            entity_id=entity_id,
            name="Lumen Brand, Products and Support.md",
            content="# Lumen",
            source="ai_generated",
            metadata={"origin": {}},
        )

    assert (await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id)
    )).all() == []


@pytest.mark.asyncio
async def test_refused_row_removes_the_file_it_would_have_pointed_at(
    db_session, monkeypatch, tmp_path,
):
    from packages.core.services import document_service

    async def over_limit(*_args, **_kwargs):
        raise RuntimeError("storage limit reached")

    monkeypatch.setattr(document_service, "_enforce_storage_limit", over_limit)
    entity_id = generate_ulid()

    with pytest.raises(RuntimeError, match="storage limit"):
        await write_markdown_document(
            db_session,
            entity_id=entity_id,
            name="Over Limit.md",
            content="# Over",
            source="ai_generated",
            metadata={"origin": {}},
        )

    assert not (tmp_path / entity_id / "Over Limit.md").exists()


@pytest.mark.asyncio
async def test_deployment_without_filesystem_keeps_the_whole_body_inline(db_session, monkeypatch):
    from packages.core.config import get_settings
    from packages.core.services.document_service import get_document_content

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", False)
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    body = "# Long\n" + "x" * 60_000

    document = await write_markdown_document(
        db_session,
        entity_id=generate_ulid(),
        name="Long",
        content=body,
        source="ai_generated",
        metadata={"origin": {}},
    )

    assert document.fs_path is None
    assert document.file_size == len(body)
    assert await get_document_content(db_session, document.id, document.entity_id) == body




@pytest.mark.parametrize(
    ("kwargs", "stored"),
    [
        ({}, False),
        ({"metadata": {"content_text": ""}}, False),
        ({"fs_path": "generated.md"}, True),
        ({"file_url": "https://example.com/generated.md"}, True),
        ({"metadata": {"content_text": "# Inline"}}, True),
    ],
    ids=["nothing", "empty_inline", "file", "url", "inline"],
)
@pytest.mark.asyncio
async def test_generated_document_row_needs_stored_content(db_session, kwargs, stored):
    from packages.core.services.document_service import create_document

    entity_id = generate_ulid()
    if stored:
        document = await create_document(
            db_session, entity_id, name="generated.md", source="ai_generated", **kwargs,
        )
        assert document.id
    else:
        with pytest.raises(ValueError, match="no stored content"):
            await create_document(
                db_session, entity_id, name="generated.md", source="ai_generated", **kwargs,
            )


class _StarterTaskHarness:
    """Run the real Celery task body at a chosen retry count, on the test's event loop."""

    def __init__(self, db_session, monkeypatch):
        import packages.core.ai.runtime as runtime
        import packages.core.database as database
        from packages.core.services import platform_faults
        from packages.core.tasks import ai_tasks
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        self.task = ai_tasks.generate_knowledge_content
        self.completions = 0
        self.retries: list[dict] = []
        self.faults: list[dict] = []

        async def completion(**_kwargs):
            self.completions += 1
            return SimpleNamespace(content="# Brand guide")

        async def report(**kwargs):
            self.faults.append(kwargs)
            return True

        def retry(**kwargs):
            self.retries.append(kwargs)
            raise Retry()

        monkeypatch.setattr(
            runtime, "runtime_execute_knowledge_starter_document_completion", completion,
        )
        monkeypatch.setattr(database, "create_worker_session", lambda: async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False,
        ))
        monkeypatch.setattr(platform_faults, "report_platform_fault", report)
        monkeypatch.setattr(ai_tasks.process_document_embeddings, "delay", lambda *_: None)
        monkeypatch.setattr(self.task, "retry", retry)
        self._monkeypatch = monkeypatch
        self._ai_tasks = ai_tasks

    async def run(self, retries: int, *args) -> None:
        loop = asyncio.get_running_loop()
        self._monkeypatch.setattr(
            self._ai_tasks, "_run_async",
            lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(),
        )

        def invoke() -> None:
            self.task.push_request(retries=retries)
            try:
                self.task.run(*args)
            finally:
                self.task.pop_request()

        await asyncio.to_thread(invoke)


async def _starter_group(db_session):
    from packages.core.models.document import DocumentGroup
    from packages.core.services.knowledge_starter import with_starter_document_settings

    entity_id, workspace_id, group_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Studio"))
    db_session.add(DocumentGroup(
        id=group_id, entity_id=entity_id, name="Brand",
        settings=with_starter_document_settings({}, group_name="Brand", status="scheduled"),
    ))
    await db_session.commit()
    return (
        entity_id, workspace_id, group_id, "Brand", "purpose", "Studio", "studio", "videos",
    )


@pytest.mark.asyncio
async def test_storage_outage_retries_the_starter_before_spending_on_the_model(
    db_session, monkeypatch,
):
    from packages.core.services import entity_fs

    harness = _StarterTaskHarness(db_session, monkeypatch)
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", _storage_unavailable)
    args = await _starter_group(db_session)

    with pytest.raises(Retry):
        await harness.run(2, *args)

    assert harness.completions == 0
    assert harness.retries[0]["countdown"] == 120
    assert harness.retries[0]["max_retries"] > 2
    assert harness.faults == []
    assert (await db_session.scalars(
        select(Document).where(Document.entity_id == args[0])
    )).all() == []


@pytest.mark.asyncio
async def test_spent_starter_retries_are_reported_and_release_the_group(
    db_session, monkeypatch,
):
    from packages.core.models.document import DocumentGroup
    from packages.core.services import entity_fs

    harness = _StarterTaskHarness(db_session, monkeypatch)
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", _storage_unavailable)
    args = await _starter_group(db_session)

    with pytest.raises(EntityFilesystemError):
        await harness.run(5, *args)

    db_session.expire_all()
    group = await db_session.get(DocumentGroup, args[2])
    assert group.settings["starter_document"]["status"] == "failed"
    assert harness.retries == []
    assert [fault["code"] for fault in harness.faults] == ["knowledge_starter_document_failed"]
    assert harness.faults[0]["target"] == f"workspace:{args[1]}"
    assert (await db_session.scalars(
        select(Document).where(Document.entity_id == args[0])
    )).all() == []


@pytest.mark.asyncio
async def test_generated_workspace_starter_stays_in_workspace_storage(
    db_session, monkeypatch, tmp_path,
):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.ai.runtime as runtime
    import packages.core.database as database
    from packages.core.models.document import DocumentGroup
    from packages.core.tasks import ai_tasks

    entity_id, workspace_id, group_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Studio"))
    db_session.add(DocumentGroup(id=group_id, entity_id=entity_id, name="Brand"))
    await db_session.commit()

    async def completion(**_kwargs):
        return SimpleNamespace(content="# Brand guide")

    pending = []
    monkeypatch.setattr(
        runtime, "runtime_execute_knowledge_starter_document_completion", completion,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    ))
    monkeypatch.setattr(ai_tasks, "_run_async", pending.append)
    monkeypatch.setattr(ai_tasks.process_document_embeddings, "delay", lambda *_: None)

    ai_tasks.generate_knowledge_content.run(
        entity_id, workspace_id, group_id, "Brand", "purpose",
        "Studio", "studio", "videos",
    )
    await pending[0]

    db_session.expire_all()
    document = (await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id)
    )).one()
    workspace = await db_session.get(Workspace, workspace_id)
    assert document.folder_id == workspace.artifact_folder_id
    assert document.fs_path == f"Workspaces/_by_id/{workspace.artifact_folder_id}/Brand.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Brand guide"
    assert not (tmp_path / entity_id / "Brand.md").exists()
