"""Load ``deck.json``, plan it against a template theme, and write the PPTX."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .layouts import plan_deck
from .model import SlidePlan, Theme, resolve_theme
from .render import render_deck
from .schema import validate_deck
from .textfit import FitError


class BuildError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def load_and_plan(deck_path: Path) -> tuple[dict[str, Any], Theme, list[SlidePlan]]:
    deck_path = Path(deck_path)
    try:
        deck = json.loads(deck_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError([f"{deck_path}: file not found"]) from None
    except json.JSONDecodeError as exc:
        raise BuildError([f"{deck_path.name}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"]) from None
    errors = validate_deck(deck)
    if errors:
        raise BuildError(errors)
    theme = resolve_theme(deck.get("theme", "mono"), deck.get("theme_overrides"))
    try:
        plans = plan_deck(deck, theme, deck_path.parent)
    except FitError as exc:
        raise BuildError(str(exc).splitlines()) from None
    return deck, theme, plans


def default_output(deck_path: Path) -> Path:
    project = Path(deck_path).resolve().parent
    return project / "exports" / f"{project.name}.pptx"


def build_deck(deck_path: Path, output: Path | None = None) -> tuple[Path, list[SlidePlan]]:
    deck, theme, plans = load_and_plan(deck_path)
    output = output or default_output(deck_path)
    render_deck(plans, theme, output, title=deck["title"])
    return output, plans
