"""Pure classification of open provider tool-result statuses.

Providers may add status strings without coordinating with Manor.  Keep that
wire vocabulary open and expose only the small, closed set of decisions that
our runtime and workers need when deciding whether a result is deliverable.
"""
from __future__ import annotations

from enum import StrEnum, unique
from typing import Any, Mapping


@unique
class ToolResultDisposition(StrEnum):
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_FAILURE = "terminal_failure"
    NONTERMINAL = "nonterminal"
    UNSPECIFIED = "unspecified"
    UNKNOWN = "unknown"


class ToolResultDispositionFactory:
    """Map provider-owned payloads to Manor-owned delivery decisions."""

    _TERMINAL_SUCCESS_STATUSES = frozenset({
        "ok",
        "success",
        "succeeded",
        "complete",
        "completed",
        "done",
        "finished",
        "sent",
        "published",
        "delivered",
        "created",
        "updated",
        "deleted",
    })
    _TERMINAL_FAILURE_STATUSES = frozenset({
        "error",
        "failed",
        "failure",
        "cancelled",
        "canceled",
        "denied",
        "rejected",
        "expired",
        "timeout",
        "timed_out",
        "aborted",
        "revoked",
        "not_dispatched",
    })
    _NONTERMINAL_STATUSES = frozenset({
        "accepted",
        "created_pending",
        "in_progress",
        "pending",
        "processing",
        "queued",
        "running",
        "scheduled",
        "submitted",
    })

    @classmethod
    def from_status(cls, value: Any) -> ToolResultDisposition:
        status = str(value or "").strip().lower()
        if not status:
            return ToolResultDisposition.UNSPECIFIED
        if status in cls._TERMINAL_SUCCESS_STATUSES:
            return ToolResultDisposition.TERMINAL_SUCCESS
        if status in cls._TERMINAL_FAILURE_STATUSES:
            return ToolResultDisposition.TERMINAL_FAILURE
        if status in cls._NONTERMINAL_STATUSES:
            return ToolResultDisposition.NONTERMINAL
        return ToolResultDisposition.UNKNOWN

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ToolResultDisposition:
        if (
            payload.get("error")
            or payload.get("ok") is False
            or payload.get("isError") is True
        ):
            return ToolResultDisposition.TERMINAL_FAILURE
        return cls.from_status(payload.get("status"))


__all__ = ["ToolResultDisposition", "ToolResultDispositionFactory"]
