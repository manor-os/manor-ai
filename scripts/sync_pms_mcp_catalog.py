#!/usr/bin/env python3
"""Synchronize Manor's PMS contract from a reviewed, credential-free PMS export.

Export in the PMS backend environment (no API or database access required):
    python -c 'import json; from mcp_server.server import get_available_tools; from mcp_server.business_tools import ROUTES; print(json.dumps({"tools": get_available_tools(), "routes": ROUTES}))' > pms-catalog.json

Then run this script with --source pms-catalog.json; add --check for parity-only
verification. The source must come from the reviewed PMS revision. This script
never imports that backend, reads credentials, or updates installed Skills.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "packages/core/ai/mcp/manor_pms_tools.json"
SKILL_CONFIG = ROOT / "packages/core/ai/skills/mcp_manor_pms/config.json"
_RESERVED_FIELDS = {
    "entity_id", "user_id", "task_id", "agent_id", "workspace_id",
    "conversation_id", "bearer_token", "access_token", "base_url",
}


def build_contract(source: dict) -> dict:
    methods = {name: spec["method"] for name, spec in source["routes"].items()}
    # These original tools select a reviewed REST transition from an enum.
    methods.update(reservation_transition="POST", cleaning_transition_task="POST")
    tools = []
    for raw in source["tools"]:
        name = raw["name"]
        schema = deepcopy(raw["parameters"])
        for original, alias in (("task_id", "cleaning_task_id"), ("user_id", "assigned_to_user_id")):
            if original not in schema["properties"]:
                continue
            if not name.startswith("cleaning_") or (original == "user_id" and name != "cleaning_assign_task"):
                raise ValueError(f"Unreviewed runtime field collision in {name}: {original}")
            schema["properties"][alias] = schema["properties"].pop(original)
            schema["required"] = [alias if field == original else field for field in schema.get("required", [])]
        if _RESERVED_FIELDS.intersection(schema["properties"]):
            raise ValueError(f"Runtime identity fields remain in {name}")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"PMS schema is not closed: {name}")
        method = methods[name]
        readonly = method == "GET" or name == "promo_validate"
        destructive = (
            method == "DELETE"
            or name in {
                "reservation_transition", "cleaning_transition_task",
                "pricing_set_override", "compliance_record_consent",
                "channel_import_ical", "review_reply",
            }
            or raw.get("annotations", {}).get("destructiveHint") is True
        )
        tools.append({
            "name": name,
            "description": raw["description"],
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": readonly,
                "destructiveHint": destructive,
                "idempotentHint": readonly,
                "openWorldHint": True,
            },
        })
    if len({tool["name"] for tool in tools}) != len(tools):
        raise ValueError("Duplicate PMS tool names")
    if {tool["name"] for tool in tools} != set(methods):
        raise ValueError("PMS tools and reviewed routes differ")
    return {
        "source": "manor-pms/backend/mcp_server/server.py::get_available_tools + business_tools.py::ROUTES",
        "schema_notes": "Mirrors the reviewed management REST catalog. Reject undeclared arguments. Effect annotations derive from route methods, retaining the read-only promo validation exception and destructive transitions. Cleaning task_id is exposed as cleaning_task_id; cleaning_assign_task user_id is exposed as assigned_to_user_id to avoid Manor runtime identity collisions.",
        "operation_methods": methods,
        "tools": tools,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source_bytes = args.source.read_bytes()
    contract = build_contract(json.loads(source_bytes))
    config = json.loads(SKILL_CONFIG.read_text())
    config["tools"] = [f"mcp__manor_pms__{tool['name']}" for tool in contract["tools"]]
    outputs = {CONTRACT: contract, SKILL_CONFIG: config}
    if args.check:
        changed = [str(path.relative_to(ROOT)) for path, value in outputs.items() if json.loads(path.read_text()) != value]
        if changed:
            raise SystemExit("PMS catalog drift: " + ", ".join(changed))
    else:
        for path, value in outputs.items():
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    reads = sum(tool["annotations"]["readOnlyHint"] for tool in contract["tools"])
    print(json.dumps({
        "checked": args.check, "tools": len(contract["tools"]), "reads": reads,
        "actions": len(contract["tools"]) - reads,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
    }))


if __name__ == "__main__":
    main()
