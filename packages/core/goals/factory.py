"""Factories for stable Goal identities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.goal import Goal


class GoalIdentityError(ValueError):
    """Base error for invalid or conflicting Goal identities."""


class GoalIdentityValidationError(GoalIdentityError):
    """A requested Goal identity is not structurally valid."""


class GoalKeyConflictError(GoalIdentityError):
    """A Goal logical key is already used in the same scope."""


@dataclass(frozen=True, slots=True)
class GoalIdentity:
    goal_key: str
    metric_key: str


class GoalIdentityFactory:
    """Resolve the canonical logical and measurement keys for one Goal."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        entity_id: str,
        workspace_id: str | None,
    ) -> None:
        self.db = db
        self.entity_id = entity_id
        self.workspace_id = workspace_id

    @staticmethod
    def _slug(title: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:72]

    @classmethod
    def normalize_records(cls, goals: list[object]) -> list[object]:
        """Return portable Goal records with stable, scope-unique keys."""
        reserved = {
            str(raw.get("goal_key") or "").strip()
            for raw in goals
            if isinstance(raw, dict) and str(raw.get("goal_key") or "").strip()
        }
        normalized: list[object] = []
        used: set[str] = set()
        for raw in goals:
            if not isinstance(raw, dict):
                normalized.append(raw)
                continue
            goal = dict(raw)
            requested = str(goal.get("goal_key") or "").strip()
            base = requested or f"goal_{cls._slug(str(goal.get('title') or '')) or 'objective'}"
            goal["goal_key"] = (
                cls._available_key(used, base=base[:100])
                if requested
                else cls._available_key(used | reserved, base=base[:100])
            )
            used.add(goal["goal_key"])
            normalized.append(goal)
        return normalized

    async def create(
        self,
        *,
        title: str,
        goal_key: str | None,
        metric_key: str | None,
    ) -> GoalIdentity:
        return GoalIdentity(
            goal_key=await self._goal_key(title=title, requested=goal_key),
            metric_key=await self._metric_key(title=title, requested=metric_key),
        )

    async def _goal_key(self, *, title: str, requested: str | None) -> str:
        requested_key = str(requested or "").strip()
        if len(requested_key) > 100:
            raise GoalIdentityValidationError(
                "goal_key must be at most 100 characters"
            )

        scope_filter = (
            Goal.workspace_id == self.workspace_id
            if self.workspace_id
            else Goal.workspace_id.is_(None)
        )
        existing = set((await self.db.execute(
            select(Goal.goal_key).where(
                Goal.entity_id == self.entity_id,
                scope_filter,
            )
        )).scalars().all())
        if requested_key:
            if requested_key in existing:
                raise GoalKeyConflictError(
                    f"goal_key {requested_key!r} already exists in this scope"
                )
            return requested_key
        return self._available_key(
            existing,
            base=f"goal_{self._slug(title) or 'objective'}",
        )

    async def _metric_key(self, *, title: str, requested: str | None) -> str:
        requested_key = str(requested or "").strip()
        if requested_key:
            return requested_key
        if not self.workspace_id:
            raise GoalIdentityValidationError(
                "metric_key is required for entity-level Goals"
            )
        existing = set((await self.db.execute(
            select(Goal.metric_key).where(
                Goal.workspace_id == self.workspace_id,
                Goal.entity_id == self.entity_id,
            )
        )).scalars().all())
        return self._available_key(
            existing,
            base=f"goal_{self._slug(title) or 'objective'}",
        )

    @staticmethod
    def _available_key(existing: set[str], *, base: str) -> str:
        if base not in existing:
            return base
        suffix = 2
        suffix_text = f"_{suffix}"
        candidate = f"{base[:100 - len(suffix_text)]}{suffix_text}"
        while candidate in existing:
            suffix += 1
            suffix_text = f"_{suffix}"
            candidate = f"{base[:100 - len(suffix_text)]}{suffix_text}"
        return candidate
