from __future__ import annotations

from packages.core.ai.workflow_runner import _bind_inputs
from packages.core.services.workflow_contracts import (
    explicit_workflow_step_contracts,
)


def test_explicit_contracts_cover_templates_conditions_and_terminal_results():
    steps = [
        {
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{"key": "source", "type": "string"}],
                "outputs": [{"key": "source", "value": "{{start.source}}", "type": "text"}],
            },
            "next": ["draft"],
        },
        {
            "id": "draft",
            "type": "agent",
            "config": {
                "input": "Draft from {{source}}",
                "output_var": "packet",
                "output_format": "json",
            },
            "next": ["gate"],
        },
        {
            "id": "gate",
            "type": "condition",
            "config": {"expression": "packet.approved == true"},
            "true_next": ["done"],
            "false_next": ["rejected"],
        },
        {
            "id": "done",
            "type": "end",
            "config": {"inputs": {"input": {"packet": "{{packet}}"}}},
            "next": [],
        },
        {
            "id": "rejected",
            "type": "end",
            "config": {"inputs": {"input": "{{packet}}"}},
            "next": [],
        },
    ]

    normalized = explicit_workflow_step_contracts(steps)
    by_id = {step["id"]: step for step in normalized}

    assert by_id["draft"]["config"]["inputs"] == [
        {"key": "source", "value": "{{source}}", "type": "text"},
    ]
    assert by_id["draft"]["config"]["outputs"] == [
        {"key": "packet", "value": "{{draft}}", "type": "json"},
    ]
    assert by_id["gate"]["config"]["inputs"] == [
        {"key": "packet", "value": "{{packet}}", "type": "json"},
    ]
    assert by_id["gate"]["config"]["outputs"] == [
        {"key": "gate", "value": "{{gate}}", "type": "boolean"},
    ]
    assert by_id["done"]["config"]["inputs"] == [
        {"key": "input", "value": {"packet": "{{packet}}"}, "type": "json"},
    ]
    assert by_id["done"]["config"]["outputs"] == [
        {"key": "done", "value": "{{done}}", "type": "json"},
    ]
    assert by_id["rejected"]["config"]["inputs"] == [
        {"key": "input", "value": "{{packet}}", "type": "json"},
    ]
    assert by_id["rejected"]["config"]["outputs"] == [
        {"key": "rejected", "value": "{{rejected}}", "type": "json"},
    ]
    assert all(isinstance(step["config"]["inputs"], list) for step in normalized)
    assert all(isinstance(step["config"]["outputs"], list) for step in normalized)


def test_structured_input_bindings_resolve_nested_workflow_references():
    variables = {
        "packet": {"title": "Explicit contracts"},
        "decision": {"choice": "approve"},
    }
    _bind_inputs(
        {
            "inputs": [
                {
                    "key": "input",
                    "type": "json",
                    "value": {
                        "packet": "{{packet}}",
                        "choice": "{{decision.choice}}",
                    },
                },
            ],
        },
        variables,
    )

    assert variables["input"] == {
        "packet": {"title": "Explicit contracts"},
        "choice": "approve",
    }
