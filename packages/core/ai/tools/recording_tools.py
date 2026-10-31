"""Source-preserving recording production; models select cues, never invent media.

The public tools share the existing media ACL, STT/TTS billing, FFmpeg, atomic
file and Knowledge boundaries. A hashed transcript snapshot separates planning
from rendering so neither a model nor a changed source can silently move the
cuts. An AI voice reads exactly the selected subtitles; it never writes them.
"""
from __future__ import annotations

import asyncio
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
from packages.core.services.voice.whisper import WhisperTimestampError
from packages.core.services.workspace_layout import PROJECT_ARTIFACT_SEGMENT


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
VOICES = ("original", "ai")
VOICE_SCHEMA = {
    "type": "string", "enum": list(VOICES), "default": "original",
    "description": "original keeps the speaker's audio. ai narrates the finished video with a generated "
                   "voice reading the subtitles, replacing the original audio; a silent recording then needs its SRT.",
}
# Pause between narrated lines, and how many lines are voiced at once.
VOICE_GAP_SECONDS = 0.08
VOICE_CONCURRENCY = 4
# The fastest a narrator still sounds natural when a chapter must fit its footage.
VOICE_MAX_TEMPO = 1.25
VOICE_EDGE_SECONDS = 0.05
# Speech is transcribed in pieces of about this length, so recordings have no length limit.
STT_CHUNK_SECONDS = 600
# What the editor can read in one plan: many hours of speech.
MAX_TRANSCRIPT_CUES = 20000
MAX_TRANSCRIPT_CHARS = 400000
VOICE_DIRECTION = "Clear, friendly instructional narration at a natural, slightly brisk pace."


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": properties, "required": required},
    }}


PREPARE_RECORDING_SCHEMA = _schema(
    "prepare_recording", "Inspect an uploaded narrated video of any length, "
    "load its supplied SRT or transcribe original speech using timestamp-capable STT, "
    "and save a hashed source/transcript snapshot in Workspace Knowledge. No URL download. "
    "Supplied SRT is user-provided timing, not independently verified speech.",
    {"source": {"type": "array", "maxItems": 1, "description": "The picked recording Document.",
                "items": {"type": "object", "required": ["document_id"]}},
     "subtitle": {"type": "array", "maxItems": 1, "description": "Optional picked SRT Document.",
                  "items": {"type": "object", "required": ["document_id"]}},
     "source_path": {"type": "string", "description": "Knowledge path, when no recording is picked."},
     "subtitle_path": {"type": "string", "default": ""},
     "voice": VOICE_SCHEMA,
     "output_directory": {"type": "string", "minLength": 1}},
    ["output_directory"],
)
RENDER_RECORDING_SCHEMA = _schema(
    "render_recording", "Render source-preserving subtitled videos from a hashed "
    "recording snapshot and cue-index cuts. Shorts: 1–5 separate 720p portrait clips of up to "
    "90 seconds, with the entire source picture fitted, not cropped. Tutorial: "
    "one 720p widescreen video of up to 12 chronological chapters, as long as they run. "
    "Keeps original audio, or with voice=ai narrates the selected subtitles in a generated voice "
    "(billed text-to-speech, timed per chapter); saves MP4, SRT, frame preview, edit timeline and QA receipt. "
    "Technical completion still requires human content/privacy review; never publishes.",
    {"snapshot_path": {"type": "string", "minLength": 1},
     "snapshot_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
     "mode": {"type": "string", "enum": ["shorts", "tutorial"]},
     "plan": PLAN_SCHEMA,
     "voice": VOICE_SCHEMA,
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
    if not raw:
        raise ValueError("No speech was found in this recording; add its SRT or choose a narrated recording")
    if len(raw) > MAX_TRANSCRIPT_CUES:
        raise ValueError(f"The transcript has {len(raw)} speech cues, more than one edit can read; split the recording")
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
    if sum(len(item["text"]) for item in result) > MAX_TRANSCRIPT_CHARS:
        raise ValueError("The transcript is longer than one edit can read; split the recording")
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
        # A short is short by definition; a tutorial is as long as its chapters.
        if duration <= 0 or (mode == "shorts" and duration > 90):
            raise ValueError("Each short must be at most 90 seconds; choose a smaller cue range")
        cuts.append({**cut, "start_seconds": start_seconds, "end_seconds": end_seconds})
        last = end
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
    entity_root = get_entity_root(entity_id)
    rel = rel.rstrip("/")
    if rel.startswith(f"{home}/{PROJECT_ARTIFACT_SEGMENT}/"):
        # A project's folder keeps each version beside the last: v1, v2, ...;
        # a component writing into a version folder writes there.
        if re.fullmatch(r"v\d+", rel.rsplit("/", 1)[-1]):
            return entity_root, rel
        return entity_root, f"{rel}/v{_next_version(Path(entity_root, rel))}"
    return entity_root, rel + "/" + generate_ulid()


def _next_version(folder: Path) -> int:
    taken = [int(match.group(1)) for child in (folder.iterdir() if folder.is_dir() else ())
             if child.is_dir() and (match := re.fullmatch(r"v(\d+)", child.name))]
    return max(taken, default=0) + 1


async def _source_path(reference: str, ctx: dict) -> tuple[str, str]:
    from packages.core.services.entity_fs import get_entity_root

    if reference.startswith(("http://", "https://")):
        raise ValueError("Upload the source to Knowledge; remote downloads are not supported")
    base = await media._workspace_media_base_dir(
        entity_id=ctx["entity_id"], workspace_id=ctx["workspace_id"], task_id=ctx["task_id"],
    )
    entity_root = get_entity_root(ctx["entity_id"])
    rel = await media._existing_media_reference(
        reference, entity_id=ctx["entity_id"], entity_root=entity_root, workspace_base_dir=base,
    )
    # The normal runtime ACL also checks shared Knowledge and actor membership.
    from packages.core.services.generated_media_naming import workspace_storage_root
    if rel.startswith("Workspaces/") and not rel.startswith(workspace_storage_root(base) + "/"):
        raise ValueError("Source belongs to another Workspace")
    if not Path(entity_root, rel).is_file():
        # A picked Knowledge file lives where it was uploaded, outside the
        # Workspace folder. Use it when no Workspace file has that name.
        document_path = await _document_fs_path(ctx["entity_id"], reference)
        if document_path:
            rel = document_path
    return rel, await media._resolve_entity_file(entity_root, rel)


async def _document_fs_path(entity_id: str, reference: str) -> str | None:
    """The reference itself when it is exactly an active Knowledge Document's path."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    path = reference.strip().lstrip("/")
    if not path or path.startswith("Workspaces/"):
        return None
    async with async_session() as db:
        found = (await db.execute(select(Document.fs_path).where(
            Document.entity_id == entity_id, Document.fs_path == path, Document.is_trashed.is_(False),
        ).limit(1))).scalar_one_or_none()
    return found


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


def _speech_breaks(silence_log: str, duration: float) -> list[float]:
    """Chunk boundaries near every ``STT_CHUNK_SECONDS``, moved into a pause when one is close."""
    pauses = [(item["start"] + item["end"]) / 2
              for item in media._parse_silence_intervals(silence_log, duration_seconds=duration)["intervals"]]
    breaks, target = [], STT_CHUNK_SECONDS
    while target < duration - 30:
        near = [pause for pause in pauses if abs(pause - target) <= 90 and pause > (breaks[-1] if breaks else 0) + 30]
        breaks.append(min(near, key=lambda pause: abs(pause - target)) if near else target)
        target = breaks[-1] + STT_CHUNK_SECONDS
    return breaks


async def _transcribe(ffmpeg: str, source: str, duration: float, ctx: dict) -> list[dict]:
    """Timestamped speech for a recording of any length.

    The STT request size is bounded, so long speech is sent in pieces cut at
    pauses and their timestamps are shifted back onto the recording.
    """
    with tempfile.TemporaryDirectory(prefix="recording-speech-") as tmp:
        audio = str(Path(tmp) / "speech.mp3")
        await media._run_process([
            ffmpeg, "-v", "error", "-y", "-i", source, "-vn",
            "-ac", "1", "-ar", "16000", "-b:a", "48k", audio,
        ], timeout_seconds=max(180.0, duration / 2))
        breaks = []
        if duration > STT_CHUNK_SECONDS:
            _, silence_log = await media._run_process([
                ffmpeg, "-hide_banner", "-nostats", "-i", audio,
                "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-",
            ], timeout_seconds=max(120.0, duration / 4))
            breaks = _speech_breaks(silence_log, duration)
        bounds = [0.0, *breaks, duration]
        segments: list[dict] = []
        for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
            part = audio
            if len(bounds) > 2:
                part = str(Path(tmp) / f"part-{index:03d}.mp3")
                await media._run_process([
                    ffmpeg, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                    "-i", audio, "-c", "copy", part,
                ], timeout_seconds=120)
            try:
                transcript = await media._transcribe_narration_audio(
                    audio_path=part, entity_id=ctx["entity_id"], user_id=ctx["user_id"],
                    workspace_id=ctx["workspace_id"], agent_id=ctx["agent_id"], conversation_id=ctx["conversation_id"],
                )
            except WhisperTimestampError as exc:
                raise ValueError(
                    "This recording needs speech timings, which the current speech-to-text setup cannot "
                    "provide. Add the recording's SRT, or add an OpenAI or Groq key for speech-to-text"
                ) from exc
            for item in transcript.segments or []:
                previous = segments[-1]["end"] if segments else 0.0
                cue_start = max(previous, start + float(item["start"]))
                cue_end = min(end, start + float(item["end"]))
                if cue_end > cue_start:
                    segments.append({**item, "start": cue_start, "end": cue_end})
        return segments


@media._with_media_file_access
async def _prepare_recording_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None,
    conversation_id: str | None = None, source: Any = None, subtitle: Any = None, source_path: str = "",
    subtitle_path: str = "", voice: str = "original", output_directory: str = "", **_: Any,
) -> str:
    from packages.core.ai.runtime.media import runtime_document_ref_paths

    ctx = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
               task_id=task_id, agent_id=agent_id, conversation_id=conversation_id)
    try:
        voice = _voice(voice)
        source_path = next(iter(await runtime_document_ref_paths(source, entity_id, limit=1)), source_path.strip())
        subtitle_path = next(iter(await runtime_document_ref_paths(subtitle, entity_id, limit=1)), subtitle_path.strip())
        if not source_path:
            raise ValueError("Pick the recording from Knowledge or upload it")
        _, output = await _scope(entity_id, workspace_id, task_id, output_directory)
        ffmpeg, ffprobe = await _runtime_preflight()
        rel, absolute = await _source_path(source_path, ctx)
        media._assert_video_path(absolute)
        report = await media._probe_media_report(ffprobe, absolute)
        duration = float(report["duration_seconds"])
        if not report["has_video"]:
            raise ValueError("This file has no picture. Choose a screen recording or video file")
        if not duration > 0:
            raise ValueError("This recording could not be played. Export it again and retry")
        # A silent recording has nothing to transcribe: its SRT is the AI voice's script.
        if not report["has_audio"] and not (voice == "ai" and subtitle_path):
            raise ValueError("This recording has no audio. Choose the AI voice and add its SRT, "
                             "or provide a narrated recording")
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
            segments = _segments(await _transcribe(ffmpeg, absolute, duration, ctx), duration)
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


def _caption_text(text: str) -> str:
    # Literal captions: strip subtitle control/markup rather than executing it.
    return re.sub(r"<[^>]*>|\{[^}]*\}", "", text).replace("\\", "")


def _subtitle_cues(cuts: list[dict], segments: list[dict], *, line_width: int = 34,
                   timing: dict[int, tuple[float, float]] | None = None) -> tuple[list, list]:
    """Captions in output time; ``timing`` places each source cue where its AI voice speaks."""
    cues, chapters, offset = [], [], 0.0
    for cut in cuts:
        chapters.append({"title": cut["title"], "start_seconds": round(offset, 3),
                         "source_start_seconds": cut["start_seconds"], "source_end_seconds": cut["end_seconds"]})
        for item in segments[cut["first_segment"]:cut["last_segment"] + 1]:
            start, end = timing[item["id"]] if timing else (
                offset + item["start"] - cut["start_seconds"], offset + item["end"] - cut["start_seconds"])
            cues.append(media.SubtitleCue(
                index=len(cues) + 1, start=start, end=end, text=_caption_text(item["text"]), cue_type="dialogue",
            ))
        offset += cut["end_seconds"] - cut["start_seconds"]
    return media._fit_subtitle_cues_to_line_limit(cues, max_chars_per_line=line_width, max_lines=2), chapters


def _voice(value: Any) -> str:
    voice = str(value or "original").strip().lower()
    if voice not in VOICES:
        raise ValueError("voice must be original or ai")
    return voice


def _voice_language(texts: list[str]) -> str:
    """The script's language when its writing system shows it; otherwise the Workspace audio language."""
    sample = "".join(texts)
    for pattern, language in ((r"[぀-ヿ]", "ja"), (r"[가-힯]", "ko"), (r"[一-鿿]", "zh")):
        if re.search(pattern, sample):
            return language
    return ""


def _place_voice(cuts: list[dict], segments: list[dict],
                 durations: dict[int, float]) -> tuple[dict[int, tuple[float, float]], dict[int, float]]:
    """Start each spoken line at its source cue, or right after the previous line.

    A chapter's narration must end within that chapter's footage, so chapter
    markers and pictures stay with what is being said. A chapter that would
    overrun is read faster by one shared factor, up to ``VOICE_MAX_TEMPO``.
    Returns each cue's (start, end) and the tempo its line is read at.
    """
    def place(items: list[dict], offset: float, cut: dict, tempo: float) -> tuple[dict, float]:
        placed, cursor = {}, offset
        for item in items:
            start = max(offset + item["start"] - cut["start_seconds"], cursor)
            placed[item["id"]] = (round(start, 3), round(start + durations[item["id"]] / tempo, 3))
            cursor = start + durations[item["id"]] / tempo + VOICE_GAP_SECONDS
        return placed, cursor - VOICE_GAP_SECONDS

    timing, tempos, offset = {}, {}, 0.0
    for cut in cuts:
        items = segments[cut["first_segment"]:cut["last_segment"] + 1]
        end = offset + cut["end_seconds"] - cut["start_seconds"]
        placed, spoken_end = place(items, offset, cut, 1.0)
        tempo = 1.0
        if spoken_end > end + 0.05:
            fastest, fastest_end = place(items, offset, cut, VOICE_MAX_TEMPO)
            if fastest_end > end + 0.05:
                raise ValueError(f'The AI voice for "{cut["title"]}" runs {fastest_end - end:.1f}s past its footage '
                                 "even when read faster. Choose a longer range, shorten its subtitles, or keep the original voice")
            low, high = 1.0, VOICE_MAX_TEMPO
            for _ in range(20):
                middle = (low + high) / 2
                low, high = (low, middle) if place(items, offset, cut, middle)[1] <= end else (middle, high)
            tempo = round(high, 4)
            placed, _ = place(items, offset, cut, tempo)
        timing.update(placed)
        tempos.update({item["id"]: tempo for item in items})
        offset = end
    return timing, tempos


async def _speak(ctx: dict, items: list[dict], directory: str, ffmpeg: str, ffprobe: str,
                 root: str) -> dict[int, dict]:
    """Voice each source cue through the shared billed TTS boundary and measure its speech."""
    from packages.core.ai.runtime.media import runtime_generate_audio_media

    language = _voice_language([item["text"] for item in items])
    gate = asyncio.Semaphore(VOICE_CONCURRENCY)

    async def line(item: dict) -> tuple[int, dict]:
        async with gate:
            result = json.loads(await runtime_generate_audio_media(
                **ctx, prompt=_caption_text(item["text"]), name=f"{directory}/line-{item['id'] + 1:04d}.mp3",
                params={"purpose": "narration", "format": "mp3", "voice_instructions": VOICE_DIRECTION,
                        **({"language": language} if language else {})},
            ))
            if result.get("status") != "completed" or not result.get("fs_path"):
                raise ValueError("The AI voice could not be generated: "
                                 + str(result.get("error") or result.get("reason") or result.get("code") or "no audio"))
            absolute = await media._resolve_entity_file(root, result["fs_path"])
            probe = await media._probe_media(ffprobe, absolute)
            duration = float(probe.get("duration_seconds") or 0)
            if not probe.get("has_audio") or duration <= 0:
                raise ValueError("The AI voice returned unplayable audio for cue " + str(item["id"]))
            # Providers pad speech with silence; only the spoken part is timed and played.
            _, stderr = await media._run_process([
                ffmpeg, "-hide_banner", "-nostats", "-i", absolute, "-map", "0:a:0",
                "-af", "silencedetect=noise=-45dB:d=0.1", "-f", "null", "-",
            ], timeout_seconds=60)
            silence = media._parse_silence_intervals(stderr, duration_seconds=duration)
            speech_start, speech_end = media._narration_segment_cue_bounds(
                duration_seconds=duration, leading_silence_seconds=silence["leading_silence_seconds"],
                trailing_silence_seconds=silence["trailing_silence_seconds"], padding_seconds=VOICE_EDGE_SECONDS,
            )
            if speech_end - speech_start < 0.05:
                raise ValueError("The AI voice returned silence for cue " + str(item["id"]))
        return item["id"], {"source": result["fs_path"], "duration_seconds": duration,
                            "speech_start": speech_start, "speech_seconds": speech_end - speech_start,
                            "model": result.get("model"), "voice": result.get("voice")}

    return dict(await asyncio.gather(*(line(item) for item in items)))


async def _fit_voice(ctx: dict, spoken: dict[int, dict], tempos: dict[int, float], directory: str) -> None:
    """Level every line, reading a chapter faster only where it must fit its footage."""
    gate = asyncio.Semaphore(VOICE_CONCURRENCY)

    async def fit(key: int, line: dict) -> None:
        tempo = tempos[key]
        async with gate:
            result = json.loads(await media._normalize_audio_loudness_handler(
                **ctx, input_path=line["source"], output_name=f"{directory}/line-{key + 1:04d}.mp3",
                output_format="mp3", target_duration_seconds=line["duration_seconds"] / tempo if tempo > 1 else None,
            ))
        if result.get("status") != "completed" or not result.get("fs_path"):
            raise ValueError("The AI voice could not be levelled: " + str(result.get("error") or result))
        line.update(fs_path=result["fs_path"], document_id=result.get("document_id"), tempo=tempo)

    await asyncio.gather(*(fit(key, line) for key, line in spoken.items()))


@media._with_media_file_access
async def _render_recording_handler(
    *, entity_id: str = "", user_id: str = "", workspace_id: str | None = None,
    task_id: str | None = None, agent_id: str | None = None,
    conversation_id: str | None = None, snapshot_path: str = "",
    snapshot_sha256: str = "", mode: str = "", plan: dict | None = None,
    voice: str = "original", output_directory: str = "", **_: Any,
) -> str:
    ctx = dict(entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
               task_id=task_id, agent_id=agent_id, conversation_id=conversation_id)
    completed = []
    try:
        voice = _voice(voice)
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
        if voice == "original" and not (snapshot.get("source_probe") or {}).get("has_audio", True):
            raise ValueError("This recording has no audio; choose the AI voice")
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
                include_source_audio=voice == "original",
            ))
            spoken, timing, narration = {}, None, []
            if voice == "ai":
                said = [item for cut in batch for item in segments[cut["first_segment"]:cut["last_segment"] + 1]]
                spoken = await _speak(ctx, said, prefix + "/voice/source", ffmpeg, ffprobe, root)
                timing, tempos = _place_voice(
                    batch, segments, {key: line["speech_seconds"] for key, line in spoken.items()})
                await _fit_voice(ctx, spoken, tempos, prefix + "/voice")
                narration = [{"id": f"voice-{key + 1:04d}", "type": "narration", "path": spoken[key]["fs_path"],
                              "source_start": round(spoken[key]["speech_start"] / tempos[key], 3),
                              "start": timing[key][0], "end": timing[key][1]}
                             for key in sorted(spoken)]
            cues, chapters = _subtitle_cues(batch, segments, line_width=line_width, timing=timing)
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
                        "clips": [{"label": "Source picture and original audio" if voice == "original"
                                   else "Source picture", "start": 0,
                                   "end": planned_duration, "path": master["fs_path"],
                                   "document_id": master["document_id"]}],
                        "audio_tracks": narration,
                        "captions": [{"id": f"caption-{cue.index}", "start": cue.start,
                                      "end": cue.end, "text": cue.text} for cue in cues],
                        "markers": [{"time": chapter["start_seconds"], "label": chapter["title"]}
                                    for chapter in chapters],
                        "source": {"snapshot_path": snapshot_path, "snapshot_sha256": snapshot_sha256},
                        "cuts": batch}
            edit = await _save(ctx, prefix + "/timeline.json", json.dumps(timeline, ensure_ascii=False), "timeline")
            final = _result(await media._compose_video_timeline_handler(
                **ctx, timeline_path=edit["fs_path"], output_name=prefix + "/final.mp4",
                subtitle_path=ass["fs_path"], burn_subtitles=True,
                include_source_audio=voice == "original", include_audio=voice == "ai", require_audio=voice == "ai",
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
                                      "-f", "null", "-"], timeout_seconds=max(180.0, expected_duration))
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
                              "voice": [{**spoken[key], "start_seconds": timing[key][0]} for key in sorted(spoken)],
                              "chapters": chapters, "sha256": media._sha256_file(final_abs),
                              "qa": {"full_decode": True, "probe": probe, "subtitles": subtitles}})
        if media._sha256_file(absolute) != snapshot["source_sha256"]:
            raise ValueError("Source video changed while rendering; prepare and plan again")
        delivery = {"status": "completed", "machine_pass": True, "review_required": True,
                    "mode": mode, "voice": voice, "timing_source": snapshot["timing_source"],
                    "final_videos": [{**item["video"], "name": item["title"] + ".mp4"} for item in completed], "outputs": completed,
                    "review_checklist": ["Watch every clip; verify cut context and subtitle accuracy",
                                         *(["Listen to the AI voice: pronunciation, names and pacing against the picture"]
                                           if voice == "ai" else []),
                                         "Check screen text readability and remove sensitive source material",
                                         "Confirm rights and consent before any external use"],
                    "not_included": ["publishing", "original speech" if voice == "ai" else "new narration",
                                     "automatic redaction", "subject tracking"]}
        receipt = await _save(ctx, output + "/delivery.json", json.dumps(delivery, ensure_ascii=False), "qa_report")
        return media._json({**delivery, "receipt": receipt})
    except Exception as exc:
        return media._json({"status": "failed", "code": "recording_render_failed", "error": str(exc),
                            "completed_outputs": completed,
                            "retry": "Fix the reported input/runtime issue and retry. Existing files are retained; retries create a new output directory."})


def get_tools() -> list[tuple[dict, Any]]:
    return [(PREPARE_RECORDING_SCHEMA, _prepare_recording_handler),
            (RENDER_RECORDING_SCHEMA, _render_recording_handler)]
