"""Request-path LLM classifiers must not run inside an open transaction.

(Auto Chat intent planning no longer waits on a model: its classifiers run
beside the turn, see ``start_deferred_chat_intent``.)

An open session that took a row lock or flushed keeps its database admission
permit and pooled connection until it commits. On a small pool that starves
every concurrent request for the length of the classifier timeout.
"""
from __future__ import annotations

import ast
from pathlib import Path

CHAT_ROUTER = Path(__file__).resolve().parents[1] / "apps" / "api" / "routers" / "chat.py"


def _is_db_commit(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and isinstance(stmt.value.value, ast.Call)
        and ast.unparse(stmt.value.value.func) == "db.commit"
    )


# Request-path LLM classifiers awaited directly by the chat router.
CLASSIFIERS = {"classify_workspace_intent"}


def _contains_classifier_call(stmt: ast.stmt) -> bool:
    return any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) in CLASSIFIERS
        for node in ast.walk(stmt)
    )


def test_every_chat_llm_classification_is_preceded_by_a_commit() -> None:
    tree = ast.parse(CHAT_ROUTER.read_text(encoding="utf-8"))
    checked = 0
    for block_owner in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(block_owner, field, None)
            if not isinstance(body, list):
                continue
            for index, stmt in enumerate(body):
                # Only statements that *are* the call site, not the enclosing
                # function/if that merely contains it somewhere below.
                if not isinstance(stmt, (ast.Assign, ast.Expr)):
                    continue
                if not _contains_classifier_call(stmt):
                    continue
                checked += 1
                assert index > 0 and _is_db_commit(body[index - 1]), (
                    f"line {stmt.lineno}: commit the request transaction "
                    "(`await db.commit()`) immediately before awaiting an "
                    "LLM classifier"
                )
    assert checked == 1, "expected the workspace-intent call site"
