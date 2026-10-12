"""Frozen inputs and actual-file completion gates for non-code studios.

Production remains on existing governed file/media tools. Neither a model's
completion claim nor a filesystem-only file is a finished delivery.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import json
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
    runtime_recording_output_scope as _scope,
    runtime_recording_source_path as _source_path,
    runtime_recording_tool_schema as _schema,
)

MODES = ["presentation", "podcast", "carousel", "dubbing", "handbook"]
PREPARE_SCHEMA = _schema(
    "prepare_creative_studio", "Freeze a creative production brief and hashes of up to five "
    "uploaded Knowledge sources in a new Workspace run folder. Readable PDF, DOCX, PPTX and "
    "text are supplied as quoted reference material; images/media remain file references. "
    "No network downloads, model calls or publishing.",
    {"brief": {"type": "string", "minLength": 10, "maxLength": 12000},
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
PUBLICATION_SCHEMA = _schema(
    "compose_creative_publication", "Compose an illustrated editable PPTX, matching PDF and speaker/source notes "
    "from structured page content, using the existing native Office engine. No code or coordinate parameters. "
    "Choose 3–16 short pages; generate or supply a real Knowledge illustration first. All files are new, "
    "under the requested production directory. Returns exact paths; final review is still required.",
    {"output_directory": {"type": "string", "minLength": 1},
     "label": {"type": "string", "minLength": 1, "maxLength": 50},
     "theme": {"type": "string", "enum": ["terracotta", "sage", "mustard"]},
     "illustration_path": {"type": "string", "minLength": 1},
     "source_notes": {"type": "string", "minLength": 1, "maxLength": 4000},
     "pages": {"type": "array", "minItems": 3, "maxItems": 16, "items": {
         "type": "object", "additionalProperties": False, "required": ["title", "bullets", "notes"],
         "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 54},
             "bullets": {"type": "array", "minItems": 1, "maxItems": 4,
                         "items": {"type": "string", "minLength": 1, "maxLength": 90}},
             "notes": {"type": "string", "maxLength": 2000}}}}},
    ["output_directory", "label", "theme", "illustration_path", "source_notes", "pages"],
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


def _estimated_dub_srt(text, duration, *, subtitle_format="srt", width=1920, height=1080):
    cue = media.SubtitleCue(index=1, start=0, end=duration, text=text, cue_type="narration",
                           estimated=True, timing_source="proportional-text")
    return media._render_subtitles([cue], subtitle_format=subtitle_format, max_chars_per_line=24,
        canvas_width=width, canvas_height=height,
        style={"max_lines": 2, "font_size": max(16, min(42, round(min(width, height) * 0.04))),
               "margin_v": max(16, round(height * 0.05)), "outline": 2, "alignment": 2})


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


def _validate_publication_layout(pages, *, label):
    # python-pptx native text boxes retain 7.2pt side and 3.6pt vertical
    # margins. Budget the existing 1.1 line spacing and inter-paragraph 12pt
    # gap; no trailing paragraph gap is drawn inside the final text line.
    for index, page in enumerate(pages, 1):
        for field, text, size, width, height in (
            ("label", label.upper(), 12, 864, 26),
            ("title", page["title"], 34, 530, 100),
            ("body", "\n".join(page["bullets"]), 21, 530, 248),
        ):
            try:
                lines, paragraphs = _publication_text_lines(text, size=size, width=width - 14.4)
            except ValueError as exc:
                raise ValueError(f"layout-density: Page {index} {field}: {exc}") from exc
            if lines * size * 1.1 + (paragraphs - 1) * 12 > height - 7.2:
                raise ValueError(
                    f"layout-density: Page {index} {field} is too dense for the fixed publication layout "
                    f"({lines} estimated lines). Shorten this text or split the content across more pages; "
                    "no text was truncated or silently resized."
                )


def _publication_operations(pages, *, label, theme, illustration):
    """A bounded editorial layout, expressed as existing native Office ops."""
    _validate_publication_layout(pages, label=label)
    background, accent = {"terracotta": ("F7F2EA", "AB513A"), "sage": ("F2F5EF", "426549"),
                          "mustard": ("F8F5ED", "A87918")}[theme]
    operations = [{"op": "page.setup", "format": {"width": 960, "height": 540}}]
    for index, page in enumerate(pages):
        slide, shape_id = index + 1, 2
        operations.extend([{"op": "slide.insert", "index": index, "layout_index": 6},
                           {"op": "slide.format", "slide": slide, "format": {"background_color": background}}])
        texts = [
            (label.upper(), 48, 28, 864, 26, 12, accent, True),
            (page["title"], 48, 86, 530, 100, 34, "282828", True),
            ("\n".join(page["bullets"]), 48, 204, 530, 248, 21, "383838", False),
            (f"{slide:02d} / {len(pages):02d}", 48, 486, 864, 25, 11, accent, False),
        ]
        for text, x, y, width, height, size, color, bold in texts:
            operations.append({"op": "textbox.insert", "slide": slide, "text": text,
                "transform": {"x": x, "y": y, "width": width, "height": height}})
            operations.append({"op": "shape.format", "slide": slide, "shape_id": shape_id,
                               "format": {"word_wrap": True}})
            for paragraph in range(len(text.split("\n"))):
                operations.append({"op": "paragraph.format", "slide": slide, "shape_id": shape_id,
                    "index": paragraph, "format": {"font_size": size, "font_color": color,
                        "font_name": "Arial", "bold": bold, "space_after": 12, "line_spacing": 1.1}})
            shape_id += 1
        operations.append({"op": "picture.insert", "slide": slide, "source": illustration,
                           "transform": {"x": 632, "y": 158, "width": 280, "height": 294}, "fit": "contain"})
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
        rel_image, absolute_image = await _source_path(illustration_path, ctx)
        if Path(absolute_image).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Provide a real PNG/JPEG/WebP illustration")
        if Path(absolute_image).stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Illustration exceeds 20 MiB")
        await asyncio.to_thread(_inspect_file, absolute_image)
        illustration = {"path": rel_image, "expected_sha256": media._sha256_file(absolute_image)}
        _, directory = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        call_context = {**ctx, "runtime_envelope": runtime.runtime_envelope, "storage_scope": "workspace"}
        deck_path, pdf_path = directory + "/publication.pptx", directory + "/publication.pdf"
        deck = json.loads(await runtime_generate_document_file(**call_context, name=deck_path, file_type="pptx",
            operations=_publication_operations(pages, label=label, theme=theme, illustration=illustration)))
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
    brief: str = "", source_paths: str = "", mode: str = "", output_directory: str = "", **kwargs: Any,
) -> str:
    try:
        validate(dict(brief=brief, source_paths=source_paths, mode=mode, output_directory=output_directory),
                 PREPARE_SCHEMA["function"]["parameters"])
        if not brief.strip():
            raise ValueError("Provide a meaningful production brief")
        ctx, runtime = _context(entity_id, user_id, workspace_id, task_id, agent_id, conversation_id, kwargs)
        _, output = await _scope(entity_id, ctx["workspace_id"], task_id, output_directory)
        paths = [p.strip() for p in source_paths.splitlines() if p.strip()]
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
        if mode in {"podcast", "dubbing"} and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
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
                     "Accept retains files here only. Revise keeps this version; rerun with the files and change instructions."]
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
            (PUBLICATION_SCHEMA, _compose_creative_publication_handler), (DUB_SCHEMA, _compose_creative_dub_handler)]
