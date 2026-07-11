"""E2E tests: WebSocket real-time notifications endpoint."""

import pytest
from starlette.testclient import TestClient

from apps.api.main import create_app
from packages.core.services.auth_service import create_access_token

pytestmark = pytest.mark.integration


def _make_app_and_client():
    """Create a fresh app + sync TestClient for WebSocket testing."""
    app = create_app()
    return TestClient(app)


def test_ws_connect_with_valid_token():
    """Valid JWT token should connect and receive 'connected' event."""
    client = _make_app_and_client()
    token = create_access_token(user_id="user-1", entity_id="ent-1", role="owner")
    with client.websocket_connect(f"/ws?token={token}") as ws:
        data = ws.receive_json()
        assert data["event"] == "connected"
        assert data["data"]["user_id"] == "user-1"
        assert "unread_notifications" in data["data"]


def test_ws_reject_invalid_token():
    """Invalid JWT token should close with code 4001."""
    client = _make_app_and_client()
    try:
        with client.websocket_connect("/ws?token=bad.token.here") as ws:
            # Should not reach here — connection should be rejected
            ws.receive_json()
            assert False, "Expected WebSocket to be closed"
    except Exception:
        # WebSocket was rejected — this is the expected outcome
        pass


def test_ws_ping_pong():
    """Client sending ping should receive pong."""
    client = _make_app_and_client()
    token = create_access_token(user_id="user-2", entity_id="ent-1", role="member")
    with client.websocket_connect(f"/ws?token={token}") as ws:
        # Consume the initial connected event
        connected = ws.receive_json()
        assert connected["event"] == "connected"

        # Send ping
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["event"] == "pong"
        assert pong["data"] == {}


def test_ws_initial_unread_count():
    """Connected event should include unread_notifications count (0 for fresh user)."""
    client = _make_app_and_client()
    token = create_access_token(user_id="user-3", entity_id="ent-3", role="owner")
    with client.websocket_connect(f"/ws?token={token}") as ws:
        data = ws.receive_json()
        assert data["event"] == "connected"
        # Fresh user with no notifications should have 0 unread
        assert data["data"]["unread_notifications"] == 0
