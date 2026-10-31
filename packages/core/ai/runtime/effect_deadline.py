"""Immutable Workflow deadlines for selected external Runtime actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from packages.core.ai.runtime.tool_effect_classification import RuntimeToolClassification, RuntimeToolEffect

_MAX_ACTION_KEYS = 64


def normalize_effect_deadline(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("workflow effect deadline must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("workflow effect deadline must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("workflow effect deadline must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_effect_action_keys(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("workflow effect deadline action_keys must be a nonempty array")
    result = tuple(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))
    if not result or len(result) > _MAX_ACTION_KEYS or len(result) != len(values):
        raise ValueError("workflow effect deadline action_keys must be unique nonempty strings")
    return result


def workflow_effect_deadline_covers_action(action_key: str, action_keys: Iterable[str]) -> bool:
    declared_keys = set(action_keys)
    # ``chrome.browser.interact`` is the public Runtime action family for a
    # Workflow that must stop starting browser mutations. Specific Chrome
    # actions retain their stricter approval keys (for example a semantic
    # publish button is representational_communication.confirm), so matching
    # only the exact key would let a model omit or mislabel its intent and
    # bypass a declared deadline. Runtime excludes reads before this match.
    return action_key in declared_keys or (
        "chrome.browser.interact" in declared_keys
        and action_key.startswith("chrome.")
    )


def workflow_effect_deadline_expired(*, deadline_at: str | None, action_keys: Iterable[str] | None,
                                     classification: RuntimeToolClassification, now: datetime | None = None) -> bool:
    if not deadline_at or not action_keys or classification.effect is not RuntimeToolEffect.ACTION:
        return False
    action = classification.action
    if action is None or not workflow_effect_deadline_covers_action(action.action_key, action_keys):
        return False
    deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) >= deadline.astimezone(timezone.utc)
