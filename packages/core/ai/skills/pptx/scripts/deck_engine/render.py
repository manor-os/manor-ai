"""Render slide plans into a native, fully editable PPTX with python-pptx."""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from .icons import icon_shape_xml
from .vector import SHADOW_XML, custom_shape_xml, soft_edge_xml
from .model import CANVAS_H, CANVAS_W, EMU_PER_PX, Para, Run, Shape, SlidePlan, Style

_ALIGN = {"l": PP_ALIGN.LEFT, "ctr": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}
_ANCHOR = {"t": MSO_ANCHOR.TOP, "ctr": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}
_CHART_TYPES = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "stacked_column": XL_CHART_TYPE.COLUMN_STACKED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
    "doughnut": XL_CHART_TYPE.DOUGHNUT,
    "area": XL_CHART_TYPE.AREA,
    "stacked_area": XL_CHART_TYPE.AREA_STACKED,
    "stacked_bar": XL_CHART_TYPE.BAR_STACKED,
    "percent_column": XL_CHART_TYPE.COLUMN_STACKED_100,
    "percent_bar": XL_CHART_TYPE.BAR_STACKED_100,
    "radar": XL_CHART_TYPE.RADAR_MARKERS,
}
_PIE_TYPES = ("pie", "doughnut")
_AUTOSHAPES = {
    "rect": MSO_SHAPE.RECTANGLE,
    "round": MSO_SHAPE.ROUNDED_RECTANGLE,
    "ellipse": MSO_SHAPE.OVAL,
    "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "gradient": MSO_SHAPE.RECTANGLE,
    "text": MSO_SHAPE.RECTANGLE,
}


def _emu(px: int) -> Emu:
    return Emu(int(px) * EMU_PER_PX)


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _latin_face(style: Style, role: str) -> str:
    if role == "display":
        return style.display
    if role == "emphasis":
        return style.get("emphasis.font") or style.display
    if role == "mono":
        return style.get("fonts.mono") or "Consolas"
    return style.latin


def _set_fonts(run_properties, style: Style, role: str = "body") -> None:
    """Pin Latin and East Asian typefaces so no renderer falls back to defaults."""
    for tag in ("a:latin", "a:ea", "a:cs"):
        existing = run_properties.find(qn(tag))
        if existing is not None:
            run_properties.remove(existing)
    latin_face = _latin_face(style, role)
    latin = etree.SubElement(run_properties, qn("a:latin"))
    latin.set("typeface", latin_face)
    ea = etree.SubElement(run_properties, qn("a:ea"))
    ea.set("typeface", style.ea)
    cs = etree.SubElement(run_properties, qn("a:cs"))
    cs.set("typeface", latin_face)


def _fill_paragraphs(text_frame, paragraphs: tuple[Para, ...], style: Style) -> None:
    text_frame.clear()
    for index, para in enumerate(paragraphs):
        paragraph = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        paragraph.alignment = _ALIGN[para.align]
        if para.line_pt:
            paragraph.line_spacing = Pt(para.line_pt)
        paragraph.space_before = Pt(0)
        paragraph.space_after = Pt(para.space_after_pt)
        for spec in para.runs:
            run = paragraph.add_run()
            run.text = spec.text
            font = run.font
            font.size = Pt(spec.size)
            font.bold = spec.bold
            font.italic = spec.italic
            font.color.rgb = _rgb(spec.color)
            if spec.highlight:
                highlight = etree.SubElement(run._r.get_or_add_rPr(), qn("a:highlight"))
                etree.SubElement(highlight, qn("a:srgbClr")).set("val", spec.highlight)
            _set_fonts(run._r.get_or_add_rPr(), style, spec.font)


def _drop_style(shape) -> None:
    """Drop the theme style reference so no renderer adds shadows or outlines."""
    shape.shadow.inherit = False
    style = shape._element.find(qn("p:style"))
    if style is not None:
        shape._element.remove(style)


def _set_alpha(shape, alpha: int) -> None:
    if alpha >= 100:
        return
    fill = shape._element.spPr.find(qn("a:solidFill"))
    srgb = fill.find(qn("a:srgbClr")) if fill is not None else None
    if srgb is not None:
        etree.SubElement(srgb, qn("a:alpha")).set("val", str(int(alpha * 1000)))


def _apply_fill(shape, spec: Shape) -> None:
    if spec.kind == "gradient" and spec.gradient:
        fill = shape.fill
        fill.gradient()
        fill.gradient_angle = spec.gradient_angle
        gs_list = fill.gradient_stops._gsLst
        for stop in list(gs_list):
            gs_list.remove(stop)
        for color, position, *alpha in spec.gradient:
            gs = etree.SubElement(gs_list, qn("a:gs"))
            gs.set("pos", str(int(position * 1000)))
            srgb = etree.SubElement(gs, qn("a:srgbClr"))
            srgb.set("val", color)
            if alpha and alpha[0] < 100:
                etree.SubElement(srgb, qn("a:alpha")).set("val", str(int(alpha[0] * 1000)))
    elif spec.fill:
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(spec.fill)
        _set_alpha(shape, spec.alpha)
    else:
        shape.fill.background()
    if spec.line and spec.line_px:
        shape.line.color.rgb = _rgb(spec.line)
        shape.line.width = Emu(int(spec.line_px * EMU_PER_PX))
    else:
        shape.line.fill.background()


def _round_picture(shape, radius: int, box: tuple[int, int, int, int]) -> None:
    shape.auto_shape_type = MSO_SHAPE.ROUNDED_RECTANGLE
    geometry = shape._element.spPr.find(qn("a:prstGeom"))
    adjustments = geometry.find(qn("a:avLst"))
    if adjustments is None:
        adjustments = etree.SubElement(geometry, qn("a:avLst"))
    for child in list(adjustments):
        adjustments.remove(child)
    guide = etree.SubElement(adjustments, qn("a:gd"))
    guide.set("name", "adj")
    guide.set("fmla", f"val {int(min(50000, radius / max(1, min(box[2], box[3])) * 100000))}")


def _add_shadow(shape) -> None:
    sp_pr = shape._element.spPr
    existing = sp_pr.find(qn("a:effectLst"))
    if existing is not None:
        sp_pr.remove(existing)
    effect = etree.fromstring(SHADOW_XML)
    # effectLst follows fill and outline; scene3d/sp3d (never emitted) would follow it.
    sp_pr.append(effect)


def _add_shape(slide, spec: Shape, style: Style):
    x, y, w, h = (_emu(v) for v in spec.box)
    if spec.kind == "path":
        xml = custom_shape_xml(slide.shapes._next_shape_id, spec.name, spec.box, spec.path, fill=spec.fill, alpha=spec.alpha, line=spec.line, line_px=spec.line_px, soft=spec.soft)
        slide.shapes._spTree.append(etree.fromstring(xml))
        return None
    if spec.kind == "icon":
        xml = icon_shape_xml(slide.shapes._next_shape_id, spec.name, spec.icon.name, spec.box, spec.icon.color, faint=spec.alpha < 100)
        if xml:
            slide.shapes._spTree.append(etree.fromstring(xml))
        return None
    if spec.kind in _AUTOSHAPES and (spec.kind != "text" or spec.fill):
        autoshape = MSO_SHAPE.ROUNDED_RECTANGLE if spec.kind == "gradient" and spec.radius else _AUTOSHAPES[spec.kind]
        shape = slide.shapes.add_shape(autoshape, x, y, w, h)
        if spec.kind in ("round", "gradient") and spec.radius:
            shape.adjustments[0] = min(0.5, spec.radius / max(1, min(spec.box[2], spec.box[3])))
        _apply_fill(shape, spec)
        _drop_style(shape)
        if spec.rotation:
            shape.rotation = spec.rotation
    elif spec.kind == "text":
        shape = slide.shapes.add_textbox(x, y, w, h)
    elif spec.kind == "chart":
        shape = _add_chart(slide, spec, x, y, w, h)
    elif spec.kind == "table":
        shape = _add_table(slide, spec, style, x, y, w, h)
    elif spec.kind == "picture":
        shape = slide.shapes.add_picture(spec.picture.path, x, y, w, h)
        left, top, right, bottom = spec.picture.crop
        shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom = left, top, right, bottom
        if spec.picture.geometry == "circle":
            shape.auto_shape_type = MSO_SHAPE.OVAL
        elif spec.picture.geometry == "round" and spec.picture.radius:
            _round_picture(shape, spec.picture.radius, spec.box)
        elif spec.picture.geometry == "arch":
            shape.auto_shape_type = MSO_SHAPE.ROUND_2_SAME_RECTANGLE
            geometry = shape._element.spPr.find(qn("a:prstGeom"))
            adjustments = geometry.find(qn("a:avLst"))
            if adjustments is None:
                adjustments = etree.SubElement(geometry, qn("a:avLst"))
            for name, value in (("adj1", 50000), ("adj2", 0)):
                guide = etree.SubElement(adjustments, qn("a:gd"))
                guide.set("name", name)
                guide.set("fmla", f"val {value}")
    else:  # pragma: no cover - planner only emits the kinds above
        raise ValueError(f"unknown shape kind {spec.kind}")
    shape.name = spec.name
    if spec.soft:
        shape._element.spPr.append(etree.fromstring(soft_edge_xml(spec.soft).replace("<a:effectLst>", '<a:effectLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">')))
    elif spec.shadow:
        _add_shadow(shape)
    if spec.paragraphs:
        frame = shape.text_frame
        # Circle labels are short (numbers, initials); an ellipse only offers its
        # inscribed rectangle, so wrapping would stack "04" into two lines.
        frame.word_wrap = spec.kind != "ellipse"
        frame.auto_size = MSO_AUTO_SIZE.NONE
        frame.vertical_anchor = _ANCHOR[spec.anchor]
        frame.margin_left = frame.margin_right = _emu(spec.inset_x)
        frame.margin_top = frame.margin_bottom = _emu(spec.inset_y)
        _fill_paragraphs(frame, spec.paragraphs, style)
    return shape


def _style_text(font, color: str, size: float, name: str) -> None:
    font.size = Pt(size)
    font.color.rgb = _rgb(color)
    font.name = name


def _add_chart(slide, spec: Shape, x, y, w, h):
    chart_spec = spec.chart
    data = CategoryChartData(number_format=chart_spec.number_format)
    data.categories = list(chart_spec.categories)
    for name, values in chart_spec.series:
        data.add_series(name, list(values))
    frame = slide.shapes.add_chart(_CHART_TYPES[chart_spec.chart_type], x, y, w, h, data)
    chart = frame.chart
    chart.has_title = False
    chart.font.size = Pt(chart_spec.label_size)
    chart.font.name = chart_spec.font
    chart.font.color.rgb = _rgb(chart_spec.text_color)
    pie = chart_spec.chart_type in _PIE_TYPES
    if not chart_spec.axes:
        return _style_bare_chart(frame, chart_spec)
    chart.has_legend = chart_spec.show_legend
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        _style_text(chart.legend.font, chart_spec.text_color, 14, chart_spec.font)
    plot = chart.plots[0]
    if pie:
        for index, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(chart_spec.colors[index % len(chart_spec.colors)])
        if chart_spec.chart_type == "doughnut" and chart_spec.hole:
            _set_hole(chart, chart_spec.hole)
    else:
        for index, series in enumerate(plot.series):
            color = _rgb(chart_spec.colors[index % len(chart_spec.colors)])
            if chart_spec.chart_type in ("line", "radar"):
                series.format.line.color.rgb = color
                series.format.line.width = Pt(3)
                series.smooth = False
            else:
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
                if chart_spec.chart_type in ("area", "stacked_area") and len(plot.series) > 1:
                    _set_alpha_fill(series.format.fill, 85)
        if chart_spec.highlight >= 0 and len(plot.series) == 1 and chart_spec.chart_type in ("column", "bar"):
            point = plot.series[0].points[chart_spec.highlight]
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(chart_spec.highlight_color)
        if hasattr(plot, "gap_width"):
            plot.gap_width = 60 if chart_spec.chart_type in ("column", "bar") else 80
        if hasattr(plot, "overlap") and chart_spec.chart_type in ("stacked_column", "stacked_bar", "percent_column", "percent_bar"):
            plot.overlap = 100
        if chart_spec.chart_type == "radar":
            _style_radar(chart, chart_spec)
            return frame
        value_axis = chart.value_axis
        value_axis.has_major_gridlines = chart_spec.grid
        if chart_spec.grid:
            value_axis.major_gridlines.format.line.color.rgb = _rgb(chart_spec.grid_color)
        value_axis.format.line.fill.background()
        _style_text(value_axis.tick_labels.font, chart_spec.text_color, 14, chart_spec.font)
        value_axis.tick_labels.number_format = chart_spec.number_format
        value_axis.tick_labels.number_format_is_linked = False
        if chart_spec.unit:
            value_axis.has_title = True
            value_axis.axis_title.text_frame.text = chart_spec.unit
            _style_text(value_axis.axis_title.text_frame.paragraphs[0].runs[0].font, chart_spec.text_color, 14, chart_spec.font)
        category_axis = chart.category_axis
        category_axis.format.line.color.rgb = _rgb(chart_spec.grid_color)
        _style_text(category_axis.tick_labels.font, chart_spec.text_color, 14, chart_spec.font)
    if chart_spec.show_labels:
        plot.has_data_labels = True
        labels = plot.data_labels
        _style_text(labels.font, chart_spec.text_color, 14, chart_spec.font)
        percent = chart_spec.chart_type in ("percent_column", "percent_bar")
        labels.number_format = "0%" if pie else ("General" if percent else chart_spec.number_format)
        labels.number_format_is_linked = False
        if pie:
            labels.show_percentage = True
            labels.show_value = False
            labels.position = XL_LABEL_POSITION.OUTSIDE_END if chart_spec.chart_type == "pie" else XL_LABEL_POSITION.CENTER
        elif chart_spec.chart_type in ("column", "bar"):
            labels.position = XL_LABEL_POSITION.OUTSIDE_END
        elif chart_spec.chart_type in ("stacked_column", "stacked_bar", "percent_column", "percent_bar"):
            labels.position = XL_LABEL_POSITION.CENTER
    return frame


def _set_hole(chart, hole: int) -> None:
    plot_area = chart._chartSpace.chart.plotArea
    for doughnut in plot_area.iter(qn("c:doughnutChart")):
        size = doughnut.find(qn("c:holeSize"))
        if size is None:
            size = etree.SubElement(doughnut, qn("c:holeSize"))
        size.set("val", str(max(10, min(90, hole))))


def _set_alpha_fill(fill, alpha: int) -> None:
    srgb = fill._xPr.find(qn("a:solidFill"))
    srgb = srgb.find(qn("a:srgbClr")) if srgb is not None else None
    if srgb is not None:
        etree.SubElement(srgb, qn("a:alpha")).set("val", str(alpha * 1000))


def _style_radar(chart, chart_spec) -> None:
    category_axis = chart.category_axis
    _style_text(category_axis.tick_labels.font, chart_spec.text_color, chart_spec.label_size, chart_spec.font)
    value_axis = chart.value_axis
    value_axis.has_major_gridlines = True
    value_axis.major_gridlines.format.line.color.rgb = _rgb(chart_spec.grid_color)
    value_axis.format.line.color.rgb = _rgb(chart_spec.grid_color)
    _style_text(value_axis.tick_labels.font, chart_spec.text_color, chart_spec.label_size, chart_spec.font)
    value_axis.tick_labels.number_format = chart_spec.number_format
    value_axis.tick_labels.number_format_is_linked = False


def _style_bare_chart(frame, chart_spec):
    """Sparklines and progress rings: data only, no axes, legend, labels or grid."""
    chart = frame.chart
    chart.has_legend = False
    plot = chart.plots[0]
    if chart_spec.chart_type in _PIE_TYPES:
        for index, point in enumerate(plot.series[0].points):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(chart_spec.colors[index % len(chart_spec.colors)])
            point.format.line.fill.background()
        if chart_spec.chart_type == "doughnut":
            _set_hole(chart, chart_spec.hole or 72)
        return frame
    for index, series in enumerate(plot.series):
        color = _rgb(chart_spec.colors[index % len(chart_spec.colors)])
        if chart_spec.chart_type == "line":
            series.format.line.color.rgb = color
            series.format.line.width = Pt(2.5)
            series.smooth = True
            series.marker.style = None
            from pptx.enum.chart import XL_MARKER_STYLE

            series.marker.style = XL_MARKER_STYLE.NONE
        else:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = color
    values = [value for _, series_values in chart_spec.series for value in series_values]
    if values and chart_spec.chart_type == "line":
        low, high = min(values), max(values)
        pad = (high - low) * 0.12 or abs(high) * 0.1 or 1.0
        chart.value_axis.minimum_scale = low - pad
        chart.value_axis.maximum_scale = high + pad
    for axis in (chart.value_axis, chart.category_axis):
        axis.visible = False
        axis.has_major_gridlines = False
        axis.has_minor_gridlines = False
    return frame


def _add_table(slide, spec: Shape, style: Style, x, y, w, h):
    table_spec = spec.table
    rows, columns = len(table_spec.rows), len(table_spec.rows[0])
    frame = slide.shapes.add_table(rows, columns, x, y, w, h)
    table = frame.table
    table.first_row = True
    table.horz_banding = False
    # "No Style, No Grid": fills come from the plan, so no theme borders or
    # banding can clash with dark styles.
    style_id = frame._element.graphic.graphicData.tbl.tblPr.find(qn("a:tableStyleId"))
    if style_id is None:
        style_id = etree.SubElement(frame._element.graphic.graphicData.tbl.tblPr, qn("a:tableStyleId"))
    style_id.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"
    for index, width in enumerate(table_spec.col_widths):
        table.columns[index].width = _emu(width)
    for index, height in enumerate(table_spec.row_heights):
        table.rows[index].height = _emu(height)
    for row_index, row in enumerate(table_spec.rows):
        header = row_index == 0
        fill = table_spec.header_fill if header else table_spec.body_fills[(row_index - 1) % 2]
        color = table_spec.header_color if header else table_spec.body_color
        for column_index, value in enumerate(row):
            cell = table.cell(row_index, column_index)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(fill)
            cell.margin_left = cell.margin_right = _emu(10)
            cell.margin_top = cell.margin_bottom = _emu(6)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            _fill_paragraphs(
                cell.text_frame,
                (
                    Para(
                        runs=(Run(text=value, size=table_spec.size, color=color, bold=header),),
                        align=table_spec.aligns[column_index],
                        line_pt=round(table_spec.size * 1.3, 1),
                    ),
                ),
                style,
            )
    return frame


def render_deck(plans: list[SlidePlan], style: Style, output: Path, *, title: str) -> None:
    prs = Presentation()
    prs.slide_width = _emu(CANVAS_W)
    prs.slide_height = _emu(CANVAS_H)
    prs.core_properties.title = title
    blank = prs.slide_layouts[6]
    for plan in plans:
        slide = prs.slides.add_slide(blank)
        background = slide.background.fill
        background.solid()
        background.fore_color.rgb = _rgb(plan.background)
        for spec in plan.shapes:
            _add_shape(slide, spec, style)
        if plan.notes:
            slide.notes_slide.notes_text_frame.text = plan.notes
    output.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output))
