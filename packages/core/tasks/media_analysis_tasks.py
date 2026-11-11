"""Background analysis of uploaded Knowledge media (see services/knowledge_media_analysis)."""
import logging

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)


@celery_app.task(name="knowledge.analyze_media", max_retries=0)
def analyze_document_media_task(document_id: str):
    """Describe one uploaded image/video/audio file. Claims make redelivery safe."""
    from packages.core.services.knowledge_media_analysis import analyze_document_media

    status = run_in_worker(analyze_document_media(document_id))
    return {"document_id": document_id, "status": status}


@celery_app.task(name="knowledge.sweep_media_analysis", max_retries=0)
def sweep_media_analysis():
    """Re-dispatch analyses whose dispatch was lost, whose retry is due, or whose worker died."""
    from packages.core.database import create_worker_session
    from packages.core.services.knowledge_media_analysis import pending_media_analysis_ids

    async def _due():
        async with create_worker_session()() as db:
            return await pending_media_analysis_ids(db)

    due = run_in_worker(_due())
    dispatched = 0
    for document_id in due:
        try:
            analyze_document_media_task.delay(document_id)
            dispatched += 1
        except Exception:
            logger.warning("media analysis sweep: dispatch failed for %s", document_id, exc_info=True)
    if dispatched:
        logger.info("media analysis sweep: dispatched=%d", dispatched)
    return {"due": len(due), "dispatched": dispatched}
