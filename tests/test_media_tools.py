import json
from pathlib import Path
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
        "probe_media",
        "render_frame_samples",
        "analyze_audio",
        "validate_subtitles",
        "still_to_video",
    ]


def test_product_video_qa_tool_schemas_are_strict_and_bounded():
    schemas = {
        schema["function"]["name"]: schema["function"]["parameters"]
        for schema in (
            media_tools.PROBE_MEDIA_SCHEMA,
            media_tools.RENDER_FRAME_SAMPLES_SCHEMA,
            media_tools.ANALYZE_AUDIO_SCHEMA,
            media_tools.VALIDATE_SUBTITLES_SCHEMA,
            media_tools.STILL_TO_VIDEO_SCHEMA,
        )
    }

    for name in (
        "probe_media",
        "render_frame_samples",
        "analyze_audio",
        "validate_subtitles",
        "still_to_video",
    ):
        assert schemas[name]["additionalProperties"] is False

    assert schemas["probe_media"]["required"] == ["input_path"]
    assert schemas["render_frame_samples"]["required"] == ["input_path", "output_dir"]
    assert schemas["render_frame_samples"]["properties"]["max_samples"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 24,
        "default": 12,
    }
    assert schemas["render_frame_samples"]["properties"]["interval_seconds"]["minimum"] == 0.25
    assert schemas["analyze_audio"]["required"] == ["input_path"]
    assert schemas["validate_subtitles"]["required"] == ["subtitle_path"]
    assert schemas["still_to_video"]["required"] == ["input_path", "output_name"]
    assert schemas["still_to_video"]["properties"]["duration_seconds"]["minimum"] == 0.1
    assert schemas["still_to_video"]["properties"]["duration_seconds"]["maximum"] == 30


def test_align_subtitles_accepts_canonical_transcript_and_audio_sources():
    props = media_tools.ALIGN_SUBTITLES_SCHEMA["function"]["parameters"]["properties"]

    assert "transcript_path" in props
    assert "audio_path" in props


def test_semantic_alignment_uses_measured_english_segment_boundaries():
    sentences = media_tools._canonical_sentences(
        "Open the Workspace. Start the approved workflow!"
    )
    segments = [
        {"start": 0.2, "end": 2.7, "text": "Open the workspace"},
        {"start": 2.9, "end": 5.8, "text": "Start the approved workflow."},
    ]

    aligned, metrics = media_tools._align_sentences_to_segments(sentences, segments)

    assert sentences == ["Open the Workspace.", "Start the approved workflow!"]
    assert aligned[0].start == pytest.approx(0.2)
    assert aligned[-1].end == pytest.approx(5.8)
    assert [cue.text for cue in aligned] == sentences
    assert all(cue.estimated is False for cue in aligned)
    assert metrics["similarity"] >= 0.90
    assert metrics["coverage"] == 1.0
    assert metrics["missing_sentence_indexes"] == []


def test_semantic_alignment_segments_cjk_without_spaces_and_normalizes_punctuation():
    sentences = media_tools._canonical_sentences(
        "打开工作区。然后启动工作流！\n检查生成结果？"
    )
    segments = [
        {"start": 0.2, "end": 1.8, "text": "打开工作区"},
        {"start": 1.9, "end": 3.8, "text": "然后启动工作流!"},
        {"start": 4.0, "end": 5.8, "text": "检查生成结果?"},
    ]

    aligned, metrics = media_tools._align_sentences_to_segments(sentences, segments)

    assert sentences == ["打开工作区。", "然后启动工作流！", "检查生成结果？"]
    assert [cue.text for cue in aligned] == sentences
    assert aligned[0].start == pytest.approx(0.2)
    assert aligned[-1].end == pytest.approx(5.8)
    assert metrics["similarity"] == pytest.approx(1.0)
    assert metrics["coverage"] == 1.0


def test_semantic_alignment_rejects_low_similarity_segments():
    aligned, metrics = media_tools._align_sentences_to_segments(
        ["Open the Workspace."],
        [{"start": 0.2, "end": 1.8, "text": "Delete the account"}],
    )

    assert aligned == []
    assert metrics["similarity"] < 0.90
    assert metrics["coverage"] == 0.0
    assert metrics["missing_sentence_indexes"] == [1]


def test_semantic_alignment_reports_coverage_below_95_percent():
    sentences = [f"Canonical sentence {index}." for index in range(1, 21)]
    segments = [
        {
            "start": float(index),
            "end": float(index + 1),
            "text": f"Canonical sentence {index + 1}.",
        }
        for index in range(18)
    ]

    aligned, metrics = media_tools._align_sentences_to_segments(sentences, segments)

    assert len(aligned) == 18
    assert metrics["coverage"] == pytest.approx(0.90)
    assert metrics["coverage"] < 0.95
    assert metrics["missing_sentence_indexes"] == [19, 20]


def test_semantic_alignment_similarity_scores_unmatched_canonical_sentences_as_zero():
    sentences = [
        f"Canonical narration step {index} opens feature number {index}."
        for index in range(1, 21)
    ]
    segments = [
        {
            "start": float(index - 1),
            "end": float(index),
            "text": sentence,
        }
        for index, sentence in enumerate(sentences[:19], start=1)
    ]

    aligned, metrics = media_tools._align_sentences_to_segments(sentences, segments)

    assert len(aligned) == 19
    assert metrics["coverage"] == pytest.approx(0.95)
    assert metrics["similarity"] == pytest.approx(0.95)
    assert metrics["missing_sentence_indexes"] == [20]


def test_semantic_alignment_uses_word_timestamps_for_sentences_inside_one_segment():
    sentences = ["Open the project.", "Choose the final render."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [
            {
                "start": 0.2,
                "end": 5.8,
                "text": "Open the project Choose the final render",
            }
        ],
        words=[
            {"start": 0.3, "end": 0.7, "text": "Open"},
            {"start": 0.8, "end": 1.0, "text": "the"},
            {"start": 1.1, "end": 1.7, "text": "project"},
            {"start": 2.5, "end": 3.1, "text": "Choose"},
            {"start": 3.2, "end": 3.5, "text": "the"},
            {"start": 3.6, "end": 4.1, "text": "final"},
            {"start": 4.2, "end": 5.5, "text": "render"},
        ],
    )

    assert [cue.text for cue in cues] == sentences
    assert (cues[0].start, cues[0].end) == pytest.approx((0.3, 1.7))
    assert (cues[1].start, cues[1].end) == pytest.approx((2.5, 5.5))
    assert all(cue.measured and not cue.estimated for cue in cues)
    assert {cue.timing_source for cue in cues} == {"measured_stt_words"}
    assert metrics["similarity"] == pytest.approx(1.0)
    assert metrics["coverage"] == pytest.approx(1.0)
    assert metrics["timing_sources"] == ["measured_stt_words"]


def test_semantic_alignment_uses_cjk_word_timestamps_inside_one_segment():
    sentences = ["打开项目。", "选择最终视频！"]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 1.0, "end": 4.6, "text": "打开项目选择最终视频"}],
        words=[
            {"start": 1.1, "end": 1.5, "text": "打开"},
            {"start": 1.6, "end": 2.0, "text": "项目"},
            {"start": 2.5, "end": 2.9, "text": "选择"},
            {"start": 3.0, "end": 3.5, "text": "最终"},
            {"start": 3.6, "end": 4.4, "text": "视频"},
        ],
    )

    assert [cue.text for cue in cues] == sentences
    assert (cues[0].start, cues[0].end) == pytest.approx((1.1, 2.0))
    assert (cues[1].start, cues[1].end) == pytest.approx((2.5, 4.4))
    assert {cue.timing_source for cue in cues} == {"measured_stt_words"}
    assert metrics["similarity"] == pytest.approx(1.0)
    assert metrics["coverage"] == pytest.approx(1.0)


def test_semantic_alignment_uses_best_complete_word_timestamp_match():
    sentences = ["Open the account settings now.", "Go."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [
            {
                "start": 0.1,
                "end": 3.5,
                "text": "Open the account settings now Go",
            }
        ],
        words=[
            {"start": 0.1, "end": 0.4, "text": "Open"},
            {"start": 0.5, "end": 0.7, "text": "the"},
            {"start": 0.8, "end": 1.2, "text": "account"},
            {"start": 1.3, "end": 1.8, "text": "settings"},
            {"start": 1.9, "end": 2.2, "text": "now"},
            {"start": 3.0, "end": 3.4, "text": "Go"},
        ],
    )

    assert [cue.text for cue in cues] == sentences
    assert [(cue.start, cue.end) for cue in cues] == pytest.approx(
        [(0.1, 2.2), (3.0, 3.4)]
    )
    assert metrics["similarity"] == pytest.approx(1.0)
    assert metrics["coverage"] == pytest.approx(1.0)
    assert metrics["timing_sources"] == ["measured_stt_words"]


def test_semantic_alignment_rejects_multi_sentence_segment_without_word_timestamps():
    with pytest.raises(
        media_tools.SubtitleWordTimestampsRequiredError,
        match="word-level timestamps",
    ):
        media_tools._align_sentences_to_segments(
            ["Open the project.", "Choose the final render."],
            [
                {
                    "start": 0.2,
                    "end": 5.8,
                    "text": "Open the project Choose the final render",
                }
            ],
        )


def test_semantic_alignment_scene_coverage_uses_declared_scenes_and_clip_boundaries():
    sentences = ["First line.", "Second line."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [
            {"start": 0.2, "end": 2.4, "text": "First line."},
            {"start": 2.7, "end": 5.8, "text": "Second line."},
        ],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "scenes": [
                    {"scene_id": "scene-1", "narration_text": "First line."},
                    {"scene_id": "scene-2", "narration_text": "Second line."},
                ],
                "clips": [
                    {
                        "scene_id": "scene-1",
                        "start": 0.0,
                        "end": 2.5,
                    },
                    {
                        "scene_id": "scene-2",
                        "start": 2.5,
                        "end": 6.0,
                    },
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage == {
        "coverage": 1.0,
        "covered_scene_ids": ["scene-1", "scene-2"],
        "missing_scene_ids": [],
        "missing_interval_scene_ids": [],
        "unmapped_scene_ids": [],
    }


def test_scene_coverage_ignores_arbitrary_clip_id_without_scene_declaration():
    sentences = ["Open settings."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "clips": [
                    {
                        "id": "not-a-scene",
                        "start": 0.0,
                        "end": 1.0,
                        "narration_text": "Open settings.",
                    }
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["coverage"] == 1.0
    assert coverage["covered_scene_ids"] == []
    assert coverage["missing_scene_ids"] == []


def test_scene_coverage_supports_explicit_visual_segments_and_reports_missing_alignment():
    sentences = ["Open settings.", "Publish the video."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "segments": [
                    {
                        "scene_id": "scene-1",
                        "start": 0.0,
                        "end": 1.0,
                        "narration_text": "Open settings.",
                    },
                    {
                        "scene_id": "scene-2",
                        "start": 1.0,
                        "end": 2.0,
                        "narration_text": "Publish the video.",
                    },
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["coverage"] == pytest.approx(0.5)
    assert coverage["covered_scene_ids"] == ["scene-1"]
    assert coverage["missing_scene_ids"] == ["scene-2"]


def test_scene_coverage_supports_visual_scene_intervals_with_narration_ranges():
    sentences = ["Open settings.", "Publish the video."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [
            {"start": 0.0, "end": 1.0, "text": "Open settings."},
            {"start": 1.0, "end": 2.0, "text": "Publish the video."},
        ],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "visual_scene_intervals": [
                    {
                        "scene_id": "scene-1",
                        "visual_start_seconds": 10.0,
                        "visual_end_seconds": 11.0,
                        "narration_start_seconds": 0.0,
                        "narration_end_seconds": 1.0,
                    },
                    {
                        "scene_id": "scene-2",
                        "visual_start_seconds": 11.0,
                        "visual_end_seconds": 12.0,
                        "narration_start_seconds": 1.0,
                        "narration_end_seconds": 2.0,
                    },
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage == {
        "coverage": 1.0,
        "covered_scene_ids": ["scene-1", "scene-2"],
        "missing_scene_ids": [],
        "missing_interval_scene_ids": [],
        "unmapped_scene_ids": [],
    }


def test_scene_coverage_does_not_treat_generic_cue_id_as_scene_id():
    sentences = ["Open settings."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "cues": [
                    {
                        "id": "scene-forged",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Open settings.",
                    }
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["covered_scene_ids"] == []
    assert coverage["missing_scene_ids"] == []


@pytest.mark.parametrize(
    "segment",
    [
        {
            "id": "generic-segment",
            "start": 0.0,
            "end": 1.0,
            "narration_text": "Open settings.",
        },
        {
            "scene_id": "scene-without-end",
            "start": 0.0,
            "narration_text": "Open settings.",
        },
    ],
)
def test_scene_coverage_ignores_non_visual_or_incomplete_segment_entries(segment):
    sentences = ["Open settings."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "segments": [segment],
                "cues": [
                    {
                        "scene_id": segment.get("scene_id") or segment.get("id"),
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Open settings.",
                        "measured": True,
                    }
                ],
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["covered_scene_ids"] == []
    assert coverage["missing_scene_ids"] == []


def test_scene_coverage_requires_mapped_cue_to_intersect_declared_interval():
    sentences = ["Open settings."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "scenes": [
                    {
                        "scene_id": "scene-late",
                        "start": 100.0,
                        "end": 110.0,
                        "narration_text": "Open settings.",
                    }
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["coverage"] == 0.0
    assert coverage["covered_scene_ids"] == []
    assert coverage["missing_scene_ids"] == ["scene-late"]


def test_scene_coverage_reports_declared_scene_without_stable_interval():
    sentences = ["Open settings."]
    cues, metrics = media_tools._align_sentences_to_segments(
        sentences,
        [{"start": 0.0, "end": 1.0, "text": "Open settings."}],
    )

    coverage = media_tools._scene_alignment_coverage(
        [
            {
                "scenes": [
                    {
                        "scene_id": "scene-no-range",
                        "narration_text": "Open settings.",
                    }
                ]
            }
        ],
        sentences,
        cues,
        metrics["aligned_sentence_indexes"],
    )

    assert coverage["coverage"] == 0.0
    assert coverage["covered_scene_ids"] == []
    assert coverage["missing_scene_ids"] == ["scene-no-range"]
    assert coverage["missing_interval_scene_ids"] == ["scene-no-range"]


def test_subtitle_font_size_and_margin_defaults_follow_canvas_short_edge():
    assert media_tools._subtitle_font_size(1920, 1080) == 52
    assert media_tools._subtitle_margin_v(1920, 1080) == 72
    assert media_tools._subtitle_font_size(1280, 720) == 35
    assert media_tools._subtitle_margin_v(1280, 720) == 48


def test_subtitle_font_size_defaults_preserve_explicit_style_overrides():
    style = media_tools._subtitle_style(
        1920,
        1080,
        {"font_size": 32, "margin_v": 64, "outline": 3, "shadow": 2},
    )

    assert style == {
        "font_size": 32,
        "margin_v": 64,
        "outline": 3,
        "shadow": 2,
        "alignment": 2,
        "max_lines": 2,
    }


def test_subtitle_font_size_default_reaches_ass_renderer_with_two_line_limit():
    cue = media_tools.SubtitleCue(
        index=1,
        start=0.2,
        end=5.8,
        text=(
            "This measured sentence is deliberately long enough to require more than two "
            "wrapped subtitle lines and therefore multiple timed ASS dialogue cues."
        ),
        cue_type="narration",
    )

    ass = media_tools._render_subtitles(
        [cue],
        subtitle_format="ass",
        max_chars_per_line=24,
        style={},
    )

    assert "Style: Default,Arial,52," in ass
    assert ",2,0,2,40,40,72,1" in ass
    dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) > 1
    for line in dialogue_lines:
        rendered_text = line.split(",,0,0,0,,", 1)[1]
        assert len(rendered_text.split(r"\N")) <= 2


def test_subtitle_cues_must_match_the_canonical_transcript_verbatim():
    matching = [
        media_tools.SubtitleCue(1, 0.0, 1.0, "Build the script.", "narration"),
        media_tools.SubtitleCue(2, 1.0, 2.0, "Generate the assets.", "narration"),
    ]
    paraphrased = [
        media_tools.SubtitleCue(1, 0.0, 1.0, "Write a script.", "narration"),
        media_tools.SubtitleCue(2, 1.0, 2.0, "Create assets.", "narration"),
    ]

    assert media_tools._subtitle_cues_match_transcript(
        matching,
        "Build the script. Generate the assets.",
    )
    assert not media_tools._subtitle_cues_match_transcript(
        paraphrased,
        "Build the script. Generate the assets.",
    )


def test_subtitle_cues_scale_to_the_real_narration_duration():
    cues = [
        media_tools.SubtitleCue(1, 0.0, 5.0, "First line.", "narration"),
        media_tools.SubtitleCue(2, 5.0, 10.0, "Second line.", "narration"),
    ]

    scaled = media_tools._scale_subtitle_cues_to_duration(cues, 8.0)

    assert [(cue.start, cue.end, cue.text) for cue in scaled] == [
        (0.0, 4.0, "First line."),
        (4.0, 8.0, "Second line."),
    ]


def test_subtitle_cues_split_to_at_most_two_rendered_lines():
    cues = [
        media_tools.SubtitleCue(
            1,
            0.0,
            8.0,
            (
                "Start in Workspace Chat where specialist roles coordinate "
                "exploration planning capture production and quality review."
            ),
            "narration",
        )
    ]

    fitted = media_tools._fit_subtitle_cues_to_line_limit(
        cues,
        max_chars_per_line=24,
        max_lines=2,
    )

    assert len(fitted) > 1
    assert fitted[0].start == 0.0
    assert fitted[-1].end == 8.0
    assert all(
        len(media_tools._wrap_subtitle_text(cue.text, 24).splitlines()) <= 2
        for cue in fitted
    )
    assert media_tools._subtitle_cues_match_transcript(
        fitted,
        "Start in Workspace Chat where specialist roles coordinate "
        "exploration planning capture production and quality review.",
    )


@pytest.mark.asyncio
async def test_align_subtitles_handler_rejects_nonverbatim_cues(monkeypatch, tmp_path):
    cues_path = tmp_path / "cues.json"
    transcript_path = tmp_path / "narration.txt"
    cues_path.write_text(
        json.dumps(
            {
                "cues": [
                    {"type": "narration", "start": 0, "end": 2, "text": "Write a script."}
                ]
            }
        ),
        encoding="utf-8",
    )
    transcript_path.write_text("Build the script.", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["code"] == "subtitle_transcript_mismatch"


@pytest.mark.asyncio
async def test_measured_transcription_resolves_stt_model_and_byok(monkeypatch, tmp_path):
    audio_path = tmp_path / "narration.wav"
    audio_path.write_bytes(b"RIFF-final-narration")
    captured: dict = {}
    expected = SimpleNamespace(
        text="Measured narration.",
        duration_seconds=5.8,
        model="groq/whisper-large-v3",
        segments=[{"start": 0.2, "end": 5.8, "text": "Measured narration."}],
    )

    async def fake_resolve_model(role, **kwargs):
        assert role == "stt"
        assert kwargs == {"user_id": "user-1", "entity_id": "entity-1"}
        return "groq/whisper-large-v3"

    async def fake_resolve_metadata(role, **kwargs):
        assert role == "stt"
        assert kwargs == {"user_id": "user-1", "entity_id": "entity-1"}
        return {"llm_api_key": "gsk_user_key"}

    async def fake_transcribe(blob, **kwargs):
        captured["blob"] = blob
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.services.voice.whisper.transcribe_blob",
        fake_transcribe,
    )

    result = await media_tools._transcribe_narration_audio(
        audio_path=str(audio_path),
        user_id="user-1",
        entity_id="entity-1",
        reference_transcript="Measured narration.",
    )

    assert result is expected
    assert captured == {
        "blob": b"RIFF-final-narration",
        "mime": "audio/wav",
        "filename": "narration.wav",
        "user_api_key": "gsk_user_key",
        "resolved_model": "groq/whisper-large-v3",
        "require_timestamps": True,
        "reference_transcript": "Measured narration.",
    }


@pytest.mark.asyncio
async def test_platform_funded_measured_transcription_checks_credit_and_records_usage(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "narration.wav"
    audio_path.write_bytes(b"RIFF-platform-narration")
    events: list[tuple[str, object]] = []
    expected = SimpleNamespace(
        text="Measured narration.",
        duration_seconds=30.0,
        model="openai/whisper-1",
        segments=[{"start": 0.2, "end": 29.8, "text": "Measured narration."}],
    )

    async def fake_resolve_model(_role, **_kwargs):
        return "openai/whisper-1"

    async def fake_resolve_metadata(_role, **_kwargs):
        return None

    async def fake_credit(entity_id, *, source):
        events.append(("credit", {"entity_id": entity_id, "source": source}))

    async def fake_transcribe(_blob, **_kwargs):
        events.append(("transcribe", None))
        return expected

    async def fake_record_media_usage(_db, **kwargs):
        events.append(("usage", kwargs))
        return True

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            events.append(("commit", None))

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        fake_credit,
    )
    monkeypatch.setattr(
        "packages.core.services.voice.whisper.transcribe_blob",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "packages.core.services.usage_service.record_media_usage",
        fake_record_media_usage,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: FakeSession(),
    )

    result = await media_tools._transcribe_narration_audio(
        audio_path=str(audio_path),
        user_id="user-1",
        entity_id="entity-1",
        workspace_id="workspace-1",
        agent_id="agent-1",
        conversation_id="conversation-1",
    )

    assert result is expected
    assert [event[0] for event in events] == ["credit", "transcribe", "usage", "commit"]
    assert events[0][1] == {"entity_id": "entity-1", "source": "audio_transcribe"}
    assert events[2][1] == {
        "entity_id": "entity-1",
        "kind": "whisper",
        "model": "openai/whisper-1",
        "cost_usd": pytest.approx(0.003),
        "units": 30,
        "workspace_id": "workspace-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "conversation_id": "conversation-1",
        "source": "audio_transcribe",
        "byok": False,
    }


@pytest.mark.asyncio
async def test_byok_measured_transcription_preserves_base_url_without_platform_billing(
    monkeypatch,
    tmp_path,
):
    audio_path = tmp_path / "narration.wav"
    audio_path.write_bytes(b"RIFF-byok-narration")
    captured: dict = {}
    expected = SimpleNamespace(
        text="Measured narration.",
        duration_seconds=3.0,
        model="openai/whisper-1",
        segments=[{"start": 0.2, "end": 2.8, "text": "Measured narration."}],
    )

    async def fake_resolve_model(_role, **_kwargs):
        return "openai/whisper-1"

    async def fake_resolve_metadata(_role, **_kwargs):
        return {
            "llm_api_key": "sk-custom-endpoint-key",
            "llm_base_url": "https://stt.example.test/v1",
        }

    async def unexpected_credit(*_args, **_kwargs):
        raise AssertionError("BYOK STT must not check platform credit")

    async def fake_transcribe(blob, **kwargs):
        captured["blob"] = blob
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        unexpected_credit,
    )
    monkeypatch.setattr(
        "packages.core.services.voice.whisper.transcribe_blob",
        fake_transcribe,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: (_ for _ in ()).throw(AssertionError("BYOK STT must not record platform usage")),
    )

    result = await media_tools._transcribe_narration_audio(
        audio_path=str(audio_path),
        user_id="user-1",
        entity_id="entity-1",
    )

    assert result is expected
    assert captured["user_api_key"] == "sk-custom-endpoint-key"
    assert captured["user_base_url"] == "https://stt.example.test/v1"


@pytest.mark.asyncio
async def test_measured_transcription_rejects_oversized_file_before_read(monkeypatch, tmp_path):
    from packages.core.services.voice.whisper import (
        WHISPER_MAX_UPLOAD_BYTES,
        WhisperUploadTooLargeError,
    )

    audio_path = tmp_path / "narration.wav"
    with audio_path.open("wb") as handle:
        handle.truncate(WHISPER_MAX_UPLOAD_BYTES + 1)

    async def fake_resolve_model(_role, **_kwargs):
        return "openai/whisper-1"

    async def fake_resolve_metadata(_role, **_kwargs):
        return None

    async def unexpected_credit(*_args, **_kwargs):
        raise AssertionError("oversized narration must fail before credit preflight")

    def unexpected_read(_path):
        raise AssertionError("oversized narration must not be loaded into memory")

    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_assert_credit_available",
        unexpected_credit,
    )
    monkeypatch.setattr(Path, "read_bytes", unexpected_read)

    with pytest.raises(WhisperUploadTooLargeError, match="25 MB"):
        await media_tools._transcribe_narration_audio(
            audio_path=str(audio_path),
            user_id="user-1",
            entity_id="entity-1",
        )


@pytest.mark.asyncio
async def test_align_subtitles_handler_uses_measured_semantic_alignment(monkeypatch, tmp_path):
    cues_path = tmp_path / "cues.json"
    timeline_path = tmp_path / "timeline.json"
    transcript_path = tmp_path / "narration.txt"
    audio_path = tmp_path / "narration.wav"
    output_path = tmp_path / "subtitles.ass"
    cues_path.write_text(
        json.dumps(
            {
                "cues": [
                    {
                        "type": "narration",
                        "start": 0,
                        "end": 5,
                        "text": "First line.",
                        "estimated": True,
                    },
                    {
                        "type": "narration",
                        "start": 5,
                        "end": 10,
                        "text": "Second line.",
                        "estimated": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    timeline_path.write_text(
        json.dumps(
            {
                "spec": {"width": 1920, "height": 1080},
                "visual_scene_intervals": [
                    {
                        "scene_id": "scene-1",
                        "visual_start_seconds": 0.0,
                        "visual_end_seconds": 2.5,
                        "narration_start_seconds": 0.0,
                        "narration_end_seconds": 2.5,
                    },
                    {
                        "scene_id": "scene-2",
                        "visual_start_seconds": 2.5,
                        "visual_end_seconds": 6.0,
                        "narration_start_seconds": 2.5,
                        "narration_end_seconds": 6.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    transcript_path.write_text("First line. Second line.", encoding="utf-8")
    audio_path.write_bytes(b"RIFF-test-audio")
    captured: dict = {}

    async def fake_probe(_ffprobe, path):
        assert path == str(audio_path)
        return {"duration_seconds": 8.0}

    async def fake_audio_transcript_match(**kwargs):
        assert kwargs["entity_id"] == "entity-1"
        assert kwargs["audio_rel_path"] == "narration.wav"
        assert kwargs["transcript"] == "First line. Second line."
        return True

    async def fake_transcribe_narration(**kwargs):
        assert kwargs == {
            "audio_path": str(audio_path),
            "user_id": "user-1",
            "entity_id": "entity-1",
                "workspace_id": None,
                "agent_id": None,
                "conversation_id": None,
                "reference_transcript": "First line. Second line.",
            }
        return SimpleNamespace(
            text="First line. Second line.",
            duration_seconds=6.0,
            model="groq/whisper-large-v3",
            segments=[
                {
                    "start": 0.2,
                    "end": 5.8,
                    "text": "First line. Second line.",
                },
            ],
            words=[
                {"start": 0.2, "end": 1.2, "text": "First"},
                {"start": 1.3, "end": 2.4, "text": "line."},
                {"start": 2.7, "end": 4.0, "text": "Second"},
                {"start": 4.1, "end": 5.8, "text": "line."},
            ],
        )

    async def fake_target(**_kwargs):
        return SimpleNamespace(
            abs_dir=str(tmp_path),
            abs_path=str(output_path),
            rel_path="subtitles.ass",
            filename="subtitles.ass",
        )

    def fake_write(_entity_id, rel_path, data, **_kwargs):
        assert rel_path == "subtitles.ass"
        output_path.write_bytes(data)
        return str(output_path)

    async def fake_register(**kwargs):
        captured["generation"] = kwargs["generation"]
        return "doc-1"

    async def fake_bind(**_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.write_entity_file_atomic",
        fake_write,
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(
        media_tools,
        "_audio_prompt_matches_transcript",
        fake_audio_transcript_match,
        raising=False,
    )
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
        raising=False,
    )
    monkeypatch.setattr(media_tools, "_build_media_target", fake_target)
    monkeypatch.setattr(media_tools, "_register_file_artifact", fake_register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", fake_bind)

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            timeline_path="timeline.json",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "completed"
    assert result["transcript_matches"] is True
    assert result["audio_transcript_matches"] is True
    assert result["audio_duration_seconds"] == 8.0
    assert result["timing_scaled"] is False
    assert result["alignment_metrics"]["similarity"] >= 0.90
    assert result["alignment_metrics"]["coverage"] == 1.0
    assert result["alignment_metrics"]["measured_timestamps"] is True
    assert result["alignment_metrics"]["transcription_model"] == "groq/whisper-large-v3"
    assert result["alignment_metrics"]["timing_sources"] == ["measured_stt_words"]
    assert result["alignment_metrics"]["sentence_timestamps"] == [
        {
            "sentence_index": 1,
            "start": 0.2,
            "end": 2.4,
            "timing_source": "measured_stt_words",
        },
        {
            "sentence_index": 2,
            "start": 2.7,
            "end": 5.8,
            "timing_source": "measured_stt_words",
        },
    ]
    assert result["alignment_metrics"]["scene_coverage"] == {
        "coverage": 1.0,
        "covered_scene_ids": ["scene-1", "scene-2"],
        "missing_scene_ids": [],
        "missing_interval_scene_ids": [],
        "unmapped_scene_ids": [],
    }
    rendered = output_path.read_text(encoding="utf-8")
    assert "Style: Default,Arial,52," in rendered
    assert ",2,0,2,40,40,72,1" in rendered
    assert "Dialogue: 0,0:00:00.20,0:00:02.40" in rendered
    assert "Dialogue: 0,0:00:02.70,0:00:05.80" in rendered
    assert captured["generation"]["transcript_matches"] is True
    assert captured["generation"]["audio_transcript_matches"] is True
    assert captured["generation"]["audio_duration_seconds"] == 8.0
    assert captured["generation"]["alignment_metrics"] == result["alignment_metrics"]


@pytest.mark.asyncio
async def test_measured_alignment_requires_timestamp_capable_stt(monkeypatch, tmp_path):
    (tmp_path / "cues.json").write_text(
        json.dumps(
            {"cues": [{"type": "narration", "start": 0, "end": 4, "text": "Exact line."}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "narration.txt").write_text("Exact line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 4.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        from packages.core.services.voice.whisper import WhisperTimestampError

        raise WhisperTimestampError(
            "Measured subtitle alignment requires a timestamp-capable STT model; "
            "openai/gpt-4o-audio-preview returns no segments."
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
        raising=False,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_timestamp_capable_stt_required"
    assert "timestamp-capable STT" in result["error"]


@pytest.mark.asyncio
async def test_measured_alignment_blocks_multi_sentence_segment_without_word_timestamps(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "cues.json").write_text(
        json.dumps(
            {
                "cues": [
                    {"type": "narration", "start": 0, "end": 2, "text": "First line."},
                    {"type": "narration", "start": 2, "end": 4, "text": "Second line."},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "narration.txt").write_text("First line. Second line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 4.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        return SimpleNamespace(
            text="First line. Second line.",
            duration_seconds=4.0,
            model="openai/whisper-1",
            segments=[
                {
                    "start": 0.2,
                    "end": 3.8,
                    "text": "First line. Second line.",
                }
            ],
            words=None,
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_word_timestamps_required"
    assert result["transcription_model"] == "openai/whisper-1"
    assert "word-level timestamps" in result["error"]


@pytest.mark.asyncio
async def test_measured_alignment_rejects_oversized_audio_with_actionable_code(
    monkeypatch,
    tmp_path,
):
    from packages.core.services.voice.whisper import (
        WHISPER_MAX_UPLOAD_BYTES,
        WhisperUploadTooLargeError,
    )

    (tmp_path / "cues.json").write_text(
        json.dumps(
            {"cues": [{"type": "narration", "start": 0, "end": 4, "text": "Exact line."}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "narration.txt").write_text("Exact line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 4.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        raise WhisperUploadTooLargeError(
            "Narration audio exceeds the 25 MB STT upload limit."
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_audio_too_large"
    assert result["max_upload_bytes"] == WHISPER_MAX_UPLOAD_BYTES
    assert "25 MB" in result["error"]


@pytest.mark.asyncio
async def test_align_subtitles_audio_requires_canonical_transcript_before_media_access(
    monkeypatch,
):
    def unexpected_entity_root(_entity_id):
        raise AssertionError("audio without transcript must fail before media access")

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        unexpected_entity_root,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "subtitle_transcript_required"
    assert "transcript_path" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segments",
    [
        [{"start": 0.2, "end": 1.8, "text": "Delete the account."}],
        [
            {
                "start": float(index),
                "end": float(index + 1),
                "text": f"Canonical sentence {index + 1}.",
            }
            for index in range(18)
        ],
    ],
)
async def test_semantic_alignment_handler_rejects_low_similarity_or_coverage_below_95(
    monkeypatch,
    tmp_path,
    segments,
):
    sentence_count = 1 if len(segments) == 1 else 20
    transcript = " ".join(
        f"Canonical sentence {index}." for index in range(1, sentence_count + 1)
    )
    cues = [
        {
            "type": "narration",
            "start": index - 1,
            "end": index,
            "text": f"Canonical sentence {index}.",
            "estimated": True,
        }
        for index in range(1, sentence_count + 1)
    ]
    (tmp_path / "cues.json").write_text(json.dumps({"cues": cues}), encoding="utf-8")
    (tmp_path / "narration.txt").write_text(transcript, encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": float(sentence_count)}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        return SimpleNamespace(
            text=" ".join(str(segment["text"]) for segment in segments),
            duration_seconds=float(sentence_count),
            model="groq/whisper-large-v3",
            segments=segments,
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
        raising=False,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_semantic_alignment_failed"
    assert result["alignment_metrics"]["coverage"] < 0.95
    assert result["alignment_metrics"]["missing_sentence_indexes"]


@pytest.mark.asyncio
async def test_semantic_alignment_blocks_with_missing_scene_ids(monkeypatch, tmp_path):
    payload = {
        "scenes": [
            {"scene_id": "scene-1", "start": 0.0, "end": 3.0},
            {"scene_id": "scene-2", "start": 6.0, "end": 8.0},
        ],
        "cues": [
            {
                "scene_id": "scene-1",
                "type": "narration",
                "start": 0,
                "end": 3,
                "text": "First line.",
            },
            {
                "scene_id": "scene-2",
                "type": "narration",
                "start": 3,
                "end": 6,
                "text": "Second line.",
            },
        ],
    }
    (tmp_path / "cues.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "narration.txt").write_text("First line. Second line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 8.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        return SimpleNamespace(
            text="First line. Second line.",
            duration_seconds=5.8,
            model="groq/whisper-large-v3",
            segments=[
                {"start": 0.2, "end": 2.4, "text": "First line."},
                {"start": 2.7, "end": 5.8, "text": "Second line."},
            ],
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
        raising=False,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_scene_alignment_incomplete"
    assert result["missing_scene_ids"] == ["scene-2"]
    assert "scene-2" in result["error"]


@pytest.mark.asyncio
async def test_semantic_alignment_blocks_actionably_when_scene_interval_is_missing(
    monkeypatch,
    tmp_path,
):
    payload = {
        "scenes": [
            {"scene_id": "scene-no-range", "narration_text": "First line."},
        ],
        "cues": [
            {
                "scene_id": "scene-no-range",
                "type": "narration",
                "start": 0,
                "end": 2,
                "text": "First line.",
                "estimated": False,
                "measured": True,
                "timing_source": "measured_stt_segments",
            },
        ],
    }
    (tmp_path / "cues.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "narration.txt").write_text("First line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 2.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def fake_transcribe_narration(**_kwargs):
        return SimpleNamespace(
            text="First line.",
            duration_seconds=1.2,
            model="groq/whisper-large-v3",
            segments=[{"start": 0.2, "end": 1.2, "text": "First line."}],
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        fake_transcribe_narration,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "blocked"
    assert result["code"] == "subtitle_scene_interval_missing"
    assert result["missing_interval_scene_ids"] == ["scene-no-range"]
    assert "stable start/end interval" in result["error"]


@pytest.mark.asyncio
async def test_measured_existing_cues_are_preserved_without_retranscription(monkeypatch, tmp_path):
    (tmp_path / "cues.json").write_text(
        json.dumps(
            {
                "cues": [
                    {
                        "scene_id": "scene-1",
                        "type": "narration",
                        "start": 0.2,
                        "end": 2.4,
                        "text": "First line.",
                        "estimated": False,
                        "timing_source": "measured_stt_segments",
                    },
                    {
                        "scene_id": "scene-2",
                        "type": "narration",
                        "start": 2.7,
                        "end": 5.8,
                        "text": "Second line.",
                        "estimated": False,
                        "timing_source": "measured_stt_segments",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "narration.txt").write_text("First line. Second line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")
    output_path = tmp_path / "subtitles.ass"

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 6.0}

    async def fake_audio_transcript_match(**_kwargs):
        return True

    async def unexpected_transcription(**_kwargs):
        raise AssertionError("already measured cues must not be retranscribed")

    async def fake_target(**_kwargs):
        return SimpleNamespace(
            abs_dir=str(tmp_path),
            abs_path=str(output_path),
            rel_path="subtitles.ass",
            filename="subtitles.ass",
        )

    def fake_write(_entity_id, _rel_path, data, **_kwargs):
        output_path.write_bytes(data)
        return str(output_path)

    async def fake_register(**_kwargs):
        return "doc-1"

    async def fake_bind(**_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.write_entity_file_atomic",
        fake_write,
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(media_tools, "_audio_prompt_matches_transcript", fake_audio_transcript_match)
    monkeypatch.setattr(
        media_tools,
        "_transcribe_narration_audio",
        unexpected_transcription,
    )
    monkeypatch.setattr(media_tools, "_build_media_target", fake_target)
    monkeypatch.setattr(media_tools, "_register_file_artifact", fake_register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", fake_bind)

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            user_id="user-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["status"] == "completed"
    assert result["alignment_metrics"]["measured_timestamps"] is True
    assert result["alignment_metrics"]["transcription_model"] == "existing_measured_cues"
    rendered = output_path.read_text(encoding="utf-8")
    assert "Dialogue: 0,0:00:00.20,0:00:02.40" in rendered
    assert "Dialogue: 0,0:00:02.70,0:00:05.80" in rendered


def test_semantic_alignment_schema_describes_measured_timing() -> None:
    schema = media_tools.ALIGN_SUBTITLES_SCHEMA["function"]
    audio_description = schema["parameters"]["properties"]["audio_path"]["description"]

    assert "measured" in schema["description"]
    assert "semantic" in schema["description"]
    assert "measured" in audio_description
    assert "semantic" in audio_description
    assert "fit cues" not in audio_description.lower()


@pytest.mark.asyncio
async def test_align_subtitles_handler_rejects_audio_generated_from_other_text(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "cues.json").write_text(
        json.dumps(
            {
                "cues": [
                    {"type": "narration", "start": 0, "end": 2, "text": "Exact line."}
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "narration.txt").write_text("Exact line.", encoding="utf-8")
    (tmp_path / "narration.wav").write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 2.0}

    async def fake_audio_transcript_match(**_kwargs):
        return False

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(
        media_tools,
        "_audio_prompt_matches_transcript",
        fake_audio_transcript_match,
        raising=False,
    )

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["code"] == "subtitle_audio_transcript_mismatch"


@pytest.mark.asyncio
async def test_align_subtitles_handler_rejects_unverified_audio_when_required(
    monkeypatch,
    tmp_path,
):
    cues_path = tmp_path / "cues.json"
    transcript_path = tmp_path / "narration.txt"
    audio_path = tmp_path / "narration.wav"
    output_path = tmp_path / "subtitles.ass"
    cues_path.write_text(
        json.dumps(
            {
                "cues": [
                    {"type": "narration", "start": 0, "end": 2, "text": "Exact line."}
                ]
            }
        ),
        encoding="utf-8",
    )
    transcript_path.write_text("Exact line.", encoding="utf-8")
    audio_path.write_bytes(b"RIFF-test-audio")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 2.0}

    async def unknown_audio_transcript_match(**_kwargs):
        return None

    async def fake_target(**_kwargs):
        return SimpleNamespace(
            abs_dir=str(tmp_path),
            abs_path=str(output_path),
            rel_path="subtitles.ass",
            filename="subtitles.ass",
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)
    monkeypatch.setattr(
        media_tools,
        "_audio_prompt_matches_transcript",
        unknown_audio_transcript_match,
        raising=False,
    )
    monkeypatch.setattr(media_tools, "_build_media_target", fake_target)

    result = json.loads(
        await media_tools._align_subtitles_handler(
            entity_id="entity-1",
            cues_path="cues.json",
            transcript_path="narration.txt",
            audio_path="narration.wav",
            require_audio_transcript_match=True,
            output_name="subtitles.ass",
            format="ass",
        )
    )

    assert result["code"] == "subtitle_audio_transcript_unverified"


def test_align_subtitles_schema_exposes_optional_strict_audio_provenance() -> None:
    props = media_tools.ALIGN_SUBTITLES_SCHEMA["function"]["parameters"]["properties"]
    assert props["require_audio_transcript_match"] == {
        "type": "boolean",
        "default": False,
        "description": (
            "Fail when narration generation metadata cannot prove that its prompt "
            "matches transcript_path."
        ),
    }

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


def test_compose_video_timeline_defaults_to_optional_audio():
    props = media_tools.COMPOSE_VIDEO_TIMELINE_SCHEMA["function"]["parameters"]["properties"]

    assert props["require_audio"]["type"] == "boolean"
    assert props["require_audio"]["default"] is False


def test_workspace_media_reference_is_scoped_to_physical_artifact_root():
    workspace_base = "Workspaces/_by_id/folder-123"

    assert media_tools._workspace_media_reference(
        "Product Videos/project-1/audio/narration.wav",
        entity_id="entity-1",
        workspace_base_dir=workspace_base,
    ) == (
        "Workspaces/_by_id/folder-123/"
        "Product Videos/project-1/audio/narration.wav"
    )
    assert media_tools._workspace_media_reference(
        "/api/v1/fs/entity-1/Workspaces/_by_id/folder-123/"
        "Product%20Videos/project-1/audio/narration.wav",
        entity_id="entity-1",
        workspace_base_dir=workspace_base,
    ) == (
        "Workspaces/_by_id/folder-123/"
        "Product Videos/project-1/audio/narration.wav"
    )


def test_video_document_from_another_workspace_is_rejected(tmp_path):
    other_path = "Workspaces/_by_id/folder-other/Recordings/scene.webm"
    source = tmp_path / other_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    document = SimpleNamespace(
        id="document-other",
        name="scene.webm",
        fs_path=other_path,
        file_type="webm",
        mime_type="video/webm",
        metadata_={"origin": {"workspace_id": "workspace-other"}},
    )

    with pytest.raises(ValueError, match="does not belong to Workspace workspace-current"):
        media_tools._video_input_from_document(
            str(tmp_path),
            document,
            workspace_id="workspace-current",
            workspace_base_dir="Workspaces/_by_id/folder-current",
        )


@pytest.mark.asyncio
async def test_normalize_audio_scopes_workspace_relative_input_path(monkeypatch, tmp_path):
    workspace_base = "Workspaces/_by_id/folder-123"
    logical_input = "Product Videos/project-1/audio/narration.wav"
    physical_input = tmp_path / workspace_base / logical_input
    physical_input.parent.mkdir(parents=True)
    physical_input.write_bytes(b"RIFF-test-audio")
    captured: dict = {}

    async def fake_workspace_base_dir(**kwargs):
        assert kwargs == {
            "entity_id": "entity-1",
            "workspace_id": "workspace-1",
            "task_id": None,
        }
        return workspace_base

    async def fake_run_process(args, **_kwargs):
        captured["input_path"] = args[args.index("-i") + 1]
        output_path = Path(args[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF-normalized-audio")
        return "", ""

    async def fake_register_file_artifact(**_kwargs):
        return "document-1"

    async def fake_bind_artifact_to_workspace(**_kwargs):
        return None

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(
        "packages.core.services.generated_media_naming.resolve_workspace_artifact_base_dir",
        fake_workspace_base_dir,
    )
    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)
    monkeypatch.setattr(media_tools, "_register_file_artifact", fake_register_file_artifact)
    monkeypatch.setattr(
        media_tools,
        "_bind_artifact_to_workspace",
        fake_bind_artifact_to_workspace,
    )

    result = json.loads(await media_tools._normalize_audio_loudness_handler(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        input_path=logical_input,
        output_name="Product Videos/project-1/audio/narration-normalized.wav",
    ))

    assert result["status"] == "completed"
    assert result["fs_path"] == (
        "Workspaces/_by_id/folder-123/"
        "Product Videos/project-1/audio/narration-normalized.wav"
    )
    assert captured["input_path"] == str(physical_input)


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


def test_merge_videos_schema_supports_ordered_trimmed_speed_clips():
    properties = media_tools.MERGE_VIDEOS_SCHEMA["function"]["parameters"]["properties"]
    clip = properties["clips"]["items"]

    assert {
        "document_id",
        "job_id",
        "path",
        "start_seconds",
        "end_seconds",
        "speed",
        "label",
    }.issubset(clip["properties"])


def test_merge_video_clip_window_applies_trim_and_speed():
    assert media_tools._resolve_clip_window(
        source_duration=30.0,
        start_seconds=5.0,
        end_seconds=17.0,
        speed=3.0,
    ) == (5.0, 17.0, 4.0)

    with pytest.raises(ValueError, match="starts after the source ends"):
        media_tools._resolve_clip_window(
            source_duration=10.0,
            start_seconds=10.0,
            end_seconds=None,
            speed=1.0,
        )


@pytest.mark.asyncio
async def test_workspace_media_artifact_uses_workspace_folder_projection(monkeypatch):
    captured: dict = {}

    async def fake_workspace_folder(**kwargs):
        captured["workspace_folder"] = kwargs
        return "workspace-project-folder"

    async def unexpected_global_folder(*_args, **_kwargs):
        raise AssertionError("Workspace media must not create a root Knowledge folder")

    async def fake_upsert(_db, _entity_id, **kwargs):
        captured["upsert"] = kwargs
        return SimpleNamespace(
            id="doc-1",
            source="ai_generated",
            created_by="user-1",
            metadata_={},
        )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(
        "packages.core.services.workspace_artifacts.ensure_workspace_document_folder",
        fake_workspace_folder,
    )
    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.ensure_folder_path",
        unexpected_global_folder,
    )
    monkeypatch.setattr(
        "packages.core.services.document_service.upsert_document_by_fs_path",
        fake_upsert,
    )
    monkeypatch.setattr(
        "packages.core.database.async_session",
        lambda: FakeSession(),
    )

    document_id = await media_tools._register_file_artifact(
        entity_id="entity-1",
        user_id="user-1",
        filename="final.mp4",
        rel_path="Workspaces/_by_id/root-folder/project-1/final/final.mp4",
        file_size=123,
        file_type="mp4",
        mime_type="video/mp4",
        workspace_id="workspace-1",
        task_id="task-1",
        agent_id="agent-1",
        conversation_id="conversation-1",
        tool_name="compose_video_timeline",
        artifact_role="final",
        generation={"operation": "compose_video_timeline"},
    )

    assert document_id == "doc-1"
    assert captured["workspace_folder"] == {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "rel_path": "Workspaces/_by_id/root-folder/project-1/final/final.mp4",
    }
    assert captured["upsert"]["folder_id"] == "workspace-project-folder"


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


def test_ass_ffmpeg_filter_preserves_embedded_style_without_compose_override():
    style = media_tools._compose_subtitle_style(
        subtitle_path="/tmp/final.ass",
        width=1920,
        height=1080,
        timeline={},
        explicit_override=None,
    )
    value = media_tools._subtitles_filter_value("/tmp/final.ass", style)

    assert style == {}
    assert "force_style=" not in value


def test_ass_ffmpeg_filter_preserves_explicit_timeline_style_override():
    style = media_tools._compose_subtitle_style(
        subtitle_path="/tmp/final.ass",
        width=1920,
        height=1080,
        timeline={"subtitles": {"style": {"font_size": 26, "margin_v": 52}}},
        explicit_override=None,
    )
    value = media_tools._subtitles_filter_value("/tmp/final.ass", style)

    assert style == {"font_size": 26, "margin_v": 52}
    assert "Fontsize=26" in value
    assert "MarginV=52" in value


def test_ass_ffmpeg_filter_direct_override_takes_precedence_over_timeline_style():
    style = media_tools._compose_subtitle_style(
        subtitle_path="/tmp/final.ass",
        width=1920,
        height=1080,
        timeline={"subtitles": {"style": {"font_size": 99, "margin_v": 99}}},
        explicit_override={"font_size": 32},
    )
    value = media_tools._subtitles_filter_value("/tmp/final.ass", style)

    assert style == {"font_size": 32, "margin_v": 99}
    assert "Fontsize=32" in value
    assert "MarginV=99" in value


def test_srt_ffmpeg_filter_receives_deterministic_default_style():
    style = media_tools._compose_subtitle_style(
        subtitle_path="/tmp/final.srt",
        width=1920,
        height=1080,
        timeline={},
        explicit_override=None,
    )
    value = media_tools._subtitles_filter_value("/tmp/final.srt", style)

    assert style == {
        "font_size": 52,
        "margin_v": 72,
        "outline": 2,
        "shadow": 0,
        "alignment": 2,
        "max_lines": 2,
    }
    assert "force_style=" in value
    assert "Fontsize=52" in value
    assert "MarginV=72" in value


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

    assert "00:00:01,200 -->" in srt
    assert "--> 00:00:03,450" in srt
    assert "Hello from the" in srt
    assert "[V4+ Styles]" in ass
    assert "Style: Default,Inter,40" in ass
    assert r"Hello from the\Naligned subtitle" in ass


def test_render_ass_subtitles_preserves_requested_delivery_style_and_line_length():
    cues = [
        media_tools.SubtitleCue(
            index=1,
            start=0,
            end=3,
            text=(
                "This subtitle sentence is deliberately long enough to wrap across multiple "
                "lines without exceeding the requested character limit"
            ),
            cue_type="dialogue",
        )
    ]

    ass = media_tools._render_subtitles(
        cues,
        subtitle_format="ass",
        max_chars_per_line=42,
        style={
            "font_size": 42,
            "outline": 2,
            "shadow": 0,
            "alignment": 2,
            "margin_v": 80,
        },
    )

    assert "PlayResX: 1920" in ass
    assert "PlayResY: 1080" in ass
    assert "Style: Default,Arial,42," in ass
    assert ",2,0,2,40,40,80,1" in ass

    dialogue_text = ass.split(",,0,0,0,,", 1)[1].splitlines()[0]
    wrapped_lines = dialogue_text.split(r"\N")
    assert len(wrapped_lines) > 1
    assert max(map(len, wrapped_lines)) <= 42


@pytest.mark.parametrize(
    "text",
    [
        "这是一段没有空格的中文字幕用于验证每一行都会严格遵守四十二个字符的最大长度限制并继续正确换行",
        "https://example.com/assets/a-very-long-unbroken-release-token-abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_wrap_subtitle_text_hard_wraps_unspaced_text(text):
    wrapped = media_tools._wrap_subtitle_text(text, 42)

    assert len(wrapped.splitlines()) > 1
    assert max(map(len, wrapped.splitlines())) <= 42
    assert wrapped.replace("\n", "") == text


def test_render_ass_subtitles_hard_wraps_unspaced_text_to_requested_length():
    cue = media_tools.SubtitleCue(
        index=1,
        start=0,
        end=3,
        text="无空格中文字幕需要在渲染后的ASS字幕中严格限制每一行长度不能超过四十二个字符否则会溢出画面边界",
        cue_type="dialogue",
    )

    ass = media_tools._render_subtitles(
        [cue],
        subtitle_format="ass",
        max_chars_per_line=42,
        style={},
    )

    dialogue_text = ass.split(",,0,0,0,,", 1)[1].splitlines()[0]
    assert max(map(len, dialogue_text.split(r"\N"))) <= 42


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
        return {"duration_seconds": 5.0, "has_audio": True}

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
async def test_timeline_audio_track_without_audio_stream_is_unusable(tmp_path, monkeypatch):
    source = tmp_path / "video-only.webm"
    source.write_bytes(b"fake")

    async def fake_probe(_ffprobe, _path):
        return {"duration_seconds": 5.0, "has_audio": False}

    monkeypatch.setattr(media_tools, "_probe_media", fake_probe)

    with pytest.raises(ValueError, match="does not contain an audio stream"):
        await media_tools._resolve_timeline_audio_tracks(
            ffprobe="/usr/bin/ffprobe",
            entity_root=str(tmp_path),
            entity_id="entity123",
            timeline={
                "audio_tracks": [
                    {
                        "id": "bad-webm",
                        "type": "narration",
                        "path": "video-only.webm",
                        "start": 0,
                        "end": 5,
                    }
                ]
            },
            enabled=True,
        )


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
        return {"duration_seconds": 5.0, "has_audio": True, "width": 1280, "height": 720}

    async def fake_compose_video_file(**kwargs):
        captured["subtitle_style"] = kwargs["subtitle_style"]
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
    assert result["audio_tracks"] == []
    assert result["include_source_audio"] is True
    assert captured["include_source_audio"] is True
    assert captured["subtitle_style"] == {
        "font_size": 35,
        "margin_v": 48,
        "outline": 1,
        "shadow": 0,
        "alignment": 2,
        "max_lines": 2,
    }
    assert captured["operation"] == "compose_video_timeline"
    assert result["editor_recipe_document_id"] == "recipe_123"
    assert result["editor_recipe_path"] == "final/mixed.video-edit.json"
    assert captured["sidecar_final_document_id"] == "doc_123"
    assert captured["sidecar_final_rel_path"] == "final/mixed.mp4"
    assert captured["attached_final_document_id"] == "doc_123"
    assert captured["attached_recipe_document_id"] == "recipe_123"


@pytest.mark.asyncio
async def test_compose_video_timeline_extends_picture_to_narration_duration(
    tmp_path, monkeypatch
):
    captured: dict = {}
    clean = tmp_path / "clean.mp4"
    clean.write_bytes(b"clean")
    narration = tmp_path / "narration.wav"
    narration.write_bytes(b"audio")
    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "delivery": {"clean_picture_master": "clean.mp4"},
                "audio_tracks": [
                    {
                        "id": "narration",
                        "type": "narration",
                        "path": "narration.wav",
                        "start": 0,
                        "end_source": "probe_duration",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    final = tmp_path / "final" / "mixed.mp4"

    async def fake_probe_media(_ffprobe, path):
        if path == str(clean):
            return {"duration_seconds": 5.0, "has_audio": False}
        assert path == str(narration)
        return {"duration_seconds": 8.0, "has_audio": True}

    async def fake_compose_video_file(**kwargs):
        captured.update(kwargs)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"mixed")
        return str(final), "final/mixed.mp4", "mixed.mp4"

    async def fake_register_merged_video(**_kwargs):
        return "doc_123"

    async def fake_create_video_editor_recipe_sidecar(**_kwargs):
        return {}

    async def fake_attach_video_editor_recipe_to_video(**_kwargs):
        return None

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path)
    )
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe_media)
    monkeypatch.setattr(media_tools, "_compose_video_file", fake_compose_video_file)
    monkeypatch.setattr(media_tools, "_register_merged_video", fake_register_merged_video)
    monkeypatch.setattr(
        media_tools,
        "_create_video_editor_recipe_sidecar",
        fake_create_video_editor_recipe_sidecar,
    )
    monkeypatch.setattr(
        media_tools,
        "_attach_video_editor_recipe_to_video",
        fake_attach_video_editor_recipe_to_video,
    )

    result = json.loads(
        await media_tools._compose_video_timeline_handler(
            entity_id="entity123",
            user_id="user123",
            timeline_path="timeline.json",
            output_name="final/mixed.mp4",
            require_audio=True,
        )
    )

    assert result["status"] == "completed"
    assert result["duration_seconds"] == 8.0
    assert captured["clean_video_duration"] == 5.0
    assert captured["total_duration"] == 8.0


@pytest.mark.asyncio
async def test_compose_video_timeline_rejects_missing_required_audio_before_composition(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clean.mp4"
    clean.write_bytes(b"clean")
    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "delivery": {"clean_picture_master": "clean.mp4"},
                "audio_tracks": [],
            }
        ),
        encoding="utf-8",
    )
    final = tmp_path / "final" / "mixed.mp4"
    compose_called = False

    async def fake_probe_media(_ffprobe, _path):
        return {"duration_seconds": 5.0, "has_audio": False}

    async def fake_compose_video_file(**_kwargs):
        nonlocal compose_called
        compose_called = True
        raise AssertionError("composition must not run without required timeline audio")

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe_media)
    monkeypatch.setattr(media_tools, "_compose_video_file", fake_compose_video_file)

    result = json.loads(
        await media_tools._compose_video_timeline_handler(
            entity_id="entity123",
            timeline_path="timeline.json",
            output_name="final/mixed.mp4",
            require_audio=True,
        )
    )

    assert result["status"] == "error"
    assert result["code"] == "audio_track_missing"
    assert compose_called is False
    assert not final.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_case",
    ["missing_file", "unsupported_extension", "invalid_timing", "no_audio_stream"],
)
@pytest.mark.parametrize(
    ("require_audio", "expected_code"),
    [(True, "audio_track_missing"), (False, "compose_failed")],
)
async def test_compose_video_timeline_maps_audio_resolution_failures_by_mode(
    tmp_path, monkeypatch, failure_case, require_audio, expected_code
):
    clean = tmp_path / "clean.mp4"
    clean.write_bytes(b"clean")
    source_name = {
        "missing_file": "missing.wav",
        "unsupported_extension": "audio.txt",
        "invalid_timing": "audio.wav",
        "no_audio_stream": "video-only.webm",
    }[failure_case]
    source = tmp_path / source_name
    if failure_case != "missing_file":
        source.write_bytes(b"fake")

    timeline = tmp_path / "timeline.json"
    timeline.write_text(
        json.dumps(
            {
                "delivery": {"clean_picture_master": "clean.mp4"},
                "audio_tracks": [
                    {
                        "id": "track-1",
                        "type": "narration",
                        "path": source_name,
                        "start": 5 if failure_case == "invalid_timing" else 0,
                        "end": 4 if failure_case == "invalid_timing" else 5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    final = tmp_path / "final" / "mixed.mp4"
    compose_called = False
    register_called = False

    async def fake_probe_media(_ffprobe, path):
        return {
            "duration_seconds": 5.0,
            "has_audio": not path.endswith("video-only.webm"),
        }

    async def fake_compose_video_file(**_kwargs):
        nonlocal compose_called
        compose_called = True
        raise AssertionError("composition must not run after audio resolution failure")

    async def fake_register_merged_video(**_kwargs):
        nonlocal register_called
        register_called = True
        raise AssertionError("registration must not run after audio resolution failure")

    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(media_tools, "_probe_media", fake_probe_media)
    monkeypatch.setattr(media_tools, "_compose_video_file", fake_compose_video_file)
    monkeypatch.setattr(media_tools, "_register_merged_video", fake_register_merged_video)

    result = json.loads(
        await media_tools._compose_video_timeline_handler(
            entity_id="entity123",
            timeline_path="timeline.json",
            output_name="final/mixed.mp4",
            require_audio=require_audio,
        )
    )

    assert result["status"] == "error"
    assert result["code"] == expected_code
    assert compose_called is False
    assert register_called is False
    assert not final.exists()


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
async def test_probe_media_uses_packet_timestamps_when_container_duration_is_missing(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_process(args, **_kwargs):
        calls.append(args)
        if "format=duration" in args:
            return json.dumps({"streams": [{"codec_type": "video"}], "format": {}}), ""
        return "17.984000,0.001000\n18.018000,0.001000\n", ""

    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)

    result = await media_tools._probe_media("/usr/bin/ffprobe", "/tmp/chrome-recording.webm")

    assert result == {"duration_seconds": pytest.approx(18.019), "has_audio": False}
    assert len(calls) == 2
    assert "packet=pts_time,duration_time" in calls[1]


@pytest.mark.asyncio
async def test_probe_media_report_normalizes_container_video_and_audio(monkeypatch):
    async def fake_run_process(args, **_kwargs):
        assert "stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,display_aspect_ratio,sample_rate,channels,channel_layout,duration" in args
        return json.dumps({
            "format": {
                "duration": "90.125",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "bit_rate": "4200000",
                "size": "47328750",
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "r_frame_rate": "60000/1001",
                    "avg_frame_rate": "60000/1001",
                    "display_aspect_ratio": "16:9",
                    "duration": "90.125",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "duration": "90.101",
                },
            ],
        }), ""

    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)

    report = await media_tools._probe_media_report("/usr/bin/ffprobe", "/tmp/final.mp4")

    assert report == {
        "decodable": True,
        "duration_seconds": 90.125,
        "format_names": ["mov", "mp4", "m4a", "3gp", "3g2", "mj2"],
        "bit_rate": 4200000,
        "size_bytes": 47328750,
        "has_video": True,
        "has_audio": True,
        "video_stream": {
            "index": 0,
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "pixel_format": "yuv420p",
            "frame_rate": pytest.approx(59.94006),
            "average_frame_rate": pytest.approx(59.94006),
            "display_aspect_ratio": "16:9",
            "duration_seconds": 90.125,
        },
        "audio_stream": {
            "index": 1,
            "codec": "aac",
            "sample_rate": 48000,
            "channels": 2,
            "channel_layout": "stereo",
            "duration_seconds": 90.101,
        },
        "streams": [
            {"index": 0, "codec_type": "video", "codec": "h264"},
            {"index": 1, "codec_type": "audio", "codec": "aac"},
        ],
    }


@pytest.mark.asyncio
async def test_probe_media_report_rejects_invalid_json_and_missing_media_streams(monkeypatch):
    async def invalid_json(*_args, **_kwargs):
        return "not-json", ""

    monkeypatch.setattr(media_tools, "_run_process", invalid_json)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await media_tools._probe_media_report("/usr/bin/ffprobe", "/tmp/broken.mp4")

    async def no_media(*_args, **_kwargs):
        return json.dumps({"format": {"duration": "1"}, "streams": []}), ""

    monkeypatch.setattr(media_tools, "_run_process", no_media)
    with pytest.raises(RuntimeError, match="no audio or video streams"):
        await media_tools._probe_media_report("/usr/bin/ffprobe", "/tmp/empty.bin")


@pytest.mark.asyncio
async def test_probe_media_handler_resolves_entity_file(monkeypatch, tmp_path):
    source = tmp_path / "project" / "final.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"media")

    async def fake_report(ffprobe, path):
        assert ffprobe == "/usr/bin/ffprobe"
        assert path == str(source)
        return {"decodable": True, "duration_seconds": 3.0, "has_video": True, "has_audio": True}

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media_report", fake_report)

    result = json.loads(await media_tools._probe_media_handler(
        entity_id="entity-1",
        input_path="project/final.mp4",
    ))

    assert result["status"] == "completed"
    assert result["fs_path"] == "project/final.mp4"
    assert result["report"]["decodable"] is True


@pytest.mark.asyncio
async def test_still_to_video_renders_and_registers_h264_scene(monkeypatch, tmp_path):
    source = tmp_path / "project" / "still.png"
    output = tmp_path / "project" / "scene.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x89PNG\r\n\x1a\n")
    captured = {}

    async def fake_target(**kwargs):
        assert kwargs["output_name"] == "project/scene.mp4"
        return SimpleNamespace(
            abs_dir=str(output.parent),
            abs_path=str(output),
            rel_path="project/scene.mp4",
            filename="scene.mp4",
        )

    async def fake_run_process(args, **_kwargs):
        captured["args"] = args
        output.write_bytes(b"video")
        return "", ""

    async def fake_register(**kwargs):
        captured["generation"] = kwargs["generation"]
        assert kwargs["artifact_role"] == "video"
        return "doc-scene"

    async def fake_bind(**kwargs):
        captured["bound_document_id"] = kwargs["document_id"]

    async def fake_workspace_media_base_dir(**_kwargs):
        return ""

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_build_media_target", fake_target)
    monkeypatch.setattr(
        media_tools,
        "_workspace_media_base_dir",
        fake_workspace_media_base_dir,
    )
    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)
    monkeypatch.setattr(media_tools, "_register_file_artifact", fake_register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", fake_bind)

    result = json.loads(await media_tools._still_to_video_handler(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        input_path="project/still.png",
        output_name="project/scene.mp4",
        duration_seconds=4.5,
        resolution="1080p",
        aspect_ratio="16:9",
        fps=30,
    ))

    args = captured["args"]
    assert args[:5] == ["/usr/bin/ffmpeg", "-y", "-loop", "1", "-i"]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in args[args.index("-vf") + 1]
    assert args[args.index("-t") + 1] == "4.500"
    assert args[args.index("-r") + 1] == "30"
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-movflags") + 1] == "+faststart"
    assert result["status"] == "completed"
    assert result["document_id"] == "doc-scene"
    assert result["fs_path"] == "project/scene.mp4"
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert captured["generation"]["duration_seconds"] == 4.5
    assert captured["bound_document_id"] == "doc-scene"


@pytest.mark.asyncio
async def test_still_to_video_rejects_non_image_input(monkeypatch, tmp_path):
    source = tmp_path / "project" / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = json.loads(await media_tools._still_to_video_handler(
        entity_id="entity-1",
        input_path="project/notes.txt",
        output_name="project/scene.mp4",
    ))

    assert result["status"] == "error"
    assert result["code"] == "still_to_video_failed"
    assert "Unsupported image extension" in result["error"]


def test_frame_sample_times_merge_explicit_boundaries_intervals_and_end_frame():
    samples = media_tools._frame_sample_times(
        duration_seconds=20,
        timestamps=[3, 3.0004, 25, -1],
        scene_boundaries=[7],
        interval_seconds=5,
        max_samples=24,
    )

    assert samples == [0.0, 3.0, 5.0, 7.0, 10.0, 15.0, 19.9]


def test_frame_sample_times_preserve_first_last_and_cap():
    samples = media_tools._frame_sample_times(
        duration_seconds=100,
        timestamps=[],
        scene_boundaries=[],
        interval_seconds=5,
        max_samples=4,
    )

    assert len(samples) == 4
    assert samples[0] == 0.0
    assert samples[-1] == 99.9
    assert samples == sorted(samples)


@pytest.mark.asyncio
async def test_render_frame_samples_registers_ordered_durable_frames(monkeypatch, tmp_path):
    source = tmp_path / "project" / "final.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    rendered_args = []
    registered = []
    bound = []

    async def fake_probe(ffprobe, path):
        assert ffprobe == "/usr/bin/ffprobe"
        assert path == str(source)
        return {
            "decodable": True,
            "duration_seconds": 10.0,
            "has_video": True,
            "has_audio": True,
            "video_stream": {"duration_seconds": 9.5},
        }

    async def fake_target(**kwargs):
        rel_path = kwargs["output_name"]
        abs_path = tmp_path / rel_path
        return SimpleNamespace(
            abs_dir=str(abs_path.parent),
            abs_path=str(abs_path),
            rel_path=rel_path,
            filename=abs_path.name,
        )

    async def fake_run_process(args, **_kwargs):
        rendered_args.append(args)
        output = Path(args[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"png")
        return "", ""

    async def fake_register(**kwargs):
        registered.append(kwargs)
        return f"doc-{len(registered)}"

    async def fake_bind(**kwargs):
        bound.append(kwargs["document_id"])

    async def fake_workspace_media_base_dir(**_kwargs):
        return ""

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media_report", fake_probe)
    monkeypatch.setattr(media_tools, "_build_media_target", fake_target)
    monkeypatch.setattr(
        media_tools,
        "_workspace_media_base_dir",
        fake_workspace_media_base_dir,
    )
    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)
    monkeypatch.setattr(media_tools, "_register_file_artifact", fake_register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", fake_bind)

    result = json.loads(await media_tools._render_frame_samples_handler(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        input_path="project/final.mp4",
        output_dir="project/qa/frames",
        timestamps=[2],
        interval_seconds=5,
        max_samples=4,
    ))

    assert result["status"] == "completed"
    assert result["sample_count"] == 4
    assert [frame["timestamp_seconds"] for frame in result["frames"]] == [0.0, 2.0, 5.0, 9.4]
    assert [frame["document_id"] for frame in result["frames"]] == [
        "doc-1",
        "doc-2",
        "doc-3",
        "doc-4",
    ]
    assert bound == ["doc-1", "doc-2", "doc-3", "doc-4"]
    assert len(rendered_args) == 4
    for index, args in enumerate(rendered_args):
        assert args[0] == "/usr/bin/ffmpeg"
        assert args[args.index("-ss") + 1] == f"{result['frames'][index]['timestamp_seconds']:.3f}"
        assert args[args.index("-frames:v") + 1] == "1"
        assert registered[index]["artifact_role"] == "qa_evidence"
        assert registered[index]["generation"]["timestamp_seconds"] == result["frames"][index]["timestamp_seconds"]


def test_parse_ebur128_uses_final_summary_values():
    stderr = """
    [Parsed_ebur128_0] I: -70.0 LUFS LRA: 0.0 LU
    Summary:
      Integrated loudness:
        I:         -16.2 LUFS
      Loudness range:
        LRA:         5.4 LU
      True peak:
        Peak:        0.2 dBFS
    """

    assert media_tools._parse_ebur128(stderr) == {
        "integrated_lufs": -16.2,
        "loudness_range_lu": 5.4,
        "true_peak_dbfs": 0.2,
    }


def test_parse_silence_intervals_closes_open_interval_at_media_end():
    stderr = """
    [silencedetect] silence_start: 0
    [silencedetect] silence_end: 1.2 | silence_duration: 1.2
    [silencedetect] silence_start: 5
    [silencedetect] silence_end: 7 | silence_duration: 2
    [silencedetect] silence_start: 9
    """

    analysis = media_tools._parse_silence_intervals(stderr, duration_seconds=10)

    assert analysis == {
        "intervals": [
            {"start": 0.0, "end": 1.2, "duration_seconds": 1.2},
            {"start": 5.0, "end": 7.0, "duration_seconds": 2.0},
            {"start": 9.0, "end": 10.0, "duration_seconds": 1.0},
        ],
        "total_silence_seconds": 4.2,
        "leading_silence_seconds": 1.2,
        "trailing_silence_seconds": 1.0,
        "silence_ratio": 0.42,
    }


@pytest.mark.asyncio
async def test_analyze_audio_reports_loudness_clipping_and_silence_findings(monkeypatch, tmp_path):
    source = tmp_path / "project" / "final.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")

    async def fake_probe(_ffprobe, _path):
        return {"decodable": True, "duration_seconds": 10.0, "has_video": True, "has_audio": True}

    async def fake_run_process(args, **_kwargs):
        filter_value = args[args.index("-af") + 1]
        if filter_value.startswith("ebur128"):
            return "", "Summary:\n I: -16.2 LUFS\n LRA: 5.4 LU\n Peak: 0.2 dBFS\n"
        assert filter_value == "silencedetect=noise=-50.0dB:d=0.500"
        return "", (
            "silence_start: 0\n"
            "silence_end: 1.2 | silence_duration: 1.2\n"
            "silence_start: 5\n"
            "silence_end: 7 | silence_duration: 2\n"
            "silence_start: 9\n"
        )

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media_report", fake_probe)
    monkeypatch.setattr(media_tools, "_run_process", fake_run_process)

    result = json.loads(await media_tools._analyze_audio_handler(
        entity_id="entity-1",
        input_path="project/final.mp4",
    ))

    assert result["status"] == "completed"
    assert result["verdict"] == "fail"
    assert result["non_empty"] is True
    assert result["integrated_lufs"] == -16.2
    assert result["loudness_range_lu"] == 5.4
    assert result["true_peak_dbfs"] == 0.2
    assert result["clipping_detected"] is True
    assert result["silence_ratio"] == 0.42
    assert {finding["code"] for finding in result["findings"]} == {
        "true_peak_exceeded",
        "clipping_detected",
        "silence_ratio_exceeded",
    }


@pytest.mark.asyncio
async def test_analyze_audio_rejects_media_without_audio_stream(monkeypatch, tmp_path):
    source = tmp_path / "project" / "silent.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")

    async def fake_probe(_ffprobe, _path):
        return {"decodable": True, "duration_seconds": 10.0, "has_video": True, "has_audio": False}

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media_report", fake_probe)

    result = json.loads(await media_tools._analyze_audio_handler(
        entity_id="entity-1",
        input_path="project/silent.mp4",
    ))

    assert result["status"] == "error"
    assert result["code"] == "analyze_audio_failed"
    assert "no audio stream" in result["error"]


@pytest.mark.parametrize(
    ("subtitle_format", "content", "expected"),
    [
        (
            "srt",
            "1\n00:00:00,000 --> 00:00:02,000\nFirst line\nSecond line\n",
            {
                "id": "1",
                "start": 0.0,
                "end": 2.0,
                "text": "First line\nSecond line",
                "line_count": 2,
            },
        ),
        (
            "vtt",
            "WEBVTT\n\nintro\n00:00:01.500 --> 00:00:03.250\nOne line\n",
            {"id": "intro", "start": 1.5, "end": 3.25, "text": "One line", "line_count": 1},
        ),
        (
            "ass",
            (
                "[Script Info]\nPlayResY: 1080\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, Alignment, MarginL, MarginR, MarginV\n"
                "Style: CleanBottom,Arial,42,2,40,40,80\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:02.00,0:00:05.50,CleanBottom,,0,0,0,,First line\\NSecond line\n"
            ),
                {
                    "id": "1",
                    "start": 2.0,
                    "end": 5.5,
                "text": "First line\nSecond line",
                "line_count": 2,
                "style": "CleanBottom",
            },
        ),
    ],
)
def test_parse_subtitle_content_supports_srt_vtt_and_ass(
    subtitle_format,
    content,
    expected,
):
    parsed = media_tools._parse_subtitle_content(content, subtitle_format)

    assert len(parsed["cues"]) == 1
    for key, value in expected.items():
        assert parsed["cues"][0][key] == value
    if subtitle_format == "ass":
        assert parsed["play_res_y"] == 1080
        assert parsed["styles"]["CleanBottom"]["alignment"] == 2
        assert parsed["styles"]["CleanBottom"]["margin_v"] == 80


@pytest.mark.asyncio
async def test_validate_subtitles_reports_timing_overlap_bounds_and_line_findings(
    monkeypatch,
    tmp_path,
):
    subtitle = tmp_path / "project" / "bad.srt"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        (
            "1\n00:00:00,000 --> 00:00:04,000\nOne\nTwo\nThree\n\n"
            "2\n00:00:03,500 --> 00:00:06,000\nOverlap\n\n"
            "3\n00:00:09,000 --> 00:00:11,000\nPast the end\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity-1",
        subtitle_path="project/bad.srt",
        media_duration_seconds=10,
        max_lines=2,
    ))

    assert result["status"] == "completed"
    assert result["verdict"] == "fail"
    assert result["cue_count"] == 3
    assert result["maximum_line_count"] == 3
    assert {finding["code"] for finding in result["findings"]} == {
        "too_many_lines",
        "cue_overlap",
        "cue_outside_media",
    }


@pytest.mark.asyncio
async def test_validate_subtitles_passes_bottom_safe_ass_style(monkeypatch, tmp_path):
    subtitle = tmp_path / "project" / "final.ass"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        (
            "[Script Info]\nPlayResY: 1080\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            "Style: CleanBottom,Arial,20,1,0,2,40,40,80\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,CleanBottom,,0,0,0,,First line\n"
            "Dialogue: 0,0:00:02.00,0:00:04.00,CleanBottom,,0,0,0,,Second line\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity-1",
        subtitle_path="project/final.ass",
        media_duration_seconds=4,
        max_lines=2,
        min_margin_v=28,
    ))

    assert result["verdict"] == "pass"
    assert result["findings"] == []
    assert result["style_evidence"]["CleanBottom"] == {
        "font_size": 20,
        "outline": 1,
        "shadow": 0,
        "alignment": 2,
        "margin_v": 80,
        "bottom_safe": True,
    }


@pytest.mark.asyncio
async def test_validate_subtitles_requires_media_duration_or_media_path(monkeypatch, tmp_path):
    subtitle = tmp_path / "project" / "final.srt"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nLine\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity-1",
        subtitle_path="project/final.srt",
    ))

    assert result["status"] == "error"
    assert result["code"] == "validate_subtitles_failed"
    assert "media_path or media_duration_seconds is required" in result["error"]


@pytest.mark.asyncio
async def test_validate_subtitles_reports_blank_non_positive_and_unsafe_ass_styles(
    monkeypatch,
    tmp_path,
):
    subtitle = tmp_path / "project" / "unsafe.ass"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        (
            "[Script Info]\nPlayResY: 1080\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, Alignment, MarginL, MarginR, MarginV\n"
            "Style: Unsafe,Arial,42,7,40,40,10\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:01.00,Unsafe,,0,0,0,,\n"
            "Dialogue: 0,0:00:02.00,0:00:03.00,Missing,,0,0,0,,Visible\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity-1",
        subtitle_path="project/unsafe.ass",
        media_duration_seconds=4,
        min_margin_v=28,
    ))

    assert result["verdict"] == "fail"
    assert {finding["code"] for finding in result["findings"]} == {
        "blank_cue",
        "non_positive_duration",
        "ass_style_not_bottom_safe",
        "ass_style_missing",
    }
    assert result["style_evidence"]["Unsafe"] == {
        "font_size": 42,
        "outline": None,
        "shadow": None,
        "alignment": 7,
        "margin_v": 10,
        "bottom_safe": False,
    }


@pytest.mark.asyncio
async def test_validate_subtitles_probes_media_duration(monkeypatch, tmp_path):
    subtitle = tmp_path / "project" / "final.vtt"
    media = tmp_path / "project" / "final.mp4"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nLine\n",
        encoding="utf-8",
    )
    media.write_bytes(b"video")
    captured = {}

    async def fake_probe(ffprobe, path):
        captured.update(ffprobe=ffprobe, path=path)
        return {"decodable": True, "duration_seconds": 2.0, "has_video": True}

    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_tools, "_probe_media_report", fake_probe)

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity-1",
        subtitle_path="project/final.vtt",
        media_path="project/final.mp4",
    ))

    assert result["verdict"] == "pass"
    assert result["media_duration_seconds"] == 2.0
    assert captured == {"ffprobe": "/usr/bin/ffprobe", "path": str(media)}


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
