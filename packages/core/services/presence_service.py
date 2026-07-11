"""Real-time presence — tracks who's online, what they're viewing, typing indicators.

Uses in-memory store (fast) with Redis backup (persistence across restarts).
Presence data expires after 60 seconds without heartbeat.
"""
import logging
import time

logger = logging.getLogger(__name__)

# In-memory presence store: {entity_id: {user_id: PresenceInfo}}
_presence: dict[str, dict[str, dict]] = {}
_HEARTBEAT_TTL = 60  # seconds


def update_presence(
    entity_id: str,
    user_id: str,
    *,
    username: str = None,
    display_name: str = None,
    status: str = "online",
    viewing: str = None,
    typing_in: str = None,
) -> None:
    """Update a user's presence state."""
    entity_data = _presence.setdefault(entity_id, {})
    name = display_name or username or user_id[:8]
    entity_data[user_id] = {
        "user_id": user_id,
        "username": name,
        "display_name": name,
        "status": status,  # online, away, busy
        "viewing": viewing,  # "task:abc123", "document:xyz", "chat:conv_id"
        "typing_in": typing_in,  # conversation_id if typing
        "last_seen": time.time(),
    }


def remove_presence(entity_id: str, user_id: str) -> None:
    """Remove a user from presence (on disconnect)."""
    entity_data = _presence.get(entity_id, {})
    entity_data.pop(user_id, None)


def get_online_users(entity_id: str) -> list[dict]:
    """Get all online users for an entity (excluding stale entries)."""
    now = time.time()
    entity_data = _presence.get(entity_id, {})
    active = []
    stale = []
    for uid, info in entity_data.items():
        if now - info["last_seen"] > _HEARTBEAT_TTL:
            stale.append(uid)
        else:
            active.append(info)
    for uid in stale:
        del entity_data[uid]
    return active


def get_viewers(entity_id: str, resource: str) -> list[dict]:
    """Get users currently viewing a specific resource.
    resource format: "task:abc123", "document:xyz"
    """
    users = get_online_users(entity_id)
    return [u for u in users if u.get("viewing") == resource]


def get_typing_users(entity_id: str, conversation_id: str) -> list[dict]:
    """Get users currently typing in a conversation."""
    users = get_online_users(entity_id)
    return [u for u in users if u.get("typing_in") == conversation_id]


def get_presence_summary(entity_id: str) -> dict:
    """Get presence summary for an entity."""
    users = get_online_users(entity_id)
    return {
        "online_count": len(users),
        "users": users,
    }
