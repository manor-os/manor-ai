import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.skills import (
    resolve_skill_descriptors,
    runtime_filter_skills_for_installed_ledgers,
    runtime_prepare_prompt_skill_tool_surface,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.services.builtin_skill_loader import (
    _parse_frontmatter,
    _read_skill_config,
    seed_builtin_skills,
)
from packages.core.services.skill_service import invoke_skill


ROOT = Path(__file__).parents[1] / "packages/core/ai/skills"
LEDGER_SKILLS = {
    "relationship-ledger": (
        "manor.relationship_ledger/v1",
        "read_relationship_ledger",
        "record_relationship_ledger",
    ),
    "finance-ledger": (
        "manor.finance_ledger/v1",
        "read_finance_ledger",
        "record_finance_ledger",
    ),
    "recruiting-ledger": (
        "manor.recruiting_ledger/v1",
        "read_recruiting_ledger",
        "record_recruiting_ledger",
    ),
    "content-ledger": (
        "manor.content_ledger/v1",
        "read_content_ledger",
        "record_content_ledger",
    ),
}


@pytest.mark.parametrize("slug", LEDGER_SKILLS)
def test_builtin_ledger_skill_declares_its_contract_and_tools(slug: str) -> None:
    contract_id, read_tool, record_tool = LEDGER_SKILLS[slug]
    skill_dir = ROOT / slug
    frontmatter, body = _parse_frontmatter(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    )
    config = _read_skill_config(skill_dir)

    assert frontmatter["name"] == slug
    assert frontmatter["description"]
    assert body.strip()
    assert config["type"] == "runtime_guidance"
    assert config["execution_mode"] == "instructions_only"
    assert config["category"] == "workspace-ledger"
    assert config["ledger_contracts"] == [contract_id]
    assert set(config["tools"]) == {
        read_tool,
        record_tool,
        "query_ledger",
        "visualize_workspace_ledgers",
    }

    assert "invocation_policy" not in config
    assert config["capability_companion"] == {
        "tool_names": [read_tool, record_tool]
    }


def test_relationship_skill_is_general_and_has_contact_storage_rules() -> None:
    prompt = (ROOT / "relationship-ledger/SKILL.md").read_text(encoding="utf-8")

    assert "contact_points" in prompt
    assert "suppression_scope" in prompt
    assert "all_channels" in prompt
    assert "shallow-merges" in prompt
    assert "creator_crm_v1" not in prompt
    assert "creator-crm" not in prompt


def _skill(slug: str, contract_id: str | None) -> SimpleNamespace:
    config = {"source": "builtin"}
    if contract_id:
        config["ledger_contracts"] = [contract_id]
    return SimpleNamespace(
        id=f"skill_{slug}",
        entity_id=None,
        slug=slug,
        config=config,
    )


def test_ledger_skills_follow_only_their_installed_contract() -> None:
    ordinary = _skill("ordinary", None)
    relationship = _skill("relationship-ledger", "manor.relationship_ledger/v1")
    finance = _skill("finance-ledger", "manor.finance_ledger/v1")

    filtered = runtime_filter_skills_for_installed_ledgers(
        [ordinary, relationship, finance],
        {"manor.relationship_ledger/v1"},
    )

    assert [skill.slug for skill in filtered] == ["ordinary", "relationship-ledger"]
    assert runtime_filter_skills_for_installed_ledgers(
        [ordinary, relationship, finance],
        set(),
    ) == [ordinary]


def test_ledger_skill_guidance_does_not_grant_write_permission() -> None:
    skill = SimpleNamespace(
        tools=[
            "read_relationship_ledger",
            "record_relationship_ledger",
            "query_ledger",
        ],
        config={},
    )
    def schemas(names):
        return [
            {"type": "function", "function": {"name": name}}
            for name in names
        ]

    read_only = runtime_prepare_prompt_skill_tool_surface(
        skill,
        allowed_tool_names={"read_relationship_ledger", "query_ledger"},
        get_schemas_for_names=schemas,
        get_registered_tool_names=lambda: (),
    )
    writable = runtime_prepare_prompt_skill_tool_surface(
        skill,
        allowed_tool_names={
            "read_relationship_ledger",
            "record_relationship_ledger",
            "query_ledger",
        },
        get_schemas_for_names=schemas,
        get_registered_tool_names=lambda: (),
    )

    assert read_only.skill_tool_names == (
        "read_relationship_ledger",
        "query_ledger",
    )
    assert "record_relationship_ledger" not in read_only.allowed_tool_names
    assert writable.skill_tool_names == (
        "read_relationship_ledger",
        "record_relationship_ledger",
        "query_ledger",
    )


def test_entity_skill_cannot_claim_a_builtin_ledger_binding() -> None:
    entity_skill = SimpleNamespace(
        id="entity_skill",
        entity_id="entity_1",
        slug="custom",
        config={
            "source": "builtin",
            "ledger_contracts": ["manor.relationship_ledger/v1"],
        },
    )

    assert runtime_filter_skills_for_installed_ledgers(
        [entity_skill],
        set(),
    ) == [entity_skill]


@pytest.mark.asyncio
async def test_builtin_ledger_skill_config_is_seeded(db_session) -> None:
    seeded = {skill.slug: skill for skill in await seed_builtin_skills(db_session)}

    for slug, (contract_id, _read_tool, _record_tool) in LEDGER_SKILLS.items():
        assert seeded[slug].entity_id is None
        assert seeded[slug].is_public is True
        assert seeded[slug].config["ledger_contracts"] == [contract_id]
        assert seeded[slug].config["execution_mode"] == "instructions_only"
        assert "invocation_policy" not in seeded[slug].config
        assert seeded[slug].config["capability_companion"] == {
            "tool_names": [_read_tool, _record_tool]
        }


@pytest.mark.asyncio
async def test_runtime_loads_only_the_installed_ledger_skill(db_session) -> None:
    entity_id = generate_ulid()
    relationship_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Relationship Workspace",
        settings={"ledger_contracts": ["manor.relationship_ledger/v1"]},
        status="active",
    )
    no_ledger_workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="No Ledger Workspace",
        settings={"ledger_contracts": []},
        status="active",
    )
    db_session.add_all([relationship_workspace, no_ledger_workspace])
    await db_session.flush()

    relationship_descriptors = await resolve_skill_descriptors(
        db_session,
        entity_id=entity_id,
        agent_id=None,
        workspace_id=relationship_workspace.id,
        surface=ChatSurface.WORKSPACE_CHAT,
        invoke_skill_visible=True,
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        allowed_tool_names={
            "invoke_skill",
            "read_relationship_ledger",
            "record_relationship_ledger",
            "query_ledger",
            "visualize_workspace_ledgers",
        },
        active_user_message="Check the contact history before following up.",
        limit=8,
    )
    relationship_slugs = {descriptor.slug for descriptor in relationship_descriptors}
    assert "relationship-ledger" in relationship_slugs
    assert not ({"finance-ledger", "recruiting-ledger", "content-ledger"} & relationship_slugs)
    relationship_descriptor = next(
        descriptor
        for descriptor in relationship_descriptors
        if descriptor.slug == "relationship-ledger"
    )
    assert relationship_descriptor.metadata["capability_companion"] == {
        "tool_names": [
            "read_relationship_ledger",
            "record_relationship_ledger",
        ]
    }

    no_ledger_descriptors = await resolve_skill_descriptors(
        db_session,
        entity_id=entity_id,
        agent_id=None,
        workspace_id=no_ledger_workspace.id,
        surface=ChatSurface.WORKSPACE_CHAT,
        invoke_skill_visible=True,
        profile=RuntimeProfile.WORKSPACE_OPERATOR,
        allowed_tool_names={"invoke_skill"},
        active_user_message="Check the contact history before following up.",
        limit=8,
    )
    assert "relationship-ledger" not in {
        descriptor.slug for descriptor in no_ledger_descriptors
    }


@pytest.mark.asyncio
async def test_runtime_cannot_invoke_uninstalled_ledger_skill(db_session) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="No Ledger Runtime",
        settings={"ledger_contracts": []},
        status="active",
    )
    db_session.add(workspace)
    await seed_builtin_skills(db_session)
    await db_session.flush()

    result = await invoke_skill(
        db_session,
        "finance-ledger",
        entity_id,
        "Read the current finance ledger.",
        workspace_id=workspace.id,
    )

    assert result["code"] == "skill_workspace_contract_missing"


@pytest.mark.asyncio
async def test_installed_ledger_skill_only_returns_instructions(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Relationship Runtime",
        settings={"ledger_contracts": ["manor.relationship_ledger/v1"]},
        status="active",
    )
    db_session.add(workspace)
    await seed_builtin_skills(db_session)
    await db_session.flush()

    def forbidden_tool_surface(*_args, **_kwargs):
        raise AssertionError("instructions-only Skill must not prepare tools")

    async def forbidden_child_loop(**_kwargs):
        raise AssertionError("instructions-only Skill must not start a child Agent")

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_prepare_prompt_skill_tool_surface",
        forbidden_tool_surface,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_execute_skill_agent_loop",
        forbidden_child_loop,
    )

    result = await invoke_skill(
        db_session,
        "relationship-ledger",
        entity_id,
        "Check the contact before following up.",
        workspace_id=workspace.id,
        allowed_tool_names={
            "read_relationship_ledger",
            "record_relationship_ledger",
        },
    )

    assert result["instructions_only"] is True
    assert result["stop_reason"] == "instructions_loaded"
    assert result["rounds"] == 0
    assert result["tools_used"] == []
    assert "Contact policy semantics" in result["content"]


def test_ledger_skill_configs_are_valid_json() -> None:
    for slug in LEDGER_SKILLS:
        config = json.loads((ROOT / slug / "config.json").read_text(encoding="utf-8"))
        assert config["id"] == slug
