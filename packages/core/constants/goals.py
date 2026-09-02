"""Canonical Goal lifecycle values."""

from enum import Enum


class GoalStatus(str, Enum):
    ACTIVE = "active"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"
    PAUSED = "paused"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]
