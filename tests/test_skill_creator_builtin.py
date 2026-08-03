from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.skill_invocation_policy import (
    PRIMARY_SOURCE,
    REQUIRED_BEFORE_ANSWER,
    SkillInvocationPolicy,
)
from packages.core.ai.runtime.skills import (
    runtime_skill_generation_contract,
    runtime_skill_generation_messages,
    runtime_skill_patch_messages,
    runtime_skill_review_messages,
)
from packages.core.ai.tools.skill_tools import get_tools as get_skill_tools
from packages.core.services.builtin_skill_loader import (
    _parse_frontmatter,
    _read_skill_config,
    _skill_source_sha256,
    seed_builtin_skills,
)
from packages.core.services.skill_service import _load_prompt_skill_extra_files


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "packages" / "core" / "ai" / "skills" / "skill-creator"


def test_builtin_skill_creator_has_public_progressive_disclosure_contract() -> None:
    frontmatter, body = _parse_frontmatter(
        (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    )
    config = _read_skill_config(SKILL_ROOT)

    assert frontmatter["name"] == "skill-creator"
    assert "Create, update, review, and evaluate" in frontmatter["description"]
    assert "references/generation-contract.md" in body
    assert "references/evaluation-playbook.md" in body
    assert config["type"] == "runtime_guidance"
    assert config["category"] == "skill-development"
    assert config["bundle_roots"] == ["references"]
    assert set(config["tools"]) == {
        "draft_skill",
        "create_skill",
        "get_skill_details",
        "update_skill",
        "invoke_skill",
        "list_skills",
        "read_file",
        "list_files",
    }

    policy = SkillInvocationPolicy.from_config(config["invocation_policy"])
    assert policy.mode == REQUIRED_BEFORE_ANSWER
    assert policy.result_authority == PRIMARY_SOURCE
    assert "regardless of language" in policy.semantic_trigger


def test_skill_creator_references_are_available_to_prompt_runtime() -> None:
    config = _read_skill_config(SKILL_ROOT)
    files = _load_prompt_skill_extra_files(
        SimpleNamespace(id="skill_creator", entity_id=None),
        {"skill_dir": str(SKILL_ROOT), **config},
    )

    assert set(files) == {
        "references/evaluation-playbook.md",
        "references/generation-contract.md",
    }
    assert (
        files["references/generation-contract.md"].strip()
        == runtime_skill_generation_contract()
    )
    assert "sole non-JSON exception" in files["references/generation-contract.md"]
    assert "does not require a fixed category taxonomy" in files[
        "references/generation-contract.md"
    ]
    assert "Direct `invoke_skill` bypasses selection" in files[
        "references/evaluation-playbook.md"
    ]


def test_skill_creator_lifecycle_tools_are_registered() -> None:
    registered = {
        schema["function"]["name"] for schema, _handler in get_skill_tools()
    }

    assert {
        "draft_skill",
        "create_skill",
        "get_skill_details",
        "update_skill",
        "invoke_skill",
        "list_skills",
    } <= registered


def test_builtin_skill_fingerprint_covers_progressive_disclosure_files(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "sample-skill"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
    reference = references / "contract.md"
    reference.write_text("version one\n", encoding="utf-8")
    first = _skill_source_sha256(skill_dir)

    reference.write_text("version two\n", encoding="utf-8")

    assert _skill_source_sha256(skill_dir) != first


def test_generation_review_and_patch_use_the_same_packaged_contract() -> None:
    contract_marker = "# Manor Skill Generation Contract"
    generated = runtime_skill_generation_messages("Create a support triage skill")
    reviewed = runtime_skill_review_messages(
        {
            "name": "support-triage",
            "description": "Triage support requests.",
            "system_prompt": "Triage the request.",
            "tools": [],
            "input_schema": {},
        }
    )
    patched = runtime_skill_patch_messages(
        SimpleNamespace(
            name="support-triage",
            slug="support-triage",
            display_name="Support Triage",
            description="Triage support requests.",
            system_prompt="Triage the request.",
            tools=[],
            input_schema={},
            output_format="markdown",
            category="support",
            tags=["support"],
        ),
        "Handle empty requests safely",
    )

    assert contract_marker in generated[0]["content"]
    assert contract_marker in reviewed[0]["content"]
    assert contract_marker in patched[0]["content"]


@pytest.mark.asyncio
async def test_builtin_skill_creator_is_seeded_for_manor_agents(db_session) -> None:
    skills = await seed_builtin_skills(db_session)
    creator = next(skill for skill in skills if skill.slug == "skill-creator")

    assert creator.entity_id is None
    assert creator.category == "skill-development"
    assert creator.config["source"] == "builtin"
    assert creator.config["type"] == "runtime_guidance"
    assert creator.config["bundle_roots"] == ["references"]
    assert creator.config["invocation_policy"]["mode"] == REQUIRED_BEFORE_ANSWER
