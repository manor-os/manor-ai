"""Retain durable entries when managed Markdown is compacted repeatedly."""

from __future__ import annotations

import re


def previous_learning_entries(text: str) -> list[str]:
    entries = []
    for block in re.findall(
        r"<!-- runtime-learning-summary -->\n(.*?)\n<!-- /runtime-learning-summary -->",
        text or "", re.DOTALL,
    ):
        for line in block.splitlines():
            if line.startswith("- ") and not line.startswith((
                "- Manor compacted ", "- Full original context ",
            )):
                entries.append(line[2:])
    return entries


def merge_learning_entries(previous: str, new: list[str]) -> list[str]:
    # A hot prompt can be bounded without deleting the only durable summary.
    return list(dict.fromkeys([*previous_learning_entries(previous), *new]))
