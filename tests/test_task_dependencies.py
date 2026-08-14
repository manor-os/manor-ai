from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import runtime_task_ticket_prompt
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.services.task_dependencies import build_dependency_outputs
from packages.core.services.task_execution_reconcile import _actual_output_from_steps


@pytest.mark.asyncio
async def test_dependency_outputs_include_legacy_response_and_step_files(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Draft source brief",
            status="completed",
            actual_output={
                "response": "Legacy agent produced the source brief.",
                "steps": [
                    {
                        "status": "done",
                        "result_summary": "Step created a reusable markdown file.",
                        "files": [
                            {
                                "name": "source-brief.md",
                                "fs_path": "/workspace/source-brief.md",
                            }
                        ],
                    }
                ],
            },
        )
    )
    await db_session.commit()

    outputs = await build_dependency_outputs(
        db_session,
        entity_id=entity_id,
        dependency_ids=[task_id],
    )

    assert outputs[0]["result_summary"] == "Legacy agent produced the source brief."
    assert outputs[0]["files"] == [{"name": "source-brief.md", "fs_path": "/workspace/source-brief.md"}]


@pytest.mark.asyncio
async def test_dependency_outputs_dedupe_top_level_and_step_files(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Create reusable deck",
            status="completed",
            actual_output={
                "summary": "Deck is ready.",
                "files": [
                    {"name": "deck.pptx", "fs_path": "/workspace/deck.pptx"},
                ],
                "steps": [
                    {
                        "status": "done",
                        "files": [
                            {"name": "deck.pptx", "fs_path": "/workspace/deck.pptx"},
                            {"name": "deck-notes.md", "fs_path": "/workspace/deck-notes.md"},
                        ],
                    }
                ],
            },
        )
    )
    await db_session.commit()

    outputs = await build_dependency_outputs(
        db_session,
        entity_id=entity_id,
        dependency_ids=[task_id],
    )

    assert outputs[0]["files"] == [
        {"name": "deck.pptx", "fs_path": "/workspace/deck.pptx"},
        {"name": "deck-notes.md", "fs_path": "/workspace/deck-notes.md"},
    ]


@pytest.mark.asyncio
async def test_dependency_outputs_keep_complete_canonical_envelope_text(db_session):
    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    complete_brief = (
        "Chosen title\nThe Two-Minute Reset\n\n"
        "Target viewer\nBusy adults.\n\n"
        "Core curiosity hook\nCan two minutes rescue the day?\n\n"
        + "Story beat: make the next tiny action visible.\n" * 45
        + "Production/script constraints\nUse a faceless stickman and bottom subtitles."
    )
    actual_output = _actual_output_from_steps(
        SimpleNamespace(id=plan_id, entity_id=entity_id, status="completed"),
        [
            SimpleNamespace(
                step_key="select_daily_topic",
                kind="subagent",
                step_status="done",
                result={
                    "status": "succeeded",
                    "summary": "Selected today's topic.",
                    "text": "Selected today's topic.",
                    "outputs": {"text": complete_brief},
                },
                error=None,
            )
        ],
    )
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Select today's topic",
            status="completed",
            actual_output=actual_output,
        )
    )
    await db_session.commit()

    outputs = await build_dependency_outputs(
        db_session,
        entity_id=entity_id,
        dependency_ids=[task_id],
    )

    assert outputs[0]["result_summary"] == complete_brief


def test_task_prompt_keeps_predecessor_file_paths_for_handoff():
    prompt = runtime_task_ticket_prompt(
        {
            "title": "Draft next wave social posts",
            "description": "",
            "priority": 3,
            "task_type": "content_ops",
            "details": {
                "dep_outputs": [
                    {
                        "task_title": "Product shortlist",
                        "result_summary": "Selected one primary product angle.",
                        "files": [
                            {
                                "name": "product-shortlist.md",
                                "path": "workspace/social_ops/product-shortlist.md",
                            },
                            {
                                "name": "public-preview.md",
                                "file_url": "https://cdn.example.test/public-preview.md",
                            },
                        ],
                    }
                ]
            },
        }
    )

    assert "## Predecessor Task Outputs" in prompt
    assert "product-shortlist.md (workspace/social_ops/product-shortlist.md)" in prompt
    assert "public-preview.md (https://cdn.example.test/public-preview.md)" in prompt
