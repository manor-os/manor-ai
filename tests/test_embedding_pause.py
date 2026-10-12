"""Regression coverage for the temporary system-wide embedding pause."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def test_document_embedding_delay_does_not_publish(monkeypatch):
    from celery.app.task import Task

    from packages.core.tasks.ai_tasks import process_document_embeddings

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("embedding task must not be published")

    monkeypatch.setattr(Task, "apply_async", unexpected_publish)

    result = process_document_embeddings.delay("doc-1")

    assert result.successful()
    assert result.get() == {"document_id": "doc-1", "status": "disabled"}


def test_queued_document_embedding_is_acknowledged_without_indexing(monkeypatch):
    from packages.core.services import embedding_service
    from packages.core.tasks.ai_tasks import process_document_embeddings

    index_document = AsyncMock(
        side_effect=AssertionError("queued embedding must not be indexed")
    )
    monkeypatch.setattr(embedding_service, "index_document", index_document)

    assert process_document_embeddings.run("doc-1") == {
        "document_id": "doc-1",
        "status": "disabled",
    }
    index_document.assert_not_awaited()


def test_embedding_sweep_is_not_periodically_published():
    from packages.core.celery_app import celery_app

    assert "embedding-sweep-pending" not in celery_app.conf.beat_schedule


@pytest.mark.asyncio
async def test_embedding_generation_returns_empty_without_provider_resolution(monkeypatch):
    from packages.core.services import embedding_service

    resolve_config = AsyncMock(
        side_effect=AssertionError("embedding provider must not be resolved")
    )
    monkeypatch.setattr(
        embedding_service,
        "_resolve_embedding_config",
        resolve_config,
    )

    assert await embedding_service.generate_embedding("hello") == []
    assert await embedding_service.generate_embeddings_batch(["hello", "world"]) == []
    resolve_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_config_resolution_skips_provider_discovery(monkeypatch):
    from packages.core.services import embedding_service

    resolve_billing = AsyncMock(
        side_effect=AssertionError("embedding billing route must not be resolved")
    )
    monkeypatch.setattr(
        embedding_service,
        "_resolve_embedding_config_from_billing_context",
        resolve_billing,
    )

    assert await embedding_service._resolve_embedding_config() is None
    resolve_billing.assert_not_awaited()


class _UnexpectedDatabase:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("paused document indexing must not access the database")


@pytest.mark.asyncio
async def test_document_indexing_entrypoints_are_successful_noops():
    from packages.core.services.embedding_service import (
        index_document,
        index_documents_for_entity,
    )

    db = _UnexpectedDatabase()

    assert await index_document(db, "doc-1") is True
    assert await index_documents_for_entity(db, "entity-1") == 0
