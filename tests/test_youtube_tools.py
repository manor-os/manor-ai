from __future__ import annotations

from dataclasses import replace
import importlib
import json
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.services.youtube_public_video import YouTubePublicVideoMetrics


def _module():
    return importlib.import_module("packages.core.ai.tools.youtube_tools")


def _metrics() -> YouTubePublicVideoMetrics:
    return YouTubePublicVideoMetrics(
        video_id="tsJff6wc2XQ",
        public_url="https://www.youtube.com/watch?v=tsJff6wc2XQ",
        title="The Two-Minute Shutdown Ritual | Stickman Productivity",
        channel_id="UCbIyc_cOTGbOyC7CaIr_ZXg",
        channel_name="Dao Simon",
        subscribers=42,
        published_at="2026-08-10T05:01:29-07:00",
        views=0,
        likes=0,
        comments=None,
        unavailable_fields=("comments",),
        collected_at="2026-08-10T10:00:00+00:00",
    )


def test_youtube_tool_schemas_do_not_expose_paths_or_arbitrary_metric_urls() -> None:
    schemas = {
        schema["function"]["name"]: schema["function"]["parameters"]
        for schema, _ in _module().get_tools()
    }

    assert set(schemas) == {
        "check_youtube_publication_setup",
        "record_youtube_publication",
        "read_youtube_public_metrics",
        "record_youtube_workspace_metrics",
    }
    assert schemas["check_youtube_publication_setup"]["properties"] == {}
    assert set(schemas["record_youtube_publication"]["properties"]) == {
        "public_url",
        "expected_title",
        "expected_video_id",
        "expected_channel_name",
        "source_kind",
    }
    assert schemas["read_youtube_public_metrics"]["properties"] == {}
    assert schemas["record_youtube_workspace_metrics"]["properties"] == {}


def test_verified_publication_count_includes_current_backfill_without_duplicates() -> None:
    module = _module()

    assert module._verified_publication_count(
        [
            {"video_id": "video-1", "watch_url": "https://youtu.be/video-1"},
            {"video_id": "video-2", "watch_url": "https://youtu.be/video-2"},
        ],
        current_video_id="video-2",
    ) == 2
    assert module._verified_publication_count(
        [],
        current_video_id="verified-backfill-video",
    ) == 1


@pytest.mark.asyncio
async def test_publication_preflight_is_bounded_and_model_free(
    monkeypatch,
) -> None:
    module = _module()
    calls: list[tuple[str, dict]] = []

    async def call_chrome(tool_name, arguments, **_kwargs):
        calls.append((tool_name, arguments))
        if tool_name == "status":
            return {"ok": True, "driver": "chrome-extension"}
        if tool_name == "open_or_reuse":
            return {"ok": True, "tabId": 42}
        return {
            "ok": True,
            "url": "https://studio.youtube.com/channel/UC123",
            "title": "Channel dashboard - YouTube Studio",
            "pageContent": '- button "Account menu: Example Channel"',
        }

    monkeypatch.setattr(module, "_call_chrome_mcp_tool", call_chrome)

    result = json.loads(
        await module._check_youtube_publication_setup(
            entity_id="entity-1",
            workspace_id="workspace-1",
            conversation_id="conversation-1",
            _user_id_from_context="user-1",
        )
    )

    assert result["ok"] is True
    assert result["ready"] is True
    assert result["runtime_ready"] is True
    assert result["studio_authenticated"] is True
    assert result["channel"] == "Example Channel"
    assert calls == [
        ("status", {}),
        (
            "open_or_reuse",
            {"url": "https://studio.youtube.com", "active": False},
        ),
        ("read_page", {"tabId": 42, "filter": "all"}),
    ]


@pytest.mark.asyncio
async def test_publication_preflight_returns_setup_blocker_instead_of_failing(
    monkeypatch,
) -> None:
    module = _module()

    async def call_chrome(*_args, **_kwargs):
        return {
            "status": "failed",
            "error": "no_paired_cli_worker",
        }

    monkeypatch.setattr(module, "_call_chrome_mcp_tool", call_chrome)

    result = json.loads(
        await module._check_youtube_publication_setup(
            entity_id="entity-1",
            workspace_id="workspace-1",
            _user_id_from_context="user-1",
        )
    )

    assert result["ok"] is True
    assert result["ready"] is False
    assert result["runtime_ready"] is False
    assert result["blocker_code"] == "manor_cli_unavailable"


@pytest.mark.asyncio
async def test_publication_preflight_detects_youtube_sign_in(
    monkeypatch,
) -> None:
    module = _module()
    responses = iter(
        [
            {"ok": True, "driver": "chrome-extension"},
            {"ok": True, "tabId": 42},
            {
                "ok": True,
                "url": "https://accounts.google.com/ServiceLogin",
                "title": "Sign in",
                "pageContent": "Sign in to continue to YouTube",
            },
        ]
    )

    async def call_chrome(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(module, "_call_chrome_mcp_tool", call_chrome)

    result = json.loads(
        await module._check_youtube_publication_setup(
            entity_id="entity-1",
            workspace_id="workspace-1",
            _user_id_from_context="user-1",
        )
    )

    assert result["ready"] is False
    assert result["runtime_ready"] is True
    assert result["studio_authenticated"] is False
    assert result["blocker_code"] == "youtube_not_signed_in"


@pytest.mark.asyncio
async def test_record_workspace_metrics_writes_daily_idempotent_observations(
    db_session,
) -> None:
    module = _module()
    from packages.core.stats.service import (
        create_stat,
        list_observations,
    )

    stats = {}
    for metric_name, key in module.YOUTUBE_WORKSPACE_STAT_KEYS.items():
        stats[metric_name] = await create_stat(
            db_session,
            entity_id="entity-1",
            workspace_id="workspace-1",
            key=key,
            name=metric_name,
            unit=metric_name,
            window="lifetime" if metric_name == "published_videos" else "latest",
            collector_type="manual",
            goal_eligible=False,
            install_schedule=False,
        )

    receipt = {"source_task_id": "publish-task-1"}
    first = await module._record_workspace_metric_observations(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        receipt=receipt,
        metrics=_metrics(),
        published_video_count=3,
    )
    second = await module._record_workspace_metric_observations(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        receipt=receipt,
        metrics=_metrics(),
        published_video_count=3,
    )

    expected = {
        module.YOUTUBE_WORKSPACE_STAT_KEYS[name]
        for name in ("published_videos", "views", "subscribers", "likes")
    }
    assert set(first) == expected
    assert set(second) == expected
    assert stats["published_videos"].current_value == 3
    assert stats["views"].current_value == 0
    assert stats["subscribers"].current_value == 42
    assert stats["likes"].current_value == 0
    assert stats["comments"].current_value is None
    for metric_name, stat in stats.items():
        observations = await list_observations(
            db_session,
            stat_id=stat.id,
        )
        assert len(observations) == (0 if metric_name == "comments" else 1)


@pytest.mark.asyncio
async def test_record_workspace_metrics_keeps_two_publications_on_same_day(
    db_session,
) -> None:
    module = _module()
    from packages.core.stats.service import create_stat, list_observations

    stats = {}
    for metric_name, key in module.YOUTUBE_WORKSPACE_STAT_KEYS.items():
        stats[metric_name] = await create_stat(
            db_session,
            entity_id="entity-1",
            workspace_id="workspace-1",
            key=key,
            name=metric_name,
            unit=metric_name,
            window="lifetime" if metric_name == "published_videos" else "latest",
            collector_type="manual",
            goal_eligible=False,
            install_schedule=False,
        )

    first_metrics = _metrics()
    second_metrics = replace(
        first_metrics,
        video_id="6JuofLI1HqY",
        public_url="https://www.youtube.com/watch?v=6JuofLI1HqY",
        subscribers=43,
        views=1,
        likes=1,
        collected_at="2026-08-10T20:00:00+00:00",
    )
    await module._record_workspace_metric_observations(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        receipt={"source_task_id": "publish-task-1"},
        metrics=first_metrics,
        published_video_count=3,
    )
    await module._record_workspace_metric_observations(
        db_session,
        entity_id="entity-1",
        workspace_id="workspace-1",
        receipt={"source_task_id": "publish-task-2"},
        metrics=second_metrics,
        published_video_count=4,
    )

    assert stats["published_videos"].current_value == 4
    assert stats["subscribers"].current_value == 43
    for metric_name in ("published_videos", "views", "subscribers", "likes"):
        observations = await list_observations(
            db_session,
            stat_id=stats[metric_name].id,
        )
        assert len(observations) == 2


@pytest.mark.asyncio
async def test_read_metrics_uses_only_the_current_workspace_receipt(
    monkeypatch,
) -> None:
    module = _module()
    receipt = {
        "public_url": "https://www.youtube.com/watch?v=tsJff6wc2XQ",
        "video_id": "tsJff6wc2XQ",
        "source_task_id": "publish-task-1",
    }
    read_receipt = AsyncMock(return_value=receipt)
    fetch_metrics = AsyncMock(return_value=_metrics())
    monkeypatch.setattr(module, "read_publication_receipt", read_receipt)
    monkeypatch.setattr(module, "fetch_youtube_public_metrics", fetch_metrics)

    result = json.loads(
        await module._read_youtube_public_metrics(
            entity_id="entity-1",
            workspace_id="workspace-1",
        )
    )

    assert result["ok"] is True
    assert result["views"] == 0
    assert result["likes"] == 0
    assert result["comments"] is None
    assert result["unavailable_fields"] == ["comments"]
    assert result["timezone"] == "UTC"
    assert result["source_task_id"] == "publish-task-1"
    read_receipt.assert_awaited_once_with(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    fetch_metrics.assert_awaited_once_with(
        receipt["public_url"],
        expected_video_id=receipt["video_id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "expected_task_id"),
    [
        ("workspace_publication", "task-1"),
        ("verified_backfill", None),
    ],
)
async def test_record_publication_uses_runtime_task_provenance(
    source_kind: str,
    expected_task_id: str | None,
    monkeypatch,
) -> None:
    module = _module()
    receipt = {
        "schema_version": 1,
        "video_id": "tsJff6wc2XQ",
        "public_url": "https://www.youtube.com/watch?v=tsJff6wc2XQ",
        "title": _metrics().title,
        "channel_name": _metrics().channel_name,
        "source_kind": source_kind,
        "source_task_id": expected_task_id,
    }
    fetch_metrics = AsyncMock(return_value=_metrics())
    build_receipt = Mock(return_value=receipt)
    write_receipt = AsyncMock(
        return_value={
            "path": "Workspaces/_by_id/folder/technical/latest-youtube-publication.json",
            "display_path": "Workspaces/Demo/technical/latest-youtube-publication.json",
            "document_id": "document-1",
            "receipt": receipt,
        }
    )
    monkeypatch.setattr(module, "fetch_youtube_public_metrics", fetch_metrics)
    monkeypatch.setattr(module, "build_publication_receipt", build_receipt)
    monkeypatch.setattr(module, "write_publication_receipt", write_receipt)

    result = json.loads(
        await module._record_youtube_publication(
            entity_id="entity-1",
            workspace_id="workspace-1",
            task_id="task-1",
            conversation_id="conversation-1",
            _agent_id_from_context="agent-1",
            _user_id_from_context="user-1",
            public_url="https://youtu.be/tsJff6wc2XQ",
            expected_title=_metrics().title,
            expected_video_id=_metrics().video_id,
            expected_channel_name=_metrics().channel_name,
            source_kind=source_kind,
        )
    )

    assert result["ok"] is True
    assert result["receipt_status"] == "recorded"
    assert result["video_id"] == "tsJff6wc2XQ"
    assert build_receipt.call_args.kwargs["source_task_id"] == expected_task_id
    write_receipt.assert_awaited_once()


@pytest.mark.asyncio
async def test_tools_require_workspace_context() -> None:
    result = json.loads(
        await _module()._read_youtube_public_metrics(entity_id="")
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "missing_workspace_context",
            "message": "Entity and Workspace context are required",
        },
    }
