from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime.skill_invocation_policy import (
    SkillInvocationPolicy,
    trusted_skill_invocation_policy,
)
from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.services.builtin_skill_loader import (
    _builtin_skill_dirs,
    _parse_frontmatter,
    _read_builtin_skill_extra_files,
    _read_skill_config,
)
from packages.core.services.skill_service import _load_prompt_skill_extra_files
from packages.core.services.skill_service import _determine_skill_type


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
VIDEO_SUITE = (
    "video-production",
    "product-promotion-video",
    "restaurant-promotion-video",
)


def test_video_suite_is_packaged_for_builtin_discovery() -> None:
    for slug in VIDEO_SUITE:
        skill_dir = SKILLS_ROOT / slug
        frontmatter, body = _parse_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        )
        config = _read_skill_config(skill_dir)
        assert frontmatter["name"] == slug
        assert body.strip()
        assert config["type"] == "runtime_guidance"
        assert config["category"] == "video"


def test_video_suite_is_discovered_and_bundled() -> None:
    discovered = {
        path.name for path in _builtin_skill_dirs(skills_root=SKILLS_ROOT)
    }
    assert set(VIDEO_SUITE) <= discovered

    production_dir = SKILLS_ROOT / "video-production"
    config = _read_skill_config(production_dir)
    assert SkillInvocationPolicy.from_config(config["invocation_policy"])
    assert config["bundle_roots"] == ["references"]
    assert trusted_skill_invocation_policy(
        SimpleNamespace(entity_id=None, config={"source": "builtin", **config})
    ) is not None

    references = _read_builtin_skill_extra_files(production_dir)
    assert set(references) == {
        "references/ai-routing.md",
        "references/canvas-contract.md",
        "references/production-and-edit.md",
    }
    loaded = _load_prompt_skill_extra_files(
        SimpleNamespace(id="video-production", entity_id=None),
        {"skill_dir": str(production_dir), **config},
    )
    assert loaded == references

    for slug in VIDEO_SUITE[1:]:
        assert _read_skill_config(SKILLS_ROOT / slug)["execution_mode"] == "instructions_only"


def test_video_production_tools_connect_to_registered_media_and_editor_tools() -> None:
    production_tools = set(_read_skill_config(SKILLS_ROOT / "video-production")["tools"])
    edit_tools = set(_read_skill_config(SKILLS_ROOT / "video-edit")["tools"])

    assert {
        "read_file",
        "search_documents",
        "search_tools",
        "generate_file",
        "plan_video_segments",
        "wait_media_jobs",
        "probe_media",
        "render_frame_samples",
        "transcribe_video",
        "build_narration_timeline",
        "normalize_audio_loudness",
        "align_subtitles",
        "merge_videos",
        "compose_video_timeline",
        "analyze_audio",
        "validate_subtitles",
        "invoke_skill",
    } <= production_tools
    assert "video_edit" in edit_tools
    assert "search_documents" in edit_tools
    assert "video_edit" not in production_tools
    assert all(runtime_tool_schema(name) is not None for name in production_tools | edit_tools)

    planner = runtime_tool_schema("plan_video_segments")["function"]["parameters"]["properties"]
    generator = runtime_tool_schema("generate_video")["function"]["parameters"]["properties"]
    editor = runtime_tool_schema("video_edit")["function"]["parameters"]["properties"]
    assert {"target_duration_seconds", "generation_mode"} <= planner.keys()
    assert {"expected_model", "first_frame_url", "last_frame_url", "duration"} <= generator.keys()
    assert {"start", "push_assets", "write_files", "review", "render"} <= set(
        editor["operation"]["enum"]
    )


def test_video_edit_uses_its_prompt_tools_even_with_a_bundled_runtime_script() -> None:
    config = _read_skill_config(SKILLS_ROOT / "video-edit")
    assert _determine_skill_type(
        SimpleNamespace(config={"skill_dir": str(SKILLS_ROOT / "video-edit"), **config})
    ) == "prompt"
