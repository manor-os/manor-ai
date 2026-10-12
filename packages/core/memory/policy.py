"""Shared memory classification and exact-content identity."""

from __future__ import annotations

import hashlib
import unicodedata


def normalized_content(value: str) -> str:
    # Do not fold case, numbers or punctuation: they can change a fact.
    return " ".join(unicodedata.normalize("NFC", value or "").split())


def content_fingerprint(value: str) -> str:
    return hashlib.sha256(normalized_content(value).encode()).hexdigest()


def canonical_filename(kind: str, content: str = "", tags=()) -> str:
    """Explicit categories win; keyword routing is a legacy fallback only."""
    if kind in {"learning", "tool_experience", "procedure", "agent_profile_patch"}:
        return {
            "learning": "LEARNINGS.md",
            "tool_experience": "TOOLS.md",
            "procedure": "RUNBOOKS.md",
            "agent_profile_patch": "AGENT.md",
        }[kind]
    if kind in {"instruction", "guidance", "rule", "profile_patch"}:
        return "RULES.md"
    if kind in {"fact", "preference", "decision"}:
        return "MEMORY.md"
    text = content.lower()
    if set(tags) & {"approval", "hitl", "guardrail", "rule"} or any(
        word in text
        for word in (
            "必须",
            "不要",
            "不能",
            "不准",
            "审核",
            "批准",
            "同意",
            "must",
            "never",
            "do not",
            "approval",
            "approve",
            "permission",
        )
    ):
        return "RULES.md"
    return "MEMORY.md"
