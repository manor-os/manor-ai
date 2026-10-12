"""Install choices use the same approval modes as account settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from packages.core.governance.policy import WorkspacePolicy
from packages.core.governance.approval_projection import policy_override, with_approval_override


@dataclass(frozen=True)
class GovernancePreset:
    key: str
    title: str
    summary: str
    transform: Callable[[WorkspacePolicy], WorkspacePolicy]


def _mode(policy: WorkspacePolicy, mode: str | None) -> WorkspacePolicy:
    override = policy_override(policy)
    override.mode = mode
    return with_approval_override(policy, override)


PRESETS: dict[str, GovernancePreset] = {
    'automatic': GovernancePreset(
        'automatic', 'Automatic',
        'Run authorized actions automatically. Explicit approval rules and Workflow review steps still apply.',
        lambda policy: _mode(policy, 'automatic'),
    ),
    'follow_default': GovernancePreset(
        'follow_default', 'Follow account default',
        'Use the current account default. Template review requirements still apply.',
        lambda policy: _mode(policy, None),
    ),
    'confirm_changes': GovernancePreset(
        'confirm_changes', 'Confirm changes',
        'Ask before making changes or taking external actions.',
        lambda policy: _mode(policy, 'confirm_changes'),
    ),
    'risk_based': GovernancePreset(
        'risk_based', 'Confirm by risk',
        'Routine edits run automatically; sending, publishing and deletion require approval.',
        lambda policy: _mode(policy, 'risk_based'),
    ),
}


def list_presets() -> list[GovernancePreset]:
    return list(PRESETS.values())


def get_preset(key: str) -> GovernancePreset:
    if key not in PRESETS:
        raise KeyError(f'unknown governance preset: {key!r}; refresh the client')
    return PRESETS[key]


def apply_preset(policy: WorkspacePolicy, preset_key: str) -> WorkspacePolicy:
    return get_preset(preset_key).transform(policy)
