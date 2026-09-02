"""Regression coverage for embedding task ownership and zombie recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, VectorStatus


def test_embedding_task_and_recovery_schedule_cover_long_local_indexes():
    from packages.core.celery_app import celery_app
    from packages.core.queues import CeleryQueue, TASK_QUEUES
    from packages.core.tasks.ai_tasks import process_document_embeddings

    assert process_document_embeddings.soft_time_limit == 6_900
    assert process_document_embeddings.time_limit == 7_200
    assert process_document_embeddings.time_limit > process_document_embeddings.soft_time_limit
    assert celery_app.conf.beat_schedule["embedding-sweep-pending"]["schedule"] == 60.0
    assert TASK_QUEUES["embeddings.sweep_pending"] == CeleryQueue.HEAVY
    assert TASK_QUEUES["packages.core.tasks.ai_tasks.process_document_embeddings"] == CeleryQueue.HEAVY


@pytest.mark.asyncio
async def test_fresh_processing_document_skips_duplicate_delivery(db_session, monkeypatch):
    from packages.core.services import embedding_service

    now = datetime.now(timezone.utc)
    doc = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="duplicate.md",
        vector_status=VectorStatus.PROCESSING,
        updated_at=now,
        metadata_={
            "indexing": {
                "run_id": "existing-run",
                "step": "embedding",
                "heartbeat_at": now.isoformat(),
            }
        },
    )
    db_session.add(doc)
    await db_session.commit()

    resolver = AsyncMock()
    monkeypatch.setattr(embedding_service, "_resolve_embedding_config", resolver)

    assert await embedding_service.index_document(db_session, doc.id) is True
    resolver.assert_not_awaited()

    await db_session.refresh(doc)
    assert doc.vector_status == VectorStatus.PROCESSING
    assert doc.metadata_["indexing"]["run_id"] == "existing-run"


@pytest.mark.asyncio
async def test_duplicate_delivery_cannot_mark_active_missing_file_as_skipped(
    db_session,
    monkeypatch,
):
    from packages.core.services import embedding_service

    now = datetime.now(timezone.utc)
    doc = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="active.pdf",
        fs_path="missing/active.pdf",
        vector_status=VectorStatus.PROCESSING,
        updated_at=now,
        metadata_={
            "indexing": {
                "run_id": "active-run",
                "step": "embedding",
                "heartbeat_epoch": now.timestamp(),
            }
        },
    )
    db_session.add(doc)
    await db_session.commit()

    file_check = AsyncMock()
    monkeypatch.setattr(embedding_service, "_resolve_embedding_config", file_check)

    assert await embedding_service.index_document(db_session, doc.id) is True
    file_check.assert_not_awaited()
    await db_session.refresh(doc)
    assert doc.vector_status == VectorStatus.PROCESSING
    assert doc.metadata_["indexing"]["run_id"] == "active-run"


@pytest.mark.asyncio
async def test_delayed_queue_delivery_does_not_reindex_ready_document(db_session, monkeypatch):
    from packages.core.services import embedding_service

    doc = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="already-ready.md",
        vector_status=VectorStatus.READY,
    )
    db_session.add(doc)
    await db_session.commit()

    resolver = AsyncMock()
    monkeypatch.setattr(embedding_service, "_resolve_embedding_config", resolver)

    assert await embedding_service.index_document(db_session, doc.id, allow_ready=False) is True
    resolver.assert_not_awaited()
    await db_session.refresh(doc)
    assert doc.vector_status == VectorStatus.READY


@pytest.mark.asyncio
async def test_lost_indexing_run_cannot_overwrite_cancelled_document(db_session):
    from packages.core.services.embedding_service import (
        _IndexingClaimLost,
        _update_document_indexing_progress,
    )

    now = datetime.now(timezone.utc)
    doc = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="cancelled.md",
        vector_status=VectorStatus.PROCESSING,
        updated_at=now,
        metadata_={
            "indexing": {
                "run_id": "old-run",
                "step": "embedding",
                "heartbeat_at": now.isoformat(),
            }
        },
    )
    db_session.add(doc)
    await db_session.commit()

    original_updated_at = doc.updated_at
    await _update_document_indexing_progress(
        db_session,
        doc.id,
        "old-run",
        step="embedding",
        progress=60,
        total_chunks=2,
        current_chunk=1,
    )
    await db_session.refresh(doc)
    assert doc.updated_at == original_updated_at
    assert isinstance(doc.metadata_["indexing"]["heartbeat_epoch"], (int, float))

    doc.vector_status = VectorStatus.SKIPPED
    await db_session.commit()

    with pytest.raises(_IndexingClaimLost):
        await _update_document_indexing_progress(
            db_session,
            doc.id,
            "old-run",
            step="storing",
            progress=95,
            total_chunks=1,
            current_chunk=1,
        )

    await db_session.refresh(doc)
    assert doc.vector_status == VectorStatus.SKIPPED


@pytest.mark.asyncio
async def test_recovery_sweep_requeues_stale_rows_and_bounds_retries(db_session):
    from packages.core.tasks.embedding_tasks import recover_stale_embedding_documents

    now = datetime.now(timezone.utc)
    stale_at = now - timedelta(minutes=20)
    fresh_at = now - timedelta(seconds=30)

    stale = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="stale.md",
        vector_status=VectorStatus.PROCESSING,
        # Heartbeat, not content-version updated_at, determines liveness.
        updated_at=fresh_at,
        metadata_={
            "indexing": {
                "run_id": "lost-run",
                "recovery_attempts": 0,
                "heartbeat_epoch": stale_at.timestamp(),
            }
        },
    )
    exhausted = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="exhausted.md",
        vector_status=VectorStatus.PROCESSING,
        updated_at=stale_at,
        metadata_={"indexing": {"run_id": "lost-again", "recovery_attempts": 3}},
    )
    pending = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="undispatched.md",
        vector_status=VectorStatus.PENDING,
        updated_at=stale_at,
    )
    workspace_deleted = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="deleted-workspace.md",
        vector_status=VectorStatus.PENDING,
        updated_at=stale_at,
        metadata_={
            "indexing": {
                "step": "blocked",
                "blocked_reason": "workspace_deleted",
            }
        },
    )
    fresh = Document(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="live.md",
        vector_status=VectorStatus.PROCESSING,
        updated_at=stale_at,
        metadata_={
            "indexing": {
                "run_id": "live-run",
                "recovery_attempts": 0,
                "heartbeat_epoch": fresh_at.timestamp(),
            }
        },
    )
    db_session.add_all([stale, exhausted, pending, workspace_deleted, fresh])
    await db_session.commit()

    result = await recover_stale_embedding_documents(
        db_session,
        now=now,
        processing_stale_seconds=300,
        pending_stale_seconds=120,
        max_recoveries=3,
        limit=10,
    )

    assert set(result["requeued"]) == {stale.id, pending.id}
    assert result["failed"] == [exhausted.id]

    for doc in (stale, exhausted, pending, workspace_deleted, fresh):
        await db_session.refresh(doc)

    assert stale.vector_status == VectorStatus.PENDING
    assert stale.metadata_["indexing"]["recovery_attempts"] == 1
    assert stale.metadata_["indexing"]["step"] == "queued"
    assert "run_id" not in stale.metadata_["indexing"]
    assert pending.vector_status == VectorStatus.PENDING
    assert pending.metadata_["indexing"]["step"] == "queued"
    assert workspace_deleted.vector_status == VectorStatus.PENDING
    assert workspace_deleted.metadata_["indexing"]["step"] == "blocked"
    assert exhausted.vector_status == VectorStatus.FAILED
    assert exhausted.metadata_["indexing"]["error_code"] == "stale_heartbeat"
    assert exhausted.metadata_["indexing"]["recovery_attempts"] == 4
    assert fresh.vector_status == VectorStatus.PROCESSING
    assert fresh.metadata_["indexing"]["run_id"] == "live-run"

    immediate_repeat = await recover_stale_embedding_documents(
        db_session,
        now=now + timedelta(seconds=30),
        processing_stale_seconds=300,
        pending_stale_seconds=120,
        max_recoveries=3,
        limit=10,
    )
    assert immediate_repeat == {"requeued": [], "failed": []}


@pytest.mark.asyncio
async def test_ollama_batch_refreshes_progress_after_each_chunk(monkeypatch):
    from packages.core.services import embedding_service

    monkeypatch.setattr(
        embedding_service,
        "_resolve_embedding_config",
        AsyncMock(
            return_value={
                "api_key": "ollama",
                "base_url": "http://ollama:11434/v1",
                "model": "mxbai-embed-large",
                "dimensions": 3,
            }
        ),
    )
    monkeypatch.setattr(
        embedding_service,
        "generate_embedding",
        AsyncMock(side_effect=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
    )
    monkeypatch.setattr(
        embedding_service.cache,
        "get_many",
        AsyncMock(return_value=[None, None, None]),
    )
    monkeypatch.setattr(embedding_service.cache, "set_many", AsyncMock(return_value=True))
    progress = AsyncMock()

    result = await embedding_service.generate_embeddings_batch(
        ["one", "two", "three"],
        on_progress=progress,
    )

    assert len(result) == 3
    assert [call.args[0] for call in progress.await_args_list] == [1, 2, 3]


@pytest.mark.asyncio
async def test_embedding_chunk_cache_resumes_without_recomputing_hits(monkeypatch):
    from packages.core.services import embedding_service

    monkeypatch.setattr(
        embedding_service,
        "_resolve_embedding_config",
        AsyncMock(
            return_value={
                "api_key": "ollama",
                "base_url": "http://ollama:11434/v1",
                "model": "mxbai-embed-large",
                "dimensions": 3,
            }
        ),
    )
    generate = AsyncMock(return_value=[0.0, 1.0, 0.0])
    monkeypatch.setattr(embedding_service, "generate_embedding", generate)
    cache_get = AsyncMock(return_value=[[1.0, 0.0, 0.0], None])
    cache_set = AsyncMock(return_value=True)
    monkeypatch.setattr(embedding_service.cache, "get_many", cache_get)
    monkeypatch.setattr(embedding_service.cache, "set_many", cache_set)
    progress = AsyncMock()

    result = await embedding_service.generate_embeddings_batch(
        ["cached secret text", "new secret text"],
        cache_namespace="entity-1",
        on_progress=progress,
    )

    assert result == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    generate.assert_awaited_once_with("new secret text", model=None)
    assert [call.args[0] for call in progress.await_args_list] == [1, 2]
    cache_keys = cache_get.await_args.args[0]
    assert all("secret text" not in key for key in cache_keys)
    cache_set.assert_awaited_once()
