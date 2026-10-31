"""A Flow's closing message is read by a person, never a dumped object."""
from __future__ import annotations

import json
import re
from pathlib import Path

CONFIGS = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"
REFERENCE = re.compile(r"{{\s*([A-Za-z0-9_]+)((?:\.[A-Za-z0-9_]+)+)\s*}}")


def _declared_type(schema: object, path: list[str]) -> str | None:
    """The declared type at path; "undeclared" when the source never promises that key."""
    for key in path:
        if not isinstance(schema, dict):
            return None
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            # A free-form object (e.g. a retry plan) may omit any key, so the
            # sentence would read "Next step: ." whenever it does.
            return "undeclared" if schema.get("type") == "object" else None
        schema = properties.get(key)
    if not isinstance(schema, dict):
        return None
    kind = schema.get("type")
    return kind if isinstance(kind, str) else None


def _workflows():
    for path in sorted(CONFIGS.rglob("*.json")):
        payload = json.loads(path.read_text())
        for workflow in (payload.get("recipe") or {}).get("workflows") or []:
            definition = workflow.get("definition") or workflow
            yield path.name, workflow.get("slug"), definition.get("steps") or []


def test_closing_messages_never_interpolate_a_whole_object():
    dumped = []
    for blueprint, slug, steps in _workflows():
        schemas = {
            step.get("id"): (step.get("config") or {}).get("output_schema")
            for step in steps
        }
        for step in steps:
            if step.get("type") not in {"stop", "end"}:
                continue
            message = (step.get("config") or {}).get("message")
            for source, path in REFERENCE.findall(message if isinstance(message, str) else ""):
                if _declared_type(schemas.get(source), path.lstrip(".").split(".")) in {"object", "array", "undeclared"}:
                    dumped.append(f"{blueprint}:{slug}:{step.get('id')} -> {source}{path}")
    assert not dumped, dumped
