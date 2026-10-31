"""Deterministic text measurement for template slots.

The estimator is deliberately at least as conservative as the final quality
gate's (`pptx_quality_gate._estimated_text_width_points`): every glyph class is
measured as wide or wider, and wrapping is simulated word by word. Text that
fits here therefore also fits the gate, so a template build never trips the
gate's overflow checks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

PX_TO_PT = 0.75  # 96 px per inch, 72 pt per inch

_WIDE = re.compile(
    "["
    "ᄀ-ᇿ"  # Hangul Jamo
    "⺀-⿟"  # CJK radicals
    "　-〿"  # CJK punctuation (、。「」 and the ideographic space)
    "぀-ヿ"  # Kana
    "㄀-ㇿ"  # Bopomofo and friends
    "㐀-䶿"  # CJK extension A
    "一-鿿"  # CJK unified ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "︰-﹏"  # CJK compatibility forms
    "＀-￯"  # Full-width forms (，：！？（） etc.)
    "]"
)
_NARROW = set("ilI1|.,:;!'’`[]()")
_BROAD = set("MW@#%&QGOD0mw")
_TOKEN = re.compile(r"\s+|[^\s]")


def char_em(character: str) -> float:
    if _WIDE.match(character):
        return 1.0
    if character.isspace():
        return 0.30
    if character in _NARROW:
        return 0.30
    if character in _BROAD:
        return 0.80
    if character.isupper() or character.isdigit():
        return 0.64
    return 0.54


def text_width_pt(text: str, size_pt: float, latin_scale: float = 1.0) -> float:
    total = 0.0
    for character in text:
        em = char_em(character)
        total += em if em >= 1.0 else em * max(1.0, latin_scale)
    return total * size_pt


def _tokens(line: str) -> list[str]:
    """Split a line into wrap units: whole Latin words, single wide glyphs, spaces."""
    tokens: list[str] = []
    word = ""
    for character in line:
        if character.isspace():
            if word:
                tokens.append(word)
                word = ""
            tokens.append(" ")
        elif _WIDE.match(character):
            if word:
                tokens.append(word)
                word = ""
            tokens.append(character)
        else:
            word += character
    if word:
        tokens.append(word)
    return tokens


def wrapped_line_count(text: str, size_pt: float, width_pt: float, latin_scale: float = 1.0) -> int:
    """Simulate greedy wrapping and return the number of visual lines."""
    # The gate wraps at 94% of the frame; at display sizes one glyph of metric
    # error is a bigger share of the line, so keep at least half an em spare.
    limit = max(1.0, min(width_pt * 0.93, width_pt - 0.5 * size_pt))
    total = 0
    for line in text.split("\n"):
        lines = 1
        used = 0.0
        previous_wide: bool | None = None
        for token in _tokens(line):
            token_width = text_width_pt(token, size_pt, latin_scale)
            if token == " ":
                previous_wide = None
                if used:
                    used += token_width
                continue
            wide = bool(_WIDE.match(token[0]))
            if used and previous_wide is not None and wide != previous_wide:
                # Renderers add ~1/4 em of autospace between CJK and Latin runs.
                token_width += 0.25 * size_pt
            previous_wide = wide
            if used + token_width <= limit:
                used += token_width
                continue
            if used:
                lines += 1
                used = 0.0
            if token_width > limit:
                # A single word wider than the slot breaks across lines.
                extra = math.ceil(token_width / limit) - 1
                lines += extra
                used = token_width - extra * limit
            else:
                used = token_width
        total += lines
    return total


def _splits_a_word(text: str, size_pt: float, width_pt: float, latin_scale: float = 1.0) -> bool:
    """True when some Latin word is wider than the slot and would break mid-word."""
    limit = max(1.0, min(width_pt * 0.93, width_pt - 0.5 * size_pt))
    return any(
        len(token) > 1 and not _WIDE.match(token[0]) and text_width_pt(token, size_pt, latin_scale) > limit
        for line in text.split("\n")
        for token in _tokens(line)
        if token != " "
    )


def longest_line_width_pt(text: str, size_pt: float) -> float:
    return max((text_width_pt(line, size_pt) for line in text.split("\n")), default=0.0)


@dataclass(frozen=True)
class Fit:
    size: float
    lines: list[int]
    line_pt: float
    height_px: int  # all texts stacked (one box holding every paragraph)
    tallest_px: int = 0  # the tallest single text (parallel slots)


class FitError(ValueError):
    pass


def line_height_pt(size_pt: float, leading: float) -> float:
    return round(size_pt * leading, 1)


def block_height_px(lines: list[int], size_pt: float, leading: float, gap_pt: float, pad_px: int) -> int:
    line_pt = line_height_pt(size_pt, leading)
    content_pt = sum(lines) * line_pt + gap_pt * max(0, len(lines) - 1)
    return int(math.ceil(content_pt / PX_TO_PT)) + 2 * pad_px


def fit_texts(
    texts: list[str],
    *,
    width_px: int,
    sizes: tuple[float, float],
    leading: float,
    max_lines: int | None = None,
    max_height_px: int | None = None,
    each_max_height_px: int | None = None,
    gap_pt: float = 0.0,
    inset_px: int = 4,
    pad_px: int = 2,
    label: str = "text",
    latin_scale: float = 1.0,
) -> Fit:
    """Pick the largest size in ``sizes`` (max, min) at which every text fits.

    ``texts`` share one size (paragraphs in one box, or sibling slots that must
    match). ``max_lines`` caps each text; ``max_height_px`` caps the stacked
    height of all texts with ``gap_pt`` between them; ``each_max_height_px``
    caps every text on its own (parallel columns).
    """
    largest, smallest = sizes
    width_pt = (width_px - 2 * inset_px) * PX_TO_PT
    candidates: list[float] = []
    size = largest
    while size > smallest:
        candidates.append(size)
        size -= 2
    candidates.append(smallest)
    for size in candidates:
        if size != smallest and any(_splits_a_word(text, size, width_pt, latin_scale) for text in texts):
            continue  # a smaller size that keeps every word whole reads better
        lines = [wrapped_line_count(text, size, width_pt, latin_scale) for text in texts]
        height = block_height_px(lines, size, leading, gap_pt, pad_px)
        tallest = max((block_height_px([count], size, leading, 0.0, pad_px) for count in lines), default=0)
        if (
            (max_lines is None or max(lines, default=0) <= max_lines)
            and (max_height_px is None or height <= max_height_px)
            and (each_max_height_px is None or tallest <= each_max_height_px)
        ):
            return Fit(
                size=size,
                lines=lines,
                line_pt=line_height_pt(size, leading),
                height_px=height,
                tallest_px=tallest,
            )
    per_line = max(1, int((width_pt * 0.93) / (smallest * 1.0)))
    lines = [wrapped_line_count(text, smallest, width_pt, latin_scale) for text in texts]
    longest = max(texts, key=len) if texts else ""
    if max_lines is not None and max(lines, default=0) > max_lines:
        budget = per_line * max_lines
        if any(text.count("\n") >= max_lines for text in texts):
            raise FitError(
                f"{label}: an entry contains line breaks but this slot allows {max_lines} line(s); "
                "remove the line breaks"
            )
        raise FitError(
            f"{label}: an entry needs more than {max_lines} line(s) even at {smallest:g} pt "
            f"(longest entry has {len(longest)} characters; keep each entry to about "
            f"{budget} CJK characters or {budget * 2} Latin characters)"
        )
    needed = block_height_px(lines, smallest, leading, gap_pt, pad_px)
    tallest = max((block_height_px([count], smallest, leading, 0.0, pad_px) for count in lines), default=0)
    limit = max_height_px if max_height_px is not None else each_max_height_px
    used = needed if max_height_px is not None else tallest
    longest_index = max(range(len(texts)), key=lambda i: len(texts[i])) if texts else 0
    target = int(len(longest) * max(0, limit or 0) / max(1, used) * 0.85)
    raise FitError(
        f"{label}: the {len(texts)} entr{'y' if len(texts) == 1 else 'ies'} need about {used}px of height "
        f"but only {max(0, limit or 0)}px remain on this slide. Entry {longest_index + 1} is the longest "
        f"({len(longest)} characters): cut it to about {target} characters, or drop an entry, or shorten "
        "the title/subtitle/lead above it"
    )
