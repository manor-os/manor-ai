"""The output-discipline prompt section keeps a chat reply to one clean answer.

Multi-round turns (search → delegate → draft) made the model stack several
restated lead-ins above the actual deliverable, so a single reply read as the
same answer "repeated many times". The fix is a language-agnostic prompt rule
wired into the runtime prompt section registry for both full and minimal modes.
"""

from __future__ import annotations

from packages.core.ai.runtime.prompt_guidance import runtime_output_discipline_guidance
from packages.core.ai.runtime.prompt_sections import (
    runtime_prompt_section_names,
    runtime_prompt_section_renderers,
)


def test_output_discipline_guidance_forbids_redundant_preamble():
    text = runtime_output_discipline_guidance()
    assert text
    low = text.lower()
    # one clean answer, no stacked versions, no process-narration preamble
    assert "one clean answer" in low
    assert "more than one version" in low
    assert "下面是重写版" in text  # the exact CJK preamble we observed in prod


def test_output_discipline_section_registered_for_both_modes():
    renderers = runtime_prompt_section_renderers()
    assert "output_discipline" in renderers
    assert "output_discipline" in runtime_prompt_section_names("full")
    assert "output_discipline" in runtime_prompt_section_names("minimal")


def test_output_discipline_section_renders_without_context():
    # The rule is always-on (no ctx dependency), so it must render for any ctx.
    render = runtime_prompt_section_renderers()["output_discipline"]
    assert render(object()) == runtime_output_discipline_guidance()
