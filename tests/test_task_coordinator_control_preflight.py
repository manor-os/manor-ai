"""Coordinator control-tool argument errors are pre-mutation preflight results.

A plain JSON error from a serial, non-read-only tool is treated as an
unconfirmed outcome: tools are stripped and the Task can end BLOCKED over a
typo. Every argument error in ``_execute_tool`` must use the host carrier.

The guard scans ``return json.dumps(...)`` statements in ``_execute_tool`` for
the canonical error shapes: a dict literal with ``"status": "error"`` or an
``"error"`` key, a ``tool_error_payload(...)`` call, or a name assigned from
such a dict literal in the same function. A return that is not allowlisted
below fails the guard. Returns of ``RuntimePreflightValidationError(...)`` are
a different carrier and are never flagged.
"""

from __future__ import annotations

import ast
import pathlib

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "packages/core/workers/task_coordinator.py"

# Plain error returns that are correct because the tool already mutated state
# (or deliberately fails closed), identified by the error code they return.
# Each must still be produced by a flagged return in the source.
POST_MUTATION_ALLOWLIST = {
    # plan_task: `_block_repair_required(...)` has already written the Task's
    # blocked state and `state["completed"]` is set before this is returned.
    "task_capability_unavailable",
    "manual_plan_submission_invalid",
    # replan_task, outcome FAILED: `prepare_task_coordinator_replan` has been
    # committed and the terminal state set before this is returned. The
    # terminal_tool_result_policy matches this exact error to end the run.
    "task_replan_unavailable",
}

# Not post-mutation: inspect_task_delivery is a read, but a failure to build a
# safe page from the delivered evidence is a defect in that evidence, not a
# model-correctable argument. The source comment keeps the serial loop's
# fail-closed path on purpose, so it is the one deliberate non-preflight error.
DELIBERATE_FAIL_CLOSED_ALLOWLIST = {"exc.code"}

ALLOWLIST = POST_MUTATION_ALLOWLIST | DELIBERATE_FAIL_CLOSED_ALLOWLIST


def _execute_tool_node(tree: ast.AST | None = None) -> ast.AsyncFunctionDef:
    tree = tree if tree is not None else ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_tool":
            return node
    raise AssertionError("_execute_tool not found")


def _is_const(node: ast.AST | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _is_error_dict(node: ast.AST) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    for key, value in zip(node.keys, node.values):
        if _is_const(key, "error"):
            return True
        if _is_const(key, "status") and _is_const(value, "error"):
            return True
    return False


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name
    )


def _resolve_codes(expr: ast.AST | None, func: ast.AST) -> set[str]:
    """The error code string(s) an expression can carry, else its source text."""
    if expr is None:
        return {"<no code>"}
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.Name):
        found: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == expr.id for t in node.targets
            ):
                found |= {
                    n.value for n in ast.walk(node.value)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                }
        return found or {expr.id}
    return {ast.unparse(expr)}


def _dict_code(node: ast.Dict, func: ast.AST) -> set[str]:
    for key, value in zip(node.keys, node.values):
        if _is_const(key, "error"):
            return _resolve_codes(value, func)
    return {"<no code>"}


def _error_codes(arg: ast.AST, func: ast.AST, error_dict_names: dict[str, ast.Dict]) -> set[str]:
    """Codes of every canonical error shape inside a ``json.dumps`` argument."""
    if _is_call_to(arg, "tool_error_payload"):
        code = next((kw.value for kw in arg.keywords if kw.arg == "code"), None)
        return _resolve_codes(code, func)
    if _is_error_dict(arg):
        return _dict_code(arg, func)
    if isinstance(arg, ast.Name) and arg.id in error_dict_names:
        return _dict_code(error_dict_names[arg.id], func)
    codes: set[str] = set()
    for child in ast.iter_child_nodes(arg):
        codes |= _error_codes(child, func, error_dict_names)
    return codes


def _flagged_returns(func: ast.AsyncFunctionDef) -> list[tuple[int, set[str]]]:
    error_dict_names: dict[str, ast.Dict] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_error_dict(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    error_dict_names[target.id] = node.value
    flagged = []
    for node in ast.walk(func):
        value = node.value if isinstance(node, ast.Return) else None
        if not (_is_call_to(value, "dumps") and value.args):
            continue
        codes = _error_codes(value.args[0], func, error_dict_names)
        if codes:
            flagged.append((node.lineno, codes))
    return flagged


def _offenders(func: ast.AsyncFunctionDef) -> list[tuple[int, set[str]]]:
    return [(line, codes) for line, codes in _flagged_returns(func) if not codes <= ALLOWLIST]


def test_execute_tool_returns_no_plain_json_errors():
    offenders = _offenders(_execute_tool_node())
    assert offenders == [], (
        "wrap these in _control_argument_error(...) / RuntimePreflightValidationError, "
        f"or allowlist a real post-mutation error: {offenders}"
    )


def test_allowlist_entries_are_all_present_in_the_source():
    produced: set[str] = set()
    for _line, codes in _flagged_returns(_execute_tool_node()):
        produced |= codes
    assert ALLOWLIST - produced == set(), f"stale allowlist entries: {sorted(ALLOWLIST - produced)}"


def test_guard_fails_on_an_unallowlisted_tool_error_payload_return():
    """Mutation check on the real source, in memory only."""
    tree = ast.parse(SOURCE.read_text())
    func = _execute_tool_node(tree)
    injected = ast.parse(
        'return json.dumps(tool_error_payload(tool_name=name, code="oops", message="x"))'
    ).body[0]
    func.body.insert(0, injected)
    ast.fix_missing_locations(tree)
    assert any("oops" in codes for _line, codes in _offenders(func))


def _parse_func(source: str) -> ast.AsyncFunctionDef:
    return _execute_tool_node(ast.parse(source))


def test_detector_flags_each_canonical_error_shape():
    shapes = {
        "status_error_dict": 'return json.dumps({"status": "error", "error": "a"})',
        "error_key_dict": 'return json.dumps({"error": "b", "x": 1})',
        "tool_error_payload": 'return json.dumps(tool_error_payload(tool_name=n, code="c", message="m"))',
        "assigned_name": 'payload = {"status": "error", "error": "d"}\n    return json.dumps(payload)',
        "conditional": 'return json.dumps(x if x else tool_error_payload(tool_name=n, code="e", message="m"))',
    }
    expected = {
        "status_error_dict": "a", "error_key_dict": "b", "tool_error_payload": "c",
        "assigned_name": "d", "conditional": "e",
    }
    for label, body in shapes.items():
        func = _parse_func(f"async def _execute_tool(n):\n    {body}\n")
        assert [codes for _l, codes in _flagged_returns(func)] == [{expected[label]}], label


def test_detector_resolves_a_code_variable_and_ignores_other_carriers():
    func = _parse_func(
        "async def _execute_tool(n):\n"
        '    code = "p" if n else "q"\n'
        "    if n:\n"
        "        return json.dumps(tool_error_payload(tool_name=n, code=code, message='m'))\n"
        '    if not n:\n        return json.dumps({"status": "blocked", "reason": "r"})\n'
        '    return RuntimePreflightValidationError(json.dumps({"status": "error", "error": "z"}))\n'
    )
    assert [codes for _l, codes in _flagged_returns(func)] == [{"p", "q"}]


def test_helper_returns_the_host_preflight_carrier():
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
    from packages.core.workers.task_coordinator import _control_argument_error

    result = _control_argument_error("unexpected_arguments")
    assert isinstance(result, RuntimePreflightValidationError)
    assert '"unexpected_arguments"' in str(result)


def test_continuation_is_forbidden_after_an_unconfirmed_effect():
    from types import SimpleNamespace

    from packages.core.workers.task_coordinator import _continuation_forbidden

    assert _continuation_forbidden(SimpleNamespace(control={"do_not_replay": True})) is True
    assert _continuation_forbidden(SimpleNamespace(control={"kind": "tool_error", "retryable": True})) is False
    assert _continuation_forbidden(SimpleNamespace(control=None)) is False
    # A stop with no outcome control (max_rounds) still left an unconfirmed
    # effect in the receipt ledger: re-waking with tools could repeat it.
    assert _continuation_forbidden(SimpleNamespace(
        control=None,
        tool_evidence={"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1"}]},
    )) is True
    assert _continuation_forbidden(SimpleNamespace(
        control=None, tool_evidence={"effect_state": "confirmed", "unconfirmed_calls": []},
    )) is False
