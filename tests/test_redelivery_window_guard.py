"""No outbox dedupe key is built from a fixed clock bucket.

``int(now.timestamp()) // window`` as part of a redelivery dedupe key lets two
sweeps a second apart straddle a bucket boundary and redeliver twice (fixed
in ``recover_waiting_sandbox_runs`` by dda7b4824, then found again in
``redeliver_exec_slot_resumes``). Redeliveries go through
``sandbox_queue_service._redelivery_due``, which slides the window from the
last redelivery. This guard scans every module under ``packages/`` for a
function that floor-divides a clock reading while it builds a dedupe key.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"

# Callables whose result is "now" as seconds since an epoch.
_CLOCK_ATTRS = {"timestamp", "time", "time_ns", "monotonic"}


def _is_clock_reading(node: ast.AST) -> bool:
    """``x.timestamp()`` / ``time.time()``, possibly wrapped in int()/float()/round()."""

    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "float", "round"}
        and node.args
    ):
        node = node.args[0]
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CLOCK_ATTRS
    )


def _clock_buckets(func: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.FloorDiv)
        and _is_clock_reading(node.left)
    ]


def bucketed_dedupe_keys(source: str, filename: str = "<src>") -> list[str]:
    """``filename:line`` of each clock bucket in a function that builds a dedupe key."""

    tree = ast.parse(source, filename=filename)
    found: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(source, func) or ""
        if "dedupe" not in segment.lower():
            continue
        for line in _clock_buckets(func):
            found.add(f"{filename}:{line}")
    return sorted(found)


def test_no_dedupe_key_uses_a_fixed_clock_bucket() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGES.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "//" not in source or "dedupe" not in source.lower():
            continue
        offenders.extend(bucketed_dedupe_keys(source, str(path.relative_to(ROOT))))
    assert not offenders, (
        "Dedupe keys built from timestamp // window redeliver twice across a "
        "bucket boundary; use sandbox_queue_service._redelivery_due (sliding "
        f"window) instead: {offenders}"
    )


def test_the_guard_sees_a_bucketed_key_and_ignores_durations_and_totp() -> None:
    bucketed = '''
async def redeliver(db, reservation, current_time, window):
    bucket = int(current_time.timestamp()) // max(1, window)
    dedupe_key = f"runtime.resume_run:{reservation.id}:redeliver:{bucket}"
'''
    assert bucketed_dedupe_keys(bucketed) == ["<src>:3"]
    clock = '''
def key(event):
    step = time.time() // 30
    return {"dedupe_key": f"{event}:{step}"}
'''
    assert bucketed_dedupe_keys(clock) == ["<src>:3"]
    unrelated = '''
def totp(secret):
    return int(time.time()) // 30

def duration(since, dedupe_key):
    return (int(time.time()) - since) // 60
'''
    assert bucketed_dedupe_keys(unrelated) == []


def test_both_redelivery_paths_use_the_sliding_window_helper() -> None:
    source = (PACKAGES / "core" / "services" / "sandbox_queue_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    callers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.AsyncFunctionDef)
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_redelivery_due"
            for node in ast.walk(func)
        )
    }
    assert {"recover_waiting_sandbox_runs", "redeliver_exec_slot_resumes"} <= callers
    # Every redelivery dedupe key is minted by the helper alone.
    assert source.count(':redeliver:"') == 1, "build :redeliver: keys only via _resume_redelivery_prefix"
