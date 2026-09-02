"""Install/export runtime boundaries found during the Blueprint review."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from packages.core.blueprints.exporter import _export_channel_requirements
from packages.core.blueprints.installer import (
    InstallError,
    _bind_blueprint_channel_configs,
    _materialize_knowledge_pack_document,
)
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.channel_bindings import load_channel_binding_scopes_for_config


def test_workspace_history_projects_merged_subagent_events_without_private_details():
    from apps.api.routers.workspace_chat import _to_message
    from packages.core.ai.runtime.streams import runtime_record_sub_agent_event_for_chat
    from packages.core.models.task import Message

    events = []
    runtime_record_sub_agent_event_for_chat(events, {
        "run_id": "review-run", "status": "failed",
        "error": "ValueError('/srv/private/config.yaml')",
        "tool_calls_made": ["mcp__private_server__private_action"],
        "tool": {"seq": 1, "name": "mcp__private_server__private_action", "status": "error"},
    })
    original = copy.deepcopy(events)
    message = Message(
        id=generate_ulid(), conversation_id=generate_ulid(), role="assistant",
        content="Working", created_at=datetime.now(timezone.utc),
        message_kind="text", author_kind="agent",
        meta={"sub_agent_events": events},
    )
    public = _to_message(message).meta["sub_agent_events"][0]
    assert public["tools"][0]["name"] == "operation"
    assert public["tool_calls_made"] == ["operation"]
    assert public["error"] == "Sorry, the request failed. Please try again."
    assert events == original


async def channel_fixture(db, channel_type):
    from packages.core.services.agent_runtime_config import normalize_agent_runtime_config

    entity_id, user_id = generate_ulid(), generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Channel review", status="active", settings={"access_mode": "members_only"})
    agent = Agent(entity_id=entity_id, name="Support", status="active", config=normalize_agent_runtime_config({}))
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type=channel_type,
        provider=channel_type, name="Support account", status="active",
    )
    db.add_all([workspace, agent, account])
    await db.flush()
    subscription = AgentSubscription(
        entity_id=entity_id, workspace_id=workspace.id, agent_id=agent.id,
        service_key="support", status="active",
    )
    db.add(subscription)
    await db.flush()
    return workspace, account, subscription


@pytest.mark.parametrize("channel_type", ["slack", "discord", "twilio_voice"])
async def test_blueprint_bound_channel_preserves_runtime_owner(db_session, channel_type):
    workspace, account, subscription = await channel_fixture(db_session, channel_type)
    await _bind_blueprint_channel_configs(
        db_session, workspace=workspace, user_id=account.owner_user_id,
        channel_requirements=[{"channel_type": channel_type, "linked_service_key": "support"}],
        selected_channel_config_ids={f"channel:0:{channel_type}": account.id},
    )
    scopes = await load_channel_binding_scopes_for_config(db_session, account)
    assert len(scopes) == 1
    assert scopes[0].binding.user_id == account.owner_user_id
    assert scopes[0].agent_subscription_id == subscription.id


async def test_blueprint_channel_export_preserves_shared_account_and_service(db_session):
    workspace, account, subscription = await channel_fixture(db_session, "telegram")
    db_session.add(Channel(
        entity_id=workspace.entity_id, user_id=account.owner_user_id,
        workspace_id=workspace.id, type="telegram", status="active",
        agent_id=subscription.agent_id, agent_subscription_id=subscription.id,
        config={"channel_config_id": account.id, "role": "primary_external"},
    ))
    await db_session.flush()
    [requirement] = await _export_channel_requirements(db_session, workspace.entity_id, workspace.id)
    assert requirement["linked_service_key"] == "support"
    assert requirement["channel_type"] == "telegram"
    assert account.id not in str(requirement)
    assert account.owner_user_id not in str(requirement)


async def test_legacy_channel_does_not_pick_an_arbitrary_service(db_session):
    workspace, account, subscription = await channel_fixture(db_session, "telegram")
    db_session.add(AgentSubscription(
        entity_id=workspace.entity_id, workspace_id=workspace.id,
        agent_id=subscription.agent_id, service_key="sales", status="active",
    ))
    await db_session.flush()
    await _bind_blueprint_channel_configs(
        db_session, workspace=workspace, user_id=account.owner_user_id,
        channel_requirements=[{"channel_type": "telegram"}],
        selected_channel_config_ids={"channel:0:telegram": account.id},
    )
    assert not (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id, Channel.status == "active",
    ))).scalars().all()


@pytest.mark.parametrize("keyed", [True, False])
async def test_live_template_cannot_reassign_a_shared_document(db_session, keyed):
    entity_id = generate_ulid()
    workspaces = [Workspace(entity_id=entity_id, name=f"Workspace {i}") for i in range(2)]
    db_session.add_all(workspaces)
    await db_session.flush()
    groups = [DocumentGroup(entity_id=entity_id, workspace_id=ws.id, name=ws.name) for ws in workspaces]
    document = Document(
        entity_id=entity_id, name="guide.md", metadata_={
            "content_text": "Operator edits",
            "origin": {"workspace_id": workspaces[0].id},
            **({"blueprint_document_key": "guide"} if keyed else {}),
        },
    )
    db_session.add_all([*groups, document])
    await db_session.flush()
    db_session.add_all([DocumentGroupMember(document_id=document.id, group_id=g.id) for g in groups])
    await db_session.flush()
    original = copy.deepcopy(document.metadata_)
    with pytest.raises(InstallError, match="shared|another Workspace"):
        await _materialize_knowledge_pack_document(
            db_session, entity_id=entity_id, workspace_id=workspaces[1].id,
            group_id=groups[1].id, knowledge_pack_slug="guide", document={
                "path": "guide.md", "body_md": "Template body",
                **({"key": "guide"} if keyed else {}),
                "template": {"id": "guide", "mode": "live_projection", "renderer": "guide"},
            },
        )
    assert document.metadata_ == original


async def test_knowledge_picker_pages_documents_before_bounded_group_fanout(db_session):
    from packages.core.blueprints.exporter import list_exportable_knowledge_documents
    from packages.core.constants.blueprints import BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES

    workspace = Workspace(entity_id=generate_ulid(), name="Paged Knowledge")
    db_session.add(workspace)
    await db_session.flush()
    groups = [DocumentGroup(entity_id=workspace.entity_id, workspace_id=workspace.id, name=f"Group {i:02}") for i in range(10)]
    documents = [Document(
        entity_id=workspace.entity_id, name=f"guide-{i}.md", mime_type="text/markdown",
        classification="public", visibility="workspace", metadata_={"content_text": "# Guide"},
    ) for i in range(5)]
    excluded = [Document(
        entity_id=workspace.entity_id, name=f"00-excluded-{i}.md", mime_type="text/markdown",
        classification="public", visibility="workspace", metadata_={"content_text": content},
    ) for i, content in enumerate(["x" * (BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES + 1), " \n\t", {"not": "text"}])]
    db_session.add_all([*groups, *documents, *excluded])
    await db_session.flush()
    db_session.add_all([DocumentGroupMember(document_id=d.id, group_id=groups[0].id) for d in documents + excluded])
    db_session.add_all([DocumentGroupMember(document_id=documents[0].id, group_id=g.id) for g in groups[1:]])
    await db_session.flush()
    pages = [await list_exportable_knowledge_documents(
        db_session, entity_id=workspace.entity_id, workspace_id=workspace.id, limit=2, offset=offset,
    ) for offset in (0, 2, 4, 6)]
    assert [len(page) for page in pages] == [2, 2, 1, 0]
    assert [d["id"] for page in pages for d in page] == [d.id for d in documents]
    assert len(pages[0][0]["groups"]) == 8
    assert pages[0][0]["groups_truncated"] is True
    assert pages[0][1]["groups_truncated"] is False
    assert "# Guide" not in str(pages)


async def test_starter_lookup_bounds_each_identity_without_loading_unrelated_documents(db_session):
    from packages.core.blueprints.installer import _knowledge_pack_document_rows, _workspace_blueprint_document_rows

    workspace = Workspace(entity_id=generate_ulid(), name="Lookup")
    db_session.add(workspace)
    await db_session.flush()
    group = DocumentGroup(entity_id=workspace.entity_id, workspace_id=workspace.id, name="Lookup pack")
    docs = [Document(entity_id=workspace.entity_id, name=f"doc-{i}.md", metadata_={
        "content_text": "Unrelated runtime body", "blueprint_document_key": "duplicated" if i < 6 else "wanted" if i == 6 else "unrelated",
    }) for i in range(12)]
    db_session.add_all([group, *docs])
    await db_session.flush()
    db_session.add_all([DocumentGroupMember(document_id=d.id, group_id=group.id) for d in docs])
    await db_session.flush()
    matches = await _knowledge_pack_document_rows(db_session, entity_id=workspace.entity_id, group_id=group.id,
        knowledge_pack_slug="lookup", documents=[{"key": "duplicated"}, {"key": "wanted"}])
    assert len(matches) == 3
    assert docs[6] in matches
    matches = await _workspace_blueprint_document_rows(db_session, entity_id=workspace.entity_id, workspace_id=workspace.id, document_key="duplicated")
    assert len(matches) == 2


@pytest.mark.parametrize("mode", ["simulate", "live"])
async def test_shared_channel_runtime_survives_export_install_reexport(db_session, mode, tmp_path):
    from packages.core.blueprints.exporter import export_workspace
    from packages.core.blueprints.installer import InstallMode, install_blueprint
    from packages.core.blueprints.payload import validate_payload

    workspace, account, subscription = await channel_fixture(db_session, "discord")
    source_agent = await db_session.get(Agent, subscription.agent_id)
    source_blueprint_id = generate_ulid()
    source_agent.config = {**source_agent.config, "source_blueprint_id": source_blueprint_id, "source_blueprint_component_key": "source-component"}
    db_session.add(Channel(entity_id=workspace.entity_id, workspace_id=workspace.id,
        user_id=account.owner_user_id, type="discord", status="active",
        agent_subscription_id=subscription.id, agent_id=subscription.agent_id,
        config={"channel_config_id": account.id, "role": "primary_external"}))
    await db_session.flush()
    payload = await export_workspace(db_session, workspace.id, title="Channel parity")
    validate_payload(payload)
    assert source_blueprint_id not in str(payload)
    assert "source_blueprint_component_key" not in str(payload)
    new_account = ChannelConfig(entity_id=workspace.entity_id, owner_user_id=account.owner_user_id,
        channel_type="discord", provider="discord", name="Support account", status="active")
    db_session.add(new_account)
    await db_session.flush()
    installed = await install_blueprint(db_session, entity_id=workspace.entity_id, user_id=account.owner_user_id,
        payload=payload, mode=InstallMode(mode), channel_config_ids={"channel:0:discord": new_account.id})
    assert not installed.todos
    [scope] = await load_channel_binding_scopes_for_config(db_session, new_account)
    assert scope.workspace_id == installed.workspace_id
    assert scope.agent_subscription_id != subscription.id
    reexported = await export_workspace(db_session, installed.workspace_id, title="Channel parity")
    validate_payload(reexported)
    assert reexported["contract"]["channels"] == payload["contract"]["channels"]
    assert reexported["recipe"]["subscriptions"][0]["service_key"] == "support"
    assert account.id not in str(reexported)
    assert new_account.id not in str(reexported)
    scripts = Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts"
    source_path, installed_path = tmp_path / "source.json", tmp_path / "installed.json"
    for path, document in [(source_path, payload), (installed_path, reexported)]:
        path.write_text(json.dumps(document))
        check = subprocess.run([sys.executable, str(scripts / "check_blueprint.py"), str(path)], capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr
    comparison = subprocess.run([sys.executable, str(scripts / "compare_roundtrip.py"), str(source_path), str(installed_path)], capture_output=True, text=True)
    assert comparison.returncode == 0, comparison.stdout + comparison.stderr
