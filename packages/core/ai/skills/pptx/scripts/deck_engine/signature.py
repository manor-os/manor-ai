"""Signature compositions: keynote and product-launch page structures.

Each is an alternative ``variant`` for a core layout, picked by a style (or by a
slide's own ``variant``). They follow the ``layouts`` contract: every text slot
is fitted, text never overlaps text, decoration sits behind what it frames, and
everything stays inside the safe area. Body geometry reads the page frame
(``body_x``/``body_w``) so a style's frame still applies.
"""

from __future__ import annotations

from typing import Any

from .layouts import (
    CANVAS_W,
    SlideBuilder,
    _columns,
    _header,
    _photo_page,
    _point_parts,
    _sizes,
    blend,
    body_w,
    body_x,
)
from .model import Shape
from .textfit import FitError, block_height_px


def _h(fit, index: int, leading: float) -> int:
    return block_height_px([fit.lines[index]], fit.size, leading, 0, 2)


def _tallest(fit, leading: float) -> int:
    return max(block_height_px([lines], fit.size, leading, 0, 2) for lines in fit.lines)


def _tile_fill(b: SlideBuilder) -> tuple[str, str, str]:
    """A raised tile on the page: the style's card colours."""
    return b.card_style(0)


# ── stats ───────────────────────────────────────────────────────────


def stats_hero(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote: numbers as the only image, huge and centred, each with a small grey label."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 24
    stats = slide["stats"]
    per_row = len(stats) if body_w() >= 900 or len(stats) <= 2 else 2
    columns = _columns(per_row, gap=40)
    width = columns[0][1]
    big = (168, 72) if len(stats) == 1 else (112, 44) if per_row == 2 else (88, 36)
    value_fit = b.fit("stat-value", [s["value"] for s in stats], width_px=width, sizes=big, leading=1.0, max_lines=1, display=True)
    label_fit = b.fit("stat-label", [s["label"] for s in stats], width_px=width, sizes=(20, 14), leading=1.3, max_lines=2)
    notes = [s.get("note", "") for s in stats]
    note_fit = b.fit("stat-note", [n for n in notes if n], width_px=width, sizes=(16, 14), leading=1.4, max_lines=2) if any(notes) else None
    block = value_fit.tallest_px + 14 + _tallest(label_fit, 1.3) + (8 + _tallest(note_fit, 1.4) if note_fit else 0)
    rows = (len(stats) + per_row - 1) // per_row
    space = b.bottom - y
    if rows * block + (rows - 1) * 32 > space:
        raise FitError(f"{b.label} stats: the numbers need more height than the {space}px left; drop the lead or the notes")
    first = y + (space - rows * block - (rows - 1) * 32) // 2
    note_index = 0
    for index, stat in enumerate(stats, start=1):
        x, column = columns[(index - 1) % per_row]
        top = first + ((index - 1) // per_row) * (block + 32)
        color = r["accent"] if index == 1 and len(stats) > 1 else r["text"]
        b.text(f"stat{index}-value", (x, top, column, value_fit.tallest_px), [b.para(stat["value"], value_fit, color, bold=t.display_bold, align="ctr", font="display")])
        cy = top + value_fit.tallest_px + 14
        b.text(f"stat{index}-label", (x, cy, column, _h(label_fit, index - 1, 1.3)), [b.para(stat["label"], label_fit, r["muted"], align="ctr")])
        if stat.get("note") and note_fit is not None:
            cy += _tallest(label_fit, 1.3) + 8
            b.text(f"stat{index}-note", (x, cy, column, _h(note_fit, note_index, 1.4)), [b.para(stat["note"], note_fit, r["muted"], align="ctr")])
            note_index += 1


def stats_bento(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Bento: the first number on a large tile, the rest on smaller tiles beside or below it."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 8
    stats = slide["stats"]
    x0, width, height = body_x(), body_w(), b.bottom - y
    gap = 18
    radius = int(t.card.get("radius", 20)) or 20
    fill, ink, soft = _tile_fill(b)
    rest = stats[1:]

    def side() -> list[tuple[int, int, int, int]]:
        big_w = width if not rest else int(width * 0.56)
        tiles = [(x0, y, big_w, height)]
        if rest:
            small_x, small_w = x0 + big_w + gap, width - big_w - gap
            small_h = (height - gap * (len(rest) - 1)) // len(rest)
            tiles += [(small_x, y + i * (small_h + gap), small_w, small_h) for i in range(len(rest))]
        return tiles

    def stacked() -> list[tuple[int, int, int, int]]:
        big_h = int(height * 0.5)
        small_w = (width - gap * (len(rest) - 1)) // len(rest)
        return [(x0, y, width, big_h)] + [(x0 + i * (small_w + gap), y + big_h + gap, small_w, height - big_h - gap) for i in range(len(rest))]

    def fitted(tiles):
        pad = 24
        small_w = min(w for _, _, w, _ in tiles[1:]) if rest else tiles[0][2]
        label_fit = b.fit("stat-label", [s["label"] for s in stats], width_px=small_w - 2 * pad, sizes=(20, 14), leading=1.3, max_lines=2)
        label_h = _tallest(label_fit, 1.3)
        room = [h - 2 * pad - 12 - label_h for _, _, _, h in tiles]
        if min(room) < 40:
            return pad, None, None, label_fit, False
        # Numbers are as large as their tile allows, by width and by height.
        big_fit = b.fit("stat1-value", [stats[0]["value"]], width_px=tiles[0][2] - 2 * pad, sizes=(150, 36), leading=1.0, max_lines=1,
                        each_max_height_px=room[0], display=True)
        small_fit = b.fit("stat-value", [s["value"] for s in rest], width_px=small_w - 2 * pad, sizes=(64, 28), leading=1.0, max_lines=1,
                          each_max_height_px=min(room[1:]), display=True) if rest else None
        return pad, big_fit, small_fit, label_fit, True

    tiles = side() if len(rest) <= 2 else stacked()
    pad, big_fit, small_fit, label_fit, ok = fitted(tiles)
    if not ok and rest and len(rest) <= 2:
        tiles = stacked()
        pad, big_fit, small_fit, label_fit, ok = fitted(tiles)
    if not ok:
        raise FitError(f"{b.label} stats: the tiles are too short for their numbers and labels; drop the lead or a number")
    for index, (stat, (x, ty, w, h)) in enumerate(zip(stats, tiles), start=1):
        b.rect(f"stat{index}-tile", (x, ty, w, h), fill, radius=radius, shadow=True)
        fit = big_fit if index == 1 else small_fit
        lh = _h(label_fit, index - 1, 1.3)
        color = r["accent"] if index == 1 else ink
        b.text(f"stat{index}-label", (x + pad, ty + pad, w - 2 * pad, lh), [b.para(stat["label"], label_fit, soft, bold=True)])
        b.text(f"stat{index}-value", (x + pad, ty + h - pad - fit.tallest_px, w - 2 * pad, fit.tallest_px), [b.para(stat["value"], fit, color, bold=t.display_bold, font="display")])


def stats_metrics(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Product-launch metrics: accent numerals divided by hairlines, a small caption under each."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 32
    stats = slide["stats"]
    per_row = len(stats) if body_w() >= 900 or len(stats) <= 2 else 2
    columns = _columns(per_row, gap=0)
    pad = 28 if per_row < 4 else 20
    inner = columns[0][1] - 2 * pad
    value_fit = b.fit("stat-value", [s["value"] for s in stats], width_px=inner, sizes=(80, 32), leading=1.0, max_lines=1, display=True)
    label_fit = b.fit("stat-label", [s["label"] for s in stats], width_px=inner, sizes=(20, 14), leading=1.3, max_lines=2)
    notes = [s.get("note", "") for s in stats]
    note_fit = b.fit("stat-note", [n for n in notes if n], width_px=inner, sizes=(16, 14), leading=1.4, max_lines=3) if any(notes) else None
    height = 24 + value_fit.tallest_px + 14 + _tallest(label_fit, 1.3) + (10 + _tallest(note_fit, 1.4) if note_fit else 0) + 24
    rows = (len(stats) + per_row - 1) // per_row
    if y + rows * height > b.bottom:
        raise FitError(f"{b.label} stats: metrics need {rows * height}px but only {b.bottom - y}px remain")
    top = y
    note_index = 0
    for index, stat in enumerate(stats, start=1):
        x, column = columns[(index - 1) % per_row]
        y = top + ((index - 1) // per_row) * height
        if (index - 1) % per_row == 0:
            b.rect(f"metrics-rule{(index - 1) // per_row + 1}", (body_x(), y, body_w(), 1), t.c("line"))
        else:
            b.rect(f"stat{index}-divider", (x, y + 24, 1, height - 48), t.c("line"))
        cy = y + 24
        b.text(f"stat{index}-value", (x + pad, cy, inner, value_fit.tallest_px), [b.para(stat["value"], value_fit, r["accent"], bold=t.display_bold, font="display")])
        cy += value_fit.tallest_px + 14
        b.text(f"stat{index}-label", (x + pad, cy, inner, _h(label_fit, index - 1, 1.3)), [b.para(stat["label"], label_fit, r["text"], bold=True)])
        if stat.get("note") and note_fit is not None:
            cy += _tallest(label_fit, 1.3) + 10
            b.text(f"stat{index}-note", (x + pad, cy, inner, _h(note_fit, note_index, 1.4)), [b.para(stat["note"], note_fit, r["muted"])])
            note_index += 1


# ── features (icon_grid) ────────────────────────────────────────────


def _bento_tiles(count: int, x0: int, y0: int, width: int, height: int, gap: int) -> list[tuple[int, int, int, int]]:
    """Tile boxes: one feature tile leading, the rest arranged beside or below it."""
    if count == 3:
        lw = int(width * 0.5)
        rh = (height - gap) // 2
        return [(x0, y0, lw, height), (x0 + lw + gap, y0, width - lw - gap, rh), (x0 + lw + gap, y0 + rh + gap, width - lw - gap, rh)]
    if count == 4:
        cw, rh = (width - gap) // 2, (height - gap) // 2
        wide = int(width * 0.6)
        return [(x0, y0, wide, rh), (x0 + wide + gap, y0, width - wide - gap, rh),
                (x0, y0 + rh + gap, width - wide - gap, rh), (x0 + width - wide, y0 + rh + gap, wide, rh)]
    if count == 5:
        lw = int(width * 0.4)
        cw, rh = (width - lw - 2 * gap) // 2, (height - gap) // 2
        rx = x0 + lw + gap
        return [(x0, y0, lw, height), (rx, y0, cw, rh), (rx + cw + gap, y0, cw, rh), (rx, y0 + rh + gap, cw, rh), (rx + cw + gap, y0 + rh + gap, cw, rh)]
    cw, rh = (width - 2 * gap) // 3, (height - gap) // 2
    return [(x0 + (i % 3) * (cw + gap), y0 + (i // 3) * (rh + gap), cw, rh) for i in range(count)]


def icon_grid_bento(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Bento grid: rounded tiles of different sizes, each with an icon, a title and a line of copy."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 4
    items = slide["items"]
    tiles = _bento_tiles(len(items), body_x(), y, body_w(), b.bottom - y, 16)
    fill, ink, soft = _tile_fill(b)
    radius = int(t.card.get("radius", 22)) or 22
    compact = len(items) >= 5 or body_w() < 900 or b.bottom - y < 520
    pad, icon = (20, 30) if compact else (26, 40)
    narrowest = min(w for _, _, w, _ in tiles) - 2 * pad
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=narrowest, sizes=(22, 16) if compact else (26, 18), leading=1.2, max_lines=2)
    texts = [item.get("text", "") for item in items]
    shortest = min(h for _, _, _, h in tiles)
    text_fit = b.fit("item-text", [x for x in texts if x], width_px=narrowest, sizes=(17, 14), leading=1.4,
                     each_max_height_px=shortest - 2 * pad - icon - 10 - _tallest(title_fit, 1.2) - 6) if any(texts) else None
    text_index = 0
    for index, (item, (x, ty, w, h)) in enumerate(zip(items, tiles), start=1):
        b.rect(f"item{index}-tile", (x, ty, w, h), fill, radius=radius, shadow=True)
        b.icon(f"item{index}-icon", item.get("icon") or item["title"], (x + pad, ty + pad, icon, icon), r["accent"])
        th = _h(title_fit, index - 1, 1.2)
        text_h = _h(text_fit, text_index, 1.4) if item.get("text") and text_fit is not None else 0
        # Copy sits at the bottom of the tile, the icon at the top: the space between is the tile.
        bottom = ty + h - pad
        if text_h:
            b.text(f"item{index}-text", (x + pad, bottom - text_h, w - 2 * pad, text_h), [b.para(item["text"], text_fit, soft)])
            text_index += 1
            bottom -= text_h + 8
        b.text(f"item{index}-title", (x + pad, bottom - th, w - 2 * pad, th), [b.para(item["title"], title_fit, ink, bold=True)])


def icon_grid_cards(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Product docs cards: soft raised cards, a gradient icon tile, title and copy."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 4
    items = slide["items"]
    per_row = 3 if len(items) in (3, 5, 6) else 2
    rows = [items[i : i + per_row] for i in range(0, len(items), per_row)]
    columns = _columns(per_row, gap=20)
    width = columns[0][1]
    card_h = (b.bottom - y - 20 * (len(rows) - 1)) // len(rows)
    compact = card_h < 230
    pad, icon = (20, 36) if compact else (28, 48)
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=width - 2 * pad, sizes=(20, 16) if compact else (22, 17), leading=1.25, max_lines=2)
    texts = [item.get("text", "") for item in items]
    text_fit = b.fit("item-text", [x for x in texts if x], width_px=width - 2 * pad, sizes=(16, 14), leading=1.45,
                     each_max_height_px=card_h - 2 * pad - icon - 12 - _tallest(title_fit, 1.25) - 6) if any(texts) else None
    used_h = 2 * pad + icon + 12 + _tallest(title_fit, 1.25) + (6 + _tallest(text_fit, 1.45) if text_fit else 0)
    card_h = min(card_h, used_h + 12)
    colors = t.colors
    grad = (colors.get("grad_a", t.c("accent")), colors.get("grad_c", t.c("primary")))
    fill = t.c("card") if "card" in colors else t.c("surface")
    text_index = 0
    index = 0
    for row_number, row in enumerate(rows):
        ry = y + row_number * (card_h + 20)
        for (x, _), item in zip(columns, row):
            index += 1
            b.rect(f"item{index}-card", (x, ry, width, card_h), fill, radius=int(t.card.get("radius", 16)), line=t.c("line"), line_px=1.0, shadow=True)
            b.plan.shapes.append(Shape(b._name(f"item{index}-chip"), "gradient", (x + pad, ry + pad, icon, icon),
                                       gradient=((grad[0], 0), (grad[1], 100)), gradient_angle=45.0, radius=icon // 4))
            inset = icon // 5
            b.icon(f"item{index}-icon", item.get("icon") or item["title"], (x + pad + inset, ry + pad + inset, icon - 2 * inset, icon - 2 * inset), "FFFFFF")
            ty = ry + pad + icon + 12
            th = _h(title_fit, index - 1, 1.25)
            b.text(f"item{index}-title", (x + pad, ty, width - 2 * pad, th), [b.para(item["title"], title_fit, r["text"], bold=True)])
            if item.get("text") and text_fit is not None:
                b.text(f"item{index}-text", (x + pad, ty + th + 6, width - 2 * pad, _h(text_fit, text_index, 1.45)), [b.para(item["text"], text_fit, r["muted"])])
                text_index += 1


# ── process ─────────────────────────────────────────────────────────


def process_numerals(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote steps: oversized thin numerals over a hairline, the step name and one line beneath."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 20
    steps = slide["steps"]
    columns = _columns(len(steps), gap=32 if len(steps) < 5 else 16)
    width = columns[0][1]
    numbers = [f"{i:02d}" for i in range(1, len(steps) + 1)]
    number_fit = b.fit("step-number", numbers, width_px=width, sizes=(104, 28), leading=1.0, max_lines=1, display=True)
    title_fit = b.fit("step-title", [s["title"] for s in steps], width_px=width, sizes=(24, 18), leading=1.25, max_lines=2)
    texts = [s.get("text", "") for s in steps]
    ty = y + number_fit.tallest_px + 26
    text_fit = b.fit("step-text", [x for x in texts if x], width_px=width, sizes=(17, 14), leading=1.45,
                     each_max_height_px=b.bottom - ty - _tallest(title_fit, 1.25) - 10) if any(texts) else None
    faint = blend(r["bg"], r["text"], 0.32)
    text_index = 0
    for index, ((x, column), step) in enumerate(zip(columns, steps), start=1):
        b.text(f"step{index}-number", (x, y, column, number_fit.tallest_px), [b.para(numbers[index - 1], number_fit, r["accent"] if index == 1 else faint, font="display")])
        b.rect(f"step{index}-rule", (x, ty - 12, column, 1), t.c("line"))
        th = _h(title_fit, index - 1, 1.25)
        b.text(f"step{index}-title", (x, ty, column, th), [b.para(step["title"], title_fit, r["text"], bold=True)])
        if step.get("text") and text_fit is not None:
            b.text(f"step{index}-text", (x, ty + th + 10, column, _h(text_fit, text_index, 1.45)), [b.para(step["text"], text_fit, r["muted"])])
            text_index += 1


def process_pipeline(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """A connected pipeline: rounded pills joined by arrows, a caption under each."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 30
    steps = slide["steps"]
    gap = 44
    columns = _columns(len(steps), gap=gap)
    width = columns[0][1]
    title_fit = b.fit("step-title", [s["title"] for s in steps], width_px=width - 28, sizes=(20, 15), leading=1.2, max_lines=2)
    pill_h = _tallest(title_fit, 1.2) + 32
    texts = [s.get("text", "") for s in steps]
    ty = y + pill_h + 22
    text_fit = b.fit("step-text", [x for x in texts if x], width_px=width, sizes=(16, 14), leading=1.45, each_max_height_px=b.bottom - ty) if any(texts) else None
    fill = t.c("card") if "card" in t.colors else t.c("surface")
    text_index = 0
    for index, ((x, column), step) in enumerate(zip(columns, steps), start=1):
        if index > 1:
            ax = x - gap + 8
            b.rect(f"step{index}-link", (ax, y + pill_h // 2 - 1, gap - 22, 2), r["accent"])
            b.rect(f"step{index}-arrow", (x - 16, y + pill_h // 2 - 6, 12, 12), r["accent"], kind="triangle").rotation = 90.0
        last = index == len(steps)
        pill_fill = t.c("accent") if last else fill
        ink = t.c("on_accent") if last else r["text"]
        th = _h(title_fit, index - 1, 1.2)
        b.rect(f"step{index}-pill", (x, y, column, pill_h), pill_fill, radius=pill_h // 2, line=None if last else t.c("line"), line_px=0 if last else 1.2, shadow=True)
        b.text(f"step{index}-title", (x + 14, y + (pill_h - th) // 2, column - 28, th), [b.para(step["title"], title_fit, ink, bold=True, align="ctr")])
        if step.get("text") and text_fit is not None:
            b.text(f"step{index}-text", (x, ty, column, _h(text_fit, text_index, 1.45)), [b.para(step["text"], text_fit, r["muted"], align="ctr")])
            text_index += 1


# ── statement, comparison, bullets ──────────────────────────────────


def statement_giant(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote statement: one idea in huge type, centred on the stage."""
    if slide.get("image"):  # only photo-bleed styles reach here with a picture
        _photo_page(b, slide, side="center")
    t, r = b.t, b.r
    b.motifs()
    width = 1080
    x = (CANVAS_W - width) // 2
    text = b.title_text(slide["title"])
    fit = b.fit("title", [text], width_px=width, sizes=_sizes(b, "statement_giant", (104, 56)), leading=1.02, max_lines=2, display=True)
    sub_fit = b.fit("subtitle", [slide["subtitle"]], width_px=820, sizes=(24, 16), leading=1.4, max_lines=2) if slide.get("subtitle") else None
    kicker_h = 40 if slide.get("kicker") else 0
    block = kicker_h + fit.height_px + (28 + sub_fit.height_px if sub_fit else 0)
    y = max(b.top + 40, (720 - block) // 2 - 20)
    if slide.get("kicker"):
        b.block("kicker", x, y, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True, align="ctr")
        y += kicker_h
    b.text("title", (x, y, width, fit.height_px), [b.rich(text, fit, r["text"], bold=t.display_bold, align="ctr")])
    y += fit.height_px + 28
    if sub_fit:
        b.text("subtitle", ((CANVAS_W - 820) // 2, y, 820, sub_fit.height_px), [b.para(slide["subtitle"], sub_fit, r["muted"], align="ctr")])


def comparison_spec(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote spec comparison: centred columns, one line per row, hairlines between."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 12
    columns_data = slide["columns"]
    columns = _columns(len(columns_data), gap=0)
    pad = 24
    inner = columns[0][1] - 2 * pad
    highlight = slide.get("highlight")
    head_fit = b.fit("column-heading", [c["heading"] for c in columns_data], width_px=inner, sizes=(34, 22), leading=1.15, max_lines=2, display=True)
    head_h = _tallest(head_fit, 1.15)
    rows = max(len(c.get("points", [])) for c in columns_data)
    texts = [p if isinstance(p, str) else _point_parts(p)[1] or _point_parts(p)[0] for c in columns_data for p in c.get("points", [])]
    row_h_max = (b.bottom - y - head_h - 32) // max(1, rows)
    point_fit = b.fit("column-point", texts, width_px=inner, sizes=(20, 14), leading=1.3, max_lines=2, each_max_height_px=row_h_max - 20) if texts else None
    row_h = (_tallest(point_fit, 1.3) + 20) if point_fit else 0
    point_index = 0
    for index, ((x, column), data) in enumerate(zip(columns, columns_data)):
        if index:
            b.rect(f"column{index + 1}-divider", (x, y, 1, head_h + 32 + rows * row_h), t.c("line"))
        color = r["accent"] if highlight == index else r["text"]
        b.text(f"column{index + 1}-heading", (x + pad, y, inner, _h(head_fit, index, 1.15)), [b.para(data["heading"], head_fit, color, bold=t.display_bold, align="ctr", font="display")])
        for row, point in enumerate(data.get("points", [])):
            text = point if isinstance(point, str) else _point_parts(point)[1] or _point_parts(point)[0]
            ry = y + head_h + 32 + row * row_h
            b.rect(f"column{index + 1}-rule{row + 1}", (x + pad, ry, inner, 1), blend(r["bg"], r["text"], 0.12))
            b.text(f"column{index + 1}-point{row + 1}", (x + pad, ry + 10, inner, _h(point_fit, point_index, 1.3)), [b.para(text, point_fit, r["text"] if highlight == index else r["muted"], align="ctr")])
            point_index += 1


def bullets_keynote(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote list: each point a large line, its detail in grey beside it, a hairline between."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 8
    points = slide["points"]
    parts = [_point_parts(p) for p in points]
    heads = [title or text for title, text in parts]
    details = [text if title else "" for title, text in parts]
    x, width = body_x(), body_w()
    head_w = int(width * 0.56) if any(details) else width
    row_max = (b.bottom - y) // len(points)
    head_fit = b.fit("point", heads, width_px=head_w, sizes=(34, 20), leading=1.2, max_lines=2, each_max_height_px=row_max - 24, display=True)
    detail_fit = b.fit("point-detail", [d for d in details if d], width_px=width - head_w - 32, sizes=(18, 14), leading=1.4, max_lines=3) if any(details) else None
    row_h = max(_tallest(head_fit, 1.2), _tallest(detail_fit, 1.4) if detail_fit else 0) + 24
    detail_index = 0
    for index, (head, detail) in enumerate(zip(heads, details), start=1):
        ry = y + (index - 1) * row_h
        if index > 1:
            b.rect(f"point{index}-rule", (x, ry - 12, width, 1), t.c("line"))
        b.text(f"point{index}", (x, ry, head_w, _h(head_fit, index - 1, 1.2)), [b.rich(head, head_fit, r["text"], bold=t.display_bold)])
        if detail and detail_fit is not None:
            b.text(f"point{index}-detail", (x + head_w + 32, ry + 4, width - head_w - 32, _h(detail_fit, detail_index, 1.4)), [b.para(detail, detail_fit, r["muted"])])
            detail_index += 1


SIGNATURE_FUNCTIONS = {
    ("stats", "hero"): stats_hero,
    ("stats", "bento"): stats_bento,
    ("stats", "metrics"): stats_metrics,
    ("icon_grid", "bento"): icon_grid_bento,
    ("icon_grid", "cards"): icon_grid_cards,
    ("process", "numerals"): process_numerals,
    ("process", "pipeline"): process_pipeline,
    ("statement", "giant"): statement_giant,
    ("comparison", "spec"): comparison_spec,
    ("bullets", "keynote"): bullets_keynote,
}
