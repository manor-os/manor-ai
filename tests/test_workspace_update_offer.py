"""A sent release is offered in the customer's Workspace chat and applies in one click."""

import pytest
from sqlalchemy import select

from packages.core.models.task import Conversation, Message
from packages.core.models.workflow import WorkflowRun
from tests.test_client_delivery_updates import _delivered, _edit


async def _cards(db, workspace_id):
    rows = (await db.scalars(
        select(Message).join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.workspace_id == workspace_id,
               Message.pending_action["kind"].as_string() == "blueprint_update")
        .order_by(Message.created_at)
        .execution_options(populate_existing=True)
    )).all()
    return list(rows)


async def _publish(client, ph, cid, delivery, notes):
    sent = await client.post(f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update", headers=ph,
                             json={"release_notes": notes})
    assert sent.status_code == 200, sent.text
    return sent.json()


@pytest.mark.asyncio
async def test_a_sent_release_is_one_click_from_applied_and_the_customers_edits_stay(
    client, db_session, monkeypatch,
):
    # Catches: a release the customer can only find on the overview page; a
    # stale card left open beside a newer one; a one-step update that
    # overwrites a Flow the customer edited.
    import packages.core.services.notify as notify_module

    told = []

    async def fake_notify(entity_id, user_id, type, title, **kwargs):
        told.append((user_id, type, kwargs.get("link")))

    monkeypatch.setattr(notify_module, "notify", fake_notify)
    ph, _provider, rh, cid, delivery, _source, flows, target, copies = await _delivered(client, db_session)
    await _edit(client, rh, copies["Follow up"].id, "Customer wording")

    await _edit(client, ph, flows["Intake"], "Done v2")
    first = await _publish(client, ph, cid, delivery, "Clearer last step")
    assert first["offered"] == {"workspace_id": target, "version": "1.0.1"}
    for flow in flows.values():
        await _edit(client, ph, flow, "Done v3")
    await _publish(client, ph, cid, delivery, "Both Flows end the same way")

    cards = await _cards(db_session, target)
    assert [card.pending_action["version"] for card in cards] == ["1.0.1", "1.0.2"]
    assert cards[0].resolution["choice"] == "superseded" and cards[1].resolved_at is None
    assert cards[1].pending_action["release_notes"] == "Both Flows end the same way"
    owner = (await client.get("/api/v1/auth/me", headers=rh)).json()["id"]
    assert told == [(owner, "workspace_update_available", f"/workspaces/{target}")] * 2

    resolved = await client.post(
        f"/api/v1/workspaces/{target}/chat/messages/{cards[1].id}/resolve", headers=rh, json={"choice": "update"},
    )
    assert resolved.status_code == 200, resolved.text
    for row in copies.values():
        await db_session.refresh(row)
    assert copies["Intake"].steps[-1]["name"] == "Done v3"
    assert copies["Follow up"].steps[-1]["name"] == "Customer wording"
    card = (await _cards(db_session, target))[1]
    assert card.resolution["choice"] == "update"
    assert card.resolution["payload"]["version"] == "1.0.2"
    assert card.resolution["payload"]["updated"] == 1 and card.resolution["payload"]["kept"] == 1
    after = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()["blueprint_update"]
    assert after["installed_version"] == "1.0.2"

    # Undoing it is remembered on the card, which no longer claims the update.
    undone = await client.post(f"/api/v1/workspaces/{target}/blueprint/revert", headers=rh)
    assert undone.status_code == 200, undone.text
    assert (await _cards(db_session, target))[1].resolution["payload"]["undone"] is True


@pytest.mark.asyncio
async def test_a_one_step_update_that_needs_a_person_says_what_and_changes_nothing(client, db_session):
    ph, _provider, rh, cid, delivery, _source, flows, target, copies = await _delivered(client, db_session)
    await _edit(client, ph, flows["Intake"], "Done v2")
    await _publish(client, ph, cid, delivery, "")
    run = WorkflowRun(workflow_id=copies["Intake"].id, entity_id=copies["Intake"].entity_id, workspace_id=target,
                      status="paused", trigger_source="manual", execution_snapshot={"steps": []})
    db_session.add(run)
    await db_session.commit()

    refused = await client.post(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3/recommended", headers=rh, json={})
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "upgrade_needs_review" and detail["reason"] == "active_runs"
    assert [item["id"] for item in detail["active_runs"]] == [run.id]
    await db_session.refresh(copies["Intake"])
    assert copies["Intake"].steps[-1]["name"] == "Done"
    assert (await _cards(db_session, target))[0].resolved_at is None

    run.status = "completed"
    await db_session.commit()
    applied = await client.post(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3/recommended", headers=rh, json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["version"] == "1.0.1" and applied.json()["can_revert"] is True
    assert (await _cards(db_session, target))[0].resolution["choice"] == "update"
