"""Approved group output is a pinned, room-only Knowledge reference."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.models.base import generate_ulid
from packages.core.models.billing import CreditReservation
from packages.core.models.chat_room import (
    ChatRoom, ChatRoomAgentBinding, ChatRoomAgentRun, ChatRoomFile,
    ChatRoomMember, ChatRoomMessage, ChatRoomToolApproval,
)
from packages.core.models.document import Document
from packages.core.models.media_job import MediaJob
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription
from packages.core.services.room_agent_runtime import room_audience_fingerprint
from packages.core.services.room_generated_files import (
    RoomGeneratedFileError, ensure_group_generated_folder,
    project_room_generated_document,
)


async def _owner(client: AsyncClient, name: str) -> tuple[dict[str, str], dict]:
    response = await client.post("/api/v1/auth/register", json={
        "email": f"{name}@example.com", "password": "test-password-123",
        "entity_name": f"{name} Co",
    })
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, me.json()


async def _approved_result(client, db_session, tmp_path, monkeypatch, *, size: int):
    from packages.core.services import entity_fs

    monkeypatch.setattr(
        entity_fs, "get_settings",
        lambda: SimpleNamespace(MANOR_FS_ROOT=str(tmp_path), MANOR_FS_ENABLED=True, DEPLOYMENT_MODE="oss"),
    )
    headers, owner = await _owner(client, "generated_room_owner")
    created = await client.post("/api/v1/chat/rooms", headers=headers, json={
        "kind": "group", "title": "Output review", "owner_scope": "entity",
        "recipient_email": "future-guest@example.com",
    })
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]
    enabled = await client.patch(
        f"/api/v1/chat/rooms/{room_id}/ai-execution",
        headers=headers, json={"enabled": True, "expected_revision": 0},
    )
    assert enabled.status_code == 200, enabled.text
    room = (await db_session.execute(select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one()
    binding = (await db_session.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.room_id == room_id,
    ))).scalar_one()
    source = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=owner["id"],
        content="Create a file", mentioned_user_ids=[],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(source)
    await db_session.flush()
    fingerprint, _ = await room_audience_fingerprint(db_session, room_id)
    run = ChatRoomAgentRun(
        id=generate_ulid(), room_id=room_id, source_message_id=source.id,
        binding_id=binding.id, agent_name="Manor AI", status="succeeded",
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
    )
    db_session.add(run)
    folder = await ensure_group_generated_folder(
        db_session, room=room, requester_user_id=owner["id"],
    )
    document_id = generate_ulid()
    approval = ChatRoomToolApproval(
        id=generate_ulid(), room_id=room_id, run_id=run.id,
        requester_user_id=owner["id"], binding_id=binding.id,
        audience_fingerprint=fingerprint, ai_tool_execution_revision=1,
        tool_call_id="approved-file-call", tool_name="room_create_file",
        arguments_hash="a" * 64, display_summary="Create a group file",
        status="succeeded", decided_by_user_id=owner["id"],
        result_document_id=document_id,
    )
    db_session.add(approval)
    name = "report.bin"
    rel_path = f"{folder.rel_dir}/{approval.id}-{name}"
    content = b"R" * size
    abs_path = Path(tmp_path) / owner["entity_id"] / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)
    document = Document(
        id=document_id, entity_id=owner["entity_id"],
        owner_id=folder.owner_user_id, created_by=owner["id"],
        name=name, fs_path=rel_path, folder_id=folder.folder_id,
        file_size=size, mime_type="application/octet-stream",
        source="ai_generated", visibility="private", classification="internal",
        client_visible=False, quarantine_status="clean",
        metadata_={"origin": {
            "user_id": owner["id"], "room_id": room_id,
            "room_agent_run_id": run.id,
            "room_tool_call_id": approval.tool_call_id,
        }},
    )
    db_session.add(document)
    await db_session.commit()
    return headers, owner, room, run, approval, document, abs_path, content


@pytest.mark.asyncio
async def test_approved_large_group_file_is_pinned_downloadable_and_not_republished(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    headers, _, room, run, _, document, path, content = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=10 * 1024 * 1024 + 1,
    )
    projected = await project_room_generated_document(
        db_session, run_id=run.id, document_id=document.id,
    )
    await db_session.commit()
    assert projected.name == "report.bin"
    assert projected.size == len(content)
    files = await client.get(f"/api/v1/chat/rooms/{room.id}/files", headers=headers)
    assert files.status_code == 200, files.text
    assert [item["id"] for item in files.json()] == [projected.id]
    assert files.json()[0]["sender_name"] == "Manor AI"
    downloaded = await client.get(
        f"/api/v1/chat/rooms/{room.id}/files/{projected.id}", headers=headers,
    )
    assert downloaded.status_code == 200 and downloaded.content == content
    retried = await project_room_generated_document(
        db_session, run_id=run.id, document_id=document.id,
    )
    assert retried.id == projected.id
    await db_session.commit()
    assert (await db_session.execute(select(ChatRoomFile).where(
        ChatRoomFile.source_run_id == run.id,
    ))).scalars().all() == [projected]

    path.write_bytes(b"changed")
    changed = await client.get(
        f"/api/v1/chat/rooms/{room.id}/files/{projected.id}", headers=headers,
    )
    assert changed.status_code == 409
    assert changed.content != b"changed"

    path.write_bytes(content)
    projected.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()
    room_id, file_id = room.id, projected.id
    with pytest.raises(RoomGeneratedFileError, match="published_file_deleted"):
        await project_room_generated_document(
            db_session, run_id=run.id, document_id=document.id,
        )
    await db_session.rollback()
    assert (await client.get(
        f"/api/v1/chat/rooms/{room_id}/files/{file_id}", headers=headers,
    )).status_code == 404


@pytest.mark.asyncio
async def test_slow_group_downloads_do_not_exhaust_small_database_pool(
    client, db_session, tmp_path, monkeypatch,
):
    from fastapi import HTTPException
    from apps.api.routers.chat_rooms import download_room_file

    _, owner, room, run, _, document, _, content = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=128,
    )
    projected = await project_room_generated_document(
        db_session, run_id=run.id, document_id=document.id,
    )
    await db_session.commit()
    # Use a real bounded pool, not the test fixture's NullPool. Two slow
    # readers used to pin both connections and block every subsequent read.
    engine = create_async_engine(
        db_session.bind.url, pool_size=2, max_overflow=0, pool_timeout=1,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    responses = []
    tasks = []
    bodies = []
    started = asyncio.Queue()
    release = asyncio.Event()

    async def receive():
        await asyncio.Event().wait()

    async def slow_send(frame):
        if frame["type"] == "http.response.start":
            started.put_nowait(True)
            await release.wait()
        elif frame.get("body"):
            bodies.append(frame["body"])

    async def fast_read():
        async with sessions() as db:
            assert await db.scalar(select(User.id).where(User.id == owner["id"])) == owner["id"]

    try:
        for _ in range(8):
            async with sessions() as db:
                user = await db.get(User, owner["id"])
                response = await download_room_file(room.id, projected.id, user=user, db=db)
                responses.append(response)
                assert not db.in_transaction()
                assert engine.pool.checkedout() == 0
                tasks.append(asyncio.create_task(response(
                    {"type": "http", "method": "GET", "headers": [],
                     "asgi": {"spec_version": "2.4"}}, receive, slow_send,
                )))
                await asyncio.wait_for(started.get(), 2)
                # Keep this session alive, like a request-scoped dependency.
                # An independent request must still obtain a connection.
                await asyncio.wait_for(fast_read(), 2)
        async with sessions() as db:
            user = await db.get(User, owner["id"])
            with pytest.raises(HTTPException) as busy:
                await download_room_file(room.id, projected.id, user=user, db=db)
            assert busy.value.status_code == 503
        # Bound the burst to the pool size: every read must find a free
        # connection immediately. An unbounded burst queues 30 waiters on
        # pool_timeout and fails on a slow runner with no connection pinned.
        burst = asyncio.Semaphore(engine.pool.size())

        async def bounded_read():
            async with burst:
                await fast_read()

        await asyncio.wait_for(asyncio.gather(*(bounded_read() for _ in range(32))), 5)
        assert engine.pool.checkedout() == 0
        assert all(not response._handle.closed for response in responses)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(response._handle.closed for response in responses)
        assert bodies == [content] * 8
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for response in responses:
            response.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ("none", 200), ("membership", 404), ("token", 404),
    ("file", 404), ("classification", 404), ("name", 409),
])
async def test_group_copy_releases_connection_and_rechecks_authority(
    client, db_session, tmp_path, monkeypatch, change, expected,
):
    from fastapi import HTTPException
    from apps.api.routers import chat_rooms

    _, owner, room, run, _, document, _, _ = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=128,
    )
    projected = await project_room_generated_document(db_session, run_id=run.id, document_id=document.id)
    await db_session.commit()
    engine = create_async_engine(db_session.bind.url, pool_size=2, max_overflow=0, pool_timeout=1)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    original = chat_rooms.anyio.to_thread.run_sync
    handles = []

    async def during_copy(fn, *args, **kwargs):
        if fn.__name__ != "verified_private_copy":
            return await original(fn, *args, **kwargs)
        assert engine.pool.checkedout() == 0
        handle = await original(fn, *args, **kwargs)
        handles.append(handle)
        async with sessions() as other:
            if change == "membership":
                member = await other.scalar(select(ChatRoomMember).where(
                    ChatRoomMember.room_id == room.id, ChatRoomMember.user_id == owner["id"],
                ))
                member.status = "left"
            elif change == "token":
                actor = await other.get(User, owner["id"])
                actor.token_version += 1
            elif change in {"file", "name"}:
                file = await other.get(ChatRoomFile, projected.id)
                if change == "file":
                    file.deleted_at = datetime.now(timezone.utc)
                else:
                    file.name = "replacement.bin"
            elif change == "classification":
                current = await other.get(Document, document.id)
                current.classification = "restricted"
            await other.commit()
        return handle

    monkeypatch.setattr(chat_rooms.anyio.to_thread, "run_sync", during_copy)
    try:
        async with sessions() as db:
            user = await db.get(User, owner["id"])
            if expected == 200:
                response = await chat_rooms.download_room_file(room.id, projected.id, user=user, db=db)
                assert not db.in_transaction() and engine.pool.checkedout() == 0
                response.close()
            else:
                with pytest.raises(HTTPException) as error:
                    await chat_rooms.download_room_file(room.id, projected.id, user=user, db=db)
                assert error.value.status_code == expected
        assert len(handles) == 1 and handles[0].closed
    finally:
        for handle in handles:
            handle.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_removes_generated_file_from_group_without_deleting_knowledge_original(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    headers, _, room, run, _, document, path, _ = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=128,
    )
    projected = await project_room_generated_document(
        db_session, run_id=run.id, document_id=document.id,
    )
    await db_session.commit()
    url = f"/api/v1/chat/rooms/{room.id}/files/{projected.id}"
    assert (await client.delete(url, headers=headers)).status_code == 204
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/chat/rooms/{room.id}/files", headers=headers)).json() == []
    assert path.exists()
    saved_document = (await db_session.execute(select(Document).where(Document.id == document.id))).scalar_one()
    assert saved_document.is_trashed is False
    saved_file = (await db_session.execute(select(ChatRoomFile).where(ChatRoomFile.id == projected.id))).scalar_one()
    assert saved_file.deleted_at is not None
    with pytest.raises(RoomGeneratedFileError, match="published_file_deleted"):
        await project_room_generated_document(db_session, run_id=run.id, document_id=document.id)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_group_file_cannot_attach_to_a_member_message(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    _, _, _, run, _, document, _, _ = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=128,
    )
    with pytest.raises(RoomGeneratedFileError, match="invalid_result_message"):
        await project_room_generated_document(
            db_session, run_id=run.id, document_id=document.id,
            message_id=run.source_message_id,
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_group_artifact_projection_rejects_stale_audience_and_unapproved_result(
    client: AsyncClient, db_session, tmp_path, monkeypatch,
):
    _, _, _, run, approval, document, _, _ = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=1024,
    )
    approval.status = "rejected"
    await db_session.commit()
    with pytest.raises(RoomGeneratedFileError, match="result_not_approved"):
        await project_room_generated_document(
            db_session, run_id=run.id, document_id=document.id,
        )
    approval.status = "succeeded"
    await db_session.commit()
    run.audience_fingerprint = "f" * 64
    await db_session.commit()
    with pytest.raises(RoomGeneratedFileError, match="audience_changed"):
        await project_room_generated_document(
            db_session, run_id=run.id, document_id=document.id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("billing", ["byok", "trial_payer"])
async def test_async_group_media_requires_completed_exact_job_receipt(
    client: AsyncClient, db_session, tmp_path, monkeypatch, billing,
):
    # trial_payer catches: the settled-credits check only finding a
    # reservation held by the job's entity, so a provider-paid trial job
    # never projects.
    _, owner, room, run, approval, document, _, _ = await _approved_result(
        client, db_session, tmp_path, monkeypatch, size=1024,
    )
    binding = (await db_session.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.id == run.binding_id,
    ))).scalar_one()
    agent_id = (await db_session.execute(select(AgentSubscription.agent_id).where(
        AgentSubscription.id == binding.subscription_id,
    ))).scalar_one()
    job = MediaJob(
        id=generate_ulid(), entity_id=owner["entity_id"], user_id=owner["id"],
        agent_id=agent_id, room_agent_run_id=run.id,
        kind="video", status="pending", prompt="Group clip",
        params={"result_document_id": document.id}, byok=billing == "byok",
        credits=0 if billing == "byok" else 10,
    )
    db_session.add(job)
    if billing == "trial_payer":
        db_session.add(CreditReservation(
            entity_id=generate_ulid(), workspace_id=None, amount_credits=10, consumed_credits=10,
            source_kind="media_job", source_id=job.id, status="consumed",
            meta={"data_owner_entity_id": owner["entity_id"]},
        ))
    approval.tool_name = "room_generate_media"
    approval.result_document_id = None
    approval.result_media_job_id = job.id
    document.metadata_ = {"origin": {"user_id": owner["id"], "agent_id": agent_id}}
    await db_session.commit()
    run_id, document_id, job_id = run.id, document.id, job.id
    with pytest.raises(RoomGeneratedFileError, match="media_result_unavailable"):
        await project_room_generated_document(
            db_session, run_id=run_id, document_id=document_id, media_job_id=job_id,
        )
    await db_session.rollback()
    job = (await db_session.execute(select(MediaJob).where(MediaJob.id == job_id))).scalar_one()
    job.status = "completed"
    job.completed_at = datetime.now(timezone.utc)
    await db_session.commit()
    projected = await project_room_generated_document(
        db_session, run_id=run_id, document_id=document_id, media_job_id=job_id,
    )
    await db_session.commit()
    assert projected.source_media_job_id == job_id
    assert projected.document_id == document_id
