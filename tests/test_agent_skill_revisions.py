"""M11 completion — Agent / Skill integer config revisions.

Agents and skills carry two version fields with disjoint owners:

* ``version`` — the AUTHOR's human label ("1.0", "1.2.3"), bumped by hand
  for releases and used by blueprint ``min_version`` matching.
* ``revision`` — the machine-owned integer counter this module covers,
  bumped by ``packages.core.revisions.bump_revision`` on every
  behavior-affecting content change, with an ``automation_revisions``
  audit row (``target_kind`` = ``agent`` / ``skill``).

Covered here:
* update_agent bumps on system_prompt / config / status, not on
  name / description / tags, and not on same-value writes
* update_skill bumps on system_prompt / tools / input_schema /
  output_format / status, not on cosmetic-only or no-op writes
* assert_revision CAS works against the two new target kinds
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models.automation_revision import AutomationRevision
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.workspace import Agent
from packages.core.revisions import (
    StaleRevisionError,
    assert_revision,
    content_patch_for,
    AGENT_CONTENT_REVISION_FIELDS,
    SKILL_CONTENT_REVISION_FIELDS,
)
from packages.core.services.agent_service import update_agent
from packages.core.services.skill_service import update_skill


async def _audit_rows(db, target_id: str) -> list[AutomationRevision]:
    return list((await db.execute(
        select(AutomationRevision)
        .where(AutomationRevision.target_id == target_id)
        .order_by(AutomationRevision.revision.asc())
    )).scalars().all())


async def _mk_agent(db, entity_id: str) -> Agent:
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Rev Agent",
        slug=f"rev-agent-{generate_ulid()[:8]}",
        description="original description",
        system_prompt="You are helpful.",
        config={"model_role": "primary"},
        tags=["a"],
        status="active",
    )
    db.add(agent)
    await db.flush()
    return agent


async def _mk_skill(db, entity_id: str) -> Skill:
    skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Rev Skill",
        slug=f"rev-skill-{generate_ulid()[:8]}",
        description="original description",
        system_prompt="Do the thing.",
        tools=["web_search"],
        input_schema={"type": "object"},
        output_format="text",
        config={"k": 1},
        tags=["a"],
        status="active",
    )
    db.add(skill)
    await db.flush()
    return skill


# ── Agent ──────────────────────────────────────────────────────────

async def test_agent_defaults_to_revision_one(db_session):
    agent = await _mk_agent(db_session, generate_ulid())
    assert agent.revision == 1
    # The human label is untouched by the revision machinery.
    assert agent.version == "1.0"
    assert await _audit_rows(db_session, agent.id) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("system_prompt", "You are a rigorous analyst."),
        ("config", {"model_role": "deep"}),
        ("status", "paused"),
    ],
)
async def test_update_agent_bumps_on_behavior_field(db_session, field, value):
    entity_id = generate_ulid()
    agent = await _mk_agent(db_session, entity_id)

    updated = await update_agent(db_session, agent.id, entity_id, **{field: value})
    assert updated is not None
    assert updated.revision == 2

    rows = await _audit_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].target_kind == "agent"
    assert rows[0].revision == 2
    assert rows[0].entity_id == entity_id
    assert set(rows[0].patch) == {field}


async def test_update_agent_cosmetic_only_does_not_bump(db_session):
    entity_id = generate_ulid()
    agent = await _mk_agent(db_session, entity_id)

    updated = await update_agent(
        db_session, agent.id, entity_id,
        name="Renamed Agent",
        description="fresh copy",
        tags=["b", "c"],
        category="ops",
        avatar_url="https://example.test/a.png",
    )
    assert updated is not None
    assert updated.name == "Renamed Agent"
    assert updated.revision == 1
    assert await _audit_rows(db_session, agent.id) == []


async def test_update_agent_same_value_write_does_not_bump(db_session):
    entity_id = generate_ulid()
    agent = await _mk_agent(db_session, entity_id)

    updated = await update_agent(
        db_session, agent.id, entity_id,
        system_prompt="You are helpful.",       # identical
        config={"model_role": "primary"},        # identical
        status="active",                         # identical
    )
    assert updated is not None
    assert updated.revision == 1
    assert await _audit_rows(db_session, agent.id) == []


async def test_update_agent_mixed_patch_records_only_content_fields(db_session):
    entity_id = generate_ulid()
    agent = await _mk_agent(db_session, entity_id)

    updated = await update_agent(
        db_session, agent.id, entity_id,
        name="Cosmetic rename",
        system_prompt="Changed behavior.",
    )
    assert updated.revision == 2
    rows = await _audit_rows(db_session, agent.id)
    assert set(rows[0].patch) == {"system_prompt"}


# ── Skill ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "field,value",
    [
        ("system_prompt", "Do the OTHER thing."),
        ("tools", ["web_search", "generate_file"]),
        ("input_schema", {"type": "object", "required": ["q"]}),
        ("output_format", "file"),
        ("config", {"k": 2}),
        ("status", "archived"),
    ],
)
async def test_update_skill_bumps_on_behavior_field(db_session, field, value):
    entity_id = generate_ulid()
    skill = await _mk_skill(db_session, entity_id)
    assert skill.revision == 1

    updated = await update_skill(db_session, skill.id, entity_id, **{field: value})
    assert updated is not None
    assert updated.revision == 2

    rows = await _audit_rows(db_session, skill.id)
    assert len(rows) == 1
    assert rows[0].target_kind == "skill"
    assert rows[0].revision == 2
    assert set(rows[0].patch) == {field}


async def test_update_skill_cosmetic_only_does_not_bump(db_session):
    entity_id = generate_ulid()
    skill = await _mk_skill(db_session, entity_id)

    updated = await update_skill(
        db_session, skill.id, entity_id,
        display_name="Pretty Name",
        description="fresh copy",
        tags=["b"],
        category="research",
        version="9.9.9",  # the AUTHOR label — never a revision bump
    )
    assert updated is not None
    assert updated.version == "9.9.9"
    assert updated.revision == 1
    assert await _audit_rows(db_session, skill.id) == []


async def test_update_skill_same_value_write_does_not_bump(db_session):
    entity_id = generate_ulid()
    skill = await _mk_skill(db_session, entity_id)

    updated = await update_skill(
        db_session, skill.id, entity_id,
        system_prompt="Do the thing.",
        tools=["web_search"],
        input_schema={"type": "object"},
        output_format="text",
        status="active",
    )
    assert updated is not None
    assert updated.revision == 1
    assert await _audit_rows(db_session, skill.id) == []


async def test_repeated_skill_content_changes_stack_revisions(db_session):
    entity_id = generate_ulid()
    skill = await _mk_skill(db_session, entity_id)

    await update_skill(db_session, skill.id, entity_id, system_prompt="v2")
    await update_skill(db_session, skill.id, entity_id, tools=["a", "b"])
    # Third call repeats the second → no-op.
    await update_skill(db_session, skill.id, entity_id, tools=["a", "b"])

    assert skill.revision == 3
    rows = await _audit_rows(db_session, skill.id)
    assert [r.revision for r in rows] == [2, 3]
    assert [set(r.patch) for r in rows] == [{"system_prompt"}, {"tools"}]


# ── CAS against the new target kinds ───────────────────────────────

async def test_assert_revision_cas_for_agent_and_skill(db_session):
    entity_id = generate_ulid()
    agent = await _mk_agent(db_session, entity_id)
    skill = await _mk_skill(db_session, entity_id)

    await assert_revision(agent, None)  # no expectation → skip
    await assert_revision(agent, 1)
    await assert_revision(skill, 1)

    await update_agent(db_session, agent.id, entity_id, system_prompt="moved on")
    await update_skill(db_session, skill.id, entity_id, system_prompt="moved on")

    with pytest.raises(StaleRevisionError) as agent_exc:
        await assert_revision(agent, 1)
    assert agent_exc.value.target_kind == "agent"
    assert (agent_exc.value.expected, agent_exc.value.actual) == (1, 2)

    with pytest.raises(StaleRevisionError) as skill_exc:
        await assert_revision(skill, 1)
    assert skill_exc.value.target_kind == "skill"
    assert (skill_exc.value.expected, skill_exc.value.actual) == (1, 2)


# ── content_patch_for helper ───────────────────────────────────────

async def test_content_patch_for_filters_cosmetic_and_noop(db_session):
    agent = await _mk_agent(db_session, generate_ulid())
    patch = content_patch_for(
        agent,
        {
            "name": "new name",                  # cosmetic
            "system_prompt": "You are helpful.",  # unchanged
            "config": {"model_role": "deep"},     # changed
            "status": None,                       # not provided
        },
        AGENT_CONTENT_REVISION_FIELDS,
    )
    assert patch == {"config": {"model_role": "deep"}}

    skill = await _mk_skill(db_session, generate_ulid())
    # ARRAY columns compare element-wise, so a tuple of the same values
    # is still a no-op.
    assert content_patch_for(
        skill, {"tools": ("web_search",)}, SKILL_CONTENT_REVISION_FIELDS,
    ) == {}
