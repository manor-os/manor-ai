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


# ── Dispatch-level guard (uses the active sandbox conversation context) ──
import pytest  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from packages.core.ai.tools.generate_file.tool import (  # noqa: E402
    _active_document_skill_media_guard,
)


@pytest.mark.asyncio
async def test_dispatch_blocks_video_while_pptx_sandbox_active(monkeypatch):
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_load_sandbox_context",
        AsyncMock(return_value={"skill_id": "pptx", "sandbox_id": "sb1"}),
    )
    out = await _active_document_skill_media_guard("c1", "video")
    assert out is not None
    assert "not allowed" in out


@pytest.mark.asyncio
async def test_dispatch_allows_video_for_non_document_sandbox(monkeypatch):
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_load_sandbox_context",
        AsyncMock(return_value={"skill_id": "some_video_skill"}),
    )
    assert await _active_document_skill_media_guard("c1", "video") is None


@pytest.mark.asyncio
async def test_dispatch_allows_when_no_active_sandbox(monkeypatch):
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_load_sandbox_context",
        AsyncMock(return_value=None),
    )
    assert await _active_document_skill_media_guard("c1", "video") is None
    assert await _active_document_skill_media_guard(None, "video") is None
