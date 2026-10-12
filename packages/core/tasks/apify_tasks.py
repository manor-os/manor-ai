"""Recover existing Instagram collections without starting new provider work."""
from __future__ import annotations

import asyncio

from packages.core.celery_app import celery_app


@celery_app.task(name="apify.reconcile_collections", ignore_result=True)
def reconcile_collections() -> dict:
    from packages.core.services.apify_collections import reconcile_pending_collections

    return asyncio.run(reconcile_pending_collections(limit=25))
