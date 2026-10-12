"""Every file-producing tool reports its output as one FileRef.

Producers keep their legacy address keys (``result_url``/``url``/...) for one
release, but those are always the percent-encoded fs route, and the FileRef's
``open_url`` is the viewer route once a Document exists.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import document_actions
from packages.core.ai.tools import media_tools, recording_tools, video_edit_tools
from packages.core.ai.tools.generate_file import code as generate_file_code

ROOT = Path(__file__).resolve().parents[1]
ENTITY = "ent1"
REL = "Workspaces/w1/My Clip.mp4"
ENCODED = "/api/v1/fs/ent1/Workspaces/w1/My%20Clip.mp4"

PRODUCER_FILES = (
    "packages/core/ai/tools/media_tools.py",
    "packages/core/ai/tools/recording_tools.py",
    "packages/core/ai/tools/video_edit_tools.py",
    "packages/core/ai/tools/generate_file/code.py",
    "packages/core/ai/runtime/generated_files.py",
    "packages/core/ai/runtime/document_actions.py",
)


@pytest.mark.parametrize("rel_path", PRODUCER_FILES)
def test_producers_never_hand_build_fs_urls(rel_path: str) -> None:
    source = (ROOT / rel_path).read_text(encoding="utf-8")
    # f"/api/v1/fs/{entity}/{path}" is unencoded and breaks on spaces; markers
    # used to *parse* an incoming address end right after the entity segment.
    built = re.findall(r'f"/api/v1/fs/\{[^}]+\}/\{[^"]+"', source)
    assert built == [], built


def test_media_produced_file_uses_viewer_when_document_exists() -> None:
    ref = media_tools._produced_file(ENTITY, REL, document_id="doc1", size=12, mime="video/mp4")
    assert ref["open_url"] == "/viewer/doc1"
    assert ref["markdown_link"] == "[My Clip.mp4](/viewer/doc1)"
    assert ref["fs_path"] == REL
    assert ref["document_id"] == "doc1"
    assert ref["size"] == 12 and ref["mime"] == "video/mp4"


def test_media_produced_file_without_document_is_encoded_fs_url() -> None:
    ref = media_tools._produced_file(ENTITY, REL)
    assert ref["open_url"] == ENCODED
    assert ref["markdown_link"] == f"[My Clip.mp4]({ENCODED})"
    assert media_tools._fs_url(ENTITY, REL) == ENCODED


@pytest.mark.asyncio
async def test_recording_save_reports_file_ref(monkeypatch, tmp_path) -> None:
    target = tmp_path / "captions.srt"
    target.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

    async def register(**_kwargs):
        return "doc-srt"

    async def bind(**_kwargs):
        return None

    monkeypatch.setattr(media_tools, "runtime_write_entity_file_atomic", lambda *a, **k: str(target))
    monkeypatch.setattr(media_tools, "_register_file_artifact", register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", bind)
    ctx = dict(entity_id=ENTITY, user_id="u1", workspace_id=None, task_id=None,
               agent_id=None, conversation_id=None)
    rel = "Workspaces/w1/rec 1/captions.srt"
    receipt = await recording_tools._save(ctx, rel, "x", "captions")

    assert receipt["viewer_url"] == "/api/v1/fs/ent1/Workspaces/w1/rec%201/captions.srt"
    assert receipt["file"]["open_url"] == "/viewer/doc-srt"
    assert receipt["file"]["markdown_link"] == "[captions.srt](/viewer/doc-srt)"
    assert receipt["file"]["sha256"] == receipt["sha256"]
    assert receipt["file"]["mime"] == "application/x-subrip"


@pytest.mark.asyncio
async def test_video_edit_artifact_carries_file_ref(monkeypatch) -> None:
    from packages.core.ai.runtime import tool_adapters

    async def register(**_kwargs):
        return {
            "document_id": "doc-v",
            "name": "My Clip.mp4",
            "fs_path": REL,
            "url": f"/api/v1/fs/{ENTITY}/{REL}",
            "mime_type": "video/mp4",
            "size_bytes": 99,
        }

    monkeypatch.setattr(tool_adapters, "runtime_register_video_edit_artifact", register)
    artifact = await video_edit_tools._register_artifact(
        abs_path=Path("/x"), entity_root=Path("/"), entity_id=ENTITY,
        context=SimpleNamespace(), artifact_role="final", generation={},
    )
    assert artifact["url"] == ENCODED
    assert artifact["file"]["open_url"] == "/viewer/doc-v"
    assert artifact["file"]["markdown_link"] == "[My Clip.mp4](/viewer/doc-v)"
    assert artifact["file"]["size"] == 99


def test_code_files_items_are_enriched_with_file_ref_keys() -> None:
    keys = generate_file_code._file_ref_keys(ENTITY, "Workspaces/w1/site/index page.html", "doc-c")
    assert keys == {
        "name": "index page.html",
        "fs_path": "Workspaces/w1/site/index page.html",
        "document_id": "doc-c",
        "open_url": "/viewer/doc-c",
        "markdown_link": "[index page.html](/viewer/doc-c)",
    }


def test_document_payload_carries_open_url() -> None:
    doc = SimpleNamespace(id="doc-d", name="Report.docx", file_type="docx", file_size=5)
    data = document_actions.runtime_document_to_dict(doc)
    assert data["open_url"] == data["viewer_url"] == "/viewer/doc-d"
    assert data["markdown_link"] == "[Report.docx](/viewer/doc-d)"
    json.dumps(data)
