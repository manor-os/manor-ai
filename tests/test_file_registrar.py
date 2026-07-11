import json

import pytest

from packages.core.ai.mcp import file_registrar


def test_extract_naming_context_prefers_tool_args_title() -> None:
    output = json.dumps(
        {
            "prompt": "neon manor lobby at dusk",
            "primary": "https://replicate.delivery/pbxt/generated_file.png",
        }
    )

    context = file_registrar._extract_naming_context(
        output,
        tool_args={"title": "Lobby concept", "prompt": "ignored only when title exists"},
    )

    assert context["title"] == "Lobby concept"
    assert context["prompt"] == "ignored only when title exists"


def test_friendly_remote_name_replaces_generated_names_from_prompt() -> None:
    name = file_registrar._friendly_remote_name(
        "https://replicate.delivery/pbxt/generated_file.png",
        prompt="neon manor lobby at dusk with brass lamps",
    )

    assert name == "neon-manor-lobby-at-dusk-with-brass.png"


def test_friendly_remote_name_keeps_human_url_names() -> None:
    name = file_registrar._friendly_remote_name(
        "https://cdn.example.com/final-campaign-hero.webp",
        prompt="some prompt",
    )

    assert name == "final-campaign-hero.webp"


def test_friendly_remote_name_indexes_multiple_outputs() -> None:
    name = file_registrar._friendly_remote_name(
        "https://replicate.delivery/pbxt/43f760a912a449f18648f3fe51d74ef2.png",
        prompt="spring collection moodboard",
        index=1,
        total=3,
    )

    assert name == "spring-collection-moodboard-2.png"


@pytest.mark.asyncio
async def test_register_generated_files_passes_origin_and_refreshes_files_cache(monkeypatch) -> None:
    calls = []

    async def fake_register_url(url: str, **kwargs):
        calls.append(("url", url, kwargs))
        return True

    async def fake_refresh_workspace_file_cache(**kwargs):
        calls.append(("refresh", kwargs))

    monkeypatch.setattr(file_registrar, "_register_url", fake_register_url)
    monkeypatch.setattr(file_registrar, "_refresh_workspace_file_cache", fake_refresh_workspace_file_cache)

    count = await file_registrar.register_generated_files(
        json.dumps({"image_url": "https://replicate.delivery/pbxt/generated_file.png"}),
        entity_id="ent_1",
        user_id="user_1",
        source="replicate",
        tool_args={"prompt": "workspace hero frame"},
        origin={
            "workspace_id": "ws_1",
            "task_id": "task_1",
            "agent_id": "agent_1",
            "tool_name": "mcp__replicate__generate_image",
        },
    )

    assert count == 1
    url_call = next(call for call in calls if call[0] == "url")
    refresh_call = next(call for call in calls if call[0] == "refresh")
    assert url_call[2]["origin"]["workspace_id"] == "ws_1"
    assert url_call[2]["origin"]["task_id"] == "task_1"
    assert refresh_call[1]["origin"]["workspace_id"] == "ws_1"
