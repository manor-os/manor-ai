"""Template layouts: deterministic slide plans from validated deck content.

Every layout keeps the final-gate invariants by construction:

* the slide title is the largest wide content text in the upper 46% of the
  slide (cover >= 48 pt, other titles >= 32 pt, at most two lines);
* audience text is >= 14 pt; only named chrome / decoration layers
  (``tpl:chrome-*``, ``tpl:deco-*``) are smaller or sit behind content;
* text never overlaps text, and decorative shapes are drawn before (behind)
  the text they frame;
* everything stays inside the safe area, and every text box is sized from the
  conservative estimator in ``textfit`` so it can never overflow its frame.

The visual language — chrome, type scale, emphasis, page backgrounds, motifs,
cards, icons, image treatment and composition variants — comes from the deck's
style pack (``templates/decks/styles.json``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .icons import load_icon, resolve_icon
from .backdrop import backdrop
from .imaging import cutout, treat
from .model import (
    CANVAS_H,
    CANVAS_W,
    SHAPE_PREFIX,
    ChartSpec,
    IconSpec,
    Para,
    PictureSpec,
    Run,
    Shape,
    SlidePlan,
    Style,
    TableSpec,
)
from .motifs import RIGHT_HEAVY, draw_motifs
from .schema import contrast_ratio
from .textfit import PX_TO_PT, Fit, FitError, block_height_px, fit_texts, text_width_pt, wrapped_line_count
from .vector import blob_path, brush_path, underline_path

MARGIN = 64
CONTENT_W = CANVAS_W - 2 * MARGIN
GAP = 24
# Where the body of the slide being planned goes. The style's page frame sets it
# in ``_header`` (a magazine column moves the body right, a keynote frame narrows
# it); layouts that read it adopt the frame, the others keep the full width.
_BODY = {"x": MARGIN, "w": CONTENT_W}
COLUMN_W = 420


def body_x() -> int:
    return _BODY["x"]


def body_w() -> int:
    return _BODY["w"]
BODY = (20, 14)  # body text size range (pt)

PAGE_ROLES = {
    "cover": "cover",
    "section": "section",
    "closing": "closing",
    "quote": "contrast",
    "statement": "contrast",
    "metric": "contrast",
}
EMPHASIS = re.compile(r"\{([^{}]+)\}")


def plain(text: str) -> str:
    """Text as rendered: emphasis braces removed."""
    return EMPHASIS.sub(r"\1", text)


def _cjk(text: str) -> bool:
    return bool(re.search(r"[㐀-鿿]", text))


def _one_line(text: str) -> str:
    """Single-line slots join explicit line breaks instead of failing the fit."""
    return " · ".join(part.strip() for part in text.splitlines() if part.strip())


def blend(a: str, b: str, t: float) -> str:
    """Mix two hex colours: t=0 → a, t=1 → b."""
    ca = [int(a[i : i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i : i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{int(round(x + (y - x) * t)):02X}" for x, y in zip(ca, cb))


def _point_parts(point: Any) -> tuple[str, str]:
    if isinstance(point, str):
        return "", point.strip()
    return str(point.get("title", "")).strip(), str(point.get("text", "")).strip()


def _point_text(point: Any) -> str:
    title, text = _point_parts(point)
    if title and text:
        separator = "：" if _cjk(title + text) else ": "
        return f"{title}{separator}{text}"
    return title or text


class SlideBuilder:
    def __init__(self, style: Style, layout: str, label: str, role_name: str, index: int, project: Path, total: int):
        self.t = style
        self.label = label
        self.index = index
        self.total = total
        self.project = project
        self.role_name = role_name
        self.r = style.role(role_name)
        self.plan = SlidePlan(layout=layout)
        self.chosen: str | None = None  # composition picked for this slide (override or style rotation)
        self.background = self.r["bg"]
        self.body_start: int | None = None
        self.chrome_top = bool(style.get("chrome.top", False))
        self.chrome_bottom = style.get("chrome.bottom", "none")
        self.top = 92 if self.chrome_top else 52
        self.on_band = False  # the page frame put the title on a colour band
        self.bottom = 632 if self.chrome_bottom != "none" else 640

    # ── primitives ──────────────────────────────────────────────────
    def _name(self, slot: str) -> str:
        return f"{SHAPE_PREFIX}{slot}"

    def rect(
        self,
        slot: str,
        box: tuple[int, int, int, int],
        fill: str | None,
        *,
        radius: int = 0,
        kind: str = "rect",
        alpha: int = 100,
        line: str | None = None,
        line_px: float = 0.0,
        shadow: bool = False,
    ) -> Shape:
        shape = Shape(
            self._name(slot),
            "round" if radius and kind == "rect" else kind,
            tuple(int(v) for v in box),
            fill=fill,
            radius=radius,
            alpha=alpha,
            line=line,
            line_px=line_px,
            shadow=shadow and bool(self.t.get("shadows", False)),
        )
        self.plan.shapes.append(shape)
        return shape

    def path(self, slot: str, box: tuple[int, int, int, int], d: str, *, fill: str | None = None, alpha: int = 100, line: str | None = None, line_px: float = 0.0) -> None:
        self.plan.shapes.append(Shape(self._name(slot), "path", tuple(int(v) for v in box), fill=fill, alpha=alpha, line=line, line_px=line_px, path=d))

    def text(
        self,
        slot: str,
        box: tuple[int, int, int, int],
        paragraphs: list[Para],
        *,
        anchor: str = "t",
        inset_x: int = 4,
        inset_y: int = 2,
        kind: str = "text",
        fill: str | None = None,
        radius: int = 0,
        alpha: int = 100,
        line: str | None = None,
        line_px: float = 0.0,
    ) -> Shape:
        shape = Shape(
            self._name(slot),
            kind,
            tuple(int(v) for v in box),
            fill=fill,
            radius=radius,
            paragraphs=tuple(paragraphs),
            anchor=anchor,
            inset_x=inset_x,
            inset_y=inset_y,
            alpha=alpha,
            line=line,
            line_px=line_px,
        )
        self.plan.shapes.append(shape)
        return shape

    def para(self, text: str, fit: Fit, color: str, *, bold: bool = False, align: str = "l", font: str = "body") -> Para:
        return Para(runs=(Run(text=text, size=fit.size, color=color, bold=bold, font=font),), align=align, line_pt=fit.line_pt)

    def rich(self, text: str, fit: Fit, color: str, *, bold: bool, align: str = "l", font: str = "display", highlight: str | None = None, highlight_ink: str | None = None) -> Para:
        """A paragraph whose {braced} words use the style's emphasis treatment."""
        if highlight is None:
            highlight = self.t.get("emphasis.highlight")
            highlight = self.t.colors.get(highlight) if highlight else None
        emphasis_color = (highlight_ink or self.t.c("on_accent")) if highlight else self.emphasis_color()
        italic = bool(self.t.get("emphasis.italic", False))
        has_font = bool(self.t.get("emphasis.font"))
        runs: list[Run] = []
        cursor = 0
        for match in EMPHASIS.finditer(text):
            if match.start() > cursor:
                runs.append(Run(text=text[cursor : match.start()], size=fit.size, color=color, bold=bold, font=font))
            word = match.group(1)
            runs.append(
                Run(
                    text=word, size=fit.size, color=emphasis_color, bold=bold,
                    italic=italic and not _cjk(word), font="emphasis" if has_font and not _cjk(word) else font,
                    highlight=highlight,
                )
            )
            cursor = match.end()
        if cursor < len(text):
            runs.append(Run(text=text[cursor:], size=fit.size, color=color, bold=bold, font=font))
        return Para(runs=tuple(runs) or (Run(text="", size=fit.size, color=color),), align=align, line_pt=fit.line_pt)

    def emphasis_color(self) -> str:
        key = self.t.get("emphasis.color") or "accent"
        color = self.r[key] if key in self.r else self.t.colors.get(key, self.r["accent"])
        if contrast_ratio(color, self.r["bg"]) < 3.0:
            return self.r["accent"] if contrast_ratio(self.r["accent"], self.r["bg"]) >= 3.0 else self.r["text"]
        return color

    def fit(self, slot: str, texts: list[str], *, display: bool = False, **kwargs: Any) -> Fit:
        scale = self.t.display_scale if display else 1.0
        return fit_texts([plain(text) for text in texts], label=f"{self.label} {slot}", latin_scale=scale, **kwargs)

    def block(
        self,
        slot: str,
        x: int,
        y: int,
        width: int,
        text: str,
        *,
        sizes: tuple[float, float],
        color: str,
        leading: float = 1.3,
        max_lines: int | None = 2,
        max_height_px: int | None = None,
        bold: bool = False,
        align: str = "l",
        anchor: str = "t",
        display: bool = False,
        font: str | None = None,
    ) -> int:
        """Place one fitted text block and return its bottom edge."""
        fit = self.fit(slot, [text], width_px=width, sizes=sizes, leading=leading, max_lines=max_lines, max_height_px=max_height_px, display=display)
        self.text(slot, (x, y, width, fit.height_px), [self.para(plain(text), fit, color, bold=bold, align=align, font=font or ("display" if display else "body"))], anchor=anchor)
        return y + fit.height_px

    def title_text(self, text: str) -> str:
        if self.t.title_case != "upper":
            return text
        return EMPHASIS.sub(lambda m: "{" + m.group(1).upper() + "}", text).upper()

    def title_block(self, slot: str, x: int, y: int, width: int, text: str, *, sizes: tuple[float, float], color: str, leading: float | None = None, max_lines: int = 2, align: str = "l") -> int:
        text = self.title_text(text)
        leading = leading or float(self.t.get("type.leading", 1.1))
        fit = self.fit(slot, [text], width_px=width, sizes=sizes, leading=leading, max_lines=max_lines, display=True)
        self.text(slot, (x, y, width, fit.height_px), [self.rich(text, fit, color, bold=self.t.display_bold, align=align)])
        return y + fit.height_px

    def pill(self, slot: str, x: int, y: int, text: str, *, fill: str | None, color: str, line: str | None = None, max_w: int = 420, size: float = 14) -> int:
        """A rounded tag; returns its width."""
        width = min(max_w, max(72, int(text_width_pt(text, size) / PX_TO_PT / 0.85) + 40))
        fit = Fit(size=size, lines=[1], line_pt=round(size * 1.2, 1), height_px=int(size * 1.2 / PX_TO_PT) + 4, tallest_px=0)
        height = fit.height_px + 14
        self.text(slot, (x, y, width, height), [self.para(text, fit, color, bold=True, align="ctr")], kind="round", fill=fill or self.r["bg"], radius=height // 2, anchor="ctr", inset_x=12, inset_y=4, line=line, line_px=1.2 if line else 0)
        return width

    def icon(self, slot: str, keyword: str, box: tuple[int, int, int, int], color: str) -> None:
        name = resolve_icon(keyword, self.t.icons)
        if not name:
            return
        if any(path.mode == "faint" for path in load_icon(name).paths):
            self.plan.shapes.append(Shape(self._name(f"{slot}-bg"), "icon", box, icon=IconSpec(name=name, color=color), alpha=25))
        self.plan.shapes.append(Shape(self._name(slot), "icon", box, icon=IconSpec(name=name, color=color)))

    def picture(self, slot: str, relative: str, box: tuple[int, int, int, int], *, geometry: str = "rect", radius: int = 0, shadow: bool = False) -> None:
        treatment = self.t.get("image.treatment")
        if slot == "hero" and treatment:
            # Full-bleed photos take the style's treatment (soft focus, halftone screen).
            relative = str(treat(_resolve_image(self.project, relative, self.label), self.project, treatment))
        spec = _picture_spec(self.project, relative, box, self.label, geometry=geometry, radius=radius)
        self.plan.shapes.append(Shape(self._name(slot), "picture", box, picture=spec, shadow=shadow and bool(self.t.get("shadows", False))))

    def cutout_picture(self, slot: str, relative: str, area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """Background-removed image, bottom-anchored and contained in ``area``."""
        from PIL import Image

        source = _resolve_image(self.project, relative, self.label)
        prepared = cutout(source, self.project)
        with Image.open(prepared) as image:
            width, height = image.size
        x, y, w, h = area
        scale = min(w / width, h / height)
        bw, bh = int(width * scale), int(height * scale)
        box = (x + (w - bw) // 2, y + h - bh, bw, bh)
        self.plan.shapes.append(Shape(self._name(slot), "picture", box, picture=PictureSpec(path=str(prepared), crop=(0.0, 0.0, 0.0, 0.0))))
        return box

    def motifs(self, *, light: bool = False) -> None:
        draw_motifs(self.plan.shapes, self.t, self.role_name, self.r, self.index, light=light)

    def card_style(self, index: int) -> tuple[str, str, str]:
        cycle = self.t.card["cycle"]
        fill, text, muted = cycle[index % len(cycle)]
        return self.t.c(fill), self.t.c(text), self.t.c(muted)

    def variant(self, layout: str, default: str) -> str:
        if self.chosen:
            return self.chosen
        value = self.t.get(f"variants.{layout}", default)
        return str(value[0] if isinstance(value, list) else value)


# ── chrome: the small editorial layer on every page ────────────────


def _chrome(b: SlideBuilder, deck: dict[str, Any], *, on_image: bool = False) -> None:
    t = b.t
    muted = "FFFFFF" if on_image else b.r["muted"]
    font = "mono" if t.get("chrome.font") == "mono" else "body"
    label = plain(deck.get("brand") or deck.get("footer") or deck["title"])
    fit = Fit(size=10, lines=[1], line_pt=12.0, height_px=20, tallest_px=20)
    if b.chrome_top:
        b.text("chrome-brand", (MARGIN, 30, 560, 20), [b.para(label, fit, muted, bold=True, font=font)])
        b.text("chrome-index", (CANVAS_W - MARGIN - 160, 30, 160, 20), [b.para(f"{b.index:02d} / {b.total:02d}", fit, muted, align="r", font=font)])
        if t.get("chrome.rule"):
            b.rect("chrome-rule", (MARGIN, 60, CONTENT_W, 1), muted, alpha=45)
    if b.chrome_bottom == "dots" and b.total > 1:
        count = min(b.total, 16)
        current = min(b.index, count) - 1
        widths = [22 if i == current else 6 for i in range(count)]
        x = (CANVAS_W - (sum(widths) + 8 * (count - 1))) // 2
        for i, width in enumerate(widths):
            b.rect(f"chrome-dot{i + 1}", (x, 690, width, 6), muted if i != current else (b.r["accent"] if not on_image else "FFFFFF"), radius=3, alpha=60 if i != current else 100)
            x += width + 8
    elif b.chrome_bottom == "label":
        b.text("chrome-foot", (MARGIN, 684, 560, 20), [b.para(label, fit, muted, font=font)])


def _legacy_footer(b: SlideBuilder, deck: dict[str, Any], page: int, *, x: int = MARGIN, width: int = CONTENT_W) -> None:
    footer = plain(deck.get("footer") or deck["title"])
    text_w = min(860, width - 100)
    fit = b.fit("footer", [footer], width_px=text_w, sizes=(12, 10), leading=1.2, max_lines=1)
    b.text("footer", (x, 674, text_w, fit.height_px), [b.para(footer, fit, b.r["muted"])])
    if deck.get("page_numbers", True):
        number = f"{page:02d}"
        fit = b.fit("page-number", [number], width_px=80, sizes=(12, 12), leading=1.2, max_lines=1)
        b.text("page-number", (x + width - 80, 674, 80, fit.height_px), [b.para(number, fit, b.r["muted"], align="r")])


# ── shared regions ─────────────────────────────────────────────────


def _sizes(b: SlideBuilder, key: str, default: tuple[float, float]) -> tuple[float, float]:
    value = b.t.get(f"type.{key}")
    return (float(value[0]), float(value[1])) if value else default


def _header(b: SlideBuilder, slide: dict[str, Any], *, x: int = MARGIN, width: int = CONTENT_W, framed: bool = False) -> int:
    """Place kicker, title and lead; return where the body starts.

    ``framed`` layouts follow the style's page frame (``frame`` in the style):
    ``column`` hangs the title in a left margin column, ``band`` sets it on a
    colour band, ``centered`` centres it over a narrower body.
    """
    frame = b.t.get("frame", "standard") if framed else "standard"
    if frame != "standard":
        mark = len(b.plan.shapes)
        try:
            return _FRAMES[frame](b, slide)
        except FitError:
            # A title too long for the frame falls back to the standard header.
            del b.plan.shapes[mark:]
            _BODY.update(x=MARGIN, w=CONTENT_W)
            b.on_band = False
    t, r = b.t, b.r
    x0 = x
    if t.header == "side":
        x += 28
        width -= 28
    y = b.top
    if slide.get("kicker"):
        if t.header == "tag":
            on_content = b.role_name == "content"
            b.pill("kicker", x, y, slide["kicker"], fill=t.c("accent") if on_content else r["accent"], color=t.c("on_accent") if on_content else r["bg"], size=14)
            y += 50
        else:
            font = "mono" if t.get("chrome.font") == "mono" else "body"
            y = b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, font=font) + 8
    title_sizes = _sizes(b, "title", (44, 32))
    title_y = y
    y = b.title_block("title", x, y, width, slide["title"], sizes=title_sizes, color=r["text"])
    if t.header == "side":
        b.plan.shapes.insert(len(b.plan.shapes) - 1, Shape(b._name("header-mark"), "rect", (x0, title_y + 8, 6, y - title_y - 16), fill=r["accent"]))
    if t.header == "bar":
        b.rect("header-mark", (x + 4, y + 12, 56, 5), r["accent"])
        y += 17
    elif t.header == "rule":
        b.rect("header-mark", (x0, y + 14, width + (x - x0), 1), t.c("line"))
        y += 15
    elif t.header == "underline":
        b.path("header-mark", (x + 2, y + 6, 150, 14), underline_path(150, 14, b.index), line=r["accent"], line_px=3)
        y += 20
    if slide.get("lead"):
        y = b.block("lead", x, y + 14, width, slide["lead"], sizes=(20, 15), color=r["muted"], leading=1.4, max_lines=2)
    b.body_start = len(b.plan.shapes)
    return y + 32


def _header_column(b: SlideBuilder, slide: dict[str, Any]) -> int:
    """Magazine frame: kicker, title and lead hang in a left column; the body fills the right."""
    t, r = b.t, b.r
    x, width, y = MARGIN, COLUMN_W, b.top + 8
    if slide.get("kicker"):
        font = "mono" if t.get("chrome.font") == "mono" else "body"
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, font=font) + 10
    y = b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "title", (44, 32)), color=r["text"])
    b.rect("header-mark", (x + 4, y + 18, 48, 4), r["accent"])
    y += 22
    if slide.get("lead"):
        b.block("lead", x, y + 18, width, slide["lead"], sizes=(18, 14), color=r["muted"], leading=1.45, max_lines=5)
    b.rect("header-divider", (x + width + 31, b.top + 8, 1, b.bottom - b.top - 16), t.c("line"))
    _BODY.update(x=x + width + 64, w=CANVAS_W - MARGIN - (x + width + 64))
    b.body_start = len(b.plan.shapes)
    return b.top + 8


def _header_band(b: SlideBuilder, slide: dict[str, Any]) -> int:
    """Corporate frame: the title sits on a full-width colour band; the body below it."""
    t = b.t
    ink, soft, accent = t.c("on_primary"), t.c("on_primary_muted"), t.c("accent_on_dark") if "accent_on_dark" in t.colors else t.c("on_primary")
    band_at = len(b.plan.shapes)
    y = b.top
    if slide.get("kicker"):
        font = "mono" if t.get("chrome.font") == "mono" else "body"
        y = b.block("kicker", MARGIN, y, CONTENT_W, slide["kicker"], sizes=(16, 14), color=accent, max_lines=1, bold=True, font=font) + 8
    y = b.title_block("title", MARGIN, y, CONTENT_W, slide["title"], sizes=_sizes(b, "title", (44, 32)), color=ink)
    if slide.get("lead"):
        y = b.block("lead", MARGIN, y + 10, CONTENT_W, slide["lead"], sizes=(18, 14), color=soft, leading=1.4, max_lines=2)
    band_h = y + 30
    b.plan.shapes.insert(band_at, Shape(b._name("header-band"), "rect", (0, 0, CANVAS_W, band_h), fill=t.c("primary")))
    b.on_band = True
    b.body_start = len(b.plan.shapes)
    return band_h + 40


def _header_centered(b: SlideBuilder, slide: dict[str, Any]) -> int:
    """Keynote frame: a centred title over a narrower, centred body."""
    t, r = b.t, b.r
    y = b.top
    if slide.get("kicker"):
        font = "mono" if t.get("chrome.font") == "mono" else "body"
        y = b.block("kicker", MARGIN, y, CONTENT_W, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, align="ctr", font=font) + 8
    y = b.title_block("title", MARGIN, y, CONTENT_W, slide["title"], sizes=_sizes(b, "title", (44, 32)), color=r["text"], align="ctr")
    b.rect("header-mark", (CANVAS_W // 2 - 24, y + 14, 48, 4), r["accent"])
    y += 18
    if slide.get("lead"):
        y = b.block("lead", MARGIN + 160, y + 14, CONTENT_W - 320, slide["lead"], sizes=(18, 14), color=r["muted"], leading=1.4, max_lines=2, align="ctr")
    _BODY.update(x=MARGIN + 96, w=CONTENT_W - 192)
    b.body_start = len(b.plan.shapes)
    return y + 40


_FRAMES = {"column": _header_column, "band": _header_band, "centered": _header_centered}


BALANCED_LAYOUTS = {"agenda", "bullets", "cards", "process", "timeline", "stats", "comparison", "quote", "icon_grid", "matrix"}


def _balance_body(b: SlideBuilder) -> None:
    """Move short body content down so free space is split ~1:2 above/below."""
    if b.body_start is None:
        return
    body = [shape for shape in b.plan.shapes[b.body_start :] if not shape.name.startswith(f"{SHAPE_PREFIX}chrome-")]
    if not body:
        return
    bottom = max(shape.box[1] + shape.box[3] for shape in body)
    free = b.bottom - bottom
    if free < 48:
        return
    shift = free // 3
    for shape in body:
        x, y, w, h = shape.box
        shape.box = (x, y + shift, w, h)


def _rows(
    b: SlideBuilder,
    slot: str,
    points: list[Any],
    *,
    x: int,
    y: int,
    width: int,
    max_height: int,
    sizes: tuple[float, float] = (22, 14),
    text_color: str | None = None,
    accent: str | None = None,
) -> int:
    """Stacked text rows with an accent marker (or the point's icon)."""
    r = b.r
    text_color = text_color or r["text"]
    accent = accent or r["accent"]
    texts = [_point_text(point) for point in points]
    has_icons = any(isinstance(point, dict) and point.get("icon") for point in points)
    indent = 44 if has_icons else 28
    gap_px = 18
    fit = b.fit(slot, texts, width_px=width - indent, sizes=sizes, leading=1.45, max_lines=3, max_height_px=max_height, gap_pt=(gap_px + 4) * PX_TO_PT)
    for index, (point, text, lines) in enumerate(zip(points, texts, fit.lines), start=1):
        height = block_height_px([lines], fit.size, 1.45, 0.0, 2)
        line_px = int(fit.line_pt / PX_TO_PT)
        if isinstance(point, dict) and point.get("icon"):
            size = min(26, line_px)
            b.icon(f"{slot}{index}-icon", point["icon"], (x, y + 2 + (line_px - size) // 2, size, size), accent)
        else:
            b.rect(f"{slot}{index}-mark", (x + 2, y + 2 + line_px // 2 - 4, 8, 8), accent, kind="ellipse")
        title, body = _point_parts(point)
        if title and body:
            separator = "：" if _cjk(title + body) else ": "
            paragraph = Para(
                runs=(
                    Run(text=f"{title}{separator}", size=fit.size, color=text_color, bold=True),
                    Run(text=body, size=fit.size, color=blend(text_color, r["bg"], 0.2)),
                ),
                line_pt=fit.line_pt,
            )
        else:
            paragraph = b.para(text, fit, text_color)
        b.text(f"{slot}{index}", (x + indent, y, width - indent, height), [paragraph])
        y += height + gap_px
    return y - gap_px


def _columns(count: int, *, x: int | None = None, width: int | None = None, gap: int = GAP) -> list[tuple[int, int]]:
    x = _BODY["x"] if x is None else x
    width = _BODY["w"] if width is None else width
    column = (width - gap * (count - 1)) // count
    return [(x + index * (column + gap), column) for index in range(count)]


def _icon_mark(b: SlideBuilder, slot: str, keyword: str, x: int, y: int, size: int, *, fill: str, color: str) -> None:
    """Icon inside the style's container (circle / rounded square / none)."""
    container = b.t.icon_container
    if container == "circle":
        b.rect(f"{slot}-chip", (x, y, size, size), fill, kind="ellipse")
    elif container == "square":
        b.rect(f"{slot}-chip", (x, y, size, size), fill, radius=size // 4)
    pad = size // 4 if container != "none" else 0
    b.icon(slot, keyword, (x + pad, y + pad, size - 2 * pad, size - 2 * pad), color)


# ── covers, sections, closings ─────────────────────────────────────


def _resolve_image(project: Path, relative: str, label: str) -> Path:
    path = (project / relative).resolve() if not relative.startswith("/") else Path(relative)
    if not path.is_file():
        raise FitError(f"{label} image: {relative!r} not found (put images under <project>/images/)")
    return path


def _cover(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    variant = b.t.cover if slide.get("image") else b.t.get("cover_plain", "field")
    if variant == "typographic":
        _cover_typographic(b, slide, deck)
    elif variant == "cutout":
        _cover_cutout(b, slide, deck)
    elif variant in ("glass", "full"):
        _cover_glass(b, slide, deck, glass=variant == "glass")
    elif variant == "centered":
        _cover_centered(b, slide, deck)
    elif variant == "poster":
        _cover_poster(b, slide, deck)
    elif variant == "device":
        _cover_device(b, slide, deck)
    elif variant == "stage":
        _cover_stage(b, slide, deck)
    elif variant == "bleed":
        _cover_bleed(b, slide, deck)
    else:
        _cover_field(b, slide, deck, split=variant == "split")


def _cover_text(b: SlideBuilder, slide: dict[str, Any], x: int, y: int, width: int, *, color: str, muted: str, accent: str, title_sizes: tuple[float, float]) -> int:
    if slide.get("kicker"):
        font = "mono" if b.t.get("chrome.font") == "mono" else "body"
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(18, 14), color=accent, max_lines=1, bold=True, font=font) + 14
    y = b.title_block("title", x, y, width, slide["title"], sizes=title_sizes, color=color, leading=float(b.t.get("type.cover_leading", 1.05))) + 14
    if b.t.get("decor.underline"):
        b.path("title-underline", (x + 4, y - 4, min(260, width // 2), 18), underline_path(min(260, width // 2), 18, b.index), line=accent, line_px=4)
        y += 26
    else:
        b.rect("title-mark", (x + 4, y, 72, 6), accent)
        y += 30
    if slide.get("subtitle"):
        y = b.block("subtitle", x, y, width, slide["subtitle"], sizes=(24, 16), color=muted, leading=1.4)
    return y


def _cover_field(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, split: bool) -> None:
    r = b.r
    # Right-side motifs (moon, hills, diamonds…) sit behind the title area on
    # an image-less cover; keep the title clear of them.
    heavy = any(name in RIGHT_HEAVY for name in b.t.motifs.get("cover", ()))
    width = 760 if heavy and not split else 1000
    b.motifs(light=split)
    if split:
        b.picture("hero", slide["image"], (700, 0, CANVAS_W - 700, CANVAS_H))
        width = 700 - MARGIN - 48
    _chrome(b, deck)
    y = _cover_text(b, slide, MARGIN, 150, width, color=r["text"], muted=r["muted"], accent=r["accent"], title_sizes=_sizes(b, "cover", (64, 48)))
    if slide.get("meta"):
        b.block("meta", MARGIN, max(y + 36, 590), width, slide["meta"], sizes=(16, 14), color=r["muted"], max_lines=1)


def _cover_typographic(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Mono-Acid style: small left column, giant title, wide rounded image band."""
    t, r = b.t, b.r
    _chrome(b, deck)
    left_w = 300
    title_x = MARGIN + left_w + 40
    title_bottom = b.title_block("title", title_x, b.top - 6, CANVAS_W - MARGIN - title_x, slide["title"], sizes=_sizes(b, "cover", (88, 52)), color=r["text"], leading=1.0)
    band_y = max(title_bottom + 28, 340)
    y = b.top + 8
    if slide.get("kicker"):
        y = b.block("kicker", MARGIN, y, left_w, slide["kicker"], sizes=(18, 14), color=r["text"], max_lines=2, bold=True) + 10
    if slide.get("subtitle"):
        b.block("subtitle", MARGIN, y, left_w, slide["subtitle"], sizes=(18, 14), color=r["muted"], max_lines=6, leading=1.4, max_height_px=band_y - 20 - y)
    band = (MARGIN, band_y, CONTENT_W, 664 - band_y)
    b.picture("hero", slide["image"], band, geometry="round", radius=28, shadow=True)
    if "asterisk" in t.motifs.get("cover", ()):
        cx, cy, length, thick = MARGIN + 72, band_y + 64, 80, 16
        for index, angle in enumerate((0.0, 60.0, 120.0), start=1):
            b.rect(f"deco-asterisk{index}", (cx - length // 2, cy - thick // 2, length, thick), t.c("accent"), radius=8).rotation = angle
    if slide.get("meta"):
        b.pill("meta", MARGIN + 24, 664 - 24 - 40, slide["meta"], fill="FFFFFF", color="111111", size=14, max_w=CONTENT_W - 48)


def _cover_cutout(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Editorial style: big title left, cut-out subject over an organic block right."""
    t, r = b.t, b.r
    b.motifs(light=True)
    block = (740, 160, 440, 440)
    shape = t.get("decor.block", "blob")
    d = brush_path(block[2], block[3], b.index) if shape == "brush" else blob_path(block[2], block[3], b.index)
    b.path("deco-block", block, d, fill=t.c(t.get("decor.block_color", "accent")))
    b.cutout_picture("hero", slide["image"], (700, 96, 520, 624))
    _chrome(b, deck)
    y = _cover_text(b, slide, MARGIN, 150, 620, color=r["text"], muted=r["muted"], accent=r["accent"], title_sizes=_sizes(b, "cover", (88, 52)))
    if slide.get("meta"):
        b.pill("meta", MARGIN, max(y + 30, 560), slide["meta"], fill=r["text"], color=r["bg"], size=14, max_w=620)


def _cover_glass(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, glass: bool) -> None:
    """Full-bleed photo with a frosted card (sky) or a dark scrim (cinematic)."""
    t = b.t
    b.picture("hero", slide["image"], (0, 0, CANVAS_W, CANVAS_H))
    if glass:
        # Frosted card: a near-opaque light panel keeps dark text legible on any photo.
        b.r = {"bg": "FFFFFF", "text": t.c("text"), "muted": t.c("muted"), "accent": t.c("accent")}
        b.rect("glass", (MARGIN - 8, 112, 700, 496), "FFFFFF", radius=32, alpha=78, line="FFFFFF", line_px=1.5, shadow=True)
        x, width = MARGIN + 36, 620
    else:
        b.rect("scrim", (0, 0, CANVAS_W, CANVAS_H), "000000", alpha=48)
        b.r = {"bg": "000000", "text": "FFFFFF", "muted": "F0F0F0", "accent": t.c("accent")}
        x, width = MARGIN, 700
    _chrome(b, deck, on_image=True)
    y = _cover_text(b, slide, x, 160, width, color=b.r["text"], muted=b.r["muted"], accent=b.r["accent"], title_sizes=_sizes(b, "cover", (72, 48)))
    if slide.get("meta"):
        fill, ink = (t.c("primary"), t.c("on_primary")) if glass else ("FFFFFF", "111111")
        b.pill("meta", x, max(y + 24, 520), slide["meta"], fill=fill, color=ink, size=14, max_w=width)


def _cover_bleed(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Photo essay: the picture edge to edge, the title low at the left over a soft shadow."""
    _photo_page(b, slide)
    _chrome(b, deck, on_image=True)
    r = b.r
    y = _cover_text(b, slide, MARGIN, 190, 760, color=r["text"], muted=r["muted"], accent=r["accent"], title_sizes=_sizes(b, "cover", (84, 52)))
    if y > 600:
        raise FitError(f"{b.label} title: the cover text runs past the foot of the page; shorten the title or subtitle")
    if slide.get("meta"):
        b.block("meta", MARGIN, max(y + 32, 592), 760, slide["meta"], sizes=(14, 14), color=r["muted"], max_lines=1, font="mono")


def _cover_centered(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Aurora style: a field of colour, one quiet centred title, a small mono caption."""
    if slide.get("image"):
        # The photo sits under the style's colour field, tinted rather than framed.
        b.picture("hero", slide["image"], (0, 0, CANVAS_W, CANVAS_H))
        colors = b.t.colors
        if b.t.get("image.tint") == "scrim":
            # A treated photo already reads as light; a thin veil keeps white type legible.
            b.rect("tint", (0, 0, CANVAS_W, CANVAS_H), "000000", alpha=24)
        else:
            b.plan.shapes.append(Shape(b._name("tint"), "gradient", (0, 0, CANVAS_W, CANVAS_H),
                                       gradient=((colors["grad_a"], 0, 82), (colors["grad_b"], 55, 70), (colors["grad_c"], 100, 82)), gradient_angle=120.0))
    else:
        b.motifs()
    _chrome(b, deck)
    r = b.r
    width = 980
    x = (CANVAS_W - width) // 2
    y = 196
    if slide.get("kicker"):
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, align="ctr", font="mono") + 18
    y = b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "cover", (72, 48)), color=r["text"], align="ctr",
                      leading=float(b.t.get("type.cover_leading", 1.05))) + 22
    if slide.get("subtitle"):
        y = b.block("subtitle", x + 100, y, width - 200, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.4, align="ctr")
    if slide.get("meta"):
        b.block("meta", x, max(y + 48, 600), width, slide["meta"], sizes=(14, 14), color=r["muted"], max_lines=1, align="ctr", font="mono")


def _cover_poster(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Documentary style: full-bleed reportage photo, a giant condensed headline set low, a mono caption."""
    if slide.get("image"):
        b.picture("hero", slide["image"], (0, 0, CANVAS_W, CANVAS_H))
        # Darken toward the bottom so the white headline reads on any photograph.
        b.plan.shapes.append(Shape(b._name("scrim"), "gradient", (0, 0, CANVAS_W, CANVAS_H),
                                   gradient=(("000000", 0, 10), ("000000", 40, 30), ("000000", 100, 88)), gradient_angle=270.0))
        b.r = {"bg": "000000", "text": "FFFFFF", "muted": "E6E6E6", "accent": b.t.c("accent_on_dark")}
        _chrome(b, deck, on_image=True)
    else:
        b.motifs()
        _chrome(b, deck)
    r = b.r
    y = 236
    if slide.get("kicker"):
        y = b.block("kicker", MARGIN, y, CONTENT_W, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True, font="mono") + 24
    # The headline gets a fixed band so subtitle and caption always fit above the footer.
    text = b.title_text(slide["title"])
    # Tight leading suits Latin capitals; CJK glyphs need their full line or they rise into the kicker.
    leading = float(b.t.get("type.cover_leading", 0.95))
    leading = max(leading, 1.15) if _cjk(plain(text)) else leading
    fit = b.fit("title", [text], width_px=CONTENT_W, sizes=_sizes(b, "cover", (112, 64)), leading=leading,
                max_lines=2, max_height_px=226, display=True)
    b.text("title", (MARGIN, y, CONTENT_W, fit.height_px), [b.rich(text, fit, r["text"], bold=b.t.display_bold)])
    y += fit.height_px + 18
    if slide.get("subtitle"):
        y = b.block("subtitle", MARGIN, y, 820, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.35, max_lines=2,
                    max_height_px=596 - y)
    if slide.get("meta"):
        b.rect("meta-rule", (MARGIN, y + 20, CONTENT_W, 1), r["muted"], alpha=60)
        b.block("meta", MARGIN, y + 32, CONTENT_W, slide["meta"], sizes=(14, 14), color=r["muted"], max_lines=1, font="mono")


def _cover_device(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Product as hero: the picture on a phone-shaped screen at the right, a heavy headline at the left."""
    t, r = b.t, b.r
    b.motifs()
    if slide.get("image"):
        frame = t.colors.get("device", "111111")
        b.rect("device", (858, 72, 312, 588), frame, radius=46, shadow=True)
        b.picture("hero", slide["image"], (872, 86, 284, 560), geometry="round", radius=34)
    _chrome(b, deck)
    y = _cover_text(b, slide, MARGIN, 168, 640, color=r["text"], muted=r["muted"], accent=r["accent"], title_sizes=_sizes(b, "cover", (84, 52)))
    if slide.get("meta"):
        b.pill("meta", MARGIN, max(y + 30, 560), slide["meta"], fill=r["accent"], color=t.c("on_accent"), size=14, max_w=600)


def _cover_stage(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Keynote stage: the name centred at the top, the product shot floating below it."""
    r = b.r
    b.motifs()
    _chrome(b, deck)
    width = 1000
    x = (CANVAS_W - width) // 2
    y = 84
    if slide.get("kicker"):
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True, align="ctr") + 10
    y = b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "cover", (84, 52)), color=r["text"], align="ctr", max_lines=2) + 12
    if slide.get("subtitle"):
        y = b.block("subtitle", x + 120, y, width - 240, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.35, align="ctr", max_lines=2)
    bottom = 640 if slide.get("meta") else 676
    if slide.get("image") and bottom - (y + 28) >= 180:
        top = y + 28
        height = bottom - top
        w = min(int(height * 1.6), 900)
        b.picture("hero", slide["image"], ((CANVAS_W - w) // 2, top, w, height))
    if slide.get("meta"):
        b.block("meta", x, 648, width, slide["meta"], sizes=(14, 14), color=r["muted"], max_lines=1, align="ctr")


def _photo_page(b: SlideBuilder, slide: dict[str, Any], *, side: str = "left") -> None:
    """The page's photo full-bleed under a scrim that darkens where the text sits; text turns white.

    ``side`` is where the text is: ``left`` (a gradient from the left edge),
    ``bottom`` (from the foot of the page) or ``center`` (an even veil).
    """
    ink = b.t.colors.get("scrim", "000000")
    b.picture("hero", slide["image"], (0, 0, CANVAS_W, CANVAS_H))
    if side == "center":
        b.rect("scrim", (0, 0, CANVAS_W, CANVAS_H), ink, alpha=58)
    else:
        stops = ((ink, 0, 84), (ink, 48, 52), (ink, 100, 6)) if side == "left" else ((ink, 0, 0), (ink, 45, 18), (ink, 100, 86))
        b.plan.shapes.append(Shape(b._name("scrim"), "gradient", (0, 0, CANVAS_W, CANVAS_H), gradient=stops, gradient_angle=0.0 if side == "left" else 90.0))
    accent = b.t.colors.get("accent_on_dark", b.r["accent"])
    b.r = {"bg": ink, "text": "FFFFFF", "muted": "EDEDED", "accent": accent if contrast_ratio(accent, ink) >= 3.0 else "FFFFFF"}


def _image_half(b: SlideBuilder, slide: dict[str, Any]) -> int:
    """Right-hand bleed image for split pages; returns the text column width."""
    b.picture("hero", slide["image"], (700, 0, CANVAS_W - 700, CANVAS_H))
    return 700 - MARGIN - 48


def _section(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], number: int) -> None:
    width = 1040
    if slide.get("image") and b.t.get("image.bleed"):
        _photo_page(b, slide)
        width = 760
    elif slide.get("image"):
        b.motifs(light=True)
        width = _image_half(b, slide)
    else:
        b.motifs()
    _chrome(b, deck)
    r = b.r
    x = MARGIN
    label = slide.get("number") or f"{number:02d}"
    if slide.get("kicker"):
        b.block("kicker", x, 132, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True)
    number_bottom = b.block("number", x, 168, 360, label, sizes=(96, 64), color=r["accent"], leading=1.0, max_lines=1, bold=b.t.display_bold, display=True)
    y = b.title_block("title", x, max(300, number_bottom + 12), width, slide["title"], sizes=_sizes(b, "section", (56, 36)), color=r["text"])
    if slide.get("subtitle"):
        b.block("subtitle", x, y + 18, width, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.4)


def _closing(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    width = CONTENT_W
    if slide.get("image") and b.t.get("image.bleed"):
        _photo_page(b, slide)
        width = 760
    elif slide.get("image"):
        b.motifs(light=True)
        width = _image_half(b, slide)
    else:
        b.motifs()
    _chrome(b, deck)
    r = b.r
    x = MARGIN
    y = 130 if slide.get("points") else 230
    if slide.get("kicker"):
        y = b.block("kicker", x, y - 40, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True) + 8
    y = b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "section", (56, 36)), color=r["text"])
    if slide.get("subtitle"):
        y = b.block("subtitle", x, y + 16, width, slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.4)
    if slide.get("points"):
        bottom = 574 if slide.get("contact") else 640
        y += 32
        _rows(b, "point", slide["points"], x=x, y=y, width=min(width, 760), max_height=bottom - y, sizes=(22, 14))
    if slide.get("contact"):
        b.block("contact", x, 600, width, slide["contact"], sizes=(18, 14), color=r["muted"], max_lines=1)


def _statement(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    width = 1080
    if slide.get("image") and b.t.get("image.bleed"):
        _photo_page(b, slide)
        width = 820
    elif slide.get("image"):
        b.motifs(light=True)
        width = _image_half(b, slide)
    r = b.r
    if not slide.get("image"):
        if b.variant("statement", "plain") == "ghost" and slide.get("kicker"):
            ghost = plain(slide["kicker"]).upper()
            color = blend(r["bg"], r["text"], 0.07)
            b.block("deco-ghost", 40, 468, CANVAS_W - 80, ghost, sizes=(160, 60), color=color, leading=1.0, max_lines=1, bold=True, display=True)
        b.motifs()
    _chrome(b, deck)
    x = MARGIN
    y = 150
    if slide.get("kicker"):
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(18, 14), color=r["accent"], max_lines=1, bold=True) + 12
    b.rect("statement-mark", (x + 4, y, 56, 6), r["accent"])
    y = b.title_block("title", x, y + 28, width, slide["title"], sizes=_sizes(b, "statement", (60, 36)), color=r["text"])
    if slide.get("subtitle"):
        b.block("subtitle", x, y + 24, min(width, 820), slide["subtitle"], sizes=(22, 16), color=r["muted"], leading=1.45, max_lines=3)


# ── content layouts ─────────────────────────────────────────────────


def _agenda(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    y = _header(b, slide, framed=True)
    r = b.r
    items = slide["items"]
    halves = [items] if len(items) <= 4 else [items[: (len(items) + 1) // 2], items[(len(items) + 1) // 2 :]]
    columns = _columns(len(halves), gap=48)
    offset = 0
    for (x, width), group in zip(columns, halves):
        fit = b.fit("items", list(group), width_px=width - 84, sizes=(30, 18), leading=1.3, max_lines=2, max_height_px=b.bottom - y, gap_pt=(32 + 4) * PX_TO_PT)
        row_y = y
        for index, (item, lines) in enumerate(zip(group, fit.lines), start=offset + 1):
            height = block_height_px([lines], fit.size, 1.3, 0.0, 2)
            b.text(f"item{index}-number", (x, row_y, 72, height), [b.para(f"{index:02d}", fit, r["accent"], bold=b.t.display_bold, font="display")])
            b.text(f"item{index}", (x + 84, row_y, width - 84, height), [b.para(item, fit, r["text"])])
            b.rect(f"item{index}-rule", (x, row_y + height + 15, width, 1), b.t.c("line"))
            row_y += height + 32
        offset += len(group)


def _bullets(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    y = _header(b, slide, framed=True)
    _rows(b, "point", slide["points"], x=body_x(), y=y, width=min(body_w(), 980), max_height=b.bottom - y)


def _cards(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any], *, variant: str) -> None:
    if variant == "tall":
        return _tall_cards(b, slide, deck)
    t, r = b.t, b.r
    y = _header(b, slide, framed=True)
    items = slide["items"]
    columns = _columns(len(items))
    pad = 28
    inner = columns[0][1] - 2 * pad
    radius = int(t.card.get("radius", 12))
    labels = [item.get("label") or f"{index:02d}" for index, item in enumerate(items, start=1)]
    head_h = 56 if variant == "icon" else (64 if variant == "numeral" and t.numerals == "big" else 40)
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=inner, sizes=(26, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [item.get("text", "") for item in items]
    available = b.bottom - y - (pad + head_h + 14 + title_h + 12 + pad)
    text_fit = None
    text_h = 0
    if any(texts):
        text_fit = b.fit("item-text", [text for text in texts if text], width_px=inner, sizes=(20, 14), leading=1.5, each_max_height_px=available)
        text_h = max(block_height_px([lines], text_fit.size, 1.5, 0, 2) for lines in text_fit.lines)
    needed = pad + head_h + 14 + title_h + (12 + text_h if text_h else 0) + pad
    if needed > b.bottom - y:
        raise FitError(f"{b.label} items: cards need {needed}px but only {b.bottom - y}px remain; shorten item text or drop the lead")
    card_h = min(b.bottom - y, max(needed, 280))
    outline = t.card.get("outline")
    alpha = int(t.card.get("alpha", 100))
    text_index = 0
    for index, ((x, width), item) in enumerate(zip(columns, items), start=1):
        fill, ink, soft = b.card_style(index - 1 if variant != "outline" else 0)
        if outline and variant in ("outline", "icon", "numeral"):
            fill, ink, soft = r["bg"], r["text"], r["muted"]
        b.rect(f"card{index}", (x, y, width, card_h), fill, radius=radius, alpha=alpha, line=t.c(outline) if outline else None, line_px=1.2 if outline else 0, shadow=True)
        cy = y + pad
        if variant == "icon" and item.get("icon"):
            _icon_mark(b, f"card{index}-icon", item["icon"], x + pad, cy, 52, fill=t.c("accent") if fill != t.c("accent") else t.c("primary"), color=t.c("on_accent") if fill != t.c("accent") else t.c("on_primary"))
        elif variant == "numeral":
            _numeral(b, f"card{index}-label", labels[index - 1], x + pad, cy, fill=ink, color=ink, on_fill=fill, width=inner)
        else:
            b.rect(f"card{index}-stripe", (x + pad, cy, 36, 5), ink if fill != r["bg"] else r["accent"])
            b.block(f"card{index}-label", x + pad, cy + 14, inner, labels[index - 1], sizes=(16, 14), color=soft, max_lines=1, bold=True)
        cy += head_h + 14
        lines = title_fit.lines[index - 1]
        b.text(f"card{index}-title", (x + pad, cy, inner, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(item["title"], title_fit, ink, bold=True)])
        cy += title_h + 12
        if item.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"card{index}-text", (x + pad, cy, inner, block_height_px([lines], text_fit.size, 1.5, 0, 2)), [b.para(item["text"], text_fit, soft)])


def _tall_cards(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Mono-Acid style: tall rounded cards, copy on top, giant numeral at the foot."""
    t = b.t
    y = _header(b, slide)
    items = slide["items"]
    columns = _columns(len(items), gap=16)
    pad = 26
    inner = columns[0][1] - 2 * pad
    radius = int(t.card.get("radius", 24))
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=inner, sizes=(24, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [item.get("text", "") for item in items]
    card_h = b.bottom - y
    numeral_h = 104
    text_fit = None
    if any(texts):
        text_fit = b.fit("item-text", [text for text in texts if text], width_px=inner, sizes=(18, 14), leading=1.45, each_max_height_px=card_h - 2 * pad - title_h - 10 - numeral_h - 20)
    labels = [item.get("label") or f"{index:02d}" for index, item in enumerate(items, start=1)]
    numeral_fit = b.fit("numeral", labels, width_px=inner, sizes=(80, 40), leading=1.0, max_lines=1, display=True)
    text_index = 0
    for index, ((x, width), item) in enumerate(zip(columns, items), start=1):
        fill, ink, soft = b.card_style(index - 1)
        b.rect(f"card{index}", (x, y, width, card_h), fill, radius=radius, shadow=True)
        lines = title_fit.lines[index - 1]
        b.text(f"card{index}-title", (x + pad, y + pad, inner, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(item["title"], title_fit, ink, bold=True)])
        if item.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"card{index}-text", (x + pad, y + pad + title_h + 10, inner, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(item["text"], text_fit, soft)])
        ny = y + card_h - pad - numeral_fit.tallest_px - 10
        b.text(f"card{index}-numeral", (x + pad, ny, inner, numeral_fit.tallest_px), [b.para(labels[index - 1], numeral_fit, ink, bold=t.display_bold, font="display")])
        b.rect(f"card{index}-numeral-rule", (x + pad, ny + numeral_fit.tallest_px + 4, inner * 2 // 3, 2), ink)


def _numeral(b: SlideBuilder, slot: str, label: str, x: int, y: int, *, fill: str, color: str, on_fill: str, size: int = 52, width: int = 160) -> int:
    """Number marker in the style's numeral treatment; returns its height."""
    mode = b.t.numerals
    if mode == "circle" and len(label) > 2:
        mode = "plain"  # a circle only holds a short number
    if mode == "circle":
        fit = Fit(size=20, lines=[1], line_pt=24.0, height_px=size, tallest_px=size)
        b.text(slot, (x, y, size, size), [b.para(label, fit, on_fill, bold=True, align="ctr")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
        return size
    if mode == "big":
        return b.block(slot, x, y, width, label, sizes=(52, 28), color=color, leading=1.05, max_lines=1, bold=b.t.display_bold, display=True) - y
    return b.block(slot, x, y, width, label, sizes=(20, 16), color=color, leading=1.2, max_lines=1, bold=True) - y


def _icon_grid(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    items = slide["items"]
    if b.variant("icon_grid", "grid") == "columns" and len(items) <= 4:
        return _icon_columns(b, slide, deck)
    t, r = b.t, b.r
    y = _header(b, slide, framed=True)
    per_row = 3 if len(items) in (3, 5, 6) else 2
    rows = [items[i : i + per_row] for i in range(0, len(items), per_row)]
    columns = _columns(per_row, gap=40)
    width = columns[0][1]
    icon = 56
    text_w = width - icon - 20
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=text_w, sizes=(24, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [item.get("text", "") for item in items]
    row_space = (b.bottom - y - 36 * (len(rows) - 1)) // len(rows)
    text_fit = None
    if any(texts):
        text_fit = b.fit("item-text", [text for text in texts if text], width_px=text_w, sizes=(18, 14), leading=1.45, each_max_height_px=row_space - title_h - 8)
    text_h = max((block_height_px([lines], text_fit.size, 1.45, 0, 2) for lines in text_fit.lines), default=0) if text_fit else 0
    row_h = max(icon, title_h + (8 + text_h if text_h else 0))
    text_index = 0
    index = 0
    for row_number, row in enumerate(rows):
        ry = y + row_number * (row_h + 36)
        for (x, _), item in zip(columns, row):
            index += 1
            on = t.icon_container != "none"
            _icon_mark(b, f"item{index}-icon", item.get("icon") or item["title"], x, ry, icon, fill=t.c("accent") if on else r["bg"], color=t.c("on_accent") if on else r["accent"])
            tx = x + icon + 20
            lines = title_fit.lines[index - 1]
            b.text(f"item{index}-title", (tx, ry, text_w, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(item["title"], title_fit, r["text"], bold=True)])
            if item.get("text") and text_fit is not None:
                lines = text_fit.lines[text_index]
                text_index += 1
                b.text(f"item{index}-text", (tx, ry + title_h + 8, text_w, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(item["text"], text_fit, r["muted"])])


def _icon_columns(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Editorial columns divided by hairlines: line icon, title, copy."""
    r = b.r
    y = _header(b, slide, framed=True)
    items = slide["items"]
    columns = _columns(len(items), gap=0)
    narrow = body_w() < 900
    pad = 18 if narrow else 28
    inner = columns[0][1] - 2 * pad
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=inner, sizes=(20, 16) if narrow else (26, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [item.get("text", "") for item in items]
    text_fit = None
    if any(texts):
        text_fit = b.fit("item-text", [text for text in texts if text], width_px=inner, sizes=(18, 14), leading=1.5, each_max_height_px=b.bottom - y - 60 - title_h - 12)
    text_h = max((block_height_px([lines], text_fit.size, 1.5, 0, 2) for lines in text_fit.lines), default=0) if text_fit else 0
    height = 64 + title_h + (12 + text_h if text_h else 0) + 8
    text_index = 0
    for index, ((x, width), item) in enumerate(zip(columns, items), start=1):
        if index > 1:
            b.rect(f"item{index}-divider", (x, y, 1, height), b.t.c("line"))
        b.icon(f"item{index}-icon", item.get("icon") or item["title"], (x + pad, y + 2, 44, 44), r["accent"])
        lines = title_fit.lines[index - 1]
        b.text(f"item{index}-title", (x + pad, y + 64, inner, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(item["title"], title_fit, r["text"], bold=True)])
        if item.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"item{index}-text", (x + pad, y + 64 + title_h + 12, inner, block_height_px([lines], text_fit.size, 1.5, 0, 2)), [b.para(item["text"], text_fit, r["muted"])])


def _process(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 6
    steps = slide["steps"]
    columns = _columns(len(steps))
    diameter = 64
    centers = [x + width // 2 for x, width in columns]
    b.rect("connector", (centers[0], y + diameter // 2 - 1, centers[-1] - centers[0], 2), t.c("line"))
    cycle = t.card["cycle"]
    for index, center in enumerate(centers, start=1):
        step = steps[index - 1]
        box = (center - diameter // 2, y, diameter, diameter)
        fill = t.c("primary") if len(cycle) < 2 or index % 2 else t.c(cycle[1][0]) if cycle[1][0] != "surface" else t.c("accent")
        ink = t.c("on_primary") if fill == t.c("primary") else t.c("on_accent") if fill == t.c("accent") else t.c(cycle[1][1])
        if step.get("icon"):
            b.rect(f"step{index}-chip", box, fill, kind="ellipse")
            b.icon(f"step{index}-icon", step["icon"], (box[0] + 18, y + 18, 28, 28), ink)
        else:
            number_fit = Fit(size=22, lines=[1], line_pt=26.0, height_px=diameter, tallest_px=diameter)
            b.text(f"step{index}-number", box, [b.para(str(index), number_fit, ink, bold=True, align="ctr", font="display")], kind="ellipse", fill=fill, anchor="ctr", inset_x=0, inset_y=0)
    width = columns[0][1]
    ty = y + diameter + 20
    title_fit = b.fit("step-title", [step["title"] for step in steps], width_px=width, sizes=(22, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [step.get("text", "") for step in steps]
    text_fit = None
    if any(texts):
        text_fit = b.fit("step-text", [text for text in texts if text], width_px=width, sizes=(16, 14), leading=1.45, each_max_height_px=b.bottom - ty - title_h - 8)
    text_index = 0
    for index, ((x, column), step) in enumerate(zip(columns, steps), start=1):
        lines = title_fit.lines[index - 1]
        b.text(f"step{index}-title", (x, ty, column, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(step["title"], title_fit, r["text"], bold=True, align="ctr")])
        if step.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"step{index}-text", (x, ty + title_h + 8, column, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(step["text"], text_fit, r["muted"], align="ctr")])


def _timeline(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide, framed=True)
    events = slide["events"]
    columns = _columns(len(events), gap=16)
    width = columns[0][1]
    date_fit = b.fit("event-date", [event["date"] for event in events], width_px=width, sizes=(26, 16), leading=1.2, max_lines=2, display=True)
    date_h = max(block_height_px([lines], date_fit.size, 1.2, 0, 2) for lines in date_fit.lines)
    axis_y = y + date_h + 18
    b.rect("axis", (body_x(), axis_y, body_w(), 2), t.c("line"))
    ty = axis_y + 30
    title_fit = b.fit("event-title", [event["title"] for event in events], width_px=width, sizes=(20, 16), leading=1.3, max_lines=3)
    title_h = max(block_height_px([lines], title_fit.size, 1.3, 0, 2) for lines in title_fit.lines)
    texts = [event.get("text", "") for event in events]
    text_fit = None
    if any(texts):
        text_fit = b.fit("event-text", [text for text in texts if text], width_px=width, sizes=(16, 14), leading=1.45, each_max_height_px=b.bottom - ty - title_h - 6)
    text_index = 0
    for index, ((x, column), event) in enumerate(zip(columns, events), start=1):
        b.rect(f"event{index}-dot", (x, axis_y - 6, 14, 14), r["accent"], kind="ellipse")
        lines = date_fit.lines[index - 1]
        b.text(f"event{index}-date", (x, y, column, block_height_px([lines], date_fit.size, 1.2, 0, 2)), [b.para(event["date"], date_fit, r["accent"], bold=b.t.display_bold, font="display")])
        lines = title_fit.lines[index - 1]
        b.text(f"event{index}-title", (x, ty, column, block_height_px([lines], title_fit.size, 1.3, 0, 2)), [b.para(event["title"], title_fit, r["text"], bold=True)])
        if event.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"event{index}-text", (x, ty + title_h + 6, column, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(event["text"], text_fit, r["muted"])])


def _stats(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    if b.variant("stats", "panels") == "rules":
        return _stats_rules(b, slide, deck)
    t = b.t
    y = _header(b, slide, framed=True) + 10
    stats = slide["stats"]
    per_row = len(stats) if body_w() >= 900 or len(stats) <= 2 else 2
    columns = _columns(per_row)
    width = columns[0][1]
    value_w = min(width - 32, 360)
    has_icons = any(stat.get("icon") for stat in stats)
    icon_h = 52 if has_icons else 0
    value_fit = b.fit("stat-value", [stat["value"] for stat in stats], width_px=value_w, sizes=(60, 36), leading=1.05, max_lines=1, display=True)
    label_fit = b.fit("stat-label", [stat["label"] for stat in stats], width_px=width - 48, sizes=(18, 14), leading=1.3, max_lines=2)
    label_h = max(block_height_px([lines], label_fit.size, 1.3, 0, 2) for lines in label_fit.lines)
    notes = [stat.get("note", "") for stat in stats]
    note_fit = None
    note_h = 0
    if any(notes):
        note_fit = b.fit("stat-note", [note for note in notes if note], width_px=width - 48, sizes=(16, 14), leading=1.4, max_lines=3)
        note_h = max(block_height_px([lines], note_fit.size, 1.4, 0, 2) for lines in note_fit.lines)
    spark_h = 64 if any(stat.get("trend") for stat in stats) else 0
    panel_h = 32 + (icon_h + 12 if icon_h else 0) + value_fit.tallest_px + 10 + label_h + (8 + note_h if note_h else 0) + (16 + spark_h if spark_h else 0) + 32
    rows = (len(stats) + per_row - 1) // per_row
    if y + rows * panel_h + (rows - 1) * GAP > b.bottom:
        raise FitError(f"{b.label} stats: content needs {rows * panel_h}px but only {b.bottom - y}px remain; shorten labels/notes or drop the lead")
    top = y
    radius = int(t.card.get("radius", 12))
    alpha = int(t.card.get("alpha", 100))
    outline = t.card.get("outline")
    note_index = 0
    for index, stat in enumerate(stats, start=1):
        x, column = columns[(index - 1) % per_row]
        y = top + ((index - 1) // per_row) * (panel_h + GAP)
        fill, ink, soft = b.card_style(index - 1)
        if outline:
            fill, ink, soft = b.r["bg"], b.r["text"], b.r["muted"]
        value_color = ink if fill not in (t.c("surface"), b.r["bg"]) else b.r["accent"]
        b.rect(f"stat{index}-panel", (x, y, column, panel_h), fill, radius=radius, alpha=alpha, line=t.c(outline) if outline else None, line_px=1.2 if outline else 0, shadow=True)
        cy = y + 32
        if icon_h:
            b.icon(f"stat{index}-icon", stat.get("icon") or "star", (x + (column - 40) // 2, cy, 40, 40), value_color)
            cy += icon_h + 12
        b.text(f"stat{index}-value", (x + (column - value_w) // 2, cy, value_w, value_fit.tallest_px), [b.para(stat["value"], value_fit, value_color, bold=t.display_bold, align="ctr", font="display")])
        cy += value_fit.tallest_px + 10
        lines = label_fit.lines[index - 1]
        b.text(f"stat{index}-label", (x + 24, cy, column - 48, block_height_px([lines], label_fit.size, 1.3, 0, 2)), [b.para(stat["label"], label_fit, ink, bold=True, align="ctr")])
        cy += label_h + 8
        if stat.get("note") and note_fit is not None:
            lines = note_fit.lines[note_index]
            note_index += 1
            b.text(f"stat{index}-note", (x + 24, cy, column - 48, block_height_px([lines], note_fit.size, 1.4, 0, 2)), [b.para(stat["note"], note_fit, soft, align="ctr")])
        if stat.get("trend"):
            from .forms import sparkline

            sparkline(b, f"stat{index}-trend", stat["trend"], (x + 24, y + panel_h - 32 - spark_h, column - 48, spark_h), value_color)


def _stats_rules(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """Soft style: big light numbers under a hairline, no panels."""
    t, r = b.t, b.r
    y = _header(b, slide, framed=True) + 24
    stats = slide["stats"]
    # A narrow body (a magazine column) sets the numbers two to a row.
    per_row = len(stats) if body_w() >= 900 or len(stats) <= 2 else 2
    columns = _columns(per_row, gap=32)
    width = columns[0][1]
    value_fit = b.fit("stat-value", [stat["value"] for stat in stats], width_px=min(width, 360), sizes=(88, 44), leading=1.0, max_lines=1, display=True)
    label_fit = b.fit("stat-label", [stat["label"] for stat in stats], width_px=width, sizes=(18, 14), leading=1.3, max_lines=2)
    label_h = max(block_height_px([lines], label_fit.size, 1.3, 0, 2) for lines in label_fit.lines)
    notes = [stat.get("note", "") for stat in stats]
    note_fit = b.fit("stat-note", [note for note in notes if note], width_px=width, sizes=(16, 14), leading=1.4, max_lines=3) if any(notes) else None
    max_note_h = max(block_height_px([lines], note_fit.size, 1.4, 0, 2) for lines in note_fit.lines) if note_fit else 0
    note_index = 0
    block_h = 22 + value_fit.tallest_px + 12 + label_h + 8 + (max_note_h + 12 if note_fit else 0) + (80 if any(stat.get("trend") for stat in stats) else 0)
    top = y
    for index, stat in enumerate(stats, start=1):
        x, column = columns[(index - 1) % per_row]
        y = top + ((index - 1) // per_row) * (block_h + 32)
        if y + block_h > b.bottom:
            raise FitError(f"{b.label} stats: {len(stats)} numbers need more height than this frame leaves; drop the notes or the lead")
        b.rect(f"stat{index}-rule", (x, y, column, 2), r["text"])
        cy = y + 22
        if stat.get("icon"):
            b.icon(f"stat{index}-icon", stat["icon"], (x + column - 32, cy + 4, 28, 28), r["accent"])
        b.text(f"stat{index}-value", (x, cy, min(column, 360), value_fit.tallest_px), [b.para(stat["value"], value_fit, r["text"], bold=t.display_bold, font="display")])
        cy += value_fit.tallest_px + 12
        lines = label_fit.lines[index - 1]
        b.text(f"stat{index}-label", (x, cy, column, block_height_px([lines], label_fit.size, 1.3, 0, 2)), [b.para(stat["label"], label_fit, r["text"], bold=True)])
        cy += label_h + 8
        if stat.get("note") and note_fit is not None:
            lines = note_fit.lines[note_index]
            note_index += 1
            b.text(f"stat{index}-note", (x, cy, column, block_height_px([lines], note_fit.size, 1.4, 0, 2)), [b.para(stat["note"], note_fit, r["muted"])])
        if stat.get("trend"):
            from .forms import sparkline

            spark_y = y + 22 + value_fit.tallest_px + 12 + label_h + 8 + (max_note_h + 12 if note_fit else 0) + 8
            if spark_y + 72 > b.bottom:
                raise FitError(f"{b.label} stats: no room for the trend lines; drop the notes or the lead")
            sparkline(b, f"stat{index}-trend", stat["trend"], (x, spark_y, column, 72), r["accent"])


def _comparison(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t = b.t
    y = _header(b, slide, framed=True)
    columns_data = slide["columns"]
    highlight = slide.get("highlight")
    columns = _columns(len(columns_data))
    width = columns[0][1]
    radius = int(t.card.get("radius", 0))
    heading_fit = b.fit("heading", [column["heading"] for column in columns_data], width_px=width - 40, sizes=(22, 18), leading=1.25, max_lines=2)
    heading_h = max(block_height_px([lines], heading_fit.size, 1.25, 0, 2) for lines in heading_fit.lines) + 28
    texts = ["\n".join(f"• {point}" for point in column["points"]) for column in columns_data]
    body_top = y + heading_h
    body_fit = b.fit("points", texts, width_px=width - 48, sizes=(18, 14), leading=1.5, each_max_height_px=b.bottom - body_top - 48)
    body_h = max(block_height_px([lines], body_fit.size, 1.5, 0, 2) for lines in body_fit.lines)
    for index, ((x, column), data) in enumerate(zip(columns, columns_data), start=1):
        chosen = highlight is None or highlight == index - 1
        b.rect(f"column{index}-panel", (x, body_top, column, body_h + 48), t.c("surface"), alpha=int(t.card.get("alpha", 100)))
        head_fill = t.c("primary") if chosen else t.c("line")
        head_ink = t.c("on_primary") if chosen else t.c("text")
        b.text(
            f"column{index}-heading",
            (x, y, column, heading_h),
            [b.para(data["heading"], heading_fit, head_ink, bold=True, align="ctr")],
            kind="round" if radius else "rect",
            fill=head_fill,
            radius=min(radius, 12),
            anchor="ctr",
            inset_x=20,
            inset_y=14,
        )
        paragraphs = [Para(runs=(Run(text=f"• {point}", size=body_fit.size, color=t.c("text")),), line_pt=body_fit.line_pt) for point in data["points"]]
        lines = body_fit.lines[index - 1]
        b.text(f"column{index}-points", (x + 24, body_top + 24, column - 48, block_height_px([lines], body_fit.size, 1.5, 0, 2)), paragraphs)


def _matrix(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    cells = slide["quadrants"]
    axes = slide.get("axes") or {}
    left = MARGIN + (44 if axes.get("y") else 0)
    bottom = b.bottom - (36 if axes.get("x") else 0)
    gap = 16
    cell_w = (CANVAS_W - MARGIN - left - gap) // 2
    cell_h = (bottom - y - gap) // 2
    pad = 22
    title_fit = b.fit("cell-title", [cell["title"] for cell in cells], width_px=cell_w - 2 * pad, sizes=(22, 18), leading=1.25, max_lines=2)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [cell.get("text", "") for cell in cells]
    text_fit = None
    if any(texts):
        text_fit = b.fit("cell-text", [text for text in texts if text], width_px=cell_w - 2 * pad, sizes=(16, 14), leading=1.45, each_max_height_px=cell_h - 2 * pad - title_h - 8)
    radius = min(int(t.card.get("radius", 0)), 18)
    text_index = 0
    for index, cell in enumerate(cells):
        cx = left + (index % 2) * (cell_w + gap)
        cy = y + (index // 2) * (cell_h + gap)
        fill, ink, soft = b.card_style(index) if len(t.card["cycle"]) > 1 else (t.c("surface"), t.c("text"), t.c("muted"))
        b.rect(f"cell{index + 1}", (cx, cy, cell_w, cell_h), fill, radius=radius, alpha=int(t.card.get("alpha", 100)))
        lines = title_fit.lines[index]
        b.text(f"cell{index + 1}-title", (cx + pad, cy + pad, cell_w - 2 * pad, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(cell["title"], title_fit, ink, bold=True)])
        if cell.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"cell{index + 1}-text", (cx + pad, cy + pad + title_h + 8, cell_w - 2 * pad, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(cell["text"], text_fit, soft)])
    if axes.get("x"):
        b.block("axis-x", left, bottom + 8, CANVAS_W - MARGIN - left, f"{axes['x']} →", sizes=(16, 14), color=r["muted"], max_lines=1, align="ctr")
    if axes.get("y"):
        arrow_bottom = b.block("axis-y", MARGIN, y + 4, 40, "↑", sizes=(20, 16), color=r["muted"], max_lines=1)
        b.block("axis-y-label", MARGIN, arrow_bottom + 4, 40, axes["y"][:6], sizes=(16, 14), color=r["muted"], max_lines=6, leading=1.2)


def _quote(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    if slide.get("image"):
        _photo_page(b, slide, side="center")  # the quote runs the full width
    b.motifs(light=True)
    r = b.r
    y = _header(b, slide) + 10
    x = MARGIN + 40
    width = CONTENT_W - 80
    quote_fit = b.fit("quote", [slide["quote"]], width_px=width, sizes=(32, 22), leading=1.45, max_lines=5, max_height_px=b.bottom - y - 80, display=True)
    b.rect("quote-mark", (MARGIN, y + 4, 6, quote_fit.height_px - 8), r["accent"])
    b.text("quote", (x, y, width, quote_fit.height_px), [b.para(slide["quote"], quote_fit, r["text"], font="display", bold=False)])
    if slide.get("attribution"):
        b.block("attribution", x, y + quote_fit.height_px + 24, width, f"— {slide['attribution']}", sizes=(18, 14), color=r["muted"], max_lines=1, align="r")


def _percent_literal(number_format: str, values: list[float]) -> str:
    """Excel's '%' multiplies by 100. Values already on a 0-100 scale (29.2 for
    29.2%) would show as 2920%, so render the sign literally instead."""
    unquoted = re.sub(r'"[^"]*"', "", number_format)
    if "%" not in unquoted or not values or max(abs(v) for v in values) <= 1.5:
        return number_format
    parts = re.split(r'("[^"]*")', number_format)
    return "".join(part if part.startswith('"') else part.replace("%", '"%"') for part in parts)


def _chart_spec(b: SlideBuilder, chart: dict[str, Any]) -> ChartSpec:
    t = b.t
    number_format = chart.get("number_format") or "General"
    series = tuple((str(item["name"]), tuple(float(v) for v in item["values"])) for item in chart["series"])
    highlight = chart.get("highlight", -1)
    if chart["type"] in ("percent_column", "percent_bar") and not chart.get("number_format"):
        number_format = "0%"
    else:
        number_format = _percent_literal(number_format, [v for _, values in series for v in values])
    colors = t.chart
    if highlight >= 0:
        colors = (blend(t.chart[0], b.r["bg"], 0.62), *t.chart[1:])
    return ChartSpec(
        chart_type=chart["type"],
        categories=tuple(str(category) for category in chart["categories"]),
        series=series,
        number_format=number_format,
        highlight=highlight,
        highlight_color=t.chart[0],
        grid=bool(t.get("chart_style.grid", True)),
        colors=colors,
        text_color=b.r["text"],
        grid_color=t.c("line"),
        font=t.latin,
        show_legend=len(series) > 1 or chart["type"] in ("pie", "doughnut"),
        show_labels=chart["type"] in ("pie", "doughnut") or len(chart["categories"]) <= 8,
        unit=str(chart.get("unit") or ""),
    )


def _chart(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t = b.t
    y = _header(b, slide, framed=True)
    height = b.bottom - y
    if height < 220:
        raise FitError(f"{b.label} chart: only {height}px left for the chart; drop the lead or kicker")
    takeaways = slide.get("takeaways") or []
    chart_w = body_w() if not takeaways else int(body_w() * 0.62)
    b.plan.shapes.append(Shape(f"{SHAPE_PREFIX}chart", "chart", (body_x(), y, chart_w, height), chart=_chart_spec(b, slide["chart"])))
    if takeaways:
        x = body_x() + chart_w + 32
        width = body_x() + body_w() - x
        fill, ink, soft = b.card_style(0)
        panel = b.rect("takeaways-panel", (x, y, width, height), fill, radius=int(t.card.get("radius", 12)), alpha=int(t.card.get("alpha", 100)), shadow=True)
        bottom = _rows(b, "takeaway", takeaways, x=x + 28, y=y + 32, width=width - 56, max_height=height - 64, sizes=(22, 14), text_color=ink, accent=ink if fill == t.c("accent") else None)
        panel.box = (x, y, width, bottom - y + 32)


def _numeric(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[$¥€£]?\s?[\d,.]+\s?(%|x|倍|万|亿|k|K|M|B)?", value.strip()))


def _table(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t = b.t
    y = _header(b, slide, framed=True)
    header = [str(cell) for cell in slide["columns"]]
    body = [[str(cell) for cell in row] for row in slide["rows"]]
    columns = len(header)
    weights = [max(6, max(len(row[i]) for row in [header, *body])) for i in range(columns)]
    total = sum(weights)
    widths = [max(110, int(body_w() * weight / total)) for weight in weights]
    widths[-1] += body_w() - sum(widths)
    if min(widths) < 90:
        raise FitError(f"{b.label} table: too many wide columns; use at most 6 columns with short headers")
    aligns = tuple("r" if body and all(_numeric(row[i]) for row in body) else "l" for i in range(columns))
    available = b.bottom - y
    for size in (18, 17, 16, 15, 14):
        line_px = size * 1.3 / PX_TO_PT
        heights = []
        for row in [header, *body]:
            lines = max(wrapped_line_count(cell, size, (width - 20) * PX_TO_PT) for cell, width in zip(row, widths))
            heights.append(int(lines * line_px + 20))
        if sum(heights) <= available and max(heights) <= line_px * 3 + 20:
            break
    else:
        raise FitError(f"{b.label} table: {len(body)} rows do not fit; split the table across slides or shorten cells")
    spec = TableSpec(
        rows=tuple(tuple(row) for row in [header, *body]),
        col_widths=tuple(widths),
        row_heights=tuple(heights),
        size=float(size),
        header_fill=t.c("primary"),
        header_color=t.c("on_primary"),
        body_fills=(b.r["bg"], t.c("surface")),
        body_color=t.c("text") if b.r["bg"] != t.c("surface") else b.r["text"],
        aligns=aligns,
    )
    b.plan.shapes.append(Shape(f"{SHAPE_PREFIX}table", "table", (body_x(), y, body_w(), sum(heights)), table=spec))


def _letterbox(image) -> tuple[int, int, int, int]:
    """The picture inside any solid black bars along its edges (left, top, right, bottom)."""
    small = image.convert("L").resize((max(1, image.width // 8), max(1, image.height // 8)))
    w, h = small.size
    pixels = small.load()
    dark_row = [max(pixels[x, y] for x in range(w)) < 14 for y in range(h)]
    dark_col = [max(pixels[x, y] for y in range(h)) < 14 for x in range(w)]
    top = next((i for i, dark in enumerate(dark_row) if not dark), 0)
    bottom = h - next((i for i, dark in enumerate(reversed(dark_row)) if not dark), 0)
    left = next((i for i, dark in enumerate(dark_col) if not dark), 0)
    right = w - next((i for i, dark in enumerate(reversed(dark_col)) if not dark), 0)
    if bottom - top < h // 2 or right - left < w // 2:
        return 0, 0, image.width, image.height  # mostly dark: a night photo, not bars
    sx, sy = image.width / w, image.height / h
    pad = 1  # one sampled pixel inside the bar edge
    return (int((left + pad) * sx) if left else 0, int((top + pad) * sy) if top else 0,
            int((right - pad) * sx) if right < w else image.width, int((bottom - pad) * sy) if bottom < h else image.height)


def _picture_spec(project: Path, relative: str, box: tuple[int, int, int, int], label: str, *, geometry: str = "rect", radius: int = 0) -> PictureSpec:
    from PIL import Image

    path = _resolve_image(project, relative, label)
    with Image.open(path) as image:
        full_w, full_h = image.size
        if image.format not in ("PNG", "JPEG", "WEBP"):
            raise FitError(f"{label} image: {relative!r} must be PNG, JPEG or WebP")
        left, top, right, bottom = _letterbox(image)
    # Generated pictures often arrive letterboxed; the black bars are cropped away first.
    width, height = right - left, bottom - top
    box_ratio = box[2] / box[3]
    ratio = width / height
    if ratio > box_ratio:
        excess = (width - height * box_ratio) / 2
        crop = (left + excess, top, full_w - right + excess, full_h - bottom)
        visible_w = width - 2 * excess
    else:
        excess = (height - width / box_ratio) / 2
        crop = (left, top + excess, full_w - right, full_h - bottom + excess)
        visible_w = width
    crop = (round(crop[0] / full_w, 5), round(crop[1] / full_h, 5), round(crop[2] / full_w, 5), round(crop[3] / full_h, 5))
    if visible_w < box[2] * 0.8:
        raise FitError(f"{label} image: {relative!r} is {width}x{height}px, too small for a {box[2]}x{box[3]}px slot")
    return PictureSpec(path=str(path), crop=crop, geometry=geometry, radius=radius)


def _framed(b: SlideBuilder) -> tuple[str, int]:
    image = b.t.image
    return image.get("geometry", "rect"), int(image.get("radius", 0))


def _image_text(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    y = _header(b, slide)
    image_w = int(CONTENT_W * 0.46)
    left = slide.get("image_side", "right") == "left"
    image_x = MARGIN if left else CANVAS_W - MARGIN - image_w
    text_x = MARGIN + image_w + 40 if left else MARGIN
    text_w = CONTENT_W - image_w - 40
    image_h = b.bottom - y - (36 if slide.get("caption") else 0)
    if image_h < 200:
        raise FitError(f"{b.label} image: only {image_h}px left for the image; drop the lead or kicker")
    geometry, radius = _framed(b)
    if geometry == "circle":
        geometry, radius = "round", 28
    b.picture("image", slide["image"], (image_x, y, image_w, image_h), geometry=geometry, radius=radius, shadow=True)
    if slide.get("caption"):
        b.block("caption", image_x, y + image_h + 8, image_w, slide["caption"], sizes=(14, 14), color=b.r["muted"], max_lines=1)
    _rows(b, "point", slide["points"], x=text_x, y=y, width=text_w, max_height=b.bottom - y, sizes=(20, 14))


def _feature(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    """A large rounded image tile beside an editorial text column."""
    t, r = b.t, b.r
    left = slide.get("image_side", "left") == "left"
    image_w = 560
    geometry, radius = _framed(b)
    geometry = "round" if geometry != "rect" else "rect"
    box = (MARGIN, b.top, image_w, b.bottom - b.top) if left else (CANVAS_W - MARGIN - image_w, b.top, image_w, b.bottom - b.top)
    b.picture("image", slide["image"], box, geometry=geometry, radius=max(radius, 24) if geometry == "round" else 0, shadow=True)
    x = MARGIN + image_w + 56 if left else MARGIN
    width = CONTENT_W - image_w - 56
    y = b.top + 40
    if slide.get("kicker"):
        y = b.block("kicker", x, y, width, slide["kicker"], sizes=(16, 14), color=r["accent"], max_lines=1, bold=True) + 12
    y = b.title_block("title", x, y, width, slide["title"], sizes=_sizes(b, "title", (48, 32)), color=r["text"]) + 22
    if slide.get("text"):
        y = b.block("text", x, y, width, slide["text"], sizes=(18, 14), color=r["muted"], leading=1.55, max_lines=6) + 22
    if slide.get("points"):
        y = _rows(b, "point", slide["points"], x=x, y=y, width=width, max_height=b.bottom - y - 70, sizes=(18, 14)) + 26
    tags = slide.get("tags") or []
    tx = x
    for index, tag in enumerate(tags, start=1):
        filled = index == len(tags)
        tx += b.pill(f"tag{index}", tx, min(y, b.bottom - 44), tag, fill=t.c("accent") if filled else r["bg"], color=t.c("on_accent") if filled else r["text"], line=None if filled else r["text"], size=14) + 10


def _split_image(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    left_image = slide.get("image_side", "right") == "left"
    image_box = (0, 0, 600, CANVAS_H) if left_image else (CANVAS_W - 600, 0, 600, CANVAS_H)
    b.picture("image", slide["image"], image_box)
    x = 600 + 56 if left_image else MARGIN
    width = CANVAS_W - 600 - 56 - MARGIN
    y = _header(b, slide, x=x, width=width)
    if slide.get("points"):
        _rows(b, "point", slide["points"], x=x, y=y, width=width, max_height=b.bottom - y, sizes=(20, 14))


def _image_cards(b: SlideBuilder, slide: dict[str, Any], deck: dict[str, Any]) -> None:
    t, r = b.t, b.r
    y = _header(b, slide)
    items = slide["items"]
    chips = b.variant("image_cards", "cards") == "chips"
    columns = _columns(len(items), gap=16 if chips else GAP)
    width = columns[0][1]
    geometry, radius = _framed(b)
    circle = geometry == "circle" and not chips
    title_fit = b.fit("item-title", [item["title"] for item in items], width_px=width - (40 if chips else 0), sizes=(20, 16), leading=1.25, max_lines=2 if not chips else 1)
    title_h = max(block_height_px([lines], title_fit.size, 1.25, 0, 2) for lines in title_fit.lines)
    texts = [item.get("text", "") for item in items]
    text_fit = None
    below_title = 0 if chips else title_h + 8
    if any(texts):
        text_fit = b.fit("item-text", [text for text in texts if text], width_px=width, sizes=(16, 14), leading=1.45, each_max_height_px=b.bottom - y - 160 - 16 - below_title)
    text_h = max((block_height_px([lines], text_fit.size, 1.45, 0, 2) for lines in text_fit.lines), default=0) if text_fit else 0
    room = b.bottom - y - 16 - below_title - text_h
    image_h = max(160, min(room, min(width, 240) if circle else room))
    image_w = image_h if circle else width
    text_index = 0
    for index, ((x, column), item) in enumerate(zip(columns, items), start=1):
        ix = x + (column - image_w) // 2 if circle else x
        b.picture(f"item{index}-image", item["image"], (ix, y, image_w, image_h), geometry="circle" if circle else ("round" if geometry != "rect" else "rect"), radius=max(radius, 20) if geometry != "rect" else 0, shadow=True)
        ty = y + image_h + 16
        align = "ctr" if circle else "l"
        lines = title_fit.lines[index - 1]
        if chips:
            chip_w = min(column - 32, int(text_width_pt(item["title"], title_fit.size) / PX_TO_PT / 0.9) + 36)
            chip_h = block_height_px([1], title_fit.size, 1.25, 0, 2) + 14
            b.text(f"item{index}-title", (x + 16, y + image_h - chip_h - 16, chip_w, chip_h), [b.para(item["title"], title_fit, "111111", bold=True, align="ctr")], kind="round", fill="FFFFFF", radius=chip_h // 2, alpha=90, anchor="ctr", inset_x=12, inset_y=4)
        else:
            b.text(f"item{index}-title", (x, ty, column, block_height_px([lines], title_fit.size, 1.25, 0, 2)), [b.para(item["title"], title_fit, r["text"], bold=True, align=align)])
        if item.get("text") and text_fit is not None:
            lines = text_fit.lines[text_index]
            text_index += 1
            b.text(f"item{index}-text", (x, ty + below_title, column, block_height_px([lines], text_fit.size, 1.45, 0, 2)), [b.para(item["text"], text_fit, r["muted"], align=align)])


def _notes(slide: dict[str, Any]) -> str:
    notes = (slide.get("notes") or "").strip()
    sources = [str(source).strip() for source in slide.get("sources") or [] if str(source).strip()]
    if sources:
        notes = (notes + "\n\n" if notes else "") + "[Sources]\n" + "\n".join(sources)
    return notes


# Fields rendered in one-line slots. Explicit line breaks in them are joined
# up front so a stray "\n" never costs a repair round.
SINGLE_LINE_FIELDS = {"kicker", "meta", "contact", "number", "caption", "attribution"}
SINGLE_LINE_ITEM_FIELDS = {"label", "value", "date"}


def _normalize_single_lines(deck: dict[str, Any]) -> dict[str, Any]:
    deck = dict(deck)
    for key in ("footer", "brand"):
        if isinstance(deck.get(key), str):
            deck[key] = _one_line(deck[key])
    slides = []
    for slide in deck["slides"]:
        slide = dict(slide)
        for key in SINGLE_LINE_FIELDS & slide.keys():
            if isinstance(slide[key], str):
                slide[key] = _one_line(slide[key])
        for key in ("items", "stats", "events"):
            if isinstance(slide.get(key), list):
                slide[key] = [
                    {k: (_one_line(v) if k in SINGLE_LINE_ITEM_FIELDS and isinstance(v, str) else v) for k, v in item.items()}
                    if isinstance(item, dict)
                    else item
                    for item in slide[key]
                ]
        slides.append(slide)
    deck["slides"] = slides
    return deck


NO_CONTENT_CHROME = {"cover", "section", "closing", "statement", "metric"}


def _card_variant(style: Style, slide: dict[str, Any], occurrence: int) -> str:
    """Rotate card treatments so repeated card slides never look identical."""
    if slide.get("variant"):
        return slide["variant"]
    variants = [v for v in style.card.get("variants", ["filled"]) if v != "icon"]
    if any(item.get("icon") for item in slide["items"]):
        return "icon"
    if "tall" in variants and not all(item.get("text") for item in slide["items"]):
        variants = [v for v in variants if v != "tall"] or ["filled"]
    return variants[occurrence % len(variants)] if variants else "filled"


def _chosen_variant(style: Style, slide: dict[str, Any], occurrences: dict[str, int]) -> str | None:
    """The slide's own ``variant``, else the style's composition, rotating when a layout repeats."""
    layout = slide["layout"]
    if layout == "cards":
        return None  # cards rotate their own treatments (see _card_variant)
    count = occurrences.get(layout, 0)
    occurrences[layout] = count + 1
    if slide.get("variant"):
        return str(slide["variant"])
    value = style.get(f"variants.{layout}")
    if isinstance(value, list) and value:
        return str(value[count % len(value)])
    return str(value) if value else None


def plan_deck(deck: dict[str, Any], theme: Style, project: Path) -> list[SlidePlan]:
    """Plan every slide. Raises ``FitError`` listing every slot that cannot fit."""
    deck = _normalize_single_lines(deck)
    plans: list[SlidePlan] = []
    errors: list[str] = []
    from .forms import FORM_FUNCTIONS, VARIANT_FUNCTIONS

    section_number = 0
    card_occurrence = 0
    occurrences: dict[str, int] = {}
    total = len(deck["slides"])
    chrome = bool(theme.get("chrome.top")) or theme.get("chrome.bottom", "none") != "none"
    for index, slide in enumerate(deck["slides"], start=1):
        layout = slide["layout"]
        role = PAGE_ROLES.get(layout, "content")
        b = SlideBuilder(theme, layout, f"slide {index} ({layout})", role, index, project, total)
        b.chosen = _chosen_variant(theme, slide, occurrences)
        _BODY.update(x=MARGIN, w=CONTENT_W)
        b.on_band = False
        _backdrop(b, deck)
        try:
            if role == "content" and layout not in ("split_image", "feature"):
                b.motifs()
            elif layout in ("split_image", "feature"):
                draw_motifs(b.plan.shapes, theme, "content", b.r, index, light=True)
            if layout == "cover":
                _cover(b, slide, deck)
            elif layout == "statement" and slide.get("image") and not b.t.get("image.bleed"):
                # A statement with its picture is the split composition, whatever the style rotates to.
                _statement(b, slide, deck)
            elif (layout, b.chosen) in VARIANT_FUNCTIONS:
                VARIANT_FUNCTIONS[(layout, b.chosen)](b, slide, deck)
            elif layout == "section":
                section_number += 1
                _section(b, slide, deck, section_number)
            elif layout == "closing":
                _closing(b, slide, deck)
            elif layout == "numbered":
                _cards(b, slide, deck, variant="tall" if b.variant("numbered", "numeral") == "tall" else "numeral")
            elif layout == "cards":
                variant = _card_variant(theme, slide, card_occurrence)
                card_occurrence += 1
                _cards(b, slide, deck, variant=variant)
            elif layout in FORM_FUNCTIONS:
                FORM_FUNCTIONS[layout](b, slide, deck)
            else:
                LAYOUT_FUNCTIONS[layout](b, slide, deck)
            if layout in BALANCED_LAYOUTS:
                _balance_body(b)
            if layout not in NO_CONTENT_CHROME:
                if chrome:
                    _chrome(b, deck, on_image=b.on_band) if layout != "split_image" else _chrome_split(b, deck, slide)
                elif layout not in ("split_image", "feature"):
                    _legacy_footer(b, deck, index)
                elif layout == "split_image":
                    left = slide.get("image_side", "right") == "left"
                    _legacy_footer(b, deck, index, x=656 if left else MARGIN, width=CANVAS_W - 600 - 56 - MARGIN)
        except FitError as exc:
            errors.append(str(exc))
            continue
        b.plan.notes = _notes(slide)
        b.plan.background = b.background
        plans.append(b.plan)
    if errors:
        raise FitError("\n".join(errors))
    return plans


def _backdrop(b: SlideBuilder, deck: dict[str, Any]) -> None:
    """The style's baked atmosphere for this page's role, under every other shape."""
    spec = (b.t.get("backdrop") or {}).get(b.role_name)
    if not spec:
        return
    cover = deck["slides"][0].get("image")
    photo = _resolve_image(b.project, cover, b.label) if cover and spec.get("photo") else None
    path = backdrop(spec, b.t.colors, b.project, photo)
    b.plan.shapes.insert(0, Shape(b._name("backdrop"), "picture", (0, 0, CANVAS_W, CANVAS_H), picture=PictureSpec(path=str(path), crop=(0.0, 0.0, 0.0, 0.0))))


def _chrome_split(b: SlideBuilder, deck: dict[str, Any], slide: dict[str, Any]) -> None:
    """Chrome for split pages: brand and index stay on the text half."""
    left = slide.get("image_side", "right") == "left"
    x0 = 656 if left else MARGIN
    width = CANVAS_W - 600 - 56 - MARGIN
    muted = b.r["muted"]
    font = "mono" if b.t.get("chrome.font") == "mono" else "body"
    fit = Fit(size=10, lines=[1], line_pt=12.0, height_px=20, tallest_px=20)
    label = plain(deck.get("brand") or deck.get("footer") or deck["title"])
    if b.chrome_top:
        b.text("chrome-brand", (x0, 30, width - 120, 20), [b.para(label, fit, muted, bold=True, font=font)])
        b.text("chrome-index", (x0 + width - 110, 30, 110, 20), [b.para(f"{b.index:02d} / {b.total:02d}", fit, muted, align="r", font=font)])


LAYOUT_FUNCTIONS = {
    "agenda": _agenda,
    "bullets": _bullets,
    "process": _process,
    "timeline": _timeline,
    "stats": _stats,
    "comparison": _comparison,
    "quote": _quote,
    "chart": _chart,
    "table": _table,
    "image_text": _image_text,
    "statement": _statement,
    "icon_grid": _icon_grid,
    "split_image": _split_image,
    "image_cards": _image_cards,
    "matrix": _matrix,
    "feature": _feature,
}
