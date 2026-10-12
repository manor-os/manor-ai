from __future__ import annotations

import json
from pathlib import Path

from packages.core.services.builtin_skill_loader import _parse_frontmatter


SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "core"
    / "ai"
    / "skills"
    / "dashboard-module-builder"
)


def test_dashboard_module_builder_is_a_fixed_builtin_skill() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(skill_text)
    config = json.loads((SKILL_DIR / "config.json").read_text(encoding="utf-8"))

    assert frontmatter["name"] == "dashboard-module-builder"
    assert frontmatter["description"]
    assert body.strip()
    assert frontmatter["version"] == "1.3.0"
    assert config["type"] == "runtime_guidance"
    assert config["tools"] == [
        "dashboard_submit_module",
        "code",
        "search_tools",
        "web_search",
        "web_event_search",
    ]
