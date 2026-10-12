"""Source-preserving recording production; models select cues, never invent media.

The public tools share the existing media ACL, STT billing, FFmpeg, atomic file
and Knowledge boundaries. A hashed transcript snapshot separates planning from
rendering so neither a model nor a changed source can silently move the cuts.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import validate

from packages.core.ai.tools import media_tools as media
from packages.core.models.base import generate_ulid


CUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "first_segment", "last_segment"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 100},
        "first_segment": {"type": "integer", "minimum": 0},
        "last_segment": {"type": "integer", "minimum": 0},
    },
}
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["cuts"],
    "properties": {"cuts": {
        "type": "array", "minItems": 1, "maxItems": 12, "items": CUT_SCHEMA,
    }},
}


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": properties, "required": required},
    }}


PREPARE_RECORDING_SCHEMA = _schema(
    "prepare_recording", "Inspect an uploaded narrated video (up to 30 minutes), "
    "load its supplied SRT or transcribe original speech using timestamp-capable STT, "
    "and save a hashed source/transcript snapshot in Workspace Knowledge. No URL download. "
    "Supplied SRT is user-provided timing, not independently verified speech.",
    {"source_path": {"type": "string", "minLength": 1},
     "subtitle_path": {"type": "string", "default": ""},
     "output_directory": {"type": "string", "minLength": 1}},
    ["source_path", "output_directory"],
)
RENDER_RECORDING_SCHEMA = _schema(
    "render_recording", "Render source-preserving subtitled videos from a hashed "
    "recording snapshot and cue-index cuts. Shorts: 1–5 separate 10–90 second "
    "720p portrait clips with the entire source picture fitted, not cropped. Tutorial: "
    "one 720p widescreen video, up to 12 chronological chapters and 10 minutes. "
    "Keeps original audio; saves MP4, SRT, frame preview, edit timeline and QA receipt. "
    "Technical completion still requires human content/privacy review; never publishes.",
    {"snapshot_path": {"type": "string", "minLength": 1},
     "snapshot_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
     "mode": {"type": "string", "enum": ["shorts", "tutorial"]},
     "plan": PLAN_SCHEMA,
     "output_directory": {"type": "string", "minLength": 1}},
    ["snapshot_path", "snapshot_sha256", "mode", "plan", "output_directory"],
)


def _result(raw: str) -> dict:
    result = json.loads(raw)
    if result.get("error") or result.get("status") == "failed":
        raise ValueError(result.get("error") or result)
    return result


async def _runtime_preflight() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("FFmpeg and ffprobe are required in the worker runtime")
    filters, _ = await media._run_process([ffmpeg, "-hide_banner", "-filters"], timeout_seconds=30)
    if not re.search(r"\bsubtitles\s+V->V", filters):
        raise ValueError("Worker FFmpeg requires the libass subtitles filter; enable it before running production")
    return ffmpeg, ffprobe


def _segments(raw: list[dict], duration: float) -> list[dict]:
    if not raw or len(raw) > 1200:
        raise ValueError("A timestamped transcript with 1–1200 speech cues is required")
    result = []
    previous_end = 0.0
    for index, item in enumerate(raw):
        start, end = float(item["start"]), float(item["end"])
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < previous_end - 0.001 or end <= start
                or end > duration + 0.05 or not text or len(text) > 2000):
            raise ValueError(f"Invalid or overlapping transcript cue {index}; correct the source SRT")
        result.append({"id": index, "start": start, "end": min(end, duration), "text": text})
        previous_end = end
    if sum(len(item["text"]) for item in result) > 80000:
        raise ValueError("Transcript exceeds the 80,000 character planning limit; split the source")
    return result


def _cuts(plan: dict, segments: list[dict], mode: str) -> list[dict]:
    validate(plan, PLAN_SCHEMA)
    if mode not in {"shorts", "tutorial"}:
        raise ValueError("mode must be shorts or tutorial")
    if mode == "shorts" and len(plan["cuts"]) > 5:
        raise ValueError("Shorts batches are limited to five clips")
    cuts, last = [], -1
    for cut in plan["cuts"]:
        first, end = cut["first_segment"], cut["last_segment"]
        if first > end or end >= len(segments) or first <= last:
            raise ValueError("Cuts must use valid, non-overlapping, chronological cue ranges")
        start_seconds, end_seconds = segments[first]["start"], segments[end]["end"]
        duration = end_seconds - start_seconds
        if duration < 1 or (mode == "shorts" and not 10 <= duration <= 90):
            raise ValueError("Shorts must be 10–90 seconds; tutorial chapters must be at least 1 second")
        cuts.append({**cut, "start_seconds": start_seconds, "end_seconds": end_seconds})
        last = end
    if mode == "tutorial" and not 10 <= sum(c["end_seconds"] - c["start_seconds"] for c in cuts) <= 600:
        raise ValueError("The tutorial must be 10–600 seconds; shorten or extend the selected chapters")
    return cuts


async def _scope(entity_id: str, workspace_id: str | None, task_id: str | None,
                 output_directory: str) -> tuple[str, str]:
    from packages.core.services.entity_fs import get_entity_root

    if not entity_id or not workspace_id:
        raise ValueError("An active Workspace and Entity are required")
    base = await media._workspace_media_base_dir(
        entity_id=entity_id, workspace_id=workspace_id, task_id=task_id,
    )
    from packages.core.services.generated_media_naming import execution_output_directory, workspace_storage_root
    output_directory = execution_output_directory(output_directory, base)
    rel = media._workspace_media_reference(
        output_directory, entity_id=entity_id, workspace_base_dir=base,
    )
    home = workspace_storage_root(base)
    if not home or not rel.startswith(home + "/"):
        raise ValueError("Outputs must remain inside the current Workspace")
    return get_entity_root(entity_id), rel.rstrip("/") + "/" + generate_ulid()


async def _source_path(reference: str, ctx: dict) -> tuple[str, str]:
    from packages.core.services.entity_fs import get_entity_root

    if reference.startswith(("http://", "https://")):
        raise ValueError("Upload the source to Knowledge; remote downloads are not supported")
    base = await media._workspace_media_base_dir(
        entity_id=ctx["entity_id"], workspace_id=ctx["workspace_id"], task_id=ctx["task_id"],
    )
    rel = media._workspace_media_reference(reference, entity_id=ctx["entity_id"], workspace_base_dir=base)
    # The normal runtime ACL also checks shared Knowledge and actor membership.
    from packages.core.services.generated_media_naming import workspace_storage_root
    if rel.startswith("Workspaces/") and not rel.startswith(workspace_storage_root(base) + "/"):
        raise ValueError("Source belongs to another Workspace")
    return rel, await media._resolve_entity_file(get_entity_root(ctx["entity_id"]), rel)


async def _save(ctx: dict, rel: str, content: str, role: str) -> dict:
    mime = {".srt": "application/x-subrip", ".ass": "text/x-ass", ".json": "application/json"}[Path(rel).suffix]
    path = media.runtime_write_entity_file_atomic(ctx["entity_id"], rel, content.encode("utf-8"), allow_empty=False)
    size = Path(path).stat().st_size
    document_id = await media._register_file_artifact(
        **ctx, filename=Path(rel).name, rel_path=rel, file_size=size,
        file_type=Path(rel).suffix.lstrip("."), mime_type=mime,
        tool_name="recording_production", artifact_role=role,
        generation={"operation": "recording_production"},
    )
    if not document_id:
        raise ValueError("Knowledge did not return a durable Document for " + rel)
    await media._bind_artifact_to_workspace(**ctx, document_id=document_id, tool_name="recording_production")
    sha256 = media._sha256_file(path)
    return {"document_id": document_id, "fs_path": rel, "file_size": size,
            "viewer_url": media._fs_url(ctx["entity_id"], rel),
            "sha256": sha256,
            "file": media._produced_file(
                ctx["entity_id"], rel, document_id=document_id,
                size=size, mime=mime, sha256=sha256,
            )}


@media._with_media_file_access
async def _prepare_recording_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None,
    conversation_id: str | None = None, source_path: str = "",
    subtitle_path: str = "", output_directory: str = "", **_: Any,
) -> str:
    ctx = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
               task_id=task_id, agent_id=agent_id, conversation_id=conversation_id)
    try:
        _, output = await _scope(entity_id, workspace_id, task_id, output_directory)
        ffmpeg, ffprobe = await _runtime_preflight()
        rel, absolute = await _source_path(source_path, ctx)
        media._assert_video_path(absolute)
        report = await media._probe_media_report(ffprobe, absolute)
        duration = float(report["duration_seconds"])
        if not report["has_video"] or not report["has_audio"] or not 10 <= duration <= 1800:
            raise ValueError("Provide a narrated video with picture and audio, 10 seconds to 30 minutes long")
        source_hash = media._sha256_file(absolute)
        if subtitle_path:
            _, subtitle = await _source_path(subtitle_path, ctx)
            if Path(subtitle).suffix.lower() != ".srt" or Path(subtitle).stat().st_size > 500000:
                raise ValueError("Supply an SRT subtitle file smaller than 500 KB")
            parsed = media._parse_subtitle_content(Path(subtitle).read_text(encoding="utf-8-sig"), "srt")
            if parsed.get("parse_errors"):
                raise ValueError("Source SRT could not be parsed")
            segments = _segments(parsed["cues"], duration)
            timing_source = "user_supplied_srt"
        else:
            # Compression keeps a 30-minute source below the native STT upload limit.
            with tempfile.TemporaryDirectory(prefix="recording-speech-") as tmp:
                audio = str(Path(tmp) / "speech.mp3")
                await media._run_process([
                    ffmpeg, "-v", "error", "-y", "-i", absolute, "-vn",
                    "-ac", "1", "-ar", "16000", "-b:a", "48k", audio,
                ], timeout_seconds=180)
                transcript = await media._transcribe_narration_audio(
                    audio_path=audio, entity_id=entity_id, user_id=user_id,
                    workspace_id=workspace_id, agent_id=agent_id, conversation_id=conversation_id,
                )
            segments = _segments(transcript.segments or [], duration)
            timing_source = "timestamped_stt"
        if media._sha256_file(absolute) != source_hash:
            raise ValueError("Source changed during preparation; restart with a stable upload")
        snapshot = {"version": 1, "source_path": rel, "source_sha256": source_hash,
                    "duration_seconds": duration, "timing_source": timing_source,
                    "segments": segments, "source_probe": report}
        receipt = await _save(ctx, output + "/source.json", json.dumps(snapshot, ensure_ascii=False), "recording_source")
        return media._json({"status": "completed", "snapshot": receipt,
                            "segments": segments, "duration_seconds": duration,
                            "timing_source": timing_source})
    except Exception as exc:
        return media._json_error(str(exc), code="recording_preparation_failed")


def _subtitle_cues(cuts: list[dict], segments: list[dict], *, line_width: int = 34) -> tuple[list, list]:
    cues, chapters, offset = [], [], 0.0
    for cut in cuts:
        chapters.append({"title": cut["title"], "start_seconds": round(offset, 3),
                         "source_start_seconds": cut["start_seconds"], "source_end_seconds": cut["end_seconds"]})
        for item in segments[cut["first_segment"]:cut["last_segment"] + 1]:
            # Literal captions: strip subtitle control/markup rather than executing it.
            text = re.sub(r"<[^>]*>|\{[^}]*\}", "", item["text"]).replace("\\", "")
            cues.append(media.SubtitleCue(
                index=len(cues) + 1, start=offset + item["start"] - cut["start_seconds"],
                end=offset + item["end"] - cut["start_seconds"], text=text, cue_type="dialogue",
            ))
        offset += cut["end_seconds"] - cut["start_seconds"]
    return media._fit_subtitle_cues_to_line_limit(cues, max_chars_per_line=line_width, max_lines=2), chapters


@media._with_media_file_access
async def _render_recording_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None,
    conversation_id: str | None = None, snapshot_path: str = "",
    snapshot_sha256: str = "", mode: str = "", plan: dict | None = None,
    output_directory: str = "", **_: Any,
) -> str:
    ctx = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
               task_id=task_id, agent_id=agent_id, conversation_id=conversation_id)
    completed = []
    try:
        root, output = await _scope(entity_id, workspace_id, task_id, output_directory)
        ffmpeg, ffprobe = await _runtime_preflight()
        _, snapshot_abs = await _source_path(snapshot_path, ctx)
        if Path(snapshot_abs).stat().st_size > 1000000 or media._sha256_file(snapshot_abs) != snapshot_sha256:
            raise ValueError("Source snapshot changed; prepare and plan again")
        snapshot = json.loads(Path(snapshot_abs).read_text())
        source, absolute = await _source_path(snapshot["source_path"], ctx)
        if media._sha256_file(absolute) != snapshot["source_sha256"]:
            raise ValueError("Source video changed; prepare and plan again")
        segments = _segments(snapshot["segments"], float(snapshot["duration_seconds"]))
        cuts = _cuts(plan, segments, mode)
        batches = [[cut] for cut in cuts] if mode == "shorts" else [cuts]
        ratio = "9:16" if mode == "shorts" else "16:9"
        expected_width, expected_height = media._target_dimensions("720p", ratio)
        # Budget full-width glyphs so CJK captions also stay inside the canvas.
        line_width = min(34, int((expected_width - 80) / media._subtitle_font_size(expected_width, expected_height)))
        for index, batch in enumerate(batches, 1):
            prefix = f"{output}/{index:02d}"
            master = _result(await media._merge_videos_handler(
                **ctx, clips=[{"path": source, "start_seconds": cut["start_seconds"],
                               "end_seconds": cut["end_seconds"], "label": cut["title"]} for cut in batch],
                output_name=prefix + "/picture.mp4", resolution="720p", aspect_ratio=ratio,
                include_source_audio=True,
            ))
            cues, chapters = _subtitle_cues(batch, segments, line_width=line_width)
            srt = await _save(ctx, prefix + "/captions.srt", media._render_subtitles(
                cues, subtitle_format="srt", max_chars_per_line=line_width, style={},
            ), "subtitle")
            # SRT has no canvas size: libass scales its default PlayRes and can
            # turn portrait subtitles into full-screen text. Burn pixel-sized
            # ASS with an explicit canvas; retain SRT as the portable download.
            ass = await _save(ctx, prefix + "/captions.ass", media._render_subtitles(
                cues, subtitle_format="ass", max_chars_per_line=line_width, style={},
                canvas_width=expected_width, canvas_height=expected_height,
            ), "subtitle")
            planned_duration = sum(c["end_seconds"] - c["start_seconds"] for c in batch)
            timeline = {"spec": {"resolution": "720p", "aspect_ratio": ratio, "fps": 30},
                        "delivery": {"clean_picture_master": master["fs_path"]},
                        "subtitles": {"srt_path": srt["fs_path"], "ass_path": ass["fs_path"]}, "chapters": chapters,
                        # Point the editor at the clean master, not the burned
                        # final video, so editable captions are not drawn twice.
                        "clips": [{"label": "Source picture and original audio", "start": 0,
                                   "end": planned_duration, "path": master["fs_path"],
                                   "document_id": master["document_id"]}],
                        "captions": [{"id": f"caption-{cue.index}", "start": cue.start,
                                      "end": cue.end, "text": cue.text} for cue in cues],
                        "markers": [{"time": chapter["start_seconds"], "label": chapter["title"]}
                                    for chapter in chapters],
                        "source": {"snapshot_path": snapshot_path, "snapshot_sha256": snapshot_sha256},
                        "cuts": batch}
            edit = await _save(ctx, prefix + "/timeline.json", json.dumps(timeline, ensure_ascii=False), "timeline")
            final = _result(await media._compose_video_timeline_handler(
                **ctx, timeline_path=edit["fs_path"], output_name=prefix + "/final.mp4",
                subtitle_path=ass["fs_path"],
                include_source_audio=True, include_audio=False, burn_subtitles=True,
            ))
            final_abs = await media._resolve_entity_file(root, final["fs_path"])
            probe = await media._probe_media_report(ffprobe, final_abs)
            expected_duration = sum(c["end_seconds"] - c["start_seconds"] for c in batch)
            stream = probe.get("video_stream") or {}
            if (not final.get("document_id") or not probe.get("has_video") or not probe.get("has_audio")
                    or abs(float(probe["duration_seconds"]) - expected_duration) > 0.25
                    or (stream.get("width"), stream.get("height")) != (expected_width, expected_height)):
                raise ValueError("Final MP4 failed duration, stream, dimensions or Knowledge checks")
            # ffprobe metadata alone is not proof that every packet decodes.
            await media._run_process([ffmpeg, "-v", "error", "-xerror", "-i", final_abs,
                                      "-f", "null", "-"], timeout_seconds=180)
            subtitles = _result(await media._validate_subtitles_handler(
                **ctx, subtitle_path=ass["fs_path"], media_path=final["fs_path"],
            ))
            if subtitles.get("verdict") != "pass":
                raise ValueError("Final subtitle validation failed: " + json.dumps(subtitles))
            frames = _result(await media._render_frame_samples_handler(
                **ctx, input_path=final["fs_path"], output_dir=prefix + "/preview",
                timestamps=[min(expected_duration / 2, 2)], max_samples=1,
            ))
            completed.append({"title": batch[0]["title"] if mode == "shorts" else "Edited tutorial",
                              "video": final, "subtitles": srt, "burned_subtitles": ass, "timeline": edit, "preview": frames,
                              "chapters": chapters, "sha256": media._sha256_file(final_abs),
                              "qa": {"full_decode": True, "probe": probe, "subtitles": subtitles}})
        if media._sha256_file(absolute) != snapshot["source_sha256"]:
            raise ValueError("Source video changed while rendering; prepare and plan again")
        delivery = {"status": "completed", "machine_pass": True, "review_required": True,
                    "mode": mode, "timing_source": snapshot["timing_source"],
                    "final_videos": [{**item["video"], "name": item["title"] + ".mp4"} for item in completed], "outputs": completed,
                    "review_checklist": ["Watch every clip; verify cut context and subtitle accuracy",
                                         "Check screen text readability and remove sensitive source material",
                                         "Confirm rights and consent before any external use"],
                    "not_included": ["publishing", "new narration", "automatic redaction", "subject tracking"]}
        receipt = await _save(ctx, output + "/delivery.json", json.dumps(delivery, ensure_ascii=False), "qa_report")
        return media._json({**delivery, "receipt": receipt})
    except Exception as exc:
        return media._json({"status": "failed", "code": "recording_render_failed", "error": str(exc),
                            "completed_outputs": completed,
                            "retry": "Fix the reported input/runtime issue and retry. Existing files are retained; retries create a new output directory."})


def get_tools() -> list[tuple[dict, Any]]:
    return [(PREPARE_RECORDING_SCHEMA, _prepare_recording_handler),
            (RENDER_RECORDING_SCHEMA, _render_recording_handler)]
