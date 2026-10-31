"""E2E tests: WebSocket real-time notifications endpoint."""

import asyncio
import json
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.api.main import create_app
from apps.api.routers import ws as ws_router
from apps.api.routers.ws import (
    ConnectionManager,
    _entity_broadcast_recipients,
    _resolve_active_websocket_identity,
    _workspace_broadcast_user_ids,
)
from packages.core import database as db_module
from packages.core.models.staff import Staff
from packages.core.models.chat_room import ChatRoomMember
from packages.core.models.base import generate_ulid
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.auth_service import create_access_token, decode_token

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_chat_room_ws_relay_sends_id_only_to_current_connected_scopes(monkeypatch):
    class Result:
        def all(self):
            # Already filtered by the room/member/account/Entity query.
            now = datetime.now(UTC)
            return [("owner", 3, "entity-a", now), ("guest", 5, "entity-b", now)]

    class FakeDb:
        async def execute(self, _query):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield FakeDb()

    deliveries: list[tuple] = []

    async def capture(entity_id, event, data, *, user_ids, token_versions):
        deliveries.append((entity_id, event, data, user_ids, token_versions))

    monkeypatch.setattr(ws_router.db_module, "async_session", fake_session)
    monkeypatch.setattr(ws_router.manager, "_connections", {
        "owner": [object()], "guest": [object()], "outsider": [object()],
    })
    monkeypatch.setattr(
        ws_router.manager, "connected_entity_ids_for_user",
        lambda user_id: {"owner": {"entity-a"}, "guest": {"entity-b"}, "outsider": {"entity-c"}}[user_id],
    )
    monkeypatch.setattr(ws_router.manager, "broadcast_to_entity", capture)
    await ws_router._relay_chat_room_target({
        "room_id": "room-1", "event": "chat_room_changed",
        "data": {"content": "must not be relayed"},
    })
    assert deliveries == [
        ("entity-a", "chat_room_changed", {"room_id": "room-1", "kind": "changed"}, {"owner"}, {"owner": 3}),
        ("entity-b", "chat_room_changed", {"room_id": "room-1", "kind": "changed"}, {"guest"}, {"guest": 5}),
    ]
    deliveries.clear()
    await ws_router._relay_chat_room_target({
        "room_id": "room-1", "event": "chat_room_changed",
        "data": {"kind": "retracted", "content": "must not be relayed"},
    })
    assert [data for _, _, data, _, _ in deliveries] == [
        {"room_id": "room-1", "kind": "retracted"},
        {"room_id": "room-1", "kind": "retracted"},
    ]


@pytest.mark.asyncio
async def test_chat_room_ws_message_respects_each_members_visibility_floor(monkeypatch):
    now = datetime.now(UTC)

    class Result:
        def all(self):
            return [
                ("early", 1, "entity-a", now - timedelta(minutes=2)),
                ("late", 2, "entity-b", now - timedelta(seconds=10)),
                ("rejoined", 3, "entity-c", now + timedelta(seconds=1)),
            ]

    class FakeDb:
        async def execute(self, _query):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield FakeDb()

    async def snapshot(_db, _room_id, _message_id):
        return ({
            "id": "message-1", "room_id": "room-1", "created_at": now.isoformat(),
            "sender_user_id": "early", "content": "hello", "attachments": [],
        }, ("previous-1", now - timedelta(minutes=1)), now - timedelta(minutes=1), {
            "id": "previous-1", "sender_name": "early", "content": "private before join",
        })

    deliveries = []

    async def capture(entity_id, event, data, *, user_ids, token_versions):
        deliveries.append((entity_id, event, data, user_ids))

    monkeypatch.setattr(ws_router.db_module, "async_session", fake_session)
    monkeypatch.setattr(ws_router, "_room_message_snapshot", snapshot)
    monkeypatch.setattr(ws_router.manager, "_connections", {
        user_id: [object()] for user_id in ("early", "late", "rejoined", "outsider")
    })
    monkeypatch.setattr(ws_router.manager, "connected_entity_ids_for_user", lambda user_id: {
        "early": {"entity-a"}, "late": {"entity-b"},
        "rejoined": {"entity-c"}, "outsider": {"entity-d"},
    }[user_id])
    monkeypatch.setattr(ws_router.manager, "broadcast_to_entity", capture)
    await ws_router._relay_chat_room_target({
        "room_id": "room-1", "event": "chat_room_changed",
        "data": {"kind": "message", "message_id": "message-1"},
    })
    assert {entity_id for entity_id, _, _, _ in deliveries} == {"entity-a", "entity-b"}
    early = next(data for entity_id, _, data, _ in deliveries if entity_id == "entity-a")
    late = next(data for entity_id, _, data, _ in deliveries if entity_id == "entity-b")
    assert early["previous_message_id"] == "previous-1"
    assert early["message"]["reply_to"]["content"] == "private before join"
    assert late["previous_message_id"] is None
    assert late["message"]["reply_to"] is None


@pytest.mark.asyncio
async def test_chat_room_ws_reads_one_snapshot_for_hundred_recipients(monkeypatch):
    now = datetime.now(UTC)
    users = [f"member-{index}" for index in range(100)]

    class Result:
        def all(self):
            return [(user_id, 0, "entity-a", now - timedelta(seconds=1)) for user_id in users]

    class FakeDb:
        async def execute(self, _query):
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield FakeDb()

    snapshots = []

    async def snapshot(_db, room_id, message_id):
        snapshots.append((room_id, message_id))
        return ({
            "id": message_id, "room_id": room_id, "created_at": now.isoformat(),
            "sender_user_id": users[0], "content": "one committed message", "attachments": [],
        }, None, None, None)

    deliveries = []

    async def capture(entity_id, event, data, *, user_ids, token_versions):
        deliveries.append((entity_id, event, data, user_ids))

    monkeypatch.setattr(ws_router.db_module, "async_session", fake_session)
    monkeypatch.setattr(ws_router, "_room_message_snapshot", snapshot)
    monkeypatch.setattr(ws_router.manager, "_connections", {user_id: [object()] for user_id in users})
    monkeypatch.setattr(ws_router.manager, "connected_entity_ids_for_user", lambda _user_id: {"entity-a"})
    monkeypatch.setattr(ws_router.manager, "broadcast_to_entity", capture)
    await ws_router._relay_chat_room_target({
        "room_id": "room-1", "event": "chat_room_changed",
        "data": {"kind": "message", "message_id": "message-1"},
    })
    assert snapshots == [("room-1", "message-1")]
    assert len(deliveries) == 1
    assert deliveries[0][3] == set(users)


@pytest.mark.asyncio
async def test_chat_room_ws_relay_is_cross_entity_and_excludes_removed_members(
    client, db_session, monkeypatch,
):
    from tests.test_chat_rooms import _register

    owner_headers, owner = await _register(client, "ws_room_owner")
    _, guest = await _register(client, "ws_room_guest")
    _, outsider = await _register(client, "ws_room_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "WS scope", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    guest_member = ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(UTC),
    )
    db_session.add(guest_member)
    await db_session.commit()

    entity_by_user = {
        user["id"]: user["entity_id"] for user in (owner, guest, outsider)
    }
    deliveries: list[tuple[str, set[str], dict]] = []

    monkeypatch.setattr(ws_router.manager, "_connections", {
        user_id: [object()] for user_id in entity_by_user
    })
    monkeypatch.setattr(
        ws_router.manager, "connected_entity_ids_for_user",
        lambda user_id: {entity_by_user[user_id]},
    )

    async def capture(entity_id, event, data, *, user_ids, token_versions):
        assert event == "chat_room_changed"
        assert data == {"room_id": room_id, "kind": "changed"}
        deliveries.append((entity_id, user_ids, token_versions))

    monkeypatch.setattr(ws_router.manager, "broadcast_to_entity", capture)
    payload = {"target": "chat_room", "room_id": room_id, "event": "chat_room_changed"}
    await ws_router._relay_chat_room_target(payload)
    assert {entity_id: users for entity_id, users, _ in deliveries} == {
        owner["entity_id"]: {owner["id"]},
        guest["entity_id"]: {guest["id"]},
    }

    guest_member.status = "left"
    await db_session.commit()
    deliveries.clear()
    await ws_router._relay_chat_room_target(payload)
    assert deliveries == [(owner["entity_id"], {owner["id"]}, {owner["id"]: 0})]


@pytest.mark.asyncio
async def test_chat_room_ws_pushes_committed_message_only_to_members_who_can_see_it(
    client, db_session, monkeypatch,
):
    from tests.test_chat_rooms import _register

    owner_headers, owner = await _register(client, "ws_message_owner")
    _, guest = await _register(client, "ws_message_guest")
    _, outsider = await _register(client, "ws_message_outsider")
    created = await client.post("/api/v1/chat/rooms", headers=owner_headers, json={
        "kind": "group", "title": "WS message", "owner_scope": "personal",
        "recipient_email": guest["email"],
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    guest_member = ChatRoomMember(
        id=generate_ulid(), room_id=room_id, user_id=guest["id"],
        role="member", status="active", visible_from=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(guest_member)
    await db_session.commit()
    posted = await client.post(f"/api/v1/chat/rooms/{room_id}/messages", headers=owner_headers, json={
        "content": "committed body", "mentioned_user_ids": [], "mentioned_agent_binding_ids": [],
    })
    assert posted.status_code == 201, posted.text
    message_id = posted.json()["id"]

    entities = {user["id"]: user["entity_id"] for user in (owner, guest, outsider)}
    monkeypatch.setattr(ws_router.manager, "_connections", {user_id: [object()] for user_id in entities})
    monkeypatch.setattr(ws_router.manager, "connected_entity_ids_for_user", lambda user_id: {entities[user_id]})
    deliveries = []

    async def capture(entity_id, event, data, *, user_ids, token_versions):
        deliveries.append((entity_id, event, data, user_ids))

    monkeypatch.setattr(ws_router.manager, "broadcast_to_entity", capture)
    signal = {"room_id": room_id, "event": "chat_room_changed", "data": {
        "kind": "message", "message_id": message_id, "content": "ignored Redis body",
    }}
    await ws_router._relay_chat_room_target(signal)
    assert {entity_id: users for entity_id, _, _, users in deliveries} == {
        owner["entity_id"]: {owner["id"]}, guest["entity_id"]: {guest["id"]},
    }
    assert all(event == "chat_room_changed" for _, event, _, _ in deliveries)
    assert all(data["kind"] == "message" and data["message"]["content"] == "committed body"
               and data["message"]["id"] == message_id for _, _, data, _ in deliveries)

    guest_member.visible_from = datetime.now(UTC) + timedelta(seconds=1)
    await db_session.commit()
    deliveries.clear()
    await ws_router._relay_chat_room_target(signal)
    assert [(entity_id, users) for entity_id, _, _, users in deliveries] == [
        (owner["entity_id"], {owner["id"]}),
    ]


def _make_app_and_client():
    """Create a fresh app + sync TestClient for WebSocket testing."""
    app = create_app()
    return TestClient(app)


@pytest.mark.asyncio
async def test_tracked_session_geo_enrichment_runs_after_commit_without_blocking(
    monkeypatch,
):
    from packages.core.services import user_session_service

    committed = asyncio.Event()
    geo_started = asyncio.Event()
    release_geo = asyncio.Event()

    class FakeDb:
        async def commit(self):
            committed.set()

    @asynccontextmanager
    async def fake_session():
        yield FakeDb()

    class FakeWebSocket:
        headers = {"user-agent": "test"}
        client = None

    async def fake_start(*_args, **_kwargs):
        return "session-1"

    async def blocking_geo(**_kwargs):
        assert committed.is_set()
        geo_started.set()
        await release_geo.wait()

    monkeypatch.setattr(ws_router.db_module, "async_session", fake_session)
    monkeypatch.setattr(user_session_service, "start_user_session_compat", fake_start)
    monkeypatch.setattr(ws_router, "_enrich_tracked_session_geo", blocking_geo)

    session_id = await ws_router._start_tracked_session(
        user_id="user-1",
        entity_id="entity-1",
        websocket=FakeWebSocket(),
        lease_id="lease-1",
    )

    assert session_id == "session-1"
    await geo_started.wait()
    release_geo.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_tracked_session_geo_lookup_does_not_hold_a_database_connection(
    monkeypatch,
):
    from packages.core.services import geo_ip, user_session_service

    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    database_opened = asyncio.Event()

    async def blocking_lookup(_ip_address):
        lookup_started.set()
        await release_lookup.wait()
        return {"country_code": "US", "country": "United States"}

    class FakeDb:
        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_session():
        database_opened.set()
        yield FakeDb()

    persist_geo = AsyncMock()
    monkeypatch.setattr(geo_ip, "lookup_geo", blocking_lookup)
    monkeypatch.setattr(ws_router.db_module, "async_session", fake_session)
    monkeypatch.setattr(
        user_session_service,
        "enrich_user_session_geo",
        persist_geo,
    )

    task = asyncio.create_task(ws_router._enrich_tracked_session_geo(
        session_id="session-1",
        entity_id="entity-1",
        user_id="user-1",
        ip_address="8.8.8.8",
    ))
    await lookup_started.wait()
    assert not database_opened.is_set()

    release_lookup.set()
    await task

    assert database_opened.is_set()
    persist_geo.assert_awaited_once()
    assert persist_geo.await_args.kwargs["expected_ip_address"] == "8.8.8.8"


@pytest.mark.asyncio
async def test_tracked_session_geo_lookup_has_bounded_concurrency(monkeypatch):
    from packages.core.services import geo_ip

    active = 0
    max_active = 0
    first_wave_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def blocking_lookup(_ip_address):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            first_wave_started.set()
        await release_lookup.wait()
        active -= 1
        return None

    monkeypatch.setattr(geo_ip, "lookup_geo", blocking_lookup)
    monkeypatch.setattr(
        ws_router,
        "_session_geo_lookup_semaphore",
        asyncio.Semaphore(2),
    )

    tasks = [
        asyncio.create_task(ws_router._enrich_tracked_session_geo(
            session_id=f"session-{index}",
            entity_id="entity-1",
            user_id=f"user-{index}",
            ip_address=f"8.8.8.{index}",
        ))
        for index in range(3)
    ]
    await asyncio.wait_for(first_wave_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert max_active == 2

    release_lookup.set()
    await asyncio.gather(*tasks)
    assert max_active == 2


def _stub_active_ws_identity(monkeypatch) -> None:
    class UnavailableSession:
        async def __aenter__(self):
            raise RuntimeError("Database access is intentionally stubbed")

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    async def resolve(payload: dict, _user_id: str):
        return str(payload["entity_id"]), int(payload.get("token_version", 0)), False

    async def recipients(*, entity_id: str, candidate_user_ids: set[str]):
        del entity_id
        return {user_id: 0 for user_id in candidate_user_ids}

    async def start_session(**_kwargs):
        return None

    monkeypatch.setattr(ws_router, "_resolve_active_websocket_identity", resolve)
    monkeypatch.setattr(ws_router, "_entity_broadcast_recipients", recipients)
    monkeypatch.setattr(ws_router, "_start_tracked_session", start_session)
    monkeypatch.setattr(ws_router.db_module, "async_session", UnavailableSession)


def test_ws_connect_with_valid_token(monkeypatch):
    """Valid JWT token should connect and receive 'connected' event."""
    _stub_active_ws_identity(monkeypatch)
    token = create_access_token(user_id="user-1", entity_id="ent-1", role="owner")
    with closing(_make_app_and_client()) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            data = ws.receive_json()
            assert data["event"] == "connected"
            assert data["data"]["user_id"] == "user-1"
            assert "unread_notifications" in data["data"]


def test_ws_reject_invalid_token():
    """Invalid JWT token should close with code 4001."""
    with closing(_make_app_and_client()) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?token=bad.token.here") as ws:
                ws.receive_json()

    assert exc_info.value.code == 4001


def test_ws_accepts_bearer_handshake_header_without_query_token(monkeypatch):
    """Native clients authenticate with the Authorization header and no ?token= query."""
    _stub_active_ws_identity(monkeypatch)
    token = create_access_token(user_id="user-header", entity_id="ent-1", role="owner")
    with closing(_make_app_and_client()) as client:
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}"}) as ws:
            data = ws.receive_json()
            assert data["event"] == "connected"
            assert data["data"]["user_id"] == "user-header"


@pytest.mark.parametrize("authorization", ["Bearer bad.token.here", "Basic dXNlcjpwYXNz"])
def test_ws_rejects_bad_or_non_bearer_handshake_credentials(authorization):
    with closing(_make_app_and_client()) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws", headers={"Authorization": authorization}) as ws:
                ws.receive_json()

    assert exc_info.value.code == 4001


def test_ws_query_token_takes_precedence_over_handshake_header(monkeypatch):
    """A bad query token is not rescued by a valid header on the same handshake."""
    _stub_active_ws_identity(monkeypatch)
    token = create_access_token(user_id="user-header", entity_id="ent-1", role="owner")
    with closing(_make_app_and_client()) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/ws?token=bad.token.here", headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                ws.receive_json()

    assert exc_info.value.code == 4001


def test_ws_closes_connection_when_current_identity_is_revoked(monkeypatch):
    """An active socket must close before acting after its identity is revoked."""
    _stub_active_ws_identity(monkeypatch)

    async def revoked_recipients(*, entity_id: str, candidate_user_ids: set[str]):
        del entity_id, candidate_user_ids
        return {}

    monkeypatch.setattr(ws_router, "_entity_broadcast_recipients", revoked_recipients)
    token = create_access_token(user_id="user-revoked", entity_id="ent-1", role="owner")

    with closing(_make_app_and_client()) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["event"] == "connected"
            ws.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 4003


@pytest.mark.asyncio
async def test_entity_recipient_lookup_propagates_transient_database_failure(monkeypatch):
    from packages.core.database_runtime import DatabaseBusyError

    @asynccontextmanager
    async def busy_session():
        raise DatabaseBusyError("Database admission queue is full")
        yield

    monkeypatch.setattr(ws_router.db_module, "async_session", busy_session)
    with pytest.raises(DatabaseBusyError):
        await _entity_broadcast_recipients(entity_id="entity", candidate_user_ids={"user"})


def test_ws_busy_identity_check_is_retryable_not_revoked(monkeypatch):
    from packages.core.database_runtime import DatabaseBusyError

    _stub_active_ws_identity(monkeypatch)

    async def busy_recipients(**_kwargs):
        raise DatabaseBusyError("Database admission queue is full")

    monkeypatch.setattr(ws_router, "_entity_broadcast_recipients", busy_recipients)
    token = create_access_token(user_id="user-busy", entity_id="ent-1", role="owner")
    with closing(_make_app_and_client()) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["event"] == "connected"
            ws.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
    assert exc_info.value.code == 1013


def test_ws_ping_pong(monkeypatch):
    """Client sending ping should receive pong."""
    _stub_active_ws_identity(monkeypatch)
    token = create_access_token(user_id="user-2", entity_id="ent-1", role="member")
    with closing(_make_app_and_client()) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            # Consume the initial connected event
            connected = ws.receive_json()
            assert connected["event"] == "connected"

            # Send ping
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["event"] == "pong"
            assert pong["data"] == {}


def test_ws_initial_unread_count(monkeypatch):
    """Connected event should include unread_notifications count (0 for fresh user)."""
    _stub_active_ws_identity(monkeypatch)
    token = create_access_token(user_id="user-3", entity_id="ent-3", role="owner")
    with closing(_make_app_and_client()) as client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            data = ws.receive_json()
            assert data["event"] == "connected"
            # Fresh user with no notifications should have 0 unread
            assert data["data"]["unread_notifications"] == 0


@pytest.mark.asyncio
async def test_entity_broadcast_only_reaches_matching_connections():
    """Entity broadcasts must never cross tenant boundaries."""

    manager = ConnectionManager()

    class FakeWebSocket:
        def __init__(self):
            self.messages: list[dict] = []

        async def accept(self):
            return None

        async def send_text(self, message: str):
            self.messages.append(json.loads(message))

    class DisconnectingWebSocket(FakeWebSocket):
        async def send_text(self, message: str):
            await super().send_text(message)
            manager.disconnect("user-1", self)

    first_entity_socket = DisconnectingWebSocket()
    second_entity_socket = FakeWebSocket()
    other_entity_socket = FakeWebSocket()

    await manager.connect("user-1", "entity-1", 0, first_entity_socket)
    await manager.connect("user-2", "entity-1", 0, second_entity_socket)
    await manager.connect("user-3", "entity-2", 0, other_entity_socket)

    await manager.broadcast_to_entity(
        "entity-1",
        "task_update",
        {"task_id": "task-secret", "title": "Private task"},
    )

    expected = [{
        "event": "task_update",
        "data": {"task_id": "task-secret", "title": "Private task"},
    }]
    assert first_entity_socket.messages == expected
    assert second_entity_socket.messages == expected
    assert other_entity_socket.messages == []


@pytest.mark.asyncio
async def test_connection_manager_scopes_sessions_and_tokens_by_entity():
    """One user's connections in different entities must remain independent."""
    manager = ConnectionManager()

    class FakeWebSocket:
        def __init__(self):
            self.messages: list[dict] = []

        async def accept(self):
            return None

        async def send_text(self, message: str):
            self.messages.append(json.loads(message))

    stale_first_entity = FakeWebSocket()
    current_first_entity = FakeWebSocket()
    second_entity = FakeWebSocket()

    assert await manager.connect("user-1", "entity-1", 0, stale_first_entity)
    assert await manager.connect("user-1", "entity-2", 0, second_entity)
    assert not await manager.connect("user-1", "entity-1", 1, current_first_entity)

    manager.set_session_id("entity-1", "user-1", "session-1")
    manager.set_session_id("entity-2", "user-1", "session-2")
    assert manager.get_session_id("entity-1", "user-1") == "session-1"
    assert manager.get_session_id("entity-2", "user-1") == "session-2"

    await manager.broadcast_to_entity(
        "entity-1",
        "task_update",
        {"task_id": "task-1"},
        user_ids={"user-1"},
        token_versions={"user-1": 1},
    )
    assert stale_first_entity.messages == []
    assert current_first_entity.messages == [{
        "event": "task_update",
        "data": {"task_id": "task-1"},
    }]
    assert second_entity.messages == []

    assert not manager.disconnect("user-1", stale_first_entity)
    assert manager.disconnect("user-1", current_first_entity)
    assert manager.pop_session_id("entity-1", "user-1") == "session-1"
    assert manager.get_session_id("entity-2", "user-1") == "session-2"
    assert manager.disconnect("user-1", second_entity)


@pytest.mark.asyncio
async def test_slow_socket_does_not_block_healthy_group_members():
    manager = ConnectionManager(send_concurrency=4, send_timeout=0.05)
    healthy_delivered = asyncio.Event()
    stalled = asyncio.Event()
    delivered: list[int] = []

    class Socket:
        closed = False

        def __init__(self, index):
            self.index = index

        async def accept(self):
            pass

        async def send_text(self, _message):
            if self.index == 0:
                stalled.set()
                await asyncio.Event().wait()
            delivered.append(self.index)
            if len(delivered) == 99:
                healthy_delivered.set()

        async def close(self, **_kwargs):
            self.closed = True
            await asyncio.Event().wait()  # close must also have a deadline

    sockets = [Socket(index) for index in range(100)]
    for index, socket in enumerate(sockets):
        await manager.connect(f"user-{index}", "entity", 0, socket)
    broadcast = asyncio.create_task(manager.broadcast_to_entity("entity", "chat_room_changed", {}))
    try:
        await asyncio.wait_for(stalled.wait(), 1)
        await asyncio.wait_for(healthy_delivered.wait(), 1)
        await asyncio.wait_for(broadcast, 1)
        assert len(delivered) == 99 and sockets[0].closed
        assert manager._connection_entities[sockets[0]] == "entity"
    finally:
        broadcast.cancel()
        await asyncio.gather(broadcast, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_count", [1, 10])
async def test_thousand_socket_fanout_bounds_workers_sends_and_cancellation(entity_count):
    manager = ConnectionManager(send_concurrency=8, send_timeout=1)
    active = peak = 0
    pause = None

    class Socket:
        def __init__(self):
            self.messages = []
            self.sending = False

        async def accept(self):
            pass

        async def send_text(self, message):
            nonlocal active, peak
            assert not self.sending
            self.sending = True
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if pause is not None:
                    await pause.wait()
                self.messages.append(json.loads(message))
            finally:
                active -= 1
                self.sending = False

    sockets = [Socket() for _ in range(1000)]
    entities = [f"entity-{index}" for index in range(entity_count)]
    for index, socket in enumerate(sockets):
        await manager.connect(f"user-{index}", entities[index % entity_count], 0, socket)

    async def broadcast_all(event, data):
        await asyncio.gather(*[
            manager.broadcast_to_entity(entity, event, {**data, "entity": entity})
            for entity in entities
        ])

    await asyncio.gather(*[
        broadcast_all("chat_room_changed", {"sequence": sequence})
        for sequence in range(3)
    ])
    assert peak <= 8 and active == 0
    assert all(len(socket.messages) == 3 for socket in sockets)
    assert all({message["data"]["sequence"] for message in socket.messages} == {0, 1, 2} for socket in sockets)
    assert all(
        all(message["data"]["entity"] == entities[index % entity_count] for message in socket.messages)
        for index, socket in enumerate(sockets)
    )

    pause = asyncio.Event()
    broadcast = asyncio.create_task(broadcast_all("paused", {}))
    try:
        async with asyncio.timeout(1):
            while active == 0:
                await asyncio.sleep(0)
        broadcast.cancel()
        with pytest.raises(asyncio.CancelledError):
            await broadcast
        assert active == 0
        pause = None
        await broadcast_all("recovered", {})
        assert all(socket.messages[-1]["event"] == "recovered" for socket in sockets)
    finally:
        broadcast.cancel()
        await asyncio.gather(broadcast, return_exceptions=True)


@pytest.mark.asyncio
async def test_entity_connection_index_tracks_tabs_scopes_and_disconnects():
    manager = ConnectionManager()

    class Socket:
        async def accept(self):
            pass

    first, second, other_scope, outsider = (Socket() for _ in range(4))
    await manager.connect("user", "entity-a", 1, first)
    await manager.connect("user", "entity-a", 1, second)
    await manager.connect("user", "entity-b", 1, other_scope)
    await manager.connect("outsider", "entity-c", 1, outsider)

    class Connections(dict):
        def items(self):
            raise AssertionError("entity lookup must not scan platform connections")

        def values(self):
            raise AssertionError("connection count must not scan platform connections")

    manager._connections = Connections(manager._connections)
    assert manager.count == 4
    audience = manager.connected_user_ids_for_entity("entity-a")
    assert audience == {"user"}
    audience.clear()  # a returned snapshot cannot mutate the live index
    assert set(manager._entity_connection_snapshot("entity-a")) == {
        ("user", first), ("user", second),
    }
    assert not manager.disconnect("user", first)
    assert manager.connected_user_ids_for_entity("entity-a") == {"user"}
    assert manager.disconnect("user", second)
    assert manager.connected_user_ids_for_entity("entity-a") == set()
    assert manager.connected_user_ids_for_entity("entity-b") == {"user"}
    assert manager.disconnect("user", other_scope)
    assert manager.disconnect("outsider", outsider)
    assert manager.count == 0 and manager._entity_users == {}
    manager.disconnect("outsider", outsider)  # repeated cleanup is harmless
    assert manager.count == 0


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_websocket_delivery_deadline_must_be_finite_positive(timeout):
    with pytest.raises(ValueError, match="positive limits"):
        ConnectionManager(send_timeout=timeout)


@pytest.mark.asyncio
async def test_scoped_fanout_does_not_scan_unrelated_platform_connections():
    manager = ConnectionManager()

    class Connections(dict):
        def items(self):
            raise AssertionError("a scoped audience must not scan every connection")

    socket = object()
    manager._connections = Connections({"recipient": [socket]})
    manager._connection_entities[socket] = "entity"
    manager._connection_token_versions[socket] = 2
    assert manager._entity_connection_snapshot(
        "entity", user_ids={"recipient"}, token_versions={"recipient": 2},
    ) == [("recipient", socket)]


@pytest.mark.asyncio
async def test_connection_manager_serializes_first_connection_after_accept():
    """Concurrent accepts for one pair must create only one tracked session."""
    manager = ConnectionManager()
    release_accept = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    class BlockingWebSocket:
        def __init__(self, started: asyncio.Event):
            self.started = started

        async def accept(self):
            self.started.set()
            await release_accept.wait()

    first_socket = BlockingWebSocket(first_started)
    second_socket = BlockingWebSocket(second_started)
    first_connect = asyncio.create_task(
        manager.connect("user-1", "entity-1", 0, first_socket),
    )
    await first_started.wait()
    second_connect = asyncio.create_task(
        manager.connect("user-1", "entity-1", 0, second_socket),
    )
    await second_started.wait()

    release_accept.set()
    results = await asyncio.gather(first_connect, second_connect)

    assert sorted(results) == [False, True]
    assert manager.count == 2


@pytest.mark.asyncio
async def test_connection_manager_starts_one_session_for_concurrent_waiters():
    manager = ConnectionManager()
    release_start = asyncio.Event()
    start_calls = 0

    class FakeWebSocket:
        async def accept(self):
            return None

    first_socket = FakeWebSocket()
    second_socket = FakeWebSocket()
    await manager.connect("user-1", "entity-1", 0, first_socket)
    await manager.connect("user-1", "entity-1", 0, second_socket)

    observed_lease_ids = []

    async def starter(lease_id):
        nonlocal start_calls
        start_calls += 1
        observed_lease_ids.append(lease_id)
        await release_start.wait()
        return "session-1"

    async def stopper(_session_id, _lease_id):
        raise AssertionError("connected session must not be stopped")

    first = asyncio.create_task(
        manager.ensure_session_started("entity-1", "user-1", starter, stopper),
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        manager.ensure_session_started("entity-1", "user-1", starter, stopper),
    )
    await asyncio.sleep(0)

    assert start_calls == 1
    release_start.set()
    assert await asyncio.gather(first, second) == ["session-1", "session-1"]
    assert start_calls == 1
    assert manager.get_session_id("entity-1", "user-1") == "session-1"
    assert manager.get_session_lease_id("entity-1", "user-1") == observed_lease_ids[0]


@pytest.mark.asyncio
async def test_connection_manager_discards_only_the_expected_stale_session():
    manager = ConnectionManager()

    class FakeWebSocket:
        async def accept(self):
            return None

    websocket = FakeWebSocket()
    await manager.connect("user-1", "entity-1", 0, websocket)

    async def starter(_lease_id):
        return "session-1"

    async def stopper(_session_id, _lease_id):
        raise AssertionError("connected session must not be stopped")

    assert await manager.ensure_session_started(
        "entity-1",
        "user-1",
        starter,
        stopper,
    ) == "session-1"
    lease_id = manager.get_session_lease_id("entity-1", "user-1")
    assert lease_id

    assert not manager.discard_tracked_session(
        "entity-1",
        "user-1",
        session_id="different-session",
        lease_id=lease_id,
    )
    assert manager.get_session_id("entity-1", "user-1") == "session-1"
    assert manager.discard_tracked_session(
        "entity-1",
        "user-1",
        session_id="session-1",
        lease_id=lease_id,
    )
    assert manager.get_session_id("entity-1", "user-1") is None
    assert manager.get_session_lease_id("entity-1", "user-1") is None


@pytest.mark.asyncio
async def test_connection_manager_closes_session_started_after_disconnect():
    manager = ConnectionManager()
    release_start = asyncio.Event()
    stopped_sessions = []
    start_calls = 0

    class FakeWebSocket:
        async def accept(self):
            return None

    websocket = FakeWebSocket()
    await manager.connect("user-1", "entity-1", 0, websocket)

    async def starter(lease_id):
        nonlocal start_calls
        start_calls += 1
        await release_start.wait()
        return "orphan-session"

    async def stopper(session_id, lease_id):
        stopped_sessions.append((session_id, lease_id))

    first = asyncio.create_task(
        manager.ensure_session_started(
            "entity-1", "user-1", starter, stopper,
        ),
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        manager.ensure_session_started(
            "entity-1", "user-1", starter, stopper,
        ),
    )
    await asyncio.sleep(0)
    assert manager.disconnect("user-1", websocket)
    release_start.set()

    assert await asyncio.gather(first, second) == [None, None]
    assert start_calls == 1
    assert len(stopped_sessions) == 1
    assert stopped_sessions[0][0] == "orphan-session"
    assert stopped_sessions[0][1]
    assert manager.get_session_id("entity-1", "user-1") is None
    assert manager.get_session_lease_id("entity-1", "user-1") is None


@pytest.mark.asyncio
async def test_connection_manager_serializes_close_and_reconnect():
    manager = ConnectionManager()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    start_calls = 0

    class FakeWebSocket:
        async def accept(self):
            return None

    first_socket = FakeWebSocket()
    assert await manager.connect("user-1", "entity-1", 0, first_socket)

    async def starter(_lease_id):
        nonlocal start_calls
        start_calls += 1
        return f"session-{start_calls}"

    async def stopper(_session_id, _lease_id):
        raise AssertionError("connected session must not be stopped")

    assert await manager.ensure_session_started(
        "entity-1", "user-1", starter, stopper,
    ) == "session-1"
    assert manager.disconnect("user-1", first_socket)

    async def closer(_session_id, _lease_id):
        close_started.set()
        await release_close.wait()

    closing = asyncio.create_task(
        manager.close_session("entity-1", "user-1", closer),
    )
    await close_started.wait()

    second_socket = FakeWebSocket()
    assert await manager.connect("user-1", "entity-1", 0, second_socket)
    reconnecting = asyncio.create_task(
        manager.ensure_session_started(
            "entity-1", "user-1", starter, stopper,
        ),
    )
    await asyncio.sleep(0)
    assert not reconnecting.done()

    release_close.set()
    await closing
    assert await reconnecting == "session-2"
    assert start_calls == 2


@pytest.mark.asyncio
async def test_connection_manager_keeps_one_lock_for_queued_reconnect_waiters():
    manager = ConnectionManager()

    class FakeWebSocket:
        async def accept(self):
            return None

    first_socket = FakeWebSocket()
    await manager.connect("user-1", "entity-1", 0, first_socket)

    async def initial_starter(_lease_id):
        return "initial-session"

    async def stopper(_session_id, _lease_id):
        return None

    assert await manager.ensure_session_started(
        "entity-1", "user-1", initial_starter, stopper,
    ) == "initial-session"
    assert manager.disconnect("user-1", first_socket)

    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    release_starters = asyncio.Event()
    starter_calls = 0
    concurrent_starters = 0
    max_concurrent_starters = 0
    reconnect_task = None

    async def racing_starter(_lease_id):
        nonlocal starter_calls, concurrent_starters, max_concurrent_starters
        starter_calls += 1
        concurrent_starters += 1
        max_concurrent_starters = max(
            max_concurrent_starters,
            concurrent_starters,
        )
        await release_starters.wait()
        concurrent_starters -= 1
        return f"replacement-{starter_calls}"

    async def reconnect():
        second_socket = FakeWebSocket()
        await manager.connect("user-1", "entity-1", 0, second_socket)
        return await manager.ensure_session_started(
            "entity-1", "user-1", racing_starter, stopper,
        )

    async def closer(_session_id, _lease_id):
        nonlocal reconnect_task
        close_entered.set()
        await release_close.wait()
        reconnect_task = asyncio.create_task(reconnect())

    closing = asyncio.create_task(
        manager.close_session("entity-1", "user-1", closer),
    )
    await close_entered.wait()
    queued_waiter = asyncio.create_task(
        manager.ensure_session_started(
            "entity-1", "user-1", racing_starter, stopper,
        ),
    )
    await asyncio.sleep(0)
    release_close.set()
    await closing

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert starter_calls == 1
        assert max_concurrent_starters == 1
    finally:
        release_starters.set()
        await queued_waiter
        assert reconnect_task is not None
        await reconnect_task


@pytest.mark.asyncio
async def test_redis_relay_reconnects_after_pubsub_stream_ends(monkeypatch):
    """A transient Redis disconnect must recreate the subscription task.

    Pub/sub has no replay, so sockets that stayed connected through the outage
    are told to resync once the relay is subscribed again.
    """
    import redis.asyncio as aioredis

    second_connected = asyncio.Event()
    release_second = asyncio.Event()
    clients: list[object] = []

    class RecordingWebSocket:
        def __init__(self):
            self.frames: list[dict] = []

        async def accept(self):
            return None

        async def send_text(self, message: str):
            self.frames.append(json.loads(message))

    relay_manager = ConnectionManager()
    monkeypatch.setattr(ws_router, "manager", relay_manager)
    socket = RecordingWebSocket()
    await relay_manager.connect("user-1", "entity-1", 0, socket)

    class FakePubSub:
        def __init__(self, *, blocks: bool):
            self.blocks = blocks
            self.subscribed = False

        async def subscribe(self, _channel: str):
            self.subscribed = True
            if self.blocks:
                second_connected.set()

        async def listen(self):
            if self.blocks:
                await release_second.wait()
            else:
                yield {"type": "subscribe"}

        async def aclose(self):
            return None

    class FakeRedis:
        def __init__(self, pubsub: FakePubSub):
            self._pubsub = pubsub

        async def ping(self):
            return True

        def pubsub(self):
            return self._pubsub

        async def aclose(self):
            return None

    def fake_from_url(*_args, **_kwargs):
        pubsub = FakePubSub(blocks=bool(clients))
        clients.append(FakeRedis(pubsub))
        return clients[-1]

    monkeypatch.setattr(aioredis, "from_url", fake_from_url)
    relay = asyncio.create_task(ws_router._redis_relay_loop())
    try:
        await asyncio.wait_for(second_connected.wait(), timeout=3)
        assert len(clients) == 2
        assert not relay.done()
        for _ in range(100):
            if socket.frames:
                break
            await asyncio.sleep(0)
        assert socket.frames == [{"event": "resync", "data": {}}]
    finally:
        release_second.set()
        relay.cancel()
        await relay
        assert relay.done()


@pytest.mark.asyncio
async def test_redis_relay_backoff_grows_across_immediate_disconnects(monkeypatch):
    """Immediate stream disconnects must not reset reconnect backoff."""
    import redis.asyncio as aioredis

    release_third = asyncio.Event()
    clients: list[object] = []
    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    class FakePubSub:
        async def subscribe(self, _channel: str):
            return None

        async def listen(self):
            if len(clients) < 3:
                return
            await release_third.wait()
            if False:
                yield {"type": "subscribe"}

        async def aclose(self):
            return None

    class FakeRedis:
        async def ping(self):
            return True

        def pubsub(self):
            return FakePubSub()

        async def aclose(self):
            return None

    def fake_from_url(*_args, **_kwargs):
        client = FakeRedis()
        clients.append(client)
        return client

    async def fake_sleep(delay: float):
        sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(aioredis, "from_url", fake_from_url)
    monkeypatch.setattr(ws_router.asyncio, "sleep", fake_sleep)
    relay = asyncio.create_task(ws_router._redis_relay_loop())
    try:
        deadline = original_sleep
        for _ in range(3000):
            if len(clients) >= 3:
                break
            await deadline(0)
        else:
            raise AssertionError("relay did not create the third Redis client")
        assert sleeps[:2] == [1.0, 2.0]
    finally:
        release_third.set()
        relay.cancel()
        with suppress(asyncio.CancelledError):
            await relay


@pytest.mark.asyncio
async def test_send_failure_preserves_entity_metadata_for_endpoint_cleanup():
    """A failed push must not orphan another entity's Session lifecycle."""
    manager = ConnectionManager()

    class FailingWebSocket:
        def __init__(self):
            self.closed = False

        async def accept(self):
            return None

        async def send_text(self, _message: str):
            raise RuntimeError("socket closed")

        async def close(self, **_kwargs):
            self.closed = True

    class HealthyWebSocket:
        async def accept(self):
            return None

    failed_socket = FailingWebSocket()
    other_entity_socket = HealthyWebSocket()
    await manager.connect("user-1", "entity-1", 0, failed_socket)
    await manager.connect("user-1", "entity-2", 0, other_entity_socket)
    manager.set_session_id("entity-1", "user-1", "session-1")
    manager.set_session_id("entity-2", "user-1", "session-2")

    await manager.broadcast_to_entity(
        "entity-1",
        "task_update",
        {"task_id": "task-1"},
    )

    assert failed_socket.closed
    assert manager.disconnect("user-1", failed_socket)
    assert manager.pop_session_id("entity-1", "user-1") == "session-1"
    assert manager.get_session_id("entity-2", "user-1") == "session-2"


@pytest.mark.asyncio
async def test_user_relay_targets_only_the_payload_entity(monkeypatch):
    from apps.api.routers import ws as ws_router

    deliveries: list[tuple] = []

    async def recipients(*, entity_id, candidate_user_ids):
        assert entity_id == "entity-1"
        assert candidate_user_ids == {"user-1"}
        return {"user-1": 7}

    async def send_to_user(
        user_id,
        event,
        data,
        *,
        entity_id=None,
        token_versions=None,
    ):
        deliveries.append((user_id, entity_id, event, data, token_versions))

    monkeypatch.setattr(ws_router, "_entity_broadcast_recipients", recipients)
    monkeypatch.setattr(ws_router.manager, "send_to_user", send_to_user)

    await ws_router._relay_user_target({
        "target": "user",
        "user_id": "user-1",
        "entity_id": "entity-1",
        "event": "chat_stream_snapshot",
        "data": {"content": "private reply"},
    })

    assert deliveries == [(
        "user-1",
        "entity-1",
        "chat_stream_snapshot",
        {"content": "private reply"},
        {"user-1": 7},
    )]


@pytest.mark.asyncio
async def test_user_relay_drops_legacy_unscoped_payload(monkeypatch):
    from apps.api.routers import ws as ws_router

    send = AsyncMock()
    monkeypatch.setattr(ws_router.manager, "send_to_user", send)

    await ws_router._relay_user_target({
        "target": "user",
        "user_id": "user-1",
        "event": "notification",
        "data": {"id": "notification-1"},
    })

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_identity_resolution_rejects_logged_out_token(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "ws_identity_logout",
            "email": "ws_identity_logout@test.com",
            "password": "pass123",
            "entity_name": "WS Identity Corp",
        },
    )
    assert response.status_code == 200, response.text
    registration = response.json()
    token = registration["access_token"]
    payload = decode_token(token)

    assert await _resolve_active_websocket_identity(
        payload,
        registration["user_id"],
    ) == (registration["entity_id"], 0, False)

    logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout.status_code == 204, logout.text
    assert await _resolve_active_websocket_identity(
        payload,
        registration["user_id"],
    ) is None


@pytest.mark.asyncio
async def test_entity_broadcast_uses_current_membership_and_token_version(client):
    from tests.test_document_permissions import _create_entity_user

    entity_id = "entity-ws-current-state"
    member = await _create_entity_user(entity_id, "ws_recipient_member")
    async with db_module.async_session() as db:
        db.add(UserMembership(
            user_id=member["id"],
            entity_id=entity_id,
            role="member",
            status="active",
        ))
        await db.commit()

    assert await _entity_broadcast_recipients(
        entity_id=entity_id,
        candidate_user_ids={member["id"]},
    ) == {member["id"]: 0}

    async with db_module.async_session() as db:
        user = await db.get(User, member["id"])
        assert user is not None
        user.token_version = 2
        await db.commit()

    assert await _entity_broadcast_recipients(
        entity_id=entity_id,
        candidate_user_ids={member["id"]},
    ) == {member["id"]: 2}

    async with db_module.async_session() as db:
        membership = (await db.execute(
            select(UserMembership).where(
                UserMembership.user_id == member["id"],
                UserMembership.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        assert membership is not None
        membership.status = "inactive"
        await db.commit()

    assert await _entity_broadcast_recipients(
        entity_id=entity_id,
        candidate_user_ids={member["id"]},
    ) == {}


@pytest.mark.asyncio
async def test_workspace_broadcast_excludes_same_entity_non_member(client):
    from tests.test_document_permissions import _auth, _create_entity_user

    owner_headers = await _auth(client, "ws_broadcast_owner")
    owner = (await client.get(
        "/api/v1/auth/me",
        headers=owner_headers,
    )).json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private realtime workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    outsider = await _create_entity_user(
        owner["entity_id"],
        "ws_broadcast_outsider",
        role="member",
    )
    outsider_me = await client.get(
        "/api/v1/auth/me",
        headers=outsider["headers"],
    )
    assert outsider_me.status_code == 200, outsider_me.text

    allowed = await _workspace_broadcast_user_ids(
        entity_id=owner["entity_id"],
        workspace_id=workspace_id,
        candidate_user_ids={owner["id"], outsider["id"]},
    )

    assert allowed == {owner["id"]}


@pytest.mark.asyncio
async def test_workspace_broadcast_does_not_fallback_after_staff_revocation(client):
    """Inactive/deleted Staff history must override permissive Membership roles."""
    from tests.test_document_permissions import _auth, _create_entity_user

    owner_headers = await _auth(client, "ws_broadcast_staff_owner")
    owner = (await client.get(
        "/api/v1/auth/me",
        headers=owner_headers,
    )).json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Entity-visible realtime workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    clean_member = await _create_entity_user(
        owner["entity_id"],
        "ws_broadcast_clean_member",
    )
    inactive_staff_user = await _create_entity_user(
        owner["entity_id"],
        "ws_broadcast_inactive_staff",
    )
    deleted_staff_user = await _create_entity_user(
        owner["entity_id"],
        "ws_broadcast_deleted_staff",
    )
    users = [clean_member, inactive_staff_user, deleted_staff_user]

    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {"access_mode": "entity_visible"}
        db.add_all([
            UserMembership(
                user_id=user["id"],
                entity_id=owner["entity_id"],
                role="member",
                status="active",
            )
            for user in users
        ])
        db.add(Staff(
            entity_id=owner["entity_id"],
            kind="employee",
            name="Inactive Staff",
            user_id=inactive_staff_user["id"],
            meta={"role": "member"},
            status="inactive",
        ))
        db.add(Staff(
            entity_id=owner["entity_id"],
            kind="employee",
            name="Deleted Staff",
            user_id=deleted_staff_user["id"],
            meta={"role": "member"},
            status="active",
            deleted_at=datetime.now(UTC),
        ))
        await db.commit()

    allowed = await _workspace_broadcast_user_ids(
        entity_id=owner["entity_id"],
        workspace_id=workspace_id,
        candidate_user_ids={owner["id"], *(user["id"] for user in users)},
    )

    assert allowed == {owner["id"], clean_member["id"]}


@pytest.mark.asyncio
async def test_workspace_broadcast_acl_query_count_is_constant(client):
    from apps.api.routers import ws as ws_router
    from tests.test_document_permissions import _auth, _create_entity_user

    owner_headers = await _auth(client, "ws_broadcast_query_owner")
    owner = (await client.get(
        "/api/v1/auth/me",
        headers=owner_headers,
    )).json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private realtime query workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    outsiders = [
        await _create_entity_user(
            owner["entity_id"],
            f"ws_broadcast_query_outsider_{index}",
            role="member",
        )
        for index in range(8)
    ]
    async with db_module.async_session() as db:
        db.add_all([
            UserMembership(
                user_id=outsider["id"],
                entity_id=owner["entity_id"],
                role="member",
                status="active",
            )
            for outsider in outsiders
        ])
        await db.commit()

    statements: list[str] = []

    def count_statement(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    sync_engine = ws_router.db_module.async_session.kw["bind"].sync_engine
    event.listen(sync_engine, "before_cursor_execute", count_statement)
    try:
        allowed = await _workspace_broadcast_user_ids(
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            candidate_user_ids={owner["id"], *(user["id"] for user in outsiders)},
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_statement)

    assert allowed == {owner["id"]}
    assert len(statements) == 4
