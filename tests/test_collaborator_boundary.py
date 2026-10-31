"""An external collaborator's token reaches only the Workspace their live grant covers."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from test_client_workspace_delivery import register
from test_workspace_collaboration import access_grant, accepted, derived, minted
from apps.api.routers import ws as ws_router
from apps.api.routers.ws import _resolve_active_websocket_identity
from apps.api.collaborator_boundary import (
    SELF_ROUTES,
    WORKSPACE_ADMIN_DENY,
    WORKSPACE_FILTERED_ROUTES,
    WORKSPACE_PATH_PARAMS,
    WORKSPACE_ROUTES_REVIEWED,
    route_access,
)
from packages.core.models.channel import ChannelConfig
from packages.core.models.chat_room import ChatRoom, ChatRoomMember
from packages.core.stats.service import create_stat_from_library
from packages.core.services.channel_bindings import (
    load_channel_binding_scopes_for_config,
    resolve_public_webchat_channel_by_token,
)
from packages.core.models.task import Task
from packages.core.models.user import UserMembership
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceStaff
from packages.core.services.auth_service import decode_token

BROWSER = {"Origin": "http://test", "X-Manor-Session": "1"}


# Every websocket and how it keeps a collaborator token out of entity-wide streams.
WEBSOCKETS = {
    "/ws": "admitted while a grant is live; ws.py delivers only that Workspace's events",
    "/api/v1/audio/live": "chat_voice authenticates through get_current_user; the boundary denies it",
    "/api/v1/chrome/connect": "a device ticket from an approved pairing, not a user token",
    "/api/v1/channels/twilio_voice/stream/{session_token}": "a provider call session token",
    "/api/v1/workers/{worker_id}/browser-mcp/connect": "a worker secret",
}


def test_every_route_has_a_collaborator_decision():
    # Catches: a new entity route (or a prefix rule) opening to collaborators
    # without being listed; a new route under a Workspace path being reachable
    # without anyone deciding it is safe for a collaborator; a listed route
    # that no longer exists; a new websocket that a collaborator token can join.
    from fastapi.routing import APIWebSocketRoute

    from apps.api.main import create_app

    app = create_app()
    routes = {
        (method, route.path_format): set(route.param_convertors)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    listed = SELF_ROUTES | WORKSPACE_ADMIN_DENY | WORKSPACE_ROUTES_REVIEWED | set(WORKSPACE_FILTERED_ROUTES)
    assert listed - routes.keys() == set()
    scoped = {key for key, params in routes.items() if params & set(WORKSPACE_PATH_PARAMS)}
    unclassified = scoped - WORKSPACE_ROUTES_REVIEWED - WORKSPACE_ADMIN_DENY
    assert not unclassified, (
        "New Workspace routes need a collaborator decision in apps/api/collaborator_boundary.py: add each to "
        f"WORKSPACE_ROUTES_REVIEWED (external collaborators may use it) or WORKSPACE_ADMIN_DENY: {sorted(unclassified)}"
    )
    assert not WORKSPACE_ROUTES_REVIEWED & WORKSPACE_ADMIN_DENY
    assert not (SELF_ROUTES | set(WORKSPACE_FILTERED_ROUTES)) & scoped
    reachable = {key for key, params in routes.items() if key not in scoped and route_access(key, params) != "deny"}
    assert reachable == SELF_ROUTES | set(WORKSPACE_FILTERED_ROUTES)
    assert {key for key in scoped if route_access(key, routes[key]) == "deny"} == WORKSPACE_ADMIN_DENY

    sockets = {route.path_format for route in app.routes if isinstance(route, APIWebSocketRoute)}
    assert sockets - WEBSOCKETS.keys() == set(), "Classify each new websocket for collaborator tokens in WEBSOCKETS"
    assert route_access(("POST", "/api/v1/audio/live"), {}) == "deny"


async def minted_in(client, db):
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db)
    customer = (await db.get(Workspace, target)).entity_id
    inside = await minted(client, ph, source, delivery["id"])
    return ph, p, rh, r, target, customer, inside


@pytest.mark.asyncio
async def test_collaborator_token_reaches_only_its_live_workspace(client, db_session):
    # Catches: any entity-gated route (CRM, memories, activity, reports, usage,
    # dashboard, Workspace creation, company settings, people, channel logs,
    # chat rooms, schedules) admitting the customer company's collaborator;
    # another customer Workspace or an entity-level row being reachable; the
    # granted Workspace itself not working; customer-only Workspace management
    # being open (including making the Workspace visible company-wide); the
    # derived seat standing in for the grant's live authority.
    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )
    assert other.status_code == 201, other.text
    in_workspace = Task(entity_id=customer, workspace_id=target, title="Trial task")
    entity_level = Task(entity_id=customer, title="Customer back-office task")
    db_session.add_all([in_workspace, entity_level])
    await db_session.commit()

    leaks = [
        ("GET", "/api/v1/clients"),
        ("GET", "/api/v1/bulk/export/clients"),
        ("GET", "/api/v1/memories"),
        ("GET", "/api/v1/activity/events"),
        ("GET", "/api/v1/reports/activity"),
        ("GET", "/api/v1/usage"),
        ("GET", "/api/v1/dashboard/stats"),
        ("POST", "/api/v1/workspaces"),
        ("GET", "/api/v1/entities/me"),
        ("GET", "/api/v1/people/directory"),
        ("GET", "/api/v1/staff"),
        ("GET", "/api/v1/integrations/logs"),
        ("GET", "/api/v1/chat/rooms"),
        ("GET", "/api/v1/notifications"),
        ("GET", "/api/v1/auth/users/directory"),
    ]
    for method, path in leaks:
        response = await client.request(method, path, headers=inside, json={} if method == "POST" else None)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"] == "This page is not available to external collaborators"
    assert (await client.get(f"/api/v1/workspaces/{other.json()['id']}", headers=inside)).status_code == 404
    assert (await client.get("/api/v1/tasks", headers=inside)).status_code == 404
    assert (await client.get("/api/v1/jobs", headers=inside)).status_code == 404
    assert (await client.get(f"/api/v1/tasks/{entity_level.id}", headers=inside)).status_code == 404

    for path in (
        f"/api/v1/workspaces/{target}",
        f"/api/v1/tasks?workspace_id={target}",
        f"/api/v1/tasks/{in_workspace.id}",
        f"/api/v1/workspaces/{target}/chat/messages",
        f"/api/v1/workspaces/{target}/activity",
    ):
        response = await client.get(path, headers=inside)
        assert response.status_code == 200, (path, response.text)
    customer_seat = (await client.get(f"/api/v1/workspaces/{target}/staff", headers=rh)).json()
    staff_id = next(row["id"] for row in customer_seat if row.get("user_id") == r.id)
    refused = [
        await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=inside, json={"action": "accept"}),
        await client.post(f"/api/v1/workspaces/{target}/staff", headers=inside, json={"user_id": p.id}),
        await client.delete(f"/api/v1/workspaces/{target}/staff/{staff_id}", headers=inside),
        await client.put(
            f"/api/v1/workspaces/{target}/blueprint-export/knowledge-documents/unreachable/initial-template",
            headers=inside,
            json={"body_md": "Reviewed starter"},
        ),
    ]
    assert [response.status_code for response in refused] == [403, 403, 403, 403]
    assert refused[-1].json()["detail"] == "This page is not available to external collaborators"
    current = (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).json()["settings"]
    shared = await client.put(
        f"/api/v1/workspaces/{target}", headers=inside, json={"settings": {**current, "access_mode": "entity_visible"}}
    )
    assert shared.status_code == 403, shared.text
    renamed = await client.put(f"/api/v1/workspaces/{target}", headers=inside, json={"name": "Trial, renamed"})
    assert renamed.status_code == 200, renamed.text
    # Notification routing sends task events through the company's channel
    # accounts to a chosen target; only the allowlisted run settings are the
    # collaborator's. A member still routes notifications.
    routed = {"task_events": {"external_chat": {"enabled": True, "channel_config_ids": ["company-bot"], "target": "+15550100"}}}
    for settings, expected, headers in (
        ({**current, "notification_policy": routed}, 403, inside),
        ({**current, "notification_policy": {"inbound_notify_user_ids": [p.id]}}, 403, inside),
        ({**current, "execution_policy": {"retry_policy": {"max_attempts": 2}}}, 200, inside),
        ({**current, "notification_policy": routed}, 200, rh),
    ):
        response = await client.put(f"/api/v1/workspaces/{target}", headers=headers, json={"settings": settings})
        assert response.status_code == expected, (settings, response.text)
        current = response.json()["settings"] if expected == 200 else current

    # The customer owner who issued the grant leaves; nothing re-derives the seat.
    issuer = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == r.id, UserMembership.entity_id == customer)
    )
    issuer.status = "inactive"
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.workspace_id for s in seats] == [target]
    ended = await client.get(f"/api/v1/workspaces/{target}", headers=inside)
    assert ended.status_code == 401, ended.text
    assert ended.json()["detail"]["code"] == "collaboration_ended"


@pytest.mark.asyncio
async def test_collaborator_app_design_stages_only_in_live_granted_workspace(client, db_session, monkeypatch):
    from packages.core.services import workspace_app_designer
    from packages.core.services.workspace_app_designer import AppDesignResult

    _ph, provider, _rh, _customer_user, target, customer, inside = await minted_in(client, db_session)
    outside = Workspace(entity_id=customer, name="Customer private workspace")
    db_session.add(outside)
    await db_session.commit()
    calls = []

    async def designer(context, **kwargs):
        calls.append((kwargs["workspace_id"], kwargs["user_id"]))
        candidate, errors, warnings = context.check({"title": "Trial page"})
        assert not errors, errors
        return AppDesignResult(candidate, [], warnings, "Local provider fixture", 1)

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", designer)
    original = (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).json()["operating_model"]
    response = await client.post(
        f"/api/v1/workspaces/{target}/app-view/design", headers=inside, json={"brief": "Design the trial page"}
    )
    assert response.status_code == 200, response.text
    draft = response.json()["draft"]
    assert draft["workspace_id"] == target
    assert draft["patches"][0]["op"] == "app_view.update"
    assert not draft["validation"].get("errors")
    current = (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).json()["operating_model"]
    assert current == original  # Designing never applies the draft.
    denied = await client.post(
        f"/api/v1/workspaces/{outside.id}/app-view/design", headers=inside, json={"brief": "Change another page"}
    )
    assert denied.status_code == 404, denied.text
    assert calls == [(target, provider.id)]


class _Socket:
    def __init__(self):
        self.events: list[str] = []

    async def accept(self):
        return None

    async def send_text(self, message: str):
        self.events.append(json.loads(message)["event"])


@pytest.mark.asyncio
async def test_collaborator_socket_receives_only_its_live_workspace(client, db_session, monkeypatch):
    # Catches: a company-wide broadcast (presence, typing, Task/job updates,
    # notifications), a personal event naming no Workspace, a withheld
    # notification, a chat room, or another customer Workspace's events
    # reaching the collaborator socket; the granted Workspace's events missing
    # it; the derived seat standing in for a grant whose issuer lost authority;
    # an ended collaboration still being admitted or its open socket passing
    # the per-frame identity check.
    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    room = ChatRoom(kind="group", title="Provider room", owner_user_id=p.id)
    db_session.add(room)
    await db_session.flush()
    db_session.add(ChatRoomMember(room_id=room.id, user_id=p.id, visible_from=datetime(2000, 1, 1, tzinfo=UTC)))
    await db_session.commit()

    def claims(headers):
        return decode_token(headers["Authorization"].removeprefix("Bearer "))

    collaborator = await _resolve_active_websocket_identity(claims(inside), p.id)
    assert collaborator is not None and collaborator[0] == customer and collaborator[2] is True
    member = await _resolve_active_websocket_identity(claims(rh), r.id)
    assert member is not None and member[0] == customer and member[2] is False
    manager = ws_router.ConnectionManager()
    monkeypatch.setattr(ws_router, "manager", manager)
    sockets = {p.id: _Socket(), r.id: _Socket()}
    await manager.connect(
        p.id, customer, collaborator[1], sockets[p.id], pinned_workspace_id=claims(inside)["cs"]["workspace_id"]
    )
    await manager.connect(r.id, customer, member[1], sockets[r.id])

    async def reached(target_kind, event="task_update", data=None, **fields):
        for socket in sockets.values():
            socket.events.clear()
        await ws_router._relay_event(
            {"target": target_kind, "entity_id": customer, "event": event, "data": data or {}, **fields}
        )
        return {user_id for user_id, socket in sockets.items() if socket.events}

    assert await reached("entity") == {r.id}
    assert await reached("entity", "typing") == {r.id}
    # An entity Task or Job update about a row of the granted Workspace reaches
    # the collaborator; one of another Workspace, or any other event, does not.
    for event in ("task_update", "job_update"):
        assert await reached("entity", event, {"workspace_id": target}) == {r.id, p.id}
        assert await reached("entity", event, {"workspace_id": other_id}) == {r.id}
    assert await reached("entity", "typing", {"workspace_id": target}) == {r.id}
    assert await reached("workspace", workspace_id=target) == {r.id, p.id}
    assert await reached("workspace", workspace_id=other_id) == {r.id}
    assert await reached("workspace_user", "approval", user_id=p.id, workspace_id=target) == {p.id}
    assert await reached("workspace_user", "approval", user_id=p.id, workspace_id=other_id) == set()
    assert await reached("workspace_user", "notification", user_id=p.id, workspace_id=target) == set()
    assert await reached("workspace_user", "notification", user_id=r.id, workspace_id=target) == {r.id}
    assert await reached("user", "conversation_message", user_id=p.id) == set()
    assert await reached("user", "conversation_message", user_id=r.id) == {r.id}
    assert await reached("chat_room", "chat_room_changed", room_id=room.id) == set()
    assert await ws_router._collaborator_connection_version(entity_id=customer, user_id=p.id) == collaborator[1]

    # The customer owner who issued the grant leaves; nothing re-derives the seat.
    issuer = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == r.id, UserMembership.entity_id == customer)
    )
    issuer.status = "inactive"
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.workspace_id for s in seats] == [target]
    assert await reached("workspace", workspace_id=target) == set()
    assert await reached("entity", "job_update", {"workspace_id": target}) == set()
    assert await reached("workspace_user", "approval", user_id=p.id, workspace_id=target) == set()
    assert await ws_router._collaborator_connection_version(entity_id=customer, user_id=p.id) is None
    assert await _resolve_active_websocket_identity(claims(inside), p.id) is None


@pytest.mark.asyncio
async def test_provider_who_stops_managing_the_source_workspace_loses_the_customer_workspace(client, db_session):
    # Catches: a delivery grant staying live after its holder lost manage on the
    # delivered source Workspace in their own company (HTTP, socket or runtime
    # scope); the check demanding a source seat from a company owner/admin who
    # manages every Workspace anyway.
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.services.collaborator_scope import collaborator_live_workspaces

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home = (await db_session.get(Workspace, target)).entity_id, p.entity_id
    inside = await minted(client, ph, source, delivery["id"])
    claims = decode_token(inside["Authorization"].removeprefix("Bearer "))
    seat = await db_session.scalar(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == source, WorkspaceStaff.user_id == p.id)
    )
    await db_session.delete(seat)
    await db_session.commit()
    # The provider company's owner still manages the source without a seat.
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).status_code == 200
    assert await collaborator_live_workspaces(db_session, user_id=p.id, entity_id=customer) == {target}

    membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == home)
    )
    membership.role = "member"
    staff = await db_session.scalar(select(Staff).where(Staff.user_id == p.id, Staff.entity_id == home))
    if staff is not None:
        restricted = StaffRole(entity_id=home, name="No administrative permissions", permissions=[])
        db_session.add(restricted)
        await db_session.flush()
        staff.role_id = restricted.id
    await db_session.commit()
    memberships, seats = await derived(db_session, p.id, customer)
    assert [m.status for m in memberships] == ["active"] and [s.workspace_id for s in seats] == [target]
    ended = await client.get(f"/api/v1/workspaces/{target}", headers=inside)
    assert ended.status_code == 401, ended.text
    assert ended.json()["detail"]["code"] == "collaboration_ended"
    assert await _resolve_active_websocket_identity(claims, p.id) is None
    assert await collaborator_live_workspaces(db_session, user_id=p.id, entity_id=customer) == frozenset()


@pytest.mark.asyncio
async def test_ended_collaboration_returns_the_collaborator_to_their_own_company(client, db_session):
    # Catches: the old collaborator token getting a generic 403 the web app
    # cannot explain; a browser-session refresh minting a token for a
    # collaborator company (live or ended) instead of the person's own; a new
    # sign-in landing there; the provider's public webchat going dark, staying
    # in their hands, or its binding keeping the provider so routing silently
    # drops it.
    from packages.core.models.browser_session import BrowserSession

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home = (await db_session.get(Workspace, target)).entity_id, p.entity_id
    login = {"email": p.email, "password": "DeliveryFixture123!", "remember_me": True}
    signed = await client.post("/api/v1/auth/login", headers=BROWSER, json=login)
    assert signed.status_code == 200, signed.text
    inside = await minted(client, ph, source, delivery["id"])
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=inside)).status_code == 200
    # Kept so a browser session left in the customer company by a switch made
    # before scoped sessions still finds its way home: refresh never returns a
    # collaborator company, however the session or the client names it.
    session = await db_session.scalar(select(BrowserSession).where(BrowserSession.user_id == p.id))
    session.entity_id = customer
    p.entity_id, p.role = customer, "collaborator"
    await db_session.commit()
    live = await client.post("/api/v1/auth/session/refresh", headers=BROWSER, json={"entity_id": customer})
    assert (live.status_code, live.json()["entity_id"]) == (200, home), live.text
    await db_session.refresh(session)
    session.entity_id = customer
    await db_session.commit()
    agent = Agent(entity_id=customer, workspace_id=target, owner_user_id=r.id, name="Support agent")
    db_session.add(agent)
    await db_session.flush()
    subscription = AgentSubscription(entity_id=customer, agent_id=agent.id, workspace_id=target, service_key="support")
    db_session.add(subscription)
    await db_session.commit()
    webchat = await client.post(
        f"/api/v1/workspaces/{target}/channels",
        headers=inside,
        json={"name": "Support chat", "agent_subscription_id": subscription.id},
    )
    assert webchat.status_code in (200, 201), webchat.text
    channel = await db_session.scalar(
        select(ChannelConfig).where(ChannelConfig.workspace_id == target, ChannelConfig.channel_type == "webchat")
    )
    assert channel.owner_user_id == p.id

    for payload in ({"action": "request_acceptance"}, {"action": "accept", "provider": "remove"}):
        decided = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
        assert decided.status_code == 200, decided.text
    ended = await client.get(f"/api/v1/workspaces/{target}", headers=inside)
    assert ended.status_code == 401, ended.text
    assert ended.json()["detail"]["code"] == "collaboration_ended"
    # The provider's public webchat stays up, now owned by the customer who authorized them.
    await db_session.refresh(channel)
    assert channel.owner_user_id == r.id
    public = await client.get(f"/api/v1/public/chat/{channel.config['public_token']}")
    assert public.status_code == 200, public.text
    # Routing still finds the binding and its agent: the binding moved with its config.
    scopes = await load_channel_binding_scopes_for_config(db_session, channel)
    assert [(scope.binding.user_id, scope.agent_id) for scope in scopes] == [(r.id, agent.id)]
    _, binding = await resolve_public_webchat_channel_by_token(db_session, channel.config["public_token"])
    assert binding is not None and binding.user_id == r.id

    refreshed = await client.post("/api/v1/auth/session/refresh", headers=BROWSER, json={"entity_id": customer})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["entity_id"] == home
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"})
    assert me.json()["entity_id"] == home
    again = await client.post("/api/v1/auth/login", headers=BROWSER, json=login)
    assert again.json()["entity_id"] == home
    registered, _ = await register(client, db_session, "bystander")
    assert (await client.get("/api/v1/auth/me", headers=registered)).status_code == 200


@pytest.mark.asyncio
async def test_workspace_page_families_open_only_inside_the_granted_workspace(client, db_session):
    # Catches: a Workspace-page route family (Workspace list, tasks,
    # goals, jobs, workflows, chat, approvals, documents, agents) staying shut
    # for the co-working provider, or reaching a row of another customer
    # Workspace or of the company itself; an entity Agent pulled into the
    # Workspace by id; a list leaking the customer's other Workspaces, Agents
    # or Workflows; the collaborator rewriting who can see or manage it.
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.task import Conversation, Message
    from packages.core.models.workflow import WorkflowDefinition


    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = (await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )).json()["id"]
    rows = {
        "task": Task(entity_id=customer, workspace_id=target, title="Trial task"),
        "back_task": Task(entity_id=customer, title="Back-office task"),
        "agent": Agent(entity_id=customer, workspace_id=target, owner_user_id=r.id, name="Trial agent"),
        "back_agent": Agent(entity_id=customer, owner_user_id=r.id, name="Payroll agent"),
        "flow": WorkflowDefinition(entity_id=customer, workspace_id=target, name="Trial flow", created_by=r.id),
        "back_flow": WorkflowDefinition(entity_id=customer, name="Payroll flow", created_by=r.id),
        "job": ScheduledJob(job_id="trial-digest", entity_id=customer, workspace_id=target, name="Digest"),
        "back_job": ScheduledJob(job_id="payroll-run", entity_id=customer, name="Payroll"),
        "chat": Conversation(entity_id=customer, workspace_id=target, user_id=p.id, title="Trial chat"),
        "back_chat": Conversation(entity_id=customer, user_id=r.id, title="Owner's private chat"),
        "doc": Document(entity_id=customer, name="Brief.md", owner_id=r.id, visibility="workspace"),
        "back_doc": Document(entity_id=customer, name="Payroll.md", owner_id=r.id, visibility="entity"),
        "group": DocumentGroup(entity_id=customer, workspace_id=target, name="Trial knowledge"),
    }
    db_session.add_all(rows.values())
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=rows["doc"].id, group_id=rows["group"].id))
    # A seat without a grant behind it (the derived rows may outlive authority) is no access.
    db_session.add(WorkspaceStaff(workspace_id=other, user_id=p.id, role="viewer", added_by=r.id, status="active"))
    messages = {
        key: Message(conversation_id=rows[chat].id, role="assistant", content="Approve?")
        for key, chat in (("message", "chat"), ("back_message", "back_chat"))
    }
    db_session.add_all(messages.values())
    await db_session.commit()
    ids = {key: row.id for key, row in {**rows, **messages}.items()}

    async def status(method, path, **kwargs):
        return (await client.request(method, path, headers=inside, **kwargs)).status_code

    families = [
        # (granted: method, path, kwargs, expected), (outside: method, path, kwargs, expected)
        (("PUT", f"/api/v1/tasks/{ids['task']}", {"json": {"title": "Renamed"}}, 200),
         ("PUT", f"/api/v1/tasks/{ids['back_task']}", {"json": {"title": "Renamed"}}, 404)),
        (("POST", "/api/v1/goals", {"json": {"title": "Ten posts", "workspace_id": target}}, 201),
         ("POST", "/api/v1/goals", {"json": {"title": "Ten posts", "workspace_id": other}}, 404)),
        (("GET", f"/api/v1/jobs/{ids['job']}", {}, 200),
         ("GET", f"/api/v1/jobs/{ids['back_job']}", {}, 404)),
        (("GET", f"/api/v1/workflows/{ids['flow']}", {}, 200),
         ("GET", f"/api/v1/workflows/{ids['back_flow']}", {}, 404)),
        (("GET", f"/api/v1/chat/conversations/{ids['chat']}/active-run", {}, 200),
         ("GET", f"/api/v1/chat/conversations/{ids['back_chat']}/active-run", {}, 404)),
        # Admitted Workspace chat reaches the handler (422: no message); anything else stops at the boundary.
        (("POST", "/api/v1/chat/stream", {"data": {"workspace_id": target}}, 422),
         ("POST", "/api/v1/chat/stream", {"data": {"workspace_id": other, "message": "hi"}}, 404)),
        (("GET", f"/api/v1/chat/composer/skills?workspace_id={target}", {}, 200),
         ("GET", f"/api/v1/chat/composer/skills?workspace_id={target}&agent_id={ids['back_agent']}", {}, 404)),
        # The approval card's own handler answers (400: not a Workflow action); another's never reaches it.
        (("POST", f"/api/v1/chat/messages/{ids['message']}/resolve", {"json": {"choice": "approve"}}, 400),
         ("POST", f"/api/v1/chat/messages/{ids['back_message']}/resolve", {"json": {"choice": "approve"}}, 404)),
        (("GET", f"/api/v1/documents/{ids['doc']}", {}, 200),
         ("GET", f"/api/v1/documents/{ids['back_doc']}", {}, 404)),
        (("GET", f"/api/v1/agents/{ids['agent']}", {}, 200),
         ("GET", f"/api/v1/agents/{ids['back_agent']}", {}, 404)),
    ]
    for granted, outside in families:
        for method, path, kwargs, expected in (granted, outside):
            response = await client.request(method, path, headers=inside, **kwargs)
            assert response.status_code == expected, (method, path, response.text)

    assert [w["id"] for w in (await client.get("/api/v1/workspaces", headers=inside)).json()] == [target]
    assert await status("GET", f"/api/v1/workspaces/{other}") == 404
    catalog = (await client.get("/api/v1/agents/catalog", headers=inside)).json()["items"]
    assert [a["id"] for a in catalog] == [ids["agent"]]
    flows = (await client.get("/api/v1/workflows", headers=inside)).json()
    assert [w["id"] for w in flows] == [ids["flow"]]
    mapped = {"service_key": "payroll", "agent_id": ids["back_agent"]}
    assert await status("POST", f"/api/v1/workspaces/{target}/agents", json=mapped) == 404
    assert await status("PUT", f"/api/v1/tasks/{ids['task']}", json={"agent_id": ids["back_agent"]}) == 404
    for path in ("/api/v1/platform/flags", "/api/v1/admin/preferences"):
        assert await status("GET", path) == 200, path

    for settings in (None, {"collaboration_group_id": "someone-else"}):
        changed = await client.put(f"/api/v1/workspaces/{target}", headers=inside, json={"settings": settings})
        assert changed.status_code == 403, changed.text


@pytest.mark.asyncio
async def test_task_board_opens_only_for_the_pinned_workspace(client, db_session, monkeypatch, tmp_path):
    # Catches: the frame's task board staying shut (403) for the provider; the
    # board answering without a Workspace (the whole company's tasks) or for
    # another customer Workspace; a board drag or a comment attachment reaching
    # a task outside it, or an attachment landing outside the customer's own
    # files; the task constants or the caller's own board preferences being refused.
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = (await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )).json()["id"]
    trial = Task(entity_id=customer, workspace_id=target, title="Trial task")
    elsewhere = Task(entity_id=customer, workspace_id=other, title="Other Workspace task")
    back_office = Task(entity_id=customer, title="Back-office task")
    db_session.add_all([trial, elsewhere, back_office])
    await db_session.commit()

    board = await client.get(f"/api/v1/tasks/board?workspace_id={target}", headers=inside)
    assert board.status_code == 200, board.text
    listed = {task["id"] for column, tasks in board.json().items() if column != "_counts" for task in tasks}
    assert listed == {trial.id}
    for path in ("/api/v1/tasks/board", f"/api/v1/tasks/board?workspace_id={other}"):
        assert (await client.get(path, headers=inside)).status_code == 404, path

    for task, expected in ((trial, 200), (elsewhere, 404), (back_office, 404)):
        moved = await client.post(f"/api/v1/tasks/{task.id}/move", headers=inside, json={"status": "on_hold"})
        assert moved.status_code == expected, (task.title, moved.text)

    def attach(task, headers):
        return client.post(
            f"/api/v1/tasks/{task.id}/attachments", headers=headers, files={"file": ("brief.txt", b"brief", "text/plain")}
        )

    attached = await attach(trial, inside)
    assert attached.status_code == 200, attached.text
    assert (tmp_path / customer / "tasks" / trial.id / attached.json()["filename"]).read_bytes() == b"brief"
    assert (await client.get(attached.json()["url"], headers=inside)).status_code == 200
    kept = await attach(elsewhere, rh)
    assert kept.status_code == 200, kept.text
    assert (await attach(elsewhere, inside)).status_code == 404
    assert (await client.get(kept.json()["url"], headers=inside)).status_code == 404

    assert (await client.get("/api/v1/tasks/constants", headers=inside)).status_code == 200
    prefs = {"mode": "list", "visible_columns": ["todo"], "column_order": ["todo"]}
    saved = await client.put("/api/v1/tasks/board-preferences", headers=inside, json=prefs)
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/v1/tasks/board-preferences", headers=inside)).json()["mode"] == "list"


@pytest.mark.asyncio
async def test_collaborator_cannot_take_over_an_ownerless_customer_webchat(db_session):
    # Catches: publishing a resident connection making the collaborator the
    # owner of a customer webchat that had none.
    from types import SimpleNamespace

    from packages.core.services import webchat_resident

    channel = SimpleNamespace(owner_user_id=None, channel_type="webchat", entity_id="customer")
    collaborator = SimpleNamespace(id="provider", _collaborator_workspace_ids=frozenset({"trial"}))
    with pytest.raises(webchat_resident.ResidentAccessError) as refused:
        await webchat_resident.publish_target(
            db_session, {"enabled": True, "integration_account_id": "pms"}, user=collaborator, cc=channel, binding=None
        )
    assert refused.value.status == 403 and channel.owner_user_id is None


@pytest.mark.asyncio
async def test_collaborator_writes_name_only_their_workspace_resources(client, db_session):
    # Catches: a collaborator scheduling a company Workflow or posting into the
    # owner's private conversation from their Workspace; owning a Workspace
    # Task by another Workspace's service; collecting another Workspace's
    # Stat; an agent configuration naming outside resources in any spelling;
    # moving a binding out of the Workspace or pointing it at a company
    # Workflow or another granted Workspace's Workflow.
    from packages.core.models.task import Conversation
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = (await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )).json()["id"]
    flow = WorkflowDefinition(entity_id=customer, workspace_id=target, name="Trial flow", created_by=r.id)
    company_flow = WorkflowDefinition(entity_id=customer, name="Payroll flow", created_by=r.id)
    private_chat = Conversation(entity_id=customer, user_id=r.id, title="Owner's private chat")
    task = Task(entity_id=customer, workspace_id=target, title="Trial task")
    agent = Agent(entity_id=customer, workspace_id=other, owner_user_id=r.id, name="Ops agent")
    db_session.add_all([flow, company_flow, private_chat, task, agent])
    await db_session.flush()
    other_service = AgentSubscription(entity_id=customer, agent_id=agent.id, workspace_id=other, service_key="ops")
    binding = WorkflowBinding(entity_id=customer, workspace_id=target, workflow_id=flow.id, name="Trial binding")
    db_session.add_all([other_service, binding])
    await db_session.commit()

    def job(name, **fields):
        return {
            "job_id": name, "name": name, "schedule_kind": "every", "every_seconds": 3600, "workspace_id": target,
            "execution_type": "workflow", **fields,
        }

    created = await client.post(
        "/api/v1/jobs", headers=inside, json=job("trial-weekly", execution_target={"workflow_id": flow.id})
    )
    assert created.status_code == 201, created.text
    for body in (
        job("company-flow", execution_target={"workflow_id": company_flow.id}),
        job("owner-chat", execution_target={"workflow_id": flow.id}, conversation_id=private_chat.id),
        job("other-service", execution_target={"workflow_id": flow.id, "agent_subscription_id": other_service.id}),
    ):
        refused = await client.post("/api/v1/jobs", headers=inside, json=body)
        assert refused.status_code == 404, (body["job_id"], refused.text)
    moved = await client.put(
        f"/api/v1/jobs/{created.json()['id']}", headers=inside, json={"execution_target": {"workflow_id": company_flow.id}}
    )
    assert moved.status_code == 404, moved.text

    owned = await client.put(f"/api/v1/tasks/{task.id}", headers=inside, json={"owner_subscription_id": other_service.id})
    assert owned.status_code == 409, owned.text

    # A stat collection job names a Stat; another Workspace's is refused like any other reference.
    other_stat = await create_stat_from_library(
        db_session, entity_id=customer, workspace_id=other, library_key="workspace.tasks.completed",
    )
    await db_session.commit()
    borrowed = await client.post("/api/v1/jobs", headers=inside, json=job(
        "borrowed-stat", execution_type="workspace_stat_collection", execution_target={"stat_id": other_stat.id},
    ))
    assert borrowed.status_code == 404, borrowed.text

    # Agent configuration references count in any spelling: plural, camelCase, non-id values.
    trial_agent = Agent(entity_id=customer, workspace_id=target, owner_user_id=r.id, name="Trial agent")
    db_session.add(trial_agent)
    await db_session.commit()
    for config, expected in (
        ({"workflow_ids": [flow.id]}, 200),
        ({"workflow_ids": [flow.id, company_flow.id]}, 404),
        ({"routing": [{"agentId": agent.id}]}, 404),
        ({"skillIds": ["payroll-skill"]}, 404),
    ):
        response = await client.put(f"/api/v1/agents/{trial_agent.id}", headers=inside, json={"config": config})
        assert response.status_code == expected, (config, response.text)

    # A second live grant does not widen this session, pinned to its Workspace,
    # and another Workspace's Workflow never becomes this binding's to run.
    await access_grant(db_session, p, r, other)
    second_flow = WorkflowDefinition(entity_id=customer, workspace_id=other, name="Ops flow", created_by=r.id)
    db_session.add(second_flow)
    await db_session.commit()
    assert (await client.get(f"/api/v1/workflows/{second_flow.id}", headers=inside)).status_code == 404
    for change, expected in (
        ({"workspace_id": None}, 403),
        ({"workflow_id": company_flow.id}, 404),
        ({"workflow_id": second_flow.id}, 404),
    ):
        response = await client.put(f"/api/v1/workflows/bindings/{binding.id}", headers=inside, json=change)
        assert response.status_code == expected, (change, response.text)


@pytest.mark.asyncio
async def test_collaborator_stats_and_capabilities_reach_only_workspace_bound_integrations(client, db_session):
    # Catches: a collaborator creating, repointing or collecting an integration
    # Stat from a company connection (named, or the newest one picked when
    # none is named) and reading its values; the capabilities page counting
    # the company's connected accounts for them; a member refused; a
    # provider a member bound to the Workspace staying unusable.
    from packages.core.models.document import Integration
    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    company = Integration(entity_id=customer, owner_user_id=r.id, provider="twitter_x", status="active", config={})
    server = await db_session.scalar(select(MCPServer).where(MCPServer.server_key == "twitter_x")) or MCPServer(
        server_key="twitter_x", name="X", transport="builtin", auth_type="oauth2",
    )
    agent = Agent(entity_id=customer, workspace_id=target, name="Social agent", status="active")
    db_session.add_all([company, server, agent])
    await db_session.flush()
    db_session.add_all([
        AgentSubscription(entity_id=customer, agent_id=agent.id, workspace_id=target, service_key="social"),
        AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id, status="active"),
    ])
    await db_session.commit()
    stats = f"/api/v1/workspaces/{target}/stats"

    def library(key, **config):
        return {"library_key": key, "key": key.replace(".", "-"), "collector_config": config}

    async def connected(headers):
        services = (await client.get(f"/api/v1/workspaces/{target}/capabilities", headers=headers)).json()["services"]
        return next(item["connected_accounts"] for service in services for item in service["integrations"])

    assert (await client.post(stats, headers=inside, json=library("twitter_x.followers_count", connection_id=company.id))).status_code == 403
    member = await client.post(stats, headers=rh, json=library("twitter_x.followers_count", connection_id=company.id))
    assert member.status_code == 201, member.text
    stat_id = member.json()["id"]
    assert (await client.post(f"{stats}/{stat_id}/collect", headers=inside)).status_code == 403
    repointed = await client.patch(f"{stats}/{stat_id}", headers=inside, json={"collector_config": {
        **member.json()["collector_config"], "connection_id": None,
    }})
    assert repointed.status_code == 403, repointed.text
    assert (await connected(inside), await connected(rh)) == (0, 1)

    # A member binds the provider to the Workspace: its connection is now the Workspace's.
    workspace = await db_session.get(Workspace, target)
    workspace.operating_model = {**(workspace.operating_model or {}), "capability_bindings": [{
        "binding_key": "x", "owner_scope": "workspace_agent", "capability_type": "mcp",
        "capability": "twitter_x", "integration_key": "twitter_x", "allowed_tools": ["get_me"],
    }]}
    await db_session.commit()
    assert await connected(inside) == 1
    allowed = await client.post(stats, headers=inside, json=library("twitter_x.following_count", connection_id=company.id))
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_goal_templates_and_goal_measurement_stay_inside_the_workspace(client, db_session):
    # Catches: apply-template open to any company member without Workspace
    # write; a collaborator applying a recipe that provisions company Agents,
    # imports marketplace Skills or measures a Goal through a company
    # integration; a Goal written through the operating model (PUT goals)
    # taking an external measurement source from a collaborator; a member
    # refused either.
    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    vh, viewer = await register(client, db_session, "viewer")
    db_session.add_all([
        UserMembership(user_id=viewer.id, entity_id=customer, role="member", status="active"),
        WorkspaceStaff(workspace_id=target, user_id=viewer.id, role="viewer", added_by=r.id, status="active"),
    ])
    await db_session.commit()
    switched = await client.post("/api/v1/auth/entities/switch", headers=vh, json={"entity_id": customer})
    viewing = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    apply = f"/api/v1/workspaces/{target}/apply-template"
    briefing = {"template_key": "daily_briefing", "params": {}}
    growth = {"template_key": "twitter_growth", "params": {"target_followers": 1000, "deadline": "2027-01-01"}}

    assert (await client.post(apply, headers=viewing, json=briefing)).status_code in {403, 404}
    assert (await client.post(apply, headers=inside, json=growth)).status_code == 403
    # Provisions a company-level Agent and imports marketplace Skills: refused
    # before anything is written.
    solo = {"template_key": "solo_services", "params": {"service": "Bookkeeping", "target_clients_per_month": 3}}
    refused_solo = await client.post(apply, headers=inside, json=solo)
    assert refused_solo.status_code == 403 and "company-level resources" in refused_solo.text, refused_solo.text
    allowed = await client.post(apply, headers=inside, json=briefing)
    assert allowed.status_code == 201, allowed.text

    goals = [{"goal_key": "followers", "title": "Followers", "metric_key": "follower_count", "target_value": 100,
              "measurement_source": {"provider": "x", "action": "x.get_profile_stats"}}]
    refused = await client.put(f"/api/v1/workspaces/{target}/goals", headers=inside, json={"goals": goals})
    assert refused.status_code == 403, refused.text
    member = await client.put(f"/api/v1/workspaces/{target}/goals", headers=rh, json={"goals": goals})
    assert member.status_code == 200, member.text


@pytest.mark.asyncio
async def test_agent_editor_skills_and_new_tasks_stay_in_the_granted_workspace(client, db_session):
    # Catches: the Agent editor's skill lists showing company Skills the
    # collaborator cannot invoke, or naming the company's other Agents and
    # Workspaces as provenance; binding a Skill of another Workspace to a trial
    # Agent; reaching another Workspace's Agent; a new Task landing outside the
    # granted Workspace, without one, or naming another Workspace's Agent, a
    # company member as assignee or a company category.
    from packages.core.models.skill import AgentSkillBinding, Skill

    ph, p, rh, r, target, customer, inside = await minted_in(client, db_session)
    other = (await client.post(
        "/api/v1/workspaces", headers=rh, json={"name": "Customer only", "kind": "campaign", "primary_work": "Ops"}
    )).json()["id"]
    agent = Agent(entity_id=customer, workspace_id=target, owner_user_id=r.id, name="Trial agent")
    back_agent = Agent(entity_id=customer, workspace_id=other, owner_user_id=r.id, name="Ops agent")

    def skill(name, workspace_id=None):
        return Skill(entity_id=customer, workspace_id=workspace_id, name=name, slug=name, system_prompt="Work.")

    own, foreign, company, borrowed = skill("own", target), skill("foreign", other), skill("company"), skill("borrowed", other)
    db_session.add_all([agent, back_agent, own, foreign, company, borrowed])
    await db_session.flush()
    db_session.add_all([
        AgentSkillBinding(agent_id=agent.id, skill_id=borrowed.id),
        AgentSkillBinding(agent_id=back_agent.id, skill_id=foreign.id),
    ])
    await db_session.commit()
    skills = f"/api/v1/skills/agents/{agent.id}"

    bound = await client.get(f"{skills}/bindings", headers=inside)
    assert bound.status_code == 200, bound.text
    assert [(s["id"], s["bindings"]) for s in bound.json()] == [(borrowed.id, [])]
    available = await client.get(f"{skills}/available", headers=inside)
    assert available.status_code == 200, available.text
    assert [s["id"] for s in available.json()] == [own.id]
    owner_view = await client.get(f"{skills}/available", headers=rh)
    assert {s["id"] for s in owner_view.json()} >= {own.id, foreign.id, company.id}
    for method, path, expected in (
        ("GET", f"/api/v1/skills/agents/{back_agent.id}/bindings", 404),
        ("GET", f"/api/v1/skills/agents/{back_agent.id}/available", 404),
        ("POST", f"{skills}/bindings/{foreign.id}", 404),
        ("POST", f"{skills}/bindings/{company.id}", 404),
        ("DELETE", f"/api/v1/skills/agents/{back_agent.id}/bindings/{foreign.id}", 404),
        ("POST", f"{skills}/bindings/{own.id}", 201),
        ("DELETE", f"{skills}/bindings/{borrowed.id}", 204),
    ):
        response = await client.request(method, path, headers=inside)
        assert response.status_code == expected, (method, path, response.text)

    for body, expected in (
        ({"title": "Trial follow-up", "workspace_id": target}, 201),
        ({"title": "Mine", "workspace_id": target, "assignee_id": p.id}, 201),
        ({"title": "Elsewhere", "workspace_id": other}, 404),
        ({"title": "Company-wide"}, 404),
        ({"title": "Borrowed", "workspace_id": target, "agent_id": back_agent.id}, 404),
        ({"title": "Assigned", "workspace_id": target, "assignee_id": r.id}, 404),
        ({"title": "Filed", "workspace_id": target, "category_id": "company-category"}, 404),
    ):
        response = await client.post("/api/v1/tasks", headers=inside, json=body)
        assert response.status_code == expected, (body, response.text)
        if expected == 201:
            assert response.json()["workspace_id"] == target


@pytest.mark.asyncio
async def test_a_staff_role_named_collaborator_is_not_a_collaborator(client, db_session):
    # Catches: Workspace-scoped company reads being widened for a member whose
    # custom StaffRole happens to be called "Collaborator".
    from packages.core.models.permission import ResourceType
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.resource_access import ResourceDescriptor, user_can_access_resource

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    mh, m = await register(client, db_session, "contractor")
    role = StaffRole(entity_id=customer, name="Collaborator", permissions=[], status="active")
    db_session.add(role)
    await db_session.flush()
    staff = Staff(entity_id=customer, user_id=m.id, name="Contractor", email=m.email, role_id=role.id, status="active")
    db_session.add(staff)
    await db_session.flush()
    db_session.add_all([
        UserMembership(user_id=m.id, entity_id=customer, role="member", status="active", staff_id=staff.id),
        WorkspaceStaff(workspace_id=target, user_id=m.id, role="viewer", added_by=r.id, status="active"),
    ])
    flow = WorkflowDefinition(entity_id=customer, workspace_id=target, name="Trial flow", created_by=r.id)
    db_session.add(flow)
    await db_session.commit()
    assert not await user_can_access_resource(
        db_session,
        descriptor=ResourceDescriptor.from_row(flow, ResourceType.WORKFLOW),
        entity_id=customer,
        user_id=m.id,
    )
