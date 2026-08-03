"""A file card is a promise that clicking opens the file.

A user typed:

    完全搜不到这个视频two_minute_start_method_openable.mp4

and the UI rendered it as a file card with a document icon — their own
complaint that a file was missing, turned into a button to open it. Clicking
fell through to a document search that found nothing and dumped them on the
Knowledge page.

The message carried nothing: attachments null, refs null, message_kind text.
The card came from a prose scanner that accepted any token ending in a known
extension. A NAME is not an ADDRESS: it may not exist, may be one of several,
and cannot be resolved without guessing.

Two halves, both pinned here:
  * the UI only builds a card from a reference it can open;
  * an agent that produces a file must state the address the tool returned,
    so real deliverables still get real cards.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FILE_REFERENCES = Path("apps/web/src/lib/fileReferences.ts")
CHAT_MARKDOWN = Path("apps/web/src/components/ChatMarkdown.tsx")


def _source(path: Path) -> str:
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


# ── The UI half ───────────────────────────────────────────────────────


def test_a_strict_openable_predicate_exists():
    source = _source(FILE_REFERENCES)
    assert "export function isOpenableFileReference(" in source


@pytest.mark.parametrize(
    "marker,why",
    [
        ("decodeFileReferenceHref", "an already-encoded manor-file: reference"),
        ("/viewer/", "a document route"),
        ("/api/v1/fs/", "an entity filesystem path"),
        ("https?", "an absolute URL"),
    ],
)
def test_the_predicate_accepts_addresses_not_names(marker, why):
    """Each accepted form is an address that resolves without guessing."""
    body = _source(FILE_REFERENCES).split("export function isOpenableFileReference(")[1]
    body = body.split("\nexport function ")[0]
    # Regex literals escape their slashes (/^\\/viewer\\/…/), so compare
    # against the unescaped form rather than the source text verbatim.
    assert marker in body.replace("\\", ""), f"the predicate should accept {why}"


def test_prose_scanning_requires_an_openable_reference():
    """Both linkify paths — inline code and plain prose — were turning bare
    filenames into links; that is what produced the card."""
    source = _source(FILE_REFERENCES)

    for fn_name in ("standaloneInlineFileReference", "linkifyPlainFileReferencesSegment"):
        body = source.split(f"function {fn_name}(")[1].split("\nfunction ")[0]
        assert "isOpenableFileReference" in body, (
            f"{fn_name} must gate on the strict predicate"
        )
        assert not re.search(r"!looksLikeFileReference\(", body), (
            f"{fn_name} still accepts a filename-shaped token"
        )


def test_the_card_component_is_gated_on_an_address():
    body = _source(CHAT_MARKDOWN)
    # The condition guarding the file card, i.e. the `if (...)` that precedes
    # the component and decides whether prose becomes a clickable card.
    gate = body.split("<InlineFileReferenceCard")[0]
    gate = gate[gate.rindex("enableFileCards &&"):]
    assert "isOpenableFileReference(targetReference)" in gate
    assert "looksLikeFileReference(label)" not in gate, (
        "link text ending in an extension is not an address"
    )


# ── The agent half ────────────────────────────────────────────────────


def test_agents_are_told_to_cite_the_returned_address():
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_artifact_reference_guidance,
    )

    guidance = runtime_artifact_reference_guidance(
        envelope=None,
        tool_names=["generate_video", "wait_media_jobs"],
        has_tools=True,
    )
    assert guidance
    assert "fs_path" in guidance
    assert "document_id" in guidance
    # The specific failure: inventing a path instead of quoting the tool's.
    assert "reconstruct" in guidance


def test_guidance_is_absent_when_the_turn_cannot_produce_files():
    from packages.core.ai.runtime.prompt_guidance import (
        runtime_artifact_reference_guidance,
    )

    assert runtime_artifact_reference_guidance(
        envelope=None, tool_names=["rag", "manor"], has_tools=True,
    ) is None
    assert runtime_artifact_reference_guidance(
        envelope=None, tool_names=["generate_video"], has_tools=False,
    ) is None


def test_the_guidance_section_is_wired_into_prompt_assembly():
    from packages.core.ai.runtime.prompt_sections import (
        runtime_prompt_section_names,
        runtime_prompt_section_renderers,
    )

    assert "artifact_reference_guidance" in runtime_prompt_section_names()
    assert "artifact_reference_guidance" in runtime_prompt_section_renderers()
