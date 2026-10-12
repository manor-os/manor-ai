"""Workspace hard constraints and the unified approval configuration binding.

Consent is evaluated only by ``approval_policy.evaluate_policy``. This module
retains the independent risk and budget boundaries for existing callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from packages.core.governance.approval_policy import ApprovalPolicyBinding, ApprovalPolicyOverride

_RISK_RANK = {"low": 0, "medium": 1, "high": 2}
_RETIRED_FIELDS = frozenset({
    "never_allow_actions", "hitl_required_actions", "auto_approve_actions",
    "never_allow_capabilities", "hitl_required_capabilities", "auto_approve_capabilities",
    "automated_runs_always_allow",
})


class PolicyError(ValueError):
    """Malformed or retired policy configuration."""


@dataclass
class WorkspacePolicy:
    approval: ApprovalPolicyBinding | ApprovalPolicyOverride | None = None
    max_risk_level: str = "high"
    budget_caps_per_kind: dict[str, int] = field(default_factory=dict)


DEFAULT_POLICY = WorkspacePolicy()


@dataclass
class PolicyDecision:
    allowed: bool
    pause_for_hitl: bool = False
    reason: Optional[str] = None
    matched_rule: Optional[str] = None
    explanation: dict[str, Any] | None = None


def policy_from_dict(raw: Optional[dict]) -> WorkspacePolicy:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise PolicyError("Workspace policy must be an object")
    retired = _RETIRED_FIELDS.intersection(raw)
    if retired:
        raise PolicyError("Retired approval fields: " + ", ".join(sorted(retired)))
    unknown = set(raw) - {"approval", "max_risk_level", "budget_caps_per_kind"}
    if unknown:
        raise PolicyError("Unknown policy fields: " + ", ".join(sorted(unknown)))
    max_risk = raw.get("max_risk_level", "high")
    if max_risk not in _RISK_RANK:
        raise PolicyError(f"max_risk_level must be low|medium|high, got {max_risk!r}")
    caps = raw.get("budget_caps_per_kind", {})
    if not isinstance(caps, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
        for k, v in caps.items()
    ):
        raise PolicyError("budget_caps_per_kind must be dict[str, int(>=0)]")
    try:
        approval_raw = raw.get("approval")
        if approval_raw is not None and not isinstance(approval_raw, dict):
            raise PolicyError("approval must be an object")
        approval = (ApprovalPolicyBinding if "root_entity_id" in approval_raw else ApprovalPolicyOverride).model_validate(approval_raw) if approval_raw is not None else None
    except ValidationError as exc:
        raise PolicyError("Invalid unified approval binding") from exc
    return WorkspacePolicy(approval=approval, max_risk_level=max_risk, budget_caps_per_kind=dict(caps))


def policy_to_dict(policy: WorkspacePolicy) -> dict[str, Any]:
    result = {
        "max_risk_level": policy.max_risk_level,
        "budget_caps_per_kind": dict(policy.budget_caps_per_kind),
    }
    if policy.approval is not None:
        result["approval"] = policy.approval.model_dump(mode="json")
    return result


def decide(
    policy: WorkspacePolicy, *, kind: str, action_key: Optional[str],
    risk_level: str, resource_id: Optional[str] = None,
    capability_id: Optional[str] = None,
    spent_credits_per_kind: Optional[dict[str, int]] = None,
) -> PolicyDecision:
    """Check hard constraints; callers must separately evaluate consent."""
    if risk_level not in _RISK_RANK:
        return PolicyDecision(False, reason="Unknown action risk", matched_rule="risk_level.invalid")
    if _RISK_RANK[risk_level] > _RISK_RANK[policy.max_risk_level]:
        return PolicyDecision(False, reason=f"risk_level={risk_level!r} exceeds workspace ceiling {policy.max_risk_level!r}", matched_rule="max_risk_level")
    if spent_credits_per_kind is not None:
        cap = policy.budget_caps_per_kind.get(kind)
        if cap is not None and spent_credits_per_kind.get(kind, 0) >= cap:
            return PolicyDecision(False, reason=f"per-kind cap reached ({cap} credits) for kind={kind!r}", matched_rule=f"budget_caps_per_kind.{kind}")
    return PolicyDecision(True)
