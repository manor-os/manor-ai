"""CJK-aware token estimation shared by context budgeting and compaction.

The old rule of thumb (4 chars ~= 1 token) is tuned for English. CJK text
tokenizes at roughly 1.5-2 chars per token, so a flat ``chars // 4`` under-
estimates Chinese/Japanese/Korean content by ~2x — history budgets overfill
the real context window and compaction triggers too late.
"""
from __future__ import annotations

import re

# CJK punctuation + kana, Han (incl. ext A), hangul syllables,
# CJK compatibility ideographs, full-width forms.
_CJK_CHAR_RE = re.compile(
    "[　-ヿ"
    "㐀-鿿"
    "가-힯"
    "豈-﫿"
    "＀-￯]"
)

_CJK_CHARS_PER_TOKEN = 1.7
_DEFAULT_CHARS_PER_TOKEN = 4.0


def runtime_estimate_tokens_for_text(text: str | None) -> int:
    """Estimate LLM tokens for ``text``, weighting CJK characters correctly."""

    if not text:
        return 0
    cjk_chars = len(_CJK_CHAR_RE.findall(text))
    other_chars = len(text) - cjk_chars
    return int(
        cjk_chars / _CJK_CHARS_PER_TOKEN + other_chars / _DEFAULT_CHARS_PER_TOKEN
    )
