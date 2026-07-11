import json
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import media_tools


def test_media_tools_registered():
    names = [schema["function"]["name"] for schema, _handler in media_tools.get_tools()]
    assert names == [
        "wait_media_jobs",
        "merge_videos",
        "align_subtitles",
        "normalize_audio_loudness",
        "compose_video_timeline",
    ]


def test_wait_media_jobs_default_poll_interval_is_responsive():
    props = media_tools.WAIT_MEDIA_JOBS_SCHEMA["function"]["parameters"]["properties"]
    prop = props["poll_interval_seconds"]
    assert media_tools.DEFAULT_POLL_INTERVAL_SECONDS == 5.0
    assert prop["default"] == 5
    assert "default" not in props["timeout_seconds"]


def test_wait_media_jobs_default_timeout_is_adaptive():
    jobs = [
        SimpleNamespace(params={"duration": 6, "resolution": "720p"}, duration_seconds=6),
        SimpleNamespace(params={"duration": 15, "resolution": "1080p"}, duration_seconds=15),
    ]

    assert media_tools._default_wait_timeout_seconds(jobs) == media_tools.MAX_WAIT_SECONDS


def test_merge_videos_defaults_to_discarding_source_audio():
    props = media_tools.MERGE_VIDEOS_SCHEMA["function"]["parameters"]["properties"]

    assert props["include_source_audio"]["default"] is False
    assert "provider" in props["include_source_audio"]["description"]
    assert "video_paths" in props


@pytest.mark.asyncio
async def test_normalize_clip_replaces_provider_audio_with_silence_by_default(monkeypatch):
    captured: dict = {}

    async def fake_run_process(args, *, timeout_seconds):
        captured["args"] = args
        captured["timeout_seconds"] = timeout_seconds
        return "", ""

    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)

    await media_tools._normalize_clip(
        ffmpeg="/usr/bin/ffmpeg",
        input_path="/tmp/source.mp4",
        output_path="/tmp/out.mp4",
        width=1920,
        height=1080,
        fps=30,
        crf=18,
        preset="veryfast",
        duration_seconds=5.0,
        has_audio=True,
        include_source_audio=False,
    )

    args = captured["args"]
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in args
    assert args[args.index("-map") + 1] == "0:v:0"
    assert "0:a:0" not in args


@pytest.mark.asyncio
async def test_normalize_clip_can_preserve_source_audio_when_requested(monkeypatch):
    captured: dict = {}

    async def fake_run_process(args, *, timeout_seconds):
        captured["args"] = args
        captured["timeout_seconds"] = timeout_seconds
        return "", ""

    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)

    await media_tools._normalize_clip(
        ffmpeg="/usr/bin/ffmpeg",
        input_path="/tmp/source.mp4",
        output_path="/tmp/out.mp4",
        width=1920,
        height=1080,
        fps=30,
        crf=18,
        preset="veryfast",
        duration_seconds=5.0,
        has_audio=True,
        include_source_audio=True,
    )

    args = captured["args"]
    assert "0:a:0" in args
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" not in args


def test_target_dimensions_are_even_and_ratio_aware():
    assert media_tools._target_dimensions("1080p", "16:9") == (1920, 1080)
    assert media_tools._target_dimensions("1080p", "9:16") == (1080, 1920)
    assert media_tools._target_dimensions("720p", "1:1") == (720, 720)
    assert media_tools._target_dimensions("480p", "4:3") == (640, 480)


def test_path_reference_parses_fs_urls():
    assert (
        media_tools._rel_path_from_reference(
            "/api/v1/fs/entity123/打工猫AI漫剧/clips/scene-01.mp4",
            "entity123",
        )
        == "打工猫AI漫剧/clips/scene-01.mp4"
    )


def test_video_document_detection_uses_mime_type_file_type_or_path():
    assert media_tools._is_video_document(SimpleNamespace(mime_type="video/mp4", file_type="", fs_path=""))
    assert media_tools._is_video_document(SimpleNamespace(mime_type="", file_type="webm", fs_path=""))
    assert media_tools._is_video_document(SimpleNamespace(mime_type="", file_type="", fs_path="clips/a.mov"))
    assert not media_tools._is_video_document(SimpleNamespace(mime_type="image/png", file_type="png", fs_path="a.png"))


def test_merge_videos_rejects_still_image_inputs_by_extension():
    assert media_tools._assert_video_path("/tmp/title.mp4") is None
    with pytest.raises(ValueError, match="Unsupported video extension"):
        media_tools._assert_video_path("/tmp/标题卡.png")
    assert media_tools._assert_image_path("/tmp/标题卡.png") is None


def test_timeline_music_and_ambience_require_explicit_end():
    with pytest.raises(ValueError, match="end is required for music"):
        media_tools._timeline_track_end(
            {"type": "music", "start": 1.0},
            1.0,
            4.0,
            1,
        )
    with pytest.raises(ValueError, match="end is required for ambience"):
        media_tools._timeline_track_end(
            {"type": "ambience", "start": 1.0},
            1.0,
            4.0,
            2,
        )


def test_timeline_short_effect_can_derive_end_from_probe_duration():
    assert (
        media_tools._timeline_track_end(
            {"type": "sfx", "start": 2.0, "end_source": "probe_duration"},
            2.0,
            0.75,
            1,
        )
        == 2.75
    )


def test_timeline_short_effect_requires_end_or_explicit_probe_duration():
    with pytest.raises(ValueError, match="end or end_source"):
        media_tools._timeline_track_end(
            {"type": "sfx", "start": 2.0},
            2.0,
            0.75,
            1,
        )


def test_subtitle_style_is_rendered_for_ffmpeg_filter():
    value = media_tools._subtitles_filter_value(
        "/tmp/final.srt",
        {
            "font_name": "Inter",
            "font_size": 44,
            "primary_color": "#FFEEDD",
            "outline_color": "#101820",
            "alignment": 2,
            "margin_v": 72,
            "bold": True,
        },
    )

    assert "force_style=" in value
    assert "Fontname=Inter" in value
    assert "Fontsize=44" in value
    assert "PrimaryColour=&H00DDEEFF" in value
    assert "OutlineColour=&H00201810" in value
    assert "Bold=-1" in value


def test_render_subtitles_outputs_srt_and_ass():
    cues = [
        media_tools.SubtitleCue(
            index=1,
            start=1.2,
            end=3.45,
            text="Hello from the aligned subtitle system",
            cue_type="dialogue",
        )
    ]

    srt = media_tools._render_subtitles(cues, subtitle_format="srt", max_chars_per_line=18, style={})
    ass = media_tools._render_subtitles(
        cues,
        subtitle_format="ass",
        max_chars_per_line=18,
        style={"font_name": "Inter", "font_size": 40},
    )

    assert "00:00:01,200 --> 00:00:03,450" in srt
    assert "Hello from the" in srt
    assert "[V4+ Styles]" in ass
    assert "Style: Default,Inter,40" in ass
    assert r"Hello from the\Naligned subtitle" in ass


def test_ducking_filter_targets_music_under_dialogue():
    music = media_tools.TimelineAudioTrack(
        track_id="m1",
        track_type="music",
        rel_path="audio/music.mp3",
        abs_path="/tmp/music.mp3",
        start=0.0,
        end=12.0,
        volume_db=-18,
        loop=True,
        fade_in=0,
        fade_out=0,
        duration=12.0,
    )
    dialogue = media_tools.TimelineAudioTrack(
        track_id="d1",
        track_type="dialogue",
        rel_path="audio/dialogue.mp3",
        abs_path="/tmp/dialogue.mp3",
        start=4.0,
        end=6.0,
        volume_db=0,
        loop=False,
        fade_in=0,
        fade_out=0,
        duration=2.0,
    )
    config = media_tools._timeline_ducking_config(
        {"mix": {"ducking": {"enabled": True, "amount_db": -12, "padding": 0.25}}},
        None,
    )

    intervals = media_tools._ducking_intervals([music, dialogue], config)
    duck_filter = media_tools._ducking_volume_filter(music, intervals, config)

    assert intervals == [(4.0, 6.0)]
    assert "between(t\\,3.750\\,6.250)" in duck_filter
    assert "0.251189" in duck_filter
    assert media_tools._ducking_volume_filter(dialogue, intervals, config) == ""


def test_loudnorm_filter_uses_broadcast_defaults_and_clamps():
    config = media_tools._timeline_loudness_config(
        {"mix": {"loudness_normalization": {"enabled": True, "target_lufs": -99, "true_peak": 4}}},
        None,
    )

    assert config["target_lufs"] == -24.0
    assert config["true_peak"] == 0.0
    assert media_tools._loudnorm_filter(config) == "loudnorm=I=-24.0:TP=0.0:LRA=11.0:print_format=summary"


@pytest.mark.asyncio
async def test_compose_video_filter_attaches_audio_input_without_empty_filter(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_run_process(args, *, timeout_seconds):
        captured["args"] = args
        captured["timeout_seconds"] = timeout_seconds
        return "", ""

    async def fake_workspace_base_dir(**_kwargs):
        return ""

    from packages.core.services.generated_media_naming import GeneratedMediaTarget

    def fake_target(**_kwargs):
        return GeneratedMediaTarget(
            filename="mixed.mp4",
            rel_dir="final",
            rel_path="final/mixed.mp4",
            abs_dir=str(tmp_path / "final"),
            abs_path=str(tmp_path / "final" / "mixed.mp4"),
        )

    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        fake_workspace_base_dir,
    )
    monkeypatch.setattr("packages.core.services.generated_media_naming.build_generated_media_target", fake_target)

    track = media_tools.TimelineAudioTrack(
        track_id="n1",
        track_type="narration",
        rel_path="audio/narration.wav",
        abs_path=str(tmp_path / "audio" / "narration.wav"),
        start=1.0,
        end=3.0,
        volume_db=0,
        loop=False,
        fade_in=0,
        fade_out=0,
        duration=2.0,
    )

    await media_tools._compose_video_file(
        ffmpeg="/usr/bin/ffmpeg",
        entity_id="entity123",
        output_name="final/mixed.mp4",
        workspace_id=None,
        clean_video_abs=str(tmp_path / "clean.mp4"),
        subtitle_abs="",
        subtitle_style={},
        audio_tracks=[track],
        include_source_audio=False,
        ducking_config={"enabled": False},
        loudness_config={"enabled": False},
        crf=18,
        preset="veryfast",
        total_duration=5.0,
    )

    filter_complex = captured["args"][captured["args"].index("-filter_complex") + 1]
    assert "[1:a:0]aresample=48000" in filter_complex
    assert "[1:a:0],aresample" not in filter_complex


@pytest.mark.asyncio
async def test_timeline_tracks_alias_is_treated_as_audio_tracks(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "ambience.wav").write_bytes(b"fake")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 5.0}

    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)

    tracks = await media_tools._resolve_timeline_audio_tracks(
        ffprobe="/usr/bin/ffprobe",
        entity_root=str(tmp_path),
        entity_id="entity123",
        timeline={
            "tracks": [
                {
                    "id": "visual-01",
                    "type": "video",
                    "path": "clips/clip-01.mp4",
                    "start": 0,
                    "end": 5,
                },
                {
                    "id": "amb-01",
                    "type": "ambience",
                    "path": "audio/ambience.wav",
                    "start": 0,
                    "end": 5,
                    "volume_db": -18,
                },
            ]
        },
        enabled=True,
    )

    assert len(tracks) == 1
    assert tracks[0].track_id == "amb-01"
    assert tracks[0].rel_path == "audio/ambience.wav"
    assert tracks[0].volume_db == -18


@pytest.mark.asyncio
async def test_compose_video_timeline_registers_include_source_audio(tmp_path, monkeypatch):
    captured: dict = {}
    clean = tmp_path / "clean.mp4"
    clean.write_bytes(b"clean")
    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "delivery": {"clean_picture_master": "clean.mp4"},
                "spec": {"resolution": "1080p", "aspect_ratio": "16:9", "fps": 30},
                "audio_tracks": [],
            }
        ),
        encoding="utf-8",
    )
    final = tmp_path / "final" / "mixed.mp4"

    async def fake_probe_media(_ffprobe, _path):
        return {"duration_seconds": 5.0, "has_audio": True}

    async def fake_compose_video_file(**_kwargs):
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"mixed")
        return str(final), "final/mixed.mp4", "mixed.mp4"

    async def fake_register_merged_video(**kwargs):
        captured.update(kwargs)
        return "doc_123"

    async def fake_create_video_editor_recipe_sidecar(**kwargs):
        captured["sidecar_final_document_id"] = kwargs["final_document_id"]
        captured["sidecar_final_rel_path"] = kwargs["final_rel_path"]
        return {
            "document_id": "recipe_123",
            "fs_path": "final/mixed.video-edit.json",
            "kind": "manor.video_edit_recipe",
        }

    async def fake_attach_video_editor_recipe_to_video(**kwargs):
        captured["attached_final_document_id"] = kwargs["final_document_id"]
        captured["attached_recipe_document_id"] = kwargs["editor_recipe"]["document_id"]

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe_media)
    monkeypatch.setattr(media_tools, "_compose_video_file", fake_compose_video_file)
    monkeypatch.setattr(media_tools, "_register_merged_video", fake_register_merged_video)
    monkeypatch.setattr(media_tools, "_create_video_editor_recipe_sidecar", fake_create_video_editor_recipe_sidecar)
    monkeypatch.setattr(media_tools, "_attach_video_editor_recipe_to_video", fake_attach_video_editor_recipe_to_video)

    result = json.loads(
        await media_tools._compose_video_timeline_handler(
            entity_id="entity123",
            user_id="user123",
            timeline_path="timeline.json",
            output_name="final/mixed.mp4",
            include_source_audio=True,
        )
    )

    assert result["status"] == "completed"
    assert result["include_source_audio"] is True
    assert captured["include_source_audio"] is True
    assert captured["operation"] == "compose_video_timeline"
    assert result["editor_recipe_document_id"] == "recipe_123"
    assert result["editor_recipe_path"] == "final/mixed.video-edit.json"
    assert captured["sidecar_final_document_id"] == "doc_123"
    assert captured["sidecar_final_rel_path"] == "final/mixed.mp4"
    assert captured["attached_final_document_id"] == "doc_123"
    assert captured["attached_recipe_document_id"] == "recipe_123"


def test_video_editor_recipe_payload_preserves_ai_composition_layers():
    payload = media_tools._build_video_editor_recipe_payload(
        entity_id="entity123",
        timeline={
            "duration": 8,
            "spec": {"resolution": "1080p", "aspect_ratio": "9:16"},
            "clips": [
                {
                    "id": "shot-01",
                    "title": "Hero reveal",
                    "path": "project/clips/shot-01.mp4",
                    "start": 0,
                    "end": 3.5,
                    "camera": "slow push-in",
                    "action": "The desktop robot wakes and turns toward camera.",
                    "dialogue": "Good morning.",
                    "video_prompt": "Approved reference-driven motion prompt.",
                },
                {
                    "id": "shot-02",
                    "title": "Product orbit",
                    "path": "project/clips/shot-02.mp4",
                    "start": 3.5,
                    "end": 8,
                    "camera": "macro orbit",
                    "action": "The robot follows the user's hand gesture.",
                },
            ],
            "audio_tracks": [
                {
                    "id": "dlg-001",
                    "type": "dialogue",
                    "path": "project/audio/dialogue/dlg-001.wav",
                    "start": 1,
                    "end": 2.2,
                    "character": "Robot",
                    "text": "Good morning.",
                    "voice_direction": "warm, concise",
                    "volume_db": -4,
                },
                {
                    "id": "amb-001",
                    "type": "ambience",
                    "path": "project/audio/ambience/desk-room.wav",
                    "start": 0,
                    "end": 8,
                    "prompt": "quiet premium desk room tone",
                    "volume_db": -22,
                    "loop": True,
                },
            ],
            "subtitle_cues": [
                {
                    "id": "dlg-001",
                    "type": "dialogue",
                    "speaker": "Robot",
                    "text": "Good morning.",
                    "start": 1,
                    "end": 2.2,
                }
            ],
        },
        timeline_path="project/06-Timeline.json",
        clean_video_path="project/final/clean-picture-master.mp4",
        final_document_id="final_doc",
        final_filename="final-subtitled.mp4",
        final_rel_path="project/final/final-subtitled.mp4",
        final_file_size=1234,
        total_duration=8,
        media_info={"width": 1080, "height": 1920, "has_audio": True},
        audio_tracks=[
            media_tools.TimelineAudioTrack(
                track_id="dlg-001",
                track_type="dialogue",
                rel_path="project/audio/dialogue/dlg-001.wav",
                abs_path="/tmp/dlg-001.wav",
                start=1,
                end=2.2,
                volume_db=-4,
                loop=False,
                fade_in=0,
                fade_out=0,
                duration=1.2,
            ),
            media_tools.TimelineAudioTrack(
                track_id="amb-001",
                track_type="ambience",
                rel_path="project/audio/ambience/desk-room.wav",
                abs_path="/tmp/desk-room.wav",
                start=0,
                end=8,
                volume_db=-22,
                loop=True,
                fade_in=0.5,
                fade_out=0.5,
                duration=8,
            ),
        ],
        subtitle_path="project/subtitles/final.srt",
        subtitle_abs_path="",
        documents_by_path={
            "project/clips/shot-01.mp4": {
                "id": "clip_doc_1",
                "name": "shot-01.mp4",
                "mime_type": "video/mp4",
            },
            "project/clips/shot-02.mp4": {
                "id": "clip_doc_2",
                "name": "shot-02.mp4",
                "mime_type": "video/mp4",
            },
            "project/audio/dialogue/dlg-001.wav": {
                "id": "audio_doc_1",
                "name": "dlg-001.wav",
                "mime_type": "audio/wav",
            },
            "project/audio/ambience/desk-room.wav": {
                "id": "audio_doc_2",
                "name": "desk-room.wav",
                "mime_type": "audio/wav",
            },
        },
        crf=18,
        preset="veryfast",
        include_source_audio=False,
    )

    assert payload["kind"] == "manor.video_edit_recipe"
    assert payload["source_document"]["id"] == "final_doc"
    assert payload["source_document"]["fs_path"] == "project/final/final-subtitled.mp4"
    assert payload["canvas"] == {"width": 1080, "height": 1920}
    assert payload["timeline"]["duration"] == 8
    assert payload["timeline"]["clips"][0]["assetDocumentId"] == "clip_doc_1"
    assert payload["timeline"]["clips"][0]["sourceEnd"] == 3.5
    assert payload["timeline"]["shots"][0]["camera"] == "slow push-in"
    assert payload["timeline"]["captions"][0]["text"] == "Good morning."
    assert payload["timeline"]["audio_cues"][0]["assetDocumentId"] == "audio_doc_1"
    assert payload["timeline"]["audio_cues"][1]["type"] == "ambience"
    assert payload["ai_composition"]["clip_count"] == 2
    assert "source_timeline" in payload


@pytest.mark.asyncio
async def test_collect_subtitle_cues_derives_end_from_audio_duration(tmp_path, monkeypatch):
    audio = tmp_path / "line-01.mp3"
    audio.write_bytes(b"fake")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 2.4}

    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)

    cues = await media_tools._collect_subtitle_cues(
        ffprobe="/usr/bin/ffprobe",
        entity_root=str(tmp_path),
        entity_id="entity123",
        timeline={
            "audio_tracks": [
                {
                    "type": "dialogue",
                    "path": "line-01.mp3",
                    "start": 5,
                    "text": "Derived from audio duration",
                }
            ]
        },
        cue_payloads=[],
        track_types={"dialogue"},
    )

    assert len(cues) == 1
    assert cues[0].start == 5
    assert cues[0].end == 7.4
    assert cues[0].estimated is False


@pytest.mark.asyncio
async def test_wait_media_jobs_requires_job_ids():
    payload = json.loads(await media_tools._wait_media_jobs_handler(entity_id="entity123"))
    assert payload["status"] == "error"
    assert "job_ids" in payload["error"]


@pytest.mark.asyncio
async def test_wait_media_jobs_timeout_is_recoverable_pending(monkeypatch):
    job = SimpleNamespace(
        id="job_pending",
        kind="video",
        status="processing",
        params={"duration": 6, "resolution": "720p"},
        duration_seconds=6,
    )

    async def fake_load_media_jobs(_entity_id, _ids):
        return [job], []

    async def fake_jobs_to_payload(_entity_id, _jobs):
        return [
            {
                "job_id": "job_pending",
                "kind": "video",
                "status": "processing",
            }
        ]

    monkeypatch.setattr(media_tools, "_load_media_jobs", fake_load_media_jobs)
    monkeypatch.setattr(media_tools, "_jobs_to_payload", fake_jobs_to_payload)

    payload = json.loads(
        await media_tools._wait_media_jobs_handler(
            entity_id="entity123",
            job_ids=["job_pending"],
            timeout_seconds=0,
        )
    )

    assert payload["status"] == "pending"
    assert payload["timed_out"] is True
    assert payload["pending_job_ids"] == ["job_pending"]


@pytest.mark.asyncio
async def test_wait_media_jobs_missing_id_does_not_fail_active_jobs(monkeypatch):
    job = SimpleNamespace(
        id="job_processing",
        kind="video",
        status="processing",
        params={"duration": 6, "resolution": "720p"},
        duration_seconds=6,
    )

    async def fake_load_media_jobs(_entity_id, _ids):
        return [job], ["job_missing"]

    async def fake_jobs_to_payload(_entity_id, _jobs):
        return [
            {
                "job_id": "job_processing",
                "kind": "video",
                "status": "processing",
            }
        ]

    monkeypatch.setattr(media_tools, "_load_media_jobs", fake_load_media_jobs)
    monkeypatch.setattr(media_tools, "_jobs_to_payload", fake_jobs_to_payload)

    payload = json.loads(
        await media_tools._wait_media_jobs_handler(
            entity_id="entity123",
            job_ids=["job_processing", "job_missing"],
            timeout_seconds=0,
        )
    )

    assert payload["status"] == "pending"
    assert payload["missing_job_ids"] == ["job_missing"]
    assert payload["pending_job_ids"] == ["job_processing"]


@pytest.mark.asyncio
async def test_wait_media_jobs_missing_id_does_not_hide_completed_jobs(monkeypatch):
    job = SimpleNamespace(
        id="job_completed",
        kind="video",
        status="completed",
        params={"duration": 6, "resolution": "720p"},
        duration_seconds=6,
    )

    async def fake_load_media_jobs(_entity_id, _ids):
        return [job], ["job_missing"]

    async def fake_jobs_to_payload(_entity_id, _jobs):
        return [
            {
                "job_id": "job_completed",
                "kind": "video",
                "status": "completed",
            }
        ]

    monkeypatch.setattr(media_tools, "_load_media_jobs", fake_load_media_jobs)
    monkeypatch.setattr(media_tools, "_jobs_to_payload", fake_jobs_to_payload)

    payload = json.loads(
        await media_tools._wait_media_jobs_handler(
            entity_id="entity123",
            job_ids=["job_completed", "job_missing"],
        )
    )

    assert payload["status"] == "completed"
    assert payload["missing_job_ids"] == ["job_missing"]
    assert payload["completed_count"] == 1
    assert payload["total_count"] == 2


@pytest.mark.asyncio
async def test_merge_videos_requires_ffmpeg(monkeypatch):
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    payload = json.loads(
        await media_tools._merge_videos_handler(
            entity_id="entity123",
            paths=["project/clips/scene-01.mp4", "project/clips/scene-02.mp4"],
            output_name="project/final/full.mp4",
        )
    )

    assert payload["status"] == "error"
    assert payload["code"] == "ffmpeg_missing"


@pytest.mark.asyncio
async def test_merge_videos_accepts_single_clip_for_clean_master(tmp_path, monkeypatch):
    captured: dict = {}
    output = tmp_path / "final" / "clean-picture-master.mp4"

    async def fake_resolve_video_inputs(**kwargs):
        captured["resolve_paths"] = kwargs["paths"]
        return [
            media_tools.VideoInput(
                source_type="path",
                source_id=None,
                rel_path="project/clips/shot-01.mp4",
                abs_path=str(tmp_path / "shot-01.mp4"),
            )
        ]

    async def fake_merge_video_files(**kwargs):
        captured["input_count"] = len(kwargs["inputs"])
        captured["include_source_audio"] = kwargs["include_source_audio"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"clean")
        return (
            str(output),
            "project/final/clean-picture-master.mp4",
            "clean-picture-master.mp4",
            [
                {
                    "fs_path": "project/clips/shot-01.mp4",
                    "duration_seconds": 5.0,
                    "has_audio": True,
                    "source_audio_used": False,
                }
            ],
            5.0,
        )

    async def fake_register_merged_video(**kwargs):
        captured["registered_inputs"] = kwargs["inputs"]
        return "doc_clean"

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_resolve_video_inputs", fake_resolve_video_inputs)
    monkeypatch.setattr(media_tools, "_merge_video_files", fake_merge_video_files)
    monkeypatch.setattr(media_tools, "_register_merged_video", fake_register_merged_video)

    payload = json.loads(
        await media_tools._merge_videos_handler(
            entity_id="entity123",
            user_id="user123",
            video_paths=["project/clips/shot-01.mp4"],
            output_name="project/final/clean-picture-master.mp4",
            include_source_audio=False,
        )
    )

    assert payload["status"] == "completed"
    assert payload["document_id"] == "doc_clean"
    assert payload["fs_path"] == "project/final/clean-picture-master.mp4"
    assert payload["source_audio_stripped"] is True
    assert captured["resolve_paths"] == ["project/clips/shot-01.mp4"]
    assert captured["input_count"] == 1
    assert captured["include_source_audio"] is False
