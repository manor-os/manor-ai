"""Media orchestration tools for generated assets and video assembly."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select

from packages.core.ai.runtime.file_actions import (
    runtime_copy_entity_file_atomic,
    runtime_guard_file_read_access,
    runtime_write_entity_file_atomic,
)
from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_handler,
)
from packages.core.models.media_job import MediaJobStatus
from packages.core.models.base import generate_ulid
from packages.core.services.audio_conversion import ffmpeg_audio_codec_args
from packages.core.services.workspace_layout import WorkspaceArtifactDir

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _MediaFileAccessContext:
    entity_id: str
    user_id: str | None
    workspace_id: str | None
    runtime_envelope: Any | None
    tool_name: str
    enforce_acl: bool


_CURRENT_MEDIA_FILE_ACCESS: contextvars.ContextVar[_MediaFileAccessContext | None] = (
    contextvars.ContextVar("media_file_access", default=None)
)


def _with_media_file_access(handler):
    """Bind trusted runtime scope for every nested media source resolution."""

    @wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any) -> str:
        inherited = _CURRENT_MEDIA_FILE_ACCESS.get()
        runtime_injected = any(
            key in kwargs
            for key in (
                "_user_id_from_context",
                "_runtime_envelope_from_context",
                "_runtime_tool_call_id_from_context",
            )
        )
        direct_user_id = str(kwargs.get("user_id") or "").strip() or None
        runtime_context = runtime_tool_call_context_from_handler(
            kwargs,
            user_id=direct_user_id,
        )
        tool_name = handler.__name__.removeprefix("_").removesuffix("_handler")
        entity_id = str(kwargs.get("entity_id") or "").strip()
        if inherited is not None and inherited.enforce_acl and not runtime_injected:
            if entity_id != inherited.entity_id:
                raise ValueError("Nested media tools must stay in the runtime Entity scope")
            access = replace(inherited, tool_name=tool_name)
        else:
            access = _MediaFileAccessContext(
                entity_id=entity_id,
                user_id=runtime_context.user_id,
                workspace_id=runtime_context.workspace_id,
                runtime_envelope=runtime_context.runtime_envelope,
                tool_name=tool_name,
                enforce_acl=runtime_injected,
            )
        token = _CURRENT_MEDIA_FILE_ACCESS.set(access)
        try:
            return await handler(*args, **kwargs)
        finally:
            _CURRENT_MEDIA_FILE_ACCESS.reset(token)

    return wrapped


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

# Single definition lives on the model; this alias keeps existing callers.
TERMINAL_JOB_STATUSES = MediaJobStatus.terminal()
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MAX_WAIT_SECONDS = 900.0
NARRATION_MIN_TEMPO_FACTOR = 0.75
NARRATION_MAX_TEMPO_FACTOR = 1.5
NARRATION_MAX_BLOCK_TEMPO_RELATIVE_SPREAD = 0.03


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
                "clips": {
                    "type": "array",
                    "description": (
                        "Ordered clips with one source key and optional trim/speed. "
                        "Do not mix with legacy source inputs."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "document_id": {"type": "string"},
                            "job_id": {"type": "string"},
                            "path": {"type": "string"},
                            "start_seconds": {"type": "number", "minimum": 0},
                            "end_seconds": {"type": "number", "minimum": 0},
                            "speed": {
                                "type": "number",
                                "minimum": 0.25,
                                "maximum": 8,
                            },
                            "label": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
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
                "require_audio": {
                    "type": "boolean",
                    "default": False,
                    "description": "Reject composition when no usable timeline audio tracks resolve.",
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
    start_seconds: float = 0.0
    end_seconds: float | None = None
    speed: float = 1.0
    label: str | None = None


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
    measured: bool = False
    timing_source: str = ""


ALIGN_SUBTITLES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "align_subtitles",
        "description": (
            "Create SRT/VTT/ASS subtitles from canonical narration and timeline or cue JSON. "
            "When final narration audio is supplied, uses measured speech timestamps and "
            "ordered semantic sentence alignment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeline_path": {"type": "string", "description": "Timeline JSON path or /api/v1/fs URL."},
                "cues_path": {"type": "string", "description": "Dialogue/subtitle cue JSON path or /api/v1/fs URL."},
                "transcript_path": {
                    "type": "string",
                    "description": "Canonical narration text file. Cue text must match it verbatim.",
                },
                "audio_path": {
                    "type": "string",
                    "description": (
                        "Final rendered narration audio to transcribe for measured semantic "
                        "sentence timing. Requires a timestamp-capable STT route."
                    ),
                },
                "require_audio_transcript_match": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Fail when narration generation metadata cannot prove that its prompt "
                        "matches transcript_path."
                    ),
                },
                "output_name": {"type": "string", "description": "Output subtitle path/filename."},
                "format": {"type": "string", "enum": ["srt", "vtt", "ass"], "default": "srt"},
                "track_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cue types to include; defaults to dialogue and narration.",
                },
                "max_chars_per_line": {"type": "integer", "minimum": 16, "maximum": 56, "default": 34},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2, "default": 2},
                "style": {"type": "object", "description": "ASS/libass style: font_name, font_size, colors, alignment, outline, shadow, margin_v."},
            },
            "required": ["output_name"],
        },
    },
}


BUILD_NARRATION_TIMELINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "build_narration_timeline",
        "description": (
            "Build durable subtitle cues and audio-track offsets from caption-sized "
            "TTS audio segments measured with ffprobe and FFmpeg silence detection. "
            "Each segment must preserve provenance for its exact spoken text. Omit "
            "text to derive it directly from the audio receipt and avoid retyping drift."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "transcript_path": {
                    "type": "string",
                    "description": "Canonical narration text file.",
                },
                "segments": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "audio_path": {"type": "string"},
                            "text": {
                                "type": "string",
                                "description": (
                                    "Optional exact TTS prompt. Prefer omitting it so the "
                                    "Harness derives text from the audio receipt."
                                ),
                            },
                            "start_seconds": {
                                "type": "number",
                                "minimum": 0,
                                "description": (
                                    "Optional absolute timeline start. Use it on the first "
                                    "segment of a fixed visual block; omitted segments follow "
                                    "the previous measured segment without a gap."
                                ),
                            },
                        },
                        "required": ["audio_path"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Ordered normalized TTS audio segments. The Harness derives omitted "
                        "text from each segment's immutable TTS provenance."
                    ),
                },
                "require_normalized_segments": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Require every audio_path to be a normalize_audio_loudness output "
                        "with a positive target_duration_seconds receipt. Keep true for "
                        "production narration so a compacted agent cannot silently mix raw TTS."
                    ),
                },
                "block_durations_seconds": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "number", "minimum": 0.25, "maximum": 60},
                    "description": (
                        "Ordered visual-block durations. When supplied, the Harness "
                        "requires one explicit segment start at every cumulative block "
                        "boundary and verifies measured narration occupancy per block."
                    ),
                },
                "minimum_block_fill_ratio": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1,
                    "default": 0.72,
                },
                "maximum_block_fill_ratio": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1,
                    "default": 0.9,
                },
                "initial_quality_warnings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Previously measured narration timing warnings that must remain attached "
                        "when rebuilding a timeline from unchanged normalized audio."
                    ),
                },
                "manifest_name": {
                    "type": "string",
                    "description": "Narration manifest output path or filename.",
                },
                "timeline_name": {
                    "type": "string",
                    "description": "Narration timeline JSON output path or filename.",
                },
                "cues_name": {
                    "type": "string",
                    "description": "Subtitle cue JSON output path or filename.",
                },
                "silence_noise_db": {
                    "type": "number",
                    "minimum": -80,
                    "maximum": -20,
                    "default": -50,
                },
                "minimum_silence_seconds": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 2,
                    "default": 0.1,
                },
                "padding_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.25,
                    "default": 0.08,
                },
            },
            "required": ["transcript_path", "segments"],
            "additionalProperties": False,
        },
    },
}


PREPARE_NARRATION_TIMELINE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "prepare_narration_timeline",
        "description": (
            "Deterministically prepare a complete fixed-block narration timeline from "
            "an immutable segment manifest and its stable source TTS files. The Harness "
            "measures every source clip, applies one shared natural tempo factor per "
            "visual block, normalizes loudness, and builds verified cues in one call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "transcript_path": {"type": "string"},
                "segment_manifest_path": {"type": "string"},
                "source_audio_directory": {"type": "string"},
                "normalized_output_directory": {"type": "string"},
                "block_durations_seconds": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "number", "minimum": 0.25, "maximum": 60},
                },
                "occupancy_ratio": {
                    "type": "number",
                    "minimum": 0.72,
                    "maximum": 0.9,
                    "default": 0.84,
                },
                "minimum_block_fill_ratio": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1,
                    "default": 0.72,
                },
                "maximum_block_fill_ratio": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1,
                    "default": 0.9,
                },
                "manifest_name": {"type": "string"},
                "timeline_name": {"type": "string"},
                "cues_name": {"type": "string"},
            },
            "required": [
                "transcript_path",
                "segment_manifest_path",
                "source_audio_directory",
                "normalized_output_directory",
                "block_durations_seconds",
                "timeline_name",
            ],
            "additionalProperties": False,
        },
    },
}


INSPECT_NARRATION_RECOVERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "inspect_narration_recovery",
        "description": (
            "Inspect immutable narration text, segment manifest, and source TTS "
            "receipts before retrying Stickman generation. Returns verified "
            "receipts to reuse and the exact missing segments to regenerate; it "
            "never creates media or changes files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "transcript_path": {"type": "string"},
                "segment_manifest_path": {"type": "string"},
                "source_audio_directory": {"type": "string"},
            },
            "required": [
                "transcript_path",
                "segment_manifest_path",
                "source_audio_directory",
            ],
            "additionalProperties": False,
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
                "target_duration_seconds": {
                    "type": "number",
                    "minimum": 0.25,
                    "maximum": 3600,
                    "description": (
                        "Optional exact output duration. The tool deterministically applies "
                        "FFmpeg tempo adjustment before loudness normalization while preserving "
                        "the narrator profile. Use this to fit locked narration into a fixed block."
                    ),
                },
                "output_format": {"type": "string", "enum": ["wav", "mp3", "m4a", "flac"], "default": "wav"},
            },
            "required": ["input_path", "output_name"],
        },
    },
}


PROBE_MEDIA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "probe_media",
        "description": (
            "Inspect a Knowledge media file with ffprobe and return deterministic "
            "container, stream, duration, video, and audio evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {
                    "type": "string",
                    "description": "Knowledge-relative media path or /api/v1/fs URL.",
                },
            },
            "required": ["input_path"],
            "additionalProperties": False,
        },
    },
}


VERIFY_STICKMAN_FINAL_MEDIA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verify_stickman_final_media",
        "description": (
            "Deterministically verify the final Stickman MP4, its measured subtitle "
            "evidence, and durable narration-quality receipt before publication."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "final_video_path": {"type": "string"},
                "subtitle_path": {"type": "string"},
                "narration_timeline_path": {"type": "string"},
                "target_duration_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 3600,
                },
            },
            "required": [
                "final_video_path",
                "subtitle_path",
                "narration_timeline_path",
                "target_duration_seconds",
            ],
            "additionalProperties": False,
        },
    },
}


RENDER_FRAME_SAMPLES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "render_frame_samples",
        "description": (
            "Render bounded PNG evidence frames from a Knowledge video and register "
            "each frame as a durable Workspace artifact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string"},
                "output_dir": {"type": "string"},
                "timestamps": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0},
                },
                "scene_boundaries": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0},
                },
                "interval_seconds": {
                    "type": "number",
                    "minimum": 0.25,
                    "maximum": 3600,
                    "default": 10,
                },
                "max_samples": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 24,
                    "default": 12,
                },
            },
            "required": ["input_path", "output_dir"],
            "additionalProperties": False,
        },
    },
}


ANALYZE_AUDIO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "analyze_audio",
        "description": (
            "Measure integrated loudness, true peak, and silence intervals for a "
            "Knowledge audio or video file with ffmpeg."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string"},
                "target_lufs_min": {"type": "number", "minimum": -40, "maximum": 0, "default": -20},
                "target_lufs_max": {"type": "number", "minimum": -40, "maximum": 0, "default": -14},
                "max_true_peak_dbfs": {"type": "number", "minimum": -12, "maximum": 0, "default": -1},
                "silence_noise_db": {"type": "number", "minimum": -80, "maximum": -20, "default": -50},
                "minimum_silence_seconds": {"type": "number", "minimum": 0.1, "maximum": 10, "default": 0.5},
                "max_silence_ratio": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.35},
            },
            "required": ["input_path"],
            "additionalProperties": False,
        },
    },
}


VALIDATE_SUBTITLES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "validate_subtitles",
        "description": (
            "Validate SRT, WebVTT, or ASS cue timing, overlap, line count, media "
            "bounds, and available ASS bottom-safe style evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subtitle_path": {"type": "string"},
                "media_path": {"type": "string"},
                "media_duration_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "min_margin_v": {"type": "integer", "minimum": 0, "maximum": 500, "default": 28},
            },
            "required": ["subtitle_path"],
            "additionalProperties": False,
        },
    },
}


STILL_TO_VIDEO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "still_to_video",
        "description": (
            "Convert one explicitly planned Knowledge still image into a bounded "
            "H.264 MP4 scene and register it as a Workspace artifact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string"},
                "output_name": {"type": "string"},
                "duration_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 30,
                    "default": 3,
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p", "1080p"],
                    "default": "1080p",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "1:1", "4:3", "3:4"],
                    "default": "16:9",
                },
                "fps": {"type": "integer", "minimum": 12, "maximum": 60, "default": 30},
                "crf": {"type": "integer", "minimum": 14, "maximum": 28, "default": 18},
                "preset": {
                    "type": "string",
                    "enum": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
                    "default": "veryfast",
                },
            },
            "required": ["input_path", "output_name"],
            "additionalProperties": False,
        },
    },
}


@_with_media_file_access
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


@_with_media_file_access
async def _merge_videos_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    job_ids: list[str] | str | None = None,
    document_ids: list[str] | str | None = None,
    paths: list[str] | str | None = None,
    video_paths: list[str] | str | None = None,
    folder_path: str | None = None,
    clips: list[dict[str, Any]] | None = None,
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
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        target_width, target_height = _target_dimensions(resolution, aspect_ratio)
        target_fps = int(_clamp_float(fps, 12, 60, 30))
        target_crf = int(_clamp_float(crf, 14, 28, 18))
        allowed_presets = MERGE_VIDEOS_SCHEMA["function"]["parameters"]["properties"]["preset"]["enum"]
        target_preset = preset if preset in allowed_presets else "veryfast"

        legacy_sources_present = bool(
            _string_list(job_ids)
            or _string_list(document_ids)
            or _string_list(paths)
            or _string_list(video_paths)
            or (folder_path or "").strip()
        )
        if clips and legacy_sources_present:
            return _json_error(
                "clips cannot be combined with job_ids, document_ids, paths, video_paths, or folder_path."
            )
        if clips:
            inputs = await _resolve_ordered_video_clips(
                entity_id=entity_id,
                clips=clips,
                workspace_id=workspace_id,
                workspace_base_dir=workspace_base_dir,
            )
        else:
            inputs = await _resolve_video_inputs(
                entity_id=entity_id,
                job_ids=_string_list(job_ids),
                document_ids=_string_list(document_ids),
                paths=[*_string_list(paths), *_string_list(video_paths)],
                folder_path=folder_path,
                workspace_id=workspace_id,
                workspace_base_dir=workspace_base_dir,
            )
        if not inputs:
            return _json_error("At least one video input is required to merge or normalize.")

        final_path, final_rel_path, final_filename, input_payloads, total_duration = await _merge_video_files(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            entity_id=entity_id,
            output_name=output_name,
            workspace_id=workspace_id,
            task_id=task_id,
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


@_with_media_file_access
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
    require_audio: bool = False,
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
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        timeline = await _load_timeline_json(
            entity_root,
            timeline_path,
            entity_id,
            workspace_base_dir,
        )
        clean_reference = _timeline_clean_video_path(timeline, clean_video_path)
        if not clean_reference:
            return _json_error(
                "clean_video_path is required when timeline.delivery.clean_picture_master is missing",
                code="clean_video_missing",
            )
        clean_rel = _workspace_media_reference(
            clean_reference,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        clean_abs = await _resolve_entity_file(entity_root, clean_rel)
        _assert_video_path(clean_abs)

        resolved_subtitle = _timeline_subtitle_path(timeline, subtitle_path)
        subtitle_abs = ""
        subtitle_rel = ""
        if burn_subtitles and resolved_subtitle:
            subtitle_rel = _workspace_media_reference(
                resolved_subtitle,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            subtitle_abs = await _resolve_entity_file(entity_root, subtitle_rel)
            _assert_subtitle_path(subtitle_abs)
        media_info = await _probe_media(ffprobe, clean_abs)
        canvas_width, canvas_height = _editor_canvas_size(
            timeline.get("spec") if isinstance(timeline.get("spec"), dict) else timeline,
            media_info,
        )
        resolved_subtitle_style = _compose_subtitle_style(
            subtitle_path=subtitle_abs,
            width=canvas_width,
            height=canvas_height,
            timeline=timeline,
            explicit_override=subtitle_style,
        )
        clean_video_duration = float(media_info.get("duration_seconds") or 0.0)
        try:
            audio_tracks = await _resolve_timeline_audio_tracks(
                ffprobe=ffprobe,
                entity_root=entity_root,
                entity_id=entity_id,
                timeline=timeline,
                enabled=bool(include_audio),
                workspace_base_dir=workspace_base_dir,
            )
        except ValueError as exc:
            if require_audio:
                return _json_error(
                    f"No usable timeline audio tracks were found: {exc}",
                    code="audio_track_missing",
                )
            raise
        if require_audio and not audio_tracks:
            return _json_error(
                "No usable timeline audio tracks were found.",
                code="audio_track_missing",
            )
        total_duration = _composition_duration(clean_video_duration, audio_tracks)
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
            task_id=task_id,
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
            clean_video_duration=clean_video_duration,
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
                    "fs_path": _workspace_media_reference(
                        timeline_path,
                        entity_id=entity_id,
                        workspace_base_dir=workspace_base_dir,
                    ),
                },
                {
                    "source_type": "clean_video",
                    "source_id": clean_video_path or clean_rel,
                    "fs_path": _normalize_user_path(clean_rel),
                    "duration_seconds": round(clean_video_duration, 2),
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


@_with_media_file_access
async def _align_subtitles_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    timeline_path: str = "",
    cues_path: str = "",
    transcript_path: str = "",
    audio_path: str = "",
    require_audio_transcript_match: bool = False,
    output_name: str = "",
    format: str = "srt",
    track_types: list[str] | str | None = None,
    max_chars_per_line: int = 34,
    max_lines: int = 2,
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
    if (audio_path or "").strip() and not (transcript_path or "").strip():
        return _json_error(
            "transcript_path is required when audio_path is supplied so narration can be "
            "aligned to canonical sentences.",
            code="subtitle_transcript_required",
        )

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        timeline: dict[str, Any] = {}
        cue_payloads: list[Any] = []
        timeline_rel = ""
        cues_rel = ""
        transcript_rel = ""
        audio_rel = ""
        if timeline_path:
            timeline_rel = _workspace_media_reference(
                timeline_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            timeline = await _load_timeline_json(
                entity_root,
                timeline_path,
                entity_id,
                workspace_base_dir,
            )
        if cues_path:
            cues_rel = _workspace_media_reference(
                cues_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            cue_payloads.append(
                await _load_entity_json(
                    entity_root,
                    cues_path,
                    entity_id,
                    workspace_base_dir,
                )
            )

        ffprobe = shutil.which("ffprobe") or ""
        include_types = _subtitle_track_types(track_types)
        cues = await _collect_subtitle_cues(
            ffprobe=ffprobe,
            entity_root=entity_root,
            entity_id=entity_id,
            timeline=timeline,
            cue_payloads=cue_payloads,
            track_types=include_types,
            workspace_base_dir=workspace_base_dir,
        )
        if not cues:
            return _json_error("No subtitle cues with text and timing were found", code="no_subtitle_cues")

        transcript = ""
        transcript_matches = None
        alignment_texts: list[str] = []
        if transcript_path:
            transcript_rel = _workspace_media_reference(
                transcript_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            transcript_abs = await _resolve_entity_file(entity_root, transcript_rel)
            transcript = Path(transcript_abs).read_text(encoding="utf-8")
            transcript_matches = _subtitle_cues_match_transcript(cues, transcript)
            if not transcript_matches:
                return _json_error(
                    "Subtitle cue text must match the canonical narration transcript verbatim.",
                    code="subtitle_transcript_mismatch",
                )
            alignment_texts = [cue.text for cue in cues]

        audio_duration = None
        audio_transcript_matches = None
        timing_scaled = False
        alignment_metrics: dict[str, Any] = {
            "similarity": 1.0 if transcript_matches else None,
            "coverage": 1.0 if transcript_matches else None,
            "missing_sentence_indexes": [],
            "measured_timestamps": False,
            "transcription_model": None,
            "alignment_unit": "subtitle_cue" if alignment_texts else None,
            "sentence_timestamps": [],
            "scene_coverage": _scene_alignment_coverage(
                [timeline, *cue_payloads],
                alignment_texts,
                cues,
                list(range(1, len(alignment_texts) + 1)),
            ),
        }
        if (
            transcript
            and not audio_path
            and _subtitle_cues_have_measured_timing(cues)
        ):
            sentences = alignment_texts
            measured_segments = [
                {
                    "start": cue.start,
                    "end": cue.end,
                    "text": cue.text,
                    "timing_source": cue.timing_source or "existing_measured_cues",
                }
                for cue in cues
            ]
            try:
                measured_sentence_cues, semantic_metrics = _align_sentences_to_segments(
                    sentences,
                    measured_segments,
                )
            except SubtitleWordTimestampsRequiredError as exc:
                return _json_blocked(
                    str(exc),
                    code="subtitle_word_timestamps_required",
                    transcription_model="tts_segment_provenance",
                )
            scene_coverage = _scene_alignment_coverage(
                [timeline, *cue_payloads],
                sentences,
                measured_sentence_cues,
                semantic_metrics["aligned_sentence_indexes"],
            )
            alignment_metrics = {
                **semantic_metrics,
                "measured_timestamps": True,
                "transcription_model": "tts_segment_provenance",
                "alignment_unit": "subtitle_cue",
                "sentence_timestamps": [
                    {
                        "sentence_index": sentence_index,
                        "start": round(cue.start, 3),
                        "end": round(cue.end, 3),
                        "timing_source": cue.timing_source,
                    }
                    for sentence_index, cue in zip(
                        semantic_metrics["aligned_sentence_indexes"],
                        measured_sentence_cues,
                        strict=True,
                    )
                ],
                "scene_coverage": scene_coverage,
            }
            if (
                semantic_metrics["similarity"] < 0.90
                or semantic_metrics["coverage"] < 0.95
            ):
                return _json_blocked(
                    "Measured narration could not be aligned to at least 95% of the "
                    "canonical sentences with 0.90 similarity.",
                    code="subtitle_semantic_alignment_failed",
                    alignment_metrics=alignment_metrics,
                )
            if scene_coverage["missing_interval_scene_ids"]:
                missing_intervals = scene_coverage["missing_interval_scene_ids"]
                missing = ", ".join(missing_intervals)
                return _json_blocked(
                    "Measured scene coverage requires a stable start/end interval "
                    f"for every declared scene. Missing interval for scene(s): {missing}.",
                    code="subtitle_scene_interval_missing",
                    missing_interval_scene_ids=missing_intervals,
                    scene_coverage=scene_coverage,
                    alignment_metrics=alignment_metrics,
                )
            if scene_coverage["missing_scene_ids"]:
                missing = ", ".join(scene_coverage["missing_scene_ids"])
                return _json_blocked(
                    f"Measured narration did not align a sentence to scene(s): {missing}.",
                    code="subtitle_scene_alignment_incomplete",
                    missing_scene_ids=scene_coverage["missing_scene_ids"],
                    scene_coverage=scene_coverage,
                    alignment_metrics=alignment_metrics,
                )
        if audio_path:
            if not ffprobe:
                return _json_error("ffprobe is required to align subtitles to narration audio")
            audio_rel = _workspace_media_reference(
                audio_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            audio_abs = await _resolve_entity_file(entity_root, audio_rel)
            _assert_audio_path(audio_abs)
            audio_info = await _probe_media(ffprobe, audio_abs)
            audio_duration = float(audio_info.get("duration_seconds") or 0.0)
            if audio_duration <= 0:
                return _json_error("Narration audio duration could not be determined")
            if transcript:
                audio_transcript_matches = await _audio_prompt_matches_transcript(
                    entity_id=entity_id,
                    audio_rel_path=audio_rel,
                    transcript=transcript,
                )
                if audio_transcript_matches is False:
                    return _json_error(
                        "Narration audio was generated from text that does not match the canonical transcript.",
                        code="subtitle_audio_transcript_mismatch",
                    )
                if require_audio_transcript_match and audio_transcript_matches is None:
                    return _json_error(
                        "Narration audio provenance could not prove that its generation prompt "
                        "matches the canonical transcript.",
                        code="subtitle_audio_transcript_unverified",
                    )
            if transcript:
                sentences = alignment_texts
                if _subtitle_cues_have_measured_timing(cues):
                    measured_segments = [
                        {
                            "start": cue.start,
                            "end": cue.end,
                            "text": cue.text,
                            "timing_source": cue.timing_source or "existing_measured_cues",
                        }
                        for cue in cues
                    ]
                    try:
                        _aligned, semantic_metrics = _align_sentences_to_segments(
                            sentences,
                            measured_segments,
                        )
                    except SubtitleWordTimestampsRequiredError as exc:
                        return _json_blocked(
                            str(exc),
                            code="subtitle_word_timestamps_required",
                            transcription_model="existing_measured_cues",
                        )
                    measured_sentence_cues = _aligned
                    transcription_model = "existing_measured_cues"
                else:
                    from packages.core.services.voice.whisper import (
                        WHISPER_MAX_UPLOAD_BYTES,
                        WhisperTimestampError,
                        WhisperUploadTooLargeError,
                    )

                    try:
                        transcription = await _transcribe_narration_audio(
                            audio_path=audio_abs,
                            user_id=user_id,
                            entity_id=entity_id,
                            reference_transcript=transcript,
                            workspace_id=workspace_id,
                            agent_id=agent_id,
                            conversation_id=conversation_id,
                        )
                    except WhisperUploadTooLargeError as exc:
                        return _json_blocked(
                            str(exc),
                            code="subtitle_audio_too_large",
                            max_upload_bytes=WHISPER_MAX_UPLOAD_BYTES,
                        )
                    except WhisperTimestampError as exc:
                        return _json_blocked(
                            str(exc),
                            code="subtitle_timestamp_capable_stt_required",
                        )
                    segments = getattr(transcription, "segments", None)
                    words = getattr(transcription, "words", None)
                    transcription_model = str(getattr(transcription, "model", "") or "")
                    if not isinstance(segments, list) or not segments:
                        return _json_blocked(
                            "Measured semantic subtitle alignment requires a timestamp-capable STT "
                            "route; the configured transcription route returned text without segments.",
                            code="subtitle_timestamp_capable_stt_required",
                            transcription_model=transcription_model or None,
                        )
                    try:
                        cues, semantic_metrics = _align_sentences_to_segments(
                            sentences,
                            segments,
                            words=words if isinstance(words, list) else None,
                        )
                    except SubtitleWordTimestampsRequiredError as exc:
                        return _json_blocked(
                            str(exc),
                            code="subtitle_word_timestamps_required",
                            transcription_model=transcription_model or None,
                        )
                    measured_sentence_cues = cues
                scene_coverage = _scene_alignment_coverage(
                    [timeline, *cue_payloads],
                    sentences,
                    measured_sentence_cues,
                    semantic_metrics["aligned_sentence_indexes"],
                )
                alignment_metrics = {
                    **semantic_metrics,
                    "measured_timestamps": True,
                    "transcription_model": transcription_model or None,
                    "alignment_unit": "subtitle_cue",
                    "sentence_timestamps": [
                        {
                            "sentence_index": sentence_index,
                            "start": round(cue.start, 3),
                            "end": round(cue.end, 3),
                            "timing_source": cue.timing_source,
                        }
                        for sentence_index, cue in zip(
                            semantic_metrics["aligned_sentence_indexes"],
                            measured_sentence_cues,
                            strict=True,
                        )
                    ],
                    "scene_coverage": scene_coverage,
                }
                if (
                    semantic_metrics["similarity"] < 0.90
                    or semantic_metrics["coverage"] < 0.95
                ):
                    return _json_blocked(
                        "Measured narration could not be aligned to at least 95% of the "
                        "canonical sentences with 0.90 similarity.",
                        code="subtitle_semantic_alignment_failed",
                        alignment_metrics=alignment_metrics,
                    )
                if scene_coverage["missing_interval_scene_ids"]:
                    missing_intervals = scene_coverage["missing_interval_scene_ids"]
                    missing = ", ".join(missing_intervals)
                    return _json_blocked(
                        "Measured scene coverage requires a stable start/end interval "
                        f"for every declared scene. Missing interval for scene(s): {missing}.",
                        code="subtitle_scene_interval_missing",
                        missing_interval_scene_ids=missing_intervals,
                        scene_coverage=scene_coverage,
                        alignment_metrics=alignment_metrics,
                    )
                if scene_coverage["missing_scene_ids"]:
                    missing = ", ".join(scene_coverage["missing_scene_ids"])
                    return _json_blocked(
                        f"Measured narration did not align a sentence to scene(s): {missing}.",
                        code="subtitle_scene_alignment_incomplete",
                        missing_scene_ids=scene_coverage["missing_scene_ids"],
                        scene_coverage=scene_coverage,
                        alignment_metrics=alignment_metrics,
                    )

        line_width = int(_clamp_float(max_chars_per_line, 16, 56, 34))
        line_limit = int(_clamp_float(max_lines, 1, 2, 2))
        spec = timeline.get("spec") if isinstance(timeline.get("spec"), dict) else timeline
        canvas_width, canvas_height = _editor_canvas_size(spec, {})
        resolved_style = _subtitle_style(
            canvas_width,
            canvas_height,
            _timeline_subtitle_style(timeline, style),
        )
        resolved_style["max_lines"] = line_limit
        cues = _fit_subtitle_cues_to_line_limit(
            cues,
            max_chars_per_line=line_width,
            max_lines=line_limit,
        )
        subtitle_format = str(format or "srt").strip().lower()
        if subtitle_format not in {"srt", "vtt", "ass"}:
            subtitle_format = "srt"
        target = await _build_media_target(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
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
            max_chars_per_line=line_width,
            style=resolved_style,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )
        data = text.encode("utf-8")
        target_abs_path = runtime_write_entity_file_atomic(
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
                "timeline_path": timeline_rel or None,
                "cues_path": cues_rel or None,
                "transcript_path": transcript_rel or None,
                "audio_path": audio_rel or None,
                "format": subtitle_format,
                "track_types": sorted(include_types),
                "cue_count": len(cues),
                "estimated_cues": sum(1 for cue in cues if cue.estimated),
                "transcript_matches": transcript_matches,
                "audio_transcript_matches": audio_transcript_matches,
                "require_audio_transcript_match": require_audio_transcript_match,
                "audio_duration_seconds": round(audio_duration, 3) if audio_duration else None,
                "timing_scaled": timing_scaled,
                "alignment_metrics": alignment_metrics,
                "max_chars_per_line": line_width,
                "max_lines": line_limit,
                "subtitle_style": resolved_style,
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
                "transcript_matches": transcript_matches,
                "audio_transcript_matches": audio_transcript_matches,
                "require_audio_transcript_match": require_audio_transcript_match,
                "audio_duration_seconds": round(audio_duration, 3) if audio_duration else None,
                "timing_scaled": timing_scaled,
                "alignment_metrics": alignment_metrics,
                "max_chars_per_line": line_width,
                "max_lines": line_limit,
                "subtitle_style": resolved_style,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("align_subtitles failed")
        return _json_error(str(exc), code="align_subtitles_failed")


def _narration_segment_cue_bounds(
    *,
    duration_seconds: float,
    leading_silence_seconds: float,
    trailing_silence_seconds: float,
    padding_seconds: float,
) -> tuple[float, float]:
    """Return safe cue bounds inside a decoded, isolated TTS segment."""
    duration = max(0.0, float(duration_seconds))
    leading = max(0.0, min(duration, float(leading_silence_seconds)))
    trailing = max(0.0, min(duration - leading, float(trailing_silence_seconds)))
    padding = max(0.0, float(padding_seconds))
    speech_start = leading
    speech_end = max(speech_start, duration - trailing)
    if speech_end <= speech_start:
        return speech_start, speech_end
    return max(0.0, speech_start - padding), min(duration, speech_end + padding)


def _narrator_profile_audio_output_name(output_name: str, voice: str) -> str:
    """Place normalized task narration beside its source voice segments."""
    voice_slug = re.sub(r"[^a-z0-9]+", "-", str(voice or "").strip().lower()).strip("-")
    if not voice_slug:
        raise ValueError("Narrator profile voice must produce a non-empty storage folder name.")
    normalized = str(output_name or "").replace("\\", "/").strip("/")
    filename = normalized.rsplit("/", 1)[-1].strip()
    if not filename:
        raise ValueError("Normalized narration output name must include a filename.")
    if normalized.startswith("runs/") and "/" in normalized:
        parent = normalized.rsplit("/", 1)[0]
        if parent.rsplit("/", 1)[-1].strip().lower() == voice_slug:
            return normalized
        return f"{parent}/{voice_slug}/{filename}"
    return f"audio/{voice_slug}/{filename}"


@_with_media_file_access
async def _build_narration_timeline_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    transcript_path: str = "",
    segments: list[dict[str, Any]] | None = None,
    manifest_name: str = "",
    timeline_name: str = "",
    cues_name: str = "",
    silence_noise_db: float = -50.0,
    minimum_silence_seconds: float = 0.1,
    padding_seconds: float = 0.08,
    require_normalized_segments: bool = True,
    block_durations_seconds: list[float] | None = None,
    minimum_block_fill_ratio: float = 0.72,
    maximum_block_fill_ratio: float = 0.9,
    initial_quality_warnings: list[dict[str, Any]] | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    """Build subtitle timing from isolated, provenance-verified TTS segments."""
    if not entity_id:
        return _json_error("entity_id is required")
    if not str(transcript_path or "").strip():
        return _json_error("transcript_path is required")
    if not isinstance(segments, list) or not segments:
        return _json_error("segments must contain at least one narration segment")
    if initial_quality_warnings is not None and (
        not isinstance(initial_quality_warnings, list)
        or any(not isinstance(warning, dict) for warning in initial_quality_warnings)
    ):
        return _json_error("initial_quality_warnings must be an array of objects")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        return _json_error("ffmpeg is required", code="ffmpeg_missing")
    if not ffprobe:
        return _json_error("ffprobe is required", code="ffprobe_missing")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        transcript_rel = _workspace_media_reference(
            transcript_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        transcript_abs = await _resolve_entity_file(entity_root, transcript_rel)
        transcript = Path(transcript_abs).read_text(encoding="utf-8")
        if not _canonical_subtitle_text(transcript):
            return _json_error("Canonical narration transcript must not be empty")

        prepared: list[dict[str, Any]] = []
        for index, item in enumerate(segments, start=1):
            if not isinstance(item, dict):
                return _json_error(
                    f"segments[{index}] must be an object",
                    code="narration_segment_invalid",
                )
            raw_audio_path = str(item.get("audio_path") or "").strip()
            if not raw_audio_path:
                return _json_error(
                    f"segments[{index}] requires audio_path",
                    code="narration_segment_invalid",
                )
            audio_rel = _workspace_media_reference(
                raw_audio_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            normalization_tempo_factor: float | None = None
            if require_normalized_segments:
                normalization = await _audio_normalization_provenance(
                    entity_id=entity_id,
                    audio_rel_path=audio_rel,
                )
                target_duration = (
                    normalization.get("target_duration_seconds")
                    if isinstance(normalization, dict)
                    else None
                )
                if (
                    not isinstance(normalization, dict)
                    or normalization.get("operation") != "normalize_audio_loudness"
                    or not isinstance(target_duration, (int, float))
                    or isinstance(target_duration, bool)
                    or target_duration <= 0
                ):
                    return _json_error(
                        (
                            f"Narration segment {index} must be a loudness-normalized "
                            "audio receipt with target_duration_seconds."
                        ),
                        code="narration_segment_not_normalized",
                    )
                tempo_factor = normalization.get("tempo_factor")
                if isinstance(tempo_factor, (int, float)) and not isinstance(
                    tempo_factor, bool
                ):
                    normalization_tempo_factor = float(tempo_factor)
                if (
                    isinstance(normalization.get("narration_profile"), dict)
                    and isinstance(tempo_factor, (int, float))
                    and not isinstance(tempo_factor, bool)
                    and not (
                        NARRATION_MIN_TEMPO_FACTOR
                        <= float(tempo_factor)
                        <= NARRATION_MAX_TEMPO_FACTOR
                    )
                ):
                    return _json_error(
                        (
                            f"Narration segment {index} tempo factor {float(tempo_factor):.3f} "
                            "would sound unnaturally slow or fast. Re-normalize from the "
                            "original TTS receipt with a duration allocated proportionally "
                            "within its visual block."
                        ),
                        code="narration_segment_tempo_out_of_range",
                    )
            text = str(item.get("text") or "").strip()
            if not text:
                provenance = await _audio_prompt_provenance(
                    entity_id=entity_id,
                    audio_rel_path=audio_rel,
                )
                if provenance is None:
                    return _json_error(
                        f"Narration segment {index} has no verifiable generation provenance.",
                        code="narration_segment_provenance_unverified",
                    )
                text = provenance[0]
            raw_start = item.get("start_seconds")
            start_seconds = None
            if raw_start is not None:
                try:
                    start_seconds = float(raw_start)
                except (TypeError, ValueError):
                    return _json_error(
                        f"segments[{index}].start_seconds must be a non-negative number",
                        code="narration_segment_start_invalid",
                    )
                if not math.isfinite(start_seconds) or start_seconds < 0:
                    return _json_error(
                        f"segments[{index}].start_seconds must be a non-negative number",
                        code="narration_segment_start_invalid",
                    )
            prepared.append(
                {
                    "index": index,
                    "text": text,
                    "audio_rel": _normalize_user_path(audio_rel),
                    "audio_abs": await _resolve_entity_file(entity_root, audio_rel),
                    "start_seconds": start_seconds,
                    "tempo_factor": normalization_tempo_factor,
                }
            )

        joined_text = "".join(item["text"] for item in prepared)
        if _canonical_subtitle_text(joined_text) != _canonical_subtitle_text(transcript):
            return _json_error(
                "Ordered narration segment text must equal the canonical transcript.",
                code="narration_segment_transcript_mismatch",
            )

        noise = _clamp_float(silence_noise_db, -80.0, -20.0, -50.0)
        silence_minimum = _clamp_float(minimum_silence_seconds, 0.05, 2.0, 0.1)
        padding = _clamp_float(padding_seconds, 0.0, 0.25, 0.08)
        offset = 0.0
        cue_payloads: list[dict[str, Any]] = []
        track_payloads: list[dict[str, Any]] = []
        segment_payloads: list[dict[str, Any]] = []
        narrator_profile: dict[str, str | int] | None = None
        narrator_profile_missing = False

        for item in prepared:
            requested_start = item["start_seconds"]
            if requested_start is not None:
                if requested_start + 0.001 < offset:
                    return _json_error(
                        f"Narration segment {item['index']} overlaps the preceding segment.",
                        code="narration_segment_overlap",
                    )
                offset = requested_start
            match = await _audio_prompt_matches_transcript(
                entity_id=entity_id,
                audio_rel_path=item["audio_rel"],
                transcript=item["text"],
            )
            if match is False:
                provenance = await _audio_prompt_provenance(
                    entity_id=entity_id,
                    audio_rel_path=item["audio_rel"],
                )
                return _json(
                    {
                        "status": "error",
                        "code": "narration_segment_provenance_mismatch",
                        "error": (
                            f"Narration segment {item['index']} was generated from different text."
                        ),
                        "segment_index": item["index"],
                        "provided_text": item["text"],
                        "receipt_text": provenance[0] if provenance else None,
                        "audio_path": item["audio_rel"],
                    }
                )
            if match is None:
                return _json_error(
                    f"Narration segment {item['index']} has no verifiable generation provenance.",
                    code="narration_segment_provenance_unverified",
                )

            segment_profile = await _audio_narrator_profile(
                entity_id=entity_id,
                audio_rel_path=item["audio_rel"],
            )
            if segment_profile is None:
                narrator_profile_missing = True
                if narrator_profile is not None:
                    return _json_error(
                        "Narration segments must use the same task narrator profile.",
                        code="narration_segment_profile_mismatch",
                    )
            else:
                if narrator_profile_missing or (
                    narrator_profile is not None and segment_profile != narrator_profile
                ):
                    return _json_error(
                        "Narration segments must use the same task narrator profile.",
                        code="narration_segment_profile_mismatch",
                    )
                narrator_profile = segment_profile

            _assert_audio_path(item["audio_abs"])
            media_info = await _probe_media(ffprobe, item["audio_abs"])
            duration = float(media_info.get("duration_seconds") or 0.0)
            if duration <= 0:
                return _json_error(
                    f"Narration segment {item['index']} has no measurable duration.",
                    code="narration_segment_duration_unavailable",
                )
            _stdout, silence_stderr = await _run_process(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostats",
                    "-i",
                    item["audio_abs"],
                    "-map",
                    "0:a:0",
                    "-af",
                    f"silencedetect=noise={noise:.1f}dB:d={silence_minimum:.3f}",
                    "-f",
                    "null",
                    "-",
                ],
                timeout_seconds=max(120.0, duration * 2.0 + 30.0),
            )
            silence = _parse_silence_intervals(silence_stderr, duration_seconds=duration)
            local_start, local_end = _narration_segment_cue_bounds(
                duration_seconds=duration,
                leading_silence_seconds=silence["leading_silence_seconds"],
                trailing_silence_seconds=silence["trailing_silence_seconds"],
                padding_seconds=padding,
            )
            if local_end - local_start < 0.05:
                return _json_error(
                    f"Narration segment {item['index']} has no detectable speech.",
                    code="narration_segment_silent",
                )

            cue = {
                "type": "narration",
                "text": item["text"],
                "start": round(offset + local_start, 3),
                "end": round(offset + local_end, 3),
                "measured": True,
                "timing_source": "measured_tts_segment_audio",
            }
            track = {
                "id": f"narration-{item['index']:03d}",
                "type": "narration",
                "path": item["audio_rel"],
                "start": round(offset, 3),
                "end": round(offset + duration, 3),
            }
            cue_payloads.append(cue)
            track_payloads.append(track)
            segment_payloads.append(
                {
                    "index": item["index"],
                    "text": item["text"],
                    "text_sha256": hashlib.sha256(
                        _canonical_subtitle_text(item["text"]).encode("utf-8")
                    ).hexdigest(),
                    "audio_path": item["audio_rel"],
                    "duration_seconds": round(duration, 3),
                    "start_seconds": round(offset, 3),
                    "leading_silence_seconds": silence["leading_silence_seconds"],
                    "trailing_silence_seconds": silence["trailing_silence_seconds"],
                    "cue_start": cue["start"],
                    "cue_end": cue["end"],
                    "narration_profile": segment_profile,
                }
            )
            offset += duration

        total_duration = round(offset, 3)
        block_fill_ratios: list[float] = []
        quality_warnings = list(initial_quality_warnings or [])
        if block_durations_seconds is not None:
            if not isinstance(block_durations_seconds, list) or not block_durations_seconds:
                return _json_error(
                    "block_durations_seconds must contain at least one positive duration.",
                    code="narration_block_plan_invalid",
                )
            block_durations: list[float] = []
            for index, value in enumerate(block_durations_seconds, start=1):
                duration = _coerce_float(value, 0.0)
                if not math.isfinite(duration) or not 0.25 <= duration <= 60:
                    return _json_error(
                        f"block_durations_seconds[{index}] must be between 0.25 and 60.",
                        code="narration_block_plan_invalid",
                    )
                block_durations.append(duration)
            minimum_fill = _clamp_float(minimum_block_fill_ratio, 0.1, 1.0, 0.72)
            maximum_fill = _clamp_float(maximum_block_fill_ratio, 0.1, 1.0, 0.9)
            if minimum_fill > maximum_fill:
                return _json_error(
                    "minimum_block_fill_ratio cannot exceed maximum_block_fill_ratio.",
                    code="narration_block_plan_invalid",
                )
            block_start = 0.0
            for block_index, block_duration in enumerate(block_durations, start=1):
                block_end = block_start + block_duration
                matching = [
                    (prepared_item, segment)
                    for prepared_item, segment in zip(prepared, segment_payloads, strict=True)
                    if block_start - 0.001
                    <= float(segment["start_seconds"])
                    < block_end - 0.001
                ]
                explicit_starts = [
                    item
                    for item in prepared
                    if item["start_seconds"] is not None
                    and abs(float(item["start_seconds"]) - block_start) <= 0.001
                ]
                if len(explicit_starts) != 1 or not matching:
                    if matching:
                        cue_end = max(
                            float(segment["cue_end"]) for _, segment in matching
                        )
                        fill_ratio = (cue_end - block_start) / block_duration
                    else:
                        fill_ratio = 0.0
                    block_fill_ratios.append(round(fill_ratio, 4))
                    quality_warnings.append(
                        {
                            "code": "narration_block_boundary_missing",
                            "block_index": block_index,
                            "expected_start_seconds": round(block_start, 3),
                            "observed_matching_count": len(matching),
                        }
                    )
                    block_start = block_end
                    continue
                if any(float(segment["start_seconds"]) + float(segment["duration_seconds"]) > block_end + 0.001 for _, segment in matching):
                    quality_warnings.append(
                        {
                            "code": "narration_block_boundary_crossed",
                            "block_index": block_index,
                            "block_end_seconds": round(block_end, 3),
                        }
                    )
                block_tempo_factors = [
                    float(prepared_item["tempo_factor"])
                    for prepared_item, _segment in matching
                    if prepared_item["tempo_factor"] is not None
                ]
                if len(block_tempo_factors) > 1:
                    slowest = min(block_tempo_factors)
                    fastest = max(block_tempo_factors)
                    relative_spread = (fastest - slowest) / slowest
                    if relative_spread > NARRATION_MAX_BLOCK_TEMPO_RELATIVE_SPREAD:
                        quality_warnings.append(
                            {
                                "code": "narration_block_tempo_inconsistent",
                                "block_index": block_index,
                                "slowest_tempo_factor": round(slowest, 6),
                                "fastest_tempo_factor": round(fastest, 6),
                                "relative_spread": round(relative_spread, 6),
                                "maximum_relative_spread": round(
                                    NARRATION_MAX_BLOCK_TEMPO_RELATIVE_SPREAD,
                                    6,
                                ),
                            }
                        )
                cue_end = max(float(segment["cue_end"]) for _, segment in matching)
                fill_ratio = (cue_end - block_start) / block_duration
                block_fill_ratios.append(round(fill_ratio, 4))
                if not minimum_fill <= fill_ratio <= maximum_fill:
                    quality_warnings.append(
                        {
                            "code": "narration_block_fill_out_of_range",
                            "block_index": block_index,
                            "measured_fill_ratio": round(fill_ratio, 4),
                            "minimum_fill_ratio": round(minimum_fill, 4),
                            "maximum_fill_ratio": round(maximum_fill, 4),
                        }
                    )
                block_start = block_end
            if any(float(segment["start_seconds"]) >= block_start - 0.001 for segment in segment_payloads):
                return _json_error(
                    "Narration contains a segment outside the declared visual blocks.",
                    code="narration_block_plan_mismatch",
                )
        manifest = {
            "version": 1,
            "timing_source": "measured_tts_segment_audio",
            "transcript_path": _normalize_user_path(transcript_rel),
            "transcript_sha256": hashlib.sha256(
                _canonical_subtitle_text(transcript).encode("utf-8")
            ).hexdigest(),
            "total_duration_seconds": total_duration,
            "segments": segment_payloads,
            "audio_tracks": track_payloads,
            "block_durations_seconds": block_durations_seconds,
            "block_fill_ratios": block_fill_ratios,
            "quality_warnings": quality_warnings,
        }
        if narrator_profile is not None:
            manifest["narration_profile"] = narrator_profile
        timeline = {
            "duration_seconds": total_duration,
            "audio_tracks": track_payloads,
            "subtitle_cues": cue_payloads,
            "block_durations_seconds": block_durations_seconds,
            "block_fill_ratios": block_fill_ratios,
            "quality_warnings": quality_warnings,
            "alignment_metrics": {
                "similarity": 1.0,
                "coverage": 1.0,
                "measured_timestamps": True,
                "transcription_model": "tts_segment_provenance",
                "timing_sources": ["measured_tts_segment_audio"],
            },
        }
        if narrator_profile is not None:
            timeline["narration_profile"] = narrator_profile
        cue_document = {"cues": cue_payloads}
        outputs = [
            (manifest_name or "technical/narration-manifest.json", "narration-manifest", manifest),
            (timeline_name or "timeline/narration-timeline.json", "narration-timeline", timeline),
            (cues_name or "subtitles/subtitle-cues.json", "subtitle-cues", cue_document),
        ]
        artifact_outputs: dict[str, dict[str, str | None]] = {}
        for output_name, fallback, payload in outputs:
            target = await _build_media_target(
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                output_name=output_name,
                ext=".json",
                fallback=fallback,
                default_dir=WorkspaceArtifactDir.ARTIFACTS.value,
            )
            if not target.abs_dir or not target.abs_path:
                raise ValueError(f"Could not resolve {fallback} output path")
            os.makedirs(target.abs_dir, exist_ok=True)
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            target_abs_path = runtime_write_entity_file_atomic(
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
                file_type="json",
                mime_type="application/json",
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool_name="build_narration_timeline",
                artifact_role="intermediate",
                generation={
                    "operation": "build_narration_timeline",
                    "transcript_path": _normalize_user_path(transcript_rel),
                    "segment_count": len(segment_payloads),
                    "total_duration_seconds": total_duration,
                    "timing_source": "measured_tts_segment_audio",
                    "narration_profile": narrator_profile,
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
                tool_name="build_narration_timeline",
            )
            artifact_outputs[fallback] = {
                "document_id": document_id,
                "fs_path": _normalize_user_path(target.rel_path),
                "name": target.filename,
            }

        return _json(
            {
                "kind": "narration_timeline",
                "status": "completed",
                "timing_source": "measured_tts_segment_audio",
                "total_duration_seconds": total_duration,
                "cue_count": len(cue_payloads),
                "audio_tracks": track_payloads,
                "block_durations_seconds": block_durations_seconds,
                "block_fill_ratios": block_fill_ratios,
                "quality_warnings": quality_warnings,
                "narration_profile": narrator_profile,
                "manifest": artifact_outputs["narration-manifest"],
                "timeline": artifact_outputs["narration-timeline"],
                "cues": artifact_outputs["subtitle-cues"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("build_narration_timeline failed")
        return _json_error(str(exc), code="build_narration_timeline_failed")


@_with_media_file_access
async def _inspect_narration_recovery_handler(
    *,
    entity_id: str = "",
    transcript_path: str = "",
    segment_manifest_path: str = "",
    source_audio_directory: str = "",
    workspace_id: str | None = None,
    task_id: str | None = None,
    **_: Any,
) -> str:
    """Return a deterministic reuse or repair plan for source narration receipts."""
    if not entity_id:
        return _json_error("entity_id is required")
    required_paths = {
        "transcript_path": transcript_path,
        "segment_manifest_path": segment_manifest_path,
        "source_audio_directory": source_audio_directory,
    }
    missing = [key for key, value in required_paths.items() if not str(value or "").strip()]
    if missing:
        return _json_error(f"{', '.join(missing)} required")

    try:
        from packages.core.services import entity_fs

        def rebuild_script_required(*, error: str, code: str) -> str:
            return _json(
                {
                    "status": "repair_required",
                    "recovery_action": "rebuild_script",
                    "inventory": {"source_receipts": [], "warnings": []},
                    "reused_segment_ids": [],
                    "source_segments": [],
                    "repair_segments": [],
                    "error": error,
                    "code": code,
                }
            )

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        transcript_rel = _workspace_media_reference(
            transcript_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        segment_manifest_rel = _workspace_media_reference(
            segment_manifest_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        source_dir_rel = _workspace_media_reference(
            source_audio_directory,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        transcript = Path(await _resolve_entity_file(entity_root, transcript_rel)).read_text(
            encoding="utf-8"
        )
        raw_manifest = json.loads(
            Path(await _resolve_entity_file(entity_root, segment_manifest_rel)).read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(raw_manifest, list) or not raw_manifest:
            return rebuild_script_required(
                error="segment_manifest_path must contain a non-empty JSON array.",
                code="narration_segment_manifest_invalid",
            )

        expected_segments: list[dict[str, str]] = []
        ordered_blocks: list[str] = []
        for index, item in enumerate(raw_manifest, start=1):
            if not isinstance(item, dict):
                return rebuild_script_required(
                    error=f"Narration manifest item {index} must be an object.",
                    code="narration_segment_manifest_invalid",
                )
            segment_id = str(item.get("segment_id") or "").strip().upper()
            expected_id = f"S{index:03d}"
            filename_style_id = f"SEGMENT-{index:03d}"
            text = str(item.get("text") or "").strip()
            block_id = str(item.get("block_id") or "").strip().upper()
            if (
                segment_id not in {expected_id, filename_style_id}
                or not block_id
                or not text
                or (block_id in ordered_blocks and ordered_blocks[-1] != block_id)
            ):
                return rebuild_script_required(
                    error=(
                        f"Narration manifest item {index} must contain its ordered "
                        "segment ID, non-empty text, and a contiguous block_id."
                    ),
                    code="narration_segment_manifest_invalid",
                )
            if block_id not in ordered_blocks:
                ordered_blocks.append(block_id)
            expected_segments.append(
                {
                    "segment_id": expected_id,
                    "text": text,
                    "text_key": _canonical_subtitle_text(text),
                }
            )

        source_root = os.path.realpath(entity_root)
        source_dir_abs = Path(
            os.path.realpath(
                os.path.join(source_root, _normalize_user_path(source_dir_rel))
            )
        )
        if os.path.commonpath([source_root, str(source_dir_abs)]) != source_root:
            raise ValueError(f"Path escapes entity root: {source_dir_rel}")
        if source_dir_abs.exists():
            source_dir_abs = Path(_resolve_entity_dir(entity_root, source_dir_rel))
        source_receipts: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        ffprobe = shutil.which("ffprobe")
        source_paths = sorted(source_dir_abs.rglob("*")) if source_dir_abs.is_dir() else []
        for path in source_paths:
            if (
                not path.is_file()
                or path.suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"}
                or "normalized" in {part.lower() for part in path.parts}
            ):
                continue
            source_rel = _normalize_user_path(os.path.relpath(path, entity_root))
            provenance = await _audio_prompt_provenance(
                entity_id=entity_id,
                audio_rel_path=source_rel,
            )
            if provenance is None:
                warnings.append(
                    {
                        "code": "narration_source_provenance_unverified",
                        "source_path": source_rel,
                    }
                )
                continue
            prompt, _source_path = provenance
            if not _canonical_subtitle_text(prompt):
                warnings.append(
                    {
                        "code": "narration_source_prompt_empty",
                        "source_path": source_rel,
                    }
                )
                continue
            try:
                if not ffprobe:
                    raise RuntimeError("ffprobe is required")
                media_info = await _probe_media(ffprobe, str(path))
                duration_seconds = float(media_info.get("duration_seconds") or 0.0)
                if not math.isfinite(duration_seconds) or duration_seconds <= 0:
                    raise ValueError("media has no measurable duration")
            except Exception:  # noqa: BLE001
                warnings.append(
                    {
                        "code": "narration_source_media_unreadable",
                        "source_path": source_rel,
                    }
                )
                continue
            source_receipts.append(
                {
                    "source_path": source_rel,
                    "text_key": _canonical_subtitle_text(prompt),
                }
            )

        inventory = {
            "source_receipts": [
                {"source_path": item["source_path"]} for item in source_receipts
            ],
            "warnings": warnings,
        }
        manifest_text = _canonical_subtitle_text(
            " ".join(item["text"] for item in expected_segments)
        )
        if manifest_text != _canonical_subtitle_text(transcript):
            return _json(
                {
                    "status": "repair_required",
                    "recovery_action": "rebuild_script",
                    "inventory": inventory,
                    "reused_segment_ids": [],
                    "repair_segments": [],
                    "error": "Narration manifest text does not equal the canonical transcript.",
                    "code": "narration_segment_transcript_mismatch",
                }
            )

        receipts_by_text: dict[str, list[dict[str, str]]] = {}
        for receipt in source_receipts:
            receipts_by_text.setdefault(receipt["text_key"], []).append(receipt)
        for candidates in receipts_by_text.values():
            candidates.sort(key=lambda item: item["source_path"])

        reused_segment_ids: list[str] = []
        source_segments: list[dict[str, str]] = []
        repair_segments: list[dict[str, str]] = []
        claimed_paths: set[str] = set()
        for index, expected in enumerate(expected_segments, start=1):
            candidates = [
                item
                for item in receipts_by_text.get(expected["text_key"], [])
                if item["source_path"] not in claimed_paths
            ]
            if not candidates:
                repair_segments.append(
                    {
                        "segment_id": expected["segment_id"],
                        "text": expected["text"],
                        "output_name": f"{source_dir_rel.rstrip('/')}/segment-{index:03d}.wav",
                    }
                )
                continue
            expected_stem = f"segment-{index:03d}"
            base_candidates = [
                item
                for item in candidates
                if Path(item["source_path"]).stem.lower() == expected_stem
            ]
            if len(base_candidates) == 1:
                selected = base_candidates[0]
            elif len(base_candidates) > 1 or len(candidates) > 1:
                return _json_error(
                    (
                        f"Expected one provenance-matched source receipt for "
                        f"{expected['segment_id']}; found {len(candidates)}."
                    ),
                    code="narration_source_receipt_ambiguous",
                )
            else:
                selected = candidates[0]
            claimed_paths.add(selected["source_path"])
            reused_segment_ids.append(expected["segment_id"])
            source_segments.append(
                {
                    "segment_id": expected["segment_id"],
                    "source_path": selected["source_path"],
                }
            )

        return _json(
            {
                "status": "repair_required" if repair_segments else "completed",
                "recovery_action": "repair_segments" if repair_segments else "continue",
                "inventory": inventory,
                "reused_segment_ids": reused_segment_ids,
                "source_segments": source_segments,
                "repair_segments": repair_segments,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("inspect_narration_recovery failed")
        return _json_error(str(exc), code="inspect_narration_recovery_failed")


@_with_media_file_access
async def _prepare_narration_timeline_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    transcript_path: str = "",
    segment_manifest_path: str = "",
    source_audio_directory: str = "",
    normalized_output_directory: str = "",
    block_durations_seconds: list[float] | None = None,
    occupancy_ratio: float = 0.84,
    minimum_block_fill_ratio: float = 0.72,
    maximum_block_fill_ratio: float = 0.9,
    manifest_name: str = "",
    timeline_name: str = "",
    cues_name: str = "",
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    """Normalize immutable TTS receipts per block and build their timeline."""
    if not entity_id:
        return _json_error("entity_id is required")
    required_paths = {
        "transcript_path": transcript_path,
        "segment_manifest_path": segment_manifest_path,
        "source_audio_directory": source_audio_directory,
        "normalized_output_directory": normalized_output_directory,
        "timeline_name": timeline_name,
    }
    missing = [key for key, value in required_paths.items() if not str(value or "").strip()]
    if missing:
        return _json_error(f"{', '.join(missing)} required")
    if not isinstance(block_durations_seconds, list) or not block_durations_seconds:
        return _json_error(
            "block_durations_seconds must contain at least one positive duration.",
            code="narration_block_plan_invalid",
        )

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return _json_error("ffprobe is required", code="ffprobe_missing")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        transcript_rel = _workspace_media_reference(
            transcript_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        segment_manifest_rel = _workspace_media_reference(
            segment_manifest_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        source_dir_rel = _workspace_media_reference(
            source_audio_directory,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        transcript = Path(await _resolve_entity_file(entity_root, transcript_rel)).read_text(
            encoding="utf-8"
        )
        segment_manifest = json.loads(
            Path(await _resolve_entity_file(entity_root, segment_manifest_rel)).read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(segment_manifest, list) or not segment_manifest:
            return _json_error(
                "segment_manifest_path must contain a non-empty JSON array.",
                code="narration_segment_manifest_invalid",
            )

        ordered_blocks: list[str] = []
        prepared_sources: list[dict[str, Any]] = []
        source_dir_abs = Path(_resolve_entity_dir(entity_root, source_dir_rel))
        source_candidates = [
            path
            for path in source_dir_abs.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".wav", ".mp3", ".m4a", ".flac"}
            and "normalized" not in {part.lower() for part in path.parts}
        ]
        narrator_profile: dict[str, str | int] | None = None
        claimed_source_paths: set[str] = set()
        rebound_source_segments: list[str] = []
        for index, item in enumerate(segment_manifest, start=1):
            if not isinstance(item, dict):
                return _json_error(
                    f"Narration manifest item {index} must be an object.",
                    code="narration_segment_manifest_invalid",
                )
            expected_id = f"S{index:03d}"
            filename_style_id = f"SEGMENT-{index:03d}"
            segment_id = str(item.get("segment_id") or "").strip().upper()
            block_id = str(item.get("block_id") or "").strip().upper()
            text = str(item.get("text") or "").strip()
            if segment_id not in {expected_id, filename_style_id} or not block_id or not text:
                return _json_error(
                    (
                        f"Narration manifest item {index} must contain segment_id={expected_id} "
                        f"or segment_id=segment-{index:03d}, "
                        "a block_id, and non-empty text."
                    ),
                    code="narration_segment_manifest_invalid",
                )
            if block_id not in ordered_blocks:
                ordered_blocks.append(block_id)
            elif ordered_blocks[-1] != block_id:
                return _json_error(
                    f"Narration block {block_id} is not contiguous in the manifest.",
                    code="narration_segment_manifest_invalid",
                )
            expected_stem = f"segment-{index:03d}"
            named_candidates = [
                path
                for path in source_candidates
                if path.stem.lower() == expected_stem
            ]
            source_abs: Path | None = None
            if len(named_candidates) == 1:
                named_rel = _normalize_user_path(
                    os.path.relpath(named_candidates[0], entity_root)
                )
                if named_rel not in claimed_source_paths and await _audio_prompt_matches_transcript(
                    entity_id=entity_id,
                    audio_rel_path=named_rel,
                    transcript=text,
                ):
                    source_abs = named_candidates[0]

            if source_abs is None:
                matching_candidates: list[Path] = []
                for candidate in source_candidates:
                    candidate_rel = _normalize_user_path(
                        os.path.relpath(candidate, entity_root)
                    )
                    if candidate_rel in claimed_source_paths:
                        continue
                    if await _audio_prompt_matches_transcript(
                        entity_id=entity_id,
                        audio_rel_path=candidate_rel,
                        transcript=text,
                    ):
                        matching_candidates.append(candidate)
                if len(matching_candidates) == 1:
                    source_abs = matching_candidates[0]
                elif not matching_candidates:
                    return _json(
                        {
                            "status": "repair_required",
                            "code": "narration_source_receipt_missing",
                            "error": f"No verified source receipt matches {expected_id}.",
                            "repair_segments": [
                                {
                                    "segment_id": expected_id,
                                    "text": text,
                                    "output_name": f"{source_dir_rel.rstrip('/')}/segment-{index:03d}.wav",
                                }
                            ],
                        }
                    )
                else:
                    return _json_error(
                        (
                            f"Expected one provenance-matched source receipt for {expected_id}; "
                            f"found {len(matching_candidates)}."
                        ),
                        code="narration_source_receipt_ambiguous",
                    )

            source_rel = _normalize_user_path(os.path.relpath(source_abs, entity_root))
            if source_abs.stem.lower() != expected_stem:
                rebound_source_segments.append(expected_id)
            claimed_source_paths.add(source_rel)
            if not await _audio_prompt_matches_transcript(
                entity_id=entity_id,
                audio_rel_path=source_rel,
                transcript=text,
            ):
                return _json_error(
                    (
                        f"Source receipt {expected_id} does not match its immutable manifest text."
                    ),
                    code="narration_segment_provenance_mismatch",
                )
            profile = await _audio_narrator_profile(
                entity_id=entity_id,
                audio_rel_path=source_rel,
            )
            if profile is None or (narrator_profile is not None and profile != narrator_profile):
                return _json_error(
                    "Every source narration segment must use one identical narrator profile.",
                    code="narration_segment_profile_mismatch",
                )
            narrator_profile = profile
            source_info = await _probe_media(ffprobe, str(source_abs))
            source_duration = float(source_info.get("duration_seconds") or 0.0)
            if source_duration <= 0:
                return _json_error(
                    f"Source receipt {expected_id} has no measurable duration.",
                    code="narration_segment_duration_unavailable",
                )
            prepared_sources.append(
                {
                    "index": index,
                    "segment_id": expected_id,
                    "block_id": block_id,
                    "text": text,
                    "source_rel": source_rel,
                    "source_duration": source_duration,
                }
            )

        if _canonical_subtitle_text(" ".join(item["text"] for item in prepared_sources)) != _canonical_subtitle_text(transcript):
            return _json_error(
                "Narration manifest text must equal the canonical transcript.",
                code="narration_segment_transcript_mismatch",
            )
        if len(ordered_blocks) != len(block_durations_seconds):
            return _json_error(
                (
                    f"Narration manifest has {len(ordered_blocks)} blocks but "
                    f"block_durations_seconds has {len(block_durations_seconds)} entries."
                ),
                code="narration_block_plan_mismatch",
            )

        output_dir = str(normalized_output_directory).replace("\\", "/").strip("/")
        reusable_normalized_segments: dict[int, dict[str, Any]] = {}
        for item in prepared_sources:
            expected_normalized_rel = _narrator_profile_audio_output_name(
                f"{output_dir}/segment-{item['index']:03d}.wav",
                str(narrator_profile["voice"]),
            )
            if expected_normalized_rel == item["source_rel"]:
                continue
            expected_normalized_abs = Path(
                os.path.realpath(
                    os.path.join(
                        entity_root,
                        _normalize_user_path(expected_normalized_rel),
                    )
                )
            )
            if (
                os.path.commonpath([os.path.realpath(entity_root), str(expected_normalized_abs)])
                != os.path.realpath(entity_root)
                or not expected_normalized_abs.is_file()
            ):
                continue
            normalization = await _audio_normalization_provenance(
                entity_id=entity_id,
                audio_rel_path=expected_normalized_rel,
            )
            normalized_source_ref = (
                str(normalization.get("input_path") or "")
                if isinstance(normalization, dict)
                else ""
            )
            source_rel = ""
            if normalized_source_ref:
                try:
                    source_rel = _normalize_user_path(
                        _rel_path_from_reference(normalized_source_ref, entity_id)
                        or normalized_source_ref
                    )
                except ValueError:
                    pass
            target_duration = (
                normalization.get("target_duration_seconds")
                if isinstance(normalization, dict)
                else None
            )
            tempo_factor = (
                normalization.get("tempo_factor")
                if isinstance(normalization, dict)
                else None
            )
            if (
                not isinstance(normalization, dict)
                or normalization.get("operation") != "normalize_audio_loudness"
                or source_rel != item["source_rel"]
                or normalization.get("narration_profile") != narrator_profile
                or not isinstance(target_duration, (int, float))
                or isinstance(target_duration, bool)
                or target_duration <= 0
                or not isinstance(tempo_factor, (int, float))
                or isinstance(tempo_factor, bool)
                or not NARRATION_MIN_TEMPO_FACTOR
                <= float(tempo_factor)
                <= NARRATION_MAX_TEMPO_FACTOR
            ):
                continue
            normalized_info = await _probe_media(ffprobe, str(expected_normalized_abs))
            decoded_duration = float(normalized_info.get("duration_seconds") or 0.0)
            if (
                not math.isfinite(decoded_duration)
                or decoded_duration <= 0
                or not math.isclose(decoded_duration, float(target_duration), abs_tol=0.05)
            ):
                continue
            reusable_normalized_segments[item["index"]] = {
                "audio_path": expected_normalized_rel,
                "tempo_factor": float(tempo_factor),
            }

        occupancy = _clamp_float(occupancy_ratio, 0.72, 0.9, 0.84)
        minimum_fill = _clamp_float(minimum_block_fill_ratio, 0.1, 1.0, 0.72)
        maximum_fill = _clamp_float(maximum_block_fill_ratio, 0.1, 1.0, 0.9)
        if minimum_fill > maximum_fill:
            return _json_error(
                "minimum_block_fill_ratio cannot exceed maximum_block_fill_ratio.",
                code="narration_block_plan_invalid",
            )
        block_starts: dict[str, float] = {}
        block_tempo_factors: dict[str, float] = {}
        block_occupancy_ratios: dict[str, float] = {}
        quality_warnings: list[dict[str, str]] = []
        cumulative_start = 0.0
        for block_id, raw_duration in zip(
            ordered_blocks, block_durations_seconds, strict=True
        ):
            block_duration = _coerce_float(raw_duration, 0.0)
            if not math.isfinite(block_duration) or not 0.25 <= block_duration <= 60:
                return _json_error(
                    f"Invalid duration for narration block {block_id}.",
                    code="narration_block_plan_invalid",
                )
            block_starts[block_id] = cumulative_start
            cumulative_start += block_duration
            source_total = sum(
                item["source_duration"]
                for item in prepared_sources
                if item["block_id"] == block_id
            )
            reusable_factors = [
                reusable_normalized_segments[item["index"]]["tempo_factor"]
                for item in prepared_sources
                if item["block_id"] == block_id
                and item["index"] in reusable_normalized_segments
            ]
            if reusable_factors:
                slowest = min(reusable_factors)
                fastest = max(reusable_factors)
                relative_spread = (fastest - slowest) / slowest
                if relative_spread <= NARRATION_MAX_BLOCK_TEMPO_RELATIVE_SPREAD:
                    tempo_factor = sum(reusable_factors) / len(reusable_factors)
                    block_occupancy_ratios[block_id] = source_total / (
                        block_duration * tempo_factor
                    )
                    block_tempo_factors[block_id] = tempo_factor
                    continue
                for item in prepared_sources:
                    if item["block_id"] == block_id:
                        reusable_normalized_segments.pop(item["index"], None)
            natural_occupancy_min = source_total / (
                block_duration * NARRATION_MAX_TEMPO_FACTOR
            )
            natural_occupancy_max = source_total / (
                block_duration * NARRATION_MIN_TEMPO_FACTOR
            )
            allowed_occupancy_min = max(minimum_fill, natural_occupancy_min)
            allowed_occupancy_max = min(maximum_fill, natural_occupancy_max)
            if allowed_occupancy_min > allowed_occupancy_max:
                requested_tempo_factor = source_total / (block_duration * occupancy)
                tempo_factor = _clamp_float(
                    requested_tempo_factor,
                    NARRATION_MIN_TEMPO_FACTOR,
                    NARRATION_MAX_TEMPO_FACTOR,
                    NARRATION_MAX_TEMPO_FACTOR,
                )
                block_occupancy = source_total / (block_duration * tempo_factor)
                quality_warnings.append(
                    {
                        "code": "narration_block_tempo_out_of_range",
                        "block_id": block_id,
                        "requested_tempo_factor": round(requested_tempo_factor, 6),
                        "applied_tempo_factor": round(tempo_factor, 6),
                        "block_occupancy_ratio": round(block_occupancy, 6),
                        "minimum_fill_ratio": round(minimum_fill, 6),
                        "maximum_fill_ratio": round(maximum_fill, 6),
                        "minimum_tempo_factor": round(NARRATION_MIN_TEMPO_FACTOR, 6),
                        "maximum_tempo_factor": round(NARRATION_MAX_TEMPO_FACTOR, 6),
                    }
                )
            else:
                block_occupancy = min(
                    max(occupancy, allowed_occupancy_min),
                    allowed_occupancy_max,
                )
                tempo_factor = source_total / (block_duration * block_occupancy)
            block_occupancy_ratios[block_id] = block_occupancy
            block_tempo_factors[block_id] = tempo_factor

        normalized_segments: list[dict[str, Any]] = []
        first_in_block: set[str] = set()
        normalized_paths: list[str] = []
        for item in prepared_sources:
            tempo_factor = block_tempo_factors[item["block_id"]]
            reusable_normalized = reusable_normalized_segments.get(item["index"])
            if reusable_normalized is not None:
                normalized_path = str(reusable_normalized["audio_path"])
            else:
                target_duration = item["source_duration"] / tempo_factor
                normalized_result = json.loads(
                    await _normalize_audio_loudness_handler(
                        entity_id=entity_id,
                        user_id=user_id,
                        input_path=item["source_rel"],
                        output_name=f"{output_dir}/segment-{item['index']:03d}.wav",
                        target_lufs=-16,
                        true_peak=-1,
                        lra=11,
                        target_duration_seconds=target_duration,
                        output_format="wav",
                        workspace_id=workspace_id,
                        task_id=task_id,
                        agent_id=agent_id,
                        conversation_id=conversation_id,
                    )
                )
                if normalized_result.get("status") != "completed":
                    return _json_error(
                        normalized_result.get("error") or "Narration normalization failed.",
                        code=str(normalized_result.get("code") or "narration_normalization_failed"),
                    )
                normalized_path = str(normalized_result.get("fs_path") or "").strip()
                if not normalized_path:
                    return _json_error(
                        f"Narration normalization returned no fs_path for {item['segment_id']}.",
                        code="narration_normalization_failed",
                    )
            segment = {"audio_path": normalized_path}
            if item["block_id"] not in first_in_block:
                segment["start_seconds"] = block_starts[item["block_id"]]
                first_in_block.add(item["block_id"])
            normalized_segments.append(segment)
            normalized_paths.append(normalized_path)

        timeline_result = json.loads(
            await _build_narration_timeline_handler(
                entity_id=entity_id,
                user_id=user_id,
                transcript_path=transcript_rel,
                segments=normalized_segments,
                manifest_name=manifest_name,
                timeline_name=timeline_name,
                cues_name=cues_name,
                require_normalized_segments=True,
                block_durations_seconds=block_durations_seconds,
                minimum_block_fill_ratio=minimum_block_fill_ratio,
                maximum_block_fill_ratio=maximum_block_fill_ratio,
                initial_quality_warnings=quality_warnings,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
        )
        if timeline_result.get("status") != "completed":
            return _json(timeline_result)
        timeline_result.update(
            preparation_mode="measured_source_duration_per_block",
            source_segment_count=len(prepared_sources),
            normalized_segment_count=len(normalized_paths),
            normalized_audio_paths=normalized_paths,
            block_tempo_factors={
                block_id: round(value, 6)
                for block_id, value in block_tempo_factors.items()
            },
            block_occupancy_ratios={
                block_id: round(value, 6)
                for block_id, value in block_occupancy_ratios.items()
            },
            occupancy_ratio=occupancy,
            rebound_source_segments=rebound_source_segments,
            reused_normalized_segment_ids=[
                item["segment_id"]
                for item in prepared_sources
                if item["index"] in reusable_normalized_segments
            ],
        )
        return _json(timeline_result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("prepare_narration_timeline failed")
        return _json_error(str(exc), code="prepare_narration_timeline_failed")


@_with_media_file_access
async def _normalize_audio_loudness_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    input_path: str = "",
    output_name: str = "",
    target_lufs: float = -16.0,
    true_peak: float = -1.5,
    lra: float = 11.0,
    target_duration_seconds: float | None = None,
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
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return _json_error(
            "ffmpeg and ffprobe are required for loudness normalization",
            code="ffmpeg_missing",
        )

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        rel_input = _workspace_media_reference(
            input_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        input_abs = await _resolve_entity_file(entity_root, rel_input)
        _assert_audio_path(input_abs)
        narration_profile = await _audio_narrator_profile(
            entity_id=entity_id,
            audio_rel_path=_normalize_user_path(rel_input),
        )
        if narration_profile is not None:
            output_name = _narrator_profile_audio_output_name(
                output_name,
                str(narration_profile["voice"]),
            )
        fmt = str(output_format or "wav").strip().lower()
        if fmt not in {"wav", "mp3", "m4a", "flac"}:
            fmt = "wav"
        target = await _build_media_target(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
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
        source_info = await _probe_media(ffprobe, input_abs)
        source_duration = float(source_info.get("duration_seconds") or 0.0)
        requested_duration = _coerce_float(target_duration_seconds, 0.0)
        if requested_duration and not 0.25 <= requested_duration <= 3600:
            raise ValueError("target_duration_seconds must be between 0.25 and 3600")
        tempo_factor = (
            source_duration / requested_duration
            if requested_duration > 0 and source_duration > 0
            else 1.0
        )
        if (
            narration_profile is not None
            and requested_duration > 0
            and not NARRATION_MIN_TEMPO_FACTOR
            <= tempo_factor
            <= NARRATION_MAX_TEMPO_FACTOR
        ):
            return _json_blocked(
                (
                    f"Narration tempo factor {tempo_factor:.3f} is outside the natural "
                    f"range {NARRATION_MIN_TEMPO_FACTOR:.2f}-{NARRATION_MAX_TEMPO_FACTOR:.2f}. "
                    "Allocate this frozen clause from its measured source TTS duration using "
                    "one shared tempo factor for the visual block, then retry from the "
                    "original TTS receipt."
                ),
                code="narration_tempo_out_of_range",
                source_duration_seconds=round(source_duration, 3),
                target_duration_seconds=round(requested_duration, 3),
                tempo_factor=round(tempo_factor, 6),
            )
        filters: list[str] = []
        if abs(tempo_factor - 1.0) > 0.001:
            filters.append(_atempo_filter(tempo_factor))
        filters.append(_loudnorm_filter(config))
        args = [
            ffmpeg,
            "-y",
            "-i",
            input_abs,
            "-vn",
            "-af",
            ",".join(filters),
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
        args.extend(_audio_codec_args(fmt))
        with tempfile.TemporaryDirectory(prefix="normalize-audio-") as tmp_dir:
            rendered_path = os.path.join(tmp_dir, f"normalized.{fmt}")
            args.append(rendered_path)
            await _run_process(args, timeout_seconds=300.0)
            runtime_copy_entity_file_atomic(
                entity_id,
                target.rel_path,
                rendered_path,
                expected_size=os.path.getsize(rendered_path),
                allow_empty=False,
            )

        generation = {
            "operation": "normalize_audio_loudness",
            "input_path": _normalize_user_path(rel_input),
            "target_lufs": _loudness_target_lufs(target_lufs),
            "true_peak": _loudness_true_peak(true_peak),
            "lra": _loudness_lra(lra),
            "format": fmt,
            "source_duration_seconds": round(source_duration, 3),
            "target_duration_seconds": round(requested_duration, 3) if requested_duration else None,
            "tempo_factor": round(tempo_factor, 6),
        }
        if narration_profile is not None:
            generation["voice"] = narration_profile["voice"]
            generation["narration_profile"] = narration_profile
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
            generation=generation,
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

        payload: dict[str, Any] = {
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
                "source_duration_seconds": round(source_duration, 3),
                "target_duration_seconds": round(requested_duration, 3) if requested_duration else None,
                "tempo_factor": round(tempo_factor, 6),
        }
        if narration_profile is not None:
            payload["voice"] = narration_profile["voice"]
            payload["narration_profile"] = narration_profile
        return _json(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("normalize_audio_loudness failed")
        return _json_error(str(exc), code="normalize_audio_loudness_failed")


@_with_media_file_access
async def _probe_media_handler(
    *,
    entity_id: str = "",
    input_path: str = "",
    workspace_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not str(input_path or "").strip():
        return _json_error("input_path is required")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return _json_error("ffprobe is required for media inspection", code="ffprobe_missing")
    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        rel_input = _workspace_media_reference(
            input_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        input_abs = await _resolve_entity_file(entity_root, rel_input)
        report = await _probe_media_report(ffprobe, input_abs)
        return _json({
            "status": "completed",
            "fs_path": _normalize_user_path(rel_input),
            "report": report,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("probe_media failed")
        return _json_error(str(exc), code="probe_media_failed")


@_with_media_file_access
async def _verify_stickman_final_media_handler(
    *,
    entity_id: str = "",
    final_video_path: str = "",
    subtitle_path: str = "",
    narration_timeline_path: str = "",
    target_duration_seconds: float = 0.0,
    workspace_id: str | None = None,
    workflow_lineage_root_run_id: str = "",
    **_: Any,
) -> str:
    """Return one deterministic publication receipt for a Stickman Workflow run."""
    base_result: dict[str, Any] = {
        "status": "completed",
        "publication_ready": False,
        "verified_video_source": "",
        "duration_seconds": 0.0,
        "duration_tolerance_seconds": 0.0,
        "duration_within_target_tolerance": False,
        "subtitle_cue_count": 0,
        "narration_quality_evidence_available": False,
        "findings": [],
        "blocker": "",
    }
    if not entity_id:
        return _json_error("entity_id is required")

    try:
        from packages.core.services import entity_fs

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError("ffprobe is required for Stickman final-media verification")

        target_duration = float(target_duration_seconds)
        if not math.isfinite(target_duration) or target_duration <= 0:
            raise ValueError("target_duration_seconds must be a positive finite number")

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        final_rel = _workspace_media_reference(
            final_video_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        subtitle_rel = _workspace_media_reference(
            subtitle_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        timeline_rel = _workspace_media_reference(
            narration_timeline_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        lineage_root = str(workflow_lineage_root_run_id or "").strip()
        if lineage_root:
            expected_prefix = (
                _workspace_media_reference(
                    f"runs/{lineage_root}",
                    entity_id=entity_id,
                    workspace_base_dir=workspace_base_dir,
                ).rstrip("/")
                + "/"
            )
            for label, rel_path in (
                ("final_video_path", final_rel),
                ("subtitle_path", subtitle_rel),
                ("narration_timeline_path", timeline_rel),
            ):
                if not rel_path.startswith(expected_prefix):
                    raise ValueError(
                        f"{label} must stay within the current Workflow artifact prefix"
                    )

        final_abs = await _resolve_entity_file(entity_root, final_rel)
        subtitle_abs = await _resolve_entity_file(entity_root, subtitle_rel)
        timeline_abs = await _resolve_entity_file(entity_root, timeline_rel)
        _assert_video_path(final_abs)
        _assert_subtitle_path(subtitle_abs)

        report = await _probe_media_report(ffprobe, final_abs)
        duration = float(_probe_number(report.get("duration_seconds")))
        tolerance = max(3.0, target_duration * 0.05)
        within_tolerance = (
            math.isfinite(duration)
            and duration > 0
            and abs(duration - target_duration) <= tolerance
        )
        base_result.update({
            "verified_video_source": _normalize_user_path(final_rel),
            "duration_seconds": round(duration, 3),
            "duration_tolerance_seconds": round(tolerance, 3),
            "duration_within_target_tolerance": within_tolerance,
        })

        findings: list[dict[str, Any]] = []
        if not report.get("decodable"):
            findings.append({"code": "final_media_not_decodable"})
        if not report.get("has_video"):
            findings.append({"code": "final_media_video_stream_missing"})
        if not report.get("has_audio"):
            findings.append({"code": "final_media_audio_stream_missing"})
        video_stream = report.get("video_stream")
        if isinstance(video_stream, dict):
            width = float(_probe_number(video_stream.get("width")))
            height = float(_probe_number(video_stream.get("height")))
            if height <= 0 or abs((width / height) - (16 / 9)) > 0.02:
                findings.append({"code": "final_media_aspect_ratio_invalid"})
        else:
            findings.append({"code": "final_media_video_stream_missing"})
        if not within_tolerance:
            findings.append({
                "code": "final_media_duration_outside_tolerance",
                "target_duration_seconds": target_duration,
                "duration_seconds": round(duration, 3),
                "duration_tolerance_seconds": round(tolerance, 3),
            })

        audio_result = json.loads(
            await _analyze_audio_handler(
                entity_id=entity_id,
                input_path=final_rel,
                target_lufs_min=-15,
                target_lufs_max=-13,
                max_true_peak_dbfs=-1,
                max_silence_ratio=0.35,
                workspace_id=workspace_id,
            )
        )
        if audio_result.get("status") != "completed" or audio_result.get("verdict") != "pass":
            findings.append({"code": "final_media_audio_qa_failed"})

        subtitle_result = json.loads(
            await _validate_subtitles_handler(
                entity_id=entity_id,
                subtitle_path=subtitle_rel,
                media_path=final_rel,
                max_lines=2,
                min_margin_v=48,
                workspace_id=workspace_id,
            )
        )
        cue_count = int(_probe_number(subtitle_result.get("cue_count"), integer=True))
        base_result["subtitle_cue_count"] = cue_count
        if subtitle_result.get("status") != "completed" or subtitle_result.get("verdict") != "pass":
            findings.append({"code": "final_media_subtitle_qa_failed"})
        elif cue_count <= 0:
            findings.append({"code": "final_media_subtitle_cues_missing"})

        timeline_payload = json.loads(Path(timeline_abs).read_text(encoding="utf-8"))
        quality_warnings = (
            timeline_payload.get("quality_warnings")
            if isinstance(timeline_payload, dict)
            else None
        )
        if not isinstance(quality_warnings, list):
            findings.append({"code": "narration_quality_evidence_missing"})
        else:
            base_result["narration_quality_evidence_available"] = True
            warning_codes = {
                str(item.get("code") or "")
                for item in quality_warnings
                if isinstance(item, dict)
            }
            if "narration_quality_evidence_unavailable_after_reconstruction" in warning_codes:
                findings.append({
                    "code": "narration_quality_evidence_unavailable_after_reconstruction"
                })

        base_result["findings"] = findings
        base_result["publication_ready"] = not findings
        base_result["blocker"] = str(findings[0]["code"]) if findings else ""
        return _json(base_result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("verify_stickman_final_media failed")
        base_result["findings"] = [{
            "code": "final_media_verification_failed",
            "diagnostic": str(exc),
        }]
        base_result["blocker"] = "final_media_verification_failed"
        return _json(base_result)


@_with_media_file_access
async def _still_to_video_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    input_path: str = "",
    output_name: str = "",
    duration_seconds: float = 3.0,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    fps: int = 30,
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
    if not str(input_path or "").strip():
        return _json_error("input_path is required")
    if not str(output_name or "").strip():
        return _json_error("output_name is required")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _json_error("ffmpeg is required to render still scenes", code="ffmpeg_missing")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        rel_input = _workspace_media_reference(
            input_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        input_abs = await _resolve_entity_file(entity_root, rel_input)
        _assert_image_path(input_abs)

        duration = _clamp_float(duration_seconds, 0.1, 30.0, 3.0)
        selected_resolution = str(resolution or "1080p").strip().lower()
        if selected_resolution not in {"480p", "720p", "1080p"}:
            selected_resolution = "1080p"
        selected_aspect_ratio = str(aspect_ratio or "16:9").strip()
        if selected_aspect_ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
            selected_aspect_ratio = "16:9"
        selected_fps = max(12, min(60, int(fps or 30)))
        selected_crf = max(14, min(28, int(crf or 18)))
        selected_preset = str(preset or "veryfast").strip().lower()
        if selected_preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}:
            selected_preset = "veryfast"
        width, height = _target_dimensions(selected_resolution, selected_aspect_ratio)

        target = await _build_media_target(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            output_name=output_name,
            ext=".mp4",
            fallback="still-scene",
            default_dir="video",
        )
        if not target.abs_dir or not target.abs_path:
            raise ValueError("Could not resolve still-scene output path")
        os.makedirs(target.abs_dir, exist_ok=True)
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={selected_fps},format=yuv420p"
        )
        with tempfile.TemporaryDirectory(prefix="still-to-video-") as tmp_dir:
            rendered_path = os.path.join(tmp_dir, "still-scene.mp4")
            await _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    input_abs,
                    "-t",
                    f"{duration:.3f}",
                    "-vf",
                    video_filter,
                    "-r",
                    str(selected_fps),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    selected_preset,
                    "-crf",
                    str(selected_crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    rendered_path,
                ],
                timeout_seconds=max(120.0, duration * 8.0 + 30.0),
            )
            runtime_copy_entity_file_atomic(
                entity_id,
                target.rel_path,
                rendered_path,
                expected_size=os.path.getsize(rendered_path),
                allow_empty=False,
            )
        if not os.path.isfile(target.abs_path) or os.path.getsize(target.abs_path) <= 0:
            raise RuntimeError("ffmpeg did not produce a still-scene video")

        generation = {
            "operation": "still_to_video",
            "input_path": _normalize_user_path(rel_input),
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "fps": selected_fps,
            "crf": selected_crf,
            "preset": selected_preset,
        }
        document_id = await _register_file_artifact(
            entity_id=entity_id,
            user_id=user_id,
            filename=target.filename,
            rel_path=target.rel_path,
            file_size=os.path.getsize(target.abs_path),
            file_type="mp4",
            mime_type="video/mp4",
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            tool_name="still_to_video",
            artifact_role="video",
            generation=generation,
        )
        await _bind_artifact_to_workspace(
            entity_id=entity_id,
            document_id=document_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tool_name="still_to_video",
        )
        return _json({
            "kind": "video",
            "status": "completed",
            "document_id": document_id,
            "name": target.filename,
            "result_url": f"/api/v1/fs/{entity_id}/{target.rel_path}",
            "fs_path": target.rel_path,
            "file_size": os.path.getsize(target.abs_path),
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "fps": selected_fps,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("still_to_video failed")
        return _json_error(str(exc), code="still_to_video_failed")


@_with_media_file_access
async def _render_frame_samples_handler(
    *,
    entity_id: str = "",
    user_id: str = "",
    input_path: str = "",
    output_dir: str = "",
    timestamps: list[float] | None = None,
    scene_boundaries: list[float] | None = None,
    interval_seconds: float = 10.0,
    max_samples: int = 12,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not str(input_path or "").strip():
        return _json_error("input_path is required")
    if not str(output_dir or "").strip():
        return _json_error("output_dir is required")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        return _json_error("ffmpeg is required to render frame samples", code="ffmpeg_missing")
    if not ffprobe:
        return _json_error("ffprobe is required to render frame samples", code="ffprobe_missing")

    completed_frames: list[dict[str, Any]] = []
    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        rel_input = _workspace_media_reference(
            input_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        input_abs = await _resolve_entity_file(entity_root, rel_input)
        _assert_video_path(input_abs)
        report = await _probe_media_report(ffprobe, input_abs)
        if not report.get("has_video"):
            raise ValueError("Frame sampling requires a video stream")
        media_duration = float(_probe_number(report.get("duration_seconds")))
        video_stream = report.get("video_stream")
        video_duration = float(_probe_number(
            video_stream.get("duration_seconds")
            if isinstance(video_stream, dict)
            else 0
        ))
        duration = video_duration if video_duration > 0 else media_duration
        if duration <= 0:
            raise ValueError("Frame sampling requires a positive media duration")
        output_base = _normalize_user_path(output_dir).rstrip("/")
        sample_times = _frame_sample_times(
            duration_seconds=duration,
            timestamps=timestamps,
            scene_boundaries=scene_boundaries,
            interval_seconds=interval_seconds,
            max_samples=max_samples,
        )
        for index, timestamp in enumerate(sample_times, start=1):
            timestamp_ms = int(round(timestamp * 1000))
            output_name = f"{output_base}/frame-{index:03d}-{timestamp_ms:09d}ms.png"
            target = await _build_media_target(
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                output_name=output_name,
                ext=".png",
                fallback=f"frame-{index:03d}",
                default_dir="qa/frames",
            )
            if not target.abs_dir or not target.abs_path:
                raise ValueError("Could not resolve frame sample output path")
            os.makedirs(target.abs_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="frame-sample-") as tmp_dir:
                rendered_path = os.path.join(tmp_dir, "frame.png")
                await _run_process(
                    [
                        ffmpeg,
                        "-y",
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        input_abs,
                        "-frames:v",
                        "1",
                        "-an",
                        rendered_path,
                    ],
                    timeout_seconds=120.0,
                )
                runtime_copy_entity_file_atomic(
                    entity_id,
                    target.rel_path,
                    rendered_path,
                    expected_size=os.path.getsize(rendered_path),
                    allow_empty=False,
                )
            if not os.path.isfile(target.abs_path) or os.path.getsize(target.abs_path) <= 0:
                raise RuntimeError(f"ffmpeg did not produce frame sample {index}")
            generation = {
                "operation": "render_frame_samples",
                "input_path": _normalize_user_path(rel_input),
                "timestamp_seconds": timestamp,
                "sample_index": index,
                "media_duration_seconds": round(duration, 3),
            }
            document_id = await _register_file_artifact(
                entity_id=entity_id,
                user_id=user_id,
                filename=target.filename,
                rel_path=target.rel_path,
                file_size=os.path.getsize(target.abs_path),
                file_type="png",
                mime_type="image/png",
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool_name="render_frame_samples",
                artifact_role="qa_evidence",
                generation=generation,
            )
            await _bind_artifact_to_workspace(
                entity_id=entity_id,
                document_id=document_id,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name="render_frame_samples",
            )
            completed_frames.append({
                "index": index,
                "timestamp_seconds": timestamp,
                "document_id": document_id,
                "name": target.filename,
                "fs_path": target.rel_path,
                "result_url": f"/api/v1/fs/{entity_id}/{target.rel_path}",
                "file_size": os.path.getsize(target.abs_path),
            })
        return _json({
            "status": "completed",
            "input_path": _normalize_user_path(rel_input),
            "duration_seconds": round(duration, 3),
            "sample_count": len(completed_frames),
            "frames": completed_frames,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("render_frame_samples failed")
        return _json({
            "status": "error",
            "code": "render_frame_samples_failed",
            "error": str(exc),
            "completed_frames": completed_frames,
        })


@_with_media_file_access
async def _analyze_audio_handler(
    *,
    entity_id: str = "",
    input_path: str = "",
    target_lufs_min: float = -20.0,
    target_lufs_max: float = -14.0,
    max_true_peak_dbfs: float = -1.0,
    silence_noise_db: float = -50.0,
    minimum_silence_seconds: float = 0.5,
    max_silence_ratio: float = 0.35,
    workspace_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not str(input_path or "").strip():
        return _json_error("input_path is required")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg:
        return _json_error("ffmpeg is required for audio analysis", code="ffmpeg_missing")
    if not ffprobe:
        return _json_error("ffprobe is required for audio analysis", code="ffprobe_missing")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        rel_input = _workspace_media_reference(
            input_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        input_abs = await _resolve_entity_file(entity_root, rel_input)
        report = await _probe_media_report(ffprobe, input_abs)
        if not report.get("has_audio"):
            raise ValueError("Media has no audio stream")
        duration = float(_probe_number(report.get("duration_seconds")))
        if duration <= 0:
            raise ValueError("Audio analysis requires a positive media duration")

        lufs_min = _clamp_float(target_lufs_min, -40.0, 0.0, -20.0)
        lufs_max = _clamp_float(target_lufs_max, -40.0, 0.0, -14.0)
        if lufs_min > lufs_max:
            raise ValueError("target_lufs_min must not exceed target_lufs_max")
        peak_limit = _clamp_float(max_true_peak_dbfs, -12.0, 0.0, -1.0)
        noise = _clamp_float(silence_noise_db, -80.0, -20.0, -50.0)
        silence_minimum = _clamp_float(minimum_silence_seconds, 0.1, 10.0, 0.5)
        silence_ratio_limit = _clamp_float(max_silence_ratio, 0.0, 1.0, 0.35)

        _loudness_stdout, loudness_stderr = await _run_process(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                input_abs,
                "-map",
                "0:a:0",
                "-af",
                "ebur128=peak=true:framelog=verbose",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=max(120.0, duration * 2.0 + 30.0),
        )
        _silence_stdout, silence_stderr = await _run_process(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                input_abs,
                "-map",
                "0:a:0",
                "-af",
                f"silencedetect=noise={noise:.1f}dB:d={silence_minimum:.3f}",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=max(120.0, duration * 2.0 + 30.0),
        )
        loudness = _parse_ebur128(loudness_stderr)
        silence = _parse_silence_intervals(silence_stderr, duration_seconds=duration)
        integrated_lufs = loudness["integrated_lufs"]
        true_peak = loudness["true_peak_dbfs"]
        active_seconds = max(0.0, duration - silence["total_silence_seconds"])
        non_empty = integrated_lufs is not None and active_seconds > 0.05
        clipping_detected = true_peak is not None and true_peak >= -0.1

        findings: list[dict[str, Any]] = []
        if integrated_lufs is None:
            findings.append({"code": "loudness_unavailable"})
        elif not lufs_min <= integrated_lufs <= lufs_max:
            findings.append({
                "code": "integrated_loudness_out_of_range",
                "actual_lufs": integrated_lufs,
                "minimum_lufs": lufs_min,
                "maximum_lufs": lufs_max,
            })
        if true_peak is None:
            findings.append({"code": "true_peak_unavailable"})
        elif true_peak > peak_limit:
            findings.append({
                "code": "true_peak_exceeded",
                "actual_dbfs": true_peak,
                "maximum_dbfs": peak_limit,
            })
        if clipping_detected:
            findings.append({"code": "clipping_detected", "actual_dbfs": true_peak})
        if silence["silence_ratio"] > silence_ratio_limit:
            findings.append({
                "code": "silence_ratio_exceeded",
                "actual_ratio": silence["silence_ratio"],
                "maximum_ratio": silence_ratio_limit,
            })
        if not non_empty:
            findings.append({"code": "audio_empty"})

        return _json({
            "status": "completed",
            "verdict": "pass" if not findings else "fail",
            "fs_path": _normalize_user_path(rel_input),
            "duration_seconds": round(duration, 3),
            "non_empty": non_empty,
            "integrated_lufs": integrated_lufs,
            "loudness_range_lu": loudness["loudness_range_lu"],
            "true_peak_dbfs": true_peak,
            "clipping_detected": clipping_detected,
            **silence,
            "thresholds": {
                "target_lufs_min": lufs_min,
                "target_lufs_max": lufs_max,
                "max_true_peak_dbfs": peak_limit,
                "max_silence_ratio": silence_ratio_limit,
            },
            "findings": findings,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze_audio failed")
        return _json_error(str(exc), code="analyze_audio_failed")


@_with_media_file_access
async def _validate_subtitles_handler(
    *,
    entity_id: str = "",
    subtitle_path: str = "",
    media_path: str = "",
    media_duration_seconds: float | None = None,
    max_lines: int = 2,
    min_margin_v: int = 28,
    workspace_id: str | None = None,
    **_: Any,
) -> str:
    if not entity_id:
        return _json_error("entity_id is required")
    if not str(subtitle_path or "").strip():
        return _json_error("subtitle_path is required")

    try:
        from packages.core.services import entity_fs

        entity_root = entity_fs.get_entity_root(entity_id)
        workspace_base_dir = await _workspace_media_base_dir(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        subtitle_rel = _workspace_media_reference(
            subtitle_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        subtitle_abs = await _resolve_entity_file(entity_root, subtitle_rel)
        _assert_subtitle_path(subtitle_abs)
        subtitle_format = Path(subtitle_abs).suffix.lower().lstrip(".")
        parsed = _parse_subtitle_content(
            Path(subtitle_abs).read_text(encoding="utf-8-sig"),
            subtitle_format,
        )

        duration = float(_probe_number(media_duration_seconds))
        media_rel = ""
        if duration <= 0:
            if not str(media_path or "").strip():
                raise ValueError("media_path or media_duration_seconds is required")
            ffprobe = shutil.which("ffprobe")
            if not ffprobe:
                raise ValueError("ffprobe is required to resolve media duration")
            media_rel = _workspace_media_reference(
                media_path,
                entity_id=entity_id,
                workspace_base_dir=workspace_base_dir,
            )
            media_abs = await _resolve_entity_file(entity_root, media_rel)
            report = await _probe_media_report(ffprobe, media_abs)
            duration = float(_probe_number(report.get("duration_seconds")))
        if duration <= 0:
            raise ValueError("Subtitle validation requires a positive media duration")

        line_limit = max(1, min(4, int(max_lines or 2)))
        margin_limit = max(0, min(500, int(min_margin_v or 0)))
        cues = list(parsed["cues"])
        findings: list[dict[str, Any]] = []
        if not cues:
            findings.append({"code": "no_subtitle_cues"})

        for cue in cues:
            cue_id = cue["id"]
            if not str(cue.get("text") or "").strip():
                findings.append({"code": "blank_cue", "cue_id": cue_id})
            if float(cue["end"]) <= float(cue["start"]):
                findings.append({
                    "code": "non_positive_duration",
                    "cue_id": cue_id,
                    "start": cue["start"],
                    "end": cue["end"],
                })
            if float(cue["start"]) < 0 or float(cue["end"]) > duration + 0.001:
                findings.append({
                    "code": "cue_outside_media",
                    "cue_id": cue_id,
                    "start": cue["start"],
                    "end": cue["end"],
                    "media_duration_seconds": round(duration, 3),
                })
            if int(cue.get("line_count") or 0) > line_limit:
                findings.append({
                    "code": "too_many_lines",
                    "cue_id": cue_id,
                    "line_count": cue["line_count"],
                    "maximum_lines": line_limit,
                })

        active_cue: dict[str, Any] | None = None
        for cue in sorted(cues, key=lambda item: (float(item["start"]), float(item["end"]))):
            if active_cue is not None and float(cue["start"]) < float(active_cue["end"]) - 0.001:
                findings.append({
                    "code": "cue_overlap",
                    "cue_id": cue["id"],
                    "overlaps_cue_id": active_cue["id"],
                })
            if active_cue is None or float(cue["end"]) > float(active_cue["end"]):
                active_cue = cue

        style_evidence: dict[str, dict[str, Any]] = {}
        if subtitle_format == "ass":
            used_styles = sorted({str(cue.get("style") or "Default") for cue in cues})
            for style_name in used_styles:
                style = parsed["styles"].get(style_name)
                if style is None:
                    findings.append({"code": "ass_style_missing", "style": style_name})
                    continue
                alignment = int(style.get("alignment") or 0)
                margin_v = int(style.get("margin_v") or 0)
                bottom_safe = alignment == 2 and margin_v >= margin_limit
                style_evidence[style_name] = {
                    "font_size": style.get("font_size"),
                    "outline": style.get("outline"),
                    "shadow": style.get("shadow"),
                    "alignment": alignment,
                    "margin_v": margin_v,
                    "bottom_safe": bottom_safe,
                }
                if not bottom_safe:
                    findings.append({
                        "code": "ass_style_not_bottom_safe",
                        "style": style_name,
                        "required_alignment": 2,
                        "minimum_margin_v": margin_limit,
                    })

        starts = [float(cue["start"]) for cue in cues]
        ends = [float(cue["end"]) for cue in cues]
        return _json({
            "status": "completed",
            "verdict": "pass" if not findings else "fail",
            "fs_path": _normalize_user_path(subtitle_rel),
            "media_path": _normalize_user_path(media_rel) if media_rel else None,
            "media_duration_seconds": round(duration, 3),
            "subtitle_format": subtitle_format,
            "cue_count": len(cues),
            "maximum_line_count": max(
                (int(cue.get("line_count") or 0) for cue in cues),
                default=0,
            ),
            "timing_bounds": {
                "start_seconds": round(min(starts), 3) if starts else None,
                "end_seconds": round(max(ends), 3) if ends else None,
            },
            "play_res_y": parsed["play_res_y"],
            "style_evidence": style_evidence,
            "findings": findings,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("validate_subtitles failed")
        return _json_error(str(exc), code="validate_subtitles_failed")


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
    workspace_id: str | None = None,
    workspace_base_dir: str = "",
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
                resolved.append(
                    await _video_input_from_document(
                        entity_root,
                        doc,
                        workspace_id=workspace_id,
                        workspace_base_dir=workspace_base_dir,
                    )
                )

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
                    if job.status != MediaJobStatus.COMPLETED:
                        raise ValueError(f"Media job is not completed: {job_id} ({job.status})")
                    params = job.params or {}
                    document_id = params.get("result_document_id")
                    if document_id:
                        doc = await get_document(db, str(document_id), entity_id)
                        if not doc:
                            raise ValueError(f"Media job document not found: {job_id}")
                        item = await _video_input_from_document(
                            entity_root,
                            doc,
                            workspace_id=workspace_id,
                            workspace_base_dir=workspace_base_dir,
                        )
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
                    rel_path = _workspace_media_reference(
                        job.result_url,
                        entity_id=entity_id,
                        workspace_base_dir=workspace_base_dir,
                    )
                    if not rel_path:
                        raise ValueError(f"Completed media job has no Knowledge path: {job_id}")
                    abs_path = await _resolve_entity_file(entity_root, rel_path)
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
        rel_path = _workspace_media_reference(
            path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        abs_path = await _resolve_entity_file(entity_root, rel_path)
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
        folder_rel_path = _workspace_media_reference(
            folder_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        folder_abs_path = _resolve_entity_dir(entity_root, folder_rel_path)
        for filename in sorted(os.listdir(folder_abs_path)):
            full_path = os.path.join(folder_abs_path, filename)
            if not os.path.isfile(full_path):
                continue
            if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            rel_path = "/".join(part for part in (folder_rel_path, filename) if part)
            full_path = await _resolve_entity_file(entity_root, rel_path)
            resolved.append(
                VideoInput(
                    source_type="folder",
                    source_id=folder_path,
                    rel_path=rel_path,
                    abs_path=full_path,
                )
            )

    return _dedupe_inputs(resolved)


async def _resolve_ordered_video_clips(
    *,
    entity_id: str,
    clips: list[dict[str, Any]],
    workspace_id: str | None = None,
    workspace_base_dir: str = "",
) -> list[VideoInput]:
    resolved: list[VideoInput] = []
    for index, raw_clip in enumerate(clips, start=1):
        if not isinstance(raw_clip, dict):
            raise ValueError(f"clips[{index}] must be an object")
        document_id = str(raw_clip.get("document_id") or "").strip()
        job_id = str(raw_clip.get("job_id") or "").strip()
        path = str(raw_clip.get("path") or "").strip()
        locator_count = sum(bool(value) for value in (document_id, job_id, path))
        if locator_count != 1:
            raise ValueError(
                f"clips[{index}] requires exactly one of document_id, job_id, or path"
            )
        items = await _resolve_video_inputs(
            entity_id=entity_id,
            job_ids=[job_id] if job_id else [],
            document_ids=[document_id] if document_id else [],
            paths=[path] if path else [],
            folder_path=None,
            workspace_id=workspace_id,
            workspace_base_dir=workspace_base_dir,
        )
        if len(items) != 1:
            raise ValueError(f"clips[{index}] did not resolve to exactly one video")
        try:
            start_seconds = float(raw_clip.get("start_seconds") or 0.0)
            end_value = raw_clip.get("end_seconds")
            end_seconds = float(end_value) if end_value is not None else None
            speed = float(raw_clip.get("speed") or 1.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"clips[{index}] has invalid trim or speed values") from exc
        if start_seconds < 0:
            raise ValueError(f"clips[{index}].start_seconds must be non-negative")
        if end_seconds is not None and end_seconds <= start_seconds:
            raise ValueError(f"clips[{index}].end_seconds must be greater than start_seconds")
        if not 0.25 <= speed <= 8.0:
            raise ValueError(f"clips[{index}].speed must be between 0.25 and 8")
        resolved.append(
            replace(
                items[0],
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                speed=speed,
                label=str(raw_clip.get("label") or "").strip() or None,
            )
        )
    return resolved


async def _video_input_from_document(
    entity_root: str,
    doc: Any,
    *,
    workspace_id: str | None = None,
    workspace_base_dir: str = "",
) -> VideoInput:
    if not _is_video_document(doc):
        raise ValueError(f"Document is not a supported video: {getattr(doc, 'id', '')}")
    rel_path = getattr(doc, "fs_path", None)
    if not rel_path:
        raise ValueError(f"Document has no filesystem path: {getattr(doc, 'id', '')}")
    _assert_document_workspace_scope(
        doc,
        rel_path=rel_path,
        workspace_id=workspace_id,
        workspace_base_dir=workspace_base_dir,
    )
    abs_path = await _resolve_entity_file(entity_root, rel_path)
    _assert_video_path(abs_path)
    return VideoInput(
        source_type="document",
        source_id=getattr(doc, "id", None),
        rel_path=_normalize_user_path(rel_path),
        abs_path=abs_path,
        document_id=getattr(doc, "id", None),
    )


def _assert_document_workspace_scope(
    doc: Any,
    *,
    rel_path: str,
    workspace_id: str | None,
    workspace_base_dir: str,
) -> None:
    current_workspace = str(workspace_id or "").strip()
    if not current_workspace:
        return

    metadata = getattr(doc, "metadata_", None)
    if not isinstance(metadata, dict):
        metadata = getattr(doc, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
    origin_workspace = str(origin.get("workspace_id") or "").strip()
    if origin_workspace and origin_workspace != current_workspace:
        raise ValueError(
            f"Document {getattr(doc, 'id', '')} does not belong to Workspace "
            f"{current_workspace}"
        )

    normalized_path = _normalize_user_path(rel_path)
    normalized_base = _normalize_user_path(workspace_base_dir) if workspace_base_dir else ""
    path_matches = bool(
        normalized_base
        and (
            normalized_path == normalized_base
            or normalized_path.startswith(f"{normalized_base}/")
        )
    )
    if path_matches or origin_workspace == current_workspace:
        return
    raise ValueError(
        f"Document {getattr(doc, 'id', '')} does not belong to Workspace "
        f"{current_workspace}"
    )


async def _merge_video_files(
    *,
    ffmpeg: str,
    ffprobe: str,
    entity_id: str,
    output_name: str,
    workspace_id: str | None,
    task_id: str | None = None,
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
        task_id=task_id,
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
        default_dir=workspace_artifact_default_dir(workspace_base_dir, WorkspaceArtifactDir.VIDEOS.value),
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
            source_duration = float(media_info.get("duration_seconds") or 0.0)
            trim_start, trim_end, planned_duration = _resolve_clip_window(
                source_duration=source_duration,
                start_seconds=item.start_seconds,
                end_seconds=item.end_seconds,
                speed=item.speed,
            )
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
                start_seconds=trim_start,
                source_duration_seconds=trim_end - trim_start,
                output_duration_seconds=planned_duration,
                speed=item.speed,
                has_audio=bool(media_info.get("has_audio")),
                include_source_audio=include_source_audio,
            )
            normalized_info = await _probe_media(ffprobe, normalized_path)
            rendered_duration = float(
                normalized_info.get("duration_seconds") or planned_duration
            )
            total_duration += rendered_duration
            normalized_paths.append(normalized_path)
            input_payloads.append(
                {
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "document_id": item.document_id,
                    "fs_path": item.rel_path,
                    "label": item.label,
                    "source_duration_seconds": round(source_duration, 3),
                    "start_seconds": round(trim_start, 3),
                    "end_seconds": round(trim_end, 3),
                    "speed": round(item.speed, 3),
                    "duration_seconds": round(rendered_duration, 3),
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
        runtime_copy_entity_file_atomic(
            entity_id,
            target.rel_path,
            tmp_output,
            expected_size=os.path.getsize(tmp_output),
            allow_empty=False,
        )

    return target.abs_path, target.rel_path, target.filename, input_payloads, total_duration


async def _artifact_document_folder_id(
    *,
    entity_id: str,
    workspace_id: str | None,
    rel_path: str,
) -> str | None:
    from packages.core.services.generated_artifact_service import (
        generated_artifact_document_folder_id,
    )

    return await generated_artifact_document_folder_id(
        entity_id=entity_id,
        workspace_id=workspace_id,
        rel_path=rel_path,
    )


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
    folder_id = await _artifact_document_folder_id(
        entity_id=entity_id,
        workspace_id=workspace_id,
        rel_path=rel_path,
    )
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
            "document_id": getattr(doc, "id", None),
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
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    abs_path = runtime_write_entity_file_atomic(
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
    folder_id = await _artifact_document_folder_id(
        entity_id=entity_id,
        workspace_id=workspace_id,
        rel_path=rel_path,
    )
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
    task_id: str | None = None,
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
        task_id=task_id,
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
    from packages.core.services.generated_artifact_service import (
        register_generated_file_artifact,
    )

    return await register_generated_file_artifact(
        entity_id=entity_id,
        user_id=user_id,
        filename=filename,
        rel_path=rel_path,
        file_size=file_size,
        file_type=file_type,
        mime_type=mime_type,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        tool_name=tool_name,
        artifact_role=artifact_role,
        generation=generation,
    )


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
    from packages.core.services.generated_artifact_service import (
        bind_generated_artifact_to_workspace,
    )

    await bind_generated_artifact_to_workspace(
        entity_id=entity_id,
        document_id=document_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
    )


async def _load_timeline_json(
    entity_root: str,
    timeline_path: str,
    entity_id: str,
    workspace_base_dir: str = "",
) -> dict[str, Any]:
    rel_path = _workspace_media_reference(
        timeline_path,
        entity_id=entity_id,
        workspace_base_dir=workspace_base_dir,
    )
    abs_path = await _resolve_entity_file(entity_root, rel_path)
    if Path(abs_path).suffix.lower() != ".json":
        raise ValueError(f"Timeline must be a JSON file: {rel_path}")
    with open(abs_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Timeline JSON must contain an object")
    return data


async def _load_entity_json(
    entity_root: str,
    path: str,
    entity_id: str,
    workspace_base_dir: str = "",
) -> Any:
    rel_path = _workspace_media_reference(
        path,
        entity_id=entity_id,
        workspace_base_dir=workspace_base_dir,
    )
    abs_path = await _resolve_entity_file(entity_root, rel_path)
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


def _compose_subtitle_style(
    *,
    subtitle_path: str,
    width: int,
    height: int,
    timeline: dict[str, Any],
    explicit_override: dict[str, Any] | None,
) -> dict[str, Any]:
    explicit = {
        str(key): value
        for key, value in (explicit_override or {}).items()
        if value is not None
    }
    requested = _timeline_subtitle_style(timeline, None)
    requested.update(explicit)
    if Path(subtitle_path).suffix.lower() == ".ass":
        return requested
    return _subtitle_style(
        width,
        height,
        requested,
    )


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
    workspace_base_dir: str = "",
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
        rel_path = _workspace_media_reference(
            raw_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        abs_path = await _resolve_entity_file(entity_root, rel_path)
        _assert_audio_path(abs_path)
        start = _coerce_required_time(item.get("start"), f"audio_tracks[{index}].start")
        media_info = await _probe_media(ffprobe, abs_path)
        if not media_info.get("has_audio"):
            raise ValueError(
                f"audio_tracks[{index}] source does not contain an audio stream: {rel_path}"
            )
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


def _composition_duration(
    clean_video_duration: float,
    audio_tracks: list[TimelineAudioTrack],
) -> float:
    scheduled_audio_end = max((track.end for track in audio_tracks), default=0.0)
    return max(0.0, float(clean_video_duration), scheduled_audio_end)


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
            item.get("document_id")
            or (doc or {}).get("document_id")
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
                "assetDocumentId": _string_or_none(
                    raw.get("document_id") or doc.get("document_id")
                ),
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


def _canonical_subtitle_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", normalized)


def _canonical_sentences(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", str(text or "")).replace("\r\n", "\n")
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s*|\n+", normalized)
        if part.strip()
    ]


def _semantic_alignment_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


class SubtitleWordTimestampsRequiredError(ValueError):
    """Raised when a multi-sentence STT segment has no usable word timing."""


def _word_units_for_segment(
    segment: dict[str, Any],
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        segment_start = float(segment["start"])
        segment_end = float(segment["end"])
    except (KeyError, TypeError, ValueError):
        return []
    units: list[dict[str, Any]] = []
    for word in words:
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(word.get("text") or word.get("word") or "").strip()
        midpoint = start + (end - start) / 2
        if text and end > start and segment_start <= midpoint <= segment_end:
            units.append({"start": start, "end": end, "text": text})
    return sorted(units, key=lambda item: (item["start"], item["end"]))


def _split_measured_segment_sentences(
    sentences: list[str],
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    canonical_cursor = 0
    for segment in segments:
        text = str(segment.get("text") or "")
        parts = _canonical_sentences(text)
        expected_segment = _semantic_alignment_text(text)
        best_canonical: tuple[float, int] | None = None
        for sentence_end in range(canonical_cursor, len(sentences)):
            canonical_text = " ".join(sentences[canonical_cursor : sentence_end + 1])
            actual_segment = _semantic_alignment_text(canonical_text)
            score = (
                SequenceMatcher(None, expected_segment, actual_segment).ratio()
                if expected_segment and actual_segment
                else 0.0
            )
            if best_canonical is None or score > best_canonical[0]:
                best_canonical = (score, sentence_end)
        if best_canonical is not None and best_canonical[0] >= 0.90:
            sentence_end = best_canonical[1]
            parts = sentences[canonical_cursor : sentence_end + 1]
            canonical_cursor = sentence_end + 1
        if len(parts) <= 1:
            expanded.append(segment)
            continue
        word_units = _word_units_for_segment(segment, words)
        if not word_units:
            raise SubtitleWordTimestampsRequiredError(
                "Measured alignment needs word-level timestamps when one STT segment "
                "contains multiple canonical sentences."
            )
        cursor = 0
        for part in parts:
            expected = _semantic_alignment_text(part)
            best: tuple[float, int] | None = None
            combined: list[str] = []
            for end_index in range(cursor, len(word_units)):
                combined.append(str(word_units[end_index]["text"]))
                actual = _semantic_alignment_text(" ".join(combined))
                score = (
                    SequenceMatcher(None, expected, actual).ratio()
                    if expected and actual
                    else 0.0
                )
                if best is None or score > best[0]:
                    best = (score, end_index)
            if best is None or best[0] < 0.90:
                raise SubtitleWordTimestampsRequiredError(
                    "The STT word-level timestamps could not be mapped to every sentence "
                    "inside a multi-sentence segment."
                )
            end_index = best[1]
            expanded.append(
                {
                    **segment,
                    "start": word_units[cursor]["start"],
                    "end": word_units[end_index]["end"],
                    "text": part,
                    "timing_source": "measured_stt_words",
                }
            )
            cursor = end_index + 1
    return expanded


def _align_sentences_to_segments(
    sentences: list[str],
    segments: list[dict[str, Any]],
    *,
    words: list[dict[str, Any]] | None = None,
) -> tuple[list[SubtitleCue], dict[str, Any]]:
    segments = _split_measured_segment_sentences(sentences, segments, words or [])
    cues: list[SubtitleCue] = []
    scores: list[float] = []
    aligned_sentence_indexes: list[int] = []
    cursor = 0

    for sentence_index, sentence in enumerate(sentences, start=1):
        expected = _semantic_alignment_text(sentence)
        best: tuple[float, int] | None = None
        combined_parts: list[str] = []
        for end in range(cursor, len(segments)):
            combined_parts.append(str(segments[end].get("text") or ""))
            actual = _semantic_alignment_text(" ".join(combined_parts))
            score = SequenceMatcher(None, expected, actual).ratio() if expected and actual else 0.0
            if best is None or score > best[0]:
                best = (score, end)
            if score >= 0.90:
                break

        if best is None or best[0] < 0.90:
            continue

        end = best[1]
        try:
            start_seconds = float(segments[cursor]["start"])
            end_seconds = float(segments[end]["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end_seconds <= start_seconds:
            continue
        timing_sources = {
            str(item.get("timing_source") or "measured_stt_segments")
            for item in segments[cursor : end + 1]
        }
        timing_source = (
            next(iter(timing_sources))
            if len(timing_sources) == 1
            else "mixed_measured_stt_timestamps"
        )
        cues.append(
            SubtitleCue(
                index=len(cues) + 1,
                start=start_seconds,
                end=end_seconds,
                text=sentence,
                cue_type="narration",
                estimated=False,
                measured=True,
                timing_source=timing_source,
            )
        )
        scores.append(best[0])
        aligned_sentence_indexes.append(sentence_index)
        cursor = end + 1

    sentence_count = len(sentences)
    aligned_set = set(aligned_sentence_indexes)
    missing_sentence_indexes = [
        index for index in range(1, sentence_count + 1) if index not in aligned_set
    ]
    return cues, {
        "similarity": sum(scores) / sentence_count if sentence_count else 0.0,
        "coverage": len(cues) / sentence_count if sentence_count else 0.0,
        "aligned_sentence_indexes": aligned_sentence_indexes,
        "missing_sentence_indexes": missing_sentence_indexes,
        "timing_sources": sorted({cue.timing_source for cue in cues}),
    }


def _subtitle_font_size(width: int, height: int) -> int:
    return max(16, min(56, round(min(width, height) * 14 / 288)))


def _subtitle_margin_v(width: int, height: int) -> int:
    return max(28, min(80, round(min(width, height) / 15)))


def _subtitle_outline(width: int, height: int) -> int:
    return max(1, min(2, round(min(width, height) / 540)))


def _subtitle_style(
    width: int,
    height: int,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    style: dict[str, Any] = {
        "font_size": _subtitle_font_size(width, height),
        "margin_v": _subtitle_margin_v(width, height),
        "outline": _subtitle_outline(width, height),
        "shadow": 0,
        "alignment": 2,
        "max_lines": 2,
    }
    if isinstance(override, dict):
        style.update({str(key): value for key, value in override.items() if value is not None})
    return style


def _subtitle_cues_match_transcript(
    cues: list[SubtitleCue],
    transcript: str,
) -> bool:
    cue_text = "".join(cue.text for cue in cues)
    return bool(transcript.strip()) and (
        _canonical_subtitle_text(cue_text) == _canonical_subtitle_text(transcript)
    )


async def _audio_prompt_matches_transcript(
    *,
    entity_id: str,
    audio_rel_path: str,
    transcript: str,
) -> bool | None:
    """Follow normalized-audio provenance to compare its TTS prompt when available."""
    provenance = await _audio_prompt_provenance(
        entity_id=entity_id,
        audio_rel_path=audio_rel_path,
    )
    if provenance is None:
        return None
    prompt, _source_path = provenance
    return _canonical_subtitle_text(prompt) == _canonical_subtitle_text(transcript)


async def _audio_prompt_provenance(
    *,
    entity_id: str,
    audio_rel_path: str,
) -> tuple[str, str] | None:
    """Return the immutable TTS prompt and source path behind normalized audio."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    current_path = _rel_path_from_reference(audio_rel_path, entity_id) or audio_rel_path
    seen: set[str] = set()
    async with async_session() as db:
        for _depth in range(3):
            current_path = str(current_path or "").strip()
            if not current_path or current_path in seen:
                return None
            seen.add(current_path)
            document = (
                await db.execute(
                    select(Document)
                    .where(
                        Document.entity_id == entity_id,
                        Document.fs_path == current_path,
                        Document.is_trashed == False,  # noqa: E712
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if document is None:
                return None
            generation = (document.metadata_ or {}).get("generation")
            generation = generation if isinstance(generation, dict) else {}
            prompt = generation.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                return prompt.strip(), current_path
            source_path = generation.get("input_path") or generation.get("source_path")
            if not isinstance(source_path, str) or not source_path.strip():
                return None
            current_path = _rel_path_from_reference(source_path, entity_id) or source_path
    return None


async def _audio_normalization_provenance(
    *,
    entity_id: str,
    audio_rel_path: str,
) -> dict[str, Any] | None:
    """Return the normalization receipt attached to this exact audio artifact."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    current_path = _rel_path_from_reference(audio_rel_path, entity_id) or audio_rel_path
    async with async_session() as db:
        document = (
            await db.execute(
                select(Document)
                .where(
                    Document.entity_id == entity_id,
                    Document.fs_path == str(current_path or "").strip(),
                    Document.is_trashed == False,  # noqa: E712
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if document is None:
        return None
    generation = (document.metadata_ or {}).get("generation")
    return generation if isinstance(generation, dict) else None


def _validate_narrator_profile(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict):
        raise ValueError("Narrator profile receipt must be an object.")
    version = value.get("version")
    provider = str(value.get("provider") or "").strip().lower()
    model = str(value.get("model") or "").strip()
    voice = str(value.get("voice") or "").strip()
    voice_instructions = str(value.get("voice_instructions") or "").strip()
    if version != 1 or not provider or not model or not voice or voice.lower() == "random":
        raise ValueError("Narrator profile receipt must contain one concrete model and voice.")
    if model.split("/", 1)[0].strip().lower() != provider:
        raise ValueError("Narrator profile provider does not match its model.")
    return {
        "version": 1,
        "provider": provider,
        "model": model,
        "voice": voice,
        "voice_instructions": voice_instructions,
    }


async def _audio_narrator_profile(
    *,
    entity_id: str,
    audio_rel_path: str,
) -> dict[str, str | int] | None:
    """Follow normalized-audio provenance to recover its task narrator profile."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    current_path = _rel_path_from_reference(audio_rel_path, entity_id) or audio_rel_path
    seen: set[str] = set()
    async with async_session() as db:
        for _depth in range(3):
            current_path = str(current_path or "").strip()
            if not current_path or current_path in seen:
                return None
            seen.add(current_path)
            document = (
                await db.execute(
                    select(Document)
                    .where(
                        Document.entity_id == entity_id,
                        Document.fs_path == current_path,
                        Document.is_trashed == False,  # noqa: E712
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if document is None:
                return None
            generation = (document.metadata_ or {}).get("generation")
            generation = generation if isinstance(generation, dict) else {}
            profile = generation.get("narration_profile")
            if profile is not None:
                return _validate_narrator_profile(profile)
            source_path = generation.get("input_path") or generation.get("source_path")
            if not isinstance(source_path, str) or not source_path.strip():
                return None
            current_path = _rel_path_from_reference(source_path, entity_id) or source_path
    return None


async def _transcribe_narration_audio(
    *,
    audio_path: str,
    user_id: str,
    entity_id: str,
    reference_transcript: str = "",
    workspace_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> Any:
    from packages.core.ai.runtime import (
        RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
        runtime_assert_credit_available,
    )
    from packages.core.services.model_resolver import (
        resolve_llm_metadata_for_user,
        resolve_model_for_user,
    )
    from packages.core.services.voice.whisper import (
        WHISPER_MAX_UPLOAD_BYTES,
        WhisperUploadTooLargeError,
        transcribe_blob,
        whisper_cost_usd,
    )

    stt_model = await resolve_model_for_user(
        "stt",
        user_id=user_id or None,
        entity_id=entity_id or None,
    )
    metadata = await resolve_llm_metadata_for_user(
        "stt",
        user_id=user_id or None,
        entity_id=entity_id or None,
    )
    user_key = (metadata or {}).get("llm_api_key")
    user_base_url = (metadata or {}).get("llm_base_url")
    path = Path(audio_path)
    file_size = path.stat().st_size
    if file_size > WHISPER_MAX_UPLOAD_BYTES:
        raise WhisperUploadTooLargeError(
            f"Narration audio is too large: {file_size / 1024 / 1024:.1f} MB exceeds "
            f"the {WHISPER_MAX_UPLOAD_BYTES // (1024 * 1024)} MB STT upload limit. "
            "Export a shorter or compressed narration file and retry."
        )
    if not user_key:
        await runtime_assert_credit_available(
            entity_id,
            source=RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
        )
    transcribe_kwargs: dict[str, Any] = {
        "mime": _audio_mime_from_path(audio_path),
        "filename": path.name,
        "user_api_key": user_key,
        "resolved_model": stt_model,
        "require_timestamps": True,
        "reference_transcript": reference_transcript,
    }
    if user_base_url:
        transcribe_kwargs["user_base_url"] = user_base_url
    result = await transcribe_blob(
        path.read_bytes(),
        **transcribe_kwargs,
    )
    if not user_key:
        try:
            from packages.core.database import async_session
            from packages.core.services.usage_service import record_media_usage

            async with async_session() as db:
                await record_media_usage(
                    db,
                    entity_id=entity_id,
                    kind="whisper",
                    model=result.model,
                    cost_usd=whisper_cost_usd(result.duration_seconds, result.model),
                    units=int(result.duration_seconds),
                    workspace_id=workspace_id,
                    user_id=user_id or None,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    source=RUNTIME_AUDIO_TRANSCRIBE_SOURCE,
                    byok=False,
                )
                await db.commit()
        except Exception:
            logger.debug("narration STT billing failed (best-effort)", exc_info=True)
    return result


def _sentence_indexes_for_text(text: str, sentences: list[str]) -> list[int]:
    indexes: list[int] = []
    cursor = 0
    for part in _canonical_sentences(text):
        expected = _semantic_alignment_text(part)
        best: tuple[float, int] | None = None
        for index in range(cursor, len(sentences)):
            score = SequenceMatcher(
                None,
                expected,
                _semantic_alignment_text(sentences[index]),
            ).ratio()
            if best is None or score > best[0]:
                best = (score, index)
        if best is None or best[0] < 0.90:
            continue
        indexes.append(best[1] + 1)
        cursor = best[1] + 1
    return indexes


def _scene_time_range(item: dict[str, Any]) -> tuple[float | None, float | None]:
    start = item.get("start", item.get("start_seconds"))
    end = item.get("end", item.get("end_seconds"))
    if start is not None or end is not None:
        return (
            _coerce_float(start, -1.0) if start is not None else None,
            _coerce_float(end, -1.0) if end is not None else None,
        )
    for key in ("range", "time", "timeline_range"):
        if item.get(key) is not None:
            return _parse_timeline_range(item.get(key))
    return None, None


def _scene_sentence_indexes(item: dict[str, Any], sentences: list[str]) -> list[int]:
    raw_indexes = item.get("sentence_indexes")
    if isinstance(raw_indexes, list):
        return sorted(
            {
                int(value)
                for value in raw_indexes
                if str(value).strip().lstrip("-").isdigit() and 1 <= int(value) <= len(sentences)
            }
        )

    raw_range = item.get("sentence_range") or item.get("narration_sentence_range")
    if isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
        try:
            start, end = int(raw_range[0]), int(raw_range[1])
        except (TypeError, ValueError):
            start = end = -1
        if start == 0:
            start += 1
            end += 1
        if 1 <= start <= end <= len(sentences):
            return list(range(start, end + 1))

    for key in ("narration_text", "narration", "subtitle", "subtitle_text", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _sentence_indexes_for_text(value, sentences)
    return []


def _scene_alignment_coverage(
    payloads: list[Any],
    sentences: list[str],
    cues: list[SubtitleCue],
    aligned_sentence_indexes: list[int],
) -> dict[str, Any]:
    declared: dict[str, dict[str, Any]] = {}
    boundary_ranges: dict[str, list[tuple[float, float]]] = {}
    cue_sentence_indexes: dict[str, set[int]] = {}

    def scene_id_for(item: dict[str, Any]) -> str:
        return str(item.get("scene_id") or "").strip()

    def item_range(item: dict[str, Any]) -> tuple[float, float] | None:
        start, end = _scene_time_range(item)
        if start is None or end is None or start < 0 or end <= start:
            return None
        return start, end

    def number_range(start_value: Any, end_value: Any) -> tuple[float, float] | None:
        if start_value is None or end_value is None:
            return None
        start = _coerce_float(start_value, -1.0)
        end = _coerce_float(end_value, -1.0)
        if start < 0 or end <= start:
            return None
        return start, end

    def declare(item: dict[str, Any]) -> None:
        scene_id = scene_id_for(item)
        if not scene_id:
            return
        scene = declared.setdefault(
            scene_id,
            {"sentence_indexes": set(), "ranges": [], "narration_ranges": []},
        )
        scene["sentence_indexes"].update(_scene_sentence_indexes(item, sentences))
        time_range = item_range(item)
        if time_range is not None:
            scene["ranges"].append(time_range)

    def declare_visual_interval(item: dict[str, Any]) -> None:
        scene_id = scene_id_for(item)
        if not scene_id:
            return
        visual_range = number_range(
            item.get("visual_start_seconds"),
            item.get("visual_end_seconds"),
        )
        narration_range = number_range(
            item.get("narration_start_seconds"),
            item.get("narration_end_seconds"),
        )
        scene = declared.setdefault(
            scene_id,
            {"sentence_indexes": set(), "ranges": [], "narration_ranges": []},
        )
        if visual_range is not None:
            scene["ranges"].append(visual_range)
        if narration_range is not None:
            scene["narration_ranges"].append(narration_range)
            start, end = narration_range
            scene["sentence_indexes"].update(
                sentence_index
                for sentence_index, cue in zip(
                    aligned_sentence_indexes,
                    cues,
                    strict=False,
                )
                if cue.end > start and cue.start < end
            )

    def collect_boundary(item: dict[str, Any]) -> None:
        scene_id = scene_id_for(item)
        time_range = item_range(item)
        if scene_id and time_range is not None:
            boundary_ranges.setdefault(scene_id, []).append(time_range)

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        scenes = payload.get("scenes")
        if isinstance(scenes, list):
            for item in scenes:
                if isinstance(item, dict):
                    declare(item)
        segments = payload.get("segments")
        if isinstance(segments, list):
            for item in segments:
                if (
                    isinstance(item, dict)
                    and scene_id_for(item)
                    and item_range(item) is not None
                ):
                    declare(item)
        visual_scene_intervals = payload.get("visual_scene_intervals")
        if isinstance(visual_scene_intervals, list):
            for item in visual_scene_intervals:
                if isinstance(item, dict):
                    declare_visual_interval(item)
        for key in ("scene_boundaries", "canonical_scene_boundaries"):
            boundaries = payload.get(key)
            if isinstance(boundaries, list):
                for item in boundaries:
                    if isinstance(item, dict):
                        collect_boundary(item)
        for item in _timeline_video_items(payload):
            collect_boundary(item)
        for item in _subtitle_items_from_payload(payload):
            if not isinstance(item, dict):
                continue
            scene_id = scene_id_for(item)
            if scene_id:
                cue_sentence_indexes.setdefault(scene_id, set()).update(
                    _sentence_indexes_for_text(_subtitle_text(item), sentences)
                )

    for scene_id, scene in declared.items():
        scene["ranges"].extend(boundary_ranges.get(scene_id, []))
        scene["sentence_indexes"].update(cue_sentence_indexes.get(scene_id, set()))

    aligned_cues = {
        sentence_index: cue
        for sentence_index, cue in zip(aligned_sentence_indexes, cues, strict=False)
    }
    covered_scene_ids: list[str] = []
    missing_scene_ids: list[str] = []
    missing_interval_scene_ids: list[str] = []
    unmapped_scene_ids: list[str] = []
    for scene_id, scene in declared.items():
        if not scene["ranges"]:
            missing_interval_scene_ids.append(scene_id)
            missing_scene_ids.append(scene_id)
            continue
        if not scene["sentence_indexes"]:
            unmapped_scene_ids.append(scene_id)
            missing_scene_ids.append(scene_id)
            continue
        alignment_ranges = scene["narration_ranges"] or scene["ranges"]
        covered = any(
            sentence_index in aligned_cues
            and aligned_cues[sentence_index].end > start
            and aligned_cues[sentence_index].start < end
            for sentence_index in scene["sentence_indexes"]
            for start, end in alignment_ranges
        )
        (covered_scene_ids if covered else missing_scene_ids).append(scene_id)

    declared_count = len(declared)
    return {
        "coverage": len(covered_scene_ids) / declared_count if declared_count else 1.0,
        "covered_scene_ids": covered_scene_ids,
        "missing_scene_ids": missing_scene_ids,
        "missing_interval_scene_ids": missing_interval_scene_ids,
        "unmapped_scene_ids": unmapped_scene_ids,
    }


def _subtitle_cues_have_measured_timing(cues: list[SubtitleCue]) -> bool:
    return bool(cues) and all(cue.measured and not cue.estimated for cue in cues)


def _scale_subtitle_cues_to_duration(
    cues: list[SubtitleCue],
    duration_seconds: float,
) -> list[SubtitleCue]:
    if not cues or duration_seconds <= 0:
        return cues
    source_end = max(cue.end for cue in cues)
    if source_end <= 0:
        return cues
    scale = duration_seconds / source_end
    scaled: list[SubtitleCue] = []
    for index, cue in enumerate(cues):
        end = duration_seconds if index == len(cues) - 1 else round(cue.end * scale, 3)
        scaled.append(
            SubtitleCue(
                index=cue.index,
                start=round(cue.start * scale, 3),
                end=round(end, 3),
                text=cue.text,
                cue_type=cue.cue_type,
                source_path=cue.source_path,
                estimated=cue.estimated or abs(scale - 1.0) > 0.001,
                measured=False,
                timing_source="proportional_duration_scale",
            )
        )
    return scaled


def _fit_subtitle_cues_to_line_limit(
    cues: list[SubtitleCue],
    *,
    max_chars_per_line: int,
    max_lines: int = 2,
) -> list[SubtitleCue]:
    """Split long cues while preserving their text order and timing span."""
    line_width = max(1, int(max_chars_per_line))
    line_limit = max(1, int(max_lines))
    fitted: list[SubtitleCue] = []

    for cue in cues:
        wrapped_lines = _wrap_subtitle_text(cue.text, line_width).splitlines()
        chunks = [
            " ".join(wrapped_lines[index:index + line_limit]).strip()
            for index in range(0, len(wrapped_lines), line_limit)
        ] or [cue.text]
        weights = [max(1, len(_canonical_subtitle_text(chunk))) for chunk in chunks]
        total_weight = sum(weights)
        cue_duration = max(0.0, cue.end - cue.start)
        elapsed_weight = 0

        for chunk_index, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
            start = cue.start + cue_duration * elapsed_weight / total_weight
            elapsed_weight += weight
            end = (
                cue.end
                if chunk_index == len(chunks) - 1
                else cue.start + cue_duration * elapsed_weight / total_weight
            )
            fitted.append(
                replace(
                    cue,
                    index=len(fitted) + 1,
                    start=round(start, 3),
                    end=round(end, 3),
                    text=chunk,
                )
            )

    return fitted


async def _collect_subtitle_cues(
    *,
    ffprobe: str,
    entity_root: str,
    entity_id: str,
    timeline: dict[str, Any],
    cue_payloads: list[Any],
    track_types: set[str],
    workspace_base_dir: str = "",
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
            workspace_base_dir=workspace_base_dir,
        )
        if end <= start:
            continue
        estimated = bool(item.get("estimated", estimated))
        timing_source = str(
            item.get("timing_source") or item.get("timestamp_source") or ""
        ).strip()
        normalized_timing_source = timing_source.casefold().replace("-", "_")
        measured = not estimated and (
            item.get("measured") is True
            or normalized_timing_source.startswith("measured")
            or normalized_timing_source
            in {"stt_segments", "speech_segments", "transcription_segments", "whisper_segments"}
        )
        cues.append(
            SubtitleCue(
                index=len(cues) + 1,
                start=start,
                end=end,
                text=text,
                cue_type=cue_type,
                source_path=str(item.get("path") or item.get("fs_path") or ""),
                estimated=estimated,
                measured=measured,
                timing_source=timing_source,
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
            measured=cue.measured,
            timing_source=cue.timing_source,
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
    workspace_base_dir: str = "",
) -> tuple[float, bool]:
    if item.get("end") is not None:
        return _coerce_required_time(item.get("end"), "subtitle cue end"), False
    if item.get("duration") is not None:
        return start + max(0.01, _coerce_float(item.get("duration"), 0.01)), False

    raw_path = str(item.get("path") or item.get("fs_path") or "").strip()
    if raw_path:
        if not ffprobe:
            raise ValueError("ffprobe is required to derive subtitle end from audio duration")
        rel_path = _workspace_media_reference(
            raw_path,
            entity_id=entity_id,
            workspace_base_dir=workspace_base_dir,
        )
        abs_path = await _resolve_entity_file(entity_root, rel_path)
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
    canvas_width: int = 1920,
    canvas_height: int = 1080,
) -> str:
    resolved_style = _subtitle_style(canvas_width, canvas_height, style)
    cues = _fit_subtitle_cues_to_line_limit(
        cues,
        max_chars_per_line=max_chars_per_line,
        max_lines=int(_clamp_float(resolved_style.get("max_lines"), 1, 2, 2)),
    )
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
        return _render_ass_subtitles(
            cues,
            max_chars_per_line=max_chars_per_line,
            style=resolved_style,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )

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
    canvas_width: int = 1920,
    canvas_height: int = 1080,
) -> str:
    force_style = _subtitle_force_style(style)
    style_map = _ass_style_map(force_style)
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {canvas_width}",
        f"PlayResY: {canvas_height}",
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
    limit = max(1, int(max_chars_per_line))
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > limit:
            if current:
                lines.append(current)
                current = ""
            while len(word) > limit:
                lines.append(word[:limit])
                word = word[limit:]
            current = word
            continue
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit:
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


def _loudnorm_measurement(stderr: str) -> dict[str, float]:
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    for match in reversed(re.findall(r"\{\s*\"input_i\".*?\}", stderr or "", re.DOTALL)):
        try:
            payload = json.loads(match)
            return {key: float(payload[key]) for key in required}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    raise ValueError("FFmpeg loudnorm analysis did not return a complete measurement")


async def _apply_two_pass_loudnorm(
    *,
    ffmpeg: str,
    input_path: str,
    config: dict[str, Any],
    total_duration: float,
) -> None:
    """Normalize a composed MP4 deterministically without re-encoding video."""
    if not config.get("enabled"):
        return
    target_i = _loudness_target_lufs(config.get("target_lufs"))
    target_tp = _loudness_true_peak(config.get("true_peak"))
    target_lra = _loudness_lra(config.get("lra"))
    analysis_filter = (
        f"loudnorm=I={target_i:.1f}:TP={target_tp:.1f}:LRA={target_lra:.1f}:"
        "print_format=json"
    )
    _stdout, stderr = await _run_process(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            input_path,
            "-vn",
            "-af",
            analysis_filter,
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=max(180.0, (total_duration or 60.0) * 4.0 + 120.0),
    )
    measured = _loudnorm_measurement(stderr)
    correction_filter = (
        f"loudnorm=I={target_i:.1f}:TP={target_tp:.1f}:LRA={target_lra:.1f}:"
        f"measured_I={measured['input_i']:.3f}:"
        f"measured_TP={measured['input_tp']:.3f}:"
        f"measured_LRA={measured['input_lra']:.3f}:"
        f"measured_thresh={measured['input_thresh']:.3f}:"
        f"offset={measured['target_offset']:.3f}:linear=true:print_format=summary"
    )
    temp_path = f"{input_path}.loudnorm-{generate_ulid()}.mp4"
    try:
        await _run_process(
            [
                ffmpeg,
                "-y",
                "-i",
                input_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c:v",
                "copy",
                "-af",
                correction_filter,
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-t",
                f"{total_duration:.3f}",
                "-movflags",
                "+faststart",
                temp_path,
            ],
            timeout_seconds=max(180.0, (total_duration or 60.0) * 4.0 + 120.0),
        )
        os.replace(temp_path, input_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    # AAC encoding can introduce inter-sample overshoot after loudnorm has
    # already limited the decoded PCM stream. Measure the encoded result and
    # apply the smallest deterministic attenuation needed to keep the final
    # file at or below the requested true-peak ceiling.
    await _enforce_encoded_true_peak(
        ffmpeg=ffmpeg,
        input_path=input_path,
        target_true_peak=target_tp,
        total_duration=total_duration,
    )


async def _measure_encoded_true_peak(
    *,
    ffmpeg: str,
    input_path: str,
    total_duration: float,
) -> float:
    _stdout, stderr = await _run_process(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            input_path,
            "-map",
            "0:a:0",
            "-af",
            "ebur128=peak=true:framelog=verbose",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=max(120.0, (total_duration or 60.0) * 2.0 + 30.0),
    )
    measured = _parse_ebur128(stderr).get("true_peak_dbfs")
    if measured is None:
        raise ValueError("FFmpeg ebur128 analysis did not return an encoded true-peak value")
    return float(measured)


async def _enforce_encoded_true_peak(
    *,
    ffmpeg: str,
    input_path: str,
    target_true_peak: float,
    total_duration: float,
    max_attempts: int = 2,
) -> None:
    """Cap post-AAC true peak while preserving the already-normalized mix."""
    target = _loudness_true_peak(target_true_peak)
    measured = await _measure_encoded_true_peak(
        ffmpeg=ffmpeg,
        input_path=input_path,
        total_duration=total_duration,
    )
    limiter_ceiling_db = target
    for attempt in range(max(1, int(max_attempts))):
        if measured <= target:
            return

        # One tenth of a decibel of safety headroom absorbs measurement
        # rounding and the small overshoot introduced by the next AAC encode.
        if attempt == 0:
            # First limit only transient sample peaks so the mix keeps its
            # normalized LUFS. If AAC inter-sample overshoot survives, the
            # next pass must attenuate the encoded mix by the measured delta.
            limiter_ceiling_db += target - measured - 0.1
            limiter_limit = max(
                0.0625,
                min(1.0, math.pow(10.0, limiter_ceiling_db / 20.0)),
            )
            correction_filter = (
                f"alimiter=limit={limiter_limit:.6f}:level=false:latency=true"
            )
        else:
            attenuation_db = target - measured - 0.1
            correction_filter = f"volume={attenuation_db:.3f}dB"
        temp_path = f"{input_path}.true-peak-{generate_ulid()}.mp4"
        try:
            await _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    input_path,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "copy",
                    "-af",
                    correction_filter,
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-t",
                    f"{total_duration:.3f}",
                    "-movflags",
                    "+faststart",
                    temp_path,
                ],
                timeout_seconds=max(180.0, (total_duration or 60.0) * 4.0 + 120.0),
            )
            os.replace(temp_path, input_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

        measured = await _measure_encoded_true_peak(
            ffmpeg=ffmpeg,
            input_path=input_path,
            total_duration=total_duration,
        )

    if measured > target:
        raise ValueError(
            "Encoded true peak remains above the requested ceiling after correction: "
            f"measured {measured:.1f} dBFS, maximum {target:.1f} dBFS"
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
    task_id: str | None = None,
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
    clean_video_duration: float | None = None,
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
        task_id=task_id,
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
        default_dir=workspace_artifact_default_dir(workspace_base_dir, WorkspaceArtifactDir.VIDEOS.value),
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
    video_filters: list[str] = []
    source_duration = total_duration if clean_video_duration is None else clean_video_duration
    extension_seconds = max(0.0, total_duration - source_duration)
    if extension_seconds > 0.001:
        video_filters.append(f"tpad=stop_mode=clone:stop_duration={extension_seconds:.3f}")
    if subtitle_abs:
        video_filters.append(f"subtitles={_subtitles_filter_value(subtitle_abs, subtitle_style)}")
    if video_filters:
        video_map = "vout"
        filter_parts.append(f"[0:v:0]{','.join(video_filters)}[vout]")

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

    with tempfile.TemporaryDirectory(prefix="compose-video-") as tmp_dir:
        rendered_path = os.path.join(tmp_dir, "composed.mp4")
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
                "-t",
                f"{total_duration:.3f}",
                "-movflags",
                "+faststart",
                rendered_path,
            ]
        )
        await _run_process(
            args,
            timeout_seconds=max(180.0, (total_duration or 60.0) * 8.0 + 120.0),
        )
        await _apply_two_pass_loudnorm(
            ffmpeg=ffmpeg,
            input_path=rendered_path,
            config=loudness_config,
            total_duration=total_duration,
        )
        runtime_copy_entity_file_atomic(
            entity_id,
            target.rel_path,
            rendered_path,
            expected_size=os.path.getsize(rendered_path),
            allow_empty=False,
        )
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
    stream_types = {
        str(stream.get("codec_type") or "")
        for stream in data.get("streams") or []
    }
    has_audio = "audio" in stream_types
    if duration <= 0 and stream_types.intersection({"video", "audio"}):
        selector = "v:0" if "video" in stream_types else "a:0"
        try:
            packet_stdout, _packet_stderr = await _run_process(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    selector,
                    "-show_entries",
                    "packet=pts_time,duration_time",
                    "-of",
                    "csv=p=0",
                    path,
                ],
                timeout_seconds=60,
            )
            for line in (packet_stdout or "").splitlines():
                fields = [field.strip() for field in line.split(",")]
                try:
                    pts = float(fields[0])
                except (IndexError, TypeError, ValueError):
                    continue
                try:
                    packet_duration = max(0.0, float(fields[1]))
                except (IndexError, TypeError, ValueError):
                    packet_duration = 0.0
                duration = max(duration, pts + packet_duration)
        except Exception:
            logger.debug("Packet timestamp duration probe failed for %s", path, exc_info=True)
    return {"duration_seconds": duration, "has_audio": has_audio}


def _probe_number(value: Any, *, integer: bool = False) -> int | float:
    try:
        return int(value) if integer else float(value)
    except (TypeError, ValueError):
        return 0 if integer else 0.0


def _probe_frame_rate(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" not in text:
        return float(_probe_number(text))
    numerator, denominator = text.split("/", 1)
    denominator_value = float(_probe_number(denominator))
    if denominator_value == 0:
        return 0.0
    return float(_probe_number(numerator)) / denominator_value


def _frame_sample_times(
    *,
    duration_seconds: float,
    timestamps: list[float] | None,
    scene_boundaries: list[float] | None,
    interval_seconds: float,
    max_samples: int,
) -> list[float]:
    duration = max(0.001, float(_probe_number(duration_seconds)))
    end_margin = min(0.1, duration / 2)
    end_time = round(max(0.0, duration - end_margin), 3)
    candidates = {0.0, end_time}
    for value in [*(timestamps or []), *(scene_boundaries or [])]:
        number = float(_probe_number(value))
        if 0 <= number < duration:
            candidates.add(round(min(number, end_time), 3))

    interval = max(0.25, float(_probe_number(interval_seconds)) or 10.0)
    cursor = interval
    while cursor < end_time:
        candidates.add(round(cursor, 3))
        cursor += interval

    ordered = sorted(candidates)
    limit = max(1, min(24, int(max_samples or 12)))
    if len(ordered) <= limit:
        return ordered
    if limit == 1:
        return [ordered[0]]
    indexes = {
        round(position * (len(ordered) - 1) / (limit - 1))
        for position in range(limit)
    }
    thinned = [ordered[index] for index in sorted(indexes)]
    if len(thinned) < limit:
        for value in ordered:
            if value not in thinned:
                thinned.append(value)
            if len(thinned) == limit:
                break
        thinned.sort()
    return thinned


def _last_ffmpeg_measurement(stderr: str, label: str, unit: str) -> float | None:
    matches = re.findall(
        rf"\b{re.escape(label)}:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*{re.escape(unit)}\b",
        stderr or "",
        flags=re.IGNORECASE,
    )
    for raw in reversed(matches):
        try:
            value = float(raw)
        except ValueError:
            continue
        if value not in {float("inf"), float("-inf")}:
            return value
    return None


def _parse_ebur128(stderr: str) -> dict[str, float | None]:
    return {
        "integrated_lufs": _last_ffmpeg_measurement(stderr, "I", "LUFS"),
        "loudness_range_lu": _last_ffmpeg_measurement(stderr, "LRA", "LU"),
        "true_peak_dbfs": _last_ffmpeg_measurement(stderr, "Peak", "dBFS"),
    }


def _parse_silence_intervals(
    stderr: str,
    *,
    duration_seconds: float,
) -> dict[str, Any]:
    duration = max(0.0, float(_probe_number(duration_seconds)))
    intervals: list[dict[str, float]] = []
    active_start: float | None = None
    for line in (stderr or "").splitlines():
        start_match = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
        if start_match:
            active_start = max(0.0, min(duration, float(start_match.group(1))))
        end_match = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?)", line)
        if end_match and active_start is not None:
            end = max(active_start, min(duration, float(end_match.group(1))))
            intervals.append({
                "start": round(active_start, 3),
                "end": round(end, 3),
                "duration_seconds": round(end - active_start, 3),
            })
            active_start = None
    if active_start is not None and duration > active_start:
        intervals.append({
            "start": round(active_start, 3),
            "end": round(duration, 3),
            "duration_seconds": round(duration - active_start, 3),
        })
    total = round(sum(item["duration_seconds"] for item in intervals), 3)
    leading = intervals[0]["duration_seconds"] if intervals and intervals[0]["start"] == 0 else 0.0
    trailing = (
        intervals[-1]["duration_seconds"]
        if intervals and abs(intervals[-1]["end"] - duration) <= 0.001
        else 0.0
    )
    return {
        "intervals": intervals,
        "total_silence_seconds": total,
        "leading_silence_seconds": round(leading, 3),
        "trailing_silence_seconds": round(trailing, 3),
        "silence_ratio": round(min(1.0, total / duration), 4) if duration > 0 else 0.0,
    }


def _parse_subtitle_timestamp(value: str) -> float:
    token = str(value or "").strip().split()[0].replace(",", ".")
    parts = token.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        else:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid subtitle timestamp: {value}") from exc
    if hours < 0 or minutes < 0 or minutes >= 60 or seconds < 0 or seconds >= 60:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    return round(hours * 3600 + minutes * 60 + seconds, 3)


def _normalize_subtitle_text(value: str, *, ass: bool = False) -> tuple[str, int]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if ass:
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
        text = re.sub(r"\{[^}]*\}", "", text)
    else:
        text = re.sub(r"<[^>]*>", "", text)
    visible_lines = [" ".join(line.split()) for line in text.split("\n")]
    normalized_lines = [line for line in visible_lines if line]
    return "\n".join(normalized_lines), len(normalized_lines)


def _parse_block_subtitles(content: str, subtitle_format: str) -> dict[str, Any]:
    cues: list[dict[str, Any]] = []
    normalized = str(content or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n[ \t]*\n", normalized):
        lines = block.strip("\n").split("\n")
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        start = _parse_subtitle_timestamp(timing[0])
        end = _parse_subtitle_timestamp(timing[1])
        raw_id = lines[timing_index - 1].strip() if timing_index > 0 else ""
        cue_id = raw_id or str(len(cues) + 1)
        text, line_count = _normalize_subtitle_text("\n".join(lines[timing_index + 1:]))
        cues.append({
            "id": cue_id,
            "start": start,
            "end": end,
            "text": text,
            "line_count": line_count,
        })
    return {
        "format": subtitle_format,
        "cues": cues,
        "styles": {},
        "play_res_y": None,
    }


def _parse_ass_subtitles(content: str) -> dict[str, Any]:
    section = ""
    style_fields: list[str] = []
    event_fields: list[str] = []
    styles: dict[str, dict[str, Any]] = {}
    cues: list[dict[str, Any]] = []
    play_res_y: int | None = None

    for raw_line in str(content or "").lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        key_lower = key.strip().lower()
        value = raw_value.lstrip()
        if section == "script info" and key_lower == "playresy":
            play_res_y = int(_probe_number(value, integer=True)) or None
            continue
        if section in {"v4+ styles", "v4 styles"}:
            if key_lower == "format":
                style_fields = [field.strip().lower() for field in value.split(",")]
                continue
            if key_lower == "style" and style_fields:
                values = [item.strip() for item in value.split(",", len(style_fields) - 1)]
                if len(values) != len(style_fields):
                    continue
                style = dict(zip(style_fields, values, strict=True))
                name = str(style.get("name") or "").strip()
                if name:
                    font_size = _probe_number(style.get("fontsize"))
                    outline = _probe_number(style.get("outline"))
                    shadow = _probe_number(style.get("shadow"))
                    styles[name] = {
                        "font_size": font_size if font_size > 0 else None,
                        "outline": outline if style.get("outline") is not None else None,
                        "shadow": shadow if style.get("shadow") is not None else None,
                        "alignment": int(_probe_number(style.get("alignment"), integer=True)),
                        "margin_v": int(_probe_number(style.get("marginv"), integer=True)),
                    }
                continue
        if section != "events":
            continue
        if key_lower == "format":
            event_fields = [field.strip().lower() for field in value.split(",")]
            continue
        if key_lower != "dialogue" or not event_fields:
            continue
        values = [item.strip() for item in value.split(",", len(event_fields) - 1)]
        if len(values) != len(event_fields):
            continue
        event = dict(zip(event_fields, values, strict=True))
        if not event.get("start") or not event.get("end"):
            continue
        text, line_count = _normalize_subtitle_text(event.get("text", ""), ass=True)
        cues.append({
            "id": str(len(cues) + 1),
            "start": _parse_subtitle_timestamp(event["start"]),
            "end": _parse_subtitle_timestamp(event["end"]),
            "text": text,
            "line_count": line_count,
            "style": str(event.get("style") or "Default").strip() or "Default",
        })
    return {
        "format": "ass",
        "cues": cues,
        "styles": styles,
        "play_res_y": play_res_y,
    }


def _parse_subtitle_content(content: str, subtitle_format: str) -> dict[str, Any]:
    selected_format = str(subtitle_format or "").strip().lower().lstrip(".")
    if selected_format in {"srt", "vtt"}:
        return _parse_block_subtitles(content, selected_format)
    if selected_format == "ass":
        return _parse_ass_subtitles(content)
    raise ValueError(f"Unsupported subtitle format: {subtitle_format}")


async def _probe_media_report(ffprobe: str, path: str) -> dict[str, Any]:
    stdout, _stderr = await _run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,bit_rate,size",
            "-show_entries",
            (
                "stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,"
                "avg_frame_rate,display_aspect_ratio,sample_rate,channels,channel_layout,duration"
            ),
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
    streams = [item for item in (data.get("streams") or []) if isinstance(item, dict)]
    media_streams = [
        item for item in streams if str(item.get("codec_type") or "") in {"video", "audio"}
    ]
    if not media_streams:
        raise RuntimeError(f"ffprobe found no audio or video streams in {path}")

    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    duration = float(_probe_number(format_info.get("duration")))
    if duration <= 0:
        duration = max(
            (float(_probe_number(stream.get("duration"))) for stream in media_streams),
            default=0.0,
        )
    video = next((item for item in media_streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in media_streams if item.get("codec_type") == "audio"), None)
    video_report = None
    if video is not None:
        video_report = {
            "index": int(_probe_number(video.get("index"), integer=True)),
            "codec": str(video.get("codec_name") or ""),
            "width": int(_probe_number(video.get("width"), integer=True)),
            "height": int(_probe_number(video.get("height"), integer=True)),
            "pixel_format": str(video.get("pix_fmt") or ""),
            "frame_rate": _probe_frame_rate(video.get("r_frame_rate")),
            "average_frame_rate": _probe_frame_rate(video.get("avg_frame_rate")),
            "display_aspect_ratio": str(video.get("display_aspect_ratio") or ""),
            "duration_seconds": float(_probe_number(video.get("duration"))),
        }
    audio_report = None
    if audio is not None:
        audio_report = {
            "index": int(_probe_number(audio.get("index"), integer=True)),
            "codec": str(audio.get("codec_name") or ""),
            "sample_rate": int(_probe_number(audio.get("sample_rate"), integer=True)),
            "channels": int(_probe_number(audio.get("channels"), integer=True)),
            "channel_layout": str(audio.get("channel_layout") or ""),
            "duration_seconds": float(_probe_number(audio.get("duration"))),
        }
    return {
        "decodable": True,
        "duration_seconds": duration,
        "format_names": [
            name for name in str(format_info.get("format_name") or "").split(",") if name
        ],
        "bit_rate": int(_probe_number(format_info.get("bit_rate"), integer=True)),
        "size_bytes": int(_probe_number(format_info.get("size"), integer=True)),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_stream": video_report,
        "audio_stream": audio_report,
        "streams": [
            {
                "index": int(_probe_number(stream.get("index"), integer=True)),
                "codec_type": str(stream.get("codec_type") or ""),
                "codec": str(stream.get("codec_name") or ""),
            }
            for stream in media_streams
        ],
    }


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
    duration_seconds: float | None = None,
    start_seconds: float = 0.0,
    source_duration_seconds: float | None = None,
    output_duration_seconds: float | None = None,
    speed: float = 1.0,
    has_audio: bool = False,
    include_source_audio: bool = False,
) -> None:
    source_span = max(
        0.1,
        float(source_duration_seconds if source_duration_seconds is not None else duration_seconds or 0.1),
    )
    output_span = max(
        0.1,
        float(output_duration_seconds if output_duration_seconds is not None else source_span / speed),
    )
    speed_filter = f",setpts=PTS/{speed:.6f}" if abs(speed - 1.0) > 0.000001 else ""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1{speed_filter},fps={fps},format=yuv420p"
    )
    base = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{source_span:.3f}",
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
            "-filter:a",
            _atempo_filter(speed),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{output_span:.3f}",
            "-shortest",
            output_path,
        ]
    else:
        silent_duration = output_span
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
            "-t",
            f"{output_span:.3f}",
            "-shortest",
            output_path,
        ]
    await _run_process(
        args,
        timeout_seconds=max(
            120.0,
            max(source_span, output_span) * 8.0 + 60.0,
        ),
    )


def _resolve_clip_window(
    *,
    source_duration: float,
    start_seconds: float,
    end_seconds: float | None,
    speed: float,
) -> tuple[float, float, float]:
    if source_duration <= 0:
        raise ValueError("Video source has no measurable duration")
    if not 0.25 <= speed <= 8.0:
        raise ValueError("Clip speed must be between 0.25 and 8")
    start = max(0.0, float(start_seconds or 0.0))
    if start >= source_duration:
        raise ValueError("Clip starts after the source ends")
    end = source_duration if end_seconds is None else min(float(end_seconds), source_duration)
    if end <= start:
        raise ValueError("Clip end must be greater than its start")
    return start, end, (end - start) / speed


def _atempo_filter(speed: float) -> str:
    remaining = float(speed)
    factors: list[float] = []
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


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


def _workspace_media_reference(
    value: str | None,
    *,
    entity_id: str,
    workspace_base_dir: str = "",
) -> str:
    """Resolve a media input reference inside its Workspace storage root."""
    from packages.core.services.generated_media_naming import scope_workspace_artifact_path

    rel_path = _rel_path_from_reference(value, entity_id) or str(value or "").strip()
    if not rel_path:
        return ""
    if workspace_base_dir:
        rel_path = scope_workspace_artifact_path(rel_path, workspace_base_dir)
    return _normalize_user_path(rel_path)


async def _workspace_media_base_dir(
    *,
    entity_id: str,
    workspace_id: str | None,
    task_id: str | None = None,
) -> str:
    if not str(workspace_id or "").strip():
        return ""
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
    )

    return await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )


def _normalize_user_path(path: str) -> str:
    from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

    rel_path = normalize_rel_path(unquote(path or ""))
    if not rel_path or not is_user_visible_path(rel_path):
        raise ValueError(f"Unsafe or hidden Knowledge path: {path}")
    return rel_path


async def _resolve_entity_file(entity_root: str, rel_path: str) -> str:
    rel = _normalize_user_path(rel_path)
    root = os.path.realpath(entity_root)
    full_path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full_path]) != root:
        raise ValueError(f"Path escapes entity root: {rel_path}")
    if not os.path.isfile(full_path):
        raise ValueError(f"Media file not found or access denied: {rel}")
    access = _CURRENT_MEDIA_FILE_ACCESS.get()
    if access is not None and access.enforce_acl:
        blocked = await runtime_guard_file_read_access(
            entity_id=access.entity_id,
            user_id=access.user_id,
            workspace_id=access.workspace_id,
            runtime_envelope=access.runtime_envelope,
            tool_name=access.tool_name,
            paths=[rel],
        )
        if blocked:
            raise ValueError(f"Media file not found or access denied: {rel}")
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
    return ffmpeg_audio_codec_args(fmt)


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


def _json_blocked(message: str, *, code: str, **details: Any) -> str:
    return _json({"status": "blocked", "code": code, "error": message, **details})


def get_tools():
    return [
        (WAIT_MEDIA_JOBS_SCHEMA, _wait_media_jobs_handler),
        (MERGE_VIDEOS_SCHEMA, _merge_videos_handler),
        (ALIGN_SUBTITLES_SCHEMA, _align_subtitles_handler),
        (BUILD_NARRATION_TIMELINE_SCHEMA, _build_narration_timeline_handler),
        (PREPARE_NARRATION_TIMELINE_SCHEMA, _prepare_narration_timeline_handler),
        (INSPECT_NARRATION_RECOVERY_SCHEMA, _inspect_narration_recovery_handler),
        (NORMALIZE_AUDIO_LOUDNESS_SCHEMA, _normalize_audio_loudness_handler),
        (COMPOSE_VIDEO_TIMELINE_SCHEMA, _compose_video_timeline_handler),
        (PROBE_MEDIA_SCHEMA, _probe_media_handler),
        (VERIFY_STICKMAN_FINAL_MEDIA_SCHEMA, _verify_stickman_final_media_handler),
        (RENDER_FRAME_SAMPLES_SCHEMA, _render_frame_samples_handler),
        (ANALYZE_AUDIO_SCHEMA, _analyze_audio_handler),
        (VALIDATE_SUBTITLES_SCHEMA, _validate_subtitles_handler),
        (STILL_TO_VIDEO_SCHEMA, _still_to_video_handler),
    ]
