"""Background translation of a Workspace's authored text (see services/workspace_translation)."""
import asyncio
import logging

from packages.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="workspace.translate_text", max_retries=0)
def translate_workspace_text_task(workspace_id: str, rerun: bool = False):
    """Translate what changed. A pass already running for the Workspace is followed once more."""
    from packages.core.services.workspace_translation import DISPATCH_DELAY_SECONDS, translate_workspace_text_once

    try:
        result = asyncio.run(translate_workspace_text_once(workspace_id))
    except Exception:
        # Viewers read the authored text until the next edit translates it.
        logger.warning("workspace translation failed for %s", workspace_id, exc_info=True)
        return {"workspace_id": workspace_id, "status": "failed"}
    if result is None and not rerun:
        translate_workspace_text_task.apply_async((workspace_id, True), countdown=DISPATCH_DELAY_SECONDS * 3)
    return {"workspace_id": workspace_id, **(result or {"status": "deferred"})}
