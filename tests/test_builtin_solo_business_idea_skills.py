from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.skill_invocation_policy import (
    PRIMARY_SOURCE,
    REQUIRED_BEFORE_ANSWER,
    SkillInvocationPolicy,
)
from packages.core.ai.runtime.skills import render_runtime_available_skills_section
from packages.core.services.builtin_skill_loader import (
    _parse_frontmatter,
    _read_skill_config,
    seed_builtin_skills,
)
from packages.core.services.skill_service import _load_prompt_skill_extra_files


ROOT = Path(__file__).parents[1] / "packages/core/ai/skills"
MARKETPLACE_ROOT = Path(__file__).parents[1] / "packages/core/ai/marketplace_skills"
SKILL_SLUGS = (
    "solo-business-idea-finder",
    "solo-business-idea-review",
)
EXPECTED_PATHS = {
    "solo-business-idea-finder": {
        "examples/professional-solo-business-ideas.md",
        "references/founder-inventory.md",
        "references/idea-generation-patterns.md",
        "references/idea-ranking-rubric.md",
        "references/manor-execution-map.md",
        "references/opc-case-patterns.md",
        "references/output-template.md",
        "references/starter-idea-library.md",
        "references/upstream-sources.md",
    },
    "solo-business-idea-review": {
        "examples/professional-solo-business-idea-review.md",
        "references/decision-template.md",
        "references/evidence-protocol.md",
        "references/experiment-library.md",
        "references/manor-execution-check.md",
        "references/review-scorecard.md",
        "references/upstream-sources.md",
    },
}
EXPECTED_TOOLS = {
    "invoke_skill",
    "read_file",
    "list_files",
    "generate_file",
    "web_search",
    "web_fetch",
    "browse_web",
    "search_tools",
    "manor",
}
EXPECTED_DISPLAY_NAMES = {
    "solo-business-idea-finder": "Solo Business Idea Finder",
    "solo-business-idea-review": "Solo Business Idea Review",
}
EXPECTED_REVIEW_DATES = {
    "solo-business-idea-finder": "2026-07-22",
    "solo-business-idea-review": "2026-07-21",
}


@pytest.mark.parametrize("slug", SKILL_SLUGS)
def test_solo_business_idea_skills_are_default_runtime_guidance(slug: str):
    skill_dir = ROOT / slug
    frontmatter, body = _parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    config = _read_skill_config(skill_dir)

    assert frontmatter["name"] == slug
    assert frontmatter["version"] == "1.1.0"
    assert frontmatter["description"]
    assert body.strip()
    assert config["type"] == "runtime_guidance"
    assert config["display_name"].startswith("Solo Business Idea")
    assert config["category"] == "one-person-company"
    assert config["output_format"] == "guidance"
    assert set(config["tools"]) == EXPECTED_TOOLS
    assert config["bundle_roots"] == ["references", "examples"]
    assert set(config["tags"]) >= {"one-person-business", "solo-founder", "builtin"}

    policy = SkillInvocationPolicy.from_config(config["invocation_policy"])
    assert policy.mode == REQUIRED_BEFORE_ANSWER
    assert policy.result_authority == PRIMARY_SOURCE
    assert "regardless of language" in policy.semantic_trigger


def test_default_idea_skills_are_not_duplicated_in_marketplace():
    assert all(not (MARKETPLACE_ROOT / slug / "SKILL.md").exists() for slug in SKILL_SLUGS)


@pytest.mark.parametrize("slug", SKILL_SLUGS)
def test_default_idea_skill_resources_are_available_to_runtime(slug: str):
    skill_dir = ROOT / slug
    config = _read_skill_config(skill_dir)
    files = _load_prompt_skill_extra_files(
        SimpleNamespace(id=f"skill_{slug}", entity_id=None),
        {"skill_dir": str(skill_dir), **config},
    )

    assert set(files) == EXPECTED_PATHS[slug]


def test_idea_finder_is_founder_fit_evidence_first_and_fast():
    prompt = (ROOT / "solo-business-idea-finder/SKILL.md").read_text(encoding="utf-8")

    assert "Never invent customer pain" in prompt
    assert "Provided fact" in prompt
    assert "Observed evidence" in prompt
    assert "weekly delivery/support load at 1, 10, and 30 customers" in prompt
    assert "Apply hard filters before scoring" in prompt
    assert "evidence caps" in prompt
    assert "Provisional ranking" in prompt
    assert "Do not require Workflow" in prompt
    assert "Use `web_search` for a fast, current public scan" in prompt
    assert 'skill="chrome"' in prompt
    assert "X or LinkedIn" in prompt
    assert "default Manor" in prompt
    assert "For a quick chat request" in prompt
    assert "Never describe an untested idea as validated demand" in prompt


def test_idea_review_has_clear_verdict_red_team_and_validation_integrity():
    prompt = (ROOT / "solo-business-idea-review/SKILL.md").read_text(encoding="utf-8")

    assert "Reserve `Validated`" in prompt
    assert "Do not average away a fatal flaw" in prompt
    assert "Do not apply venture-capital thresholds" in prompt
    assert "PROCEED TO TEST" in prompt
    assert "REVISE BEFORE TEST" in prompt
    assert "REFRAME" in prompt
    assert "PARK / STOP" in prompt
    assert "strongest disconfirming evidence" in prompt
    assert "1, 10, and 30 customers" in prompt
    assert "customer-research" in prompt
    assert "competitor-brief" in prompt
    assert "Use `web_search` for a fast, current public scan" in prompt
    assert 'skill="chrome"' in prompt
    assert "Planning an experiment does not authorize outreach" in prompt
    assert "For a quick audit" in prompt
    assert "Do not require Workflow" in prompt


@pytest.mark.parametrize("slug", SKILL_SLUGS)
def test_solo_business_idea_bundles_have_professional_examples_and_no_placeholders(
    slug: str,
):
    skill_dir = ROOT / slug
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in skill_dir.rglob("*.md"))

    assert "[TODO:" not in all_text
    assert "This example is fictional" in all_text
    assert "success probability" in all_text or "probability of success" in all_text


@pytest.mark.parametrize("slug", SKILL_SLUGS)
def test_solo_business_idea_skills_record_sources_and_license_boundaries(slug: str):
    provenance = (ROOT / slug / "references/upstream-sources.md").read_text(encoding="utf-8")

    assert f"Reviewed on {EXPECTED_REVIEW_DATES[slug]}" in provenance
    assert "https://github.com/easychen/opc-methodology" in provenance
    assert "b3d0503a52298a2fbe4751231f3119d6a015eab5" in provenance
    assert "CC-BY-NC-SA-4.0" in provenance
    assert "No text" in provenance
    assert "https://github.com/ferdinandobons/startup-skill" in provenance
    assert "a5f97c317b93caedbb49d28f20e9ec283b2ec087" in provenance
    assert "https://github.com/vasilyu1983/AI-Agents-public" in provenance
    assert "869e4c339aeefb0a28b12c395981b046e8e220e6" in provenance
    assert "4c2d9687d5b58e006c0e5b3a6fb157c3174959c7" in provenance
    assert "No license found" in provenance


def test_review_scorecard_caps_weak_evidence_and_never_greenlights_a_full_build():
    scorecard = (ROOT / "solo-business-idea-review/references/review-scorecard.md").read_text(encoding="utf-8")
    example = (ROOT / "solo-business-idea-review/examples/professional-solo-business-idea-review.md").read_text(
        encoding="utf-8"
    )

    assert "Problem evidence below E2" in scorecard
    assert "Only hypothetical willingness-to-pay statements" in scorecard
    assert "cannot exceed `REVISE BEFORE TEST`" in scorecard
    assert "not a 76% success probability" in example
    assert "authorizes only the bounded test" in example
    assert "do not build software" in example


def test_semantic_policies_force_the_right_default_skill_before_answering():
    skills = []
    for slug in SKILL_SLUGS:
        config = _read_skill_config(ROOT / slug)
        skills.append(
            SimpleNamespace(
                entity_id=None,
                slug=slug,
                name=slug,
                display_name=slug,
                description=config["description"],
                tools=config["tools"],
                config={"source": "builtin", **config},
            )
        )

    section = render_runtime_available_skills_section(
        skills,
        active_user_message="帮我判断这个一人公司的想法值不值得做",
        loaded_tool_names=["invoke_skill"],
        available_tool_names=EXPECTED_TOOLS,
    )

    assert section is not None
    assert "### Required Skill Invocation Policies" in section
    assert 'invoke_skill(skill="solo-business-idea-finder"' in section
    assert 'invoke_skill(skill="solo-business-idea-review"' in section
    assert "semantic meaning, not literal keyword matching" in section


@pytest.mark.asyncio
async def test_default_idea_skills_seed_as_shared_platform_rows(db_session):
    skills = await seed_builtin_skills(db_session)
    ideas = {skill.slug: skill for skill in skills if skill.slug in SKILL_SLUGS}

    assert set(ideas) == set(SKILL_SLUGS)
    for slug, skill in ideas.items():
        assert skill.entity_id is None
        assert skill.is_public is True
        assert skill.status == "active"
        assert skill.category == "one-person-company"
        assert skill.display_name == EXPECTED_DISPLAY_NAMES[slug]
        assert skill.config["source"] == "builtin"
        assert skill.config["type"] == "runtime_guidance"
        assert skill.config["skill_dir"].endswith(f"/skills/{slug}")
        assert skill.config["invocation_policy"]["mode"] == REQUIRED_BEFORE_ANSWER
        assert skill.config["source_sha256"]
