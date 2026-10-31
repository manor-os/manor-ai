"""A Skill whose declared tools are all outside the caller's scope is refused before its model runs.

Without this, the child loop ran with zero usable tools and could only describe
work it never did (the E1/E8 eval failures).
"""
import json

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
        Entity(id=entity_id, name="Skill dependency test"),
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


def _model(monkeypatch):
    calls: list[str] = []

    async def completion(messages, tools, **_kwargs):
        calls.append(str(messages[0].get("content") or ""))
        return "Done.", None, {"total": 1}

    async def text_completion(messages, **_kwargs):
        calls.append(str(messages[0].get("content") or ""))
        return "Done.", {"total": 1}

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", completion,
    )
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", text_completion,
    )
    return calls


async def _skill(db_session, entity_id, slug, tools, **config):
    from packages.core.models.skill import Skill

    skill = Skill(
        id=generate_ulid(), entity_id=entity_id, name=slug, slug=slug,
        system_prompt=f"{slug} instructions.", is_public=False, status="active",
        tools=tools, config=config,
    )
    db_session.add(skill)
    await db_session.flush()
    return skill


@pytest.mark.asyncio
async def test_skill_with_no_executable_declared_tool_is_refused_without_a_model_call(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "file-writer", ["generate_file", "patch_file"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "file-writer", entity_id, "write it", user_id=user_id,
                                allowed_tool_names={"read_file", "invoke_skill"})

    assert result["code"] == "skill_dependencies_unavailable"
    assert result["missing_tools"] == ["generate_file", "patch_file"]
    assert "acquire_capabilities" in result["instruction"]
    assert calls == []


@pytest.mark.asyncio
async def test_refusal_for_an_mcp_skill_points_at_the_integration_setup(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "notion-writer", ["mcp__notion__search", "mcp__notion__create_page"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "notion-writer", entity_id, "add a page", user_id=user_id,
                                allowed_tool_names={"read_file"})

    assert result["code"] == "skill_dependencies_unavailable"
    assert result["setup_url"] == "/integrations?provider=notion"
    assert calls == []


@pytest.mark.asyncio
async def test_partially_executable_skill_still_runs(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "reader-writer", ["read_file", "generate_file"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "reader-writer", entity_id, "go", user_id=user_id,
                                allowed_tool_names={"read_file"})

    assert "code" not in result and result["content"] == "Done."
    assert len(calls) == 1
    # The run is not silently narrower: the child and its caller both see what was withheld.
    assert result["withheld_tools"] == ["generate_file"]
    assert "does not grant these tools this Skill declares: generate_file" in calls[0]


@pytest.mark.asyncio
async def test_tools_optional_skill_opts_out_of_the_refusal(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "advisor", ["generate_file"], runtime={"tools_optional": True})
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "advisor", entity_id, "advise", user_id=user_id,
                                allowed_tool_names={"read_file"})

    assert "code" not in result and result["content"] == "Done."
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_instructions_only_skill_still_returns_its_guide(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "guide-only", ["generate_file"], execution_mode="instructions_only")
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "guide-only", entity_id, "how?", user_id=user_id,
                                allowed_tool_names={"read_file"})

    assert "code" not in result
    assert "guide-only instructions." in json.dumps(result)
    assert calls == []


@pytest.mark.asyncio
async def test_unrestricted_parent_scope_is_not_refused(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "open-writer", ["generate_file"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "open-writer", entity_id, "go", user_id=user_id,
                                allowed_tool_names=None)

    assert "code" not in result
    assert len(calls) == 1
    assert "withheld_tools" not in result
    assert "does not grant" not in calls[0]


@pytest.mark.asyncio
async def test_runtime_action_returns_a_no_effect_preflight_refusal(db_session, monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state
    from packages.core.ai.runtime.skills import runtime_invoke_skill_action
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "file-writer", ["generate_file", "patch_file"])
    calls = _model(monkeypatch)
    context = SimpleNamespace(
        active_user_message="write it", agent_id=None, workspace_id=None, conversation_id=None,
        allowed_tool_names={"read_file", "invoke_skill"}, tool_profile=None, manual_skill_selected=False,
    )

    result = await runtime_invoke_skill_action(
        entity_id=entity_id, user_id=user_id, skill_id="file-writer", runtime_context=context,
    )

    assert isinstance(result, RuntimePreflightValidationError)
    payload = json.loads(result)
    assert payload["error"] == "skill_dependencies_unavailable"
    assert payload["details"]["missing_tools"] == ["generate_file", "patch_file"]
    assert payload["details"]["instruction"]
    assert runtime_tool_effect_state("invoke_skill", {"skill_id": "file-writer"}, result) is EffectState.NONE
    assert calls == []


@pytest.mark.asyncio
async def test_every_builtin_prompt_skill_with_declared_tools_is_refused_when_none_are_in_scope(
    db_session, monkeypatch,
):
    """Guard the class: a new built-in Skill cannot reintroduce a tool-less child run."""
    from sqlalchemy import select

    from packages.core.ai.runtime.skills import runtime_skill_work_tools
    from packages.core.models.skill import Skill
    from packages.core.services.skill_service import (
        INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE,
        _determine_skill_type,
        skill_tools_optional,
    )

    from pathlib import Path

    from packages.core.services.builtin_skill_loader import _read_skill_config

    entity_id, user_id = await _actor(db_session, monkeypatch)
    calls = _model(monkeypatch)
    rows = (await db_session.execute(select(Skill).where(Skill.entity_id.is_(None)))).scalars().all()
    checked = []
    for row in rows:
        config = row.config or {}
        source_config = _read_skill_config(Path(config["skill_dir"])) if config.get("skill_dir") else {}
        if config.get("source") == "builtin" and not source_config.get("tools"):
            # The loader may fill default parent tools; the Skill declared none.
            assert runtime_skill_work_tools(row) == (), row.slug
        if (
            config.get("source") != "builtin"
            or not runtime_skill_work_tools(row)
            or config.get("execution_mode") == INSTRUCTIONS_ONLY_SKILL_EXECUTION_MODE
            or _determine_skill_type(row) != "prompt"
            or skill_tools_optional(row)
        ):
            continue
        # A real restricted parent always holds invoke_skill and usually
        # search_tools; neither is the Skill's work.
        result = await invoke_skill(db_session, row.slug, entity_id, "do the task", user_id=user_id,
                                    allowed_tool_names={"invoke_skill", "search_tools"})
        assert result.get("code") == "skill_dependencies_unavailable", (row.slug, result)
        checked.append(row.slug)
    assert calls == []
    # The scan must actually cover the class, including the eval's MCP packs.
    assert {"file_engine", "mcp_notion"} <= set(checked)
    assert len(checked) >= 20


@pytest.mark.asyncio
async def test_builtin_config_carries_runtime_tools_optional(db_session, monkeypatch, tmp_path):
    from packages.core.services import builtin_skill_loader
    from packages.core.services.skill_service import get_skill_by_slug, skill_tools_optional

    entity_id, user_id = await _actor(db_session, monkeypatch)
    skill_dir = tmp_path / "optional-tools-guide"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: optional-tools-guide\ndescription: Advises, tools optional.\nversion: 1.0.0\n---\n\nAdvise.\n",
        encoding="utf-8",
    )
    (skill_dir / "config.json").write_text(json.dumps({
        "tools": ["generate_file"],
        "runtime": {"tools_optional": True, "terminal_tool_results": ["ignored"]},
    }), encoding="utf-8")
    monkeypatch.setattr(builtin_skill_loader, "_builtin_skill_dirs", lambda **_kw: [skill_dir])
    await builtin_skill_loader.seed_builtin_skills(db_session)
    await db_session.flush()

    row = await get_skill_by_slug(db_session, "optional-tools-guide", entity_id)
    assert row.config["runtime"] == {"tools_optional": True}
    assert skill_tools_optional(row)

    calls = _model(monkeypatch)
    result = await invoke_skill(db_session, "optional-tools-guide", entity_id, "advise", user_id=user_id,
                                allowed_tool_names={"invoke_skill", "search_tools"})
    assert "code" not in result and len(calls) == 1


@pytest.mark.parametrize("name,orchestration", [
    ("invoke_skill", True), ("search_tools", True), ("call_tool", True), ("request_input", True),
    ("delegate_to_agent", True), ("complete_task", True), ("list_skills", True),
    ("read_file", False), ("generate_file", False), ("workspace_search", False), ("mcp__chrome__status", False),
])
def test_orchestration_tools_are_not_a_skills_work(name, orchestration):
    from packages.core.ai.runtime.approval_classifier import runtime_is_orchestration_tool

    assert runtime_is_orchestration_tool(name) is orchestration


@pytest.mark.asyncio
async def test_orchestration_tools_in_scope_do_not_make_a_skill_executable(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "video-maker", ["invoke_skill", "search_tools", "generate_file"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "video-maker", entity_id, "make it", user_id=user_id,
                                allowed_tool_names={"invoke_skill", "search_tools"})

    assert result["code"] == "skill_dependencies_unavailable"
    assert result["missing_tools"] == ["generate_file"]
    assert calls == []


@pytest.mark.asyncio
async def test_skill_declaring_only_orchestration_tools_still_runs(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "router", ["invoke_skill"])
    calls = _model(monkeypatch)

    result = await invoke_skill(db_session, "router", entity_id, "route", user_id=user_id,
                                allowed_tool_names={"invoke_skill"})

    assert "code" not in result and len(calls) == 1


@pytest.mark.asyncio
async def test_skill_with_discoverable_work_tools_still_runs(db_session, monkeypatch):
    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(
        db_session, entity_id, "notion-helper", ["search_tools", "mcp__notion__search"],
        discoverable_provider_keys=["notion"], discoverable_tool_prefixes=["mcp__notion__"],
    )
    calls = _model(monkeypatch)
    allowed = {"invoke_skill", "search_tools", "mcp__notion__get_page"}
    assert "mcp__notion__search" not in allowed

    result = await invoke_skill(db_session, "notion-helper", entity_id, "find the page", user_id=user_id,
                                allowed_tool_names=allowed)

    assert "code" not in result and len(calls) == 1




def _agreement_envelope(entity_id, user_id, allowed, *, mcp_provider_scopes=()):
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    return RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=resolve_runtime_principal(
            surface=ChatSurface.GLOBAL_OWNER_CHAT, entity_id=entity_id, user_id=user_id,
        ),
        profile=RuntimeProfile.OWNER_COPILOT, entity_id=entity_id, user_id=user_id, agent_id="agent-1",
        tool_names=tuple(allowed), allowed_tool_names=tuple(allowed),
        mcp_provider_scopes=tuple(mcp_provider_scopes),
    )


async def _search_state(envelope, skill_row, monkeypatch, *, connected):
    from packages.core.ai.runtime.skills import descriptor_from_skill
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
    from packages.core.ai.runtime.tool_search import runtime_execute_search_tools_handler
    from packages.core.services import agent_permission_service

    async def usable(*_args, **_kwargs):
        return frozenset(connected)

    async def loader():
        return [descriptor_from_skill(skill_row, source="builtin" if skill_row.entity_id is None else "entity")]

    monkeypatch.setattr(agent_permission_service, "resolve_usable_mcp_providers", usable)
    names = list(runtime_registered_tool_names())
    payload = json.loads(await runtime_execute_search_tools_handler(
        arguments={"mode": "exact", "kinds": ["skill"], "exact_names": [skill_row.slug],
                   "_runtime_envelope_from_context": envelope},
        entity_id=envelope.entity_id, user_id=envelope.user_id,
        tool_schemas=[(name, {"type": "function", "function": {"name": name, "parameters": {}}}) for name in names],
        available_tool_names=names, skill_descriptor_loader=loader,
    ))
    [match] = [item for item in payload["matches"] if item.get("slug") == skill_row.slug]
    return match["availability"]


@pytest.mark.parametrize("case", ["discoverable_provider", "parent_provider_scope", "all_out_of_scope"])
@pytest.mark.asyncio
async def test_search_readiness_and_invoke_agree_on_whether_a_skill_can_run(db_session, monkeypatch, case):
    from sqlalchemy import select

    from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
    from packages.core.models.skill import Skill

    entity_id, user_id = await _actor(db_session, monkeypatch)
    scopes = ()
    if case == "discoverable_provider":
        row = await _skill(
            db_session, entity_id, "notion-helper", ["search_tools", "mcp__notion__search"],
            discoverable_provider_keys=["notion"], discoverable_tool_prefixes=["mcp__notion__"],
        )
        allowed = {"invoke_skill", "search_tools", "mcp__notion__get_page"}
        assert "mcp__notion__search" not in allowed
        expected_runs = True
    elif case == "parent_provider_scope":
        row = (await db_session.execute(
            select(Skill).where(Skill.slug == "mcp_notion", Skill.entity_id.is_(None))
        )).scalar_one()
        allowed = {"invoke_skill", "search_tools"}
        scopes = (RuntimeMCPProviderToolScope(provider="notion"),)
        expected_runs = True
    else:
        row = await _skill(db_session, entity_id, "file-writer", ["generate_file", "patch_file"])
        allowed = {"invoke_skill", "search_tools"}
        expected_runs = False
    envelope = _agreement_envelope(entity_id, user_id, allowed, mcp_provider_scopes=scopes)

    availability = await _search_state(envelope, row, monkeypatch, connected={"notion"})
    calls = _model(monkeypatch)
    result = await invoke_skill(db_session, row.slug, entity_id, "do it", user_id=user_id,
                                allowed_tool_names=set(allowed), runtime_envelope=envelope)

    invoked = result.get("code") != "skill_dependencies_unavailable"
    assert (availability["state"] == "invocable_under_policy") is invoked is expected_runs
    assert bool(calls) is expected_runs
    if not expected_runs:
        assert availability["state"] == "declared_tools_out_of_scope"
        assert availability["missing_tools"] == result["missing_tools"]


def _write_builtin(tmp_path, slug, config):
    skill_dir = tmp_path / slug
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {slug} test skill.\nversion: 1.0.0\n---\n\nDo it.\n",
        encoding="utf-8",
    )
    (skill_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return skill_dir


@pytest.mark.asyncio
async def test_loader_default_tools_are_not_a_declaration(db_session, monkeypatch, tmp_path):
    from packages.core.ai.runtime.skills import runtime_skill_work_tools
    from packages.core.services import builtin_skill_loader
    from packages.core.services.skill_service import get_skill_by_slug

    entity_id, user_id = await _actor(db_session, monkeypatch)
    skill_dir = _write_builtin(tmp_path, "repo-guide", {"type": "prompt"})
    monkeypatch.setattr(builtin_skill_loader, "_builtin_skill_dirs", lambda **_kw: [skill_dir])
    await builtin_skill_loader.seed_builtin_skills(db_session)
    await db_session.flush()

    row = await get_skill_by_slug(db_session, "repo-guide", entity_id)
    assert row.tools == builtin_skill_loader._DEFAULT_SKILL_TOOLS  # still provided to script Skills
    assert row.config["tools_defaulted"] is True
    assert runtime_skill_work_tools(row) == ()

    calls = _model(monkeypatch)
    result = await invoke_skill(db_session, "repo-guide", entity_id, "help", user_id=user_id,
                                allowed_tool_names={"invoke_skill", "search_tools"})
    assert result.get("code") != "skill_dependencies_unavailable"
    assert len(calls) == 1

    # An existing row seeded before the flag existed is updated on reseed.
    row.config = {key: value for key, value in row.config.items() if key != "tools_defaulted"}
    await db_session.flush()
    await builtin_skill_loader.seed_builtin_skills(db_session)
    await db_session.flush()
    row = await get_skill_by_slug(db_session, "repo-guide", entity_id)
    assert row.config["tools_defaulted"] is True


@pytest.mark.asyncio
async def test_search_view_of_a_defaulted_skill_agrees_with_invoke(db_session, monkeypatch, tmp_path):
    from packages.core.services import builtin_skill_loader
    from packages.core.services.skill_service import get_skill_by_slug

    entity_id, user_id = await _actor(db_session, monkeypatch)
    skill_dir = _write_builtin(tmp_path, "repo-guide-two", {"type": "prompt"})
    monkeypatch.setattr(builtin_skill_loader, "_builtin_skill_dirs", lambda **_kw: [skill_dir])
    await builtin_skill_loader.seed_builtin_skills(db_session)
    await db_session.flush()
    row = await get_skill_by_slug(db_session, "repo-guide-two", entity_id)
    envelope = _agreement_envelope(entity_id, user_id, {"invoke_skill", "search_tools"})

    availability = await _search_state(envelope, row, monkeypatch, connected=set())

    assert availability["state"] == "invocable_under_policy"
    assert "missing_tools" not in availability


@pytest.mark.asyncio
async def test_refusal_happens_before_the_prompt_and_bundle_are_loaded(db_session, monkeypatch):
    from packages.core.services import skill_file_storage, skill_service

    entity_id, user_id = await _actor(db_session, monkeypatch)
    await _skill(db_session, entity_id, "file-writer", ["generate_file", "patch_file"])
    loads = []

    def no_prompt_load(*_args, **_kwargs):
        loads.append("prompt")
        return None

    def no_bundle_load(*_args, **_kwargs):
        loads.append("bundle")
        return {}

    monkeypatch.setattr(skill_file_storage, "load_skill_prompt", no_prompt_load)
    monkeypatch.setattr(skill_service, "_load_prompt_skill_extra_files", no_bundle_load)

    result = await invoke_skill(db_session, "file-writer", entity_id, "write it", user_id=user_id,
                                allowed_tool_names={"invoke_skill", "search_tools"})

    assert result["code"] == "skill_dependencies_unavailable"
    assert loads == []
