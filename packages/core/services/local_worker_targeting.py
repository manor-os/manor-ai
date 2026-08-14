"""User-facing naming and deterministic routing for paired local workers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from typing import Any, Callable, Iterable

from packages.core.constants.execution import WorkerStatus


LOCAL_WORKER_HEARTBEAT_TTL_SECONDS = 60
_LEGACY_CLI_PREFIX = re.compile(r"^\s*cli\s*[·•:\-]\s*", re.IGNORECASE)


def local_worker_display_name(value: Any) -> str:
    """Return the human machine name, hiding the legacy ``CLI ·`` prefix."""
    text = str(getattr(value, "display_name", value) or "").strip()
    return _LEGACY_CLI_PREFIX.sub("", text).strip() or text


def normalize_local_worker_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", local_worker_display_name(value))
    return " ".join(text.casefold().split())


def local_worker_is_online(
    worker: Any,
    *,
    now: datetime | None = None,
    heartbeat_ttl_seconds: int = LOCAL_WORKER_HEARTBEAT_TTL_SECONDS,
) -> bool:
    if str(getattr(worker, "status", "") or "") != "active":
        return False
    heartbeat = getattr(worker, "last_heartbeat_at", None)
    if not isinstance(heartbeat, datetime):
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    capabilities = getattr(worker, "capabilities", None)
    daemon = capabilities.get("daemon") if isinstance(capabilities, dict) else None
    if isinstance(daemon, dict) and daemon.get("running") is False:
        return False
    return heartbeat >= current - timedelta(seconds=heartbeat_ttl_seconds)


def _local_worker_recency_key(worker: Any) -> tuple[float, str]:
    heartbeat = getattr(worker, "last_heartbeat_at", None)
    if isinstance(heartbeat, datetime):
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        timestamp = heartbeat.timestamp()
    else:
        timestamp = 0.0
    return timestamp, str(getattr(worker, "id", "") or "")


def local_worker_name_matches(workers: Iterable[Any], requested_name: str) -> list[Any]:
    requested = normalize_local_worker_name(requested_name)
    if not requested:
        return []
    return [
        worker
        for worker in workers
        if normalize_local_worker_name(worker) == requested
    ]


def infer_local_worker_name_matches(workers: Iterable[Any], message: str) -> list[Any]:
    """Find configured machine names explicitly present in the message."""
    normalized_message = " ".join(
        unicodedata.normalize("NFKC", str(message or "")).casefold().split()
    )
    if not normalized_message:
        return []
    matches: list[tuple[int, int, Any]] = []
    for worker in workers:
        name = normalize_local_worker_name(worker)
        if not name:
            continue
        left_boundary = r"(?<![a-z0-9])" if name[0].isascii() and name[0].isalnum() else ""
        right_boundary = r"(?![a-z0-9])" if name[-1].isascii() and name[-1].isalnum() else ""
        pattern = re.compile(f"{left_boundary}{re.escape(name)}{right_boundary}")
        for match in pattern.finditer(normalized_message):
            matches.append((match.start(), match.end(), worker))
    if not matches:
        return []
    # Prefer a longer name when aliases overlap at the same mention (for
    # example, "MacBook" and "Office MacBook"), while retaining two names
    # mentioned in separate parts of the same request so routing can reject
    # the ambiguity instead of silently picking one.
    retained = [
        match
        for match in matches
        if not any(
            other_start <= match[0]
            and other_end >= match[1]
            and (other_end - other_start) > (match[1] - match[0])
            for other_start, other_end, _other_worker in matches
        )
    ]
    seen: set[str] = set()
    result: list[Any] = []
    for _start, _end, worker in retained:
        worker_id = str(getattr(worker, "id", "") or "")
        if worker_id not in seen:
            seen.add(worker_id)
            result.append(worker)
    return result


def local_worker_catalog(
    workers: Iterable[Any],
    *,
    supports: Callable[[Any], bool] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for worker in workers:
        online = local_worker_is_online(worker)
        rows.append(
            {
                "name": local_worker_display_name(worker),
                "status": "online" if online else "offline",
                "available": bool(online and (supports(worker) if supports else True)),
            }
        )
    return rows


@dataclass(frozen=True)
class LocalWorkerResolution:
    worker: Any | None = None
    error: dict[str, Any] | None = None
    explicit: bool = False


def resolve_local_worker_target(
    workers: Iterable[Any],
    *,
    requested_name: str | None = None,
    requested_message: str | None = None,
    remembered_worker_id: str | None = None,
    supports: Callable[[Any], bool] | None = None,
) -> LocalWorkerResolution:
    candidates = [
        worker
        for worker in workers
        if str(getattr(worker, "status", "") or "") != "revoked"
    ]
    catalog = local_worker_catalog(candidates, supports=supports)
    explicit_matches = local_worker_name_matches(candidates, requested_name or "")
    explicit = bool(str(requested_name or "").strip())
    if not explicit and requested_message:
        explicit_matches = infer_local_worker_name_matches(candidates, requested_message)
        explicit = bool(explicit_matches)

    if explicit:
        if not explicit_matches:
            return LocalWorkerResolution(
                error={
                    "status": "rejected",
                    "error": "machine_not_found",
                    "message": f"No paired computer is named '{local_worker_display_name(requested_name)}'.",
                    "available_machines": catalog,
                },
                explicit=True,
            )
        unique_ids = {str(getattr(worker, "id", "")) for worker in explicit_matches}
        if len(unique_ids) != 1:
            return LocalWorkerResolution(
                error={
                    "status": "rejected",
                    "error": "machine_name_ambiguous",
                    "message": "More than one paired computer has that name. Rename them before routing work.",
                    "available_machines": catalog,
                },
                explicit=True,
            )
        selected = explicit_matches[0]
        name = local_worker_display_name(selected)
        if not local_worker_is_online(selected):
            return LocalWorkerResolution(
                error={
                    "status": "rejected",
                    "error": "machine_offline",
                    "machine": name,
                    "message": f"The selected computer '{name}' is offline.",
                    "available_machines": catalog,
                },
                explicit=True,
            )
        if supports and not supports(selected):
            return LocalWorkerResolution(
                error={
                    "status": "rejected",
                    "error": "machine_tool_unavailable",
                    "machine": name,
                    "message": f"The selected computer '{name}' does not have the requested local tool ready.",
                    "available_machines": catalog,
                },
                explicit=True,
            )
        return LocalWorkerResolution(worker=selected, explicit=True)

    remembered_id = str(remembered_worker_id or "").strip()
    if remembered_id:
        remembered = next(
            (
                worker
                for worker in candidates
                if str(getattr(worker, "id", "") or "") == remembered_id
            ),
            None,
        )
        if remembered is not None and local_worker_is_online(remembered) and (
            supports(remembered) if supports else True
        ):
            return LocalWorkerResolution(worker=remembered)

    ready = [
        worker
        for worker in candidates
        if local_worker_is_online(worker) and (supports(worker) if supports else True)
    ]
    if ready:
        return LocalWorkerResolution(worker=max(ready, key=_local_worker_recency_key))
    if not candidates:
        return LocalWorkerResolution(
            error={
                "status": "rejected",
                "error": "no_paired_cli_worker",
                "message": "No paired local computer is available.",
                "available_machines": [],
            },
        )
    return LocalWorkerResolution(
        error={
            "status": "rejected",
            "error": "no_ready_cli_worker",
            "message": "No paired local computer is online with the requested tool ready.",
            "available_machines": catalog,
        },
    )


def conversation_local_worker_target(meta: Any, user_id: str | None) -> dict[str, Any] | None:
    targets = meta.get("local_worker_targets") if isinstance(meta, dict) else None
    entry = targets.get(str(user_id or "")) if isinstance(targets, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


async def remember_conversation_local_worker_target(
    db: Any,
    *,
    conversation_id: str | None,
    entity_id: str,
    user_id: str | None,
    worker: Any,
) -> None:
    if not conversation_id or not user_id:
        return
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from packages.core.models.task import Conversation

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        return
    meta = dict(conversation.meta or {}) if isinstance(conversation.meta, dict) else {}
    targets = dict(meta.get("local_worker_targets") or {})
    targets[str(user_id)] = {
        "worker_id": str(getattr(worker, "id", "") or ""),
        "display_name": local_worker_display_name(worker),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    meta["local_worker_targets"] = targets
    conversation.meta = meta
    flag_modified(conversation, "meta")


async def load_conversation_local_worker_target(
    db: Any,
    *,
    conversation_id: str | None,
    entity_id: str,
    user_id: str | None,
) -> dict[str, Any] | None:
    if not conversation_id or not user_id:
        return None
    from sqlalchemy import select
    from packages.core.models.task import Conversation

    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        return None
    return conversation_local_worker_target(conversation.meta, user_id)


async def select_conversation_local_worker_target(
    db: Any,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    worker_id: str,
) -> Any:
    """Validate and remember an explicitly requested computer target."""
    from sqlalchemy import select
    from packages.core.models.worker import Worker

    worker = (
        await db.execute(
            select(Worker).where(
                Worker.id == str(worker_id or "").strip(),
                Worker.entity_id == entity_id,
                Worker.created_by_user_id == user_id,
                Worker.kind == "custom_http",
                Worker.status != WorkerStatus.REVOKED,
            )
        )
    ).scalar_one_or_none()
    if worker is None:
        raise LookupError("Selected computer was not found")
    if not local_worker_is_online(worker):
        raise RuntimeError(
            f"The selected computer '{local_worker_display_name(worker)}' is offline"
        )
    await remember_conversation_local_worker_target(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        worker=worker,
    )
    return worker
