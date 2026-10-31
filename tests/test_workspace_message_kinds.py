"""Every Workspace message kind the backend writes is known to web and mobile.

The kind is internal: the clients render each message as a bubble or an event
row and never show the kind as a label. A kind the clients do not list used to
reach the screen as raw text (``HITL_PROMPT`` on the external reply review card).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRS = ("packages", "apps/api", "ai", "services")
BLUEPRINT_CONFIGS = ROOT / "packages/core/blueprints/configs"
CLIENT_KIND_LISTS = {
    "web": ROOT / "apps/web/src/lib/workspaceMessageKinds.ts",
}
CLIENT_SOURCE_DIRS = (
    ROOT / "apps/web/src",
)


def _string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_constants(node.body) + _string_constants(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [value for operand in node.values for value in _string_constants(operand)]
    return []


def _python_kinds(tree: ast.AST) -> set[str]:
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "message_kind":
            kinds.update(_string_constants(node.value))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "message_kind":
                    kinds.update(_string_constants(value))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args + node.args.kwonlyargs
            defaults = [None] * (len(node.args.args) - len(node.args.defaults))
            defaults += list(node.args.defaults) + list(node.args.kw_defaults)
            for arg, default in zip(args, defaults):
                if arg.arg == "message_kind" and default is not None:
                    kinds.update(_string_constants(default))
    return kinds


def _json_kinds(value: object) -> set[str]:
    if isinstance(value, dict):
        kinds = {value["message_kind"]} if isinstance(value.get("message_kind"), str) else set()
        return kinds.union(*(_json_kinds(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_json_kinds(child) for child in value))
    return set()


def _backend_kinds() -> set[str]:
    kinds: set[str] = set()
    for directory in BACKEND_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            if "tests" in path.parts or "node_modules" in path.parts:
                continue
            kinds |= _python_kinds(ast.parse(path.read_text(encoding="utf-8")))
    for path in BLUEPRINT_CONFIGS.rglob("*.json"):
        kinds |= _json_kinds(json.loads(path.read_text(encoding="utf-8")))
    return kinds


def _client_kinds(path: Path) -> set[str]:
    match = re.search(
        r"WORKSPACE_MESSAGE_KINDS\s*=\s*\[(.*?)\]\s*as const",
        path.read_text(encoding="utf-8"),
        re.S,
    )
    assert match, f"{path.relative_to(ROOT)} must define WORKSPACE_MESSAGE_KINDS"
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def test_backend_kind_scan_finds_known_writers():
    kinds = _backend_kinds()
    # channel_reply_approvals.py writes hitl_prompt; simulation stages in
    # blueprint configs write goal_alert.
    assert {"hitl_prompt", "hitl_request", "text", "goal_alert"} <= kinds


def test_clients_list_every_backend_message_kind():
    backend = _backend_kinds()
    for client, path in CLIENT_KIND_LISTS.items():
        missing = sorted(backend - _client_kinds(path))
        assert not missing, (
            f"{client} does not know message kinds {missing}; add them to "
            f"WORKSPACE_MESSAGE_KINDS in {path.relative_to(ROOT)} and render them "
            "as a bubble or an event row"
        )


RAW_KIND_DISPLAY = re.compile(
    r"(?:message_kind|messageKind)\s*\|\|"  # kind as a text fallback
    r"|\{\s*[\w.?]*(?:message_kind|messageKind)\s*\}"  # JSX interpolation
    r"|(?:message_kind|messageKind)[\w?.()]*\.(?:toUpperCase|replace)\("
    r"|label:\s*kind\b"  # kind -> label fallback
)


def test_clients_never_render_a_raw_message_kind():
    offenders = []
    for directory in CLIENT_SOURCE_DIRS:
        for path in directory.rglob("*.ts*"):
            if "__tests__" in path.parts or ".test." in path.name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if RAW_KIND_DISPLAY.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "message kinds are internal; never show them:\n" + "\n".join(offenders)
