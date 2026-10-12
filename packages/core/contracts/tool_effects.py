"""Pure factories for durable external-effect evidence and decisions.

The worker, dispatcher, coordinator, Task recovery card, and HTTP router all
cross the same persisted JSON boundary.  This module is deliberately free of
ORM and API imports so every layer applies the same merge and choice rules.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from packages.core.constants.tool_effects import (
    EffectReconciliationChoice,
    EffectReconciliationDecision,
    ToolEffectEvidenceKind,
    ToolEffectOutcome,
    ToolEffectSettlementOutcome,
)


@dataclass(frozen=True, slots=True)
class ToolEffectIdentity:
    """Identity of one exact external call generation."""

    tool: str
    arguments_hash: str
    tool_call_id: str
    effect_generation: str
    owner_plan_id: str
    owner_step_id: str
    coordinator_child_step_id: str


class ToolEffectEvidenceFactory:
    """Construct and merge evidence without trusting a worker as authority."""

    _SERVER_OWNED_KINDS = frozenset({
        ToolEffectEvidenceKind.RECONCILIATION.value,
        ToolEffectEvidenceKind.INTENT_SETTLEMENT.value,
        ToolEffectEvidenceKind.RESULT_HANDOFF.value,
    })

    @staticmethod
    def identity(ref: Mapping[str, Any]) -> ToolEffectIdentity:
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        return ToolEffectIdentity(
            tool=str(fields.get("tool") or ""),
            arguments_hash=str(fields.get("arguments_hash") or ""),
            tool_call_id=str(fields.get("tool_call_id") or ""),
            effect_generation=str(fields.get("effect_generation") or "0"),
            owner_plan_id=str(fields.get("owner_plan_id") or ""),
            owner_step_id=str(fields.get("owner_step_id") or ""),
            coordinator_child_step_id=str(
                ref.get("coordinator_child_step_id")
                or fields.get("coordinator_child_step_id")
                or ""
            ),
        )

    @classmethod
    def effect_id(cls, ref: Mapping[str, Any]) -> str:
        identity = cls.identity(ref)
        return hashlib.sha256(json.dumps(
            {
                "tool": identity.tool,
                "arguments_hash": identity.arguments_hash,
                "tool_call_id": identity.tool_call_id,
                "effect_generation": identity.effect_generation,
                "owner_plan_id": identity.owner_plan_id,
                "owner_step_id": identity.owner_step_id,
                "coordinator_child_step_id": identity.coordinator_child_step_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @classmethod
    def bind_effect_id(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        """Return evidence carrying the server-derived scoped identity."""

        bound = deepcopy(dict(ref))
        fields = (
            dict(bound.get("fields") or {})
            if isinstance(bound.get("fields"), dict)
            else {}
        )
        fields["effect_id"] = cls.effect_id(bound)
        bound["fields"] = fields
        return bound

    @classmethod
    def has_valid_effect_id(cls, ref: Mapping[str, Any]) -> bool:
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        claimed = str(fields.get("effect_id") or "")
        return not claimed or claimed == cls.effect_id(ref)

    @staticmethod
    def receipt_digest(ref: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            dict(ref), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    @classmethod
    def result_handoff(cls, source: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
        """Host-only continuation proof; never a new child execution receipt."""
        source_fields = source.get("fields")
        source_identity = cls.identity(source)
        target_identity = cls.identity({"fields": dict(fields)})
        if (
            source.get("kind") != ToolEffectEvidenceKind.TOOL_EFFECT.value
            or source.get("effect") not in (ToolEffectOutcome.ACTION.value, ToolEffectOutcome.PUBLISH.value)
            or not isinstance(source_fields, dict)
            or not source_fields.get("effect_id")
            or source_fields.get("intent_state") != "returned"
            or not source_fields.get("result_sha256")
            or not cls.has_valid_effect_id(source)
            or not all((source_identity.tool, source_identity.tool_call_id, source_identity.arguments_hash,
                        source_identity.owner_step_id, source_identity.owner_plan_id))
            or target_identity.tool != "invoke_skill"
            or not target_identity.tool_call_id or not target_identity.arguments_hash
            or target_identity.tool_call_id == source_identity.tool_call_id
            or target_identity.owner_step_id != source_identity.owner_step_id
            or target_identity.owner_plan_id != source_identity.owner_plan_id
            or target_identity.effect_generation != source_identity.effect_generation
            or target_identity.coordinator_child_step_id != source_identity.coordinator_child_step_id
            or fields.get("intent_state") != "returned" or not fields.get("result_sha256")
        ):
            raise ValueError("Result handoff requires a settled source effect")
        return {
            "kind": ToolEffectEvidenceKind.RESULT_HANDOFF.value,
            "fields": {
                **deepcopy(dict(fields)),
                "source_effect_id": cls.effect_id(source),
                "source_effect_digest": cls.receipt_digest(source),
            },
        }

    @staticmethod
    def _signature(ref: Mapping[str, Any]) -> str:
        return json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _settled_pending_signatures(
        cls,
        pending_items: Iterable[Mapping[str, Any]],
        terminal_items: Iterable[Mapping[str, Any]],
    ) -> set[str]:
        """Resolve terminal receipts to pending intents without scope bleed.

        New records carry Plan/Step (and, for coordinator work, child) scope
        plus a server-derived ``effect_id``.  Matching is deliberately exact:
        uniqueness of a legacy four-field key is not proof that an unscoped or
        differently scoped receipt belongs to the sole pending call.  Legacy
        records still match each other because their empty scopes are equal.

        Return signatures, rather than identities, so a legacy duplicate can
        never cause a modern effect-id-bound pending record with the same
        visible fields to be removed as collateral.
        """

        pending = [
            (item, cls.identity(item))
            for item in pending_items
            if cls.has_valid_effect_id(item)
        ]
        settled: set[str] = set()
        for terminal in terminal_items:
            if not cls.has_valid_effect_id(terminal):
                continue
            terminal_identity = cls.identity(terminal)
            terminal_fields = (
                terminal.get("fields")
                if isinstance(terminal.get("fields"), dict)
                else {}
            )
            terminal_effect_id = str(terminal_fields.get("effect_id") or "")
            for pending_item, pending_identity in pending:
                if pending_identity != terminal_identity:
                    continue
                pending_fields = (
                    pending_item.get("fields")
                    if isinstance(pending_item.get("fields"), dict)
                    else {}
                )
                pending_effect_id = str(pending_fields.get("effect_id") or "")
                if pending_effect_id and terminal_effect_id != pending_effect_id:
                    continue
                settled.add(cls._signature(pending_item))
        return settled

    @classmethod
    def is_server_owned(cls, ref: Mapping[str, Any]) -> bool:
        from packages.core.contracts.artifact_delivery import ARTIFACT_DELIVERY_SOURCE

        return (
            ref.get("kind") in cls._SERVER_OWNED_KINDS
            or ref.get("source") == ARTIFACT_DELIVERY_SOURCE
            or (
                ref.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and ref.get("effect") == ToolEffectOutcome.OPERATOR_CONFIRMED.value
            )
        )

    @staticmethod
    def effect(
        outcome: ToolEffectOutcome | str,
        fields: Mapping[str, Any],
        *,
        coordinator_child_step_id: str | None = None,
    ) -> dict[str, Any]:
        value = ToolEffectOutcome(str(outcome)).value
        return {
            "kind": ToolEffectEvidenceKind.TOOL_EFFECT.value,
            "effect": value,
            **(
                {"coordinator_child_step_id": coordinator_child_step_id}
                if coordinator_child_step_id
                else {}
            ),
            "fields": deepcopy(dict(fields)),
        }

    @staticmethod
    def reconciliation(fields: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "kind": ToolEffectEvidenceKind.RECONCILIATION.value,
            "fields": deepcopy(dict(fields)),
        }

    @staticmethod
    def settlement(
        outcome: ToolEffectSettlementOutcome | str,
        fields: Mapping[str, Any],
        *,
        coordinator_child_step_id: str | None = None,
    ) -> dict[str, Any]:
        value = ToolEffectSettlementOutcome(str(outcome)).value
        return {
            "kind": ToolEffectEvidenceKind.INTENT_SETTLEMENT.value,
            "effect": value,
            **(
                {"coordinator_child_step_id": coordinator_child_step_id}
                if coordinator_child_step_id
                else {}
            ),
            "fields": deepcopy(dict(fields)),
        }

    @classmethod
    def merge_untrusted(
        cls,
        existing: Any,
        incoming: Iterable[Any] | None,
    ) -> list[dict[str, Any]]:
        """Merge worker/provider evidence at the server trust boundary.

        Server-owned reconciliation records are ignored.  A terminal receipt
        removes only the pending intent for the same exact call generation.
        Unrelated pending intents remain visible and keep replay fail-closed.
        """

        merged = [dict(item) for item in (existing or []) if isinstance(item, dict)]
        signatures = {cls._signature(item) for item in merged}
        for item in incoming or []:
            if not isinstance(item, dict) or cls.is_server_owned(item):
                continue
            signature = cls._signature(item)
            if signature in signatures:
                continue
            signatures.add(signature)
            merged.append(dict(item))

        pending_items = [
            item
            for item in merged
            if (
                item.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and item.get("effect") == ToolEffectOutcome.PENDING.value
            )
        ]
        returned_items = [
            item
            for item in merged
            if (
                (
                    item.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                    and item.get("effect") in {
                        ToolEffectOutcome.ACTION.value,
                        ToolEffectOutcome.PUBLISH.value,
                    }
                )
                or (
                    item.get("kind")
                    == ToolEffectEvidenceKind.INTENT_SETTLEMENT.value
                    and item.get("effect") in {
                        ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_APPLIED.value,
                        ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_NOT_APPLIED.value,
                        ToolEffectSettlementOutcome.RUNTIME_CONFIRMED_NOT_APPLIED.value,
                    }
                )
            )
            and isinstance(item.get("fields"), dict)
            and cls.identity(item).tool
            and cls.identity(item).arguments_hash
        ]
        settled = cls._settled_pending_signatures(pending_items, returned_items)
        return [
            item
            for item in merged
            if not (
                item.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and item.get("effect") == ToolEffectOutcome.PENDING.value
                and cls._signature(item) in settled
            )
        ]

    @classmethod
    def append_server_owned(
        cls,
        existing: Any,
        incoming: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append trusted service evidence and settle explicitly named intents.

        Unlike :meth:`merge_untrusted`, only an incoming settlement record may
        remove a pending intent.  This keeps the authority transition explicit
        inside the locked reconciliation transaction.
        """

        incoming_items = [dict(item) for item in incoming if isinstance(item, dict)]
        merged = [dict(item) for item in (existing or []) if isinstance(item, dict)]
        signatures = {cls._signature(item) for item in merged}
        for item in incoming_items:
            signature = cls._signature(item)
            if signature in signatures:
                continue
            signatures.add(signature)
            merged.append(deepcopy(item))
        pending_items = [
            item
            for item in merged
            if (
                item.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and item.get("effect") == ToolEffectOutcome.PENDING.value
            )
        ]
        settlement_items = [
            item
            for item in incoming_items
            if (
                item.get("kind") == ToolEffectEvidenceKind.INTENT_SETTLEMENT.value
                and item.get("effect") in {
                    ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_APPLIED.value,
                    ToolEffectSettlementOutcome.OPERATOR_CONFIRMED_NOT_APPLIED.value,
                    ToolEffectSettlementOutcome.RUNTIME_CONFIRMED_NOT_APPLIED.value,
                }
            )
        ]
        settled = cls._settled_pending_signatures(pending_items, settlement_items)
        return [
            item
            for item in merged
            if not (
                item.get("kind") == ToolEffectEvidenceKind.TOOL_EFFECT.value
                and item.get("effect") == ToolEffectOutcome.PENDING.value
                and cls._signature(item) in settled
            )
        ]


class EffectReconciliationDecisionFactory:
    """One parser and policy for every reconciliation decision surface."""

    _ALIASES: dict[str, EffectReconciliationDecision] = {
        EffectReconciliationDecision.APPLIED.value:
            EffectReconciliationDecision.APPLIED,
        EffectReconciliationChoice.CONFIRM_APPLIED.value:
            EffectReconciliationDecision.APPLIED,
        EffectReconciliationDecision.NOT_APPLIED.value:
            EffectReconciliationDecision.NOT_APPLIED,
        EffectReconciliationChoice.CONFIRM_NOT_APPLIED.value:
            EffectReconciliationDecision.NOT_APPLIED,
        EffectReconciliationDecision.CANCEL.value:
            EffectReconciliationDecision.CANCEL,
    }
    _CHOICE_ALIASES: dict[str, EffectReconciliationDecision] = {
        EffectReconciliationChoice.CONFIRM_APPLIED.value:
            EffectReconciliationDecision.APPLIED,
        EffectReconciliationChoice.CONFIRM_NOT_APPLIED.value:
            EffectReconciliationDecision.NOT_APPLIED,
        EffectReconciliationChoice.CANCEL.value:
            EffectReconciliationDecision.CANCEL,
    }

    @classmethod
    def normalize(cls, value: Any) -> EffectReconciliationDecision | None:
        return cls._ALIASES.get(str(value or "").strip().lower())

    @classmethod
    def normalize_choice(cls, value: Any) -> EffectReconciliationDecision | None:
        return cls._CHOICE_ALIASES.get(str(value or "").strip().lower())

    @classmethod
    def normalize_map(cls, values: Mapping[Any, Any]) -> dict[str, str] | None:
        """Normalize an exact digest map, rejecting rather than aliasing bad input."""

        normalized_values: dict[str, str] = {}
        for digest, decision in values.items():
            if not isinstance(digest, str) or not digest:
                return None
            normalized = cls.normalize(decision)
            if normalized is None:
                return None
            normalized_values[digest] = normalized.value
        return normalized_values

    @classmethod
    def is_reconciliation_choice(
        cls,
        value: Any,
        *,
        allow_cancel: bool,
    ) -> bool:
        """Whether a UI choice belongs to the effect-reconciliation domain."""

        raw = str(value or "").strip().lower()
        if raw == EffectReconciliationChoice.RECONCILE.value:
            return True
        decision = cls.normalize(raw)
        return decision in {
            EffectReconciliationDecision.APPLIED,
            EffectReconciliationDecision.NOT_APPLIED,
        } or (
            allow_cancel and decision is EffectReconciliationDecision.CANCEL
        )

    @staticmethod
    def effect_digests(snapshot: Mapping[str, Any]) -> frozenset[str]:
        return frozenset(
            str(item.get("effect_digest") or "")
            for item in (snapshot.get("effects") or [])
            if isinstance(item, dict) and item.get("effect_digest")
        )

    @staticmethod
    def allows_not_applied(snapshot: Mapping[str, Any]) -> bool:
        return snapshot.get("not_applied_allowed") is not False

    @classmethod
    def card_options(cls, snapshot: Mapping[str, Any]) -> list[str]:
        # Options express operator intent; they are not authorization.  The
        # locked service re-evaluates live lease/claim quiescence. Persisting
        # a creation-time ``False`` as a permanent capability would strand the
        # card after the in-flight claim is later released.
        return [
            EffectReconciliationChoice.CONFIRM_APPLIED.value,
            EffectReconciliationChoice.CONFIRM_NOT_APPLIED.value,
            EffectReconciliationChoice.CANCEL.value,
        ]

    @classmethod
    def action_to_take(cls, snapshot: Mapping[str, Any]) -> str:
        if cls.allows_not_applied(snapshot):
            return (
                "Check the provider, then confirm whether every listed action "
                "happened. Paste an exact JSON result when confirming an applied "
                "action that has no saved provider result."
            )
        return (
            "Check the provider, then confirm the recorded action happened or "
            "cancel this run. Retrying is unavailable while the prior provider "
            "call may still be in flight."
        )


__all__ = [
    "EffectReconciliationDecisionFactory",
    "ToolEffectEvidenceFactory",
    "ToolEffectIdentity",
]
