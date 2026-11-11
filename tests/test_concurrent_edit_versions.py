"""Two editors never silently overwrite each other's Workflow or Skill.

A save names the version it started from; a save made since then wins and the
late one is refused with 409, leaving the stored content as the first editor
left it. Saves without a version keep the old behaviour.
"""

import pytest
from httpx import AsyncClient

from packages.core.revisions import StaleRevisionError
from packages.core.services import skill_generator, skill_service
from tests.auth_helpers import register_user_and_get_token


async def _auth(client: AsyncClient) -> dict:
    resp = await register_user_and_get_token(client, json={
        "username": "coeditor", "email": "coeditor@test.com",
        "password": "CoEditorTest123!", "entity_name": "Co-edit Corp",
    })
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


_STEPS = [
    {"id": "start", "type": "trigger", "name": "Start", "config": {}, "next": ["done"]},
    {"id": "done", "type": "output", "name": "Done", "config": {}, "next": []},
]


@pytest.mark.asyncio
async def test_workflow_save_from_an_old_version_is_refused(client: AsyncClient):
    headers = await _auth(client)
    created = (await client.post("/api/v1/workflows", headers=headers, json={"name": "Flow", "steps": _STEPS})).json()
    url = f"/api/v1/workflows/{created['id']}"
    loaded = created["version"]

    first = await client.put(url, headers=headers, json={"name": "Flow A", "expected_version": loaded})
    assert first.status_code == 200, first.text
    assert first.json()["version"] == loaded + 1

    late = await client.put(url, headers=headers, json={"name": "Flow B", "expected_version": loaded})
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "workflow.error.edited_elsewhere"
    assert late.json()["detail"]["vars"] == {"expected": loaded, "actual": loaded + 1}
    assert (await client.get(url, headers=headers)).json()["name"] == "Flow A"

    unversioned = await client.put(url, headers=headers, json={"description": "still allowed"})
    assert unversioned.status_code == 200


@pytest.mark.asyncio
async def test_skill_save_from_an_old_edit_version_is_refused(client: AsyncClient):
    headers = await _auth(client)
    created = (await client.post("/api/v1/skills", headers=headers, json={
        "name": "brief-writer", "system_prompt": "Write the brief.", "description": "Writes briefs",
    })).json()
    url = f"/api/v1/skills/{created['id']}"
    loaded = created["edit_version"]

    # A description change is cosmetic for ``revision`` but still a save.
    first = await client.put(url, headers=headers, json={"description": "Writes launch briefs", "expected_edit_version": loaded})
    assert first.status_code == 200, first.text
    assert first.json()["edit_version"] == loaded + 1
    assert first.json()["description"] == "Writes launch briefs"

    same = await client.put(url, headers=headers, json={"description": "Writes launch briefs"})
    assert same.json()["edit_version"] == loaded + 1

    late = await client.put(url, headers=headers, json={"description": "Writes ads", "expected_edit_version": loaded})
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "skill.error.edited_elsewhere"
    assert (await client.get(url, headers=headers)).json()["description"] == "Writes launch briefs"


@pytest.mark.asyncio
async def test_ai_skill_edit_does_not_overwrite_a_save_made_while_the_model_worked(
    client: AsyncClient, db_session, monkeypatch,
):
    headers = await _auth(client)
    created = (await client.post("/api/v1/skills", headers=headers, json={
        "name": "brief-writer", "system_prompt": "Write the brief.",
    })).json()
    entity_id = created["entity_id"]

    async def patch_while_someone_saves(existing, *args, **kwargs):
        await skill_service.update_skill(db_session, existing.id, entity_id, system_prompt="Human edit.")
        return {"system_prompt": "Model edit.", "version": "1.0.1"}

    monkeypatch.setattr(skill_generator, "build_skill_update_patch", patch_while_someone_saves)
    with pytest.raises(StaleRevisionError):
        await skill_generator.update_skill(created["id"], "Tighten it", entity_id, db_session)
    assert (await skill_service.get_skill(db_session, created["id"])).system_prompt == "Human edit."
