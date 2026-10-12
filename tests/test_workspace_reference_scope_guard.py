"""Guard the class: Workspace membership is the home, never one run archive.

Inside a Workflow or Task, ``resolve_workspace_artifact_base_dir`` returns that
execution's archive (``<home>/workflow/<run>``), while shared Workspace files
(``generate_file(storage_scope='workspace')``) live in the home itself. A
membership check that rejects a path for being "outside"/"another" Workspace by
prefix-matching the resolved base therefore rejects the Workspace's own files.
Such a check must derive the home with ``workspace_storage_root`` (or compare
Workspace identities) instead.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_REJECTION = re.compile(r"\b(outside|another|different)\b[^.]*\bWorkspace", re.I)
_HOME_DERIVATIONS = (
    "workspace_storage_root",
    "workspace_artifact_storage_base",
    "infer_workspace_id_from_storage_path",
)


def _derives_home(node: ast.AST) -> bool:
    return any(
        isinstance(call, ast.Call)
        and (getattr(call.func, "id", None) or getattr(call.func, "attr", None)) in _HOME_DERIVATIONS
        for call in ast.walk(node)
    )


def _home_names(function: ast.AST) -> set[str]:
    """Names bound to a derived Workspace home (``home = workspace_storage_root(base)``)."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and _derives_home(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)}
    return names


def _prefix_checks_against_a_resolved_base(function: ast.AST) -> list[ast.Call]:
    """``x.startswith(...)`` whose argument uses a resolved base rather than the derived home."""
    homes = _home_names(function)
    offenders = []
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ):
            continue
        for argument in node.args:
            if _derives_home(argument):
                continue
            names = {name.id for name in ast.walk(argument) if isinstance(name, ast.Name)}
            if any("base" in name.lower() and name not in homes for name in names):
                offenders.append(node)
    return offenders


def workspace_membership_checks_against_a_run_archive(root: Path = ROOT) -> list[str]:
    offenders = []
    for path in sorted((root / "packages").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "Workspace" not in source or "startswith" not in source:
            continue
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rejects_membership = any(
                isinstance(constant, ast.Constant)
                and isinstance(constant.value, str)
                and _REJECTION.search(constant.value)
                for statement in ast.walk(function) if isinstance(statement, ast.Raise)
                for constant in ast.walk(statement)
            )
            if rejects_membership and _prefix_checks_against_a_resolved_base(function):
                offenders.append(f"{path.relative_to(root)}::{function.name}")
    return offenders


def test_workspace_membership_checks_use_the_workspace_home():
    assert workspace_membership_checks_against_a_run_archive() == []


_REJECTED = "        raise PermissionError('Reference is outside the active Workspace')\n"


@pytest.mark.parametrize("body, flagged", [
    # The original bug: membership against the run archive.
    ("    if not rel_path.startswith(f'{workspace_base_dir}/'):\n", True),
    # The home is derived but the check still uses the archive (the mutation must be caught).
    ("    home = workspace_storage_root(workspace_base_dir)\n"
     "    if not rel_path.startswith(f'{workspace_base_dir}/'):\n", True),
    ("    home = workspace_storage_root(workspace_base_dir)\n"
     "    if not (rel_path.startswith(f'{home}/') or rel_path.startswith(workspace_base_dir + '/')):\n", True),
    ("    home = workspace_storage_root(workspace_base_dir)\n"
     "    if not rel_path.startswith(f'{home}/'):\n", False),
    ("    if not rel_path.startswith(workspace_storage_root(workspace_base_dir) + '/'):\n", False),
    ("    workspace_base = workspace_artifact_storage_base(folder_id)\n"
     "    if not rel_path.startswith(f'{workspace_base}/'):\n", False),
])
def test_guard_flags_prefix_checks_against_the_resolved_base(tmp_path, body, flagged):
    module = tmp_path / "packages" / "tool.py"
    module.parent.mkdir()
    module.write_text(
        "from x import workspace_storage_root, workspace_artifact_storage_base\n"
        "def check(rel_path, workspace_base_dir, folder_id):\n" + body + _REJECTED,
        encoding="utf-8",
    )
    expected = ["packages/tool.py::check"] if flagged else []
    assert workspace_membership_checks_against_a_run_archive(tmp_path) == expected


def test_guard_catches_the_video_edit_regression_in_place(tmp_path):
    """Mutation check against the real module: keep the home line, restore the old prefix."""
    source = (ROOT / "packages/core/ai/tools/video_edit_tools.py").read_text(encoding="utf-8")
    fixed = 'if not home or not rel_path.startswith(f"{home}/"):'
    assert fixed in source
    mutated = source.replace(fixed, 'if not home or not rel_path.startswith(f"{workspace_base_dir}/"):')
    module = tmp_path / "packages" / "video_edit_tools.py"
    module.parent.mkdir()
    module.write_text(mutated, encoding="utf-8")
    assert workspace_membership_checks_against_a_run_archive(tmp_path) == [
        "packages/video_edit_tools.py::_assert_workspace_reference_scope"]
