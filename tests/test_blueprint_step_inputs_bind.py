"""Every step input a built-in Flow declares reaches the runtime as a binding."""
from __future__ import annotations

import json
from pathlib import Path

CONFIGS = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs"


def test_step_inputs_are_never_silently_ignored():
    # The runtime binds config.inputs rows ({key, value}). The {name: value}
    # shorthand is converted only for Flows with explicit data contracts;
    # anywhere else it is skipped, and an end step returns the last upstream
    # value instead of the result it declares.
    ignored = []
    for path in sorted(CONFIGS.rglob("*.json")):
        payload = json.loads(path.read_text())
        for workflow in (payload.get("recipe") or {}).get("workflows") or []:
            definition = workflow.get("definition") or workflow
            if workflow.get("explicit_data_contracts") or definition.get("explicit_data_contracts"):
                continue
            for step in definition.get("steps") or []:
                if isinstance((step.get("config") or {}).get("inputs"), dict):
                    ignored.append(f"{path.name}:{workflow.get('slug')}:{step.get('id')}")
    assert not ignored, ignored
