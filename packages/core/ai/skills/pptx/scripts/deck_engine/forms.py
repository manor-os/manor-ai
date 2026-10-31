"""Presentation forms beyond text: data shapes, diagrams, people and image mosaics,
plus the alternative compositions styles pick for the core layouts.

Everything here follows the same contract as ``layouts``: every text slot is
fitted by the conservative estimator, text never overlaps text, decoration is
drawn before the text it frames, and every shape stays inside the safe area.
Diagrams are native shapes (editable), measured data uses native charts.
"""

from __future__ import annotations

import math
from typing import Any

from .layouts import (
    CANVAS_W,
    CONTENT_W,
    GAP,
    MARGIN,
    SHAPE_PREFIX,
    SlideBuilder,
    _chart_spec,
    _columns,
    _framed,
    _header,
    _icon_mark,
    _point_parts,
    _rows,
    _sizes,
    blend,
    plain,
)
from .model import ChartSpec, Para, Run, Shape
from .schema import contrast_ratio
from .textfit import PX_TO_PT, Fit, FitError, block_height_px, text_width_pt


def _ink_on(b: SlideBuilder, fill: str) -> str:
    """The most legible of the style's ink colors on ``fill``."""
    candidates = [b.t.c("text"), "FFFFFF", b.t.c("on_primary"), "111111"]
    return max(candidates, key=lambda color: contrast_ratio(color, fill))


def _height(fit: Fit, index: int, leading: float) -> int:
    return block_height_px([fit.lines[index]], fit.size, leading, 0, 2)


def _tallest(fit: Fit, leading: float) -> int:
    return max(block_height_px([lines], fit.size, leading, 0, 2) for lines in fit.lines)


def _fmt_percent(value: float) -> str:
    return f"{value:g}%"


def _series_color(b: SlideBuilder, index: int) -> str:
    return b.t.chart[index % len(b.t.chart)]


# ── progress: bars and rings ───────────────────────────────────────


def _progress(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    items = slide["items"]
    if b.variant("progress", "bars") == "rings" and len(items) <= 4:
        return _progress_rings(b, slide)
    t, r = b.t, b.r
    y = _header(b, slide)
    label_w, value_w = 320, 120
    track_x = MARGIN + label_w + 32
    track_w = CONTENT_W - label_w - 32 - value_w - 24
    labels = [item["label"] for item in items]
    notes = [item.get("note", "") for item in items]
    label_fit = b.fit("item-label", labels, width_px=label_w, sizes=(22, 16), leading=1.25, max_lines=2)
    note_fit = b.fit("item-note", [n for n in notes if n], width_px=label_w, sizes=(15, 14), leading=1.35, max_lines=2) if any(notes) else None
    label_h = _tallest(label_fit, 1.25)
    note_h = _tallest(note_fit, 1.35) if note_fit else 0
    row_h = label_h + (6 + note_h if note_h else 0)
    available = b.bottom - y
    gap = min(44, (available - row_h * len(items)) // max(1, len(items) - 1)) if len(items) > 1 else 0
    if gap < 14:
        raise FitError(f"{b.label} items: {len(items)} rows need {row_h * len(items) + 14 * (len(items) - 1)}px but only {available}px remain; drop a row, the notes or the lead")
    value_fit = b.fit("item-value", [_fmt_percent(item["value"]) for item in items], width_px=value_w, sizes=(30, 20), leading=1.0, max_lines=1, display=True)
    top_value = max(item["value"] for item in items)
    note_index = 0
    for index, item in enumerate(items, start=1):
        ry = y + (index - 1) * (row_h + gap)
        b.text(f"item{index}-label", (MARGIN, ry, label_w, _height(label_fit, index - 1, 1.25)), [b.para(item["label"], label_fit, r["text"], bold=True)])
        if item.get("note") and note_fit is not None:
            b.text(f"item{index}-note", (MARGIN, ry + label_h + 6, label_w, _height(note_fit, note_index, 1.35)), [b.para(item["note"], note_fit, r["muted"])])
            note_index += 1
        bar_y = ry + int(label_fit.line_pt / PX_TO_PT) // 2 - 9
        b.rect(f"item{index}-track", (track_x, bar_y, track_w, 18), t.c("line"), radius=9, alpha=70)
        width = max(18, int(track_w * item["value"] / 100))
        color = _series_color(b, 1) if item["value"] == top_value else _series_color(b, 0)
        b.rect(f"item{index}-bar", (track_x, bar_y, width, 18), color, radius=9)
        b.text(f"item{index}-value", (CANVAS_W - MARGIN - value_w, bar_y + 9 - value_fit.tallest_px // 2, value_w, value_fit.tallest_px), [b.para(_fmt_percent(item["value"]), value_fit, r["text"], bold=t.display_bold, align="r", font="display")])


def _ring_spec(b: SlideBuilder, value: float, color: str) -> ChartSpec:
    return ChartSpec(
        chart_type="doughnut",
        categories=("value", "rest"),
        series=(("share", (float(value), float(max(0.0, 100 - value)))),),
        number_format="0",
        colors=(color, b.t.c("line")),
        text_color=b.r["text"],
        grid_color=b.t.c("line"),
        font=b.t.latin,
        show_legend=False,
        show_labels=False,
        axes=False,
        hole=74,
    )


def _progress_rings(b: SlideBuilder, slide: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    items = slide["items"]
    columns = _columns(len(items), gap=32)
    width = columns[0][1]
    label_fit = b.fit("item-label", [item["label"] for item in items], width_px=width, sizes=(22, 16), leading=1.25, max_lines=2)
    notes = [item.get("note", "") for item in items]
    note_fit = b.fit("item-note", [n for n in notes if n], width_px=width, sizes=(16, 14), leading=1.4, max_lines=3) if any(notes) else None
    text_h = _tallest(label_fit, 1.25) + (8 + _tallest(note_fit, 1.4) if note_fit else 0)
    ring = min(width - 24, 230, b.bottom - y - text_h - 24)
    if ring < 120:
        raise FitError(f"{b.label} items: only {ring}px left for the rings; drop the lead or the notes")
    value_fit = b.fit("item-value", [_fmt_percent(item["value"]) for item in items], width_px=int(ring * 0.62), sizes=(44, 22), leading=1.0, max_lines=1, display=True)
    note_index = 0
    for index, ((x, column), item) in enumerate(zip(columns, items), start=1):
        cx = x + column // 2
        box = (cx - ring // 2, y, ring, ring)
        b.plan.shapes.append(Shape(b._name(f"item{index}-ring"), "chart", box, chart=_ring_spec(b, item["value"], _series_color(b, index - 1))))
        vw = int(ring * 0.62)
        b.text(f"item{index}-value", (cx - vw // 2, y + ring // 2 - value_fit.tallest_px // 2, vw, value_fit.tallest_px), [b.para(_fmt_percent(item["value"]), value_fit, r["text"], bold=t.display_bold, align="ctr", font="display")])
        ty = y + ring + 24
        b.text(f"item{index}-label", (x, ty, column, _height(label_fit, index - 1, 1.25)), [b.para(item["label"], label_fit, r["text"], bold=True, align="ctr")])
        if item.get("note") and note_fit is not None:
            b.text(f"item{index}-note", (x, ty + _tallest(label_fit, 1.25) + 8, column, _height(note_fit, note_index, 1.4)), [b.para(item["note"], note_fit, r["muted"], align="ctr")])
            note_index += 1


# ── stacked diagrams: funnel and pyramid ───────────────────────────


def _trapezoid(width: int, height: int, top: float, bottom: float) -> str:
    """Path for a centred trapezoid in a ``width`` x ``height`` box."""
    lt, rt = (width - top) / 2, (width + top) / 2
    lb, rb = (width - bottom) / 2, (width + bottom) / 2
    return f"M{lt:.1f} 0 L{rt:.1f} 0 L{rb:.1f} {height} L{lb:.1f} {height} Z"


def _stack_diagram(b: SlideBuilder, slide: dict[str, Any], key: str, *, pyramid: bool) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    entries = slide[key]
    count = len(entries)
    gap = 8
    available = b.bottom - y
    stage_h = (available - gap * (count - 1)) // count
    if stage_h < 56:
        raise FitError(f"{b.label} {key}: {count} levels need at least {56 * count + gap * (count - 1)}px but only {available}px remain; drop the lead")
    shape_w = 540
    cx = MARGIN + shape_w // 2
    text_x = MARGIN + shape_w + 56
    text_w = CANVAS_W - MARGIN - text_x
    title_fit = b.fit(f"{key}-title", [entry["title"] for entry in entries], width_px=text_w, sizes=(22, 16), leading=1.25, max_lines=1)
    title_h = _tallest(title_fit, 1.25)
    texts = [entry.get("text", "") for entry in entries]
    text_fit = None
    if any(texts):
        text_fit = b.fit(f"{key}-text", [text for text in texts if text], width_px=text_w, sizes=(16, 14), leading=1.35, each_max_height_px=stage_h - title_h - 4)
    inner = [entry.get("value") or f"{index:02d}" for index, entry in enumerate(entries, start=1)]
    total_h = count * stage_h + gap * (count - 1)
    text_index = 0
    for index, entry in enumerate(entries, start=1):
        sy = y + (index - 1) * (stage_h + gap)
        if pyramid:
            top = shape_w * ((sy - y) / total_h)
            bottom = shape_w * ((sy - y + stage_h) / total_h)
        else:
            top = shape_w * (1 - 0.62 * (index - 1) / count)
            bottom = shape_w * (1 - 0.62 * index / count)
        box_w = int(max(top, bottom))
        fill = blend(t.c("primary"), r["bg"], 0.62 * (index - 1) / max(1, count - 1))
        b.path(f"{key}{index}-shape", (cx - box_w // 2, sy, box_w, stage_h), _trapezoid(box_w, stage_h, top, bottom), fill=fill)
        label_w = int(min(top, bottom) * 0.8) if min(top, bottom) > 120 else int(max(top, bottom) * 0.5)
        label_w = max(label_w, 90)
        size_cap = 30 if stage_h >= 80 else 22
        value_fit = b.fit(f"{key}{index}-value", [inner[index - 1]], width_px=label_w, sizes=(size_cap, 14), leading=1.0, max_lines=1, display=True)
        label_y = sy + (stage_h - value_fit.tallest_px) // 2 + (stage_h // 6 if pyramid and index == 1 else 0)
        b.text(f"{key}{index}-value", (cx - label_w // 2, label_y, label_w, value_fit.tallest_px), [b.para(inner[index - 1], value_fit, _ink_on(b, fill), bold=True, align="ctr", font="display")])
        rx = cx + int(max(top, bottom) / 2) + 8
        b.rect(f"{key}{index}-leader", (rx, sy + stage_h // 2, text_x - 16 - rx, 1), t.c("line"))
        block_h = title_h + (4 + _height(text_fit, text_index, 1.35) if entry.get("text") and text_fit else 0)
        ty = sy + (stage_h - block_h) // 2
        b.text(f"{key}{index}-title", (text_x, ty, text_w, title_h), [b.para(entry["title"], title_fit, r["text"], bold=True)])
        if entry.get("text") and text_fit is not None:
            b.text(f"{key}{index}-text", (text_x, ty + title_h + 4, text_w, _height(text_fit, text_index, 1.35)), [b.para(entry["text"], text_fit, r["muted"])])
            text_index += 1


def _funnel(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    _stack_diagram(b, slide, "stages", pyramid=False)


def _pyramid(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    _stack_diagram(b, slide, "levels", pyramid=True)


# ── cycle ───────────────────────────────────────────────────────────


def _cycle(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    steps = slide["steps"]
    count = len(steps)
    available = b.bottom - y
    node = 64
    radius = min(200, (available - node) // 2)
    if radius < 110:
        raise FitError(f"{b.label} steps: only {available}px left for the cycle; drop the lead")
    cx, cy = MARGIN + 40 + radius + node // 2, y + available // 2
    b.rect("ring", (cx - radius, cy - radius, 2 * radius, 2 * radius), None, kind="ellipse", line=t.c("line"), line_px=3)
    b.rect("ring-core", (cx - radius // 2, cy - radius // 2, radius, radius), t.c("surface"), kind="ellipse", alpha=int(t.card.get("alpha", 100)))
    core_text = plain(slide.get("kicker") or "")
    if core_text:
        core_fit = b.fit("ring-label", [core_text], width_px=int(radius * 0.8), sizes=(20, 14), leading=1.2, max_lines=2)
        b.text("ring-label", (cx - int(radius * 0.4), cy - core_fit.height_px // 2, int(radius * 0.8), core_fit.height_px), [b.para(core_text, core_fit, r["text"], bold=True, align="ctr")])
    number_fit = Fit(size=20, lines=[1], line_pt=24.0, height_px=node, tallest_px=node)
    for index in range(count):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        nx = int(cx + radius * math.cos(angle)) - node // 2
        ny = int(cy + radius * math.sin(angle)) - node // 2
        fill = _series_color(b, index)
        step = steps[index]
        if step.get("icon"):
            b.rect(f"step{index + 1}-node", (nx, ny, node, node), fill, kind="ellipse")
            b.icon(f"step{index + 1}-icon", step["icon"], (nx + 18, ny + 18, node - 36, node - 36), _ink_on(b, fill))
        else:
            b.text(f"step{index + 1}-node", (nx, ny, node, node), [b.para(f"{index + 1:02d}", number_fit, _ink_on(b, fill), bold=True, align="ctr", font="display")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
    list_x = cx + radius + node // 2 + 64
    list_w = CANVAS_W - MARGIN - list_x
    _numbered_list(b, "step", steps, x=list_x, y=y, width=list_w, max_height=available)


def _numbered_list(b: SlideBuilder, slot: str, entries: list[dict[str, Any]], *, x: int, y: int, width: int, max_height: int, colors: bool = True) -> int:
    """Numbered rows with hairlines: '01  Title / text'."""
    r = b.r
    count = len(entries)
    badge = 40
    text_w = width - badge - 18
    title_fit = b.fit(f"{slot}-title", [entry["title"] for entry in entries], width_px=text_w, sizes=(22, 16), leading=1.25, max_lines=2)
    texts = [entry.get("text", "") for entry in entries]
    gap = 22
    row_budget = (max_height - gap * (count - 1)) // count
    text_fit = None
    if any(texts):
        text_fit = b.fit(f"{slot}-text", [text for text in texts if text], width_px=text_w, sizes=(16, 14), leading=1.4, each_max_height_px=row_budget - _tallest(title_fit, 1.25) - 4)
    rows = []
    text_index = 0
    for index, entry in enumerate(entries):
        title_h = _height(title_fit, index, 1.25)
        text_h = 0
        if entry.get("text") and text_fit is not None:
            text_h = _height(text_fit, text_index, 1.4)
            text_index += 1
        rows.append((title_h, text_h))
    total = sum(max(badge, th + (4 + xh if xh else 0)) for th, xh in rows) + gap * (count - 1)
    cy = y + max(0, (max_height - total) // 2)
    number_fit = Fit(size=16, lines=[1], line_pt=20.0, height_px=badge, tallest_px=badge)
    text_index = 0
    for index, (entry, (title_h, text_h)) in enumerate(zip(entries, rows), start=1):
        fill = _series_color(b, index - 1) if colors else r["accent"]
        b.text(f"{slot}{index}-badge", (x, cy, badge, badge), [b.para(f"{index:02d}", number_fit, _ink_on(b, fill), bold=True, align="ctr")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
        b.text(f"{slot}{index}-title", (x + badge + 18, cy + max(0, (badge - title_h) // 2 if not text_h else 0), text_w, title_h), [b.para(entry["title"], title_fit, r["text"], bold=True)])
        if text_h and text_fit is not None:
            b.text(f"{slot}{index}-text", (x + badge + 18, cy + title_h + 4, text_w, text_h), [b.para(entry["text"], text_fit, r["muted"])])
            text_index += 1
        row = max(badge, title_h + (4 + text_h if text_h else 0))
        cy += row + gap
        if index < count:
            b.rect(f"{slot}{index}-rule", (x + badge + 18, cy - gap // 2, text_w, 1), b.t.c("line"))
    return cy - gap


# ── venn ────────────────────────────────────────────────────────────


def _venn(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    sets = slide["sets"]
    available = b.bottom - y
    area_w = 640
    if len(sets) == 2:
        diameter = min(available, 400)
        offset = int(diameter * 0.32)
        cx, cy = MARGIN + area_w // 2, y + available // 2
        centers = [(cx - offset, cy), (cx + offset, cy)]
        label_dx = [-int(diameter * 0.2), int(diameter * 0.2)]
        label_dy = [0, 0]
    else:
        diameter = min(int(available * 0.68), 340)
        offset = int(diameter * 0.3)
        cx, cy = MARGIN + area_w // 2, y + available // 2 + 10
        centers = [(cx - offset, cy - int(offset * 0.62)), (cx + offset, cy - int(offset * 0.62)), (cx, cy + int(offset * 0.95))]
        label_dx = [-int(diameter * 0.16), int(diameter * 0.16), 0]
        label_dy = [-int(diameter * 0.12), -int(diameter * 0.12), int(diameter * 0.18)]
    if diameter < 220:
        raise FitError(f"{b.label} sets: only {available}px left for the diagram; drop the lead")
    label_w = int(diameter * 0.46)
    title_fit = b.fit("set-label", [entry["title"] for entry in sets], width_px=label_w, sizes=(22, 15), leading=1.2, max_lines=2)
    fills = [_series_color(b, index) for index in range(len(sets))]
    for index, ((sx, sy), fill) in enumerate(zip(centers, fills), start=1):
        b.rect(f"set{index}-circle", (sx - diameter // 2, sy - diameter // 2, diameter, diameter), fill, kind="ellipse", alpha=58)
    for index, ((sx, sy), fill) in enumerate(zip(centers, fills), start=1):
        seen = blend(r["bg"], fill, 0.58)
        h = _height(title_fit, index - 1, 1.2)
        lx = sx + label_dx[index - 1] - label_w // 2
        ly = sy + label_dy[index - 1] - h // 2
        b.text(f"set{index}-label", (lx, ly, label_w, h), [b.para(sets[index - 1]["title"], title_fit, _ink_on(b, seen), bold=True, align="ctr")])
    if slide.get("center"):
        center_y = cy if len(sets) == 2 else cy - int(offset * 0.05)
        badge_w = min(int(diameter * 0.42), int(text_width_pt(slide["center"], 16) / PX_TO_PT / 0.9) + 32)
        fit = b.fit("center", [slide["center"]], width_px=badge_w - 20, sizes=(16, 14), leading=1.2, max_lines=1)
        h = fit.height_px + 12
        b.text("center", (cx - badge_w // 2, center_y - h // 2, badge_w, h), [b.para(slide["center"], fit, t.c("on_primary"), bold=True, align="ctr")], kind="round", fill=t.c("primary"), radius=h // 2, anchor="ctr", inset_x=10, inset_y=2)
    list_x = MARGIN + area_w + 48
    entries = [{"title": entry["title"], "text": entry.get("text", "")} for entry in sets]
    _swatch_list(b, "set", entries, fills, x=list_x, y=y, width=CANVAS_W - MARGIN - list_x, max_height=available)


def _swatch_list(b: SlideBuilder, slot: str, entries: list[dict[str, Any]], fills: list[str], *, x: int, y: int, width: int, max_height: int) -> None:
    r = b.r
    text_w = width - 40
    title_fit = b.fit(f"{slot}-title", [entry["title"] for entry in entries], width_px=text_w, sizes=(22, 16), leading=1.25, max_lines=2)
    texts = [entry.get("text", "") for entry in entries]
    gap = 28
    budget = (max_height - gap * (len(entries) - 1)) // len(entries)
    text_fit = b.fit(f"{slot}-text", [text for text in texts if text], width_px=text_w, sizes=(16, 14), leading=1.4, each_max_height_px=budget - _tallest(title_fit, 1.25) - 6) if any(texts) else None
    heights = []
    text_index = 0
    for index, entry in enumerate(entries):
        th = _height(title_fit, index, 1.25)
        xh = 0
        if entry.get("text") and text_fit is not None:
            xh = _height(text_fit, text_index, 1.4)
            text_index += 1
        heights.append((th, xh))
    total = sum(th + (6 + xh if xh else 0) for th, xh in heights) + gap * (len(entries) - 1)
    cy = y + max(0, (max_height - total) // 2)
    text_index = 0
    for index, (entry, fill, (th, xh)) in enumerate(zip(entries, fills, heights), start=1):
        b.rect(f"{slot}{index}-swatch", (x, cy + 6, 22, 22), fill, kind="ellipse")
        b.text(f"{slot}{index}-title", (x + 40, cy, text_w, th), [b.para(entry["title"], title_fit, r["text"], bold=True)])
        if xh and text_fit is not None:
            b.text(f"{slot}{index}-text", (x + 40, cy + th + 6, text_w, xh), [b.para(entry["text"], text_fit, r["muted"])])
            text_index += 1
        cy += th + (6 + xh if xh else 0) + gap


# ── people ──────────────────────────────────────────────────────────


def _initials(name: str) -> str:
    name = plain(name).strip()
    if not name:
        return "?"
    if any("㐀" <= ch <= "鿿" for ch in name):
        return name[-2:] if len(name) <= 3 else name[:1]
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2])


def _team(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    people = slide["people"]
    portraits = b.variant("team", "circles") == "portraits" and all(person.get("image") for person in people)
    y = _header(b, slide)
    columns = _columns(len(people), gap=28 if portraits else 40)
    width = columns[0][1]
    name_fit = b.fit("person-name", [person["name"] for person in people], width_px=width - (32 if portraits else 0), sizes=(24, 18), leading=1.2, max_lines=1)
    role_fit = b.fit("person-role", [person["role"] for person in people], width_px=width - (32 if portraits else 0), sizes=(16, 14), leading=1.3, max_lines=2)
    texts = [person.get("text", "") for person in people]
    text_fit = b.fit("person-text", [x for x in texts if x], width_px=width, sizes=(16, 14), leading=1.4, max_lines=3) if any(texts) else None
    name_h, role_h = _tallest(name_fit, 1.2), _tallest(role_fit, 1.3)
    text_h = _tallest(text_fit, 1.4) if text_fit else 0
    available = b.bottom - y
    text_index = 0
    if portraits:
        card_h = available - (text_h + 16 if text_h else 0)
        geometry, radius = _framed(b)
        for index, ((x, column), person) in enumerate(zip(columns, people), start=1):
            b.picture(f"person{index}-image", person["image"], (x, y, column, card_h), geometry="round" if geometry != "rect" else "rect", radius=max(radius, 22) if geometry != "rect" else 0, shadow=True)
            chip_h = name_h + role_h + 22
            b.rect(f"person{index}-chip", (x + 14, y + card_h - chip_h - 14, column - 28, chip_h), "FFFFFF", radius=16, alpha=88)
            b.text(f"person{index}-name", (x + 30, y + card_h - chip_h - 4, column - 60, name_h), [b.para(person["name"], name_fit, "111111", bold=True)])
            b.text(f"person{index}-role", (x + 30, y + card_h - chip_h - 2 + name_h, column - 60, role_h), [b.para(person["role"], role_fit, "444444")])
            if person.get("text") and text_fit is not None:
                b.text(f"person{index}-text", (x, y + card_h + 16, column, _height(text_fit, text_index, 1.4)), [b.para(person["text"], text_fit, r["muted"])])
                text_index += 1
        return
    diameter = min(width - 40, 210, available - name_h - role_h - text_h - 48)
    if diameter < 110:
        raise FitError(f"{b.label} people: only {available}px left; drop the lead or shorten the bios")
    for index, ((x, column), person) in enumerate(zip(columns, people), start=1):
        cx = x + column // 2
        box = (cx - diameter // 2, y, diameter, diameter)
        if person.get("image"):
            b.picture(f"person{index}-image", person["image"], box, geometry="circle", shadow=True)
        else:
            fill = _series_color(b, index - 1)
            initials = _initials(person["name"])
            fit = b.fit(f"person{index}-initials", [initials], width_px=int(diameter * 0.7), sizes=(48, 24), leading=1.0, max_lines=1, display=True)
            b.text(f"person{index}-initials", box, [b.para(initials, fit, _ink_on(b, fill), bold=True, align="ctr", font="display")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
        if person.get("image"):
            badge = 40
            b.rect(f"person{index}-badge", (cx + int(diameter * 0.30), y + diameter - badge - 6, badge, badge), t.c("accent"), kind="ellipse")
            b.icon(f"person{index}-badge-icon", "arrow-up-right", (cx + int(diameter * 0.30) + 10, y + diameter - badge + 4, badge - 20, badge - 20), t.c("on_accent"))
        ty = y + diameter + 22
        b.text(f"person{index}-name", (x, ty, column, name_h), [b.para(person["name"], name_fit, r["text"], bold=True, align="ctr")])
        b.text(f"person{index}-role", (x, ty + name_h + 2, column, role_h), [b.para(person["role"], role_fit, r["accent"], align="ctr")])
        if person.get("text") and text_fit is not None:
            b.text(f"person{index}-text", (x, ty + name_h + role_h + 10, column, _height(text_fit, text_index, 1.4)), [b.para(person["text"], text_fit, r["muted"], align="ctr")])
            text_index += 1


# ── gallery ─────────────────────────────────────────────────────────


def _gallery(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    y = _header(b, slide)
    images = slide["images"]
    available = b.bottom - y
    gap = 16
    geometry, radius = _framed(b)
    geometry = "round" if geometry != "rect" else "rect"
    radius = max(radius, 18) if geometry == "round" else 0
    count = len(images)
    boxes: list[tuple[int, int, int, int]]
    if b.variant("gallery", "mosaic") == "strip" or count == 3 and available < 300:
        columns = _columns(count, gap=gap)
        boxes = [(x, y, w, available) for x, w in columns]
    elif count == 3:
        big = int(CONTENT_W * 0.58)
        small_w = CONTENT_W - big - gap
        half = (available - gap) // 2
        boxes = [(MARGIN, y, big, available), (MARGIN + big + gap, y, small_w, half), (MARGIN + big + gap, y + half + gap, small_w, available - half - gap)]
    elif count == 4:
        big = int(CONTENT_W * 0.5)
        rest = CONTENT_W - big - gap
        half = (available - gap) // 2
        small = (rest - gap) // 2
        boxes = [
            (MARGIN, y, big, available),
            (MARGIN + big + gap, y, rest, half),
            (MARGIN + big + gap, y + half + gap, small, available - half - gap),
            (MARGIN + big + gap + small + gap, y + half + gap, rest - small - gap, available - half - gap),
        ]
    else:
        big = int(CONTENT_W * 0.4)
        rest = CONTENT_W - big - gap
        small = (rest - gap) // 2
        half = (available - gap) // 2
        boxes = [(MARGIN, y, big, available)] + [
            (MARGIN + big + gap + col * (small + gap), y + row * (half + gap), small if col == 0 else rest - small - gap, half if row == 0 else available - half - gap)
            for row in range(2)
            for col in range(2)
        ]
    for index, (entry, box) in enumerate(zip(images, boxes), start=1):
        b.picture(f"image{index}", entry["image"], box, geometry=geometry, radius=radius)
        if entry.get("caption"):
            x, by, w, h = box
            fit = b.fit(f"image{index}-caption", [entry["caption"]], width_px=min(w - 72, 360), sizes=(15, 14), leading=1.2, max_lines=1)
            cw = min(w - 32, max(96, int(text_width_pt(entry["caption"], fit.size) / PX_TO_PT / 0.85) + 40))
            ch = fit.height_px + 14
            b.text(f"image{index}-caption", (x + 16, by + h - ch - 16, cw, ch), [b.para(entry["caption"], fit, "111111", bold=True, align="ctr")], kind="round", fill="FFFFFF", radius=ch // 2, alpha=90, anchor="ctr", inset_x=12, inset_y=2)


def _fade(b: SlideBuilder, slot: str, box: tuple[int, int, int, int], *, radius: int = 0, share: float = 0.6, steps: int = 7) -> None:
    """A dark fade over the lower part of an image, as stacked translucent bands.

    Bands render identically everywhere (per-stop gradient alpha does not)."""
    x, y, w, h = box
    fade_h = int(h * share)
    for step in range(steps):
        top = y + h - fade_h + int(fade_h * step / steps)
        b.rect(f"{slot}{step + 1}", (x, top, w, y + h - top), "000000", radius=radius, alpha=13)


# ── alternative compositions for core layouts ───────────────────────


def agenda_typographic(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Cobalt 'CONT-ENTS': a huge title, items laid out in columns along the foot."""
    t, r = b.t, b.r
    title_bottom = b.title_block("title", MARGIN, b.top, 760, slide["title"], sizes=_sizes(b, "cover", (88, 48)), color=r["text"], leading=1.0)
    b.rect("deco-ring", (980, b.top + 10, 160, 160), None, kind="ellipse", line=r["muted"], line_px=1.5)
    b.icon("deco-arrow", "arrow-up-right", (1024, b.top + 54, 72, 72), r["accent"])
    items = slide["items"]
    per_col = 2 if len(items) <= 6 else 3
    cols = (len(items) + per_col - 1) // per_col
    columns = _columns(cols, gap=40)
    fit = b.fit("items", list(items), width_px=columns[0][1] - 40, sizes=(20, 15), leading=1.3, max_lines=2)
    row_h = _tallest(fit, 1.3) + 22
    y0 = max(title_bottom + 40, b.bottom - per_col * row_h)
    for index, item in enumerate(items, start=1):
        col, row = (index - 1) // per_col, (index - 1) % per_col
        x, width = columns[col]
        ry = y0 + row * row_h
        b.rect(f"item{index}-mark", (x, ry + int(fit.line_pt / PX_TO_PT) // 2 - 3, 18, 2), r["accent"])
        b.text(f"item{index}", (x + 32, ry, width - 40, _height(fit, index - 1, 1.3)), [b.para(item, fit, r["text"], bold=True)])


def agenda_cards(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    items = slide["items"]
    per_row = 3 if len(items) in (3, 5, 6) else (4 if len(items) in (4, 7) else 3)
    rows = (len(items) + per_row - 1) // per_row
    columns = _columns(per_row, gap=16)
    height = (b.bottom - y - 16 * (rows - 1)) // rows
    fit = b.fit("items", list(items), width_px=columns[0][1] - 48, sizes=(24, 16), leading=1.25, max_lines=2)
    radius = int(t.card.get("radius", 12))
    number_fit = b.fit("item-number", [f"{i:02d}" for i in range(1, len(items) + 1)], width_px=120, sizes=(44, 24), leading=1.0, max_lines=1, display=True)
    if height < number_fit.tallest_px + _tallest(fit, 1.25) + 56:
        raise FitError(f"{b.label} items: {len(items)} cards do not fit; drop the lead or an item")
    for index, item in enumerate(items, start=1):
        x, width = columns[(index - 1) % per_row]
        cy = y + ((index - 1) // per_row) * (height + 16)
        fill, ink, soft = b.card_style(index - 1)
        b.rect(f"item{index}-card", (x, cy, width, height), fill, radius=radius, alpha=int(t.card.get("alpha", 100)), shadow=True)
        b.text(f"item{index}-number", (x + 24, cy + 20, 120, number_fit.tallest_px), [b.para(f"{index:02d}", number_fit, ink, bold=t.display_bold, font="display")])
        h = _height(fit, index - 1, 1.25)
        b.text(f"item{index}", (x + 24, cy + height - h - 22, width - 48, h), [b.para(item, fit, ink, bold=True)])


def statement_centered(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, boxed: bool = False) -> None:
    """Starlit / neon: one centred message; 'boxed' puts the emphasis in highlight blocks."""
    from .layouts import _chrome, _photo_page

    if slide.get("image"):  # only photo-bleed styles reach here with a picture
        _photo_page(b, slide, side="center")
    r = b.r
    b.motifs(light=True)
    _chrome(b, deck)
    width = 1000
    x = (CANVAS_W - width) // 2
    sizes = _sizes(b, "statement", (60, 36))
    title = b.title_text(slide["title"])
    if boxed:
        title = title.upper() if b.t.title_case != "none" else title
    fit = b.fit("title", [title], width_px=width, sizes=sizes, leading=float(b.t.get("type.leading", 1.1)), max_lines=3 if boxed else 2, display=True)
    sub_fit = b.fit("subtitle", [slide["subtitle"]], width_px=760, sizes=(22, 16), leading=1.45, max_lines=3) if slide.get("subtitle") else None
    kicker_h = 40 if slide.get("kicker") else 0
    total = kicker_h + fit.height_px + (28 + sub_fit.height_px if sub_fit else 0)
    y = max(150, (720 - total) // 2)
    if slide.get("kicker"):
        b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, align="ctr")
        y += kicker_h
    marker = ink = None
    if boxed:
        marker = b.t.c("accent") if contrast_ratio(b.t.c("accent"), r["bg"]) >= 1.6 else r["text"]
        ink = _ink_on(b, marker)
    para = b.rich(title, fit, r["text"], bold=b.t.display_bold, align="ctr", highlight=marker, highlight_ink=ink)
    b.text("title", (x, y, width, fit.height_px), [para])
    y += fit.height_px
    if sub_fit is not None:
        b.text("subtitle", ((CANVAS_W - 760) // 2, y + 28, 760, sub_fit.height_px), [b.para(slide["subtitle"], sub_fit, r["muted"], align="ctr")])


def stats_steps(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Neon / cobalt-growth: cards rising left to right like a bar chart."""
    t, r = b.t, b.r
    y = _header(b, slide)
    stats = slide["stats"]
    count = len(stats)
    columns = _columns(count, gap=16)
    width = columns[0][1]
    pad = 24
    value_fit = b.fit("stat-value", [stat["value"] for stat in stats], width_px=width - 2 * pad, sizes=(64, 32), leading=1.0, max_lines=1, display=True)
    label_fit = b.fit("stat-label", [stat["label"] for stat in stats], width_px=width - 2 * pad, sizes=(18, 14), leading=1.3, max_lines=2)
    notes = [stat.get("note", "") for stat in stats]
    note_fit = b.fit("stat-note", [n for n in notes if n], width_px=width - 2 * pad, sizes=(15, 14), leading=1.35, max_lines=2) if any(notes) else None
    min_h = 2 * pad + value_fit.tallest_px + 16 + _tallest(label_fit, 1.3) + (6 + _tallest(note_fit, 1.35) if note_fit else 0)
    max_h = b.bottom - y
    if min_h > max_h:
        raise FitError(f"{b.label} stats: cards need {min_h}px but only {max_h}px remain; shorten labels or drop the lead")
    radius = int(t.card.get("radius", 16))
    note_index = 0
    for index, ((x, column), stat) in enumerate(zip(columns, stats), start=1):
        h = int(min_h + (max_h - min_h) * (index - 1) / max(1, count - 1)) if count > 1 else max_h
        cy = b.bottom - h
        fill, ink, soft = b.card_style(index - 1)
        if index == count:
            fill = t.c("accent")
            ink = soft = _ink_on(b, fill)
        b.rect(f"stat{index}-card", (x, cy, column, h), fill, radius=radius, shadow=True)
        b.text(f"stat{index}-value", (x + pad, cy + pad, column - 2 * pad, value_fit.tallest_px), [b.para(stat["value"], value_fit, ink, bold=t.display_bold, font="display")])
        lh = _height(label_fit, index - 1, 1.3)
        nh = 0
        if stat.get("note") and note_fit is not None:
            nh = _height(note_fit, note_index, 1.35)
        bottom = cy + h - pad
        if nh:
            b.text(f"stat{index}-note", (x + pad, bottom - nh, column - 2 * pad, nh), [b.para(stat["note"], note_fit, soft)])
            note_index += 1
            bottom -= nh + 6
        b.text(f"stat{index}-label", (x + pad, bottom - lh, column - 2 * pad, lh), [b.para(stat["label"], label_fit, ink, bold=True)])


def sparkline(b: SlideBuilder, slot: str, values: list[float], box: tuple[int, int, int, int], color: str) -> None:
    spec = ChartSpec(
        chart_type="line",
        categories=tuple(str(i + 1) for i in range(len(values))),
        series=(("trend", tuple(float(v) for v in values)),),
        number_format="General",
        colors=(color,),
        text_color=b.r["text"],
        grid_color=b.t.c("line"),
        font=b.t.latin,
        show_legend=False,
        show_labels=False,
        axes=False,
    )
    b.plan.shapes.append(Shape(b._name(slot), "chart", box, chart=spec))


def process_zigzag(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Steps alternate above and below one line, nodes on the line."""
    t, r = b.t, b.r
    y = _header(b, slide)
    steps = slide["steps"]
    count = len(steps)
    available = b.bottom - y
    line_y = y + available // 2
    columns = _columns(count, gap=16)
    width = min(columns[0][1] + 60, 300)
    title_fit = b.fit("step-title", [step["title"] for step in steps], width_px=width, sizes=(22, 16), leading=1.25, max_lines=2)
    texts = [step.get("text", "") for step in steps]
    half = available // 2 - 44
    text_fit = b.fit("step-text", [x for x in texts if x], width_px=width, sizes=(16, 14), leading=1.4, each_max_height_px=half - _tallest(title_fit, 1.25) - 6) if any(texts) else None
    b.rect("line", (MARGIN, line_y - 1, CONTENT_W, 2), t.c("line"))
    node = 44
    text_index = 0
    for index, ((x, column), step) in enumerate(zip(columns, steps), start=1):
        cx = x + column // 2
        fill = _series_color(b, index - 1)
        if step.get("icon"):
            b.rect(f"step{index}-node", (cx - node // 2, line_y - node // 2, node, node), fill, kind="ellipse")
            b.icon(f"step{index}-icon", step["icon"], (cx - 12, line_y - 12, 24, 24), _ink_on(b, fill))
        else:
            fit = Fit(size=16, lines=[1], line_pt=20.0, height_px=node, tallest_px=node)
            b.text(f"step{index}-node", (cx - node // 2, line_y - node // 2, node, node), [b.para(f"{index:02d}", fit, _ink_on(b, fill), bold=True, align="ctr")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
        th = _height(title_fit, index - 1, 1.25)
        xh = 0
        if step.get("text") and text_fit is not None:
            xh = _height(text_fit, text_index, 1.4)
        block = th + (6 + xh if xh else 0)
        tx = max(MARGIN, min(cx - width // 2, CANVAS_W - MARGIN - width))
        top = line_y - node // 2 - 20 - block if index % 2 else line_y + node // 2 + 20
        b.rect(f"step{index}-stem", (cx, top + block if index % 2 else line_y + node // 2, 1, 20), t.c("line"))
        b.text(f"step{index}-title", (tx, top, width, th), [b.para(step["title"], title_fit, r["text"], bold=True, align="ctr")])
        if xh:
            b.text(f"step{index}-text", (tx, top + th + 6, width, xh), [b.para(step["text"], text_fit, r["muted"], align="ctr")])
            text_index += 1


def process_growing(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Cobalt-growth: circles that grow step by step, titles beneath."""
    t, r = b.t, b.r
    y = _header(b, slide)
    steps = slide["steps"]
    count = len(steps)
    columns = _columns(count, gap=0)
    width = columns[0][1]
    title_fit = b.fit("step-title", [step["title"] for step in steps], width_px=width - 16, sizes=(22, 16), leading=1.25, max_lines=2)
    texts = [step.get("text", "") for step in steps]
    text_fit = b.fit("step-text", [x for x in texts if x], width_px=width - 16, sizes=(16, 14), leading=1.4, max_lines=3) if any(texts) else None
    text_h = _tallest(title_fit, 1.25) + (6 + _tallest(text_fit, 1.4) if text_fit else 0)
    biggest = min(int(width * 1.15), b.bottom - y - text_h - 24, 260)
    smallest = max(70, int(biggest * 0.42))
    if biggest < 110:
        raise FitError(f"{b.label} steps: only {b.bottom - y}px left; drop the lead or shorten step text")
    base = y + biggest
    text_index = 0
    for index, ((x, column), step) in enumerate(zip(columns, steps), start=1):
        d = int(smallest + (biggest - smallest) * (index - 1) / max(1, count - 1))
        cx = x + column // 2
        fill = t.c("accent") if index % 2 else t.c("primary")
        number_fit = b.fit(f"step{index}-number", [f"{index:02d}"], width_px=int(d * 0.7), sizes=(min(48, d // 2.4), 16), leading=1.0, max_lines=1, display=True)
        b.text(f"step{index}-node", (cx - d // 2, base - d, d, d), [b.para(f"{index:02d}", number_fit, _ink_on(b, fill), bold=t.display_bold, align="ctr", font="display")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
        ty = base + 24
        b.text(f"step{index}-title", (x + 8, ty, column - 16, _height(title_fit, index - 1, 1.25)), [b.para(step["title"], title_fit, r["text"], bold=True, align="ctr")])
        if step.get("text") and text_fit is not None:
            b.text(f"step{index}-text", (x + 8, ty + _tallest(title_fit, 1.25) + 6, column - 16, _height(text_fit, text_index, 1.4)), [b.para(step["text"], text_fit, r["muted"], align="ctr")])
            text_index += 1


def timeline_glow(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Starlit: soft glowing points on a hairline, dates set small above."""
    t, r = b.t, b.r
    y = _header(b, slide)
    events = slide["events"]
    columns = _columns(len(events), gap=16)
    width = columns[0][1]
    available = b.bottom - y
    line_y = y + int(available * 0.38)
    b.rect("axis", (MARGIN, line_y, CONTENT_W, 1), r["muted"], alpha=50)
    date_fit = b.fit("event-date", [event["date"] for event in events], width_px=width, sizes=(16, 14), leading=1.2, max_lines=1)
    title_fit = b.fit("event-title", [event["title"] for event in events], width_px=width, sizes=(24, 18), leading=1.25, max_lines=2, display=True)
    texts = [event.get("text", "") for event in events]
    text_fit = b.fit("event-text", [x for x in texts if x], width_px=width, sizes=(16, 14), leading=1.4, each_max_height_px=b.bottom - line_y - 40 - _tallest(title_fit, 1.25)) if any(texts) else None
    text_index = 0
    for index, ((x, column), event) in enumerate(zip(columns, events), start=1):
        cx = x + column // 2
        b.rect(f"event{index}-halo", (cx - 22, line_y - 22, 44, 44), r["accent"], kind="ellipse", alpha=18)
        b.rect(f"event{index}-dot", (cx - 7, line_y - 7, 14, 14), r["accent"], kind="ellipse")
        dh = date_fit.height_px
        b.text(f"event{index}-date", (x, line_y - 40 - dh, column, dh), [b.para(event["date"].upper(), date_fit, r["accent"], bold=True, align="ctr")])
        th = _height(title_fit, index - 1, 1.25)
        b.text(f"event{index}-title", (x, line_y + 36, column, th), [b.para(event["title"], title_fit, r["text"], align="ctr", font="display")])
        if event.get("text") and text_fit is not None:
            b.text(f"event{index}-text", (x, line_y + 36 + _tallest(title_fit, 1.25) + 8, column, _height(text_fit, text_index, 1.4)), [b.para(event["text"], text_fit, r["muted"], align="ctr")])
            text_index += 1


def timeline_vertical(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    events = slide["events"]
    available = b.bottom - y
    date_w = 200
    x_line = MARGIN + date_w + 28
    text_x = x_line + 36
    text_w = CANVAS_W - MARGIN - text_x
    date_fit = b.fit("event-date", [event["date"] for event in events], width_px=date_w, sizes=(26, 16), leading=1.15, max_lines=1, display=True)
    lines = [event["title"] + ("：" + event["text"] if event.get("text") else "") for event in events]
    gap = 14
    fit = b.fit("event-title", lines, width_px=text_w, sizes=(20, 14), leading=1.35, max_height_px=available - gap * (len(events) - 1), max_lines=2)
    heights = [max(date_fit.tallest_px, _height(fit, i, 1.35)) for i in range(len(events))]
    total = sum(heights) + gap * (len(events) - 1)
    cy = y + max(0, (available - total) // 2)
    b.rect("axis", (x_line, cy, 2, total), t.c("line"))
    for index, (event, h) in enumerate(zip(events, heights), start=1):
        b.text(f"event{index}-date", (MARGIN, cy, date_w, date_fit.tallest_px), [b.para(event["date"], date_fit, r["accent"], bold=t.display_bold, align="r", font="display")])
        b.rect(f"event{index}-dot", (x_line - 7, cy + date_fit.tallest_px // 2 - 8, 16, 16), r["accent"], kind="ellipse")
        title, text = event["title"], event.get("text", "")
        separator = "：" if any("㐀" <= ch <= "鿿" for ch in title + text) else ": "
        runs = [Run(text=title, size=fit.size, color=r["text"], bold=True)]
        if text:
            runs.append(Run(text=separator + text, size=fit.size, color=r["muted"]))
        b.text(f"event{index}-title", (text_x, cy + max(0, (date_fit.tallest_px - _height(fit, index - 1, 1.35)) // 2), text_w, _height(fit, index - 1, 1.35)), [Para(runs=tuple(runs), line_pt=fit.line_pt)])
        cy += h + gap


def image_cards_overlay(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Blue-lime: tall photo cards, copy set inside over a dark fade, number badges."""
    t, r = b.t, b.r
    y = _header(b, slide)
    items = slide["items"]
    columns = _columns(len(items), gap=16)
    width = columns[0][1]
    pad = 24
    height = b.bottom - y
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=width - 2 * pad, sizes=(24, 18), leading=1.25, max_lines=2)
    texts = [item.get("text", "") for item in items]
    text_fit = b.fit("item-text", [x for x in texts if x], width_px=width - 2 * pad, sizes=(16, 14), leading=1.4, max_lines=3) if any(texts) else None
    geometry, radius = _framed(b)
    radius = max(radius, 22) if geometry != "rect" else 0
    text_index = 0
    for index, ((x, column), item) in enumerate(zip(columns, items), start=1):
        b.picture(f"item{index}-image", item["image"], (x, y, column, height), geometry="round" if radius else "rect", radius=radius, shadow=True)
        _fade(b, f"item{index}-fade", (x, y, column, height), radius=radius)
        badge = 36
        fit = Fit(size=15, lines=[1], line_pt=18.0, height_px=badge, tallest_px=badge)
        b.text(f"item{index}-badge", (x + pad - 4, y + pad - 4, badge, badge), [b.para(str(index), fit, "111111", bold=True, align="ctr")], kind="ellipse", fill="FFFFFF", anchor="ctr", inset_x=0, inset_y=0)
        xh = 0
        if item.get("text") and text_fit is not None:
            xh = _height(text_fit, text_index, 1.4)
        th = _height(title_fit, index - 1, 1.25)
        bottom = y + height - pad - 44
        if xh:
            b.text(f"item{index}-text", (x + pad, bottom - xh, column - 2 * pad, xh), [b.para(item["text"], text_fit, "F2F2F2")])
            text_index += 1
            bottom -= xh + 8
        b.text(f"item{index}-title", (x + pad, bottom - th, column - 2 * pad, th), [b.para(item["title"], title_fit, "FFFFFF", bold=True)])
        accent = t.c("accent") if index % 2 else t.c("primary")
        b.rect(f"item{index}-arrow", (x + pad, y + height - pad - 36, 36, 36), accent, kind="ellipse")
        b.icon(f"item{index}-arrow-icon", "arrow-up-right", (x + pad + 9, y + height - pad - 27, 18, 18), _ink_on(b, accent))


def quote_centered(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    if slide.get("image"):
        from .layouts import _photo_page

        _photo_page(b, slide, side="center")
    r = b.r
    b.motifs()
    width = 980
    x = (CANVAS_W - width) // 2
    fit = b.fit("quote", [slide["quote"]], width_px=width, sizes=(40, 24), leading=1.35, max_lines=4, display=True)
    mark_fit = b.fit("deco-mark", ["“"], width_px=200, sizes=(120, 72), leading=1.0, max_lines=1, display=True)
    attribution_h = 40 if slide.get("attribution") else 0
    total = 80 + fit.height_px + attribution_h
    y = max(b.top + 40, (720 - total) // 2)
    b.text("deco-mark", ((CANVAS_W - 200) // 2, y - 30, 200, mark_fit.tallest_px), [b.para("“", mark_fit, r["accent"], bold=True, align="ctr", font="display")])
    y += 80
    b.text("quote", (x, y, width, fit.height_px), [b.para(slide["quote"], fit, r["text"], align="ctr", font="display")])
    if slide.get("attribution"):
        b.block("attribution", x, y + fit.height_px + 20, width, f"— {slide['attribution']}", sizes=(18, 14), color=r["muted"], max_lines=1, align="ctr")
    b.block("title", MARGIN, b.top, 600, slide["title"], sizes=(32, 32), color=r["muted"], max_lines=1, bold=False, display=True)


def bullets_lines(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, checklist: bool = False) -> None:
    """Mono 'measured rollout': numbered (or checked) rows separated by hairlines."""
    t, r = b.t, b.r
    y = _header(b, slide)
    points = slide["points"]
    available = b.bottom - y
    lead_w = 84
    title_w = 300
    parts = [_point_parts(point) for point in points]
    has_titles = all(title for title, _ in parts)
    text_x = MARGIN + lead_w + (title_w + 24 if has_titles else 0)
    text_w = CANVAS_W - MARGIN - text_x
    titles = [title for title, _ in parts]
    texts = [text or title for title, text in parts] if not has_titles else [text for _, text in parts]
    gap = 0
    text_fit = b.fit("point-text", [x for x in texts if x] or ["-"], width_px=text_w, sizes=(20, 14), leading=1.4, max_lines=2)
    title_fit = b.fit("point-title", titles, width_px=title_w, sizes=(24, 16), leading=1.25, max_lines=2) if has_titles else None
    row_h = max(_tallest(text_fit, 1.4), _tallest(title_fit, 1.25) if title_fit else 0) + 32
    if row_h * len(points) > available:
        raise FitError(f"{b.label} points: {len(points)} rows need {row_h * len(points)}px but only {available}px remain; shorten points or drop the lead")
    number_fit = None if checklist else b.fit("point-number", [f"{i:02d}" for i in range(1, len(points) + 1)], width_px=lead_w, sizes=(24, 16), leading=1.2, max_lines=1, display=True)
    text_index = 0
    for index, (title, text) in enumerate(parts, start=1):
        ry = y + (index - 1) * row_h
        b.rect(f"point{index}-rule", (MARGIN, ry, CONTENT_W, 1), t.c("line") if index > 1 else r["text"])
        if checklist:
            b.icon(f"point{index}-icon", "check", (MARGIN, ry + 18, 26, 26), r["accent"])
        else:
            b.text(f"point{index}-number", (MARGIN, ry + 16, lead_w, number_fit.tallest_px), [b.para(f"{index:02d}", number_fit, r["text"], font="display", bold=t.display_bold)])
        if title_fit is not None:
            b.text(f"point{index}-title", (MARGIN + lead_w, ry + 16, title_w, _height(title_fit, index - 1, 1.25)), [b.para(title, title_fit, r["text"], bold=True)])
        body = texts[index - 1]
        if body:
            b.text(f"point{index}", (text_x, ry + 18, text_w, _height(text_fit, text_index, 1.4)), [b.para(body, text_fit, r["muted"] if has_titles else r["text"])])
            text_index += 1


def closing_centered(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    from .layouts import _chrome, _photo_page

    if slide.get("image"):  # only photo-bleed styles reach here with a picture
        _photo_page(b, slide, side="center")
    r = b.r
    b.motifs(light=True)
    _chrome(b, deck)
    width = 1000
    x = (CANVAS_W - width) // 2
    sizes = _sizes(b, "section", (60, 40))
    title = b.title_text(slide["title"])
    fit = b.fit("title", [title], width_px=width, sizes=sizes, leading=float(b.t.get("type.leading", 1.1)), max_lines=2, display=True)
    y = 200 if slide.get("points") else 240
    if slide.get("kicker"):
        b.block("kicker", x, y - 44, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, align="ctr")
    b.text("title", (x, y, width, fit.height_px), [b.rich(title, fit, r["text"], bold=b.t.display_bold, align="ctr")])
    y += fit.height_px + 20
    if slide.get("subtitle"):
        y = b.block("subtitle", (CANVAS_W - 760) // 2, y, 760, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.45, max_lines=2, align="ctr") + 24
    points = [_point_parts(p)[0] or _point_parts(p)[1] for p in slide.get("points") or []]
    if points:
        widths = [min(360, int(text_width_pt(p, 16) / PX_TO_PT / 0.9) + 40) for p in points]
        total = sum(widths) + 12 * (len(widths) - 1)
        if total > CONTENT_W:
            raise FitError(f"{b.label} points: the {len(points)} closing points are too long for one row; keep each to about 12 characters")
        px = (CANVAS_W - total) // 2
        for index, (point, w) in enumerate(zip(points, widths), start=1):
            filled = index == 1
            b.pill(f"point{index}", px, y + 8, point, fill=b.t.c("accent") if filled else r["bg"], color=b.t.c("on_accent") if filled else r["text"], line=None if filled else r["text"], max_w=w, size=16)
            px += w + 12
    if slide.get("contact"):
        b.block("contact", x, 600, width, slide["contact"], sizes=(16, 14), color=r["muted"], max_lines=1, align="ctr")


def chart_split(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Sky-glass: the takeaways as a text column, the chart inside a soft card."""
    t, r = b.t, b.r
    takeaways = slide.get("takeaways") or []
    if not takeaways:
        from .layouts import _chart

        return _chart(b, slide, deck)
    text_w = 360
    y = _header(b, slide, width=CONTENT_W)
    height = b.bottom - y
    if height < 240:
        raise FitError(f"{b.label} chart: only {height}px left for the chart; drop the lead or kicker")
    _rows(b, "takeaway", takeaways, x=MARGIN, y=y + 8, width=text_w, max_height=height - 8, sizes=(22, 14))
    card_x = MARGIN + text_w + 40
    card_w = CANVAS_W - MARGIN - card_x
    radius = max(16, int(t.card.get("radius", 16)))
    b.rect("chart-card", (card_x, y, card_w, height), t.c("surface"), radius=radius, alpha=int(t.card.get("alpha", 100)), shadow=True)
    spec = _chart_spec(b, slide["chart"])
    b.plan.shapes.append(Shape(f"{SHAPE_PREFIX}chart", "chart", (card_x + 24, y + 20, card_w - 48, height - 40), chart=spec))


FORM_FUNCTIONS = {
    "progress": _progress,
    "funnel": _funnel,
    "pyramid": _pyramid,
    "cycle": _cycle,
    "venn": _venn,
    "team": _team,
    "gallery": _gallery,
}

# layout -> variant -> function. A layout's default composition lives in layouts.py.
VARIANT_FUNCTIONS = {
    ("agenda", "typographic"): agenda_typographic,
    ("agenda", "cards"): agenda_cards,
    ("statement", "centered"): statement_centered,
    ("statement", "boxed"): lambda b, s, d: statement_centered(b, s, d, boxed=True),
    ("stats", "steps"): stats_steps,
    ("process", "zigzag"): process_zigzag,
    ("process", "growing"): process_growing,
    ("timeline", "glow"): timeline_glow,
    ("timeline", "vertical"): timeline_vertical,
    ("image_cards", "overlay"): image_cards_overlay,
    ("quote", "centered"): quote_centered,
    ("bullets", "lines"): bullets_lines,
    ("bullets", "checklist"): lambda b, s, d: bullets_lines(b, s, d, checklist=True),
    ("closing", "centered"): closing_centered,
    ("chart", "split"): chart_split,
}

# Keynote and product-launch structures live in their own module.
from .signature import SIGNATURE_FUNCTIONS  # noqa: E402

VARIANT_FUNCTIONS.update(SIGNATURE_FUNCTIONS)

# Commercial and keynote page types (logo wall, pricing, device, roadmap ...).
from .pages import PAGE_FUNCTIONS, PAGE_VARIANTS  # noqa: E402

FORM_FUNCTIONS.update(PAGE_FUNCTIONS)
VARIANT_FUNCTIONS.update(PAGE_VARIANTS)
