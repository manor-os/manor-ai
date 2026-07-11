"""YAML frontmatter parser + Pydantic schema.

A workspace memory file looks like:

    ---
    id: 01KZ...
    title: Tutorial format outperforms hot takes
    scope: learning
    confidence: 0.9
    tags: [content, twitter]
    ---

    # Body
    Free-form Markdown.

We keep the parser intentionally tiny — no third-party python-frontmatter
library — so this stays a leaf with one direct dep (PyYAML) and the
serialize/parse round-trip is byte-stable for git diffs.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

MemoryScope = Literal[
    "guidance",
    "decision",
    "learning",
    "fact",
    "preference",
]
"""Canonical scope vocabulary. Mirrors AgentMemory.scope."""


_SCOPE_DIR = {
    "guidance": "guidance",
    "decision": "decisions",
    "learning": "learnings",
    "fact": "facts",
    "preference": "preferences",
}
_DIR_SCOPE = {v: k for k, v in _SCOPE_DIR.items()}


def scope_to_dirname(scope: MemoryScope) -> str:
    return _SCOPE_DIR[scope]


def dirname_to_scope(dirname: str) -> Optional[MemoryScope]:
    return _DIR_SCOPE.get(dirname)


class AppliesTo(BaseModel):
    """Optional filter — Strategist/Planner can restrict context lookup."""

    services: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)


class Frontmatter(BaseModel):
    """Frontmatter schema — strict on required fields, lenient on extras
    so users can scribble custom keys without the parser rejecting."""

    id: str = Field(..., min_length=1, max_length=64)
    """Stable identifier; ULID by convention. Mirrors agent_memories.id."""

    title: str = Field(..., min_length=1, max_length=255)
    scope: MemoryScope

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    importance: int = Field(default=5, ge=1, le=10)

    source: Optional[str] = None
    """Provenance: 'plan_eval:<id>' | 'user_chat:<msg>' | 'manual' | 'strategist:<run>'."""

    tags: list[str] = Field(default_factory=list)
    applies_to: AppliesTo = Field(default_factory=AppliesTo)

    created_at: datetime
    updated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    status: Literal["active", "archived"] = "active"

    model_config = {"extra": "allow", "populate_by_name": True}

    @field_validator("created_at", "updated_at", "expires_at", mode="before")
    @classmethod
    def _parse_dt(cls, v: Any) -> Any:
        if v is None or isinstance(v, datetime):
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return list(v)


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def parse_md(text: str) -> tuple[Frontmatter, str]:
    """Split a Markdown string into (frontmatter, body).

    Raises ValueError if frontmatter is missing or malformed — memory
    files are required to declare metadata so the index can use them.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(
            "memory MD missing YAML frontmatter delimited by '---' lines"
        )
    raw_yaml = m.group("yaml")
    body = m.group("body").lstrip("\n")

    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter YAML must be a mapping")

    fm = Frontmatter.model_validate(data)
    return fm, body


def serialize_md(fm: Frontmatter, body: str) -> str:
    """Round-trip the inverse of parse_md. Sort keys for byte stability
    so git diffs only show real changes."""
    # Drop unknowns from extras into the dict so user-added keys survive.
    data = fm.model_dump(mode="json", exclude_none=False)
    # Keep keys ordered deterministically.
    yaml_text = yaml.safe_dump(
        data,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"


def stub_frontmatter(
    *,
    title: str,
    scope: MemoryScope,
    id_: str,
    source: Optional[str] = None,
    tags: Optional[list[str]] = None,
    confidence: float = 1.0,
) -> Frontmatter:
    """Convenience constructor for code that's about to write a fresh
    note (Strategist learnings, programmatic seeds)."""
    now = datetime.now(timezone.utc)
    return Frontmatter(
        id=id_,
        title=title,
        scope=scope,
        confidence=confidence,
        source=source,
        tags=tags or [],
        created_at=now,
        updated_at=now,
    )
