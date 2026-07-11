"""Background task for batch document embedding."""
import logging

from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="embeddings.batch_index")
def batch_index_entity(self, entity_id: str):
    """Index all pending documents for an entity."""
    import asyncio
    from packages.core.database import create_worker_session
    from packages.core.services.embedding_service import index_documents_for_entity

    async def _run():
        async with create_worker_session()() as db:
            count = await index_documents_for_entity(db, entity_id)
            await db.commit()
            return count

    count = asyncio.run(_run())
    return {"entity_id": entity_id, "indexed": count}


@celery_app.task(bind=True, name="embeddings.sweep_pending", max_retries=0)
def sweep_pending_documents(self):
    """Periodic sweep: index any documents stuck at 'pending'.

    Catches documents whose Celery task dispatch was lost (e.g. broker
    unreachable at upload time, worker restart, etc).
    """
    import asyncio
    from packages.core.database import create_worker_session
    from packages.core.services.embedding_service import index_document
    from sqlalchemy import select
    from packages.core.models.document import Document

    async def _sweep():
        async with create_worker_session()() as db:
            result = await db.execute(
                select(Document.id).where(
                    Document.vector_status == "pending",
                    Document.is_trashed == False,  # noqa: E712
                ).limit(50)
            )
            doc_ids = [row[0] for row in result.all()]
            if not doc_ids:
                return 0
            count = 0
            for doc_id in doc_ids:
                try:
                    ok = await index_document(db, doc_id)
                    await db.commit()
                    if ok:
                        count += 1
                except Exception:
                    logger.warning("sweep_pending: failed to index %s", doc_id, exc_info=True)
                    await db.rollback()
            return count

    indexed = asyncio.run(_sweep())
    if indexed:
        logger.info("sweep_pending: indexed %d stuck documents", indexed)
    return {"indexed": indexed}
