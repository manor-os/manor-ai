"""Every terminal Runtime run write goes through one of two owners.

A run ended by its own agent loop is finalized by
``complete_runtime_run_execution``. Every other writer (a failed or expired
Sandbox ticket, an unanswered question, a cancel of a parked run, a lost
worker) must call ``finish_runtime_run_out_of_loop``, which also settles the
assistant Message and tells the client. Writing a terminal status directly
left the 2026-10-07 staging chat on "Waiting to start" forever.

The scan is over the whole repository, not one file: the next sweep that ends
a run will live somewhere new.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("packages", "apps")
SKIP_PARTS = {"node_modules", "migrations", "__pycache__", ".venv", "dist", "build"}
TERMINAL_MEMBERS = {"FAILED", "CANCELLED", "COMPLETED"}
TERMINAL_LITERALS = {"failed", "cancelled", "completed"}
ALLOWED_OWNERS = {
    ("packages/core/services/runtime_run_terminal.py", "finish_runtime_run_out_of_loop"),
    ("packages/core/services/runtime_run_service.py", "complete_runtime_run_execution"),
}


OWNER_CALLS = {"finish_runtime_run_out_of_loop"}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _status_aliases(tree: ast.AST) -> set[str]:
    aliases = {"RuntimeRunStatus"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for name in node.names:
                if name.name == "RuntimeRunStatus":
                    aliases.add(name.asname or name.name)
    return aliases


def _mentions_runtime_run(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            name.name == "RuntimeRun" for name in node.names
        ):
            return True
    return False


def _is_terminal_value(node: ast.AST, aliases: set[str], *, literals: bool) -> bool:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr in TERMINAL_MEMBERS
            and isinstance(child.value, ast.Name)
            and child.value.id in aliases
        ):
            return True
        if (
            literals
            and isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in TERMINAL_LITERALS
        ):
            return True
    return False


def terminal_status_writes(source: str, relative_path: str) -> list[str]:
    """Return ``path:line in function`` for each unowned terminal status write."""

    tree = ast.parse(source)
    aliases = _status_aliases(tree)
    # String literals are only suspicious where the module handles runs at all;
    # Tasks, Plans and workflow runs have their own "failed" statuses.
    literals = _mentions_runtime_run(tree)
    violations: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            scope = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else function
            )
            if (relative_path, scope) not in ALLOWED_OWNERS:
                value = None
                targets: list[ast.AST] = []
                if isinstance(child, ast.Assign):
                    value, targets = child.value, list(child.targets)
                elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                    value, targets = child.value, [child.target]
                if value is not None and any(
                    isinstance(target, ast.Attribute) and target.attr == "status"
                    for target in targets
                ) and _is_terminal_value(value, aliases, literals=literals):
                    violations.append(f"{relative_path}:{child.lineno} in {scope}")
                if isinstance(child, ast.Call) and _callee(child) not in OWNER_CALLS:
                    for keyword in child.keywords:
                        if keyword.arg == "status" and _is_terminal_value(
                            keyword.value, aliases, literals=False,
                        ):
                            violations.append(f"{relative_path}:{child.lineno} in {scope}")
            visit(child, scope)

    visit(tree, None)
    return violations


def _python_sources():
    for root in SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if SKIP_PARTS.intersection(path.relative_to(ROOT).parts):
                continue
            yield path


def test_no_terminal_runtime_run_status_written_outside_its_owners():
    violations: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        if "RuntimeRunStatus" not in source and "RuntimeRun" not in source:
            continue
        violations.extend(
            terminal_status_writes(source, path.relative_to(ROOT).as_posix())
        )
    assert violations == [], (
        "End a Runtime run through finish_runtime_run_out_of_loop (or the loop's "
        "complete_runtime_run_execution) so its assistant Message and client are "
        "settled too:\n" + "\n".join(violations)
    )


def test_scan_actually_covers_the_known_out_of_loop_writers():
    # If the scan stopped seeing these modules it would pass vacuously.
    scanned = {path.relative_to(ROOT).as_posix() for path in _python_sources()}
    for expected in (
        "packages/core/services/sandbox_queue_service.py",
        "packages/core/services/runtime_run_service.py",
        "packages/core/services/runtime_human_input.py",
        "apps/api/routers/chat.py",
    ):
        assert expected in scanned


def test_detector_flags_each_direct_terminal_write_shape():
    source = '''
from packages.core.models.runtime_run import RuntimeRun, RuntimeRunStatus as RRS

async def sweep(run, db):
    run.status = RRS.FAILED.value

async def expire(run):
    run.status = RRS.CANCELLED

async def literal(run):
    run.status = "failed"

async def bulk(db):
    await db.execute(update(RuntimeRun).values(status=RRS.COMPLETED.value))

async def fine(run, db):
    await finish_runtime_run_out_of_loop(db, run, status=RRS.FAILED, reason="x")
    run.status = RRS.WAITING_RESOURCE.value
    if run.status == RRS.FAILED.value:
        return {"status": RRS.FAILED.value}
'''
    found = terminal_status_writes(source, "packages/x.py")
    assert [entry.rsplit(" in ", 1)[1] for entry in found] == [
        "sweep", "expire", "literal", "bulk",
    ]


def test_detector_allows_only_the_named_owners():
    source = '''
from packages.core.models.runtime_run import RuntimeRunStatus

async def finish_runtime_run_out_of_loop(db, run):
    run.status = RuntimeRunStatus.FAILED.value
'''
    assert terminal_status_writes(source, "packages/core/services/runtime_run_terminal.py") == []
    assert terminal_status_writes(source, "packages/core/services/elsewhere.py") != []


def test_literals_ignored_where_runtime_runs_are_not_handled():
    source = '''
async def workflow(run):
    run.status = "failed"
'''
    assert terminal_status_writes(source, "packages/core/ai/workflow_runner.py") == []
