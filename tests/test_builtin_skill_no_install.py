from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.services.builtin_skill_loader import (
    _DEFAULT_SKILL_TOOLS,
    _skill_source_sha256,
    seed_builtin_skills,
)
from packages.core.services.marketplace_skill_service import (
    ensure_marketplace_skill_installed,
)
from packages.core.services.skill_service import invoke_skill, list_public_skills


def _builtin_skill() -> Skill:
    return Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Built-in XLSX",
        slug=f"xlsx-{generate_ulid()}",
        system_prompt="Use the bundled scripts.",
        tools=[],
        is_public=True,
        config={"source": "builtin", "type": "sandbox"},
        status="active",
    )


async def test_reseeding_restores_builtin_public_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    (tmp_path / "SKILL.md").write_text(
        "---\nname: public-test-skill\n---\nUse test.\n",
        encoding="utf-8",
    )
    existing = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="public-test-skill",
        slug="public-test-skill",
        display_name="public-test-skill",
        description="",
        system_prompt="Use test.",
        tools=list(_DEFAULT_SKILL_TOOLS),
        output_format="file",
        is_public=False,
        version="1.0.0",
        config={
            "source": "builtin",
            "skill_dir": str(tmp_path),
            "source_sha256": _skill_source_sha256(tmp_path),
        },
        status="active",
    )
    db = MagicMock()
    db.execute = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = existing
    db.flush = AsyncMock()
    monkeypatch.setattr(
        "packages.core.services.builtin_skill_loader._retire_removed_builtin_skills",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "packages.core.services.builtin_skill_loader._builtin_skill_dirs",
        lambda: [tmp_path],
    )

    assert await seed_builtin_skills(db) == [existing]
    assert existing.is_public is True


async def test_builtin_skills_are_not_in_the_marketplace_store():
    db = MagicMock()
    db.execute = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalars.return_value.all.return_value = []

    assert await list_public_skills(db) == []

    statement = db.execute.await_args.args[0]
    where_clause = str(statement.whereclause)
    assert "skills.entity_id IS NOT NULL" in where_clause
    assert "skills.is_public = true" in where_clause


async def test_any_user_can_invoke_builtin_without_install_or_agent_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    source = _builtin_skill()
    executed_skill_ids: list[str] = []

    async def fake_get_skill(db, skill_id):
        return source if skill_id == source.id else None

    async def fake_sandbox(skill: Skill, *args, **kwargs):
        executed_skill_ids.append(skill.id)
        return {"skill": skill.name, "content": "ok"}

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", fake_get_skill)
    monkeypatch.setattr(
        "packages.core.services.skill_service._invoke_sandbox_skill",
        fake_sandbox,
    )

    result = await invoke_skill(
        MagicMock(),
        source.id,
        generate_ulid(),
        "Create a spreadsheet",
        user_id=generate_ulid(),
    )

    assert result["content"] == "ok"
    assert executed_skill_ids == [source.id]


async def test_builtin_skill_is_used_without_entity_install(
    monkeypatch: pytest.MonkeyPatch,
):
    source = _builtin_skill()
    db = MagicMock()
    db.execute = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = source
    lifecycle_lock = AsyncMock()
    monkeypatch.setattr(
        "packages.core.services.marketplace_skill_service.lock_reusable_resource_lifecycle",
        lifecycle_lock,
    )

    resolved = await ensure_marketplace_skill_installed(
        db, entity_id=generate_ulid(), skill_id=source.id,
    )

    assert resolved is source
    db.add.assert_not_called()
    db.flush.assert_not_called()
    assert db.execute.await_count == 1


@pytest.mark.parametrize("include_platform", [False, True])
async def test_skill_list_projects_legacy_copy_bindings_onto_builtin(monkeypatch, include_platform):
    from apps.api.routers import skills as router
    from packages.core.services import builtin_plugin_service

    source = _builtin_skill()
    source.tools = []
    source.tags = []
    source.version = "1.0.0"
    source.output_format = "text"
    legacy = Skill(
        id=generate_ulid(), entity_id="entity", name=source.name, slug=source.slug,
        system_prompt="legacy", tools=[], tags=[], status="active", is_public=False,
        version="1.0.0", output_format="text",
        config={"source": "marketplace", "source_skill_id": source.id},
    )
    binding = {"binding_id": "historical-binding", "agent_id": "agent"}
    monkeypatch.setattr(router, "list_skills", AsyncMock(return_value=[legacy, source]))
    monkeypatch.setattr(router, "user_can_access_resource", AsyncMock(return_value=True))
    monkeypatch.setattr(router, "_binding_contexts_for_skills", AsyncMock(return_value={source.id: [binding]}))
    monkeypatch.setattr(builtin_plugin_service, "load_user_builtin_plugin_preferences", AsyncMock(return_value={}))

    rows = await router._skill_list_response(
        MagicMock(), SimpleNamespace(id="user", entity_id="entity", role="admin"),
        include_platform=include_platform,
    )

    assert [row.id for row in rows] == ([source.id] if include_platform else [])
    if include_platform:
        assert rows[0].bindings == [binding]


@pytest.mark.parametrize("can_view_copy", [False, True])
async def test_builtin_detail_includes_only_own_live_legacy_bindings(db_session, monkeypatch, can_view_copy):
    from apps.api.routers.skills import _binding_contexts_for_skills
    from packages.core.models.skill import AgentSkillBinding
    from packages.core.models.workspace import Agent

    source = _builtin_skill()
    db_session.add(source)
    expected_binding_id = None
    for entity_id, status in [("entity", "active"), ("other", "active"), ("entity", "inactive")]:
        legacy = Skill(
            id=generate_ulid(), entity_id=entity_id, name=source.name, slug=source.slug,
            system_prompt="legacy", status="active", is_public=False,
            config={"source": "marketplace", "source_skill_id": source.id},
        )
        agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Bound agent", status="active")
        binding = AgentSkillBinding(
            id=generate_ulid(), agent_id=agent.id, skill_id=legacy.id, status=status,
        )
        db_session.add_all([legacy, agent, binding])
        if entity_id == "entity" and status == "active":
            expected_binding_id = binding.id
    await db_session.flush()

    monkeypatch.setattr("apps.api.routers.skills.user_can_access_resource", AsyncMock(return_value=can_view_copy))
    contexts = await _binding_contexts_for_skills(
        db_session, [source], entity_id="entity",
        user=SimpleNamespace(id="user", role="admin"),
    )

    assert [row["binding_id"] for row in contexts[source.id]] == ([expected_binding_id] if can_view_copy else [])


@pytest.mark.parametrize("reference_kind", ["slug", "id"])
@pytest.mark.parametrize("bound_agent", [False, True])
@pytest.mark.parametrize("source_kind", ["builtin", "marketplace"])
async def test_legacy_copy_executes_builtin_source_only(
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
    bound_agent: bool,
    source_kind: str,
):
    entity_id = generate_ulid()
    source = _builtin_skill()
    source.config = {**source.config, "source": source_kind}
    legacy_copy = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name=source.name,
        slug=source.slug,
        system_prompt=source.system_prompt,
        tools=[],
        is_public=False,
        config={
            "source": "marketplace",
            "source_skill_id": source.id,
            "type": "sandbox",
        },
        status="active",
    )

    async def fake_get_skill(db, skill_id):
        return {source.id: source, legacy_copy.id: legacy_copy}.get(skill_id)

    async def fake_get_by_slug(db, slug, entity_id):
        return legacy_copy if slug == legacy_copy.slug else None

    executed_skill_ids: list[str] = []

    async def fake_sandbox(skill: Skill, *args, **kwargs):
        executed_skill_ids.append(skill.id)
        return {"skill": skill.name, "content": "ok"}

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", fake_get_skill)
    monkeypatch.setattr("packages.core.services.skill_service.get_skill_by_slug", fake_get_by_slug)
    monkeypatch.setattr(
        "packages.core.services.skill_service.list_skills_for_agent",
        AsyncMock(return_value=[legacy_copy]),
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.skills.runtime_skill_is_eligible",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        "packages.core.services.skill_service._invoke_sandbox_skill",
        fake_sandbox,
    )
    reference = legacy_copy.slug if reference_kind == "slug" else legacy_copy.id
    result = await invoke_skill(
        MagicMock(),
        reference,
        entity_id,
        "Create a spreadsheet",
        agent_id=generate_ulid() if bound_agent else None,
        enforce_agent_access=bound_agent,
    )

    assert result["content"] == "ok"
    assert executed_skill_ids == [
        source.id if source_kind == "builtin" else legacy_copy.id
    ]


async def test_legacy_copy_obeys_current_builtin_surface_policy(
    monkeypatch: pytest.MonkeyPatch,
):
    entity_id = generate_ulid()
    source = _builtin_skill()
    legacy_copy = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name=source.name,
        slug=source.slug,
        system_prompt=source.system_prompt,
        tools=[],
        is_public=False,
        config={"source": "marketplace", "source_skill_id": source.id},
        status="active",
    )

    async def fake_get_skill(db, skill_id):
        return {source.id: source, legacy_copy.id: legacy_copy}.get(skill_id)

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", fake_get_skill)
    monkeypatch.setattr(
        "packages.core.ai.runtime.skills.runtime_skill_is_eligible",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.skills.runtime_skill_allowed_on_surface",
        lambda skill, surface: skill.id != source.id,
    )
    result = await invoke_skill(
        MagicMock(),
        legacy_copy.id,
        entity_id,
        "Create a spreadsheet",
        runtime_envelope=SimpleNamespace(
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
            metadata={},
        ),
    )

    assert result["code"] == "skill_surface_not_allowed"


@pytest.mark.parametrize("surface", [ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.PUBLIC_CUSTOMER_CHAT])
@pytest.mark.parametrize("agent_id", [None, "agent"])
async def test_runtime_catalog_uses_current_builtin_for_legacy_copy(monkeypatch, surface, agent_id):
    from packages.core.ai.runtime import skills as runtime_skills
    from packages.core.services import skill_service

    source = _builtin_skill()
    source.config["allowed_surfaces"] = [surface.value]
    legacy = Skill(
        id=generate_ulid(), entity_id="entity", name=source.name, slug=source.slug,
        system_prompt="stale", tools=[], status="active", is_public=False,
        config={"source": "marketplace", "source_skill_id": source.id,
                "allowed_surfaces": [surface.value]},
    )
    rows = [legacy, source]
    monkeypatch.setattr(skill_service, "list_skills", AsyncMock(return_value=rows))
    monkeypatch.setattr(skill_service, "list_skills_for_agent", AsyncMock(return_value=rows))
    monkeypatch.setattr(runtime_skills, "runtime_agent_bound_skill_ids", AsyncMock(return_value={legacy.id} if agent_id else set()))

    descriptors = await runtime_skills.resolve_skill_descriptors(
        object(), entity_id="entity", agent_id=agent_id, workspace_id=None,
        surface=surface, invoke_skill_visible=True, limit=8,
    )

    expected = [] if surface == ChatSurface.PUBLIC_CUSTOMER_CHAT and not agent_id else [source.id]
    assert [item.id for item in descriptors] == expected


@pytest.mark.parametrize("matching_source", [False, True])
async def test_canonical_builtin_invocation_honors_legacy_public_agent_binding(monkeypatch, matching_source):
    from packages.core.services import skill_service

    source = _builtin_skill()
    source.config["allowed_surfaces"] = [ChatSurface.PUBLIC_CUSTOMER_CHAT.value]
    legacy = Skill(
        id=generate_ulid(), entity_id="entity", name=source.name, slug=source.slug,
        system_prompt="stale", tools=[], status="active", is_public=False,
        config={"source": "marketplace", "source_skill_id": source.id if matching_source else generate_ulid()},
    )
    monkeypatch.setattr(skill_service, "get_skill", AsyncMock(return_value=source))
    monkeypatch.setattr(skill_service, "list_agent_skill_bindings", AsyncMock(return_value=[legacy]))
    monkeypatch.setattr(skill_service, "list_skills_for_agent", AsyncMock(return_value=[legacy, source]))
    executor = AsyncMock(return_value={"skill": source.name, "content": "ok"})
    monkeypatch.setattr(skill_service, "_invoke_sandbox_skill", executor)

    result = await skill_service.invoke_skill(
        MagicMock(), source.id, "entity", "Use the bound skill", agent_id="agent",
        runtime_envelope=SimpleNamespace(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, metadata={}, allowed_tool_names=()),
    )

    if matching_source:
        assert result.get("content") == "ok"
        executor.assert_awaited_once()
    else:
        assert result["code"] == "skill_not_bound_to_agent"
        executor.assert_not_awaited()


def test_skill_source_sha256_reuses_digest_until_a_file_changes(tmp_path: Path, monkeypatch):
    import os

    (tmp_path / "SKILL.md").write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "run.py"
    script.write_text("print(1)\n", encoding="utf-8")

    first = _skill_source_sha256(tmp_path)

    reads: list[Path] = []
    original_read_bytes = Path.read_bytes

    def counting_read_bytes(self: Path) -> bytes:
        reads.append(self)
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    assert _skill_source_sha256(tmp_path) == first
    assert reads == []

    # Same size, bumped mtime: the bundle must be re-read and re-hashed.
    stat = script.stat()
    script.write_text("print(2)\n", encoding="utf-8")
    os.utime(script, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    changed = _skill_source_sha256(tmp_path)
    assert changed != first
    assert reads
