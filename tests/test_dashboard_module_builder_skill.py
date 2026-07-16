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
    assert frontmatter["version"] == "1.2.1"
    assert config["type"] == "runtime_guidance"
    assert config["tools"] == [
        "dashboard_submit_module",
        "code",
        "search_tools",
        "web_search",
        "web_event_search",
    ]
    assert "dashboard_submit_module" in body
    assert "`news`" in body
    assert "update that module instead of creating a duplicate" in body
    assert "Choose the source that best satisfies" in body
    assert "Do not choose a source from keywords alone" in body
    assert "public or location-based external information" in body
    assert "Never silently replace external information" in body
    assert "multiple independent locations or categories" in body
    assert "When the user changes only presentation" in body
    assert "use `web_event_search`" in body
    assert "private inbox, email, Gmail, Outlook, or unread-message views" in body
    assert "`mcp__gmail__list_messages`" in body
    assert "`include_details: true`" in body
    assert "`http_json`" in body
    assert "domain-specific parsing" in body
    assert "request-specific behavior" in body
    assert "action: dashboard_module_validate" in body
    assert "platform_ready" in body
    assert "The Dashboard host owns the module title" in body
    assert "one universal builder" in body
    assert "the host runtime stays domain-neutral" in body
    assert "Use semantic color tokens only" in body
    assert "Let the host own elevation" in body
    assert "--module-space-1" in body
    assert "--module-type-metric" in body
