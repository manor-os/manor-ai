"""Guard the skill generator's quality bar.

Generated skills must be *detailed* — a 200-300 line system_prompt with the
full section structure — and carry a discovery-ready description (what it does
AND when to use it). A regression toward the old terse "4-12 steps / one-line
description" prompt would silently degrade every skill created afterwards.

These assert on the generator's prompt *source* so they run without importing
the heavy runtime (cryptography/db) or calling a model.
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILLS_PY = Path(__file__).resolve().parents[1] / "packages" / "core" / "ai" / "runtime" / "skills.py"
_SRC = _SKILLS_PY.read_text()


def _generation_prompt() -> str:
    m = re.search(r'RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT = """\\\n(.*?)"""', _SRC, re.S)
    assert m, "generation system prompt constant not found"
    return m.group(1)


def test_generation_prompt_demands_a_detailed_skill():
    g = _generation_prompt()
    assert "200-300 lines" in g, "no explicit length target — skills stay short"
    required_sections = [
        "## Overview",
        "## When to use",
        "## Inputs",
        "## Workflow",
        "## Tools",
        "## Worked example",
        "Edge cases",
        "## Quality bar",
        "## Output format",
    ]
    for section in required_sections:
        assert section in g, f"generation prompt no longer requires section: {section}"


def test_description_rule_requires_a_trigger():
    g = _generation_prompt()
    # The description must say WHEN to use the skill, not just what it does.
    assert "Use this skill when" in g
    assert "WHEN to use it" in g


def test_review_holds_the_same_bar():
    # The refine loop must push toward detail + a trigger-bearing description,
    # otherwise it would happily pass a thin skill.
    assert "detailed enough" in _SRC
    assert (
        "200-300 lines" in _SRC.split("RUNTIME_SKILL_GENERATION_SYSTEM_PROMPT", 1)[0]
        or _SRC.count("200-300 lines") >= 2
    ), "review prompt should reassert the length bar"


def test_token_caps_allow_long_skills():
    # A 200-300 line system_prompt + JSON fields cannot fit in the old 2000-2500
    # cap; the generation/review calls need real headroom.
    relevant_source = "\n".join(
        block
        for block in re.findall(
            r"async def runtime_execute_skill_(?:generation|review|patch)_completion\(.*?(?=\n\n(?:async )?def |\n\n_LOCAL_)",
            _SRC,
            re.S,
        )
    )
    caps = sorted(int(x) for x in re.findall(r"max_tokens=(\d+)", relevant_source))
    assert caps, "no max_tokens settings found"
    assert caps[0] >= 6000, f"smallest cap {caps[0]} too low for a full skill spec"
    assert caps[-1] >= 8000, f"largest cap {caps[-1]} too low for generation/review"
