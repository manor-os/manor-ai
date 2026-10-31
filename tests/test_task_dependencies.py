from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.services.task_dependencies import build_dependency_outputs
from packages.core.workers.task_agent_loop import task_brief


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


def test_task_brief_keeps_predecessor_file_paths_and_schedule_details_for_handoff():
    brief = task_brief(SimpleNamespace(
        title="Draft next wave social posts",
        description="",
        expected_output=None,
        details={
            "execution_script": "1. Read the shortlist. 2. Draft three posts.",
            "done_when": "Three posts are saved as drafts.",
            "deliverable": "A draft file per post.",
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
            ],
        },
    ))

    assert "## Outputs of the Tasks this one depends on" in brief
    assert "Selected one primary product angle." in brief
    assert "product-shortlist.md (workspace/social_ops/product-shortlist.md)" in brief
    assert "public-preview.md (https://cdn.example.test/public-preview.md)" in brief
    assert "1. Read the shortlist. 2. Draft three posts." in brief
    assert "Three posts are saved as drafts." in brief
    assert "A draft file per post." in brief
