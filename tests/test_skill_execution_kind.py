"""Explicit Skill execution types take precedence over bundled helper files."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.skill_service import _determine_skill_type


VIDEO_EDIT_DIR = (
    Path(__file__).resolve().parents[1] / "packages/core/ai/skills/video-edit"
)


@pytest.mark.parametrize("source", ["builtin", "marketplace"])
def test_video_edit_runtime_guidance_does_not_execute_its_tool_private_helper(source):
    config = json.loads((VIDEO_EDIT_DIR / "config.json").read_text(encoding="utf-8"))
    config.update(source=source, skill_dir=str(VIDEO_EDIT_DIR))
    if source == "marketplace":
        config.update(source_skill_id="source-video-edit", source_skill_slug="video-edit")
    skill = SimpleNamespace(
        entity_id=None if source == "builtin" else "entity",
        config=config,
    )

    assert config["type"] == "runtime_guidance"
    assert (VIDEO_EDIT_DIR / "scripts/video_runtime.py").is_file()
    assert "video_edit" in config["tools"]
    assert _determine_skill_type(skill) == "prompt"


@pytest.mark.parametrize(
    "declared, expected",
    [("sandbox", "sandbox"), ("prompt", "prompt"), ("runtime_guidance", "prompt")],
)
@pytest.mark.parametrize(
    "helper_config",
    [
        {"scripts": {"helper.py": "print('helper')"}},
        {"requirements": "example-package==1.0"},
        {"skill_dir": str(VIDEO_EDIT_DIR)},
    ],
)
def test_declared_execution_type_precedes_script_detection(declared, expected, helper_config):
    skill = SimpleNamespace(config={"type": declared, **helper_config})

    assert _determine_skill_type(skill) == expected


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"scripts": {"run.py": "print('legacy')"}}, "sandbox"),
        ({"requirements": "example-package==1.0"}, "sandbox"),
        ({"skill_dir": str(VIDEO_EDIT_DIR)}, "sandbox"),
        ({}, "prompt"),
        ({"scripts": {}, "requirements": ""}, "prompt"),
        ({"type": "sandbox"}, "sandbox"),
    ],
)
def test_legacy_detection_and_explicit_sandbox_contract_are_preserved(config, expected):
    assert _determine_skill_type(SimpleNamespace(config=config)) == expected


@pytest.mark.parametrize("execution_mode", [None, "instructions_only"])
def test_execution_kind_does_not_rewrite_instruction_handoff_mode(execution_mode):
    config = {"type": "runtime_guidance", "skill_dir": str(VIDEO_EDIT_DIR)}
    if execution_mode is not None:
        config["execution_mode"] = execution_mode
    original_config = dict(config)

    assert _determine_skill_type(SimpleNamespace(config=config)) == "prompt"
    assert config == original_config
