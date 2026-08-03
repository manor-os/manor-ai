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

    image_url = "https://replicate.delivery/pbxt/generated_file.png"
    count = await file_registrar.register_generated_files(
        json.dumps({
            "outputs": [image_url],
            "primary": image_url,
        }),
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
        knowledge_artifacts=[{"url": image_url, "kind": "image"}],
    )

    assert count == 1
    url_call = next(call for call in calls if call[0] == "url")
    refresh_call = next(call for call in calls if call[0] == "refresh")
    assert url_call[2]["origin"]["workspace_id"] == "ws_1"
    assert url_call[2]["origin"]["task_id"] == "task_1"
    assert [call[1] for call in calls if call[0] == "url"] == [image_url]
    assert refresh_call[1]["origin"]["workspace_id"] == "ws_1"


@pytest.mark.asyncio
async def test_register_generated_files_ignores_remote_images_embedded_in_page_content(
    monkeypatch,
) -> None:
    registered_urls: list[str] = []

    async def fake_register_url(url: str, **_kwargs):
        registered_urls.append(url)
        return True

    monkeypatch.setattr(file_registrar, "_register_url", fake_register_url)

    count = await file_registrar.register_generated_files(
        json.dumps(
            {
                "content": (
                    '<img src="https://media.licdn.com/media/'
                    'AAYABATzAAwAAQAAAAAAAMf6E0TuucIkSYGFrDoCICokGw.png">'
                )
            }
        ),
        entity_id="ent_1",
        user_id="user_1",
        source="chrome",
        origin={"tool_name": "mcp__chrome__get_web_content"},
    )

    assert count == 0
    assert registered_urls == []


@pytest.mark.asyncio
async def test_register_generated_files_accepts_declared_logo_artifact(monkeypatch) -> None:
    registered_urls: list[str] = []

    async def fake_register_url(url: str, **_kwargs):
        registered_urls.append(url)
        return True

    monkeypatch.setattr(file_registrar, "_register_url", fake_register_url)

    url = "https://replicate.delivery/pbxt/final-brand-logo.png"
    count = await file_registrar.register_generated_files(
        json.dumps({"outputs": [url]}),
        entity_id="ent_1",
        source="replicate",
        knowledge_artifacts=[{"url": url, "kind": "image"}],
    )

    assert count == 1
    assert registered_urls == [url]


@pytest.mark.asyncio
async def test_register_generated_files_ignores_model_visible_artifacts_without_sidecar(
    monkeypatch,
) -> None:
    registered_urls: list[str] = []

    async def fake_register_url(url: str, **_kwargs):
        registered_urls.append(url)
        return True

    monkeypatch.setattr(file_registrar, "_register_url", fake_register_url)

    count = await file_registrar.register_generated_files(
        json.dumps({
            "artifacts": [
                {
                    "url": "https://media.licdn.com/media/page-evidence.png",
                    "kind": "image",
                }
            ]
        }),
        entity_id="ent_1",
        source="chrome",
    )

    assert count == 0
    assert registered_urls == []
