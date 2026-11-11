"""A Blueprint file built anywhere reaches the Workspaces installed from it as an upgrade."""

import json

import pytest
from sqlalchemy import select

from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
from tests.test_client_workspace_delivery import register


def _steps(label):
    return [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
        {"id": "end", "type": "end", "name": label, "config": {}, "next": []},
    ]


async def _workflow(client, headers, workspace_id, name, label="Done"):
    created = await client.post("/api/v1/workflows", headers=headers, json={
        "name": name, "steps": _steps(label), "workspace_id": workspace_id, "visibility": "workspace",
    })
    assert created.status_code in (200, 201), created.text
    bound = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": created.json()["id"], "workspace_id": workspace_id, "trigger_type": "manual",
    })
    assert bound.status_code in (200, 201), bound.text
    return created.json()["id"]


async def _export(client, headers, workspace_id):
    exported = await client.post(f"/api/v1/workspaces/{workspace_id}/export-blueprint-payload", headers=headers, json={})
    assert exported.status_code == 200, exported.text
    return exported.json()


async def _publish(client, headers, workspace_id, payload, **form):
    return await client.post(
        f"/api/v1/workspaces/{workspace_id}/blueprint/releases", headers=headers,
        files={"file": ("blueprint.json", json.dumps(payload).encode(), "application/json")},
        data={key: str(value).lower() if isinstance(value, bool) else value for key, value in form.items()},
    )


async def _flows(db, workspace_id):
    rows = (await db.scalars(
        select(WorkflowDefinition).join(WorkflowBinding, WorkflowBinding.workflow_id == WorkflowDefinition.id)
        .where(WorkflowBinding.workspace_id == workspace_id)
    )).all()
    return {row.name: row for row in rows}


async def _apply_upgrade(client, headers, workspace_id):
    preview = await client.get(f"/api/v1/workspaces/{workspace_id}/blueprint/upgrade/v3", headers=headers)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    applied = await client.post(f"/api/v1/workspaces/{workspace_id}/blueprint/upgrade/v3", headers=headers, json={
        "expected_blueprint_fingerprint": plan["blueprint_fingerprint"],
        "expected_plan_fingerprint": plan["plan_fingerprint"],
        "configuration_resolutions": [
            {"key": item["key"], "resolution": "keep_yours" if item["action"] == "keep_yours" else "use_blueprint"}
            for item in plan["items"] if item["action"] != "unchanged"
        ],
    })
    assert applied.status_code == 200, applied.text
    return plan


@pytest.mark.asyncio
async def test_a_newer_file_reaches_the_workspace_installed_from_it(client, db_session):
    # Catches: a file install recorded with no update source, so a newer
    # file could only arrive as a second Workspace.
    headers, _user = await register(client, db_session, "packager")
    source = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Intake", "kind": "campaign"})).json()["id"]
    flows = {name: await _workflow(client, headers, source, name) for name in ("Intake", "Follow up")}
    first = await _export(client, headers, source)

    installed = await client.post("/api/v1/blueprints/install-payload", headers=headers, json={"payload": first, "mode": "simulate"})
    assert installed.status_code == 201, installed.text
    target = installed.json()["workspace_id"]
    update = (await client.get(f"/api/v1/workspaces/{target}", headers=headers)).json()["blueprint_update"]
    assert update["blueprint_id"].startswith("team-package:") and update["status"] == "current"
    assert update["installed_version"] == "1.0.0"

    edited = await client.put(f"/api/v1/workflows/{flows['Intake']}", headers=headers, json={"steps": _steps("Done v2")})
    assert edited.status_code == 200, edited.text
    second = await _export(client, headers, source)
    dry = await _publish(client, headers, target, second, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True and dry.json()["current_version"] == "1.0.0"

    published = await _publish(client, headers, target, second, release_notes="Clearer last step")
    assert published.status_code == 200, published.text
    assert published.json()["published"] is True and published.json()["version"] != "1.0.0"
    # The same content again moves nothing.
    again = await _publish(client, headers, target, second)
    assert again.status_code == 200, again.text
    assert again.json()["published"] is False and again.json()["version"] == published.json()["version"]

    update = (await client.get(f"/api/v1/workspaces/{target}", headers=headers)).json()["blueprint_update"]
    assert update["status"] == "update_available"
    plan = await _apply_upgrade(client, headers, target)
    actions = {item["name"]: item["action"] for item in plan["items"] if item["kind"] == "workflow"}
    assert actions == {"Intake": "update", "Follow up": "unchanged"}, actions
    copies = await _flows(db_session, target)
    assert copies["Intake"].steps[-1]["name"] == "Done v2"
    after = (await client.get(f"/api/v1/workspaces/{target}", headers=headers)).json()["blueprint_update"]
    assert after["status"] == "current" and after["installed_version"] == published.json()["version"]

    # Its next export names the package, so any environment recognises it.
    assert (await _export(client, headers, target))["manifest"]["package"]["name"] == update["blueprint_id"].split(":", 2)[2]


@pytest.mark.asyncio
async def test_another_workspaces_file_is_not_a_release(client, db_session):
    headers, _user = await register(client, db_session, "packager2")
    source = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Intake", "kind": "campaign"})).json()["id"]
    await _workflow(client, headers, source, "Intake")
    installed = await client.post("/api/v1/blueprints/install-payload", headers=headers, json={
        "payload": await _export(client, headers, source), "mode": "simulate",
    })
    target = installed.json()["workspace_id"]
    other = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Other", "kind": "campaign"})).json()["id"]
    await _workflow(client, headers, other, "Unrelated")
    rejected = await _publish(client, headers, target, await _export(client, headers, other))
    assert rejected.status_code == 422, rejected.text
    renamed = await _export(client, headers, source)
    renamed["manifest"]["package"] = {"name": "@someone/else"}
    assert (await _publish(client, headers, target, renamed)).status_code == 422


@pytest.mark.asyncio
async def test_a_workspace_without_a_package_adopts_one_and_upgrades_to_it(client, db_session):
    # Catches: a Workspace built by hand (or installed before packages) that
    # can only take a newer file by being rebuilt or edited in place.
    headers, _user = await register(client, db_session, "adopter")
    built = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Built", "kind": "campaign"})).json()["id"]
    flows = {name: await _workflow(client, headers, built, name) for name in ("Intake", "Follow up")}
    package = await _export(client, headers, built)
    for step in package["recipe"]["workflows"]:
        if step["name"] == "Intake":
            step["steps"][-1]["name"] = "Done v2"

    refused = await _publish(client, headers, built, package)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "package_adoption_required"
    dry = await _publish(client, headers, built, package, adopt=True, dry_run=True)
    assert dry.status_code == 200, dry.text
    matched = {item["name"] for item in dry.json()["adoption"]["matched"]}
    assert matched == {"Intake", "Follow up"} and dry.json()["adoption"]["unmatched"] == []

    adopted = await _publish(client, headers, built, package, adopt=True)
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["adoption"]["unmatched"] == []
    update = (await client.get(f"/api/v1/workspaces/{built}", headers=headers)).json()["blueprint_update"]
    assert update["status"] == "update_available"
    plan = await _apply_upgrade(client, headers, built)
    actions = {item["name"]: item["action"] for item in plan["items"] if item["kind"] == "workflow"}
    # Matched in place: nothing is installed a second time.
    assert actions == {"Intake": "update", "Follow up": "unchanged"}, actions
    rows = await _flows(db_session, built)
    assert set(rows) == {"Intake", "Follow up"} and rows["Intake"].id == flows["Intake"]
    assert rows["Intake"].steps[-1]["name"] == "Done v2"
    after = (await client.get(f"/api/v1/workspaces/{built}", headers=headers)).json()["blueprint_update"]
    assert after["status"] == "current"
