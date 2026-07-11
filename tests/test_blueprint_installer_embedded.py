"""Unit tests for the installer's embedded.skills/agents/knowledge_packs path.

Uses postgres via the ``db_session`` fixture from conftest — see the
top-of-file comment in test_blueprint_exporter_embedded.py for why we
moved off in-memory sqlite (shared MetaData mutation broke downstream
tests in the same pytest process).

Exercises:
  * embedded.skills create new Skill rows (entity-private)
  * embedded.agents create Agent + tool/MCP/skill bindings
  * idempotent re-install reuses existing (entity_id, slug) rows
  * missing ToolDefinition raises InstallError (fast-fail)
  * missing MCPServer becomes an InstallTodo (not an error)
  * governance preset never_allow blocks bound tools at install time
  * knowledge_pack creates a DocumentGroup; inline_text mode emits
    knowledge_pack_document todos
  * starter_memory rows land at the agent level (no user_id, no workspace_id)
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.installer import InstallError, InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.document import DocumentGroup
from packages.core.models.memory import AgentMemory
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentToolBinding,
    ToolDefinition,
)


@pytest.fixture
def entity_id() -> str:
    """Per-test unique entity_id (ULID = 26 chars, matches column width)."""
    return generate_ulid()


def _base_payload(**overrides) -> dict:
    """Minimal v1.1 payload. Caller can override at any nested path with
    dotted keys, e.g. ``_base_payload(**{"embedded.agents": [...]})``."""
    payload = {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Test",
            "kind": "social_media",
            "description": "T",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [],
            "agents": [],
            "knowledge_packs": [],
        },
        "recipe": {
            "operating_model": {},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }
    for k, v in overrides.items():
        sections = k.split(".")
        cursor = payload
        for s in sections[:-1]:
            cursor = cursor.setdefault(s, {})
        cursor[sections[-1]] = v
    return payload


# ── Skills ────────────────────────────────────────────────────────────


async def test_embedded_skill_creates_row(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"reply-tone-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [
                {
                    "slug": slug,
                    "name": "Reply Tone",
                    "system_prompt": "Reply nicely.",
                    "tools": [],
                    "is_public": False,
                    "version": "1.0.0",
                }
            ],
        }
    )
    await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()

    row = (await db_session.execute(select(Skill).where(Skill.entity_id == entity_id, Skill.slug == slug))).scalar_one()
    assert row.name == "Reply Tone"
    assert row.is_public is False  # embedded must be entity-private


async def test_embedded_skill_idempotent_reinstall(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"reply-tone-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [{"slug": slug, "name": "T", "system_prompt": "x", "tools": []}],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    rows = list(
        (await db_session.execute(select(Skill).where(Skill.entity_id == entity_id, Skill.slug == slug)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ── Agents ────────────────────────────────────────────────────────────


async def test_embedded_agent_creates_with_tool_bindings(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.post.{entity_id}"
    agent_slug = f"calvin-reply-{entity_id}"
    td = ToolDefinition(
        id=generate_ulid(),
        name=tool_name,
        display_name="X Post",
    )
    db_session.add(td)
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Calvin Reply",
                    "system_prompt": "Reply like Calvin.",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    agent = (
        await db_session.execute(select(Agent).where(Agent.entity_id == entity_id, Agent.slug == agent_slug))
    ).scalar_one()
    bindings = list(
        (await db_session.execute(select(AgentToolBinding).where(AgentToolBinding.agent_id == agent.id)))
        .scalars()
        .all()
    )
    assert agent.is_public is False
    assert agent.source == "blueprint"
    assert len(bindings) == 1
    assert bindings[0].tool_id == td.id


async def test_embedded_agent_missing_tool_raises_install_error(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.unknown.{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": f"calvin-reply-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    with pytest.raises(InstallError, match="not in this deployment"):
        await install_blueprint(db_session, entity_id=entity_id, payload=payload)


async def test_embedded_agent_missing_mcp_becomes_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": f"calvin-reply-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [
                        {
                            "server_slug": f"linear-mcp-{entity_id}",
                            "allowed_tools": ["linear.create_issue"],
                            "config_override_allowlist": ["team_id"],
                        }
                    ],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    mcp_todos = [t for t in result.todos if t.kind == "mcp_server"]
    assert len(mcp_todos) == 1
    assert f"linear-mcp-{entity_id}" in mcp_todos[0].detail
    assert mcp_todos[0].blocking is True


async def test_governance_blocks_embedded_agent_tool(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.delete_account.{entity_id}"
    db_session.add(
        ToolDefinition(
            id=generate_ulid(),
            name=tool_name,
            display_name="X Delete",
        )
    )
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": f"destroyer-{entity_id}",
                    "name": "D",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "policy.governance": {
                "never_allow_actions": ["x.delete_*"],  # matches!
                "max_risk_level": "medium",
            },
        }
    )
    with pytest.raises(InstallError, match="permanently blocked by governance"):
        await install_blueprint(
            db_session,
            entity_id=entity_id,
            payload=payload,
            governance_preset="standard",
        )


async def test_governance_safe_preset_passes_benign_tool(
    db_session: AsyncSession,
    entity_id: str,
):
    """Safe preset only adds wildcard HITL, doesn't expand never_allow —
    a benign tool binding survives. Sanity check on the preview path."""
    tool_name = f"tool.benign.read.{entity_id}"
    agent_slug = f"reader-{entity_id}"
    db_session.add(
        ToolDefinition(
            id=generate_ulid(),
            name=tool_name,
            display_name="Read",
        )
    )
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "R",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        governance_preset="safe",
    )
    await db_session.commit()
    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one_or_none()
    assert agent is not None
    assert result.governance_applied is True


# ── Skill binding resolution ──────────────────────────────────────────


async def test_agent_binds_to_just_installed_embedded_skill(
    db_session: AsyncSession,
    entity_id: str,
):
    skill_slug = f"reply-tone-{entity_id}"
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [
                {
                    "slug": skill_slug,
                    "name": "RT",
                    "system_prompt": "x",
                    "tools": [],
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [skill_slug],
                    "starter_memory": [],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    skill = (await db_session.execute(select(Skill).where(Skill.slug == skill_slug))).scalar_one()
    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one()
    bindings = list(
        (await db_session.execute(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id)))
        .scalars()
        .all()
    )
    assert len(bindings) == 1
    assert bindings[0].skill_id == skill.id


async def test_missing_skill_binding_becomes_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    missing_slug = f"nonexistent-skill-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": f"calvin-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [missing_slug],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    missing = [t for t in result.todos if t.kind == "missing_skill"]
    assert len(missing) == 1
    assert missing_slug in missing[0].detail


# ── Starter memory ────────────────────────────────────────────────────


async def test_starter_memory_creates_agent_level_row(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [
                        {
                            "memory_type": "instruction",
                            "scope": "guidance",
                            "content": "Reply within 1h.",
                            "importance": 8,
                            "confidence": 0.9,
                        }
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one()
    mems = list((await db_session.execute(select(AgentMemory).where(AgentMemory.agent_id == agent.id))).scalars().all())
    assert len(mems) == 1
    m = mems[0]
    assert m.content == "Reply within 1h."
    assert m.user_id is None
    assert m.workspace_id is None
    assert m.source == "blueprint"
    assert m.importance == 8


# ── Knowledge packs ───────────────────────────────────────────────────


async def test_knowledge_pack_creates_document_group(
    db_session: AsyncSession,
    entity_id: str,
):
    pack_slug = f"competitor-intel-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.knowledge_packs": [
                {
                    "slug": pack_slug,
                    "title": f"Competitor Intelligence {entity_id}",
                    "purpose": "background on top competitors",
                    "mode": "skeleton",
                    "folder_structure": [{"path": "competitors/", "description": "..."}],
                    "starter_documents": [],
                    "external_source": None,
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    groups = list(
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.entity_id == entity_id))).scalars().all()
    )
    assert len(groups) == 1
    g = groups[0]
    assert g.name == f"Competitor Intelligence {entity_id}"
    assert (g.settings or {}).get("mode") == "skeleton"
    assert not any(t.kind == "knowledge_pack_document" for t in result.todos)


async def test_knowledge_pack_inline_text_emits_todos(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "embedded.knowledge_packs": [
                {
                    "slug": f"voice-{entity_id}",
                    "title": f"Voice Guide {entity_id}",
                    "purpose": "...",
                    "mode": "inline_text",
                    "folder_structure": [],
                    "starter_documents": [
                        {"path": "voice.md", "body_md": "# Voice\n\nFounder-led."},
                        {"path": "examples.md", "body_md": "# Examples"},
                    ],
                    "external_source": None,
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    todos = [t for t in result.todos if t.kind == "knowledge_pack_document"]
    assert len(todos) == 2
    paths = [t.payload["path"] for t in todos]
    assert "voice.md" in paths
    assert "examples.md" in paths


# ── End-to-end roundtrip ──────────────────────────────────────────────


async def test_subscription_after_embedded_agent_resolves_locally(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "social.x.reply",
                    "agent_slug": agent_slug,
                    "custom_prompt": None,
                    "config": {},
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    assert len(result.subscription_ids) == 1
    missing = [t for t in result.todos if t.kind == "missing_agent"]
    assert len(missing) == 0
