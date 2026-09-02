"""Closed vocabulary for human decision authority.

These values are persisted in ``ParticipantProfile.authority`` and are also
used by the infra authorization layer when deciding who may resolve HITL.
Keeping the vocabulary here prevents approval surfaces from inventing string
keys independently.
"""

from __future__ import annotations

from enum import Enum


class ParticipantAuthority(str, Enum):
    APPROVE_TASKS = "approve_tasks"
    APPROVE_AUTOMATION_CHANGES = "approve_automation_changes"
    APPROVE_EXTERNAL_PUBLISH = "approve_external_publish"
    APPROVE_GOAL_CHANGES = "approve_goal_changes"
    MANAGE_STANDING_GRANTS = "manage_standing_grants"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(item.value for item in cls)
