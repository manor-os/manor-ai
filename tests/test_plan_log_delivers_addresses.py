"""A step that produced a file says where the file is.

A production plan finished and its task log read:

    ↳ File: `Workspaces/Faceless Stickman Video Studio (TABDWT)/…/scene-01-hook.png`
    ↳ File: `two_minute_rule_silent_subtitled.mp4` (Workspaces/…/final/two_minute_rule_silent_subtitled.mp4)
    ↳ File: `two_minute_rule_silent_subtitled.mp4`

Two defects in three lines.

The paths are in backticks — inline code. The UI only builds a file card
from a reference it can resolve, and a workspace-relative path is a name
with directories in it, not an address. So a real deliverable arrived as
grey monospace text that led nowhere.

And the MP4 is listed twice, because the extractor treated the file's
`name` as another kind of location. A name is not a location: it produced
a second entry for every file that also had a path, and that second entry
was the one that could never be opened.
"""
from __future__ import annotations

import re

import pytest

from packages.core.workspace_chat.notifiers import (
    _artifacts_as_attachments,
    _format_artifact,
    _render_dag,
    _render_plan_completion_summary,
    _render_step_completion_summary,
    extract_artifacts_for_chat,
)

ENTITY = "01KQDCA7E9E7G20HNYE51VJECQ"


def _step(artifacts):
    return {
        "key": "assemble_final_mp4",
        "description": "Assemble Final Mp4",
        "service_key": "stickman.production",
        "status": "done",
        "depends_on": [],
        "artifacts": artifacts,
    }


# ── One file, one line ────────────────────────────────────────────────


def test_a_name_is_not_another_location():
    """The production duplicate: fs_path and name both became `file`."""
    found = extract_artifacts_for_chat(
        {"files": [{"fs_path": "Workspaces/W/final/clip.mp4", "name": "clip.mp4"}]}
    )
    files = [a for a in found if a["kind"] == "file"]
    assert len(files) == 1, f"one file, one entry — got {files}"
    assert files[0]["value"] == "Workspaces/W/final/clip.mp4"
    assert files[0]["name"] == "clip.mp4", "the name survives as the label"


@pytest.mark.parametrize(
    "second",
    [
        {"path": "/Workspaces/W/final/clip.mp4"},          # leading slash
        {"fs_path": "Workspaces/W/final/clip.mp4/"},       # trailing slash
        {"output_path": "Workspaces/W/final/CLIP.MP4"},    # case
        {"file_path": "Workspaces\\W\\final\\clip.mp4"},          # separators
    ],
)
def test_the_same_file_spelled_differently_collapses(second):
    found = extract_artifacts_for_chat(
        {"files": [{"fs_path": "Workspaces/W/final/clip.mp4"}, second]}
    )
    assert len([a for a in found if a["kind"] == "file"]) == 1


def test_genuinely_different_files_both_survive():
    found = extract_artifacts_for_chat(
        {"files": [
            {"fs_path": "Workspaces/W/final/clip.mp4"},
            {"fs_path": "Workspaces/W/scenes/scene-01.png"},
        ]}
    )
    assert len([a for a in found if a["kind"] == "file"]) == 2


def test_a_knowledge_document_replaces_its_raw_filesystem_reference():
    found = extract_artifacts_for_chat(
        {
            "fs_path": "Workspaces/W/final/report.md",
            "document_id": "01KYNERQ91YH35G3V7FA1M0J8F",
            "viewer_url": "/viewer/01KYNERQ91YH35G3V7FA1M0J8F",
        }
    )

    assert found == [{
        "kind": "document",
        "value": "01KYNERQ91YH35G3V7FA1M0J8F",
        "name": "report.md",
    }]


def test_canonical_knowledge_artifacts_are_discovered():
    found = extract_artifacts_for_chat({
        "knowledge_artifacts": [{
            "name": "report.md",
            "fs_path": "Workspaces/W/final/report.md",
            "document_id": "01KYNERQ91YH35G3V7FA1M0J8F",
        }]
    })

    assert found[0]["kind"] == "document"
    assert found[0]["value"] == "01KYNERQ91YH35G3V7FA1M0J8F"


def test_multiple_knowledge_artifacts_hide_their_raw_path_duplicates():
    found = extract_artifacts_for_chat({
        "files": [
            {"name": "one.md", "fs_path": "Workspaces/W/one.md"},
            {"name": "two.md", "fs_path": "Workspaces/W/two.md"},
        ],
        "knowledge_artifacts": [
            {"name": "one.md", "document_id": "doc_one", "fs_path": "Workspaces/W/one.md"},
            {"name": "two.md", "document_id": "doc_two", "fs_path": "Workspaces/W/two.md"},
        ],
    })

    assert [(item["kind"], item["value"]) for item in found] == [
        ("document", "doc_one"),
        ("document", "doc_two"),
    ]


# ── The line is an address ────────────────────────────────────────────


def test_a_file_renders_as_a_resolvable_link():
    line = _format_artifact(
        {"kind": "file", "value": "Workspaces/W/final/clip.mp4", "name": "clip.mp4"},
        entity_id=ENTITY,
    )
    assert f"](/api/v1/fs/{ENTITY}/Workspaces/W/final/clip.mp4)" in line
    assert "`" not in line, "backticks make it inline code, which is not a link"


def test_a_path_with_spaces_and_parens_is_encoded():
    line = _format_artifact(
        {"kind": "file", "value": "Workspaces/Studio (TABDWT)/final/clip.mp4"},
        entity_id=ENTITY,
    )
    href = re.search(r"\]\((.+)\)$", line).group(1)
    assert " " not in href and "(" not in href, f"unescaped href: {href}"
    assert href.startswith(f"/api/v1/fs/{ENTITY}/")


def test_a_document_links_to_its_viewer_route():
    line = _format_artifact(
        {"kind": "document", "value": "01KYNERQ91YH35G3V7FA1M0J8F", "name": "final.mp4"},
    )
    assert "](/viewer/01KYNERQ91YH35G3V7FA1M0J8F)" in line


def test_a_url_artifact_links_to_itself():
    line = _format_artifact({"kind": "url", "value": "https://example.test/a.mp4"})
    assert "](https://example.test/a.mp4)" in line


def test_no_entity_means_plain_text_not_a_fake_link():
    """Better an honest name than a link that goes nowhere."""
    line = _format_artifact({"kind": "file", "value": "Workspaces/W/final/clip.mp4"})
    assert "](" not in line
    assert "`Workspaces/W/final/clip.mp4`" in line


def test_a_file_without_a_name_is_labelled_by_its_basename():
    line = _format_artifact(
        {"kind": "file", "value": "Workspaces/W/final/clip.mp4"}, entity_id=ENTITY,
    )
    assert "[clip.mp4](" in line


# ── End to end ────────────────────────────────────────────────────────


def test_the_rendered_plan_log_carries_openable_files():
    artifacts = extract_artifacts_for_chat(
        {"files": [
            {"fs_path": "Workspaces/W/final/clip.mp4", "name": "clip.mp4"},
            {"path": "/Workspaces/W/final/clip.mp4", "name": "clip.mp4"},
        ]}
    )
    rendered = _render_dag([_step(artifacts)], entity_id=ENTITY)
    assert rendered.count("File:") == 1, f"still duplicated:\n{rendered}"
    assert f"/api/v1/fs/{ENTITY}/" in rendered


def test_completion_summaries_carry_openable_file_addresses():
    artifacts = [
        {
            "kind": "file",
            "value": "Workspaces/W/final/clip.mp4",
            "name": "clip.mp4",
        }
    ]
    step_summary = _render_step_completion_summary(
        label="Render final clip",
        agent_part="",
        time_part="",
        summary="Rendered the final video.",
        artifacts=artifacts,
        max_summary_chars=1000,
        entity_id=ENTITY,
    )
    plan_summary = _render_plan_completion_summary(
        plan_id="plan_1",
        task_title="Render final clip",
        duration_seconds=12,
        cost_usd=None,
        steps=[_step(artifacts)],
        entity_id=ENTITY,
    )

    assert f"/api/v1/fs/{ENTITY}/Workspaces/W/final/clip.mp4" in step_summary
    assert f"/api/v1/fs/{ENTITY}/Workspaces/W/final/clip.mp4" in plan_summary


def test_completed_artifacts_share_the_structured_chat_attachment_contract():
    attachments = _artifacts_as_attachments(
        [
            {
                "kind": "file",
                "value": "Workspaces/W/final/clip.mp4",
                "name": "clip.mp4",
            },
            {
                "kind": "document",
                "value": "01KYNERQ91YH35G3V7FA1M0J8F",
                "name": "clip.mp4",
            },
        ],
        entity_id=ENTITY,
    )

    assert attachments == [
        {
            "name": "clip.mp4",
            "type": "knowledge",
            "fileType": "mp4",
            "previewUrl": f"/api/v1/fs/{ENTITY}/Workspaces/W/final/clip.mp4",
            "document_id": "01KYNERQ91YH35G3V7FA1M0J8F",
        }
    ]


def test_the_renderer_still_works_without_an_entity():
    rendered = _render_dag([_step([{"kind": "file", "value": "a/b.mp4"}])])
    assert "b.mp4" in rendered
