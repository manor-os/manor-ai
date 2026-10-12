"""Closed wire vocabulary for durable external-tool effects.

These values are persisted in ``ExecutionStep.evidence_refs`` and Workspace
Chat pending actions.  Keep the string values stable; changing one requires a
data migration for in-flight Plans and unresolved HITL requests.
"""
from __future__ import annotations

from enum import StrEnum, unique


@unique
class ToolEffectEvidenceKind(StrEnum):
    TOOL_EFFECT = "tool_effect"
    RECONCILIATION = "tool_effect_reconciliation"
    INTENT_SETTLEMENT = "tool_intent_settlement"
    RESULT_HANDOFF = "tool_result_handoff"


@unique
class ToolEffectOutcome(StrEnum):
    PENDING = "pending"
    ACTION = "action"
    PUBLISH = "publish"
    OPERATOR_CONFIRMED = "operator_confirmed"


@unique
class ToolEffectIntentState(StrEnum):
    STARTED = "started"
    RETURNED = "returned"
    OPERATOR_CONFIRMED_APPLIED = "operator_confirmed_applied"


@unique
class ToolEffectSettlementOutcome(StrEnum):
    OPERATOR_CONFIRMED_APPLIED = "operator_confirmed_applied"
    OPERATOR_CONFIRMED_NOT_APPLIED = "operator_confirmed_not_applied"
    RUNTIME_CONFIRMED_NOT_APPLIED = "runtime_confirmed_not_applied"


@unique
class EffectReconciliationDecision(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    CANCEL = "cancel"


@unique
class EffectReconciliationChoice(StrEnum):
    CONFIRM_APPLIED = "confirm_effect_applied"
    CONFIRM_NOT_APPLIED = "confirm_effect_not_applied"
    RECONCILE = "reconcile_effects"
    CANCEL = "cancel"


__all__ = [
    "EffectReconciliationChoice",
    "EffectReconciliationDecision",
    "ToolEffectEvidenceKind",
    "ToolEffectIntentState",
    "ToolEffectOutcome",
    "ToolEffectSettlementOutcome",
]
