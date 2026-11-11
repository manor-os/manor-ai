"""Vocabularies for attention items: situations that deserve someone's notice.

An attention item says "this thing, right now, may need a person". Watchers
open, update and resolve items from the platform's own state; they never
decide how (or whether) anyone is told.
"""
from __future__ import annotations

from enum import Enum


class AttentionKind(str, Enum):
    """Closed set of situations, built only on platform primitives."""

    NEEDS_INPUT = "needs_input"
    DECISION_PENDING = "decision_pending"
    ACCOUNT_BROKEN = "account_broken"
    SETUP_INCOMPLETE = "setup_incomplete"
    AUTOMATION_PAUSED = "automation_paused"
    BUDGET_THRESHOLD = "budget_threshold"
    GOAL_OFF_PACE = "goal_off_pace"
    WORK_BLOCKED = "work_blocked"
    WORK_OVERDUE = "work_overdue"

    __str__ = str.__str__
    __format__ = str.__format__


class AttentionUrgency(str, Enum):
    """How soon a person should look.

    ``act_now``: only a person can do it and waiting causes real loss.
    ``act_today``: only a person can do it; today is fine.
    ``decide``: a proposal waits for someone's decision.
    ``fyi``: an outcome or change worth knowing.
    """

    ACT_NOW = "act_now"
    ACT_TODAY = "act_today"
    DECIDE = "decide"
    FYI = "fyi"

    __str__ = str.__str__
    __format__ = str.__format__


class AttentionState(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"

    __str__ = str.__str__
    __format__ = str.__format__
