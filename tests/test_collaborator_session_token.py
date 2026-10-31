"""A collaborator works in a customer Workspace only through a scoped session pinned to it."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select

from test_workspace_collaboration import accepted, minted
from apps.api.routers.ws import _resolve_active_websocket_identity
from packages.core.config import get_settings
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.auth_service import (
    create_access_token,
    create_collaborator_session_token,
    decode_token,
)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def claims(headers):
    return decode_token(headers["Authorization"].removeprefix("Bearer "))


async def token_version(db, user_id):
    return await db.scalar(
        select(User.token_version).where(User.id == user_id).execution_options(populate_existing=True)
    )


@pytest.mark.asyncio
async def test_a_scoped_session_reaches_only_its_pinned_workspace(client, db_session):
    # Catches: a scoped token reaching another live-granted Workspace of the
    # same customer (HTTP or socket); a scoped token renewing itself,
    # switching company or signing the person out (which would end the
    # provider's own session); the provider's home sign-in being affected.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    second = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Second", "kind": "project", "primary_work": "More"}
    )
    other = second.json()["id"]
    invitation = await client.post(
        f"/api/v1/workspaces/{other}/collaboration", headers=rh, json={"email": p.email, "role": "maintainer"}
    )
    gid = invitation.json()["id"]
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200

    inside = await minted(client, ph, source, delivery["id"])
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{other}", headers=inside)).status_code == 404
    listed = await client.get("/api/v1/workspaces", headers=inside)
    assert [w["id"] for w in listed.json()] == [target], listed.text
    granted = await client.post(f"/api/v1/workspace-collaborations/{gid}/session", headers=ph)
    assert granted.status_code == 200, granted.text
    assert (await client.get(f"/api/v1/workspaces/{other}", headers=bearer(granted.json()["access_token"]))).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=bearer(granted.json()["access_token"]))).status_code == 404
    identity = await _resolve_active_websocket_identity(claims(inside), p.id)
    assert identity is not None and identity[0] == customer and identity[2] is True

    version = await token_version(db_session, p.id)
    for path, body in (
        ("/api/v1/auth/renew", None),
        ("/api/v1/auth/entities/switch", {"entity_id": p.entity_id}),
        ("/api/v1/auth/logout", None),
    ):
        refused = await client.post(path, headers=inside, json=body)
        assert refused.status_code == 403, (path, refused.text)
    assert await token_version(db_session, p.id) == version
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{source}", headers=ph)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=ph)).json()["entity_id"] == p.entity_id


@pytest.mark.asyncio
async def test_only_a_scoped_session_authenticates_a_collaborator(client, db_session):
    # Catches: an ordinary token for the customer company (a switch, a stale
    # browser token) still reaching the Workspace or the socket; a scoped
    # token for a company where the person is a real member being accepted as
    # an ordinary sign-in; an expired scoped token still working.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    plain = create_access_token(p.id, customer, "collaborator", token_version=p.token_version)
    refused = await client.get(f"/api/v1/workspaces/{target}", headers=bearer(plain))
    assert refused.status_code == 401 and refused.json()["detail"]["code"] == "collaboration_ended"
    # The socket closes with 4001 when no identity resolves.
    assert await _resolve_active_websocket_identity(decode_token(plain), p.id) is None

    home, _ = create_collaborator_session_token(
        user_id=p.id, entity_id=p.entity_id, workspace_id=source, kind="client_trial", ref=delivery["id"],
        token_version=p.token_version,
    )
    assert (await client.get(f"/api/v1/workspaces/{source}", headers=bearer(home))).status_code == 401
    assert await _resolve_active_websocket_identity(decode_token(home), p.id) is None

    inside = await minted(client, ph, source, delivery["id"])
    payload = claims(inside)
    payload["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    settings = get_settings()
    expired = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=bearer(expired))).status_code == 401


async def two_grants(client, db_session):
    """The provider in a trial Workspace and, by the customer's invitation, a second one."""
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    second = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Second", "kind": "project", "primary_work": "More"}
    )
    other = second.json()["id"]
    invitation = await client.post(
        f"/api/v1/workspaces/{other}/collaboration", headers=rh, json={"email": p.email, "role": "maintainer"}
    )
    gid = invitation.json()["id"]
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    return ph, p, rh, r, source, target, other, customer, delivery, gid


@pytest.mark.asyncio
async def test_everything_a_scoped_session_decides_stays_in_its_pinned_workspace(client, db_session):
    # Catches: Knowledge, resource or runtime checks inside a scoped session's
    # request reading every live grant of the person instead of the one
    # Workspace the session pins.
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services.collaborator_scope import collaborator_live_workspaces
    from packages.core.services.collaborator_session import pin_session
    from packages.core.services.document_access import user_can_read_document

    ph, p, rh, r, source, target, other, customer, delivery, gid = await two_grants(client, db_session)
    doc = Document(entity_id=customer, name="Second brief.md", owner_id=r.id, visibility="workspace")
    group = DocumentGroup(entity_id=customer, workspace_id=other, name="Second knowledge")
    db_session.add_all([doc, group])
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
    await db_session.commit()
    inside = await minted(client, ph, source, delivery["id"])
    assert (await client.get(f"/api/v1/documents/{doc.id}", headers=inside)).status_code == 404

    assert await collaborator_live_workspaces(db_session, user_id=p.id, entity_id=customer) == {target, other}
    assert await user_can_read_document(db_session, doc, entity_id=customer, user_id=p.id)
    pin_session(customer, p.id, {target})
    try:
        assert await collaborator_live_workspaces(db_session, user_id=p.id, entity_id=customer) == {target}
        assert not await user_can_read_document(db_session, doc, entity_id=customer, user_id=p.id)
    finally:
        pin_session(None, None, ())


@pytest.mark.asyncio
async def test_a_scoped_session_ends_with_the_grant_it_names(client, db_session):
    # Catches: a session minted for one grant (or one delivery) staying valid
    # through another live grant on the same Workspace, or naming a delivery
    # it was never minted for.
    ph, p, rh, r, source, target, other, customer, delivery, gid = await two_grants(client, db_session)
    granted = await client.post(f"/api/v1/workspace-collaborations/{gid}/session", headers=ph)
    old = bearer(granted.json()["access_token"])
    assert (await client.get(f"/api/v1/workspaces/{other}", headers=old)).status_code == 200
    assert (await client.post(f"/api/v1/workspaces/{other}/collaboration/{gid}/revoke", headers=rh)).status_code == 200
    again = await client.post(
        f"/api/v1/workspaces/{other}/collaboration", headers=rh, json={"email": p.email, "role": "maintainer"}
    )
    assert (await client.post(f"/api/v1/workspace-collaborations/{again.json()['id']}/accept", headers=ph)).status_code == 200
    ended = await client.get(f"/api/v1/workspaces/{other}", headers=old)
    assert ended.status_code == 401 and ended.json()["detail"]["code"] == "collaboration_ended"
    assert await _resolve_active_websocket_identity(claims(old), p.id) is None
    renewed = await client.post(f"/api/v1/workspace-collaborations/{again.json()['id']}/session", headers=ph)
    assert (await client.get(f"/api/v1/workspaces/{other}", headers=bearer(renewed.json()["access_token"]))).status_code == 200

    forged, _ = create_collaborator_session_token(
        user_id=p.id, entity_id=customer, workspace_id=target, kind="client_trial", ref="0" * 26,
        token_version=p.token_version,
    )
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=bearer(forged))).status_code == 401


@pytest.mark.asyncio
async def test_a_scoped_socket_hears_only_its_workspace_and_closes_when_the_session_expires(
    client, db_session, monkeypatch
):
    # Catches: a scoped socket receiving another live-granted Workspace's
    # events, or staying open (and fed) after its session expired.
    import json
    from types import SimpleNamespace

    from apps.api.routers import ws as ws_router

    ph, p, rh, r, source, target, other, customer, delivery, gid = await two_grants(client, db_session)
    inside = await minted(client, ph, source, delivery["id"])
    manager = ws_router.ConnectionManager()
    monkeypatch.setattr(ws_router, "manager", manager)

    class Socket:
        def __init__(self, frames=()):
            self.events, self.frames, self.closed = [], list(frames), None

        async def accept(self):
            return None

        async def send_text(self, message):
            self.events.append(json.loads(message)["event"])

        async def receive_text(self):
            return self.frames.pop(0)

        async def close(self, code=1000, reason=""):
            self.closed = code

    socket = Socket()
    payload = claims(inside)
    await manager.connect(p.id, customer, p.token_version, socket, pinned_workspace_id=target, expires_at=payload["exp"])

    async def reached(workspace_id):
        socket.events.clear()
        await ws_router._relay_event(
            {"target": "workspace", "entity_id": customer, "workspace_id": workspace_id, "event": "task_update", "data": {}}
        )
        return bool(socket.events)

    assert (await reached(target), await reached(other)) == (True, False)
    # The socket module's clock only: the session's expiry has passed.
    monkeypatch.setattr(ws_router, "time", SimpleNamespace(time=lambda: payload["exp"] + 1))
    assert await reached(target) is False

    async def no_session(**_kwargs):
        return None

    monkeypatch.setattr(ws_router, "_ensure_tracked_session", no_session)
    monkeypatch.setattr(ws_router, "_close_tracked_session", no_session)
    live = Socket(frames=[json.dumps({"type": "ping"})])
    await ws_router.websocket_endpoint(live, token=inside["Authorization"].removeprefix("Bearer "))
    assert live.closed == 4001 and "pong" not in live.events


def test_session_lifetime_is_bounded(monkeypatch):
    # Catches: a misconfigured lifetime minting a long-lived customer token.
    from packages.core.services.auth_service import collaborator_session_minutes

    for value, expected in (("1", 2), ("15", 15), ("600", 60), ("junk", 15)):
        monkeypatch.setenv("MANOR_COLLABORATOR_SESSION_MINUTES", value)
        assert collaborator_session_minutes() == expected
    monkeypatch.delenv("MANOR_COLLABORATOR_SESSION_MINUTES")
    token, expires = create_collaborator_session_token(
        user_id="U", entity_id="E", workspace_id="W", kind="grant", ref="G"
    )
    assert timedelta(minutes=14) < expires - datetime.now(timezone.utc) <= timedelta(minutes=15)
    assert decode_token(token)["cs"] == {"workspace_id": "W", "kind": "grant", "ref": "G"}


@pytest.mark.asyncio
async def test_a_collaborator_membership_is_never_a_company_to_switch_to(client, db_session):
    # Catches: the account and people views offering the customer company;
    # either switch endpoint (or the pointer helper) moving the person into
    # it; leaving another company landing there; a browser-session refresh
    # that names it minting or saving it.
    from test_collaborator_boundary import BROWSER
    from packages.core.models.browser_session import BrowserSession
    from packages.core.models.staff import Staff
    from packages.core.models.user import Entity, UserMembership
    from packages.core.services.auth_service import activate_user_membership

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home = (await db_session.get(Workspace, target)).entity_id, p.entity_id
    pid, email, version = p.id, p.email, p.token_version
    me = await client.get("/api/v1/auth/me", headers=ph)
    assert [m["entity_id"] for m in me.json()["memberships"]] == [home]
    people = await client.get("/api/v1/people/me", headers=ph)
    assert [m["entity_id"] for m in people.json()["memberships"]] == [home]
    for path, body in (
        ("/api/v1/auth/entities/switch", {"entity_id": customer}),
        (f"/api/v1/people/memberships/{customer}/switch", None),
    ):
        refused = await client.post(path, headers=ph, json=body)
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "collaborator_membership_not_switchable"
    collaborator = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == customer)
    )
    with pytest.raises(ValueError):
        await activate_user_membership(db_session, user=p, membership=collaborator)
    await db_session.rollback()

    # Leaving a third company returns to home, even when the collaborator
    # membership would sort first.
    third = Entity(name="Agency")
    db_session.add(third)
    await db_session.flush()
    staff = Staff(entity_id=third.id, user_id=pid, name="Provider", email=email, status="active")
    db_session.add(staff)
    await db_session.flush()
    db_session.add(UserMembership(user_id=pid, entity_id=third.id, role="member", status="active", staff_id=staff.id))
    collaborator = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == pid, UserMembership.entity_id == customer)
    )
    collaborator.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    own = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == pid, UserMembership.entity_id == home)
    )
    own.is_primary = False
    await db_session.commit()
    inside_third = bearer(create_access_token(pid, third.id, "member", token_version=version))
    left = await client.post(f"/api/v1/people/memberships/{third.id}/leave", headers=inside_third)
    assert left.status_code == 200, left.text
    assert decode_token(left.json()["access_token"])["entity_id"] == home
    assert await db_session.scalar(
        select(User.entity_id).where(User.id == pid).execution_options(populate_existing=True)
    ) == home

    login = {"email": email, "password": "DeliveryFixture123!", "remember_me": True}
    assert (await client.post("/api/v1/auth/login", headers=BROWSER, json=login)).status_code == 200
    refreshed = await client.post("/api/v1/auth/session/refresh", headers=BROWSER, json={"entity_id": customer})
    assert (refreshed.status_code, refreshed.json()["entity_id"]) == (200, home), refreshed.text
    saved = await db_session.scalars(
        select(BrowserSession.entity_id)
        .where(BrowserSession.user_id == pid, BrowserSession.revoked_at.is_(None))
        .execution_options(populate_existing=True)
    )
    assert set(saved.all()) == {home}


async def second_collaborator_company(db, user_id):
    """A collaborator membership in another customer company."""
    from packages.core.models.user import Entity, UserMembership

    other = Entity(name="Second customer")
    db.add(other)
    await db.flush()
    db.add(UserMembership(user_id=user_id, entity_id=other.id, role="collaborator", status="active"))
    await db.commit()
    return other.id


@pytest.mark.asyncio
async def test_losing_a_company_never_lands_in_a_collaborator_one(client, db_session):
    # Catches: removal from a company moving the person's company to a
    # collaborator membership (or keeping them active with none of their own
    # left); a browser refresh falling back to another collaborator company
    # the person's pointer happens to name.
    from test_collaborator_boundary import BROWSER
    from packages.core.models.browser_session import BrowserSession
    from packages.core.services.people_service import _apply_entity_access

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home, pid, email = (await db_session.get(Workspace, target)).entity_id, p.entity_id, p.id, p.email
    other = await second_collaborator_company(db_session, pid)
    login = {"email": email, "password": "DeliveryFixture123!", "remember_me": True}
    assert (await client.post("/api/v1/auth/login", headers=BROWSER, json=login)).status_code == 200
    session = await db_session.scalar(select(BrowserSession).where(BrowserSession.user_id == pid))
    session.entity_id = customer
    await db_session.execute(User.__table__.update().where(User.id == pid).values(entity_id=other))
    await db_session.commit()
    refreshed = await client.post("/api/v1/auth/session/refresh", headers=BROWSER, json={})
    assert (refreshed.status_code, refreshed.json()["entity_id"]) == (200, home), refreshed.text

    await db_session.execute(User.__table__.update().where(User.id == pid).values(entity_id=home))
    await db_session.commit()
    await _apply_entity_access(db_session, user_id=pid, entity_id=home, enabled=False)
    await db_session.commit()
    person = (await db_session.execute(
        select(User.entity_id, User.status).where(User.id == pid).execution_options(populate_existing=True)
    )).one()
    assert (person.entity_id, person.status) == (home, "inactive")
