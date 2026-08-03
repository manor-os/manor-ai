"""The author slot names someone, or it names Manor AI.

A task log the backend could not attribute to a specific agent is stored with
``created_by = "workspace-agent"``. The activity feed fell through its whole
resolution chain and rendered that string verbatim, styled as a person: a
task authored by "workspace-agent", complete with initials avatar.

There is no such agent — and there never was an unattributable one either.
Every writer that stored a placeholder was holding a determinate agent: the
task's agent, the step's resolved agent, or the master agent running as the
workspace agent. So the placeholders are a legacy read concern, not a
category of actor.

Both sides of the wire are pinned here, because a sentinel the backend writes
and the frontend does not recognise leaks straight back into the UI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.core.constants.agents import (
    MANOR_AGENT_NAME,
    LEGACY_AGENT_AUTHOR_PLACEHOLDERS,
    is_legacy_agent_author_placeholder,
)

WEB_CONSTANTS = Path("apps/web/src/lib/constants.ts")
TASK_LOG_ITEM = Path("apps/web/src/components/task/TaskLogItem.tsx")


def _source(path: Path) -> str:
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


# ── The sentinel ──────────────────────────────────────────────────────


def test_the_placeholder_set_is_defined_once():
    """No site retypes the literal."""
    # Only authorship sites: exporter.py uses the same word as a *slug*
    # fallback for an unnamed agent row, which is a different concern.
    call = re.compile(r'agent_log_authorship\([^)]*"workspace-agent"', re.S)
    literals = []
    for path in Path("packages/core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in call.finditer(text):
            literals.append(f"{path}:{text[:match.start()].count(chr(10)) + 1}")
    assert not literals, (
        "the unattributed-agent sentinel is spelled out at "
        f"{literals} — import UNATTRIBUTED_AGENT_AUTHOR instead"
    )


@pytest.mark.parametrize(
    "value",
    ["workspace-agent", "WORKSPACE-AGENT", " workspace-agent ", "ai-agent", "AI Agent", "AI Supervisor"],
)
def test_known_placeholders_are_recognised(value):
    """Each came from a distinct writer (see constants/agents.py). They are
    not spellings of one another — they are four places that held an agent
    and stored a stand-in."""
    assert is_legacy_agent_author_placeholder(value)


@pytest.mark.parametrize("value", ["Calvin Lin", "Sprint Planner", "", None, "Agentic Writer"])
def test_real_names_are_not_placeholders(value):
    """A specific agent keeps its own name — this must not swallow them."""
    assert not is_legacy_agent_author_placeholder(value)


def test_nothing_writes_a_placeholder_any_more():
    """The point of the fix: every writer had a determinate agent all along.
    A placeholder passed as an authorship fallback means a site is still
    declining to name the agent it is holding."""
    # Authorship sites only — exporter.py uses the same word as a *slug*
    # fallback for an unnamed agent row, which is a different concern.
    call = re.compile(
        r'agent_log_authorship\([^)]*fallback\s*=\s*'
        r'(?:UNATTRIBUTED_AGENT_AUTHOR|"workspace-agent")',
        re.S,
    )
    offenders = []
    for path in Path("packages/core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in call.finditer(text):
            offenders.append(f"{path}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, f"placeholder still written at {offenders}"


def test_an_agent_less_call_resolves_to_the_master_agent():
    """No agent id does not mean no agent: the master agent ran it."""
    import inspect

    from packages.core.services.task_service import agent_log_authorship

    body = inspect.getsource(agent_log_authorship)
    assert "return MANOR_AGENT_NAME, None, TaskActor.MANOR" in body


# ── The wire ──────────────────────────────────────────────────────────


def test_the_frontend_mirrors_every_placeholder():
    """constants.ts declares itself a mirror of the Python module; a value
    present on only one side is a row that renders as a person again."""
    source = _source(WEB_CONSTANTS)
    block = source.split("LEGACY_AGENT_AUTHOR_PLACEHOLDERS: ReadonlySet<string>")[1]
    block = block.split("]")[0]
    mirrored = set(re.findall(r'"([^"]+)"', block))
    assert mirrored == set(LEGACY_AGENT_AUTHOR_PLACEHOLDERS), (
        f"only in Python: {set(LEGACY_AGENT_AUTHOR_PLACEHOLDERS) - mirrored}; "
        f"only in TypeScript: {mirrored - set(LEGACY_AGENT_AUTHOR_PLACEHOLDERS)}"
    )


def test_the_frontend_display_name_matches():
    assert f'MANOR_AGENT_NAME = "{MANOR_AGENT_NAME}"' in _source(WEB_CONSTANTS)


# ── The rendering ─────────────────────────────────────────────────────


def test_the_activity_feed_resolves_the_placeholder():
    """Author resolution consults the predicate, and the generic branch ends
    at Manor AI rather than echoing whatever string it was handed."""
    body = _source(TASK_LOG_ITEM)
    resolver = body.split("function resolveAuthor(")[1].split("\nfunction ")[0]

    assert "isLegacyAgentAuthorPlaceholder(cb)" in resolver, (
        "resolveAuthor must recognise the sentinel"
    )
    assert 'return { name: cb || t("component.task_log_item.agent"), kind: "agent" };' not in resolver, (
        "the placeholder branch still renders the stored string verbatim"
    )
