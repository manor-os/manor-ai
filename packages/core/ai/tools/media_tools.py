"""Media orchestration tools for generated assets and video assembly."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select

logger = logging.getLogger(__name__)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
AUDIO_TIMELINE_TYPES = {
    "audio",
    "dialogue",
    "narration",
    "music",
    "ambience",
    "soundscape",
    "sfx",
    "foley",
    "transition",
}
TERMINAL_JOB_STATUSES = {"completed", "failed"}
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MAX_WAIT_SECONDS = 900.0


WAIT_MEDIA_JOBS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait_media_jobs",
        "description": (
            "Poll generated media jobs until they complete, fail, or time out. "
            "Use after generate_file(kind='video') returns pending job_ids before "
            "attempting to merge or reference the generated videos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Media job IDs returned by generate_file.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "Optional maximum total seconds to wait. If omitted, "
                        "uses an adaptive budget based on the requested jobs, capped at 900."
                    ),
                },
                "poll_interval_seconds": {
                    "type": "number",
                    "description": "Seconds between status checks. Defaults to 5.",
                    "default": 5,
                },
            },
            "required": ["job_ids"],
        },
    },
}


MERGE_VIDEOS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "merge_videos",
        "description": (
            "Merge or normalize one or more Knowledge videos into one final MP4. Accepts completed "
            "media job IDs, document IDs, explicit Knowledge video paths, or all "
            "videos in a folder. The service normalizes resolution/FPS and, by "
            "default, replaces source/provider audio with silence for clean picture "
            "masters before registering the merged video back into Knowledge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Completed video MediaJob IDs to merge in order.",
                },
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Knowledge document IDs for video files to merge in order.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Knowledge-relative video paths or /api/v1/fs URLs to merge in order.",
                },
                "video_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for paths. Knowledge-relative video paths or /api/v1/fs URLs to merge in order.",
                },
                "folder_path": {
                    "type": "string",
                    "description": "Optional Knowledge folder path. Video files inside it are merged by filename.",
                },
                "output_name": {
                    "type": "string",
                    "description": (
                        "Output Knowledge path or filename, for example "
                        "'打工猫AI漫剧/final/打工猫的一天.mp4'."
                    ),
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p", "1080p"],
                    "default": "1080p",
                    "description": "Target output resolution tier.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                    "default": "16:9",
                    "description": "Target output aspect ratio.",
                },
                "fps": {
                    "type": "integer",
                    "minimum": 12,
                    "maximum": 60,
                    "default": 30,
                    "description": "Target frames per second.",
                },
                "crf": {
                    "type": "integer",
                    "minimum": 14,
                    "maximum": 28,
                    "default": 18,
                    "description": "H.264 CRF. Lower is larger/higher quality.",
                },
                "preset": {
                    "type": "string",
                    "enum": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
                    "default": "veryfast",
                    "description": "ffmpeg x264 encoding preset.",
                },
                "include_source_audio": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Preserve original audio from source clips. Keep false for clean picture "
                        "masters because video providers may include unwanted music/SFX even when "
                        "generate_audio=false."
                    ),
                },
            },
            "required": ["output_name"],
        },
    },
}


COMPOSE_VIDEO_TIMELINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "compose_video_timeline",
        "description": (
            "Render a post-production MP4 from a clean picture master and a "
            "timeline JSON. Supports timed dialogue/narration/music/ambience/"
            "SFX audio tracks, optional looping/fades/volume, and optional SRT/"
            "VTT subtitle burn-in. Registers the composed video back into "
            "Knowledge and writes a same-folder .video-edit.json sidecar so "
            "the final MP4 opens in Video Editor with editable clips, shots, "
            "captions, and audio cues instead of a flat render only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeline_path": {
                    "type": "string",
                    "description": "Knowledge-relative path or /api/v1/fs URL for the timeline JSON.",
                },
                "clean_video_path": {
                    "type": "string",
                    "description": (
                        "Optional Knowledge-relative path or /api/v1/fs URL for the clean picture master. "
                        "If omitted, the tool reads delivery.clean_picture_master from the timeline."
                    ),
                },
                "output_name": {
                    "type": "string",
                    "description": "Output Knowledge path or filename for the mixed/subtitled MP4.",
                },
                "subtitle_path": {
                    "type": "string",
                    "description": (
                        "Optional Knowledge-relative SRT/VTT subtitle path. If omitted, "
                        "uses timeline.subtitles.srt_path, subtitle_path, or path when present."
                    ),
                },
                "burn_subtitles": {
                    "type": "boolean",
                    "default": True,
                    "description": "Burn subtitles into the video when a subtitle file is present.",
                },
                "subtitle_style": {
                    "type": "object",
                    "description": (
                        "Optional libass style for burned subtitles: font_name, font_size, "
                        "primary_color, outline_color, back_color, alignment, margin_v, "
                        "outline, shadow, bold."
                    ),
                },
                "include_audio": {
                    "type": "boolean",
                    "default": True,
                    "description": "Mix timeline audio tracks into the output.",
                },
                "include_source_audio": {
                    "type": "boolean",
                    "default": False,
                    "description": "Keep the clean video's existing audio under generated tracks.",
                },
                "ducking": {
                    "type": "object",
                    "description": (
                        "Optional dialogue ducking config. Example: "
                        "{enabled:true, amount_db:-9, padding:0.15, "
                        "target_types:['music','ambience'], sidechain_types:['dialogue','narration']}."
                    ),
                },
                "loudness_normalization": {
                    "type": "object",
                    "description": (
                        "Optional final mix loudnorm config: enabled, target_lufs, true_peak, lra."
                    ),
                },
                "crf": {
                    "type": "integer",
                    "minimum": 14,
                    "maximum": 28,
                    "default": 18,
                    "description": "H.264 CRF. Lower is larger/higher quality.",
                },
                "preset": {
                    "type": "string",
                    "enum": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
                    "default": "veryfast",
                    "description": "ffmpeg x264 encoding preset.",
                },
            },
            "required": ["timeline_path", "output_name"],
        },
    },
}


@dataclass(frozen=True)
class VideoInput:
    """Resolved video input inside the entity filesystem."""

    source_type: str
    source_id: str | None
    rel_path: str
    abs_path: str
    document_id: str | None = None


@dataclass(frozen=True)
class TimelineAudioTrack:
    """Resolved audio track scheduled on the final timeline."""

    track_id: str
    track_type: str
    rel_path: str
    abs_path: str
    start: float
    end: float
    volume_db: float
    loop: bool
    fade_in: float
    fade_out: float
    duration: float


@dataclass(frozen=True)
class SubtitleCue:
    """Text cue resolved onto the final picture timeline."""

    index: int
    start: float
    end: float
    text: str
    cue_type: str
    source_path: str = ""
    estimated: bool = False


ALIGN_SUBTITLES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "align_subtitles",
        "description": (
            "Create timed SRT/VTT/ASS subtitles from timeline or cue JSON. "
            "Uses cue start/end times and can derive cue end from referenced "
            "dialogue audio duration with ffprobe."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeline_path": {"type": "string", "description": "Timeline JSON path or /api/v1/fs URL."},
                "cues_path": {"type": "string", "description": "Dialogue/subtitle cue JSON path or /api/v1/fs URL."},
                "output_name": {"type": "string", "description": "Output subtitle path/filename."},
                "format": {"type": "string", "enum": ["srt", "vtt", "ass"], "default": "srt"},
                "track_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cue types to include; defaults to dialogue and narration.",
                },
                "max_chars_per_line": {"type": "integer", "minimum": 16, "maximum": 56, "default": 34},
                "style": {"type": "object", "description": "ASS/libass style: font_name, font_size, colors, alignment, outline, shadow, margin_v."},
            },
            "required": ["output_name"],
        },
    },
}


NORMALIZE_AUDIO_LOUDNESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "normalize_audio_loudness",
        "description": "Render a loudness-normalized audio asset with ffmpeg loudnorm and register it in Knowledge.",
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "Knowledge audio path or /api/v1/fs URL."},
                "output_name": {"type": "string", "description": "Output audio path/filename."},
                "target_lufs": {"type": "number", "default": -16},
                "true_peak": {"type": "number", "default": -1.5},
                "lra": {"type": "number", "default": 11},
                "output_format": {"type": "string", "enum": ["wav", "mp3", "m4a", "flac"], "default": "wav"},
            },
            "required": ["input_path", "output_name"],
        },
    },
}


async def _wait_media_jobs_handler(
    *,
    entity_id: str = "",
    job_ids: list[str] | str | None = None,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    ids = _string_list(job_ids)
    if not ids:
        return _json_error("job_ids is required")

    timeout = (
        _clamp_float(timeout_seconds, 0.0, MAX_WAIT_SECONDS, MAX_WAIT_SECONDS)
        if timeout_seconds is not None
        else None
    )
    interval = _clamp_float(
        poll_interval_seconds,
        1.0,
        30.0,
        DEFAULT_POLL_INTERVAL_SECONDS,
    )
    deadline: float | None = None
    timed_out = False

    jobs: list[Any] = []
    missing: list[str] = []
    while True:
        jobs, missing = await _load_media_jobs(entity_id, ids)
        if timeout is None:
            timeout = _default_wait_timeout_seconds(jobs)
        if deadline is None:
            deadline = time.monotonic() + timeout
        statuses = {getattr(job, "status", "") for job in jobs}
        # A bad/hallucinated id should not make real media jobs fail early.
        # Wait for the jobs that do exist, and return missing ids as a warning.
        done = bool(jobs) and statuses.issubset(TERMINAL_JOB_STATUSES)
        timed_out = time.monotonic() >= deadline
        if (missing and not jobs) or done or timed_out or timeout <= 0:
            break
        await asyncio.sleep(interval)

    job_payloads = await _jobs_to_payload(entity_id, jobs)
    pending_job_ids = [
        str(item.get("job_id"))
        for item in job_payloads
        if item.get("job_id") and item.get("status") not in TERMINAL_JOB_STATUSES
    ]
    failed_job_ids = [
        str(item.get("job_id"))
        for item in job_payloads
        if item.get("job_id") and item.get("status") == "failed"
    ]
    if failed_job_ids:
        status = "failed"
    elif pending_job_ids:
        status = "pending"
    elif len(job_payloads) == len(ids) and all(item.get("status") == "completed" for item in job_payloads):
        status = "completed"
    elif job_payloads and all(item.get("status") == "completed" for item in job_payloads):
        # Partial input issue: existing jobs completed, but at least one
        # requested id was missing. Surface the warning without hiding success.
        status = "completed"
    elif missing:
        status = "failed"
    else:
        status = "pending"

    return _json(
        {
            "kind": "media_jobs",
            "status": status,
            "jobs": job_payloads,
            "missing_job_ids": missing,
            "pending_job_ids": pending_job_ids,
            "failed_job_ids": failed_job_ids,
            "completed_count": sum(1 for item in job_payloads if item.get("status") == "completed"),
            "total_count": len(ids),
            "timed_out": bool(timed_out and status == "pending"),
            "timeout_seconds": timeout,
        }
    )


async def _merge_videos_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    job_ids: list[str] | str | None = None,
    document_ids: list[str] | str | None = None,
    paths: list[str] | str | None = None,
    video_paths: list[str] | str | None = None,
    folder_path: str | None = None,
    output_name: str = "",
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    fps: int = 30,
    crf: int = 18,
    preset: str = "veryfast",
    include_source_audio: bool = False,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not (output_name or "").strip():
        return _json_error("output_name is required")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return _json_error(
            "ffmpeg/ffprobe is not installed in the API/worker runtime. "
            "Install ffmpeg in docker/Dockerfile.api before using merge_videos.",
            code="ffmpeg_missing",
        )

    try:
        target_width, target_height = _target_dimensions(resolution, aspect_ratio)
        target_fps = int(_clamp_float(fps, 12, 60, 30))
        target_crf = int(_clamp_float(crf, 14, 28, 18))
        allowed_presets = MERGE_VIDEOS_SCHEMA["function"]["parameters"]["properties"]["preset"]["enum"]
        target_preset = preset if preset in allowed_presets else "veryfast"

        inputs = await _resolve_video_inputs(
            entity_id=entity_id,
            job_ids=_string_list(job_ids),
            document_ids=_string_list(document_ids),
            paths=[*_string_list(paths), *_string_list(video_paths)],
            folder_path=folder_path,
        )
        if not inputs:
            return _json_error("At least one video input is required to merge or normalize.")

        final_path, final_rel_path, final_filename, input_payloads, total_duration = await _merge_video_files(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            entity_id=entity_id,
            output_name=output_name,
            workspace_id=workspace_id,
            inputs=inputs,
            width=target_width,
            height=target_height,
            fps=target_fps,
            crf=target_crf,
            preset=target_preset,
            include_source_audio=bool(include_source_audio),
        )

        document_id = await _register_merged_video(
            entity_id=entity_id,
            user_id=user_id,
            filename=final_filename,
            rel_path=final_rel_path,
            file_size=os.path.getsize(final_path),
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            inputs=input_payloads,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            fps=target_fps,
            crf=target_crf,
            preset=target_preset,
            include_source_audio=bool(include_source_audio),
        )

        if workspace_id and document_id:
            from packages.core.services.knowledge_sync import bind_document_to_workspace

            await bind_document_to_workspace(
                entity_id=entity_id,
                document_id=document_id,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name="merge_videos",
            )

        return _json(
            {
                "kind": "video",
                "status": "completed",
                "document_id": document_id,
                "name": final_filename,
                "result_url": f"/api/v1/fs/{entity_id}/{final_rel_path}",
                "fs_path": final_rel_path,
                "file_size": os.path.getsize(final_path),
                "duration_seconds": round(total_duration, 2),
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "fps": target_fps,
                "include_source_audio": bool(include_source_audio),
                "source_audio_stripped": bool(
                    not include_source_audio
                    and any(item.get("has_audio") for item in input_payloads)
                ),
                "inputs": input_payloads,
            }
        )
    except Exception as exc:  # noqa: BLE001 - tool results should be structured
        logger.exception("merge_videos failed")
        return _json_error(str(exc), code="merge_failed")


async def _compose_video_timeline_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    timeline_path: str = "",
    clean_video_path: str = "",
    output_name: str = "",
    subtitle_path: str = "",
    burn_subtitles: bool = True,
    subtitle_style: dict[str, Any] | None = None,
    include_audio: bool = True,
    include_source_audio: bool = False,
    ducking: dict[str, Any] | bool | None = None,
    loudness_normalization: dict[str, Any] | bool | None = None,
    crf: int = 18,
    preset: str = "veryfast",
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not (timeline_path or "").strip():
        return _json_error("timeline_path is required")
    if not (output_name or "").strip():
        return _json_error("output_name is required")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return _json_error(
            "ffmpeg/ffprobe is not installed in the API/worker runtime. "
            "Install ffmpeg in docker/Dockerfile.api before using compose_video_timeline.",
            code="ffmpeg_missing",
        )

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        timeline = _load_timeline_json(entity_root, timeline_path, entity_id)
        clean_rel = _timeline_clean_video_path(timeline, clean_video_path)
        if not clean_rel:
            return _json_error(
                "clean_video_path is required when timeline.delivery.clean_picture_master is missing",
                code="clean_video_missing",
            )
        clean_abs = _resolve_entity_file(entity_root, clean_rel)
        _assert_video_path(clean_abs)

        resolved_subtitle = _timeline_subtitle_path(timeline, subtitle_path)
        subtitle_abs = ""
        subtitle_rel = ""
        if burn_subtitles and resolved_subtitle:
            subtitle_rel = _rel_path_from_reference(resolved_subtitle, entity_id) or resolved_subtitle
            subtitle_abs = _resolve_entity_file(entity_root, subtitle_rel)
            _assert_subtitle_path(subtitle_abs)
        resolved_subtitle_style = _timeline_subtitle_style(timeline, subtitle_style)

        media_info = await _probe_media(ffprobe, clean_abs)
        total_duration = float(media_info.get("duration_seconds") or 0.0)
        audio_tracks = await _resolve_timeline_audio_tracks(
            ffprobe=ffprobe,
            entity_root=entity_root,
            entity_id=entity_id,
            timeline=timeline,
            enabled=bool(include_audio),
        )
        ducking_config = _timeline_ducking_config(timeline, ducking)
        loudness_config = _timeline_loudness_config(timeline, loudness_normalization)

        allowed_presets = MERGE_VIDEOS_SCHEMA["function"]["parameters"]["properties"]["preset"]["enum"]
        target_preset = preset if preset in allowed_presets else "veryfast"
        target_crf = int(_clamp_float(crf, 14, 28, 18))

        final_path, final_rel_path, final_filename = await _compose_video_file(
            ffmpeg=ffmpeg,
            entity_id=entity_id,
            output_name=output_name,
            workspace_id=workspace_id,
            clean_video_abs=clean_abs,
            subtitle_abs=subtitle_abs,
            subtitle_style=resolved_subtitle_style,
            audio_tracks=audio_tracks,
            include_source_audio=bool(include_source_audio and media_info.get("has_audio")),
            ducking_config=ducking_config,
            loudness_config=loudness_config,
            crf=target_crf,
            preset=target_preset,
            total_duration=total_duration,
        )

        audio_payloads = [
            {
                "id": track.track_id,
                "type": track.track_type,
                "fs_path": track.rel_path,
                "start": round(track.start, 3),
                "end": round(track.end, 3),
                "duration_seconds": round(track.duration, 3),
                "volume_db": track.volume_db,
                "loop": track.loop,
            }
            for track in audio_tracks
        ]
        document_id = await _register_merged_video(
            entity_id=entity_id,
            user_id=user_id,
            filename=final_filename,
            rel_path=final_rel_path,
            file_size=os.path.getsize(final_path),
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            inputs=[
                {
                    "source_type": "timeline",
                    "source_id": timeline_path,
                    "fs_path": _rel_path_from_reference(timeline_path, entity_id) or timeline_path,
                },
                {
                    "source_type": "clean_video",
                    "source_id": clean_video_path or clean_rel,
                    "fs_path": _normalize_user_path(clean_rel),
                    "duration_seconds": round(total_duration, 2),
                    "has_audio": bool(media_info.get("has_audio")),
                },
            ],
            resolution=str((timeline.get("spec") or {}).get("resolution") or ""),
            aspect_ratio=str((timeline.get("spec") or {}).get("aspect_ratio") or ""),
            fps=int(_clamp_float((timeline.get("spec") or {}).get("fps"), 12, 60, 30)),
            crf=target_crf,
            preset=target_preset,
            include_source_audio=bool(include_source_audio and media_info.get("has_audio")),
            operation="compose_video_timeline",
        )

        if workspace_id and document_id:
            from packages.core.services.knowledge_sync import bind_document_to_workspace

            await bind_document_to_workspace(
                entity_id=entity_id,
                document_id=document_id,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name="compose_video_timeline",
            )

        editor_recipe = await _create_video_editor_recipe_sidecar(
            entity_id=entity_id,
            user_id=user_id,
            timeline=timeline,
            timeline_path=timeline_path,
            clean_video_path=clean_rel,
            final_document_id=document_id,
            final_filename=final_filename,
            final_rel_path=final_rel_path,
            final_file_size=os.path.getsize(final_path),
            total_duration=total_duration,
            media_info=media_info,
            audio_tracks=audio_tracks,
            subtitle_path=subtitle_rel,
            subtitle_abs_path=subtitle_abs,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            crf=target_crf,
            preset=target_preset,
            include_source_audio=bool(include_source_audio and media_info.get("has_audio")),
        )
        await _attach_video_editor_recipe_to_video(
            entity_id=entity_id,
            final_document_id=document_id,
            editor_recipe=editor_recipe,
        )

        return _json(
            {
                "kind": "video",
                "status": "completed",
                "document_id": document_id,
                "name": final_filename,
                "result_url": f"/api/v1/fs/{entity_id}/{final_rel_path}",
                "fs_path": final_rel_path,
                "file_size": os.path.getsize(final_path),
                "duration_seconds": round(total_duration, 2),
                "audio_tracks": audio_payloads,
                "subtitle_path": subtitle_rel or None,
                "burn_subtitles": bool(subtitle_abs),
                "include_source_audio": bool(include_source_audio and media_info.get("has_audio")),
                "ducking": ducking_config if ducking_config.get("enabled") else None,
                "loudness_normalization": (
                    loudness_config if loudness_config.get("enabled") else None
                ),
                "editor_recipe": editor_recipe,
                "editor_recipe_document_id": editor_recipe.get("document_id") if editor_recipe else None,
                "editor_recipe_path": editor_recipe.get("fs_path") if editor_recipe else None,
            }
        )
    except Exception as exc:  # noqa: BLE001 - tool results should be structured
        logger.exception("compose_video_timeline failed")
        return _json_error(str(exc), code="compose_failed")


async def _align_subtitles_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    timeline_path: str = "",
    cues_path: str = "",
    output_name: str = "",
    format: str = "srt",
    track_types: list[str] | str | None = None,
    max_chars_per_line: int = 34,
    style: dict[str, Any] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not (output_name or "").strip():
        return _json_error("output_name is required")
    if not (timeline_path or cues_path or "").strip():
        return _json_error("timeline_path or cues_path is required")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        timeline: dict[str, Any] = {}
        cue_payloads: list[Any] = []
        if timeline_path:
            timeline = _load_timeline_json(entity_root, timeline_path, entity_id)
        if cues_path:
            cue_payloads.append(_load_entity_json(entity_root, cues_path, entity_id))

        ffprobe = shutil.which("ffprobe") or ""
        include_types = _subtitle_track_types(track_types)
        cues = await _collect_subtitle_cues(
            ffprobe=ffprobe,
            entity_root=entity_root,
            entity_id=entity_id,
            timeline=timeline,
            cue_payloads=cue_payloads,
            track_types=include_types,
        )
        if not cues:
            return _json_error("No subtitle cues with text and timing were found", code="no_subtitle_cues")

        subtitle_format = str(format or "srt").strip().lower()
        if subtitle_format not in {"srt", "vtt", "ass"}:
            subtitle_format = "srt"
        target = await _build_media_target(
            entity_id=entity_id,
            workspace_id=workspace_id,
            output_name=output_name,
            ext=f".{subtitle_format}",
            fallback="subtitles",
            default_dir="subtitles",
        )
        if not target.abs_dir or not target.abs_path:
            raise ValueError("Could not resolve subtitle output path")
        os.makedirs(target.abs_dir, exist_ok=True)

        text = _render_subtitles(
            cues,
            subtitle_format=subtitle_format,
            max_chars_per_line=int(_clamp_float(max_chars_per_line, 16, 56, 34)),
            style=style or _timeline_subtitle_style(timeline, None),
        )
        data = text.encode("utf-8")
        target_abs_path = entity_fs.write_entity_file_atomic(
            entity_id,
            target.rel_path,
            data,
            expected_size=len(data),
            allow_empty=False,
        )

        document_id = await _register_file_artifact(
            entity_id=entity_id,
            user_id=user_id,
            filename=target.filename,
            rel_path=target.rel_path,
            file_size=os.path.getsize(target_abs_path),
            file_type=subtitle_format,
            mime_type=_subtitle_mime(subtitle_format),
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            tool_name="align_subtitles",
            artifact_role="subtitle",
            generation={
                "operation": "align_subtitles",
                "timeline_path": _rel_path_from_reference(timeline_path, entity_id) or timeline_path or None,
                "cues_path": _rel_path_from_reference(cues_path, entity_id) or cues_path or None,
                "format": subtitle_format,
                "track_types": sorted(include_types),
                "cue_count": len(cues),
                "estimated_cues": sum(1 for cue in cues if cue.estimated),
            },
        )
        await _bind_artifact_to_workspace(
            entity_id=entity_id,
            document_id=document_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name="align_subtitles",
        )

        return _json(
            {
                "kind": "subtitle",
                "status": "completed",
                "document_id": document_id,
                "name": target.filename,
                "result_url": f"/api/v1/fs/{entity_id}/{target.rel_path}",
                "fs_path": target.rel_path,
                "format": subtitle_format,
                "cue_count": len(cues),
                "estimated_cues": sum(1 for cue in cues if cue.estimated),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("align_subtitles failed")
        return _json_error(str(exc), code="align_subtitles_failed")


async def _normalize_audio_loudness_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    input_path: str = "",
    output_name: str = "",
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
    lra: float = 11.0,
    output_format: str = "wav",
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not (input_path or "").strip():
        return _json_error("input_path is required")
    if not (output_name or "").strip():
        return _json_error("output_name is required")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _json_error("ffmpeg is required for loudness normalization", code="ffmpeg_missing")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        rel_input = _rel_path_from_reference(input_path, entity_id) or input_path
        input_abs = _resolve_entity_file(entity_root, rel_input)
        _assert_audio_path(input_abs)
        fmt = str(output_format or "wav").strip().lower()
        if fmt not in {"wav", "mp3", "m4a", "flac"}:
            fmt = "wav"
        target = await _build_media_target(
            entity_id=entity_id,
            workspace_id=workspace_id,
            output_name=output_name,
            ext=f".{fmt}",
            fallback="normalized-audio",
            default_dir="audio",
        )
        if not target.abs_dir or not target.abs_path:
            raise ValueError("Could not resolve audio output path")
        os.makedirs(target.abs_dir, exist_ok=True)

        config = {
            "enabled": True,
            "target_lufs": target_lufs,
            "true_peak": true_peak,
            "lra": lra,
        }
        args = [
            ffmpeg,
            "-y",
            "-i",
            input_abs,
            "-vn",
            "-af",
            _loudnorm_filter(config),
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
        args.extend(_audio_codec_args(fmt))
        args.append(target.abs_path)
        await _run_process(args, timeout_seconds=300.0)

        document_id = await _register_file_artifact(
            entity_id=entity_id,
            user_id=user_id,
            filename=target.filename,
            rel_path=target.rel_path,
            file_size=os.path.getsize(target.abs_path),
            file_type=fmt,
            mime_type=_audio_mime(fmt),
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            tool_name="normalize_audio_loudness",
            artifact_role="audio",
            generation={
                "operation": "normalize_audio_loudness",
                "input_path": _normalize_user_path(rel_input),
                "target_lufs": _loudness_target_lufs(target_lufs),
                "true_peak": _loudness_true_peak(true_peak),
                "lra": _loudness_lra(lra),
                "format": fmt,
            },
        )
        await _bind_artifact_to_workspace(
            entity_id=entity_id,
            document_id=document_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name="normalize_audio_loudness",
        )

        return _json(
            {
                "kind": "audio",
                "status": "completed",
                "document_id": document_id,
                "name": target.filename,
                "result_url": f"/api/v1/fs/{entity_id}/{target.rel_path}",
                "audio_url": f"/api/v1/fs/{entity_id}/{target.rel_path}",
                "fs_path": target.rel_path,
                "file_size": os.path.getsize(target.abs_path),
                "format": fmt,
                "target_lufs": _loudness_target_lufs(target_lufs),
                "true_peak": _loudness_true_peak(true_peak),
                "lra": _loudness_lra(lra),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("normalize_audio_loudness failed")
        return _json_error(str(exc), code="normalize_audio_loudness_failed")


async def _load_media_jobs(entity_id: str, ids: list[str]) -> tuple[list[Any], list[str]]:
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob

    async with async_session() as db:
        result = await db.execute(
            select(MediaJob).where(
                MediaJob.entity_id == entity_id,
                MediaJob.id.in_(ids),
            )
        )
        jobs_by_id = {job.id: job for job in result.scalars().all()}
    jobs = [jobs_by_id[job_id] for job_id in ids if job_id in jobs_by_id]
    missing = [job_id for job_id in ids if job_id not in jobs_by_id]
    return jobs, missing


async def _jobs_to_payload(entity_id: str, jobs: list[Any]) -> list[dict[str, Any]]:
    from packages.core.database import async_session
    from packages.core.models.document import Document

    doc_ids = [
        str((getattr(job, "params", {}) or {}).get("result_document_id"))
        for job in jobs
        if (getattr(job, "params", {}) or {}).get("result_document_id")
    ]
    docs_by_id: dict[str, Any] = {}
    if doc_ids:
        async with async_session() as db:
            result = await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.id.in_(doc_ids),
                    Document.is_trashed == False,  # noqa: E712
                )
            )
            docs_by_id = {doc.id: doc for doc in result.scalars().all()}

    payloads: list[dict[str, Any]] = []
    for job in jobs:
        params = getattr(job, "params", {}) or {}
        doc_id = params.get("result_document_id")
        doc = docs_by_id.get(str(doc_id)) if doc_id else None
        payloads.append(
            {
                "job_id": getattr(job, "id", None),
                "kind": getattr(job, "kind", None),
                "status": getattr(job, "status", None),
                "model": getattr(job, "model", None),
                "result_url": getattr(job, "result_url", None),
                "source_url": getattr(job, "source_url", None),
                "error": getattr(job, "error", None),
                "document_id": doc_id,
                "fs_path": (
                    getattr(doc, "fs_path", None)
                    if doc
                    else _rel_path_from_reference(getattr(job, "result_url", None), entity_id)
                ),
                "file_size": getattr(job, "file_size", None),
                "duration_seconds": getattr(job, "duration_seconds", None),
                "created_at": _iso(getattr(job, "created_at", None)),
                "started_at": _iso(getattr(job, "started_at", None)),
                "completed_at": _iso(getattr(job, "completed_at", None)),
            }
        )
    return payloads


async def _resolve_video_inputs(
    *,
    entity_id: str,
    job_ids: list[str],
    document_ids: list[str],
    paths: list[str],
    folder_path: str | None,
) -> list[VideoInput]:
    from packages.core.services import entity_fs

    entity_root = entity_fs.get_entity_root(entity_id)
    resolved: list[VideoInput] = []

    if document_ids or job_ids:
        from packages.core.database import async_session
        from packages.core.models.media_job import MediaJob
        from packages.core.services.document_service import get_document

        async with async_session() as db:
            for document_id in document_ids:
                doc = await get_document(db, document_id, entity_id)
                if not doc:
                    raise ValueError(f"Document not found: {document_id}")
                resolved.append(_video_input_from_document(entity_root, doc))

            if job_ids:
                result = await db.execute(
                    select(MediaJob).where(
                        MediaJob.entity_id == entity_id,
                        MediaJob.id.in_(job_ids),
                    )
                )
                jobs_by_id = {job.id: job for job in result.scalars().all()}
                for job_id in job_ids:
                    job = jobs_by_id.get(job_id)
                    if not job:
                        raise ValueError(f"Media job not found: {job_id}")
                    if job.kind != "video":
                        raise ValueError(f"Media job is not a video job: {job_id}")
                    if job.status != "completed":
                        raise ValueError(f"Media job is not completed: {job_id} ({job.status})")
                    params = job.params or {}
                    document_id = params.get("result_document_id")
                    if document_id:
                        doc = await get_document(db, str(document_id), entity_id)
                        if not doc:
                            raise ValueError(f"Media job document not found: {job_id}")
                        item = _video_input_from_document(entity_root, doc)
                        resolved.append(
                            VideoInput(
                                source_type="job",
                                source_id=job_id,
                                rel_path=item.rel_path,
                                abs_path=item.abs_path,
                                document_id=doc.id,
                            )
                        )
                        continue
                    rel_path = _rel_path_from_reference(job.result_url, entity_id)
                    if not rel_path:
                        raise ValueError(f"Completed media job has no Knowledge path: {job_id}")
                    abs_path = _resolve_entity_file(entity_root, rel_path)
                    _assert_video_path(abs_path)
                    resolved.append(
                        VideoInput(
                            source_type="job",
                            source_id=job_id,
                            rel_path=rel_path,
                            abs_path=abs_path,
                        )
                    )

    for path in paths:
        rel_path = _rel_path_from_reference(path, entity_id) or path
        abs_path = _resolve_entity_file(entity_root, rel_path)
        _assert_video_path(abs_path)
        resolved.append(
            VideoInput(
                source_type="path",
                source_id=path,
                rel_path=_normalize_user_path(rel_path),
                abs_path=abs_path,
            )
        )

    if folder_path:
        folder_rel_path = _normalize_user_path(folder_path)
        folder_abs_path = _resolve_entity_dir(entity_root, folder_rel_path)
        for filename in sorted(os.listdir(folder_abs_path)):
            full_path = os.path.join(folder_abs_path, filename)
            if not os.path.isfile(full_path):
                continue
            if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            rel_path = "/".join(part for part in (folder_rel_path, filename) if part)
            resolved.append(
                VideoInput(
                    source_type="folder",
                    source_id=folder_path,
                    rel_path=rel_path,
                    abs_path=full_path,
                )
            )

    return _dedupe_inputs(resolved)


def _video_input_from_document(entity_root: str, doc: Any) -> VideoInput:
    if not _is_video_document(doc):
        raise ValueError(f"Document is not a supported video: {getattr(doc, 'id', '')}")
    rel_path = getattr(doc, "fs_path", None)
    if not rel_path:
        raise ValueError(f"Document has no filesystem path: {getattr(doc, 'id', '')}")
    abs_path = _resolve_entity_file(entity_root, rel_path)
    _assert_video_path(abs_path)
    return VideoInput(
        source_type="document",
        source_id=getattr(doc, "id", None),
        rel_path=_normalize_user_path(rel_path),
        abs_path=abs_path,
        document_id=getattr(doc, "id", None),
    )


async def _merge_video_files(
    *,
    ffmpeg: str,
    ffprobe: str,
    entity_id: str,
    output_name: str,
    workspace_id: str | None,
    inputs: list[VideoInput],
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    include_source_audio: bool,
) -> tuple[str, str, str, list[dict[str, Any]], float]:
    from packages.core.services import entity_fs
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )

    entity_root = entity_fs.get_entity_root(entity_id)
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    target = build_generated_media_target(
        prompt=output_name,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=".mp4",
        fallback="merged-video",
        default_dir=workspace_artifact_default_dir(workspace_base_dir, "videos"),
        entity_root=entity_root,
    )
    if not target.abs_dir or not target.abs_path:
        raise ValueError("Could not resolve output path")

    total_duration = 0.0
    input_payloads: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="merge-videos-") as tmp_dir:
        normalized_paths: list[str] = []
        for index, item in enumerate(inputs, start=1):
            media_info = await _probe_media(ffprobe, item.abs_path)
            duration = media_info.get("duration_seconds") or 0.0
            total_duration += duration
            normalized_path = os.path.join(tmp_dir, f"clip-{index:03d}.mp4")
            await _normalize_clip(
                ffmpeg=ffmpeg,
                input_path=item.abs_path,
                output_path=normalized_path,
                width=width,
                height=height,
                fps=fps,
                crf=crf,
                preset=preset,
                duration_seconds=duration,
                has_audio=bool(media_info.get("has_audio")),
                include_source_audio=include_source_audio,
            )
            normalized_paths.append(normalized_path)
            input_payloads.append(
                {
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "document_id": item.document_id,
                    "fs_path": item.rel_path,
                    "duration_seconds": round(duration, 2),
                    "has_audio": bool(media_info.get("has_audio")),
                    "source_audio_used": bool(include_source_audio and media_info.get("has_audio")),
                }
            )

        concat_file = os.path.join(tmp_dir, "concat.txt")
        with open(concat_file, "w", encoding="utf-8") as handle:
            for normalized_path in normalized_paths:
                handle.write(f"file '{_concat_escape(normalized_path)}'\n")

        tmp_output = os.path.join(tmp_dir, "merged.mp4")
        await _run_process(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                tmp_output,
            ],
            timeout_seconds=max(120.0, total_duration * 4.0 + 60.0),
        )
        entity_fs.copy_entity_file_atomic(
            entity_id,
            target.rel_path,
            tmp_output,
            expected_size=os.path.getsize(tmp_output),
            allow_empty=False,
        )

    return target.abs_path, target.rel_path, target.filename, input_payloads, total_duration


async def _register_merged_video(
    *,
    entity_id: str,
    user_id: str,
    filename: str,
    rel_path: str,
    file_size: int,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    inputs: list[dict[str, Any]],
    resolution: str,
    aspect_ratio: str,
    fps: int,
    crf: int,
    preset: str,
    include_source_audio: bool,
    operation: str = "merge_videos",
) -> str | None:
    from packages.core.database import async_session
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import upsert_document_by_fs_path
    from packages.core.services.knowledge_sync import ensure_folder_path

    rel_dir = str(Path(rel_path).parent).replace("\\", "/")
    rel_dir = "" if rel_dir == "." else rel_dir
    folder_id = await ensure_folder_path(entity_id, rel_dir)
    async with async_session() as db:
        doc = await upsert_document_by_fs_path(
            db,
            entity_id,
            name=filename,
            fs_path=rel_path,
            file_size=file_size,
            file_type="mp4",
            mime_type="video/mp4",
            source="ai_generated",
            created_by=user_id or None,
            folder_id=folder_id,
        )
        doc.source = "ai_generated"
        if user_id:
            doc.created_by = user_id
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            artifact={"role": "final", "storage_scope": "artifact"},
            origin={
                "workspace_id": workspace_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tool_name": operation,
            },
            generation={
                "operation": operation,
                "inputs": inputs,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "fps": fps,
                "crf": crf,
                "preset": preset,
                "include_source_audio": include_source_audio,
            },
        )
        document_id = doc.id
        await db.commit()
    return document_id


async def _create_video_editor_recipe_sidecar(
    *,
    entity_id: str,
    user_id: str,
    timeline: dict[str, Any],
    timeline_path: str,
    clean_video_path: str,
    final_document_id: str | None,
    final_filename: str,
    final_rel_path: str,
    final_file_size: int,
    total_duration: float,
    media_info: dict[str, Any],
    audio_tracks: list[TimelineAudioTrack],
    subtitle_path: str,
    subtitle_abs_path: str,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    crf: int,
    preset: str,
    include_source_audio: bool,
) -> dict[str, Any]:
    """Persist the editor recipe that lets a final render reopen as layers."""
    recipe_rel_path = _video_editor_recipe_rel_path(final_rel_path)
    candidate_paths = _editor_recipe_candidate_paths(
        timeline=timeline,
        clean_video_path=clean_video_path,
        subtitle_path=subtitle_path,
        audio_tracks=audio_tracks,
        entity_id=entity_id,
    )
    documents_by_path = await _lookup_documents_by_rel_paths(entity_id, candidate_paths)
    recipe = _build_video_editor_recipe_payload(
        entity_id=entity_id,
        timeline=timeline,
        timeline_path=timeline_path,
        clean_video_path=clean_video_path,
        final_document_id=final_document_id,
        final_filename=final_filename,
        final_rel_path=final_rel_path,
        final_file_size=final_file_size,
        total_duration=total_duration,
        media_info=media_info,
        audio_tracks=audio_tracks,
        subtitle_path=subtitle_path,
        subtitle_abs_path=subtitle_abs_path,
        documents_by_path=documents_by_path,
        crf=crf,
        preset=preset,
        include_source_audio=include_source_audio,
    )
    recipe_abs_path, recipe_file_size = _write_video_editor_recipe_file(
        entity_id=entity_id,
        rel_path=recipe_rel_path,
        payload=recipe,
    )
    recipe_document_id = await _register_video_editor_recipe(
        entity_id=entity_id,
        user_id=user_id,
        filename=Path(recipe_rel_path).name,
        rel_path=recipe_rel_path,
        file_size=recipe_file_size,
        final_document_id=final_document_id,
        final_rel_path=final_rel_path,
        timeline_path=timeline_path,
        clean_video_path=clean_video_path,
        subtitle_path=subtitle_path,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
    )
    return {
        "document_id": recipe_document_id,
        "name": Path(recipe_rel_path).name,
        "fs_path": recipe_rel_path,
        "result_url": f"/api/v1/fs/{entity_id}/{recipe_rel_path}",
        "file_size": recipe_file_size,
        "abs_path": recipe_abs_path,
        "kind": "manor.video_edit_recipe",
    }


def _video_editor_recipe_rel_path(video_rel_path: str) -> str:
    normalized = _normalize_user_path(video_rel_path)
    path = Path(normalized)
    filename = f"{path.stem}.video-edit.json" if path.suffix else f"{path.name}.video-edit.json"
    parent = str(path.parent).replace("\\", "/")
    return filename if parent == "." else f"{parent}/{filename}"


def _editor_recipe_candidate_paths(
    *,
    timeline: dict[str, Any],
    clean_video_path: str,
    subtitle_path: str,
    audio_tracks: list[TimelineAudioTrack],
    entity_id: str,
) -> list[str]:
    paths: list[str] = []
    for raw in (clean_video_path, subtitle_path):
        rel = _safe_editor_rel_path(raw, entity_id)
        if rel:
            paths.append(rel)
    for item in _timeline_video_items(timeline):
        rel = _safe_editor_rel_path(_timeline_media_path(item), entity_id)
        if rel:
            paths.append(rel)
    for track in audio_tracks:
        paths.append(track.rel_path)
    return sorted(set(paths))


async def _lookup_documents_by_rel_paths(
    entity_id: str,
    rel_paths: list[str],
) -> dict[str, dict[str, Any]]:
    normalized_paths: list[str] = []
    for raw_path in rel_paths:
        rel = _safe_editor_rel_path(raw_path, entity_id)
        if rel:
            normalized_paths.append(rel)
    unique_paths = sorted(set(normalized_paths))
    if not unique_paths:
        return {}

    from packages.core.database import async_session
    from packages.core.models.document import Document

    async with async_session() as db:
        result = await db.execute(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path.in_(unique_paths),
                Document.is_trashed == False,  # noqa: E712
            )
        )
        docs = result.scalars().all()

    payloads: dict[str, dict[str, Any]] = {}
    for doc in docs:
        fs_path = _safe_editor_rel_path(getattr(doc, "fs_path", None), entity_id)
        if not fs_path:
            continue
        payloads[fs_path] = {
            "id": getattr(doc, "id", None),
            "name": getattr(doc, "name", None),
            "fs_path": fs_path,
            "mime_type": getattr(doc, "mime_type", None),
            "file_type": getattr(doc, "file_type", None),
            "file_size": getattr(doc, "file_size", None),
        }
    return payloads


def _build_video_editor_recipe_payload(
    *,
    entity_id: str,
    timeline: dict[str, Any],
    timeline_path: str,
    clean_video_path: str,
    final_document_id: str | None,
    final_filename: str,
    final_rel_path: str,
    final_file_size: int,
    total_duration: float,
    media_info: dict[str, Any],
    audio_tracks: list[TimelineAudioTrack],
    subtitle_path: str,
    subtitle_abs_path: str,
    documents_by_path: dict[str, dict[str, Any]],
    crf: int,
    preset: str,
    include_source_audio: bool,
) -> dict[str, Any]:
    duration = _timeline_recipe_duration(timeline, total_duration)
    clips = _editor_recipe_clips(
        entity_id=entity_id,
        timeline=timeline,
        duration=duration,
        documents_by_path=documents_by_path,
    )
    shots = _editor_recipe_shots(timeline, duration, clips)
    captions = _editor_recipe_captions(
        timeline=timeline,
        subtitle_path=subtitle_path,
        subtitle_abs_path=subtitle_abs_path,
        audio_tracks=audio_tracks,
        duration=duration,
    )
    audio_cues = _editor_recipe_audio_cues(
        entity_id=entity_id,
        timeline=timeline,
        audio_tracks=audio_tracks,
        documents_by_path=documents_by_path,
        duration=duration,
    )
    markers = _editor_recipe_markers(timeline, duration, clips, shots)
    spec = timeline.get("spec") if isinstance(timeline.get("spec"), dict) else {}
    width, height = _editor_canvas_size(spec, media_info)
    timeline_rel = _safe_editor_rel_path(timeline_path, entity_id) or timeline_path
    clean_rel = _safe_editor_rel_path(clean_video_path, entity_id) or clean_video_path

    return {
        "version": 1,
        "kind": "manor.video_edit_recipe",
        "created_by": "compose_video_timeline",
        "source_document": {
            "id": final_document_id,
            "name": final_filename,
            "folder_id": None,
            "fs_path": _normalize_user_path(final_rel_path),
            "mime_type": "video/mp4",
            "file_size": final_file_size,
        },
        "canvas": {
            "width": width,
            "height": height,
        },
        "timeline": {
            "duration": round(duration, 3),
            "clips": clips,
            "shots": shots,
            "captions": captions,
            "audio_cues": audio_cues,
            "markers": markers,
        },
        "manual_edits": [],
        "editor_settings": {
            "track_states": {
                "markers": {"locked": False, "muted": False, "visible": True},
                "shots": {"locked": False, "muted": False, "visible": True},
                "video": {"locked": False, "muted": False, "visible": True},
                "captions": {"locked": False, "muted": False, "visible": True},
                "audio": {"locked": False, "muted": False, "visible": True},
            },
            "work_area": {"enabled": False, "start": 0, "end": round(duration, 3)},
        },
        "ai_composition": {
            "timeline_path": timeline_rel,
            "clean_picture_master": clean_rel,
            "subtitle_path": subtitle_path or None,
            "final_video_path": _normalize_user_path(final_rel_path),
            "clip_count": len(clips),
            "shot_count": len(shots),
            "caption_count": len(captions),
            "audio_track_count": len(audio_cues),
            "editable_sources": ["clips", "shots", "captions", "audio_cues", "markers"],
        },
        "render_contract": {
            "video": (
                "Recreate the final video by applying clip order, source/replacement "
                "asset trims, and video track mute/visibility settings."
            ),
            "audio": (
                "Mix dialogue, narration, music, ambience, and SFX from explicit cue "
                "assets with start/end times, loop, fades, ducking, and volume."
            ),
            "captions": (
                "Burn or export captions from the caption track; do not depend on "
                "provider-generated subtitles hidden inside video clips."
            ),
            "export": (
                "Render final MP4 plus this editable recipe sidecar so future editor "
                "opens preserve the composition."
            ),
        },
        "source_timeline": timeline,
        "generation": {
            "operation": "compose_video_timeline",
            "crf": crf,
            "preset": preset,
            "include_source_audio": include_source_audio,
        },
    }


def _write_video_editor_recipe_file(
    *,
    entity_id: str,
    rel_path: str,
    payload: dict[str, Any],
) -> tuple[str, int]:
    from packages.core.services import entity_fs

    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    abs_path = entity_fs.write_entity_file_atomic(
        entity_id,
        rel_path,
        data,
        expected_size=len(data),
        allow_empty=False,
    )
    return abs_path, len(data)


async def _register_video_editor_recipe(
    *,
    entity_id: str,
    user_id: str,
    filename: str,
    rel_path: str,
    file_size: int,
    final_document_id: str | None,
    final_rel_path: str,
    timeline_path: str,
    clean_video_path: str,
    subtitle_path: str,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
) -> str | None:
    from packages.core.database import async_session
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import upsert_document_by_fs_path
    from packages.core.services.knowledge_sync import ensure_folder_path

    rel_dir = str(Path(rel_path).parent).replace("\\", "/")
    rel_dir = "" if rel_dir == "." else rel_dir
    folder_id = await ensure_folder_path(entity_id, rel_dir)
    async with async_session() as db:
        doc = await upsert_document_by_fs_path(
            db,
            entity_id,
            name=filename,
            fs_path=rel_path,
            file_size=file_size,
            file_type="json",
            mime_type="application/json",
            source="ai_generated",
            created_by=user_id or None,
            folder_id=folder_id,
        )
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            artifact={
                "role": "editor_recipe",
                "storage_scope": "artifact",
                "paired_video_document_id": final_document_id,
                "paired_video_path": final_rel_path,
            },
            origin={
                "workspace_id": workspace_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tool_name": "compose_video_timeline",
            },
            generation={
                "operation": "compose_video_timeline",
                "timeline_path": timeline_path,
                "clean_video_path": clean_video_path,
                "subtitle_path": subtitle_path,
                "final_document_id": final_document_id,
                "final_video_path": final_rel_path,
            },
        )
        document_id = doc.id
        await db.commit()
    return document_id


async def _attach_video_editor_recipe_to_video(
    *,
    entity_id: str,
    final_document_id: str | None,
    editor_recipe: dict[str, Any] | None,
) -> None:
    recipe_document_id = _string_or_none((editor_recipe or {}).get("document_id"))
    recipe_path = _string_or_none((editor_recipe or {}).get("fs_path"))
    if not final_document_id or not (recipe_document_id or recipe_path):
        return

    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services.document_metadata import merge_document_metadata

    async with async_session() as db:
        result = await db.execute(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.id == final_document_id,
                Document.is_trashed == False,  # noqa: E712
            )
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            artifact={
                "editor_recipe_document_id": recipe_document_id,
                "editor_recipe_path": recipe_path,
                "editor_recipe_name": _string_or_none((editor_recipe or {}).get("name")),
            },
            generation={
                "editor_recipe_document_id": recipe_document_id,
                "editor_recipe_path": recipe_path,
            },
        )
        await db.commit()


async def _build_media_target(
    *,
    entity_id: str,
    workspace_id: str | None,
    output_name: str,
    ext: str,
    fallback: str,
    default_dir: str,
):
    from packages.core.services import entity_fs
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )

    entity_root = entity_fs.get_entity_root(entity_id)
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    return build_generated_media_target(
        prompt=output_name or fallback,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=ext,
        fallback=fallback,
        default_dir=workspace_artifact_default_dir(workspace_base_dir, default_dir),
        entity_root=entity_root,
    )


async def _register_file_artifact(
    *,
    entity_id: str,
    user_id: str,
    filename: str,
    rel_path: str,
    file_size: int,
    file_type: str,
    mime_type: str,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    tool_name: str,
    artifact_role: str,
    generation: dict[str, Any],
) -> str | None:
    from packages.core.database import async_session
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import upsert_document_by_fs_path
    from packages.core.services.knowledge_sync import ensure_folder_path

    rel_dir = str(Path(rel_path).parent).replace("\\", "/")
    rel_dir = "" if rel_dir == "." else rel_dir
    folder_id = await ensure_folder_path(entity_id, rel_dir)
    async with async_session() as db:
        doc = await upsert_document_by_fs_path(
            db,
            entity_id,
            name=filename,
            fs_path=rel_path,
            file_size=file_size,
            file_type=file_type,
            mime_type=mime_type,
            source="ai_generated",
            created_by=user_id or None,
            folder_id=folder_id,
        )
        doc.source = "ai_generated"
        if user_id:
            doc.created_by = user_id
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            artifact={"role": artifact_role, "storage_scope": "artifact"},
            origin={
                "workspace_id": workspace_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tool_name": tool_name,
            },
            generation=generation,
        )
        document_id = doc.id
        await db.commit()
    return document_id


async def _bind_artifact_to_workspace(
    *,
    entity_id: str,
    document_id: str | None,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    user_id: str,
    tool_name: str,
) -> None:
    if not workspace_id or not document_id:
        return
    from packages.core.services.knowledge_sync import bind_document_to_workspace

    await bind_document_to_workspace(
        entity_id=entity_id,
        document_id=document_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
    )


def _load_timeline_json(entity_root: str, timeline_path: str, entity_id: str) -> dict[str, Any]:
    rel_path = _rel_path_from_reference(timeline_path, entity_id) or timeline_path
    abs_path = _resolve_entity_file(entity_root, rel_path)
    if Path(abs_path).suffix.lower() != ".json":
        raise ValueError(f"Timeline must be a JSON file: {rel_path}")
    with open(abs_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Timeline JSON must contain an object")
    return data


def _load_entity_json(entity_root: str, path: str, entity_id: str) -> Any:
    rel_path = _rel_path_from_reference(path, entity_id) or path
    abs_path = _resolve_entity_file(entity_root, rel_path)
    if Path(abs_path).suffix.lower() != ".json":
        raise ValueError(f"JSON file expected: {rel_path}")
    with open(abs_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _timeline_clean_video_path(timeline: dict[str, Any], override: str = "") -> str:
    if (override or "").strip():
        return str(override).strip()
    delivery = timeline.get("delivery") if isinstance(timeline.get("delivery"), dict) else {}
    for key in (
        "clean_picture_master",
        "clean_picture_master_path",
        "clean_video_path",
        "clean_master_path",
        "picture_master",
        "video_path",
    ):
        value = delivery.get(key)
        if value:
            return str(value)
    return ""


def _timeline_subtitle_path(timeline: dict[str, Any], override: str = "") -> str:
    if (override or "").strip():
        return str(override).strip()
    subtitles = timeline.get("subtitles")
    if isinstance(subtitles, str):
        return subtitles
    if isinstance(subtitles, dict):
        for key in ("srt_path", "vtt_path", "subtitle_path", "path"):
            value = subtitles.get(key)
            if value:
                return str(value)
    return ""


def _timeline_subtitle_style(
    timeline: dict[str, Any],
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    style: dict[str, Any] = {}
    subtitles = timeline.get("subtitles")
    if isinstance(subtitles, dict) and isinstance(subtitles.get("style"), dict):
        style.update(subtitles["style"])
    if isinstance(override, dict):
        style.update(override)
    return {str(key): value for key, value in style.items() if value is not None}


def _timeline_ducking_config(
    timeline: dict[str, Any],
    override: dict[str, Any] | bool | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    mix = timeline.get("mix") if isinstance(timeline.get("mix"), dict) else {}
    audio_mix = timeline.get("audio_mix") if isinstance(timeline.get("audio_mix"), dict) else {}
    for source in (audio_mix, mix):
        value = source.get("ducking") if isinstance(source, dict) else None
        if isinstance(value, dict):
            raw.update(value)
        elif isinstance(value, bool):
            raw["enabled"] = value
    if isinstance(override, dict):
        raw.update(override)
    elif isinstance(override, bool):
        raw["enabled"] = override

    enabled = bool(raw.get("enabled", False))
    return {
        "enabled": enabled,
        "amount_db": _clamp_float(raw.get("amount_db"), -30.0, 0.0, -9.0),
        "padding": _clamp_float(raw.get("padding"), 0.0, 2.0, 0.15),
        "target_types": sorted(_string_set(raw.get("target_types")) or {"music", "ambience"}),
        "sidechain_types": sorted(_string_set(raw.get("sidechain_types")) or {"dialogue", "narration"}),
    }


def _timeline_loudness_config(
    timeline: dict[str, Any],
    override: dict[str, Any] | bool | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    mix = timeline.get("mix") if isinstance(timeline.get("mix"), dict) else {}
    audio_mix = timeline.get("audio_mix") if isinstance(timeline.get("audio_mix"), dict) else {}
    for source in (audio_mix, mix):
        value = source.get("loudness_normalization") if isinstance(source, dict) else None
        if isinstance(value, dict):
            raw.update(value)
        elif isinstance(value, bool):
            raw["enabled"] = value
    if isinstance(override, dict):
        raw.update(override)
    elif isinstance(override, bool):
        raw["enabled"] = override

    return {
        "enabled": bool(raw.get("enabled", False)),
        "target_lufs": _loudness_target_lufs(raw.get("target_lufs")),
        "true_peak": _loudness_true_peak(raw.get("true_peak")),
        "lra": _loudness_lra(raw.get("lra")),
    }


async def _resolve_timeline_audio_tracks(
    *,
    ffprobe: str,
    entity_root: str,
    entity_id: str,
    timeline: dict[str, Any],
    enabled: bool,
) -> list[TimelineAudioTrack]:
    if not enabled:
        return []
    raw_tracks, source_key = _timeline_audio_track_items(timeline)
    if isinstance(raw_tracks, dict):
        raw_tracks = raw_tracks.get("tracks") or []
    if not isinstance(raw_tracks, list):
        raise ValueError(f"timeline.{source_key} must be a list")

    tracks: list[TimelineAudioTrack] = []
    for index, item in enumerate(raw_tracks, start=1):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").startswith("needs_"):
            continue
        if source_key == "tracks" and not _looks_like_audio_timeline_track(item):
            continue
        raw_path = str(item.get("path") or item.get("fs_path") or "").strip()
        if not raw_path:
            continue
        rel_path = _rel_path_from_reference(raw_path, entity_id) or raw_path
        abs_path = _resolve_entity_file(entity_root, rel_path)
        _assert_audio_path(abs_path)
        start = _coerce_required_time(item.get("start"), f"audio_tracks[{index}].start")
        media_info = await _probe_media(ffprobe, abs_path)
        source_duration = max(0.01, float(media_info.get("duration_seconds") or 0.01))
        end = _timeline_track_end(item, start, source_duration, index)
        if end <= start:
            raise ValueError(f"audio_tracks[{index}].end must be greater than start")
        fade_in = max(0.0, _coerce_float(item.get("fade_in"), 0.0))
        fade_out = max(0.0, _coerce_float(item.get("fade_out"), 0.0))
        tracks.append(
            TimelineAudioTrack(
                track_id=str(item.get("id") or f"audio-{index:03d}"),
                track_type=str(item.get("type") or "audio"),
                rel_path=_normalize_user_path(rel_path),
                abs_path=abs_path,
                start=start,
                end=end,
                volume_db=_coerce_float(item.get("volume_db", item.get("volume")), 0.0),
                loop=bool(item.get("loop")),
                fade_in=fade_in,
                fade_out=fade_out,
                duration=end - start,
            )
        )
    return tracks


def _timeline_audio_track_items(timeline: dict[str, Any]) -> tuple[Any, str]:
    raw_tracks = timeline.get("audio_tracks")
    if raw_tracks is None:
        return timeline.get("tracks") or [], "tracks"
    return raw_tracks, "audio_tracks"


def _safe_editor_rel_path(value: Any, entity_id: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return _rel_path_from_reference(raw, entity_id) or _normalize_user_path(raw)
    except ValueError:
        return ""


def _timeline_recipe_duration(timeline: dict[str, Any], fallback_duration: float) -> float:
    spec = timeline.get("spec") if isinstance(timeline.get("spec"), dict) else {}
    delivery = timeline.get("delivery") if isinstance(timeline.get("delivery"), dict) else {}
    for value in (
        timeline.get("duration"),
        timeline.get("duration_seconds"),
        spec.get("duration"),
        spec.get("duration_seconds"),
        delivery.get("duration"),
        delivery.get("duration_seconds"),
    ):
        duration = _coerce_float(value, 0.0)
        if duration > 0:
            return duration
    return max(0.05, float(fallback_duration or 0.05))


def _timeline_video_items(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("clips", "video_clips", "shot_clips", "shots", "shot_beats", "storyboards", "storyboard"):
        value = timeline.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    scenes = timeline.get("scenes")
    if isinstance(scenes, list):
        items: list[dict[str, Any]] = []
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            scene_id = str(scene.get("id") or scene.get("scene_id") or scene.get("scene") or "")
            shots = scene.get("shots") or scene.get("clips") or scene.get("beats")
            if not isinstance(shots, list):
                continue
            for shot in shots:
                if not isinstance(shot, dict):
                    continue
                item = dict(shot)
                if scene_id and not item.get("scene_id"):
                    item["scene_id"] = scene_id
                items.append(item)
        if items:
            return items
    return []


def _editor_recipe_clips(
    *,
    entity_id: str,
    timeline: dict[str, Any],
    duration: float,
    documents_by_path: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items = _timeline_video_items(timeline)
    if not items:
        return [
            {
                "id": "clip-001",
                "label": "Final composed picture",
                "sourceStart": 0,
                "sourceEnd": round(max(0.05, duration), 3),
                "muted": False,
                "color": "#0f766e",
                "replacementPrompt": "",
                "editNotes": "No shot-level clip list was found in the source timeline; this covers the final rendered picture master.",
            }
        ]

    default_span = max(0.05, duration / max(len(items), 1))
    clips: list[dict[str, Any]] = []
    cursor = 0.0
    for index, item in enumerate(items, start=1):
        start, end = _timeline_item_start_end(item, cursor, default_span, duration)
        span = max(0.05, end - start)
        media_path = _safe_editor_rel_path(_timeline_media_path(item), entity_id)
        doc = documents_by_path.get(media_path) if media_path else None
        asset_document_id = _string_or_none(
            item.get("assetDocumentId")
            or item.get("asset_document_id")
            or item.get("document_id")
            or item.get("doc_id")
            or (doc or {}).get("id")
        )
        if asset_document_id:
            source_start = max(0.0, _coerce_float(_first_present(item, ("sourceStart", "source_start", "trim_start")), 0.0))
            source_end = source_start + max(0.05, _coerce_float(_first_present(item, ("sourceDuration", "source_duration")), span))
        else:
            source_start = start
            source_end = end
        clip = {
            "id": str(item.get("id") or item.get("clip_id") or item.get("shot_id") or f"clip-{index:03d}"),
            "label": _editor_label(item, index, prefix="Clip"),
            "sourceStart": round(source_start, 3),
            "sourceEnd": round(source_end, 3),
            "muted": bool(item.get("muted", False)),
            "color": _editor_color(index),
            "assetDocumentId": asset_document_id,
            "assetName": _string_or_none(item.get("assetName") or item.get("asset_name") or (doc or {}).get("name") or Path(media_path).name),
            "assetMimeType": _string_or_none(item.get("assetMimeType") or item.get("asset_mime_type") or (doc or {}).get("mime_type")),
            "assetDuration": round(span, 3) if asset_document_id else None,
            "replacementPrompt": str(item.get("replacement_prompt") or item.get("prompt") or item.get("video_prompt") or ""),
            "editNotes": _editor_clip_notes(item, media_path),
        }
        clips.append(clip)
        cursor = end
    return clips


def _editor_recipe_shots(
    timeline: dict[str, Any],
    duration: float,
    clips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items = _timeline_video_items(timeline)
    if not items:
        return [
            {
                "id": "shot-001",
                "title": "Final composed picture",
                "scene": "Scene 1",
                "shot": "Shot 1",
                "start": 0,
                "end": round(max(0.05, duration), 3),
                "location": "",
                "camera": "",
                "action": "",
                "dialogue": "",
                "notes": "",
            }
        ]

    default_span = max(0.05, duration / max(len(items), 1))
    shots: list[dict[str, Any]] = []
    cursor = 0.0
    for index, item in enumerate(items, start=1):
        start, end = _timeline_item_start_end(item, cursor, default_span, duration)
        shots.append(
            {
                "id": str(item.get("shot_id") or item.get("id") or f"shot-{index:03d}"),
                "title": _editor_label(item, index, prefix="Beat"),
                "scene": str(item.get("scene") or item.get("scene_id") or f"Scene {index}"),
                "shot": str(item.get("shot") or item.get("shot_id") or item.get("clip_id") or f"Shot {index}"),
                "start": round(start, 3),
                "end": round(max(start + 0.05, end), 3),
                "location": str(item.get("location") or item.get("setting") or ""),
                "camera": str(item.get("camera") or item.get("camera_move") or item.get("lens") or ""),
                "action": _editor_shot_action(item),
                "dialogue": _editor_shot_dialogue(item),
                "notes": _editor_shot_notes(item),
            }
        )
        cursor = end
    return shots


def _editor_recipe_captions(
    *,
    timeline: dict[str, Any],
    subtitle_path: str,
    subtitle_abs_path: str,
    audio_tracks: list[TimelineAudioTrack],
    duration: float,
) -> list[dict[str, Any]]:
    track_by_id = {track.track_id: track for track in audio_tracks}
    track_by_path = {track.rel_path: track for track in audio_tracks}
    captions: list[dict[str, Any]] = []
    for item in _subtitle_items_from_payload(timeline):
        text = _subtitle_text(item)
        if not text:
            continue
        start = _coerce_float(item.get("start"), -1.0)
        if start < 0:
            continue
        end = _coerce_float(item.get("end"), -1.0)
        if end <= start:
            raw_path = str(item.get("path") or item.get("fs_path") or "").strip()
            track = track_by_id.get(str(item.get("id") or item.get("cue_id") or "")) or track_by_path.get(raw_path)
            if track:
                end = track.end
            elif item.get("duration") is not None:
                end = start + max(0.05, _coerce_float(item.get("duration"), 0.05))
        if end <= start:
            continue
        cue_type = str(item.get("type") or item.get("kind") or "dialogue").lower()
        style = "narrationBox" if cue_type == "narration" else "subtitle"
        captions.append(
            {
                "id": str(item.get("id") or item.get("cue_id") or f"caption-{len(captions) + 1:03d}"),
                "speaker": _string_or_none(item.get("speaker") or item.get("character")),
                "emotion": _string_or_none(item.get("emotion") or item.get("performance")),
                "style": style,
                "text": text,
                "start": round(max(0.0, start), 3),
                "end": round(min(duration, max(start + 0.05, end)), 3),
                "x": 50,
                "y": 84 if style == "subtitle" else 12,
                "size": 32,
                "color": "#ffffff",
                "background": "rgba(15,23,42,0.72)",
                "backgroundColor": "#0f172a",
                "backgroundOpacity": 0.72,
                "align": "center",
            }
        )

    if not captions:
        captions.extend(_editor_recipe_captions_from_subtitle_file(subtitle_abs_path or subtitle_path, duration))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[float, float, str]] = set()
    for cue in captions:
        key = (round(_coerce_float(cue.get("start"), 0.0), 2), round(_coerce_float(cue.get("end"), 0.0), 2), str(cue.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(cue)
    unique.sort(key=lambda cue: (cue["start"], cue["end"]))
    return unique


def _editor_recipe_captions_from_subtitle_file(subtitle_path: str, duration: float) -> list[dict[str, Any]]:
    path = Path(subtitle_path)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = [block.strip() for block in content.replace("\r\n", "\n").split("\n\n") if block.strip()]
    captions: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        start_raw, end_raw = [part.strip() for part in time_line.split("-->", 1)]
        start = _parse_editor_subtitle_time(start_raw)
        end = _parse_editor_subtitle_time(end_raw)
        if end <= start:
            continue
        text = " ".join(line for line in lines if line != time_line and not line.isdigit())
        if not text:
            continue
        captions.append(
            {
                "id": f"caption-{len(captions) + 1:03d}",
                "speaker": None,
                "emotion": None,
                "style": "subtitle",
                "text": text,
                "start": round(max(0.0, start), 3),
                "end": round(min(duration, end), 3),
                "x": 50,
                "y": 84,
                "size": 32,
                "color": "#ffffff",
                "background": "rgba(15,23,42,0.72)",
                "backgroundColor": "#0f172a",
                "backgroundOpacity": 0.72,
                "align": "center",
            }
        )
    return captions


def _editor_recipe_audio_cues(
    *,
    entity_id: str,
    timeline: dict[str, Any],
    audio_tracks: list[TimelineAudioTrack],
    documents_by_path: dict[str, dict[str, Any]],
    duration: float,
) -> list[dict[str, Any]]:
    raw_tracks, _source_key = _timeline_audio_track_items(timeline)
    if isinstance(raw_tracks, dict):
        raw_tracks = raw_tracks.get("tracks") or []
    raw_by_id: dict[str, dict[str, Any]] = {}
    raw_by_path: dict[str, dict[str, Any]] = {}
    if isinstance(raw_tracks, list):
        for item in raw_tracks:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or item.get("cue_id") or "")
            if raw_id:
                raw_by_id[raw_id] = item
            rel = _safe_editor_rel_path(item.get("path") or item.get("fs_path"), entity_id)
            if rel:
                raw_by_path[rel] = item

    cues: list[dict[str, Any]] = []
    for track in audio_tracks:
        raw = raw_by_id.get(track.track_id) or raw_by_path.get(track.rel_path) or {}
        doc = documents_by_path.get(track.rel_path) or {}
        cue_type = _editor_audio_type(track.track_type)
        cues.append(
            {
                "id": track.track_id,
                "type": cue_type,
                "label": _editor_audio_label(track, raw),
                "start": round(max(0.0, min(track.start, duration)), 3),
                "end": round(max(track.start + 0.05, min(track.end, duration)), 3),
                "volumeDb": track.volume_db,
                "fadeIn": track.fade_in,
                "fadeOut": track.fade_out,
                "loop": track.loop,
                "duckUnderDialogue": cue_type in {"music", "ambience"} or bool(raw.get("duckUnderDialogue") or raw.get("duck_under_dialogue")),
                "muted": False,
                "assetDocumentId": _string_or_none(raw.get("assetDocumentId") or raw.get("asset_document_id") or raw.get("document_id") or doc.get("id")),
                "assetName": _string_or_none(raw.get("assetName") or raw.get("asset_name") or doc.get("name") or Path(track.rel_path).name),
                "assetMimeType": _string_or_none(raw.get("assetMimeType") or raw.get("asset_mime_type") or doc.get("mime_type") or _audio_mime_from_path(track.rel_path)),
                "sourcePlan": _editor_audio_source_plan(raw, track),
                "prompt": str(raw.get("prompt") or raw.get("text") or raw.get("voice_direction") or ""),
            }
        )
    return cues


def _editor_recipe_markers(
    timeline: dict[str, Any],
    duration: float,
    clips: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    raw_markers = timeline.get("markers")
    if isinstance(raw_markers, list):
        for index, item in enumerate(raw_markers, start=1):
            if not isinstance(item, dict):
                continue
            markers.append(
                {
                    "id": str(item.get("id") or f"marker-{index:03d}"),
                    "time": round(max(0.0, min(_coerce_float(item.get("time"), 0.0), duration)), 3),
                    "label": str(item.get("label") or item.get("title") or f"Marker {index}"),
                    "color": str(item.get("color") or _editor_marker_color(index)),
                    "notes": str(item.get("notes") or ""),
                }
            )
    if not markers:
        cursor = 0.0
        for index, clip in enumerate(clips, start=1):
            markers.append(
                {
                    "id": f"marker-{index:03d}",
                    "time": round(min(cursor, duration), 3),
                    "label": str(clip.get("label") or f"Clip {index}"),
                    "color": _editor_marker_color(index),
                    "notes": "",
                }
            )
            cursor += max(0.0, _coerce_float(clip.get("sourceEnd"), 0.0) - _coerce_float(clip.get("sourceStart"), 0.0))
    if shots and len(markers) < len(shots):
        for index, shot in enumerate(shots, start=1):
            markers.append(
                {
                    "id": f"shot-marker-{index:03d}",
                    "time": round(_coerce_float(shot.get("start"), 0.0), 3),
                    "label": str(shot.get("title") or shot.get("shot") or f"Shot {index}"),
                    "color": _editor_marker_color(index),
                    "notes": str(shot.get("notes") or ""),
                }
            )
    markers.append(
        {
            "id": "marker-final-render",
            "time": round(duration, 3),
            "label": "Final render end",
            "color": "#ef4444",
            "notes": "End of the AI-composed final master.",
        }
    )
    markers.sort(key=lambda marker: (marker["time"], marker["id"]))
    return markers


def _timeline_item_start_end(
    item: dict[str, Any],
    cursor: float,
    default_span: float,
    total_duration: float,
) -> tuple[float, float]:
    range_start, range_end = _parse_timeline_range(item.get("time") or item.get("range"))
    raw_start = _first_present(item, ("timelineStart", "timeline_start", "start", "in"))
    raw_end = _first_present(item, ("timelineEnd", "timeline_end", "end", "out"))
    raw_duration = _first_present(item, ("duration", "duration_seconds", "target_duration"))
    start = range_start if range_start is not None else _coerce_float(raw_start, cursor)
    if range_end is not None:
        end = range_end
    elif raw_end is not None:
        end = _coerce_float(raw_end, start + default_span)
    elif raw_duration is not None:
        end = start + max(0.05, _coerce_float(raw_duration, default_span))
    else:
        end = start + default_span
    start = max(0.0, min(start, max(total_duration, start)))
    end = max(start + 0.05, min(end, max(total_duration, end)))
    return start, end


def _parse_timeline_range(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, str) or "-" not in value:
        return None, None
    left, right = [part.strip() for part in value.split("-", 1)]
    start = _coerce_float(left, -1.0)
    end = _coerce_float(right, -1.0)
    if start < 0 or end <= start:
        return None, None
    return start, end


def _parse_editor_subtitle_time(value: str) -> float:
    token = value.strip().split()[0].replace(",", ".")
    parts = token.split(":")
    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
        return float(token)
    except ValueError:
        return 0.0


def _timeline_media_path(item: dict[str, Any]) -> str:
    for key in (
        "path",
        "fs_path",
        "video_path",
        "clip_path",
        "output_path",
        "result_path",
        "result_url",
        "url",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _editor_label(item: dict[str, Any], index: int, *, prefix: str) -> str:
    for key in ("label", "title", "name", "shot_title", "purpose", "shot_id", "clip_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{prefix} {index}"


def _editor_color(index: int) -> str:
    colors = ["#0f766e", "#2563eb", "#9333ea", "#ea580c", "#be123c"]
    return colors[(index - 1) % len(colors)]


def _editor_marker_color(index: int) -> str:
    colors = ["#f59e0b", "#14b8a6", "#3b82f6", "#a855f7", "#ef4444"]
    return colors[(index - 1) % len(colors)]


def _editor_clip_notes(item: dict[str, Any], media_path: str) -> str:
    notes: list[str] = []
    for key in ("notes", "qc_notes", "visual_notes", "video_prompt", "motion_prompt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            notes.append(value.strip())
    if media_path:
        notes.append(f"Source asset: {media_path}")
    return "\n".join(notes)


def _editor_shot_action(item: dict[str, Any]) -> str:
    for key in ("action", "blocking", "description", "visual", "motion", "video_prompt"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    panels = item.get("panels")
    if isinstance(panels, list):
        parts: list[str] = []
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            text = " ".join(
                str(panel.get(key) or "").strip()
                for key in ("frame", "blocking", "action")
                if str(panel.get(key) or "").strip()
            )
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _editor_shot_dialogue(item: dict[str, Any]) -> str:
    direct = item.get("dialogue") or item.get("line") or item.get("subtitle")
    if isinstance(direct, str):
        return direct.strip()
    cues = item.get("dialogue_cues")
    if isinstance(cues, list):
        lines = []
        for cue in cues:
            if not isinstance(cue, dict):
                continue
            speaker = str(cue.get("speaker") or cue.get("character") or "").strip()
            text = str(cue.get("text") or cue.get("line") or cue.get("subtitle") or "").strip()
            if text:
                lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines)
    return ""


def _editor_shot_notes(item: dict[str, Any]) -> str:
    notes: list[str] = []
    for key in ("sound", "audio", "notes", "first_frame", "end_frame", "still_prompt_id", "motion_prompt_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            notes.append(f"{key}: {value.strip()}")
    panels = item.get("panels")
    if isinstance(panels, list):
        for panel in panels:
            if isinstance(panel, dict) and isinstance(panel.get("sound"), str) and panel["sound"].strip():
                notes.append(f"panel sound: {panel['sound'].strip()}")
    return "\n".join(notes)


def _editor_audio_type(track_type: str) -> str:
    normalized = (track_type or "").strip().lower()
    if normalized in {"dialogue", "narration", "music", "ambience", "sfx"}:
        return normalized
    if normalized in {"soundscape", "roomtone", "room_tone"}:
        return "ambience"
    if normalized in {"foley", "transition", "effect", "audio"}:
        return "sfx"
    return "ambience"


def _editor_audio_label(track: TimelineAudioTrack, raw: dict[str, Any]) -> str:
    for key in ("label", "title", "name", "character", "speaker", "text"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{track.track_type}: {Path(track.rel_path).name}"


def _editor_audio_source_plan(raw: dict[str, Any], track: TimelineAudioTrack) -> str:
    for key in ("sourcePlan", "source_plan", "generation_plan", "reuse_plan"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Timeline audio asset: {track.rel_path}"


def _audio_mime_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".flac":
        return "audio/flac"
    if suffix == ".webm":
        return "audio/webm"
    return "audio/*"


def _editor_canvas_size(spec: dict[str, Any], media_info: dict[str, Any]) -> tuple[int, int]:
    width = int(_coerce_float(media_info.get("width"), 0.0))
    height = int(_coerce_float(media_info.get("height"), 0.0))
    if width > 0 and height > 0:
        return width, height
    resolution = str(spec.get("resolution") or "")
    aspect_ratio = str(spec.get("aspect_ratio") or "16:9")
    if resolution:
        return _target_dimensions(resolution, aspect_ratio)
    return 1920, 1080


def _looks_like_audio_timeline_track(item: dict[str, Any]) -> bool:
    track_type = str(item.get("type") or item.get("kind") or "").strip().lower()
    if track_type:
        return track_type in AUDIO_TIMELINE_TYPES
    raw_path = str(item.get("path") or item.get("fs_path") or "").strip()
    return Path(raw_path).suffix.lower() in AUDIO_EXTENSIONS


def _timeline_track_end(item: dict[str, Any], start: float, source_duration: float, index: int) -> float:
    if item.get("end") is not None:
        return _coerce_required_time(item.get("end"), f"audio_tracks[{index}].end")
    track_type = str(item.get("type") or "").lower()
    if track_type in {"music", "ambience"}:
        raise ValueError(f"audio_tracks[{index}].end is required for {track_type} tracks")
    if str(item.get("end_source") or "") == "probe_duration":
        return start + source_duration
    raise ValueError(f"audio_tracks[{index}].end or end_source='probe_duration' is required")


def _coerce_required_time(value: Any, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} is required")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if number < 0:
        raise ValueError(f"{label} must be non-negative")
    return number


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _subtitle_track_types(value: list[str] | str | None) -> set[str]:
    items = _string_set(value)
    return items or {"dialogue", "narration"}


async def _collect_subtitle_cues(
    *,
    ffprobe: str,
    entity_root: str,
    entity_id: str,
    timeline: dict[str, Any],
    cue_payloads: list[Any],
    track_types: set[str],
) -> list[SubtitleCue]:
    raw_items: list[dict[str, Any]] = []
    raw_items.extend(_subtitle_items_from_payload(timeline))
    for payload in cue_payloads:
        raw_items.extend(_subtitle_items_from_payload(payload))

    cues: list[SubtitleCue] = []
    for item in raw_items:
        cue_type = str(item.get("type") or item.get("kind") or item.get("role") or "dialogue").strip().lower()
        if cue_type not in track_types:
            continue
        text = _subtitle_text(item)
        if not text:
            continue
        start = _coerce_required_time(item.get("start"), "subtitle cue start")
        end, estimated = await _subtitle_cue_end(
            item=item,
            start=start,
            ffprobe=ffprobe,
            entity_root=entity_root,
            entity_id=entity_id,
        )
        if end <= start:
            continue
        cues.append(
            SubtitleCue(
                index=len(cues) + 1,
                start=start,
                end=end,
                text=text,
                cue_type=cue_type,
                source_path=str(item.get("path") or item.get("fs_path") or ""),
                estimated=estimated,
            )
        )
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return [
        SubtitleCue(
            index=index,
            start=cue.start,
            end=cue.end,
            text=cue.text,
            cue_type=cue.cue_type,
            source_path=cue.source_path,
            estimated=cue.estimated,
        )
        for index, cue in enumerate(cues, start=1)
    ]


def _subtitle_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []
    for key in ("subtitle_cues", "cues", "items", "dialogue", "dialogue_cues", "captions"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    subtitles = payload.get("subtitles")
    if isinstance(subtitles, dict):
        for key in ("cues", "items", "captions"):
            value = subtitles.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    raw_tracks, _source_key = _timeline_audio_track_items(payload)
    if isinstance(raw_tracks, dict):
        raw_tracks = raw_tracks.get("tracks")
    if isinstance(raw_tracks, list):
        for item in raw_tracks:
            if not isinstance(item, dict):
                continue
            cue_type = str(item.get("type") or item.get("kind") or "").lower()
            if cue_type in {"dialogue", "narration"}:
                items.append(item)
    return items


def _subtitle_text(item: dict[str, Any]) -> str:
    for key in ("subtitle", "subtitle_text", "text", "dialogue", "line", "content", "caption"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


async def _subtitle_cue_end(
    *,
    item: dict[str, Any],
    start: float,
    ffprobe: str,
    entity_root: str,
    entity_id: str,
) -> tuple[float, bool]:
    if item.get("end") is not None:
        return _coerce_required_time(item.get("end"), "subtitle cue end"), False
    if item.get("duration") is not None:
        return start + max(0.01, _coerce_float(item.get("duration"), 0.01)), False

    raw_path = str(item.get("path") or item.get("fs_path") or "").strip()
    if raw_path:
        if not ffprobe:
            raise ValueError("ffprobe is required to derive subtitle end from audio duration")
        rel_path = _rel_path_from_reference(raw_path, entity_id) or raw_path
        abs_path = _resolve_entity_file(entity_root, rel_path)
        _assert_audio_path(abs_path)
        media_info = await _probe_media(ffprobe, abs_path)
        duration = max(0.01, float(media_info.get("duration_seconds") or 0.01))
        return start + duration, False

    raise ValueError("subtitle cue end requires end, duration, or a referenced audio path")


def _render_subtitles(
    cues: list[SubtitleCue],
    *,
    subtitle_format: str,
    max_chars_per_line: int,
    style: dict[str, Any],
) -> str:
    if subtitle_format == "vtt":
        body = ["WEBVTT", ""]
        for cue in cues:
            body.extend(
                [
                    str(cue.index),
                    f"{_format_vtt_time(cue.start)} --> {_format_vtt_time(cue.end)}",
                    _wrap_subtitle_text(cue.text, max_chars_per_line),
                    "",
                ]
            )
        return "\n".join(body)
    if subtitle_format == "ass":
        return _render_ass_subtitles(cues, max_chars_per_line=max_chars_per_line, style=style)

    body = []
    for cue in cues:
        body.extend(
            [
                str(cue.index),
                f"{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}",
                _wrap_subtitle_text(cue.text, max_chars_per_line),
                "",
            ]
        )
    return "\n".join(body)


def _render_ass_subtitles(
    cues: list[SubtitleCue],
    *,
    max_chars_per_line: int,
    style: dict[str, Any],
) -> str:
    force_style = _subtitle_force_style(style)
    style_map = _ass_style_map(force_style)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Default,"
        f"{style_map.get('Fontname', 'Arial')},"
        f"{style_map.get('Fontsize', '42')},"
        f"{style_map.get('PrimaryColour', '&H00FFFFFF')},"
        "&H000000FF,"
        f"{style_map.get('OutlineColour', '&H00000000')},"
        f"{style_map.get('BackColour', '&H80000000')},"
        f"{style_map.get('Bold', '0')},0,0,0,100,100,0,0,1,"
        f"{style_map.get('Outline', '2')},"
        f"{style_map.get('Shadow', '1')},"
        f"{style_map.get('Alignment', '2')},40,40,"
        f"{style_map.get('MarginV', '80')},1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for cue in cues:
        text = _wrap_subtitle_text(cue.text, max_chars_per_line).replace("\n", r"\N")
        text = text.replace("{", r"\{").replace("}", r"\}")
        header.append(
            f"Dialogue: 0,{_format_ass_time(cue.start)},{_format_ass_time(cue.end)},"
            f"Default,,0,0,0,,{text}"
        )
    return "\n".join(header) + "\n"


def _wrap_subtitle_text(text: str, max_chars_per_line: int) -> str:
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars_per_line:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_seconds = total_ms // 1000
    sec = total_seconds % 60
    total_minutes = total_seconds // 60
    minute = total_minutes % 60
    hour = total_minutes // 60
    return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"


def _format_vtt_time(seconds: float) -> str:
    return _format_srt_time(seconds).replace(",", ".")


def _format_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    cs = total_cs % 100
    total_seconds = total_cs // 100
    sec = total_seconds % 60
    total_minutes = total_seconds // 60
    minute = total_minutes % 60
    hour = total_minutes // 60
    return f"{hour}:{minute:02d}:{sec:02d}.{cs:02d}"


def _ducking_intervals(
    audio_tracks: list[TimelineAudioTrack],
    ducking_config: dict[str, Any],
) -> list[tuple[float, float]]:
    if not ducking_config.get("enabled"):
        return []
    sidechain_types = set(ducking_config.get("sidechain_types") or {"dialogue", "narration"})
    intervals = [
        (track.start, track.end)
        for track in audio_tracks
        if track.track_type.lower() in sidechain_types and track.end > track.start
    ]
    return _merge_intervals(intervals)


def _ducking_volume_filter(
    track: TimelineAudioTrack,
    intervals: list[tuple[float, float]],
    ducking_config: dict[str, Any],
) -> str:
    if not intervals or not ducking_config.get("enabled"):
        return ""
    target_types = set(ducking_config.get("target_types") or {"music", "ambience"})
    if track.track_type.lower() not in target_types:
        return ""
    padding = float(ducking_config.get("padding") or 0.0)
    local_intervals: list[tuple[float, float]] = []
    for start, end in intervals:
        overlap_start = max(track.start, start - padding)
        overlap_end = min(track.end, end + padding)
        if overlap_end > overlap_start:
            local_intervals.append((overlap_start - track.start, overlap_end - track.start))
    if not local_intervals:
        return ""
    condition = "+".join(
        f"between(t\\,{start:.3f}\\,{end:.3f})"
        for start, end in _merge_intervals(local_intervals)
    )
    amount_db = ducking_config.get("amount_db")
    if amount_db is None:
        amount_db = -9.0
    factor = 10 ** (float(amount_db) / 20.0)
    return f"volume='if({condition}\\,{factor:.6f}\\,1)':eval=frame"


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _loudnorm_filter(config: dict[str, Any]) -> str:
    if not config.get("enabled"):
        return ""
    return (
        f"loudnorm=I={_loudness_target_lufs(config.get('target_lufs')):.1f}:"
        f"TP={_loudness_true_peak(config.get('true_peak')):.1f}:"
        f"LRA={_loudness_lra(config.get('lra')):.1f}:print_format=summary"
    )


def _loudness_target_lufs(value: Any) -> float:
    return _clamp_float(value, -24.0, -6.0, -16.0)


def _loudness_true_peak(value: Any) -> float:
    return _clamp_float(value, -6.0, 0.0, -1.5)


def _loudness_lra(value: Any) -> float:
    return _clamp_float(value, 1.0, 20.0, 11.0)


async def _compose_video_file(
    *,
    ffmpeg: str,
    entity_id: str,
    output_name: str,
    workspace_id: str | None,
    clean_video_abs: str,
    subtitle_abs: str,
    subtitle_style: dict[str, Any],
    audio_tracks: list[TimelineAudioTrack],
    include_source_audio: bool,
    ducking_config: dict[str, Any],
    loudness_config: dict[str, Any],
    crf: int,
    preset: str,
    total_duration: float,
) -> tuple[str, str, str]:
    from packages.core.services import entity_fs
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )

    entity_root = entity_fs.get_entity_root(entity_id)
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    target = build_generated_media_target(
        prompt=output_name,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=".mp4",
        fallback="composed-video",
        default_dir=workspace_artifact_default_dir(workspace_base_dir, "videos"),
        entity_root=entity_root,
    )
    if not target.abs_dir or not target.abs_path:
        raise ValueError("Could not resolve output path")
    os.makedirs(target.abs_dir, exist_ok=True)

    args = [ffmpeg, "-y", "-i", clean_video_abs]
    for track in audio_tracks:
        if track.loop:
            args.extend(["-stream_loop", "-1", "-t", f"{track.duration:.3f}"])
        args.extend(["-i", track.abs_path])

    filter_parts: list[str] = []
    video_map = "0:v:0"
    if subtitle_abs:
        video_map = "vout"
        filter_parts.append(f"[0:v:0]subtitles={_subtitles_filter_value(subtitle_abs, subtitle_style)}[vout]")

    audio_labels: list[str] = []
    ducking_intervals = _ducking_intervals(audio_tracks, ducking_config)
    if include_source_audio:
        filter_parts.append(
            "[0:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[srca]"
        )
        audio_labels.append("srca")
    for index, track in enumerate(audio_tracks, start=1):
        label = f"a{index}"
        delay_ms = int(round(track.start * 1000))
        duration = max(0.01, track.duration)
        effects = [
            f"[{index}:a:0]aresample=48000",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"atrim=0:{duration:.3f}",
            "asetpts=PTS-STARTPTS",
            f"volume={track.volume_db}dB",
        ]
        duck_filter = _ducking_volume_filter(track, ducking_intervals, ducking_config)
        if duck_filter:
            effects.append(duck_filter)
        if track.fade_in > 0:
            effects.append(f"afade=t=in:st=0:d={min(track.fade_in, duration):.3f}")
        if track.fade_out > 0:
            fade_start = max(0.0, duration - track.fade_out)
            effects.append(f"afade=t=out:st={fade_start:.3f}:d={min(track.fade_out, duration):.3f}")
        effects.append(f"adelay={delay_ms}|{delay_ms}")
        filter_parts.append(",".join(effects) + f"[{label}]")
        audio_labels.append(label)

    output_audio_label = ""
    if audio_labels:
        output_audio_label = "aout"
        mix_chain = (
            "".join(f"[{label}]" for label in audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0:normalize=0"
        )
        if total_duration > 0:
            mix_chain += f",atrim=0:{total_duration:.3f},asetpts=PTS-STARTPTS"
        loudnorm = _loudnorm_filter(loudness_config)
        if loudnorm:
            mix_chain += f",{loudnorm}"
        filter_parts.append(mix_chain + "[aout]")
    else:
        output_audio_label = "aout"
        silent_duration = max(0.1, total_duration or 0.1)
        args.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{silent_duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
        silent_index = len(audio_tracks) + 1
        filter_parts.append(f"[{silent_index}:a:0]anull[aout]")

    if filter_parts:
        args.extend(["-filter_complex", ";".join(filter_parts)])
        mapped_video = f"[{video_map}]" if video_map == "vout" else video_map
        args.extend(["-map", mapped_video, "-map", f"[{output_audio_label}]"])
    else:
        args.extend(["-map", video_map, "-map", "0:a:0?"])

    args.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            target.abs_path,
        ]
    )
    await _run_process(args, timeout_seconds=max(180.0, (total_duration or 60.0) * 8.0 + 120.0))
    return target.abs_path, target.rel_path, target.filename


async def _probe_media(ffprobe: str, path: str) -> dict[str, Any]:
    stdout, _stderr = await _run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            path,
        ],
        timeout_seconds=60,
    )
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from exc

    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    has_audio = any(stream.get("codec_type") == "audio" for stream in data.get("streams") or [])
    return {"duration_seconds": duration, "has_audio": has_audio}


async def _normalize_clip(
    *,
    ffmpeg: str,
    input_path: str,
    output_path: str,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
    duration_seconds: float,
    has_audio: bool,
    include_source_audio: bool,
) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={fps},format=yuv420p"
    )
    base = [
        ffmpeg,
        "-y",
        "-i",
        input_path,
    ]
    if has_audio and include_source_audio:
        args = [
            *base,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            output_path,
        ]
    else:
        silent_duration = max(0.1, duration_seconds or 0.1)
        args = [
            *base,
            "-f",
            "lavfi",
            "-t",
            f"{silent_duration:.3f}",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            output_path,
        ]
    await _run_process(args, timeout_seconds=max(120.0, duration_seconds * 8.0 + 60.0))


async def _run_process(args: list[str], *, timeout_seconds: float) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"Command timed out after {timeout_seconds:.0f}s: {args[0]}") from exc
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"{args[0]} failed with exit code {process.returncode}: {stderr_text[-2000:]}")
    return stdout_text, stderr_text


def _target_dimensions(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    long_side_by_resolution = {"480p": 480, "720p": 720, "1080p": 1080}
    base = long_side_by_resolution.get(resolution, 1080)
    ratios = {
        "adaptive": (16, 9),
        "21:9": (21, 9),
        "16:9": (16, 9),
        "9:16": (9, 16),
        "1:1": (1, 1),
        "4:3": (4, 3),
        "3:4": (3, 4),
    }
    numerator, denominator = ratios.get(aspect_ratio, (16, 9))
    if numerator >= denominator:
        height = base
        width = round(base * numerator / denominator)
    else:
        width = base
        height = round(base * denominator / numerator)
    return _even(width), _even(height)


def _even(value: int) -> int:
    return int(value) if int(value) % 2 == 0 else int(value) + 1


def _string_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.split(",") if "," in value else [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [str(value)]
    result: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _string_set(value: list[str] | str | set[str] | tuple[str, ...] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, set):
        raw_items = list(value)
    elif isinstance(value, tuple):
        raw_items = list(value)
    else:
        raw_items = _string_list(value)  # type: ignore[arg-type]
    return {str(item).strip().lower() for item in raw_items if str(item).strip()}


def _job_requested_duration_seconds(job: Any) -> float:
    params = getattr(job, "params", {}) or {}
    value = params.get("duration") or getattr(job, "duration_seconds", None) or 5
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return 5.0


def _job_resolution_factor(job: Any) -> float:
    params = getattr(job, "params", {}) or {}
    resolution = str(params.get("resolution") or "").lower()
    if "1080" in resolution:
        return 60.0
    if "480" in resolution:
        return 35.0
    return 45.0


def _default_wait_timeout_seconds(jobs: list[Any]) -> float:
    """Derive a wait budget from the requested media jobs.

    Video providers vary a lot under load. A fixed 300s budget can report a
    timeout even though the provider job is still healthy, especially for longer
    or 1080p clips. Keep the chat wait bounded, but scale it with the slowest
    requested clip and a small multi-job queue allowance.
    """
    if not jobs:
        return 240.0
    slowest = max(
        _job_requested_duration_seconds(job) * _job_resolution_factor(job)
        for job in jobs
    )
    queue_allowance = min(len(jobs), 8) * 15.0
    return min(MAX_WAIT_SECONDS, max(240.0, slowest + 120.0 + queue_allowance))


def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _rel_path_from_reference(value: str | None, entity_id: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    marker = f"/api/v1/fs/{entity_id}/"
    if path.startswith(marker):
        return _normalize_user_path(unquote(path[len(marker):]))
    alt_marker = f"api/v1/fs/{entity_id}/"
    if path.startswith(alt_marker):
        return _normalize_user_path(unquote(path[len(alt_marker):]))
    return ""


def _normalize_user_path(path: str) -> str:
    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    rel_path = normalize_rel_path(unquote(path or ""))
    if not rel_path or not is_user_visible_path(rel_path):
        raise ValueError(f"Unsafe or hidden Knowledge path: {path}")
    return rel_path


def _resolve_entity_file(entity_root: str, rel_path: str) -> str:
    rel = _normalize_user_path(rel_path)
    root = os.path.realpath(entity_root)
    full_path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full_path]) != root:
        raise ValueError(f"Path escapes entity root: {rel_path}")
    if not os.path.isfile(full_path):
        raise ValueError(f"Media file not found: {rel}")
    return full_path


def _resolve_entity_dir(entity_root: str, rel_path: str) -> str:
    rel = _normalize_user_path(rel_path)
    root = os.path.realpath(entity_root)
    full_path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full_path]) != root:
        raise ValueError(f"Path escapes entity root: {rel_path}")
    if not os.path.isdir(full_path):
        raise ValueError(f"Folder not found: {rel}")
    return full_path


def _assert_video_path(abs_path: str) -> None:
    if Path(abs_path).suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported video extension: {abs_path}")


def _assert_image_path(abs_path: str) -> None:
    if Path(abs_path).suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension: {abs_path}")


def _assert_audio_path(abs_path: str) -> None:
    if Path(abs_path).suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(f"Unsupported audio extension: {abs_path}")


def _assert_subtitle_path(abs_path: str) -> None:
    if Path(abs_path).suffix.lower() not in {".srt", ".vtt", ".ass"}:
        raise ValueError(f"Unsupported subtitle extension: {abs_path}")


def _subtitles_filter_value(abs_path: str, style: dict[str, Any] | None = None) -> str:
    escaped = abs_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    value = f"'{escaped}'"
    force_style = _subtitle_force_style(style or {})
    if force_style:
        value += f":force_style='{force_style}'"
    return value


def _subtitle_force_style(style: dict[str, Any]) -> str:
    if not isinstance(style, dict) or not style:
        return ""
    mapping = {
        "font_name": "Fontname",
        "font": "Fontname",
        "font_size": "Fontsize",
        "fontsize": "Fontsize",
        "primary_color": "PrimaryColour",
        "color": "PrimaryColour",
        "outline_color": "OutlineColour",
        "back_color": "BackColour",
        "background_color": "BackColour",
        "alignment": "Alignment",
        "margin_v": "MarginV",
        "outline": "Outline",
        "shadow": "Shadow",
        "bold": "Bold",
    }
    parts: list[str] = []
    for raw_key, value in style.items():
        key = mapping.get(str(raw_key))
        if not key or value is None:
            continue
        if key.endswith("Colour"):
            rendered = _ass_color(value)
        elif key == "Bold":
            rendered = "-1" if bool(value) else "0"
        elif key in {"Fontsize", "Alignment", "MarginV", "Outline", "Shadow"}:
            rendered = str(int(_coerce_float(value, 0)))
        else:
            rendered = str(value).replace(",", " ").strip()
        if rendered:
            parts.append(f"{key}={rendered}")
    return ",".join(parts)


def _ass_style_map(force_style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in force_style.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _ass_color(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("&H"):
        return raw
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 6:
        rr, gg, bb = raw[0:2], raw[2:4], raw[4:6]
        return f"&H00{bb}{gg}{rr}".upper()
    if len(raw) == 8:
        aa, rr, gg, bb = raw[0:2], raw[2:4], raw[4:6], raw[6:8]
        return f"&H{aa}{bb}{gg}{rr}".upper()
    return raw.replace(",", " ")


def _subtitle_mime(fmt: str) -> str:
    return {
        "ass": "text/x-ssa",
        "vtt": "text/vtt",
        "srt": "application/x-subrip",
    }.get(fmt.lower(), "text/plain")


def _audio_mime(fmt: str) -> str:
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "flac": "audio/flac",
    }.get(fmt.lower(), "application/octet-stream")


def _audio_codec_args(fmt: str) -> list[str]:
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if fmt == "m4a":
        return ["-c:a", "aac", "-b:a", "192k"]
    if fmt == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "pcm_s16le"]


def _is_video_document(doc: Any) -> bool:
    mime = (getattr(doc, "mime_type", "") or "").lower()
    file_type = (getattr(doc, "file_type", "") or "").lower().lstrip(".")
    fs_path = getattr(doc, "fs_path", "") or ""
    return (
        mime.startswith("video/")
        or f".{file_type}" in VIDEO_EXTENSIONS
        or Path(fs_path).suffix.lower() in VIDEO_EXTENSIONS
    )


def _dedupe_inputs(inputs: list[VideoInput]) -> list[VideoInput]:
    seen: set[str] = set()
    deduped: list[VideoInput] = []
    for item in inputs:
        key = os.path.realpath(item.abs_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _concat_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace("'", "\\'")


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_error(message: str, *, code: str = "invalid_request") -> str:
    return _json({"status": "error", "code": code, "error": message})


def get_tools():
    return [
        (WAIT_MEDIA_JOBS_SCHEMA, _wait_media_jobs_handler),
        (MERGE_VIDEOS_SCHEMA, _merge_videos_handler),
        (ALIGN_SUBTITLES_SCHEMA, _align_subtitles_handler),
        (NORMALIZE_AUDIO_LOUDNESS_SCHEMA, _normalize_audio_loudness_handler),
        (COMPOSE_VIDEO_TIMELINE_SCHEMA, _compose_video_timeline_handler),
    ]
