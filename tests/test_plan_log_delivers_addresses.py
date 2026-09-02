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
from urllib.parse import quote

import pytest

from packages.core.workspace_chat.notifiers import (
    _artifacts_as_attachments,
    _format_artifact,
    _render_dag,
    _render_plan_completion_summary,
    _render_step_completion_summary,
    _unique_artifacts,
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


def test_one_result_with_full_and_relative_paths_has_one_completion_file():
    name = "AI_SDE_20小时课程_学生教材版.md"
    path = f"Workspaces/W/final/{name}"
    artifacts = extract_artifacts_for_chat({
        "files": [{"name": name, "fs_path": path, "path": name}],
    })
    assert artifacts == [{"kind": "file", "value": path, "name": name}]
    summary = _render_plan_completion_summary(
        plan_id="plan_1", task_title="Write textbook", duration_seconds=1,
        cost_usd=None, steps=[_step(artifacts)], entity_id=ENTITY,
    )
    assert summary.count("File:") == 1
    assert len(_artifacts_as_attachments(artifacts, entity_id=ENTITY)) == 1


def test_completion_preserves_same_named_files_at_distinct_paths():
    artifacts = extract_artifacts_for_chat({"files": [
        {"fs_path": "Workspaces/W/a/report.md"},
        {"fs_path": "Workspaces/W/b/report.md"},
        {"fs_path": "Workspaces/W/a/REPORT.md"},
    ]})
    assert len(artifacts) == 3
    attachments = _artifacts_as_attachments(artifacts, entity_id=ENTITY)
    assert len(attachments) == 3
    assert len({item["previewUrl"] for item in attachments}) == 3


def test_completion_preserves_literal_path_characters_through_all_projections():
    paths = [f"Workspaces/W/{name}" for name in ["report#1.md", "report#2.md", "report%2520.md", "report?2.md"]]
    artifacts = extract_artifacts_for_chat({"files": [{"fs_path": path} for path in paths]})
    assert [item["value"] for item in artifacts] == paths
    attachments = _artifacts_as_attachments(artifacts, entity_id=ENTITY)
    assert [item["fsPath"] for item in attachments] == paths
    summary = _render_plan_completion_summary(
        plan_id="plan_1", task_title="Save reports", duration_seconds=1,
        cost_usd=None, steps=[_step(artifacts)], entity_id=ENTITY,
    )
    assert summary.count("File:") == len(paths)
    for path, attachment in zip(paths, attachments):
        address = f"/api/v1/fs/{ENTITY}/{quote(path)}"
        assert attachment["openUrl"] == address
        assert f"]({address})" in summary


@pytest.mark.parametrize("reverse", [False, True])
def test_completion_merges_viewer_url_with_known_document(reverse):
    files = [{"document_id": "doc_1", "name": "report.md"}, {"open_url": "/viewer/doc_1", "name": "report.md"}]
    artifacts = extract_artifacts_for_chat({"files": list(reversed(files)) if reverse else files})
    assert artifacts == [{"kind": "document", "value": "doc_1", "name": "report.md"}]
    assert len(_artifacts_as_attachments(artifacts)) == 1
    assert _render_dag([_step(artifacts)]).count("/viewer/doc_1") == 1


@pytest.mark.parametrize("field", ["previewUrl", "preview_url"])
def test_formal_output_address_does_not_emit_its_thumbnail_as_another_file(field):
    address = "https://cdn.example/report.pdf"
    artifacts = extract_artifacts_for_chat({"file_url": address, field: "https://cdn.example/cover.png", "name": "report.pdf"})
    assert artifacts == [{"kind": "url", "value": address, "name": "report.pdf"}]


def test_document_only_reference_does_not_hide_an_unrelated_same_named_file():
    artifacts = extract_artifacts_for_chat({
        "files": [{"fs_path": "Workspaces/W/other/report.md"}],
        "knowledge_artifacts": [{"document_id": "doc_report", "name": "report.md"}],
    })
    assert len(artifacts) == 2
    assert len(_artifacts_as_attachments(artifacts, entity_id=ENTITY)) == 2


def test_plan_summary_and_attachments_share_canonical_path_identity():
    artifacts = [
        {"kind": "file", "value": "Workspaces/W/final/report.md", "name": "report.md"},
        {"kind": "file", "value": "/Workspaces/W/final/./report.md", "name": "report.md"},
        {"kind": "url", "value": f"/api/v1/fs/{ENTITY}/Workspaces/W/final/report.md", "name": "report.md"},
    ]
    assert len(_unique_artifacts(artifacts)) == 1
    assert len(_artifacts_as_attachments(artifacts, entity_id=ENTITY)) == 1
    summary = _render_step_completion_summary(
        label="Write report", agent_part="", time_part="", summary=None,
        artifacts=artifacts, max_summary_chars=1000, entity_id=ENTITY,
    )
    assert summary.count("File:") == 1
    assert _render_dag([_step(artifacts)], entity_id=ENTITY).count("File:") == 1


@pytest.mark.parametrize("key", ["url", "previewUrl", "preview_url"])
def test_url_only_output_keeps_its_exact_address_without_entity_context(key):
    address = f"https://manor.example/api/v1/fs/{ENTITY}/Workspaces/W/report.md"
    artifacts = extract_artifacts_for_chat({key: address, "name": "report.md"})
    assert artifacts == [{"kind": "url", "value": address, "name": "report.md"}]
    assert f"]({address})" in _format_artifact(artifacts[0])
    assert _artifacts_as_attachments(artifacts)[0]["openUrl"] == address


@pytest.mark.parametrize("id_key", ["id", "document_id"])
def test_nested_document_keeps_its_path_identity(id_key):
    artifacts = extract_artifacts_for_chat({
        "files": [{"fs_path": "Workspaces/W/report.md"}],
        "document": {
            id_key: "doc_report", "fs_path": "Workspaces/W/report.md", "name": "report.md",
        },
    })
    assert artifacts == [{
        "kind": "document", "value": "doc_report", "name": "report.md",
        "fs_path": "Workspaces/W/report.md",
    }]


@pytest.mark.parametrize("id_key", ["id", "document_id"])
def test_nested_document_inherits_explicit_parent_path(id_key):
    path = "Workspaces/W/report.md"
    artifacts = extract_artifacts_for_chat({
        "fs_path": path, "name": "report.md",
        "document": {id_key: "doc_report", "name": "report.md"},
    })
    assert artifacts == [{"kind": "document", "value": "doc_report", "name": "report.md", "fs_path": path}]
    assert len(_artifacts_as_attachments(artifacts, entity_id=ENTITY)) == 1


def test_nested_document_does_not_inherit_another_documents_path_or_a_thumbnail():
    path = "Workspaces/W/report.md"
    for parent in [{"fs_path": path, "document_id": "doc_parent"}, {"previewUrl": f"/api/v1/fs/{ENTITY}/{path}"}]:
        artifacts = extract_artifacts_for_chat({**parent, "document": {"id": "doc_child", "name": "report.md"}})
        child = next(item for item in artifacts if item["value"] == "doc_child")
        assert "fs_path" not in child
        assert len(artifacts) == 2
    artifacts = extract_artifacts_for_chat({
        "fs_path": path, "document": {"id": "doc_child", "fs_path": "Workspaces/W/other.md"},
    })
    assert len(artifacts) == 2
    assert artifacts[0]["fs_path"] == "Workspaces/W/other.md"


def test_document_path_identity_survives_step_extraction_for_plan_dedupe():
    document = extract_artifacts_for_chat({
        "document_id": "doc_report", "fs_path": "Workspaces/W/report.md",
    })
    file = extract_artifacts_for_chat({"fs_path": "Workspaces/W/report.md"})
    artifacts = _unique_artifacts([*file, *document])
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "document"
    assert artifacts[0]["value"] == "doc_report"
    assert artifacts[0]["fs_path"] == "Workspaces/W/report.md"


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
        "fs_path": "Workspaces/W/final/report.md",
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
                "fs_path": "Workspaces/W/final/clip.mp4",
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
            "fsPath": "Workspaces/W/final/clip.mp4",
            "openUrl": "/viewer/01KYNERQ91YH35G3V7FA1M0J8F",
        }
    ]


def test_the_renderer_still_works_without_an_entity():
    rendered = _render_dag([_step([{"kind": "file", "value": "a/b.mp4"}])])
    assert "b.mp4" in rendered
