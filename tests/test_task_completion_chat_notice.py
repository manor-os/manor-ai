"""A completed Workspace Task hands over its files in one bubble and says so in one event row."""
from unittest.mock import AsyncMock

import pytest

from packages.core.i18n import t
from packages.core.workspace_chat import notifiers

DOCUMENT_ID = "01M5ZA82ZP3XP5HDFFZR7XKWF5"
FILE = {
    "name": "captions.md",
    "type": "file",
    "origin": "created",
    "fs_path": "Workspaces/_by_id/ws/tasks/task/documents/captions.md",
    "open_url": f"/viewer/{DOCUMENT_ID}",
    "document_id": DOCUMENT_ID,
}


@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.asyncio
async def test_completion_posts_a_result_bubble_then_an_event_row(monkeypatch, locale):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)

    await notifiers.notify_task_completed(
        object(), entity_id="entity", workspace_id="workspace", task_id="01TASK0000000000000009E1YVV",
        task_title="Three captions", summary="Saved the three captions.", files=[FILE],
        subscription_id="nova-subscription", agent_name="Nova", locale=locale,
    )

    bubble, row = (call.kwargs for call in post.await_args_list)
    task_ref = {"type": "task", "id": "01TASK0000000000000009E1YVV", "title": "Three captions"}
    assert row["body"] == t(
        "task.execution.task_completed_by", locale=locale, agent_name="Nova", subject="Three captions",
    )
    assert (row["message_kind"], row["author_kind"], row["refs"]) == ("system", "system", [task_ref])
    assert bubble["body"] == "Saved the three captions."
    assert (bubble["message_kind"], bubble["author_kind"]) == ("agent_update", "agent")
    assert bubble["author_subscription_id"] == "nova-subscription"
    assert bubble["refs"] == [task_ref]
    assert bubble["meta"] == {"feedback_target_kind": "task_completion"}
    [attachment] = bubble["attachments"]
    assert attachment["document_id"] == DOCUMENT_ID and attachment["name"] == "captions.md"


@pytest.mark.asyncio
async def test_completion_without_an_agent_or_result_is_only_the_event_row(monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)

    await notifiers.notify_task_completed(
        object(), entity_id="entity", workspace_id="workspace", task_id="01TASK0000000000000009E1YVV",
        task_title=None, summary=None, files=[], locale="en",
    )

    [row] = (call.kwargs for call in post.await_args_list)
    assert row["body"] == "Completed “#9E1YVV”"
