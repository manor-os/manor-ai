"""Every chat-surface string literal in the codebase must be a real ChatSurface.

A surface picks the runtime profile, tool policy and audience. A literal that
is not a ChatSurface value fails only when its branch runs (a fallback, a rare
entry point), so it is checked here for the whole repository instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

from packages.core.ai.runtime.surfaces import ChatSurface

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("packages", "apps/api")
VALID = {surface.value for surface in ChatSurface}


def _sources():
    for base in SOURCE_DIRS:
        for path in (ROOT / base).rglob("*.py"):
            if {"tests", "migrations", "node_modules"} & set(path.parts):
                continue
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _annotation_is_chat_surface(annotation: ast.AST | None) -> bool:
    return annotation is not None and "ChatSurface" in ast.unparse(annotation)


def _chat_surface_callables(trees) -> set[str]:
    """Functions and classes whose ``surface`` parameter/field is a ChatSurface."""
    names: set[str] = set()
    for _, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.args + node.args.kwonlyargs + node.args.posonlyargs
                if any(a.arg == "surface" and _annotation_is_chat_surface(a.annotation) for a in args):
                    names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                if any(
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                    and item.target.id == "surface"
                    and _annotation_is_chat_surface(item.annotation)
                    for item in node.body
                ):
                    names.add(node.name)
    return names


def _value_literals(expr: ast.AST):
    """String literals that can become the argument's value (not comparison operands)."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        yield expr
    elif isinstance(expr, ast.BoolOp):
        for value in expr.values:
            yield from _value_literals(value)
    elif isinstance(expr, ast.IfExp):
        yield from _value_literals(expr.body)
        yield from _value_literals(expr.orelse)
    elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "str":
        for arg in expr.args:
            yield from _value_literals(arg)


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _surface_literals(trees, surface_callables):
    for path, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node)
            exprs = [kw.value for kw in node.keywords if kw.arg == "runtime_surface"]
            if callee in surface_callables:
                exprs += [kw.value for kw in node.keywords if kw.arg == "surface"]
            if callee == "normalize_surface" and node.args:
                exprs.append(node.args[0])
            for expr in exprs:
                for literal in _value_literals(expr):
                    yield path.relative_to(ROOT), literal.lineno, literal.value


def test_chat_surface_literals_are_real_surfaces():
    trees = list(_sources())
    surface_callables = _chat_surface_callables(trees)
    assert "runtime_stream_chat_turn" in surface_callables
    found = list(_surface_literals(trees, surface_callables))
    invalid = [f"{path}:{line} {value!r}" for path, line, value in found if value not in VALID]
    assert not invalid, "Unknown chat surface literals:\n" + "\n".join(invalid)
    # The scan must keep seeing call sites, or it guards nothing.
    assert len(found) >= 3


def test_guard_rejects_an_invented_fallback():
    tree = ast.parse(
        "def runtime_stream_chat_turn(m, c, *, surface: ChatSurface): pass\n"
        'runtime_stream_chat_turn(m, c, surface=str(p.get("surface") or "main_assistant_chat"))\n'
        'unrelated(surface="main_assistant_chat")\n'
    )
    trees = [(ROOT / "guard_example.py", tree)]
    found = list(_surface_literals(trees, _chat_surface_callables(trees)))
    assert [(str(path), line, value) for path, line, value in found] == [
        ("guard_example.py", 2, "main_assistant_chat"),
    ]
    assert found[0][2] not in VALID
