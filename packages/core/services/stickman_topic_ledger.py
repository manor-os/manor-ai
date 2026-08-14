"""Append-only Workspace Knowledge ledger for Stickman video Topics.

The filesystem JSON records are the source of truth.  Knowledge indexing makes
them visible and searchable, but no topic-specific database table is used.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import unicodedata
from urllib.parse import quote, urlparse
from typing import Any, Literal

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_sync_entity_file_to_knowledge,
)
from packages.core.models.base import generate_ulid
from packages.core.services.workspace_artifacts import (
    ensure_workspace_artifact_directory,
)


LEDGER_DIRECTORY = "topic-ledger"
RESERVATION_DIRECTORY = "reservations"
EVENT_DIRECTORY = "events"
LEDGER_SCHEMA_VERSION = 1
LEDGER_SOURCE = "ai_generated"
_RUN_KEY = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
TopicLedgerEvent = Literal["video_ready", "youtube_saved"]
LIVE_LEDGER_PACK_SLUG = "solo-stickman-studio-ops"
LIVE_LEDGER_DOCUMENT_PATH = "topic-ledger/ledger.md"
LIVE_LEDGER_TEMPLATE_ID = "stickman-topic-video-ledger"
LIVE_LEDGER_RENDERER = "stickman_topic_video_ledger"


class StickmanTopicLedgerError(ValueError):
    """Stable, user-reportable Topic Ledger validation or storage failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_topic(topic: str) -> str:
    """Return the canonical exact-deduplication form for a Topic."""

    normalized = unicodedata.normalize("NFKC", str(topic or "")).casefold()
    words = "".join(character if character.isalnum() else " " for character in normalized)
    return " ".join(words.split())


def topic_fingerprint(topic: str) -> str:
    normalized = normalize_topic(topic)
    if not normalized:
        raise StickmanTopicLedgerError(
            "invalid_topic",
            "selected_topic must contain text",
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StickmanTopicLedgerError(
            "invalid_input",
            f"{field} is required",
        )
    return text


async def _ledger_location(
    *,
    entity_id: str,
    workspace_id: str,
) -> tuple[Any, str, str]:
    if not entity_id or not workspace_id:
        raise StickmanTopicLedgerError(
            "missing_workspace_context",
            "Entity and Workspace context are required",
        )
    directory = await ensure_workspace_artifact_directory(
        entity_id=entity_id,
        workspace_id=workspace_id,
        directory_path=LEDGER_DIRECTORY,
    )
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        raise StickmanTopicLedgerError(
            "filesystem_unavailable",
            "Workspace filesystem is not enabled",
        )
    ledger_root = os.path.realpath(os.path.join(entity_root, directory.storage_path))
    root = os.path.realpath(entity_root)
    if os.path.commonpath([root, ledger_root]) != root:
        raise StickmanTopicLedgerError(
            "invalid_ledger_path",
            "Topic Ledger path escaped the Workspace filesystem",
        )
    os.makedirs(ledger_root, exist_ok=True)
    return directory, ledger_root, root


def _read_json_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise StickmanTopicLedgerError(
            "ledger_unreadable",
            f"Topic Ledger record is unreadable: {os.path.basename(path)}",
        ) from exc
    if not isinstance(value, dict):
        raise StickmanTopicLedgerError(
            "ledger_unreadable",
            f"Topic Ledger record is not a JSON object: {os.path.basename(path)}",
        )
    return value


def _record_files(ledger_root: str) -> list[str]:
    paths: list[str] = []
    for directory_name in (RESERVATION_DIRECTORY, EVENT_DIRECTORY):
        directory = os.path.join(ledger_root, directory_name)
        if not os.path.isdir(directory):
            continue
        paths.extend(os.path.join(directory, name) for name in os.listdir(directory) if name.endswith(".json"))
    return sorted(paths)


def _reservation_records(ledger_root: str) -> list[dict[str, Any]]:
    reservations: list[dict[str, Any]] = []
    directory = os.path.join(ledger_root, RESERVATION_DIRECTORY)
    if not os.path.isdir(directory):
        return reservations
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        record = _read_json_file(os.path.join(directory, name))
        if record.get("entry_type") != "topic_reservation":
            raise StickmanTopicLedgerError(
                "ledger_unreadable",
                f"Unexpected Topic Ledger reservation record: {name}",
            )
        reservations.append(record)
    return reservations


def topic_ledger_workspace_id_for_document(document: Any) -> str | None:
    """Return the Workspace id when ``document`` is the live Ledger view."""

    metadata = getattr(document, "metadata_", None)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("blueprint_knowledge_pack_slug") != LIVE_LEDGER_PACK_SLUG:
        return None
    if metadata.get("blueprint_starter_path") != LIVE_LEDGER_DOCUMENT_PATH:
        return None
    template = metadata.get("blueprint_template")
    if isinstance(template, dict) and (
        template.get("id") != LIVE_LEDGER_TEMPLATE_ID
        or template.get("mode") != "live_projection"
        or template.get("renderer") != LIVE_LEDGER_RENDERER
    ):
        return None
    origin = metadata.get("origin")
    if not isinstance(origin, dict):
        return None
    workspace_id = str(origin.get("workspace_id") or "").strip()
    return workspace_id or None


def _topic_rows(
    reservations: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events_by_reservation: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("entry_type") != "topic_status":
            continue
        reservation_id = str(record.get("reservation_id") or "").strip()
        if reservation_id:
            events_by_reservation.setdefault(reservation_id, []).append(record)

    rows: list[dict[str, Any]] = []
    status_rank = {"reserved": 1, "used_backfill": 1, "video_ready": 2, "youtube_saved": 3}
    for reservation in reservations:
        reservation_id = str(reservation.get("entry_id") or "").strip()
        status = str(reservation.get("status") or "reserved")
        latest_at = str(reservation.get("recorded_at") or "")
        details: dict[str, Any] = {}
        source_task_id = reservation.get("source_task_id")
        for event in sorted(
            events_by_reservation.get(reservation_id, []),
            key=lambda item: str(item.get("recorded_at") or ""),
        ):
            event_status = str(event.get("status") or "")
            if status_rank.get(event_status, 0) >= status_rank.get(status, 0):
                status = event_status
                candidate_details = event.get("details")
                if isinstance(candidate_details, dict):
                    details.update(candidate_details)
                latest_at = str(event.get("recorded_at") or latest_at)
                source_task_id = event.get("source_task_id") or source_task_id

        video_id = str(details.get("video_id") or "").strip()
        watch_url = str(details.get("watch_url") or "").strip()
        if not watch_url and re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
        rows.append({
            "reservation_id": reservation_id,
            "run_key": reservation.get("run_key"),
            "selected_topic": str(reservation.get("selected_topic") or ""),
            "youtube_title": str(reservation.get("youtube_title") or ""),
            "status": status,
            "recorded_at": str(reservation.get("recorded_at") or ""),
            "latest_at": latest_at,
            "video_source": str(details.get("video_source") or "").strip(),
            "video_id": video_id,
            "watch_url": watch_url,
            "visibility": str(details.get("visibility") or "").strip().lower(),
            "source_task_id": source_task_id,
        })
    return sorted(rows, key=lambda item: item["recorded_at"], reverse=True)


async def read_topic_ledger(
    *,
    entity_id: str,
    workspace_id: str,
    recent_limit: int = 100,
) -> dict[str, Any]:
    """Read exact used Topics and recent append-only records from Knowledge."""

    _, ledger_root, _ = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    reservations = _reservation_records(ledger_root)
    records = [_read_json_file(path) for path in _record_files(ledger_root)]
    records.sort(key=lambda item: str(item.get("recorded_at") or ""))
    topics = _topic_rows(reservations, records)
    limit = max(1, min(int(recent_limit or 100), 500))
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "run_key": generate_ulid(),
        "entry_count": len(records),
        "used_topic_count": len(reservations),
        "used_topics": [
            str(item.get("selected_topic") or "")
            for item in reservations
            if str(item.get("selected_topic") or "").strip()
        ],
        "video_ready_count": sum(
            item["status"] in {"video_ready", "youtube_saved"}
            for item in topics
        ),
        "youtube_saved_count": sum(
            item["status"] == "youtube_saved"
            for item in topics
        ),
        "topics": topics,
        "recent_entries": records[-limit:],
    }


def _markdown_cell(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _safe_ledger_link(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("/api/v1/fs/"):
        return quote(raw, safe="/:?=&%#-._~")
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return quote(raw, safe="/:?=&%#-._~")
    return ""


def _display_recorded_at(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return _markdown_cell(raw)


async def render_topic_ledger_markdown(
    *,
    entity_id: str,
    workspace_id: str,
) -> str:
    """Render the immutable records as a live operator-facing list."""

    ledger = await read_topic_ledger(
        entity_id=entity_id,
        workspace_id=workspace_id,
        recent_limit=500,
    )
    lines = [
        "# Stickman Topic & Video Ledger",
        "",
        "> Live view derived from immutable Workspace Topic records. A reserved Topic counts as used; this page is not an installation-time snapshot.",
        "",
        f"- Topics created or reserved: {ledger['used_topic_count']}",
        f"- Final videos ready: {ledger['video_ready_count']}",
        f"- Videos saved to YouTube: {ledger['youtube_saved_count']}",
        "",
    ]
    topics = ledger["topics"]
    if not topics:
        lines.extend([
            "No Topic has been created yet. The first successful reservation will appear here automatically.",
            "",
        ])
        return "\n".join(lines)

    lines.extend([
        "| Topic | Video | Status | Created |",
        "| --- | --- | --- | --- |",
    ])
    for topic in topics:
        status = topic["status"]
        if status == "youtube_saved":
            visibility = str(topic.get("visibility") or "").capitalize()
            status_label = "YouTube saved" + (f" · {visibility}" if visibility else "")
        elif status == "video_ready":
            status_label = "Video ready"
        elif status == "used_backfill":
            status_label = "Imported history"
        else:
            status_label = "Reserved"

        watch_url = _safe_ledger_link(topic.get("watch_url"))
        video_source = _safe_ledger_link(topic.get("video_source"))
        if watch_url:
            video_cell = f"[Watch on YouTube]({watch_url})"
        elif video_source:
            video_cell = f"[Open final video]({video_source})"
        else:
            video_cell = "—"
        lines.append(
            "| "
            + " | ".join([
                _markdown_cell(topic["selected_topic"]),
                video_cell,
                _markdown_cell(status_label),
                _display_recorded_at(topic["recorded_at"]),
            ])
            + " |"
        )
    lines.extend([
        "",
        "## Source of truth",
        "",
        "Reservations live under `topic-ledger/reservations/`; video-ready and YouTube-saved events live under `topic-ledger/events/`. Those JSON records remain append-only and idempotent.",
        "",
    ])
    return "\n".join(lines)


async def list_saved_youtube_publications(
    *,
    entity_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Return one durable successful public-upload event per YouTube video."""

    _, ledger_root, _ = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    records = [_read_json_file(path) for path in _record_files(ledger_root)]
    saved_by_identity: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("entry_type") != "topic_status" or record.get("status") != "youtube_saved":
            continue
        details = record.get("details")
        if not isinstance(details, dict):
            continue
        success = details.get("success")
        success_ready = success is True or str(success or "").strip().lower() in {
            "1", "true", "yes", "ready", "success",
        }
        visibility = str(details.get("visibility") or "").strip().lower()
        if not success_ready or visibility != "public":
            continue
        video_id = str(details.get("video_id") or "").strip()
        watch_url = str(details.get("watch_url") or "").strip()
        identity = video_id or watch_url
        if not identity:
            continue
        saved_by_identity[identity] = {
            "video_id": video_id,
            "watch_url": watch_url,
            "recorded_at": str(record.get("recorded_at") or ""),
            "source_task_id": record.get("source_task_id"),
        }
    return sorted(
        saved_by_identity.values(),
        key=lambda item: item["recorded_at"],
    )


def _candidate_records(candidates: Any) -> list[dict[str, Any]]:
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise StickmanTopicLedgerError(
            "invalid_candidates",
            "Exactly five generated Topic candidates are required",
        )
    normalized: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise StickmanTopicLedgerError(
                "invalid_candidates",
                "Every Topic candidate must be an object",
            )
        topic = _required_text(candidate.get("topic"), "candidate.topic")
        canonical = normalize_topic(topic)
        if canonical in seen_topics:
            raise StickmanTopicLedgerError(
                "invalid_candidates",
                "Generated Topic candidates must be unique",
            )
        seen_topics.add(canonical)
        normalized.append(dict(candidate))
    return normalized


def _exclusive_json_write(path: str, record: dict[str, Any]) -> bool:
    """Publish one complete immutable JSON record unless its path exists."""

    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = os.path.join(
        os.path.dirname(path),
        f".{os.path.basename(path)}.{generate_ulid()}.tmp",
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        # A same-directory hard link is an atomic create-if-absent operation.
        # The public record is therefore never visible while partially written.
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


async def _sync_record(
    *,
    entity_id: str,
    workspace_id: str,
    entity_root: str,
    abs_path: str,
    agent_id: str | None,
    task_id: str | None,
    conversation_id: str | None,
    user_id: str | None,
) -> str | None:
    result = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source=LEDGER_SOURCE,
        created_by=agent_id or "stickman_topic_ledger",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name="record_stickman_topic_ledger",
    )
    if not bool(getattr(result, "synced", False)):
        raise StickmanTopicLedgerError(
            "knowledge_sync_failed",
            f"Topic Ledger record could not be synced to Workspace Knowledge: {getattr(result, 'reason', 'unknown')}",
        )
    return getattr(result, "document_id", None)


async def reserve_topic(
    *,
    entity_id: str,
    workspace_id: str,
    run_key: str,
    candidates: Any,
    selected_topic: str,
    youtube_title: str,
    youtube_description: str,
    selection_reason: str,
    content_direction: str = "",
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Atomically reserve one fresh Topic before paid video generation."""

    clean_run_key = _required_text(run_key, "run_key")
    if not _RUN_KEY.fullmatch(clean_run_key):
        raise StickmanTopicLedgerError(
            "invalid_run_key",
            "run_key has an invalid format",
        )
    clean_candidates = _candidate_records(candidates)
    clean_topic = _required_text(selected_topic, "selected_topic")
    if clean_topic not in [str(item["topic"]).strip() for item in clean_candidates]:
        raise StickmanTopicLedgerError(
            "selected_topic_not_in_candidates",
            "selected_topic must exactly match one generated candidate",
        )
    fingerprint = topic_fingerprint(clean_topic)
    directory, ledger_root, entity_root = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    relative_name = f"{RESERVATION_DIRECTORY}/topic-{fingerprint}.json"
    abs_path = os.path.join(ledger_root, relative_name)
    entry_id = generate_ulid()
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "entry_type": "topic_reservation",
        "entry_id": entry_id,
        "run_key": clean_run_key,
        "recorded_at": _now_iso(),
        "status": "reserved",
        "selected_topic": clean_topic,
        "normalized_topic": normalize_topic(clean_topic),
        "topic_fingerprint": fingerprint,
        "candidates": clean_candidates,
        "youtube_title": _required_text(youtube_title, "youtube_title"),
        "youtube_description": _required_text(
            youtube_description,
            "youtube_description",
        ),
        "selection_reason": _required_text(selection_reason, "selection_reason"),
        "content_direction": str(content_direction or "").strip(),
        "source_task_id": task_id,
    }
    created = _exclusive_json_write(abs_path, record)
    if not created:
        existing = _read_json_file(abs_path)
        if existing.get("run_key") == clean_run_key:
            return {
                "ok": True,
                "idempotent": True,
                "entry_id": existing.get("entry_id"),
                "selected_topic": existing.get("selected_topic"),
                "status": existing.get("status"),
                "path": f"{directory.storage_path}/{relative_name}",
                "display_path": f"{directory.display_path}/{relative_name}",
            }
        return {
            "ok": False,
            "code": "topic_already_used",
            "message": "The selected Topic is already reserved or used in this Workspace",
            "selected_topic": clean_topic,
            "existing_entry_id": existing.get("entry_id"),
            "existing_recorded_at": existing.get("recorded_at"),
        }

    try:
        document_id = await _sync_record(
            entity_id=entity_id,
            workspace_id=workspace_id,
            entity_root=entity_root,
            abs_path=abs_path,
            agent_id=agent_id,
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
    except Exception:
        try:
            os.unlink(abs_path)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "idempotent": False,
        "entry_id": entry_id,
        "selected_topic": clean_topic,
        "status": "reserved",
        "path": f"{directory.storage_path}/{relative_name}",
        "display_path": f"{directory.display_path}/{relative_name}",
        "document_id": document_id,
    }


async def backfill_used_topic(
    *,
    entity_id: str,
    workspace_id: str,
    selected_topic: str,
    evidence: dict[str, Any],
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Import one evidence-backed historical Topic into the immutable ledger."""

    clean_topic = _required_text(selected_topic, "selected_topic")
    clean_evidence = dict(evidence or {})
    source_artifacts = clean_evidence.get("source_artifacts")
    if not isinstance(source_artifacts, list) or not any(str(value or "").strip() for value in source_artifacts):
        raise StickmanTopicLedgerError(
            "backfill_evidence_required",
            "backfill_used requires at least one source_artifacts evidence path or URL",
        )
    fingerprint = topic_fingerprint(clean_topic)
    directory, ledger_root, entity_root = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    relative_name = f"{RESERVATION_DIRECTORY}/topic-{fingerprint}.json"
    abs_path = os.path.join(ledger_root, relative_name)
    entry_id = generate_ulid()
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "entry_type": "topic_reservation",
        "entry_id": entry_id,
        "run_key": f"backfill-{entry_id}",
        "recorded_at": _now_iso(),
        "status": "used_backfill",
        "selected_topic": clean_topic,
        "normalized_topic": normalize_topic(clean_topic),
        "topic_fingerprint": fingerprint,
        "candidates": [],
        "evidence": clean_evidence,
        "source_task_id": task_id,
    }
    created = _exclusive_json_write(abs_path, record)
    if not created:
        existing = _read_json_file(abs_path)
        if existing.get("status") == "used_backfill":
            return {
                "ok": True,
                "idempotent": True,
                "entry_id": existing.get("entry_id"),
                "selected_topic": existing.get("selected_topic"),
                "status": existing.get("status"),
                "path": f"{directory.storage_path}/{relative_name}",
                "display_path": f"{directory.display_path}/{relative_name}",
            }
        return {
            "ok": False,
            "code": "topic_already_used",
            "message": "The historical Topic already exists in the Workspace Topic Ledger",
            "selected_topic": clean_topic,
            "existing_entry_id": existing.get("entry_id"),
            "existing_recorded_at": existing.get("recorded_at"),
        }
    try:
        document_id = await _sync_record(
            entity_id=entity_id,
            workspace_id=workspace_id,
            entity_root=entity_root,
            abs_path=abs_path,
            agent_id=agent_id,
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
    except Exception:
        try:
            os.unlink(abs_path)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "idempotent": False,
        "entry_id": entry_id,
        "selected_topic": clean_topic,
        "status": "used_backfill",
        "path": f"{directory.storage_path}/{relative_name}",
        "display_path": f"{directory.display_path}/{relative_name}",
        "document_id": document_id,
    }


async def record_topic_event(
    *,
    entity_id: str,
    workspace_id: str,
    event: TopicLedgerEvent,
    reservation_id: str,
    selected_topic: str,
    details: dict[str, Any] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Append one idempotent status event for an existing Topic reservation."""

    if event not in {"video_ready", "youtube_saved"}:
        raise StickmanTopicLedgerError(
            "invalid_event",
            "event must be video_ready or youtube_saved",
        )
    clean_reservation_id = _required_text(reservation_id, "reservation_id")
    clean_topic = _required_text(selected_topic, "selected_topic")
    fingerprint = topic_fingerprint(clean_topic)
    directory, ledger_root, entity_root = await _ledger_location(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    reservation_path = os.path.join(
        ledger_root,
        RESERVATION_DIRECTORY,
        f"topic-{fingerprint}.json",
    )
    if not os.path.isfile(reservation_path):
        raise StickmanTopicLedgerError(
            "reservation_missing",
            "No Topic reservation exists for selected_topic",
        )
    reservation = _read_json_file(reservation_path)
    if reservation.get("entry_id") != clean_reservation_id:
        raise StickmanTopicLedgerError(
            "reservation_mismatch",
            "reservation_id does not match selected_topic",
        )

    relative_name = f"{EVENT_DIRECTORY}/{clean_reservation_id}-{event}.json"
    abs_path = os.path.join(ledger_root, relative_name)
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "entry_type": "topic_status",
        "event_id": generate_ulid(),
        "reservation_id": clean_reservation_id,
        "recorded_at": _now_iso(),
        "status": event,
        "selected_topic": clean_topic,
        "details": dict(details or {}),
        "source_task_id": task_id,
    }
    created = _exclusive_json_write(abs_path, record)
    if not created:
        existing = _read_json_file(abs_path)
        return {
            "ok": True,
            "idempotent": True,
            "event_id": existing.get("event_id"),
            "reservation_id": clean_reservation_id,
            "selected_topic": clean_topic,
            "status": existing.get("status"),
            "path": f"{directory.storage_path}/{relative_name}",
            "display_path": f"{directory.display_path}/{relative_name}",
        }
    try:
        document_id = await _sync_record(
            entity_id=entity_id,
            workspace_id=workspace_id,
            entity_root=entity_root,
            abs_path=abs_path,
            agent_id=agent_id,
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
        )
    except Exception:
        try:
            os.unlink(abs_path)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "idempotent": False,
        "event_id": record["event_id"],
        "reservation_id": clean_reservation_id,
        "selected_topic": clean_topic,
        "status": event,
        "path": f"{directory.storage_path}/{relative_name}",
        "display_path": f"{directory.display_path}/{relative_name}",
        "document_id": document_id,
    }
