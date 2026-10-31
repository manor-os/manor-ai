"""Template deck engine: deck.json -> fitted slide plans -> native editable PPTX."""

from .build import BuildError, build_deck, load_and_plan
from .verify import is_template_deck, verify_template_deck

__all__ = ["BuildError", "build_deck", "is_template_deck", "load_and_plan", "verify_template_deck"]
