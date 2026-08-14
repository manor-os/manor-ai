#!/usr/bin/env python3
"""Machine-verifiable final quality gate for Manor-generated PPTX files.

The SVG checker validates authoring input.  This script validates the exported
Office artifact that users actually receive.  It deliberately complements,
rather than replaces, full-size visual inspection of every rendered slide.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops, ImageStat
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from layout_plan import load_layout_plan, validate_layout_plan
from svg_quality_checker import SVGQualityChecker
from svg_to_pptx.native_charts import (
    NativeChartSpecError,
    expected_native_chart_counts,
    load_native_chart_manifest,
)
from svg_to_pptx.embedded_media import (
    EmbeddedMediaSpecError,
    expected_embedded_video_counts,
    load_embedded_media_manifest,
)


REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
EMU_PER_INCH = 914400
URL_RE = re.compile(r"https?://[^\s<>)\]]+", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}|\b(?:lorem ipsum|todo|tbd)\b", re.IGNORECASE)
PROCESS_COPY_RE = re.compile(
    r"\b(?:speaker notes?|slide plan|image prompt|layout rationale|talk track)\b",
    re.IGNORECASE,
)
VISUAL_RECEIPT_CHECKS = (
    "title_wrapping",
    "overflow_overlap",
    "font_substitution",
    "image_crops",
    "chart_data_fidelity",
    "nested_slide_screenshot",
    "duplicate_or_stale_layers",
    "footer_callout_duplicates",
    "adjacent_layout_repetition",
    "connector_routing",
    "text_containment",
)


@dataclass
class GateResult:
    pptx: str
    mode: str
    slide_count: int = 0
    metrics: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def payload(self, min_score: int) -> dict[str, object]:
        score = max(0, 100 - 20 * len(self.errors) - 2 * len(self.warnings))
        passed = not self.errors and score >= min_score
        return {
            "status": "pass" if passed else "fail",
            "quality_score": score,
            "minimum_score": min_score,
            "pptx": self.pptx,
            "mode": self.mode,
            "slide_count": self.slide_count,
            "metrics": self.metrics,
            "errors": self.errors,
            "warnings": self.warnings,
            "repair_actions": _repair_actions(self.errors, self.warnings),
        }


def _repair_actions(errors: list[str], warnings: list[str]) -> list[dict[str, str]]:
    """Translate findings into deterministic next-pass repair guidance."""
    joined = "\n".join([*errors, *warnings]).lower()
    actions: list[dict[str, str]] = []

    def add(code: str, action: str) -> None:
        if not any(item["code"] == code for item in actions):
            actions.append({"code": code, "action": action})

    if "title overlaps" in joined or "title needs approximately" in joined:
        add(
            "repair-title-stack",
            "Shorten the claim, keep it in one bounded title region, and move subtitle/body below the measured title box.",
        )
    if "below 16 pt" in joined or "below 9 pt" in joined:
        add(
            "repair-density",
            "Switch to a lower-density layout or split the slide; never solve fit by shrinking audience text.",
        )
    if "does not fit inside its powerpoint text frame" in joined:
        add(
            "repair-text-frame-fit",
            "Resize or reposition the text frame, reflow or shorten the copy, and keep the required minimum font size; do not rely on PowerPoint auto-fit or clipping.",
        )
    if "distributed alignment" in joined or "character spacing exceeds" in joined:
        add(
            "repair-character-spacing",
            "Replace distributed alignment with left, center, right, or justified alignment and keep explicit character spacing at or below 1.2 pt, then inspect the server render.",
        )
    if (
        "crosses the slide" in joined
        or "cross the slide" in joined
        or "outside the slide" in joined
        or "safe boundary" in joined
    ):
        add(
            "repair-bounds",
            "Recompute the affected region inside the canvas safe area and re-render the complete slide.",
        )
    if "same geometric silhouette" in joined or "repeat the same" in joined:
        add(
            "repair-rhythm",
            "Choose a different layout family, background mode, or dominant visual for the repeated slide sequence.",
        )
    if "native chart" in joined:
        add(
            "repair-native-chart",
            "Fix native_charts.json or its SVG slot, then re-export so every declared data chart is a real PowerPoint chart object.",
        )
    if "embedded video" in joined or "embedded media" in joined:
        add(
            "repair-embedded-media",
            "Fix embedded_media.json, its local source/poster files, or the SVG video slot, then re-export so every declared video is a real PowerPoint media object.",
        )
    if "media region appears blank" in joined or "image file not found" in joined:
        add(
            "repair-blank-media",
            "Resolve or embed the SVG image source, rerasterize the page, and verify that the declared media region has real visual variance in the final render.",
        )
    if "layout plan" in joined or "layout_plan" in joined:
        add(
            "repair-layout-plan",
            "Repair layout_plan.json and make each SVG root's layout metadata match before exporting again.",
        )
    if "explicit typeface" in joined or "font portability" in joined:
        add(
            "repair-font-portability",
            "Set an explicit portable typeface and paragraph alignment on every table text run, then verify the exported OOXML in the server renderer.",
        )
    if "overlap" in joined:
        add(
            "inspect-overlap",
            "Inspect the rendered slide at full size and separate any unintended intersecting text or content regions.",
        )
    if "render" in joined:
        add(
            "repair-render-evidence",
            "Render every final slide again and rerun the gate against the exact exported PPTX bytes.",
        )
    if "visual inspection receipt" in joined:
        add(
            "repair-visual-inspection",
            "Open every final render at full size, record the required checks and exact PNG hashes in qa/visual-inspection.json, then rerun the gate.",
        )
    return actions


def _inspect_visual_receipt(
    project: Path,
    render_dir: Path,
    slide_count: int,
    pptx_sha256: str,
    prs: Presentation,
    result: GateResult,
) -> None:
    """Bind a human/model visual-review receipt to the exact final pixels."""
    receipt_path = project / "qa" / "visual-inspection.json"
    if not receipt_path.is_file():
        result.error("Visual inspection receipt missing: qa/visual-inspection.json")
        return
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.error(f"Visual inspection receipt is unreadable: {exc}")
        return
    if not isinstance(receipt, dict) or receipt.get("version") != 1:
        result.error("Visual inspection receipt version must be 1")
        return
    if receipt.get("pptx_sha256") != pptx_sha256:
        result.error("Visual inspection receipt does not match the exact final PPTX SHA-256")

    expected_slides = list(range(1, slide_count + 1))
    for field_name in ("inspected_slides", "full_size_reads"):
        raw = receipt.get(field_name)
        try:
            actual = sorted({int(value) for value in raw}) if isinstance(raw, list) else []
        except (TypeError, ValueError):
            actual = []
        if actual != expected_slides:
            result.error(
                f"Visual inspection receipt {field_name} must contain every slide 1-{slide_count}"
            )
    if receipt.get("contact_sheet_read") is not True:
        result.error("Visual inspection receipt must confirm contact_sheet_read=true")

    checks = receipt.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    failed_checks = [name for name in VISUAL_RECEIPT_CHECKS if checks.get(name) != "pass"]
    if failed_checks:
        result.error(
            "Visual inspection receipt has missing or failed checks: " + ", ".join(failed_checks)
        )
    if checks.get("text_containment") == "pass":
        _inspect_text_containment_evidence(
            project,
            render_dir,
            expected_slides,
            pptx_sha256,
            receipt,
            prs,
            result,
        )
    defects = receipt.get("defects")
    if not isinstance(defects, list):
        result.error("Visual inspection receipt defects must be a list")
    elif defects:
        result.error("Visual inspection receipt reports unresolved defects")

    render_hashes = receipt.get("render_sha256")
    render_hashes = render_hashes if isinstance(render_hashes, dict) else {}
    hash_errors: list[str] = []
    for slide_index in expected_slides:
        render_file = render_dir / f"slide-{slide_index}.png"
        if not render_file.is_file():
            hash_errors.append(render_file.name)
            continue
        expected_hash = hashlib.sha256(render_file.read_bytes()).hexdigest()
        if render_hashes.get(render_file.name) != expected_hash:
            hash_errors.append(render_file.name)
    if hash_errors:
        result.error(
            "Visual inspection receipt render hashes are missing or stale: "
            + ", ".join(hash_errors[:8])
        )
    result.metrics["visual_inspection_receipt"] = {
        "inspected_slide_count": len(expected_slides) - len(hash_errors),
        "required_check_count": len(VISUAL_RECEIPT_CHECKS),
    }


def _inspect_text_containment_evidence(
    project: Path,
    render_dir: Path,
    expected_slides: list[int],
    pptx_sha256: str,
    visual_receipt: dict[str, object],
    prs: Presentation,
    result: GateResult,
) -> None:
    """Require slide-scoped, pixel-bound evidence for rasterized slide text."""

    section = visual_receipt.get("text_containment")
    if not isinstance(section, dict) or section.get("status") != "pass":
        result.error(
            "Text containment evidence must declare status=pass in the visual inspection receipt"
        )
        return
    raw_path = section.get("evidence_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        result.error("Text containment evidence_path is missing")
        return
    evidence_path = Path(raw_path)
    if not evidence_path.is_absolute():
        evidence_path = project / evidence_path
    try:
        evidence_path = evidence_path.expanduser().resolve()
        evidence_path.relative_to(project.resolve())
    except (OSError, ValueError):
        result.error("Text containment evidence_path must resolve inside the project")
        return
    if not evidence_path.is_file():
        result.error(f"Text containment evidence is missing: {evidence_path}")
        return
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.error(f"Text containment evidence is unreadable: {exc}")
        return
    if not isinstance(evidence, dict) or evidence.get("version") != 2:
        result.error("Text containment evidence version must be 2")
        return
    if evidence.get("pptx_sha256") != pptx_sha256:
        result.error("Text containment evidence does not match the exact final PPTX SHA-256")

    raw_inspected = evidence.get("inspected_slides")
    try:
        inspected = (
            sorted({int(value) for value in raw_inspected})
            if isinstance(raw_inspected, list)
            else []
        )
    except (TypeError, ValueError):
        inspected = []
    if inspected != expected_slides:
        result.error(
            "Text containment evidence inspected_slides must contain every final slide"
        )

    slide_entries = evidence.get("slides")
    slide_entries = slide_entries if isinstance(slide_entries, list) else []
    by_slide: dict[int, dict[str, object]] = {}
    for entry in slide_entries:
        if not isinstance(entry, dict):
            continue
        try:
            slide_index = int(entry.get("slide"))
        except (TypeError, ValueError):
            continue
        if slide_index in by_slide:
            result.error(
                f"Text containment evidence repeats slide {slide_index}"
            )
        by_slide[slide_index] = entry

    missing = [index for index in expected_slides if index not in by_slide]
    if missing:
        result.error(
            "Text containment evidence has no slide record for: "
            + ", ".join(str(index) for index in missing[:8])
        )

    measured_slide_count = 0
    measured_record_count = 0
    for slide_index in expected_slides:
        entry = by_slide.get(slide_index)
        if entry is None:
            continue
        if entry.get("status") != "pass":
            result.error(f"Text containment evidence slide {slide_index} is not pass")
        render_file = render_dir / f"slide-{slide_index}.png"
        if render_file.is_file():
            render_sha256 = hashlib.sha256(render_file.read_bytes()).hexdigest()
            if entry.get("render_sha256") != render_sha256:
                result.error(
                    f"Text containment evidence slide {slide_index} render hash is missing or stale"
                )
            try:
                with Image.open(render_file) as rendered:
                    canvas_width, canvas_height = rendered.size
            except OSError:
                canvas_width, canvas_height = 0, 0
        else:
            canvas_width, canvas_height = 0, 0

        method = entry.get("method")
        if method not in {"full-size-review", "pixel-bbox"}:
            result.error(
                f"Text containment evidence slide {slide_index} has invalid method"
            )
        records = entry.get("records")
        records = records if isinstance(records, list) else []
        if method == "pixel-bbox":
            measured_slide_count += 1
            if not records:
                result.error(
                    f"Text containment evidence slide {slide_index} requires measured records"
                )
            def svg_page_sort_key(path: Path) -> tuple[int, str]:
                match = re.search(r"(\d+)$", path.stem)
                return (int(match.group(1)) if match else 10**9, path.name)

            svg_files = sorted(
                (project / "svg_output").glob("*.svg"), key=svg_page_sort_key
            )
            source_svg = (
                svg_files[slide_index - 1]
                if 0 < slide_index <= len(svg_files)
                else None
            )
            svg_text = ""
            if source_svg is not None:
                try:
                    svg_root = ET.parse(source_svg).getroot()
                    svg_text = " ".join(
                        "".join(node.itertext()).strip()
                        for node in svg_root.iter()
                        if node.tag.rsplit("}", 1)[-1] == "text"
                    ).strip()
                except (OSError, ET.ParseError):
                    svg_text = ""
            if not svg_text:
                result.error(
                    f"Text containment evidence slide {slide_index} source SVG has no text primitives; redraw typography from vector text instead of upscaling a flattened raster"
                )
            declared_source_size = entry.get("source_pixel_dimensions")
            pictures = [
                shape
                for shape in _walk_shapes(prs.slides[slide_index - 1].shapes)
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
            ]
            actual_source_size: list[int] | None = None
            effective_dpi = 0.0
            if len(pictures) == 1:
                picture = pictures[0]
                try:
                    px_width, px_height = picture.image.size
                    actual_source_size = [int(px_width), int(px_height)]
                    effective_dpi = min(
                        px_width / max(int(picture.width) / EMU_PER_INCH, 0.01),
                        px_height / max(int(picture.height) / EMU_PER_INCH, 0.01),
                    )
                except (AttributeError, OSError, ValueError):
                    actual_source_size = None
            if declared_source_size != actual_source_size:
                result.error(
                    f"Text containment evidence slide {slide_index} source pixel dimensions are missing or stale"
                )
            if effective_dpi < 192:
                result.error(
                    f"Text containment evidence slide {slide_index} source image is only {effective_dpi:.0f} DPI; premium raster typography requires at least 192 DPI"
                )
        for record_index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} is invalid"
                )
                continue
            measured_record_count += 1
            if record.get("contained") is not True:
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} is not contained"
                )
            required = record.get("minimum_required_padding")
            actual = record.get("actual_minimum_padding")
            if not isinstance(required, (int, float)) or not isinstance(actual, (int, float)):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} lacks numeric padding"
                )
            elif actual < required:
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} misses padding"
                )
            bbox = record.get("bbox")

            def valid_canvas_bbox(value: object) -> bool:
                return bool(
                    isinstance(value, list)
                    and len(value) == 4
                    and all(isinstance(item, (int, float)) for item in value)
                    and value[0] <= value[2]
                    and value[1] <= value[3]
                    and value[0] >= 0
                    and value[1] >= 0
                    and value[2] <= canvas_width
                    and value[3] <= canvas_height
                )

            def inside(inner: list[float], outer: list[float]) -> bool:
                return bool(
                    inner[0] >= outer[0]
                    and inner[1] >= outer[1]
                    and inner[2] <= outer[2]
                    and inner[3] <= outer[3]
                )

            valid_bbox = valid_canvas_bbox(bbox)
            if not valid_bbox:
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} has an invalid bbox"
                )
                continue

            containing_region = record.get("containing_region_bbox")
            parent_region = record.get("parent_region_bbox")
            if not valid_canvas_bbox(containing_region):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} has an invalid containing region"
                )
                continue
            if not valid_canvas_bbox(parent_region):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} has an invalid parent region"
                )
                continue
            if not inside(bbox, containing_region):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} escapes its containing region"
                )
            if not inside(containing_region, parent_region):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} containing region escapes its parent"
                )

            computed_padding = min(
                bbox[0] - containing_region[0],
                bbox[1] - containing_region[1],
                containing_region[2] - bbox[2],
                containing_region[3] - bbox[3],
            )
            if isinstance(actual, (int, float)) and abs(actual - computed_padding) > 0.5:
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} has stale or inflated padding"
                )

            font_size = record.get("font_size")
            minimum_font_size = record.get("minimum_font_size")
            if not isinstance(font_size, (int, float)) or not isinstance(
                minimum_font_size, (int, float)
            ):
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} lacks numeric font-size evidence"
                )
            elif font_size < minimum_font_size:
                result.error(
                    f"Text containment evidence slide {slide_index} record {record_index} misses minimum font size"
                )

    if measured_slide_count < 1 or measured_record_count < 1:
        result.error(
            "Text containment evidence must include at least one pixel-bbox slide with measured records"
        )
    result.metrics["text_containment_evidence"] = {
        "slide_count": len(by_slide),
        "pixel_measured_slide_count": measured_slide_count,
        "measured_record_count": measured_record_count,
    }


def _relationship_source_dir(rel_path: str) -> str:
    """Return the directory of the OOXML part owning ``rel_path``."""
    if rel_path == "_rels/.rels":
        return ""
    parent = posixpath.dirname(rel_path)
    if posixpath.basename(parent) != "_rels":
        return posixpath.dirname(rel_path)
    owning_dir = posixpath.dirname(parent)
    rel_name = posixpath.basename(rel_path)
    source_name = rel_name[: -len(".rels")]
    return posixpath.dirname(posixpath.join(owning_dir, source_name))


def validate_relationships(pptx_path: Path) -> list[str]:
    """Return missing or malformed internal OOXML relationship errors."""
    errors: list[str] = []
    try:
        with zipfile.ZipFile(pptx_path) as package:
            names = set(package.namelist())
            rel_paths = sorted(name for name in names if name.endswith(".rels"))
            for rel_path in rel_paths:
                try:
                    root = ET.fromstring(package.read(rel_path))
                except (KeyError, ET.ParseError) as exc:
                    errors.append(f"{rel_path}: unreadable relationship XML ({exc})")
                    continue
                base_dir = _relationship_source_dir(rel_path)
                for rel in root.findall(f"{{{REL_NS}}}Relationship"):
                    if rel.attrib.get("TargetMode") == "External":
                        continue
                    target = rel.attrib.get("Target", "").split("#", 1)[0]
                    if not target:
                        errors.append(f"{rel_path}: relationship without Target")
                        continue
                    resolved = unquote(
                        target.lstrip("/")
                        if target.startswith("/")
                        else posixpath.normpath(posixpath.join(base_dir, target))
                    )
                    if resolved not in names:
                        errors.append(f"{rel_path}: missing target {resolved}")
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"Invalid PPTX/ZIP package: {exc}"]
    return errors


def _inspect_font_portability(pptx_path: Path, result: GateResult) -> None:
    """Reject table text that depends on theme-font or renderer defaults.

    Table typography is unusually sensitive to LibreOffice substitutions. A
    missing ``a:latin`` typeface can make Calibri resolve to Liberation Mono in
    a minimal Linux image, while a missing paragraph alignment lets renderers
    choose inconsistent spacing. The final artifact must carry both values.
    """

    table_runs = 0
    explicit_typeface_runs = 0
    aligned_table_paragraphs = 0
    table_paragraphs = 0
    slides_with_implicit_typeface: set[int] = set()
    slides_with_implicit_alignment: set[int] = set()
    slides_with_distributed_alignment: set[int] = set()

    try:
        with zipfile.ZipFile(pptx_path) as package:
            slide_names = sorted(
                (
                    name
                    for name in package.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)),
            )
            for slide_name in slide_names:
                slide_number = int(re.search(r"slide(\d+)\.xml$", slide_name).group(1))
                root = ET.fromstring(package.read(slide_name))
                for table in root.findall(f".//{{{DRAWINGML_NS}}}tbl"):
                    for paragraph in table.findall(f".//{{{DRAWINGML_NS}}}p"):
                        runs = []
                        for tag in ("r", "fld"):
                            for run in paragraph.findall(f"{{{DRAWINGML_NS}}}{tag}"):
                                text = run.find(f"{{{DRAWINGML_NS}}}t")
                                if text is not None and (text.text or "").strip():
                                    runs.append(run)
                        if not runs:
                            continue

                        table_paragraphs += 1
                        paragraph_properties = paragraph.find(f"{{{DRAWINGML_NS}}}pPr")
                        alignment = (
                            paragraph_properties.get("algn")
                            if paragraph_properties is not None
                            else None
                        )
                        if (
                            alignment in {"l", "ctr", "r", "just"}
                        ):
                            aligned_table_paragraphs += 1
                        else:
                            slides_with_implicit_alignment.add(slide_number)
                            if alignment == "dist":
                                slides_with_distributed_alignment.add(slide_number)

                        for run in runs:
                            table_runs += 1
                            run_properties = run.find(f"{{{DRAWINGML_NS}}}rPr")
                            latin = (
                                run_properties.find(f"{{{DRAWINGML_NS}}}latin")
                                if run_properties is not None
                                else None
                            )
                            if latin is not None and (latin.get("typeface") or "").strip():
                                explicit_typeface_runs += 1
                            else:
                                slides_with_implicit_typeface.add(slide_number)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        result.error(f"Font portability inspection failed: {exc}")
        return

    result.metrics["table_text_runs"] = table_runs
    result.metrics["table_text_runs_with_explicit_typeface"] = explicit_typeface_runs
    result.metrics["table_paragraphs"] = table_paragraphs
    result.metrics["table_paragraphs_with_explicit_alignment"] = aligned_table_paragraphs
    result.metrics["distributed_table_paragraphs"] = len(
        slides_with_distributed_alignment
    )

    if slides_with_implicit_typeface:
        slides = ", ".join(str(index) for index in sorted(slides_with_implicit_typeface))
        result.error(f"Table text lacks an explicit typeface on slide(s): {slides}")
    if slides_with_implicit_alignment:
        slides = ", ".join(str(index) for index in sorted(slides_with_implicit_alignment))
        result.error(f"Table text lacks explicit paragraph alignment on slide(s): {slides}")
    if slides_with_distributed_alignment:
        slides = ", ".join(
            str(index) for index in sorted(slides_with_distributed_alignment)
        )
        result.error(
            "Table text uses distributed alignment, which visibly stretches "
            f"characters in Office renderers, on slide(s): {slides}"
        )


def _inspect_character_spacing(pptx_path: Path, result: GateResult) -> None:
    """Reject DrawingML typography that expands characters unpredictably.

    ``algn=\"dist\"`` distributes characters across the entire text region,
    which is the source of the visibly separated words seen in table cells and
    short labels. Large ``spc`` values have the same effect even when paragraph
    alignment is ordinary. Both are renderer-sensitive and unsafe for a final
    cross-platform PPTX.
    """
    distributed_slides: set[int] = set()
    excessive_spacing_slides: set[int] = set()
    max_spacing = 0
    try:
        with zipfile.ZipFile(pptx_path) as package:
            slide_names = sorted(
                (
                    name
                    for name in package.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=lambda name: int(
                    re.search(r"slide(\d+)\.xml$", name).group(1)
                ),
            )
            for slide_name in slide_names:
                slide_number = int(
                    re.search(r"slide(\d+)\.xml$", slide_name).group(1)
                )
                root = ET.fromstring(package.read(slide_name))
                for paragraph_properties in root.findall(
                    f".//{{{DRAWINGML_NS}}}pPr"
                ):
                    if paragraph_properties.get("algn") == "dist":
                        distributed_slides.add(slide_number)
                for run_properties in root.findall(
                    f".//{{{DRAWINGML_NS}}}rPr"
                ):
                    raw_spacing = run_properties.get("spc")
                    if raw_spacing is None:
                        continue
                    try:
                        spacing = abs(int(raw_spacing))
                    except ValueError:
                        excessive_spacing_slides.add(slide_number)
                        continue
                    max_spacing = max(max_spacing, spacing)
                    # DrawingML stores character spacing in hundredths of a
                    # point. More than 1.2 pt is visibly loose for body copy.
                    if spacing > 120:
                        excessive_spacing_slides.add(slide_number)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        result.error(f"Character-spacing inspection failed: {exc}")
        return

    result.metrics["distributed_alignment_slides"] = sorted(distributed_slides)
    result.metrics["excessive_character_spacing_slides"] = sorted(
        excessive_spacing_slides
    )
    result.metrics["maximum_character_spacing_hundredths_pt"] = max_spacing
    if distributed_slides:
        result.error(
            "Distributed paragraph alignment can separate characters on slide(s): "
            + ", ".join(map(str, sorted(distributed_slides)))
        )
    if excessive_spacing_slides:
        result.error(
            "Character spacing exceeds the 1.2 pt portability limit on slide(s): "
            + ", ".join(map(str, sorted(excessive_spacing_slides)))
        )


def _walk_shapes(shapes: Iterable) -> Iterable:
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk_shapes(shape.shapes)


def _text_and_sizes(shape) -> tuple[str, list[float]]:
    if not getattr(shape, "has_text_frame", False):
        return "", []
    text = (shape.text or "").strip()
    sizes: list[float] = []
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.font.size is not None:
                sizes.append(float(run.font.size.pt))
    return text, sizes


def _shape_signature(slide, slide_w: int, slide_h: int) -> tuple:
    signature = []
    for shape in slide.shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.LINE:
            continue
        left = int(round((int(shape.left) / slide_w) * 20))
        top = int(round((int(shape.top) / slide_h) * 12))
        width = int(round((int(shape.width) / slide_w) * 20))
        height = int(round((int(shape.height) / slide_h) * 12))
        signature.append((int(shape.shape_type), left, top, width, height))
    return tuple(sorted(signature))


def _bbox(shape) -> tuple[int, int, int, int]:
    left, top = int(shape.left), int(shape.top)
    return left, top, left + int(shape.width), top + int(shape.height)


def _intersection_ratio(first, second) -> float:
    a_left, a_top, a_right, a_bottom = _bbox(first)
    b_left, b_top, b_right, b_bottom = _bbox(second)
    width = max(0, min(a_right, b_right) - max(a_left, b_left))
    height = max(0, min(a_bottom, b_bottom) - max(a_top, b_top))
    overlap = width * height
    if not overlap:
        return 0.0
    smaller = min(
        max(1, (a_right - a_left) * (a_bottom - a_top)),
        max(1, (b_right - b_left) * (b_bottom - b_top)),
    )
    return overlap / smaller


def _shape_contains(container, child, *, tolerance: int = 0) -> bool:
    """Return whether *container* encloses *child* within an EMU tolerance."""
    c_left, c_top, c_right, c_bottom = _bbox(container)
    x_left, x_top, x_right, x_bottom = _bbox(child)
    return (
        x_left >= c_left - tolerance
        and x_top >= c_top - tolerance
        and x_right <= c_right + tolerance
        and x_bottom <= c_bottom + tolerance
    )


def _shape_z_order(shape) -> tuple[object, int] | None:
    """Return the OOXML parent and stacking index for a shape when available."""
    element = getattr(shape, "_element", None)
    parent = element.getparent() if element is not None else None
    if parent is None:
        return None
    return parent, parent.index(element)


def _shape_layout_evidence(shape, slide_w: int, slide_h: int) -> dict[str, object]:
    """Return compact, normalized evidence that an authoring pass can act on."""
    left, top, right, bottom = _bbox(shape)
    text, _ = _text_and_sizes(shape)
    return {
        "shape_id": int(getattr(shape, "shape_id", 0) or 0),
        "shape_type": str(getattr(shape.shape_type, "name", shape.shape_type)),
        "text": text[:120],
        "bounds": {
            "x": round(left / slide_w, 4),
            "y": round(top / slide_h, 4),
            "width": round((right - left) / slide_w, 4),
            "height": round((bottom - top) / slide_h, 4),
        },
    }


def _unintended_overlap(first, second) -> bool:
    """Classify a final-PPTX intersection without flagging label containers.

    Text intentionally sits above panels, nodes, pills, lines, and timeline
    bars. Those background relationships are valid. Text/text intersections or
    a foreground visual object intruding into a text region are not.
    """
    ratio = _intersection_ratio(first, second)
    if ratio <= 0:
        return False
    first_text, _ = _text_and_sizes(first)
    second_text, _ = _text_and_sizes(second)
    if first_text and second_text:
        return ratio >= 0.08
    if not (first_text or second_text):
        return False

    text_shape = first if first_text else second
    visual_shape = second if first_text else first
    text_z_order = _shape_z_order(text_shape)
    visual_z_order = _shape_z_order(visual_shape)
    if (
        text_z_order is not None
        and visual_z_order is not None
        and text_z_order[0] is visual_z_order[0]
        and visual_z_order[1] < text_z_order[1]
    ):
        # The visual is behind the text in the same shape tree. This is the
        # normal construction for panels, label containers, and connectors.
        return False
    # If stack order cannot be resolved (for example across nested groups), a
    # fully enclosed text box is still a conventional label relationship.
    if text_z_order is None and _shape_contains(visual_shape, text_shape, tolerance=9525):
        return False
    return ratio >= 0.22


def _looks_like_footer(shape, slide_h: int) -> bool:
    return int(shape.top) >= int(slide_h * 0.88)


def _title_candidate(slide, slide_w: int, slide_h: int):
    candidates: list[tuple[float, int, str, object]] = []
    for shape in _walk_shapes(slide.shapes):
        text, sizes = _text_and_sizes(shape)
        if not text or int(shape.top) > slide_h * 0.46:
            continue
        size = max(sizes) if sizes else 0.0
        candidates.append((size, -int(shape.top), text, shape))
    if not candidates:
        return "", None, None
    wide_candidates = [
        item for item in candidates
        if int(item[3].width) >= slide_w * 0.30
    ]
    size, _position, text, shape = max(wide_candidates or candidates, key=lambda item: item[:3])
    return text, size or None, shape


def _estimated_text_width_points(text: str, font_size: float) -> float:
    """Conservatively estimate rendered width for title-fit validation."""
    units = 0.0
    for character in text:
        if re.match(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", character):
            units += 1.0
        elif character.isspace():
            units += 0.28
        elif character in "ilI1|.,:;!'’`":
            units += 0.25
        elif character in "MW@#%&QGOD0":
            units += 0.72
        elif character.isupper():
            units += 0.60
        else:
            units += 0.48
    return units * font_size


def _title_fit_error(text: str, title_size: float | None, shape) -> str | None:
    if not text or title_size is None or shape is None:
        return None
    explicit_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(explicit_lines) > 2:
        return "title uses more than two explicit lines"
    available_width_points = max(1.0, (int(shape.width) / EMU_PER_INCH) * 72 - 18)
    widest = max(
        (_estimated_text_width_points(line, title_size) for line in explicit_lines),
        default=0.0,
    )
    required_lines = max(1, math.ceil(widest / max(1.0, available_width_points * 1.08)))
    box_height_points = (int(shape.height) / EMU_PER_INCH) * 72
    line_capacity = max(1, math.floor(box_height_points / max(1.0, title_size * 1.12)))
    if required_lines > min(2, line_capacity):
        return (
            f"title needs approximately {required_lines} lines but its box safely "
            f"supports {min(2, line_capacity)}"
        )
    return None


def _length_points(value) -> float:
    if value is None:
        return 0.0
    points = getattr(value, "pt", None)
    if points is not None:
        return float(points)
    return (int(value) / EMU_PER_INCH) * 72


def _text_frame_fit_defect(shape) -> dict[str, object] | None:
    """Estimate severe text overflow inside a fixed PowerPoint text frame.

    Shape-boundary checks cannot see text that PowerPoint later clips or
    expands into neighboring content. This conservative estimator only rejects
    clear overflow, leaving normal Office font-metric differences a 20% buffer.
    """
    if not getattr(shape, "has_text_frame", False):
        return None
    text_frame = shape.text_frame
    text = (shape.text or "").strip()
    if not text:
        return None

    width_points = (int(shape.width) / EMU_PER_INCH) * 72
    height_points = (int(shape.height) / EMU_PER_INCH) * 72
    available_width = max(
        1.0,
        width_points
        - _length_points(text_frame.margin_left)
        - _length_points(text_frame.margin_right),
    )
    available_height = max(
        1.0,
        height_points
        - _length_points(text_frame.margin_top)
        - _length_points(text_frame.margin_bottom),
    )

    required_height = 0.0
    maximum_line_width = 0.0
    measured_paragraphs = 0
    for paragraph in text_frame.paragraphs:
        paragraph_text = paragraph.text or ""
        if not paragraph_text.strip():
            continue
        sizes = [
            float(run.font.size.pt)
            for run in paragraph.runs
            if run.font.size is not None
        ]
        if not sizes:
            # Theme-inherited size cannot be measured reliably here. Other
            # portability checks report inherited typography separately.
            continue
        measured_paragraphs += 1
        font_size = max(sizes)
        line_spacing = paragraph.line_spacing
        if isinstance(line_spacing, float):
            line_height = font_size * max(0.8, line_spacing)
        elif line_spacing is not None:
            line_height = max(font_size, _length_points(line_spacing))
        else:
            # Office reports font size as the em box; using an additional
            # leading multiplier here produces false vertical failures for
            # conventional 38 pt text in a 0.6-inch title frame.
            line_height = font_size

        visual_lines = re.split(r"[\n\v]", paragraph_text)
        for line in visual_lines:
            line_width = _estimated_text_width_points(line, font_size)
            maximum_line_width = max(maximum_line_width, line_width)
            if text_frame.word_wrap is False:
                wrapped_lines = 1
            else:
                wrapped_lines = max(
                    1,
                    math.ceil(line_width / max(1.0, available_width * 0.94)),
                )
            required_height += line_height * wrapped_lines
        required_height += _length_points(paragraph.space_before)
        required_height += _length_points(paragraph.space_after)

    if not measured_paragraphs:
        return None

    width_ratio = maximum_line_width / available_width
    height_ratio = required_height / available_height
    overflow_axes: list[str] = []
    if text_frame.word_wrap is False and width_ratio > 1.35:
        overflow_axes.append("horizontal")
    if height_ratio > 1.20:
        overflow_axes.append("vertical")
    if not overflow_axes:
        return None
    return {
        "axes": overflow_axes,
        "required_width_pt": round(maximum_line_width, 1),
        "available_width_pt": round(available_width, 1),
        "required_height_pt": round(required_height, 1),
        "available_height_pt": round(available_height, 1),
        "word_wrap": text_frame.word_wrap,
    }


def _detect_mode(prs: Presentation) -> str:
    """Detect the two supported delivery modes from final slide geometry."""
    slide_w, slide_h = int(prs.slide_width), int(prs.slide_height)
    if not prs.slides:
        return "editable"
    for slide in prs.slides:
        flattened = list(_walk_shapes(slide.shapes))
        pictures = [shape for shape in flattened if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        extras = [
            shape
            for shape in flattened
            if shape.shape_type not in {MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.GROUP}
        ]
        if len(pictures) != 1 or extras:
            return "editable"
        left, top, right, bottom = _bbox(pictures[0])
        if not (
            (left == 0 and top == 0 and right == slide_w and bottom == slide_h)
            or (left == 0 and right == slide_w and top >= 0 and bottom <= slide_h)
            or (top == 0 and bottom == slide_h and left >= 0 and right <= slide_w)
        ):
            return "editable"
    return "full_page_image"


def _inspect_editable_deck(prs: Presentation, result: GateResult) -> None:
    slide_w, slide_h = int(prs.slide_width), int(prs.slide_height)
    signatures: list[tuple] = []
    slides_without_editable_text: list[int] = []
    undersized_body_slides: list[int] = []
    overlap_error_slides: list[int] = []
    partial_overflow_slides: list[int] = []
    text_overflow_slides: list[int] = []
    text_frame_overflow_slides: list[int] = []
    text_safe_boundary_slides: list[int] = []
    image_dpi_warnings: list[str] = []
    notes_missing: list[int] = []
    source_format_issues: list[int] = []
    native_chart_count = 0
    embedded_video_count = 0
    layout_defects: list[dict[str, object]] = []

    for slide_index, slide in enumerate(prs.slides, start=1):
        signatures.append(_shape_signature(slide, slide_w, slide_h))
        flattened = list(_walk_shapes(slide.shapes))
        text_shapes = []
        picture_count = 0
        small_body = False

        if not slide.shapes:
            result.error(f"Slide {slide_index}: empty slide")

        for shape in flattened:
            left, top, right, bottom = _bbox(shape)
            text, sizes = _text_and_sizes(shape)
            if right <= 0 or bottom <= 0 or left >= slide_w or top >= slide_h:
                result.error(f"Slide {slide_index}: shape is entirely outside the slide canvas")
                layout_defects.append(
                    {
                        "slide": slide_index,
                        "kind": "outside-canvas",
                        "shapes": [_shape_layout_evidence(shape, slide_w, slide_h)],
                    }
                )
            elif left < 0 or top < 0 or right > slide_w or bottom > slide_h:
                if text:
                    text_overflow_slides.append(slide_index)
                    kind = "text-crosses-canvas"
                else:
                    partial_overflow_slides.append(slide_index)
                    kind = "shape-crosses-canvas"
                layout_defects.append(
                    {
                        "slide": slide_index,
                        "kind": kind,
                        "shapes": [_shape_layout_evidence(shape, slide_w, slide_h)],
                    }
                )

            if text:
                text_shapes.append(shape)
                text_frame_defect = _text_frame_fit_defect(shape)
                if text_frame_defect is not None:
                    text_frame_overflow_slides.append(slide_index)
                    layout_defects.append(
                        {
                            "slide": slide_index,
                            "kind": "text-frame-overflow",
                            "fit": text_frame_defect,
                            "shapes": [
                                _shape_layout_evidence(
                                    shape, slide_w, slide_h
                                )
                            ],
                        }
                    )
                if not _looks_like_footer(shape, slide_h):
                    safe_x = int(slide_w * 0.025)
                    safe_y = int(slide_h * 0.02)
                    if (
                        left < safe_x
                        or right > slide_w - safe_x
                        or top < safe_y
                        or bottom > slide_h - safe_y
                    ):
                        text_safe_boundary_slides.append(slide_index)
                        layout_defects.append(
                            {
                                "slide": slide_index,
                                "kind": "text-safe-boundary",
                                "shapes": [_shape_layout_evidence(shape, slide_w, slide_h)],
                            }
                        )
                if PLACEHOLDER_RE.search(text):
                    result.error(f"Slide {slide_index}: unresolved placeholder or draft copy: {text[:80]!r}")
                if PROCESS_COPY_RE.search(text):
                    result.error(f"Slide {slide_index}: internal production language is visible: {text[:80]!r}")
                if sizes and not _looks_like_footer(shape, slide_h) and min(sizes) < 16:
                    small_body = True
                if sizes and min(sizes) < 9:
                    result.error(f"Slide {slide_index}: text below 9 pt is not readable")

            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                picture_count += 1
                try:
                    px_w, px_h = shape.image.size
                    dpi = min(
                        px_w / max(int(shape.width) / EMU_PER_INCH, 0.01),
                        px_h / max(int(shape.height) / EMU_PER_INCH, 0.01),
                    )
                    if dpi < 72:
                        result.error(f"Slide {slide_index}: picture effective resolution is {dpi:.0f} DPI")
                    elif dpi < 96:
                        image_dpi_warnings.append(f"slide {slide_index}: {dpi:.0f} DPI")
                except (AttributeError, OSError, ValueError):
                    result.warn(f"Slide {slide_index}: could not inspect one picture's effective DPI")
            elif shape.shape_type == MSO_SHAPE_TYPE.CHART:
                native_chart_count += 1
            elif shape.shape_type == MSO_SHAPE_TYPE.MEDIA:
                embedded_video_count += 1

        if not text_shapes:
            slides_without_editable_text.append(slide_index)
        if picture_count == 1 and len(flattened) == 1:
            slides_without_editable_text.append(slide_index)
        if small_body:
            undersized_body_slides.append(slide_index)

        title, title_size, title_shape = _title_candidate(slide, slide_w, slide_h)
        # Keep authoring targets at 50/35 pt, but allow a small final-package
        # tolerance for layout-specific closing titles and Office rounding.
        minimum_title = 48 if slide_index == 1 else 32
        if not title:
            result.warn(f"Slide {slide_index}: no title candidate found in the upper slide region")
        elif title_size is None:
            result.warn(f"Slide {slide_index}: title font size is inherited and cannot be verified")
        elif title_size < minimum_title:
            result.error(
                f"Slide {slide_index}: title is {title_size:.1f} pt; minimum is {minimum_title} pt"
            )
        title_fit_error = _title_fit_error(title, title_size, title_shape)
        if title_fit_error:
            result.error(f"Slide {slide_index}: {title_fit_error}")
        if title_shape is not None:
            for other_shape in text_shapes:
                if (
                    getattr(other_shape, "shape_id", None) == getattr(title_shape, "shape_id", None)
                    or int(other_shape.top) > slide_h * 0.46
                ):
                    continue
                if _intersection_ratio(title_shape, other_shape) >= 0.08:
                    result.error(f"Slide {slide_index}: title overlaps another header text box")
                    break
        if title and (len(title.split()) > 14 or len(re.findall(r"[\u3400-\u9fff]", title)) > 28):
            result.warn(f"Slide {slide_index}: title may wrap and should be shortened")

        content_shapes = [
            shape
            for shape in flattened
            if int(shape.width) * int(shape.height) < slide_w * slide_h * 0.90
            and shape.shape_type not in {MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.GROUP}
        ]
        overlap_found = False
        for first_index, first in enumerate(content_shapes):
            for second in content_shapes[first_index + 1 :]:
                if _unintended_overlap(first, second):
                    overlap_found = True
                    layout_defects.append(
                        {
                            "slide": slide_index,
                            "kind": "unintended-overlap",
                            "intersection_ratio": round(_intersection_ratio(first, second), 4),
                            "shapes": [
                                _shape_layout_evidence(first, slide_w, slide_h),
                                _shape_layout_evidence(second, slide_w, slide_h),
                            ],
                        }
                    )
                    break
            if overlap_found:
                break
        if overlap_found:
            overlap_error_slides.append(slide_index)

        has_notes = bool(getattr(slide, "has_notes_slide", False))
        if not has_notes:
            notes_missing.append(slide_index)
        else:
            notes_text = slide.notes_slide.notes_text_frame.text or ""
            if URL_RE.search(notes_text) and "[Sources]" not in notes_text:
                source_format_issues.append(slide_index)

    content_signatures = signatures[1:] if len(signatures) > 1 else signatures
    if len(content_signatures) >= 3 and len(set(content_signatures)) == 1:
        result.error("All content slides use the same geometric silhouette")
    for index in range(max(0, len(content_signatures) - 2)):
        if len(set(content_signatures[index : index + 3])) == 1:
            result.warn(f"Slides {index + 2}-{index + 4}: three adjacent slides repeat one layout")

    if len(set(slides_without_editable_text)) > max(1, math.floor(len(prs.slides) * 0.25)):
        result.error(
            "Editable mode contains too many slides without editable text: "
            + ", ".join(map(str, sorted(set(slides_without_editable_text))))
        )
    if undersized_body_slides:
        result.error("Body or label text below 16 pt on slide(s): " + ", ".join(map(str, sorted(set(undersized_body_slides)))))
    if text_overflow_slides:
        result.error(
            "Text crosses the slide boundary on slide(s): "
            + ", ".join(map(str, sorted(set(text_overflow_slides))))
        )
    if text_frame_overflow_slides:
        result.error(
            "Text does not fit inside its PowerPoint text frame on slide(s): "
            + ", ".join(
                map(str, sorted(set(text_frame_overflow_slides)))
            )
        )
    if text_safe_boundary_slides:
        result.error(
            "Audience text violates the slide safe boundary on slide(s): "
            + ", ".join(map(str, sorted(set(text_safe_boundary_slides))))
        )
    if partial_overflow_slides:
        result.error(
            "Non-text shapes cross the slide boundary on slide(s): "
            + ", ".join(map(str, sorted(set(partial_overflow_slides))))
        )
    if overlap_error_slides:
        result.error(
            "Unintended text or partial-object overlap on slide(s): "
            + ", ".join(map(str, sorted(set(overlap_error_slides))))
        )
    if image_dpi_warnings:
        result.warn("Pictures below 96 effective DPI: " + "; ".join(image_dpi_warnings))
    if notes_missing:
        result.warn("Speaker notes missing on slide(s): " + ", ".join(map(str, notes_missing)))
    if source_format_issues:
        result.error("External URLs are not under a [Sources] block on slide(s): " + ", ".join(map(str, source_format_issues)))

    result.metrics["unique_layout_signatures"] = len(set(signatures))
    result.metrics["editable_text_slides"] = len(prs.slides) - len(set(slides_without_editable_text))
    result.metrics["native_chart_count"] = native_chart_count
    result.metrics["embedded_video_count"] = embedded_video_count
    result.metrics["layout_defects"] = layout_defects


def _inspect_image_deck(prs: Presentation, result: GateResult) -> None:
    slide_w, slide_h = int(prs.slide_width), int(prs.slide_height)
    for slide_index, slide in enumerate(prs.slides, start=1):
        flattened = list(_walk_shapes(slide.shapes))
        pictures = [shape for shape in flattened if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        text_shapes = [shape for shape in flattened if _text_and_sizes(shape)[0]]
        non_picture_shapes = [
            shape
            for shape in flattened
            if shape.shape_type not in {MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.GROUP}
        ]
        if len(pictures) != 1:
            result.error(f"Slide {slide_index}: full-page image mode requires exactly one picture")
            continue
        if text_shapes or non_picture_shapes:
            result.error(f"Slide {slide_index}: image mode contains editable text or extra shapes")
        picture = pictures[0]
        left, top, right, bottom = _bbox(picture)
        if left < 0 or top < 0 or right > slide_w or bottom > slide_h:
            result.error(f"Slide {slide_index}: page image crosses the slide boundary")
        fills_canvas = left == 0 and top == 0 and right == slide_w and bottom == slide_h
        centered_contain = (
            (left == 0 and right == slide_w and top >= 0 and bottom <= slide_h)
            or (top == 0 and bottom == slide_h and left >= 0 and right <= slide_w)
        )
        if not fills_canvas and not centered_contain:
            result.error(f"Slide {slide_index}: page image is neither full-bleed nor correctly contained")


def _inspect_render_dir(render_dir: Path, slide_count: int, result: GateResult) -> None:
    if not render_dir.is_dir():
        result.error(f"Final render directory does not exist: {render_dir}")
        return
    rendered = sorted(
        render_dir.glob("slide-*.png"),
        key=lambda path: int(re.search(r"(\d+)$", path.stem).group(1)),
    )
    if len(rendered) != slide_count:
        result.error(f"Rendered slide count {len(rendered)} does not match PPTX slide count {slide_count}")
        return
    duplicate_pairs: list[str] = []
    previous_thumb = None
    for index, path in enumerate(rendered, start=1):
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                rgb = image.convert("RGB")
                extrema = rgb.convert("L").getextrema()
                if extrema and extrema[1] - extrema[0] < 3:
                    result.warn(f"Rendered slide {index} is nearly blank or monochrome")
                thumb = rgb.resize((64, 36))
                if previous_thumb is not None:
                    rms = ImageStat.Stat(ImageChops.difference(previous_thumb, thumb)).rms
                    if sum(rms) / len(rms) < 0.5:
                        duplicate_pairs.append(f"{index - 1}-{index}")
                previous_thumb = thumb
        except (OSError, ValueError) as exc:
            result.error(f"Rendered slide {index} is unreadable: {exc}")
    if duplicate_pairs:
        result.error("Adjacent rendered slides are visually identical: " + ", ".join(duplicate_pairs))
    result.metrics["rendered_slide_count"] = len(rendered)


def _inspect_project(
    project: Path,
    mode: str,
    slide_count: int,
    prs: Presentation,
    result: GateResult,
    render_dir: Path | None = None,
) -> None:
    required_files = ["design_spec.md", "spec_lock.md", "layout_plan.json"]
    for required in required_files:
        if not (project / required).is_file():
            result.error(f"Pipeline evidence missing: {required}")
    def page_sort_key(path: Path) -> tuple[int, str]:
        match = re.search(r"(\d+)$", path.stem)
        return (int(match.group(1)) if match else 10**9, path.name)

    svg_files = sorted((project / "svg_output").glob("*.svg"), key=page_sort_key)
    svg_count = len(svg_files)
    if svg_count != slide_count:
        result.error(f"svg_output contains {svg_count} page(s), expected {slide_count}")
    result.metrics["source_svg_count"] = svg_count

    svg_checker = SVGQualityChecker()
    svg_errors: list[str] = []
    for svg_file in svg_files:
        svg_result = svg_checker.check_file(str(svg_file))
        svg_errors.extend(
            f"{svg_file.name}: {message}"
            for message in svg_result.get("errors", [])
        )
    if svg_errors:
        for message in svg_errors[:16]:
            result.error(f"SVG quality: {message}")
        if len(svg_errors) > 16:
            result.error(f"SVG quality: {len(svg_errors) - 16} additional error(s)")
    result.metrics["source_svg_error_count"] = len(svg_errors)

    if render_dir is not None and render_dir.is_dir() and svg_count == slide_count:
        rendered = sorted(
            render_dir.glob("slide-*.png"),
            key=lambda path: int(re.search(r"(\d+)$", path.stem).group(1)),
        )
        blank_media_slots: list[str] = []
        inspected_slots = 0
        for slide_index, (svg_file, render_file) in enumerate(zip(svg_files, rendered), start=1):
            try:
                svg_root = ET.fromstring(svg_file.read_text(encoding="utf-8"))
                viewbox = [float(value) for value in (svg_root.get("viewBox") or "").split()]
                if len(viewbox) != 4:
                    continue
                vb_x, vb_y, vb_w, vb_h = viewbox
                with Image.open(render_file) as rendered_image:
                    rgb = rendered_image.convert("RGB")
                    scale_x = rgb.width / vb_w
                    scale_y = rgb.height / vb_h
                    for element in svg_root.iter():
                        if element.tag.rsplit("}", 1)[-1] != "image":
                            continue
                        x = float(element.get("x", vb_x))
                        y = float(element.get("y", vb_y))
                        width = float(element.get("width", 0))
                        height = float(element.get("height", 0))
                        if width <= 0 or height <= 0:
                            continue
                        left = max(0, int(round((x - vb_x) * scale_x)))
                        top = max(0, int(round((y - vb_y) * scale_y)))
                        right = min(rgb.width, int(round((x + width - vb_x) * scale_x)))
                        bottom = min(rgb.height, int(round((y + height - vb_y) * scale_y)))
                        if right <= left or bottom <= top:
                            continue
                        inspected_slots += 1
                        crop = rgb.crop((left, top, right, bottom)).resize((96, 54)).convert("L")
                        extrema = crop.getextrema()
                        stddev = ImageStat.Stat(crop).stddev[0]
                        if not extrema or extrema[1] - extrema[0] < 8 or stddev < 3.0:
                            blank_media_slots.append(
                                f"slide {slide_index} image region x={x:g}, y={y:g}, width={width:g}, height={height:g}"
                            )
            except (OSError, ValueError, ET.ParseError):
                continue
        if blank_media_slots:
            result.error(
                "Rendered SVG media region appears blank or unresolved: "
                + "; ".join(blank_media_slots[:8])
            )
        result.metrics["rendered_svg_media_slots"] = inspected_slots

    layout_plan_path = project / "layout_plan.json"
    if layout_plan_path.is_file():
        try:
            layout_payload = validate_layout_plan(
                load_layout_plan(layout_plan_path),
                svg_dir=project / "svg_output",
                expected_slide_count=slide_count,
            )
        except ValueError as exc:
            result.error(f"Layout plan: {exc}")
        else:
            for message in layout_payload["errors"]:
                result.error(f"Layout plan: {message}")
            for message in layout_payload["warnings"]:
                result.warn(f"Layout plan: {message}")
            result.metrics["layout_plan"] = layout_payload["metrics"]

    if mode == "editable":
        note_count = len([path for path in (project / "notes").glob("*.md") if path.name != "total.md"])
        if note_count != slide_count:
            result.error(f"notes contains {note_count} per-slide file(s), expected {slide_count}")
        result.metrics["per_slide_note_count"] = note_count

        manifest_path = project / "native_charts.json"
        if manifest_path.is_file():
            try:
                manifest = load_native_chart_manifest(manifest_path)
                expected = expected_native_chart_counts(manifest, svg_files)
            except NativeChartSpecError as exc:
                result.error(f"Native chart manifest: {exc}")
            else:
                actual: dict[int, int] = {}
                for index, slide in enumerate(prs.slides, start=1):
                    actual[index] = sum(
                        1
                        for shape in _walk_shapes(slide.shapes)
                        if shape.shape_type == MSO_SHAPE_TYPE.CHART
                    )
                for index, required_count in expected.items():
                    if actual.get(index, 0) < required_count:
                        result.error(
                            f"Slide {index}: native chart manifest requires {required_count} "
                            f"chart object(s), found {actual.get(index, 0)}"
                        )
                result.metrics["native_chart_expected"] = sum(expected.values())

        media_manifest_path = project / "embedded_media.json"
        if media_manifest_path.is_file():
            try:
                media_manifest = load_embedded_media_manifest(media_manifest_path)
                expected_media = expected_embedded_video_counts(
                    media_manifest,
                    svg_files,
                    media_manifest_path.parent,
                )
            except EmbeddedMediaSpecError as exc:
                result.error(f"Embedded media manifest: {exc}")
            else:
                actual_media: dict[int, int] = {}
                for index, slide in enumerate(prs.slides, start=1):
                    actual_media[index] = sum(
                        1
                        for shape in _walk_shapes(slide.shapes)
                        if shape.shape_type == MSO_SHAPE_TYPE.MEDIA
                    )
                for index, required_count in expected_media.items():
                    if actual_media.get(index, 0) < required_count:
                        result.error(
                            f"Slide {index}: embedded video manifest requires {required_count} "
                            f"media object(s), found {actual_media.get(index, 0)}"
                        )
                result.metrics["embedded_video_expected"] = sum(expected_media.values())


def run_gate(
    pptx_path: Path,
    *,
    mode: str = "editable",
    render_dir: Path | None = None,
    project: Path | None = None,
    min_score: int = 90,
) -> dict[str, object]:
    pptx_path = pptx_path.expanduser().resolve()
    result = GateResult(pptx=str(pptx_path), mode=mode)
    if not pptx_path.is_file():
        result.error(f"PPTX does not exist: {pptx_path}")
        return result.payload(min_score)
    result.metrics["pptx_sha256"] = hashlib.sha256(pptx_path.read_bytes()).hexdigest()
    result.metrics["pptx_size_bytes"] = pptx_path.stat().st_size

    for relationship_error in validate_relationships(pptx_path):
        result.error(relationship_error)

    try:
        prs = Presentation(str(pptx_path))
    except Exception as exc:  # python-pptx exposes several parse exception types
        result.error(f"PowerPoint package cannot be opened: {exc}")
        return result.payload(min_score)

    result.slide_count = len(prs.slides)
    if not prs.slides:
        result.error("Presentation has no slides")
        return result.payload(min_score)
    result.metrics["slide_size_emu"] = [int(prs.slide_width), int(prs.slide_height)]
    result.metrics["aspect_ratio"] = round(int(prs.slide_width) / int(prs.slide_height), 5)

    if mode == "auto":
        mode = _detect_mode(prs)
        result.mode = mode

    if mode == "full_page_image":
        _inspect_image_deck(prs, result)
    else:
        _inspect_font_portability(pptx_path, result)
        _inspect_character_spacing(pptx_path, result)
        _inspect_editable_deck(prs, result)
    if render_dir is not None:
        _inspect_render_dir(render_dir.expanduser().resolve(), result.slide_count, result)
    if project is not None:
        resolved_project = project.expanduser().resolve()
        resolved_render_dir = render_dir.expanduser().resolve() if render_dir is not None else None
        _inspect_project(
            resolved_project,
            mode,
            result.slide_count,
            prs,
            result,
            resolved_render_dir,
        )
        if mode == "full_page_image" and resolved_render_dir is not None:
            _inspect_visual_receipt(
                resolved_project,
                resolved_render_dir,
                result.slide_count,
                str(result.metrics["pptx_sha256"]),
                prs,
                result,
            )
    return result.payload(min_score)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--mode", choices=("auto", "editable", "full_page_image"), default="auto")
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--min-score", type=int, default=90)
    args = parser.parse_args()

    payload = run_gate(
        args.pptx,
        mode=args.mode,
        render_dir=args.render_dir,
        project=args.project,
        min_score=args.min_score,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
