"""Guard: every writer that closes a HITL request undecided wakes the child parked on it.

A delegated child run parked on a card waits until its decision reaches it.
A request closed any other way (expired, withdrawn, an approval that can no
longer run) must wake that child in the same transaction
(``task_agent_loop_hitl.wake_run_for_closed_request``), or the child waits
on nothing. Scanned repo-wide over the production code: every function that
assigns ``<request>.status = ApprovalStatus.EXPIRED.value`` calls the wake;
writers of DENIED are a person's decision and are listed with why. The
recovery sweep (``recover_runs_on_closed_cards_once``) is the backstop.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DIRS = ("packages", "apps")
_WAKE = "wake_run_for_closed_request"
_CLOSING = {"EXPIRED"}
# Writers of a terminal status that are a person's decision, with why.
_DECISIONS = {
    # deny_approval is the rejection itself: approval_service routes it to the
    # parked run (stage_task_agent_loop_approval_resolution), the answer path
    # routes a refused question; any other denier is caught by the sweep.
    ("packages/core/governance/approvals.py", "deny_approval"),
    # A person rejecting a provider approval on its card (a decision).
    ("packages/core/ai/runtime/approval_service.py", "_reject_unclaimed_provider_runtime_approval"),
}


def _status_writes(source: str) -> list[tuple[str, str, int, bool]]:
    """(function, status, line, calls the wake) for every ``x.status = ApprovalStatus.<S>.value``."""

    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_wake = any(
            isinstance(sub, ast.Call)
            and getattr(sub.func, "id", getattr(sub.func, "attr", None)) == _WAKE
            for sub in ast.walk(node)
        )
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            value = stmt.value
            if not (
                isinstance(value, ast.Attribute) and value.attr == "value"
                and isinstance(value.value, ast.Attribute)
                and isinstance(value.value.value, ast.Name) and value.value.value.id == "ApprovalStatus"
            ):
                continue
            if any(isinstance(target, ast.Attribute) and target.attr == "status" for target in stmt.targets):
                found.append((node.name, value.value.attr, stmt.lineno, calls_wake))
    return found


def _sources() -> dict[str, str]:
    sources = {}
    for directory in _PRODUCTION_DIRS:
        for file in (ROOT / directory).rglob("*.py"):
            if "node_modules" in file.parts or "tests" in file.parts:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "ApprovalStatus." in text and ".status = ApprovalStatus." in text:
                sources[str(file.relative_to(ROOT))] = text
    return sources


def _violations(sources: dict[str, str]) -> list[tuple[str, str, int]]:
    out = []
    for path, source in sources.items():
        for function, status, line, calls_wake in _status_writes(source):
            if status in _CLOSING and not calls_wake and (path, function) not in _DECISIONS:
                out.append((path, function, line))
    return out


def test_every_undecided_close_wakes_the_child_parked_on_it():
    sources = _sources()
    assert _violations(sources) == []
    # The scan sees the known closers, and every listed decision still exists.
    closers = {
        (path, function) for path, source in sources.items()
        for function, status, _line, _wake in _status_writes(source) if status in _CLOSING
    }
    assert ("packages/core/services/task_runtime_approvals.py", "_expire_locked_runtime_request") in closers
    assert len(closers) >= 8
    for path, function in _DECISIONS:
        assert any(found[0] == function for found in _status_writes(sources[path])), (path, function)


def test_the_guard_catches_a_closer_that_does_not_wake():
    sources = _sources()
    path = "packages/core/services/task_runtime_approvals.py"
    needle = "    await wake_run_for_closed_request(db, request)\n"
    assert sources[path].count(needle) == 1
    sources[path] = sources[path].replace(needle, "")
    assert [(found[0], found[1]) for found in _violations(sources)] == [(path, "_expire_locked_runtime_request")]


def test_the_guard_catches_a_new_closer():
    sources = _sources()
    path = "packages/core/services/task_chat_hitl.py"
    sources[path] += (
        "\n\nasync def sneaky(db, request):\n"
        "    request.status = ApprovalStatus.EXPIRED.value\n"
    )
    assert [(found[0], found[1]) for found in _violations(sources)] == [(path, "sneaky")]
