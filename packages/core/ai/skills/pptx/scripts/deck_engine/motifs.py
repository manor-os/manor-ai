"""Decorative motifs drawn as native shapes behind slide content.

Every motif is deterministic (seeded by the slide index) so a re-plan of the
same deck.json reproduces the exact same shapes, stays inside the canvas, and
carries no text — decoration never competes with audience copy.
"""

from __future__ import annotations

import random
from typing import Callable

from .model import CANVAS_H, CANVAS_W, SHAPE_PREFIX, Shape


class MotifCanvas:
    """Minimal surface the motif functions draw on."""

    def __init__(self, shapes: list[Shape], style, role: dict[str, str], seed: int):
        self.shapes = shapes
        self.style = style
        self.role = role
        self.rng = random.Random(seed)
        self.count = sum(1 for shape in shapes if shape.name.startswith(f"{SHAPE_PREFIX}motif"))

    def add(self, kind: str, box: tuple[float, float, float, float], fill: str | None, **extra) -> Shape:
        self.count += 1
        x, y, w, h = (int(round(v)) for v in box)
        x = max(0, min(x, CANVAS_W - 1))
        y = max(0, min(y, CANVAS_H - 1))
        w = max(1, min(w, CANVAS_W - x))
        h = max(1, min(h, CANVAS_H - y))
        shape = Shape(f"{SHAPE_PREFIX}motif{self.count}", kind, (x, y, w, h), fill=fill, **extra)
        self.shapes.append(shape)
        return shape


def _c(m: MotifCanvas, key: str) -> str:
    return m.style.colors.get(key) or m.role.get(key) or m.role["accent"]


def diamonds(m: MotifCanvas) -> None:
    tone = m.role["text"]
    for x, y, size, alpha in ((860, 120, 360, 10), (1010, 360, 250, 14), (720, 420, 180, 8)):
        m.add("rect", (x, y, size, size), tone, alpha=alpha, rotation=45.0)


def brackets(m: MotifCanvas) -> None:
    color = m.role["accent"]
    x, y, size, bar = 1084, 520, 96, 12
    m.add("rect", (x, y, size, bar), color)
    m.add("rect", (x, y, bar, size), color)
    m.add("rect", (x + 60, y + size - bar + 52, size, bar), color)
    m.add("rect", (x + 60 + size - bar, y + 52, bar, size), color)


def corner_mark(m: MotifCanvas) -> None:
    color = m.role["accent"]
    m.add("rect", (CANVAS_W - 72 - 28, 40, 28, 6), color)
    m.add("rect", (CANVAS_W - 72 - 6, 40, 6, 28), color)


def asterisk(m: MotifCanvas) -> None:
    color = m.style.colors["accent"]
    cx, cy, length, thick = 1120, 150, 96, 18
    for angle in (0.0, 60.0, 120.0):
        m.add("round", (cx - length / 2, cy - thick / 2, length, thick), color, radius=9, rotation=angle)


def meta_rule(m: MotifCanvas) -> None:
    m.add("rect", (72, 36, CANVAS_W - 144, 1), m.role["muted"], alpha=60)


def accent_bar(m: MotifCanvas) -> None:
    m.add("rect", (72, 0, 120, 8), m.style.colors["accent"])


def tracker(m: MotifCanvas) -> None:
    m.add("rect", (0, 0, CANVAS_W, 6), m.style.colors["primary"])
    m.add("rect", (72, 0, 96, 6), m.style.colors["accent"])


def big_numeral_field(m: MotifCanvas) -> None:
    m.add("rect", (CANVAS_W - 420, 0, 420, CANVAS_H), m.style.colors["surface"])


def ink_hills(m: MotifCanvas) -> None:
    ink = m.style.colors.get("ink", m.role["muted"])
    for x, w, h, alpha in ((560, 760, 300, 10), (760, 520, 380, 14), (980, 300, 240, 22), (420, 420, 180, 8)):
        m.add("triangle", (x, CANVAS_H - h, w, h), ink, alpha=alpha)


def seal(m: MotifCanvas) -> None:
    m.add("round", (CANVAS_W - 72 - 40, 56, 40, 40), m.style.colors["accent"], radius=4)


def paper_rule(m: MotifCanvas) -> None:
    m.add("rect", (72, CANVAS_H - 58, CANVAS_W - 144, 1), m.role["muted"], alpha=50)


def moon(m: MotifCanvas) -> None:
    accent = m.role["accent"]
    m.add("ellipse", (1020, 84, 150, 150), accent, alpha=90)
    m.add("ellipse", (1056, 66, 150, 150), m.style.colors["background"])


def confetti(m: MotifCanvas, count: int = 18) -> None:
    palette = [m.style.colors[key] for key in ("yellow", "cyan", "pink", "coral") if key in m.style.colors]
    for _ in range(count):
        size = m.rng.randint(10, 22)
        # Right-hand margins only: the title and body live on the left.
        x = m.rng.randint(1000, CANVAS_W - 48)
        # Below the top chrome band (brand, page index) and above the bottom one.
        y = m.rng.choice((m.rng.randint(76, 150), m.rng.randint(560, CANVAS_H - 80)))
        kind = m.rng.choice(("ellipse", "rect", "round"))
        m.add(kind, (x, y, size, size if kind == "ellipse" else size * 2), m.rng.choice(palette), rotation=float(m.rng.randint(0, 80)), radius=4 if kind == "round" else 0)


def confetti_sparse(m: MotifCanvas) -> None:
    palette = [m.style.colors[key] for key in ("yellow", "cyan", "pink", "coral") if key in m.style.colors]
    # Bottom-right corner, clear of titles and of the top chrome band.
    for index, (x, y) in enumerate(((1150, 652), (1190, 638), (1222, 664))):
        m.add("ellipse", (x, y, 14, 14), palette[index % len(palette)])


def circles(m: MotifCanvas) -> None:
    colors = m.style.colors
    m.add("ellipse", (860, 300, 380, 380), colors.get("yellow", m.role["accent"]), alpha=95)
    m.add("ellipse", (1040, 150, 200, 200), colors.get("pink", m.role["accent"]), alpha=95)


def rules(m: MotifCanvas) -> None:
    color = m.role["text"]
    m.add("rect", (72, 40, CANVAS_W - 144, 2), color)
    m.add("rect", (72, CANVAS_H - 44, CANVAS_W - 144, 1), color, alpha=60)


def rules_top(m: MotifCanvas) -> None:
    m.add("rect", (72, 36, CANVAS_W - 144, 1), m.role["text"], alpha=70)


def offset_block(m: MotifCanvas) -> None:
    m.add("rect", (960, 380, 248, 248), m.style.colors.get("mustard", m.role["accent"]))


def horizon(m: MotifCanvas) -> None:
    """A planet rising from the bottom edge, its rim lit in the accent colour."""
    from .vector import horizon_path

    box = (0, CANVAS_H - 230, CANVAS_W, 230)
    m.add("path", box, m.style.colors.get("planet", m.style.colors["surface"]), path=horizon_path(box[2], box[3]))
    m.add("path", box, None, path=horizon_path(box[2], box[3], rim=True), line=m.role["accent"], line_px=2.0)


def horizon_low(m: MotifCanvas) -> None:
    """The same rim, lower and fainter, under content."""
    from .vector import horizon_path

    box = (0, CANVAS_H - 90, CANVAS_W, 90)
    m.add("path", box, None, path=horizon_path(box[2], box[3], rim=True), line=m.role["accent"], line_px=1.5)


def kpi_ring(m: MotifCanvas) -> None:
    """A donut KPI ring: a faint full track and an accent arc."""
    from .vector import arc_path

    box = (716, 500, 132, 132)
    bg, muted = m.role["bg"], m.role["muted"]
    track = "".join(f"{(int(bg[i:i + 2], 16) * 3 + int(muted[i:i + 2], 16)) // 4:02X}" for i in (0, 2, 4))
    m.add("ellipse", (box[0] + 5, box[1] + 5, box[2] - 10, box[3] - 10), None, line=track, line_px=10.0)
    m.add("path", (box[0] + 5, box[1] + 5, box[2] - 10, box[3] - 10), None, path=arc_path(box[2] - 10, box[3] - 10, -90, 250),
          line=m.role["accent"], line_px=10.0)


def ui_cards(m: MotifCanvas) -> None:
    """Floating product cards: rounded panels with placeholder bars, like UI fragments."""
    surface = m.style.colors.get("card", "FFFFFF")
    bar = m.style.colors.get("card_bar", m.role["bg"])
    accent = m.role["accent"]
    for x, y, w, h in ((684, 150, 236, 120), (1000, 430, 220, 150)):
        m.add("round", (x, y, w, h), surface, radius=22, alpha=96)
        m.add("ellipse", (x + 20, y + 22, 34, 34), accent)
        m.add("round", (x + 68, y + 26, w - 96, 10), bar, radius=5, alpha=70)
        m.add("round", (x + 68, y + 46, (w - 96) // 2, 10), bar, radius=5, alpha=45)
        m.add("round", (x + 20, y + h - 36, w - 40, 14), bar, radius=7, alpha=35)


def swiss_grid(m: MotifCanvas) -> None:
    """A 12-column module grid with registration marks at its corners."""
    color = m.role["muted"]
    column = (CANVAS_W - 128) / 12
    for index in range(13):
        m.add("rect", (64 + index * column, 64, 1, CANVAS_H - 128), color, alpha=16)
    for y in (64, CANVAS_H - 64):
        m.add("rect", (0, y, CANVAS_W, 1), color, alpha=16)
    for x, y in ((64, 64), (CANVAS_W - 64, 64), (64, CANVAS_H - 64), (CANVAS_W - 64, CANVAS_H - 64)):
        m.add("rect", (x - 10, y, 21, 1), m.role["accent"])
        m.add("rect", (x, y - 10, 1, 21), m.role["accent"])


MOTIFS: dict[str, Callable[[MotifCanvas], None]] = {
    "diamonds": diamonds,
    "brackets": brackets,
    "corner_mark": corner_mark,
    "asterisk": asterisk,
    "meta_rule": meta_rule,
    "accent_bar": accent_bar,
    "tracker": tracker,
    "big_numeral_field": big_numeral_field,
    "ink_hills": ink_hills,
    "seal": seal,
    "paper_rule": paper_rule,
    "moon": moon,
    "confetti": confetti,
    "confetti_sparse": confetti_sparse,
    "circles": circles,
    "rules": rules,
    "rules_top": rules_top,
    "offset_block": offset_block,
    "horizon": horizon,
    "horizon_low": horizon_low,
    "kpi_ring": kpi_ring,
    "ui_cards": ui_cards,
    "swiss_grid": swiss_grid,
}

# Motifs that occupy the right half and must yield when an image or dense
# content uses that space.
RIGHT_HEAVY = {"diamonds", "ink_hills", "circles", "moon", "offset_block", "big_numeral_field", "brackets",
               "kpi_ring", "ui_cards", "horizon"}


# Motifs that draw in the top margin, where the page chrome (brand, index)
# lives when a style turns it on.
CHROME_ZONE = {"meta_rule", "rules_top", "corner_mark", "rules"}


def draw_motifs(shapes: list[Shape], style, role_name: str, role: dict[str, str], seed: int, *, light: bool = False) -> None:
    names = style.motifs.get(role_name, ())
    canvas = MotifCanvas(shapes, style, role, seed)
    chrome = bool(style.get("chrome.top", False))
    for name in names:
        if light and name in RIGHT_HEAVY:
            continue
        if chrome and name in CHROME_ZONE:
            continue
        MOTIFS[name](canvas)
