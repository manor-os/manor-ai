"""How App blocks present media: the shapes a field's media companions take, and what a file says about itself.

Pure functions over what a Workflow saved (state, run variables) and a Document's
own metadata. Nothing here reads the database; ``workspace_app_data`` calls them.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from packages.core.contracts.project_display import display_file

# How much one field shows: clips in a strip, segments on a timeline, chapters, lines of a transcript.
CLIPS_FIELD_LIMIT = 24
TIMELINE_SEGMENT_LIMIT = 200
CHAPTER_LIMIT = 60
TRANSCRIPT_SEGMENT_LIMIT = 600
_TEXT_LIMIT = 500
_LABEL_LIMIT = 120
_PROMPT_LIMIT = 1200
# A media length past this (a day) is not a time on a player.
_SECONDS_LIMIT = 86_400
_CLOCK = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")
_SIZE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$")
_RATIO = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*[:/]\s*(\d{1,3}(?:\.\d+)?)\s*$")


def media_seconds(value: Any) -> float | None:
    """A moment on a player: seconds as a number, or a clock ("1:05", "01:02:03.5"); None otherwise."""
    if isinstance(value, bool):
        return None
    seconds: float | None = None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        match = _CLOCK.fullmatch(text)
        if match:
            hours, minutes, rest = match.groups()
            if float(rest) >= 60 or (hours is not None and int(minutes) >= 60):
                return None
            seconds = int(hours or 0) * 3600 + int(minutes) * 60 + float(rest)
        else:
            try:
                seconds = float(text)
            except ValueError:
                return None
    if seconds is None or seconds != seconds or not 0 <= seconds <= _SECONDS_LIMIT:
        return None
    return round(seconds, 3)


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[:limit - 1].rstrip()}…"


def _first(entry: dict[str, Any], *keys: str) -> Any:
    return next((entry[key] for key in keys if entry.get(key) not in (None, "")), None)


def timeline_segments(value: Any) -> list[dict[str, Any]]:
    """An edit's segments in time order: ``{start, end, label?, track?}``, each with a length of its own.

    A segment gives ``end`` or ``duration``; one with neither, or that ends before it
    starts, is not a segment. ``track`` names its lane (video, voice, music)."""
    segments = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        start = media_seconds(_first(entry, "start", "at", "time"))
        end = media_seconds(entry.get("end"))
        length = media_seconds(entry.get("duration"))
        if end is None and start is not None and length is not None:
            end = min(start + length, _SECONDS_LIMIT)
        if start is None or end is None or end <= start:
            continue
        label = _text(_first(entry, "label", "title", "name", "text"), _LABEL_LIMIT)
        track = _text(_first(entry, "track", "lane", "kind"), 40)
        segments.append({"start": start, "end": end, **({"label": label} if label else {}),
                         **({"track": track} if track else {})})
        if len(segments) >= TIMELINE_SEGMENT_LIMIT:
            break
    return sorted(segments, key=lambda segment: (segment["start"], segment["end"]))


def chapter_marks(value: Any) -> list[dict[str, Any]]:
    """A recording's chapters in time order: ``{at, title}``; one without a time or a title is left out."""
    marks: dict[float, dict[str, Any]] = {}
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        at = media_seconds(_first(entry, "at", "start", "time"))
        title = _text(_first(entry, "title", "label", "name", "text"), _LABEL_LIMIT)
        if at is not None and title and at not in marks:
            marks[at] = {"at": at, "title": title}
        if len(marks) >= CHAPTER_LIMIT:
            break
    return [marks[at] for at in sorted(marks)]


def transcript_segments(value: Any) -> list[dict[str, Any]]:
    """What is said, line by line: ``{start, end?, text, speaker?}`` in time order."""
    lines = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        start = media_seconds(_first(entry, "start", "at", "time"))
        text = _text(_first(entry, "text", "line", "content"), _TEXT_LIMIT)
        if start is None or not text:
            continue
        end = media_seconds(entry.get("end"))
        speaker = _text(_first(entry, "speaker", "voice", "role"), 60)
        lines.append({"start": start, **({"end": end} if end is not None and end > start else {}), "text": text,
                      **({"speaker": speaker} if speaker else {})})
        if len(lines) >= TRANSCRIPT_SEGMENT_LIMIT:
            break
    return sorted(lines, key=lambda line: line["start"])


def media_file_list(value: Any, limit: int = CLIPS_FIELD_LIMIT) -> list[dict[str, Any]]:
    """The files a ``clips`` field holds, each once: ``{document_id, href, label?}``."""
    files, seen = [], set()
    for part in value if isinstance(value, list) else [value]:
        file = display_file(part)
        if not file or file["document_id"] in seen:
            continue
        seen.add(file["document_id"])
        files.append({"document_id": file["document_id"], "href": f"/viewer/{file['document_id']}",
                      **({"label": file["label"]} if file.get("label") else {})})
        if len(files) >= limit:
            break
    return files


def media_companions(configured: dict[str, Any], lookup: Callable[[list[str]], Any],
                     public_href: Callable[[Any], str | None]) -> dict[str, Any]:
    """What a media field shows with its file: what it is compared with, its captions, words and chapters.

    A companion file is named only by its Document id (the page opens it through
    the documents API, which checks it may be read), or for a picture by a public
    address; a companion that names neither shows nothing."""
    shown: dict[str, Any] = {}
    for option in ("aspect", "layout"):
        if configured.get(option):
            shown[option] = configured[option]
    if configured.get("transparent"):
        shown["transparent"] = True
    if configured.get("compare_field"):
        held = lookup(configured["compare_field"])
        file = display_file(held[0] if isinstance(held, list) and held else held)
        address = public_href(held) if configured["format"] == "image" and not file else None
        if file:
            shown["compare"] = {"document_id": file["document_id"], "href": f"/viewer/{file['document_id']}",
                                **({"label": file["label"]} if file.get("label") else {}), "mode": configured["compare"]}
        elif address:
            shown["compare"] = {"image_url": address, "href": address, "mode": configured["compare"]}
    if configured.get("captions_field"):
        file = display_file(lookup(configured["captions_field"]))
        if file:
            shown["captions_document_id"] = file["document_id"]
    if configured.get("transcript_field"):
        held = lookup(configured["transcript_field"])
        file = display_file(held)
        if file:
            shown["transcript_document_id"] = file["document_id"]
        elif transcript := transcript_segments(held):
            shown["transcript"] = transcript
    if configured.get("chapters_field"):
        if chapters := chapter_marks(lookup(configured["chapters_field"])):
            shown["chapters"] = chapters
    return shown


def _dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 1 <= value <= 100_000 else None


def media_facts(metadata: Any) -> dict[str, Any]:
    """The pixel size, shape and length a Document's own metadata records, where it records them.

    Generated media keeps its request in ``generation.params`` (``size: "1024x1536"``,
    ``aspect_ratio: "9:16"``, ``resolution: "1080x1920"``, ``duration``); a file
    that says nothing gives nothing, and the page measures what it loads instead."""
    meta = metadata if isinstance(metadata, dict) else {}
    generation = meta.get("generation") if isinstance(meta.get("generation"), dict) else {}
    params = generation.get("params") if isinstance(generation.get("params"), dict) else {}
    places = [meta, meta.get("media") if isinstance(meta.get("media"), dict) else {}, generation, params]
    facts: dict[str, Any] = {}
    for place in places:
        width, height = _dimension(place.get("width")), _dimension(place.get("height"))
        if width and height and "width" not in facts:
            facts.update(width=width, height=height)
        for key in ("size", "resolution", "dimensions"):
            match = _SIZE.fullmatch(place.get(key)) if isinstance(place.get(key), str) else None
            if match and "width" not in facts:
                facts.update(width=int(match.group(1)), height=int(match.group(2)))
        ratio = _RATIO.fullmatch(place.get("aspect_ratio")) if isinstance(place.get("aspect_ratio"), str) else None
        if ratio and float(ratio.group(1)) > 0 and float(ratio.group(2)) > 0 and "aspect" not in facts:
            facts["aspect"] = f"{ratio.group(1)}:{ratio.group(2)}"
        for key in ("duration_seconds", "duration"):
            seconds = media_seconds(place.get(key))
            if seconds and "duration" not in facts:
                facts["duration"] = seconds
    return facts


def media_details(metadata: Any, *, source: str | None, created_at: str | None) -> dict[str, Any]:
    """How a file was made, in words a person reads: where it came from, the model and the prompt."""
    meta = metadata if isinstance(metadata, dict) else {}
    generation = meta.get("generation") if isinstance(meta.get("generation"), dict) else {}
    details = {
        "source": _text(source, 40),
        "model": _text(generation.get("model"), 80),
        "provider": _text(generation.get("provider"), 60),
        "prompt": _text(generation.get("prompt"), _PROMPT_LIMIT),
        "created_at": created_at,
    }
    return {key: value for key, value in details.items() if value}
