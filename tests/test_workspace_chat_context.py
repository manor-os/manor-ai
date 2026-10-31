from __future__ import annotations

import pytest
from sqlalchemy import select, text

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.models.goal import Goal
from packages.core.models.task import Task
from packages.core.humans import open_commitment, resolve_commitment
from packages.core.workspace_chat.context import (
    _build_summary,
    _operating_rules_block,
    _query_matches,
    get_summary,
    workspace_search,
)


@pytest.mark.asyncio
async def test_optional_channel_summary_failure_does_not_poison_chat_session(
    db_session,
    monkeypatch,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Transaction isolation test",
        kind="project",
        status="active",
    )
    db_session.add(workspace)
    await db_session.flush()

    async def fail_channel_read(db, _workspace):
        await db.execute(text("SELECT missing_prompt_column FROM channel_configs LIMIT 1"))

    monkeypatch.setattr(
        "packages.core.services.workspace_readiness.list_configured_workspace_channels",
        fail_channel_read,
    )

    summary = await _build_summary(db_session, workspace_id, entity_id)

    assert 'Workspace: "Transaction isolation test"' in summary
    assert (await db_session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id)
    )).scalar_one() == workspace_id



def test_operating_rules_reach_the_chat_summary_as_bounded_instructions() -> None:
    """A rule's description is the operator's instruction; a bare count hid it."""
    block = _operating_rules_block([
        {"rule_key": "rule_1", "name": "English only", "description": "Write every deliverable in English."},
        {"rule_key": "rule_2", "name": "Watchlist edits", "description": "x" * 2500},
        {"rule_key": "rule_3", "name": "Second long", "description": "y" * 2500},
        {"rule_key": "rule_4", "name": "Dropped", "description": "z" * 2500},
    ])

    assert block.startswith("Operating rules (4)")
    assert "- English only: Write every deliverable in English." in block
    assert "x" * 2000 + " ..." in block and "x" * 2001 not in block
    assert "Dropped" not in block and "search rules with manor(action='workspace')" in block
    assert len(block) < 5200


def test_query_matches_exact_substring_and_any_token() -> None:
    assert _query_matches("tour", "Lift lead-to-tour conversion to 40%")
    assert _query_matches(
        "rights distribution royalty campaign",
        "Rights clearance ready",
        "rights_clearance_ready",
    )
    assert _query_matches(
        "first-release goals tasks distribution package",
        "Write file-grounded first-release plan for all five goals",
    )
    assert not _query_matches("playlist pitching", "Rights clearance ready")
    assert _query_matches("", "anything")
    assert not _query_matches("the and", "Rights clearance ready")


@pytest.mark.asyncio
async def test_workspace_search_goals_and_tasks_match_keyword_bags(db_session) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Search match test",
        kind="project",
        status="active",
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Rights clearance ready",
        metric_key="rights_clearance_ready",
        target_value=100,
        status="active",
    )
    task = Task(
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Write file-grounded first-release plan for all five goals",
        description="Draft the first-release plan",
        status="in_progress",
    )
    db_session.add_all([workspace, goal, task])
    await db_session.flush()

    goal_search = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="goals",
        query="rights distribution royalty campaign",
    )
    assert "Rights clearance ready" in goal_search
    assert f"goal_id={goal.id}" in goal_search
    assert f"goal_key={goal.goal_key}" in goal_search

    by_key = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="goals",
        query=goal.goal_key,
    )
    assert f"goal_id={goal.id}" in by_key

    task_search = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="tasks",
        query="first-release goals tasks distribution package",
    )
    assert "Write file-grounded first-release plan for all five goals" in task_search

    miss = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="goals",
        query="playlist pitching",
    )
    assert "Rights clearance ready" not in miss


@pytest.mark.asyncio
async def test_human_queue_answers_are_visible_in_summary_and_search(db_session) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Human queue visibility",
        kind="project",
        status="active",
    )
    db_session.add(workspace)
    await db_session.flush()

    await open_commitment(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        request_kind="decision",
        source_kind="chat",
        source_id="msg_wait",
        expected_input="Which digital distributor should this first single use?",
    )
    cached = await get_summary(db_session, workspace_id, entity_id)
    assert "Which digital distributor should this first single use?" in cached
    assert "Alex Chen" not in cached

    answered = await open_commitment(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        request_kind="input",
        source_kind="chat",
        source_id="msg_answer",
        expected_input="Confirm the legal artist name and track title.",
    )
    await resolve_commitment(
        db_session,
        answered,
        status="fulfilled",
        response={
            "text": (
                "Legal/artist name: Alex Chen performing as Alex Chen. "
                "Track title: Tonight (digital single). "
                "Preferred distributor: DistroKid."
            )
        },
    )

    summary = await get_summary(db_session, workspace_id, entity_id)
    assert "Alex Chen" in summary
    assert "Tonight" in summary
    assert "DistroKid" in summary
    assert "Need Input" in summary
    assert "user-confirmed facts" in summary
    assert "Which digital distributor should this first single use?" in summary

    found = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="humans",
        query="Alex Tonight DistroKid",
    )
    assert "Alex Chen" in found
    assert "Tonight" in found
    assert "DistroKid" in found

    miss = await workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="humans",
        query="playlist pitching",
    )
    assert "Alex Chen" not in miss
