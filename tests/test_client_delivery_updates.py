"""A provider's newer release reaches a delivered Workspace through the Blueprint upgrade."""

import pytest
from sqlalchemy import select

from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import UserMembership
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.workspace import Workspace, WorkspaceStaff
from tests.test_client_workspace_delivery import register


def _steps(label):
    return [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
        {"id": "end", "type": "end", "name": label, "config": {}, "next": []},
    ]


async def _workflow(client, headers, workspace_id, name):
    created = await client.post("/api/v1/workflows", headers=headers, json={"name": name, "steps": _steps("Done")})
    assert created.status_code in (200, 201), created.text
    bound = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": created.json()["id"], "workspace_id": workspace_id, "trigger_type": "manual",
    })
    assert bound.status_code in (200, 201), bound.text
    return created.json()["id"]


async def _edit(client, headers, workflow_id, label):
    edited = await client.put(f"/api/v1/workflows/{workflow_id}", headers=headers, json={"steps": _steps(label)})
    assert edited.status_code == 200, edited.text


async def _delivered(client, db):
    ph, provider = await register(client, db, "provider")
    rh, recipient = await register(client, db, "recipient")
    cid = (await client.post("/api/v1/clients", headers=ph, json={"name": "Customer"})).json()["id"]
    contact = (await client.post(
        f"/api/v1/clients/{cid}/contacts", headers=ph, json={"name": "Owner", "email": recipient.email},
    )).json()
    source = (await client.post("/api/v1/workspaces", headers=ph, json={"name": "Intake", "kind": "campaign"})).json()["id"]
    flows = {name: await _workflow(client, ph, source, name) for name in ("Intake", "Follow up")}
    created = await client.post(
        f"/api/v1/clients/{cid}/deliveries", headers=ph, json={"workspace_id": source, "contact_id": contact["id"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["content_version"] == "1.0.0"
    token = created.json()["invite_path"].rsplit("/", 1)[-1]
    accepted = await client.post(f"/api/v1/client-deliveries/invites/{token}/accept", headers=rh, json={})
    assert accepted.status_code == 200, accepted.text
    target = accepted.json()["target_workspace_id"]
    copies = {
        row.name: row for row in (await db.scalars(
            select(WorkflowDefinition).join(WorkflowBinding, WorkflowBinding.workflow_id == WorkflowDefinition.id)
            .where(WorkflowBinding.workspace_id == target)
        )).all()
    }
    return ph, provider, rh, cid, created.json()["id"], source, flows, target, copies


@pytest.mark.asyncio
async def test_published_update_previews_and_applies_while_customer_edits_stay(client, db_session):
    # Catches: a delivered Workspace with no update source; a delivery-only
    # apply path; overwriting a Flow the customer changed in their copy.
    ph, _provider, rh, cid, delivery, source, flows, target, copies = await _delivered(client, db_session)
    current = await client.get(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh)
    assert current.status_code == 200, current.text
    assert all(item["action"] == "unchanged" for item in current.json()["items"]), current.json()["items"]

    for flow in flows.values():
        await _edit(client, ph, flow, "Done v2")
    await _edit(client, rh, copies["Follow up"].id, "Customer wording")
    published = await client.post(
        f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update", headers=ph,
        json={"release_notes": "Clearer last step"},
    )
    assert published.status_code == 200, published.text
    assert published.json()["published"] is True and published.json()["content_version"] == "1.0.1"
    # Republishing unchanged content moves no version.
    again = await client.post(f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update", headers=ph, json={})
    assert again.json()["published"] is False and again.json()["content_version"] == "1.0.1"

    workspace = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()
    assert workspace["blueprint_update"]["status"] == "update_available"
    assert workspace["blueprint_update"]["current_version"] == "1.0.1"
    preview = await client.get(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    flow_items = {item["name"]: item for item in plan["items"] if item["kind"] == "workflow"}
    assert flow_items["Intake"]["action"] == "update"
    assert flow_items["Follow up"]["action"] == "keep_yours"

    applied = await client.post(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh, json={
        "expected_blueprint_fingerprint": plan["blueprint_fingerprint"],
        "expected_plan_fingerprint": plan["plan_fingerprint"],
        "configuration_resolutions": [
            {"key": item["key"], "resolution": "keep_yours" if item["action"] == "keep_yours" else "use_blueprint"}
            for item in plan["items"] if item["action"] != "unchanged"
        ],
    })
    assert applied.status_code == 200, applied.text
    for row in copies.values():
        await db_session.refresh(row)
    assert copies["Intake"].steps[-1]["name"] == "Done v2"
    assert copies["Follow up"].steps[-1]["name"] == "Customer wording"
    after = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()["blueprint_update"]
    assert after["installed_version"] == "1.0.1" and after["status"] != "update_available"
    # The source Workspace itself never changed hands.
    assert (await client.get(f"/api/v1/workspaces/{source}", headers=ph)).status_code == 200


@pytest.mark.asyncio
async def test_only_a_source_workspace_manager_can_publish_an_update(client, db_session):
    ph, provider, rh, cid, delivery, source, _flows, _target, _copies = await _delivered(client, db_session)
    path = f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update"
    # The customer cannot reach the provider's delivery record at all.
    assert (await client.post(path, headers=rh, json={})).status_code == 404
    for seat in (await db_session.scalars(select(WorkspaceStaff).where(
        WorkspaceStaff.workspace_id == source, WorkspaceStaff.user_id == provider.id,
    ))).all():
        seat.status = "inactive"
    # Their home company only: the trial also gave them a collaborator membership.
    home = (await db_session.execute(select(UserMembership).where(
        UserMembership.user_id == provider.id, UserMembership.entity_id == provider.entity_id,
    ))).scalar_one()
    home.role = provider.role = "member"
    staff = (await db_session.execute(select(Staff).where(
        Staff.user_id == provider.id, Staff.entity_id == provider.entity_id,
    ))).scalar_one()
    role = StaffRole(entity_id=provider.entity_id, name="No administrative permissions", permissions=[])
    db_session.add(role)
    await db_session.flush()
    staff.role_id = role.id
    await db_session.commit()
    refused = await client.post(path, headers=ph, json={})
    assert refused.status_code == 403, refused.text


@pytest.mark.asyncio
async def test_each_published_update_is_a_release_the_customer_can_name(client, db_session):
    # Catches: a publish that overwrites the delivery without history; an
    # install or upgrade that leaves _blueprint.package naming the wrong
    # release; a freshness badge that cannot say how far behind or what
    # changed; an undo that leaves the upgrade history claiming it applied.
    from packages.core.models.blueprint_registry import BlueprintVersion, WorkspaceBlueprintUpgrade
    from packages.core.models.workspace import Workspace

    ph, _provider, rh, cid, delivery, _source, flows, target, _copies = await _delivered(client, db_session)
    ref = f"client-delivery:{delivery}"
    workspace = await db_session.get(Workspace, target)
    package = workspace.settings["_blueprint"]["package"]
    assert (package["ref"], package["version"], package["base"]) == (ref, "1.0.0", "exact")

    for flow in flows.values():
        await _edit(client, ph, flow, "Done v2")
    published = await client.post(
        f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update", headers=ph,
        json={"release_notes": "Clearer last step"},
    )
    assert published.status_code == 200, published.text
    await _edit(client, ph, flows["Intake"], "Done v3")
    published = await client.post(f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update", headers=ph, json={})
    assert published.json()["content_version"] == "1.0.2"
    versions = (await db_session.scalars(
        select(BlueprintVersion.version).where(BlueprintVersion.package_ref == ref)
    )).all()
    assert sorted(versions) == ["1.0.0", "1.0.1", "1.0.2"]

    update = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()["blueprint_update"]
    assert (update["latest_version"], update["versions_behind"]) == ("1.0.2", 2)
    assert [(entry["version"], entry["changelog"]) for entry in update["changelog"]] == [
        ("1.0.1", "Clearer last step"), ("1.0.2", None),
    ]
    # Any listed release can be the target, not only the latest.
    middle = (await client.get(
        f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh, params={"target_version": "1.0.1"},
    )).json()
    assert (middle["target_version"], middle["current_version"], middle["downgrade"]) == ("1.0.1", "1.0.1", False)
    assert [entry["version"] for entry in middle["available_versions"]] == ["1.0.2", "1.0.1", "1.0.0"]
    assert (middle["version_change"]["from"], middle["version_change"]["to"]) == ("1.0.0", "1.0.1")
    missing = await client.get(
        f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh, params={"target_version": "9.9.9"},
    )
    assert missing.status_code == 404
    plan = (await client.get(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh)).json()
    assert (plan["latest_version"], plan["versions_behind"]) == ("1.0.2", 2)
    assert (plan["target_version"], plan["blueprint_fingerprint"] != middle["blueprint_fingerprint"]) == ("1.0.2", True)

    applied = await client.post(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh, json={
        "expected_blueprint_fingerprint": plan["blueprint_fingerprint"],
        "expected_plan_fingerprint": plan["plan_fingerprint"],
        "configuration_resolutions": [
            {"key": item["key"], "resolution": "use_blueprint"}
            for item in plan["items"] if item["action"] != "unchanged"
        ],
    })
    assert applied.status_code == 200, applied.text
    await db_session.refresh(workspace)
    assert workspace.settings["_blueprint"]["package"]["version"] == "1.0.2"
    history = (await db_session.scalars(
        select(WorkspaceBlueprintUpgrade).where(WorkspaceBlueprintUpgrade.workspace_id == target)
    )).one()
    assert (history.package_ref, history.from_version, history.to_version) == (ref, "1.0.0", "1.0.2")
    assert history.reverted_at is None
    after = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()["blueprint_update"]
    assert (after["versions_behind"], after["changelog"]) == (0, [])

    reverted = await client.post(f"/api/v1/workspaces/{target}/blueprint/revert", headers=rh)
    assert reverted.status_code == 200, reverted.text
    await db_session.refresh(workspace)
    await db_session.refresh(history)
    assert workspace.settings["_blueprint"]["package"]["version"] == "1.0.0"
    assert history.reverted_at is not None


async def _release_file(client, headers, source, label):
    """The source's Blueprint as a teammate or staging would hand it over, one step renamed."""
    exported = await client.post(f"/api/v1/workspaces/{source}/export-blueprint-payload", headers=headers, json={})
    assert exported.status_code == 200, exported.text
    payload = exported.json()
    intake = next(flow for flow in payload["recipe"]["workflows"] if flow["name"] == "Intake")
    intake["steps"][-1]["name"] = label
    return payload


async def _send_file(client, headers, cid, delivery, payload, **form):
    import json

    return await client.post(
        f"/api/v1/clients/{cid}/deliveries/{delivery}/publish-update-file", headers=headers,
        files={"file": ("blueprint.json", json.dumps(payload).encode(), "application/json")},
        data={key: str(value).lower() if isinstance(value, bool) else value for key, value in form.items()},
    )


@pytest.mark.asyncio
async def test_a_blueprint_built_elsewhere_reaches_the_customer_through_the_same_upgrade(client, db_session):
    # Catches: a file update that bypasses the release history, a preview that
    # sends, and a file update that touches the provider's source Workspace.
    ph, _provider, rh, cid, delivery, source, flows, target, copies = await _delivered(client, db_session)
    payload = await _release_file(client, ph, source, "Done on staging")

    preview = await _send_file(client, ph, cid, delivery, payload, dry_run=True)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True and body["would_publish"] is True and body["published"] is False
    assert body["version"] == "1.0.1" and body["level"] == "patch" and body["content_version"] == "1.0.0"
    assert any(key.startswith("workflow:") for key in body["components"]["changed"])
    workspace = (await client.get(f"/api/v1/workspaces/{target}", headers=rh)).json()
    assert workspace["blueprint_update"]["status"] != "update_available"

    sent = await _send_file(client, ph, cid, delivery, payload, release_notes="Tested on staging")
    assert sent.status_code == 200, sent.text
    assert sent.json()["published"] is True and sent.json()["content_version"] == "1.0.1"

    plan = (await client.get(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh)).json()
    applied = await client.post(f"/api/v1/workspaces/{target}/blueprint/upgrade/v3", headers=rh, json={
        "expected_blueprint_fingerprint": plan["blueprint_fingerprint"],
        "expected_plan_fingerprint": plan["plan_fingerprint"],
        "configuration_resolutions": [
            {"key": item["key"], "resolution": "use_blueprint"} for item in plan["items"] if item["action"] != "unchanged"
        ],
    })
    assert applied.status_code == 200, applied.text
    await db_session.refresh(copies["Intake"])
    assert copies["Intake"].steps[-1]["name"] == "Done on staging"
    source_flow = await db_session.get(WorkflowDefinition, flows["Intake"])
    await db_session.refresh(source_flow)
    assert source_flow.steps[-1]["name"] == "Done"


@pytest.mark.asyncio
async def test_a_file_from_another_workspace_or_with_files_before_acceptance_is_refused(client, db_session, monkeypatch):
    from packages.core.blueprints.package import asset_from_bytes
    from packages.core.services import client_delivery

    ph, provider, _rh, cid, delivery, source, _flows, target, _copies = await _delivered(client, db_session)
    payload = await _release_file(client, ph, source, "Done")
    unrelated = {**payload, "recipe": {**payload["recipe"], "workflows": [
        {**flow, "slug": f"other-{index}", "component_id": f"other-{index}", "name": f"Other {index}"}
        for index, flow in enumerate(payload["recipe"]["workflows"])
    ]}}
    refused = await _send_file(client, ph, cid, delivery, unrelated, dry_run=True)
    assert refused.status_code == 422 and "different Workspace" in refused.text

    # A package's files go into the customer's team, where their Workspace reads them.
    stored = {}

    async def fake_store(entity_id, assets):
        stored["entity_id"] = entity_id
        return {asset.sha256: f"/api/v1/fs/{entity_id}/marketplace/blueprint-assets/{asset.sha256}.png" for asset in assets}

    monkeypatch.setattr("packages.core.blueprints.package.store_assets", fake_store)
    asset = asset_from_bytes(b"png", name="cover.png", mime_type="image/png")
    with_file = await _release_file(client, ph, source, "Done with cover")
    with_file["manifest"]["cover_image_url"] = asset.placeholder
    user = await db_session.get(type(provider), provider.id)
    target_entity = (await db_session.get(Workspace, target)).entity_id
    result = await client_delivery.publish_update(
        db_session, user=user, client_id=cid, delivery_id=delivery, payload=with_file, assets=[asset],
    )
    assert result["published"] is True and stored["entity_id"] == target_entity

    row = await db_session.get(ClientWorkspaceDelivery, delivery)
    row.target_workspace_id = None
    with pytest.raises(ValueError, match="not accepted the delivery yet"):
        await client_delivery.publish_update(
            db_session, user=user, client_id=cid, delivery_id=delivery, payload=with_file, assets=[asset],
        )
