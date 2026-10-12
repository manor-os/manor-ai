"""An invoker reads an instructions-only Skill's bundled references via invoke_skill.

The caller's ``read_file`` only serves the caller's own bundle, so the
instructions-only Skill hands its references back through
``invoke_skill(bundle_file=...)`` instead.
"""
import json
from pathlib import Path

import pytest

from packages.core.models.base import generate_ulid
from packages.core.services.builtin_skill_loader import seed_builtin_skills
from packages.core.services.skill_service import invoke_skill

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
DETERMINISM = (SKILLS_ROOT / "motion-design" / "references" / "determinism.md").read_text(encoding="utf-8")


def _forbid_child_loop(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("instructions-only Skill must not start a child loop")

    monkeypatch.setattr("packages.core.ai.runtime.runtime_prepare_prompt_skill_tool_surface", forbidden)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", forbidden)


async def _invoke(db_session, slug, **kwargs):
    await seed_builtin_skills(db_session)
    await db_session.flush()
    return await invoke_skill(db_session, slug, generate_ulid(), "", **kwargs)


@pytest.mark.asyncio
async def test_reference_returns_the_bundled_file(db_session, monkeypatch):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(db_session, "motion-design", bundle_reference="references/determinism.md")

    assert result["instructions_only"] is True and result["rounds"] == 0
    payload = json.loads(result["content"])
    assert payload["bundle_file"] == "references/determinism.md"
    assert payload["skill"] == "motion-design"
    assert payload["content"] == DETERMINISM
    assert payload["char_offset"] == 0 and payload["next_char_offset"] is None
    assert payload["total_chars"] == len(DETERMINISM)


@pytest.mark.asyncio
async def test_reference_continues_from_char_offset(db_session, monkeypatch):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(
        db_session, "motion-design",
        bundle_reference="references/determinism.md", bundle_char_offset=1000,
    )
    payload = json.loads(result["content"])
    assert payload["char_offset"] == 1000
    assert payload["content"] == DETERMINISM[1000:]


@pytest.mark.asyncio
async def test_unknown_reference_lists_the_available_files(db_session, monkeypatch):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(db_session, "motion-design", bundle_reference="references/nope.md")

    assert result["code"] == "skill_reference_not_found"
    assert "references/nope.md" in result["error"]
    for name in ("determinism", "gsap-timelines", "threejs", "captions-and-audio", "transitions", "safe-zones"):
        assert f"references/{name}.md" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [
    "../motion-design/SKILL.md",
    "references/../../video-edit/SKILL.md",
    "references/../config.json",
])
async def test_reference_traversal_is_rejected(db_session, monkeypatch, reference):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(db_session, "motion-design", bundle_reference=reference)

    assert result["code"] in {"skill_reference_invalid", "skill_reference_not_found"}
    assert "content" not in result


@pytest.mark.asyncio
async def test_reference_is_refused_for_skills_that_run_their_own_loop(db_session, monkeypatch):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(db_session, "video-production", bundle_reference="references/ai-routing.md")

    assert result["code"] == "skill_reference_unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["motion-design", "video-type-playbooks"])
async def test_instructions_point_at_invoke_skill_bundle_file_and_fit_a_nested_tool_result(db_session, monkeypatch, slug):
    from packages.core.ai.agentic_loop import TOOL_RESULT_MAX_CHARS

    _forbid_child_loop(monkeypatch)
    result = await _invoke(db_session, slug)

    content = result["content"]
    assert "## Bundled Skill Files" in content
    assert "`bundle_file`" in content and "invoke_skill" in content
    assert "call `read_file`" not in content
    # A nested prompt-skill loop compacts every tool result to this budget; the
    # instructions must reach the model whole, not middle-truncated.
    assert len(content) <= TOOL_RESULT_MAX_CHARS


# ── Integration: a nested video-edit loop reads motion-design's reference ──


@pytest.mark.asyncio
async def test_nested_video_edit_loop_receives_motion_design_reference(db_session, monkeypatch):
    """Run the real video-edit prompt-skill loop with a scripted model.

    The model calls ``invoke_skill(skill="motion-design", bundle_file=...)``
    through video-edit's nested tool executor; the reference text must be in
    the tool message the model sees on its next round.
    """
    from packages.core import database
    from packages.core.models.user import Entity, User, UserMembership

    await seed_builtin_skills(db_session)
    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        Entity(id=entity_id, name="Motion reference test"),
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

    seen_messages: list[list[dict]] = []
    pages: list[dict] = []

    def reference_call(call_id, **extra):
        return (
            "",
            [{
                "id": call_id,
                "name": "invoke_skill",
                "arguments": {
                    "skill": "motion-design",
                    "input": "seek-safe randomness",
                    "bundle_file": "references/determinism.md",
                    **extra,
                },
            }],
            {"total": 1},
        )

    async def scripted_completion(messages, tools, **_kwargs):
        seen_messages.append([dict(m) for m in messages])
        if len(seen_messages) == 1:
            assert "invoke_skill" in {t["function"]["name"] for t in tools}
            # A wrong path must come back as a correctable error, not end the loop.
            return reference_call("call-bad-ref", bundle_file="references/determinism")
        if len(seen_messages) == 2:
            error = json.loads(str(messages[-1]["content"]))
            assert "references/determinism.md" in json.dumps(error), error
            return reference_call("call-ref-1")
        # What the model actually sees: the compacted tool message.
        pages.append(json.loads(str(messages[-1]["content"])))
        next_offset = pages[-1].get("next_char_offset")
        if next_offset is not None and len(pages) < 5:
            return reference_call(f"call-ref-{len(pages) + 1}", char_offset=next_offset)
        return "Applied the determinism guidance.", None, {"total": 1}

    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
        scripted_completion,
    )

    result = await invoke_skill(
        db_session, "video-edit", entity_id, "Animate the title with seeded particles.",
        user_id=user_id,
        allowed_tool_names={"invoke_skill", "list_skills", "video_edit", "read_file"},
    )

    assert pages, result
    assert all(page["bundle_file"] == "references/determinism.md" for page in pages)
    assert "mulberry32" in pages[0]["content"]
    # A nested loop compacts a long result to a clean prefix with a truthful
    # continuation offset; invoke_skill honours it, so the model can read the
    # whole file.
    assert "".join(page["content"] for page in pages) == DETERMINISM
    assert pages[-1]["next_char_offset"] is None
    # One delivery when the file fits the loop's first-delivery budget, pages
    # with honest offsets when it does not (paging: test_loop_tool_result_budget).
    from packages.core.ai.agentic_loop import LOOP_TOOL_RESULT_MAX_CHARS

    assert (len(pages) == 1) == (len(DETERMINISM) < LOOP_TOOL_RESULT_MAX_CHARS - 1_000)


# ── An empty reference is an omitted reference ──


@pytest.mark.asyncio
async def test_empty_reference_on_instructions_only_skill_returns_instructions(db_session, monkeypatch):
    _forbid_child_loop(monkeypatch)
    omitted = await _invoke(db_session, "motion-design")
    empty = await invoke_skill(db_session, "motion-design", generate_ulid(), "", bundle_reference="  ")

    assert empty["instructions_only"] is True
    assert empty["content"] == omitted["content"]
    assert empty["content"].startswith("# Motion Design")


@pytest.mark.asyncio
async def test_empty_reference_on_a_looping_skill_runs_it_normally(db_session, monkeypatch):
    from types import SimpleNamespace

    ran = []

    async def child_loop(**kwargs):
        ran.append(kwargs)
        return SimpleNamespace(
            content="child done", usage={}, tool_calls_made=[], rounds=1,
            stop_reason="completed", control={}, messages=[], tool_evidence={},
            error=None, error_detail=None,
        )

    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", child_loop)
    result = await _invoke(db_session, "video-production", bundle_reference="")

    assert "code" not in result, result
    assert result["content"] == "child done"
    assert len(ran) == 1


@pytest.mark.asyncio
async def test_handler_drops_an_empty_reference(monkeypatch):
    from packages.core.ai.tools import skill_tools

    calls = []

    async def action(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(skill_tools, "runtime_invoke_skill_action", action)
    for reference in (None, "", "   "):
        await skill_tools._invoke_skill_handler(
            entity_id="entity", skill="motion-design", input="x", bundle_file=reference, char_offset=5,
        )
    await skill_tools._invoke_skill_handler(
        entity_id="entity", skill="motion-design", input="x", bundle_file=" references/threejs.md ",
    )
    # No alias: the retired parameter name is not a bundle read.
    await skill_tools._invoke_skill_handler(
        entity_id="entity", skill="motion-design", input="x", reference="references/threejs.md",
    )

    assert all("bundle_file" not in call for call in calls[:3])
    assert calls[3]["bundle_file"] == "references/threejs.md"
    assert "bundle_file" not in calls[4]


@pytest.mark.asyncio
@pytest.mark.parametrize("char_offset", [None, "", -5, "-1"])
async def test_unset_empty_or_negative_char_offset_starts_at_zero(db_session, monkeypatch, char_offset):
    _forbid_child_loop(monkeypatch)
    result = await _invoke(
        db_session, "motion-design",
        bundle_reference="references/determinism.md", bundle_char_offset=char_offset,
    )
    payload = json.loads(result["content"])
    assert payload["char_offset"] == 0
    assert payload["content"] == DETERMINISM


# ── Authorization and the correctable-error carrier ──


@pytest.mark.asyncio
async def test_agent_not_bound_to_the_skill_gets_no_reference(db_session, monkeypatch):
    # Platform built-ins are available to every agent, so the binding gate is
    # exercised with an entity-private instructions-only skill.
    from packages.core.models.skill import AgentSkillBinding, Skill

    _forbid_child_loop(monkeypatch)
    entity_id, bound_agent, other_agent = generate_ulid(), generate_ulid(), generate_ulid()
    skill = Skill(
        id=generate_ulid(), entity_id=entity_id, name="private-guide", slug="private-guide",
        system_prompt="Private guidance.", is_public=False, status="active",
        config={"execution_mode": "instructions_only", "extra_files": {"references/a.md": "private body"}},
    )
    db_session.add_all([
        skill,
        AgentSkillBinding(id=generate_ulid(), agent_id=bound_agent, skill_id=skill.id, status="active"),
    ])
    await db_session.flush()

    async def read(agent_id):
        return await invoke_skill(
            db_session, skill.id, entity_id, "", agent_id=agent_id, enforce_agent_access=True,
            bundle_reference="references/a.md",
        )

    denied = await read(other_agent)
    assert denied["code"] == "skill_not_allowed"
    assert "private body" not in json.dumps(denied)

    allowed = await read(bound_agent)
    assert json.loads(allowed["content"])["content"] == "private body"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["skill_reference_not_found", "skill_reference_invalid", "skill_reference_unsupported"])
async def test_runtime_action_returns_preflight_error_for_a_rejected_reference(monkeypatch, code):
    from types import SimpleNamespace

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
        return SimpleNamespace(
            id="01M36ABH8VXTSN9Q5CB6G59RHJ", slug="motion-design", name="motion-design",
            status="active", entity_id=None, tools=[],
        )

    seen = []

    async def fake_invoke(db, skill_id, entity_id, input_text, **kwargs):
        seen.append(kwargs)
        return {"error": "Skill 'motion-design' has no bundled file 'x'.", "code": code}

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(skill_service, "get_skill", get_skill)
    monkeypatch.setattr(skill_service, "invoke_skill", fake_invoke)
    context = SimpleNamespace(
        active_user_message="", agent_id=None, workspace_id=None, conversation_id=None,
        allowed_tool_names={"invoke_skill"}, tool_profile=None, manual_skill_selected=False,
    )

    result = await runtime_invoke_skill_action(
        entity_id="entity", skill_id="motion-design", runtime_context=context,
        bundle_file="x", bundle_char_offset=None,
    )

    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["error"].startswith("Skill 'motion-design' has no bundled file")
    assert seen[0]["bundle_reference"] == "x"

    # Without a reference, the same failure code is an ordinary failed result.
    plain = await runtime_invoke_skill_action(
        entity_id="entity", skill_id="motion-design", runtime_context=context,
    )
    assert not isinstance(plain, RuntimePreflightValidationError)
    assert "bundle_reference" not in seen[1]
