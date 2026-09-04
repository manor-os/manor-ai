from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
)
from packages.core.models.base import generate_ulid
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Agent, Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_setup_service import (
    DEFAULT_FIELDS,
    WorkspaceSetupSession,
    dispatch_workspace_post_commit,
    finalize_setup,
)


async def _register(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _complete_new_draft_fields() -> dict:
    return {
        **dict(DEFAULT_FIELDS),
        "name": "Conversation Confirmed Workspace",
        "kind": "operations",
        "operating_context": "Run a confirmed manual workspace.",
        "primary_work": "Handle the configured workspace work.",
        "services": [{
            "service_key": "workspace_operations",
            "name": "Workspace Operations",
            "description": "Handle the configured workspace work.",
            "autonomy_level": "supervised",
            "owner_role": "operator",
        }],
        "agent_mappings": [{
            "service_key": "workspace_operations",
            "strategy": "create_custom",
            "create_agent_draft": {
                "agent_name": "Workspace Operations Agent",
                "system_prompt": (
                    "Handle workspace operations and stay within this capability."
                ),
                "business_capabilities": ["runtime.discovery"],
            },
        }],
        "channel_config": {},
        WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD: (
            CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION
        ),
        CREATION_PREFERENCES_FIELD: {
            "goal_confirmed": False,
            "autonomy_confirmed": False,
        },
    }


def test_workspace_architect_confirms_creation_choices_in_conversation() -> None:
    from packages.core.services.workspace_architect import ARCHITECT_SYSTEM_PROMPT

    assert "Ask this as one concise combined question" in ARCHITECT_SYSTEM_PROMPT
    assert "ws_confirm_creation_preferences" in ARCHITECT_SYSTEM_PROMPT
    assert "explicit autonomous true/false answer" in ARCHITECT_SYSTEM_PROMPT
    assert "STOP this turn before" in ARCHITECT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_new_workspace_draft_tracks_unconfirmed_creation_choices(
    db_session,
) -> None:
    from packages.core.services.workspace_draft_service import create_draft_shell

    draft = await create_draft_shell(
        db_session,
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    assert draft.fields["_creation_preferences"] == {
        "goal_confirmed": False,
        "autonomy_confirmed": False,
    }
    assert (
        draft.fields[WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD]
        == CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION
    )


@pytest.mark.asyncio
async def test_workspace_architect_can_set_autonomy_without_adding_a_goal(
    db_session,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import _set_autonomy

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields={
            **dict(DEFAULT_FIELDS),
            "_creation_preferences": {
                "goal_confirmed": False,
                "autonomy_confirmed": False,
            },
        },
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _set_autonomy(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        enabled=True,
        cadence="weekly",
    ))

    assert result["ok"] is True
    assert draft.fields["heartbeat_enabled"] is True
    assert draft.fields["heartbeat_cadence"] == "weekly"
    assert draft.fields["goals"] == []
    assert draft.fields["_creation_preferences"] == {
        "goal_confirmed": False,
        "autonomy_confirmed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "tool_args"),
    [
        ("_set_autonomy", {}),
        ("_set_autonomy", {"enabled": "false"}),
        ("_confirm_creation_preferences", {"goal_choice": "none"}),
        (
            "_confirm_creation_preferences",
            {"goal_choice": "none", "autonomous_enabled": "false"},
        ),
    ],
    ids=[
        "set-missing",
        "set-string-false",
        "confirm-missing",
        "confirm-string-false",
    ],
)
async def test_autonomy_tools_reject_non_boolean_choices_without_mutation(
    db_session,
    handler_name: str,
    tool_args: dict,
) -> None:
    from packages.core.ai.tools import workspace_arch_tools

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_complete_new_draft_fields(),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()
    before = copy.deepcopy(draft.fields)

    handler = getattr(workspace_arch_tools, handler_name)
    result = json.loads(await handler(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        **tool_args,
    ))

    assert result["ok"] is False
    assert "boolean" in result["error"]
    assert draft.fields == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "tool_args", "stored_cadence"),
    [
        ("_set_autonomy", {"enabled": True, "cadence": "nonsense"}, None),
        ("_set_autonomy", {"enabled": True}, "nonsense"),
        (
            "_confirm_creation_preferences",
            {
                "goal_choice": "none",
                "autonomous_enabled": True,
                "autonomy_cadence": "nonsense",
            },
            None,
        ),
        (
            "_confirm_creation_preferences",
            {"goal_choice": "none", "autonomous_enabled": True},
            "nonsense",
        ),
    ],
    ids=[
        "set-explicit",
        "set-stored",
        "confirm-explicit",
        "confirm-stored",
    ],
)
async def test_autonomy_tools_reject_invalid_cadence_without_mutation(
    db_session,
    handler_name: str,
    tool_args: dict,
    stored_cadence: str | None,
) -> None:
    from packages.core.ai.tools import workspace_arch_tools

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    fields = _complete_new_draft_fields()
    if stored_cadence is not None:
        fields["heartbeat_cadence"] = stored_cadence
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()
    before = copy.deepcopy(draft.fields)

    handler = getattr(workspace_arch_tools, handler_name)
    result = json.loads(await handler(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        **tool_args,
    ))

    assert result["ok"] is False
    assert "cadence" in result["error"]
    assert draft.fields == before


@pytest.mark.asyncio
async def test_creation_confirmation_reuses_a_valid_stored_cron_cadence(
    db_session,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import (
        _confirm_creation_preferences,
    )

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    fields = _complete_new_draft_fields()
    fields["heartbeat_cadence"] = "0 9 * * *"
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    result = json.loads(await _confirm_creation_preferences(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        goal_choice="none",
        autonomous_enabled=True,
    ))

    assert result["ok"] is True
    assert draft.fields["heartbeat_enabled"] is True
    assert draft.fields["heartbeat_cadence"] == "0 9 * * *"
    assert draft.fields[CREATION_PREFERENCES_FIELD] == {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }


@pytest.mark.asyncio
async def test_workspace_draft_is_not_ready_until_both_choices_are_confirmed(
    db_session,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import (
        _confirm_creation_preferences,
        _lint_draft,
    )

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=_complete_new_draft_fields(),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    before = json.loads(await _lint_draft(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
    ))
    assert {
        issue["where"]
        for issue in before["issues"]
        if issue["severity"] == "P0"
    } == {"creation_preferences.goal", "creation_preferences.autonomy"}

    await _confirm_creation_preferences(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        goal_choice="none",
        autonomous_enabled=False,
    )

    after = json.loads(await _lint_draft(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
    ))
    assert after["p0"] == 0
    assert after["ok_to_finalize"] is True
    assert draft.fields["goals"] == []
    assert draft.fields["heartbeat_enabled"] is False


@pytest.mark.asyncio
async def test_new_draft_with_damaged_creation_preferences_fails_closed(
    db_session,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import _lint_draft

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = None
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.flush()

    lint = json.loads(await _lint_draft(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
    ))

    assert {
        issue["where"]
        for issue in lint["issues"]
        if issue["severity"] == "P0"
    } == {"creation_preferences.goal", "creation_preferences.autonomy"}
    assert lint["ok_to_finalize"] is False


@pytest.mark.asyncio
async def test_creation_preferences_complete_through_conversation(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import (
        _confirm_creation_preferences,
    )
    from packages.core.services import workspace_draft_service

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = await workspace_draft_service.create_draft_shell(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
    )
    draft.fields = _complete_new_draft_fields()
    await db_session.flush()

    async def fake_architect_turn(
        db,
        *,
        draft,
        entity_id,
        user_id,
        user_message,
        **_kwargs,
    ) -> str:
        if user_message == "Finish setup":
            return (
                "Do you want to set a Goal, and should autonomous mode "
                "start after creation?"
            )
        result = json.loads(await _confirm_creation_preferences(
            db,
            entity_id=entity_id,
            user_id=user_id,
            draft_id=draft.id,
            goal_choice="none",
            autonomous_enabled=False,
        ))
        assert result["ok"] is True
        return "Confirmed: no Goal and autonomous mode stays off."

    monkeypatch.setattr(
        workspace_draft_service,
        "_architect_turn",
        fake_architect_turn,
    )

    question, draft = await workspace_draft_service.process_draft_message(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        user_id=creator_id,
        user_message="Finish setup",
    )
    assert "set a Goal" in question
    assert draft.ready is False
    assert draft.status == "active"
    assert draft.missing == ["creation_preferences"]

    confirmation, draft = await workspace_draft_service.process_draft_message(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        user_id=creator_id,
        user_message="No Goal, and keep autonomous mode off.",
    )
    assert confirmation.startswith("Confirmed:")
    assert draft.ready is True
    assert draft.status == "ready"
    assert draft.missing == []
    assert draft.fields["goals"] == []
    assert draft.fields["heartbeat_enabled"] is False


@pytest.mark.asyncio
async def test_draft_fields_api_rejects_internal_creation_state(
    client: AsyncClient,
    db_session,
) -> None:
    from packages.core.models.user import User
    from packages.core.services.workspace_draft_service import create_draft_shell

    headers = await _register(client, "draft_private_creation_state")
    user = (await db_session.execute(
        select(User).where(User.email == "draft_private_creation_state@test.com")
    )).scalar_one()
    draft = await create_draft_shell(
        db_session,
        entity_id=user.entity_id,
        user_id=user.id,
    )
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/workspace-drafts/{draft.id}/fields",
        headers=headers,
        json={
            "heartbeat_enabled": True,
            CREATION_PREFERENCES_FIELD: None,
        },
    )

    assert response.status_code == 400
    assert "Internal draft fields cannot be updated" in response.json()["detail"]
    await db_session.refresh(draft)
    assert draft.fields["heartbeat_enabled"] is False
    assert draft.fields[CREATION_PREFERENCES_FIELD] == {
        "goal_confirmed": False,
        "autonomy_confirmed": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_patch", "invalidated_preference", "username"),
    [
        (
            {
                "goals": [{
                    "goal_key": "completed_items",
                    "title": "Completed items",
                    "target": "10",
                    "cadence": "weekly",
                }],
            },
            "goal_confirmed",
            "draft_public_goal_edit",
        ),
        (
            {"heartbeat_enabled": True},
            "autonomy_confirmed",
            "draft_public_autonomy_edit",
        ),
    ],
)
async def test_public_draft_choice_edits_require_fresh_conversation_confirmation(
    client: AsyncClient,
    db_session,
    field_patch: dict,
    invalidated_preference: str,
    username: str,
) -> None:
    from packages.core.models.user import User

    headers = await _register(client, username)
    user = (await db_session.execute(
        select(User).where(User.email == f"{username}@test.com")
    )).scalar_one()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }
    draft = WorkspaceDraft(
        entity_id=user.entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/workspace-drafts/{draft.id}/fields",
        headers=headers,
        json=field_patch,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["status"] == "active"
    assert body["missing"] == ["creation_preferences"]
    assert (
        body["fields"][CREATION_PREFERENCES_FIELD][invalidated_preference]
        is False
    )


def test_noop_public_draft_choice_edit_preserves_confirmation() -> None:
    from packages.core.services.workspace_draft_service import (
        apply_public_field_updates,
    )

    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }
    draft = WorkspaceDraft(
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )

    apply_public_field_updates(draft, {
        "goals": fields["goals"],
        "heartbeat_enabled": fields["heartbeat_enabled"],
        "heartbeat_cadence": fields.get("heartbeat_cadence"),
    })

    assert draft.fields[CREATION_PREFERENCES_FIELD] == {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }


@pytest.mark.asyncio
async def test_finalize_revalidates_stale_ready_draft(
    db_session,
) -> None:
    from packages.core.services.workspace_draft_service import finalize_draft

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = None
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.flush()

    with pytest.raises(ValueError, match="creation_preferences"):
        await finalize_draft(
            db_session,
            draft_id=draft.id,
            entity_id=entity_id,
            user_id=creator_id,
        )

    assert draft.ready is False
    assert draft.status == "active"
    assert draft.missing == ["creation_preferences"]


@pytest.mark.asyncio
async def test_finalize_http_persists_verified_not_ready_state(
    client: AsyncClient,
    db_session,
) -> None:
    from packages.core.models.user import User

    headers = await _register(client, "draft_finalize_revalidation")
    user = (await db_session.execute(
        select(User).where(User.email == "draft_finalize_revalidation@test.com")
    )).scalar_one()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = None
    draft = WorkspaceDraft(
        entity_id=user.entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.commit()

    response = await client.post(
        f"/api/v1/workspace-drafts/{draft.id}/finalize",
        headers=headers,
    )

    assert response.status_code == 400
    assert "creation_preferences" in response.json()["detail"]
    await db_session.refresh(draft)
    assert draft.ready is False
    assert draft.status == "active"
    assert draft.missing == ["creation_preferences"]

    persisted = await client.get(
        f"/api/v1/workspace-drafts/{draft.id}",
        headers=headers,
    )
    assert persisted.status_code == 200
    assert persisted.json()["ready"] is False
    assert persisted.json()["status"] == "active"
    assert persisted.json()["missing"] == ["creation_preferences"]


@pytest.mark.asyncio
async def test_finalize_http_lint_outage_remains_retryable(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.models.user import User
    from packages.core.services import workspace_draft_service

    headers = await _register(client, "draft_finalize_lint_outage")
    user = (await db_session.execute(
        select(User).where(User.email == "draft_finalize_lint_outage@test.com")
    )).scalar_one()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }
    draft = WorkspaceDraft(
        entity_id=user.entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.commit()
    monkeypatch.setattr(
        workspace_draft_service,
        "runtime_lint_workspace_draft",
        AsyncMock(return_value=None),
    )

    response = await client.post(
        f"/api/v1/workspace-drafts/{draft.id}/finalize",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Draft readiness could not be verified"
    await db_session.refresh(draft)
    assert draft.ready is True
    assert draft.status == "ready"


@pytest.mark.asyncio
async def test_finalize_fails_closed_when_lint_cannot_be_verified(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.services import workspace_draft_service

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    fields = _complete_new_draft_fields()
    fields[CREATION_PREFERENCES_FIELD] = {
        "goal_confirmed": True,
        "autonomy_confirmed": True,
    }
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.flush()
    monkeypatch.setattr(
        workspace_draft_service,
        "runtime_lint_workspace_draft",
        AsyncMock(return_value=None),
    )

    with pytest.raises(ValueError, match="readiness could not be verified"):
        await workspace_draft_service.finalize_draft(
            db_session,
            draft_id=draft.id,
            entity_id=entity_id,
            user_id=creator_id,
        )

    assert draft.ready is False
    assert draft.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("heartbeat_enabled", "with_goal"),
    [(False, False), (False, True), (True, False), (True, True)],
    ids=[
        "manual-no-goal",
        "manual-with-goal",
        "autonomous-no-goal",
        "autonomous-with-goal",
    ],
)
async def test_workspace_draft_creation_modes_are_independent(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_enabled: bool,
    with_goal: bool,
) -> None:
    from packages.core.tasks import ai_tasks

    entity_id = generate_ulid()
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Workspace Creation Agent",
        system_prompt="Handle the workspace's configured service.",
        status="active",
    )
    db_session.add(agent)
    await db_session.flush()

    goals = []
    if with_goal:
        goals.append({
            "goal_key": "completed_items",
            "title": "Completed items",
            "description": "Track completed workspace items.",
            "target": "10",
            "cadence": "weekly",
        })
    session = WorkspaceSetupSession(
        entity_id=entity_id,
        fields={
            "name": "Independent Creation Mode",
            "kind": "operations",
            "operating_context": "Verify independent goal and runtime choices.",
            "primary_work": "Handle incoming workspace work.",
            "services": [{
                "service_key": "workspace_operations",
                "name": "Workspace operations",
                "description": "Handle incoming workspace work.",
                "autonomy_level": "supervised",
                "owner_role": "operator",
            }],
            "agent_mappings": [{
                "service_key": "workspace_operations",
                "agent_id": agent.id,
                "strategy": "match",
            }],
            "goals": goals,
            "channel_config": {},
            "heartbeat_enabled": heartbeat_enabled,
            "heartbeat_cadence": "daily",
            "_creation_preferences": {
                "goal_confirmed": True,
                "autonomy_confirmed": True,
            },
        },
        messages=[],
        ready=True,
        missing=[],
    )

    workspace_id = await finalize_setup(session, db_session)
    await db_session.commit()
    workspace = await db_session.get(Workspace, workspace_id)
    assert workspace is not None
    assert workspace.status == "active"
    assert workspace.heartbeat_enabled is heartbeat_enabled
    assert "_creation_preferences" not in (workspace.operating_model or {})
    assert "_draft_schema_version" not in (workspace.operating_model or {})

    goal_rows = list((await db_session.execute(
        select(Goal).where(Goal.workspace_id == workspace_id)
    )).scalars().all())
    assert len(goal_rows) == int(with_goal)
    jobs = list((await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == workspace_id)
    )).scalars().all())
    assert any(job.execution_type == "strategist_review" for job in jobs) is heartbeat_enabled
    assert any(job.execution_type == "goal_measurement" for job in jobs) is (
        heartbeat_enabled and with_goal
    )

    strategist_calls: list[dict] = []
    monkeypatch.setattr(
        ai_tasks,
        "run_strategist_review",
        SimpleNamespace(apply_async=lambda *args, **kwargs: strategist_calls.append({
            "args": args,
            "kwargs": kwargs,
        })),
    )
    monkeypatch.setattr(
        ai_tasks,
        "send_agent_greetings",
        SimpleNamespace(delay=lambda *args, **kwargs: None),
    )
    dispatch = await dispatch_workspace_post_commit(
        db_session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        strategist_countdown_seconds=0,
    )
    await db_session.commit()

    assert dispatch["strategist_dispatched"] is heartbeat_enabled
    assert bool(strategist_calls) is heartbeat_enabled
