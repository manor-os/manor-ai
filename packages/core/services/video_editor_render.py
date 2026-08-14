"""Stable finalization for browser-authored video editor previews.

The web editor owns interactive composition and emits a short-lived WebM
preview.  This module turns that preview into a delivery-safe, constant-frame
rate MP4 using the FFmpeg runtime that Manor already ships for media tools.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
from dataclasses import dataclass
from typing import Any, Sequence


class VideoEditorRenderError(RuntimeError):
    """Raised when a video editor preview cannot be finalized safely."""


@dataclass(frozen=True)
class VideoEditorRenderResult:
    duration_seconds: float
    width: int
    height: int
    fps: float
    file_size: int
    has_audio: bool
    video_codec: str
    audio_codec: str | None


async def _run_process(
    args: Sequence[str],
    *,
    timeout_seconds: float,
) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise VideoEditorRenderError(f"Required media executable is unavailable: {args[0]}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise VideoEditorRenderError("Video finalization timed out") from exc

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        detail = stderr_text.strip() or stdout_text.strip() or "media process failed"
        raise VideoEditorRenderError(detail[-1200:])
    return stdout_text, stderr_text


def _ratio(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" not in text:
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0
    numerator, denominator = text.split("/", 1)
    try:
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _duration_seconds(probe: dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _atempo_filters(factor: float) -> list[str]:
    """Split an audio tempo factor into FFmpeg's supported 0.5–2.0 range."""
    if not math.isfinite(factor) or factor <= 0:
        return []
    filters: list[str] = []
    while factor < 0.5:
        filters.append("atempo=0.5")
        factor /= 0.5
    while factor > 2.0:
        filters.append("atempo=2")
        factor /= 2.0
    if abs(factor - 1.0) > 0.000001:
        filters.append(f"atempo={factor:.12f}")
    return filters


async def _probe_media(ffprobe: str, path: str) -> dict[str, Any]:
    stdout, _stderr = await _run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            path,
        ],
        timeout_seconds=60,
    )
    try:
        return json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoEditorRenderError("ffprobe returned invalid video metadata") from exc


def _render_result(probe: dict[str, Any], path: str) -> VideoEditorRenderResult:
    streams = [item for item in probe.get("streams") or [] if isinstance(item, dict)]
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise VideoEditorRenderError("Finalized file has no video stream")
    try:
        duration = float((probe.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise VideoEditorRenderError("Finalized file has no measurable duration")
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    if size <= 0:
        raise VideoEditorRenderError("Finalized file is empty")
    return VideoEditorRenderResult(
        duration_seconds=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_ratio(video.get("avg_frame_rate")),
        file_size=size,
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or "") if audio else None,
    )


async def finalize_video_editor_preview(
    input_path: str,
    output_path: str,
    *,
    fps: int = 30,
    crf: int = 18,
    preset: str = "veryfast",
    target_duration_seconds: float | None = None,
) -> VideoEditorRenderResult:
    """Normalize a browser preview into a constant-frame-rate H.264/AAC MP4."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VideoEditorRenderError("ffmpeg and ffprobe are required for final MP4 rendering")
    if not os.path.isfile(input_path) or os.path.getsize(input_path) <= 0:
        raise VideoEditorRenderError("Browser preview is missing or empty")

    input_probe = await _probe_media(ffprobe, input_path)
    input_streams = [item for item in input_probe.get("streams") or [] if isinstance(item, dict)]
    if not any(item.get("codec_type") == "video" for item in input_streams):
        raise VideoEditorRenderError("Browser preview has no video stream")
    has_audio = any(item.get("codec_type") == "audio" for item in input_streams)
    input_duration = _duration_seconds(input_probe)
    target_duration = 0.0
    if target_duration_seconds is not None:
        try:
            candidate_duration = float(target_duration_seconds)
        except (TypeError, ValueError):
            candidate_duration = 0.0
        if math.isfinite(candidate_duration) and 0.05 <= candidate_duration <= 21600:
            target_duration = candidate_duration
    stretch = target_duration / input_duration if target_duration > 0 and input_duration > 0 else 1.0

    safe_fps = min(60, max(12, int(fps or 30)))
    safe_crf = min(28, max(14, int(crf or 18)))
    allowed_presets = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}
    safe_preset = preset if preset in allowed_presets else "veryfast"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
    ]
    if has_audio:
        args.extend(["-map", "0:a:0?"])
    video_filters: list[str] = []
    if abs(stretch - 1.0) > 0.001:
        video_filters.append(f"setpts={stretch:.12f}*PTS")
    video_filters.append(f"fps={safe_fps}")
    if target_duration > 0:
        # A stretched source can end one frame before the requested boundary
        # because its final packet carries no following duration.  Pad first,
        # then let ``-t`` cut on the exact delivery boundary instead of letting
        # ``-shortest`` silently shorten an authored composition.
        video_filters.append(
            f"tpad=stop_mode=clone:stop_duration={max(1.0, target_duration):.6f}"
        )
    video_filters.extend(["scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"])
    args.extend(
        [
            "-vf",
            ",".join(video_filters),
            "-fps_mode",
            "cfr",
            "-c:v",
            "libx264",
            "-preset",
            safe_preset,
            "-crf",
            str(safe_crf),
        ]
    )
    if has_audio:
        audio_filters = _atempo_filters(1.0 / stretch)
        audio_filters.extend(
            ["aresample=48000:async=1:first_pts=0", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        )
        if target_duration > 0:
            audio_filters.append(f"apad=pad_dur={max(1.0, target_duration):.6f}")
        args.extend(
            [
                "-af",
                ",".join(audio_filters),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        )
    if target_duration > 0:
        args.extend(["-t", f"{target_duration:.6f}"])
    args.extend(["-movflags", "+faststart"])
    if target_duration <= 0:
        args.append("-shortest")
    args.append(output_path)

    timeout = max(180.0, (os.path.getsize(input_path) / (1024 * 1024)) * 6.0 + 120.0)
    await _run_process(args, timeout_seconds=timeout)
    await _run_process(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", output_path, "-f", "null", "-"],
        timeout_seconds=timeout,
    )
    return _render_result(await _probe_media(ffprobe, output_path), output_path)
