"""Deterministic state and retry decisions for browser side effects."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class BrowserEffectContractError(ValueError):
    """Raised when a browser effect record does not match the contract."""


class BrowserEffectTransitionError(ValueError):
    """Raised when a browser effect attempts an unsafe state transition."""


class BrowserEffectStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    OBSERVED_COMPLETE = "observed_complete"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BrowserEffectDecision(str, Enum):
    EXECUTE = "execute"
    REUSE = "reuse"
    RETRY = "retry"
    OBSERVE_OR_PAUSE = "observe_or_pause"


@dataclass(frozen=True)
class BrowserEffectRecord:
    effect_id: str
    scene_id: str
    action: str
    precondition: dict[str, Any]
    expected_postcondition: dict[str, Any]
    status: BrowserEffectStatus
    evidence: tuple[dict[str, Any], ...]
    attempt_count: int


_REQUIRED_FIELDS = frozenset({
    "effect_id",
    "scene_id",
    "action",
    "precondition",
    "expected_postcondition",
    "status",
    "evidence",
    "attempt_count",
})


def _required_text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BrowserEffectContractError(f"Browser effect {key} must be a non-empty string")
    return value.strip()


def _required_mapping(record: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = record.get(key)
    if not isinstance(value, Mapping):
        raise BrowserEffectContractError(f"Browser effect {key} must be an object")
    return deepcopy(dict(value))


def _effect_status(value: Any) -> BrowserEffectStatus:
    try:
        return value if isinstance(value, BrowserEffectStatus) else BrowserEffectStatus(value)
    except (TypeError, ValueError) as exc:
        raise BrowserEffectContractError("Browser effect status is invalid") from exc


def _effect_evidence(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise BrowserEffectContractError("Browser effect evidence must be an array")
    evidence: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise BrowserEffectContractError("Browser effect evidence entries must be objects")
        evidence.append(deepcopy(dict(item)))
    return tuple(evidence)


def validate_browser_effect_record(record: Any) -> BrowserEffectRecord:
    """Validate and normalize one Workflow-persisted browser effect record."""

    if isinstance(record, BrowserEffectRecord):
        record = {
            "effect_id": record.effect_id,
            "scene_id": record.scene_id,
            "action": record.action,
            "precondition": record.precondition,
            "expected_postcondition": record.expected_postcondition,
            "status": record.status,
            "evidence": record.evidence,
            "attempt_count": record.attempt_count,
        }
    if not isinstance(record, Mapping):
        raise BrowserEffectContractError("Browser effect record must be an object")
    missing = _REQUIRED_FIELDS.difference(record)
    if missing:
        raise BrowserEffectContractError(
            f"Browser effect record is missing fields: {', '.join(sorted(missing))}"
        )

    attempt_count = record.get("attempt_count")
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count < 0:
        raise BrowserEffectContractError(
            "Browser effect attempt_count must be a non-negative integer"
        )

    return BrowserEffectRecord(
        effect_id=_required_text(record, "effect_id"),
        scene_id=_required_text(record, "scene_id"),
        action=_required_text(record, "action"),
        precondition=_required_mapping(record, "precondition"),
        expected_postcondition=_required_mapping(record, "expected_postcondition"),
        status=_effect_status(record.get("status")),
        evidence=_effect_evidence(record.get("evidence")),
        attempt_count=attempt_count,
    )


def _effect_observation_exists(
    evidence: tuple[dict[str, Any], ...],
    expected: bool,
) -> bool:
    return any(item.get("effect_observed") is expected for item in evidence)


def browser_effect_execution_decision(
    effect: BrowserEffectRecord | Mapping[str, Any],
) -> BrowserEffectDecision:
    """Choose the only automatic action permitted by the current evidence."""

    record = validate_browser_effect_record(effect)
    if record.status is BrowserEffectStatus.NOT_STARTED:
        return BrowserEffectDecision.EXECUTE
    if record.status is BrowserEffectStatus.OBSERVED_COMPLETE:
        return BrowserEffectDecision.REUSE
    if (
        record.status is BrowserEffectStatus.FAILED
        and _effect_observation_exists(record.evidence, False)
    ):
        return BrowserEffectDecision.RETRY
    return BrowserEffectDecision.OBSERVE_OR_PAUSE


def transition_browser_effect(
    effect: BrowserEffectRecord | Mapping[str, Any],
    target_status: BrowserEffectStatus | str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> BrowserEffectRecord:
    """Apply one legal effect transition without weakening retry evidence."""

    record = validate_browser_effect_record(effect)
    try:
        target = (
            target_status
            if isinstance(target_status, BrowserEffectStatus)
            else BrowserEffectStatus(target_status)
        )
    except (TypeError, ValueError) as exc:
        raise BrowserEffectTransitionError("Browser effect target status is invalid") from exc

    next_evidence = record.evidence
    if evidence is not None:
        if not isinstance(evidence, Mapping):
            raise BrowserEffectTransitionError("Browser effect transition evidence must be an object")
        next_evidence = (*next_evidence, deepcopy(dict(evidence)))

    allowed_targets = {
        BrowserEffectStatus.NOT_STARTED: {BrowserEffectStatus.IN_PROGRESS},
        BrowserEffectStatus.IN_PROGRESS: {
            BrowserEffectStatus.OBSERVED_COMPLETE,
            BrowserEffectStatus.FAILED,
            BrowserEffectStatus.UNKNOWN,
        },
        BrowserEffectStatus.FAILED: {BrowserEffectStatus.IN_PROGRESS},
        BrowserEffectStatus.OBSERVED_COMPLETE: set(),
        BrowserEffectStatus.UNKNOWN: {
            BrowserEffectStatus.OBSERVED_COMPLETE,
            BrowserEffectStatus.FAILED,
        },
    }
    if target not in allowed_targets[record.status]:
        raise BrowserEffectTransitionError(
            f"Browser effect cannot transition from {record.status.value} to {target.value}"
        )
    if (
        record.status is BrowserEffectStatus.FAILED
        and target is BrowserEffectStatus.IN_PROGRESS
        and not _effect_observation_exists(next_evidence, False)
    ):
        raise BrowserEffectTransitionError(
            "Failed browser effect cannot retry until evidence proves no effect occurred"
        )
    if (
        record.status is BrowserEffectStatus.UNKNOWN
        and target is BrowserEffectStatus.FAILED
        and not _effect_observation_exists(next_evidence, False)
    ):
        raise BrowserEffectTransitionError(
            "Unknown browser effect requires observed absence before it can fail safely"
        )
    if (
        target is BrowserEffectStatus.OBSERVED_COMPLETE
        and not _effect_observation_exists(next_evidence, True)
    ):
        raise BrowserEffectTransitionError(
            "Completed browser effect requires observed postcondition evidence"
        )

    attempt_count = record.attempt_count
    if target is BrowserEffectStatus.IN_PROGRESS:
        attempt_count += 1
    return replace(
        record,
        status=target,
        evidence=next_evidence,
        attempt_count=attempt_count,
    )
