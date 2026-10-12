"""Every generate_file kind must answer with a structured result.

The 2026-08-22 CRM task failed because kind=spreadsheet answered with free
text (a sandbox contract). This guard enumerates the kind enum so a new or
refactored kind cannot regress into prose.
"""

from __future__ import annotations

import json

import pytest

from packages.core.ai.tools import generate_file_tool
from packages.core.ai.tools.generate_file import audio as audio_kind
from packages.core.ai.tools.generate_file import common, office_common
from packages.core.ai.tools.generate_file import diagram as diagram_kind
from packages.core.ai.tools.generate_file import document as document_kind
from packages.core.ai.tools.generate_file import image as image_kind
from packages.core.ai.tools.generate_file import tool as tool_module
from packages.core.ai.tools.generate_file import video as video_kind
from packages.core.contracts.audio_generation import GenerateFileKind

_STRUCTURED_KEYS = ("fs_path", "document_id", "job_id", "error")
_OFFICE_KINDS = {
    GenerateFileKind.WORD_DOCUMENT,
    GenerateFileKind.PRESENTATION,
    GenerateFileKind.SPREADSHEET,
    GenerateFileKind.PDF,
}
_ALL_KINDS = [kind for kind in GenerateFileKind if kind is not GenerateFileKind.SEARCH]


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return None


@pytest.fixture
def structured_leaves(monkeypatch):
    from packages.core import database
    from packages.core.services import skill_service

    canned = json.dumps({"status": "completed", "fs_path": "Workspaces/demo/out.bin", "document_id": "doc-1"})

    async def canned_async(*_args, **_kwargs):
        return canned

    async def fake_scope(**kwargs):
        return kwargs.get("name") or "artifact"

    async def fake_list_skills(_db, _entity_id):
        return []

    async def fake_invoke(_db, skill, _entity_id, _input_text, **_kwargs):
        return {
            "skill": skill,
            "content": f"✓ Sandbox ready for skill {skill}",
            "usage": {},
            "tools_used": [],
            "rounds": 0,
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sb-1",
        }

    monkeypatch.setattr(image_kind, "runtime_generate_image_media", canned_async)
    monkeypatch.setattr(audio_kind, "runtime_generate_audio_media", canned_async)
    monkeypatch.setattr(video_kind, "runtime_generate_video_media", canned_async)
    monkeypatch.setattr(document_kind, "runtime_generate_document_file", canned_async)
    # ``code`` is stubbed at the dispatch target: handle_code needs the entity
    # filesystem projection transaction, so the real handler cannot run here.
    # This row therefore only proves the dispatcher round-trips a structured
    # result for kind=code; the handler's own contract is covered elsewhere.
    monkeypatch.setattr(tool_module, "handle_code", canned_async)
    monkeypatch.setattr(diagram_kind, "generate_diagram_file", canned_async)
    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)
    monkeypatch.setattr(office_common, "runtime_invoke_skill", fake_invoke)
    # The generate_file_tool shim re-points common._scope_workspace_output_name
    # to its own module global on every call, so patch the shim's name.
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)


def _kwargs_for(kind: GenerateFileKind) -> dict:
    kwargs = {"kind": kind.value, "prompt": "make the artifact", "name": f"artifact.{kind.value}"}
    if kind is GenerateFileKind.CODE:
        kwargs["files"] = [{"path": "index.html", "content": "<html></html>"}]
    if kind is GenerateFileKind.DOCUMENT:
        kwargs["content"] = "# hello"
        kwargs["name"] = "notes.md"
    return kwargs


def test_kind_enum_is_fully_enumerated() -> None:
    assert {kind.value for kind in _ALL_KINDS} == {
        "diagram",
        "code",
        "document",
        "word_document",
        "pdf",
        "presentation",
        "spreadsheet",
        "image",
        "video",
        "audio",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", (False, True), ids=("success", "error"))
async def test_compat_wrapper_restores_helper_overrides_after_dispatch(monkeypatch, raises) -> None:
    original_scope = common._scope_workspace_output_name
    original_invoke = office_common._invoke_builtin_skill

    async def fake_scope(**_kwargs):
        return "fake"

    async def fake_invoke(**_kwargs):
        return "fake"

    async def dispatch(**_kwargs):
        assert common._scope_workspace_output_name is fake_scope
        assert office_common._invoke_builtin_skill is fake_invoke
        if raises:
            raise RuntimeError("dispatch failed")
        return "handled"

    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)
    monkeypatch.setattr(generate_file_tool, "_invoke_builtin_skill", fake_invoke)
    monkeypatch.setattr(generate_file_tool, "_package_generate_file_handler", dispatch)

    if raises:
        with pytest.raises(RuntimeError, match="dispatch failed"):
            await generate_file_tool._generate_file_handler(kind="document")
    else:
        assert await generate_file_tool._generate_file_handler(kind="document") == "handled"

    assert common._scope_workspace_output_name is original_scope
    assert office_common._invoke_builtin_skill is original_invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _ALL_KINDS, ids=[kind.value for kind in _ALL_KINDS])
async def test_every_generate_file_kind_returns_a_structured_result(kind, structured_leaves) -> None:
    result = await generate_file_tool._generate_file_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        **_kwargs_for(kind),
    )

    assert isinstance(result, str), kind
    try:
        payload = json.loads(result)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"kind={kind.value} returned free text: {result[:200]!r}") from exc
    assert isinstance(payload, dict), (kind, payload)
    structured = any(key in payload for key in _STRUCTURED_KEYS) or payload.get("status") == "sandbox_ready"
    assert structured, (kind, payload)

    if kind in _OFFICE_KINDS:
        assert payload["status"] == "sandbox_ready"
        assert payload["sandbox_id"] == "sb-1"
        assert payload["loaded_tools"] == ["sandbox"]
