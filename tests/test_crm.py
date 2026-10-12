"""B2B CRM isolation, ordinary-member access and durable lifecycle behavior."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from packages.core.models.crm import ClientActivity, ClientFollowup, ClientOpportunity
from packages.core.models.staff import Staff
from packages.core.models.workspace import Workspace
from test_client_workspace_delivery import prepared, register, restrict_company_role


async def customer(client, headers, **extra):
    response = await client.post(
        "/api/v1/crm/clients",
        headers=headers,
        json={"name": "Acme", "industry": "Manufacturing", "tags": ["B2B", " B2B "], **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_member_crm_scope_owner_and_pagination(client, db_session):
    headers, user = await register(client, db_session, "sales")
    other_headers, other = await register(client, db_session, "foreign")
    before_staff = await db_session.scalar(
        select(func.count()).select_from(Staff).where(Staff.entity_id == user.entity_id)
    )
    await restrict_company_role(db_session, user, "member")
    row = await customer(client, headers, owner_id=user.id, lifecycle="prospect")
    assert row["metadata"]["crm"]["tags"] == ["B2B"]
    found = await client.get(
        "/api/v1/crm/clients",
        headers=headers,
        params={"search": "Acme", "tag": "B2B", "lifecycle": "prospect", "limit": 1},
    )
    assert found.json()["total"] == 1
    assert (await client.get("/api/v1/crm/clients", headers=headers, params={"offset": 1})).json()["items"] == []
    assert (await client.get("/api/v1/crm/clients", headers=other_headers)).json()["total"] == 0
    assert (await client.get(f"/api/v1/clients/{row['id']}/activities", headers=other_headers)).status_code == 404
    assert (
        await client.post("/api/v1/crm/clients", headers=headers, json={"name": "Bad owner", "owner_id": other.id})
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/crm/clients", headers=headers, json={"name": "Bad scope", "entity_id": other.entity_id}
        )
    ).status_code == 422
    await restrict_company_role(db_session, user, "viewer")
    assert (
        await client.post(
            f"/api/v1/clients/{row['id']}/activities", headers=headers, json={"subject": "Call logged", "kind": "call"}
        )
    ).status_code == 201
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.entity_id == user.entity_id)
        )
    ) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(Staff).where(Staff.entity_id == user.entity_id))
    ) == before_staff  # CRM never creates Staff


@pytest.mark.asyncio
async def test_pipeline_currency_stage_history_and_soft_delete(client, db_session):
    headers, user = await register(client, db_session, "pipeline")
    row = await customer(client, headers)
    cid = row["id"]
    base = f"/api/v1/clients/{cid}"
    data = {"title": "Annual retainer", "amount": "1234.56", "currency": "USD", "probability": 50, "owner_id": user.id}
    deal = await client.post(base + "/opportunities", headers=headers, json=data)
    assert deal.status_code == 201, deal.text
    assert deal.json()["amount"] == "1234.56"
    await client.post(
        base + "/opportunities",
        headers=headers,
        json={**data, "title": "China project", "currency": "CNY", "amount": "2000.00", "probability": 25},
    )
    overview = (await client.get("/api/v1/crm/overview", headers=headers)).json()
    amounts = {d["currency"]: (d["amount"], d["weighted"]) for d in overview["pipeline"]}
    assert float(amounts["USD"][1]) == 617.28
    assert float(amounts["CNY"][1]) == 500
    for stage, probability in [("won", 100), ("lost", 0)]:
        edited = await client.put(
            base + "/opportunities/" + deal.json()["id"],
            headers=headers,
            json={**data, "stage": stage, "loss_reason": "Budget"},
        )
        assert edited.json()["probability"] == probability
        assert edited.json()["loss_reason"] == ("Budget" if stage == "lost" else "")
    history = (await client.get(base + "/activities", headers=headers)).json()
    assert history["total"] == 4
    audit_id = history["items"][0]["id"]
    assert (await client.delete(base + "/activities/" + audit_id, headers=headers)).status_code == 409
    assert (
        await client.put(base + "/activities/" + audit_id, headers=headers, json={"subject": "Changed"})
    ).status_code == 409
    assert all(a["kind"] == "deal" for a in history["items"])
    pipeline = (await client.get("/api/v1/crm/opportunities", headers=headers, params={"stage": "lost"})).json()
    assert pipeline["total"] == 1 and pipeline["items"][0]["client_name"] == "Acme"
    second = await customer(client, headers, name="Other client")
    assert (
        await client.put(
            f"/api/v1/clients/{second['id']}/opportunities/{deal.json()['id']}", headers=headers, json=data
        )
    ).status_code == 404
    assert (await client.delete(base + "/opportunities/" + deal.json()["id"], headers=headers)).status_code == 204
    assert (await client.get(base + "/opportunities", headers=headers)).json()["total"] == 1


@pytest.mark.asyncio
async def test_followup_complete_reopen_archive_and_history(client, db_session):
    headers, user = await register(client, db_session, "followups")
    row = await customer(client, headers)
    base = f"/api/v1/clients/{row['id']}"
    data = {
        "title": "Approve proposal",
        "due_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "owner_id": user.id,
        "priority": "high",
    }
    created = await client.post(base + "/followups", headers=headers, json=data)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    assert (await client.get("/api/v1/crm/overview", headers=headers)).json()["overdue"] == 1
    assert (await client.get("/api/v1/crm/followups", headers=headers, params={"overdue": True})).json()["total"] == 1
    completed = await client.put(base + "/followups/" + tid, headers=headers, json={**data, "status": "done"})
    again = await client.put(base + "/followups/" + tid, headers=headers, json={**data, "status": "done"})
    assert completed.json()["completed_at"] == again.json()["completed_at"]
    assert (await client.get("/api/v1/crm/overview", headers=headers)).json()["overdue"] == 0
    reopened = await client.put(base + "/followups/" + tid, headers=headers, json={**data, "status": "open"})
    assert reopened.json()["completed_at"] is None
    assert (await client.put(base, headers=headers, json={"status": "inactive"})).status_code == 200
    assert (await client.get("/api/v1/crm/overview", headers=headers)).json()["overdue"] == 0
    assert (await client.get(base + "/followups", headers=headers)).json()["total"] == 1
    assert (await client.post(base + "/followups", headers=headers, json=data)).status_code == 409
    assert (await client.get("/api/v1/crm/followups", headers=headers)).json()["total"] == 0
    assert (
        await client.put(base, headers=headers, json={"status": "active", "metadata": {"crm_invite_version": 0}})
    ).status_code == 200
    assert (await client.get("/api/v1/crm/overview", headers=headers)).json()["overdue"] == 1


@pytest.mark.asyncio
async def test_input_validation_and_nonexistent_owner(client, db_session):
    headers, _ = await register(client, db_session, "validation")
    row = await customer(client, headers)
    base = f"/api/v1/clients/{row['id']}"
    for data in [
        {"title": " "},
        {"title": "Invalid", "amount": "-1"},
        {"title": "Invalid", "amount": "NaN"},
        {"title": "Invalid", "currency": "usd"},
        {"title": "Invalid", "probability": 101},
        {"title": "Invalid", "owner_id": "missing"},
    ]:
        assert (await client.post(base + "/opportunities", headers=headers, json=data)).status_code == 422
    assert (
        await client.post(
            base + "/followups", headers=headers, json={"title": "Naive", "due_at": "2026-10-01T12:00:00"}
        )
    ).status_code == 422
    assert (await client.get("/api/v1/crm/clients", headers=headers, params={"status": "unknown"})).status_code == 422
    assert (await client.get("/api/v1/crm/opportunities", headers=headers, params={"limit": 101})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", ["membership", "user", "deleted"])
async def test_historical_owner_does_not_block_unrelated_crm_updates(client, db_session, inactive):
    from packages.core.models.user import UserMembership

    headers, user = await register(client, db_session, "manager")
    _, former_owner = await register(client, db_session, "former_owner")
    membership = UserMembership(
        user_id=former_owner.id, entity_id=user.entity_id, role="member", status="active"
    )
    db_session.add(membership)
    await db_session.commit()
    row = await customer(client, headers, owner_id=former_owner.id)
    cid = row["id"]
    base = f"/api/v1/clients/{cid}"
    deal_data = {"title": "Existing deal", "owner_id": former_owner.id}
    followup_data = {
        "title": "Existing follow-up", "owner_id": former_owner.id,
        "due_at": datetime.now(timezone.utc).isoformat(),
    }
    deal = await client.post(base + "/opportunities", headers=headers, json=deal_data)
    followup = await client.post(base + "/followups", headers=headers, json=followup_data)
    assert deal.status_code == followup.status_code == 201
    if inactive == "membership":
        membership.status = "inactive"
    elif inactive == "user":
        former_owner.status = "inactive"
    else:
        former_owner.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    profile_data = {"name": row["name"], "owner_id": former_owner.id}
    for status in ("inactive", "active"):
        saved = await client.put(base + "/crm", headers=headers,
                                 json={**profile_data, "status": status})
        assert saved.status_code == 200, saved.text
        assert saved.json()["metadata"]["crm"]["owner_id"] == former_owner.id
    completed = await client.put(base + "/followups/" + followup.json()["id"], headers=headers,
                                 json={**followup_data, "status": "done"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["completed_at"]
    won = await client.put(base + "/opportunities/" + deal.json()["id"], headers=headers,
                           json={**deal_data, "stage": "won"})
    assert won.status_code == 200, won.text

    assert (await client.post("/api/v1/crm/clients", headers=headers,
                              json=profile_data)).status_code == 422
    assert (await client.post(base + "/followups", headers=headers,
                              json=followup_data)).status_code == 422
    assert (await client.post(base + "/opportunities", headers=headers,
                              json=deal_data)).status_code == 422
    cleared = await client.put(base + "/crm", headers=headers,
                               json={**profile_data, "owner_id": None})
    assert cleared.status_code == 200, cleared.text
    assert (await client.put(base + "/crm", headers=headers,
                             json=profile_data)).status_code == 422
    cleared = await client.put(base + "/followups/" + followup.json()["id"], headers=headers,
                               json={**followup_data, "owner_id": None})
    assert cleared.status_code == 200, cleared.text
    assert (await client.put(base + "/followups/" + followup.json()["id"], headers=headers,
                             json=followup_data)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["archive", "contact_email", "contact_delete"])
async def test_old_delivery_invitation_never_revives_after_identity_change(client, db_session, mutation):
    ph, _, rh, recipient, cid, _, receipt, token = await prepared(client, db_session)
    base = f"/api/v1/clients/{cid}"
    if mutation == "archive":
        await client.put(base, headers=ph, json={"status": "inactive"})
        assert (await client.get(base + "/deliveries", headers=ph)).json()[0]["status"] == "revoked"
        await client.put(base, headers=ph, json={"status": "active", "metadata": {"crm_invite_version": 0}})
    elif mutation == "contact_email":
        path = base + "/contacts/" + receipt["contact_id"]
        await client.put(path, headers=ph, json={"name": "Owner", "email": "other@delivery.test"})
        await client.put(path, headers=ph, json={"name": "Owner", "email": recipient.email})
    else:
        await client.delete(base + "/contacts/" + receipt["contact_id"], headers=ph)
        await client.post(base + "/contacts", headers=ph, json={"name": "Owner", "email": recipient.email})
    response = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert response.status_code == 410, response.text
    assert (await client.get(base + "/deliveries", headers=ph)).json()[0]["status"] == "revoked"


@pytest.mark.asyncio
async def test_primary_contacts_and_delivered_workspace_survive_crm_archive(client, db_session):
    ph, _, rh, recipient, cid, _, receipt, token = await prepared(client, db_session)
    base = f"/api/v1/clients/{cid}"
    contact = await client.put(
        base + "/contacts/" + receipt["contact_id"],
        headers=ph,
        json={"name": "Owner", "email": recipient.email, "job_title": "CEO", "is_primary": True},
    )
    assert contact.json()["is_primary"]
    added = await client.post(
        base + "/contacts", headers=ph, json={"name": "Buyer", "email": "buyer@delivery.test", "is_primary": True}
    )
    contacts = (await client.get(base + "/contacts", headers=ph)).json()
    assert [c["id"] for c in contacts if c["is_primary"]] == [added.json()["id"]]
    accepted = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert accepted.status_code == 200, accepted.text
    await client.delete(base + "/contacts/" + receipt["contact_id"], headers=ph)
    await client.put(base, headers=ph, json={"status": "inactive"})
    repeated = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert (
        repeated.status_code == 200 and repeated.json()["target_workspace_id"] == accepted.json()["target_workspace_id"]
    )


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_crm_migration_repeats_and_matches_models(db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from tests.migration_test_helpers import script_directory

    script = script_directory()

    def migrate(session):
        connection = session.connection()
        connection.execute(text("CREATE SCHEMA crm_migration_test"))
        connection.execute(text("SET LOCAL search_path TO crm_migration_test"))
        connection.execute(text("CREATE TABLE workspace_staff (staff_id VARCHAR(26) NOT NULL)"))
        with Operations.context(MigrationContext.configure(connection)):
            script.get_revision("20261001_02").module.upgrade()
            revision = script.get_revision("20261001_03").module
            for _ in range(2):
                revision.upgrade()
                for model in (ClientActivity, ClientOpportunity, ClientFollowup):
                    columns = {c["name"]: c for c in inspect(connection).get_columns(model.__tablename__)}
                    assert set(columns) == set(model.__table__.columns.keys())
                    assert all(columns[c.name]["nullable"] == c.nullable for c in model.__table__.columns)
                revision.downgrade()
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text("DROP SCHEMA crm_migration_test CASCADE"))

    await db_session.run_sync(migrate)


@pytest.mark.asyncio
async def test_profile_update_preserves_delivery_version_and_zero_forecast_format(client, db_session):
    headers, user = await register(client, db_session, "profile")
    row = await customer(client, headers)
    cid = row["id"]
    payload = {
        "name": "Acme Updated",
        "email": "owner@example.test",
        "lifecycle": "customer",
        "industry": "Services",
        "notes": "Approved scope",
        "owner_id": user.id,
        "tags": ["Delivery"],
        "status": "inactive",
    }
    updated = await client.put(f"/api/v1/clients/{cid}/crm", headers=headers, json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["metadata"]["crm_invite_version"] == 1
    assert updated.json()["metadata"]["crm"]["notes"] == "Approved scope"
    restored = await client.put(f"/api/v1/clients/{cid}/crm", headers=headers, json={**payload, "status": "active"})
    assert restored.json()["metadata"]["crm_invite_version"] == 1
    saved = await client.post(
        f"/api/v1/clients/{cid}/opportunities",
        headers=headers,
        json={"title": "Early discovery", "amount": "100.00", "probability": 0},
    )
    assert saved.status_code == 201
    forecast = (await client.get("/api/v1/crm/overview", headers=headers)).json()["pipeline"][0]
    assert "E" not in forecast["weighted"] and float(forecast["weighted"]) == 0
    assert (await client.get(f"/api/v1/clients/{cid}", headers=headers)).json()["metadata"]["crm"][
        "lifecycle"
    ] == "customer"
