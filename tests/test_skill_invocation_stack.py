"""A nested invoke_skill cannot re-enter a running Skill, and a Skill may limit what it invokes."""
import json
from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.services.builtin_skill_loader import seed_builtin_skills
from packages.core.services.skill_service import invoke_skill


async def _actor(db_session, monkeypatch):
    from packages.core import database
    from packages.core.models.user import Entity, User, UserMembership

    await seed_builtin_skills(db_session)
    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Skill stack test"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
             password_hash="test", role="owner", status="active"),
        UserMembership(id=generate_ulid(), user_id=user_id, entity_id=entity_id,
                       role="owner", status="active", is_primary=True),
    ])
    await db_session.flush()

    class SharedSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(database, "async_session", SharedSession)
    return entity_id, user_id


def _script(monkeypatch, plans):
    """Each Skill loop (keyed by a marker in its system prompt) calls invoke_skill once, then stops."""
    results: dict[str, list] = {marker: [] for marker in plans}

    async def completion(messages, tools, **_kwargs):
        system = str(messages[0].get("content") or "")
        marker = next(marker for marker in plans if marker in system)
        if messages[-1].get("role") == "tool":
            results[marker].append(str(messages[-1]["content"]))
            return f"{marker} finished.", None, {"total": 1}
        target = plans[marker]
        if target is None:
            return f"{marker} finished.", None, {"total": 1}
        return "", [{"id": f"call-{marker}", "name": "invoke_skill",
                     "arguments": {"skill_id": target, "input": "go"}}], {"total": 1}

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", completion,
    )
    return results


def _prompt_skill(entity_id, slug, marker, **config):
    from packages.core.models.skill import Skill

    return Skill(
        id=generate_ulid(), entity_id=entity_id, name=slug, slug=slug, system_prompt=f"{marker} instructions.",
        is_public=False, status="active", tools=["invoke_skill"], config=config,
    )


@pytest.mark.asyncio
async def test_a_skill_cannot_invoke_itself_or_an_ancestor(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    outer = _prompt_skill(entity_id, "outer-skill", "OUTER")
    inner = _prompt_skill(entity_id, "inner-skill", "INNER")
    db_session.add_all([outer, inner])
    await db_session.flush()
    results = _script(monkeypatch, {"OUTER": "inner-skill", "INNER": "outer-skill"})

    await invoke_skill(db_session, "outer-skill", entity_id, "start", user_id=user_id,
                       allowed_tool_names={"invoke_skill"})

    [cycle] = results["INNER"]
    assert "skill_invocation_cycle" in cycle and "outer-skill" in cycle
    # The outer Skill saw its child finish normally.
    assert results["OUTER"] and "INNER finished." in results["OUTER"][0]

    results = _script(monkeypatch, {"OUTER": "outer-skill"})
    await invoke_skill(db_session, "outer-skill", entity_id, "start", user_id=user_id,
                       allowed_tool_names={"invoke_skill"})
    [self_call] = results["OUTER"]
    assert "skill_invocation_cycle" in self_call


@pytest.mark.asyncio
async def test_siblings_may_reuse_a_skill_after_it_returns(db_session, monkeypatch):
    from packages.core.services import skill_service

    entity_id, user_id = await _actor(db_session, monkeypatch)
    leaf = _prompt_skill(entity_id, "leaf-skill", "LEAF")
    db_session.add(leaf)
    await db_session.flush()
    _script(monkeypatch, {"LEAF": None})
    first = await invoke_skill(db_session, "leaf-skill", entity_id, "a", user_id=user_id,
                               allowed_tool_names={"invoke_skill"})
    second = await invoke_skill(db_session, "leaf-skill", entity_id, "b", user_id=user_id,
                                allowed_tool_names={"invoke_skill"})
    assert first["content"] == second["content"] == "LEAF finished."
    assert skill_service.running_skill_invocations() == ()


@pytest.mark.asyncio
async def test_invocable_skills_limits_what_a_skill_may_invoke(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    limited = _prompt_skill(entity_id, "limited-skill", "LIMITED", invocable_skills=["motion-design"])
    other = _prompt_skill(entity_id, "other-skill", "OTHER")
    db_session.add_all([limited, other])
    await db_session.flush()

    results = _script(monkeypatch, {"LIMITED": "other-skill", "OTHER": None})
    await invoke_skill(db_session, "limited-skill", entity_id, "start", user_id=user_id,
                       allowed_tool_names={"invoke_skill"})
    [refused] = results["LIMITED"]
    assert "skill_not_invocable" in refused and "motion-design" in refused
    assert results["OTHER"] == []

    results = _script(monkeypatch, {"LIMITED": "motion-design"})
    await invoke_skill(db_session, "limited-skill", entity_id, "start", user_id=user_id,
                       allowed_tool_names={"invoke_skill"})
    [guide] = results["LIMITED"]
    assert "# Motion Design" in guide


@pytest.mark.asyncio
@pytest.mark.parametrize("target,code", [
    ("video-production", "skill_not_invocable"),
    ("video-edit", "skill_invocation_cycle"),
])
async def test_video_edit_only_invokes_its_guidance_skills(db_session, monkeypatch, target, code):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    results = _script(monkeypatch, {"# Video Edit": target, "# Video Production": None})
    await invoke_skill(db_session, "video-edit", entity_id, "Build it.", user_id=user_id,
                       allowed_tool_names={"invoke_skill", "list_skills", "video_edit", "read_file"})
    [refused] = results["# Video Edit"]
    assert code in refused
    assert results["# Video Production"] == []


def test_builtin_video_edit_declares_its_invocable_guidance_skills():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
    config = json.loads((root / "video-edit" / "config.json").read_text(encoding="utf-8"))
    assert config["invocable_skills"] == ["motion-design", "video-type-playbooks"]
    for slug in config["invocable_skills"]:
        guide = json.loads((root / slug / "config.json").read_text(encoding="utf-8"))
        assert guide["execution_mode"] == "instructions_only", slug


@pytest.mark.asyncio
async def test_seeded_video_edit_row_carries_invocable_skills(db_session):
    from packages.core.services.skill_service import get_skill_by_slug

    await seed_builtin_skills(db_session)
    await db_session.flush()
    row = await get_skill_by_slug(db_session, "video-edit", generate_ulid())
    assert row.config["invocable_skills"] == ["motion-design", "video-type-playbooks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["skill_invocation_cycle", "skill_not_invocable"])
async def test_runtime_action_returns_preflight_error_for_a_refused_invocation(monkeypatch, code):
    from packages.core import database
    from packages.core.ai.runtime.skills import runtime_invoke_skill_action
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
    from packages.core.services import skill_service

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def get_skill(_db, _requested):
        return SimpleNamespace(id="01M36ABH8VXTSN9Q5CB6G59RHJ", slug="video-production", name="video-production",
                               status="active", entity_id=None, tools=[])

    async def fake_invoke(db, skill_id, entity_id, input_text, **kwargs):
        return {"error": "refused before running", "code": code}

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(skill_service, "get_skill", get_skill)
    monkeypatch.setattr(skill_service, "invoke_skill", fake_invoke)
    context = SimpleNamespace(
        active_user_message="", agent_id=None, workspace_id=None, conversation_id=None,
        allowed_tool_names={"invoke_skill"}, tool_profile=None, manual_skill_selected=False,
    )
    result = await runtime_invoke_skill_action(
        entity_id="entity", skill_id="video-production", runtime_context=context,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    assert "refused before running" in result
