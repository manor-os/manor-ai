"""Subtitle snapshot validation shares the ordinary media tool's findings."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.tools import media_tools


def _ass(*, alignment=2, margin_v=28, style="Default", text="One\\NTwo"):
    return (
        "[Script Info]\nPlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, Outline, Shadow, Alignment, MarginV\n"
        f"Style: Default,Arial,20,1,0,{alignment},{margin_v}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:02.00,{style},{text}\n"
    )


@pytest.mark.parametrize("subtitle_format, content", [
    ("srt", "1\n00:00:00,000 --> 00:00:02,000\nOne\nTwo\n"),
    ("vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nOne\nTwo\n"),
    ("ass", _ass()),
])
@pytest.mark.parametrize("probe_duration", [False, True])
async def test_content_and_file_handler_reports_match(
    tmp_path, monkeypatch, subtitle_format, content, probe_duration,
):
    relative_path = f"project/final.{subtitle_format}"
    subtitle = tmp_path / relative_path
    subtitle.parent.mkdir()
    subtitle.write_text(content, encoding="utf-8-sig")
    media = subtitle.parent / "final.mp4"
    media.write_bytes(b"Mocked media duration, no decoder is invoked.")
    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity: str(tmp_path))
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: "/mock/ffprobe")
    probe = AsyncMock(return_value={"duration_seconds": 2.0})
    monkeypatch.setattr(media_tools, "_probe_media_report", probe)

    result = json.loads(await media_tools._validate_subtitles_handler(
        entity_id="entity", subtitle_path=relative_path,
        media_path="project/final.mp4" if probe_duration else "",
        media_duration_seconds=None if probe_duration else 2.0,
        max_lines=2, min_margin_v=28,
    ))

    assert result.pop("fs_path") == relative_path
    assert result.pop("media_path") == ("project/final.mp4" if probe_duration else None)
    assert result == media_tools._validate_subtitle_content(
        content, subtitle_format, 2.0, max_lines=2, min_margin_v=28,
    )
    assert result["verdict"] == "pass"
    if probe_duration:
        probe.assert_awaited_once_with("/mock/ffprobe", str(media))
    else:
        probe.assert_not_awaited()


def test_content_validation_does_not_reopen_files(monkeypatch):
    def unexpected_read(*_args, **_kwargs):
        pytest.fail("Content validation must not reopen any path")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    result = media_tools._validate_subtitle_content(
        "1\n00:00:00,000 --> 00:00:01,000\nFrozen subtitle bytes\n", "srt", 1,
    )
    assert result["verdict"] == "pass"
    assert "fs_path" not in result
    assert "media_path" not in result


def test_content_validation_keeps_all_timing_and_line_findings():
    result = media_tools._validate_subtitle_content(
        "1\n00:00:00,000 --> 00:00:04,000\nOne\nTwo\nThree\n\n"
        "2\n00:00:03,500 --> 00:00:06,000\nOverlap\n\n"
        "3\n00:00:09,000 --> 00:00:11,000\nPast the end\n\n"
        "4\n00:00:08,000 --> 00:00:08,000\n\n",
        "srt", 10,
    )
    assert result["verdict"] == "fail"
    assert result["cue_count"] == 4
    assert result["maximum_line_count"] == 3
    assert result["timing_bounds"] == {"start_seconds": 0.0, "end_seconds": 11.0}
    assert result["findings"] == [
        {"code": "too_many_lines", "cue_id": "1", "line_count": 3, "maximum_lines": 2},
        {"code": "cue_outside_media", "cue_id": "3", "start": 9.0, "end": 11.0,
         "media_duration_seconds": 10.0},
        {"code": "blank_cue", "cue_id": "4"},
        {"code": "non_positive_duration", "cue_id": "4", "start": 8.0, "end": 8.0},
        {"code": "cue_overlap", "cue_id": "2", "overlaps_cue_id": "1"},
    ]


@pytest.mark.parametrize("duration", [0, -1])
def test_content_validation_requires_positive_duration(duration):
    with pytest.raises(ValueError, match="positive media duration"):
        media_tools._validate_subtitle_content(
            "1\n00:00:00,000 --> 00:00:01,000\nLine\n", "srt", duration,
        )


@pytest.mark.parametrize("alignment, margin_v, expected_safe", [
    (2, 27, False), (2, 28, True), (2, 29, True), (7, 28, False),
])
def test_ass_bottom_safe_boundary(alignment, margin_v, expected_safe):
    result = media_tools._validate_subtitle_content(
        _ass(alignment=alignment, margin_v=margin_v), "ass", 2,
    )
    assert result["play_res_y"] == 1080
    assert result["style_evidence"]["Default"] == {
        "font_size": 20.0, "outline": 1.0, "shadow": 0.0,
        "alignment": alignment, "margin_v": margin_v, "bottom_safe": expected_safe,
    }
    assert result["findings"] == ([] if expected_safe else [{
        "code": "ass_style_not_bottom_safe", "style": "Default",
        "required_alignment": 2, "minimum_margin_v": 28,
    }])


def test_ass_missing_style_and_excess_lines_are_not_accepted():
    result = media_tools._validate_subtitle_content(
        _ass(style="Missing", text="One\\NTwo\\NThree"), "ass", 2,
    )
    assert result["verdict"] == "fail"
    assert result["findings"] == [
        {"code": "too_many_lines", "cue_id": "1", "line_count": 3, "maximum_lines": 2},
        {"code": "ass_style_missing", "style": "Missing"},
    ]


def test_content_validation_preserves_empty_and_invalid_subtitle_behavior():
    empty = media_tools._validate_subtitle_content("", "srt", 2)
    assert empty["verdict"] == "fail"
    assert empty["findings"] == [{"code": "no_subtitle_cues"}]
    with pytest.raises(ValueError, match="Invalid subtitle timestamp"):
        media_tools._validate_subtitle_content("1\ninvalid --> 00:00:01,000\nText\n", "srt", 2)
    with pytest.raises(ValueError, match="Unsupported subtitle format"):
        media_tools._validate_subtitle_content("", "txt", 2)
