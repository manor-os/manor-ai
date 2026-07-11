"""Unit tests for presence service (no DB required)."""

import time

from packages.core.services import presence_service
from packages.core.services.presence_service import (
    update_presence,
    get_online_users,
    get_viewers,
    remove_presence,
)


def _clear():
    """Reset global presence state between tests."""
    presence_service._presence.clear()


def test_update_and_get_presence():
    _clear()
    update_presence("e1", "u1", username="Alice", status="online")
    update_presence("e1", "u2", username="Bob", status="busy")

    users = get_online_users("e1")
    assert len(users) == 2
    names = {u["username"] for u in users}
    assert names == {"Alice", "Bob"}

    # Different entity should be empty
    assert get_online_users("e2") == []

    # Remove one user
    remove_presence("e1", "u1")
    users = get_online_users("e1")
    assert len(users) == 1
    assert users[0]["username"] == "Bob"


def test_stale_presence_cleanup():
    _clear()
    update_presence("e1", "u1", username="Alice")
    update_presence("e1", "u2", username="Bob")

    # Make u1 stale by backdating last_seen
    presence_service._presence["e1"]["u1"]["last_seen"] = time.time() - 120

    users = get_online_users("e1")
    assert len(users) == 1
    assert users[0]["username"] == "Bob"

    # Stale entry should be removed from the store
    assert "u1" not in presence_service._presence["e1"]


def test_viewers_filter():
    _clear()
    update_presence("e1", "u1", username="Alice", viewing="task:abc123")
    update_presence("e1", "u2", username="Bob", viewing="task:abc123")
    update_presence("e1", "u3", username="Carol", viewing="document:xyz")
    update_presence("e1", "u4", username="Dave")  # not viewing anything

    viewers = get_viewers("e1", "task:abc123")
    assert len(viewers) == 2
    names = {v["username"] for v in viewers}
    assert names == {"Alice", "Bob"}

    viewers = get_viewers("e1", "document:xyz")
    assert len(viewers) == 1
    assert viewers[0]["username"] == "Carol"

    # Non-existent resource
    assert get_viewers("e1", "task:nope") == []
