from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from auth_helpers import register_user_and_get_token
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
)
from packages.core.services.workspace_draft_service import _blueprint_agent_mappings
from packages.core.services.workspace_setup_service import (
    _custom_agent_design_cache_key,
)


@pytest.mark.asyncio
async def test_workspace_finalize_identity_conflict_returns_409(
    client: AsyncClient,
    monkeypatch,
):
    response = await register_user_and_get_token(client, json={
        "username": "workspace-identity-conflict",
        "email": "workspace-identity-conflict@test.com",
        "password": "TestPassword123!",
    })
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def raise_identity_conflict(*args, **kwargs):
        raise MarketplaceIdentityConflictError("conflicting Marketplace identity")

    monkeypatch.setattr(
        "apps.api.routers.workspace_drafts.draft_service.finalize_draft",
        raise_identity_conflict,
    )
    response = await client.post(
        f"/api/v1/workspace-drafts/{generate_ulid()}/finalize",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "conflicting Marketplace identity"


def test_custom_agent_design_cache_includes_exact_marketplace_identity():
    def mapping(marketplace_id: str) -> dict:
        return {
            "create_agent_draft": {
                "agent_name": "Research Agent",
                "system_prompt": "Research carefully.",
                "skill_binding_refs": [{
                    "marketplace_source": "platform",
                    "marketplace_id": marketplace_id,
                    "slug": "shared-display-slug",
                }],
                "source_blueprint_id": "blueprint-1",
                "source_blueprint_component_key": "research-agent",
            }
        }

    assert _custom_agent_design_cache_key(
        mapping("marketplace-skill-1")
    ) != _custom_agent_design_cache_key(mapping("marketplace-skill-2"))


async def test_blueprint_draft_resolves_external_agent_by_exact_marketplace_id(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    shared_slug = f"shared-agent-{entity_id}"
    local_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Local Collision",
        slug=shared_slug,
        system_prompt="Local implementation.",
        config={},
        is_public=False,
        status="active",
    )
    marketplace_agent = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=shared_slug,
        system_prompt="Marketplace implementation.",
        config={},
        is_template=True,
        is_public=True,
        status="active",
    )
    db_session.add_all([local_agent, marketplace_agent])
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        recipe={
            "subscriptions": [
                {
                    "service_key": "research",
                    "agent_slug": shared_slug,
                    "marketplace_agent_id": marketplace_agent.id,
                }
            ]
        },
        embedded={},
    )

    assert missing == []
    assert mappings[0]["strategy"] == "match"
    assert mappings[0]["recommended_agent_id"] == marketplace_agent.id


async def test_blueprint_draft_uses_exact_skill_refs_for_embedded_agent(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    shared_slug = "shared-skill"
    marketplace_skill_id = generate_ulid()
    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        source_blueprint_id="marketplace-blueprint-1",
        recipe={
            "subscriptions": [
                {"service_key": "research", "agent_slug": "research-agent"}
            ]
        },
        embedded={
            "agents": [
                {
                    "slug": "research-agent",
                    "name": "Research Agent",
                    "system_prompt": "Research carefully.",
                    "skill_bindings": [shared_slug, "embedded-only"],
                    "skill_binding_refs": [
                        {
                            "slug": shared_slug,
                            "marketplace_source": "platform",
                            "marketplace_id": marketplace_skill_id,
                        }
                    ],
                }
            ],
            "skills": [
                {
                    "slug": shared_slug,
                    "name": "Colliding Embedded Skill",
                    "system_prompt": "Do not create this copy.",
                },
                {
                    "slug": "embedded-only",
                    "name": "Embedded Only",
                    "system_prompt": "Create this Skill.",
                },
            ],
        },
    )

    assert missing == []
    create_draft = mappings[0]["create_agent_draft"]
    assert create_draft["skill_bindings"] == ["embedded-only"]
    assert create_draft["skill_binding_refs"] == [
        {
            "slug": shared_slug,
            "marketplace_source": "platform",
            "marketplace_id": marketplace_skill_id,
        }
    ]
    assert [item["slug"] for item in create_draft["missing_skill_specs"]] == [
        "embedded-only"
    ]
    assert create_draft["source_blueprint_id"] == "marketplace-blueprint-1"
    assert create_draft["source_blueprint_component_key"] == "research-agent"
    assert create_draft["missing_skill_specs"][0][
        "source_blueprint_component_key"
    ] == "embedded-only"


async def test_blueprint_draft_rejects_ambiguous_legacy_agent_slug(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    shared_slug = f"legacy-agent-{entity_id}"
    db_session.add_all([
        Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=user_id,
            name="Entity Agent",
            slug=shared_slug,
            system_prompt="Entity implementation.",
            config={},
            is_public=False,
            status="active",
        ),
        Agent(
            id=generate_ulid(),
            entity_id=None,
            name="Marketplace Agent",
            slug=shared_slug,
            system_prompt="Marketplace implementation.",
            config={},
            is_template=True,
            is_public=True,
            status="active",
        ),
    ])
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        user_id=user_id,
        recipe={
            "subscriptions": [
                {"service_key": "research", "agent_slug": shared_slug}
            ]
        },
        embedded={},
    )

    assert missing == [shared_slug]
    assert mappings[0]["strategy"] == "blueprint_external"
    assert mappings[0]["agent_id"] is None


async def test_blueprint_draft_canonicalizes_unique_legacy_marketplace_agent(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    source = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Legacy Marketplace Agent",
        slug=f"legacy-marketplace-agent-{entity_id}",
        system_prompt="Marketplace implementation.",
        is_template=True,
        is_public=True,
        status="active",
    )
    db_session.add(source)
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        recipe={
            "subscriptions": [{
                "service_key": "research",
                "agent_slug": source.slug,
            }]
        },
        embedded={},
    )

    assert missing == []
    assert mappings[0]["agent_id"] == source.id
    assert mappings[0]["marketplace_agent_id"] == source.id


async def test_blueprint_draft_rejects_non_marketplace_exact_agent_ids(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    local_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Local Agent",
        system_prompt="Local implementation.",
        status="active",
    )
    private_global = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Private Global Agent",
        system_prompt="Private platform implementation.",
        is_template=True,
        is_public=False,
        status="active",
    )
    db_session.add_all([local_agent, private_global])
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        recipe={
            "subscriptions": [
                {
                    "service_key": "local",
                    "marketplace_agent_id": local_agent.id,
                },
                {
                    "service_key": "private",
                    "marketplace_agent_id": private_global.id,
                },
            ]
        },
        embedded={},
    )

    assert missing == sorted([local_agent.id, private_global.id])
    assert [mapping["strategy"] for mapping in mappings] == [
        "blueprint_external",
        "blueprint_external",
    ]


async def test_blueprint_draft_legacy_slug_resolves_accessible_home_agent(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    foreign_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user_id,
        workspace_id=generate_ulid(),
        visibility="private",
        name="Workspace-home Agent",
        slug=f"workspace-private-agent-{entity_id}",
        system_prompt="Reuse this Agent when the caller can read it.",
        status="active",
    )
    db_session.add(foreign_agent)
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        user_id=user_id,
        recipe={
            "subscriptions": [{
                "service_key": "private",
                "agent_slug": foreign_agent.slug,
            }]
        },
        embedded={},
    )

    assert missing == []
    assert mappings[0]["strategy"] == "match"
    assert mappings[0]["agent_id"] == foreign_agent.id


async def test_blueprint_draft_legacy_slug_rejects_inaccessible_home_agent(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    foreign_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        workspace_id=generate_ulid(),
        visibility="private",
        name="Private Workspace-home Agent",
        slug=f"inaccessible-workspace-agent-{entity_id}",
        system_prompt="Do not reuse this Agent for another caller.",
        status="active",
    )
    db_session.add(foreign_agent)
    await db_session.commit()

    mappings, missing = await _blueprint_agent_mappings(
        db_session,
        entity_id=entity_id,
        user_id=generate_ulid(),
        recipe={
            "subscriptions": [{
                "service_key": "private",
                "agent_slug": foreign_agent.slug,
            }]
        },
        embedded={},
    )

    assert missing == [foreign_agent.slug]
    assert mappings[0]["strategy"] == "blueprint_external"
    assert mappings[0]["agent_id"] is None
