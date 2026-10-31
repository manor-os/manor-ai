"""Guard: ``rebind_task_lifecycle`` is the only writer of a run's ``lifecycle_generation``.

A Task agent loop's root run and every open delegated child run move to a new
Task lifecycle together (a person's answer, a Task resume). A write anywhere
else would rebind one row and strand the rest: admission refuses a child on
another lifecycle than its Task, so its parent would wait forever. Rows are
created with their lifecycle (an insert's column value); after that only the
helper rewrites it. Scanned repo-wide over the production code.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DIRS = ("packages", "apps", "sandbox-service", "scripts")
_HELPER = ("packages/core/services/task_agent_loop_hitl.py", "rebind_task_lifecycle")
# Reviewed writers outside the helper, as (file, function), each with why.
_EXEMPT: set[tuple[str, str]] = set()


_FIELD = "lifecycle_generation"
_SQL_SET = re.compile(r"\bSET\b[^;]*?\blifecycle_generation\b\s*=", re.IGNORECASE | re.DOTALL)


def _names_field(node: ast.AST | None) -> bool:
    """A dict literal / dict(...) call / keyword list that sets the field."""

    if isinstance(node, ast.Dict):
        return any(isinstance(key, ast.Constant) and key.value == _FIELD for key in node.keys)
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
        return any(keyword.arg == _FIELD for keyword in node.keywords)
    return False


def _chain_calls(node: ast.AST) -> list[str]:
    """Names of the calls a method chain is built on (``update(T).where(...)`` -> update, where)."""

    names = []
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call):
            func = node.func
            names.append(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
            node = func.value if isinstance(func, ast.Attribute) else func
        else:
            node = node.value
    return names


def _core_rewrite(node: ast.Call) -> bool:
    """A Core statement that rewrites the field of existing rows.

    ``update(...).values(lifecycle_generation=...)`` (keyword or dict) and an
    upsert's ``on_conflict_do_update(set_=...)`` naming it; an insert's
    ``values`` is a row's creation and stays allowed.
    """

    name = getattr(node.func, "attr", None)
    if name == "values" and "update" in _chain_calls(node.func.value):
        return any(keyword.arg == _FIELD for keyword in node.keywords) or any(
            _names_field(arg) for arg in node.args
        )
    if name == "on_conflict_do_update":
        return any(keyword.arg == "set_" and _names_field(keyword.value) for keyword in node.keywords)
    return False


def _writers(path: str, source: str) -> list[tuple[str, str, int]]:
    found = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute) and sub.attr == "lifecycle_generation":
                    found.append((path, function, node.lineno))
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "setattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == _FIELD:
                found.append((path, function, node.lineno))
        if isinstance(node, ast.Call) and _core_rewrite(node):
            found.append((path, function, node.lineno))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL_SET.search(node.value):
            found.append((path, function, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), "<module>")
    return found


def _sources() -> dict[str, str]:
    sources = {}
    for directory in _PRODUCTION_DIRS:
        for file in (ROOT / directory).rglob("*.py"):
            if "node_modules" in file.parts or "/tests/" in str(file):
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "lifecycle_generation" in text:
                sources[str(file.relative_to(ROOT))] = text
    return sources


def _violations(sources: dict[str, str]) -> list[tuple[str, str, int]]:
    allowed = {_HELPER, *_EXEMPT}
    return [
        writer for path, source in sources.items() for writer in _writers(path, source)
        if (writer[0], writer[1]) not in allowed
    ]


def test_only_the_rebind_helper_writes_lifecycle_generation():
    sources = _sources()
    assert _violations(sources) == []
    # The scan sees the helper's own write, and every exemption still exists.
    helper_writes = [writer for writer in _writers(_HELPER[0], sources[_HELPER[0]]) if writer[1] == _HELPER[1]]
    assert len(helper_writes) >= 2, "the helper rebinds the run and its children"
    for path, function in _EXEMPT:
        assert any(writer[1] == function for writer in _writers(path, sources[path])), (path, function)


def test_the_guard_catches_a_second_writer():
    sources = _sources()
    path = "packages/core/services/task_coordinator_wakeup.py"
    needle = "    rebind_task_lifecycle(task, run, children)\n"
    assert sources[path].count(needle) == 1
    sources[path] = sources[path].replace(needle, "    run.lifecycle_generation = lifecycle_generation\n")
    assert [(found[0], found[1]) for found in _violations(sources)] == [(path, "ensure_task_coordinator_run")]


@pytest.mark.parametrize("body", [
    "    setattr(child, 'lifecycle_generation', 'x')",
    "    db.execute(update(TaskAgentChildRun).where(TaskAgentChildRun.id == 'c').values(lifecycle_generation='x'))",
    "    db.execute(sa.update(TaskAgentChildRun).values({'lifecycle_generation': 'x'}))",
    "    db.execute(update(TaskCoordinatorRun).values(dict(phase='ready', lifecycle_generation='x')))",
    "    stmt.on_conflict_do_update(index_elements=['id'], set_={'lifecycle_generation': 'x'})",
    "    stmt.on_conflict_do_update(index_elements=['id'], set_=dict(lifecycle_generation='x'))",
    "    db.execute(text('UPDATE task_agent_child_runs SET phase = 1, lifecycle_generation = :g'))",
])
def test_the_guard_catches_a_child_rebound_by_hand(body):
    sources = _sources()
    path = "packages/core/services/task_agent_delegation.py"
    sources[path] += f"\n\ndef sneaky(db, stmt, child, task):\n{body}\n"
    assert [(found[0], found[1]) for found in _violations(sources)] == [(path, "sneaky")]


def test_a_rows_creation_with_its_lifecycle_is_allowed():
    sources = {"x.py": (
        "def create(db):\n"
        "    db.execute(insert(TaskAgentChildRun).values(lifecycle_generation='g'))\n"
        "    db.execute(pg_insert(TaskCoordinatorRun).values({'lifecycle_generation': 'g'})"
        ".on_conflict_do_nothing())\n"
    )}
    assert _violations(sources) == []
