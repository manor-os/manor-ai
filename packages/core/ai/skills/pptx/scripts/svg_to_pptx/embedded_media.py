"""Embedded video support for the editable SVG-to-PPTX export path.

The slide SVG owns the visual composition and reserves video regions with a
``data-video-slot`` attribute.  ``embedded_media.json`` supplies local video
files and playback settings.  This module overlays real PowerPoint media
objects after the native DrawingML deck has been assembled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn
from pptx.util import Emu

VIDEO_CONTENT_TYPES = {
    ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".wmv": "video/x-ms-wmv",
}

_PLAY_MODES = {"on_click", "auto"}
_FIT_MODES = {"contain", "stretch"}


class EmbeddedMediaSpecError(ValueError):
    """Raised when an embedded-media manifest cannot be exported safely."""


def load_embedded_media_manifest(path: Path) -> dict[str, Any]:
    """Load and minimally validate an ``embedded_media.json`` manifest."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddedMediaSpecError(f"Cannot read embedded media manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EmbeddedMediaSpecError("embedded_media.json must contain one JSON object")
    if payload.get("version") != 1:
        raise EmbeddedMediaSpecError("embedded_media.json version must be 1")
    if not isinstance(payload.get("slides"), dict) or not payload["slides"]:
        raise EmbeddedMediaSpecError("embedded_media.json slides must be a non-empty object")
    return payload


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _slide_lookup(svg_files: Iterable[Path]) -> dict[str, tuple[int, Path]]:
    lookup: dict[str, tuple[int, Path]] = {}
    for index, path in enumerate(svg_files, start=1):
        lookup[path.stem] = (index, path)
        lookup[str(index)] = (index, path)
        lookup[f"P{index:02d}"] = (index, path)
    return lookup


def _svg_viewbox_and_slots(svg_path: Path) -> tuple[tuple[float, float], dict[str, dict[str, float]]]:
    try:
        root = ET.parse(svg_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise EmbeddedMediaSpecError(f"Cannot parse SVG video slots in {svg_path}: {exc}") from exc
    values = str(root.get("viewBox") or "").replace(",", " ").split()
    if len(values) != 4:
        raise EmbeddedMediaSpecError(f"SVG {svg_path.name} requires a four-number viewBox")
    try:
        _min_x, _min_y, canvas_width, canvas_height = map(float, values)
    except ValueError as exc:
        raise EmbeddedMediaSpecError(f"SVG {svg_path.name} has an invalid viewBox") from exc
    slots: dict[str, dict[str, float]] = {}
    for element in root.iter():
        slot_id = element.get("data-video-slot") or element.get("data-native-media-slot")
        if not slot_id:
            continue
        try:
            slot = {
                "x": float(element.get("x") or 0),
                "y": float(element.get("y") or 0),
                "width": float(element.get("width") or 0),
                "height": float(element.get("height") or 0),
            }
        except ValueError as exc:
            raise EmbeddedMediaSpecError(
                f"SVG {svg_path.name} video slot {slot_id!r} has non-numeric geometry"
            ) from exc
        if slot["width"] <= 0 or slot["height"] <= 0:
            raise EmbeddedMediaSpecError(f"SVG {svg_path.name} video slot {slot_id!r} must have positive dimensions")
        slots[str(slot_id)] = slot
    return (canvas_width, canvas_height), slots


def _resolve_slot(
    spec: dict[str, Any],
    *,
    svg_path: Path,
    slide_width: int,
    slide_height: int,
) -> tuple[int, int, int, int]:
    (canvas_width, canvas_height), slots = _svg_viewbox_and_slots(svg_path)
    position = _as_dict(spec.get("position"))
    if not position:
        slot_id = str(spec.get("slot") or spec.get("id") or "")
        position = slots.get(slot_id, {})
        if not position:
            raise EmbeddedMediaSpecError(
                f"Video {spec.get('id')!r} has no position and SVG slot {slot_id!r} was not found"
            )
    try:
        x, y, width, height = (float(position[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddedMediaSpecError("Video position requires numeric x, y, width, and height") from exc
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise EmbeddedMediaSpecError("Video position must use non-negative x/y and positive dimensions")
    if x + width > canvas_width or y + height > canvas_height:
        raise EmbeddedMediaSpecError(f"Video {spec.get('id')!r} crosses the SVG canvas boundary")
    return (
        round((x / canvas_width) * slide_width),
        round((y / canvas_height) * slide_height),
        round((width / canvas_width) * slide_width),
        round((height / canvas_height) * slide_height),
    )


def _resolve_local_path(value: Any, manifest_dir: Path, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise EmbeddedMediaSpecError(f"Every video requires a non-empty {field}")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = manifest_dir / path
    path = path.resolve()
    if not path.is_file():
        raise EmbeddedMediaSpecError(f"Video {field} does not exist: {path}")
    return path


def _validate_video_source(path: Path) -> str:
    mime_type = VIDEO_CONTENT_TYPES.get(path.suffix.lower())
    if mime_type is None:
        supported = ", ".join(sorted(VIDEO_CONTENT_TYPES))
        raise EmbeddedMediaSpecError(f"Unsupported embedded video extension {path.suffix!r}; supported: {supported}")
    return mime_type


def _validate_poster(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, ValueError) as exc:
        raise EmbeddedMediaSpecError(f"Video poster is not a readable image: {path}") from exc


def _probe_video_dimensions(video_path: Path) -> tuple[int, int]:
    if shutil.which("ffprobe") is None:
        raise EmbeddedMediaSpecError("ffprobe is required for fit='contain'; install ffmpeg or use fit='stretch'")
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        stream = _as_list(json.loads(result.stdout or "{}").get("streams"))[0]
        width, height = int(stream["width"]), int(stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError("non-positive video dimensions")
        return width, height
    except (IndexError, KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise EmbeddedMediaSpecError(f"Cannot read video dimensions: {video_path}") from exc


def _contain_position(box: tuple[int, int, int, int], video_dimensions: tuple[int, int]) -> tuple[int, int, int, int]:
    left, top, width, height = box
    video_width, video_height = video_dimensions
    scale = min(width / video_width, height / video_height)
    fitted_width = max(1, round(video_width * scale))
    fitted_height = max(1, round(video_height * scale))
    return (
        left + (width - fitted_width) // 2,
        top + (height - fitted_height) // 2,
        fitted_width,
        fitted_height,
    )


def _create_poster(video_path: Path, output_path: Path, poster_time: float) -> Path:
    if shutil.which("ffmpeg") is None:
        raise EmbeddedMediaSpecError("ffmpeg is required to create a video poster; provide poster or install ffmpeg")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-ss",
                str(poster_time),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-y",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EmbeddedMediaSpecError(f"Cannot create poster from video: {video_path}") from exc
    _validate_poster(output_path)
    return output_path


def _effective_spec(raw_spec: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    spec = {**defaults, **_as_dict(raw_spec)}
    video_id = str(spec.get("id") or "").strip()
    if not video_id:
        raise EmbeddedMediaSpecError("Every embedded video requires a non-empty id")
    media_type = str(spec.get("type") or "video").strip().lower()
    if media_type != "video":
        raise EmbeddedMediaSpecError(f"Embedded media {video_id!r} has unsupported type {media_type!r}")
    play = str(spec.get("play") or "on_click").strip().lower().replace("-", "_")
    if play not in _PLAY_MODES:
        raise EmbeddedMediaSpecError(f"Video {video_id!r} play must be auto or on_click")
    fit = str(spec.get("fit") or "contain").strip().lower()
    if fit not in _FIT_MODES:
        raise EmbeddedMediaSpecError(f"Video {video_id!r} fit must be contain or stretch")
    try:
        volume = float(spec.get("volume", 80))
        poster_time = float(spec.get("poster_time", 0.25))
    except (TypeError, ValueError) as exc:
        raise EmbeddedMediaSpecError(f"Video {video_id!r} volume and poster_time must be numeric") from exc
    if not 0 <= volume <= 100:
        raise EmbeddedMediaSpecError(f"Video {video_id!r} volume must be between 0 and 100")
    if poster_time < 0:
        raise EmbeddedMediaSpecError(f"Video {video_id!r} poster_time must be non-negative")
    spec.update(id=video_id, type="video", play=play, fit=fit, volume=volume, poster_time=poster_time)
    return spec


def _set_playback(shape, *, play: str, loop: bool, volume: float) -> None:
    shape_id = str(shape.shape_id)
    videos = shape.part.slide.element.xpath(f".//p:video[p:cMediaNode/p:tgtEl/p:spTgt[@spid='{shape_id}']]")
    if not videos:
        raise EmbeddedMediaSpecError(f"Cannot find PowerPoint timing node for video shape {shape_id}")
    media_node = videos[-1].find(qn("p:cMediaNode"))
    if media_node is None:
        raise EmbeddedMediaSpecError(f"Cannot find PowerPoint media node for video shape {shape_id}")
    media_node.set("vol", str(round(volume * 1000)))
    timing_node = media_node.find(qn("p:cTn"))
    if timing_node is None:
        raise EmbeddedMediaSpecError(f"Cannot find PowerPoint timing node for video shape {shape_id}")
    if loop:
        timing_node.set("repeatCount", "indefinite")
    condition_list = timing_node.find(qn("p:stCondLst"))
    condition = condition_list.find(qn("p:cond")) if condition_list is not None else None
    if condition is None:
        raise EmbeddedMediaSpecError(f"Cannot find PowerPoint start condition for video shape {shape_id}")
    condition.set("delay", "0" if play == "auto" else "indefinite")


def _validated_specs(
    manifest: dict[str, Any],
    svg_files: Iterable[Path],
    manifest_dir: Path,
) -> list[tuple[int, Path, dict[str, Any], Path, str, Path | None]]:
    lookup = _slide_lookup(svg_files)
    defaults = _as_dict(manifest.get("defaults"))
    resolved_specs: list[tuple[int, Path, dict[str, Any], Path, str, Path | None]] = []
    for slide_key, raw_specs in _as_dict(manifest.get("slides")).items():
        resolved = lookup.get(str(slide_key))
        if resolved is None:
            raise EmbeddedMediaSpecError(f"Unknown slide key in embedded_media.json: {slide_key}")
        specs = _as_list(raw_specs)
        if not specs:
            raise EmbeddedMediaSpecError(f"Slide {slide_key} must declare at least one video")
        slide_index, svg_path = resolved
        ids: set[str] = set()
        for raw_spec in specs:
            spec = _effective_spec(raw_spec, defaults)
            if spec["id"] in ids:
                raise EmbeddedMediaSpecError(f"Slide {slide_key} repeats embedded media id {spec['id']!r}")
            ids.add(spec["id"])
            source = _resolve_local_path(spec.get("source"), manifest_dir, "source")
            mime_type = _validate_video_source(source)
            poster = None
            if spec.get("poster"):
                poster = _resolve_local_path(spec.get("poster"), manifest_dir, "poster")
                _validate_poster(poster)
            _resolve_slot(spec, svg_path=svg_path, slide_width=1_280_000, slide_height=720_000)
            resolved_specs.append((slide_index, svg_path, spec, source, mime_type, poster))
    return resolved_specs


def expected_embedded_video_counts(
    manifest: dict[str, Any],
    svg_files: Iterable[Path],
    manifest_dir: Path,
) -> dict[int, int]:
    """Validate a manifest and return required video counts by slide number."""
    counts: dict[int, int] = {}
    for slide_index, _svg, _spec, _source, _mime, _poster in _validated_specs(manifest, svg_files, manifest_dir):
        counts[slide_index] = counts.get(slide_index, 0) + 1
    return counts


def apply_embedded_media(
    pptx_path: Path,
    manifest: dict[str, Any],
    svg_files: list[Path],
    *,
    manifest_dir: Path,
) -> dict[str, Any]:
    """Overlay real PowerPoint video objects and replace ``pptx_path`` atomically."""
    resolved_specs = _validated_specs(manifest, svg_files, manifest_dir.resolve())
    presentation = Presentation(str(pptx_path))
    if len(presentation.slides) != len(svg_files):
        raise EmbeddedMediaSpecError(f"PPTX has {len(presentation.slides)} slides for {len(svg_files)} SVG pages")

    video_count = 0
    counts_by_slide: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="manor-pptx-posters-") as poster_dir_name:
        poster_dir = Path(poster_dir_name)
        for item_index, (slide_index, svg_path, spec, source, mime_type, poster) in enumerate(resolved_specs, start=1):
            slide = presentation.slides[slide_index - 1]
            box = _resolve_slot(
                spec,
                svg_path=svg_path,
                slide_width=int(presentation.slide_width),
                slide_height=int(presentation.slide_height),
            )
            if spec["fit"] == "contain":
                box = _contain_position(box, _probe_video_dimensions(source))
            if poster is None:
                poster = _create_poster(
                    source,
                    poster_dir / f"poster-{item_index}.png",
                    spec["poster_time"],
                )
            left, top, width, height = map(Emu, box)
            movie = slide.shapes.add_movie(
                str(source),
                left,
                top,
                width,
                height,
                str(poster),
                mime_type,
            )
            movie.name = f"Manor Embedded Video · {spec['id']}"
            _set_playback(
                movie,
                play=spec["play"],
                loop=bool(spec.get("loop", False)),
                volume=spec["volume"],
            )
            video_count += 1
            key = f"P{slide_index:02d}"
            counts_by_slide[key] = counts_by_slide.get(key, 0) + 1

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{pptx_path.stem}.embedded-media-",
            suffix=".pptx",
            dir=pptx_path.parent,
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            presentation.save(str(temp_path))
            verified = Presentation(str(temp_path))
            actual_count = sum(
                1 for slide in verified.slides for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.MEDIA
            )
            if actual_count < video_count:
                raise EmbeddedMediaSpecError(
                    f"Embedded video verification found {actual_count}, expected at least {video_count}"
                )
            os.replace(temp_path, pptx_path)
        finally:
            temp_path.unlink(missing_ok=True)

    return {
        "embedded_video_count": video_count,
        "embedded_video_counts_by_slide": counts_by_slide,
    }
