"""Background tasks for async media generation (video, image)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from packages.core.tasks import video_adapters

try:
    from packages.core.celery_app import celery_app
    from packages.core.tasks._runtime import run_in_worker as _run_async
except ModuleNotFoundError as exc:  # pragma: no cover - local lightweight test env
    if exc.name != "celery":
        raise

    class _NoopCelery:
        def task(self, *args, **kwargs):
            def _decorator(fn):
                def _delay(*_args, **_kwargs):
                    raise RuntimeError("Celery is not installed")
                fn.delay = _delay
                return fn
            return _decorator

    celery_app = _NoopCelery()

    def _run_async(coro):
        return asyncio.run(coro)

logger = logging.getLogger(__name__)


MEDIA_API_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=180.0, pool=30.0)
MEDIA_JOB_STALE_SECONDS = int(os.getenv("MEDIA_JOB_STALE_SECONDS", "120"))
MEDIA_JOB_MAX_PROCESSING_SECONDS = int(os.getenv("MEDIA_JOB_MAX_PROCESSING_SECONDS", "1800"))
MEDIA_JOB_ORPHAN_GRACE_SECONDS = int(os.getenv("MEDIA_JOB_ORPHAN_GRACE_SECONDS", "600"))
VIDEO_DURATION_DEFAULT_SECONDS = 5
VIDEO_DURATION_MIN_SECONDS = 4
VIDEO_DURATION_MAX_SECONDS = 15
VIDEO_REFERENCE_MAX_SECONDS = 15.0
VIDEO_REFERENCE_TRIM_SECONDS = 14.9
VIDEO_REFERENCE_EXTENSIONS = {".mp4", ".mov", ".webm"}
VIDEO_RESOLUTION_DEFAULT = "720p"
VIDEO_RESOLUTION_CHOICES = ("480p", "720p", "1080p")
SEEDANCE_FAST_RESOLUTION_CHOICES = ("480p", "720p")
OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
COMPLETED_WITHOUT_VIDEO_URL_ERROR = "Provider reported completion but did not return a video URL"
MEDIA_REFERENCE_URL_EXPIRES_SECONDS = int(os.getenv("MEDIA_REFERENCE_URL_EXPIRES_SECONDS", str(6 * 60 * 60)))
MEDIA_REFERENCE_URL_PREFLIGHT_ENABLED = os.getenv(
    "MEDIA_REFERENCE_URL_PREFLIGHT_ENABLED", "true"
).strip().lower() not in {"0", "false", "no", "off"}
MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS = float(os.getenv("MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS", "10"))
MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS = max(
    1,
    int(os.getenv("MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS", "4")),
)
MEDIA_REFERENCE_URL_PREFLIGHT_RETRY_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("MEDIA_REFERENCE_URL_PREFLIGHT_RETRY_DELAY_SECONDS", "1")),
)
_MEDIA_REFERENCE_URL_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class ProviderPollTimeout(TimeoutError):
    """Provider job is still pending after this worker's active poll window."""


def parse_video_duration(value: Any) -> int | None:
    """Best-effort parse of user/tool duration values into whole seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        for suffix in ("seconds", "second", "secs", "sec", "s"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break
        value = text
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_video_duration(value: Any) -> int:
    """Clamp duration to the provider-supported short-video range."""
    duration = parse_video_duration(value)
    if duration is None:
        duration = VIDEO_DURATION_DEFAULT_SECONDS
    return min(max(duration, VIDEO_DURATION_MIN_SECONDS), VIDEO_DURATION_MAX_SECONDS)


def normalize_video_resolution(model: str | None, value: Any) -> str:
    """Normalize video resolution and clamp known provider/model limits."""
    raw = str(value or VIDEO_RESOLUTION_DEFAULT).strip().lower()
    aliases = {
        "480": "480p",
        "480p": "480p",
        "720": "720p",
        "720p": "720p",
        "1080": "1080p",
        "1080p": "1080p",
    }
    resolution = aliases.get(raw, VIDEO_RESOLUTION_DEFAULT)

    model_id = str(model or "").lower()
    if "seedance-2.0-fast" in model_id or "seedance-2-0-fast" in model_id:
        if resolution not in SEEDANCE_FAST_RESOLUTION_CHOICES:
            return VIDEO_RESOLUTION_DEFAULT
    return resolution


def _provider_exception_message(exc: Exception) -> str:
    """Return a user/debug-friendly error for provider exceptions.

    Some httpx timeout exceptions stringify to an empty string, which made
    media_jobs.error blank and left the UI with only "Video failed".
    """
    if isinstance(exc, httpx.WriteTimeout):
        return "Provider request write timed out while uploading prompt/reference images."
    if isinstance(exc, httpx.ReadTimeout):
        return "Provider request timed out while waiting for a response."
    if isinstance(exc, httpx.ConnectTimeout):
        return "Provider connection timed out."
    if isinstance(exc, httpx.TimeoutException):
        return f"Provider request timed out ({exc.__class__.__name__})."
    message = str(exc).strip()
    return message or exc.__class__.__name__


# ── Public entry points ──────────────────────────────────────────────────────


async def process_video_job(job_id: str) -> None:
    """Execute a video generation job in the background.

    1. Marks job as processing
    2. Calls OpenRouter video API
    3. Downloads result, saves to entity FS, registers as document
    4. Updates job status
    5. Pushes real-time notification (both success AND failure)
    """
    ok = await _mark_video_job_processing(job_id)
    if not ok:
        return

    # Run generation (long-running, outside DB session)
    try:
        result = await _call_video_api(job_id)
    except ProviderPollTimeout as exc:
        logger.info(
            "Video job %s is still pending after active poll window: %s. "
            "Leaving it processing for recovery polling.",
            job_id,
            exc,
        )
        return
    except Exception as exc:
        error_message = _provider_exception_message(exc)
        logger.error("Video job %s failed: %s", job_id, error_message, exc_info=True)
        result = {"error": error_message}

    await _finalize_video_job(job_id, result)


@celery_app.task(
    name="media.process_video_job",
    max_retries=0,
    soft_time_limit=900,
    time_limit=1200,
)
def process_video_job_task(job_id: str) -> dict:
    """Run a video job in the Celery worker instead of the API process."""
    try:
        _run_async(process_video_job(job_id))
        return {"ok": True, "job_id": job_id}
    except Exception as exc:  # noqa: BLE001
        logger.exception("media.process_video_job crashed for %s: %s", job_id, exc)
        try:
            _run_async(_finalize_video_job(job_id, {"error": _provider_exception_message(exc)}))
        except Exception:
            logger.exception("Failed to mark crashed video job %s as failed", job_id)
        return {"ok": False, "job_id": job_id, "error": _provider_exception_message(exc)}


@celery_app.task(
    name="media.recover_stale_jobs",
    max_retries=0,
    soft_time_limit=240,
    time_limit=300,
)
def recover_stale_jobs_task() -> dict:
    """Recover video jobs stranded by API reloads, worker crashes, or lost polls."""
    try:
        return _run_async(recover_stale_video_jobs())
    except Exception as exc:  # noqa: BLE001
        logger.exception("media.recover_stale_jobs failed: %s", exc)
        return {"ok": False, "error": _provider_exception_message(exc)}


# Strong references to background tasks to prevent GC (Python 3.12+)
_background_tasks: set = set()


def schedule_video_job(job_id: str) -> None:
    """Schedule a video job for background processing.

    Prefer Celery so provider polling survives API reloads. Falls back to an
    in-process task only if the broker is unavailable.
    """
    try:
        process_video_job_task.delay(job_id)
        return
    except Exception:
        logger.warning("Celery dispatch failed for video job %s; falling back in-process", job_id, exc_info=True)

    async def _run():
        try:
            await process_video_job(job_id)
        except Exception:
            logger.error("Video job %s crashed", job_id, exc_info=True)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        # Keep strong reference to prevent GC before completion
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        import threading
        threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _mark_video_job_processing(job_id: str) -> bool:
    """Move a job into processing unless it already reached a terminal state."""
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import select

    async with async_session() as db:
        job = (await db.execute(
            select(MediaJob).where(MediaJob.id == job_id)
        )).scalar_one_or_none()
        if not job:
            logger.error("Video job %s not found", job_id)
            return False
        if job.status in {"completed", "failed"}:
            logger.info("Video job %s already terminal (%s); skipping", job_id, job.status)
            return False

        job.status = "processing"
        if not job.started_at:
            job.started_at = datetime.now(timezone.utc)
        await db.commit()
        return True


async def _finalize_video_job(job_id: str, result: dict[str, Any], *, force: bool = False) -> bool:
    """Persist a terminal media job state and always notify the user."""
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import select

    async with async_session() as db:
        job = (await db.execute(
            select(MediaJob).where(MediaJob.id == job_id)
        )).scalar_one_or_none()
        if not job:
            logger.error("Video job %s vanished from DB", job_id)
            return False
        if not force and job.status in {"completed", "failed"} and job.completed_at:
            logger.info("Video job %s already finalized as %s", job_id, job.status)
            return True

        params = dict(job.params or {})
        if "error" in result:
            job.status = "failed"
            job.error = str(result.get("error") or "Unknown error")
            params["provider_poll_status"] = "failed"
        else:
            job.status = "completed"
            job.result_url = result.get("result_url")
            job.source_url = result.get("source_url")
            job.file_size = result.get("file_size")
            if result.get("duration_seconds"):
                job.duration_seconds = int(result["duration_seconds"])
                params["duration"] = job.duration_seconds
            job.cost_usd = 0 if job.byok else result.get("cost_usd")
            job.credits = 0 if job.byok else result.get("credits")
            params["provider_poll_status"] = "completed"
            if result.get("document_id"):
                params["result_document_id"] = result["document_id"]

        try:
            if not job.byok and job.status == "failed":
                from packages.core.services.credit_reservations import release_reservation_by_source

                await release_reservation_by_source(
                    db,
                    source_kind="media_job",
                    source_id=job.id,
                    reason=job.error or "video job failed",
                )
        except Exception:
            logger.warning("Video job %s reservation settlement failed", job.id, exc_info=True)

        params["provider_poll_completed_at"] = datetime.now(timezone.utc).isoformat()
        job.params = params
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()

        # Bill usage (only on success + platform key)
        if job.status == "completed" and not job.byok and job.cost_usd:
            billed = await _bill_usage(job)
            if billed:
                await _consume_video_reservation(job.id, int(job.credits or 0) or None)
        elif job.status == "completed" and not job.byok:
            await _release_video_reservation(job.id, "video completed without billable cost")

        # Always push notification — success or failure.
        await _push_notification(job)
        return True


async def _remember_provider_poll(
    job_id: str,
    provider: str,
    poll_url: str,
    generation_id: str | None = None,
) -> None:
    """Persist enough provider state to resume polling after a process dies."""
    if not poll_url:
        return

    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import select

    try:
        async with async_session() as db:
            job = (await db.execute(
                select(MediaJob).where(MediaJob.id == job_id)
            )).scalar_one_or_none()
            if not job:
                logger.warning("Cannot store provider poll for missing video job %s", job_id)
                return
            params = dict(job.params or {})
            params["provider_poll"] = {
                "provider": provider,
                "poll_url": poll_url,
                "generation_id": generation_id or "",
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            params["provider_poll_status"] = "pending"
            job.params = params
            await db.commit()
    except Exception:
        logger.warning("Failed to persist provider poll state for video job %s", job_id, exc_info=True)


def _aware_utc(value: datetime | None) -> datetime | None:
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _job_age_seconds(job) -> float:
    started = _aware_utc(job.started_at) or _aware_utc(job.created_at)
    if not started:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


async def recover_stale_video_jobs(
    *,
    stale_after_seconds: int = MEDIA_JOB_STALE_SECONDS,
    max_processing_seconds: int = MEDIA_JOB_MAX_PROCESSING_SECONDS,
    orphan_grace_seconds: int = MEDIA_JOB_ORPHAN_GRACE_SECONDS,
    limit: int = 20,
) -> dict:
    """Finalize or resume video jobs that got stranded mid-generation.

    Provider polling is persisted in ``media_jobs.params.provider_poll`` before
    the long poll begins. This sweeper uses that state to ask the provider for
    the latest status exactly once per tick, then writes a terminal result when
    the provider returns success/failure. Jobs without poll state eventually
    fail with a clear retryable error instead of staying stuck forever.
    """
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import or_, select

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=stale_after_seconds)
    stats = {
        "ok": True,
        "checked": 0,
        "completed": 0,
        "failed": 0,
        "still_pending": 0,
        "pending_requeued": 0,
        "failed_recovered": 0,
    }

    async with async_session() as db:
        pending_jobs = (await db.execute(
            select(MediaJob)
            .where(
                MediaJob.kind == "video",
                MediaJob.status == "pending",
                MediaJob.created_at <= cutoff,
            )
            .order_by(MediaJob.created_at.asc())
            .limit(limit)
        )).scalars().all()
        for job in pending_jobs:
            try:
                params = dict(job.params or {})
                params["recovery_dispatch_at"] = now.isoformat()
                job.params = params
                process_video_job_task.delay(job.id)
                stats["pending_requeued"] += 1
            except Exception as exc:  # noqa: BLE001
                await _finalize_video_job(
                    job.id,
                    {"error": f"Video worker dispatch failed: {_provider_exception_message(exc)}"},
                )
                stats["failed"] += 1
        if pending_jobs:
            await db.commit()

        processing_jobs = (await db.execute(
            select(MediaJob)
            .where(
                MediaJob.kind == "video",
                MediaJob.status == "processing",
                or_(MediaJob.started_at.is_(None), MediaJob.started_at <= cutoff),
            )
            .order_by(MediaJob.started_at.asc().nullsfirst(), MediaJob.created_at.asc())
            .limit(limit)
        )).scalars().all()

        failed_recoverable_jobs = (await db.execute(
            select(MediaJob)
            .where(
                MediaJob.kind == "video",
                MediaJob.status == "failed",
                MediaJob.error == COMPLETED_WITHOUT_VIDEO_URL_ERROR,
                MediaJob.completed_at >= now - timedelta(hours=24),
            )
            .order_by(MediaJob.completed_at.asc().nullsfirst(), MediaJob.created_at.asc())
            .limit(limit)
        )).scalars().all()

    for job in processing_jobs:
        stats["checked"] += 1
        params = job.params or {}
        poll_state = params.get("provider_poll") or {}
        poll_url = poll_state.get("poll_url") if isinstance(poll_state, dict) else ""
        age_seconds = _job_age_seconds(job)

        if not poll_url:
            if age_seconds >= orphan_grace_seconds:
                await _finalize_video_job(
                    job.id,
                    {
                        "error": (
                            "Video generation worker stopped before provider polling information "
                            "was saved. Please retry."
                        )
                    },
                )
                stats["failed"] += 1
            else:
                stats["still_pending"] += 1
            continue

        try:
            result = await _resume_video_job_from_provider(job)
        except Exception as exc:  # noqa: BLE001
            result = {"error": _provider_exception_message(exc)}

        if result.get("status") == "pending":
            if age_seconds >= max_processing_seconds:
                await _finalize_video_job(
                    job.id,
                    {
                        "error": (
                            f"Video generation timed out after {int(age_seconds)}s without a "
                            "final provider response. Please retry."
                        )
                    },
                )
                stats["failed"] += 1
            else:
                stats["still_pending"] += 1
            continue

        await _finalize_video_job(job.id, result)
        if "error" in result:
            stats["failed"] += 1
        else:
            stats["completed"] += 1

    for job in failed_recoverable_jobs:
        stats["checked"] += 1
        try:
            result = await _resume_video_job_from_provider(job)
        except Exception as exc:  # noqa: BLE001
            result = {"error": _provider_exception_message(exc)}

        if result.get("status") == "pending" or "error" in result:
            stats["still_pending"] += 1
            continue

        await _finalize_video_job(job.id, result, force=True)
        stats["completed"] += 1
        stats["failed_recovered"] += 1

    return stats


async def _resume_video_job_from_provider(job) -> dict[str, Any]:
    """Poll a stranded provider job once and download the result if complete."""
    params = job.params or {}
    poll_state = params.get("provider_poll") or {}
    poll_url = poll_state.get("poll_url") if isinstance(poll_state, dict) else ""
    if not poll_url:
        return {"status": "pending"}

    model = job.model or "bytedance/seedance-2.0"
    from packages.core.ai.runtime import (
        runtime_resolve_video_recovery_credentials,
    )

    stored_provider = str(poll_state.get("provider") or "").lower()
    credentials = await runtime_resolve_video_recovery_credentials(
        user_id=job.user_id or "",
        entity_id=job.entity_id,
        model=model,
        stored_provider=stored_provider,
    )
    api_key = credentials.api_key
    if not api_key:
        return {"error": "No API key configured to recover video generation"}

    if credentials.provider == "openrouter":
        provider = "openrouter"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://manor.ai",
            "X-Title": "Manor AI",
        }
        download_headers = {
            "Authorization": headers["Authorization"],
            "HTTP-Referer": "https://manor.ai",
        }
    else:
        provider = credentials.provider
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        download_headers = None

    poll_result = await _check_provider_poll_once(poll_url, headers, provider=provider)
    if poll_result.get("status") == "pending" or "error" in poll_result:
        return poll_result

    video_url = poll_result.get("video_url") or ""
    if not video_url:
        return {"error": COMPLETED_WITHOUT_VIDEO_URL_ERROR}

    duration = normalize_video_duration(params.get("duration", job.duration_seconds or 5))
    resolution = normalize_video_resolution(model, params.get("resolution", "720p"))
    return await _download_and_save(
        video_url,
        job.prompt,
        model,
        job.id,
        job.entity_id,
        duration,
        resolution,
        output_name=params.get("output_name") or "",
        auth_headers=download_headers,
        workspace_id=params.get("workspace_id"),
        task_id=params.get("task_id"),
        agent_id=getattr(job, "agent_id", None),
        conversation_id=getattr(job, "conversation_id", None),
        user_id=getattr(job, "user_id", None),
    )


def _provider_error_message(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error") or data.get("message") or data.get("reason")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)
        if err:
            return str(err)
        nested = data.get("data") or data.get("result")
        if nested is not data:
            msg = _provider_error_message(nested)
            if msg:
                return msg
    return "Unknown error"


def _video_adapter_runtime() -> video_adapters.VideoAdapterRuntime:
    return video_adapters.VideoAdapterRuntime(
        http_client_cls=httpx.AsyncClient,
        media_api_timeout=MEDIA_API_TIMEOUT,
        ensure_public_url=_ensure_public_url,
        public_url_kwargs=_public_url_kwargs,
        remember_provider_poll=_remember_provider_poll,
        poll_openrouter_generation=_poll_video_generation,
        poll_volcengine_task=_poll_volcengine_task,
        poll_generic_video_task=_poll_generic_video_task,
        download_and_save=_download_and_save,
        extract_video_url=_extract_video_url,
        extract_task_id=_extract_task_id,
        provider_error_message=_provider_error_message,
        openrouter_api_url=_openrouter_api_url,
        normalize_duration=normalize_video_duration,
        normalize_resolution=normalize_video_resolution,
    )


async def _check_provider_poll_once(poll_url: str, headers: dict, *, provider: str) -> dict[str, Any]:
    """Read one provider poll endpoint and normalize pending/success/failure."""
    request_url = _openrouter_api_url(poll_url) if provider == "openrouter" else poll_url
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(request_url, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"message": resp.text[:500]}
    except Exception as exc:
        logger.debug("Video provider poll failed once: %s", exc)
        return {"status": "pending"}

    if resp.status_code >= 400:
        return {"error": f"Provider poll failed ({resp.status_code}): {_provider_error_message(data)}"}

    video_url = _extract_video_url(data)
    status = str(
        data.get("status")
        or (data.get("data") or {}).get("status")
        or (data.get("result") or {}).get("status")
        or ""
    ).lower()

    if video_url and (not status or status in {"completed", "complete", "succeeded", "success", "done"}):
        return {"video_url": video_url}
    if provider == "openrouter" and status == "completed":
        return {"video_url": video_url or _openrouter_video_content_url(data, request_url)}
    if status in {"completed", "complete", "succeeded", "success", "done"}:
        return {"video_url": video_url} if video_url else {"error": "Provider completed without a video URL"}
    if status in {"failed", "failure", "error", "cancelled", "canceled", "expired"}:
        return {"error": f"Provider failed: {_provider_error_message(data)}"}

    return {"status": "pending"}


async def _bill_usage(job) -> bool:
    """Record media usage for billing."""
    try:
        from packages.core.database import async_session
        from packages.core.services.usage_service import record_media_usage
        async with async_session() as db:
            recorded = await record_media_usage(
                db,
                entity_id=job.entity_id,
                kind="video",
                model=job.model or "bytedance/seedance-2.0",
                cost_usd=job.cost_usd,
                units=job.duration_seconds or 0,
                user_id=getattr(job, "user_id", None),
                agent_id=getattr(job, "agent_id", None),
                conversation_id=getattr(job, "conversation_id", None),
                source="tool:video",
                byok=job.byok,
            )
            await db.commit()
            return bool(recorded)
    except Exception:
        logger.warning("Video job %s billing failed", job.id, exc_info=True)
        return False


async def _consume_video_reservation(job_id: str, consumed_credits: int | None) -> None:
    try:
        from packages.core.database import async_session
        from packages.core.services.credit_reservations import consume_reservation_by_source

        async with async_session() as db:
            await consume_reservation_by_source(
                db,
                source_kind="media_job",
                source_id=job_id,
                consumed_credits=consumed_credits,
            )
            await db.commit()
    except Exception:
        logger.warning("Video job %s reservation consume failed", job_id, exc_info=True)


async def _release_video_reservation(job_id: str, reason: str) -> None:
    try:
        from packages.core.database import async_session
        from packages.core.services.credit_reservations import release_reservation_by_source

        async with async_session() as db:
            await release_reservation_by_source(
                db,
                source_kind="media_job",
                source_id=job_id,
                reason=reason,
            )
            await db.commit()
    except Exception:
        logger.warning("Video job %s reservation release failed", job_id, exc_info=True)


async def _push_notification(job) -> None:
    """Notify user of video job completion via unified dispatcher."""
    from packages.core.services.notify import notify

    prompt_preview = job.prompt[:50] if job.prompt else "Video"
    if job.status == "completed":
        title = f"Video ready: {prompt_preview}..."
        body = "Your video has been generated successfully."
    else:
        title = f"Video failed: {prompt_preview}..."
        body = job.error[:200] if job.error else "Unknown error"
    document_id = (job.params or {}).get("result_document_id")
    link = f"/viewer/{document_id}" if job.status == "completed" and document_id else None
    display_resolution = normalize_video_resolution(
        job.model,
        (job.params or {}).get("resolution", "720p"),
    )

    await notify(
        entity_id=job.entity_id,
        user_id=getattr(job, "user_id", None) or "",
        type="video",
        title=title,
        body=body,
        link=link,
        meta={
            "broadcast_event": "video_ready",
            "job_id": job.id,
            "document_id": document_id,
            "conversation_id": job.conversation_id,
            "status": job.status,
            "result_url": job.result_url,
            "error": job.error,
            "prompt": job.prompt[:100],
            "duration": job.duration_seconds,
            "resolution": display_resolution,
            "model": job.model,
        },
        channels=["db", "broadcast"],
    )


async def _call_video_api(job_id: str) -> dict:
    """Generate video through the adapter selected for the stored job model."""
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from sqlalchemy import select

    async with async_session() as db:
        job = (await db.execute(
            select(MediaJob).where(MediaJob.id == job_id)
        )).scalar_one_or_none()
        if not job:
            return {"error": "Job not found"}

    model = job.model or "bytedance/seedance-2.0"
    params = job.params or {}
    stored_adapter_name = str(params.get("video_adapter") or "").strip()
    from packages.core.ai.runtime import (
        runtime_resolve_video_generation_credentials,
    )
    credentials = await runtime_resolve_video_generation_credentials(
        user_id=job.user_id or "",
        entity_id=job.entity_id,
        model=model,
        stored_adapter_name=stored_adapter_name,
        openrouter_adapter_name=video_adapters.OpenRouterVideoAdapter.adapter_name,
    )
    provider = credentials.provider
    api_key = credentials.api_key
    base_url_override = credentials.base_url_override
    if not api_key:
        return {"error": "No API key configured for video generation"}

    wants_native_seedance_reference_audio = bool(
        params.get("reference_video_urls")
        or params.get("audio_reference_urls")
        or params.get("audio_reference_url")
        or _seedance_bool(params.get("generate_audio"))
    )
    if api_key.startswith("sk-or-") and provider == "bytedance" and wants_native_seedance_reference_audio:
        return {
            "error": (
                "Seedance reference video/audio and native generated audio require Manor's "
                "official Volcengine/Seedance API route. Configure a native Seedance/Volcengine "
                "key or remove reference_video_urls/audio_reference_urls/generate_audio."
            )
        }

    adapter = (
        video_adapters.video_adapter_by_name(stored_adapter_name)
        if stored_adapter_name
        else video_adapters.select_video_generation_adapter(
            model=model,
            provider=provider,
            api_key=api_key,
        )
    )
    if not adapter:
        return {
            "error": (
                f"No video adapter for {provider or 'this'} model. "
                "Use OpenRouter or choose Seedance/Kling."
            )
        }
    if (
        adapter.adapter_name == video_adapters.OpenRouterVideoAdapter.adapter_name
        and not api_key.startswith("sk-or-")
    ):
        return {"error": "Stored OpenRouter video route requires an OpenRouter API key."}
    if (
        adapter.adapter_name != video_adapters.OpenRouterVideoAdapter.adapter_name
        and api_key.startswith("sk-or-")
    ):
        return {"error": f"Stored {adapter.adapter_name} video route requires a native provider API key."}
    return await adapter.submit(job, api_key, base_url_override, _video_adapter_runtime())


def _media_provider_model(model: str) -> str:
    return video_adapters.native_video_model(model)


def _normalize_volcengine_base_url(base_url: str | None) -> str:
    return video_adapters.normalize_volcengine_base_url(base_url)


def _volcengine_base_url_candidates(base_url: str | None) -> list[str]:
    return video_adapters.volcengine_base_url_candidates(base_url)


def _seedance_duration(value) -> int:
    return normalize_video_duration(value)


def _seedance_bool(value) -> bool:
    return video_adapters.seedance_bool(value)


def _seedance_reference_media_blocked_by_frames(params: dict[str, Any]) -> bool:
    return video_adapters.seedance_reference_media_blocked_by_frames(params)


def _public_url_kwargs(public_base_url: str = "") -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "allow_data_uri": False,
        "expires_in_seconds": MEDIA_REFERENCE_URL_EXPIRES_SECONDS,
    }
    if public_base_url:
        kwargs["public_base_url"] = public_base_url
    return kwargs


async def _seedance_image_content(
    url: str,
    entity_id: str,
    role: str | None = None,
    *,
    public_base_url: str = "",
) -> dict:
    return await _seedance_media_content(
        url,
        entity_id,
        media_type="image",
        role=role,
        public_base_url=public_base_url,
    )


async def _seedance_media_content(
    url: str,
    entity_id: str,
    *,
    media_type: str,
    role: str | None = None,
    public_base_url: str = "",
) -> dict:
    field = f"{media_type}_url"
    item = {
        "type": field,
        field: {
            "url": await _ensure_public_url(url, entity_id, **_public_url_kwargs(public_base_url))
        },
    }
    if role:
        item["role"] = role
    return item


def _extract_task_id(data: dict) -> str:
    data_obj = data.get("data") or {}
    return (
        data.get("id")
        or data.get("task_id")
        or data.get("taskId")
        or data_obj.get("id")
        or data_obj.get("task_id")
        or data_obj.get("taskId")
        or ""
    )


def _openrouter_api_url(path_or_url: str) -> str:
    """Normalize OpenRouter relative polling/content paths to absolute URLs."""
    value = str(path_or_url or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/api/v1/"):
        return f"https://openrouter.ai{value}"
    return f"{OPENROUTER_API_BASE_URL}/{value.lstrip('/')}"


def _openrouter_video_job_id(data: dict[str, Any], poll_url: str) -> str:
    """Return the OpenRouter video job id, preferring the polling URL path."""
    try:
        path = urlsplit(_openrouter_api_url(poll_url)).path.rstrip("/")
        if path:
            tail = path.rsplit("/", 1)[-1]
            if tail and tail != "videos":
                return tail
    except Exception:
        pass
    return str(data.get("id") or (data.get("data") or {}).get("id") or "").strip()


def _openrouter_video_content_url(data: dict[str, Any], poll_url: str) -> str:
    """Fallback download URL for completed OpenRouter jobs without unsigned URLs."""
    job_id = _openrouter_video_job_id(data, poll_url)
    if not job_id:
        return ""
    return f"{OPENROUTER_API_BASE_URL}/videos/{job_id}/content?index=0"


def _normalize_kling_base_url(base_url: str | None) -> str:
    return video_adapters.normalize_kling_base_url(base_url)


def _kling_base_url_candidates(base_url: str | None) -> list[str]:
    return video_adapters.kling_base_url_candidates(base_url)


def _is_official_kling_base(base_url: str) -> bool:
    return video_adapters.is_official_kling_base(base_url)


def _kling_official_model_and_mode(native_model: str, params: dict[str, Any]) -> tuple[str, str]:
    return video_adapters.kling_official_model_and_mode(native_model, params)


def _kling_payload_for_base(
    *,
    base: str,
    native_model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    first_frame_url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    return video_adapters.kling_payload_for_base(
        base=base,
        native_model=native_model,
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        first_frame_url=first_frame_url,
        params=params,
    )


async def _call_volcengine_seedance_api(job, api_key: str, base_url: str | None) -> dict:
    """Call Volcengine Seedance native async task API."""
    return await video_adapters.VolcengineSeedanceAdapter().submit(
        job,
        api_key,
        base_url,
        _video_adapter_runtime(),
    )


async def _call_kling_api(job, api_key: str, base_url: str | None) -> dict:
    """Call Kling native async video API."""
    return await video_adapters.KlingVideoAdapter().submit(
        job,
        api_key,
        base_url,
        _video_adapter_runtime(),
    )


def _extract_video_url(value) -> str:
    """Best-effort recursive extraction across native video provider shapes."""
    if isinstance(value, dict):
        for key in ("video_url", "videoUrl", "url", "file_url", "fileUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("http"):
                return candidate
        for key in ("unsigned_urls", "videos", "output", "outputs", "content", "data", "result"):
            found = _extract_video_url(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _extract_video_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_video_url(item)
            if found:
                return found
    elif isinstance(value, str) and value.startswith("http") and any(ext in value.lower() for ext in (".mp4", ".webm", ".mov")):
        return value
    return ""


async def _poll_volcengine_task(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
    deadline = time.monotonic() + timeout
    interval = 5.0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            interval = min(interval * 1.2, 15.0)
            try:
                resp = await client.get(poll_url, headers=headers)
                data = resp.json()
            except Exception as exc:
                logger.debug("Volcengine poll error: %s", exc)
                continue
            status = str(data.get("status") or (data.get("data") or {}).get("status") or "").lower()
            if status in {"succeeded", "success", "completed"}:
                return _extract_video_url(data)
            if status in {"failed", "error", "cancelled", "expired"}:
                raise RuntimeError(f"Seedance generation failed: {_provider_error_message(data)}")
    raise ProviderPollTimeout(f"Seedance generation still pending after {timeout}s")


async def _poll_generic_video_task(poll_url: str, headers: dict, *, timeout: float = 420.0) -> str:
    deadline = time.monotonic() + timeout
    interval = 5.0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            interval = min(interval * 1.2, 15.0)
            try:
                resp = await client.get(poll_url, headers=headers)
                data = resp.json()
            except Exception as exc:
                logger.debug("Video poll error: %s", exc)
                continue
            status = str(data.get("status") or (data.get("data") or {}).get("status") or "").lower()
            if status in {"succeeded", "success", "completed"}:
                return _extract_video_url(data)
            if status in {"failed", "error", "cancelled", "expired"}:
                raise RuntimeError(f"Video generation failed: {_provider_error_message(data)}")
    raise ProviderPollTimeout(f"Video generation still pending after {timeout}s")


async def _build_frame_images(
    params: dict,
    entity_id: str,
    *,
    public_base_url: str = "",
) -> list[dict]:
    return await video_adapters.build_openrouter_frame_images(
        params,
        entity_id,
        runtime=_video_adapter_runtime(),
        public_base_url=public_base_url,
    )


async def _download_and_save(
    video_url: str, prompt: str, model: str, job_id: str,
    entity_id: str, duration: int, resolution: str,
    *, output_name: str = "", auth_headers: dict | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Download video from URL, save to entity FS, register as KB document."""
    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic

    dl_headers = auth_headers or {}
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(video_url, headers=dl_headers)
        resp.raise_for_status()
        video_bytes = resp.content
    if not video_bytes:
        raise RuntimeError("Provider returned an empty video file")

    ct = resp.headers.get("content-type", "")
    ext = ".webm" if "webm" in ct else ".mp4"

    entity_root = get_entity_root(entity_id)
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    target = build_generated_media_target(
        prompt=prompt,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=ext,
        fallback="generated-video",
        default_dir=workspace_artifact_default_dir(workspace_base_dir, "videos"),
        entity_root=entity_root,
    )
    filepath = write_entity_file_atomic(
        entity_id,
        target.rel_path,
        video_bytes,
        expected_size=len(video_bytes),
        allow_empty=False,
    )

    local_url = f"/api/v1/fs/{entity_id}/{target.rel_path}"
    logger.info("Video job %s saved: %s (%d bytes)", job_id, filepath, len(video_bytes))

    # Register as KB document
    document_id = None
    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.document import Document
        from packages.core.services.document_metadata import merge_document_metadata
        from packages.core.services.knowledge_sync import sync_file_to_knowledge

        sync = await sync_file_to_knowledge(
            entity_id=entity_id,
            abs_path=filepath,
            entity_root=entity_root,
            source="ai_generated",
            created_by=user_id or "ai-agent",
            force=True,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name="generate_video",
        )
        document_id = sync.document_id if sync.synced else None
        async with async_session() as db:
            doc = None
            if document_id:
                doc = (await db.execute(
                    select(Document).where(
                        Document.entity_id == entity_id,
                        Document.id == document_id,
                    ).limit(1)
                )).scalar_one_or_none()
            if doc:
                doc.source = "ai_generated"
                if user_id:
                    doc.created_by = user_id
                doc.metadata_ = merge_document_metadata(
                    doc.metadata_,
                    artifact={"role": "final", "storage_scope": "artifact"},
                    origin={
                        "workspace_id": workspace_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "run_id": job_id,
                        "tool_name": "generate_video",
                    },
                    generation={
                        "prompt": prompt,
                        "model": model,
                        "job_id": job_id,
                        "params": {
                            "duration": duration,
                            "resolution": resolution,
                        },
                    },
                )
                await db.commit()
        if not sync.synced:
            logger.warning("Video job %s doc registration skipped: %s", job_id, sync.reason)
    except Exception:
        logger.warning("Video job %s doc registration failed", job_id, exc_info=True)

    # Calculate cost
    cost_usd = 0.0
    credits_charged = 0
    try:
        from packages.core.services.billing_service import estimate_video_cost, video_to_credits
        cost_usd = estimate_video_cost(model, duration, resolution)
        credits_charged = video_to_credits(model, duration, resolution)
    except Exception:
        pass

    return {
        "result_url": local_url,
        "document_id": document_id,
        "source_url": video_url,
        "file_size": len(video_bytes),
        "duration_seconds": duration,
        "cost_usd": cost_usd,
        "credits": credits_charged,
    }


async def _poll_video_generation(
    poll_url: str, headers: dict, *, timeout: float = 300.0
) -> str:
    """Poll OpenRouter video generation URL until the video is ready."""
    poll_url = _openrouter_api_url(poll_url)
    deadline = time.monotonic() + timeout
    poll_interval = 5.0

    logger.info("Polling video generation at %s (timeout=%ss)", poll_url, timeout)

    async with httpx.AsyncClient(timeout=30.0) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.2, 15.0)

            try:
                resp = await client.get(poll_url, headers=headers)
                data = resp.json()
            except Exception as exc:
                logger.debug("Poll error: %s", exc)
                continue

            status = str(
                data.get("status")
                or (data.get("data") or {}).get("status")
                or (data.get("result") or {}).get("status")
                or ""
            ).lower()
            logger.debug("Poll status=%s, keys=%s", status, list(data.keys()))

            if status in {"completed", "complete", "succeeded", "success", "done"}:
                # OpenRouter returns video URL in unsigned_urls array
                unsigned = data.get("unsigned_urls") or []
                return (
                    (unsigned[0] if unsigned else "")
                    or data.get("url")
                    or data.get("video_url")
                    or (data.get("output", {}) or {}).get("url")
                    or _extract_video_url(data)
                    or _openrouter_video_content_url(data, poll_url)
                    or ""
                )
            elif status in {"failed", "failure", "error", "cancelled", "canceled", "expired"}:
                raise RuntimeError(f"Video generation failed: {_provider_error_message(data)}")
            # else: pending/processing — keep polling

    raise ProviderPollTimeout(f"Video generation still pending after {timeout}s")


def _safe_entity_rel_path(path: str) -> str | None:
    rel_path = unquote((path or "").replace("\\", "/")).strip().lstrip("/")
    if not rel_path:
        return None
    rel_path = os.path.normpath(rel_path).replace("\\", "/")
    if rel_path in {"", "."} or rel_path == ".." or rel_path.startswith("../"):
        return None
    return rel_path


def _entity_rel_path_from_reference(value: str, entity_id: str, entity_root: str) -> str | None:
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme else value

    if path.startswith("/api/v1/fs/public/"):
        # Manor-generated public URLs are short-lived provider URLs, not the
        # canonical file reference. If one is passed back into a later media
        # call, decode it and mint a fresh URL instead of forwarding an
        # expiring/expired token to the provider.
        parts = path.split("/", 5)
        token_part = parts[5] if len(parts) >= 6 else ""
        token = token_part.split("/", 1)[0]
        if not token:
            return None
        try:
            from packages.core.services.file_access_tokens import verify_file_access_token

            payload = verify_file_access_token(token)
        except Exception:
            payload = None
        if payload and payload.get("entity_id") == entity_id:
            return _safe_entity_rel_path(str(payload.get("path") or ""))
        return None

    if path.startswith("/api/v1/fs/"):
        # /api/v1/fs/{entity_id}/path/to/file -> path/to/file
        parts = path.split("/", 5)
        if len(parts) < 6:
            return None
        if parts[4] != entity_id:
            logger.warning("Entity mismatch for local media URL conversion: %s", value)
            return None
        return _safe_entity_rel_path(parts[5])

    if parsed.scheme:
        return None

    # Agents sometimes pass Knowledge-relative paths directly, e.g.
    # 猫咪打工人动漫/images/场景.png. Treat them as entity-scoped file paths.
    if os.path.isabs(value):
        try:
            full_path = os.path.realpath(value)
            root = os.path.realpath(entity_root)
            if os.path.commonpath([root, full_path]) != root:
                logger.warning("Rejected local media path outside entity root: %s", value)
                return None
            return _safe_entity_rel_path(os.path.relpath(full_path, root))
        except ValueError:
            return None
    return _safe_entity_rel_path(value)


def _looks_like_entity_fs_reference(value: str) -> bool:
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme else value
    return path.startswith("/api/v1/fs/") and not path.startswith("/api/v1/fs/public/")


def _invalid_entity_fs_reference_message(value: str, entity_id: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme else value
    parts = path.split("/", 5)
    if len(parts) < 6:
        return (
            "Invalid Manor media reference. `/api/v1/fs/...` URLs must include "
            f"the entity id and path: `/api/v1/fs/{entity_id}/path/to/file.png`. "
            "Use a plain Knowledge-relative path such as `project/storyboards/frame.png` "
            "when the entity id is not available."
        )
    return (
        "Invalid Manor media reference. The entity segment in "
        f"`{path}` is `{parts[4]}`, but this job belongs to `{entity_id}`. "
        "Use the plain Knowledge-relative path or a URL generated by Manor."
    )


def _provider_safe_reference_filename(rel_path: str) -> str:
    """Give provider-facing signed URLs a media extension for strict validators."""
    ext = os.path.splitext(rel_path)[1].lower()
    if ext == ".jpeg":
        ext = ".jpg"
    allowed = {
        ".jpg", ".png", ".webp",
        ".mp4", ".mov", ".webm",
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    }
    if ext not in allowed:
        ext = ".png"
    return f"reference{ext}"


def _media_reference_ext(path: str = "", mime_type: str = "") -> str:
    ext = os.path.splitext(path or "")[1].lower()
    if ext == ".jpeg":
        ext = ".jpg"
    allowed = {
        ".jpg", ".png", ".webp",
        ".mp4", ".mov", ".webm",
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    }
    if ext in allowed:
        return ext
    mime_ext = mimetypes.guess_extension((mime_type or "").split(";", 1)[0].strip())
    if mime_ext == ".jpeg":
        mime_ext = ".jpg"
    return mime_ext if mime_ext in allowed else ".png"


def _is_video_reference_path(path: str) -> bool:
    ref_path = (urlsplit(path).path if "://" in path else path) or ""
    return os.path.splitext(ref_path)[1].lower() in VIDEO_REFERENCE_EXTENSIONS


def _video_reference_duration_seconds(path: str) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        logger.warning("ffprobe is unavailable; skipping Seedance reference video duration check")
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe failed for Seedance reference video %s: %s", path, exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "ffprobe returned %d for Seedance reference video %s: %s",
            result.returncode,
            path,
            (result.stderr or "").strip()[:300],
        )
        return None
    try:
        return float((result.stdout or "").strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        logger.warning("ffprobe returned an invalid duration for Seedance reference video %s", path)
        return None


def _trim_local_reference_video(
    source_abs: str,
    target_abs: str,
    *,
    target_seconds: float = VIDEO_REFERENCE_TRIM_SECONDS,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to trim Seedance reference videos")

    os.makedirs(os.path.dirname(target_abs), exist_ok=True)
    root, ext = os.path.splitext(target_abs)
    ext = ext.lower() if ext.lower() in VIDEO_REFERENCE_EXTENSIONS else ".mp4"
    tmp_abs = f"{root}.trim-{os.getpid()}-{int(time.time() * 1000)}{ext}"
    args = [
        ffmpeg,
        "-y",
        "-i",
        source_abs,
        "-t",
        f"{target_seconds:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
    ]
    if ext in {".mp4", ".mov"}:
        args.extend([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ])
    else:
        args.extend(["-c", "copy"])
    args.append(tmp_abs)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "ffmpeg failed").strip()[:500])
        if not os.path.isfile(tmp_abs) or os.path.getsize(tmp_abs) <= 0:
            raise RuntimeError("ffmpeg produced an empty reference video")
        os.replace(tmp_abs, target_abs)
    finally:
        try:
            if os.path.exists(tmp_abs):
                os.unlink(tmp_abs)
        except OSError:
            logger.debug("Failed to clean temporary Seedance reference trim file %s", tmp_abs, exc_info=True)


def _copy_or_trim_video_reference(
    *,
    entity_id: str,
    target_rel: str,
    target_abs: str,
    source_abs: str,
    source_label: str,
    copy_entity_file_atomic,
) -> str:
    duration = _video_reference_duration_seconds(source_abs)
    if duration is None or duration <= VIDEO_REFERENCE_MAX_SECONDS:
        return copy_entity_file_atomic(
            entity_id,
            target_rel,
            source_abs,
            expected_size=os.path.getsize(source_abs),
            allow_empty=False,
        )

    try:
        _trim_local_reference_video(
            source_abs,
            target_abs,
            target_seconds=VIDEO_REFERENCE_TRIM_SECONDS,
        )
    except Exception as exc:
        raise ValueError(
            f"Seedance reference video `{source_label}` is {duration:.2f}s, "
            f"but reference videos must be <= {VIDEO_REFERENCE_MAX_SECONDS:g}s. "
            f"Automatic trim failed: {exc}"
        ) from exc

    trimmed_duration = _video_reference_duration_seconds(target_abs)
    if trimmed_duration is not None and trimmed_duration > VIDEO_REFERENCE_MAX_SECONDS:
        raise ValueError(
            f"Seedance reference video `{source_label}` is {trimmed_duration:.2f}s after trimming; "
            f"trim it below {VIDEO_REFERENCE_MAX_SECONDS:g}s before generating."
        )
    logger.info(
        "Trimmed Seedance reference video %s from %.2fs to %s",
        source_label,
        duration,
        f"{trimmed_duration:.2f}s" if trimmed_duration is not None else f"{VIDEO_REFERENCE_TRIM_SECONDS:.2f}s",
    )
    return target_abs


def _decode_image_data_uri(value: str) -> tuple[bytes, str] | None:
    if not value.startswith("data:image/"):
        return None
    try:
        header, encoded = value.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header:
        return None
    mime_type = header[5:].split(";", 1)[0] or "image/png"
    return base64.b64decode(encoded), mime_type


def _snapshot_one_video_reference(value: str, entity_id: str, job_id: str, slot: str, index: int) -> str:
    """Copy transient/local reference media into a job-owned hidden path.

    Providers fetch references after the tool has returned a pending job. If a
    reference points at ``uploads/chat`` or a short-lived Manor signed URL, the
    original file may disappear or the token may expire before Seedance reads
    it. Snapshotting creates a stable per-job source that can be re-signed by
    the worker right before provider submission.
    """
    from packages.core.services.entity_fs import copy_entity_file_atomic, get_entity_root, write_entity_file_atomic

    raw = str(value or "").strip()
    if not raw:
        return raw

    entity_root = get_entity_root(entity_id)
    data_uri = _decode_image_data_uri(raw)
    rel_path = _entity_rel_path_from_reference(raw, entity_id, entity_root)
    if not data_uri and not rel_path:
        if _looks_like_entity_fs_reference(raw):
            raise ValueError(_invalid_entity_fs_reference_message(raw, entity_id))
        return raw

    safe_slot = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in slot) or "reference"
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]

    if data_uri:
        image_bytes, mime_type = data_uri
        ext = _media_reference_ext(mime_type=mime_type)
        target_rel = f"uploads/media-references/{job_id}/{index:02d}-{safe_slot}-{digest}{ext}"
        write_entity_file_atomic(
            entity_id,
            target_rel,
            image_bytes,
            expected_size=len(image_bytes),
            allow_empty=False,
        )
        return f"/api/v1/fs/{entity_id}/{target_rel}"

    assert rel_path is not None
    source_abs = os.path.realpath(os.path.join(entity_root, rel_path))
    root_abs = os.path.realpath(entity_root)
    try:
        if os.path.commonpath([root_abs, source_abs]) != root_abs:
            raise ValueError
    except ValueError:
        raise FileNotFoundError(f"Media reference is outside the entity filesystem: {raw}")
    if not os.path.isfile(source_abs):
        raise FileNotFoundError(f"Media reference not found: {rel_path}")

    ext = _media_reference_ext(rel_path)
    target_rel = f"uploads/media-references/{job_id}/{index:02d}-{safe_slot}-{digest}{ext}"
    target_abs = os.path.join(entity_root, target_rel)
    if os.path.realpath(target_abs) != source_abs:
        if _is_video_reference_path(rel_path):
            _copy_or_trim_video_reference(
                entity_id=entity_id,
                target_rel=target_rel,
                target_abs=target_abs,
                source_abs=source_abs,
                source_label=rel_path,
                copy_entity_file_atomic=copy_entity_file_atomic,
            )
        else:
            copy_entity_file_atomic(
                entity_id,
                target_rel,
                source_abs,
                expected_size=os.path.getsize(source_abs),
                allow_empty=False,
            )
    return f"/api/v1/fs/{entity_id}/{target_rel}"


def snapshot_video_reference_urls(
    *,
    entity_id: str,
    job_id: str,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Return video reference params with local inputs persisted per job."""
    refs = list(reference_urls or [])
    video_refs = list(reference_video_urls or [])
    audio_refs = list(audio_reference_urls or [])
    out_refs: list[str] = []
    out_video_refs: list[str] = []
    out_audio_refs: list[str] = []
    slot_index = 0
    first = first_frame_url
    if first:
        first = _snapshot_one_video_reference(first, entity_id, job_id, "first-frame", slot_index)
        slot_index += 1
    last = last_frame_url
    if last:
        last = _snapshot_one_video_reference(last, entity_id, job_id, "last-frame", slot_index)
        slot_index += 1
    for idx, ref in enumerate(refs[:9]):
        out_refs.append(_snapshot_one_video_reference(ref, entity_id, job_id, f"reference-{idx + 1}", slot_index))
        slot_index += 1
    for idx, ref in enumerate(video_refs[:3]):
        out_video_refs.append(_snapshot_one_video_reference(ref, entity_id, job_id, f"reference-video-{idx + 1}", slot_index))
        slot_index += 1
    for idx, ref in enumerate(audio_refs[:3]):
        out_audio_refs.append(_snapshot_one_video_reference(ref, entity_id, job_id, f"reference-audio-{idx + 1}", slot_index))
        slot_index += 1
    return {
        "first_frame_url": first,
        "last_frame_url": last,
        "reference_urls": out_refs,
        "reference_video_urls": out_video_refs,
        "audio_reference_urls": out_audio_refs,
    }


def _should_preflight_public_media_url(public_base: str) -> bool:
    if not MEDIA_REFERENCE_URL_PREFLIGHT_ENABLED:
        return False
    host = (urlsplit(public_base).hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".test"):
        return False
    return True


async def _preflight_public_media_url(url: str) -> tuple[bool, str]:
    timeout = httpx.Timeout(
        connect=min(MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS, 5.0),
        read=MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS,
        write=MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS,
        pool=MEDIA_REFERENCE_URL_PREFLIGHT_TIMEOUT_SECONDS,
    )
    last_reason = ""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for attempt in range(MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS):
                should_retry = False
                try:
                    resp = await client.head(url)
                    if resp.status_code == 405:
                        resp = await client.get(url, headers={"Range": "bytes=0-0"})
                    if 200 <= resp.status_code < 300:
                        return True, ""
                    body = ""
                    if resp.request.method != "HEAD":
                        body = resp.text[:200]
                    last_reason = f"HTTP {resp.status_code}{': ' + body if body else ''}"
                    should_retry = resp.status_code in _MEDIA_REFERENCE_URL_TRANSIENT_STATUSES
                except Exception as exc:  # noqa: BLE001 - surfaced as a structured preflight reason
                    last_reason = str(exc)
                    should_retry = True

                if not should_retry or attempt + 1 >= MEDIA_REFERENCE_URL_PREFLIGHT_ATTEMPTS:
                    break
                delay = min(
                    MEDIA_REFERENCE_URL_PREFLIGHT_RETRY_DELAY_SECONDS * (2 ** attempt),
                    5.0,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        return False, last_reason or "preflight failed"
    except Exception as exc:
        return False, str(exc)


async def _ensure_public_url(
    url: str,
    entity_id: str,
    *,
    allow_data_uri: bool = True,
    public_base_url: str | None = None,
    expires_in_seconds: int = 900,
) -> str:
    """Convert local /api/v1/fs/... URLs into provider-readable media URLs.

    In production, providers should receive a short-lived HTTPS URL they can
    fetch without Manor auth. Some provider routes accept data URIs, so callers
    may allow a data URI fallback for local/dev. Media-generation reference
    images are URL-only and call this with ``allow_data_uri=False``.
    """
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.config import get_settings
    entity_root = get_entity_root(entity_id)
    value = str(url or "").strip()
    if value.startswith("data:image/"):
        if allow_data_uri:
            return value
        raise RuntimeError(
            "Media references must be provider-readable HTTPS URLs. "
            "Inline data URIs must be saved to the workspace filesystem before video generation."
        )
    rel_path = _entity_rel_path_from_reference(value, entity_id, entity_root)
    if not rel_path and _looks_like_entity_fs_reference(value):
        raise RuntimeError(_invalid_entity_fs_reference_message(value, entity_id))
    if not rel_path:
        return url

    job_public_base = (public_base_url or "").rstrip("/")
    settings_public_base = (get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    public_base = job_public_base if job_public_base.startswith("https://") else settings_public_base
    if public_base.startswith("https://"):
        from packages.core.services.file_access_tokens import create_file_access_token

        token = create_file_access_token(
            entity_id=entity_id,
            rel_path=rel_path,
            expires_in_seconds=expires_in_seconds,
        )
        filename = quote(_provider_safe_reference_filename(rel_path), safe="")
        signed_url = f"{public_base}/api/v1/fs/public/{token}/{filename}"
        if _should_preflight_public_media_url(public_base):
            ok, reason = await _preflight_public_media_url(signed_url)
            if not ok:
                raise RuntimeError(
                    "Media reference is not fetchable through the signed public URL "
                    f"(path={rel_path}, reason={reason})."
                )
        return signed_url

    full_path = os.path.join(entity_root, rel_path)
    if not os.path.isfile(full_path):
        logger.warning("Local file not found for URL conversion: %s", full_path)
        return url

    if not allow_data_uri:
        raise RuntimeError(
            "Media references require a provider-readable HTTPS URL. "
            "Set PUBLIC_BASE_URL to an externally reachable HTTPS base URL so "
            "Manor can create /api/v1/fs/public/{token} signed media URLs."
        )

    mime_type = mimetypes.guess_type(full_path)[0] or "image/png"
    with open(full_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime_type};base64,{b64}"
