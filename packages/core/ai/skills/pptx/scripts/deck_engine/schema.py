"""Validation for ``deck.json`` — the only file the model writes in template mode.

Every problem is reported at once with its JSON path and the allowed values, so
one repair pass fixes everything.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .model import is_hex_color, load_themes, resolve_theme

# A theme is a designed palette: only the brand colors may be swapped.
OVERRIDABLE_COLORS = ("primary", "accent")

# Pairs that must stay legible after theme_overrides: (foreground, background,
# minimum WCAG contrast). Accent/primary text is always >= 16 pt bold or used
# as large shapes, so the large-text ratio applies to them.
CONTRAST_PAIRS = (
    ("text", "background", 4.5),
    ("text", "surface", 4.5),
    ("muted", "background", 4.5),
    ("muted", "surface", 4.0),
    ("accent", "background", 3.0),
    ("primary", "background", 3.0),
    ("on_primary", "primary", 4.5),
)


def _luminance(hex_color: str) -> float:
    channels = []
    for index in (0, 2, 4):
        value = int(hex_color[index : index + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)

COMMON_FIELDS = {"layout", "notes", "sources"}
HEADER_FIELDS = {"title", "kicker", "lead"}


@dataclass(frozen=True)
class LayoutRule:
    purpose: str
    required: frozenset[str]
    optional: frozenset[str]
    lists: dict[str, tuple[int, int]]


def _rule(purpose: str, required: set[str], optional: set[str], lists: dict[str, tuple[int, int]] | None = None) -> LayoutRule:
    return LayoutRule(purpose, frozenset(required), frozenset(optional | COMMON_FIELDS), lists or {})


LAYOUTS: dict[str, LayoutRule] = {
    "cover": _rule("Opening slide (must be slide 1); add an image for a photo cover", {"title"}, {"subtitle", "kicker", "meta", "image"}),
    "section": _rule("Chapter divider", {"title"}, {"subtitle", "number", "kicker", "image"}),
    "statement": _rule("One big message on a contrast page", {"title"}, {"subtitle", "kicker", "image"}),
    "agenda": _rule("Table of contents / agenda", {"title", "items"}, {"kicker", "lead"}, {"items": (3, 7)}),
    "bullets": _rule("One claim supported by 2-6 points", {"title", "points"}, {"kicker", "lead"}, {"points": (2, 6)}),
    "cards": _rule("2-4 parallel ideas, features or pillars", {"title", "items"}, {"kicker", "lead", "variant"}, {"items": (2, 4)}),
    "numbered": _rule("2-4 numbered points with big numerals", {"title", "items"}, {"kicker", "lead"}, {"items": (2, 4)}),
    "icon_grid": _rule("3-6 capabilities or features, each with an icon", {"title", "items"}, {"kicker", "lead"}, {"items": (3, 6)}),
    "process": _rule("3-5 sequential steps", {"title", "steps"}, {"kicker", "lead"}, {"steps": (3, 5)}),
    "timeline": _rule("3-6 dated milestones", {"title", "events"}, {"kicker", "lead"}, {"events": (3, 6)}),
    "stats": _rule("2-4 headline numbers", {"title", "stats"}, {"kicker", "lead"}, {"stats": (2, 4)}),
    "comparison": _rule("2-3 options side by side", {"title", "columns"}, {"kicker", "lead", "highlight"}, {"columns": (2, 3)}),
    "matrix": _rule("2x2 matrix (e.g. SWOT, priority, positioning)", {"title", "quadrants"}, {"kicker", "lead", "axes"}, {"quadrants": (4, 4)}),
    "quote": _rule("A testimonial or key statement; with an image, the quote sits over the photo", {"title", "quote"}, {"attribution", "kicker", "image"}),
    "chart": _rule("A native, editable bar/column/line/pie chart", {"title", "chart"}, {"kicker", "lead", "takeaways"}, {"takeaways": (1, 4)}),
    "table": _rule("A compact data table (up to 8 rows x 6 columns)", {"title", "columns", "rows"}, {"kicker", "lead"}, {"columns": (2, 6), "rows": (1, 8)}),
    "image_text": _rule("A framed image beside 1-5 points", {"title", "image", "points"}, {"kicker", "lead", "image_side", "caption"}, {"points": (1, 5)}),
    "split_image": _rule("Half-page bleed image with title and 1-5 points", {"title", "image"}, {"kicker", "lead", "points", "image_side"}, {"points": (1, 5)}),
    "image_cards": _rule("2-4 cards, each with its own image", {"title", "items"}, {"kicker", "lead"}, {"items": (2, 4)}),
    "progress": _rule("2-6 measured shares or completion levels (0-100) as bars or rings", {"title", "items"}, {"kicker", "lead"}, {"items": (2, 6)}),
    "funnel": _rule("3-5 narrowing stages (sales funnel, conversion, filtering)", {"title", "stages"}, {"kicker", "lead"}, {"stages": (3, 5)}),
    "pyramid": _rule("3-5 stacked levels from base to top (hierarchy, maturity, priorities)", {"title", "levels"}, {"kicker", "lead"}, {"levels": (3, 5)}),
    "cycle": _rule("3-6 steps of a repeating loop", {"title", "steps"}, {"kicker", "lead"}, {"steps": (3, 6)}),
    "venn": _rule("2-3 overlapping sets and what they share", {"title", "sets"}, {"kicker", "lead", "center"}, {"sets": (2, 3)}),
    "team": _rule("2-4 people with name, role and optional portrait", {"title", "people"}, {"kicker", "lead"}, {"people": (2, 4)}),
    "gallery": _rule("3-5 images as a mosaic with optional captions", {"title", "images"}, {"kicker", "lead"}, {"images": (3, 5)}),
    "feature": _rule(
        "One product/person/place feature: large rounded image tile, title, short text, 1-3 points, pill tags",
        {"title", "image"},
        {"kicker", "text", "points", "tags", "image_side"},
        {"points": (1, 3), "tags": (1, 3)},
    ),
    "metric": _rule("One headline number filling the page; title is the one-line claim it proves", {"title", "value"}, {"kicker", "subtitle", "image"}),
    "logos": _rule("Customer or partner logo wall (names set as wordmarks), optionally beside one headline number", {"title", "logos"}, {"kicker", "lead", "value", "label"}, {"logos": (3, 16)}),
    "pricing": _rule("2-4 pricing plans or packages side by side with features; highlight the recommended one", {"title", "plans"}, {"kicker", "lead", "highlight"}, {"plans": (2, 4)}),
    "device": _rule("A product screenshot in a browser, laptop or phone frame with 0-4 numbered call-outs", {"title", "image"}, {"kicker", "lead", "points", "device", "image_side"}, {"points": (1, 4)}),
    "market": _rule("Market size: exactly 3 tiers from total to obtainable (TAM / SAM / SOM)", {"title", "tiers"}, {"kicker", "lead"}, {"tiers": (3, 3)}),
    "launches": _rule("What's new: 3-10 short launch or feature names, one optionally highlighted", {"title", "items"}, {"kicker", "tag", "highlight", "icon"}, {"items": (3, 10)}),
    "case_study": _rule("Customer story: photo, 2-3 labelled sections (challenge / approach / result) and an optional result number", {"title", "image", "sections"}, {"kicker", "lead", "value", "value_label"}, {"sections": (2, 3)}),
    "testimonials": _rule("2-3 customer quotes, each with name, role and optional portrait", {"title", "quotes"}, {"kicker", "lead"}, {"quotes": (2, 3)}),
    "roadmap": _rule("Gantt roadmap: 2-7 workstreams as bars across 3-8 periods", {"title", "periods", "tasks"}, {"kicker", "lead"}, {"periods": (3, 8), "tasks": (2, 7)}),
    "closing": _rule("Final takeaway / next steps", {"title"}, {"subtitle", "points", "contact", "kicker", "image"}, {"points": (1, 4)}),
}

CHART_TYPES = (
    "column", "bar", "stacked_column", "stacked_bar", "percent_column", "percent_bar",
    "line", "area", "stacked_area", "pie", "doughnut", "radar",
)
CARD_VARIANTS = ("filled", "numeral", "icon", "outline", "tall")

# Alternative compositions per layout. Styles pick (and rotate) them; a slide
# may force one with "variant". The first entry is the layout's default.
LAYOUT_VARIANTS: dict[str, tuple[str, ...]] = {
    "agenda": ("list", "typographic", "cards"),
    "statement": ("plain", "ghost", "centered", "boxed", "giant"),
    "numbered": ("numeral", "tall"),
    "icon_grid": ("grid", "columns", "bento", "cards"),
    "stats": ("panels", "rules", "steps", "hero", "bento", "metrics"),
    "process": ("circles", "zigzag", "growing", "numerals", "pipeline"),
    "timeline": ("axis", "glow", "vertical"),
    "image_cards": ("cards", "chips", "overlay"),
    "quote": ("bar", "centered"),
    "bullets": ("rows", "lines", "checklist", "keynote"),
    "closing": ("plain", "centered"),
    "chart": ("panel", "split"),
    "progress": ("bars", "rings"),
    "team": ("circles", "portraits"),
    "gallery": ("mosaic", "strip"),
    "comparison": ("columns", "spec"),
    "metric": ("centered", "side"),
    "logos": ("grid", "open"),
    "pricing": ("cards", "columns"),
    "device": ("side", "stage"),
    "market": ("circles", "bars"),
    "launches": ("list", "icon", "chips"),
    "case_study": ("split", "band"),
    "testimonials": ("cards", "columns"),
}
DECK_FIELDS = {"title", "theme", "theme_overrides", "footer", "brand", "page_numbers", "slides", "language"}
TEXT_LIMITS = {"title": 40, "kicker": 24, "lead": 90, "subtitle": 80, "meta": 60, "number": 4}


class DeckSchemaError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


EMPHASIS = re.compile(r"\{([^{}]+)\}")


def _plain(value: str) -> str:
    return EMPHASIS.sub(r"\1", value)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def layout_catalog() -> str:
    return "; ".join(f"{name}: {rule.purpose}" for name, rule in LAYOUTS.items())


def _check_point(path: str, value: Any, errors: list[str], *, keys: tuple[str, ...] = ("title", "text", "icon")) -> None:
    if _text(value):
        return
    if isinstance(value, dict):
        unknown = set(value) - set(keys)
        if unknown:
            errors.append(f"{path}: unknown field(s) {sorted(unknown)}; allowed {list(keys)}")
        if not any(_text(value.get(key)) for key in ("title", "text")):
            errors.append(f"{path}: needs non-empty title or text")
        for key in keys:
            if key in value and not _text(value[key]):
                errors.append(f"{path}.{key}: must be a non-empty string")
        return
    errors.append(f"{path}: must be a non-empty string or an object with {list(keys)}")


def _check_items(
    path: str,
    values: Any,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    errors: list[str],
    *,
    other: tuple[str, ...] = (),
) -> None:
    """Items with required/optional string fields; ``other`` fields are checked by the caller."""
    for index, item in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path}: must be an object with {list(required + optional)}")
            continue
        unknown = set(item) - set(required) - set(optional) - set(other)
        if unknown:
            errors.append(f"{item_path}: unknown field(s) {sorted(unknown)}; allowed {list(required + optional + other)}")
        for key in required:
            if not _text(item.get(key)):
                errors.append(f"{item_path}.{key}: required non-empty string")
        for key in optional:
            if key in item and not _text(item[key]):
                errors.append(f"{item_path}.{key}: must be a non-empty string when present")


def _check_chart(path: str, chart: Any, errors: list[str]) -> None:
    if not isinstance(chart, dict):
        errors.append(f"{path}: must be an object {{type, categories, series, number_format?}}")
        return
    allowed = {"type", "categories", "series", "number_format", "unit", "highlight"}
    unknown = set(chart) - allowed
    if unknown:
        errors.append(f"{path}: unknown field(s) {sorted(unknown)}; allowed {sorted(allowed)}")
    chart_type = chart.get("type")
    if chart_type not in CHART_TYPES:
        errors.append(f"{path}.type: must be one of {list(CHART_TYPES)}")
    categories = chart.get("categories")
    if not isinstance(categories, list) or not (2 <= len(categories) <= 12) or not all(_text(str(c)) for c in categories):
        errors.append(f"{path}.categories: 2-12 non-empty labels")
        categories = []
    series = chart.get("series")
    max_series = 1 if chart_type in ("pie", "doughnut") else 4
    if chart_type == "radar" and len(categories) < 3:
        errors.append(f"{path}.categories: radar charts need 3-12 categories")
    highlight = chart.get("highlight")
    if highlight is not None and (
        not isinstance(highlight, int) or isinstance(highlight, bool) or not (0 <= highlight < max(1, len(categories)))
        or chart_type not in ("column", "bar")
    ):
        errors.append(f"{path}.highlight: index of one category to emphasise (column/bar charts only)")
    if not isinstance(series, list) or not (1 <= len(series) <= max_series):
        errors.append(f"{path}.series: 1-{max_series} series of {{name, values}} for type {chart_type!r}")
        return
    for index, item in enumerate(series):
        item_path = f"{path}.series[{index}]"
        if not isinstance(item, dict) or not _text(item.get("name")):
            errors.append(f"{item_path}.name: required non-empty string")
            continue
        values = item.get("values")
        if (
            not isinstance(values, list)
            or len(values) != len(categories)
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
        ):
            errors.append(f"{item_path}.values: {len(categories)} numbers, one per category")
        elif chart_type in ("pie", "doughnut") and any(v < 0 for v in values):
            errors.append(f"{item_path}.values: pie/doughnut values must be non-negative")


def validate_deck(deck: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(deck, dict):
        return ["deck.json: top level must be an object"]
    unknown = set(deck) - DECK_FIELDS
    if unknown:
        errors.append(f"deck: unknown field(s) {sorted(unknown)}; allowed {sorted(DECK_FIELDS)}")
    if not _text(deck.get("title")):
        errors.append("deck.title: required non-empty string (also used as file title)")
    themes = load_themes()
    theme = deck.get("theme", "mono")
    if theme not in themes:
        errors.append(f"deck.theme: {theme!r} is not a template theme; choose one of {sorted(themes)}")
    overrides = deck.get("theme_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - {"colors", "fonts"}:
            errors.append("deck.theme_overrides: object with optional 'colors' and 'fonts'")
        else:
            for key, value in (overrides.get("colors") or {}).items():
                if key not in OVERRIDABLE_COLORS:
                    errors.append(
                        f"deck.theme_overrides.colors.{key}: only brand colors {list(OVERRIDABLE_COLORS)} can be "
                        "overridden; backgrounds and text colors come from the theme (pick a dark theme such as "
                        "'tech' or 'elegant' for a dark look)"
                    )
                elif not is_hex_color(value):
                    errors.append(f"deck.theme_overrides.colors.{key}: must be a 6-digit hex color like '0B3D91'")
            for key, value in (overrides.get("fonts") or {}).items():
                if key not in ("latin", "ea") or not _text(value):
                    errors.append(f"deck.theme_overrides.fonts.{key}: allowed keys 'latin' and 'ea' with font names")
            colors = overrides.get("colors") or {}
            if theme in themes and colors and all(key in OVERRIDABLE_COLORS and is_hex_color(value) for key, value in colors.items()):
                resolved = resolve_theme(theme, overrides).colors
                for foreground, background, minimum in CONTRAST_PAIRS:
                    if foreground not in colors and background not in colors:
                        continue
                    ratio = contrast_ratio(resolved[foreground], resolved[background])
                    if ratio < minimum:
                        errors.append(
                            f"deck.theme_overrides.colors: {foreground} {resolved[foreground]} on {background} "
                            f"{resolved[background]} has contrast {math.floor(ratio * 100) / 100:.2f}:1, below {minimum}:1; darken or "
                            "lighten it, or drop the override and use the theme's own color"
                        )
    for key in ("footer", "brand"):
        if key in deck and (not _text(deck[key]) or len(deck[key]) > 60):
            errors.append(f"deck.{key}: non-empty string up to 60 characters")
    if "page_numbers" in deck and not isinstance(deck["page_numbers"], bool):
        errors.append("deck.page_numbers: true or false")

    slides = deck.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append("deck.slides: required non-empty list")
        return errors
    if len(slides) > 40:
        errors.append("deck.slides: at most 40 slides per template build")
    for index, slide in enumerate(slides):
        path = f"slides[{index}]"
        if not isinstance(slide, dict):
            errors.append(f"{path}: must be an object with a 'layout'")
            continue
        layout = slide.get("layout")
        if layout not in LAYOUTS:
            errors.append(f"{path}.layout: {layout!r} is unknown; choose one of {list(LAYOUTS)}")
            continue
        if index == 0 and layout != "cover":
            errors.append(f"{path}.layout: the first slide must use 'cover'")
        rule = LAYOUTS[layout]
        variants = LAYOUT_VARIANTS.get(layout, ())
        unknown = set(slide) - rule.required - rule.optional - ({"variant"} if variants else set())
        if unknown:
            allowed = sorted(rule.required | rule.optional)
            errors.append(f"{path}: unknown field(s) {sorted(unknown)} for layout {layout!r}; allowed {allowed}")
        for key in rule.required:
            if key not in slide:
                errors.append(f"{path}.{key}: required for layout {layout!r}")
        for key, limit in TEXT_LIMITS.items():
            if key in slide:
                if not _text(slide[key]):
                    errors.append(f"{path}.{key}: must be a non-empty string")
                elif len(_plain(slide[key])) > limit:
                    errors.append(f"{path}.{key}: {len(_plain(slide[key]))} characters; keep it to {limit} or fewer")
        title = slide.get("title")
        if _text(title) and ("{" in _plain(title) or "}" in _plain(title)):
            errors.append(f"{path}.title: emphasis braces must wrap a word like 'Grow {{faster}}' and must not nest")
        for key, (low, high) in rule.lists.items():
            if key in slide and (not isinstance(slide[key], list) or not (low <= len(slide[key]) <= high)):
                errors.append(f"{path}.{key}: {low}-{high} entries for layout {layout!r}")
        if variants and "variant" in slide and slide["variant"] not in variants:
            errors.append(f"{path}.variant: one of {list(variants)} for layout {layout!r}")
        if "notes" in slide and not isinstance(slide["notes"], str):
            errors.append(f"{path}.notes: string")
        if "sources" in slide and (
            not isinstance(slide["sources"], list) or not all(_text(s) for s in slide["sources"])
        ):
            errors.append(f"{path}.sources: list of source URLs or citations")

        def listed(key: str) -> list[Any]:
            value = slide.get(key)
            return value if isinstance(value, list) else []

        if layout in ("bullets", "image_text", "split_image", "feature"):
            for i, point in enumerate(listed("points")):
                _check_point(f"{path}.points[{i}]", point, errors)
        if layout == "closing":
            for i, point in enumerate(listed("points")):
                _check_point(f"{path}.points[{i}]", point, errors)
            if "contact" in slide and not _text(slide["contact"]):
                errors.append(f"{path}.contact: non-empty string")
        if layout == "agenda":
            for i, item in enumerate(listed("items")):
                if not _text(item):
                    errors.append(f"{path}.items[{i}]: non-empty string")
        if layout in ("cards", "numbered"):
            _check_items(f"{path}.items", listed("items"), ("title",), ("text", "label", "icon"), errors)
            if "variant" in slide and slide["variant"] not in CARD_VARIANTS:
                errors.append(f"{path}.variant: one of {list(CARD_VARIANTS)}")
        if layout == "icon_grid":
            _check_items(f"{path}.items", listed("items"), ("title", "icon"), ("text",), errors)
        if layout == "image_cards":
            _check_items(f"{path}.items", listed("items"), ("title", "image"), ("text",), errors)
        if layout == "matrix":
            _check_items(f"{path}.quadrants", listed("quadrants"), ("title",), ("text",), errors)
            axes = slide.get("axes")
            if axes is not None and (not isinstance(axes, dict) or set(axes) - {"x", "y"} or not all(_text(v) for v in axes.values())):
                errors.append(f"{path}.axes: object with optional 'x' and 'y' labels")
        if layout == "process":
            _check_items(f"{path}.steps", listed("steps"), ("title",), ("text", "icon"), errors)
        if layout == "timeline":
            _check_items(f"{path}.events", listed("events"), ("date", "title"), ("text",), errors)
        if layout == "stats":
            _check_items(f"{path}.stats", listed("stats"), ("value", "label"), ("note", "icon"), errors, other=("trend",))
            for i, stat in enumerate(listed("stats")):
                trend = stat.get("trend") if isinstance(stat, dict) else None
                if trend is None:
                    continue
                if not isinstance(trend, list) or not (3 <= len(trend) <= 16) or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in trend
                ):
                    errors.append(f"{path}.stats[{i}].trend: 3-16 numbers drawn as a sparkline")
            for i, stat in enumerate(listed("stats")):
                if isinstance(stat, dict) and _text(stat.get("value")) and len(stat["value"]) > 8:
                    errors.append(f"{path}.stats[{i}].value: keep headline numbers to 8 characters (e.g. '42%', '3.5x', '¥1.2亿')")
        if layout == "progress":
            for i, item in enumerate(listed("items")):
                item_path = f"{path}.items[{i}]"
                if not isinstance(item, dict) or set(item) - {"label", "value", "note"} or not _text(item.get("label")):
                    errors.append(f"{item_path}: object {{label, value, note?}}")
                    continue
                value = item.get("value")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 <= value <= 100):
                    errors.append(f"{item_path}.value: a number from 0 to 100 (percent)")
                if "note" in item and not _text(item["note"]):
                    errors.append(f"{item_path}.note: non-empty string")
        if layout == "funnel":
            _check_items(f"{path}.stages", listed("stages"), ("title",), ("value", "text"), errors)
        if layout == "pyramid":
            _check_items(f"{path}.levels", listed("levels"), ("title",), ("text",), errors)
        if layout == "cycle":
            _check_items(f"{path}.steps", listed("steps"), ("title",), ("text", "icon"), errors)
        if layout == "venn":
            _check_items(f"{path}.sets", listed("sets"), ("title",), ("text",), errors)
            if "center" in slide and (not _text(slide["center"]) or len(slide["center"]) > 16):
                errors.append(f"{path}.center: non-empty string up to 16 characters (what the sets share)")
        if layout == "team":
            _check_items(f"{path}.people", listed("people"), ("name", "role"), ("image", "text"), errors)
        if layout == "gallery":
            _check_items(f"{path}.images", listed("images"), ("image",), ("caption",), errors)
        if layout == "comparison":
            for i, column in enumerate(listed("columns")):
                column_path = f"{path}.columns[{i}]"
                if not isinstance(column, dict) or not _text(column.get("heading")):
                    errors.append(f"{column_path}.heading: required non-empty string")
                    continue
                if set(column) - {"heading", "points"}:
                    errors.append(f"{column_path}: allowed fields ['heading', 'points']")
                points = column.get("points")
                if not isinstance(points, list) or not (1 <= len(points) <= 5) or not all(_text(p) for p in points):
                    errors.append(f"{column_path}.points: 1-5 non-empty strings")
            highlight = slide.get("highlight")
            if highlight is not None and (
                not isinstance(highlight, int) or isinstance(highlight, bool) or not (0 <= highlight < len(listed("columns")))
            ):
                errors.append(f"{path}.highlight: index of the recommended column")
        if layout == "quote":
            if not _text(slide.get("quote")) or len(slide.get("quote", "")) > 120:
                errors.append(f"{path}.quote: non-empty string up to 120 characters")
            if "attribution" in slide and not _text(slide["attribution"]):
                errors.append(f"{path}.attribution: non-empty string")
        if layout == "chart":
            _check_chart(f"{path}.chart", slide.get("chart"), errors)
            for i, item in enumerate(listed("takeaways")):
                if not _text(item):
                    errors.append(f"{path}.takeaways[{i}]: non-empty string")
        if layout == "table":
            columns = listed("columns")
            if not all(_text(c) for c in columns):
                errors.append(f"{path}.columns: non-empty header strings")
            for i, row in enumerate(listed("rows")):
                if not isinstance(row, list) or len(row) != len(columns):
                    errors.append(f"{path}.rows[{i}]: exactly {len(columns)} cells")
                elif not all(isinstance(cell, (str, int, float)) and not isinstance(cell, bool) for cell in row):
                    errors.append(f"{path}.rows[{i}]: cells must be strings or numbers")
        if layout in ("metric", "logos", "case_study") and "value" in slide:
            if not _text(slide["value"]) or len(slide["value"]) > 8:
                errors.append(f"{path}.value: headline number up to 8 characters (e.g. '97%', '300K+', '¥1.2亿')")
        if layout == "logos":
            for i, name in enumerate(listed("logos")):
                if not _text(name) or len(name) > 24:
                    errors.append(f"{path}.logos[{i}]: company name up to 24 characters")
            if "label" in slide and (not _text(slide["label"]) or "value" not in slide):
                errors.append(f"{path}.label: non-empty string describing 'value' (set both)")
        if layout == "pricing":
            _check_items(f"{path}.plans", listed("plans"), ("name", "price"), ("period", "text", "badge"), errors, other=("features",))
            for i, plan in enumerate(listed("plans")):
                if not isinstance(plan, dict):
                    continue
                if _text(plan.get("price")) and len(plan["price"]) > 10:
                    errors.append(f"{path}.plans[{i}].price: up to 10 characters (e.g. '$49', '¥2,999')")
                if _text(plan.get("badge")) and len(plan["badge"]) > 14:
                    errors.append(f"{path}.plans[{i}].badge: up to 14 characters")
                features = plan.get("features")
                if not isinstance(features, list) or not (1 <= len(features) <= 6) or not all(_text(f) for f in features):
                    errors.append(f"{path}.plans[{i}].features: 1-6 non-empty strings")
            highlight = slide.get("highlight")
            if highlight is not None and (
                not isinstance(highlight, int) or isinstance(highlight, bool) or not (0 <= highlight < len(listed("plans")))
            ):
                errors.append(f"{path}.highlight: index of the recommended plan")
        if layout == "device":
            for i, point in enumerate(listed("points")):
                _check_point(f"{path}.points[{i}]", point, errors, keys=("title", "text"))
            if slide.get("device", "browser") not in ("browser", "laptop", "phone"):
                errors.append(f"{path}.device: 'browser', 'laptop' or 'phone'")
            if slide.get("image_side", "right") not in ("left", "right"):
                errors.append(f"{path}.image_side: 'left' or 'right'")
        if layout == "market":
            _check_items(f"{path}.tiers", listed("tiers"), ("label", "value"), ("text",), errors)
            for i, tier in enumerate(listed("tiers")):
                if isinstance(tier, dict) and _text(tier.get("value")) and len(tier["value"]) > 8:
                    errors.append(f"{path}.tiers[{i}].value: up to 8 characters (e.g. '$48B', '¥320亿')")
        if layout == "launches":
            for i, item in enumerate(listed("items")):
                if not _text(item) or len(item) > 40:
                    errors.append(f"{path}.items[{i}]: non-empty string up to 40 characters")
            if "tag" in slide and (not _text(slide["tag"]) or len(slide["tag"]) > 16):
                errors.append(f"{path}.tag: short label up to 16 characters (e.g. 'New')")
            if "icon" in slide and not _text(slide["icon"]):
                errors.append(f"{path}.icon: icon keyword")
            highlight = slide.get("highlight")
            if highlight is not None and (
                not isinstance(highlight, int) or isinstance(highlight, bool) or not (0 <= highlight < len(listed("items")))
            ):
                errors.append(f"{path}.highlight: index of the item to light up")
        if layout == "case_study":
            _check_items(f"{path}.sections", listed("sections"), ("label", "text"), (), errors)
            for i, section in enumerate(listed("sections")):
                if isinstance(section, dict) and _text(section.get("text")) and len(section["text"]) > 160:
                    errors.append(f"{path}.sections[{i}].text: up to 160 characters")
            if "value_label" in slide and (not _text(slide["value_label"]) or "value" not in slide):
                errors.append(f"{path}.value_label: non-empty string describing 'value' (set both)")
        if layout == "testimonials":
            _check_items(f"{path}.quotes", listed("quotes"), ("quote", "name"), ("role", "image"), errors)
            for i, quote in enumerate(listed("quotes")):
                if isinstance(quote, dict) and _text(quote.get("quote")) and len(quote["quote"]) > 160:
                    errors.append(f"{path}.quotes[{i}].quote: up to 160 characters")
        if layout == "roadmap":
            periods = listed("periods")
            for i, period in enumerate(periods):
                if not _text(period) or len(period) > 12:
                    errors.append(f"{path}.periods[{i}]: label up to 12 characters (e.g. 'Q1', '2026 H2')")
            _check_items(f"{path}.tasks", listed("tasks"), ("label",), ("note",), errors, other=("start", "end"))
            for i, task in enumerate(listed("tasks")):
                if not isinstance(task, dict):
                    continue
                start, end = task.get("start"), task.get("end")
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)) or not (0 <= start <= end < max(1, len(periods))):
                    errors.append(f"{path}.tasks[{i}]: 'start' and 'end' are period indexes with 0 <= start <= end < {len(periods)}")
        if "image" in slide and not _text(slide.get("image")):
            errors.append(f"{path}.image: path to a PNG/JPEG inside the project, e.g. 'images/hero.png'")
        if layout == "feature":
            if "text" in slide and (not _text(slide["text"]) or len(slide["text"]) > 160):
                errors.append(f"{path}.text: non-empty string up to 160 characters")
            for i, tag in enumerate(listed("tags")):
                if not _text(tag) or len(tag) > 20:
                    errors.append(f"{path}.tags[{i}]: non-empty string up to 20 characters")
        if layout in ("image_text", "split_image", "feature"):
            if slide.get("image_side", "right") not in ("left", "right"):
                errors.append(f"{path}.image_side: 'left' or 'right'")
            if "caption" in slide and (not _text(slide["caption"]) or len(slide["caption"]) > 60):
                errors.append(f"{path}.caption: non-empty string up to 60 characters")

    def silhouette(slide: dict[str, Any]) -> tuple[Any, ...]:
        counts = tuple(len(slide[key]) for key in sorted(LAYOUTS[slide["layout"]].lists) if isinstance(slide.get(key), list))
        return (slide["layout"], counts, "lead" in slide, "kicker" in slide)

    content = [
        silhouette(s)
        for s in slides[1:]
        if isinstance(s, dict) and s.get("layout") in LAYOUTS
    ]
    if len(content) >= 3 and len(set(content)) == 1:
        errors.append(
            "deck.slides: every content slide has the same layout and item count; "
            "vary layouts (e.g. cards, process, stats, chart) so the deck has visual rhythm"
        )
    return errors
