"""Normalize server-owned action metadata for the single approval evaluator."""
from __future__ import annotations

import logging
import sys

from packages.core.governance.action_effects import (
    GATE_RESOLVED_ACTION_KEYS,
    SANDBOX_TOOL_ACTION_KEYS as SANDBOX_TOOL_ACTION_KEYS,  # re-exported
    UndeclaredEffectError,
    declared_action_effect,
    strict_effect_contract,
)
from packages.core.governance.approval_policy import ActionFacts, ApprovalTarget

logger = logging.getLogger(__name__)

UNDECLARED_EFFECT_EVENT = "approval.effect_undeclared"

# Plan step kinds whose subject has no effect of its own when it names no
# action and no capability (their tool calls are gated at the runtime gate).
EFFECTLESS_STEP_KINDS = frozenset({"llm", "subagent"})


class ExemptFacts:
    """Sentinel facts: there is no effect to evaluate, only hard constraints."""

    _instance: "ExemptFacts | None" = None

    def __new__(cls) -> "ExemptFacts":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "EXEMPT"


EXEMPT = ExemptFacts()


def _caller() -> str:
    frame = sys._getframe(3)  # _caller <- _report_undeclared <- action_facts <- caller
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}"


def _report_undeclared(key: str, *, named: bool) -> None:
    caller = _caller()
    # Strict mode guards *named* keys: a key that exists must be declared. A
    # subject that names no action at all (an unclassified plan step) has
    # nothing to declare; it is reported and evaluates as unknown (ask).
    if named and strict_effect_contract():
        raise UndeclaredEffectError(
            f"Action {key!r} has no declared effect (caller {caller}); declare it in "
            "packages/core/governance/action_effects.py or pass the factory's effect."
        )
    logger.warning(UNDECLARED_EFFECT_EVENT, extra={
        "event": UNDECLARED_EFFECT_EVENT, "action_key": key, "caller": caller,
    })
    try:
        from opentelemetry import trace

        trace.get_current_span().add_event(UNDECLARED_EFFECT_EVENT, {"action_key": key, "caller": caller})
    except Exception:  # tracing is optional
        pass


def action_facts(
    *, action_key: str | None, capability_id: str | None = None,
    resource_kind: str | None = None, resource_id: str | None = None,
    resource_ids: tuple[str, ...] = (), account_id: str | None = None,
    operation: str | None = None, risk_level: str = "medium",
    impact: str | None = None, category: str | None = None, gate: str | None = None,
    aliases: tuple[str, ...] = (), restrict_only_keys: tuple[str, ...] = (),
) -> ActionFacts:
    """Facts come from the effect declaration, never from guessing.

    Precedence: an effect passed explicitly by its factory (dynamic MCP
    families), then :func:`declared_action_effect`. An undeclared key is
    ``unknown``/``unknown`` (which asks in every mode) and is reported as
    ``approval.effect_undeclared``; under ``MANOR_STRICT_EFFECT_CONTRACT`` it
    raises instead. ``capability_id`` and ``operation`` never change facts.
    """
    del capability_id, operation
    named = bool(str(action_key or "").strip())
    key = str(action_key or "").strip() or "unclassified.action"
    declared = declared_action_effect(key)
    if impact is None:
        if declared is None:
            # A gate-resolved key is declared as resolved at the gate: a
            # static caller gets the honest unknown, never an undeclared error.
            if key not in GATE_RESOLVED_ACTION_KEYS:
                _report_undeclared(key, named=named)
            impact = "unknown"
        else:
            impact = declared.impact
    if category is None:
        category = declared.category if declared is not None else "unknown"
    if impact == "read":
        # A read is in the read category, except a basic browser read the
        # declaration (or its factory) puts in the browser category, so the
        # user's browser rules reach it and legacy ``read`` rules do not.
        browser = category == "browser" or (declared is not None and declared.category == "browser")
        category = "browser" if browser else "read"
    if gate is None:
        gate = declared.gate.value if declared is not None else "policy"
    targets = [
        ApprovalTarget(account_id=account_id, resource_kind=resource_kind or "resource", resource_id=target)
        for target in dict.fromkeys((resource_id, *resource_ids)) if target
    ]
    return ActionFacts(action_key=key, category=category, impact=impact, risk_level=risk_level,
                       targets=targets, gate=str(gate),
                       aliases=[alias for alias in dict.fromkeys(aliases) if alias and alias != key],
                       restrict_only_keys=[k for k in dict.fromkeys(restrict_only_keys) if k])
