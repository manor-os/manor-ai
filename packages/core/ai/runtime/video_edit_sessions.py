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
# Version 2 binds its owner strictly (and records the acting agent for audit);
# older records allowed an empty user or conversation and cannot prove their owner.
VIDEO_EDIT_SESSION_RECORD_VERSION = 2
_SESSION_PREFIX = "video-edit:session:"
_CONVERSATION_PREFIX = "video-edit:conversation:"
# The owner's current session: one Video Edit session per owner, so a start
# anywhere replaces it and an older record can never revive its container.
_OWNER_PREFIX = "video-edit:owner:"

# Redis is the cross-worker source of truth in the normal Manor stack.  The
# process-local copy keeps a just-created session usable in tests and in a
# degraded single-process installation when Redis is temporarily unavailable.
_LOCAL_SESSIONS: dict[str, dict[str, Any]] = {}
_LOCAL_CONVERSATIONS: dict[str, str] = {}
_LOCAL_OWNERS: dict[str, str] = {}


def video_edit_owner_key(*, entity_id: str | None, user_id: str | None, task_key: str | None) -> str:
    """Who owns a Video Edit session: the user, or (user-less) the entity's task.

    A user owns one session across all their conversations; a user-less run
    (scheduled work, workflows) is owned per task or run by its workspace.
    Empty when no owner can be named: such a caller owns nothing.
    """

    entity = str(entity_id or "").strip()
    user = str(user_id or "").strip()
    task = str(task_key or "").strip()
    if not entity or not (user or task):
        return ""
    return f"{entity}:user:{user}" if user else f"{entity}:task:{task}"


def new_video_edit_session(
    *,
    sandbox_id: str,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    agent_id: str | None,
    project_path: str,
    workspace_id: str | None = None,
    legacy_external_authoring: bool = False,
) -> dict[str, Any]:
    """A session record owned by the user (or, user-less, the entity's task).

    ``conversation_id`` (the run's sandbox binding key: the conversation of a
    chat run, the synthetic key of a task run) and ``agent_id`` record where
    and by whom it was started: attribution, not ownership -- except that a
    user-less session's task key is its owner."""

    owner_key = video_edit_owner_key(entity_id=entity_id, user_id=user_id, task_key=conversation_id)
    if not owner_key:
        raise PermissionError("A Video Edit session needs an entity and a user (or, user-less, a task)")
    now = time.time()
    return {
        "version": VIDEO_EDIT_SESSION_RECORD_VERSION,
        "session_id": f"ved_{uuid.uuid4().hex[:24]}",
        "sandbox_id": sandbox_id,
        "entity_id": entity_id,
        "user_id": str(user_id or ""),
        "conversation_id": str(conversation_id or ""),
        "agent_id": str(agent_id or ""),
        "owner_key": owner_key,
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
    """Fail closed unless the caller is exactly the session's owner.

    The owner is the user (entity + user), in any of their conversations and
    for any of their agents; conversation and agent are attribution only. A
    user-less run is owned per task: entity + its task key. Nothing is
    skipped because one side is empty: a caller with no entity, or with
    neither a user nor a task key, owns nothing, and a user-less caller never
    owns a user's session (or the other way round). A record from before the
    strict binding is refused: its owner cannot be proven.
    """

    if int(state.get("version") or 0) < VIDEO_EDIT_SESSION_RECORD_VERSION:
        raise PermissionError("Video Edit session predates owner binding; call start again")
    caller = video_edit_owner_key(entity_id=entity_id, user_id=user_id, task_key=conversation_id)
    if not caller:
        raise PermissionError("Video Edit sessions can only be used by a user or a task run")
    recorded = video_edit_owner_key(
        entity_id=state.get("entity_id"), user_id=state.get("user_id"), task_key=state.get("conversation_id"),
    )
    if recorded != caller:
        raise PermissionError("Video Edit session belongs to a different owner")


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


async def make_current_video_edit_session(state: dict[str, Any]) -> None:
    """Point the owner at this (newly started) session.

    Only a start moves the pointer. ``save_video_edit_session`` never does, so
    touching an older record cannot make it current again.
    """

    session_id = str(state.get("session_id") or "")
    owner_key = str(state.get("owner_key") or "")
    if not session_id or not owner_key:
        return
    _LOCAL_OWNERS[owner_key] = session_id
    try:
        from packages.core.cache import cache

        await cache.set(f"{_OWNER_PREFIX}{owner_key}", {"session_id": session_id}, ttl=VIDEO_EDIT_SESSION_TTL)
    except Exception:
        pass


async def current_video_edit_session_id(owner_key: str) -> str | None:
    """The owner's current session id, or None when it is unknown."""

    normalized = str(owner_key or "").strip()
    if not normalized:
        return None
    try:
        from packages.core.cache import cache

        cached = await cache.get(f"{_OWNER_PREFIX}{normalized}")
        if isinstance(cached, dict) and cached.get("session_id"):
            return str(cached["session_id"])
    except Exception:
        pass
    return _LOCAL_OWNERS.get(normalized)


async def load_owner_video_edit_session(owner_key: str) -> dict[str, Any] | None:
    """The owner's current session record, if it is still alive."""

    session_id = await current_video_edit_session_id(owner_key)
    return await load_video_edit_session(session_id) if session_id else None


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


_CLOSED_PREFIX = "video-edit:closed:"
_LOCAL_CLOSED: dict[str, dict[str, Any]] = {}
_CLOSED_FIELDS = (
    "version", "session_id", "entity_id", "user_id", "conversation_id", "owner_key",
    "workspace_id", "project_path", "last_review_sha256",
)


async def close_video_edit_session(state: dict[str, Any]) -> None:
    """Drop a session whose sandbox is gone, keeping what its review approved.

    A later step may render the hash this session reviewed (closing at the end
    of a review-only step is easy to do). The closed record names the owner,
    the project and that hash, nothing that could revive a container.
    """

    session_id = str(state.get("session_id") or "")
    if session_id and str(state.get("last_review_sha256") or ""):
        closed = {key: state.get(key) for key in _CLOSED_FIELDS}
        closed["closed_at"] = time.time()
        _LOCAL_CLOSED[session_id] = closed
        try:
            from packages.core.cache import cache

            await cache.set(f"{_CLOSED_PREFIX}{session_id}", closed, ttl=VIDEO_EDIT_SESSION_TTL)
        except Exception:
            pass
    await delete_video_edit_session(state)


async def load_closed_video_edit_session(session_id: str) -> dict[str, Any] | None:
    """The closed record of a session that had a reviewed hash, if still kept."""

    normalized = str(session_id or "").strip()
    if not normalized:
        return None
    try:
        from packages.core.cache import cache

        cached = await cache.get(f"{_CLOSED_PREFIX}{normalized}")
        if isinstance(cached, dict):
            return dict(cached)
    except Exception:
        pass
    local = _LOCAL_CLOSED.get(normalized)
    if not isinstance(local, dict):
        return None
    if time.time() - float(local.get("closed_at") or 0) > VIDEO_EDIT_SESSION_TTL:
        _LOCAL_CLOSED.pop(normalized, None)
        return None
    return dict(local)


async def delete_video_edit_session(state: dict[str, Any]) -> None:
    session_id = str(state.get("session_id") or "")
    conversation_id = str(state.get("conversation_id") or "")
    owner_key = str(state.get("owner_key") or "")
    _LOCAL_SESSIONS.pop(session_id, None)
    if conversation_id and _LOCAL_CONVERSATIONS.get(conversation_id) == session_id:
        _LOCAL_CONVERSATIONS.pop(conversation_id, None)
    if owner_key and _LOCAL_OWNERS.get(owner_key) == session_id:
        _LOCAL_OWNERS.pop(owner_key, None)
    try:
        from packages.core.cache import cache

        if session_id:
            await cache.delete(f"{_SESSION_PREFIX}{session_id}")
        if conversation_id:
            current = await cache.get(f"{_CONVERSATION_PREFIX}{conversation_id}")
            if isinstance(current, dict) and current.get("session_id") == session_id:
                await cache.delete(f"{_CONVERSATION_PREFIX}{conversation_id}")
        if owner_key:
            current = await cache.get(f"{_OWNER_PREFIX}{owner_key}")
            if isinstance(current, dict) and current.get("session_id") == session_id:
                await cache.delete(f"{_OWNER_PREFIX}{owner_key}")
    except Exception:
        pass
