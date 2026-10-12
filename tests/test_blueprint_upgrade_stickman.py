"""The actual screenshot Blueprint upgrades in its original Workspace."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.core.blueprints import component_upgrade, reconfigure
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Document
from packages.core.models.skill import Skill
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from tests.test_blueprint_reconfiguration import payload as small_payload


STICKMAN = Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs/solo_company/solo-faceless-stickman-studio-v1.json"


async def _review_apply(db, workspace, source, *, blueprint_id):
    reviewed = await reconfigure.plan(db, workspace=workspace, payload=source, source_payload=source,
                                     current_version="test-upgrade", source_blueprint_id=blueprint_id)
    result = await reconfigure.apply(
        db, workspace=workspace, payload=source, source_payload=source, by_user_id=None, actor=None,
        current_version="test-upgrade", source_blueprint_id=blueprint_id,
        expected_blueprint_fingerprint=reviewed["blueprint_fingerprint"],
        expected_plan_fingerprint=reviewed["plan_fingerprint"],
        configuration_resolutions=[{"key": item["key"], "resolution": "use_blueprint"} for item in reviewed["items"]],
    )
    return reviewed, result


@pytest.mark.skipif(not STICKMAN.is_file(), reason="The optional Cloud Blueprint catalog is not included in OSS")
async def test_full_stickman_payload_upgrades_skill_and_configuration_and_undo_keeps_history(db_session):
    source = json.loads(STICKMAN.read_text())
    blueprint_id = "builtin:solo-faceless-stickman-studio-v1"
    installed = await install_blueprint(db_session, entity_id=generate_ulid(), payload=source,
                                        mode=InstallMode.LIVE, blueprint_id=blueprint_id,
                                        workspace_name="My original Stickman studio")
    workspace = await db_session.get(Workspace, installed.workspace_id)
    original_id, original_name = workspace.id, workspace.name
    assert not (await db_session.execute(select(Task.id).where(Task.workspace_id == original_id))).scalars().all()
    assert not (await db_session.execute(select(WorkflowRun.id).where(WorkflowRun.workspace_id == original_id))).scalars().all()
    fresh = await reconfigure.plan(db_session, workspace=workspace, payload=source, source_payload=source,
                                   current_version="test-upgrade", source_blueprint_id=blueprint_id)
    differences = [(item["key"], item["action"], item["current_content"], item["new_content"])
                   for item in fresh["items"] if item["action"] != "unchanged"]
    assert not differences, differences
    initial_components = await component_upgrade.capture_components(db_session, workspace=workspace, payload=source)
    flow_id = initial_components["workflow:stickman-create-video-v1"]["id"]
    task = Task(entity_id=workspace.entity_id, workspace_id=original_id, title="Existing verified video", status="completed")
    run = WorkflowRun(entity_id=workspace.entity_id, workspace_id=original_id, workflow_id=flow_id,
                      status="completed", definition_snapshot={"historic": "immutable definition"},
                      step_results={"video": {"artifact": "verified-existing-video.mp4"}})
    db_session.add_all([task, run])
    await db_session.flush()
    documents = {row.id: (row.name, deepcopy(row.metadata_)) for row in (await db_session.execute(select(Document).where(Document.entity_id == workspace.entity_id))).scalars().all()}
    assert documents  # Includes the actual initialized Topic Ledger, not a stub.
    changed = deepcopy(source)
    old_context = source["recipe"]["operating_model"]["context"]
    changed["recipe"]["operating_model"]["context"] += " Preserve this original production Workspace during upgrades."
    changed["embedded"]["skills"][0]["system_prompt"] += "\nUpgrade regression: preserve accepted render receipts."
    reviewed, result = await _review_apply(db_session, workspace, changed, blueprint_id=blueprint_id)
    assert result["synchronization_status"] == "current", [(item["key"], item["current_content"], item["new_content"]) for item in reviewed["items"] if item["current_content"] != item["new_content"] and item["action"] == "unchanged"]
    assert workspace.id == original_id and workspace.name == original_name
    assert workspace.operating_context == changed["recipe"]["operating_model"]["context"]
    skill_id = initial_components["skill:stickman-video-creator"]["id"]
    assert (await db_session.get(Skill, skill_id)).system_prompt == changed["embedded"]["skills"][0]["system_prompt"]
    current_components = await component_upgrade.capture_components(db_session, workspace=workspace, payload=changed)
    assert {key: row["id"] for key, row in current_components.items()} == {key: row["id"] for key, row in initial_components.items()}
    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert workspace.id == original_id and workspace.name == original_name and workspace.operating_context == old_context
    assert (await db_session.get(Skill, skill_id)).system_prompt == source["embedded"]["skills"][0]["system_prompt"]
    assert (await db_session.get(Task, task.id)).status == "completed"
    assert (await db_session.get(WorkflowRun, run.id)).definition_snapshot == {"historic": "immutable definition"}
    assert (await db_session.get(WorkflowRun, run.id)).step_results == {"video": {"artifact": "verified-existing-video.mp4"}}
    assert {row.id: (row.name, row.metadata_) for row in (await db_session.execute(select(Document).where(Document.entity_id == workspace.entity_id))).scalars().all()} == documents
    assert (await db_session.execute(select(Workspace.id).where(Workspace.entity_id == workspace.entity_id))).scalars().all() == [original_id]


async def test_shared_agent_upgrade_retargets_only_original_workspace_channel_and_undo(db_session):
    source = small_payload()
    source["embedded"]["agents"] = [{"slug": "support", "name": "Support", "system_prompt": "Original support instructions"}]
    source["recipe"]["subscriptions"] = [{"service_key": "support", "agent_slug": "support"}]
    blueprint_id = "builtin:upgrade-test"
    installed = await install_blueprint(db_session, entity_id=generate_ulid(), payload=source,
                                        mode=InstallMode.LIVE, blueprint_id=blueprint_id)
    workspace = await db_session.get(Workspace, installed.workspace_id)
    subscription = (await db_session.execute(select(AgentSubscription).where(AgentSubscription.workspace_id == workspace.id))).scalar_one()
    original_agent_id, original_subscription_id = subscription.agent_id, subscription.id
    other_workspace = Workspace(entity_id=workspace.entity_id, name="Other production Workspace")
    account = ChannelConfig(entity_id=workspace.entity_id, owner_user_id=generate_ulid(), channel_type="telegram", provider="telegram", status="active", credentials={"api_token": "must-remain-private"})
    db_session.add_all([other_workspace, account])
    await db_session.flush()
    other_subscription = AgentSubscription(entity_id=workspace.entity_id, workspace_id=other_workspace.id, agent_id=original_agent_id, service_key="support", status="active")
    db_session.add(other_subscription)
    await db_session.flush()
    mine = Channel(entity_id=workspace.entity_id, workspace_id=workspace.id, user_id=account.owner_user_id,
                   type="telegram", agent_id=original_agent_id, agent_subscription_id=subscription.id,
                   config={"channel_config_id": account.id}, status="active")
    other = Channel(entity_id=workspace.entity_id, workspace_id=other_workspace.id, user_id=account.owner_user_id,
                    type="telegram", agent_id=original_agent_id, agent_subscription_id=other_subscription.id,
                    config={"channel_config_id": account.id}, status="active")
    db_session.add_all([mine, other])
    await db_session.flush()
    changed = deepcopy(source)
    changed["embedded"]["agents"][0]["system_prompt"] = "New support instructions"
    await _review_apply(db_session, workspace, changed, blueprint_id=blueprint_id)
    assert subscription.id == original_subscription_id and subscription.agent_id != original_agent_id
    assert mine.agent_id == subscription.agent_id and mine.agent_subscription_id == original_subscription_id
    assert other.agent_id == other_subscription.agent_id == original_agent_id
    assert (await db_session.get(Agent, original_agent_id)).system_prompt == "Original support instructions"
    assert (await db_session.get(Agent, mine.agent_id)).system_prompt == "New support instructions"
    await reconfigure.revert(db_session, workspace=workspace, by_user_id=None, actor=None)
    assert mine.agent_id == subscription.agent_id == original_agent_id
    assert mine.agent_subscription_id == subscription.id == original_subscription_id
    assert other.agent_id == other_subscription.agent_id == original_agent_id
    assert account.credentials == {"api_token": "must-remain-private"}
