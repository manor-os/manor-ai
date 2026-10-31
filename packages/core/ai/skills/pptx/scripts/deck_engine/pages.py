"""Page types that best-selling commercial decks and launch keynotes rely on.

One giant number, a logo wall, pricing plans, a device mockup, market sizing,
a launch list, a case study, testimonials and a roadmap. Each follows the
``layouts`` contract: every text slot is fitted, text never overlaps text,
decoration sits behind what it frames and everything stays inside the safe
area. Framed pages read ``body_x``/``body_w`` so a style's page frame applies.
"""

from __future__ import annotations

from typing import Any

from .layouts import (
    CANVAS_W,
    CONTENT_W,
    MARGIN,
    SlideBuilder,
    _BODY,
    _columns,
    _framed,
    _header,
    _photo_page,
    _point_parts,
    _sizes,
    blend,
    body_w,
    body_x,
    plain,
)
from .model import CANVAS_H, Shape
from .textfit import PX_TO_PT, Fit, FitError, block_height_px, text_width_pt


def _h(fit, index: int, leading: float) -> int:
    return block_height_px([fit.lines[index]], fit.size, leading, 0, 2)


def _tallest(fit, leading: float) -> int:
    return max(block_height_px([lines], fit.size, leading, 0, 2) for lines in fit.lines)


def _initials(name: str) -> str:
    name = plain(name).strip()
    if any("㐀" <= ch <= "鿿" for ch in name):
        return name[-2:] if len(name) <= 3 else name[:1]
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "?"


def _soft(b: SlideBuilder, share: float = 0.5) -> str:
    """Secondary text: between the page's text and background."""
    return blend(b.r["text"], b.r["bg"], share)


def _gradient(b: SlideBuilder) -> tuple[str, str]:
    colors = b.t.colors
    return colors.get("grad_a", b.r["accent"]), colors.get("grad_c", colors.get("primary", b.r["accent"]))


def _card(b: SlideBuilder, index: int = 0) -> tuple[str, str, str]:
    return b.card_style(index)


def _wide_header(b: SlideBuilder, slide: dict[str, Any]) -> int:
    """Header for pages that need the full width: a band or centred frame styles the
    title, a magazine column is skipped, and the body spans the page."""
    y = _header(b, slide, framed=b.t.get("frame") != "column")
    _BODY.update(x=MARGIN, w=CONTENT_W)
    return y


# ── metric: one number fills the page ──────────────────────────────


def metric(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote metric: one huge number centred, its meaning in one line beneath."""
    if slide.get("image"):
        _photo_page(b, slide, side="center")
    t, r = b.t, b.r
    b.motifs(light=True)  # the number owns the centre; no motif may cross it
    width = 1100
    x = (CANVAS_W - width) // 2
    value_fit = b.fit("value", [slide["value"]], width_px=width, sizes=_sizes(b, "metric", (220, 96)), leading=1.0, max_lines=1, display=True)
    title = b.title_text(slide["title"])
    title_fit = b.fit("title", [title], width_px=900, sizes=(36, 26), leading=1.2, max_lines=2, display=True)
    sub_fit = b.fit("subtitle", [slide["subtitle"]], width_px=760, sizes=(20, 15), leading=1.4, max_lines=2) if slide.get("subtitle") else None
    kicker_h = 40 if slide.get("kicker") else 0
    block = kicker_h + value_fit.height_px + 16 + title_fit.height_px + (16 + sub_fit.height_px if sub_fit else 0)
    y = max(b.top + 8, (CANVAS_H - block) // 2 - 16)
    if slide.get("kicker"):
        b.block("kicker", x, y, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True, align="ctr")
        y += kicker_h
    glow = blend(r["bg"], r["accent"], 0.16)
    halo = b.rect("value-glow", (CANVAS_W // 2 - 320, y + value_fit.height_px // 2 - 140, 640, 280), glow, kind="ellipse", alpha=70)
    halo.soft = 70  # a feathered halo, not a disc
    b.text("value", (x, y, width, value_fit.height_px), [b.para(slide["value"], value_fit, r["accent"], bold=t.display_bold, align="ctr", font="display")])
    y += value_fit.height_px + 16
    b.text("title", ((CANVAS_W - 900) // 2, y, 900, title_fit.height_px), [b.rich(title, title_fit, r["text"], bold=False, align="ctr")])
    y += title_fit.height_px + 16
    if sub_fit:
        b.text("subtitle", ((CANVAS_W - 760) // 2, y, 760, sub_fit.height_px), [b.para(slide["subtitle"], sub_fit, r["muted"], align="ctr")])


def metric_side(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Editorial metric: the number set huge at the left, the claim in a column at the right."""
    if slide.get("image"):
        _photo_page(b, slide, side="center")  # text on both halves
    t, r = b.t, b.r
    b.motifs(light=True)
    left_w = 600
    value_fit = b.fit("value", [slide["value"]], width_px=left_w, sizes=_sizes(b, "metric", (220, 96)), leading=1.0, max_lines=1, display=True)
    vy = max(b.top + 20, (CANVAS_H - value_fit.height_px) // 2 - 20)
    b.text("value", (MARGIN, vy, left_w, value_fit.height_px), [b.para(slide["value"], value_fit, r["accent"], bold=t.display_bold, font="display")])
    x = MARGIN + left_w + 64
    width = CANVAS_W - MARGIN - x
    b.rect("divider", (x - 32, b.top + 40, 1, b.bottom - b.top - 80), blend(r["bg"], r["text"], 0.2))
    title = b.title_text(slide["title"])
    title_fit = b.fit("title", [title], width_px=width, sizes=(40, 26), leading=1.15, max_lines=3, display=True)
    sub_fit = b.fit("subtitle", [slide["subtitle"]], width_px=width, sizes=(20, 15), leading=1.45, max_lines=4) if slide.get("subtitle") else None
    kicker_h = 36 if slide.get("kicker") else 0
    block = kicker_h + title_fit.height_px + (20 + sub_fit.height_px if sub_fit else 0)
    y = max(b.top + 20, (CANVAS_H - block) // 2 - 20)
    if slide.get("kicker"):
        b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True)
        y += kicker_h
    b.text("title", (x, y, width, title_fit.height_px), [b.rich(title, title_fit, r["text"], bold=t.display_bold)])
    y += title_fit.height_px + 20
    if sub_fit:
        b.text("subtitle", (x, y, width, sub_fit.height_px), [b.para(slide["subtitle"], sub_fit, r["muted"])])


# ── logos: customers and partners ──────────────────────────────────


def _logo_value(b: SlideBuilder, slide: dict[str, Any], y: int) -> int:
    """The optional headline number beside a logo wall; returns the width it took."""
    if not slide.get("value"):
        return 0
    r = b.r
    width = 300
    value_fit = b.fit("value", [slide["value"]], width_px=width, sizes=(112, 52), leading=1.0, max_lines=1, display=True)
    label_fit = b.fit("value-label", [slide.get("label", "")], width_px=width, sizes=(20, 14), leading=1.35, max_lines=3) if slide.get("label") else None
    block = value_fit.height_px + (12 + label_fit.height_px if label_fit else 0)
    top = y + max(0, (b.bottom - y - block) // 2)
    b.text("value", (body_x(), top, width, value_fit.height_px), [b.para(slide["value"], value_fit, r["accent"], bold=b.t.display_bold, font="display")])
    if label_fit:
        b.text("value-label", (body_x(), top + value_fit.height_px + 12, width, label_fit.height_px), [b.para(slide["label"], label_fit, r["muted"])])
    return width + 48


def logos(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, cells: bool = True) -> None:
    """Logo wall: customer names set as wordmarks in a ruled grid (or open rows)."""
    t, r = b.t, b.r
    y = _wide_header(b, slide) + 8
    names = slide["logos"]
    offset = _logo_value(b, slide, y)
    x0, width = body_x() + offset, body_w() - offset
    per_row = 3 if width < 720 or len(names) in (3, 6, 9) else 4
    per_row = min(per_row, len(names)) if len(names) <= 4 else per_row
    rows = (len(names) + per_row - 1) // per_row
    cell_w = width // per_row
    cell_h = min(132, (b.bottom - y) // rows)
    if cell_h < 56:
        raise FitError(f"{b.label} logos: {len(names)} names need more height than the {b.bottom - y}px left; drop the lead")
    fit = b.fit("logo", names, width_px=cell_w - 32, sizes=(28, 16) if cells else (32, 18), leading=1.1, max_lines=2,
                each_max_height_px=cell_h - 24, display=True)
    top = y + max(0, (b.bottom - y - rows * cell_h) // 2)
    line = t.c("line")
    if cells:
        for row in range(1, rows):
            b.rect(f"logo-rule-h{row}", (x0, top + row * cell_h, width, 1), line)
        for column in range(1, per_row):
            b.rect(f"logo-rule-v{column}", (x0 + column * cell_w, top, 1, rows * cell_h), line)
    for index, name in enumerate(names, start=1):
        row, column = divmod(index - 1, per_row)
        in_row = min(per_row, len(names) - row * per_row)
        shift = 0 if cells else (per_row - in_row) * cell_w // 2
        x = x0 + shift + column * cell_w
        height = _h(fit, index - 1, 1.1)
        color = r["text"] if cells else (r["text"] if index % 2 else _soft(b, 0.25))
        b.text(f"logo{index}", (x + 16, top + row * cell_h + (cell_h - height) // 2, cell_w - 32, height),
               [b.para(name, fit, color, bold=True, align="ctr", font="display")])


# ── pricing ────────────────────────────────────────────────────────


def pricing(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, cards: bool = True) -> None:
    """Pricing plans side by side; the highlighted plan is filled (cards) or barred (columns)."""
    t, r = b.t, b.r
    y = _wide_header(b, slide) + (16 if cards else 4)
    plans = slide["plans"]
    highlight = slide.get("highlight")
    columns = _columns(len(plans), x=MARGIN, width=CONTENT_W, gap=20 if cards else 0)
    width = columns[0][1]
    pad = 24
    inner = width - 2 * pad
    badge_h = 30
    name_fit = b.fit("plan-name", [p["name"] for p in plans], width_px=inner - (162 if any(p.get("badge") for p in plans) else 0), sizes=(20, 16), leading=1.2, max_lines=1)
    price_fit = b.fit("plan-price", [p["price"] for p in plans], width_px=inner, sizes=(52, 28), leading=1.0, max_lines=1, display=True)
    period_fit = b.fit("plan-period", [p["period"] for p in plans if p.get("period")], width_px=inner, sizes=(15, 14), leading=1.2, max_lines=1) if any(p.get("period") for p in plans) else None

    def price_width(plan: dict[str, Any]) -> int:
        return min(inner, int(text_width_pt(plan["price"], price_fit.size) / PX_TO_PT / 0.9) + 8)

    def period_width(plan: dict[str, Any]) -> int:
        return int(text_width_pt(plan["period"], period_fit.size) / PX_TO_PT / 0.85) + 8

    # The period sits beside the price when there is room, otherwise on its own line.
    below = {i for i, p in enumerate(plans) if p.get("period") and price_width(p) + 6 + period_width(p) > inner}
    period_row = (_tallest(period_fit, 1.2) + 4) if below and period_fit else 0
    texts = [p.get("text", "") for p in plans]
    text_fit = b.fit("plan-text", [x for x in texts if x], width_px=inner, sizes=(16, 14), leading=1.4, max_lines=2) if any(texts) else None
    head_h = max(_tallest(name_fit, 1.2), badge_h) + 14 + price_fit.tallest_px + period_row + (10 + _tallest(text_fit, 1.4) if text_fit else 0) + 20
    features = [f for p in plans for f in p["features"]]
    most = max(len(p["features"]) for p in plans)
    room = b.bottom - y - 2 * pad - head_h - (12 if cards else 0)
    feature_fit = b.fit("plan-feature", features, width_px=inner - 30, sizes=(17, 14), leading=1.3, max_lines=2,
                        each_max_height_px=max(20, room // most - 10))
    row_h = _tallest(feature_fit, 1.3) + 10
    card_h = 2 * pad + head_h + most * row_h
    if card_h > b.bottom - y + (12 if cards else 0):
        raise FitError(f"{b.label} pricing: the plans need {card_h}px but {b.bottom - y}px are left; drop a feature or the lead")
    line = t.c("line")
    text_index = 0
    period_index = 0
    feature_index = 0
    for index, ((x, column), plan) in enumerate(zip(columns, plans)):
        hot = highlight == index
        fill, ink, soft = _card(b, index)
        accent = r["accent"]
        top = y
        if cards:
            if hot:
                fill, ink, soft = t.c("primary"), t.c("on_primary"), t.colors.get("on_primary_muted", t.c("on_primary"))
                accent = _legible(t.colors.get("accent_on_dark", ink), fill, ink)
                top = y - 12
            b.rect(f"plan{index + 1}-card", (x, top, column, card_h + (12 if hot else 0)), fill, radius=int(t.card.get("radius", 16)),
                   line=None if hot else line, line_px=0 if hot else 1.0, shadow=True)
        else:
            ink, soft = r["text"], r["muted"]
            if index:
                b.rect(f"plan{index + 1}-divider", (x, y, 1, card_h), line)
            b.rect(f"plan{index + 1}-bar", (x + pad, y, inner, 4 if hot else 1), accent if hot else line)
        cy = top + pad
        bw = min(150, int(text_width_pt(plan["badge"], 14) / PX_TO_PT / 0.85) + 28) if plan.get("badge") else 0
        b.text(f"plan{index + 1}-name", (x + pad, cy + (badge_h - _h(name_fit, index, 1.2)) // 2, inner - (bw + 12 if bw else 0), _h(name_fit, index, 1.2)),
               [b.para(plan["name"], name_fit, ink, bold=True)])
        if plan.get("badge"):
            badge_fit = Fit(size=14, lines=[1], line_pt=16.8, height_px=22, tallest_px=22)
            # On the filled plan the badge inverts: card ink as the pill, card colour as its text.
            badge_fill, badge_ink = (accent, fill) if hot else (t.c("accent"), t.c("on_accent"))
            b.text(f"plan{index + 1}-badge", (x + column - pad - bw, cy, bw, badge_h), [b.para(plan["badge"], badge_fit, badge_ink, bold=True, align="ctr")],
                   kind="round", fill=badge_fill, radius=badge_h // 2, anchor="ctr", inset_x=8, inset_y=2)
        cy += max(_tallest(name_fit, 1.2), badge_h) + 14
        price_w = price_width(plan)
        b.text(f"plan{index + 1}-price", (x + pad, cy, price_w, price_fit.tallest_px), [b.para(plan["price"], price_fit, accent if hot else ink, bold=t.display_bold, font="display")])
        if plan.get("period") and period_fit is not None:
            ph = _h(period_fit, period_index, 1.2)
            if index in below:
                box = (x + pad, cy + price_fit.tallest_px + 4, inner, ph)
            else:
                box = (x + pad + price_w + 6, cy + price_fit.tallest_px - ph - 6, inner - price_w - 6, ph)
            b.text(f"plan{index + 1}-period", box, [b.para(plan["period"], period_fit, soft)])
            period_index += 1
        cy += price_fit.tallest_px + period_row
        if plan.get("text") and text_fit is not None:
            b.text(f"plan{index + 1}-text", (x + pad, cy + 10, inner, _h(text_fit, text_index, 1.4)), [b.para(plan["text"], text_fit, soft)])
            text_index += 1
        cy = top + pad + head_h - 10
        b.rect(f"plan{index + 1}-rule", (x + pad, cy, inner, 1), blend(fill if cards else r["bg"], ink, 0.18))
        cy += 18
        for number, feature in enumerate(plan["features"], start=1):
            fh = _h(feature_fit, feature_index, 1.3)
            mark = min(18, int(feature_fit.line_pt / PX_TO_PT))
            b.icon(f"plan{index + 1}-feature{number}-icon", "check", (x + pad, cy + (int(feature_fit.line_pt / PX_TO_PT) - mark) // 2 + 1, mark, mark), accent)
            b.text(f"plan{index + 1}-feature{number}", (x + pad + 30, cy, inner - 30, fh), [b.para(feature, feature_fit, ink)])
            cy += row_h
            feature_index += 1


# ── device mockup ──────────────────────────────────────────────────

_DEVICE_RATIO = {"browser": 1280 / 840, "laptop": 1.6 * 1.04, "phone": 9 / 19.5}


def _device(b: SlideBuilder, slot: str, kind: str, image: str, area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Draw a device holding ``image`` contained in ``area``; return the device box."""
    ax, ay, aw, ah = area
    ratio = _DEVICE_RATIO[kind]
    w = min(aw, int(ah * ratio))
    h = int(w / ratio)
    x, y = ax + (aw - w) // 2, ay + (ah - h) // 2
    ink = "1D1D1F"
    if kind == "browser":
        frame = b.t.colors.get("card", "FFFFFF")
        b.rect(f"{slot}-frame", (x, y, w, h), frame, radius=14, line=b.t.c("line"), line_px=1.0, shadow=True)
        bar = 34
        for i, color in enumerate(("FF5F57", "FEBC2E", "28C840")):
            b.rect(f"{slot}-dot{i + 1}", (x + 16 + i * 20, y + bar // 2 - 6, 12, 12), color, kind="ellipse")
        b.rect(f"{slot}-address", (x + w // 2 - min(220, w // 4), y + 9, 2 * min(220, w // 4), bar - 18), blend(frame, "000000", 0.06), radius=(bar - 18) // 2)
        b.picture(f"{slot}-screen", image, (x + 1, y + bar, w - 2, h - bar - 1))
    elif kind == "laptop":
        base_h = max(12, int(h * 0.04))
        screen_h = h - base_h
        sw = int(w / 1.08)
        sx = x + (w - sw) // 2
        b.rect(f"{slot}-bezel", (sx, y, sw, screen_h), ink, radius=16, shadow=True)
        bezel = max(10, sw // 60)
        b.picture(f"{slot}-screen", image, (sx + bezel, y + bezel, sw - 2 * bezel, screen_h - 2 * bezel))
        b.rect(f"{slot}-base", (x, y + screen_h, w, base_h), "C8C9CE", radius=base_h // 2)
        b.rect(f"{slot}-notch", (x + w // 2 - w // 14, y + screen_h, w // 7, base_h // 2), "A3A5AB", radius=base_h // 4)
    else:
        radius = int(w * 0.16)
        b.rect(f"{slot}-bezel", (x, y, w, h), ink, radius=radius, shadow=True)
        bezel = max(8, w // 28)
        b.picture(f"{slot}-screen", image, (x + bezel, y + bezel, w - 2 * bezel, h - 2 * bezel), geometry="round", radius=radius - bezel)
        b.rect(f"{slot}-island", (x + w // 2 - w // 7, y + bezel + 10, 2 * (w // 7), max(14, w // 12)), "000000", radius=max(7, w // 24))
    return (x, y, w, h)


def device(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Product screen in a device on one side; title and numbered call-outs on the other."""
    kind = slide.get("device", "browser")
    left = slide.get("image_side", "right") == "left"
    share = 0.36 if kind == "phone" else 0.58
    device_w = int(CONTENT_W * share)
    text_w = CONTENT_W - device_w - 56
    text_x = MARGIN + device_w + 56 if left else MARGIN
    mark = len(b.plan.shapes)
    try:
        y = _header(b, slide, x=text_x, width=text_w)
    except FitError:
        # A title too long for the side column (wide display faces) takes the stage layout.
        del b.plan.shapes[mark:]
        device_stage(b, slide, deck)
        return
    area = (MARGIN if left else CANVAS_W - MARGIN - device_w, b.top, device_w, b.bottom - b.top)
    _device(b, "device", kind, slide["image"], area)
    points = slide.get("points") or []
    if not points:
        return
    _callouts(b, points, x=text_x, y=y, width=text_w, max_height=b.bottom - y)


def _callouts(b: SlideBuilder, points: list[Any], *, x: int, y: int, width: int, max_height: int, align: str = "l") -> None:
    """Numbered call-outs: a small accent numeral, a bold head and a muted line."""
    t, r = b.t, b.r
    parts = [_point_parts(p) for p in points]
    heads = [title or text for title, text in parts]
    details = [text if title else "" for title, text in parts]
    gap = 22
    head_fit = b.fit("point", heads, width_px=width - 48, sizes=(20, 15), leading=1.3, max_lines=2)
    detail_fit = b.fit("point-detail", [d for d in details if d], width_px=width - 48, sizes=(16, 14), leading=1.4, max_lines=3) if any(details) else None
    heights = []
    detail_index = 0
    for index, detail in enumerate(details):
        hh = _h(head_fit, index, 1.3)
        dh = 0
        if detail and detail_fit is not None:
            dh = _h(detail_fit, detail_index, 1.4)
            detail_index += 1
        heights.append((hh, dh))
    total = sum(hh + (4 + dh if dh else 0) for hh, dh in heights) + gap * (len(points) - 1)
    if total > max_height:
        raise FitError(f"{b.label} points: the call-outs need {total}px but {max_height}px are left; shorten them or drop one")
    cy = y + max(0, (max_height - total) // 3)
    detail_index = 0
    for index, (head, detail, (hh, dh)) in enumerate(zip(heads, details, heights), start=1):
        size = 30
        b.text(f"point{index}-number", (x, cy - 2, size, size), [b.para(str(index), Fit(size=14, lines=[1], line_pt=16.8, height_px=22, tallest_px=22), t.c("on_accent"), bold=True, align="ctr")],
               kind="ellipse", fill=t.c("accent"), anchor="ctr", inset_x=0, inset_y=0)
        b.text(f"point{index}", (x + 48, cy, width - 48, hh), [b.para(head, head_fit, r["text"], bold=True)])
        if dh and detail_fit is not None:
            b.text(f"point{index}-detail", (x + 48, cy + hh + 4, width - 48, dh), [b.para(detail, detail_fit, r["muted"])])
            detail_index += 1
        cy += hh + (4 + dh if dh else 0) + gap


def device_stage(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Launch stage: a centred title, the device large in the middle, call-outs either side."""
    kind = slide.get("device", "browser")
    y = _wide_header(b, slide) - 8
    points = slide.get("points") or []
    side = 0 if not points else (300 if kind == "phone" else 260)
    area = (MARGIN + side, y, CONTENT_W - 2 * side, b.bottom - y)
    box = _device(b, "device", kind, slide["image"], area)
    if not points:
        return
    left_points = points[: (len(points) + 1) // 2]
    right_points = points[(len(points) + 1) // 2 :]
    room = box[0] - MARGIN - 32
    _callouts(b, left_points, x=MARGIN, y=box[1] + 24, width=room, max_height=box[3] - 24)
    if right_points:
        rx = box[0] + box[2] + 32
        _callouts(b, right_points, x=rx, y=box[1] + 24, width=CANVAS_W - MARGIN - rx, max_height=box[3] - 24)


# ── market size ────────────────────────────────────────────────────


def market(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """TAM / SAM / SOM as nested circles sharing a base, with a legend of values."""
    t, r = b.t, b.r
    y = _wide_header(b, slide)
    tiers = slide["tiers"]
    height = b.bottom - y
    diameter = min(height, 470, int(body_w() * 0.48))
    if diameter < 260:
        raise FitError(f"{b.label} tiers: only {height}px left for the circles; drop the lead")
    x0 = body_x()
    base = y + (height + diameter) // 2
    accent = r["accent"]
    shares = (1.0, 0.7, 0.44)
    fills = [blend(r["bg"], accent, share) for share in (0.18, 0.42, 1.0)]
    for index, (tier, share, fill) in enumerate(zip(tiers, shares, fills), start=1):
        d = int(diameter * share)
        b.rect(f"tier{index}-circle", (x0 + (diameter - d) // 2, base - d, d, d), fill, kind="ellipse")
    # Each value sits in the band its circle shows above the next one, as wide as
    # the circle's chord through the middle of that band.
    slots = []
    for index, share in enumerate(shares):
        radius = diameter * share / 2
        if index < 2:
            band = diameter * (share - shares[index + 1])
            depth = band / 2
            chord = 2 * (radius ** 2 - (radius - depth) ** 2) ** 0.5
            slots.append((base - int(diameter * share), int(band), int(chord * 0.82)))
        else:
            slots.append((base - int(diameter * share), int(diameter * share), int(diameter * share * 0.84)))
    value_fit = b.fit("tier-value", [tier["value"] for tier in tiers], width_px=min(w for _, _, w in slots), sizes=(30, 14), leading=1.0, max_lines=1, display=True)
    for index, (tier, (top, band, vw)) in enumerate(zip(tiers, slots), start=1):
        if index < 3:
            color = r["text"]
        else:
            color = t.c("on_accent") if contrast_ok(t.c("on_accent"), fills[2]) else r["bg"]
        vy = top + (band - value_fit.tallest_px) // 2
        b.text(f"tier{index}-value", (x0 + (diameter - vw) // 2, vy, vw, value_fit.tallest_px), [b.para(tier["value"], value_fit, color, bold=True, align="ctr", font="display")])
    lx = x0 + diameter + 64
    lw = x0 + body_w() - lx
    label_fit = b.fit("tier-label", [f"{tier['label']} · {tier['value']}" for tier in tiers], width_px=lw - 40, sizes=(22, 16), leading=1.25, max_lines=2)
    texts = [tier.get("text", "") for tier in tiers]
    text_fit = b.fit("tier-text", [x for x in texts if x], width_px=lw - 40, sizes=(16, 14), leading=1.4, max_lines=3) if any(texts) else None
    rows = []
    text_index = 0
    for index, tier in enumerate(tiers):
        lh = _h(label_fit, index, 1.25)
        th = 0
        if tier.get("text") and text_fit is not None:
            th = _h(text_fit, text_index, 1.4)
            text_index += 1
        rows.append((lh, th))
    total = sum(lh + (6 + th if th else 0) for lh, th in rows) + 32 * 2
    cy = y + max(0, (height - total) // 2)
    text_index = 0
    for index, (tier, fill, (lh, th)) in enumerate(zip(tiers, fills, rows), start=1):
        b.rect(f"tier{index}-swatch", (lx, cy + 5, 22, 22), fill, kind="ellipse", line=accent, line_px=1.0)
        b.text(f"tier{index}-label", (lx + 40, cy, lw - 40, lh), [b.para(f"{tier['label']} · {tier['value']}", label_fit, r["text"], bold=True)])
        if th and text_fit is not None:
            b.text(f"tier{index}-text", (lx + 40, cy + lh + 6, lw - 40, th), [b.para(tier["text"], text_fit, r["muted"])])
            text_index += 1
        cy += lh + (6 + th if th else 0) + 32


def _legible(color: str, background: str, fallback: str) -> str:
    """``color`` when it reads on ``background`` (3:1, large text), else ``fallback``."""
    return color if contrast_ok(color, background) else fallback


def contrast_ok(foreground: str, background: str) -> bool:
    from .schema import contrast_ratio

    return contrast_ratio(foreground, background) >= 3.0


def market_bars(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """TAM / SAM / SOM as three narrowing bars, each value inside its bar and its meaning beside it."""
    t, r = b.t, b.r
    y = _wide_header(b, slide) + 8
    tiers = slide["tiers"]
    x0, width = body_x(), body_w()
    max_bar = int(width * 0.58)
    shares = (1.0, 0.66, 0.4)
    gap = 22
    bar_h = min(112, (b.bottom - y - 2 * gap) // 3)
    if bar_h < 64:
        raise FitError(f"{b.label} tiers: only {b.bottom - y}px left for three bars; drop the lead")
    value_fit = b.fit("tier-value", [tier["value"] for tier in tiers], width_px=int(max_bar * shares[2]) - 40, sizes=(40, 20), leading=1.0, max_lines=1, display=True)
    label_w = width - max_bar - 32  # beside the widest bar, so every label fits
    label_fit = b.fit("tier-label", [tier["label"] for tier in tiers], width_px=label_w, sizes=(22, 16), leading=1.25, max_lines=1)
    texts = [tier.get("text", "") for tier in tiers]
    text_fit = b.fit("tier-text", [x for x in texts if x], width_px=label_w, sizes=(16, 14), leading=1.35, max_lines=2,
                     each_max_height_px=bar_h - _tallest(label_fit, 1.25) - 4) if any(texts) else None
    top = y + max(0, (b.bottom - y - 3 * bar_h - 2 * gap) // 3)
    text_index = 0
    for index, (tier, share) in enumerate(zip(tiers, shares), start=1):
        by = top + (index - 1) * (bar_h + gap)
        bw = int(max_bar * share)
        fill = blend(r["bg"], r["accent"], (0.35, 0.65, 1.0)[index - 1])
        b.rect(f"tier{index}-bar", (x0, by, bw, bar_h), fill, radius=min(16, bar_h // 4))
        ink = r["text"] if index < 3 else (t.c("on_accent") if contrast_ok(t.c("on_accent"), fill) else r["bg"])
        b.text(f"tier{index}-value", (x0 + 20, by + (bar_h - value_fit.tallest_px) // 2, bw - 40, value_fit.tallest_px), [b.para(tier["value"], value_fit, ink, bold=True, font="display")])
        lx = x0 + bw + 32
        lw = x0 + width - lx
        lh = _h(label_fit, index - 1, 1.25)
        th = 0
        if tier.get("text") and text_fit is not None:
            th = _h(text_fit, text_index, 1.35)
        block = lh + (4 + th if th else 0)
        ly = by + (bar_h - block) // 2
        b.text(f"tier{index}-label", (lx, ly, lw, lh), [b.para(tier["label"], label_fit, r["text"], bold=True)])
        if th and text_fit is not None:
            b.text(f"tier{index}-text", (lx, ly + lh + 4, lw, th), [b.para(tier["text"], text_fit, r["muted"])])
            text_index += 1


# ── launches: what's new ───────────────────────────────────────────


def _tag_and_title(b: SlideBuilder, slide: dict[str, Any], x: int, y: int, width: int, *, align: str = "l") -> int:
    t, r = b.t, b.r
    if slide.get("tag"):
        tw = int(text_width_pt(slide["tag"], 14) / PX_TO_PT / 0.85) + 40
        tx = x if align == "l" else x + (width - tw) // 2
        b.pill("tag", tx, y, slide["tag"], fill=t.c("accent"), color=t.c("on_accent"), size=14)
        y += 50
    elif slide.get("kicker"):
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, align=align) + 10
    return b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "title", (44, 32)), color=r["text"], align=align, max_lines=3)


def launches(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Launch list: a tag and title at the left, every launch in large type at the right; one may be lit."""
    t, r = b.t, b.r
    items = slide["items"]
    highlight = slide.get("highlight")
    left_w = 380
    _tag_and_title(b, slide, MARGIN, b.top + 8, left_w)
    b.body_start = len(b.plan.shapes)
    x = MARGIN + left_w + 64
    width = CANVAS_W - MARGIN - x
    two = len(items) > 6
    columns = _columns(2, x=x, width=width, gap=32) if two else [(x, width)]
    per_column = (len(items) + 1) // 2 if two else len(items)
    gap = 14
    room = (b.bottom - b.top - 8 - gap * (per_column - 1)) // per_column
    fit = b.fit("item", items, width_px=columns[0][1], sizes=(40, 20) if not two else (30, 18), leading=1.15, max_lines=2, each_max_height_px=room, display=True)
    dim = _soft(b, 0.55) if highlight is not None else r["text"]
    heights = [_h(fit, i, 1.15) for i in range(len(items))]
    tallest = max(heights)
    total = per_column * tallest + gap * (per_column - 1)
    top = b.top + 8 + max(0, (b.bottom - b.top - 8 - total) // 2)
    for index, item in enumerate(items):
        column, row = divmod(index, per_column)
        cx, cw = columns[column]
        color = r["accent"] if highlight == index else dim
        b.text(f"item{index + 1}", (cx, top + row * (tallest + gap), cw, heights[index]), [b.para(item, fit, color, bold=t.display_bold, font="display")])


def launches_icon(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote feature list: a large app-icon tile at the left, the features listed at the right."""
    t, r = b.t, b.r
    y = _header(b, slide)
    items = slide["items"]
    highlight = slide.get("highlight")
    height = b.bottom - y
    tile = min(260, height - 20)
    if tile < 140:
        raise FitError(f"{b.label} icon: only {height}px left for the icon tile; drop the lead")
    tx = MARGIN + 40
    ty = y + (height - tile) // 2
    a, c = _gradient(b)
    b.plan.shapes.append(Shape(b._name("icon-tile"), "gradient", (tx, ty, tile, tile), gradient=((a, 0), (c, 100)), gradient_angle=45.0, radius=tile // 4))
    inset = tile // 5
    b.icon("feature-icon", slide.get("icon") or slide["title"], (tx + inset, ty + inset, tile - 2 * inset, tile - 2 * inset), "FFFFFF")
    x = tx + tile + 96
    width = CANVAS_W - MARGIN - x
    gap = 12
    room = (height - gap * (len(items) - 1)) // len(items)
    fit = b.fit("item", items, width_px=width, sizes=(32, 16), leading=1.2, max_lines=1, each_max_height_px=room, display=True)
    row = _tallest(fit, 1.2) + gap
    top = y + max(0, (height - row * len(items) + gap) // 2)
    for index, item in enumerate(items):
        color = r["accent"] if highlight == index else r["text"]
        b.text(f"item{index + 1}", (x, top + index * row, width, _h(fit, index, 1.2)), [b.para(item, fit, color, bold=t.display_bold, font="display")])


def launches_chips(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Everything included: each item a pill, flowed into centred rows under a centred title."""
    t, r = b.t, b.r
    y = b.top + 12
    width = CONTENT_W
    y = _tag_and_title(b, slide, MARGIN, y, width, align="ctr") + 40
    b.body_start = len(b.plan.shapes)
    items = slide["items"]
    highlight = slide.get("highlight")
    for size in (22, 20, 18, 16, 14):
        widths = [min(560, max(72, int(text_width_pt(item, size) / PX_TO_PT / 0.85) + 48)) for item in items]
        rows: list[list[int]] = [[]]
        used = 0
        for index, w in enumerate(widths):
            if rows[-1] and used + 16 + w > width - 80:
                rows.append([])
                used = 0
            used += (16 if rows[-1] else 0) + w
            rows[-1].append(index)
        pill_h = int(size * 1.2 / PX_TO_PT) + 4 + 22
        total = len(rows) * pill_h + (len(rows) - 1) * 18
        if total <= b.bottom - y:
            break
    else:
        raise FitError(f"{b.label} items: {len(items)} items do not fit as pills; shorten them")
    top = y + max(0, (b.bottom - y - total) // 3)
    line = blend(r["bg"], r["text"], 0.3)
    for row_number, row in enumerate(rows):
        row_w = sum(widths[i] for i in row) + 16 * (len(row) - 1)
        x = MARGIN + (width - row_w) // 2
        py = top + row_number * (pill_h + 18)
        for index in row:
            hot = highlight == index
            fit = Fit(size=size, lines=[1], line_pt=round(size * 1.2, 1), height_px=int(size * 1.2 / PX_TO_PT) + 4, tallest_px=0)
            b.text(f"item{index + 1}", (x, py, widths[index], pill_h), [b.para(items[index], fit, t.c("on_accent") if hot else r["text"], bold=hot, align="ctr")],
                   kind="round", fill=t.c("accent") if hot else r["bg"], radius=pill_h // 2, anchor="ctr", inset_x=12, inset_y=4,
                   line=None if hot else line, line_px=0 if hot else 1.2)
            x += widths[index] + 16


# ── case study ─────────────────────────────────────────────────────


def _sections(b: SlideBuilder, sections: list[dict[str, Any]], *, x: int, y: int, width: int, max_height: int) -> int:
    r = b.r
    label_fit = b.fit("section-label", [s["label"] for s in sections], width_px=width, sizes=(14, 14), leading=1.2, max_lines=1)
    gap = 18
    text_fit = b.fit("section-text", [s["text"] for s in sections], width_px=width, sizes=(18, 14), leading=1.4, max_lines=4,
                     each_max_height_px=(max_height - gap * (len(sections) - 1)) // len(sections) - _tallest(label_fit, 1.2) - 6)
    cy = y
    for index, section in enumerate(sections, start=1):
        lh = _h(label_fit, index - 1, 1.2)
        b.text(f"section{index}-label", (x, cy, width, lh), [b.para(section["label"].upper() if b.t.title_case == "upper" else section["label"], label_fit, r["accent"], bold=True)])
        cy += lh + 6
        th = _h(text_fit, index - 1, 1.4)
        b.text(f"section{index}-text", (x, cy, width, th), [b.para(section["text"], text_fit, r["text"])])
        cy += th + gap
    return cy - gap


def case_study(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Case study: a tall framed photo carrying the result card; challenge, approach and result at the right."""
    t = b.t
    image_w = 420
    geometry, radius = _framed(b)
    geometry = "round" if geometry != "rect" else "rect"
    box = (MARGIN, b.top, image_w, b.bottom - b.top)
    b.picture("image", slide["image"], box, geometry=geometry, radius=max(radius, 20) if geometry == "round" else 0, shadow=True)
    if slide.get("value"):
        pad = 22
        cw = image_w - 2 * 20
        value_fit = b.fit("value", [slide["value"]], width_px=cw - 2 * pad, sizes=(56, 32), leading=1.0, max_lines=1, display=True)
        label_fit = b.fit("value-label", [slide["value_label"]], width_px=cw - 2 * pad, sizes=(16, 14), leading=1.3, max_lines=2) if slide.get("value_label") else None
        ch = 2 * pad + value_fit.height_px + (6 + label_fit.height_px if label_fit else 0)
        cy = box[1] + box[3] - 20 - ch
        b.rect("result-card", (MARGIN + 20, cy, cw, ch), "FFFFFF", radius=16, alpha=94)
        b.text("value", (MARGIN + 20 + pad, cy + pad, cw - 2 * pad, value_fit.height_px), [b.para(slide["value"], value_fit, _legible(t.colors.get("accent_on_light", "111111"), "FFFFFF", "1B1B1F"), bold=t.display_bold, font="display")])
        if label_fit:
            b.text("value-label", (MARGIN + 20 + pad, cy + pad + value_fit.height_px + 6, cw - 2 * pad, label_fit.height_px), [b.para(slide["value_label"], label_fit, "4A4A55")])
    x = MARGIN + image_w + 56
    width = CANVAS_W - MARGIN - x
    y = _header(b, slide, x=x, width=width) - 8
    _sections(b, slide["sections"], x=x, y=y, width=width, max_height=b.bottom - y)


def case_study_band(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Case study: a wide photo band under the title, the story in columns beneath it."""
    y = _header(b, slide, framed=True) - 8
    geometry, radius = _framed(b)
    geometry = "round" if geometry != "rect" else "rect"
    band_h = min(190, (b.bottom - y) // 2 - 10)
    if band_h < 120:
        raise FitError(f"{b.label} image: only {b.bottom - y}px left for the photo band and the story; drop the lead")
    b.picture("image", slide["image"], (body_x(), y, body_w(), band_h), geometry=geometry, radius=max(radius, 18) if geometry == "round" else 0)
    y += band_h + 28
    sections = slide["sections"]
    count = len(sections) + (1 if slide.get("value") else 0)
    columns = _columns(count, gap=36)
    for index, ((x, width), section) in enumerate(zip(columns, sections), start=1):
        _sections_one(b, index, section, x=x, y=y, width=width, max_height=b.bottom - y)
    if slide.get("value"):
        x, width = columns[-1]
        b.rect("result-rule", (x - 18, y, 1, b.bottom - y), b.t.c("line"))
        value_fit = b.fit("value", [slide["value"]], width_px=width, sizes=(60, 32), leading=1.0, max_lines=1, display=True)
        b.text("value", (x, y, width, value_fit.height_px), [b.para(slide["value"], value_fit, b.r["accent"], bold=b.t.display_bold, font="display")])
        if slide.get("value_label"):
            b.block("value-label", x, y + value_fit.height_px + 8, width, slide["value_label"], sizes=(16, 14), color=b.r["muted"], max_lines=2)


def _sections_one(b: SlideBuilder, index: int, section: dict[str, Any], *, x: int, y: int, width: int, max_height: int) -> None:
    r = b.r
    label = section["label"].upper() if b.t.title_case == "upper" else section["label"]
    y = b.block(f"section{index}-label", x, y, width, label, sizes=(14, 14), color=r["accent"], max_lines=1, bold=True) + 8
    b.block(f"section{index}-text", x, y, width, section["text"], sizes=(18, 14), color=r["text"], leading=1.45, max_lines=None, max_height_px=max_height - 30)


# ── testimonials ───────────────────────────────────────────────────


def _avatar(b: SlideBuilder, slot: str, person: dict[str, Any], x: int, y: int, size: int, fill: str, ink: str) -> None:
    if person.get("image"):
        b.picture(slot, person["image"], (x, y, size, size), geometry="circle")
        return
    fit = Fit(size=15, lines=[1], line_pt=18.0, height_px=24, tallest_px=24)
    b.text(slot, (x, y, size, size), [b.para(_initials(person["name"]), fit, ink, bold=True, align="ctr")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)


def testimonials(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, cards: bool = True) -> None:
    """Two or three customer quotes, each signed with an avatar, name and role."""
    t, r = b.t, b.r
    y = _wide_header(b, slide) + (4 if cards else 0)
    quotes = slide["quotes"]
    columns = _columns(len(quotes), gap=24 if cards else 48)
    width = columns[0][1]
    pad = 28 if cards else 0
    indent = 0 if cards else 24
    inner = width - 2 * pad - indent
    mark_h = 70
    avatar = 48
    name_fit = b.fit("quote-name", [q["name"] for q in quotes], width_px=inner - avatar - 14, sizes=(16, 14), leading=1.2, max_lines=1)
    roles = [q.get("role", "") for q in quotes]
    role_fit = b.fit("quote-role", [x for x in roles if x], width_px=inner - avatar - 14, sizes=(14, 14), leading=1.2, max_lines=2) if any(roles) else None
    sign_h = max(avatar, _tallest(name_fit, 1.2) + (2 + _tallest(role_fit, 1.2) if role_fit else 0))
    room = b.bottom - y - 2 * pad - mark_h - 24 - sign_h
    quote_fit = b.fit("quote", [q["quote"] for q in quotes], width_px=inner, sizes=(20, 14) if cards else (24, 15), leading=1.45, max_lines=7, each_max_height_px=room)
    quote_h = _tallest(quote_fit, 1.45)
    card_h = 2 * pad + mark_h + quote_h + 24 + sign_h
    top = y + max(0, (b.bottom - y - card_h) // 3)
    role_index = 0
    for index, ((x, column), q) in enumerate(zip(columns, quotes), start=1):
        fill, ink, soft = _card(b, index - 1) if cards else (r["bg"], r["text"], r["muted"])
        if cards:
            b.rect(f"quote{index}-card", (x, top, column, card_h), fill, radius=int(t.card.get("radius", 16)), shadow=True)
        else:
            b.rect(f"quote{index}-bar", (x, top + 8, 3, card_h - 16), r["accent"])
        qx = x + pad + indent
        mark_fit = Fit(size=60, lines=[1], line_pt=60.0, height_px=80, tallest_px=80)
        b.text(f"quote{index}-mark", (qx, top + pad - 12, 60, 80), [b.para("“", mark_fit, r["accent"] if not cards or fill == r["bg"] else blend(fill, ink, 0.45), bold=True, font="display")])
        qy = top + pad + mark_h
        b.text(f"quote{index}", (qx, qy, inner, _h(quote_fit, index - 1, 1.45)), [b.para(q["quote"], quote_fit, ink, font="display" if not cards else "body")])
        sy = top + card_h - pad - sign_h
        _avatar(b, f"quote{index}-avatar", q, qx, sy + (sign_h - avatar) // 2, avatar, t.c("accent"), t.c("on_accent"))
        nh = _h(name_fit, index - 1, 1.2)
        rh = 0
        if q.get("role") and role_fit is not None:
            rh = _h(role_fit, role_index, 1.2)
        ny = sy + (sign_h - nh - (2 + rh if rh else 0)) // 2
        b.text(f"quote{index}-name", (qx + avatar + 14, ny, inner - avatar - 14, nh), [b.para(q["name"], name_fit, ink, bold=True)])
        if rh and role_fit is not None:
            b.text(f"quote{index}-role", (qx + avatar + 14, ny + nh + 2, inner - avatar - 14, rh), [b.para(q["role"], role_fit, soft)])
            role_index += 1


# ── roadmap ────────────────────────────────────────────────────────


def roadmap(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Gantt roadmap: workstreams down the side, periods across the top, a bar per workstream."""
    t, r = b.t, b.r
    y = _wide_header(b, slide) - 4
    periods = slide["periods"]
    tasks = slide["tasks"]
    x0, width = body_x(), body_w()
    label_w = min(300, int(width * 0.27))
    grid_x = x0 + label_w + 16
    grid_w = x0 + width - grid_x
    cell = grid_w / len(periods)
    period_fit = b.fit("period", periods, width_px=int(cell) - 8, sizes=(16, 14), leading=1.2, max_lines=1)
    head_h = _tallest(period_fit, 1.2) + 18
    label_fit = b.fit("task", [task["label"] for task in tasks], width_px=label_w, sizes=(18, 14), leading=1.25, max_lines=2)
    notes = [task.get("note", "") for task in tasks]
    row_h = min(78, (b.bottom - y - head_h) // len(tasks))
    if row_h < _tallest(label_fit, 1.25) + 12:
        raise FitError(f"{b.label} tasks: {len(tasks)} rows do not fit in the {b.bottom - y}px left; drop the lead or a task")
    bar_h = min(34, row_h - 18)
    note_fit = None
    if any(notes):
        widths = [int((task["end"] - task["start"] + 1) * cell) - 24 for task, note in zip(tasks, notes) if note]
        note_fit = b.fit("task-note", [n for n in notes if n], width_px=max(60, min(widths)), sizes=(14, 14), leading=1.1, max_lines=1)
    line = t.c("line")
    for index, period in enumerate(periods):
        px = int(grid_x + index * cell)
        b.text(f"period{index + 1}", (px + 4, y, int(cell) - 8, _h(period_fit, index, 1.2)), [b.para(period, period_fit, r["muted"], bold=True, align="ctr")])
        b.rect(f"period{index + 1}-rule", (px, y + head_h - 6, 1, row_h * len(tasks) + 6), line)
    b.rect("period-end-rule", (int(grid_x + grid_w) - 1, y + head_h - 6, 1, row_h * len(tasks) + 6), line)
    note_index = 0
    for index, task in enumerate(tasks, start=1):
        ry = y + head_h + (index - 1) * row_h
        if index > 1:
            b.rect(f"task{index}-rule", (x0, ry, width, 1), blend(r["bg"], line, 0.6))
        lh = _h(label_fit, index - 1, 1.25)
        b.text(f"task{index}", (x0, ry + (row_h - lh) // 2, label_w, lh), [b.para(task["label"], label_fit, r["text"], bold=True)])
        bx = int(grid_x + task["start"] * cell) + 6
        bw = int((task["end"] - task["start"] + 1) * cell) - 12
        color = t.chart[(index - 1) % len(t.chart)]
        by = ry + (row_h - bar_h) // 2
        if task.get("note") and note_fit is not None:
            ink = "FFFFFF" if contrast_ok("FFFFFF", color) else "111111"
            b.text(f"task{index}-bar", (bx, by, bw, bar_h), [b.para(task["note"], note_fit, ink, bold=True)], kind="round", fill=color, radius=bar_h // 2, anchor="ctr", inset_x=12, inset_y=0)
            note_index += 1
        else:
            b.rect(f"task{index}-bar", (bx, by, bw, bar_h), color, radius=bar_h // 2)


PAGE_FUNCTIONS = {
    "metric": metric,
    "logos": logos,
    "pricing": pricing,
    "device": device,
    "market": market,
    "launches": launches,
    "case_study": case_study,
    "testimonials": testimonials,
    "roadmap": roadmap,
}

PAGE_VARIANTS = {
    ("metric", "side"): metric_side,
    ("logos", "open"): lambda b, s, d: logos(b, s, d, cells=False),
    ("pricing", "columns"): lambda b, s, d: pricing(b, s, d, cards=False),
    ("device", "stage"): device_stage,
    ("market", "bars"): market_bars,
    ("launches", "icon"): launches_icon,
    ("launches", "chips"): launches_chips,
    ("case_study", "band"): case_study_band,
    ("testimonials", "columns"): lambda b, s, d: testimonials(b, s, d, cards=False),
}
