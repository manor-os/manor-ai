"""Frozen inputs and actual-file completion gates for non-code studios.

Production remains on existing governed file/media tools. Neither a model's
completion claim nor a filesystem-only file is a finished delivery.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from jsonschema import validate

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_handler
from packages.core.ai.tools import media_tools as media
from packages.core.ai.runtime.generated_files import (
    runtime_commit_file_projection as _commit_file_projection,
    runtime_read_supported_text as _read_supported_text,
)
from packages.core.ai.runtime.media import (
    runtime_document_ref_paths as _document_ref_paths,
    runtime_recording_output_scope as _scope,
    runtime_recording_source_path as _source_path,
    runtime_recording_tool_schema as _schema,
)
from packages.core.services.generated_artifact_service import mark_production_results

MODES =["presentation", "podcast", "carousel", "dubbing", "handbook", "video"]
# The one rule for a brief's length; the studio Flows' forms and run schemas use it
# too, so a brief the form accepts is never refused when the run freezes it.
# Two characters: a few CJK characters name a whole subject.
BRIEF_MIN_LENGTH, BRIEF_MAX_LENGTH = 2, 12000
PREPARE_SCHEMA = _schema(
    "prepare_creative_studio", "Freeze a creative production brief and hashes of up to five "
    "uploaded Knowledge sources in a new Workspace run folder. Readable PDF, DOCX, PPTX and "
    "text are supplied as quoted reference material; images/media remain file references. "
    "No network downloads, model calls or publishing.",
    {"brief": {"type": "string", "minLength": BRIEF_MIN_LENGTH, "maxLength": BRIEF_MAX_LENGTH},
     "sources": {"type": "array", "maxItems": 5, "description": "Optional picked Knowledge Documents.",
                 "items": {"type": "object", "required": ["document_id"],
                           "properties": {"document_id": {"type": "string", "minLength": 1}}}},
     "source_paths": {"type": "string", "maxLength": 6000, "description": "Optional Knowledge paths, one per line."},
     "mode": {"type": "string", "enum": MODES},
     "output_directory": {"type": "string", "minLength": 1}},
    ["brief", "mode", "output_directory"],
)
VERIFY_SCHEMA = _schema(
    "verify_creative_studio", "Verify actual creative outputs in the frozen run folder: "
    "unchanged inputs, current Knowledge Documents, readable Office/PDF/images or decodable "
    "audio/video, required formats and page counts. Save actual PDF/slide previews and a "
    "technical receipt. Does not judge facts, image text, translation accuracy, or listening quality.",
    {"snapshot_path": {"type": "string", "minLength": 1},
     "snapshot_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
     "files": {"type": "array", "minItems": 1, "maxItems": 24,
               "items": {"type": "string", "minLength": 1}},
     "output_directory": {"type": "string", "minLength": 1}},
    ["snapshot_path", "snapshot_sha256", "files", "output_directory"],
)
PUBLICATION_LAYOUTS = ["cover", "image", "statement", "stats", "closing"]
PUBLICATION_SCHEMA = _schema(
    "compose_creative_publication", "Compose an illustrated editable PPTX, matching PDF and speaker/source notes "
    "from structured page content, using the existing native Office engine. No code or coordinate parameters. "
    "Choose 3–16 short pages; generate or supply a real Knowledge illustration first. Each page picks a layout: "
    "cover (full-bleed picture, title and one subtitle line), image (large picture beside the text), statement "
    "(one big message), stats (2–3 big numbers with labels, optional one-line footnote) or closing (the ask, "
    "picture beside it). A page may use its own picture; otherwise the main illustration is used. All files "
    "are new, under the requested production directory. Returns exact paths; final review is still required.",
    {"output_directory": {"type": "string", "minLength": 1},
     "label": {"type": "string", "minLength": 1, "maxLength": 50},
     "theme": {"type": "string", "enum": ["terracotta", "sage", "mustard"]},
     "illustration_path": {"type": "string", "minLength": 1},
     "source_notes": {"type": "string", "minLength": 1, "maxLength": 4000},
     "pages": {"type": "array", "minItems": 3, "maxItems": 16, "items": {
         "type": "object", "additionalProperties": False, "required": ["title", "bullets", "notes"],
         "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 54},
             "layout": {"type": "string", "enum": PUBLICATION_LAYOUTS},
             "image": {"type": "string", "minLength": 1, "description": "This page's own picture."},
             "bullets": {"type": "array", "minItems": 0, "maxItems": 4,
                         "items": {"type": "string", "minLength": 1, "maxLength": 90}},
             "stats": {"type": "array", "minItems": 2, "maxItems": 3, "items": {
                 "type": "object", "additionalProperties": False, "required": ["value", "label"],
                 "properties": {"value": {"type": "string", "minLength": 1, "maxLength": 10},
                                "label": {"type": "string", "minLength": 1, "maxLength": 40}}}},
             "notes": {"type": "string", "maxLength": 2000}}}}},
    ["output_directory", "label", "theme", "illustration_path", "source_notes", "pages"],
)
# The built-in pptx skill's designed deck engine: complete visual styles and
# slot-fitted layouts, run here so a deck is rendered the same way every time.
DECK_ENGINE_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "scripts"
DECK_STYLE_CATALOG = DECK_ENGINE_SCRIPTS.parent / "templates" / "decks" / "styles.json"


def deck_styles() -> dict[str, dict[str, Any]]:
    """Each designed style: its name, what it suits, its picture art direction and cover kind."""
    styles = json.loads(DECK_STYLE_CATALOG.read_text(encoding="utf-8"))["styles"]
    return {key: {field: style.get(field) for field in ("name", "use_for", "art_direction", "cover")}
            for key, style in styles.items()}


DECK_STYLES = list(deck_styles())
DECK_SCHEMA = _schema(
    "compose_creative_deck", "Compose an editable PPTX in a designed style, its matching PDF and speaker/source "
    "notes, from a structured deck description. The server lays every slide out on the style's own templates "
    "and runs the deck quality gate; no code, coordinates or colors. Write 3–16 slides; slide 1 is cover. "
    "Each slide has a layout, its fields and notes (speaker notes). Image fields are Knowledge paths "
    "returned by generate_file or supplied by the person (PNG/JPEG/WebP, at most eight distinct). Layouts and "
    "their fields: cover(title; subtitle, kicker, meta, image) · section(title; subtitle, number, image) · "
    "statement(title ≤40 chars; subtitle, image) · agenda(title, items 3–7 strings) · bullets(title, points 2–6 "
    "strings or {title,text}) · cards(title, items 2–4 {title,text?,label?,icon?}) · numbered(title, items 2–4 "
    "{title,text?}) · icon_grid(title, items 3–6 {icon,title,text?}) · process(title, steps 3–5 {title,text?,icon?}) · "
    "timeline(title, events 3–6 {date,title,text?}) · stats(title, stats 2–4 {value ≤8 chars,label,note?}) · "
    "comparison(title, columns 2–3 {heading,points 1–5}; highlight) · matrix(title, quadrants 4 {title,text?}; axes "
    "{x,y}) · quote(title, quote ≤120 chars; attribution) · chart(title, chart {type,categories,series "
    "[{name,values}]}; takeaways) · table(title, columns 2–6, rows 1–8) · progress(title, items 2–6 {label,value 0–100}) · "
    "funnel(title, stages 3–5 {title,value?,text?}) · pyramid(title, levels 3–5 {title,text?}) · cycle(title, steps "
    "3–6 {title,text?}) · venn(title, sets 2–3 {title,text?}; center) · team(title, people 2–4 {name,role,image?}) · "
    "gallery(title, images 3–5 {image,caption?}) · image_text(title, image, points 1–5) · split_image(title, image; "
    "points) · image_cards(title, items 2–4 {image,title,text?}) · feature(title, image; text ≤160, points 1–3, tags) · "
    "closing(title; subtitle, points 1–4, contact). Titles state the takeaway; wrap one key word group in {braces} "
    "for emphasis. Never invent numbers: use stats/chart/progress only with real data. A deck the engine cannot lay "
    "out returns every problem with its JSON path; fix them all and call again. Returns exact file paths; final "
    "review is still required.",
    {"output_directory": {"type": "string", "minLength": 1},
     "style": {"type": "string", "enum": DECK_STYLES},
     "deck": {"type": "object", "required": ["title", "slides"], "properties": {
         "title": {"type": "string", "minLength": 1, "maxLength": 120},
         "brand": {"type": "string", "maxLength": 60},
         "footer": {"type": "string", "maxLength": 60},
         "slides": {"type": "array", "minItems": 3, "maxItems": 16, "items": {
             "type": "object", "required": ["layout"],
             "properties": {"layout": {"type": "string", "minLength": 1},
                            "notes": {"type": "string", "maxLength": 2000}}}}}},
     "source_notes": {"type": "string", "minLength": 1, "maxLength": 4000}},
    ["output_directory", "style", "deck", "source_notes"],
)
DUB_SCHEMA = _schema(
    "compose_creative_dub", "Compose a new 10–180 second translated MP4 from an uploaded video and actual "
    "target-language narration. Preserve picture timing, replace all original audio, normalize speech, "
    "and burn two-line subtitles. Subtitle timing is proportional/estimated, not word aligned. "
    "No AI generation, voice cloning, lip sync or publishing. Returns actual MP4/SRT/notes paths.",
    {"output_directory": {"type": "string", "minLength": 1},
     "source_path": {"type": "string", "minLength": 1},
     "narration_path": {"type": "string", "minLength": 1},
     "translated_text": {"type": "string", "minLength": 10, "maxLength": 8000},
     "translation_notes": {"type": "string", "minLength": 10, "maxLength": 16000}},
    ["output_directory", "source_path", "narration_path", "translated_text", "translation_notes"],
)


def _estimated_dub_srt(text, duration, *, subtitle_format="srt", width=1920, height=1080, offset=0.0):
    portrait = height > width  # A phone-sized vertical frame needs larger captions set higher.
    # One cue per sentence, timed by its share of the text, so a cue never ends
    # one sentence and starts the next; long sentences still split to two lines.
    sentences = [part.strip() for part in re.split(r"(?<=[.!?…。！？])\s*", text) if part.strip()] or [text]
    total = sum(len(sentence) for sentence in sentences)
    cues, start = [], 0.0
    for index, sentence in enumerate(sentences, 1):
        end = duration if index == len(sentences) else start + duration * len(sentence) / total
        # ``offset`` moves the cues past an opening clip played before the narration.
        cues.append(media.SubtitleCue(index=index, start=start + offset, end=end + offset, text=sentence, cue_type="narration",
                                      estimated=True, timing_source="proportional-text"))
        start = end
    return media._render_subtitles(cues, subtitle_format=subtitle_format, max_chars_per_line=24,
        canvas_width=width, canvas_height=height,
        style={"max_lines": 2,
               "font_size": round(width * 0.056) if portrait else max(16, min(42, round(min(width, height) * 0.04))),
               "margin_v": round(height * 0.14) if portrait else max(16, round(height * 0.05)),
               "outline": 3 if portrait else 2, "alignment": 2})


def _dub_caption_duration(video_duration, narration_duration):
    # Encoders can end a few samples before the requested video duration.
    # Floor to SRT millisecond precision; do not relax the timing validator.
    return int(min(video_duration, narration_duration) * 1000) / 1000


@media._with_media_file_access
async def _compose_creative_dub_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    output_directory: str = "", source_path: str = "", narration_path: str = "",
    translated_text: str = "", translation_notes: str = "", **kwargs: Any,
) -> str:
    try:
        validate(dict(output_directory=output_directory, source_path=source_path, narration_path=narration_path,
                      translated_text=translated_text, translation_notes=translation_notes),
                 DUB_SCHEMA["function"]["parameters"])
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        call_context = {**ctx, "runtime_envelope": runtime.runtime_envelope}
        source, source_abs = await _source_path(source_path, ctx)
        narration, narration_abs = await _source_path(narration_path, ctx)
        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not shutil.which("ffmpeg"):
            raise ValueError("FFmpeg and ffprobe are required")
        if any(Path(path).stat().st_size > 100 * 1024 * 1024 for path in (source_abs, narration_abs)):
            raise ValueError("Each source must be at most 100 MiB")
        video = await media._probe_media_report(ffprobe, source_abs)
        audio = await media._probe_media_report(ffprobe, narration_abs)
        duration = video.get("duration_seconds", 0)
        if not video.get("has_video") or not 10 <= duration <= 180 or not audio.get("has_audio"):
            raise ValueError("Provide one 10–180 second video and actual target-language narration")
        if not 0.75 <= audio.get("duration_seconds", 0) / duration <= 1.5:
            raise ValueError("Narration duration must be 0.75–1.5 times the video duration; revise the translation")
        _, directory = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        normalized = json.loads(await media._normalize_audio_loudness_handler(**call_context,
            input_path=narration, output_name=directory + "/narration.wav", output_format="wav",
            target_lufs=-16, true_peak=-1.5, target_duration_seconds=duration))
        if normalized.get("status") != "completed" or not normalized.get("fs_path"):
            raise ValueError("Narration normalization failed: " + json.dumps(normalized))
        _, normalized_abs = await _source_path(normalized["fs_path"], ctx)
        normalized_report = await media._probe_media_report(ffprobe, normalized_abs)
        caption_duration = _dub_caption_duration(duration, normalized_report.get("duration_seconds", 0))
        if caption_duration <= 0:
            raise ValueError("Normalized narration has no measurable duration")
        subtitles = await _save(ctx, runtime, directory + "/subtitles.srt",
                                _estimated_dub_srt(translated_text, caption_duration).encode("utf-8"))
        checked = json.loads(await media._validate_subtitles_handler(**call_context,
            subtitle_path=subtitles["fs_path"], media_path=normalized["fs_path"], max_lines=2))
        if checked.get("verdict") != "pass":
            raise ValueError("Subtitle validation failed: " + json.dumps(checked))
        dimensions = video.get("video_stream") or {}
        width, height = dimensions.get("width", 0), dimensions.get("height", 0)
        if not width or not height:
            raise ValueError("Source video dimensions are unavailable")
        # SRT has no canvas size: libass's default 384×288 script space can
        # multiply pixel-sized fonts. Burn an ASS sidecar with explicit size.
        burn_subtitles = await _save(ctx, runtime, directory + "/subtitles.ass",
            _estimated_dub_srt(translated_text, caption_duration, subtitle_format="ass",
                               width=width, height=height).encode("utf-8"))
        timeline = await _save(ctx, runtime, directory + "/timeline.json", _json_bytes({
            "duration": duration, "delivery": {"clean_picture_master": source},
            "audio_tracks": [{"id": "dub", "type": "narration", "path": normalized["fs_path"],
                              "start": 0, "end": duration, "volume_db": 0}],
            "subtitles": {"srt_path": subtitles["fs_path"]}}))
        rendered = json.loads(await media._compose_video_timeline_handler(**call_context,
            clean_video_path=source, timeline_path=timeline["fs_path"], subtitle_path=burn_subtitles["fs_path"],
            output_name=directory + "/dubbed.mp4", include_audio=True, require_audio=True,
            include_source_audio=False, burn_subtitles=True))
        if rendered.get("status") != "completed" or not rendered.get("fs_path"):
            raise ValueError("Video composition failed: " + json.dumps(rendered))
        notes = await _save(ctx, runtime, directory + "/translation.md",
            (translation_notes + "\n\n## Timing and audio limits\n\n"
             "Subtitles use proportional text timing, not measured word alignment. Review the whole recording. "
             "The original picture timing is retained; all original audio is replaced by normalized narration. "
             "No voice cloning, lip sync, music separation or publishing.\n").encode("utf-8"))
        return json.dumps({"status": "completed", "files": [rendered["fs_path"], subtitles["fs_path"], notes["fs_path"]],
                           "duration_seconds": duration, "requires_playback_review": True})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


VIDEO_CANVAS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
VIDEO_SCHEMA = _schema(
    "compose_creative_video", "Compose one narrated MP4 episode from 2–12 scene pictures and actual narration. "
    "Each scene shows for its share of the spoken text with a slow push-in; narration is loudness-normalized and "
    "two-line subtitles are burned in. Timing is proportional to the text, not word aligned. An intro_path clip (a "
    "series intro) plays first. No AI generation or publishing. Returns actual MP4 and SRT paths.",
    {"output_directory": {"type": "string", "minLength": 1},
     "aspect_ratio": {"type": "string", "enum": list(VIDEO_CANVAS)},
     "narration_path": {"type": "string", "minLength": 1},
     "scenes": {"type": "array", "minItems": 2, "maxItems": 12, "items": {
         "type": "object", "additionalProperties": False, "required": ["image_path", "text"],
         "properties": {"image_path": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1, "maxLength": 1200,
                                 "description": "The exact narration spoken over this picture."}}}},
     "intro_path": {"type": "string", "description": "An opening clip played before the episode, e.g. the series intro."}},
    ["output_directory", "aspect_ratio", "narration_path", "scenes"],
)
VIDEO_FPS = 30


def _scene_seconds(texts: list[str], duration: float) -> list[float]:
    """Each picture stays for its share of the spoken text; the last one ends with the narration."""
    weights = [max(1, len(text.strip())) for text in texts]
    seconds = [duration * weight / sum(weights) for weight in weights]
    if min(seconds) < 1.0:
        raise ValueError("A scene would show for under a second; use fewer scenes or longer narration per scene")
    return seconds


def _picture_master_args(ffmpeg: str, pictures: list[str], seconds: list[float], width: int, height: int,
                         output: str) -> list[str]:
    """One silent H.264 picture track: each still scaled to fill the frame with a slow push-in."""
    args = [ffmpeg, "-y", "-v", "error"]
    for picture in pictures:
        args += ["-i", picture]  # One frame each: zoompan makes the scene's frames from it.
    filters = []
    for index, scene_seconds in enumerate(seconds):
        frames = max(1, round(scene_seconds * VIDEO_FPS))
        filters.append(
            f"[{index}:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='min(1+0.06*on/{frames},1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={width}x{height}:fps={VIDEO_FPS},setsar=1,format=yuv420p[v{index}]")
    filters.append("".join(f"[v{index}]" for index in range(len(seconds))) + f"concat=n={len(seconds)}:v=1:a=0[out]")
    return args + ["-filter_complex", ";".join(filters), "-map", "[out]", "-r", str(VIDEO_FPS),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", output]


@media._with_media_file_access
async def _compose_creative_video_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    output_directory: str = "", aspect_ratio: str = "9:16", narration_path: str = "",
    scenes: list[dict] | None = None, intro_path: str = "", **kwargs: Any,
) -> str:
    try:
        validate(dict(output_directory=output_directory, aspect_ratio=aspect_ratio, narration_path=narration_path,
                      scenes=scenes, **({"intro_path": intro_path} if intro_path else {})),
                 VIDEO_SCHEMA["function"]["parameters"])
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        call_context = {**ctx, "runtime_envelope": runtime.runtime_envelope}
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ValueError("FFmpeg and ffprobe are required")
        narration, narration_abs = await _source_path(narration_path, ctx)
        pictures = []
        for scene in scenes:
            _, picture_abs = await _source_path(scene["image_path"], ctx)
            if Path(picture_abs).suffix.lower() not in media.IMAGE_EXTENSIONS:
                raise ValueError("Each scene needs an actual PNG, JPEG or WebP picture: " + scene["image_path"])
            if Path(picture_abs).stat().st_size > 30 * 1024 * 1024:
                raise ValueError("Each scene picture must be at most 30 MiB")
            pictures.append(picture_abs)
        intro, intro_seconds = None, 0.0
        if str(intro_path or "").strip():
            intro, intro_abs = await _source_path(intro_path, ctx)
            intro_seconds = float((await media._probe_media_report(ffprobe, intro_abs)).get("duration_seconds") or 0)
            if not 0.5 <= intro_seconds <= 10:
                raise ValueError("The intro must be an actual video of 0.5 to 10 seconds")
        audio = await media._probe_media_report(ffprobe, narration_abs)
        if not audio.get("has_audio") or not 10 <= audio.get("duration_seconds", 0) <= 600:
            raise ValueError("Provide 10 seconds to 10 minutes of actual narration")
        _, directory = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        normalized = json.loads(await media._normalize_audio_loudness_handler(**call_context,
            input_path=narration, output_name=directory + "/narration.wav", output_format="wav",
            target_lufs=-16, true_peak=-1.5))
        if normalized.get("status") != "completed" or not normalized.get("fs_path"):
            raise ValueError("Narration normalization failed: " + json.dumps(normalized))
        _, normalized_abs = await _source_path(normalized["fs_path"], ctx)
        duration = _dub_caption_duration(
            (await media._probe_media_report(ffprobe, normalized_abs)).get("duration_seconds", 0), 10 ** 6)
        if duration <= 0:
            raise ValueError("Normalized narration has no measurable duration")
        texts = [scene["text"].strip() for scene in scenes]
        seconds = _scene_seconds(texts, duration)
        width, height = VIDEO_CANVAS[aspect_ratio]
        with tempfile.TemporaryDirectory(prefix="manor-creative-video-") as working:
            rendered_picture = str(Path(working) / "picture.mp4")
            await media._run_process(_picture_master_args(ffmpeg, pictures, seconds, width, height, rendered_picture),
                                     timeout_seconds=900)
            if not Path(rendered_picture).is_file() or not Path(rendered_picture).stat().st_size:
                raise ValueError("The picture track could not be rendered")
            picture = await _save(ctx, runtime, directory + "/picture.mp4", Path(rendered_picture).read_bytes())
        spoken = " ".join(texts)
        burn_subtitles = await _save(ctx, runtime, directory + "/subtitles.ass",
            _estimated_dub_srt(spoken, duration, subtitle_format="ass", width=width, height=height).encode("utf-8"))
        timeline = await _save(ctx, runtime, directory + "/timeline.json", _json_bytes({
            "duration": duration, "delivery": {"clean_picture_master": picture["fs_path"]},
            "audio_tracks": [{"id": "narration", "type": "narration", "path": normalized["fs_path"],
                              "start": 0, "end": duration, "volume_db": 0}],
            "subtitles": {"subtitle_path": burn_subtitles["fs_path"]}}))
        # With an intro, the narrated part is composed first and the intro plays before it.
        rendered = json.loads(await media._compose_video_timeline_handler(**call_context,
            clean_video_path=picture["fs_path"], timeline_path=timeline["fs_path"],
            subtitle_path=burn_subtitles["fs_path"],
            output_name=directory + ("/episode-body.mp4" if intro else "/episode.mp4"),
            include_audio=True, require_audio=True, include_source_audio=False, burn_subtitles=True))
        if rendered.get("status") != "completed" or not rendered.get("fs_path"):
            raise ValueError("Video composition failed: " + json.dumps(rendered))
        if intro:
            rendered = json.loads(await media._merge_videos_handler(**call_context,
                paths=[intro, rendered["fs_path"]], output_name=directory + "/episode.mp4", resolution="1080p",
                aspect_ratio=aspect_ratio, include_source_audio=True))
            if rendered.get("status") != "completed" or not rendered.get("fs_path"):
                raise ValueError("Adding the intro failed: " + json.dumps(rendered))
        subtitles = await _save(ctx, runtime, directory + "/subtitles.srt", _estimated_dub_srt(
            spoken, duration, width=width, height=height, offset=intro_seconds).encode("utf-8"))
        checked = json.loads(await media._validate_subtitles_handler(**call_context,
            subtitle_path=subtitles["fs_path"], media_path=rendered["fs_path"], max_lines=2))
        if checked.get("verdict") != "pass":
            raise ValueError("Subtitle validation failed: " + json.dumps(checked))
        return json.dumps({"status": "completed", "files": [rendered["fs_path"], subtitles["fs_path"]],
                           "duration_seconds": round(duration + intro_seconds, 3), "scenes": len(scenes),
                           "intro_seconds": round(intro_seconds, 3), "requires_playback_review": True})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def _publication_text_lines(text: str, *, size: int, width: float) -> tuple[int, int]:
    """Conservatively budget native text wrapping without changing its content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n").replace("\u2029", "\n")
    paragraphs = text.split("\n")
    lines = 0

    def advance(char):
        if unicodedata.combining(char):
            return 0
        if char == " ":
            return size * 0.35
        if char.isascii():
            if char in "MWmw@%&":
                return size
            return size * (0.8 if char.isupper() else 0.6)
        return size * 1.1

    for paragraph in paragraphs:
        tokens, word = [], ""
        for char in paragraph:
            if unicodedata.category(char).startswith("C"):
                raise ValueError("Replace tabs and control characters with ordinary spaces")
            if char.isalnum() and unicodedata.east_asian_width(char) not in {"W", "F"}:
                word += char
            else:
                if word:
                    tokens.append(word)
                    word = ""
                tokens.append(char)
        if word:
            tokens.append(word)
        used = 0.0
        lines += 1
        for token in tokens:
            token_width = sum(advance(char) for char in token)
            if used and used + token_width > width:
                lines += 1
                used = 0
            if token_width > width:
                for char in token:
                    char_width = advance(char)
                    if used and used + char_width > width:
                        lines += 1
                        used = 0
                    used += char_width
            else:
                used += token_width
    return lines, len(paragraphs)


PICTURE_LAYOUTS = {"cover", "image", "closing"}
PUBLICATION_THEMES = {"terracotta": ("F7F2EA", "AB513A"), "sage": ("F2F5EF", "426549"),
                      "mustard": ("F8F5ED", "A87918")}
INK, BODY, ON_PHOTO = "1C1917", "44403C", "FFFFFF"


def _page_pictures(pages):
    """Distinct page images that will be drawn; stats and statement pages show none,
    so an image named there is never opened (or able to fail the deck)."""
    return list(dict.fromkeys(
        page["image"] for index, page in enumerate(pages)
        if page.get("image") and _page_layout(page, index, len(pages)) in PICTURE_LAYOUTS))


def _page_layout(page, index, count):
    """The page's chosen layout. Calls without one (older installed Workspaces) get picture
    pages, which accept any 1–4 lines, so they never fail on a stricter layout."""
    del index, count
    return page.get("layout") or "image"


def _publication_page_plan(page, *, index, count, label, accent):
    """Pictures, shapes and text boxes of one 960×540 page, in drawing order.

    Each item is ("picture", x, y, w, h), ("shape", preset, x, y, w, h, format) or
    ("text", field, text, x, y, w, h, size, color, bold).
    """
    layout = _page_layout(page, index, count)
    bullets, number = page.get("bullets") or [], f"{index + 1:02d} / {count:02d}"
    if layout == "cover":
        if len(bullets) > 1:
            raise ValueError(f"layout-density: Page {index + 1} cover takes one subtitle line; "
                             "move the other lines to a following page")
        # A full-height shade has no visible top edge, even where a renderer flattens gradient alpha.
        band = {"line_color": None, "shadow": None, "gradient_fill": {"angle": 90, "stops": [
            {"position": 0, "color": INK, "opacity": 0}, {"position": 0.45, "color": INK, "opacity": 0.15},
            {"position": 1, "color": INK, "opacity": 0.8}]}}
        return [("picture", 0, 0, 960, 540), ("shape", "rect", 0, 0, 960, 540, band),
                ("text", "label", label.upper(), 56, 320, 848, 26, 12, ON_PHOTO, True),
                ("text", "title", page["title"], 56, 348, 848, 108, 40, ON_PHOTO, True),
                *([("text", "subtitle", bullets[0], 56, 458, 848, 52, 18, "EDE9E4", False)] if bullets else [])]
    if layout == "stats":
        stats = page.get("stats") or []
        if not 2 <= len(stats) <= 3 or len(bullets) > 1:
            raise ValueError(f"layout-density: Page {index + 1} stats needs 2–3 stats and at most one footnote line")
        gap = 24
        width = (848 - gap * (len(stats) - 1)) / len(stats)
        card = {"fill_color": "FFFFFF", "line_color": None, "shadow": None, "corner_radius": 16}
        plan = [("text", "label", label.upper(), 56, 48, 848, 26, 12, accent, True),
                ("text", "title", page["title"], 56, 80, 848, 96, 32, INK, True)]
        for column, stat in enumerate(stats):
            x = 56 + column * (width + gap)
            plan += [("shape", "roundRect", x, 196, width, 210, card),
                     ("text", "stat value", stat["value"], x + 20, 216, width - 40, 84, 56, accent, True),
                     ("text", "stat label", stat["label"], x + 20, 306, width - 40, 84, 16, BODY, False)]
        if bullets:
            plan.append(("text", "footnote", bullets[0], 56, 424, 848, 48, 14, BODY, False))
        return plan + [("text", "number", number, 56, 486, 848, 25, 11, accent, False)]
    if not bullets:
        raise ValueError(f"layout-density: Page {index + 1} {layout} needs 1–4 lines of text")
    if layout == "statement":
        rule = {"fill_color": accent, "line_color": None}
        return [("text", "label", label.upper(), 56, 48, 848, 26, 12, accent, True),
                ("shape", "rect", 56, 150, 56, 4, rule),
                ("text", "title", page["title"], 56, 170, 790, 150, 44, INK, True),
                ("text", "body", "\n".join(bullets), 56, 326, 790, 150, 20, BODY, False),
                ("text", "number", number, 56, 486, 848, 25, 11, accent, False)]
    # image: picture on the left; closing: the ask, picture on the right.
    picture_x, text_x = (0, 488) if layout == "image" else (520, 56)
    return [("picture", picture_x, 0, 440, 540),
            ("text", "label", label.upper(), text_x, 48, 416, 26, 12, accent, True),
            ("text", "title", page["title"], text_x, 80, 416, 132, 34 if layout == "closing" else 30, INK, True),
            ("text", "body", "\n".join(bullets), text_x, 224, 416, 240, 18, BODY, False),
            ("text", "number", number, text_x, 486, 416, 25, 11, accent, False)]


def _validate_publication_layout(pages, *, label):
    # python-pptx native text boxes retain 7.2pt side and 3.6pt vertical
    # margins. Budget the existing 1.1 line spacing and inter-paragraph 12pt
    # gap; no trailing paragraph gap is drawn inside the final text line.
    plans = []
    for index, page in enumerate(pages):
        plan = _publication_page_plan(page, index=index, count=len(pages), label=label, accent="000000")
        for item in plan:
            if item[0] != "text":
                continue
            _, field, text, _, _, width, height, size, _, _ = item
            try:
                lines, paragraphs = _publication_text_lines(text, size=size, width=width - 14.4)
            except ValueError as exc:
                raise ValueError(f"layout-density: Page {index + 1} {field}: {exc}") from exc
            if lines * size * 1.1 + (paragraphs - 1) * 12 > height - 7.2:
                raise ValueError(
                    f"layout-density: Page {index + 1} {field} is too dense for its "
                    f"{_page_layout(page, index, len(pages))} layout ({lines} estimated lines). Shorten this "
                    "text or split the content across more pages; no text was truncated or silently resized."
                )
        plans.append(plan)
    return plans


def _publication_operations(pages, *, label, theme, illustration, pictures=None):
    """Editorial page layouts, expressed as existing native Office ops.

    ``illustration`` is the main picture source; ``pictures`` maps a page's own
    ``image`` reference to its source.
    """
    _validate_publication_layout(pages, label=label)
    background, accent = PUBLICATION_THEMES[theme]
    operations = [{"op": "page.setup", "format": {"width": 960, "height": 540}}]
    for index, page in enumerate(pages):
        slide, shape_id = index + 1, 2
        operations.extend([{"op": "slide.insert", "index": index, "layout_index": 6},
                           {"op": "slide.format", "slide": slide, "format": {"background_color": background}}])
        source = (pictures or {}).get(page.get("image") or "", illustration)
        for item in _publication_page_plan(page, index=index, count=len(pages), label=label, accent=accent):
            if item[0] == "picture":
                _, x, y, width, height = item
                operations.append({"op": "picture.insert", "slide": slide, "source": source,
                                   "transform": {"x": x, "y": y, "width": width, "height": height},
                                   "fit": "cover", "format": {"alt_text": page["title"]}})
            elif item[0] == "shape":
                _, preset, x, y, width, height, shape_format = item
                operations.append({"op": "shape.insert", "slide": slide, "preset": preset,
                                   "transform": {"x": x, "y": y, "width": width, "height": height},
                                   "format": shape_format})
            else:
                _, _, text, x, y, width, height, size, color, bold = item
                operations.append({"op": "textbox.insert", "slide": slide, "text": text,
                    "transform": {"x": x, "y": y, "width": width, "height": height}})
                operations.append({"op": "shape.format", "slide": slide, "shape_id": shape_id,
                                   "format": {"word_wrap": True}})
                for paragraph in range(len(text.split("\n"))):
                    operations.append({"op": "paragraph.format", "slide": slide, "shape_id": shape_id,
                        "index": paragraph, "format": {"font_size": size, "font_color": color,
                            "font_name": "Arial", "bold": bold, "space_after": 12, "line_spacing": 1.1}})
            shape_id += 1
    return operations


@media._with_media_file_access
async def _compose_creative_publication_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    output_directory: str = "", label: str = "", theme: str = "", illustration_path: str = "",
    source_notes: str = "", pages: list[dict] | None = None, **kwargs: Any,
) -> str:
    try:
        from packages.core.ai.runtime.generated_files import runtime_generate_document_file

        validate(dict(output_directory=output_directory, label=label, theme=theme,
                      illustration_path=illustration_path, source_notes=source_notes, pages=pages),
                 PUBLICATION_SCHEMA["function"]["parameters"])
        _validate_publication_layout(pages, label=label)
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)

        async def picture(reference):
            rel_image, absolute_image = await _source_path(reference, ctx)
            if Path(absolute_image).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("Provide a real PNG/JPEG/WebP illustration")
            if Path(absolute_image).stat().st_size > 20 * 1024 * 1024:
                raise ValueError("Illustration exceeds 20 MiB")
            await asyncio.to_thread(_inspect_file, absolute_image)
            return {"path": rel_image, "expected_sha256": media._sha256_file(absolute_image)}

        illustration = await picture(illustration_path)
        page_images = _page_pictures(pages)
        if len(page_images) > 6:
            raise ValueError("Use at most six distinct page pictures")
        pictures = {reference: await picture(reference) for reference in page_images}
        _, directory = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        call_context = {**ctx, "runtime_envelope": runtime.runtime_envelope, "storage_scope": "workspace"}
        deck_path, pdf_path = directory + "/publication.pptx", directory + "/publication.pdf"
        deck = json.loads(await runtime_generate_document_file(**call_context, name=deck_path, file_type="pptx",
            operations=_publication_operations(pages, label=label, theme=theme, illustration=illustration,
                                               pictures=pictures)))
        if deck.get("error"):
            raise ValueError("Native presentation creation failed: " + json.dumps(deck, ensure_ascii=False))
        _, absolute_deck = await _source_path(deck_path, ctx)
        pdf = json.loads(await runtime_generate_document_file(**call_context, name=pdf_path, file_type="pdf",
            source={"path": deck_path, "expected_sha256": media._sha256_file(absolute_deck)}))
        if pdf.get("error"):
            raise ValueError("Native PDF conversion failed: " + json.dumps(pdf, ensure_ascii=False))
        notes = "# " + label + "\n\n" + "\n\n".join(
            f"## Page {index + 1}: {page['title']}\n\n{page['notes']}" for index, page in enumerate(pages))
        notes += "\n\n## Sources and production notes\n\n" + source_notes + "\n"
        saved_notes = await _save(ctx, runtime, directory + "/notes.md", notes.encode("utf-8"))
        return json.dumps({"status": "completed", "files": [deck_path, pdf_path, saved_notes["fs_path"]],
                           "pages": len(pages), "layout": "editable-native-office", "requires_visual_review": True})
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


DECK_BUILD_TIMEOUT_SECONDS = 240
DECK_MAX_PICTURES = 8


def _build_styled_deck(deck_json: Path) -> dict[str, Any]:
    """Run the deck engine and its quality gate on one deck.json; its JSON summary.

    The engine is the pptx skill's own builder, run as a separate process so its
    modules stay its own; it writes ``exports/<project>.pptx`` beside deck.json.
    """
    import subprocess
    import sys

    built = subprocess.run(
        [sys.executable, str(DECK_ENGINE_SCRIPTS / "build_deck.py"), str(deck_json)],
        cwd=DECK_ENGINE_SCRIPTS, capture_output=True, text=True, timeout=DECK_BUILD_TIMEOUT_SECONDS,
    )
    try:
        return json.loads(built.stdout)
    except ValueError:
        raise ValueError("The deck engine did not finish: " + (built.stderr or built.stdout)[-1500:]) from None


@media._with_media_file_access
async def _compose_creative_deck_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    output_directory: str = "", style: str = "", deck: dict | None = None, source_notes: str = "",
    **kwargs: Any,
) -> str:
    try:
        validate(dict(output_directory=output_directory, style=style, deck=deck, source_notes=source_notes),
                 DECK_SCHEMA["function"]["parameters"])
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        _, directory = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        spec = json.loads(json.dumps(deck))
        # The style is the person's choice, passed on its own; a deck cannot restyle itself.
        spec["theme"] = style
        spec.pop("theme_overrides", None)
        with tempfile.TemporaryDirectory(prefix="manor-creative-deck-") as workdir:
            project = Path(workdir) / "deck"
            (project / "images").mkdir(parents=True)
            pictures: dict[str, str] = {}

            async def local_picture(reference: str) -> str:
                if reference in pictures:
                    return pictures[reference]
                if len(pictures) >= DECK_MAX_PICTURES:
                    raise ValueError(f"Use at most {DECK_MAX_PICTURES} distinct pictures")
                _, absolute = await _source_path(reference, ctx)
                suffix = Path(absolute).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ValueError("Pictures must be PNG, JPEG or WebP: " + reference)
                if Path(absolute).stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("Picture exceeds 20 MiB: " + reference)
                await asyncio.to_thread(_inspect_file, absolute)
                local = f"images/picture-{len(pictures) + 1:02d}{suffix}"
                await asyncio.to_thread(shutil.copyfile, absolute, project / local)
                pictures[reference] = local
                return local

            async def localize(value: Any) -> None:
                # Every picture field, at any depth (cover, cards, team, gallery…), is a Knowledge path.
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == "image" and isinstance(item, str) and item.strip():
                            value[key] = await local_picture(item.strip())
                        else:
                            await localize(item)
                elif isinstance(value, list):
                    for item in value:
                        await localize(item)

            await localize(spec["slides"])
            (project / "deck.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            summary = await asyncio.to_thread(_build_styled_deck, project / "deck.json")
            if summary.get("status") != "ready":
                return json.dumps({
                    "status": "failed",
                    "error": "The deck could not be laid out as written; fix every listed problem and call again.",
                    "engine_status": summary.get("status"),
                    "problems": summary.get("errors") or [],
                    **({"layouts": summary["layouts"]} if summary.get("layouts") else {}),
                }, ensure_ascii=False)
            data = Path(summary["pptx"]).read_bytes()
        from packages.core.services.file_conversion import convert_office_file

        # The PDF is made from the same bytes before anything is written, so a
        # failed conversion leaves no half-delivered package behind.
        pdf_data = await convert_office_file(data, "pptx", "pdf")
        deck_path = directory + "/deck.pptx"
        await _save(ctx, runtime, deck_path, data)
        saved_pdf = await _save(ctx, runtime, directory + "/deck.pdf", pdf_data)
        pdf_path = saved_pdf["fs_path"]
        notes = "# " + spec["title"] + "\n\n" + "\n\n".join(
            f"## Slide {index + 1}: {slide.get('title') or slide['layout']}\n\n{slide.get('notes') or ''}".rstrip()
            for index, slide in enumerate(spec["slides"]))
        notes += "\n\n## Sources and production notes\n\n" + source_notes + "\n"
        saved_notes = await _save(ctx, runtime, directory + "/notes.md", notes.encode("utf-8"))
        return json.dumps({"status": "completed", "files": [deck_path, pdf_path, saved_notes["fs_path"]],
                           "pages": summary.get("slides"), "style": style,
                           "quality_score": summary.get("quality_score"), "warnings": summary.get("warnings") or [],
                           "requires_visual_review": True}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs):
    values = {**kwargs, "workspace_id": workspace_id, "task_id": task_id,
              "conversation_id": conversation_id}
    values.setdefault("_agent_id_from_context", agent_id)
    runtime = runtime_tool_call_context_from_handler(values, user_id=user_id)
    return dict(entity_id=entity_id, user_id=runtime.user_id or "", workspace_id=runtime.workspace_id,
                task_id=runtime.task_id, agent_id=runtime.agent_id, conversation_id=runtime.conversation_id), runtime


async def _save(ctx, runtime, rel: str, data: bytes) -> dict:
    from packages.core.services.entity_fs import get_entity_root

    absolute, synced, _ = await _commit_file_projection(
        entity_id=ctx["entity_id"], rel_path=rel, data=data,
        entity_root=get_entity_root(ctx["entity_id"]), runtime_context=runtime,
        tool_name="creative_studio", allow_empty=False, expected_source_version=None,
    )
    if not synced.document_id:
        raise ValueError("Knowledge did not return a Document")
    return {"document_id": synced.document_id, "fs_path": rel, "name": Path(rel).name,
            "file_size": Path(absolute).stat().st_size, "sha256": hashlib.sha256(data).hexdigest(),
            "kind": {".png": "image", ".mp3": "audio", ".mp4": "video"}.get(Path(rel).suffix, "document")}


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


@media._with_media_file_access
async def _prepare_creative_studio_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    brief: str = "", sources: Any = None, source_paths: str = "", mode: str = "", output_directory: str = "",
    **kwargs: Any,
) -> str:
    try:
        validate(dict(brief=brief, sources=sources or [], source_paths=source_paths, mode=mode,
                      output_directory=output_directory), PREPARE_SCHEMA["function"]["parameters"])
        if not brief.strip():
            raise ValueError("Provide a meaningful production brief")
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        _, output = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        paths = await _document_ref_paths(sources, entity_id, limit=5)
        paths += [p.strip() for p in source_paths.splitlines() if p.strip()]
        if len(paths) > 5 or len(set(paths)) != len(paths):
            raise ValueError("Provide up to five distinct source paths, one per line")
        sources, text_size = [], 0
        for reference in paths:
            rel, absolute = await _source_path(reference, ctx)
            path = Path(absolute)
            if not 0 < path.stat().st_size <= 100 * 1024 * 1024:
                raise ValueError("Each source must be non-empty and at most 100 MiB")
            ext = path.suffix.lower()
            if ext not in {".txt", ".md", ".json", ".srt", ".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mp3", ".wav"}:
                raise ValueError("Unsupported source format: " + ext)
            digest = await asyncio.to_thread(media._sha256_file, absolute)
            item = {"fs_path": rel, "sha256": digest, "file_size": path.stat().st_size}
            if ext in {".txt", ".md", ".json", ".srt", ".pdf", ".docx", ".pptx"}:
                if path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("Text and Office sources are limited to 20 MiB")
                item["quoted_text"] = await _read_supported_text(absolute)
                text_size += len(item["quoted_text"])
                if text_size > 50000:
                    raise ValueError("Sources exceed 50,000 extracted characters; supply a focused excerpt")
            if await asyncio.to_thread(media._sha256_file, absolute) != digest:
                raise ValueError("Source changed during preparation; retry with the current file")
            sources.append(item)
        if mode == "dubbing" and sum(Path(p["fs_path"]).suffix.lower() in media.VIDEO_EXTENSIONS for p in sources) != 1:
            raise ValueError("Dubbing requires exactly one uploaded narrated video, plus optional matching SRT")
        if mode in {"presentation", "handbook"} and not (shutil.which("soffice") and shutil.which("pdftoppm")):
            raise ValueError("The worker requires LibreOffice and pdftoppm for real PDF/page rendering")
        if mode in {"podcast", "dubbing", "video"} and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise ValueError("The worker requires FFmpeg and ffprobe for audio/video production")
        snapshot = {"schema_version": 1, "mode": mode, "brief": brief.strip(), "sources": sources,
                    "output_directory": output, "workspace_id": ctx["workspace_id"]}
        saved = await _save(ctx, runtime, output + "/brief.json", _json_bytes(snapshot))
        return json.dumps({"status": "completed", "snapshot": saved, **snapshot}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def _inspect_file(path: str) -> dict:
    from PIL import Image
    from pypdf import PdfReader

    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        reader = PdfReader(path)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 24:
            raise ValueError("PDF must have 1–24 readable pages")
        if any(not (p.extract_text() or "").strip() for p in reader.pages):
            raise ValueError("Every PDF page must contain readable text")
        return {"pages": len(reader.pages)}
    if ext == ".pptx":
        from defusedxml import ElementTree

        with ZipFile(path) as archive:
            slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
            if not 1 <= len(slides) <= 24:
                raise ValueError("Presentation must have 1–24 slides")
            if sum(entry.file_size for entry in archive.infolist()) > 200 * 1024 * 1024:
                raise ValueError("Expanded presentation is too large")
            for slide in slides:
                root = ElementTree.fromstring(archive.read(slide))
                if not any((item.text or "").strip() for item in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")):
                    raise ValueError("Every slide must have editable text")
            return {"pages": len(slides), "pictures": sum(n.startswith("ppt/media/") for n in archive.namelist())}
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        with Image.open(path) as image:
            if not 256 <= min(image.size) or max(image.size) > 8192:
                raise ValueError("Image dimensions must be between 256 and 8192 pixels")
            size = image.size
            image.verify()
        # Container validation alone does not decode JPEG/WebP pixel data.
        with Image.open(path) as image:
            image.load()
        return {"width": size[0], "height": size[1]}
    if ext in {".md", ".txt", ".srt", ".json"}:
        if Path(path).stat().st_size > 1024 * 1024:
            raise ValueError("Delivery text exceeds 1 MiB")
        value = Path(path).read_text(encoding="utf-8")
        if not value.strip():
            raise ValueError("Empty text delivery")
        if ext == ".json":
            json.loads(value)
        return {"characters": len(value)}
    if ext in {".mp3", ".mp4"}:
        return {}
    raise ValueError("Unsupported delivery format: " + ext)


def _capture_verification_file(
    entity_id: str, rel: str, absolute: str, destination: str | None, max_bytes: int,
) -> tuple[str, int]:
    """Hash a bounded, pinned read; optionally freeze those exact bytes for QA."""
    from packages.core.services.entity_fs import open_entity_file_snapshot

    digest, size = hashlib.sha256(), 0
    with open_entity_file_snapshot(entity_id, rel, expected_resolved_path=absolute) as snapshot:
        if not 0 < snapshot.stat.st_size <= max_bytes:
            raise ValueError("Verification file is empty or exceeds its size limit: " + rel)
        with open(snapshot.descriptor_path, "rb") as source, (
            open(destination, "xb") if destination is not None else nullcontext()
        ) as target:
            while chunk := source.read(min(1024 * 1024, max_bytes - size + 1)):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Verification file grew beyond its size limit: " + rel)
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
        if size != snapshot.stat.st_size:
            raise ValueError("File changed during verification; retry with the current version")
    return digest.hexdigest(), size


async def _capture_verification_file_async(*args) -> tuple[str, int]:
    # A cancelled to_thread call keeps writing. Finish that bounded copy before
    # releasing the read lock or deleting its private temporary directory.
    task = asyncio.create_task(asyncio.to_thread(_capture_verification_file, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Preserve cancellation even if the copy failed while we waited.
        if not task.cancelled():
            task.exception()
        raise


def _check_package(mode: str, inspected: list[dict]) -> None:
    def items(ext):
        return [f for f in inspected if Path(f["fs_path"]).suffix.lower() == ext]

    if not items(".md"):
        raise ValueError("Include a Markdown script, speaker notes, or production/source notes")
    if mode in {"presentation", "handbook"}:
        decks, pdfs = items(".pptx"), items(".pdf")
        if len(decks) != 1 or len(pdfs) != 1:
            raise ValueError("Deliver one editable PPTX and its matching PDF")
        if not 3 <= decks[0]["qa"]["pages"] <= 16 or decks[0]["qa"]["pages"] != pdfs[0]["qa"]["pages"]:
            raise ValueError("PPTX/PDF must have the same 3–16 page count")
        if not decks[0]["qa"]["pictures"]:
            raise ValueError("The publication must contain actual illustrations, not text only")
    if mode == "carousel":
        images = [f for f in inspected if f["kind"] == "image"]
        if not 3 <= len(images) <= 6 or len({f["sha256"] for f in images}) != len(images):
            raise ValueError("Deliver 3–6 distinct finished image pages")
    if mode == "podcast" and (len(items(".mp3")) != 1 or not 20 <= items(".mp3")[0]["qa"].get("duration_seconds", 0) <= 600):
        raise ValueError("Deliver one decodable MP3, 20 seconds to 10 minutes")
    if mode == "video":
        videos = items(".mp4")
        if len(videos) != 1 or not videos[0]["qa"].get("has_audio") or not videos[0]["qa"].get("has_video") \
                or not 10 <= videos[0]["qa"].get("duration_seconds", 0) <= 600:
            raise ValueError("Deliver one narrated MP4 with picture and sound, 10 seconds to 10 minutes")
    if mode == "dubbing":
        if len(items(".mp4")) != 1 or len(items(".srt")) != 1:
            raise ValueError("Deliver one dubbed MP4 and matching target-language SRT")
        if not items(".mp4")[0]["qa"].get("has_audio") or not items(".mp4")[0]["qa"].get("has_video"):
            raise ValueError("The dubbed video requires both picture and audible media streams")


@media._with_media_file_access
async def _verify_creative_studio_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None, conversation_id: str | None = None,
    snapshot_path: str = "", snapshot_sha256: str = "", files: list[str] | None = None,
    output_directory: str = "", **kwargs: Any,
) -> str:
    try:
        from sqlalchemy import select
        from packages.core.database import async_session
        from packages.core.models.document import Document
        from packages.core.services.entity_fs import entity_filesystem_read_lock
        from packages.core.services.slide_renderer import render_slides

        validate(dict(snapshot_path=snapshot_path, snapshot_sha256=snapshot_sha256,
                      files=files, output_directory=output_directory), VERIFY_SCHEMA["function"]["parameters"])
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        entity_root, receipt_dir = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        with tempfile.TemporaryDirectory(prefix="manor-creative-verify-") as verification_dir:
            frozen_brief = str(Path(verification_dir) / "brief.json")
            async with entity_filesystem_read_lock(entity_root):
                brief_rel, snapshot_abs = await _source_path(snapshot_path, ctx)
                brief_digest, _ = await _capture_verification_file_async(
                    entity_id, brief_rel, snapshot_abs, frozen_brief, 1024 * 1024,
                )
            if brief_digest != snapshot_sha256:
                raise ValueError("Frozen brief changed; restart production")
            snapshot = json.loads(Path(frozen_brief).read_text())
            if snapshot["workspace_id"] != ctx["workspace_id"] or snapshot["mode"] not in MODES:
                raise ValueError("The frozen brief belongs to another Workspace or unsupported studio")
            for source in snapshot["sources"]:
                _, source_abs = await _source_path(source["fs_path"], ctx)
                if await asyncio.to_thread(media._sha256_file, source_abs) != source["sha256"]:
                    raise ValueError("Source changed since preparation: " + source["fs_path"])
            if len(set(files)) != len(files):
                raise ValueError("Duplicate output references are not separate deliverables")
            inspected, local_paths, total = [], {}, 0
            for index, reference in enumerate(files):
                async with entity_filesystem_read_lock(entity_root):
                    rel, absolute = await _source_path(reference, ctx)
                    if not rel.startswith(snapshot["output_directory"] + "/") or rel == brief_rel:
                        raise ValueError("Deliverables must be new files inside this production's output directory")
                    async with async_session() as db:
                        doc = await db.scalar(select(Document).where(Document.entity_id == entity_id,
                            Document.fs_path == rel, Document.is_trashed.is_(False), Document.quarantine_status == "clean"))
                        if not doc:
                            raise ValueError("Missing current, clean Knowledge Document: " + rel)
                        document_id = doc.id
                    ext = Path(absolute).suffix.lower()
                    frozen_path = str(Path(verification_dir) / f"delivery-{index:02}{ext}")
                    digest, size = await _capture_verification_file_async(
                        entity_id, rel, absolute, frozen_path,
                        min(100 * 1024 * 1024, 250 * 1024 * 1024 - total),
                    )
                    total += size
                # Native parsers, probes, decoders and previews all see the same
                # private bytes. The shared read lock is not held during QA.
                qa = await asyncio.to_thread(_inspect_file, frozen_path)
                if ext in {".mp3", ".mp4"}:
                    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
                    if not ffmpeg or not ffprobe:
                        raise ValueError("FFmpeg and ffprobe are required")
                    qa = await media._probe_media_report(ffprobe, frozen_path)
                    if not 0 < qa.get("duration_seconds", 0) <= 1800 or not qa.get("has_audio"):
                        raise ValueError("Invalid media duration or missing audio")
                    _, decode_errors = await media._run_process(
                        [ffmpeg, "-v", "error", "-xerror", "-i", frozen_path, "-f", "null", "-"],
                        timeout_seconds=180,
                    )
                    if decode_errors.strip():
                        raise ValueError(f"ffmpeg reported decode errors: {decode_errors[-2000:]}")
                kind = "image" if ext in media.IMAGE_EXTENSIONS else {".mp4": "video", ".mp3": "audio"}.get(ext, "document")
                inspected.append({"document_id": document_id, "fs_path": rel, "name": Path(rel).name,
                                  "file_size": size, "kind": kind, "status": "completed", "qa": qa, "sha256": digest})
                local_paths[rel] = frozen_path
            _check_package(snapshot["mode"], inspected)
            if snapshot["mode"] == "dubbing":
                srt = next(f for f in inspected if f["fs_path"].endswith(".srt"))
                video = next(f for f in inspected if f["fs_path"].endswith(".mp4"))
                subtitle_check = media._validate_subtitle_content(
                    Path(local_paths[srt["fs_path"]]).read_text(encoding="utf-8-sig"),
                    "srt", video["qa"]["duration_seconds"],
                )
                if subtitle_check.get("verdict") != "pass":
                    raise ValueError("Subtitle timing validation failed: " + json.dumps(subtitle_check))
            previews = []
            deck = next((f for f in inspected if f["fs_path"].endswith(".pptx")), None)
            if deck:
                rendered = await render_slides(local_paths[deck["fs_path"]], str(Path(verification_dir) / "previews"), dpi=100)
                if len(rendered) != deck["qa"]["pages"]:
                    raise ValueError("Page rendering did not produce every slide")
                for index, path in enumerate(rendered, 1):
                    previews.append(await _save(ctx, runtime, f"{receipt_dir}/page-{index:02}.png", Path(path).read_bytes()))
            async with entity_filesystem_read_lock(entity_root):
                for item in inspected:
                    rel, absolute = await _source_path(item["fs_path"], ctx)
                    digest, _ = await _capture_verification_file_async(
                        entity_id, rel, absolute, None, 100 * 1024 * 1024,
                    )
                    if digest != item["sha256"]:
                        raise ValueError("Output changed during verification; verify the current version again")
        checklist = ["Open every final file and check all pages or the whole recording.",
                     "Check wording, source accuracy, visual fit, rights and privacy.",
                     "For AI speech: listen for omissions, pronunciation and timing; no voice cloning or lip sync is claimed.",
                     "Accept retains files here only. Request changes with a note to get a new version; this one is kept."]
        await mark_production_results(entity_id, snapshot["output_directory"], {item["fs_path"] for item in inspected})
        receipt = await _save(ctx, runtime, receipt_dir + "/verification.json", _json_bytes({
            "mode": snapshot["mode"], "snapshot_sha256": snapshot_sha256,
            "technical_checks_passed": True, "files": inspected, "previews": previews,
            "limitations": checklist, "published": False,
        }))
        return json.dumps({"status": "completed", "technical_checks_passed": True,
                           "final_files": inspected, "previews": previews, "receipt": receipt,
                           "review_checklist": checklist}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


def get_tools():
    return [(PREPARE_SCHEMA, _prepare_creative_studio_handler), (VERIFY_SCHEMA, _verify_creative_studio_handler),
            (PUBLICATION_SCHEMA, _compose_creative_publication_handler), (DECK_SCHEMA, _compose_creative_deck_handler),
            (DUB_SCHEMA, _compose_creative_dub_handler), (VIDEO_SCHEMA, _compose_creative_video_handler)]
