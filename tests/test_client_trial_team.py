"""The provider team opens a customer trial from its own company, each through their own grant."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from test_client_workspace_delivery import register
from test_collaborator_boundary import BROWSER
from test_workspace_collaboration import accepted, derived
from packages.core.models.browser_session import BrowserSession
from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace, WorkspaceActivity, WorkspaceStaff
from packages.core.models.workspace_collaboration import WorkspaceCollaboration
from packages.core.services.auth_service import create_access_token, decode_token
from packages.core.services.user_email_proof import mark_email_verified

JOINED = "Provider team member joined the trial"


async def teammate(client, db, provider, source, name, *, company_role="member", seat="owner"):
    """A person working in the provider's company, optionally seated on the source Workspace.

    Signed in to the provider company with an ordinary token, as after a
    switch there; their persisted company is the provider's.
    """
    _, person = await register(client, db, name)
    db.add(UserMembership(user_id=person.id, entity_id=provider.entity_id, role=company_role, status="active"))
    if seat:
        db.add(WorkspaceStaff(workspace_id=source, user_id=person.id, role=seat, added_by=provider.id, status="active"))
    person.entity_id, person.role = provider.entity_id, company_role
    await db.commit()
    token = create_access_token(person.id, provider.entity_id, company_role, token_version=person.token_version)
    return {"Authorization": f"Bearer {token}"}, person


def bearer(response):
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def mint(client, headers, source, delivery_id):
    return await client.post(f"/api/v1/workspaces/{source}/client-trials/{delivery_id}/session", headers=headers)


async def grants_of(db, delivery_id, user_id=None):
    query = select(WorkspaceCollaboration).where(WorkspaceCollaboration.delivery_id == delivery_id)
    if user_id:
        query = query.where(WorkspaceCollaboration.user_id == user_id)
    return (
        await db.scalars(
            query.order_by(WorkspaceCollaboration.created_at).execution_options(populate_existing=True)
        )
    ).all()


async def decide(client, rh, target, provider):
    for payload in ({"action": "request_acceptance"}, {"action": "accept", "provider": provider}):
        response = await client.post(f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json=payload)
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_every_source_manager_opens_the_trial_and_no_one_else(client, db_session):
    # Catches: the mint admitting a teammate without manage on the source, a
    # third company, the customer, a scoped token or another company's
    # delivery; a teammate with manage (seat or company owner) refused; the
    # minted token not reaching the customer Workspace; the teammate's grant
    # not carrying the customer's authorization or not being audited.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    seated, s = await teammate(client, db_session, p, source, "seated")
    owner, a = await teammate(client, db_session, p, source, "company-owner", company_role="owner", seat=None)
    member, m = await teammate(client, db_session, p, source, "seatless", seat=None)
    outsider, _ = await register(client, db_session, "third-company")
    [anchor] = await grants_of(db_session, delivery["id"])

    for headers in (ph, seated, owner):
        opened = await mint(client, headers, source, delivery["id"])
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert {k: body[k] for k in ("entity_id", "workspace_id", "role", "phase", "access", "customer_name")} == {
            "entity_id": customer, "workspace_id": target, "role": "collaborator", "phase": "trial",
            "access": "trial", "customer_name": "recipient",
        }
        claims = decode_token(body["access_token"])
        assert (claims["typ"], claims["cs"]) == (
            "collaborator_session", {"workspace_id": target, "kind": "client_trial", "ref": delivery["id"]}
        )
        assert (await client.get(f"/api/v1/workspaces/{target}", headers=bearer(opened))).status_code == 200
    teammates = await grants_of(db_session, delivery["id"], s.id) + await grants_of(db_session, delivery["id"], a.id)
    assert [(g.role, g.status, g.email, g.requested_by, g.share_configuration, g.snapshot) for g in teammates] == [
        ("provider_trial", "active", s.email, None, False, {}),
        ("provider_trial", "active", a.email, None, False, {}),
    ]
    for grant in teammates:
        assert (grant.authorized_by, grant.authority_fingerprint, grant.expires_at) == (
            anchor.authorized_by, anchor.authority_fingerprint, anchor.expires_at
        )
        memberships, seats = await derived(db_session, grant.user_id, customer)
        assert [(x.role, x.status) for x in memberships] == [("collaborator", "active")]
        assert [(x.role, x.collaboration_id) for x in seats] == [("owner", grant.id)]
    joined = (
        await db_session.scalars(
            select(WorkspaceActivity.user_id).where(
                WorkspaceActivity.workspace_id == target, WorkspaceActivity.summary == JOINED
            )
        )
    ).all()
    assert sorted(joined) == sorted([s.id, a.id])

    refused = await mint(client, member, source, delivery["id"])
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "client_trial_manager_required"
    assert (await mint(client, outsider, source, delivery["id"])).status_code == 404
    assert (await mint(client, rh, source, delivery["id"])).status_code == 404
    scoped = bearer(await mint(client, seated, source, delivery["id"]))
    assert (await mint(client, scoped, source, delivery["id"])).status_code == 403
    assert (await client.get(f"/api/v1/workspaces/{source}/client-trials", headers=scoped)).status_code == 403
    assert len(await grants_of(db_session, delivery["id"])) == 3


@pytest.mark.asyncio
async def test_minting_never_moves_the_persisted_company_and_is_idempotent(client, db_session):
    # Catches: a mint switching User.entity_id/role, the browser session's
    # company or the primary membership; a repeat or concurrent mint creating a
    # second grant (or a second audit row); the database allowing one.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer, home = (await db_session.get(Workspace, target)).entity_id, p.entity_id
    signed = await client.post(
        "/api/v1/auth/login",
        headers=BROWSER,
        json={"email": p.email, "password": "DeliveryFixture123!", "remember_me": True},
    )
    assert signed.status_code == 200, signed.text
    seated, s = await teammate(client, db_session, p, source, "seated")

    people = [p.id, s.id]

    async def pointers():
        db_session.expire_all()
        users = await db_session.execute(select(User.id, User.entity_id, User.role).where(User.id.in_(people)))
        sessions = await db_session.scalars(select(BrowserSession.entity_id).where(BrowserSession.user_id == people[0]))
        primary = await db_session.scalars(
            select(UserMembership.entity_id).where(
                UserMembership.user_id.in_(people), UserMembership.is_primary.is_(True)
            )
        )
        return sorted(users.all()), sorted(sessions.all()), sorted(primary.all())

    before = await pointers()
    assert before[1] == [home]
    for _ in range(2):
        assert (await mint(client, ph, source, delivery["id"])).status_code == 200
    results = await asyncio.gather(*[mint(client, seated, source, delivery["id"]) for _ in range(2)])
    assert [x.status_code for x in results] == [200, 200], [x.text for x in results]
    assert (await mint(client, seated, source, delivery["id"])).status_code == 200
    assert await pointers() == before
    assert [g.user_id for g in await grants_of(db_session, delivery["id"])] == people
    assert await db_session.scalar(
        select(func.count()).select_from(WorkspaceActivity).where(WorkspaceActivity.summary == JOINED)
    ) == 1
    [grant] = await grants_of(db_session, delivery["id"], people[1])
    db_session.add(WorkspaceCollaboration(
        entity_id=customer, workspace_id=target, delivery_id=delivery["id"], email=grant.email, role="maintainer",
        status="active", user_id=people[1], authorized_by=grant.authorized_by, snapshot={},
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_the_team_role_follows_the_phase_and_the_customers_decision(client, db_session):
    # Catches: awaiting acceptance closing the trial; a kept provider's
    # teammate not getting maintainer access (or getting trial access);
    # a lapsed own grant blocking a re-mint.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    requested = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "request_acceptance"}
    )
    assert requested.json()["phase"] == "awaiting_acceptance"
    awaiting = await mint(client, seated, source, delivery["id"])
    assert (awaiting.status_code, awaiting.json()["access"]) == (200, "trial"), awaiting.text
    kept = await client.post(
        f"/api/v1/workspaces/{target}/delivery-phase", headers=rh, json={"action": "accept", "provider": "keep"}
    )
    assert kept.status_code == 200, kept.text
    # A teammate's own grant that lapsed follows the team's on the next mint.
    [mine] = await grants_of(db_session, delivery["id"], s.id)
    mine.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    for headers in (ph, seated):
        opened = await mint(client, headers, source, delivery["id"])
        assert (opened.status_code, opened.json()["phase"], opened.json()["access"]) == (
            200, "delivered", "maintainer"
        ), opened.text
    [mine] = await grants_of(db_session, delivery["id"], s.id)
    assert (mine.role, mine.status, mine.expires_at) == ("maintainer", "active", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["removed", "direct_delivery"])
async def test_a_delivery_without_live_provider_access_does_not_open(client, db_session, case):
    # Catches: a removed provider team, or a direct delivery that never gave
    # the provider access, still opening (and being granted access by the mint).
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(
        client, db_session, phase="trial" if case == "removed" else "delivered"
    )
    if case == "removed":
        await decide(client, rh, target, "remove")
    refused = await mint(client, ph, source, delivery["id"])
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "client_trial_unavailable"
    assert [g.status for g in await grants_of(db_session, delivery["id"])] == (["revoked"] if case == "removed" else [])


@pytest.mark.asyncio
async def test_losing_the_source_seat_ends_the_session_and_the_next_mint(client, db_session):
    # Catches: a teammate's minted token outliving their manage right on the
    # source Workspace, or the mint re-admitting them.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    scoped = bearer(await mint(client, seated, source, delivery["id"]))
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=scoped)).status_code == 200
    seat = await db_session.scalar(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == source, WorkspaceStaff.user_id == s.id)
    )
    await db_session.delete(seat)
    await db_session.commit()
    ended = await client.get(f"/api/v1/workspaces/{target}", headers=scoped)
    assert ended.status_code == 401 and ended.json()["detail"]["code"] == "collaboration_ended"
    assert (await mint(client, seated, source, delivery["id"])).status_code == 403


@pytest.mark.asyncio
async def test_managers_list_the_trials_they_can_open(client, db_session):
    # Catches: the list hiding a live trial from a teammate who has not opened
    # it yet, showing it to a non-manager, or still offering a delivery whose
    # provider team the customer removed.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    member, m = await teammate(client, db_session, p, source, "seatless", seat=None)
    path = f"/api/v1/workspaces/{source}/client-trials"
    listed = await client.get(path, headers=seated)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()
    customer = (await db_session.get(Workspace, target)).entity_id
    assert {k: v for k, v in item.items() if k != "accepted_at"} == {
        "delivery_id": delivery["id"], "client_id": cid, "client_name": "Customer", "customer_name": "recipient",
        "title": "Private marketing", "phase": "trial", "access": "trial", "target_workspace_id": target,
        "target_entity_id": customer,
    }
    assert item["accepted_at"] is not None
    refused = await client.get(path, headers=member)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "client_trial_manager_required"
    await decide(client, rh, target, "remove")
    assert (await client.get(path, headers=ph)).json() == []


async def delivery_grant(db, target, delivery_id, person, issuer, *, status="active", role="maintainer"):
    """A grant of the delivery made outside today's flows (an older release, or a race)."""
    from packages.core.services.external_collaboration_access import sync_external_access
    from packages.core.services.workspace_collaboration import issuer_fingerprint

    ws = await db.get(Workspace, target)
    row = WorkspaceCollaboration(
        entity_id=ws.entity_id, workspace_id=target, delivery_id=delivery_id, email=person.email.casefold(),
        role=role, status=status, authorized_by=issuer.id, user_id=person.id if status == "active" else None,
        authority_fingerprint=await issuer_fingerprint(db, ws, issuer.id), snapshot={},
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(row)
    await db.flush()
    await sync_external_access(db, row)
    await db.commit()
    return row


async def maintenance_request(client, headers, cid, delivery_id):
    return await client.post(f"/api/v1/clients/{cid}/deliveries/{delivery_id}/maintenance/request", headers=headers)


@pytest.mark.asyncio
async def test_during_the_trial_no_grant_of_the_team_can_be_requested_or_removed(client, db_session):
    # Catches: a maintenance request during the trial (team access is
    # automatic then) creating a maintainer grant the customer can use to
    # remove the whole team mid-trial; the customer revoking or updating any
    # delivery grant before acceptance; a trial-time maintainer invitation
    # being accepted.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    mark_email_verified(s, s.email)
    await db_session.commit()
    refused = await maintenance_request(client, seated, cid, delivery["id"])
    assert refused.status_code == 409, refused.text
    assert await grants_of(db_session, delivery["id"], s.id) == []

    legacy = await delivery_grant(db_session, target, delivery["id"], s, r)
    base = f"/api/v1/workspaces/{target}/collaboration/{legacy.id}"
    assert (await client.post(f"{base}/revoke", headers=rh)).status_code == 409
    body = {"email": s.email, "role": "maintainer", "days": 30}
    assert (await client.post(f"{base}/update", headers=rh, json=body)).status_code == 409
    assert [g.status for g in await grants_of(db_session, delivery["id"])] == ["active", "active"]

    _, t = await teammate(client, db_session, p, source, "invitee")
    mark_email_verified(t, t.email)
    await db_session.commit()
    pending = await delivery_grant(db_session, target, delivery["id"], t, r, status="invited")
    invitee = {"Authorization": f"Bearer {create_access_token(t.id, p.entity_id, 'member', token_version=t.token_version)}"}
    accepted_pending = await client.post(f"/api/v1/workspace-collaborations/{pending.id}/accept", headers=invitee)
    assert accepted_pending.status_code == 409, accepted_pending.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["keep", "remove"])
async def test_acceptance_decides_on_every_grant_of_the_team(client, db_session, provider):
    # Catches: remove leaving a trial-time maintainer grant live or a pending
    # invitation that could restore the team; keep leaving a trial-time
    # maintainer checked against its old issuer or expiry.
    from packages.core.services.workspace_collaboration import issuer_fingerprint

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    _, s = await teammate(client, db_session, p, source, "seated")
    _, t = await teammate(client, db_session, p, source, "invitee")
    await delivery_grant(db_session, target, delivery["id"], s, r)
    await delivery_grant(db_session, target, delivery["id"], t, r, status="invited")
    await decide(client, rh, target, provider)
    team = await grants_of(db_session, delivery["id"])
    if provider == "remove":
        assert [g.status for g in team] == ["revoked"] * 3
        for user in (p, s):
            memberships, seats = await derived(db_session, user.id, customer)
            assert [m.status for m in memberships] == ["inactive"] and seats == []
    else:
        fingerprint = await issuer_fingerprint(db_session, await db_session.get(Workspace, target), r.id)
        assert [(g.role, g.status, g.authorized_by, g.authority_fingerprint, g.expires_at) for g in team[:2]] == [
            ("maintainer", "active", r.id, fingerprint, None)
        ] * 2
        assert team[2].status == "invited"


@pytest.mark.asyncio
async def test_a_removed_team_comes_back_only_through_a_new_authorization(client, db_session):
    # Catches: an invitation authorized before the customer removed the team
    # being accepted afterwards (restoring the whole team); a re-authorization
    # after the removal not being accepted.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    mark_email_verified(s, s.email)
    await db_session.commit()
    await decide(client, rh, target, "keep")
    requested = await maintenance_request(client, seated, cid, delivery["id"])
    assert requested.status_code == 200, requested.text
    gid = requested.json()["id"]
    authorize = {"email": s.email, "role": "maintainer", "days": 7}
    path = f"/api/v1/workspaces/{target}/collaboration"
    assert (await client.post(f"{path}/{gid}/authorize", headers=rh, json=authorize)).status_code == 200
    [kept] = await grants_of(db_session, delivery["id"], p.id)
    assert (await client.post(f"{path}/{kept.id}/revoke", headers=rh)).status_code == 200
    assert (await db_session.get(WorkspaceCollaboration, gid, populate_existing=True)).status == "revoked"
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=seated)).status_code == 410

    # An invitation that escaped the removal is still not accepted.
    stale = await delivery_grant(db_session, target, delivery["id"], s, r, status="invited")
    stale_accept = await client.post(f"/api/v1/workspace-collaborations/{stale.id}/accept", headers=seated)
    assert stale_accept.status_code == 409, stale_accept.text
    stale.status = "revoked"
    await db_session.commit()

    again = await maintenance_request(client, seated, cid, delivery["id"])
    assert again.status_code == 200, again.text
    assert (await client.post(f"{path}/{again.json()['id']}/authorize", headers=rh, json=authorize)).status_code == 200
    restored = await client.post(f"/api/v1/workspace-collaborations/{again.json()['id']}/accept", headers=seated)
    assert restored.status_code == 200, restored.text
    row = await db_session.get(ClientWorkspaceDelivery, delivery["id"], populate_existing=True)
    assert row.acceptance["provider"] == "keep"
    reopened = await mint(client, ph, source, delivery["id"])
    assert (reopened.status_code, reopened.json()["access"]) == (200, "maintainer"), reopened.text


@pytest.mark.asyncio
async def test_customer_invited_maintainer_opens_the_workspace_through_their_grant(client, db_session):
    # Catches: the per-grant mint admitting someone else's grant, an
    # invitation that is not yet accepted, a tester grant, or a revoked one.
    from test_workspace_collaboration import invited

    ph, p, rh, r, target, gid = await invited(client, db_session, accept=False)
    path = f"/api/v1/workspace-collaborations/{gid}/session"
    assert (await client.post(path, headers=ph)).status_code == 403
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    outsider, _ = await register(client, db_session, "outsider")
    assert (await client.post(path, headers=outsider)).status_code == 404
    opened = await client.post(path, headers=ph)
    assert opened.status_code == 200, opened.text
    assert decode_token(opened.json()["access_token"])["cs"] == {"workspace_id": target, "kind": "grant", "ref": gid}
    assert (await client.get(f"/api/v1/workspaces/{target}", headers=bearer(opened))).status_code == 200
    assert (await client.post(path, headers=bearer(opened))).status_code == 403
    assert (await client.post(f"/api/v1/workspaces/{target}/collaboration/{gid}/revoke", headers=rh)).status_code == 200
    assert (await client.post(path, headers=ph)).status_code == 410

    tester = await client.post(
        f"/api/v1/workspaces/{target}/collaboration", headers=rh, json={"email": p.email, "role": "tester"}
    )
    assert (await client.post(f"/api/v1/workspace-collaborations/{tester.json()['id']}/accept", headers=ph)).status_code == 200
    assert (await client.post(f"/api/v1/workspace-collaborations/{tester.json()['id']}/session", headers=ph)).status_code == 409


async def live_for(db, users, customer):
    from packages.core.services.external_collaboration_access import collaborator_workspace_ids

    return [await collaborator_workspace_ids(db, user_id=u.id, entity_id=customer) for u in users]


@pytest.mark.asyncio
async def test_the_customer_decides_on_the_provider_team_as_a_whole(client, db_session):
    # Catches: the customer removing one teammate mid-trial; keep converting
    # only one of the team's grants (the rest left as trial grants, or
    # checked against the provider-side issuer); a later teammate not joining
    # as maintainer; an update or revoke of one maintainer touching only that
    # grant; a removed team still opening the Workspace; re-authorization not
    # reopening it for the whole team; the customer's view missing the team.
    from packages.core.services.workspace_collaboration import issuer_fingerprint

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    ws = await db_session.get(Workspace, target)
    customer = ws.entity_id
    seated, s = await teammate(client, db_session, p, source, "seated")
    later, t = await teammate(client, db_session, p, source, "later")
    assert (await mint(client, seated, source, delivery["id"])).status_code == 200
    [theirs] = await grants_of(db_session, delivery["id"], s.id)
    base = f"/api/v1/workspaces/{target}/collaboration/{theirs.id}"
    assert (await client.post(f"{base}/revoke", headers=rh)).status_code == 409
    body = {"email": s.email, "role": "maintainer", "days": 30}
    assert (await client.post(f"{base}/update", headers=rh, json=body)).status_code == 409
    state = (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()
    assert {k: v for k, v in state["provider_team"].items() if k != "members"} == {
        "delivery_id": delivery["id"], "access": "trial"
    }
    assert [m["email"] for m in state["provider_team"]["members"]] == [p.email, s.email]
    assert all(g["team"] for g in state["grants"])

    await decide(client, rh, target, "keep")
    fingerprint = await issuer_fingerprint(db_session, ws, r.id)
    team = await grants_of(db_session, delivery["id"])
    assert [(g.role, g.status, g.authorized_by, g.authority_fingerprint, g.expires_at) for g in team] == [
        ("maintainer", "active", r.id, fingerprint, None)
    ] * 2
    assert await live_for(db_session, (p, s), customer) == [{target}] * 2
    joined = await mint(client, later, source, delivery["id"])
    assert (joined.status_code, joined.json()["access"]) == (200, "maintainer"), joined.text
    assert (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()["provider_team"][
        "access"
    ] == "maintainer"

    # One update re-scopes every member; one revoke removes the whole team.
    updated = await client.post(f"{base}/update", headers=rh, json=body)
    assert updated.status_code == 200, updated.text
    team = await grants_of(db_session, delivery["id"])
    assert len({g.expires_at for g in team}) == 1 and team[0].expires_at is not None and len(team) == 3
    assert (await client.post(f"{base}/revoke", headers=rh)).status_code == 200
    assert [g.status for g in await grants_of(db_session, delivery["id"])] == ["revoked"] * 3
    assert await live_for(db_session, (p, s, t), customer) == [frozenset()] * 3
    row = await db_session.get(ClientWorkspaceDelivery, delivery["id"])
    await db_session.refresh(row)
    assert row.acceptance["provider"] == "removed" and row.acceptance["removed_by"] == r.id
    for headers in (ph, seated):
        refused = await mint(client, headers, source, delivery["id"])
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "client_trial_unavailable"

    # The customer authorizes the provider again: the team may open it again.
    requested = await client.post(f"/api/v1/clients/{cid}/deliveries/{delivery['id']}/maintenance/request", headers=ph)
    assert requested.status_code == 200, requested.text
    gid = requested.json()["id"]
    authorized = await client.post(
        f"/api/v1/workspaces/{target}/collaboration/{gid}/authorize",
        headers=rh,
        json={"email": p.email, "role": "maintainer", "days": 7},
    )
    assert authorized.status_code == 200, authorized.text
    assert (await client.post(f"/api/v1/workspace-collaborations/{gid}/accept", headers=ph)).status_code == 200
    await db_session.refresh(row)
    assert row.acceptance["provider"] == "keep" and row.acceptance["restored_at"]
    reopened = await mint(client, seated, source, delivery["id"])
    assert (reopened.status_code, reopened.json()["access"]) == (200, "maintainer"), reopened.text


@pytest.mark.asyncio
async def test_removing_the_provider_at_acceptance_removes_every_teammate(client, db_session):
    # Catches: "remove" at acceptance revoking only one grant of the team.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    seated, s = await teammate(client, db_session, p, source, "seated")
    assert (await mint(client, seated, source, delivery["id"])).status_code == 200
    await decide(client, rh, target, "remove")
    assert [g.status for g in await grants_of(db_session, delivery["id"])] == ["revoked", "revoked"]
    for user in (p, s):
        memberships, seats = await derived(db_session, user.id, customer)
        assert [m.status for m in memberships] == ["inactive"] and seats == []
    assert (await mint(client, seated, source, delivery["id"])).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["seat_id", "user_id"])
async def test_a_teammate_removed_from_the_source_leaves_the_team(client, db_session, key):
    # Catches: the provider owner unable to remove a teammate's user-only
    # source seat; the customer still shown that teammate in the provider
    # team; keep promoting their grant to maintainer instead of revoking it;
    # the customer removing a grant-derived seat behind the grant's back.
    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    customer = (await db_session.get(Workspace, target)).entity_id
    seated, s = await teammate(client, db_session, p, source, "seated")
    assert (await mint(client, seated, source, delivery["id"])).status_code == 200
    seat = await db_session.scalar(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == source, WorkspaceStaff.user_id == s.id)
    )
    assert seat.staff_id is None
    removed = await client.delete(
        f"/api/v1/workspaces/{source}/staff/{seat.id if key == 'seat_id' else s.id}", headers=ph
    )
    assert removed.status_code == 204, removed.text
    state = (await client.get(f"/api/v1/workspaces/{target}/collaboration", headers=rh)).json()
    assert [m["user_id"] for m in state["provider_team"]["members"]] == [p.id]
    derived_seat = await db_session.scalar(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id == target, WorkspaceStaff.user_id == s.id)
    )
    assert (await client.delete(f"/api/v1/workspaces/{target}/staff/{derived_seat.id}", headers=rh)).status_code == 409

    await decide(client, rh, target, "keep")
    assert [(g.user_id, g.role, g.status) for g in await grants_of(db_session, delivery["id"])] == [
        (p.id, "maintainer", "active"), (s.id, "provider_trial", "revoked")
    ]
    memberships, seats = await derived(db_session, s.id, customer)
    assert [m.status for m in memberships] == ["inactive"] and seats == []


@pytest.mark.asyncio
async def test_a_real_customer_member_is_not_given_a_collaborator_session(client, db_session):
    # Catches: the per-grant mint handing a collaborator token to someone who
    # belongs to the customer company (who works there as a member).
    from test_workspace_collaboration import invited

    ph, p, rh, r, target, gid = await invited(client, db_session)
    membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == p.id, UserMembership.entity_id == r.entity_id)
    )
    membership.role = "member"
    await db_session.commit()
    refused = await client.post(f"/api/v1/workspace-collaborations/{gid}/session", headers=ph)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "client_trial_customer_member"


@pytest.mark.asyncio
async def test_removing_the_provider_team_revokes_every_grant_of_the_delivery(client, db_session):
    # Catches: a removal leaving one teammate's grant, seat or membership live,
    # not recording the removal (so the team could open it again) or not
    # auditing it.
    from packages.core.services.client_trial_access import revoke_provider_team
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    ph, p, rh, r, cid, source, target, delivery, token = await accepted(client, db_session)
    seated, s = await teammate(client, db_session, p, source, "seated")
    assert (await mint(client, seated, source, delivery["id"])).status_code == 200
    customer = (await db_session.get(Workspace, target)).entity_id
    ws = await lock_workspace_access_boundary(db_session, workspace_id=target, entity_id=customer)
    row = await db_session.scalar(
        select(ClientWorkspaceDelivery).where(ClientWorkspaceDelivery.id == delivery["id"]).with_for_update()
    )
    revoked = await revoke_provider_team(db_session, ws, row, r)
    await db_session.commit()
    assert sorted(g.user_id for g in revoked) == sorted([p.id, s.id])
    assert [g.status for g in await grants_of(db_session, delivery["id"])] == ["revoked", "revoked"]
    for user_id in (p.id, s.id):
        memberships, seats = await derived(db_session, user_id, customer)
        assert [m.status for m in memberships] == ["inactive"] and seats == []
    await db_session.refresh(row)
    assert (row.acceptance["provider"], row.acceptance["removed_by"]) == ("removed", r.id)
    assert await db_session.scalar(
        select(func.count()).select_from(WorkspaceActivity).where(WorkspaceActivity.summary == "Provider team removed")
    ) == 1
    assert (await mint(client, seated, source, delivery["id"])).status_code == 409


