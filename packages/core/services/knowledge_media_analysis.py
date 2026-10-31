"""Knowledge media analysis: describe uploaded images, video and audio once, search them forever.

Lifecycle of ``documents.metadata["media_analysis"]``:

* ``knowledge_sync`` marks a new or changed upload ``pending`` (generated
  media is skipped: its generation prompt already describes it).
* ``analyze_document_media`` claims it (``running``), checks the Workspace /
  account opt-out and credits, runs ``media_analysis.analyze_media`` billed
  to the document's account, and stores the record as ``ready`` — unless the
  file changed meanwhile, in which case the newer ``pending`` mark wins.
* The sweep re-dispatches pending rows whose dispatch was lost, retries
  transient failures with backoff, and retries ``waiting_credit`` rows after
  the account has had time to top up.

Readers never call a model: ``stored_media_text`` returns the ready record's
text, and the metadata itself is already matched by name search.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from packages.core.services.media_analysis import (
    MEDIA_ANALYSIS_VERSION,
    MediaAnalysisSkipped,
    analyze_media,
    media_kind,
    search_text,
)

logger = logging.getLogger(__name__)

METADATA_KEY = "media_analysis"
# The vision model that writes the description. Chosen on a 53-file, 84-query
# Knowledge retrieval evaluation (images, video, audio; Chinese and English):
# top-3 hit rate 93% at about $0.0004 per image, the same hit rate as models
# costing 3-4x more. Operators can point it at another image-capable model.
MEDIA_ANALYSIS_MODEL = os.getenv("MEDIA_ANALYSIS_MODEL", "google/gemini-2.5-flash-lite")
# The platform transcription model when the account has no STT key of its own:
# the cheapest route that returns plain text (no timestamps are needed here).
PLATFORM_STT_MODEL = "openai/gpt-4o-mini-transcribe"
# Media written by Manor itself; mirrors the generated-asset filter in
# document_service. Their prompts are already in their metadata.
GENERATED_SOURCES = frozenset({"ai_generated", "sandbox", "bash", "agent", "elevenlabs", "mcp"})
MAX_ATTEMPTS = 3
RUNNING_STALE_SECONDS = 15 * 60
WAITING_CREDIT_RETRY_SECONDS = 6 * 60 * 60
SWEEP_BATCH = 25


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def analysis_kind_for(*, source: str | None, mime_type: str | None, file_type: str | None, name: str | None) -> str | None:
    """The media kind to analyse for this document, or ``None`` to leave it alone."""
    if str(source or "") in GENERATED_SOURCES:
        return None
    return media_kind(mime_type, file_type, name)


def mark_media_analysis_pending(metadata: dict | None, *, kind: str, mtime_ns: int | None) -> dict:
    """Metadata with this file version queued for analysis (any older record is dropped)."""
    meta = dict(metadata or {})
    meta[METADATA_KEY] = {
        "status": "pending",
        "media_kind": kind,
        "mtime_ns": mtime_ns,
        "requested_at": _iso(_now()),
        "attempts": 0,
    }
    return meta


def stored_media_text(metadata: dict | None) -> str:
    """Searchable text of a ready analysis; ``""`` when there is none."""
    record = (metadata or {}).get(METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(record, dict) or record.get("status") != "ready":
        return ""
    return search_text(record)


def media_analysis_disabled(settings: Any) -> bool:
    """Default on; ``{"media_analysis": {"enabled": false}}`` turns it off."""
    if not isinstance(settings, dict):
        return False
    block = settings.get(METADATA_KEY)
    if isinstance(block, dict) and block.get("enabled") is False:
        return True
    return settings.get("media_analysis_enabled") is False


def schedule_media_analysis(document_id: str | None) -> None:
    """Best-effort dispatch after commit; the sweep is the durable fallback."""
    if not document_id:
        return
    try:
        from packages.core.tasks.media_analysis_tasks import analyze_document_media_task

        analyze_document_media_task.delay(document_id)
    except Exception:
        logger.debug("media analysis dispatch failed for %s; sweep will retry", document_id, exc_info=True)


async def _workspace_id(db: Any, doc: Any) -> str | None:
    """The Workspace that pays and whose opt-out applies: origin, else storage folder."""
    origin = (doc.metadata_ or {}).get("origin") if isinstance(doc.metadata_, dict) else None
    workspace_id = (origin or {}).get("workspace_id") if isinstance(origin, dict) else None
    if workspace_id:
        return str(workspace_id)
    from packages.core.services.workspace_artifacts import infer_workspace_id_from_storage_path

    return await infer_workspace_id_from_storage_path(entity_id=doc.entity_id, rel_path=doc.fs_path or "", db=db)


async def _set_record(db: Any, doc: Any, record: dict[str, Any]) -> None:
    meta = dict(doc.metadata_ or {})
    meta[METADATA_KEY] = record
    doc.metadata_ = meta
    await db.flush()


async def _claim(document_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Mark the pending row ``running``; returns (doc facts, claimed record) or ``None``."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.document import Document

    async with create_worker_session()() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == document_id).with_for_update()
        )).scalar_one_or_none()
        if doc is None or doc.is_trashed or not doc.fs_path:
            return None
        if doc.quarantine_status != "clean":
            # Unscanned or infected files are never sent to a model; the
            # sweep picks the row up once the scan marks it clean.
            return None
        record = dict(((doc.metadata_ or {}).get(METADATA_KEY)) or {})
        status = record.get("status")
        now = _now()
        if status == "running":
            claimed_at = record.get("claimed_at")
            if claimed_at and datetime.fromisoformat(claimed_at) > now - timedelta(seconds=RUNNING_STALE_SECONDS):
                return None
        elif status not in ("pending", "waiting_credit"):
            return None
        retry_after = record.get("retry_after")
        if retry_after and datetime.fromisoformat(retry_after) > now:
            return None
        record.update({"status": "running", "claimed_at": _iso(now), "attempts": int(record.get("attempts") or 0) + 1})
        record.pop("retry_after", None)
        await _set_record(db, doc, record)
        facts = {
            "entity_id": doc.entity_id,
            "fs_path": doc.fs_path,
            "name": doc.name,
            "workspace_id": await _workspace_id(db, doc),
            "user_id": doc.owner_id or doc.created_by,
        }
        await db.commit()
        return facts, record


async def _finish(document_id: str, claimed: dict[str, Any], record: dict[str, Any]) -> bool:
    """Store ``record`` if our claim still holds and the file is the analysed version."""
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.document import Document
    from packages.core.services.tool_cache_version import bump_tool_cache_version

    async with create_worker_session()() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == document_id).with_for_update()
        )).scalar_one_or_none()
        if doc is None:
            return False
        current = ((doc.metadata_ or {}).get(METADATA_KEY)) or {}
        if current.get("claimed_at") != claimed.get("claimed_at") or current.get("status") != "running":
            # Re-uploaded meanwhile: the newer pending mark wins.
            return False
        await _set_record(db, doc, record)
        await db.commit()
        entity_id = doc.entity_id
    try:
        await bump_tool_cache_version(entity_id, "documents")
    except Exception:
        logger.debug("tool cache bump failed after media analysis", exc_info=True)
    return True


async def _analysis_disabled_for(entity_id: str, workspace_id: str | None) -> bool:
    from packages.core.database import create_worker_session
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace

    async with create_worker_session()() as db:
        entity = await db.get(Entity, entity_id)
        if entity is not None and media_analysis_disabled(entity.settings):
            return True
        if workspace_id:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is not None and media_analysis_disabled(workspace.settings):
                return True
    return False


def _model_callers(facts: dict[str, Any], billed: dict[str, Any]):
    """The vision completion and transcription calls, billed to the document's account."""
    from packages.core.ai.runtime import RUNTIME_KNOWLEDGE_MEDIA_ANALYSIS_SOURCE
    from packages.core.ai.runtime.completions import runtime_execute_text_completion

    entity_id, workspace_id = facts["entity_id"], facts["workspace_id"]

    async def complete(messages: list[dict[str, Any]], max_tokens: int) -> tuple[str, dict[str, Any]]:
        # No user_id: the description must follow the prompt's bilingual
        # contract, not the uploader's reply-language preference.
        result = await runtime_execute_text_completion(
            messages,
            entity_id=entity_id,
            workspace_id=workspace_id,
            source=RUNTIME_KNOWLEDGE_MEDIA_ANALYSIS_SOURCE,
            model=MEDIA_ANALYSIS_MODEL,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        usage = dict(result.usage or {})
        if not result.content:
            raise RuntimeError(f"media analysis completion returned nothing: {str(usage.get('error') or '')[:200]}")
        billed["model"] = usage.get("model") or billed.get("model")
        return result.content, {"cost_usd": usage.get("cost_usd")}

    async def transcribe(audio: bytes, duration: float) -> tuple[str, dict[str, Any]]:
        from packages.core.services.model_resolver import resolve_llm_metadata_for_user, resolve_model_for_user
        from packages.core.services.voice.whisper import transcribe_blob, whisper_cost_usd

        metadata = await resolve_llm_metadata_for_user("stt", user_id=None, entity_id=entity_id, workspace_id=workspace_id)
        user_key = (metadata or {}).get("llm_api_key")
        kwargs: dict[str, Any] = {"mime": "audio/mpeg", "filename": "speech.mp3"}
        if user_key:
            kwargs.update({
                "user_api_key": user_key,
                "resolved_model": await resolve_model_for_user("stt", user_id=None, entity_id=entity_id, workspace_id=workspace_id),
            })
            if (metadata or {}).get("llm_base_url"):
                kwargs["user_base_url"] = metadata["llm_base_url"]
        else:
            kwargs["resolved_model"] = PLATFORM_STT_MODEL
        result = await transcribe_blob(audio, **kwargs)
        seconds = float(result.duration_seconds or duration or 0.0)
        cost = whisper_cost_usd(seconds, result.model)
        billed["stt_model"] = result.model
        if not user_key:
            from packages.core.ai.runtime import RUNTIME_KNOWLEDGE_MEDIA_ANALYSIS_SOURCE as source
            from packages.core.database import create_worker_session
            from packages.core.services.usage_service import record_media_usage

            async with create_worker_session()() as db:
                await record_media_usage(
                    db, entity_id, kind="whisper", model=result.model, cost_usd=cost, units=int(seconds),
                    workspace_id=workspace_id, source=source, byok=False,
                )
                await db.commit()
        return result.text or "", {"cost_usd": cost}

    return complete, transcribe


async def analyze_document_media(document_id: str) -> str:
    """Analyse one pending document; returns the resulting status."""
    from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError
    from packages.core.ai.runtime import RUNTIME_KNOWLEDGE_MEDIA_ANALYSIS_SOURCE, runtime_assert_credit_available
    from packages.core.services.entity_fs import get_entity_root

    claim = await _claim(document_id)
    if claim is None:
        return "not_claimed"
    facts, claimed = claim
    base = {key: claimed.get(key) for key in ("media_kind", "requested_at", "attempts", "claimed_at")}

    def done(status: str, **fields: Any) -> dict[str, Any]:
        return {**base, "status": status, "finished_at": _iso(_now()), **fields}

    if await _analysis_disabled_for(facts["entity_id"], facts["workspace_id"]):
        await _finish(document_id, claimed, done("disabled"))
        return "disabled"

    path = os.path.join(get_entity_root(facts["entity_id"]), facts["fs_path"])
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        await _finish(document_id, claimed, done("skipped", reason="file missing"))
        return "skipped"

    try:
        await runtime_assert_credit_available(
            facts["entity_id"], source=RUNTIME_KNOWLEDGE_MEDIA_ANALYSIS_SOURCE, workspace_id=facts["workspace_id"],
        )
    except (CreditExhaustedError, CreditCheckUnavailableError) as exc:
        waiting = done("waiting_credit", reason=type(exc).__name__,
                       retry_after=_iso(_now() + timedelta(seconds=WAITING_CREDIT_RETRY_SECONDS)))
        # Credit waits are not failures; they do not use up an attempt.
        waiting["attempts"] = max(0, int(base.get("attempts") or 1) - 1)
        await _finish(document_id, claimed, waiting)
        return "waiting_credit"

    billed: dict[str, Any] = {}
    complete, transcribe = _model_callers(facts, billed)
    try:
        record = await analyze_media(path, str(claimed.get("media_kind") or ""), complete=complete, transcribe=transcribe)
    except MediaAnalysisSkipped as exc:
        await _finish(document_id, claimed, done("skipped", reason=str(exc)[:300], mtime_ns=mtime_ns))
        return "skipped"
    except Exception as exc:
        attempts = int(base.get("attempts") or 1)
        logger.warning("media analysis failed for %s (attempt %d): %s", document_id, attempts, exc)
        if attempts >= MAX_ATTEMPTS:
            await _finish(document_id, claimed, done("failed", error=f"{type(exc).__name__}: {exc}"[:300]))
            return "failed"
        await _finish(document_id, claimed, done(
            "pending", error=f"{type(exc).__name__}: {exc}"[:300],
            retry_after=_iso(_now() + timedelta(minutes=10 * attempts)),
        ))
        return "pending"

    record.update(done("ready", mtime_ns=mtime_ns, model=billed.get("model"), stt_model=billed.get("stt_model")))
    record["version"] = MEDIA_ANALYSIS_VERSION
    stored = await _finish(document_id, claimed, record)
    return "ready" if stored else "superseded"


async def pending_media_analysis_ids(db: Any, *, limit: int = SWEEP_BATCH) -> list[str]:
    """Documents whose analysis is due: pending/waiting past their retry time, or stuck running."""
    from sqlalchemy import and_, or_, select

    from packages.core.models.document import Document

    record = Document.metadata_[METADATA_KEY]
    status = record["status"].astext
    now = _iso(_now())
    stale = _iso(_now() - timedelta(seconds=RUNNING_STALE_SECONDS))
    rows = await db.execute(
        select(Document.id)
        .where(
            Document.is_trashed == False,  # noqa: E712
            Document.fs_path.is_not(None),
            Document.quarantine_status == "clean",
            or_(
                and_(status.in_(("pending", "waiting_credit")),
                     or_(record["retry_after"].astext.is_(None), record["retry_after"].astext <= now)),
                and_(status == "running", record["claimed_at"].astext <= stale),
            ),
        )
        .order_by(Document.updated_at.asc())
        .limit(limit)
    )
    return [row[0] for row in rows.all()]


__all__ = [
    "GENERATED_SOURCES",
    "METADATA_KEY",
    "analysis_kind_for",
    "analyze_document_media",
    "mark_media_analysis_pending",
    "media_analysis_disabled",
    "pending_media_analysis_ids",
    "schedule_media_analysis",
    "stored_media_text",
]
