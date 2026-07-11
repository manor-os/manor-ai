import json

from packages.core.services.skill_service import (
    _append_skill_bundle_manifest,
    _try_list_skill_bundle_files,
    _try_read_skill_bundle_file,
)


def test_skill_bundle_manifest_and_file_tools():
    extra_files = {
        "references/draft-bundle-schema.md": "schema body",
        "references/platforms.md": "platform body",
        "examples/sample.md": "example body",
    }

    prompt = _append_skill_bundle_manifest("Use references.", extra_files)
    assert "Bundled Skill Files" in prompt
    assert "references/draft-bundle-schema.md" in prompt

    read_payload = json.loads(
        _try_read_skill_bundle_file(
            extra_files,
            {"path": "references/draft-bundle-schema.md"},
        )
    )
    assert read_payload["source"] == "skill_bundle"
    assert read_payload["content"] == "schema body"

    list_payload = json.loads(
        _try_list_skill_bundle_files(
            extra_files,
            {"path": "references"},
        )
    )
    assert {entry["path"] for entry in list_payload["entries"]} == {
        "references/draft-bundle-schema.md",
        "references/platforms.md",
    }


def test_skill_bundle_file_tools_fall_back_outside_bundle_namespace():
    extra_files = {"references/draft-bundle-schema.md": "schema body"}

    assert (
        _try_read_skill_bundle_file(
            extra_files,
            {"path": "Uploads/demo/post.json"},
        )
        is None
    )

    missing_payload = json.loads(
        _try_read_skill_bundle_file(
            extra_files,
            {"path": "references/missing.md"},
        )
    )
    assert "skill bundle" in missing_payload["error"]

    traversal_payload = json.loads(
        _try_read_skill_bundle_file(
            extra_files,
            {"path": "../references/draft-bundle-schema.md"},
        )
    )
    assert traversal_payload["error"] == "Path traversal detected"
