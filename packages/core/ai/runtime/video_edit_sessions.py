"""Durable ownership and event state for interactive Video Edit sandboxes."""

from __future__ import annotations

import time
import uuid
from typing import Any


# Keep ownership/recovery metadata longer than the short Sandbox idle lease.
# A pruned container can therefore be recreated from durable Manor source on a
# later turn without keeping an idle container alive all day.
VIDEO_EDIT_SESSION_TTL = 24 * 3600
VIDEO_EDIT_SESSION_EVENT_LIMIT = 100
_SESSION_PREFIX = "video-edit:session:"
_CONVERSATION_PREFIX = "video-edit:conversation:"

# Redis is the cross-worker source of truth in the normal Manor stack.  The
# process-local copy keeps a just-created session usable in tests and in a
# degraded single-process installation when Redis is temporarily unavailable.
_LOCAL_SESSIONS: dict[str, dict[str, Any]] = {}
_LOCAL_CONVERSATIONS: dict[str, str] = {}


def new_video_edit_session(
    *,
    sandbox_id: str,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    project_path: str,
    workspace_id: str | None = None,
    legacy_external_authoring: bool = False,
) -> dict[str, Any]:
    now = time.time()
    return {
        "version": 1,
        "session_id": f"ved_{uuid.uuid4().hex[:24]}",
        "sandbox_id": sandbox_id,
        "entity_id": entity_id,
        "user_id": str(user_id or ""),
        "conversation_id": str(conversation_id or ""),
        "workspace_id": str(workspace_id or ""),
        "project_path": project_path,
        "legacy_external_authoring": bool(legacy_external_authoring),
        "created_at": now,
        "updated_at": now,
        "last_event_seq": 0,
        "events": [],
        "source_revision": 0,
        "last_synced_sha256": "",
    }


def assert_video_edit_session_owner(
    state: dict[str, Any],
    *,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
) -> None:
    if str(state.get("entity_id") or "") != str(entity_id or ""):
        raise PermissionError("Video Edit session belongs to a different entity")
    owner_user = str(state.get("user_id") or "")
    current_user = str(user_id or "")
    if owner_user and current_user and owner_user != current_user:
        raise PermissionError("Video Edit session belongs to a different user")
    owner_conversation = str(state.get("conversation_id") or "")
    current_conversation = str(conversation_id or "")
    if owner_conversation and current_conversation and owner_conversation != current_conversation:
        raise PermissionError("Video Edit session belongs to a different conversation")


def append_video_edit_session_event(
    state: dict[str, Any],
    event_type: str,
    *,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sequence = int(state.get("last_event_seq") or 0) + 1
    event = {
        "seq": sequence,
        "type": str(event_type or "progress"),
        "message": str(message or ""),
        "created_at": time.time(),
    }
    if data:
        event["data"] = dict(data)
    events = [item for item in state.get("events", []) if isinstance(item, dict)]
    events.append(event)
    state["events"] = events[-VIDEO_EDIT_SESSION_EVENT_LIMIT:]
    state["last_event_seq"] = sequence
    state["updated_at"] = event["created_at"]
    return event


def video_edit_session_events(
    state: dict[str, Any],
    *,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in state.get("events", [])
        if isinstance(item, dict) and int(item.get("seq") or 0) > max(0, int(after_seq))
    ]


async def save_video_edit_session(state: dict[str, Any]) -> None:
    session_id = str(state.get("session_id") or "")
    if not session_id:
        raise ValueError("Video Edit session state is missing session_id")
    state["updated_at"] = time.time()
    snapshot = dict(state)
    snapshot["events"] = [dict(item) for item in state.get("events", []) if isinstance(item, dict)]
    _LOCAL_SESSIONS[session_id] = snapshot
    conversation_id = str(state.get("conversation_id") or "")
    if conversation_id:
        _LOCAL_CONVERSATIONS[conversation_id] = session_id
    try:
        from packages.core.cache import cache

        await cache.set(f"{_SESSION_PREFIX}{session_id}", snapshot, ttl=VIDEO_EDIT_SESSION_TTL)
        if conversation_id:
            await cache.set(
                f"{_CONVERSATION_PREFIX}{conversation_id}",
                {"session_id": session_id},
                ttl=VIDEO_EDIT_SESSION_TTL,
            )
    except Exception:
        pass


async def load_video_edit_session(session_id: str) -> dict[str, Any] | None:
    normalized = str(session_id or "").strip()
    if not normalized:
        return None
    cached: Any = None
    try:
        from packages.core.cache import cache

        cached = await cache.get(f"{_SESSION_PREFIX}{normalized}")
    except Exception:
        cached = None
    if isinstance(cached, dict):
        _LOCAL_SESSIONS[normalized] = dict(cached)
        return dict(cached)
    local = _LOCAL_SESSIONS.get(normalized)
    if not isinstance(local, dict):
        return None
    updated_at = float(local.get("updated_at") or local.get("created_at") or 0)
    if updated_at and time.time() - updated_at > VIDEO_EDIT_SESSION_TTL:
        _LOCAL_SESSIONS.pop(normalized, None)
        return None
    return dict(local)


async def load_conversation_video_edit_session(conversation_id: str) -> dict[str, Any] | None:
    normalized = str(conversation_id or "").strip()
    if not normalized:
        return None
    session_id = ""
    try:
        from packages.core.cache import cache

        cached = await cache.get(f"{_CONVERSATION_PREFIX}{normalized}")
        if isinstance(cached, dict):
            session_id = str(cached.get("session_id") or "")
    except Exception:
        session_id = ""
    session_id = session_id or _LOCAL_CONVERSATIONS.get(normalized, "")
    return await load_video_edit_session(session_id) if session_id else None


async def delete_video_edit_session(state: dict[str, Any]) -> None:
    session_id = str(state.get("session_id") or "")
    conversation_id = str(state.get("conversation_id") or "")
    _LOCAL_SESSIONS.pop(session_id, None)
    if conversation_id and _LOCAL_CONVERSATIONS.get(conversation_id) == session_id:
        _LOCAL_CONVERSATIONS.pop(conversation_id, None)
    try:
        from packages.core.cache import cache

        if session_id:
            await cache.delete(f"{_SESSION_PREFIX}{session_id}")
        if conversation_id:
            current = await cache.get(f"{_CONVERSATION_PREFIX}{conversation_id}")
            if isinstance(current, dict) and current.get("session_id") == session_id:
                await cache.delete(f"{_CONVERSATION_PREFIX}{conversation_id}")
    except Exception:
        pass
