"""Guard: what a WorkflowProject run makes lands in that project's folder, repo-wide.

A run that works on a WorkflowProject carries the project's folder
(``workflow_project_root``) into every tool call. The project's App view
("Project folder files") and its reviews list only that folder, so a file
written anywhere else is invisible to them. Each writer that picked its
folder from the run id alone, or from the Workspace home, put a run's files
outside the project: run archives (#1618), the Workspace root (#1633),
``code/<slug>/`` through ``code_project``, the archived file collection, and
video clips the worker refused.

The checks scan every Python file under ``packages/`` and the Python apps
(the class, not the instances fixed so far).
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("packages", "apps/api", "apps/wechat_personal_runner")

# Both pick the folder for a run's new files; given a run, they need its project.
_RUN_FOLDER_RESOLVERS = frozenset({"workspace_execution_artifact_dir", "resolve_workspace_artifact_base_dir"})


@lru_cache(maxsize=1)
def _python_sources() -> tuple[tuple[str, str], ...]:
    sources = []
    for scan_root in SCAN_ROOTS:
        for path in sorted((ROOT / scan_root).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" not in rel:
                sources.append((rel, path.read_text(encoding="utf-8")))
    return tuple(sources)


class _CallFinder(ast.NodeVisitor):
    """Calls to ``names`` with the function each one sits in."""

    def __init__(self, names: frozenset[str]):
        self.names = names
        self.function = ""
        self.found: list[tuple[str, ast.Call]] = []

    def visit_FunctionDef(self, node):
        outer, self.function = self.function, node.name
        self.generic_visit(node)
        self.function = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in self.names:
            self.found.append((self.function, node))
        self.generic_visit(node)


def _calls(names: frozenset[str]):
    for rel, text in _python_sources():
        if not any(name in text for name in names):
            continue
        finder = _CallFinder(names)
        finder.visit(ast.parse(text, filename=rel))
        for function, node in finder.found:
            yield rel, function, node


def test_a_run_folder_is_never_chosen_without_the_runs_project():
    """Passing a run id without its project root selects the run archive even
    when the run works on a WorkflowProject."""
    seen = []
    offenders = []
    for rel, function, call in _calls(_RUN_FOLDER_RESOLVERS):
        keywords = {keyword.arg for keyword in call.keywords}
        seen.append(rel)
        if "workflow_run_id" in keywords and "workflow_project_root" not in keywords and None not in keywords:
            offenders.append(f"{rel}:{call.lineno} ({function})")
    assert len(seen) > 20, "the scan no longer finds the resolver calls"
    assert not offenders, (
        "Pass workflow_project_root wherever workflow_run_id picks a folder, so a run that "
        "works on a WorkflowProject writes into the project's folder:\n" + "\n".join(offenders)
    )


def test_only_the_shared_rule_places_a_new_code_project():
    """``code/`` is the default home for a code project; a run working on a
    WorkflowProject makes it in the project's folder instead."""
    callers = {(rel, function) for rel, function, _ in _calls(frozenset({"project_parent_dir"}))}
    assert callers == {("packages/core/ai/runtime/code_projects.py", "new_project_parent")}, (
        "Choose a new code project's folder with code_projects.new_project_parent, "
        f"which knows the run's project folder; found {sorted(callers)}"
    )


def test_every_file_writing_tool_resolves_its_folder_through_the_run():
    """A file-writing tool either takes its folder from the shared run resolver
    (the same one its approval uses) or is listed here with its own
    project-aware rule and a test of it."""
    from packages.core.ai.runtime.authorization_receipts import WorkspaceFileAuthorizationResourceFactory
    from packages.core.ai.runtime.capability_bindings import _FILE_WRITE_RUNTIME_TOOLS
    from packages.core.ai.runtime.file_context import (
        FILE_WRITE_TOOLS,
        MANAGED_OUTPUT_FILE_TOOLS,
        SANDBOX_SCOPED_FILE_TOOLS,
    )

    own_rule = {
        # code_projects.new_project_parent; test_code_project_tool covers a project run.
        "code_project",
    }
    writers = FILE_WRITE_TOOLS | _FILE_WRITE_RUNTIME_TOOLS | MANAGED_OUTPUT_FILE_TOOLS
    unrouted = {
        name for name in writers - own_rule - SANDBOX_SCOPED_FILE_TOOLS
        if not WorkspaceFileAuthorizationResourceFactory.supports_workspace_scope(name)
    }
    assert not unrouted, (
        "Route these tools' Workspace writes through the shared folder resolver "
        f"(workspace_execution_artifact_dir) or give them a project-aware rule: {sorted(unrouted)}"
    )
