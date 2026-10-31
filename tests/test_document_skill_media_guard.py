import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.ai.runtime.skills import document_skill_media_guard  # noqa: E402


def test_pptx_video_is_blocked():
    out = document_skill_media_guard("pptx", "generate_file", {"kind": "video"})
    assert out is not None
    assert "not allowed" in out
    assert "pptx" in out


def test_pptx_audio_is_blocked():
    assert document_skill_media_guard("pptx", "generate_file", {"kind": "audio"}) is not None


def test_docx_and_xlsx_video_blocked():
    assert document_skill_media_guard("docx", "generate_file", {"kind": "video"}) is not None
    assert document_skill_media_guard("xlsx", "generate_file", {"kind": "video"}) is not None


def test_pptx_image_is_allowed():
    # The deck legitimately needs images — only video/audio are blocked.
    assert document_skill_media_guard("pptx", "generate_file", {"kind": "image"}) is None


def test_pptx_presentation_is_allowed():
    assert document_skill_media_guard("pptx", "generate_file", {"kind": "presentation"}) is None


def test_other_tools_pass_through():
    assert document_skill_media_guard("pptx", "sandbox_exec", {"command": "ls"}) is None


def test_non_document_skill_can_make_video():
    # A general/media skill is unaffected by the guard.
    assert document_skill_media_guard("research", "generate_file", {"kind": "video"}) is None
    assert document_skill_media_guard(None, "generate_file", {"kind": "video"}) is None


def test_case_insensitive_slug_and_kind():
    assert document_skill_media_guard("PPTX", "generate_file", {"kind": "VIDEO"}) is not None


# ── Dispatch-level guard (the calling agent's view of the conversation sandbox) ──
import pytest  # noqa: E402

from packages.core.ai.tools.generate_file.tool import (  # noqa: E402
    _active_document_skill_media_guard,
)
from tests.test_sandbox_session_service import fake_cache  # noqa: E402,F401 - fixture


def _guard(binding_key="c1", agent_id="agent-1"):
    return _active_document_skill_media_guard(
        entity_id="ent1",
        user_id="user1",
        agent_id=agent_id,
        binding_key=binding_key,
        kind="video",
    )


async def _drive(skill_id, *, agent_id="agent-1", sandbox_id="sb1", user_id="user1", coexist=True):
    from packages.core.ai.runtime.sandbox import runtime_init_sandbox_binding, runtime_sandbox_scope

    scope = runtime_sandbox_scope(entity_id="ent1", user_id=user_id, binding_key="c1")
    await runtime_init_sandbox_binding(scope, sandbox_id, skill_id, agent_id=agent_id, coexist=coexist)


@pytest.mark.asyncio
async def test_dispatch_blocks_video_while_pptx_sandbox_active(fake_cache):
    await _drive("pptx")
    out = await _guard()
    assert out is not None
    assert "not allowed" in out


@pytest.mark.asyncio
async def test_dispatch_ignores_another_agents_document_sandbox(fake_cache):
    """A delegated sub-agent driving pptx in the shared sandbox does not
    constrain its parent, which only joined the sandbox for generic work."""

    await _drive("pptx", agent_id="agent-b")
    await _drive("", agent_id="agent-1")  # agent-1 uses the same sandbox, generically
    assert await _guard() is None
    assert await _guard(agent_id="agent-b") is not None


@pytest.mark.asyncio
async def test_dispatch_keeps_blocking_while_another_skill_loads_beside_it(fake_cache):
    """Skills coexist in the user's shared sandbox: another agent loading a
    media Skill beside pptx neither lifts agent-1's guard nor inherits it."""

    await _drive("pptx")
    await _drive("some_video_skill", agent_id="agent-b")
    assert await _guard() is not None
    assert await _guard(agent_id="agent-b") is None


@pytest.mark.asyncio
async def test_dispatch_stops_blocking_once_the_skill_is_swapped_out(fake_cache):
    await _drive("pptx")
    # A runner without Skill slots replaces the Skill it holds.
    await _drive("some_video_skill", agent_id="agent-b", coexist=False)
    assert await _guard() is None


@pytest.mark.asyncio
async def test_dispatch_ignores_another_users_document_sandbox(fake_cache):
    await _drive("pptx", user_id="user2")
    assert await _guard() is None


@pytest.mark.asyncio
async def test_dispatch_allows_video_for_non_document_sandbox(fake_cache):
    await _drive("some_video_skill")
    assert await _guard() is None


@pytest.mark.asyncio
async def test_dispatch_allows_when_no_active_sandbox(fake_cache):
    assert await _guard() is None
    assert await _guard(binding_key=None) is None
