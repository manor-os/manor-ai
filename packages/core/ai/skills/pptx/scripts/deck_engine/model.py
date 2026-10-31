"""Plan objects shared by the layout planner, the renderer, and the verifier.

A slide plan is the complete, ordered list of shapes a template slide will
contain, with exact pixel geometry on the 1280x720 canvas. The renderer turns a
plan into PPTX shapes one-to-one, and the quality gate re-plans ``deck.json``
and compares the result with the delivered PPTX shape by shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CANVAS_W = 1280
CANVAS_H = 720
EMU_PER_PX = 9525
SHAPE_PREFIX = "tpl:"

STYLES_PATH = Path(__file__).resolve().parents[2] / "templates" / "decks" / "styles.json"
_HEX = re.compile(r"^[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class Run:
    text: str
    size: float
    color: str
    bold: bool = False
    italic: bool = False
    font: str = "body"  # body | display | emphasis | mono
    highlight: str | None = None  # marker fill behind the run


@dataclass(frozen=True)
class Para:
    runs: tuple[Run, ...]
    align: str = "l"  # l | ctr | r
    line_pt: float = 0.0
    space_after_pt: float = 0.0

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True)
class ChartSpec:
    chart_type: str
    categories: tuple[str, ...]
    series: tuple[tuple[str, tuple[float, ...]], ...]
    number_format: str
    colors: tuple[str, ...]
    text_color: str
    grid_color: str
    font: str
    show_legend: bool
    show_labels: bool
    unit: str = ""
    highlight: int = -1  # single-series bar/column: index of the emphasised category
    highlight_color: str = ""
    grid: bool = True  # value gridlines
    axes: bool = True  # False = sparkline / ring: no axes, legend or labels
    hole: int = 0  # doughnut hole size percent (0 = theme default)
    label_size: float = 14.0


@dataclass(frozen=True)
class TableSpec:
    rows: tuple[tuple[str, ...], ...]
    col_widths: tuple[int, ...]
    row_heights: tuple[int, ...]
    size: float
    header_fill: str
    header_color: str
    body_fills: tuple[str, str]
    body_color: str
    aligns: tuple[str, ...]


@dataclass(frozen=True)
class PictureSpec:
    path: str
    crop: tuple[float, float, float, float]  # left, top, right, bottom fractions
    geometry: str = "rect"  # rect | round | circle | arch
    radius: int = 0


@dataclass(frozen=True)
class IconSpec:
    name: str  # library/icon
    color: str


@dataclass
class Shape:
    name: str
    # rect | round | ellipse | triangle | text | chart | table | picture | icon | gradient
    kind: str
    box: tuple[int, int, int, int]
    fill: str | None = None
    radius: int = 0
    paragraphs: tuple[Para, ...] = ()
    anchor: str = "t"  # t | ctr | b
    inset_x: int = 4
    inset_y: int = 2
    chart: ChartSpec | None = None
    table: TableSpec | None = None
    picture: PictureSpec | None = None
    icon: IconSpec | None = None
    alpha: int = 100  # fill opacity percent
    rotation: float = 0.0
    line: str | None = None  # outline color
    line_px: float = 0.0
    gradient: tuple[tuple, ...] = ()  # (color, position percent[, alpha percent])
    gradient_angle: float = 90.0
    path: str = ""  # SVG path in box coordinates for kind == "path"
    shadow: bool = False
    soft: int = 0  # soft-edge (feather) radius in px: glows and light blooms

    @property
    def text(self) -> str:
        return "\n".join(paragraph.text for paragraph in self.paragraphs)

    @property
    def emu_box(self) -> tuple[int, int, int, int]:
        return tuple(value * EMU_PER_PX for value in self.box)  # type: ignore[return-value]


@dataclass
class SlidePlan:
    layout: str
    shapes: list[Shape] = field(default_factory=list)
    notes: str = ""
    background: str = "FFFFFF"


ROLE_KEYS = ("bg", "text", "muted", "accent")


@dataclass(frozen=True, eq=False)
class Style:
    """A complete visual language: palette, type, page rhythm, cards, imagery, motifs."""

    style_id: str
    name: str
    display: str  # Latin display face (titles, numerals)
    latin: str  # Latin body face
    ea: str  # East Asian face
    display_scale: float  # width factor of the display face vs the estimator baseline
    display_bold: bool
    title_case: str  # none | upper
    colors: dict[str, str]
    chart: tuple[str, ...]
    roles: dict[str, dict[str, str]]
    header: str  # bar | rule | side | tag | none
    card: dict[str, Any]
    image: dict[str, Any]
    icons: str
    icon_container: str  # circle | square | none
    motifs: dict[str, tuple[str, ...]]
    cover: str  # field | split | full
    numerals: str  # plain | big | circle
    art_direction: str
    spec: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.spec
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def c(self, key: str) -> str:
        return self.colors[key]

    def role(self, name: str) -> dict[str, str]:
        role = self.roles.get(name) or self.roles["content"]
        return {key: self.colors[role[key]] for key in ROLE_KEYS}


# Backwards-compatible name used across the engine.
Theme = Style


def load_styles() -> dict[str, Any]:
    return json.loads(STYLES_PATH.read_text(encoding="utf-8"))


def load_themes() -> dict[str, Any]:
    """Every selectable id: real styles plus legacy aliases."""
    data = load_styles()
    styles = dict(data["styles"])
    for alias, target in data.get("aliases", {}).items():
        styles[alias] = styles[target]
    return styles


def canonical_style_id(style_id: str) -> str:
    data = load_styles()
    return data.get("aliases", {}).get(style_id, style_id)


def resolve_theme(theme_id: str, overrides: dict[str, Any] | None) -> Style:
    data = load_styles()
    style_id = data.get("aliases", {}).get(theme_id, theme_id)
    base = data["styles"][style_id]
    colors = {key: value.upper() for key, value in base["colors"].items()}
    fonts = dict(base["fonts"])
    chart = [value.upper() for value in base["chart"]]
    overrides = overrides or {}
    for key, value in (overrides.get("colors") or {}).items():
        colors[key] = str(value).lstrip("#").upper()
    for key, value in (overrides.get("fonts") or {}).items():
        if key == "latin":
            fonts["latin"] = str(value)
            fonts["display"] = str(value)
        else:
            fonts[key] = str(value)
    if (overrides.get("colors") or {}).get("primary"):
        chart[0] = colors["primary"]
    if (overrides.get("colors") or {}).get("accent"):
        chart[1] = colors["accent"]
    return Style(
        style_id=style_id,
        name=base["name"],
        display=fonts["display"],
        latin=fonts["latin"],
        ea=fonts["ea"],
        display_scale=float(fonts.get("display_scale", 1.0)),
        display_bold=bool(fonts.get("display_bold", True)),
        title_case=base.get("title_case", "none"),
        colors=colors,
        chart=tuple(chart),
        roles=base["roles"],
        header=base["header"],
        card=base["card"],
        image=base["image"],
        icons=base["icons"],
        icon_container=base.get("icon_container", "circle"),
        motifs={key: tuple(value) for key, value in base.get("motifs", {}).items()},
        cover=base.get("cover", "field"),
        numerals=base.get("numerals", "plain"),
        art_direction=base.get("art_direction", ""),
        spec=base,
    )


def is_hex_color(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX.match(value.lstrip("#")))
