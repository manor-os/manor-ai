"""Describe image, video and audio files so Knowledge can find them by content.

Text extraction returns nothing for media, so an uploaded photo, recording or
clip is findable by its file name only. This module turns one media file into
a short searchable record: what it shows, the text visible in it, what is said
in it, and bilingual search keywords. It costs at most one vision call per
file (a video is summarised from one contact sheet of sampled frames) plus a
transcription of any speech, and is run once per file version in the
background, never on a search or chat path.

The model calls are injected (``Complete`` / ``Transcribe``) so the caller owns
model choice and billing; this module owns preparation, prompts and parsing.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Bump when the prompt or the stored shape changes; stored results with an
# older version are re-analysed when their file is next synced.
MEDIA_ANALYSIS_VERSION = 1

IMAGE_MAX_SIDE = 1024
# Icons, emoji and tracking pixels carry nothing worth a model call.
IMAGE_MIN_SIDE = 64
VIDEO_SHEET_FRAMES = 9
VIDEO_SHEET_CELL = 512
# Transcribe at most this much speech per file; long recordings keep their
# opening, which is what a search for them usually remembers.
TRANSCRIBE_MAX_SECONDS = 30 * 60
TRANSCRIPT_MAX_CHARS = 20_000
# Below this peak level the track is silent; no transcription is attempted.
SILENCE_PEAK_DB = -50.0
_PROCESS_TIMEOUT_SECONDS = 180

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif"})
VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "webm", "mkv", "avi"})
AUDIO_EXTENSIONS = frozenset({"mp3", "m4a", "wav", "aac", "ogg", "oga", "opus", "flac", "weba"})

# (messages, max_tokens) -> (content, usage)
Complete = Callable[[list[dict[str, Any]], int], Awaitable[tuple[str, dict[str, Any]]]]
# (mp3 bytes, duration_seconds) -> (transcript, usage)
Transcribe = Callable[[bytes, float], Awaitable[tuple[str, dict[str, Any]]]]


class MediaAnalysisSkipped(Exception):
    """The file has nothing a model could describe (too small, silent, unreadable)."""


def media_kind(mime_type: str | None, file_type: str | None = None, name: str | None = None) -> str | None:
    """``image`` / ``video`` / ``audio`` for analysable media, else ``None``."""
    mime = str(mime_type or "").lower()
    ext = str(file_type or "").lower().lstrip(".") or os.path.splitext(str(name or ""))[1].lower().lstrip(".")
    if mime.startswith("image/") and mime != "image/svg+xml" or ext in IMAGE_EXTENSIONS:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    if mime.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio"
    return None


_JSON_SHAPE = """Return one JSON object and nothing else:
{
  "kind": "%(kinds)s",
  "summary_zh": "一句中文，说明这是什么文件、主要内容（40字以内）",
  "summary_en": "One English sentence saying what the file is and its main content (max 30 words)",
  "description_zh": "2-4 句中文：主体、人物（角色、穿着、动作）、场景、风格、明显的品牌或产品",
  "description_en": "The same description in English",
  "visible_text": "%(text_hint)s",
  "keywords": ["12-24 short search terms a person might type to find this file, each concept in BOTH Chinese and English, e.g. \\"饮品机\\", \\"beverage machine\\"; include category words, brands, product names, people's roles, places, and any important names, numbers or dates"]
}
Rules: describe only what is actually there. Name a brand, product model or company only when its name or logo text is visible (or spoken); otherwise use a generic description such as "饮品机 / beverage machine" — never guess a brand. Never guess who a person is unless their name is written or spoken. Copy names, numbers, prices, dates, phone numbers and emails exactly. Use "" or [] when something is absent."""

_IMAGE_KINDS = "photo|product_photo|poster|flyer|screenshot|document|receipt|invoice|menu|chart|logo|whiteboard|business_card|illustration|other"
_VIDEO_KINDS = "advertisement|product_demo|tutorial|customer_story|talking_head|event|screen_recording|footage|other"
_AUDIO_KINDS = "voice_message|phone_call|meeting|interview|presentation|podcast|music|other"

IMAGE_PROMPT = (
    "You index files uploaded to a business's knowledge base so people can later find them by typing words. "
    "Look at the image and describe it factually.\n"
    + _JSON_SHAPE % {"kinds": _IMAGE_KINDS, "text_hint": "All legible text in the image, verbatim, original language, reading order, one line per line of text; \"\" if none"}
)
VIDEO_PROMPT = (
    "You index files uploaded to a business's knowledge base so people can later find them by typing words. "
    "The image is a contact sheet of frames sampled from one video in time order (left to right, top to bottom); "
    "each frame is labelled with its time. The video's speech transcript, if any, follows. Describe the whole video factually.\n"
    + _JSON_SHAPE % {"kinds": _VIDEO_KINDS, "text_hint": "Important on-screen text (titles, captions, product names, prices), verbatim; \"\" if none"}
)
AUDIO_PROMPT = (
    "You index files uploaded to a business's knowledge base so people can later find them by typing words. "
    "Below is the speech transcript of one audio file. Describe the recording factually: who seems to be speaking "
    "(role, not identity, unless they say their name), the purpose, and the key facts, requests, numbers and dates.\n"
    + _JSON_SHAPE % {"kinds": _AUDIO_KINDS, "text_hint": "\"\""}
).replace("主体、人物（角色、穿着、动作）、场景、风格、明显的品牌或产品", "说话人角色、目的、关键信息、请求、数字和日期")


# ── preparation ──────────────────────────────────────────────────────────


def prepare_image(data: bytes, *, max_side: int = IMAGE_MAX_SIDE) -> tuple[bytes, int, int]:
    """JPEG bytes no larger than ``max_side``, plus the original size.

    Raises ``MediaAnalysisSkipped`` for unreadable or icon-sized images.
    """
    from PIL import Image, ImageOps

    from packages.core.services.image_formats import register_heif_decoder

    register_heif_decoder()
    try:
        with Image.open(io.BytesIO(data)) as opened:
            img = ImageOps.exif_transpose(opened)
            width, height = img.size
            if min(width, height) < IMAGE_MIN_SIDE:
                raise MediaAnalysisSkipped(f"image too small ({width}x{height})")
            if img.mode != "RGB":
                background = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            img.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), width, height
    except MediaAnalysisSkipped:
        raise
    except Exception as exc:
        raise MediaAnalysisSkipped(f"unreadable image: {exc}") from exc


async def _run(args: list[str], *, timeout: float = _PROCESS_TIMEOUT_SECONDS) -> tuple[bytes, bytes, int]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return stdout, stderr, process.returncode or 0


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} is not installed")
    return path


async def probe(path: str) -> dict[str, Any]:
    """Duration, video size and which stream types the file carries."""
    stdout, _stderr, _code = await _run([
        _tool("ffprobe"), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height,disposition", "-of", "json", path,
    ], timeout=60)
    data = json.loads(stdout or b"{}")
    streams = data.get("streams") or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration_seconds": round(duration, 2),
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "has_video": video is not None,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


async def audio_peak_db(path: str, *, max_seconds: float = TRANSCRIBE_MAX_SECONDS) -> float:
    """Peak level of the first audio track in dBFS (``-inf`` when silent)."""
    _stdout, stderr, _code = await _run([
        _tool("ffmpeg"), "-nostats", "-hide_banner", "-t", str(max_seconds), "-i", path,
        "-vn", "-af", "volumedetect", "-f", "null", "-",
    ])
    match = re.search(rb"max_volume:\s*(-?[\d.]+|-inf) dB", stderr)
    if not match:
        return float("-inf")
    value = match.group(1).decode()
    return float("-inf") if value == "-inf" else float(value)


async def extract_speech_audio(path: str, *, max_seconds: float = TRANSCRIBE_MAX_SECONDS) -> bytes:
    """Mono 16 kHz MP3 of the first ``max_seconds`` — what speech-to-text needs, about 6 MB per 30 minutes."""
    with tempfile.TemporaryDirectory(prefix="manor-media-analysis-") as tmp:
        out = os.path.join(tmp, "speech.mp3")
        _stdout, stderr, code = await _run([
            _tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-t", str(max_seconds), "-i", path,
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k", out,
        ])
        if code != 0 or not os.path.isfile(out):
            raise RuntimeError(f"audio extraction failed: {stderr[-300:].decode(errors='replace')}")
        with open(out, "rb") as handle:
            return handle.read()


async def video_contact_sheet(path: str, duration: float, *, frames: int = VIDEO_SHEET_FRAMES) -> bytes:
    """One JPEG grid of ``frames`` evenly spaced frames, each stamped with its time."""
    from PIL import Image, ImageDraw

    columns = 3 if frames > 4 else 2
    times = [duration * (index + 0.5) / frames for index in range(frames)] if duration > 0 else [0.0]
    cells: list[tuple[float, Image.Image]] = []
    with tempfile.TemporaryDirectory(prefix="manor-media-analysis-") as tmp:
        for index, at in enumerate(times):
            out = os.path.join(tmp, f"f{index}.jpg")
            await _run([
                _tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", path,
                "-frames:v", "1", "-vf", f"scale={VIDEO_SHEET_CELL}:{VIDEO_SHEET_CELL}:force_original_aspect_ratio=decrease",
                "-q:v", "4", out,
            ], timeout=60)
            if os.path.isfile(out):
                with Image.open(out) as frame:
                    cells.append((at, frame.convert("RGB")))
    if not cells:
        raise MediaAnalysisSkipped("no decodable video frames")
    cell_w = max(image.width for _at, image in cells)
    cell_h = max(image.height for _at, image in cells)
    rows = (len(cells) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for index, (at, image) in enumerate(cells):
        x, y = (index % columns) * cell_w, (index // columns) * cell_h
        sheet.paste(image, (x, y))
        label = f"{int(at // 60)}:{int(at % 60):02d}"
        draw.rectangle((x, y, x + 12 + 9 * len(label), y + 22), fill=(0, 0, 0))
        draw.text((x + 6, y + 5), label, fill=(255, 255, 0))
    sheet.thumbnail((1536, 1536))
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


# ── model calls ──────────────────────────────────────────────────────────


def _image_message(prompt: str, jpeg: bytes, extra_text: str = "") -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt + (f"\n\n{extra_text}" if extra_text else "")}]
    content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}})
    return [{"role": "user", "content": content}]


def parse_analysis(raw: str) -> dict[str, Any]:
    """The model's JSON object, tolerant of code fences and surrounding prose."""
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not an object")

    def clean(value: Any, limit: int) -> str:
        # Some models return a list of lines where a string was asked for.
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(item) for item in value if item is not None)
        return str(value or "").strip()[:limit]

    keywords = data.get("keywords") or []
    if isinstance(keywords, str):
        keywords = re.split(r"[,，;；\n]", keywords)
    return {
        "kind": clean(data.get("kind"), 40),
        "summary_zh": clean(data.get("summary_zh"), 200),
        "summary_en": clean(data.get("summary_en"), 300),
        "description_zh": clean(data.get("description_zh"), 1200),
        "description_en": clean(data.get("description_en"), 2000),
        "visible_text": clean(data.get("visible_text"), 6000),
        "keywords": [k for k in dict.fromkeys(clean(item, 60) for item in keywords if clean(item, 60))][:40],
    }


def _merge_usage(total: dict[str, Any], usage: dict[str, Any] | None) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance((usage or {}).get(key), (int, float)):
            total[key] = total.get(key, 0) + usage[key]
    cost = (usage or {}).get("cost_usd")
    if isinstance(cost, (int, float)):
        total["cost_usd"] = round(total.get("cost_usd", 0.0) + float(cost), 8)


async def _describe(complete: Complete, messages: list[dict[str, Any]], usage: dict[str, Any]) -> dict[str, Any]:
    content, call_usage = await complete(messages, 1500)
    _merge_usage(usage, call_usage)
    try:
        return parse_analysis(content)
    except (ValueError, json.JSONDecodeError):
        # One retry: cheap models occasionally truncate or wrap the object.
        content, call_usage = await complete(messages, 2000)
        _merge_usage(usage, call_usage)
        return parse_analysis(content)


async def _transcript(path: str, info: dict[str, Any], transcribe: Transcribe, usage: dict[str, Any]) -> tuple[str, str]:
    """(transcript, speech status) where status is ``speech`` / ``silent`` / ``no_speech``."""
    if not info.get("has_audio"):
        return "", "silent"
    if await audio_peak_db(path) < SILENCE_PEAK_DB:
        return "", "silent"
    audio = await extract_speech_audio(path)
    text, call_usage = await transcribe(audio, min(float(info.get("duration_seconds") or 0.0), TRANSCRIBE_MAX_SECONDS))
    _merge_usage(usage, call_usage)
    usage["transcribed_seconds"] = round(min(float(info.get("duration_seconds") or 0.0), TRANSCRIBE_MAX_SECONDS), 2)
    text = re.sub(r"\s+", " ", text or "").strip()
    # A few words over minutes of audio is music or noise misheard as speech.
    duration = float(info.get("duration_seconds") or 0.0)
    if len(text) < 4 or (duration > 30 and len(text) < duration / 15):
        return "", "no_speech"
    return text[:TRANSCRIPT_MAX_CHARS], "speech"


async def analyze_media(
    path: str,
    kind: str,
    *,
    complete: Complete,
    transcribe: Transcribe | None = None,
) -> dict[str, Any]:
    """Analyse one file. Returns the stored record (see ``search_text`` for the indexed text)."""
    usage: dict[str, Any] = {}
    record: dict[str, Any] = {"version": MEDIA_ANALYSIS_VERSION, "media_kind": kind}
    if kind == "image":
        with open(path, "rb") as handle:
            jpeg, width, height = prepare_image(handle.read())
        record.update({"width": width, "height": height})
        record.update(await _describe(complete, _image_message(IMAGE_PROMPT, jpeg), usage))
    elif kind == "video":
        info = await probe(path)
        if not info["has_video"] and not info["has_audio"]:
            raise MediaAnalysisSkipped("no decodable video or audio stream")
        record.update({key: info[key] for key in ("duration_seconds", "width", "height")})
        transcript, speech = ("", "silent") if transcribe is None else await _transcript(path, info, transcribe, usage)
        record.update({"speech": speech, "transcript": transcript})
        if info["has_video"]:
            sheet = await video_contact_sheet(path, info["duration_seconds"])
            extra = f"Transcript:\n{transcript[:6000]}" if transcript else "The video has no speech."
            record.update(await _describe(complete, _image_message(VIDEO_PROMPT, sheet, extra), usage))
        elif transcript:
            record.update(await _describe(complete, [{"role": "user", "content": f"{AUDIO_PROMPT}\n\nTranscript:\n{transcript[:12000]}"}], usage))
    elif kind == "audio":
        if transcribe is None:
            raise MediaAnalysisSkipped("no transcription available")
        info = await probe(path)
        if not info["has_audio"]:
            raise MediaAnalysisSkipped("no decodable audio stream")
        record["duration_seconds"] = info["duration_seconds"]
        transcript, speech = await _transcript(path, info, transcribe, usage)
        record.update({"speech": speech, "transcript": transcript})
        if transcript:
            record.update(await _describe(complete, [{"role": "user", "content": f"{AUDIO_PROMPT}\n\nTranscript:\n{transcript[:12000]}"}], usage))
        else:
            label = ("静音音频", "silent audio") if speech == "silent" else ("无人声音频（音乐或环境声）", "audio without speech (music or ambient sound)")
            record.update({"kind": "music" if speech == "no_speech" else "other", "summary_zh": label[0], "summary_en": label[1],
                           "keywords": ["音频", "audio", label[0], label[1]] + (["音乐", "music", "背景音乐", "background music"] if speech == "no_speech" else [])})
    else:
        raise MediaAnalysisSkipped(f"not a media kind: {kind}")
    record["usage"] = usage
    return record


def search_text(record: dict[str, Any] | None) -> str:
    """The text a search matches against, assembled from a stored record."""
    if not record:
        return ""
    parts = [
        record.get("summary_zh"), record.get("summary_en"),
        record.get("description_zh"), record.get("description_en"),
        record.get("visible_text"),
        " ".join(record.get("keywords") or []),
        record.get("transcript"),
    ]
    return "\n".join(str(part).strip() for part in parts if part and str(part).strip())


__all__ = [
    "MEDIA_ANALYSIS_VERSION",
    "MediaAnalysisSkipped",
    "analyze_media",
    "media_kind",
    "parse_analysis",
    "prepare_image",
    "search_text",
]
