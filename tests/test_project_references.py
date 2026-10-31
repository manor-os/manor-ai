"""A project's references (its voices) are pinned once and shared down its tree."""
import json
from contextlib import asynccontextmanager

import pytest

from packages.core.services.project_references import resolve_project_reference
from packages.core.services.workflow_project_service import create_workflow_project, patch_workflow_project

TTS = "google/gemini-2.5-flash-preview-tts"


async def _series_with_episodes(db, *, declare: bool):
    series = await create_workflow_project(
        db, entity_id="ent1", workspace_id="ws1", project_type="podcast_series", created_by="user1",
        state={"title": "Ghost lantern", **({"references": {"voices": {"narrator": {}}}} if declare else {})},
    )
    episodes = [await create_workflow_project(
        db, entity_id="ent1", workspace_id="ws1", project_type="podcast_episode", created_by="user1",
        state={"parent_project_id": series.id, "number": number},
    ) for number in (1, 2)]
    await db.commit()
    return series, episodes


@pytest.fixture
def project_session(db_session, monkeypatch):
    import packages.core.database as database

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(database, "async_session", session)
    return db_session


@pytest.mark.asyncio
async def test_every_episode_speaks_in_the_voice_its_series_pins(project_session):
    from packages.core.ai.tools.extended_tools import _resolve_project_narrator_profile

    series, (first, second) = await _series_with_episodes(project_session, declare=True)
    common = {"entity_id": "ent1", "workspace_id": "ws1", "candidate_model": TTS, "voice_instructions": "Warm."}

    narrator, receipt = await _resolve_project_narrator_profile(project_id=first.id, role="narrator", **common)
    again, again_receipt = await _resolve_project_narrator_profile(project_id=second.id, role="narrator", **common)
    host, _ = await _resolve_project_narrator_profile(project_id=second.id, role="host", **common)

    assert narrator == again and narrator["voice"] == "Puck"
    assert receipt == {"scope": "project", "kind": "voices", "role": "narrator", "project_id": series.id,
                       "requested_project_id": first.id, "inherited": True, "pinned_now": True}
    assert again_receipt["pinned_now"] is False and again_receipt["requested_project_id"] == second.id
    # Another role gets a voice of its own, held by the same series.
    assert host["voice"] != narrator["voice"]
    await project_session.refresh(series)
    assert set(series.state["references"]["voices"]) == {"narrator", "host"}
    # Pinning is not a Flow state change: the Flow's next patch still applies.
    assert series.revision == 0


@pytest.mark.asyncio
async def test_without_a_declared_slot_the_outermost_project_holds_the_voice(project_session):
    series, (episode, _) = await _series_with_episodes(project_session, declare=False)

    reference = await resolve_project_reference(
        project_session, entity_id="ent1", workspace_id="ws1", project_id=episode.id,
        kind="voices", role="narrator", create=lambda _siblings: {"version": 1, "voice": "Kore"},
    )

    assert reference.project_id == series.id and reference.inherited


@pytest.mark.asyncio
async def test_a_flow_patch_never_clears_a_voice_pinned_after_it_read_the_state(project_session):
    series, (episode, _) = await _series_with_episodes(project_session, declare=True)
    stale_state = dict(series.state)
    await resolve_project_reference(
        project_session, entity_id="ent1", workspace_id="ws1", project_id=episode.id,
        kind="voices", role="narrator", create=lambda _siblings: {"version": 1, "voice": "Kore"},
    )

    patched = await patch_workflow_project(
        project_session, project_id=series.id, entity_id="ent1", workspace_id="ws1",
        expected_revision=0, state={**stale_state, "episodes_made": 1},
    )

    assert patched.state["episodes_made"] == 1
    assert patched.state["references"]["voices"]["narrator"]["voice"] == "Kore"


@pytest.mark.asyncio
async def test_a_project_that_holds_a_voice_decides_narration_inside_it(monkeypatch):
    from packages.core.ai.tools import extended_tools

    calls: dict = {}

    class ReachedProject(Exception):
        pass

    async def holds(**kwargs):
        calls["holds"] = kwargs
        return True

    async def project_profile(**kwargs):
        calls["profile"] = kwargs
        raise ReachedProject

    async def actor(*_args):
        return "user"

    async def model(*_args, **_kwargs):
        return TTS, extended_tools.AudioGenerationRole.VOICE

    async def runnable(_user, _entity, selected, _role):
        return selected, "managed"

    async def no_language(**_kwargs):
        return ""

    async def other_scope(**_kwargs):
        raise AssertionError("a project's voice wins over the scope the call asked for")

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", model)
    monkeypatch.setattr(extended_tools, "_runnable_audio_model", runnable)
    monkeypatch.setattr(extended_tools, "_workspace_default_audio_language", no_language)
    monkeypatch.setattr(extended_tools, "_workspace_stickman_studio_profile", lambda **_k: _empty())
    monkeypatch.setattr(extended_tools, "_project_holds_voice", holds)
    monkeypatch.setattr(extended_tools, "_resolve_project_narrator_profile", project_profile)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", other_scope)

    with pytest.raises(ReachedProject):
        await extended_tools._generate_audio_handler(
            entity_id="ent1", user_id="user", prompt="Episode two.", purpose="narration",
            narration_voice_mode="random_per_task", voice_role="host", workspace_id="ws1", task_id="task1",
            _workflow_project_id_from_context="episode2",
        )

    assert calls["holds"]["role"] == "host" and calls["holds"]["project_id"] == "episode2"
    assert calls["profile"]["role"] == "host" and calls["profile"]["project_id"] == "episode2"


async def _empty():
    return {}


@pytest.mark.asyncio
async def test_generate_file_audio_carries_the_project_and_voice_role(monkeypatch):
    """The project a Flow runs in reaches narration through generate_file."""
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file import tool as generate_file_tool

    captured: dict = {}

    async def handler(**kwargs):
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(extended_tools, "_generate_audio_handler", handler)
    await generate_file_tool._generate_file_handler(
        entity_id="ent1", user_id="user", kind="audio", prompt="Episode two.", name="episode.mp3",
        params={"purpose": "narration"}, voice_role="host", workspace_id="ws1",
        _workflow_project_id_from_context="episode2", _workflow_run_id_from_context="run1",
    )

    assert captured["voice_role"] == "host"
    # A named asset given beside params is never dropped.
    from packages.core.ai.tools.generate_file.common import _merge_params
    assert _merge_params({"workspace_asset_key": "series-key-art", "reuse_if_exists": True,
                          "reference_assets": ["hu-bayi"]}) == {
        "workspace_asset_key": "series-key-art", "reuse_if_exists": True, "reference_assets": ["hu-bayi"]}
    assert captured["_workflow_project_id_from_context"] == "episode2"
    assert captured["_workflow_run_id_from_context"] == "run1"


@pytest.mark.asyncio
async def test_projects_keep_readable_folders_at_the_workspace_home(db_session, tmp_path):
    from packages.core.ai.tools.recording_tools import _next_version
    from packages.core.services.generated_media_naming import scope_workspace_artifact_path
    from packages.core.services.workflow_project_service import unique_project_root

    async def root(name, under=None):
        return await unique_project_root(db_session, entity_id="ent1", workspace_id="ws1", name=name, under=under)

    series_root = await root("Ghost Lantern: 鬼吹灯")
    await create_workflow_project(db_session, entity_id="ent1", workspace_id="ws1", project_type="podcast_series",
                                  created_by="user1", state={"project_root": series_root})
    assert series_root == "projects/ghost-lantern-鬼吹灯"
    assert await root("ghost lantern  鬼吹灯") == "projects/ghost-lantern-鬼吹灯-2"
    assert await root("Episode 01", under=series_root) == "projects/ghost-lantern-鬼吹灯/episode-01"
    with pytest.raises(ValueError):
        await root("Escape", under="projects/../tasks")

    # Whichever run writes into a project folder, it lands at the Workspace home.
    run_archive = "Workspaces/_by_id/folder1/workflow/run1"
    assert scope_workspace_artifact_path(f"{series_root}/episode-01/v1/episode.mp3", run_archive) == (
        f"Workspaces/_by_id/folder1/{series_root}/episode-01/v1/episode.mp3")
    (tmp_path / "v1").mkdir()
    (tmp_path / "v3").mkdir()
    (tmp_path / "notes").mkdir()
    assert _next_version(tmp_path) == 4
    assert _next_version(tmp_path / "missing") == 1


@pytest.mark.asyncio
async def test_normalizing_a_pinned_voice_keeps_it_in_the_folder_it_was_made_in(project_session):
    """A series' one narrator voice needs no voice-name folder, even after loudness normalization."""
    from packages.core.ai.tools.media_tools import _audio_narrator_provenance
    from packages.core.models.document import Document
    from packages.core.services.generated_media_naming import voice_folder_applies

    profile = {"version": 1, "provider": "google", "model": TTS, "voice": "Puck", "voice_instructions": ""}
    for path, reference in (("projects/s/e01/v1/narration.mp3", {"scope": "project", "project_id": "series"}),
                            ("tasks/t1/audio/puck/take.mp3", {"scope": "task", "id": "t1"})):
        project_session.add(Document(entity_id="ent1", name=path.rsplit("/", 1)[1], file_type="mp3", source="generated",
                                     fs_path=path, metadata_={"generation": {"narration_profile": profile,
                                                                             "voice_reference": reference}}))
    await project_session.commit()

    pinned, pinned_reference = await _audio_narrator_provenance(entity_id="ent1", audio_rel_path="projects/s/e01/v1/narration.mp3")
    _, random_reference = await _audio_narrator_provenance(entity_id="ent1", audio_rel_path="tasks/t1/audio/puck/take.mp3")

    assert pinned["voice"] == "Puck" and not voice_folder_applies(pinned_reference)
    assert voice_folder_applies(random_reference) and voice_folder_applies(None)


@pytest.mark.asyncio
async def test_a_series_keeps_the_named_images_its_episodes_make_and_reference(project_session, monkeypatch, tmp_path):
    """A key visual made in episode 1 is the series' own: episode 2 reuses it and references it by name."""
    from packages.core.ai.tools import extended_tools
    from packages.core.models.workspace import Workspace

    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity: str(tmp_path))
    monkeypatch.setattr(extended_tools, "_fs_path_from_result_url", lambda url, _entity: url.removeprefix("/api/v1/fs/file/"))
    series = await create_workflow_project(
        project_session, entity_id="ent1", workspace_id="ws1", project_type="podcast_series", created_by="user1",
        state={"title": "Ghost lantern", "project_root": "projects/ghost", "references": {"images": {}}},
    )
    first, second = [await create_workflow_project(
        project_session, entity_id="ent1", workspace_id="ws1", project_type="podcast_episode", created_by="user1",
        state={"parent_project_id": series.id}) for _ in range(2)]
    project_session.add(Workspace(id="ws1", entity_id="ent1", name="Podcast", settings={"reusable_media_assets": {
        "brand-logo": {"version": 1, "kind": "image", "result_url": "/api/v1/fs/file/logo.png", "fs_path": "logo.png"}}}))
    await project_session.commit()
    (tmp_path / "logo.png").write_bytes(b"png")
    (tmp_path / "projects/ghost/references").mkdir(parents=True)
    (tmp_path / "projects/ghost/references/series-key-art.png").write_bytes(b"png")

    holder = await extended_tools._project_image_holder(
        entity_id="ent1", workspace_id="ws1", project_id=first.id, asset_key="series-key-art")
    assert holder == {"project_id": series.id, "requested_project_id": first.id, "project_root": "projects/ghost"}
    await extended_tools._remember_project_image(
        entity_id="ent1", workspace_id="ws1", holder=holder, asset_key="series-key-art",
        image_url="/api/v1/fs/file/projects/ghost/references/series-key-art.png", prompt="lantern", model="m", size="1:1")

    urls, receipts = await extended_tools._reference_asset_urls(
        entity_id="ent1", workspace_id="ws1", project_id=second.id, keys=["series-key-art", "brand-logo"])
    assert urls == ["/api/v1/fs/file/projects/ghost/references/series-key-art.png", "/api/v1/fs/file/logo.png"]
    assert receipts[0] == {"key": "series-key-art", "scope": "project", "project_id": series.id, "inherited": True}
    assert receipts[1] == {"key": "brand-logo", "scope": "workspace", "id": "ws1"}
    with pytest.raises(extended_tools._UnknownReferenceAssets, match="brand-logo, series-key-art"):
        await extended_tools._reference_asset_urls(
            entity_id="ent1", workspace_id="ws1", project_id=second.id, keys=["hero"])
    # Outside a project that holds images, a name stays with the Workspace, as before.
    assert await extended_tools._project_image_holder(
        entity_id="ent1", workspace_id="ws1", project_id="", asset_key="series-key-art") is None


@pytest.mark.asyncio
async def test_an_episode_reuses_the_series_key_visual_without_making_it_again(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def actor(*_args):
        return "user"

    async def no_profile(**_kwargs):
        return {}

    async def holder(**_kwargs):
        return {"project_id": "series", "requested_project_id": "episode2", "project_root": "projects/ghost"}

    async def assets(**kwargs):
        assert kwargs["project_id"] == "episode2"
        return {"series-key-art": ({"result_url": "/api/v1/fs/file/projects/ghost/references/series-key-art.png",
                                    "fs_path": "projects/ghost/references/series-key-art.png", "prompt": "lantern"},
                                   {"scope": "project", "project_id": "series", "inherited": True})}

    async def paid(*_args, **_kwargs):
        raise AssertionError("a kept key visual is not made again")

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_workspace_stickman_studio_profile", no_profile)
    monkeypatch.setattr(extended_tools, "_project_image_holder", holder)
    monkeypatch.setattr(extended_tools, "_project_named_images", assets)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", paid)

    result = json.loads(await extended_tools._generate_image_handler(
        entity_id="ent1", user_id="user", prompt="Series key visual", workspace_id="ws1",
        workspace_asset_key="series-key-art", reuse_if_exists=True, _workflow_project_id_from_context="episode2",
    ))

    assert result["reused_workspace_asset"] is True
    assert result["asset_reference"] == {"scope": "project", "project_id": "series", "inherited": True}


@pytest.mark.asyncio
async def test_a_clip_may_use_the_series_assets_it_names(monkeypatch):
    """Named assets are the project's own files: the 'not selected by the user' rule never drops them."""
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    class Started(Exception):
        pass

    async def actor(*_args):
        return "user"

    async def resolved(**kwargs):
        captured["keys"] = kwargs["keys"]
        return ["/api/v1/fs/file/projects/ghost/references/hu-bayi.png"], [{"key": "hu-bayi", "scope": "project"}]

    original_filter = extended_tools._filter_unmentioned_visual_references

    def filtered(**kwargs):
        captured["allowed"] = set(kwargs["allowed_reference_urls"])
        return original_filter(**kwargs)

    async def model(*_args, **_kwargs):
        raise Started

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_reference_asset_urls", resolved)
    monkeypatch.setattr(extended_tools, "_filter_unmentioned_visual_references", filtered)
    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", model)

    with pytest.raises(Started):
        await extended_tools._generate_video_handler(
            entity_id="ent1", user_id="user", prompt="Hu Bayi lifts the lantern", workspace_id="ws1",
            reference_assets=["hu-bayi"], _active_user_message_from_context="Make episode 2's opening shot",
            _workflow_project_id_from_context="episode2",
        )

    assert captured["keys"] == ["hu-bayi"]
    assert "/api/v1/fs/file/projects/ghost/references/hu-bayi.png" in captured["allowed"]



@pytest.mark.asyncio
async def test_a_component_writing_into_a_project_version_writes_there(monkeypatch, tmp_path):
    from packages.core.ai.tools import recording_tools

    async def base(**_kwargs):
        return "Workspaces/_by_id/f1/workflow/run1"

    monkeypatch.setattr(recording_tools.media, "_workspace_media_base_dir", base)
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity: str(tmp_path))
    (tmp_path / "Workspaces/_by_id/f1/projects/s/e01/v1").mkdir(parents=True)

    _, new_version = await recording_tools._scope("ent1", "ws1", None, "projects/s/e01")
    _, same_version = await recording_tools._scope("ent1", "ws1", None, "Workspaces/_by_id/f1/projects/s/e01/v1")

    assert new_version == "Workspaces/_by_id/f1/projects/s/e01/v2"
    assert same_version == "Workspaces/_by_id/f1/projects/s/e01/v1"
