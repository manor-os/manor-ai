"""Prove a delivered PPTX is exactly the template build of its ``deck.json``.

Template decks are correct by construction, so instead of rendering them the
quality gate re-plans ``deck.json`` and checks that every slide holds exactly
the planned shapes — same names, same geometry, same text. A deck edited by
hand after the build, or one that only imitates template shape names, fails.
"""

from __future__ import annotations

from pathlib import Path

from pptx.enum.shapes import MSO_SHAPE_TYPE

from .build import BuildError, load_and_plan
from .model import SHAPE_PREFIX, Shape


def is_template_deck(prs) -> bool:
    return any(
        str(getattr(shape, "name", "")).startswith(SHAPE_PREFIX)
        for slide in prs.slides
        for shape in slide.shapes
    )


def _shape_error(slide_index: int, actual, expected: Shape) -> str | None:
    where = f"slide {slide_index} shape {expected.name!r}"
    if actual.name != expected.name:
        return f"slide {slide_index}: found {actual.name!r} where the template plan has {expected.name!r}"
    box = (int(actual.left), int(actual.top), int(actual.width), int(actual.height))
    if box != expected.emu_box:
        return f"{where}: geometry differs from the template plan"
    if expected.kind == "chart":
        if not getattr(actual, "has_chart", False):
            return f"{where}: expected a native chart"
        return None
    if expected.kind == "table":
        if not getattr(actual, "has_table", False):
            return f"{where}: expected a native table"
        cells = tuple(
            tuple(actual.table.cell(r, c).text for c in range(len(actual.table.columns)))
            for r in range(len(actual.table.rows))
        )
        if cells != expected.table.rows:
            return f"{where}: table text differs from deck.json"
        return None
    if expected.kind == "picture":
        if actual.shape_type != MSO_SHAPE_TYPE.PICTURE:
            return f"{where}: expected a picture"
        return None
    actual_text = actual.text_frame.text if getattr(actual, "has_text_frame", False) else ""
    if actual_text != expected.text:
        return f"{where}: text differs from deck.json"
    return None


def verify_template_deck(prs, project: Path) -> list[str]:
    deck_path = Path(project) / "deck.json"
    if not deck_path.is_file():
        return ["Template deck evidence missing: deck.json"]
    try:
        _deck, _theme, plans = load_and_plan(deck_path)
    except BuildError as exc:
        return [f"deck.json does not build: {message}" for message in exc.errors[:5]]
    slides = list(prs.slides)
    if len(plans) != len(slides):
        return [f"PPTX has {len(slides)} slide(s) but deck.json plans {len(plans)}; rerun build_deck.py"]
    errors: list[str] = []
    for index, (plan, slide) in enumerate(zip(plans, slides), start=1):
        actual = list(slide.shapes)
        if len(actual) != len(plan.shapes):
            errors.append(
                f"slide {index}: has {len(actual)} shape(s) but the template plan has {len(plan.shapes)}; rerun build_deck.py"
            )
            continue
        for shape, expected in zip(actual, plan.shapes):
            error = _shape_error(index, shape, expected)
            if error:
                errors.append(error)
                break
    return errors
