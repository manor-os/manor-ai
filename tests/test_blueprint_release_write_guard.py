"""Guard the class: a Blueprint payload becomes current only through a release.

``workspace_blueprints.payload`` and ``client_workspace_deliveries.payload``
are what installed Workspaces upgrade toward. Every release must also be an
immutable ``blueprint_versions`` row, so the only code allowed to write those
columns is ``packages/core/blueprints/registry.py``: ``record_release`` for a
release, ``set_draft_payload`` for unpublished content and
``set_listing_manifest`` for listing copy outside the fingerprinted content.

The scan covers ``packages/``, ``apps/`` and ``scripts/`` and flags:

1. any assignment to an attribute named ``payload`` (other models' ``payload``
   attributes are allowlisted by function, with the model named);
2. ``setattr(obj, "payload", ...)``, and a dynamic ``setattr`` in a function
   that names a ``"payload"`` field;
3. ``WorkspaceBlueprint(...)`` / ``ClientWorkspaceDelivery(...)`` constructed
   with ``payload=`` (a Blueprint born as a draft is allowed) or with
   ``**fields`` in a function that names a ``"payload"`` field;
4. SQL ``update(...)``/``insert(...)`` of either table that sets ``payload``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

RELEASE_WRITERS = {
    "packages/core/blueprints/registry.py::record_release",
    "packages/core/blueprints/registry.py::set_draft_payload",
    "packages/core/blueprints/registry.py::set_listing_manifest",
}

# ``payload`` attributes of unrelated models and objects.
OTHER_PAYLOAD_WRITES = {
    "packages/core/ai/tools/bash_tool.py::_StagingRefused.__init__": "an exception's payload",
    "packages/core/services/notification_scheduler.py::_record_delivered_target":
        "NotificationOutboxEvent.payload",
    "packages/core/services/runtime_learning.py::_upsert_learning_candidate":
        "AgentLearningCandidate.payload",
    "packages/core/services/task_chat_hitl.py::ensure_task_recovery_hitl": "HitlRequest.payload",
    "apps/api/routers/workspace_chat.py::resolve_chat_action": "a pending-action request body",
}

GUARDED_MODELS = {"WorkspaceBlueprint", "ClientWorkspaceDelivery"}
GUARDED_TABLES = re.compile(
    r"\b(?:update|insert\s+into)\s+(?:workspace_blueprints|client_workspace_deliveries)\b[^;]*\bpayload\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Write:
    location: str  # path::dotted enclosing function
    line: int
    reason: str


def _sources(root: Path):
    for top in ("packages", "apps", "scripts"):
        for path in sorted((root / top).rglob("*.py")):
            relative = path.relative_to(root)
            parts = relative.parts
            if "tests" in parts or "node_modules" in parts or "migrations" in parts:
                continue
            yield relative.as_posix(), path.read_text(encoding="utf-8")


def _names_payload_field(function: ast.AST) -> bool:
    """A ``"payload"`` field a loop could ``setattr``: a dict key, a member of
    a literal field list, or ``dict(payload=...)``."""
    for node in ast.walk(function):
        if isinstance(node, ast.Dict):
            members = node.keys
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            members = node.elts
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            if any(keyword.arg == "payload" for keyword in node.keywords):
                return True
            continue
        else:
            continue
        if any(isinstance(member, ast.Constant) and member.value == "payload" for member in members):
            return True
    return False


def _is_draft_status(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "draft"
    text = ast.unparse(node)
    return text in {"BlueprintStatus.DRAFT.value", "BlueprintStatus.DRAFT"}


def _attribute_targets(target: ast.AST):
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _attribute_targets(element)
    elif isinstance(target, ast.Starred):
        yield from _attribute_targets(target.value)
    elif isinstance(target, ast.Attribute) and target.attr == "payload":
        yield target


def _chain_models(node: ast.AST) -> set[str]:
    """Models named by ``update(Model)`` / ``insert(Model)`` in a call chain."""
    found = set()
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in {"update", "insert", "pg_insert"}
            and inner.args
            and isinstance(inner.args[0], ast.Name)
        ):
            found.add(inner.args[0].id)
    return found


def scan(relative: str, text: str) -> list[Write]:
    tree = ast.parse(text, filename=relative)
    writes: list[Write] = []

    def visit(node: ast.AST, scope: tuple[str, ...], function: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner_function = node if not isinstance(node, ast.ClassDef) else function
            for child in ast.iter_child_nodes(node):
                visit(child, (*scope, node.name), inner_function)
            return
        location = f"{relative}::{'.'.join(scope) or '<module>'}"

        def flag(reason: str) -> None:
            writes.append(Write(location, node.lineno, reason))

        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for attribute in _attribute_targets(target):
                    flag(f"assigns {ast.unparse(attribute)}")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if name == "setattr" and len(node.args) >= 2:
                field = node.args[1]
                if isinstance(field, ast.Constant) and field.value == "payload":
                    flag("setattr(..., 'payload', ...)")
                elif not isinstance(field, ast.Constant) and function is not None and _names_payload_field(function):
                    flag("dynamic setattr in a function naming a 'payload' field")
            if name in GUARDED_MODELS:
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                if "payload" in keywords and not (
                    name == "WorkspaceBlueprint" and _is_draft_status(keywords.get("status", ast.Constant(None)))
                ):
                    flag(f"{name}(payload=...)")
                if None in keywords and function is not None and _names_payload_field(function):
                    flag(f"{name}(**fields) in a function naming a 'payload' field")
            if name == "values" and _chain_models(node.func) & GUARDED_MODELS and (
                any(keyword.arg == "payload" for keyword in node.keywords)
                or any(
                    isinstance(arg, ast.Dict)
                    and any(isinstance(key, ast.Constant) and key.value == "payload" for key in arg.keys)
                    for arg in node.args
                )
            ):
                flag("SQL write of payload")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and GUARDED_TABLES.search(node.value):
            flag("raw SQL write of payload")
        for child in ast.iter_child_nodes(node):
            visit(child, scope, function)

    visit(tree, (), None)
    return writes


def _unexpected(writes: list[Write]) -> list[Write]:
    return [
        write for write in writes
        if write.location not in RELEASE_WRITERS and write.location not in OTHER_PAYLOAD_WRITES
    ]


def test_blueprint_payload_is_written_only_by_the_release_registry():
    found: list[Write] = []
    for relative, text in _sources(ROOT):
        found.extend(scan(relative, text))
    unexpected = _unexpected(found)
    assert not unexpected, (
        "A Blueprint/delivery payload write bypasses packages/core/blueprints/registry.py. "
        "Release through record_release (draft content: set_draft_payload; listing copy: "
        "set_listing_manifest). If the attribute belongs to another model, add the function "
        "to OTHER_PAYLOAD_WRITES with that model named.\n"
        + "\n".join(f"{write.location}:{write.line}: {write.reason}" for write in unexpected)
    )
    # A stale allowlist entry would let a later write in that function through.
    located = {write.location for write in found}
    assert set(OTHER_PAYLOAD_WRITES) <= located
    assert RELEASE_WRITERS <= located


def test_the_guard_flags_each_way_around_the_registry():
    source = '''
async def publish(db, row, delivery, payload):
    row.payload = payload
    row.title, row.payload = "x", payload
    setattr(row, "payload", payload)
    fields = {"payload": payload}
    for key, value in fields.items():
        setattr(row, key, value)
    db.add(WorkspaceBlueprint(id="x", **fields))
    db.add(WorkspaceBlueprint(id="x", payload=payload, status="published"))
    db.add(ClientWorkspaceDelivery(id="x", payload=payload))
    await db.execute(update(ClientWorkspaceDelivery).values(payload=payload))
    await db.execute(text("UPDATE workspace_blueprints SET payload = :p"))


def draft(payload):
    return WorkspaceBlueprint(id="x", payload=payload, status=BlueprintStatus.DRAFT.value)
'''
    writes = scan("packages/example.py", source)
    assert sorted(write.line for write in writes) == [3, 4, 5, 8, 9, 10, 11, 12, 13]
    assert {write.location for write in writes} == {"packages/example.py::publish"}
