"""Native PowerPoint chart support for the editable SVG export path.

The SVG remains the visual composition source.  A ``native_charts.json``
sidecar reserves chart slots and supplies structured data; this module overlays
real PowerPoint chart objects after the SVG shapes have been assembled.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET

from pptx import Presentation
from pptx.chart.data import BubbleChartData, CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import (
    XL_CHART_TYPE,
    XL_DATA_LABEL_POSITION,
    XL_LEGEND_POSITION,
    XL_MARKER_STYLE,
    XL_TICK_LABEL_POSITION,
)
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Emu, Pt

_CATEGORY_TYPES = {
    "column",
    "bar",
    "line",
    "area",
    "pie",
    "doughnut",
    "radar",
}
_ALL_TYPES = _CATEGORY_TYPES | {"scatter", "bubble"}

_LEGEND_POSITIONS = {
    "bottom": XL_LEGEND_POSITION.BOTTOM,
    "left": XL_LEGEND_POSITION.LEFT,
    "right": XL_LEGEND_POSITION.RIGHT,
    "top": XL_LEGEND_POSITION.TOP,
    "top_right": XL_LEGEND_POSITION.CORNER,
}
_LABEL_POSITIONS = {
    "above": XL_DATA_LABEL_POSITION.ABOVE,
    "below": XL_DATA_LABEL_POSITION.BELOW,
    "center": XL_DATA_LABEL_POSITION.CENTER,
    "inside_base": XL_DATA_LABEL_POSITION.INSIDE_BASE,
    "inside_end": XL_DATA_LABEL_POSITION.INSIDE_END,
    "outside_end": XL_DATA_LABEL_POSITION.OUTSIDE_END,
    "left": XL_DATA_LABEL_POSITION.LEFT,
    "right": XL_DATA_LABEL_POSITION.RIGHT,
}
_MARKER_STYLES = {
    "automatic": XL_MARKER_STYLE.AUTOMATIC,
    "circle": XL_MARKER_STYLE.CIRCLE,
    "diamond": XL_MARKER_STYLE.DIAMOND,
    "dot": XL_MARKER_STYLE.DOT,
    "none": XL_MARKER_STYLE.NONE,
    "plus": XL_MARKER_STYLE.PLUS,
    "square": XL_MARKER_STYLE.SQUARE,
    "star": XL_MARKER_STYLE.STAR,
    "triangle": XL_MARKER_STYLE.TRIANGLE,
    "x": XL_MARKER_STYLE.X,
}


class NativeChartSpecError(ValueError):
    """Raised when a native chart manifest cannot be exported safely."""


def load_native_chart_manifest(path: Path) -> dict[str, Any]:
    """Load and minimally validate a native chart manifest."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeChartSpecError(f"Cannot read native chart manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise NativeChartSpecError("native_charts.json must contain one JSON object")
    if payload.get("version") != 1:
        raise NativeChartSpecError("native_charts.json version must be 1")
    if not isinstance(payload.get("slides"), dict) or not payload["slides"]:
        raise NativeChartSpecError("native_charts.json slides must be a non-empty object")
    return payload


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_rgb(value: Any, field: str) -> RGBColor:
    text = str(value or "").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        raise NativeChartSpecError(f"{field} must be a six-digit hex color")
    return RGBColor.from_string(text[1:].upper())


def _optional_rgb(value: Any, field: str) -> RGBColor | None:
    return None if value in (None, "") else _parse_rgb(value, field)


def _chart_type(spec: dict[str, Any]):
    kind = str(spec.get("type") or "").strip().lower()
    default_grouping = "standard" if kind in {"area", "radar"} else "clustered"
    grouping = str(spec.get("grouping") or default_grouping).strip().lower()
    markers = bool(spec.get("markers", True))
    mapping = {
        ("column", "clustered"): XL_CHART_TYPE.COLUMN_CLUSTERED,
        ("column", "stacked"): XL_CHART_TYPE.COLUMN_STACKED,
        ("column", "percent_stacked"): XL_CHART_TYPE.COLUMN_STACKED_100,
        ("bar", "clustered"): XL_CHART_TYPE.BAR_CLUSTERED,
        ("bar", "stacked"): XL_CHART_TYPE.BAR_STACKED,
        ("bar", "percent_stacked"): XL_CHART_TYPE.BAR_STACKED_100,
        ("area", "standard"): XL_CHART_TYPE.AREA,
        ("area", "stacked"): XL_CHART_TYPE.AREA_STACKED,
        ("area", "percent_stacked"): XL_CHART_TYPE.AREA_STACKED_100,
        ("radar", "standard"): XL_CHART_TYPE.RADAR_MARKERS if markers else XL_CHART_TYPE.RADAR,
    }
    if kind == "line":
        return XL_CHART_TYPE.LINE_MARKERS if markers else XL_CHART_TYPE.LINE
    if kind == "pie":
        return XL_CHART_TYPE.PIE
    if kind == "doughnut":
        return XL_CHART_TYPE.DOUGHNUT
    if kind == "scatter":
        return XL_CHART_TYPE.XY_SCATTER if markers else XL_CHART_TYPE.XY_SCATTER_LINES_NO_MARKERS
    if kind == "bubble":
        return XL_CHART_TYPE.BUBBLE
    resolved = mapping.get((kind, grouping))
    if resolved is None:
        raise NativeChartSpecError(f"Unsupported native chart type/grouping: {kind or '<missing>'}/{grouping}")
    return resolved


def _category_chart_data(spec: dict[str, Any]) -> CategoryChartData:
    categories = _as_list(spec.get("categories"))
    series_specs = _as_list(spec.get("series"))
    if not categories:
        raise NativeChartSpecError("Category charts require non-empty categories")
    if not series_specs:
        raise NativeChartSpecError("Every native chart requires at least one series")
    data = CategoryChartData(number_format=str(spec.get("number_format") or "General"))
    data.categories = [str(value) for value in categories]
    for index, raw_series in enumerate(series_specs, start=1):
        series = _as_dict(raw_series)
        values = _as_list(series.get("values"))
        if len(values) != len(categories):
            raise NativeChartSpecError(f"Series {index} has {len(values)} values for {len(categories)} categories")
        try:
            numeric = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise NativeChartSpecError(f"Series {index} contains a non-numeric value") from exc
        data.add_series(str(series.get("name") or f"Series {index}"), numeric)
    return data


def _xy_chart_data(spec: dict[str, Any], *, bubble: bool):
    series_specs = _as_list(spec.get("series"))
    if not series_specs:
        raise NativeChartSpecError("Every native chart requires at least one series")
    data = BubbleChartData() if bubble else XyChartData()
    for index, raw_series in enumerate(series_specs, start=1):
        series = _as_dict(raw_series)
        x_values = _as_list(series.get("x_values"))
        y_values = _as_list(series.get("y_values"))
        sizes = _as_list(series.get("sizes")) if bubble else []
        expected = len(x_values)
        if not expected or len(y_values) != expected or (bubble and len(sizes) != expected):
            raise NativeChartSpecError(f"Series {index} x_values, y_values, and sizes must have matching lengths")
        data_series = data.add_series(str(series.get("name") or f"Series {index}"))
        try:
            for point_index, (x_value, y_value) in enumerate(zip(x_values, y_values)):
                if bubble:
                    data_series.add_data_point(float(x_value), float(y_value), float(sizes[point_index]))
                else:
                    data_series.add_data_point(float(x_value), float(y_value))
        except (TypeError, ValueError) as exc:
            raise NativeChartSpecError(f"Series {index} contains a non-numeric point") from exc
    return data


def _svg_viewbox_and_slots(svg_path: Path) -> tuple[tuple[float, float], dict[str, dict[str, float]]]:
    try:
        root = SafeET.parse(svg_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise NativeChartSpecError(f"Cannot parse SVG chart slots in {svg_path}: {exc}") from exc
    values = str(root.get("viewBox") or "").replace(",", " ").split()
    if len(values) != 4:
        raise NativeChartSpecError(f"SVG {svg_path.name} requires a four-number viewBox")
    try:
        _min_x, _min_y, canvas_width, canvas_height = map(float, values)
    except ValueError as exc:
        raise NativeChartSpecError(f"SVG {svg_path.name} has an invalid viewBox") from exc
    slots: dict[str, dict[str, float]] = {}
    for element in root.iter():
        slot_id = element.get("data-native-chart-slot")
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
            raise NativeChartSpecError(f"SVG {svg_path.name} chart slot {slot_id!r} has non-numeric geometry") from exc
        if slot["width"] <= 0 or slot["height"] <= 0:
            raise NativeChartSpecError(f"SVG {svg_path.name} chart slot {slot_id!r} must have positive dimensions")
        slots[str(slot_id)] = slot
    return (canvas_width, canvas_height), slots


def _slide_lookup(svg_files: Iterable[Path]) -> dict[str, tuple[int, Path]]:
    lookup: dict[str, tuple[int, Path]] = {}
    for index, path in enumerate(svg_files, start=1):
        lookup[path.stem] = (index, path)
        lookup[str(index)] = (index, path)
        lookup[f"P{index:02d}"] = (index, path)
    return lookup


def _resolve_position(
    spec: dict[str, Any],
    *,
    svg_path: Path,
    slide_width: int,
    slide_height: int,
) -> tuple[Emu, Emu, Emu, Emu]:
    (canvas_width, canvas_height), slots = _svg_viewbox_and_slots(svg_path)
    position = _as_dict(spec.get("position"))
    if not position:
        slot_id = str(spec.get("slot") or spec.get("id") or "")
        position = slots.get(slot_id, {})
        if not position:
            raise NativeChartSpecError(
                f"Chart {spec.get('id')!r} has no position and SVG slot {slot_id!r} was not found"
            )
    required = ("x", "y", "width", "height")
    try:
        x, y, width, height = (float(position[key]) for key in required)
    except (KeyError, TypeError, ValueError) as exc:
        raise NativeChartSpecError("Chart position requires numeric x, y, width, and height") from exc
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise NativeChartSpecError("Chart position must use non-negative x/y and positive dimensions")
    if x + width > canvas_width or y + height > canvas_height:
        raise NativeChartSpecError(f"Chart {spec.get('id')!r} crosses the SVG canvas boundary")
    return (
        Emu(round((x / canvas_width) * slide_width)),
        Emu(round((y / canvas_height) * slide_height)),
        Emu(round((width / canvas_width) * slide_width)),
        Emu(round((height / canvas_height) * slide_height)),
    )


def _set_font(font, config: dict[str, Any], prefix: str, defaults: dict[str, Any]) -> None:
    name = config.get("font_family", defaults.get("font_family"))
    size = config.get("font_size", defaults.get("font_size"))
    color = config.get("color", defaults.get("font_color"))
    if name:
        font.name = str(name)
    if size is not None:
        font.size = Pt(float(size))
    if color:
        font.color.rgb = _parse_rgb(color, f"{prefix}.color")
    if "bold" in config:
        font.bold = bool(config["bold"])


def _style_axis(axis, config: dict[str, Any], defaults: dict[str, Any], prefix: str) -> None:
    if not config:
        return
    if "show" in config:
        axis.visible = bool(config["show"])
    numeric_fields = {
        "minimum": "minimum_scale",
        "maximum": "maximum_scale",
        "major_unit": "major_unit",
    }
    for source, target in numeric_fields.items():
        if config.get(source) is not None:
            setattr(axis, target, float(config[source]))
    if config.get("number_format"):
        axis.tick_labels.number_format = str(config["number_format"])
    axis.tick_label_position = (
        XL_TICK_LABEL_POSITION.NEXT_TO_AXIS if config.get("show_labels", True) else XL_TICK_LABEL_POSITION.NONE
    )
    _set_font(axis.tick_labels.font, config, prefix, defaults)
    line_color = _optional_rgb(config.get("line_color"), f"{prefix}.line_color")
    if line_color:
        axis.format.line.color.rgb = line_color
    gridlines = bool(config.get("gridlines", False))
    axis.has_major_gridlines = gridlines
    if gridlines:
        grid_color = _optional_rgb(
            config.get("gridline_color", defaults.get("gridline_color")),
            f"{prefix}.gridline_color",
        )
        if grid_color:
            axis.major_gridlines.format.line.color.rgb = grid_color
        axis.major_gridlines.format.line.width = Pt(float(config.get("gridline_width", 1)))


def _style_series(chart, spec: dict[str, Any], defaults: dict[str, Any]) -> None:
    series_specs = [_as_dict(item) for item in _as_list(spec.get("series"))]
    kind = str(spec.get("type") or "").lower()
    for index, series in enumerate(chart.series):
        series_spec = series_specs[index]
        color = _optional_rgb(series_spec.get("color"), f"series[{index}].color")
        if kind in {"line", "scatter"}:
            line_color = _optional_rgb(
                series_spec.get("line_color") or series_spec.get("color"),
                f"series[{index}].line_color",
            )
            if line_color:
                series.format.line.color.rgb = line_color
            series.format.line.width = Pt(float(series_spec.get("line_width", 2.5)))
        elif color:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = color
            series.format.line.fill.background()
        marker_name = str(series_spec.get("marker") or "circle").lower()
        if hasattr(series, "marker") and marker_name in _MARKER_STYLES:
            series.marker.style = _MARKER_STYLES[marker_name]
            series.marker.size = int(series_spec.get("marker_size", 7))
            if color:
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = color
                series.marker.format.line.color.rgb = color

    plot = chart.plots[0]
    if hasattr(plot, "gap_width") and spec.get("gap_width") is not None:
        plot.gap_width = int(spec["gap_width"])
    if hasattr(plot, "overlap") and spec.get("overlap") is not None:
        plot.overlap = int(spec["overlap"])

    label_config = _as_dict(spec.get("data_labels"))
    if label_config.get("show"):
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.show_value = bool(label_config.get("show_value", True))
        labels.show_category_name = bool(label_config.get("show_category", False))
        labels.show_series_name = bool(label_config.get("show_series", False))
        labels.show_percentage = bool(label_config.get("show_percentage", False))
        position = str(label_config.get("position") or "outside_end").lower()
        if position not in _LABEL_POSITIONS:
            raise NativeChartSpecError(f"Unsupported data label position: {position}")
        labels.position = _LABEL_POSITIONS[position]
        _set_font(labels.font, label_config, "data_labels", defaults)


def _add_chart(slide, spec: dict[str, Any], svg_path: Path, defaults: dict[str, Any]) -> None:
    kind = str(spec.get("type") or "").strip().lower()
    if kind not in _ALL_TYPES:
        raise NativeChartSpecError(f"Unsupported native chart type: {kind or '<missing>'}")
    if kind in _CATEGORY_TYPES:
        chart_data = _category_chart_data(spec)
    else:
        chart_data = _xy_chart_data(spec, bubble=kind == "bubble")
    left, top, width, height = _resolve_position(
        spec,
        svg_path=svg_path,
        slide_width=int(slide.part.package.presentation_part.presentation.slide_width),
        slide_height=int(slide.part.package.presentation_part.presentation.slide_height),
    )
    chart_shape = slide.shapes.add_chart(_chart_type(spec), left, top, width, height, chart_data)
    chart_shape.name = f"Manor Native Chart · {spec.get('id') or kind}"
    chart = chart_shape.chart
    _set_font(chart.font, _as_dict(spec.get("style")), "style", defaults)

    title = str(spec.get("title") or "").strip()
    chart.has_title = bool(title)
    if title:
        chart.chart_title.text_frame.text = title
        _set_font(
            chart.chart_title.text_frame.paragraphs[0].runs[0].font,
            _as_dict(spec.get("title_style")),
            "title_style",
            defaults,
        )

    legend_config = _as_dict(spec.get("legend"))
    chart.has_legend = bool(legend_config.get("show", len(chart.series) > 1))
    if chart.has_legend:
        position = str(legend_config.get("position") or "bottom").lower()
        if position not in _LEGEND_POSITIONS:
            raise NativeChartSpecError(f"Unsupported legend position: {position}")
        chart.legend.position = _LEGEND_POSITIONS[position]
        chart.legend.include_in_layout = False
        _set_font(chart.legend.font, legend_config, "legend", defaults)

    _style_series(chart, spec, defaults)
    if kind not in {"pie", "doughnut"}:
        _style_axis(
            chart.category_axis,
            _as_dict(spec.get("category_axis")),
            defaults,
            "category_axis",
        )
        _style_axis(
            chart.value_axis,
            _as_dict(spec.get("value_axis")),
            defaults,
            "value_axis",
        )


def expected_native_chart_counts(manifest: dict[str, Any], svg_files: Iterable[Path]) -> dict[int, int]:
    """Validate the manifest and return counts indexed by one-based slide number."""
    lookup = _slide_lookup(svg_files)
    counts: dict[int, int] = {}
    for slide_key, raw_specs in _as_dict(manifest.get("slides")).items():
        resolved = lookup.get(str(slide_key))
        if resolved is None:
            raise NativeChartSpecError(f"Unknown slide key in native_charts.json: {slide_key}")
        specs = _as_list(raw_specs)
        if not specs:
            raise NativeChartSpecError(f"Slide {slide_key} must declare at least one chart")
        index, svg_path = resolved
        ids: set[str] = set()
        for raw_spec in specs:
            spec = _as_dict(raw_spec)
            chart_id = str(spec.get("id") or "").strip()
            if not chart_id:
                raise NativeChartSpecError(f"Slide {slide_key} contains a chart without an id")
            if chart_id in ids:
                raise NativeChartSpecError(f"Slide {slide_key} repeats chart id {chart_id!r}")
            ids.add(chart_id)
            kind = str(spec.get("type") or "").strip().lower()
            if kind not in _ALL_TYPES:
                raise NativeChartSpecError(f"Unsupported native chart type: {kind or '<missing>'}")
            _chart_type(spec)
            if kind in _CATEGORY_TYPES:
                _category_chart_data(spec)
            else:
                _xy_chart_data(spec, bubble=kind == "bubble")
            _resolve_position(
                spec,
                svg_path=svg_path,
                slide_width=1_280_000,
                slide_height=720_000,
            )
        counts[index] = counts.get(index, 0) + len(specs)
    return counts


def apply_native_charts(
    pptx_path: Path,
    manifest: dict[str, Any],
    svg_files: list[Path],
) -> dict[str, Any]:
    """Overlay manifest charts onto ``pptx_path`` and replace it atomically."""
    lookup = _slide_lookup(svg_files)
    defaults = _as_dict(manifest.get("defaults"))
    presentation = Presentation(str(pptx_path))
    if len(presentation.slides) != len(svg_files):
        raise NativeChartSpecError(f"PPTX has {len(presentation.slides)} slides for {len(svg_files)} SVG pages")

    chart_count = 0
    chart_counts_by_slide: dict[str, int] = {}
    for slide_key, raw_specs in _as_dict(manifest.get("slides")).items():
        resolved = lookup.get(str(slide_key))
        if resolved is None:
            raise NativeChartSpecError(f"Unknown slide key in native_charts.json: {slide_key}")
        slide_index, svg_path = resolved
        specs = _as_list(raw_specs)
        if not specs:
            raise NativeChartSpecError(f"Slide {slide_key} must declare at least one chart")
        ids: set[str] = set()
        for raw_spec in specs:
            spec = _as_dict(raw_spec)
            chart_id = str(spec.get("id") or "").strip()
            if not chart_id:
                raise NativeChartSpecError(f"Slide {slide_key} contains a chart without an id")
            if chart_id in ids:
                raise NativeChartSpecError(f"Slide {slide_key} repeats chart id {chart_id!r}")
            ids.add(chart_id)
            _add_chart(presentation.slides[slide_index - 1], spec, svg_path, defaults)
            chart_count += 1
        chart_counts_by_slide[f"P{slide_index:02d}"] = len(specs)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{pptx_path.stem}.native-charts-",
        suffix=".pptx",
        dir=pptx_path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        presentation.save(str(temp_path))
        verified = Presentation(str(temp_path))
        actual_count = sum(
            1 for slide in verified.slides for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.CHART
        )
        if actual_count < chart_count:
            raise NativeChartSpecError(
                f"Native chart verification found {actual_count}, expected at least {chart_count}"
            )
        os.replace(temp_path, pptx_path)
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "native_chart_count": chart_count,
        "native_chart_counts_by_slide": chart_counts_by_slide,
    }
