from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.simulation_runtime import (
    resolve_simulation_action,
    start_simulation_run,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace import Workspace


def _payload() -> dict:
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "slug": "simulation-runtime-test",
            "title": "Blueprint Video Studio",
            "summary": "Plan, produce, and review one video.",
            "kind": "one_person_company",
            "category": "content.video",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {
                "primary_work": "Create a review-ready Blueprint video.",
                "services": [
                    {"key": "video.strategy", "name": "Video strategy", "description": "Choose a useful direction."},
                    {"key": "video.script", "name": "Script", "description": "Write the approved script."},
                    {"key": "video.production", "name": "Production", "description": "Produce and verify the video."},
                ],
            },
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [{"name": "Produce the approved video", "description": "Create and verify an MP4."}],
            "goals": [{"metric_key": "video_complete", "title": "Complete one approved video", "target_value": 1}],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": {"runnable_in_simulation": True},
        },
    }


async def _workspace_messages(db: AsyncSession, workspace_id: str) -> list[Message]:
    return list((await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.workspace_id == workspace_id)
        .order_by(Message.created_at, Message.id)
    )).scalars().all())


async def test_simulation_run_persists_chat_stages_and_continues_to_goal(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    installed = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_payload(),
        mode=InstallMode.SIMULATE,
        user_id=user_id,
    )
    workspace = await db_session.get(Workspace, installed.workspace_id)
    assert workspace is not None

    state = await start_simulation_run(
        db_session,
        workspace=workspace,
        user_id=user_id,
    )
    assert state["status"] == "waiting"
    assert state["stage_index"] == 2
    assert state["stage_count"] == 13

    safety_counter = 0
    while state["status"] == "waiting":
        safety_counter += 1
        assert safety_counter <= 6
        message = await db_session.get(Message, state["waiting_message_id"])
        assert message is not None
        action = message.pending_action
        assert action["simulation_runtime"] is True
        kind = action["kind"]
        choice = {
            "approve_proposals": "approve",
            "needs_input": "provide_answers",
            "governance_approval": "approve",
            "workflow_retry": "retry",
        }[kind]
        await resolve_simulation_action(
            db_session,
            workspace=workspace,
            message=message,
            user_id=user_id,
            choice=choice,
            payload={"answers": {"audience": "primary"}} if kind == "needs_input" else None,
        )
        state = {
            "status": workspace.settings["simulation_run"]["status"],
            "waiting_message_id": workspace.settings["simulation_run"].get("waiting_message_id"),
        }

    assert state["status"] == "completed"
    messages = await _workspace_messages(db_session, workspace.id)
    runtime_messages = [
        message for message in messages
        if (message.meta or {}).get("simulation_runtime") is True
    ]
    assert len([message for message in runtime_messages if message.message_kind == "proposal"]) == 3
    assert any((message.meta or {}).get("simulation_artifacts") for message in runtime_messages)
    assert runtime_messages[-1].message_kind == "goal_alert"
    assert "Goal completed" in (runtime_messages[-1].content or "")
    assert workspace.settings["simulation_run"]["next_stage_index"] == 13
    assert len(workspace.settings["simulation_run"]["decisions"]) == 6


async def test_start_repairs_a_legacy_name_only_sandbox_and_restart_keeps_audit(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    installed = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_payload(),
        mode=InstallMode.SIMULATE,
    )
    workspace = await db_session.get(Workspace, installed.workspace_id)
    assert workspace is not None
    workspace.settings = {
        "sandbox": True,
        "_blueprint": dict(workspace.settings["_blueprint"]),
    }
    await db_session.flush()

    first = await start_simulation_run(db_session, workspace=workspace)
    first_messages = await _workspace_messages(db_session, workspace.id)
    second = await start_simulation_run(db_session, workspace=workspace, restart=True)
    second_messages = await _workspace_messages(db_session, workspace.id)

    assert first["run_id"] != second["run_id"]
    assert first["status"] == second["status"] == "waiting"
    assert workspace.settings["simulation_experience"]["stages"]
    assert workspace.settings["simulation_repaired_at"]
    assert len(second_messages) > len(first_messages)
    assert first_messages[-1].resolved_at is not None
    assert first_messages[-1].resolution["choice"] == "stopped"
