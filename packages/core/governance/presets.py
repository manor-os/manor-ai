"""Governance presets — Safe / Standard / Aggressive.

Picked at blueprint-install time. Each preset is a transform that takes
the blueprint author's policy and returns the variant the operator
actually wants to run with. The operator can edit the result freely
afterwards via the regular governance API — presets are a starting
point, not a lock.

Why presets and not "edit before install": new operators usually don't
have a calibrated sense of what's risky for the wedge they're trying.
"Safe" is the answer to "I don't know what I'm doing yet, please don't
let me set my house on fire." "Aggressive" is the answer to "I've run
this for a month, I trust it, get out of my way."

Design: each preset is a pure function ``(WorkspacePolicy) → WorkspacePolicy``.
That keeps the variants composable for the M12.4 counterfactual
report — we can re-run history through each preset without touching
the live policy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from packages.core.governance.policy import (
    DEFAULT_AUTO_APPROVE_CAPABILITIES,
    WorkspacePolicy,
)

PresetTransform = Callable[[WorkspacePolicy], WorkspacePolicy]


@dataclass(frozen=True)
class GovernancePreset:
    """One option in the install picker."""

    key: str
    """Stable identifier ('safe' | 'standard' | 'aggressive')."""
    title: str
    summary: str
    """One-sentence pitch shown in the install dialog."""
    transform: PresetTransform


# ── Preset implementations ───────────────────────────────────────────

def _safe(base: WorkspacePolicy) -> WorkspacePolicy:
    """Maximally cautious overlay.

    Rules:
      * cap risk at low — no medium/high actions ever
      * everything not in auto_approve becomes HITL (we add a wildcard
        to hitl_required_actions so the dispatcher pauses by default)
      * tighten per-kind budgets to half of what the blueprint asked
        for, and floor llm/action at modest defaults if absent
    """
    hitl = list(base.hitl_required_actions)
    if "*" not in hitl:
        hitl.append("*")
    hitl_capabilities = list(base.hitl_required_capabilities)
    if "*" not in hitl_capabilities:
        hitl_capabilities.append("*")

    caps = dict(base.budget_caps_per_kind)
    for k in ("llm", "action", "code", "subagent"):
        existing = caps.get(k)
        if existing is None:
            caps[k] = 50  # safe default ceiling
        else:
            caps[k] = max(1, existing // 2)

    return replace(
        base,
        max_risk_level="low",
        hitl_required_actions=hitl,
        hitl_required_capabilities=hitl_capabilities,
        # Safe stays cautious: strip the default auto-approve capabilities
        # (file.write, manor.composite, …) so those still pause (everything
        # routes via the "*" hitl wildcard above).
        auto_approve_capabilities=[
            c for c in base.auto_approve_capabilities
            if c not in DEFAULT_AUTO_APPROVE_CAPABILITIES
        ],
        budget_caps_per_kind=caps,
    )


def _standard(base: WorkspacePolicy) -> WorkspacePolicy:
    """Ship the blueprint author's policy as-is, plus the default capability
    grants (file.write, manor.composite, …) so a standard workspace stops
    prompting for routine workspace file/doc operations."""
    missing = [
        c for c in DEFAULT_AUTO_APPROVE_CAPABILITIES
        if c not in base.auto_approve_capabilities
    ]
    if not missing:
        return base
    return replace(
        base,
        auto_approve_capabilities=base.auto_approve_capabilities + missing,
    )


def _aggressive(base: WorkspacePolicy) -> WorkspacePolicy:
    """Hands-off overlay.

    Rules:
      * lift risk ceiling to high (whatever the blueprint asks)
      * downgrade most HITL gates by re-routing them to auto_approve.
        Exception: ``never_allow_actions`` stay hard-denied — operator
        can still strip those in a separate edit if they really mean it.
      * double per-kind budget caps so the agent has more rope
    """
    auto = list(base.auto_approve_actions)
    for pattern in base.hitl_required_actions:
        if pattern == "*":
            # Don't auto-approve a global wildcard — that's silly.
            continue
        if pattern not in auto:
            auto.append(pattern)
    auto_capabilities = list(base.auto_approve_capabilities)
    for pattern in base.hitl_required_capabilities:
        if pattern == "*":
            continue
        if pattern not in auto_capabilities:
            auto_capabilities.append(pattern)
    # Aggressive always grants the default auto-approve capabilities.
    for cap in DEFAULT_AUTO_APPROVE_CAPABILITIES:
        if cap not in auto_capabilities:
            auto_capabilities.append(cap)

    caps = dict(base.budget_caps_per_kind)
    for k, v in list(caps.items()):
        caps[k] = v * 2

    return replace(
        base,
        max_risk_level="high",
        # Keep never_allow as the author wrote it — that's a hard ceiling.
        # Drop the HITL gates (they moved to auto_approve).
        hitl_required_actions=[],
        auto_approve_actions=auto,
        hitl_required_capabilities=[],
        auto_approve_capabilities=auto_capabilities,
        budget_caps_per_kind=caps,
    )


# ── Registry ─────────────────────────────────────────────────────────

PRESETS: dict[str, GovernancePreset] = {
    "safe": GovernancePreset(
        key="safe",
        title="Safe",
        summary=(
            "Hold my hand — every action that isn't pre-approved pauses "
            "for me to review. Risk capped at 'low'. Tight per-kind budgets."
        ),
        transform=_safe,
    ),
    "standard": GovernancePreset(
        key="standard",
        title="Standard",
        summary=(
            "Use the policy the blueprint author shipped. Good default if "
            "you trust the recipe and want to mirror its calibration."
        ),
        transform=_standard,
    ),
    "aggressive": GovernancePreset(
        key="aggressive",
        title="Aggressive",
        summary=(
            "Stay out of my way. Lifts the risk ceiling, auto-approves "
            "the author's HITL gates, doubles per-kind budgets. Hard 'never "
            "allow' rules still apply."
        ),
        transform=_aggressive,
    ),
}


def list_presets() -> list[GovernancePreset]:
    """Picker-friendly list, in the canonical Safe → Standard → Aggressive order."""
    return [PRESETS["safe"], PRESETS["standard"], PRESETS["aggressive"]]


def get_preset(key: str) -> GovernancePreset:
    if key not in PRESETS:
        raise KeyError(f"unknown governance preset: {key!r}")
    return PRESETS[key]


def apply_preset(policy: WorkspacePolicy, preset_key: str) -> WorkspacePolicy:
    """Convenience: look up + transform in one call."""
    return get_preset(preset_key).transform(policy)
