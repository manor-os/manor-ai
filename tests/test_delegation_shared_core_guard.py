"""Guard: one place builds delegated results and bills delegated children.

Every ``"delegated": True`` payload (a delegated child's result, a host-ended
child, a pause), the parallel batch envelope and the ``delegation:`` billing
prefix are built only by the shared delegation core
(packages/core/ai/runtime/workspace_delegation_actions.py), inside the
functions every host calls; the few legacy sites are listed by name. A copy
elsewhere would let chat and durable Task children drift apart in what the
parent model sees or how a child is billed.
"""
from __future__ import annotations

import ast
from pathlib import Path

from packages.core.ai.runtime import workspace_delegation_actions as delegation

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(delegation.__file__).resolve()

_PRODUCTION_DIRS = ("packages", "apps", "sandbox-service", "scripts")
# Where the shared core builds each shape (function names in CORE).
_CORE_BUILDERS = {
    "delegated_payload": {
        "runtime_delegated_child_result",
        "runtime_delegated_child_failure",
        "_delegated_input_pause_payload",
        "runtime_delegation_batch_result",
    },
    "parallel_envelope": {"runtime_delegation_batch_result"},
    "delegation_billing_prefix": {"runtime_delegated_child_billing"},
}
# Reviewed "delegated": True literals outside the core, as
# (file, function, shape): count. None of them is a delegated child-run result.
_LEGACY_SITES = {
    # A workflow wait step approved by a delegate: another meaning of
    # "delegated" (who approved), not a delegated agent's result.
    ("packages/core/ai/workflow_runner.py", "_execute_wait_step", "delegated_payload"): 1,
}


def _keys(node: ast.AST) -> dict[str, ast.AST]:
    if isinstance(node, ast.Dict):
        return {key.value: value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
    return {}


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _delegation_prefix(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("delegation:")
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and str(first.value).startswith("delegation:")
    return False


def _shapes(path: Path) -> list[tuple[str, str]]:
    """(shape, enclosing function) for every guarded construct in a file."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        keys = _keys(node)
        if _is_true(keys.get("delegated")):
            found.append(("delegated_payload", function))
            if _is_true(keys.get("parallel")):
                found.append(("parallel_envelope", function))
        if _delegation_prefix(node):
            found.append(("delegation_billing_prefix", function))
        # An f-string's parts are checked as the whole f-string.
        if not isinstance(node, ast.JoinedStr):
            for child in ast.iter_child_nodes(node):
                visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), "<module>")
    return found


def _outside_the_core(paths) -> dict[tuple[str, str, str], int]:
    """Every guarded construct that is not in its shared-core builder, counted."""
    out: dict[tuple[str, str, str], int] = {}
    for path in paths:
        resolved = Path(path).resolve()
        for shape, function in _shapes(resolved):
            if resolved == CORE and function in _CORE_BUILDERS[shape]:
                continue
            try:
                name = str(resolved.relative_to(ROOT))
            except ValueError:
                name = resolved.name
            key = (name, function, shape)
            out[key] = out.get(key, 0) + 1
    return out


def _production_files():
    for directory in _PRODUCTION_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            parts = path.relative_to(ROOT).parts
            if "tests" in parts or "node_modules" in parts or ".venv" in parts:
                continue
            yield path


def test_only_the_shared_core_builds_delegated_results_and_bills_children():
    assert _outside_the_core(_production_files()) == _LEGACY_SITES, (
        "A delegated payload, the parallel envelope and the delegation: billing"
        " prefix are built only by the shared delegation core, so every host"
        " returns the same tool result and bills a child the same way. Call"
        " runtime_delegated_child_result / runtime_delegated_child_failure /"
        " runtime_delegation_batch_result / runtime_delegated_child_billing"
        " instead (or update _LEGACY_SITES when a reviewed legacy site goes away)."
    )
    # The core still builds each shape exactly where the guard expects it.
    assert {(shape, function) for shape, function in _shapes(CORE)} == {
        (shape, function) for shape, functions in _CORE_BUILDERS.items() for function in functions
    }


def test_the_guard_catches_a_copy_outside_the_shared_core(tmp_path):
    copies = {
        "result.py": 'def run():\n    return {"delegated": True, "run_id": "r", "status": "completed"}\n',
        "failure.py": 'def fail():\n    return {"delegated": True, "status": "failed", "error": "x"}\n',
        "envelope.py": 'def run(entries):\n    return dict(delegated=True, parallel=True, results=entries)\n',
        "billing.py": 'def run(build, run_id):\n    return build(source_prefix=f"delegation:{run_id}:x")\n',
        "unrelated.py": 'def run():\n    return {"delegated": False, "source": "delegation"}\n',
    }
    for name, source in copies.items():
        (tmp_path / name).write_text(source)
    assert _outside_the_core(tmp_path / name for name in copies) == {
        ("result.py", "run", "delegated_payload"): 1,
        ("failure.py", "fail", "delegated_payload"): 1,
        ("envelope.py", "run", "delegated_payload"): 1,
        ("envelope.py", "run", "parallel_envelope"): 1,
        ("billing.py", "run", "delegation_billing_prefix"): 1,
    }
