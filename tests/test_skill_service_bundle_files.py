import hashlib
import json

from packages.core.services.skill_service import (
    DEFAULT_SKILL_MAX_ROUNDS,
    _append_skill_bundle_manifest,
    _try_list_skill_bundle_files,
    _try_read_skill_bundle_file,
)


def test_prompt_skill_default_budget_is_200_rounds():
    assert DEFAULT_SKILL_MAX_ROUNDS == 200


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

    sliced = json.loads(_try_read_skill_bundle_file(
        {"references/course.md": "Overview\nLesson one\nLesson two\n"},
        {"path": "references/course.md", "offset": 1, "limit": 1},
    ))
    assert sliced["content"] == "Lesson one\n"
    assert sliced["offset"] == 1 and sliced["next_offset"] == 2
    assert sliced["char_offset"] == len("Overview\n")

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


def test_skill_bundle_read_continues_after_context_truncation():
    from packages.core.ai.agentic_loop import _compact_tool_result_for_context

    path = "references/course-outline.md"
    content = "".join(f"Lesson {i}: 渐进练习 and practical examples.\n" for i in range(700))
    source_hash = hashlib.sha256(content.encode()).hexdigest()
    offset, collected = 0, []
    for _ in range(30):
        raw = _try_read_skill_bundle_file(
            {path: content},
            {"path": path, "char_offset": offset, "max_chars": 9000,
             "expected_sha256": source_hash},
        )
        source_page = json.loads(raw)
        assert source_page["slice_sha256"] == hashlib.sha256(source_page["content"].encode()).hexdigest()
        page = json.loads(_compact_tool_result_for_context("read_file", raw, 4000))
        assert page["char_offset"] == offset
        assert page["source_sha256"] == source_hash
        assert page["content"] == content[offset:offset + len(page["content"])]
        collected.append(page["content"])
        if page["next_char_offset"] is None:
            break
        assert page["next_char_offset"] > offset
        offset = page["next_char_offset"]
    assert "".join(collected) == content
    changed = json.loads(_try_read_skill_bundle_file(
        {path: content + "changed"},
        {"path": path, "char_offset": offset, "expected_sha256": source_hash},
    ))
    assert changed["error"] == "source_changed"
    assert "content" not in changed
