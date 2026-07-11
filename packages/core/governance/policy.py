"""Validated WorkspacePolicy dataclass.

The DB stores the policy as JSONB so it can grow without migrations.
This module is the contract between storage and runtime: everything
that reads or writes a policy goes through ``policy_from_dict`` /
``policy_to_dict`` so we catch shape drift in one place.

Policy semantics — only what the M10 slice needs:

  * ``never_allow_actions``  — hard block. The Dispatcher refuses to
                               lease these no matter who's asking.

  * ``hitl_required_actions`` — soft block. The step gets paused +
                               a HITL prompt is posted to chat.
                               Dispatcher returns 'paused', not 'failed'.

  * ``auto_approve_actions``  — for actions that *would* otherwise need
                               operator approval (e.g. risk_level=high),
                               this list lets the operator pre-approve
                               specific safe ones.

  * ``*_capabilities``       — the same gates, but matched against Runtime
                               BusinessCapability ids such as
                               'workspace.task' or 'file.write'.

  * ``max_risk_level``        — global ceiling. Steps above this are
                               refused regardless of worker capability.

  * ``budget_caps_per_kind``  — soft per-kind credit ceilings inside
                               the workspace's monthly budget. The
                               Dispatcher enforces by skipping steps
                               whose kind has exhausted its slice.

The matcher is glob-style: 'x.*' matches every X action; 'x.post_*'
matches any post variant. Empty lists = no opinion.
"""
from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Risk ranking — duplicated with dispatcher.service to keep the two
# from import-coupling. Three levels is the contract.
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


class PolicyError(Exception):
    """Raised on malformed policy JSONB."""


@dataclass
class WorkspacePolicy:
    """Operator-supplied rules. All fields default to 'no opinion'."""

    never_allow_actions: list[str] = field(default_factory=list)
    hitl_required_actions: list[str] = field(default_factory=list)
    auto_approve_actions: list[str] = field(default_factory=list)
    never_allow_capabilities: list[str] = field(default_factory=list)
    hitl_required_capabilities: list[str] = field(default_factory=list)
    auto_approve_capabilities: list[str] = field(default_factory=list)
    max_risk_level: str = "high"
    """One of 'low' | 'medium' | 'high'. 'high' = no ceiling."""

    # Per-kind credit allowance — kind → max credits per month.
    # Missing key means "no per-kind limit". Workspace-wide budget
    # still applies on top.
    budget_caps_per_kind: dict[str, int] = field(default_factory=dict)


# Capabilities a fresh workspace auto-approves out of the box. Keeping this
# off the WorkspacePolicy field defaults is deliberate: a raw
# ``WorkspacePolicy()`` must stay "no opinion" (empty) so it can represent a
# blueprint author's untouched policy. The grant lives in DEFAULT_POLICY (and
# the presets) instead.
DEFAULT_AUTO_APPROVE_CAPABILITIES = ("file.write", "manor.composite")

# A fresh workspace gets this until the operator overrides.
DEFAULT_POLICY = WorkspacePolicy(
    auto_approve_capabilities=list(DEFAULT_AUTO_APPROVE_CAPABILITIES)
)


@dataclass
class PolicyDecision:
    """What ``check_step_policy`` returns to the Dispatcher."""

    allowed: bool
    pause_for_hitl: bool = False
    reason: Optional[str] = None
    matched_rule: Optional[str] = None
    """Rule pattern that drove the decision — surfaced into audit logs
    so the operator can find which entry to edit."""


# ── Serialization ─────────────────────────────────────────────────────

def policy_from_dict(raw: Optional[dict]) -> WorkspacePolicy:
    """Inflate a JSONB blob into a validated dataclass.

    Unknown keys are dropped silently — forward-compatible. Bad types
    raise PolicyError so the operator notices instead of silently
    falling back to permissive."""
    if raw is None:
        return WorkspacePolicy()

    def _strs(key: str) -> list[str]:
        v = raw.get(key, [])
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            raise PolicyError(f"{key} must be a list[str]")
        return list(v)

    max_risk = raw.get("max_risk_level", "high")
    if max_risk not in _RISK_RANK:
        raise PolicyError(
            f"max_risk_level must be low|medium|high, got {max_risk!r}"
        )

    caps = raw.get("budget_caps_per_kind", {})
    if not isinstance(caps, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and v >= 0
        for k, v in caps.items()
    ):
        raise PolicyError(
            "budget_caps_per_kind must be dict[str, int(>=0)]"
        )

    return WorkspacePolicy(
        never_allow_actions=_strs("never_allow_actions"),
        hitl_required_actions=_strs("hitl_required_actions"),
        auto_approve_actions=_strs("auto_approve_actions"),
        never_allow_capabilities=_strs("never_allow_capabilities"),
        hitl_required_capabilities=_strs("hitl_required_capabilities"),
        auto_approve_capabilities=_strs("auto_approve_capabilities"),
        max_risk_level=max_risk,
        budget_caps_per_kind=dict(caps),
    )


def policy_to_dict(p: WorkspacePolicy) -> dict[str, Any]:
    """Plain dict suitable for JSONB persistence."""
    return asdict(p)


# ── Decision logic ────────────────────────────────────────────────────

def policy_auto_approves(
    policy: WorkspacePolicy,
    *,
    action_key: Optional[str] = None,
    capability_id: Optional[str] = None,
) -> bool:
    """True only when the policy *explicitly* auto-approves this action or
    capability (matches an ``auto_approve_*`` pattern).

    This is intentionally narrow: a default-allow (no rule matched anything) is
    NOT an auto-approval. Used by the dispatcher to let a workspace's explicit
    auto-approve override a capability's intrinsic ``required_approval`` — never
    to clear approval just because nothing denied it. Deny/risk/budget gates
    still run afterward in :func:`decide`."""
    if action_key:
        for pattern in policy.auto_approve_actions:
            if _matches(pattern, action_key):
                return True
    if capability_id:
        for pattern in policy.auto_approve_capabilities:
            if _matches(pattern, capability_id):
                return True
    return False


def decide(
    policy: WorkspacePolicy,
    *,
    kind: str,
    action_key: Optional[str],
    risk_level: str,
    capability_id: Optional[str] = None,
    spent_credits_per_kind: Optional[dict[str, int]] = None,
) -> PolicyDecision:
    """Evaluate a step against the policy. Returns a PolicyDecision —
    the Dispatcher branches on .allowed / .pause_for_hitl.

    ``spent_credits_per_kind`` is the rolling per-kind monthly spend;
    omit when the caller doesn't care about budget caps (e.g. preview).
    """
    # Risk ceiling first — cheapest check.
    if _RISK_RANK.get(risk_level, 0) > _RISK_RANK[policy.max_risk_level]:
        return PolicyDecision(
            allowed=False,
            reason=(
                f"risk_level={risk_level!r} exceeds workspace ceiling "
                f"{policy.max_risk_level!r}"
            ),
            matched_rule="max_risk_level",
        )

    # Per-kind budget caps.
    if spent_credits_per_kind is not None:
        cap = policy.budget_caps_per_kind.get(kind)
        if cap is not None and spent_credits_per_kind.get(kind, 0) >= cap:
            return PolicyDecision(
                allowed=False,
                reason=f"per-kind cap reached ({cap} credits) for kind={kind!r}",
                matched_rule=f"budget_caps_per_kind.{kind}",
            )

    # Deny gates have highest precedence across action and capability axes.
    if action_key:
        for pattern in policy.never_allow_actions:
            if _matches(pattern, action_key):
                return PolicyDecision(
                    allowed=False,
                    reason=f"action_key={action_key!r} blocked by rule {pattern!r}",
                    matched_rule=pattern,
                )
    if capability_id:
        for pattern in policy.never_allow_capabilities:
            if _matches(pattern, capability_id):
                return PolicyDecision(
                    allowed=False,
                    reason=f"capability_id={capability_id!r} blocked by rule {pattern!r}",
                    matched_rule=pattern,
                )

    # auto_approve has higher precedence than hitl_required so the operator can
    # carve exceptions out of an otherwise-HITL pattern.
    if action_key:
        for pattern in policy.auto_approve_actions:
            if _matches(pattern, action_key):
                return PolicyDecision(allowed=True, matched_rule=pattern)
    if capability_id:
        for pattern in policy.auto_approve_capabilities:
            if _matches(pattern, capability_id):
                return PolicyDecision(allowed=True, matched_rule=pattern)

    if action_key:
        for pattern in policy.hitl_required_actions:
            if _matches(pattern, action_key):
                return PolicyDecision(
                    allowed=False,
                    pause_for_hitl=True,
                    reason=(
                        f"action_key={action_key!r} requires operator "
                        f"approval (rule {pattern!r})"
                    ),
                    matched_rule=pattern,
                )
    if capability_id:
        for pattern in policy.hitl_required_capabilities:
            if _matches(pattern, capability_id):
                return PolicyDecision(
                    allowed=False,
                    pause_for_hitl=True,
                    reason=(
                        f"capability_id={capability_id!r} requires operator "
                        f"approval (rule {pattern!r})"
                    ),
                    matched_rule=pattern,
                )

    return PolicyDecision(allowed=True)


def _matches(pattern: str, action_key: str) -> bool:
    return fnmatch.fnmatchcase(action_key, pattern)
