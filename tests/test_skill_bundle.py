"""Unit tests for skill bundle packaging (pure, no DB/runtime).

A generated skill becomes a *sandbox bundle* when it ships standalone scripts
or on-demand references; otherwise it stays a plain prompt skill. The bundle
must set ``config.type='sandbox'`` and the sandbox toolset so it routes through
the same executor as builtin skills (``_determine_skill_type``).
"""

from __future__ import annotations

from packages.core.services.skill_bundle import (
    SANDBOX_SKILL_TOOLS,
    assemble_skill_bundle,
)

_BASE = {"source": "llm-generated", "complexity": "primary"}


def test_plain_skill_stays_prompt():
    spec = {"tools": ["bash", "manor"]}
    tools, config = assemble_skill_bundle(spec, _BASE)
    assert tools == ["bash", "manor"]
    assert "type" not in config and "scripts" not in config
    assert config["source"] == "llm-generated"  # base config preserved


def test_scripts_make_it_a_sandbox_bundle():
    spec = {
        "tools": ["bash"],  # ignored — sandbox toolset takes over
        "scripts": {"build_report.py": "print('hi')", "  ": "", "x.py": "  "},
    }
    tools, config = assemble_skill_bundle(spec, _BASE)
    assert config["type"] == "sandbox"
    assert tools == SANDBOX_SKILL_TOOLS
    # blank name / blank content entries dropped
    assert config["scripts"] == {"build_report.py": "print('hi')"}
    # routes to sandbox: _determine_skill_type keys on config.scripts / type
    assert config.get("scripts") and config.get("type") == "sandbox"


def test_references_are_namespaced_and_trigger_bundle():
    spec = {"references": {"style-guide.md": "# Voice", "nested/schema.md": "{}"}}
    tools, config = assemble_skill_bundle(spec, _BASE)
    assert config["type"] == "sandbox"
    assert tools == SANDBOX_SKILL_TOOLS
    # references stored under references/<basename>
    assert config["extra_files"] == {
        "references/style-guide.md": "# Voice",
        "references/schema.md": "{}",
    }


def test_scripts_path_is_basename_only():
    spec = {"scripts": {"sub/dir/run.py": "x = 1"}}
    _, config = assemble_skill_bundle(spec, _BASE)
    assert config["scripts"] == {"run.py": "x = 1"}


def test_base_config_not_mutated():
    base = {"source": "llm-generated"}
    assemble_skill_bundle({"scripts": {"a.py": "1"}}, base)
    assert base == {"source": "llm-generated"}  # caller's dict untouched


# ── clarifying questions parser ──────────────────────────────────────────────

from packages.core.services.skill_bundle import parse_clarifying_questions  # noqa: E402


def test_ready_means_no_questions():
    assert parse_clarifying_questions("READY") == []
    assert parse_clarifying_questions("  ready  ") == []
    assert parse_clarifying_questions("") == []


def test_strips_numbering_and_caps_at_three():
    raw = "1. What channel?\n2) Which audience?\n- How often?\n4. Extra one?"
    qs = parse_clarifying_questions(raw)
    assert qs == ["What channel?", "Which audience?", "How often?"]


def test_dedupes_preserving_order():
    raw = "What format?\nwhat format?\nWhen to run?"
    assert parse_clarifying_questions(raw) == ["What format?", "When to run?"]
