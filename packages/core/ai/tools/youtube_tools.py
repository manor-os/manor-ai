"""Narrow Workspace tools for public YouTube publication receipts and metrics."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
)
from packages.core.services.stickman_topic_ledger import (
    StickmanTopicLedgerError,
    list_saved_youtube_publications,
)
from packages.core.services.youtube_public_video import (
    YouTubePublicVideoError,
    fetch_youtube_public_metrics,
)
from packages.core.services.youtube_publication_receipts import (
    YouTubePublicationReceiptError,
    build_publication_receipt,
    read_publication_receipt,
    write_publication_receipt,
)


logger = logging.getLogger(__name__)




RECORD_YOUTUBE_PUBLICATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_youtube_publication",
        "description": (
            "Verify a public YouTube video against the completed Studio publish "
            "result and atomically save it as this Workspace's latest publication."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "public_url": {
                    "type": "string",
                    "description": "The public youtube.com/watch or youtu.be URL returned by Studio.",
                },
                "expected_title": {
                    "type": "string",
                    "description": "The final title verified in YouTube Studio.",
                },
                "expected_video_id": {
                    "type": "string",
                    "description": "The exact video ID returned by the verified YouTube Studio upload.",
                },
                "expected_channel_name": {
                    "type": "string",
                    "description": "Optional channel name only when it was independently verified in Studio; otherwise the authoritative public page value is recorded.",
                },
                "source_kind": {
                    "type": "string",
                    "enum": ["workspace_publication", "verified_backfill"],
                    "description": (
                        "Use workspace_publication after this Task publishes; use "
                        "verified_backfill only to register an already published video."
                    ),
                },
            },
            "required": [
                "public_url",
                "expected_title",
                "expected_video_id",
                "source_kind",
            ],
        },
    },
}

READ_YOUTUBE_PUBLIC_METRICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_youtube_public_metrics",
        "description": (
            "Read real public metrics for the video in this Workspace's latest "
            "verified YouTube publication receipt. Does not use Chrome or login state."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

RECORD_YOUTUBE_WORKSPACE_METRICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record_youtube_workspace_metrics",
        "description": (
            "Read the latest verified public YouTube video and append its real "
            "public metrics plus the Workspace's published-video count to the "
            "preconfigured Workspace Metrics. Does not use Chrome or login state."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

YOUTUBE_WORKSPACE_STAT_KEYS = {
    "published_videos": "youtube.workspace_published_videos",
    "views": "youtube.latest_video_views",
    "subscribers": "youtube.channel_subscribers",
    "likes": "youtube.latest_video_likes",
    "comments": "youtube.latest_video_comments",
}



def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _context_error(entity_id: str, workspace_id: str | None) -> str | None:
    if entity_id and workspace_id:
        return None
    return _result(
        {
            "ok": False,
            "error": {
                "code": "missing_workspace_context",
                "message": "Entity and Workspace context are required",
            },
        }
    )


def _service_error(exc: Exception) -> str:
    return _result(
        {
            "ok": False,
            "error": {
                "code": str(getattr(exc, "code", "youtube_public_error")),
                "message": str(exc),
            },
        }
    )




def _verified_publication_count(
    publications: list[dict[str, Any]],
    *,
    current_video_id: str,
) -> int:
    identities = {
        str(item.get("video_id") or item.get("watch_url") or "").strip()
        for item in publications
    }
    identities.discard("")
    if current_video_id:
        identities.add(current_video_id)
    return len(identities)


async def _record_youtube_publication(
    entity_id: str = "",
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    workspace_id = context.workspace_id
    if error := _context_error(entity_id, workspace_id):
        return error

    public_url = str(kwargs.get("public_url") or "").strip()
    expected_title = str(kwargs.get("expected_title") or "").strip()
    expected_video_id = str(kwargs.get("expected_video_id") or "").strip()
    expected_channel_name = str(
        kwargs.get("expected_channel_name") or ""
    ).strip()
    source_kind = str(kwargs.get("source_kind") or "").strip()
    if not public_url or not expected_title or not expected_video_id:
        return _service_error(
            YouTubePublicationReceiptError(
                "invalid_input",
                "public_url, expected_title, and expected_video_id are required",
            )
        )
    if source_kind not in {"workspace_publication", "verified_backfill"}:
        return _service_error(
            YouTubePublicationReceiptError(
                "invalid_source_kind",
                "source_kind must be workspace_publication or verified_backfill",
            )
        )

    try:
        metrics = await fetch_youtube_public_metrics(public_url)
        source_task_id = (
            context.task_id if source_kind == "workspace_publication" else None
        )
        receipt = build_publication_receipt(
            metrics=metrics,
            expected_title=expected_title,
            expected_video_id=expected_video_id,
            expected_channel_name=expected_channel_name,
            source_kind=source_kind,
            source_task_id=source_task_id,
            source_workflow_run_id=context.workflow_run_id,
        )
        written = await write_publication_receipt(
            entity_id=entity_id,
            workspace_id=str(workspace_id),
            receipt=receipt,
            agent_id=context.agent_id,
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
        )
    except (YouTubePublicVideoError, YouTubePublicationReceiptError) as exc:
        return _service_error(exc)
    except Exception as exc:
        logger.exception("Failed to record YouTube publication")
        return _service_error(
            YouTubePublicationReceiptError(
                "receipt_write_failed",
                f"YouTube publication receipt could not be recorded: {exc}",
            )
        )

    return _result(
        {
            "ok": True,
            "receipt_status": "recorded",
            "video_id": receipt["video_id"],
            "public_url": receipt["public_url"],
            "title": receipt["title"],
            "channel_name": receipt["channel_name"],
            "source_kind": receipt["source_kind"],
            "path": written["path"],
            "display_path": written["display_path"],
            "document_id": written.get("document_id"),
        }
    )


async def _read_youtube_public_metrics(
    entity_id: str = "",
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    workspace_id = context.workspace_id
    if error := _context_error(entity_id, workspace_id):
        return error

    try:
        receipt = await read_publication_receipt(
            entity_id=entity_id,
            workspace_id=str(workspace_id),
        )
        metrics = await fetch_youtube_public_metrics(
            receipt["public_url"],
            expected_video_id=receipt["video_id"],
        )
    except (YouTubePublicVideoError, YouTubePublicationReceiptError) as exc:
        return _service_error(exc)
    except Exception as exc:
        logger.exception("Failed to read public YouTube metrics")
        return _service_error(
            YouTubePublicVideoError(
                "metrics_read_failed",
                f"YouTube public metrics could not be read: {exc}",
            )
        )

    return _result(
        {
            "ok": True,
            **asdict(metrics),
            "source_task_id": receipt.get("source_task_id"),
            "timezone": "UTC",
        }
    )


async def _record_workspace_metric_observations(
    db: Any,
    *,
    entity_id: str,
    workspace_id: str,
    receipt: dict[str, Any],
    metrics: Any,
    published_video_count: int,
) -> list[str]:
    """Append one daily public-metrics snapshot to configured Workspace Stats."""

    from packages.core.stats import service as stat_service

    stats = {
        stat.key: stat
        for stat in await stat_service.list_stats(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
    }
    missing = sorted(set(YOUTUBE_WORKSPACE_STAT_KEYS.values()) - set(stats))
    if missing:
        raise YouTubePublicVideoError(
            "workspace_metrics_not_configured",
            "Required Workspace Metrics are missing: " + ", ".join(missing),
        )

    try:
        observed_at = datetime.fromisoformat(str(metrics.collected_at))
    except (TypeError, ValueError) as exc:
        raise YouTubePublicVideoError(
            "invalid_collection_time",
            "YouTube public metrics returned an invalid collection time",
        ) from exc
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)

    values = {
        "published_videos": published_video_count,
        "views": metrics.views,
        "subscribers": metrics.subscribers,
        "likes": metrics.likes,
        "comments": metrics.comments,
    }
    evidence = {
        "provider": "youtube_public_watch_page",
        "video_id": metrics.video_id,
        "public_url": metrics.public_url,
        "title": metrics.title,
        "channel_id": metrics.channel_id,
        "channel_name": metrics.channel_name,
        "publication_receipt_source_task_id": receipt.get("source_task_id"),
        "collected_at": metrics.collected_at,
    }
    recorded: list[str] = []
    day_key = observed_at.astimezone(timezone.utc).date().isoformat()
    for metric_name, value in values.items():
        if value is None:
            continue
        stat_key = YOUTUBE_WORKSPACE_STAT_KEYS[metric_name]
        # A snapshot is idempotent for one public video on one UTC day.  Using
        # only the Workspace for lifetime/channel metrics would suppress a
        # second legitimate publication on the same day, leaving the
        # published-video count and subscriber snapshot stale.
        video_scope = metrics.video_id
        await stat_service.record_observation(
            db,
            stats[stat_key],
            value=value,
            source="youtube_public_metrics",
            observed_at=observed_at,
            evidence={**evidence, "metric": metric_name},
            idempotency_key=(
                f"youtube-public:{stat_key}:{day_key}:{video_scope}"
            ),
        )
        recorded.append(stat_key)
    return recorded


async def _record_youtube_workspace_metrics(
    entity_id: str = "",
    **kwargs: Any,
) -> str:
    context = runtime_tool_call_context_from_kwargs(kwargs)
    workspace_id = context.workspace_id
    if error := _context_error(entity_id, workspace_id):
        return error

    try:
        receipt = await read_publication_receipt(
            entity_id=entity_id,
            workspace_id=str(workspace_id),
        )
        metrics = await fetch_youtube_public_metrics(
            receipt["public_url"],
            expected_video_id=receipt["video_id"],
        )
        publications = await list_saved_youtube_publications(
            entity_id=entity_id,
            workspace_id=str(workspace_id),
        )
        # A verified backfill receipt is durable Workspace evidence too. This
        # also makes the current public video countable when it predates the
        # append-only Topic Ledger introduced by a later Blueprint version.
        published_video_count = _verified_publication_count(
            publications,
            current_video_id=metrics.video_id,
        )

        from packages.core.database import async_session

        async with async_session() as db:
            recorded = await _record_workspace_metric_observations(
                db,
                entity_id=entity_id,
                workspace_id=str(workspace_id),
                receipt=receipt,
                metrics=metrics,
                published_video_count=published_video_count,
            )
            await db.commit()
    except (
        StickmanTopicLedgerError,
        YouTubePublicVideoError,
        YouTubePublicationReceiptError,
    ) as exc:
        return _service_error(exc)
    except Exception as exc:
        logger.exception("Failed to record public YouTube Workspace Metrics")
        return _service_error(
            YouTubePublicVideoError(
                "workspace_metrics_record_failed",
                f"YouTube Workspace Metrics could not be recorded: {exc}",
            )
        )

    return _result(
        {
            "ok": True,
            "video_id": metrics.video_id,
            "public_url": metrics.public_url,
            "title": metrics.title,
            "published_video_count": published_video_count,
            "views": metrics.views,
            "subscribers": metrics.subscribers,
            "likes": metrics.likes,
            "comments": metrics.comments,
            "unavailable_fields": list(metrics.unavailable_fields),
            "collected_at": metrics.collected_at,
            "timezone": "UTC",
            "recorded_workspace_metrics": recorded,
            "metrics_location": "Workspace Overview > Metrics",
        }
    )


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [
        (RECORD_YOUTUBE_PUBLICATION_SCHEMA, _record_youtube_publication),
        (READ_YOUTUBE_PUBLIC_METRICS_SCHEMA, _read_youtube_public_metrics),
        (
            RECORD_YOUTUBE_WORKSPACE_METRICS_SCHEMA,
            _record_youtube_workspace_metrics,
        ),
    ]
