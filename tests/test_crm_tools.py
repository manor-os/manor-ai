"""CRM tool/API equivalence, actor isolation and explicit ledger references."""

import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.crm_actions import CRM_ACTIONS, runtime_crm_action
from packages.core.models.user import UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services import crm_relationships
from test_client_workspace_delivery import register, restrict_company_role, prepared


async def run(db, **kwargs):
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as tool_db:
        return await runtime_crm_action(tool_db, **kwargs)


async def call(db, user, action, **params):
    return json.loads(await run(db, action=action, params=params, entity_id=user.entity_id, user_id=user.id))


@pytest.mark.asyncio
async def test_crm_tool_uses_same_company_profiles_and_validation_as_http(client, db_session):
    headers, user = await register(client, db_session, "tool-sales")
    await restrict_company_role(db_session, user, "viewer")
    created = await call(db_session, user, "create_client", name="Company", industry="Manufacturing", tags=["B2B"])
    assert created["ok"]
    cid = created["id"]
    read = await client.get(f"/api/v1/clients/{cid}", headers=headers)
    assert read.json()["metadata"]["crm"]["industry"] == "Manufacturing"
    edited = await call(db_session, user, "update_client", client_id=cid, notes="Discuss renewal")
    assert edited["metadata"]["crm"]["industry"] == "Manufacturing"
    assert edited["metadata"]["crm"]["tags"] == ["B2B"]
    for params in ({"name": "Bad", "owner_id": "foreign"}, {"name": "Bad", "entity_id": "other"}):
        rejected = await call(db_session, user, "create_client", **params)
        assert not rejected["ok"] and rejected["error"]["status"] == 422
    found = await call(db_session, user, "list_clients", tag="B2B", lifecycle="lead")
    assert found["total"] == 1
    assert not (await call(db_session, user, "list_clients", limit=101))["ok"]


@pytest.mark.asyncio
async def test_crm_contacts_and_record_lifecycles_visible_across_entrypoints(client, db_session):
    headers, user = await register(client, db_session, "tool-records")
    await restrict_company_role(db_session, user, "member")
    cid = (await call(db_session, user, "create_client", name="Company"))["id"]
    a = await call(
        db_session,
        user,
        "create_client_contact",
        client_id=cid,
        name="Marketing",
        email="marketing@acme.test",
        is_primary=True,
    )
    b = await call(
        db_session,
        user,
        "create_client_contact",
        client_id=cid,
        name="Finance",
        email="finance@acme.test",
        job_title="CFO",
        is_primary=True,
    )
    updated = await call(db_session, user, "update_client_contact", client_id=cid, contact_id=a["id"], phone="123")
    assert updated["name"] == "Marketing" and updated["email"] == "marketing@acme.test"
    contacts = (await client.get(f"/api/v1/clients/{cid}/contacts", headers=headers)).json()
    assert len(contacts) == 2 and next(x for x in contacts if x["is_primary"])["id"] == b["id"]
    deal = await call(
        db_session, user, "create_client_opportunity", client_id=cid, title="Retainer", amount="1234.56", probability=50
    )
    edited = await call(db_session, user, "update_client_opportunity", client_id=cid, record_id=deal["id"], stage="won")
    assert edited["probability"] == 100 and edited["amount"] == "1234.56"
    history = await call(db_session, user, "list_client_activities", client_id=cid)
    assert history["total"] == 2
    assert not (
        await call(db_session, user, "delete_client_activity", client_id=cid, record_id=history["items"][0]["id"])
    )["ok"]
    activity = await call(
        db_session, user, "create_client_activity", client_id=cid, subject="Meeting notes", kind="meeting"
    )
    assert activity["kind"] == "meeting"
    due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    followup = await call(db_session, user, "create_client_followup", client_id=cid, title="Send proposal", due_at=due)
    done = await call(
        db_session, user, "update_client_followup", client_id=cid, record_id=followup["id"], status="done"
    )
    again = await call(
        db_session, user, "update_client_followup", client_id=cid, record_id=followup["id"], status="done"
    )
    assert done["completed_at"] == again["completed_at"] and done["title"] == "Send proposal"
    assert (
        await call(db_session, user, "update_client_followup", client_id=cid, record_id=followup["id"], status="open")
    )["completed_at"] is None
    assert (await client.get("/api/v1/crm/followups", headers=headers, params={"overdue": True})).json()["total"] == 1
    await call(db_session, user, "update_client", client_id=cid, status="inactive")
    assert not (await call(db_session, user, "create_client_activity", client_id=cid, subject="Blocked"))["ok"]
    assert (await call(db_session, user, "list_client_activities", client_id=cid))["total"] == 3


@pytest.mark.asyncio
async def test_crm_actor_no_anonymous_foreign_inactive_or_external_access(client, db_session):
    _, user = await register(client, db_session, "tool-scope")
    _, other = await register(client, db_session, "tool-other")
    cid = (await call(db_session, user, "create_client", name="Private"))["id"]
    assert not (await call(db_session, other, "get_client", client_id=cid))["ok"]
    for actor in (None, other.id):
        result = json.loads(
            await run(db_session, action="get_crm_overview", params={}, entity_id=user.entity_id, user_id=actor)
        )
        assert not result["ok"]
    result = json.loads(
        await run(
            db_session,
            action="list_clients",
            params={},
            entity_id=user.entity_id,
            user_id=user.id,
            runtime_kwargs={
                "_runtime_envelope_from_context": SimpleNamespace(
                    surface="public_customer_chat", profile="external_customer_safe"
                )
            },
        )
    )
    assert not result["ok"] and result["error"]["status"] == 403
    membership = await db_session.scalar(
        select(UserMembership).where(UserMembership.user_id == user.id, UserMembership.entity_id == user.entity_id)
    )
    membership.status = "inactive"
    await db_session.commit()
    assert not (await call(db_session, user, "get_client", client_id=cid))["ok"]


@pytest.mark.asyncio
async def test_crm_tool_delivery_keeps_source_authority_and_recipient_confirmation(client, db_session):
    ph, p, rh, r, cid, source, delivery, token = await prepared(client, db_session)
    contact = (await call(db_session, p, "list_client_contacts", client_id=cid))[0]
    invite = await call(
        db_session, p, "prepare_client_delivery", client_id=cid, contact_id=contact["id"], workspace_id=source
    )
    assert invite["ok"] and invite["status"] == "pending" and invite["target_workspace_id"] is None
    assert (
        "accept_client_delivery"
        not in __import__("packages.core.ai.runtime.crm_actions", fromlist=["CRM_ACTIONS"]).CRM_ACTIONS
    )
    rotated = await call(db_session, p, "rotate_client_delivery", client_id=cid, delivery_id=invite["id"])
    assert rotated["invite_path"] != invite["invite_path"]
    old = invite["invite_path"].rsplit("/", 1)[-1]
    assert (await client.get(f"/api/v1/client-deliveries/invites/{old}", headers=rh)).status_code == 404
    await call(
        db_session, p, "update_client_contact", client_id=cid, contact_id=contact["id"], email="changed@acme.test"
    )
    rows = await call(db_session, p, "list_client_deliveries", client_id=cid)
    assert all(x["status"] == "revoked" for x in rows)
    assert not (await call(db_session, p, "rotate_client_delivery", client_id=cid, delivery_id=invite["id"]))["ok"]
    db_session.add(
        UserMembership(user_id=r.id, entity_id=p.entity_id, role="viewer", status="active", is_primary=False)
    )
    await db_session.commit()
    source_viewer = SimpleNamespace(id=r.id, entity_id=p.entity_id)
    assert not (
        await call(
            db_session,
            source_viewer,
            "prepare_client_delivery",
            client_id=cid,
            contact_id=contact["id"],
            workspace_id=source,
        )
    )["ok"]


@pytest.mark.asyncio
async def test_ledger_links_are_explicit_idempotent_scoped_and_read_fresh_crm(client, db_session, monkeypatch):
    headers, user = await register(client, db_session, "tool-ledger")
    _, other = await register(client, db_session, "tool-ledger-other")
    created = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "CRM sales", "kind": "campaign", "primary_work": "Sales"}
    )
    wid = created.json()["id"]
    ws = await db_session.get(Workspace, wid)
    ws.settings = {**ws.settings, "ledger_contracts": ["relationship_ledger"]}
    await db_session.commit()

    async def read(**kwargs):
        return {
            "rows": [
                {
                    "identity_key": "org:acme",
                    "identity_fingerprint": "opaque",
                    "payload": {"private_note": "Workspace only"},
                }
            ]
        }

    monkeypatch.setattr(crm_relationships, "read_relationship_ledger", read)
    cid = (await call(db_session, user, "create_client", name="Acme"))["id"]

    async def link(action, **extra):
        return json.loads(
            await run(
                db_session,
                action=action,
                params={"client_id": cid, **extra},
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=wid,
            )
        )

    for _ in range(2):
        assert (await link("link_client_relationship", identity_key="org:acme"))["linked"]
    # Generic metadata edits cannot mint/remove the Workspace-authorized refs.
    blocked_meta = await client.put(
        f"/api/v1/clients/{cid}", headers=headers, json={"metadata": {"relationship_links": []}}
    )
    assert blocked_meta.status_code == 200
    profile = await call(db_session, user, "get_client", client_id=cid)
    assert len(profile["metadata"]["relationship_links"]) == 1
    assert "private_note" not in json.dumps(profile)
    await call(db_session, user, "update_client", client_id=cid, name="Acme renamed")
    from packages.core.ai.runtime.crm_actions import crm_actor

    actor = await crm_actor(db_session, entity_id=user.entity_id, user_id=user.id)
    enriched = await crm_relationships.enrich_relationships(db_session, actor, wid, await read())
    assert enriched["rows"][0]["crm"]["profile"]["name"] == "Acme renamed"
    assert (await link("list_client_relationships"))["items"][0]["identity_key"] == "org:acme"
    second = (await call(db_session, user, "create_client", name="Second"))["id"]
    conflict = json.loads(
        await run(
            db_session,
            action="link_client_relationship",
            params={"client_id": second, "identity_key": "org:acme"},
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=wid,
        )
    )
    assert conflict["error"]["status"] == 409
    forbidden = json.loads(
        await run(
            db_session,
            action="link_client_relationship",
            params={"client_id": cid, "identity_key": "org:acme"},
            entity_id=other.entity_id,
            user_id=other.id,
            workspace_id=wid,
        )
    )
    assert not forbidden["ok"]
    assert not (await link("link_client_relationship", identity_key="missing"))["ok"]
    assert not (await link("link_client_relationship", identity_key="org:acme", workspace_id="model-selected"))["ok"]
    assert not (await link("unlink_client_relationship", identity_key="org:acme"))["linked"]
    assert (await link("list_client_relationships"))["items"] == []


@pytest.mark.parametrize("action", sorted(CRM_ACTIONS))
def test_crm_catalog_classifies_reads_and_reviewed_mutations(action):
    from packages.core.ai.tools.manor_tool import manor_action_manifests
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.manor_read_policy import ManorReadCapabilityFactory
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.permissions import Permission
    from packages.core.services.runtime_authorization.domain import AuthorizationSubject
    from packages.core.services.runtime_authorization.mappers import RegistryEntityPermissionMapper

    ids = {row["catalog_id"] for row in manor_action_manifests()}
    assert f"manor_action:{action}" in ids
    classified = classify_runtime_tool("manor", {"action": action})
    if ManorReadCapabilityFactory.supports(action):
        assert classified.effect is RuntimeToolEffect.READ_ONLY
        assert classified.authorization.capability_id == "crm.read"
    else:
        assert classified.effect is RuntimeToolEffect.ACTION
        high = action.startswith("delete_") or action in {
            "prepare_client_delivery",
            "rotate_client_delivery",
            "revoke_client_delivery",
        }
        assert classified.action.risk_level == ("high" if high else "medium")
    assert (
        RegistryEntityPermissionMapper().permission_for(
            AuthorizationSubject(
                action_key=classified.authorization.action_key, capability_id=classified.authorization.capability_id
            )
        )
        is Permission.CHAT_USE
    )
    # Internal team admin remains a distinct, unchanged capability.
    staff = classify_runtime_tool("manor", {"action": "get_staff"})
    assert (
        RegistryEntityPermissionMapper().permission_for(
            AuthorizationSubject(
                action_key=staff.authorization.action_key, capability_id=staff.authorization.capability_id
            )
        )
        is Permission.USERS_READ
    )


@pytest.mark.asyncio
async def test_manor_handler_dispatch_uses_injected_actor_not_model_user(client, db_session, monkeypatch):
    from packages.core.ai.tools.manor_tool import _manor_handler
    from packages.core import database
    from sqlalchemy.ext.asyncio import async_sessionmaker

    headers, user = await register(client, db_session, "tool-gateway")
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    result = json.loads(
        await _manor_handler(
            entity_id=user.entity_id,
            action="create_client",
            params={"name": "Gateway company"},
            _user_id_from_context=user.id,
        )
    )
    assert result["ok"]
    assert (await client.get(f"/api/v1/clients/{result['id']}", headers=headers)).status_code == 200
    denied = json.loads(
        await _manor_handler(
            entity_id=user.entity_id, action="list_clients", user_id=user.id, _user_id_from_context=None
        )
    )
    assert not denied["ok"] and denied["error"]["status"] == 401


@pytest.mark.asyncio
async def test_crm_runtime_authorization_matches_company_crm_without_staff_admin(client, db_session):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services.runtime_authorization import get_authorization_service
    from packages.core.services.runtime_authorization.domain import RuntimeAuthorizationRequest

    _, user = await register(client, db_session, "crm-rbac")
    await restrict_company_role(db_session, user, "viewer")
    for action in ("get_crm_overview", "create_client", "get_staff"):
        classified = classify_runtime_tool("manor", {"action": action})
        auth = classified.authorization
        request = RuntimeAuthorizationRequest.create(
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=None,
            action_key=auth.action_key,
            capability_id=auth.capability_id,
            access=auth.access,
        )
        decision = await get_authorization_service().authorize_runtime(db_session, request)
        assert decision.allowed is (action != "get_staff")


@pytest.mark.asyncio
async def test_linked_ledger_tool_enrichment_requires_current_internal_actor(client, db_session, monkeypatch):
    from packages.core.ai.tools import relationship_ledger_tools as tools
    from packages.core import database
    from sqlalchemy.ext.asyncio import async_sessionmaker

    headers, user = await register(client, db_session, "crm-ledger-read")
    _, foreign = await register(client, db_session, "crm-ledger-read-other")
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Linked sales", "kind": "campaign", "primary_work": "Sales"},
    )
    wid = created.json()["id"]
    ws = await db_session.get(Workspace, wid)
    ws.settings = {**ws.settings, "ledger_contracts": ["relationship_ledger"]}
    await db_session.commit()

    async def read(**kwargs):
        return {"rows": [{"identity_key": "org:acme", "identity_fingerprint": "opaque"}]}

    monkeypatch.setattr(crm_relationships, "read_relationship_ledger", read)
    monkeypatch.setattr(tools, "read_relationship_ledger", read)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    cid = (await call(db_session, user, "create_client", name="Acme live"))["id"]
    linked = json.loads(
        await run(
            db_session,
            action="link_client_relationship",
            params={"client_id": cid, "identity_key": "org:acme"},
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=wid,
        )
    )
    assert linked["ok"]
    args = {"workspace_id": wid, "_user_id_from_context": user.id}
    result = json.loads(await tools._read_relationship_ledger(entity_id=user.entity_id, **args))
    assert result["rows"][0]["crm"]["profile"]["name"] == "Acme live"
    anonymous = json.loads(await tools._read_relationship_ledger(entity_id=user.entity_id, workspace_id=wid))
    assert "crm" not in anonymous["rows"][0]
    forbidden = json.loads(
        await tools._read_relationship_ledger(
            entity_id=user.entity_id, workspace_id=wid, _user_id_from_context=foreign.id
        )
    )
    assert not forbidden["ok"]
