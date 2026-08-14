from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from packages.core.services.video_editor_render import finalize_video_editor_preview


def test_finalize_video_editor_preview_outputs_cfr_h264_aac_mp4(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe are unavailable")

    preview = tmp_path / "preview.mkv"
    final = tmp_path / "final.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#102033:s=640x360:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-shortest",
            str(preview),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if generated.returncode != 0:
        pytest.skip(f"ffmpeg fixture generation unavailable: {generated.stderr[-240:]}")

    result = asyncio.run(
        finalize_video_editor_preview(
            str(preview),
            str(final),
            target_duration_seconds=1.5,
        )
    )

    assert final.is_file()
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.has_audio is True
    assert result.width == 640
    assert result.height == 360
    assert result.fps == pytest.approx(30, abs=0.01)
    assert result.duration_seconds == pytest.approx(1.5, abs=(1 / 30) + 0.002)
    assert result.file_size > 0

    decoded = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(final), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert decoded.returncode == 0
    assert decoded.stderr.strip() == ""
