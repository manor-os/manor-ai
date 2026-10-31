"""Every runner container is admitted by the sandbox session service.

Until 2026-10 the generic sandbox tool and sandbox Skills went through
``sandbox_session_service`` while Video Edit (session and render containers)
and workflow code nodes (and the ``bash`` tool, still pending) built their own
``SandboxClient`` on ``SANDBOX_SERVICE_URL`` and created containers directly.
Those containers took runner slots the scheduler could not see (queued runs
then hit the runner's 429 and failed), had no instance row (a cancelled run
could not destroy its render, multi-runner routing ignored them) and failed at
once on a full runner instead of waiting.

The class of mistake is "a module other than the entry point talks to a
runner on its own", so this guard scans every production module of the
repository (``git ls-files packages apps``) instead of naming the old
offenders: nothing outside the allowed sites may construct ``SandboxClient``,
import it, call ``create_from_files``/``create_from_builtin``, or name the
runner's create endpoint. Clients for existing containers come from
``sandbox_session_service.sandbox_client_for`` / ``sandbox_runner_client``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from tests.source_inventory import production_python_files

ROOT = Path(__file__).resolve().parents[1]

ENTRY_POINT = "packages/core/services/sandbox_session_service.py"
SCHEDULER = "packages/core/tasks/runtime_tasks.py"
# The scheduler's own runner calls: allocation the session service's tickets
# drive, the health/listing sweep, the queue handover and the cleanup of an
# allocation that could not be committed. Kept exact so a stale entry fails.
SCHEDULER_FUNCTIONS = {
    "_refresh_runner_health": "runner health and container listing",
    "_create_reserved_sandbox": "creates the container a reservation was claimed for",
    "_hand_finished_sandboxes_to_queue": "destroys idle warm containers for queued tickets",
    "allocate_reserved_sandbox_once": "destroys a container whose allocation did not commit",
}
# The SDK itself is the client.
SDK_PREFIX = "packages/core/services/sandbox_sdk/"
# The entry point's own create helper (sandbox-toolset-image): the
# baked-or-upload choice for a built-in Skill. Calling it IS a create, so
# only the entry point and the scheduler may (see _CREATE_METHODS).
ENTRY_POINT_HELPERS = {
    ("packages/core/services/sandbox_baked_skills.py", "create_builtin_sandbox"),
}
# Direct runner users kept on purpose, per (module, function). Exact: a
# function that stops talking to a runner must leave this list. None left:
# bash runs in the caller's own sandbox, admitted like the generic sandbox
# tool (bash-in-sandbox).
PENDING_DIRECT_USERS: set[tuple[str, str]] = set()

_CREATE_METHODS = frozenset({"create_from_files", "create_from_builtin", "create_builtin_sandbox"})
_CREATE_ENDPOINT = "/api/v1/sandbox/create"


@dataclass(frozen=True)
class Finding:
    path: str
    function: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} ({self.function or '<module>'}): {self.what}"


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: list[str] = []
        self.findings: list[Finding] = []

    def _add(self, node: ast.AST, what: str) -> None:
        self.findings.append(Finding(self.path, self.stack[0] if self.stack else "", node.lineno, what))

    def _visit_function(self, node: ast.AST) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name == "SandboxClient":
            self._add(node, "constructs SandboxClient")
        elif name in _CREATE_METHODS:
            self._add(node, f"calls {name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.endswith("sandbox_sdk.client") or any(alias.name == "SandboxClient" for alias in node.names):
            self._add(node, f"imports SandboxClient from {module}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if any(alias.name.endswith("sandbox_sdk.client") for alias in node.names):
            self._add(node, "imports the sandbox SDK client module")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _CREATE_ENDPOINT in node.value:
            self._add(node, "names the runner's create endpoint")


def scan_source(path: str, source: str) -> list[Finding]:
    scanner = _Scanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.findings


def _production_modules() -> list[str]:
    return [
        path
        for path in production_python_files(ROOT)
        if not path.startswith(SDK_PREFIX)
        and "/tests/" not in path
        and not Path(path).name.startswith("test_")
    ]


def _allowed(finding: Finding) -> bool:
    if finding.path == ENTRY_POINT:
        return True
    if (finding.path, finding.function) in PENDING_DIRECT_USERS | ENTRY_POINT_HELPERS:
        return True
    return finding.path == SCHEDULER and finding.function in SCHEDULER_FUNCTIONS


def _repository_findings() -> list[Finding]:
    findings: list[Finding] = []
    for path in _production_modules():
        source = (ROOT / path).read_text(encoding="utf-8")
        findings.extend(scan_source(path, source))
    return findings


def test_no_module_but_the_entry_point_and_the_scheduler_talks_to_a_runner_on_its_own() -> None:
    offenders = [str(finding) for finding in _repository_findings() if not _allowed(finding)]
    assert offenders == [], (
        "Admit containers through sandbox_session_service.admit_sandbox and reach existing ones "
        "through sandbox_client_for / sandbox_runner_client:\n  " + "\n  ".join(offenders)
    )


def test_the_allowed_sites_are_exact_and_still_seen() -> None:
    findings = _repository_findings()
    scheduler_functions = {finding.function for finding in findings if finding.path == SCHEDULER}
    # A function that no longer talks to a runner must leave the allowlist.
    assert scheduler_functions == set(SCHEDULER_FUNCTIONS)
    outside = {
        (finding.path, finding.function)
        for finding in findings
        if finding.path not in {SCHEDULER, ENTRY_POINT}
    }
    assert outside == PENDING_DIRECT_USERS | ENTRY_POINT_HELPERS
    entry = [finding for finding in findings if finding.path == ENTRY_POINT]
    # Sanity: the scanner still sees the entry point's own client and creates.
    assert {finding.what for finding in entry} >= {
        "constructs SandboxClient",
        "calls create_from_files",
        # A built-in Skill: the baked-or-upload helper (sandbox-toolset-image).
        "calls create_builtin_sandbox",
    }
    assert len(_production_modules()) > 500, "the scan no longer covers the repository"


def test_the_detector_flags_every_way_around_the_entry_point() -> None:
    cases = {
        "construct": "from packages.core.services.sandbox_sdk import SandboxClient\nSandboxClient(base_url='x')\n",
        "attribute construct": "import packages.core.services.sandbox_sdk as sdk\nsdk.SandboxClient(base_url='x')\n",
        "aliased import": "from packages.core.services.sandbox_sdk import SandboxClient as C\n",
        "client module": "import packages.core.services.sandbox_sdk.client\n",
        "create files": "async def f(c):\n    await c.create_from_files(skill_name='s', files={})\n",
        "create builtin": "async def f(c):\n    await c.create_from_builtin(skill_name='s', files={})\n",
        "create builtin helper": "async def f(c):\n    await create_builtin_sandbox(c, skill_name='s', files={}, baked=None)\n",
        "raw endpoint": "async def f(h):\n    await h.post('/api/v1/sandbox/create-from-files', json={})\n",
    }
    for label, source in cases.items():
        assert scan_source("packages/example.py", source), label
    # Operating an existing container through the entry point is fine.
    assert not scan_source(
        "packages/example.py",
        "from packages.core.services.sandbox_session_service import sandbox_client_for\n"
        "async def f(sid):\n    c = await sandbox_client_for(sid)\n    await c.exec(sandbox_id=sid, command='ls')\n",
    )
