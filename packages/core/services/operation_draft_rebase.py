"""Carry an operation draft onto a Workspace that changed after it was made.

A draft holds the whole Workspace state it means to write, so applying it on
top of someone else's newer change would undo that change. Instead the draft
is merged three ways — the state it started from (``base``), the live state
(``live``) and its own state (``draft``) — field by field:

* a field only the draft changed takes the draft's value;
* a field only someone else changed keeps the live value;
* a field both changed to the same value is fine;
* a field both changed differently is a conflict, and nothing is applied.

List items are matched by their key (``rule_key``, ``service_key`` …), so two
people adding or editing different rules, services or bindings merge cleanly.
A list whose items carry no stable key is one field: changed on both sides,
it conflicts rather than guessing.
"""

from __future__ import annotations

import json
from typing import Any

_MISSING = object()

# Item keys tried in order; the first one every item carries, uniquely, is the
# list's identity.
_ITEM_KEYS = (
    "rule_key", "service_key", "goal_key", "process_key", "key", "slug",
    "workflow_id", "binding_id", "skill_id", "agent_id", "id",
)


def _item_key(items: list[Any]) -> str | None:
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for key in _ITEM_KEYS:
        values = [item.get(key) for item in items]
        if all(isinstance(value, str) and value for value in values):
            return key
    return None


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]] | None:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if item[key] in out:
            return None
        out[item[key]] = item
    return out


def _same(a: Any, b: Any) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def _merge(base: Any, live: Any, draft: Any, path: str, conflicts: list[str]) -> Any:
    if _same(draft, base):
        return live
    if _same(live, base) or _same(live, draft):
        return draft
    # A section both sides created (absent at the base) merges inside.
    if base is _MISSING and type(live) is type(draft) and isinstance(live, (dict, list)):
        base = type(live)()
    if isinstance(base, dict) and isinstance(live, dict) and isinstance(draft, dict):
        merged: dict[str, Any] = {}
        for name in [*live, *(k for k in draft if k not in live), *(k for k in base if k not in live and k not in draft)]:
            value = _merge(base.get(name, _MISSING), live.get(name, _MISSING), draft.get(name, _MISSING),
                           f"{path}.{name}" if path else str(name), conflicts)
            if value is not _MISSING:
                merged[name] = value
        return merged
    if isinstance(base, list) and isinstance(live, list) and isinstance(draft, list):
        key = _item_key([*base, *live, *draft])
        maps = [_keyed(items, key) for items in (base, live, draft)] if key else []
        if key and all(m is not None for m in maps):
            base_items, live_items, draft_items = maps
            merged_items = []
            for ident in [*live_items, *(i for i in draft_items if i not in live_items)]:
                value = _merge(base_items.get(ident, _MISSING), live_items.get(ident, _MISSING),
                               draft_items.get(ident, _MISSING), f"{path}[{key}={ident}]", conflicts)
                if value is not _MISSING:
                    merged_items.append(value)
            return merged_items
    conflicts.append(path or "(state)")
    return live


def rebase_operation_state(
    base: dict[str, Any], live: dict[str, Any], draft: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """The draft's changes laid over ``live``, and the fields both changed.

    ``operating_model`` mirrors the top-level sections; a conflict reported
    under both names is reported once, under the top-level one.
    """
    conflicts: list[str] = []
    merged = _merge(base, live, draft, "", conflicts)
    top_level = set(live) | set(draft)
    mirrored = {"knowledge": "knowledge_policy", "governance": "governance_policy"}
    unique: list[str] = []
    for path in conflicts:
        if path.startswith("operating_model."):
            section = path.split(".", 2)[1].split("[", 1)[0]
            if section in top_level or mirrored.get(section) in top_level:
                continue
        unique.append(path)
    return (merged if isinstance(merged, dict) else dict(live)), unique
