"""Music falls back to Manor's own library when live generation fails."""
import json

import pytest

from packages.core.services import music_library


def _library(tmp_path, tracks):
    for track in tracks:
        (tmp_path / track["file"]).write_bytes(b"ID3music")
    (tmp_path / "catalog.json").write_text(json.dumps({
        "license": "Manor-generated; free to use in Manor outputs.",
        "tracks": tracks,
    }))
    return tmp_path


TRACKS = [
    {"id": "calm-piano", "file": "calm.mp3", "moods": ["calm", "warm", "minimal"], "duration_seconds": 60, "loopable": True},
    {"id": "upbeat-tech", "file": "tech.mp3", "moods": ["upbeat", "tech", "energetic"], "duration_seconds": 60, "loopable": True},
    {"id": "cinematic", "file": "cine.mp3", "moods": ["cinematic", "inspiring"], "duration_seconds": 30, "loopable": False},
]


def test_the_track_matching_the_prompt_mood_is_chosen(tmp_path):
    root = _library(tmp_path, TRACKS)
    assert music_library.pick_library_track("Upbeat tech pulse for a launch", 20, root).id == "upbeat-tech"
    assert music_library.pick_library_track("calm warm piano bed", 20, root).id == "calm-piano"
    # No mood words: a deterministic default (first track that fits).
    assert music_library.pick_library_track("something", 20, root).id == "calm-piano"
    # A track too short and not loopable loses a tie to one that fits.
    assert music_library.pick_library_track("inspiring", 90, root).id == "cinematic"


def test_tracks_without_their_audio_file_are_ignored(tmp_path):
    root = _library(tmp_path, TRACKS)
    (root / "tech.mp3").unlink()
    assert [track.id for track in music_library.library_tracks(root)] == ["calm-piano", "cinematic"]
    assert music_library.pick_library_track("prompt", root=tmp_path / "missing") is None


@pytest.mark.asyncio
async def test_music_uses_the_library_when_live_generation_fails(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(music_library, "LIBRARY_ROOT", _library(tmp_path, TRACKS))

    async def live_fails(**_kwargs):
        return json.dumps({"status": "error", "code": "native_music_key_required", "error": "needs a key"})

    saved = {}

    async def save(**kwargs):
        saved.update(kwargs)
        return "/api/v1/fs/entity/video/music.mp3"

    async def actor(*_args):
        return "user"

    monkeypatch.setattr(extended_tools, "_generate_live_audio", live_fails)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", save)
    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)

    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="entity", user_id="user", prompt="upbeat tech bed", purpose="music",
        duration_seconds=20, name="video/music.mp3",
    ))

    assert result["status"] == "completed" and result["source"] == "library"
    assert result["library_track_id"] == "upbeat-tech"
    assert result["result_url"] == "/api/v1/fs/entity/video/music.mp3"
    assert result["live_generation_error"] == "native_music_key_required"
    assert saved["output_name"] == "video/music.mp3" and saved["bill_media"] is False


@pytest.mark.asyncio
async def test_live_results_and_other_audio_pass_through(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(music_library, "LIBRARY_ROOT", _library(tmp_path, TRACKS))
    replies = {}

    async def live(**kwargs):
        return replies[kwargs["purpose"]]

    async def no_save(**_kwargs):
        raise AssertionError("the library must not be used")

    monkeypatch.setattr(extended_tools, "_generate_live_audio", live)
    monkeypatch.setattr(extended_tools, "_save_generated_audio_bytes", no_save)

    replies["music"] = json.dumps({"status": "completed", "result_url": "/live.mp3"})
    replies["narration"] = json.dumps({"status": "error", "code": "provider_down"})
    assert json.loads(await extended_tools._generate_audio_handler(
        entity_id="e", user_id="u", prompt="calm", purpose="music"))["result_url"] == "/live.mp3"
    # Narration has no library: its failure is reported as is.
    assert json.loads(await extended_tools._generate_audio_handler(
        entity_id="e", user_id="u", prompt="Hello", purpose="narration"))["code"] == "provider_down"


@pytest.mark.asyncio
async def test_without_a_library_the_live_failure_is_reported(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(music_library, "LIBRARY_ROOT", tmp_path / "empty")

    async def live_fails(**_kwargs):
        return json.dumps({"status": "error", "code": "native_music_key_required"})

    monkeypatch.setattr(extended_tools, "_generate_live_audio", live_fails)
    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="e", user_id="u", prompt="calm", purpose="music"))
    # Music that cannot be made is skipped, never an error that stops a video.
    assert result["status"] == "skipped" and result["optional"] is True
    assert result["skip_code"] == "native_music_key_required"
    assert "continue without music" in result["reason"] and "result_url" not in result


@pytest.mark.asyncio
async def test_a_bad_music_request_is_still_an_error(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(music_library, "LIBRARY_ROOT", tmp_path / "empty")

    async def invalid(**_kwargs):
        return json.dumps({"status": "error", "code": "invalid_request", "error": "prompt is required"})

    monkeypatch.setattr(extended_tools, "_generate_live_audio", invalid)
    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="e", user_id="u", prompt="", purpose="music"))
    assert result["status"] == "error" and result["code"] == "invalid_request"


def test_prompts_in_chinese_or_synonyms_find_the_mood(tmp_path):
    root = _library(tmp_path, TRACKS)
    assert music_library.prompt_moods("轻快的科技感背景音乐") == {"upbeat", "tech"}
    assert music_library.prompt_moods("A chill, cozy bed") == {"calm", "warm"}
    # "fun" must not match inside another word such as "funnel".
    assert "upbeat" not in music_library.prompt_moods("a sales funnel explainer")
    assert music_library.pick_library_track("电影感 史诗", 30, root).id == "cinematic"


SHIPPED = music_library.LIBRARY_ROOT


def test_the_shipped_catalog_only_lists_real_tracks_from_the_spec():
    import hashlib

    spec = json.loads((SHIPPED / "spec.json").read_text())
    catalog = json.loads((SHIPPED / "catalog.json").read_text())
    assert catalog["license"].strip()
    spec_ids = {track["id"] for track in spec["tracks"]}
    assert len(spec_ids) == len(spec["tracks"])
    # Every spec mood is one the prompt matcher understands.
    assert {mood for track in spec["tracks"] for mood in track["moods"]} <= set(music_library.MOOD_WORDS)
    assert all("no vocals" in track["prompt"].lower() for track in spec["tracks"])
    ids = [entry["id"] for entry in catalog["tracks"]]
    assert len(ids) == len(set(ids)) and set(ids) <= spec_ids
    for entry in catalog["tracks"]:
        path = SHIPPED / entry["file"]
        assert path.is_file(), entry["id"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["id"]
        assert entry["moods"] and entry["duration_seconds"] > 0 and entry["model"] and entry["prompt"]


def _tone(seconds: float) -> bytes:
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = f"{tmp}/tone.mp3"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
             "-c:a", "libmp3lame", out],
            check=True,
        )
        return open(out, "rb").read()


@pytest.mark.asyncio
@pytest.mark.media_integration
@pytest.mark.oss_regression
async def test_the_build_script_makes_loopable_levelled_tracks_and_resumes(tmp_path):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to build library tracks")
    from scripts.build_music_library import build

    (tmp_path / "spec.json").write_text(json.dumps({
        "duration_seconds": 8, "crossfade_seconds": 1, "loudness_lufs": -16,
        "tracks": [
            {"id": "calm-one", "moods": ["calm"], "prompt": "calm, no vocals"},
            {"id": "short-one", "moods": ["tech"], "prompt": "tech, no vocals"},
        ],
    }))
    (tmp_path / "catalog.json").write_text(json.dumps({"license": "Manor", "tracks": []}))
    requested = []

    async def generate(prompt, seconds):
        requested.append(seconds)
        # The second track comes back too short to loop.
        return _tone(seconds if "calm" in prompt else 5)

    assert await build(generate, root=tmp_path, model="test/model") == ["calm-one", "short-one"]
    assert requested[0] >= 8 + 1  # extra length for the loop crossfade
    catalog = {entry["id"]: entry for entry in json.loads((tmp_path / "catalog.json").read_text())["tracks"]}
    assert catalog["calm-one"]["loopable"] is True and abs(catalog["calm-one"]["duration_seconds"] - 8) < 0.2
    assert catalog["short-one"]["loopable"] is False and catalog["short-one"]["duration_seconds"] <= 5.2
    assert catalog["calm-one"]["model"] == "test/model" and len(catalog["calm-one"]["sha256"]) == 64

    # A rerun keeps what is built; the library service now serves it.
    assert await build(generate, root=tmp_path) == []
    assert music_library.pick_library_track("calm", 8, tmp_path).id == "calm-one"
