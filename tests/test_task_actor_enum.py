"""Every task log says what kind of thing acted.

A task-log row used to answer "who did this" with a free-text string, and
each writer invented its own spelling. Staging showed all of these in one
column, with no way to tell them apart without guessing:

    Sprint Planner        a specific workspace agent, by display name
    daily_progress_review a specific workspace agent, by service key
    Manor AI              the master agent as the workspace agent
    AI Supervisor         the plan supervisor
    AI Agent              the executor's own summary
    ai-agent              an agent creating a task
    workspace-agent       a workspace reply with no resolved agent
    01KQDCA7EB…           a person, by id
    Calvin Lin            a person, by display name
    mojiamenke123         a person, by email local-part
    client:…              an external portal client
    system                the platform

That guesswork is what rendered "workspace-agent" as a person with an
initials avatar. TaskActor is the closed set; it is declared at the call
site and stored, so reading it back is a lookup.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from packages.core.constants.agents import agent_display_name_from_service_key
from packages.core.constants.task_actors import (
    TASK_ACTOR_META_KEY,
    TaskActor,
    task_actor_from_meta,
    task_actor_meta,
)

WEB_CONSTANTS = Path("apps/web/src/lib/constants.ts")
SEARCH_ROOTS = (Path("packages/core"), Path("apps/api"))


# ── The enum ──────────────────────────────────────────────────────────


def test_the_actor_set_covers_what_actually_writes_logs():
    """Each member exists because a real writer produces it — see the module
    docstring in constants/task_actors.py for the writer of each."""
    assert set(TaskActor.values()) == {
        "user",        # a person
        "agent",       # a specific workspace agent
        "manor",       # the master agent acting as the workspace agent
        "supervisor",  # the plan supervisor
        "system",      # the platform itself
        "client",      # an external portal client
    }


def test_the_actor_survives_a_round_trip():
    meta = task_actor_meta(TaskActor.SUPERVISOR, metadata={"plan_id": "p1"})
    assert meta[TASK_ACTOR_META_KEY] == "supervisor"
    assert meta["plan_id"] == "p1", "existing metadata must be preserved"
    assert task_actor_from_meta(meta) is TaskActor.SUPERVISOR


def test_the_declared_actor_wins_over_a_stale_one():
    meta = task_actor_meta(TaskActor.USER, metadata={TASK_ACTOR_META_KEY: "system"})
    assert task_actor_from_meta(meta) is TaskActor.USER


@pytest.mark.parametrize("metadata", [None, {}, {TASK_ACTOR_META_KEY: "kangaroo"}])
def test_rows_without_a_usable_actor_read_as_none(metadata):
    """Logs written before this existed must not crash a reader, and must not
    silently masquerade as a valid actor."""
    assert task_actor_from_meta(metadata) is None


# ── 1:1 coverage ──────────────────────────────────────────────────────


def _add_task_log_calls():
    """Every add_task_log(...) call in the product, as AST nodes."""
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "add_task_log":
                    yield path, node


def test_every_call_site_declares_an_actor():
    """The point of the enum is that nobody gets to stay ambiguous. A new
    writer that forgets this fails here rather than shipping another
    unclassifiable string."""
    calls = list(_add_task_log_calls())
    assert len(calls) >= 25, f"expected the known call sites, found {len(calls)}"

    missing = [
        f"{path}:{node.lineno}"
        for path, node in calls
        if not any(kw.arg == "actor" for kw in node.keywords)
    ]
    assert not missing, f"add_task_log without an actor= at {missing}"


def test_add_task_log_requires_the_actor():
    """Keyword-only and no default: a forgotten actor is a TypeError, not a
    silently wrong row."""
    import inspect

    from packages.core.services.task_service import add_task_log

    param = inspect.signature(add_task_log).parameters["actor"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


# ── The wire ──────────────────────────────────────────────────────────


def test_the_frontend_mirrors_the_enum():
    source = WEB_CONSTANTS.read_text(encoding="utf-8")
    block = source.split("TASK_ACTORS")[1].split("}")[0]
    mirrored = set(re.findall(r'"([^"]+)"', block))
    assert mirrored == set(TaskActor.values()), (
        f"only in Python: {set(TaskActor.values()) - mirrored}; "
        f"only in TypeScript: {mirrored - set(TaskActor.values())}"
    )


def test_the_frontend_reads_the_same_metadata_key():
    assert f'"{TASK_ACTOR_META_KEY}"' in WEB_CONSTANTS.read_text(encoding="utf-8")


# ── A nameless agent is still a named agent ───────────────────────────


@pytest.mark.parametrize(
    "service_key,expected",
    [
        ("daily_progress_review", "Daily Progress Review"),
        ("stickman.production", "Stickman Production"),
        ("linux-troubleshooting", "Linux Troubleshooting"),
        ("", ""),
        (None, ""),
    ],
)
def test_a_service_key_is_rendered_as_a_name(service_key, expected):
    """Subscriptions with a NULL name fall back to their service key. It is
    the right identity — it is just not spelled like a name."""
    assert agent_display_name_from_service_key(service_key) == expected


def test_the_dispatcher_does_not_store_a_raw_service_key_as_a_name():
    body = Path("packages/core/dispatcher/service.py").read_text(encoding="utf-8")
    assert 'sub.name or sub.service_key or step.service_key' not in body, (
        "a raw service key is being written into agent_name"
    )
    assert "agent_display_name_from_service_key" in body
