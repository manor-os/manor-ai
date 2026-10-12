"""Class-level guards for the execution-path robustness contracts.

These do not test one instance of a fix; they scan the source so the same
class of regression cannot come back somewhere else in the same function.

1. Every ``continue`` in the dispatcher checkout loop must either record why
   the step was skipped (``_stamp_dispatch_skip``), move the step to a
   terminal/paused status itself, or be one of the explicitly allow-listed
   non-structural skips (retry backoff, setup blocked, target-worker
   mismatch, dependency waiting). A silent ``continue`` is exactly the
   "pending forever with no reason" class the watchdog cannot see.
2. Every ``fail_lease(`` call in the internal worker's lease body must carry
   the attempt's captured evidence, so a retry can never repeat an external
   side effect the failed attempt already performed.
3. The four dispatcher report surfaces must all check lease ownership before
   touching the step.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DISPATCHER_SRC = REPO / "packages/core/dispatcher/service.py"
WORKER_SRC = REPO / "packages/core/workers/internal.py"

# ``if <test>: ... continue`` blocks that legitimately skip without a stamp.
# Each entry is a substring that must appear in the ``if`` test's source.
_ALLOWED_UNSTAMPED_SKIP_TESTS = (
    "step_retry_ready",          # retry backoff: the real error already explains it
    "setup_status",              # workspace setup blocked: executor reports it
    "target_worker_id",          # plan pinned to another worker
    "worker.kind != INTERNAL_WORKER_KIND",  # coordinator root is SQL-filtered from external workers
    "local_worker_plan",         # local-worker plans are for paired workers
    'step.provider == "browser_setup"',
    'dep_status == "waiting"',   # dependency not done yet: expected
    "len(leased) >= max_n",      # window full for this pass
)


def _function_node(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _block_records_skip(body: list[ast.stmt]) -> bool:
    """True when the statements around a ``continue`` explain the skip."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                target = node.func
                fname = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if fname == "_stamp_dispatch_skip":
                    return True
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Attribute)
                        and tgt.attr in {"step_status"}
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "step"
                    ):
                        return True
    return False


def _parent_map(node: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def test_every_dispatcher_skip_is_stamped_or_allow_listed() -> None:
    source = DISPATCHER_SRC.read_text()
    tree = ast.parse(source)
    fn = _function_node(tree, "_checkout_steps_for_worker_inner")
    parents = _parent_map(fn)
    lines = source.splitlines()

    unexplained: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Continue):
            continue
        # Walk up to the nearest ``if`` / ``for`` that owns this continue.
        owner = parents.get(node)
        body: list[ast.stmt] | None = None
        while owner is not None and owner is not fn:
            if isinstance(owner, ast.If):
                body = owner.body if node in ast.walk(ast.Module(body=owner.body, type_ignores=[])) else owner.orelse
                break
            if isinstance(owner, ast.ExceptHandler):
                # ``except SomeError: step.step_status = FAILED; continue``
                body = owner.body
                break
            if isinstance(owner, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)):
                body = owner.body
                break
            owner = parents.get(owner)
        if body is None or not isinstance(owner, (ast.If, ast.ExceptHandler)):
            unexplained.append(f"line {node.lineno}: continue outside an if/except block")
            continue
        if _block_records_skip(body):
            continue
        if isinstance(owner, ast.ExceptHandler):
            unexplained.append(
                f"line {node.lineno}: except-block skips the step without failing it"
            )
            continue
        test_src = ast.get_source_segment(source, owner.test) or ""
        if any(marker in test_src for marker in _ALLOWED_UNSTAMPED_SKIP_TESTS):
            continue
        unexplained.append(
            f"line {node.lineno}: `if {test_src.strip()[:80]}` skips the step without "
            "_stamp_dispatch_skip or a status change"
        )
    assert not unexplained, (
        "dispatcher checkout skips a step silently — the executor watchdog can "
        "never escalate a skip it cannot see:\n" + "\n".join(unexplained)
        + f"\n(context: {lines[0][:60]}...)"
    )


def test_every_worker_fail_lease_carries_evidence() -> None:
    source = WORKER_SRC.read_text()
    tree = ast.parse(source)
    fn = _function_node(tree, "_execute_claimed_lease")
    missing: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "fail_lease":
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if "evidence_refs" not in kwargs:
            missing.append(f"line {node.lineno}")
    assert not missing, (
        "fail_lease is called without the attempt's captured evidence at: "
        + ", ".join(missing)
        + " — a retry after such a failure can repeat an external side effect"
    )


@pytest.mark.parametrize(
    "surface",
    ["_complete_lease_inner", "fail_lease", "lease_needs_human", "extend_lease"],
)
def test_every_dispatcher_report_surface_checks_lease_ownership(surface: str) -> None:
    from packages.core.dispatcher.service import Dispatcher

    src = inspect.getsource(getattr(Dispatcher, surface))
    assert re.search(r"_lease_owns_step\(", src), (
        f"Dispatcher.{surface} reports on a step without checking the lease "
        "still owns it — a stale report would overwrite a reset or cancelled step"
    )
