"""Blueprint component identity must not be inferred from an entity slug.

Slug normalization remains useful only for controlled historical repair.
Fresh installs use ``(Marketplace Blueprint id, Workspace id, component
key)`` and mint a Workspace-scoped local Skill, so two Marketplace authors
may publish the same display slug without either claiming the other's row.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.core.blueprints.installer import normalize_skill_slug

BLUEPRINT = Path(
    "packages/core/blueprints/configs/solo_company/"
    "solo-faceless-stickman-studio-v1.json"
)


# ── Slug drift no longer forks a row ──────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("stickman-video-creator", "stickman_video_creator"),
        ("Stickman Video Creator", "stickman-video-creator"),
        ("video--creator", "video_creator"),
        ("  Video Creator  ", "video-creator"),
    ],
)
def test_separator_and_case_drift_fold_together(a, b):
    assert normalize_skill_slug(a) == normalize_skill_slug(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("video-creator", "video-editor"),
        ("stickman-video-creator", "stickman-video-reviewer"),
        ("chrome", "chromium"),
    ],
)
def test_genuinely_different_skills_stay_distinct(a, b):
    assert normalize_skill_slug(a) != normalize_skill_slug(b)


def test_normalization_handles_empty_input():
    assert normalize_skill_slug(None) == ""
    assert normalize_skill_slug("") == ""
    assert normalize_skill_slug("---") == ""


def test_installer_uses_source_component_links_instead_of_slug_adoption():
    source = Path("packages/core/blueprints/installer.py").read_text(encoding="utf-8")
    assert "_find_installed_skill(" not in source
    assert "normalize_skill_slug" in source
    install_body = source.split("async def _install_embedded_skill(")[1].split(
        "async def _install_embedded_agent("
    )[0]
    assert "_find_installed_skill(" not in install_body
    assert "get_marketplace_resource_link(" in install_body
    assert "source_blueprint_id" in install_body
    assert "scope_id=workspace_id" in install_body


# ── The blueprint ships the definition that actually works ────────────


def test_blueprint_skill_carries_a_production_prompt():
    """The embedded copy was a 636-character sketch: no scene plan, no
    render sequence. An agent told to 'run the skill end-to-end' got no
    procedure to follow and searched for tools instead of calling them."""
    blueprint = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    skills = blueprint["embedded"]["skills"]
    # The publisher was folded into the Producer agent's own prompt (direct
    # Chrome publication), so the blueprint embeds one skill now.  Its absence
    # is the thing under test in
    # test_solo_company_blueprints.py::test_stickman_blueprint_does_not_install_unfinished_youtube_publisher
    # — asserting it here too is what made these two tests contradict.
    assert {skill["slug"] for skill in skills} == {"stickman-video-creator"}

    skill = next(
        skill for skill in skills if skill["slug"] == "stickman-video-creator"
    )
    prompt = skill.get("system_prompt") or ""
    assert len(prompt) > 3000, (
        f"embedded skill prompt is only {len(prompt)} chars — too thin to "
        "drive an end-to-end video production"
    )
    assert "MP4" in prompt


def test_blueprint_skill_declares_the_media_toolchain():
    blueprint = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    skill = next(
        skill
        for skill in blueprint["embedded"]["skills"]
        if skill["slug"] == "stickman-video-creator"
    )
    tools = set(skill.get("tools") or [])
    for required in (
        "generate_image",
        "generate_video",
        "wait_media_jobs",
        "compose_video_timeline",
        "merge_videos",
    ):
        assert required in tools, f"{required} missing — the skill cannot render"


def test_blueprint_agent_prompt_and_skill_slug_agree():
    """The producer's prompt names the skill by slug; a rename on either
    side silently leaves the agent instructed to run something that is not
    bound to it."""
    blueprint = json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    skill_slug = next(
        skill["slug"]
        for skill in blueprint["embedded"]["skills"]
        if skill["slug"] == "stickman-video-creator"
    )

    prompts = [
        agent.get("system_prompt") or ""
        for agent in blueprint["embedded"].get("agents") or []
    ]
    referencing = [p for p in prompts if skill_slug in p]
    assert referencing, (
        f"no agent prompt mentions {skill_slug!r} — the blueprint ships a "
        "skill nobody is told to use"
    )
