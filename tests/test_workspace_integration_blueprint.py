"""Disabled workspace integrations retain portable requirements without blockers."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.blueprints.exporter import export_workspace
from packages.core.blueprints.installer import (
    InstallMode,
    InstallTodo,
    _effective_install_todos,
    _persist_install_todos,
    install_blueprint,
)
from packages.core.blueprints.payload import validate_payload
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.user import Entity
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_operation_service import apply_operation_draft, create_operation_draft


def test_install_receipt_excludes_disabled_mcp_without_erasing_live_requirements():
    workspace = SimpleNamespace(
        operating_model={"integration_scope": {"disabled_providers": ["elevenlabs"]}}, settings={},
    )
    todos = [
        InstallTodo(kind="missing_integration", detail="Connect", payload={"provider": "ElevenLabs"}),
        InstallTodo(kind="mcp_configuration", detail="Configure", payload={"server_slug": "elevenlabs"}),
        InstallTodo(kind="missing_integration", detail="Slack", payload={"provider": "slack"}),
        InstallTodo(kind="channel", detail="Channel", payload={"provider": "elevenlabs"}),
    ]

    _persist_install_todos(workspace, todos, [])

    record = workspace.settings["_blueprint"]
    assert record["blocking_todo_count"] == 2
    assert [todo["detail"] for todo in record["install_todos"]] == ["Slack", "Channel"]
    assert len(record["live_setup_requirements"]) == 4
    assert len(todos) == 4
    workspace.operating_model = {"integration_scope": {"disabled_providers": []}}
    assert _effective_install_todos(workspace, todos) == todos


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_disabled_shared_agent_provider_survives_export_install_export(db_session, tmp_path, mode):
    from packages.core.governance.approval_policy_service import account_defaults, initialize_workspace_approval
    from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory
    from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup

    entity_id = generate_ulid()
    target_entity_id = generate_ulid()
    source = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Tutorial source", kind="project",
        operating_model={}, settings={"access_mode": "members_only"}, heartbeat_enabled=False, heartbeat_cadence="daily",
    )
    peer = Workspace(id=generate_ulid(), entity_id=entity_id, name="Shared Agent peer", settings={})
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Tutorial producer", slug="tutorial-producer",
        system_prompt="Produce tutorials.", config={"model_mode": "inherit"}, is_public=False, status="active", version="1.0",
    )
    db_session.add_all([
        Entity(id=entity_id, name="Tutorial source entity", settings={"approval_defaults": account_defaults()}),
        Entity(id=target_entity_id, name="Tutorial target entity", settings={"approval_defaults": account_defaults()}),
        source, peer, agent,
    ])
    await db_session.flush()
    server = (await db_session.execute(select(MCPServer).where(MCPServer.server_key == "elevenlabs"))).scalar_one()
    binding = AgentMCPBinding(
        id=generate_ulid(), agent_id=agent.id, mcp_server_id=server.id,
        allowed_tools=["text_to_speech"], config_override={"api_token": "not-portable-secret"}, status="active",
    )
    db_session.add(binding)
    for workspace in (source, peer):
        db_session.add(AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
            agent_id=agent.id, service_key="tutorial_production", config={}, status="active",
        ))
    await initialize_workspace_approval(db_session, source)
    await initialize_workspace_approval(db_session, peer)
    await db_session.flush()

    actor_id = generate_ulid()
    baseline = await WorkspaceConnectionStatusFactory.create(db_session, workspace=source, user_id=actor_id)
    assert baseline["required_issue_count"] == 1
    draft = await create_operation_draft(
        db_session, source.id, entity_id,
        initial_patches=[{"op": "integration.remove", "payload": {"provider": "elevenlabs"}}],
    )
    assert draft is not None and draft.validation["valid"] is True
    assert source.operating_model == {}
    await apply_operation_draft(db_session, draft.id, entity_id, source.id, user_confirmation=True)
    assert draft.status == "applied"
    assert source.operation_revision == 1
    assert (await WorkspaceConnectionStatusFactory.create(db_session, workspace=source, user_id=actor_id))["required_issue_count"] == 0
    assert (await WorkspaceConnectionStatusFactory.create(db_session, workspace=peer, user_id=actor_id))["required_issue_count"] == 1

    exported = await export_workspace(db_session, source.id, title="Portable tutorial")
    validate_payload(exported)
    encoded = json.dumps(exported)
    assert "not-portable-secret" not in encoded
    for local_id in (source.id, peer.id, agent.id, binding.id, entity_id):
        assert local_id not in encoded
    assert any(row["slug"] == "elevenlabs" for row in exported["contract"]["requires"]["mcp_servers"])

    result = await install_blueprint(db_session, entity_id=target_entity_id, payload=exported, mode=mode)
    installed = await db_session.get(Workspace, result.workspace_id)
    assert installed is not None
    assert installed.operating_model["integration_scope"] == {"disabled_providers": ["elevenlabs"]}
    assert not result.todos, result.todos
    record = installed.settings["_blueprint"]
    assert record["blocking_todo_count"] == 0
    assert record["install_todos"] == []
    assert any(row["kind"] == "missing_integration" and row["payload"]["provider"] == "elevenlabs" for row in record["live_setup_requirements"])
    assert not (await evaluate_workspace_blocking_setup(db_session, installed)).blocks_work
    await db_session.refresh(binding)
    await db_session.refresh(peer)
    assert binding.status == "active"
    assert binding.config_override == {"api_token": "not-portable-secret"}
    assert not peer.operating_model

    reexported = await export_workspace(db_session, installed.id, title="Portable tutorial")
    validate_payload(reexported)
    assert reexported["recipe"]["operating_model"]["integration_scope"] == exported["recipe"]["operating_model"]["integration_scope"]
    assert reexported["contract"]["requires"]["mcp_servers"] == exported["contract"]["requires"]["mcp_servers"]
    scripts = Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts"
    source_path, installed_path = tmp_path / "source.json", tmp_path / "installed.json"
    source_path.write_text(json.dumps(exported), encoding="utf-8")
    installed_path.write_text(json.dumps(reexported), encoding="utf-8")
    for args in (
        [str(scripts / "check_blueprint.py"), str(source_path)],
        [str(scripts / "check_blueprint.py"), str(installed_path)],
        [str(scripts / "compare_roundtrip.py"), str(source_path), str(installed_path)],
    ):
        checked = subprocess.run([sys.executable, *args], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stdout + checked.stderr

    # Restoring the provider restores the original required connection check.
    enabled = await create_operation_draft(
        db_session, installed.id, installed.entity_id,
        initial_patches=[{"op": "integration.enable", "payload": {"provider": "elevenlabs"}}],
    )
    assert enabled is not None and enabled.validation["valid"] is True
    await apply_operation_draft(db_session, enabled.id, installed.entity_id, installed.id, user_confirmation=True)
    assert (await evaluate_workspace_blocking_setup(db_session, installed)).blocks_work
