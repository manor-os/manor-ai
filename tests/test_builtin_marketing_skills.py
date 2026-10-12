from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime.skill_invocation_policy import (
    SkillInvocationPolicy,
    trusted_skill_invocation_policy,
)
from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.ai.runtime.skills import render_runtime_available_skills_section
from packages.core.services.builtin_skill_loader import (
    _builtin_skill_dirs,
    _parse_frontmatter,
    _read_builtin_skill_extra_files,
    _read_skill_config,
)
from packages.core.services.skill_service import _load_prompt_skill_extra_files


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
MARKETING_SUITE = (
    "marketing-campaign",
    "marketing-platform",
    "marketing-industry",
    "marketing-method",
)
AXIS_REFERENCES = {
    "marketing-platform": {
        "references/video-platforms.md",
        "references/social-platforms.md",
        "references/search-and-maps.md",
        "references/commerce-and-owned.md",
    },
    "marketing-industry": {
        "references/restaurant.md",
        "references/digital-product.md",
        "references/physical-product.md",
        "references/services-and-other.md",
    },
    "marketing-method": {
        "references/organic-content.md",
        "references/paid-acquisition.md",
        "references/search-local.md",
        "references/lifecycle.md",
        "references/partners-and-referrals.md",
        "references/conversion-and-launch.md",
    },
}


def test_marketing_axes_are_built_in_and_independently_invocable() -> None:
    discovered = {path.name for path in _builtin_skill_dirs(skills_root=SKILLS_ROOT)}
    assert set(MARKETING_SUITE) <= discovered

    for slug in MARKETING_SUITE:
        skill_dir = SKILLS_ROOT / slug
        frontmatter, body = _parse_frontmatter(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        )
        config = _read_skill_config(skill_dir)
        assert frontmatter["name"] == slug
        assert body.strip()
        assert config["type"] == "runtime_guidance"
        assert config["category"] == "marketing"

    for slug, expected_paths in AXIS_REFERENCES.items():
        skill_dir = SKILLS_ROOT / slug
        config = _read_skill_config(skill_dir)
        assert config.get("execution_mode") is None
        assert config["bundle_roots"] == ["references"]
        assert config["tools"] == ["read_file"]
        references = _read_builtin_skill_extra_files(skill_dir)
        assert set(references) == expected_paths
        assert _load_prompt_skill_extra_files(
            SimpleNamespace(id=slug, entity_id=None),
            {"skill_dir": str(skill_dir), **config},
        ) == references


def test_marketing_campaign_routes_through_registered_tools_and_bundles() -> None:
    campaign_dir = SKILLS_ROOT / "marketing-campaign"
    config = _read_skill_config(campaign_dir)
    assert SkillInvocationPolicy.from_config(config["invocation_policy"])
    assert trusted_skill_invocation_policy(
        SimpleNamespace(entity_id=None, config={"source": "builtin", **config})
    ) is not None

    references = _read_builtin_skill_extra_files(campaign_dir)
    assert set(references) == {"references/campaign-contract.md"}
    assert _load_prompt_skill_extra_files(
        SimpleNamespace(id="marketing-campaign", entity_id=None),
        {"skill_dir": str(campaign_dir), **config},
    ) == references

    tools = set(config["tools"])
    assert {"list_skills", "invoke_skill", "read_file", "generate_file"} <= tools
    assert all(runtime_tool_schema(name) is not None for name in tools)
    assert {"generate_video", "video_edit", "compose_video_timeline"}.isdisjoint(tools)


def test_plain_language_marketing_request_exposes_required_invocation() -> None:
    config = _read_skill_config(SKILLS_ROOT / "marketing-campaign")
    skill = SimpleNamespace(
        entity_id=None,
        id="marketing-campaign",
        slug="marketing-campaign",
        name="marketing-campaign",
        display_name="Marketing Campaign",
        description=config["description"],
        tools=config["tools"],
        config={"source": "builtin", **config},
    )

    for request in (
        "帮我的餐厅做小红书开业推广，目标是周末预订",
        "给我们的 SaaS 产品策划 LinkedIn 获客内容和广告",
    ):
        section = render_runtime_available_skills_section(
            [skill],
            active_user_message=request,
            loaded_tool_names=["invoke_skill"],
            available_tool_names=["invoke_skill"],
            include_ordinary=False,
        )
        assert section is not None
        assert "Required Skill Invocation Policies" in section
        assert 'invoke_skill(skill_id="marketing-campaign"' in section
        assert "video-production directly" in config["invocation_policy"]["semantic_trigger"]
