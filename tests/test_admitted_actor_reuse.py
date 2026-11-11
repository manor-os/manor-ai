"""A request resolves its actor once at authentication; protected reads reuse it.

Each gate still re-resolves the actor after its policy reads, so a change made
while the request runs is denied as before, and a write in the session drops the
remembered actor.
"""

from datetime import datetime, timezone

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from auth_helpers import register_user_and_get_token
from httpx import AsyncClient
from sqlalchemy import select, update

from packages.core.models.permission import Classification
from packages.core.models.user import Entity, User
from packages.core.services import permission_gate
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    ResolvedUserActor,
    remember_admitted_actor,
    resolve_current_user_actor,
)
from packages.core.services.permission_gate import DocumentDownloadNotAllowed, ResourcePermissionGate


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await register_user_and_get_token(client, {
        "username": username,
        "email": f"{username}@test.com",
        "password": "actor-reuse-pass-123",
        "entity_name": "Actor Corp",
    })
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _admitted_reader(client: AsyncClient, db_session, username: str):
    headers = await _register(client, username)
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Reads"})
    user = await db_session.scalar(select(User).where(User.email == f"{username}@test.com"))
    credential = AuthenticatedUserCredential.from_user(user)
    actor = await resolve_current_user_actor(db_session, credential, enforce_entity_suspension=False)
    remember_admitted_actor(db_session, credential, actor)
    return credential, created.json()["id"]


def _count_resolutions(monkeypatch, *, revoke_on_call: int | None = None) -> list[int]:
    calls: list[int] = []

    async def counting(db, credential, **kwargs):
        calls.append(1)
        if revoke_on_call is not None and len(calls) == revoke_on_call:
            return None
        return await resolve_current_user_actor(db, credential, **kwargs)

    monkeypatch.setattr(permission_gate, "resolve_current_user_actor", counting)
    monkeypatch.setattr(
        "packages.core.services.actor_authorization.resolve_current_user_actor", counting,
    )
    return calls


@pytest.mark.asyncio
async def test_gate_starts_from_the_admitted_actor_and_still_rechecks(client, db_session, monkeypatch):
    credential, workspace_id = await _admitted_reader(client, db_session, "actorreuse")
    calls = _count_resolutions(monkeypatch)

    authorized = await ResourcePermissionGate.authorize_workspace_read(
        db_session, credential=credential, workspace_id=workspace_id,
    )

    assert authorized is not None
    assert len(calls) == 1  # only the re-check after the policy reads


@pytest.mark.asyncio
async def test_a_change_during_the_request_is_still_denied(client, db_session, monkeypatch):
    credential, workspace_id = await _admitted_reader(client, db_session, "actorrevoked")
    _count_resolutions(monkeypatch, revoke_on_call=1)

    assert await ResourcePermissionGate.authorize_workspace_read(
        db_session, credential=credential, workspace_id=workspace_id,
    ) is None


@pytest.mark.asyncio
async def test_a_write_in_the_session_drops_the_admitted_actor(client, db_session, monkeypatch):
    credential, workspace_id = await _admitted_reader(client, db_session, "actorwrite")
    await db_session.execute(
        update(User).where(User.id == credential.user_id).values(display_name="Renamed"),
    )
    calls = _count_resolutions(monkeypatch)

    await ResourcePermissionGate.authorize_workspace_read(
        db_session, credential=credential, workspace_id=workspace_id,
    )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_staff_assignment_is_read_once_until_a_write(client, db_session, monkeypatch):
    from packages.core import permissions
    from packages.core.models.staff import Staff

    credential, _workspace_id = await _admitted_reader(client, db_session, "actorstaff")
    reads: list[int] = []
    read = permissions._read_staff_role_assignment

    async def counting(*args, **kwargs):
        reads.append(1)
        return await read(*args, **kwargs)

    monkeypatch.setattr(permissions, "_read_staff_role_assignment", counting)
    ask = lambda: permissions.user_staff_role_assignment(  # noqa: E731
        db_session, credential.user_id, credential.entity_id,
    )

    first = await ask()
    assert await ask() == first
    assert len(reads) == 1
    assert first[0] and first[3]

    await db_session.execute(
        update(Staff)
        .where(Staff.user_id == credential.user_id, Staff.entity_id == credential.entity_id)
        .values(status="inactive"),
    )

    assert await ask() == (True, None, None, [])
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_staff_assignment_outside_an_admitted_request_is_always_read(client, db_session, monkeypatch):
    from packages.core import permissions

    await _register(client, "actorstaffplain")
    user = await db_session.scalar(select(User).where(User.email == "actorstaffplain@test.com"))
    reads: list[int] = []
    read = permissions._read_staff_role_assignment

    async def counting(*args, **kwargs):
        reads.append(1)
        return await read(*args, **kwargs)

    monkeypatch.setattr(permissions, "_read_staff_role_assignment", counting)
    for _ in range(2):
        await permissions.user_staff_role_assignment(db_session, user.id, user.entity_id)

    assert len(reads) == 2


@pytest.mark.asyncio
async def test_authentication_reads_the_account_with_its_admission(client, db_session, monkeypatch):
    from apps.api import deps

    headers = await _register(client, "actoronce")
    separate_reads = AsyncMock(side_effect=AssertionError("account read twice"))
    monkeypatch.setattr(deps, "get_user_by_id", separate_reads)

    resp = await client.get("/api/v1/auth/me", headers=headers)

    assert resp.status_code == 200, resp.text
    separate_reads.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_deleted_account_is_still_not_found(client, db_session):
    headers = await _register(client, "actordeleted")
    await db_session.execute(
        update(User).where(User.email == "actordeleted@test.com")
        .values(deleted_at=datetime.now(timezone.utc)),
    )
    await db_session.commit()

    resp = await client.get("/api/v1/auth/me", headers=headers)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "User not found"


@pytest.mark.asyncio
async def test_suspended_entity_is_refused_from_the_actor_read(client, db_session, monkeypatch):
    headers = await _register(client, "actorsuspended")
    user = await db_session.scalar(select(User).where(User.email == "actorsuspended@test.com"))
    await db_session.execute(update(Entity).where(Entity.id == user.entity_id).values(settings={
        "platform_suspended_at": datetime.now(timezone.utc).isoformat(),
        "platform_suspended_reason": "Unpaid invoice.",
    }))
    await db_session.commit()
    monkeypatch.setattr("packages.core.constants.plans.is_cloud", lambda: True)

    resp = await client.get("/api/v1/auth/me", headers=headers)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Your organization has been suspended. Unpaid invoice."


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_revoked", [False, True])
async def test_download_denial_requires_a_current_actor(monkeypatch, actor_revoked):
    db = SimpleNamespace(info={})
    credential = AuthenticatedUserCredential("user", "entity", 1)
    actor = ResolvedUserActor(
        user_id="user",
        entity_id="entity",
        display_role="member",
        role="member",
        can_read_documents=True,
        can_read_workspaces=True,
        can_read_all_documents=False,
        can_manage_all_documents=False,
        can_read_all_workspaces=False,
    )
    remember_admitted_actor(db, credential, actor)
    monkeypatch.setattr(permission_gate, "user_can_read_document", AsyncMock(return_value=True))
    monkeypatch.setattr(
        permission_gate, "effective_document_folder_policy",
        AsyncMock(return_value=(Classification.RESTRICTED, None, None)),
    )
    monkeypatch.setattr(
        permission_gate, "effective_document_capabilities_for_user", AsyncMock(return_value=set()),
    )
    recheck = AsyncMock(return_value=None if actor_revoked else actor)
    monkeypatch.setattr(permission_gate, "resolve_current_user_actor", recheck)

    document = SimpleNamespace()
    if actor_revoked:
        assert await ResourcePermissionGate.authorize_document_read(
            db, credential=credential, document=document, download=True,
        ) is None
    else:
        with pytest.raises(DocumentDownloadNotAllowed):
            await ResourcePermissionGate.authorize_document_read(
                db, credential=credential, document=document, download=True,
            )
    recheck.assert_awaited_once_with(db, credential)
