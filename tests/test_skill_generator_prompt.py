"""Guard the packaged Skill Creator contract used by Manor generation."""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_PY = _REPO_ROOT / "packages" / "core" / "ai" / "runtime" / "skills.py"
_CONTRACT_PATH = (
    _REPO_ROOT
    / "packages"
    / "core"
    / "ai"
    / "skills"
    / "skill-creator"
    / "references"
    / "generation-contract.md"
)
_SRC = _SKILLS_PY.read_text(encoding="utf-8")
_CONTRACT = _CONTRACT_PATH.read_text(encoding="utf-8")


def _generation_prompt() -> str:
    match = re.search(
        r'RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT = """\\\n(.*?)"""',
        _SRC,
        re.S,
    )
    assert match, "generation system prompt constant not found"
    return match.group(1)


def test_generation_prompt_loads_the_packaged_skill_creator_contract() -> None:
    prompt = _generation_prompt()

    assert "fully defines" in prompt
    assert "reusable skill at production quality" in prompt
    assert "packaged Skill Creator contract" in prompt
    assert "runtime_skill_generation_contract()" in _SRC
    assert "_system_prompt_with_skill_creation_contract(" in _SRC


def test_generation_contract_defines_the_complete_spec_envelope() -> None:
    for field in (
        '"name"',
        '"slug"',
        '"display_name"',
        '"description"',
        '"system_prompt"',
        '"tools"',
        '"input_schema"',
        '"output_format"',
        '"category"',
        '"tags"',
        '"complexity"',
        '"scripts"',
        '"references"',
    ):
        assert field in _CONTRACT, f"generation contract is missing {field}"


def test_contract_prefers_semantic_discovery_and_progressive_disclosure() -> None:
    assert "semantic situations" in _CONTRACT
    assert "near-misses" in _CONTRACT
    assert "literal keyword list" in _CONTRACT
    assert "progressive disclosure" in _CONTRACT
    assert "Do not target an arbitrary size such as 200-300 lines" in _CONTRACT
    assert "move detailed static material into references" in _CONTRACT


def test_review_and_patch_share_the_same_contract() -> None:
    review_block = _SRC.split("RUNTIME_SKILL_REVIEW_SYSTEM_PROMPT", 1)[1]
    patch_block = _SRC.split("RUNTIME_SKILL_PATCH_SYSTEM_PROMPT", 1)[1]

    assert "Do not treat 200-300 lines as a quality target" in _SRC
    assert "_system_prompt_with_skill_creation_contract(" in review_block
    assert "_system_prompt_with_skill_creation_contract(" in patch_block
    assert "side effects, approvals, authorization" in _SRC


def test_token_caps_allow_bundled_resources() -> None:
    relevant_source = "\n".join(
        block
        for block in re.findall(
            r"async def runtime_execute_skill_(?:generation|review|patch)_completion\(.*?(?=\n\n(?:async )?def |\n\n_LOCAL_)",
            _SRC,
            re.S,
        )
    )
    caps = sorted(int(value) for value in re.findall(r"max_tokens=(\d+)", relevant_source))

    assert caps
    assert caps[0] >= 6000
    assert caps[-1] >= 8000
