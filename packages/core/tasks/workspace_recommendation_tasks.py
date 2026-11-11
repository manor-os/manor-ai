"""Background refresh of the Workspaces suggested to one person."""
from __future__ import annotations

import logging

from packages.core.celery_app import celery_app
from packages.core.tasks._runtime import run_in_worker

logger = logging.getLogger(__name__)


@celery_app.task(name="workspace.refresh_recommendations", max_retries=0)
def refresh_workspace_recommendations_task(user_id: str):
    from packages.core.services.workspace_recommendations import refresh_workspace_recommendations

    try:
        return {"user_id": user_id, **run_in_worker(refresh_workspace_recommendations(user_id))}
    except Exception:
        # The page keeps its local suggestions until the next refresh.
        logger.warning("workspace recommendations failed for %s", user_id, exc_info=True)
        return {"user_id": user_id, "status": "failed"}
