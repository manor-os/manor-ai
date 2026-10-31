"""Workflow node timeout contract regressions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.ai import workflow_runner as workflow_runner_module
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_service import validate_workflow_steps


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["simulate", "live"])
async def test_configured_workspace_timeout_survives_install_export_and_upgrade_projection(db_session, mode):
    from sqlalchemy import select
    from test_blueprint_installer_workflows import _base_payload
    from packages.core.blueprints.exporter import _export_workflows
    from packages.core.blueprints.installer import InstallMode, install_blueprint
    from packages.core.blueprints.upgrade import _desired_content
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.workflow_service import update_workflow

    entity_id = generate_ulid()
    payload = _base_payload(**{"recipe.workflows": [{"slug": "timeout-contract", "steps": _workflow(30)}]})
    source = await install_blueprint(db_session, entity_id=entity_id, payload=payload, mode=InstallMode(mode))
    definition = await db_session.scalar(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == source.workspace_id))
    configured = deepcopy(definition.steps)
    configured[1]["config"]["timeout"] = None
    await update_workflow(db_session, definition.id, entity_id, require_valid_steps=True, steps=configured)
    await db_session.commit()
    first_export = await _export_workflows(db_session, entity_id, source.workspace_id)
    assert first_export.payloads[0]["steps"][1]["config"]["timeout"] is None
    copied_payload = _base_payload(**{"recipe.workflows": first_export.payloads})
    copied = await install_blueprint(db_session, entity_id=entity_id, payload=copied_payload, mode=InstallMode(mode))
    await db_session.commit()
    second_export = await _export_workflows(db_session, entity_id, copied.workspace_id)
    assert second_export.payloads[0]["steps"] == first_export.payloads[0]["steps"]
    desired = _desired_content("workflow", first_export.payloads[0])
    assert desired["steps"][1]["config"]["timeout"] is None


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run-timeout", workflow_id="workflow-timeout", entity_id="entity-timeout",
        variables={}, step_results={},
    )


def _workflow(timeout):
    return [
        {"id": "start", "type": "trigger", "config": {}, "next": ["work"]},
        {"id": "work", "type": "agent", "config": {"timeout": timeout}, "next": []},
    ]


@pytest.mark.parametrize("timeout", [0, -1, True, "1", float("inf"), float("nan"), 10 ** 400])
def test_workflow_validation_rejects_invalid_node_timeouts(timeout):
    result = validate_workflow_steps(_workflow(timeout))

    assert result["valid"] is False
    assert any(error["code"] == "invalid_node_timeout" for error in result["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 10 ** 400])
async def test_invalid_timeout_fails_before_cache_or_step_execution(monkeypatch, timeout):
    calls = 0
    step = {
        "id": "invalid-timeout",
        "type": "transform",
        "config": {"timeout": timeout},
        "next": [],
    }
    fingerprint = workflow_runner_module._step_fingerprint(step, {})
    runner = WorkflowRunner(cache_index={fingerprint: {"status": "completed", "output": "cached"}})

    async def execute_step(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "completed"}

    monkeypatch.setattr(runner, "_execute_step", execute_step)

    result = await runner._execute_step_safe(step, _run(), db=None)

    assert result["status"] == "failed"
    assert result["code"] == "invalid_node_timeout"
    assert "retry_blocked" not in result
    assert "do_not_replay" not in result
    assert calls == 0


def test_blueprint_workflow_install_export_round_trip_preserves_timeout_values():
    from packages.core.blueprints.exporter import _export_workflow_row
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    source = {
        "slug": "timeout-round-trip",
        "steps": [
            {"id": "start", "type": "trigger", "config": {}, "next": ["unbounded"]},
            {"id": "unbounded", "type": "agent", "config": {"timeout": None}, "next": ["bounded"]},
            {"id": "bounded", "type": "agent", "config": {"timeout": 42}, "next": []},
        ],
    }
    installed = _blueprint_workflow_definition_values(source)
    workflow = SimpleNamespace(
        variables=installed["variables"],
        trigger_type=installed["trigger_type"],
        trigger_config=installed["trigger_config"],
        steps=installed["steps"],
        name=installed["name"],
        description=installed["description"],
        category=installed["category"],
        tags=installed["tags"],
        version=installed["version"],
        status=installed["status"],
        is_active=installed["is_active"],
    )

    exported = _export_workflow_row(
        workflow,
        slug=source["slug"],
        workflow_key_by_id={},
        skill_ref_by_id={},
    )

    assert [step["config"].get("timeout") for step in exported["steps"][1:]] == [None, 42]


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", ["agent", "tool", "http", "code", "transform"])
async def test_missing_or_null_timeout_does_not_apply_outer_node_wall_clock_limit(
    monkeypatch, node_type,
):
    async def delayed_step(step, run, db):
        await asyncio.sleep(0.02)
        return {"status": "completed", "output": node_type}

    runner = WorkflowRunner()
    monkeypatch.setattr(runner, "_execute_step", delayed_step)

    async def unexpected_wait_for(*_args, **_kwargs):
        raise AssertionError("unbounded node must not call asyncio.wait_for")

    monkeypatch.setattr(workflow_runner_module.asyncio, "wait_for", unexpected_wait_for)

    for config in ({}, {"timeout": None}):
        result = await runner._execute_step_safe(
            {"id": f"{node_type}-node", "type": node_type, "config": config},
            _run(),
            db=None,
        )
        assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_positive_timeout_still_applies_outer_node_wall_clock_limit(monkeypatch):
    async def delayed_step(step, run, db):
        await asyncio.sleep(0.02)
        return {"status": "completed"}

    runner = WorkflowRunner()
    monkeypatch.setattr(runner, "_execute_step", delayed_step)

    result = await runner._execute_step_safe(
        {"id": "bounded", "type": "agent", "config": {"timeout": 0.001}},
        _run(),
        db=None,
    )

    assert result["status"] == "failed"
    assert result["error"] == "Step bounded timed out"


@pytest.mark.asyncio
async def test_http_null_node_timeout_keeps_its_request_timeout(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *, timeout, **_kwargs):
            captured["timeout"] = timeout
            self.cookies = _kwargs.get("cookies")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, *_args, **_kwargs):
            return SimpleNamespace(
                status_code=200,
                is_success=True,
                json=lambda: {"ok": True},
                text='{"ok": true}',
                headers={},
                content=b'{"ok": true}',
            )

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(workflow_runner_module, "_resolve_workflow_http_host", lambda _url: asyncio.sleep(0))

    result = await WorkflowRunner()._execute_http_step(
        {"id": "request", "type": "http", "config": {"url": "https://example.com", "timeout": None}},
        {},
    )

    assert result["status"] == "completed"
    assert captured["timeout"] == 30.0
