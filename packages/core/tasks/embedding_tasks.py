"""Background task for batch document embedding."""
from datetime import datetime, timedelta, timezone
import logging
import os

from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.celery_app import celery_app
from packages.core.models.document import Document, VectorStatus
from packages.core.services.embedding_service import EMBEDDING_PROCESSING_STALE_SECONDS

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


EMBEDDING_PENDING_STALE_SECONDS = _positive_int_env(
    "EMBEDDING_PENDING_STALE_SECONDS",
    120,
    minimum=30,
)
EMBEDDING_MAX_STALE_RECOVERIES = _positive_int_env(
    "EMBEDDING_MAX_STALE_RECOVERIES",
    3,
)
EMBEDDING_SWEEP_LIMIT = _positive_int_env("EMBEDDING_SWEEP_LIMIT", 50)
_WORKSPACE_DELETED_BLOCK_REASON = "workspace_deleted"


async def recover_stale_embedding_documents(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    processing_stale_seconds: int = EMBEDDING_PROCESSING_STALE_SECONDS,
    pending_stale_seconds: int = EMBEDDING_PENDING_STALE_SECONDS,
    max_recoveries: int = EMBEDDING_MAX_STALE_RECOVERIES,
    limit: int = EMBEDDING_SWEEP_LIMIT,
) -> dict[str, list[str]]:
    """Recover abandoned index runs and return document IDs to enqueue.

    ``metadata.indexing.heartbeat_epoch`` is the durable heartbeat, falling
    back to updated/created timestamps for legacy rows. Rows are locked with
    SKIP LOCKED so overlapping sweeps cannot recover or dispatch the same
    document. Recovery is bounded; exhausted rows become visibly failed.
    """

    checked_at = now or datetime.now(timezone.utc)
    processing_stale_before = checked_at - timedelta(seconds=max(1, processing_stale_seconds))
    pending_stale_before = checked_at - timedelta(seconds=max(1, pending_stale_seconds))
    pending_age = func.coalesce(Document.updated_at, Document.created_at)
    blocked_reason = Document.metadata_["indexing"]["blocked_reason"].astext
    heartbeat_epoch = Document.metadata_["indexing"]["heartbeat_epoch"]
    heartbeat_at = case(
        (
            func.jsonb_typeof(heartbeat_epoch) == "number",
            func.to_timestamp(cast(heartbeat_epoch.astext, Float)),
        ),
        else_=None,
    )
    processing_age = func.coalesce(heartbeat_at, Document.updated_at, Document.created_at)

    stale_result = await db.execute(
        select(Document)
        .where(
            Document.vector_status == VectorStatus.PROCESSING,
            Document.is_trashed.is_(False),
            processing_age < processing_stale_before,
        )
        .order_by(processing_age.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )

    requeued: list[str] = []
    failed: list[str] = []
    for doc in stale_result.scalars().all():
        metadata = dict(doc.metadata_ or {})
        indexing = dict(metadata.get("indexing") or {})
        recovery_attempts = int(indexing.get("recovery_attempts") or 0) + 1
        previous_run_id = indexing.get("run_id")
        indexing.update(
            {
                "recovery_attempts": recovery_attempts,
                "last_recovered_at": checked_at.isoformat(),
                "last_stale_run_id": previous_run_id,
                "error_code": "stale_heartbeat",
                "error": "Indexing worker heartbeat expired",
            }
        )
        indexing.pop("run_id", None)
        indexing.pop("heartbeat_at", None)
        indexing.pop("heartbeat_epoch", None)

        if recovery_attempts > max(0, max_recoveries):
            doc.vector_status = VectorStatus.FAILED
            indexing.update(
                {
                    "step": "failed",
                    "progress": 0,
                    "failed_at": checked_at.isoformat(),
                }
            )
            failed.append(doc.id)
        else:
            doc.vector_status = VectorStatus.PENDING
            indexing.update(
                {
                    "step": "queued",
                    "progress": 0,
                    "current_chunk": 0,
                    "queued_at": checked_at.isoformat(),
                }
            )
            requeued.append(doc.id)
        metadata["indexing"] = indexing
        doc.metadata_ = metadata

    await db.flush()

    # Pending rows are the durable fallback for dispatch failures. Wait briefly
    # before redispatching so a just-created upload has time to reach its normal
    # task, and stamp queued_at/updated_at to suppress duplicate sweep delivery.
    remaining = max(0, limit - len(requeued))
    if remaining:
        pending_result = await db.execute(
            select(Document)
            .where(
                Document.vector_status == VectorStatus.PENDING,
                Document.is_trashed.is_(False),
                func.coalesce(blocked_reason, "")
                != _WORKSPACE_DELETED_BLOCK_REASON,
                pending_age < pending_stale_before,
                Document.id.not_in(requeued) if requeued else True,
            )
            .order_by(pending_age.asc())
            .with_for_update(skip_locked=True)
            .limit(remaining)
        )
        for doc in pending_result.scalars().all():
            metadata = dict(doc.metadata_ or {})
            indexing = dict(metadata.get("indexing") or {})
            indexing.update(
                {
                    "step": "queued",
                    "progress": 0,
                    "current_chunk": 0,
                    "queued_at": checked_at.isoformat(),
                }
            )
            indexing.pop("run_id", None)
            indexing.pop("heartbeat_at", None)
            indexing.pop("heartbeat_epoch", None)
            metadata["indexing"] = indexing
            doc.metadata_ = metadata
            requeued.append(doc.id)

    await db.commit()
    return {"requeued": requeued, "failed": failed}


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
    """Recover stale processing rows and redispatch abandoned pending rows.

    The sweep never performs embeddings itself, keeping recovery short and
    bounded. Document-level claims in ``index_document`` make redispatch safe.
    """
    import asyncio
    from packages.core.database import create_worker_session

    async def _sweep():
        async with create_worker_session()() as db:
            return await recover_stale_embedding_documents(db)

    result = asyncio.run(_sweep())
    from packages.core.tasks.ai_tasks import process_document_embeddings

    dispatched = 0
    for doc_id in result["requeued"]:
        try:
            process_document_embeddings.delay(doc_id)
            dispatched += 1
        except Exception:
            # The row stays pending and becomes eligible again after the
            # pending-stale window if the broker was unavailable.
            logger.warning("sweep_pending: failed to dispatch %s", doc_id, exc_info=True)

    if dispatched or result["failed"]:
        logger.info(
            "sweep_pending: dispatched=%d terminal_failed=%d",
            dispatched,
            len(result["failed"]),
        )
    return {
        "requeued": len(result["requeued"]),
        "dispatched": dispatched,
        "failed": len(result["failed"]),
    }
