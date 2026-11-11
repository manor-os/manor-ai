"""Chat history reads follow the existing visibility rules on every entry."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.manor_actions import (
    runtime_manor_list_conversations,
    runtime_manor_read_conversation_messages,
    runtime_manor_search_conversation_messages,
)
from packages.core.constants.conversation import ConversationSurfaceKind
from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomMessage
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services import chat_history_access as history
from packages.core.services.auth_service import create_access_token, hash_password
from packages.core.services.conversation_surfaces import ConversationSurfaceMetadataFactory

NEEDLE = "needle-42"


async def _member(db: AsyncSession, entity_id: str, name: str, *, status: str = "active") -> User:
    user = User(id=generate_ulid(), entity_id=entity_id, email=f"{name}@example.com",
                password_hash=hash_password("pass123"), role="member", status=status)
    db.add_all([user, UserMembership(user_id=user.id, entity_id=entity_id, role="member",
                                     status="active", is_primary=True)])
    await db.flush()
    return user


async def _world(client: AsyncClient, db: AsyncSession) -> SimpleNamespace:
    registered = await client.post("/api/v1/auth/register", json={
        "email": "history-owner@example.com", "password": "test-password-123", "entity_name": "History Co",
    })
    assert registered.status_code in (200, 201), registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = owner["entity_id"]
    me = await _member(db, entity_id, "history-me")
    other = await _member(db, entity_id, "history-other")
    now = datetime.now(timezone.utc)

    def conversation(**fields) -> Conversation:
        return Conversation(id=generate_ulid(), entity_id=entity_id, **fields)

    def said(conv: Conversation, text: str, role: str = "user", **fields) -> Message:
        return Message(id=generate_ulid(), conversation_id=conv.id, role=role, content=text,
                       created_at=fields.pop("created_at", now), **fields)

    mine = conversation(user_id=me.id, title="Mine", channel="web")
    theirs = conversation(user_id=other.id, title="Theirs", channel="web")
    channel = conversation(title="Telegram", channel="telegram", scope="channel")
    hidden = conversation(user_id=me.id, title="AI Edit", channel="web", meta={
        ConversationSurfaceMetadataFactory.META_KEY: ConversationSurfaceKind.AI_EDIT.value,
    })
    my_ws = Workspace(id=generate_ulid(), entity_id=entity_id, name="Mine",
                      settings={"access_mode": "members_only"})
    closed_ws = Workspace(id=generate_ulid(), entity_id=entity_id, name="Closed",
                          settings={"access_mode": "members_only"})
    my_ws_chat = conversation(workspace_id=my_ws.id, title="Mine WS", scope="workspace_main")
    closed_ws_chat = conversation(workspace_id=closed_ws.id, title="Closed WS", scope="workspace_main")

    joined = ChatRoom(id=generate_ulid(), kind="group", title="Joined", owner_entity_id=entity_id,
                      created_by_user_id=owner["id"], status="active")
    left = ChatRoom(id=generate_ulid(), kind="group", title="Left", owner_entity_id=entity_id,
                    created_by_user_id=owner["id"], status="active")
    foreign = ChatRoom(id=generate_ulid(), kind="group", title="Foreign", owner_entity_id=entity_id,
                       created_by_user_id=owner["id"], status="active")
    db.add_all([mine, theirs, channel, hidden, my_ws, closed_ws, my_ws_chat, closed_ws_chat, joined, left, foreign,
                WorkspaceStaff(workspace_id=my_ws.id, user_id=me.id, role="viewer", status="active")])
    await db.flush()
    visible_from = now - timedelta(hours=1)
    db.add_all([
        ChatRoomMember(id=generate_ulid(), room_id=joined.id, user_id=me.id, role="member",
                       status="active", visible_from=visible_from),
        ChatRoomMember(id=generate_ulid(), room_id=left.id, user_id=me.id, role="member",
                       status="left", visible_from=visible_from),
        ChatRoomMessage(id=generate_ulid(), room_id=joined.id, sender_user_id=other.id,
                        content=f"{NEEDLE} joined", created_at=now),
        ChatRoomMessage(id=generate_ulid(), room_id=joined.id, sender_user_id=other.id,
                        content=f"{NEEDLE} before I joined", created_at=now - timedelta(hours=2)),
        ChatRoomMessage(id=generate_ulid(), room_id=joined.id, sender_user_id=other.id,
                        content=f"{NEEDLE} retracted", created_at=now, deleted_at=now),
        ChatRoomMessage(id=generate_ulid(), room_id=left.id, sender_user_id=other.id,
                        content=f"{NEEDLE} left", created_at=now),
        ChatRoomMessage(id=generate_ulid(), room_id=foreign.id, sender_user_id=other.id,
                        content=f"{NEEDLE} foreign", created_at=now),
        said(mine, f"{NEEDLE} personal"),
        said(mine, f"{NEEDLE} ancient", created_at=now - timedelta(days=200)),
        said(mine, f"{NEEDLE} tool row", role="tool"),
        said(mine, f"{NEEDLE} receipt", meta={"approval_receipt": True}),
        said(mine, f"{NEEDLE} streaming", role="assistant", meta={"stream_status": "running"}),
        said(theirs, f"{NEEDLE} theirs"),
        said(channel, f"{NEEDLE} channel"),
        said(hidden, f"{NEEDLE} hidden"),
        said(my_ws_chat, f"{NEEDLE} my workspace"),
        said(closed_ws_chat, f"{NEEDLE} closed workspace"),
    ])
    await db.commit()
    return SimpleNamespace(
        entity_id=entity_id, me=me, other=other, owner_headers=headers, mine=mine, theirs=theirs,
        channel=channel, hidden=hidden, my_ws=my_ws, closed_ws=closed_ws, my_ws_chat=my_ws_chat,
        closed_ws_chat=closed_ws_chat, joined=joined, left=left, foreign=foreign, now=now, said=said,
    )


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.entity_id, 'member')}"}


@pytest.mark.asyncio
async def test_group_with_a_departed_owner_is_unreadable(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    departed = await _member(db_session, world.entity_id, "history-departed", status="inactive")
    room = ChatRoom(id=generate_ulid(), kind="group", title="Orphan", owner_user_id=departed.id,
                    created_by_user_id=departed.id, status="active")
    db_session.add(room)
    await db_session.flush()
    db_session.add(ChatRoomMember(id=generate_ulid(), room_id=room.id, user_id=world.me.id, role="member",
                                  status="active", visible_from=world.now))
    await db_session.commit()

    response = await client.get(f"/api/v1/chat/rooms/{room.id}", headers=_headers(world.me))
    assert response.status_code == 404
    rooms = await db_session.execute(select(ChatRoom.id).where(ChatRoom.id == room.id, history.live_room_clause()))
    assert rooms.first() is None


def _reader(world: SimpleNamespace, **fields) -> history.ChatHistoryReader:
    return history.ChatHistoryReader(user_id=world.me.id, entity_id=world.entity_id, role="member", **fields)


@pytest.mark.asyncio
async def test_list_returns_only_what_the_reader_may_see(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    result = await history.list_conversation_sources(db_session, _reader(world))

    assert {(item["kind"], item["id"]) for item in result["conversations"]} == {
        ("personal", world.mine.id),
        ("channel", world.channel.id),
        ("workspace", world.my_ws_chat.id),
        ("room", world.joined.id),
    }


@pytest.mark.asyncio
async def test_private_chat_room_is_titled_by_the_other_person(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    room = ChatRoom(id=generate_ulid(), kind="dm", owner_user_id=world.me.id,
                    created_by_user_id=world.me.id, status="active")
    db_session.add(room)
    await db_session.flush()
    db_session.add_all([
        ChatRoomMember(id=generate_ulid(), room_id=room.id, user_id=user.id, role="member",
                       status="active", visible_from=world.now)
        for user in (world.me, world.other)
    ])
    await db_session.commit()

    result = await history.list_conversation_sources(db_session, _reader(world), kind="room")

    item = next(item for item in result["conversations"] if item["id"] == room.id)
    assert item["room_kind"] == "dm"
    assert item["title"] == world.other.email


@pytest.mark.asyncio
async def test_workspace_audience_lists_only_that_workspace(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    reader = _reader(world, workspace_id=world.my_ws.id)

    result = await history.list_conversation_sources(db_session, reader)
    assert [item["id"] for item in result["conversations"]] == [world.my_ws_chat.id]

    with pytest.raises(history.ChatHistoryInvalid):
        await history.list_conversation_sources(db_session, reader, kind="personal")
    with pytest.raises(history.ChatHistoryInvalid):
        await history.list_conversation_sources(db_session, reader, workspace_id=world.closed_ws.id)


def _texts(result: dict) -> set[str]:
    return {hit["snippet"] for hit in result["hits"]}


@pytest.mark.asyncio
async def test_search_covers_exactly_the_visible_messages(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    result = await history.search_messages(db_session, _reader(world), query=NEEDLE)

    assert _texts(result) == {f"{NEEDLE} personal", f"{NEEDLE} joined", f"{NEEDLE} channel", f"{NEEDLE} my workspace"}
    assert result["searched_after"]


@pytest.mark.asyncio
async def test_search_defaults_to_recent_and_can_be_widened(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    older = (world.now - timedelta(days=365)).isoformat()

    result = await history.search_messages(db_session, _reader(world), query=NEEDLE, kind="personal", after=older)

    assert _texts(result) == {f"{NEEDLE} personal", f"{NEEDLE} ancient"}


@pytest.mark.asyncio
async def test_workspace_audience_never_finds_private_records(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    reader = _reader(world, workspace_id=world.my_ws.id)

    result = await history.search_messages(db_session, reader, query=NEEDLE)

    assert _texts(result) == {f"{NEEDLE} my workspace"}
    with pytest.raises(history.ChatHistoryInvalid):
        await history.search_messages(db_session, reader, query=NEEDLE, kind="room")


@pytest.mark.asyncio
async def test_search_filters_by_sender_and_conversation(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    by_other = await history.search_messages(db_session, _reader(world), query=NEEDLE, sender_user_id=world.other.id)
    assert _texts(by_other) == {f"{NEEDLE} joined"}

    one = await history.search_messages(
        db_session, _reader(world), query=NEEDLE, kind="workspace", conversation_id=world.my_ws_chat.id,
    )
    assert _texts(one) == {f"{NEEDLE} my workspace"}

    foreign = await history.search_messages(
        db_session, _reader(world), query=NEEDLE, kind="workspace", conversation_id=world.closed_ws_chat.id,
    )
    assert foreign["hits"] == []


@pytest.mark.asyncio
async def test_search_pages_cover_every_hit_once(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    db_session.add_all([
        world.said(world.mine, f"paged-7 {index}", created_at=world.now - timedelta(minutes=index))
        for index in range(25)
    ])
    await db_session.commit()

    seen: list[str] = []
    cursor = None
    while True:
        page = await history.search_messages(db_session, _reader(world), query="paged-7", cursor=cursor)
        seen += [hit["message_id"] for hit in page["hits"]]
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert len(seen) == 25
    assert len(set(seen)) == 25


@pytest.mark.asyncio
async def test_search_treats_wildcards_as_text_and_rejects_short_queries(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    assert (await history.search_messages(db_session, _reader(world), query="%%%"))["hits"] == []
    with pytest.raises(history.ChatHistoryInvalid):
        await history.search_messages(db_session, _reader(world), query="a")


@pytest.mark.asyncio
async def test_a_query_past_the_time_budget_asks_to_narrow(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(history, "_STATEMENT_TIMEOUT_MS", 1)

    with pytest.raises(history.ChatHistoryQueryTooBroad):
        await history._bounded_rows(db_session, select(func.pg_sleep(0.2)))

    assert (await db_session.execute(select(func.now()))).scalar_one() is not None


@pytest.mark.asyncio
async def test_read_returns_visible_messages_oldest_first(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    room = await history.read_messages(db_session, _reader(world), kind="room", conversation_id=world.joined.id)
    assert [item["body"] for item in room["messages"]] == [f"{NEEDLE} joined"]
    assert room["messages"][0]["sender_name"] == world.other.email

    personal = await history.read_messages(db_session, _reader(world), kind="personal", conversation_id=world.mine.id)
    assert [item["body"] for item in personal["messages"]] == [f"{NEEDLE} ancient", f"{NEEDLE} personal"]


@pytest.mark.asyncio
async def test_read_expands_around_a_search_hit(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    rows = [world.said(world.mine, f"line {index}", created_at=world.now + timedelta(seconds=index + 1))
            for index in range(9)]
    db_session.add_all(rows)
    await db_session.commit()

    window = await history.read_messages(
        db_session, _reader(world), kind="personal", conversation_id=world.mine.id,
        around_message_id=rows[4].id, limit=5,
    )

    assert [item["body"] for item in window["messages"]] == [f"line {index}" for index in range(2, 7)]
    assert window["before_cursor"] == rows[2].id
    assert window["after_cursor"] == rows[6].id


@pytest.mark.parametrize("target", ["theirs", "closed_ws_chat", "hidden", "left", "foreign"])
@pytest.mark.asyncio
async def test_read_refuses_what_the_reader_may_not_see(client: AsyncClient, db_session: AsyncSession, target):
    world = await _world(client, db_session)
    kinds = {"theirs": "personal", "closed_ws_chat": "workspace", "hidden": "personal", "left": "room", "foreign": "room"}

    with pytest.raises(history.ChatHistoryNotFound):
        await history.read_messages(db_session, _reader(world), kind=kinds[target],
                                    conversation_id=getattr(world, target).id)


@pytest.mark.asyncio
async def test_read_cannot_anchor_on_a_message_from_before_joining(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    before_join = await db_session.scalar(select(ChatRoomMessage.id).where(
        ChatRoomMessage.room_id == world.joined.id, ChatRoomMessage.content == f"{NEEDLE} before I joined",
    ))

    with pytest.raises(history.ChatHistoryNotFound):
        await history.read_messages(db_session, _reader(world), kind="room", conversation_id=world.joined.id,
                                    around_message_id=before_join)


@pytest.mark.asyncio
async def test_workspace_audience_cannot_read_other_records(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    reader = _reader(world, workspace_id=world.my_ws.id)

    await history.read_messages(db_session, reader, kind="workspace", conversation_id=world.my_ws_chat.id)
    with pytest.raises(history.ChatHistoryInvalid):
        await history.read_messages(db_session, reader, kind="personal", conversation_id=world.mine.id)
    with pytest.raises(history.ChatHistoryNotFound):
        await history.read_messages(db_session, reader, kind="workspace", conversation_id=world.closed_ws_chat.id)


@pytest.mark.asyncio
async def test_rest_routes_read_as_the_signed_in_user(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)
    headers = _headers(world.me)

    listed = await client.get("/api/v1/chat/history/conversations", headers=headers, params={"kind": "room"})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["conversations"]] == [world.joined.id]

    found = await client.get("/api/v1/chat/history/search", headers=headers, params={"query": NEEDLE})
    assert found.status_code == 200, found.text
    assert f"{NEEDLE} theirs" not in {hit["snippet"] for hit in found.json()["hits"]}

    read = await client.get(f"/api/v1/chat/history/personal/{world.mine.id}/messages", headers=headers)
    assert read.status_code == 200, read.text

    refused = await client.get(f"/api/v1/chat/history/personal/{world.theirs.id}/messages", headers=headers)
    assert refused.status_code == 404

    invalid = await client.get("/api/v1/chat/history/search", headers=headers, params={"query": "a"})
    assert invalid.status_code == 422


async def _search_action(db: AsyncSession, world: SimpleNamespace, **fields) -> dict:
    return json.loads(await runtime_manor_search_conversation_messages(
        db, entity_id=world.entity_id, user_id=fields.pop("user_id", world.me.id),
        params={"query": NEEDLE, **fields.pop("params", {})}, **fields,
    ))


@pytest.mark.asyncio
async def test_personal_turn_reaches_everything_the_person_may_see(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    result = await _search_action(db_session, world, conversation_id=world.mine.id)

    assert {hit["kind"] for hit in result["hits"]} == {"personal", "room", "workspace", "channel"}


@pytest.mark.asyncio
async def test_shared_workspace_turn_stays_in_that_workspace(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    # The stored conversation decides, even with no Workspace in the tool context.
    from_conversation = await _search_action(db_session, world, conversation_id=world.my_ws_chat.id)
    assert {hit["conversation_id"] for hit in from_conversation["hits"]} == {world.my_ws_chat.id}

    from_context = await _search_action(db_session, world, workspace_ids=(world.my_ws.id,))
    assert {hit["conversation_id"] for hit in from_context["hits"]} == {world.my_ws_chat.id}

    from_task = await _search_action(db_session, world, workspace_ids=(world.my_ws.id,), shared_run=True)
    assert {hit["conversation_id"] for hit in from_task["hits"]} == {world.my_ws_chat.id}

    refused = await _search_action(db_session, world, workspace_ids=(world.my_ws.id,), params={"kind": "personal"})
    assert "error" in refused


@pytest.mark.asyncio
async def test_turns_without_a_private_reader_are_refused(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    world = await _world(client, db_session)

    assert "error" in await _search_action(db_session, world, user_id=None)
    assert "error" in await _search_action(db_session, world, conversation_id=world.channel.id)
    assert "error" in await _search_action(db_session, world, conversation_id=world.my_ws_chat.id,
                                           workspace_ids=(world.closed_ws.id,))

    # A Task outside a Workspace has organization-readable logs, even when
    # its model and tools execute as its requester or the account owner.
    task_thread = Conversation(id=generate_ulid(), entity_id=world.entity_id,
                               scope="workspace_task", channel="web", thread_ref_kind="task",
                               thread_ref_id=generate_ulid())
    db_session.add(task_thread)
    await db_session.commit()
    assert "error" in await _search_action(db_session, world, conversation_id=task_thread.id)
    assert "error" in await _search_action(db_session, world, conversation_id=task_thread.id,
                                           workspace_ids=(world.my_ws.id,))
    assert "error" in await _search_action(db_session, world, shared_run=True)

    async def always_collaborator(user_id, entity_id, db=None):
        return True

    monkeypatch.setattr(
        "packages.core.services.runtime_authorization.collaborator.is_runtime_collaborator", always_collaborator,
    )
    assert "error" in await _search_action(db_session, world, conversation_id=world.mine.id)


@pytest.mark.asyncio
async def test_manor_list_and_read_share_the_rules(client: AsyncClient, db_session: AsyncSession):
    world = await _world(client, db_session)

    listed = json.loads(await runtime_manor_list_conversations(
        db_session, entity_id=world.entity_id, user_id=world.me.id, params={"kind": "room"},
    ))
    assert [item["id"] for item in listed["conversations"]] == [world.joined.id]

    read = json.loads(await runtime_manor_read_conversation_messages(
        db_session, entity_id=world.entity_id, user_id=world.me.id,
        params={"kind": "personal", "conversation_id": world.theirs.id},
    ))
    assert "error" in read

    # Models call this bare after a catalog search; the error must say what to pass.
    bare = json.loads(await runtime_manor_read_conversation_messages(
        db_session, entity_id=world.entity_id, user_id=world.me.id,
    ))
    assert "conversation_id" in bare["error"]


@pytest.mark.asyncio
async def test_an_agent_reaches_chat_records_through_the_manor_tool(client: AsyncClient, db_session: AsyncSession):
    from packages.core.ai.runtime import (
        ChatSurface, runtime_prepare_local_tool_surface_for_turn, runtime_request_for_surface_turn,
    )
    from packages.core.ai.tools.manor_tool import _manor_handler

    world = await _world(client, db_session)

    personal = json.loads(await _manor_handler(
        entity_id=world.entity_id, action="search_conversation_messages", query=NEEDLE,
        _user_id_from_context=world.me.id, conversation_id=world.mine.id,
    ))
    assert {hit["kind"] for hit in personal["hits"]} == {"personal", "room", "workspace", "channel"}

    shared = json.loads(await _manor_handler(
        entity_id=world.entity_id, action="search_conversation_messages", params={"query": NEEDLE},
        _user_id_from_context=world.me.id, conversation_id=world.my_ws_chat.id,
    ))
    assert {hit["conversation_id"] for hit in shared["hits"]} == {world.my_ws_chat.id}

    request = runtime_request_for_surface_turn(
        surface=ChatSurface.SCHEDULED_AGENT_RUN, entity_id=world.entity_id, user_id=world.me.id,
        workspace_id=world.my_ws.id, conversation_id=world.my_ws_chat.id, task_id="scoped-task",
    )
    envelope = runtime_prepare_local_tool_surface_for_turn(request, tool_schemas=[{
        "type": "function", "function": {"name": "manor", "parameters": {"type": "object", "properties": {}}},
    }]).envelope
    scoped_task = json.loads(await _manor_handler(
        entity_id=world.entity_id, action="search_conversation_messages", query=NEEDLE,
        _user_id_from_context=world.me.id, _runtime_envelope_from_context=envelope,
    ))
    assert {hit["conversation_id"] for hit in scoped_task["hits"]} == {world.my_ws_chat.id}

    standalone = Task(id=generate_ulid(), entity_id=world.entity_id, creator_id=world.me.id,
                      title="An organization Task without a Workspace")
    db_session.add(standalone)
    await db_session.commit()
    # An accessible Workspace supplied as a tool argument cannot become this
    # entity Task's audience and expose its contents in organization Task logs.
    refused = json.loads(await _manor_handler(
        entity_id=world.entity_id, action="search_conversation_messages", query=NEEDLE,
        _user_id_from_context=world.me.id, task_id=standalone.id, workspace_id=world.my_ws.id,
        conversation_id=world.my_ws_chat.id,
    ))
    assert "error" in refused


@pytest.mark.asyncio
async def test_reading_a_knowledge_file_shows_what_people_said_about_it(
    client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch,
):
    from contextlib import asynccontextmanager

    import packages.core.database as db_module
    from packages.core.ai.tools.file_tools import _read_file
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    world = await _world(client, db_session)
    name = "brochure-local-copy.pdf.md"
    # Stored under another name, as uploads are: people use the Document's name.
    document = Document(id=generate_ulid(), entity_id=world.entity_id, name=name, file_type="md",
                        source="upload", fs_path="Brand/4875792e05ce1760.md", owner_id=world.me.id)
    earlier = world.now - timedelta(minutes=5)
    db_session.add_all([
        document,
        world.said(world.my_ws_chat, f"{name} is a test file; take it out of the index.", created_at=earlier),
        world.said(world.my_ws_chat, f"Added {name} to the source index.", role="assistant"),
        world.said(world.closed_ws_chat, f"{name} is the real brochure."),
        world.said(world.mine, f"My own note on {name}."),
    ])
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    source = tmp_path / world.entity_id / document.fs_path
    source.parent.mkdir(parents=True)
    source.write_text("VC 1,200+ cups per day\n" * 3, encoding="utf-8")

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(db_module, "async_session", session)

    async def read(**fields) -> dict:
        return json.loads(await _read_file(world.entity_id, document_id=document.id,
                                           **{"_user_id_from_context": world.me.id, **fields}))

    shared = await read(conversation_id=world.my_ws_chat.id, workspace_id=world.my_ws.id)
    mentions = shared["chat_mentions"]["messages"]
    assert [(item["from_person"], item["conversation_id"]) for item in mentions] == [
        (True, world.my_ws_chat.id), (False, world.my_ws_chat.id),
    ]
    assert "test file" in mentions[0]["snippet"]
    assert "content" in shared

    personal = await read(conversation_id=world.mine.id)
    assert {item["conversation_id"] for item in personal["chat_mentions"]["messages"]} == {
        world.my_ws_chat.id, world.mine.id,
    }

    assert "chat_mentions" not in await read(conversation_id=world.mine.id, offset=1)
    assert "chat_mentions" not in await read(conversation_id=world.mine.id, _user_id_from_context=None)
