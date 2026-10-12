from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from packages.core.ai.runtime.integration_skill_registry import (
    AUXILIARY_INTEGRATION_SKILL_ROUTES,
    integration_skill_route_for_message,
)
from packages.core.services.builtin_plugin_service import (
    BUILTIN_PLUGINS,
    builtin_plugin_definition,
    builtin_plugin_key_for_skill,
    normalize_builtin_plugin_preferences,
)
from packages.core.services.builtin_skill_loader import (
    _parse_frontmatter,
    _read_skill_config,
    seed_builtin_skills,
)


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "packages" / "core" / "ai" / "skills"
REMOTION_SKILL_SLUGS = tuple(
    skill.slug for skill in BUILTIN_PLUGINS["remotion"].skills
)


def test_remotion_plugin_contains_the_twelve_official_skill_adapters() -> None:
    assert REMOTION_SKILL_SLUGS == (
        "remotion-best-practices",
        "remotion-captions",
        "remotion-create",
        "remotion-docs",
        "remotion-interactivity",
        "remotion-maps",
        "remotion-markup",
        "remotion-multimedia",
        "remotion-render",
        "remotion-saas",
        "remotion-studio",
        "remotion-upgrade",
    )

    for slug in REMOTION_SKILL_SLUGS:
        skill_dir = SKILLS_ROOT / slug
        frontmatter, body = _parse_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        )
        config = _read_skill_config(skill_dir)
        package = json.loads(
            (skill_dir / "package.json").read_text(encoding="utf-8")
        )

        assert frontmatter["name"] == slug
        assert frontmatter["version"] == "4.0.506"
        assert body.strip()
        assert config["type"] == "sandbox"
        assert config["plugin_key"] == "remotion"
        assert config["category"] == "video"
        assert package["dependencies"]["@remotion/cli"] == frontmatter["version"]
        assert package["dependencies"]["remotion"] == frontmatter["version"]


def test_only_optional_builtin_plugins_have_user_preferences() -> None:
    preferences = normalize_builtin_plugin_preferences({})

    assert preferences == {"remotion": False}
    assert normalize_builtin_plugin_preferences(
        {"builtin_plugins": {"manor_calendar": False}}
    ) == {"remotion": False}


def test_remotion_plugin_declares_render_capable_sandbox_resources() -> None:
    definition = builtin_plugin_definition("remotion")

    assert definition is not None
    assert definition.sandbox_config == {
        "memory": "2g",
        "workdir_tmpfs_size": "1g",
        "pids_limit": 512,
    }
    assert (
        builtin_plugin_key_for_skill(
            type(
                "SkillStub",
                (),
                {
                    "config": {"plugin_key": "remotion"},
                    "slug": "remotion-create",
                    "name": "Remotion Create",
                },
            )()
        )
        == "remotion"
    )


def test_explicit_remotion_requests_route_to_the_plugin_entry_skill() -> None:
    route = integration_skill_route_for_message(
        "Use @remotion to create an animated React video"
    )

    assert route is AUXILIARY_INTEGRATION_SKILL_ROUTES["remotion"]
    assert route.child_skill == "remotion-best-practices"
    assert route.parent_skill is None
    assert route.status == "auxiliary"


@pytest.mark.asyncio
async def test_remotion_builtin_rows_keep_their_sandbox_type(db_session) -> None:
    from packages.core.services.skill_service import _determine_skill_type

    seeded = {
        skill.slug: skill for skill in await seed_builtin_skills(db_session)
    }

    for slug in REMOTION_SKILL_SLUGS:
        assert seeded[slug].config["type"] == "sandbox"
        assert _determine_skill_type(seeded[slug]) == "sandbox"


def test_remotion_integration_card_uses_the_user_plugin_contract() -> None:
    backend = (
        ROOT / "apps" / "api" / "routers" / "integrations.py"
    ).read_text(encoding="utf-8")
    frontend = (
        ROOT / "apps" / "web" / "src" / "pages" / "Integrations.tsx"
    ).read_text(encoding="utf-8")

    assert 'server_key="remotion"' in backend
    assert 'auth_type="skill_plugin"' in backend
    assert 'plugin_key="remotion"' in backend
    assert "server.plugin_skills?.map" in frontend
    assert "setBuiltinPluginEnabled" in frontend
    assert "!server.plugin_enabled" in frontend
    assert "<InfoAlert>{server.setup_hint}</InfoAlert>" not in frontend


async def _register_owner(
    client: AsyncClient,
    username: str,
) -> tuple[dict[str, str], str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "securepass123",
            "entity_name": f"{username} Co",
        },
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, response.json()["user_id"], me.json()["entity_id"]


@pytest.mark.asyncio
async def test_user_can_enable_remotion_while_calendar_stays_internal(
    client: AsyncClient,
) -> None:
    headers, user_id, entity_id = await _register_owner(
        client,
        "builtin_plugin_toggle",
    )

    initial = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )
    assert initial.status_code == 200, initial.text
    catalog = {row["server_key"]: row for row in initial.json()}
    assert catalog["remotion"]["plugin_enabled"] is False
    assert catalog["remotion"]["agent_can_use"] is False
    assert len(catalog["remotion"]["plugin_skills"]) == 12
    assert all(row["installed"] for row in catalog["remotion"]["plugin_skills"])
    assert "manor_mcp_calendar" not in catalog

    initial_skills = await client.get(
        "/api/v1/skills?include_platform=true",
        headers=headers,
    )
    assert initial_skills.status_code == 200, initial_skills.text
    assert not {
        row["slug"] for row in initial_skills.json()
    }.intersection(REMOTION_SKILL_SLUGS)

    import packages.core.database as dbmod
    from packages.core.ai.runtime.skills import runtime_skill_is_eligible
    from packages.core.services.skill_service import get_skill_by_slug

    async with dbmod.async_session() as db:
        remotion_skill = await get_skill_by_slug(
            db,
            "remotion-best-practices",
            entity_id,
        )
        assert remotion_skill is not None
        eligible, reason = await runtime_skill_is_eligible(
            db,
            remotion_skill,
            entity_id=entity_id,
            workspace_id=None,
            user_id=user_id,
        )
    assert eligible is False
    assert reason == "skill_plugin_disabled"

    enabled = await client.put(
        "/api/v1/integrations/plugins/remotion",
        headers=headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json() == {"plugin_key": "remotion", "enabled": True}

    calendar_toggle = await client.put(
        "/api/v1/integrations/plugins/manor_calendar",
        headers=headers,
        json={"enabled": False},
    )
    assert calendar_toggle.status_code == 404

    updated = await client.get(
        "/api/v1/integrations/mcp-servers",
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    catalog = {row["server_key"]: row for row in updated.json()}
    assert catalog["remotion"]["agent_can_use"] is True
    assert "manor_mcp_calendar" not in catalog

    enabled_skills = await client.get(
        "/api/v1/skills?include_platform=true",
        headers=headers,
    )
    assert enabled_skills.status_code == 200, enabled_skills.text
    assert {
        row["slug"] for row in enabled_skills.json()
    }.intersection(REMOTION_SKILL_SLUGS) == set(REMOTION_SKILL_SLUGS)

    from packages.core.services.agent_permission_service import can_use_integration

    async with dbmod.async_session() as db:
        calendar_decision = await can_use_integration(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider="manor_mcp_calendar",
            allow_env_fallback=False,
        )
        remotion_skill = await get_skill_by_slug(
            db,
            "remotion-best-practices",
            entity_id,
        )
        assert remotion_skill is not None
        remotion_eligible, _ = await runtime_skill_is_eligible(
            db,
            remotion_skill,
            entity_id=entity_id,
            workspace_id=None,
            user_id=user_id,
        )
    assert calendar_decision.allowed is True
    assert remotion_eligible is True
