"""Real source/configure/export/install parity for the practical catalogue."""
from __future__ import annotations

import importlib.util
import json
import os
from difflib import unified_diff
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_service import validate_workflow_steps
from scripts.blueprints.practical_workspaces import WORKSPACES, configure_and_export, workflow_steps

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "packages/core/blueprints/configs/practical"


def _checker(name):
    path = ROOT / ".agents/skills/manor-workspace-blueprint/scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _entity(db):
    row = Entity(id=generate_ulid(), name="Practical blueprint test", plan_id="plan_enterprise",
        settings={"approval_defaults": account_defaults()})
    db.add(row)
    await db.flush()
    return row.id


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_real_workspace_export_install_roundtrip(db_session, spec, mode):
    entity_id = await _entity(db_session)
    source, exported = await configure_and_export(db_session, entity_id=entity_id, spec=spec)
    published = json.loads((CONFIG_DIR / f"{spec.slug}.json").read_text())
    # The current authoring services export inherited defaults as no policy.
    published["policy"]["governance"] = {}
    assert exported == published, "Regenerate catalogue from the real Workspace services"
    assert _checker("check_blueprint").check(exported) == []
    assert exported["recipe"]["workflows"][0]["steps"] == workflow_steps(spec)
    assert validate_workflow_steps(exported["recipe"]["workflows"][0]["steps"])["valid"]
    assert exported["contract"]["variables"] == []
    assert exported["contract"]["channels"] == exported["contract"]["sessions"] == []
    assert exported["policy"]["expected_baseline"] is None
    assert not exported["recipe"]["scheduled_jobs"]
    assert not exported["recipe"]["goals"]
    assert not exported["recipe"]["task_categories"]
    assert source.id not in json.dumps(exported)
    assert entity_id not in json.dumps(exported)

    target_entity_id = await _entity(db_session)
    installed = await install_blueprint(
        db_session, entity_id=target_entity_id, payload=published, mode=mode,
        blueprint_slug=spec.slug,
    )
    assert not installed.todos, installed.todos
    assert len(installed.subscription_ids) == 1
    assert len(installed.workflow_binding_ids) == 1
    target = await db_session.get(Workspace, installed.workspace_id)
    assert not target.heartbeat_enabled
    assert bool(target.settings.get("sandbox")) == (mode == InstallMode.SIMULATE)
    checks = [item["payload"]["check"] for item in target.settings["_blueprint"]["live_setup_requirements"] if item["kind"] == "post_install_check"]
    assert checks == published["policy"]["post_install_checks"]
    actual = await export_workspace(
        db_session, target.id, title=spec.title,
        context=ExportContext(include_knowledge_packs=False),
    )
    comparator = _checker("compare_roundtrip")
    expected_runtime = comparator._normalize(comparator._runtime_projection(exported))
    actual_runtime = comparator._normalize(comparator._runtime_projection(actual))
    assert comparator.compare(exported, actual) == [], "\n".join(unified_diff(
        json.dumps(expected_runtime, indent=2, sort_keys=True).splitlines(),
        json.dumps(actual_runtime, indent=2, sort_keys=True).splitlines(),
    ))


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
async def test_missing_input_fails_before_model_and_needs_input_never_saves(spec):
    runner = WorkflowRunner()
    steps = {step["id"]: step for step in workflow_steps(spec)}
    run = WorkflowRun(id="test-run", workflow_id="test-workflow", entity_id="test-entity", variables={"source_text": "  "}, step_results={})
    result = await runner._execute_step_safe(steps["start"], run, None)
    assert result["status"] == "failed"
    assert result["code"] == "input_schema_validation_failed"
    run.variables = {"draft": {"status": "needs_input", "markdown": "", "questions": ["Please provide the source material."]}}
    route = await runner._execute_step_safe(steps["ready"], run, None)
    assert route["next_override"] == ["needs_input"]
    result = await runner._execute_step_safe(steps["needs_input"], run, None)
    assert result["output"]["status"] == "needs_input"
    run.variables = {"draft": {"status": "ready", "markdown": "", "questions": []}}
    result = await runner._execute_step_safe(steps["save"], run, None)
    assert result["status"] == "failed"
    assert result["code"] == "input_schema_validation_failed"


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
async def test_success_requires_knowledge_receipt(spec):
    runner = WorkflowRunner()
    steps = {step["id"]: step for step in workflow_steps(spec)}
    run = WorkflowRun(id="test-run", workflow_id="test-workflow", entity_id="test-entity", variables={"saved_document": {"created": True, "document": {"fs_path": "pretend.md"}}}, step_results={})
    result = await runner._execute_step_safe(steps["done"], run, None)
    assert result["status"] == "failed"
    assert result["code"] == "output_schema_validation_failed"
    run.variables = {"saved_document": {"created": True, "document": {"document_id": "doc1", "fs_path": "test.md", "viewer_url": "/viewer/doc1", "file_size": 100}}}
    result = await runner._execute_step_safe(steps["done"], run, None)
    assert result["status"] == "completed"
    assert result["output"]["document"]["document_id"] == "doc1"


async def test_export_omits_default_account_following_governance(monkeypatch):
    """The section helper and complete Blueprint have different empty shapes."""
    from unittest.mock import AsyncMock
    from packages.core.blueprints import exporter
    from packages.core.governance.policy import policy_from_dict

    policy = policy_from_dict({"approval": {
        "root_entity_id": "source-account", "policy": {"mode": None, "rules": []},
    }})
    read = AsyncMock(return_value=policy)
    monkeypatch.setattr(exporter, "get_policy", read)
    db = object()
    assert await exporter._export_governance(db, "source-workspace") is None
    read.assert_awaited_once_with(db, "source-workspace")

    workspace = Workspace(id="source-workspace", entity_id="source-account", name="Source",
        kind="creative", operating_model={}, settings={}, heartbeat_enabled=False)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: workspace)))
    payload = await export_workspace(db, workspace.id, title="Source", context=ExportContext(
        include_subscriptions=False, include_goals=False, include_stats=False,
        include_scheduled_jobs=False, include_workflows=False, include_custom_fields=False,
        include_channel_requirements=False, include_session_requirements=False,
        include_embedded_agents=False, include_embedded_skills=False,
        include_knowledge_packs=False,
    ))
    assert payload["policy"]["governance"] == {}


def test_export_preserves_schema_field_names_without_preserving_source_ids():
    from packages.core.blueprints.exporter import _without_nonportable_references

    schema = {"type": "object", "properties": {
        "document_id": {"type": "string", "minLength": 1},
        "records": {"type": "array", "items": {"type": "object", "properties": {
            "workspace_id": {"type": "string"},
        }}},
    }, "required": ["document_id"]}
    projected = _without_nonportable_references({
        "output_schema": schema, "args": {"document_id": "source-document", "content": "portable"},
    })
    assert projected["output_schema"] == schema
    assert projected["args"] == {"content": "portable"}
    assert _without_nonportable_references({"properties": {"document_id": "source-document"}}) == {"properties": {}}


@pytest.mark.parametrize("spec", WORKSPACES, ids=lambda spec: spec.key)
async def test_installed_workflow_writes_real_knowledge_files(client, db_session, monkeypatch, tmp_path, spec):
    """Run graph/ACL/persistence; opt in to model calls with PRACTICAL_LIVE_MODEL_SMOKE=1."""
    from auth_helpers import register_user_and_get_token
    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.runtime import file_actions
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.services.workflow_service import start_workflow_from_binding

    live_model = os.getenv("PRACTICAL_LIVE_MODEL_SMOKE") == "1"
    settings = get_settings()
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", os.getenv("DEPLOYMENT_MODE", "oss") if live_model else "oss")
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    if live_model and settings.DEPLOYMENT_MODE == "cloud":
        # Exercise Cloud model routing with local test files, not a production
        # JuiceFS volume. Only this temporary root simulates a mounted volume;
        # the marker, path safety, ACL, atomic write and DB projection stay real.
        original_ismount = os.path.ismount
        monkeypatch.setattr(os.path, "ismount", lambda path: os.path.abspath(path) == str(tmp_path) or original_ismount(path))
        (tmp_path / ".accesschk").touch()
    # Indexing is asynchronous and outside the durable file/Document contract.
    monkeypatch.setattr(file_actions, "runtime_trigger_document_embeddings", lambda _id: None)
    account = (await register_user_and_get_token(client, {
        "username": "practical-test", "email": "practical-test@example.test",
        "password": "TestPassword123!", "entity_name": "Practical Workflow Test",
    })).json()
    entity_id, user_id = account["entity_id"], account["user_id"]
    payload = json.loads((CONFIG_DIR / f"{spec.slug}.json").read_text())
    installed = await install_blueprint(
        db_session, entity_id=entity_id, user_id=user_id, payload=payload,
        mode=InstallMode.LIVE, blueprint_slug=spec.slug,
    )
    assert not installed.todos
    from packages.core.governance import get_policy
    from packages.core.governance.approval_projection import policy_override
    from approval_settings_test_utils import action_rule, set_workspace_test_approval

    # The operator authorizes only the durable Knowledge write under test;
    # template asks and hard denies remain in the existing policy.
    previous = policy_override(await get_policy(db_session, installed.workspace_id))
    await set_workspace_test_approval(db_session, entity_id=entity_id,
        workspace_id=installed.workspace_id, actor_id=user_id, mode=previous.mode,
        user_rules=[rule.model_dump(include={"id", "effect", "selector"})
                    for rule in previous.rules if rule.source_kind == "user"]
            + [action_rule("workspace.file.create")])
    await db_session.commit()
    binding = await db_session.get(WorkflowBinding, installed.workflow_binding_ids[0])
    workflow = await db_session.get(WorkflowDefinition, binding.workflow_id)
    markdown = f"# {spec.title}\n\n## Source-grounded draft\n\n{spec.example_source}\n\n## Review questions\nConfirm details before external use.\n"
    model_calls = []

    async def model_output(**kwargs):
        assert not kwargs["tools"], "The drafting node must not perform side effects"
        assert spec.example_source in kwargs["user_message"]
        assert spec.rules in kwargs["system_prompt"]
        model_calls.append(kwargs["agent_id"])
        return SimpleNamespace(
            content=json.dumps({"status": "ready", "markdown": markdown, "questions": []}),
            error=None, stop_reason="end_turn", usage={}, tool_calls_made=[], rounds=1,
        )

    if not live_model:
        monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", model_output)
    receipts = []
    for _ in range(1 if live_model else 2):
        run = await start_workflow_from_binding(
            db_session, binding, variables={"source_text": spec.example_source, "instructions": spec.example_instructions},
            trigger_source="manual", started_by=user_id, user_requested=True,
        )
        await db_session.commit()
        await WorkflowRunner()._run_loop(workflow, run, db_session)
        assert run.status == "completed", (run.error, run.step_results)
        receipt = run.variables["__result"]
        assert receipt["created"] is True
        document = await db_session.get(Document, receipt["document"]["document_id"])
        assert document is not None
        assert document.entity_id == entity_id
        assert document.metadata_["origin"]["workspace_id"] == installed.workspace_id
        assert document.fs_path == receipt["document"]["fs_path"]
        body = (tmp_path / entity_id / document.fs_path).read_text()
        if live_model:
            assert len(body.strip()) >= 200
            assert "#" in body
            assert "{{" not in body
            print("PRACTICAL_LIVE_RESULT=" + json.dumps({"workspace": spec.key, "document_id": document.id, "markdown": body}, ensure_ascii=False))
        else:
            assert body == markdown
        receipts.append(receipt["document"])
    if not live_model:
        assert len(model_calls) == 2
        assert receipts[0]["document_id"] != receipts[1]["document_id"]
        assert receipts[0]["fs_path"] != receipts[1]["fs_path"]
    assert len((await db_session.scalars(select(Document).where(Document.entity_id == entity_id))).all()) == len(receipts)
