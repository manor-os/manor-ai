from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "pass123",
        "entity_name": "Flow Template Corp",
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_flow_template_installs_by_catalogue_id_and_reuses_runtime_id(
    client: AsyncClient,
):
    headers = await _auth(client, "flowtemplateid")
    listing = await client.get("/api/v1/workflows/templates", headers=headers)
    assert listing.status_code == 200, listing.text
    templates = listing.json()
    linkedin = next(
        item for item in templates
        if item["id"] == "builtin-flow:opc-publish-linkedin-v1"
    )
    assert linkedin["key"] == "opc-publish-linkedin-v1"
    assert linkedin["verification_status"] == "graph_verified"
    assert linkedin["installed"] is False

    first = await client.post(
        f"/api/v1/workflows/templates/{linkedin['id']}/install",
        headers=headers,
        json={},
    )
    assert first.status_code == 201, first.text
    first_data = first.json()
    workflow_id = first_data["workflow"]["id"]
    assert workflow_id != linkedin["id"]
    assert first_data["template"]["id"] == linkedin["id"]
    assert first_data["already_installed"] is False

    second = await client.post(
        f"/api/v1/workflows/templates/{linkedin['id']}/install",
        headers=headers,
        json={},
    )
    assert second.status_code == 201, second.text
    assert second.json()["workflow"]["id"] == workflow_id
    assert second.json()["already_installed"] is True

    refreshed = await client.get("/api/v1/workflows/templates", headers=headers)
    refreshed_linkedin = next(
        item for item in refreshed.json() if item["id"] == linkedin["id"]
    )
    assert refreshed_linkedin["installed"] is True
    assert refreshed_linkedin["installed_workflow_id"] == workflow_id

    metadata = await client.get(
        f"/api/v1/workflows/{workflow_id}/metadata",
        headers=headers,
    )
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["template_sources"] == [{
        "template_id": linkedin["id"],
        "component_key": "main",
        "installed_version": linkedin["version"],
        "source_type": "flow_template",
    }]


@pytest.mark.asyncio
async def test_dispatcher_template_installs_and_resolves_subflow_ids(
    client: AsyncClient,
):
    headers = await _auth(client, "flowtemplatedeps")
    template_id = "builtin-flow:opc-daily-content-dispatcher-v1"
    response = await client.post(
        f"/api/v1/workflows/templates/{template_id}/install",
        headers=headers,
        json={},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["dependencies"]

    installed = (await client.get("/api/v1/workflows", headers=headers)).json()
    installed_ids = {item["id"] for item in installed}
    dispatcher = data["workflow"]
    subflow_nodes = [
        step for step in dispatcher["steps"]
        if step["type"] in {"subworkflow", "foreach_subworkflow"}
    ]
    assert subflow_nodes
    for node in subflow_nodes:
        assert node["config"]["workflow_id"] in installed_ids
        assert node["config"]["workflow_id"].startswith("01")
        assert node["config"]["source_workflow_key"].startswith("opc-")


@pytest.mark.asyncio
async def test_template_install_can_attach_runtime_workflow_to_workspace(
    client: AsyncClient,
):
    headers = await _auth(client, "flowtemplateworkspace")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Content Workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    template_id = "builtin-flow:opc-generate-topic-from-knowledge-v1"

    installed = await client.post(
        f"/api/v1/workflows/templates/{template_id}/install",
        headers=headers,
        json={"workspace_id": workspace_id},
    )
    assert installed.status_code == 201, installed.text
    data = installed.json()
    assert data["binding_id"]

    bindings = await client.get(
        f"/api/v1/workflows/bindings?workspace_id={workspace_id}",
        headers=headers,
    )
    assert bindings.status_code == 200, bindings.text
    assert any(
        binding["id"] == data["binding_id"]
        and binding["workflow_id"] == data["workflow"]["id"]
        for binding in bindings.json()
    )
