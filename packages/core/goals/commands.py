"""Validated commands accepted by the Goal mutation service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from packages.core.constants.goals import GoalStatus


class GoalUpdateValidationError(ValueError):
    """Raised before an update can mutate protected Goal state."""


_MUTABLE_GOAL_FIELDS = frozenset({
    "title",
    "description",
    "metric_key",
    "target_value",
    "baseline_value",
    "current_value",
    "deadline",
    "status",
    "stat_id",
    "measurement_source",
    "measurement_cadence",
    "priority",
})

_ALLOWED_GOAL_STATUS_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.ACTIVE: frozenset({GoalStatus.PAUSED, GoalStatus.ABANDONED}),
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.ABANDONED}),
    GoalStatus.ACHIEVED: frozenset(),
    GoalStatus.ABANDONED: frozenset(),
}


def validate_goal_status_transition(current: str, requested: str | None) -> None:
    """Reject attempts to revive a terminal Goal through generic updates."""
    if requested is None:
        return
    try:
        current_status = GoalStatus(current)
        requested_status = GoalStatus(requested)
    except ValueError as exc:
        raise GoalUpdateValidationError("Goal has an invalid persisted status") from exc
    if requested_status == current_status:
        return
    if requested_status not in _ALLOWED_GOAL_STATUS_TRANSITIONS[current_status]:
        raise GoalUpdateValidationError(
            f"cannot move Goal from {current_status.value} to {requested_status.value}"
        )


@dataclass(frozen=True, slots=True)
class GoalUpdateCommand:
    """A normalized, allowlisted Goal update."""

    values: dict[str, Any]

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "GoalUpdateCommand":
        unknown = sorted(set(fields) - _MUTABLE_GOAL_FIELDS)
        if unknown:
            raise GoalUpdateValidationError(
                f"unsupported Goal update fields: {', '.join(unknown)}"
            )

        values = dict(fields)
        status = values.get("status")
        if status is not None:
            try:
                values["status"] = GoalStatus(status).value
            except ValueError as exc:
                raise GoalUpdateValidationError(
                    f"invalid Goal status {status!r}; expected one of "
                    f"{', '.join(GoalStatus.values())}"
                ) from exc
            if values["status"] == GoalStatus.ACHIEVED.value:
                raise GoalUpdateValidationError(
                    "Goal status 'achieved' is measurement-derived and cannot be set directly"
                )
        return cls(values=values)
