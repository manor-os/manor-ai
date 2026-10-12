from copy import deepcopy

import pytest
from sqlalchemy import func, select

from packages.core.models.skill import Skill
from packages.core.services.agent_provisioning_service import (
    CustomAgentSpec, _create_provisioned_skill, provision_custom_agent,
)


@pytest.mark.asyncio
async def test_same_acquired_content_reuses_identity_despite_different_search_reason(db_session, monkeypatch):
    from packages.core.services import agent_provisioning_service, skill_service

    monkeypatch.setattr(skill_service, "_save_skill_files_to_minio", lambda *a, **k: None)

    async def must_not_select(*args, **kwargs):
        raise AssertionError("finalize must not repeat discovery under database locks")

    monkeypatch.setattr(agent_provisioning_service, "_select_existing_skill_for_missing_spec", must_not_select)
    spec = {
        "name": "Evidence methodology", "system_prompt": "Verify evidence and cite sources.",
        "tools": [], "input_schema": {"type": "object"}, "output_format": "json",
        "config": {"source": "github_skill_acquisition", "commit_sha": "a" * 40,
                   "content_sha256": "b" * 64,
                   "skill_acquisition": {"reason": "First search explanation"}},
    }
    for index in range(2):
        selected = deepcopy(spec)
        selected["config"]["skill_acquisition"]["reason"] = f"Search explanation {index}"
        await provision_custom_agent(db_session, entity_id="acquired_identity_test", spec=CustomAgentSpec(
            agent_name=f"Research agent {index}", system_prompt="Research carefully.",
            missing_skill_specs=[selected],
        ))
    skills = (await db_session.execute(select(Skill).where(Skill.entity_id == "acquired_identity_test"))).scalars().all()
    assert len(skills) == 1
    assert skills[0].input_schema == {"type": "object"}
    assert skills[0].output_format == "json"
    assert skills[0].config["source"] == "github_skill_acquisition"

    # A stored identity cannot outlive edits to the executable contract.
    current = skills[0]
    for index, (field, changed) in enumerate((
        ("input_schema", {"type": "object", "required": ["unexpected"]}),
        ("output_format", "text"),
    )):
        setattr(current, field, changed)
        await db_session.flush()
        replacement = await provision_custom_agent(db_session, entity_id="acquired_identity_test", spec=CustomAgentSpec(
            agent_name=f"Fresh research agent {index}", system_prompt="Research carefully.",
            missing_skill_specs=[deepcopy(spec)],
        ))
        assert replacement.created_skills
        current = (await db_session.execute(select(Skill).where(
            Skill.entity_id == "acquired_identity_test", Skill.slug == replacement.created_skills[0],
        ))).scalar_one()
        assert current.input_schema == {"type": "object"}
        assert current.output_format == "json"


@pytest.mark.asyncio
async def test_incomplete_acquired_bundle_rolls_back_new_skill(db_session, monkeypatch):
    from packages.core.services import skill_file_storage, skill_service

    cleaned = []
    monkeypatch.setattr(skill_service, "_save_skill_files_to_minio", lambda *a, **k: "new-owned-test-dir")
    monkeypatch.setattr(skill_file_storage, "load_skill_extra_files", lambda *a, **k: {})
    monkeypatch.setattr(skill_service, "_delete_skill_files_best_effort", lambda skill, config, **k: cleaned.append(skill.id))
    with pytest.raises(ValueError, match="storage is incomplete"):
        await _create_provisioned_skill(
            db_session, verify_bundle=True, entity_id="acquired_bundle_test",
            name="Complete bundle required", system_prompt="Run scripts/nested/main.py.",
            config={"type": "sandbox", "scripts": {"scripts/nested/main.py": "print('ok')"}},
        )
    assert len(cleaned) == 1
    assert await db_session.scalar(select(func.count()).select_from(Skill).where(Skill.entity_id == "acquired_bundle_test")) == 0
