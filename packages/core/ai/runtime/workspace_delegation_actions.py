"""Runtime-owned workspace service delegation actions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from hashlib import sha256
from inspect import isawaitable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator

if TYPE_CHECKING:
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from packages.core.models.base import generate_ulid

from packages.core.constants.tool_effects import (
    ToolEffectIntentState,
    ToolEffectOutcome,
)
from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory
from packages.core.contracts.tool_result_disposition import (
    ToolResultDisposition,
    ToolResultDispositionFactory,
)

logger = logging.getLogger(__name__)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _event_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_event_signature(name: str, args: dict[str, Any] | None) -> str:
    try:
        encoded = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = str(args or {})
    return f"{name}\0{encoded}"


def _tool_arguments_hash(arguments: dict[str, Any] | None) -> str:
    from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
    from packages.core.services.sensitive_data import sanitize_approval_credentials

    public_arguments = {
        key: value
        for key, value in (arguments or {}).items()
        if key not in RUNTIME_TOOL_CONTEXT_KEYS
    }
    sanitized = sanitize_approval_credentials(public_arguments)
    encoded = json.dumps(
        sanitized if isinstance(sanitized, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _delegation_usage_delta(
    cumulative: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return only usage produced by this invocation of a resumed child.

    A child loop resumes with cumulative usage from its checkpoint.  Recording
    that whole value into the parent nested-usage collector would charge and
    attribute the pre-resume rounds twice, so only monotonic counter deltas
    cross the nesting boundary.
    """

    current = cumulative if isinstance(cumulative, dict) else {}
    previous = baseline if isinstance(baseline, dict) else {}
    delta: dict[str, Any] = {}
    for key in (
        "prompt",
        "prompt_tokens",
        "completion",
        "completion_tokens",
        "total",
        "total_tokens",
        "cache_read",
        "cache_read_input_tokens",
        "cache_creation",
        "cache_creation_input_tokens",
    ):
        if key not in current:
            continue
        try:
            amount = max(0, int(current.get(key) or 0) - int(previous.get(key) or 0))
        except (TypeError, ValueError):
            continue
        if amount:
            delta[key] = amount
    if current.get("cost_usd") is not None:
        try:
            amount = float(current.get("cost_usd") or 0) - float(
                previous.get("cost_usd") or 0
            )
        except (TypeError, ValueError):
            amount = 0.0
        if math.isfinite(amount) and amount > 0:
            delta["cost_usd"] = amount
    if delta:
        for key in (
            "model",
            "provider",
            "byok",
            "billing_mode",
            "llm_billing_mode",
            "api_key_source",
            "llm_api_key_source",
        ):
            if current.get(key) is not None:
                delta[key] = current[key]
    return delta


def _bounded_max_rounds(
    value: Any,
    *,
    default: int = 200,
    upper: int = 200,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(1, min(parsed, upper))


def _delegation_max_rounds(
    value: Any,
    *,
    task_id: str | None,
    step_id: str | None,
) -> int:
    """Keep interactive delegation tight while giving Plan children full depth."""

    if task_id and step_id:
        from packages.core.ai.runtime.subagents import PLAN_STEP_MAX_ROUNDS

        return _bounded_max_rounds(
            value,
            default=PLAN_STEP_MAX_ROUNDS,
            upper=PLAN_STEP_MAX_ROUNDS,
        )
    return _bounded_max_rounds(value)


def _delegation_prompt(raw_params: dict[str, Any]) -> str:
    for key in (
        "prompt",
        "instructions",
        "instruction",
        "task",
        "message",
        "request",
    ):
        value = raw_params.get(key)
        if value is None:
            continue
        text = _clean(value)
        if text:
            return text
    return ""


def runtime_child_ledger_items(run_id: str, tool_evidence_state: Any) -> list[dict[str, Any]]:
    """A child run's whole ledger as parent observations.

    The run prefix keeps parallel children's local call IDs apart (and a
    parent call's ID unique across its children). Every host that merges a
    child's ledger into its parent uses this one shape.
    """
    from packages.core.ai.runtime import tool_evidence as evidence

    return [
        {**item, "call_id": f"{run_id}/{item['call_id']}",
         **({"parent_call_id": f"{run_id}/{item['parent_call_id']}"} if item.get("parent_call_id") else {})}
        for item in evidence.runtime_child_evidence_from_state(tool_evidence_state or {})
        if isinstance(item, dict) and isinstance(item.get("call_id"), str)
    ]


def _publish_delegated_child_ledger(run_id: str, result: Any) -> bool:
    """Hand the child's whole ledger to the calling tool call (host channel).

    The child's receipts and effect states reach the parent ledger
    structurally, whatever the string result goes through on its way to the
    parent model. Bookkeeping only: a ledger bug never turns a delegation
    that ran into a reported failure. True only when a calling tool call
    received the child's ledger (a listening sink, a ledger to hand over, no
    error).
    """
    from packages.core.ai.runtime import tool_evidence as evidence

    state = getattr(result, "tool_evidence", None)
    try:
        published = evidence.runtime_publish_child_evidence(runtime_child_ledger_items(run_id, state))
    except Exception:
        logger.warning("Delegated child ledger hand-off failed: run=%s", run_id, exc_info=True)
        return False
    return bool(published) and isinstance(state, dict)


def _delegated_pause_result(pause_payload: dict[str, Any], *, published: bool) -> str:
    """A child's pause: the host carrier only when its ledger reached the parent."""

    from packages.core.ai.runtime.tool_results import RuntimeDelegatedPauseToolResult

    raw = _dumps(pause_payload)
    return RuntimeDelegatedPauseToolResult(raw) if published else raw


def _delegated_hitl_result(result: Any) -> dict[str, Any] | None:
    from packages.core.constants.hitl_envelope import parse_hitl_envelope

    return parse_hitl_envelope(result)


def _raw_tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return _clean(function.get("name"))
    return _clean(tool_call.get("name"))


def _raw_tool_call_arguments(tool_call: dict[str, Any]) -> Any:
    function = tool_call.get("function")
    return function.get("arguments") if isinstance(function, dict) else tool_call.get("arguments")


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    """The tool a history call really invoked (looking through call_tool)."""
    from packages.core.ai.runtime.tool_gateway import runtime_tool_call_target

    return _clean(runtime_tool_call_target(_raw_tool_call_name(tool_call), _raw_tool_call_arguments(tool_call))[0])


def _tool_call_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    from packages.core.ai.runtime.tool_gateway import runtime_tool_call_target

    value = runtime_tool_call_target(_raw_tool_call_name(tool_call), _raw_tool_call_arguments(tool_call))[1]
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


_PROVIDER_DELIVERY_RECEIPT_FIELDS = (
    "message_id",
    "tweet_id",
    "tweet_url",
    "post_id",
    "post_url",
    "share_url",
    "urn",
    "post_urn",
    "published_at",
    "sent_at",
)
_ACTION_POSTCONDITION_FLAGS = (
    "saved",
    "created",
    "updated",
    "deleted",
)
_ACTION_POSTCONDITION_REFERENCE_FIELDS = (
    "document_id",
    "path",
    "fs_path",
    "file_path",
    "saved_to",
    "local_path",
    "artifact_path",
    "output_path",
)


def _delivery_payload_views(
    payload: dict[str, Any],
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Flatten provider result envelopes without inspecting echoed inputs."""

    if _depth > 6:
        return []
    seen = _seen if _seen is not None else set()
    marker = id(payload)
    if marker in seen:
        return []
    seen.add(marker)
    views = [payload]
    for key in (
        "data",
        "result",
        "response",
        "payload",
        "tweet",
        "post",
        "publication_receipt",
        "postcondition",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            views.extend(
                _delivery_payload_views(value, _depth=_depth + 1, _seen=seen)
            )
    content = payload.get("content")
    if isinstance(content, list):
        from packages.core.dispatcher.output_coercion import (
            parse_json_from_text_for_schema,
        )

        for block in content:
            if not isinstance(block, dict):
                continue
            parsed = parse_json_from_text_for_schema(str(block.get("text") or ""))
            if isinstance(parsed, dict):
                views.extend(
                    _delivery_payload_views(
                        parsed,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
                )
    return views


def _native_delivery_receipt_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract provider-native delivery identifiers from known result envelopes."""

    fields: dict[str, Any] = {}
    for view in _delivery_payload_views(payload):
        for key in _PROVIDER_DELIVERY_RECEIPT_FIELDS:
            value = view.get(key)
            if value not in (None, ""):
                fields.setdefault(key, value)
        urn = view.get("id")
        if isinstance(urn, str) and urn.startswith("urn:li:"):
            fields.setdefault("urn", urn)
        history = view.get("edit_history_tweet_ids")
        if isinstance(history, list):
            tweet_id = next(
                (str(value).strip() for value in history if str(value).strip()),
                "",
            )
            if tweet_id:
                fields.setdefault("tweet_id", tweet_id)
                fields.setdefault("tweet_url", f"https://x.com/i/web/status/{tweet_id}")
        if (
            view.get("created_at") not in (None, "")
            and any(
                key in fields
                for key in (
                    "message_id",
                    "tweet_id",
                    "post_id",
                    "urn",
                    "post_urn",
                )
            )
        ):
            fields.setdefault("published_at", view["created_at"])
    return fields


def _provider_delivery_receipt(payload: dict[str, Any]) -> bool:
    """Whether a provider returned native evidence of send/publish delivery.

    A generic ``id`` is deliberately excluded: asynchronous job, upload, and
    request APIs all return one before the requested side effect is complete.
    """

    return bool(_native_delivery_receipt_fields(payload))


def _explicit_action_postcondition(payload: dict[str, Any]) -> bool:
    """Whether a write returned a concrete, non-generic success signal."""

    return (
        payload.get("ok") is True
        or any(payload.get(key) is True for key in _ACTION_POSTCONDITION_FLAGS)
        or any(
            payload.get(key) not in (None, "")
            for key in _ACTION_POSTCONDITION_REFERENCE_FIELDS
        )
    )


def _typed_delivery_postcondition(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return one validated, explicitly verified publication postcondition."""

    from packages.core.services.workflow_publication_receipts import (
        PUBLICATION_RECEIPT_SCHEMA,
        PublicationReceiptError,
        normalize_publication_receipt,
    )

    candidates: list[Any] = _delivery_payload_views(payload)
    for candidate in candidates:
        if (
            not isinstance(candidate, dict)
            or candidate.get("schema_version") != PUBLICATION_RECEIPT_SCHEMA
        ):
            continue
        try:
            return normalize_publication_receipt(candidate, require_verified=True)
        except PublicationReceiptError:
            continue
    return None


def _verified_terminal_delivery_payload(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a flattened delivery receipt only for terminal verified results."""

    views = _delivery_payload_views(payload)
    dispositions = [
        ToolResultDispositionFactory.from_payload(view)
        for view in views
    ]
    if ToolResultDisposition.TERMINAL_FAILURE in dispositions:
        return None
    if any(
        disposition in {
            ToolResultDisposition.NONTERMINAL,
            ToolResultDisposition.UNKNOWN,
        }
        for disposition in dispositions
    ):
        return None

    native_fields = _native_delivery_receipt_fields(payload)
    typed_postcondition = _typed_delivery_postcondition(payload)
    if not native_fields and typed_postcondition is None:
        return None
    has_explicit_status = any(_clean(view.get("status")) for view in views)
    if (
        has_explicit_status
        and ToolResultDisposition.TERMINAL_SUCCESS not in dispositions
    ):
        return None

    flattened = dict(payload)
    flattened.update(native_fields)
    if typed_postcondition is not None:
        flattened["publication_receipt"] = typed_postcondition
        flattened.setdefault("platform", typed_postcondition.get("platform"))
        flattened.setdefault("post_id", typed_postcondition.get("external_id"))
        flattened.setdefault("post_url", typed_postcondition.get("published_url"))
        flattened.setdefault("published_at", typed_postcondition.get("published_at"))
    flattened["delivery_confirmed"] = True
    return flattened


def _tool_result_canonical_sha256(raw_result: str) -> str | None:
    """Fingerprint complete JSON without trusting ambiguous parser inputs."""

    if not isinstance(raw_result, str) or len(raw_result) > 1_048_576:
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON member")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(
            raw_result, object_pairs_hook=unique_object, parse_constant=reject_constant,
        )
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def _successful_tool_payloads(
    messages: list[dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    payloads: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    from packages.core.ai.runtime.streams import runtime_tool_call_error
    from packages.core.constants.hitl_envelope import parse_hitl_envelope
    from packages.core.dispatcher.output_coercion import parse_json_from_text_for_schema

    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = _clean(call.get("id"))
                if call_id:
                    calls[call_id] = (_tool_call_name(call), _tool_call_arguments(call))
            continue
        if message.get("role") != "tool":
            continue
        raw_result = str(message.get("content") or "")
        if (
            runtime_tool_call_error(raw_result) is not None
            or parse_hitl_envelope(raw_result) is not None
        ):
            continue
        parsed = parse_json_from_text_for_schema(raw_result)
        payload = dict(parsed) if isinstance(parsed, dict) else {
            "_runtime_non_json_result": True,
        }
        disposition = ToolResultDispositionFactory.from_payload(payload)
        if disposition is ToolResultDisposition.TERMINAL_FAILURE:
            continue
        typed_postcondition = _typed_delivery_postcondition(payload)
        if typed_postcondition is not None:
            payload["_runtime_typed_postcondition"] = typed_postcondition
        verified_delivery = _verified_terminal_delivery_payload(payload)
        if verified_delivery is not None:
            payload["_runtime_verified_delivery"] = verified_delivery
        # Status handling is a whitelist, not "anything except pending".
        # When a provider omits status entirely, only native delivery evidence
        # or a validated typed postcondition can prove a terminal return.
        payload["_runtime_terminal_result"] = (
            disposition is ToolResultDisposition.TERMINAL_SUCCESS
            or (
                disposition is ToolResultDisposition.UNSPECIFIED
                and (
                    _provider_delivery_receipt(payload)
                    or typed_postcondition is not None
                    or _explicit_action_postcondition(payload)
                )
            )
        )
        payload["_runtime_result_sha256"] = sha256(
            raw_result.encode("utf-8")
        ).hexdigest()
        payload["_runtime_result_canonical_sha256"] = _tool_result_canonical_sha256(raw_result)
        tool_call_id = _clean(message.get("tool_call_id"))
        name, arguments = calls.get(tool_call_id, ("", {}))
        payloads.append((tool_call_id, name, arguments, payload))
    return payloads


def _delegation_effect_intent(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str | None,
    tool_call_id: str | None = None,
    classification: "RuntimeToolClassification | None" = None,
    approval_request_id: str | None = None,
) -> dict[str, Any] | None:
    """Describe a possibly mutating call before it crosses external I/O.

    A classifier failure is treated as ambiguous rather than read-only.  That
    is intentionally conservative: replay safety is more important than an
    automatic retry when Runtime cannot establish the effect class.
    """

    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    classification_error = False
    if classification is None:
        try:
            classification = classify_runtime_tool(
                tool_name,
                arguments,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001 - ambiguity must be durable and fail closed
            classification = None
            classification_error = True
    if classification is not None and classification.effect in {
        RuntimeToolEffect.READ_ONLY,
        RuntimeToolEffect.ORCHESTRATOR,
        RuntimeToolEffect.CONTROL,
    }:
        return None

    action = classification.action if classification is not None else None
    authorization = (
        classification.authorization if classification is not None else None
    )
    return ToolEffectEvidenceFactory.effect(
        ToolEffectOutcome.PENDING,
        {
            "tool": tool_name,
            "tool_call_id": _clean(tool_call_id) or None,
            "arguments_hash": _tool_arguments_hash(arguments),
            # Only the exact unified-approval consume path can populate this.
            # ``arguments.approval_token`` is model-visible replay data, not
            # receipt authority.
            "approval_request_id": _clean(approval_request_id) or None,
            "action_key": (
                _clean(getattr(authorization, "action_key", None)) or None
            ),
            "capability_id": (
                _clean(getattr(authorization, "capability_id", None)) or None
            ),
            "operation": _clean(getattr(action, "operation", None)).lower() or None,
            "classification": (
                "unknown"
                if classification_error
                or (
                    classification is not None
                    and classification.effect is RuntimeToolEffect.UNKNOWN
                )
                else "action"
            ),
            "intent_state": ToolEffectIntentState.STARTED.value,
        },
    )


def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(value)
    return output


def _document_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        document_id = _clean(value.get("document_id"))
        if document_id:
            found.append(document_id)
        for item in value.values():
            found.extend(_document_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_document_ids(item))
    return list(dict.fromkeys(found))


def _delegation_receipts(
    messages: list[dict[str, Any]],
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str | None,
    authoritative_intents: list[dict[str, Any]] | None = None,
    owner_plan_id: str | None = None,
    owner_step_id: str | None = None,
    effect_generation: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return captured effect, artifact, and Knowledge references.

    Evidence is derived only from successful tool results plus Runtime's
    deterministic tool-effect classifier. Assistant prose never counts.
    """

    from packages.core.ai.runtime.artifacts import runtime_extract_artifact_urls_from_tool_result

    evidence: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    knowledge: list[dict[str, Any]] = []
    scoped_rescan = bool(owner_plan_id and owner_step_id and effect_generation is not None)

    def matches_current_scope(ref: dict[str, Any]) -> bool:
        fields = ref.get("fields") if isinstance(ref.get("fields"), dict) else {}
        identity = ToolEffectEvidenceFactory.identity(ref)
        return bool(
            fields.get("effect_id") and ToolEffectEvidenceFactory.has_valid_effect_id(ref)
            and identity.owner_plan_id == owner_plan_id
            and identity.owner_step_id == owner_step_id
            and identity.effect_generation == str(effect_generation)
            and not identity.coordinator_child_step_id
            and identity.tool and identity.tool_call_id and identity.arguments_hash
        )

    intent_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    settled_index: set[tuple[str, str, str, str]] = set()
    for ref in authoritative_intents or []:
        if not isinstance(ref, dict):
            continue
        fields = ref.get("fields")
        if (
            ref.get("kind") == "tool_effect"
            and ref.get("effect") == "pending"
            and isinstance(fields, dict)
        ):
            intent_index.setdefault((
                _clean(fields.get("tool")),
                _clean(fields.get("arguments_hash")),
                _clean(fields.get("tool_call_id")),
            ), []).append(ref)
        elif (
            scoped_rescan
            and ref.get("kind") == "tool_effect"
            and ref.get("effect") in {
                ToolEffectOutcome.ACTION.value, ToolEffectOutcome.PUBLISH.value,
            }
            and isinstance(fields, dict)
            and fields.get("intent_state") == ToolEffectIntentState.RETURNED.value
            and matches_current_scope(ref)
            and fields.get("result_sha256")
        ):
            identity = ToolEffectEvidenceFactory.identity(ref)
            for digest in (fields["result_sha256"], fields.get("result_canonical_sha256")):
                if digest:
                    settled_index.add((
                        identity.tool, identity.arguments_hash, identity.tool_call_id,
                        str(digest),
                    ))
    # Do not infer "handler was not invoked" from a tool-provided __hitl__
    # payload. Runtime approval/provider preflight returns before the pre-I/O
    # intent hook, so trusted deferrals have no pending marker to settle. A
    # handler-level lookalike arrives after that hook and may follow a real
    # external write; keeping its pending marker is the fail-closed choice.
    for tool_call_id, tool_name, arguments, payload in _successful_tool_payloads(messages):
        settled_action = False
        intent_candidates = intent_index.get((
            _clean(tool_name),
            _tool_arguments_hash(arguments),
            _clean(tool_call_id),
        ), [])
        if len(intent_candidates) > 1:
            # The Runtime transcript does not carry Plan/Step ownership.  A
            # shared legacy call id therefore cannot identify which concurrent
            # intent returned. Keep every candidate pending and fail closed.
            continue
        intent = intent_candidates[0] if intent_candidates else None
        if intent is not None and scoped_rescan and not matches_current_scope(intent):
            # A foreign or weak pending marker sharing a transcript call id is
            # ambiguous, even if the current Step also has a returned receipt.
            # Do not settle it, fall back to classification, or erase the wait.
            continue
        if intent is None:
            if any((
                _clean(tool_name), _tool_arguments_hash(arguments), _clean(tool_call_id), _clean(digest),
            ) in settled_index for digest in (
                payload.get("_runtime_result_sha256"), payload.get("_runtime_result_canonical_sha256"),
            ) if digest):
                # Completed-round transcripts include historical results. An
                # exact current-Step receipt already owns this result; asking
                # the classifier again could turn yesterday's create into a
                # modify merely because its output now exists. Pending intents
                # above retain priority, and weak/foreign receipts never dedupe.
                continue
            intent = _delegation_effect_intent(
                tool_name,
                arguments,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
        if intent is not None:
            intent_fields = (
                intent.get("fields")
                if isinstance(intent.get("fields"), dict)
                else {}
            )
            operation = _clean(intent_fields.get("operation")).lower()
            classification_unknown = intent_fields.get("classification") == "unknown"
            receipt_fields = {
                key: payload[key]
                for key in (
                    "id", *_PROVIDER_DELIVERY_RECEIPT_FIELDS,
                    *_ACTION_POSTCONDITION_FLAGS,
                    *_ACTION_POSTCONDITION_REFERENCE_FIELDS,
                    "platform", "status",
                )
                if payload.get(key) not in (None, "")
            }
            terminal_result = payload.get("_runtime_terminal_result") is True
            typed_postcondition = payload.get("_runtime_typed_postcondition")
            verified_delivery = payload.get("_runtime_verified_delivery")
            delivery_receipt = isinstance(verified_delivery, dict)
            # UNKNOWN means Runtime could not prove whether this call is safe
            # to replay or what result would settle it. Keep the pre-I/O marker
            # unresolved and force reconciliation; a returned string is not
            # enough authority to silently classify the effect after the fact.
            result_disposition = ToolResultDispositionFactory.from_payload(payload)
            if operation in {"publish", "send"}:
                effect_settled = terminal_result and delivery_receipt
            else:
                effect_settled = terminal_result and (
                    result_disposition is ToolResultDisposition.TERMINAL_SUCCESS
                    or _explicit_action_postcondition(payload)
                    or isinstance(typed_postcondition, dict)
                    or _provider_delivery_receipt(payload)
                )
            if not classification_unknown and effect_settled:
                settled_action = True
                evidence.append(ToolEffectEvidenceFactory.effect(
                    # A publish/send expectation needs a terminal native receipt,
                    # not merely a handler return such as {status: "queued"} or
                    # {ok: true}. Other known actions still settle replay safety.
                    (
                        ToolEffectOutcome.PUBLISH
                        if operation in {"publish", "send"}
                        and terminal_result
                        and delivery_receipt
                        else ToolEffectOutcome.ACTION
                    ),
                    {
                        **intent_fields,
                        "intent_state": ToolEffectIntentState.RETURNED.value,
                        "result_sha256": _clean(
                            payload.get("_runtime_result_sha256")
                        ) or None,
                        "result_canonical_sha256": payload.get("_runtime_result_canonical_sha256"),
                        "delivery_confirmed": bool(
                            operation in {"publish", "send"}
                            and terminal_result
                            and delivery_receipt
                        ),
                        **(
                            {"postcondition": typed_postcondition}
                            if isinstance(typed_postcondition, dict)
                            else {}
                        ),
                        **(
                            {
                                key: value
                                for key, value in verified_delivery.items()
                                if key in (
                                    *_PROVIDER_DELIVERY_RECEIPT_FIELDS,
                                    "delivery_confirmed",
                                    "publication_receipt",
                                )
                                and value not in (None, "")
                            }
                            if isinstance(verified_delivery, dict)
                            else {}
                        ),
                        **receipt_fields,
                    },
                ))

        if (
            payload.get("_runtime_terminal_result") is not True
            or not settled_action
        ):
            # Async/ambiguous results are not artifact or Knowledge receipts
            # either. Read-only results can point at pre-existing documents,
            # while a job URL/provisional id can disappear when the provider
            # later rejects the operation; neither proves this step delivered
            # a new artifact.
            continue
        for artifact_ref in runtime_extract_artifact_urls_from_tool_result(
            payload, structured_only=True,
        ):
            text = _clean(artifact_ref)
            if not text:
                continue
            ref = {
                "type": "artifact",
                "source": tool_name or "tool_result",
                ("url" if text.startswith(("http://", "https://")) else "fs_path"): text,
            }
            artifacts.append(ref)
            evidence.append({"kind": "artifact", **ref})
        for document_id in _document_ids(payload):
            ref = {
                "type": "knowledge_document",
                "source": tool_name or "tool_result",
                "document_id": document_id,
            }
            knowledge.append(ref)
            artifacts.append(ref)
            evidence.append({"kind": "artifact", **ref})
    return _dedupe_dicts(evidence), _dedupe_dicts(artifacts), _dedupe_dicts(knowledge)


def _typed_delegation_result(content: str, output_schema: dict[str, Any] | None) -> Any:
    from packages.core.dispatcher.output_coercion import parse_json_from_text_for_schema

    parsed = parse_json_from_text_for_schema(content, schema=output_schema)
    if parsed is not None or content.strip() == "null":
        return parsed
    return {"text": content}


def _delegation_schema_error(
    value: Any,
    output_schema: dict[str, Any] | None,
) -> str | None:
    """Final fail-closed validation, including the max-rounds summary path."""

    if output_schema is None:
        return None
    try:
        from packages.core.contracts.json_schema import SchemaContractValidatorFactory

        errors = sorted(
            SchemaContractValidatorFactory.build(output_schema).iter_errors(value),
            key=lambda item: [str(part) for part in item.absolute_path],
        )
    except Exception as exc:  # noqa: BLE001 — an unreadable contract cannot succeed
        return f"$: output schema validation could not run ({exc})"
    if not errors:
        return None
    error = errors[0]
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return f"{path}: {error.message}"


async def _load_delegation_resume(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
    conversation_id: str | None,
    approval_token: str,
    expected_task_id: str | None = None,
    expected_step_id: str | None = None,
    expected_idempotency_key: str | None = None,
    expected_service_key: str | None = None,
    expected_subscription_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
    from packages.core.models.hitl_request import HitlRequest

    if not conversation_id or not user_id:
        return None, "Delegated approval requires its original conversation and user."
    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == approval_token,
            HitlRequest.entity_id == entity_id,
            HitlRequest.workspace_id == workspace_id,
            HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL,
            HitlRequest.origin_conversation_id == conversation_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if request is None:
        return None, "Delegated approval was not found for this conversation."
    if request.status != ApprovalStatus.GRANTED or request.consumed_at is not None:
        return None, "Delegated approval is not approved or was already used."

    context = dict(request.context or {})
    resume = context.get("delegation_resume")
    if not isinstance(resume, dict) or resume.get("version") != 1:
        return None, "Delegated approval continuation is unavailable."
    expected = {
        "approval_token": approval_token,
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    if any(str(resume.get(key) or "") != str(value or "") for key, value in expected.items()):
        return None, "Delegated approval continuation does not match this runtime scope."
    if str(context.get("requested_by") or "") != user_id:
        return None, "Delegated approval continuation belongs to another user."
    # ``user_id`` is the original execution/requester scope and must stay
    # stable, but the human who grants a Workspace approval may legitimately
    # be another authorized participant. ``grant_approval`` already enforces
    # that actor's authority; requiring approver == requester here stranded
    # otherwise valid team approvals.

    continuation = context.get("continuation")
    tool_name = _clean(context.get("tool"))
    if (
        not isinstance(continuation, dict)
        or continuation.get("version") != 1
        or _clean(continuation.get("tool")) != tool_name
        or _clean(resume.get("tool_name")) != tool_name
    ):
        return None, "Delegated approval continuation does not match the approved tool."
    tool_arguments = continuation.get("arguments")
    if not isinstance(tool_arguments, dict):
        return None, "Delegated approval tool arguments are unavailable."
    if not _clean(resume.get("prompt")):
        return None, "Delegated approval prompt is unavailable."
    if not all(
        _clean(resume.get(key))
        for key in ("agent_subscription_id", "agent_id", "service_key")
    ):
        return None, "Delegated approval service identity is unavailable."
    expected_scope = {
        "task_id": expected_task_id,
        "step_id": expected_step_id,
        "delegation_idempotency_key": expected_idempotency_key,
        "service_key": expected_service_key,
        "agent_subscription_id": expected_subscription_id,
    }
    if any(
        expected is not None
        and str(resume.get(key) or "") != str(expected)
        for key, expected in expected_scope.items()
    ):
        return None, "Delegated approval continuation belongs to another child run."
    return {
        **resume,
        "tool_name": tool_name,
        "tool_arguments": dict(tool_arguments),
    }, None


async def _persist_delegation_resume(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
    conversation_id: str | None,
    sub: Any,
    delegation_session_id: str,
    prompt: str,
    max_rounds: int,
    delegation_idempotency_key: str | None,
    task_id: str | None,
    step_id: str | None,
    tool_name: str,
    tool_arguments: dict[str, Any],
    hitl: dict[str, Any],
    tool_call_id: str | None = None,
    remaining_tool_calls: list[dict[str, Any]] | None = None,
    disable_followup_tools: bool = False,
    tool_continuations: dict[str, dict[str, Any]] | None = None,
    parent_step_id: str | None = None,
    parent_lease_id: str | None = None,
    parent_execution_claim_id: str | None = None,
) -> bool:
    from packages.core.ai.runtime.approval_messages import (
        RuntimeApprovalContinuationError,
        runtime_approval_continuation,
    )
    from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus
    from packages.core.constants.execution import (
        COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
        COORDINATOR_CHILD_ITEM_PARAM_KEY,
        COORDINATOR_CHILD_PARAM_KEY,
        COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
        COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY,
        COORDINATOR_PARAM_KEY,
        COORDINATOR_PARENT_STEP_PARAM_KEY,
        ExecutionStepStatus,
        WorkLeaseStatus,
    )
    from packages.core.database import async_session
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.worker import WorkLease

    approval_token = _clean(hitl.get("approval_token"))
    hitl_request = hitl.get("hitl")
    operation = hitl.get("operation")
    if (
        not approval_token
        or not conversation_id
        or not user_id
        or not isinstance(hitl_request, dict)
        or _clean(hitl_request.get("id")) != approval_token
        or _clean(hitl_request.get("tool")) != tool_name
        or not isinstance(operation, dict)
        or _clean(operation.get("tool")) != tool_name
    ):
        return False
    try:
        continuation = runtime_approval_continuation(tool_name, tool_arguments)
    except RuntimeApprovalContinuationError:
        return False

    async with async_session() as db:
        # A coordinator child creates its Runtime approval before the parent
        # dispatcher can mint the canonical lease card.  Mark that request as
        # coordinator-owned in this earlier transaction, so every generic
        # Runtime/Human Queue surface fails closed throughout the gap.  The
        # outer request id is deliberately empty until lease_needs_human fills
        # it while holding the nested row lock.
        # A Task-linked interactive Chat may carry ``task_id`` without being a
        # materialized coordinator child.  Only the server-supplied child step
        # or stable child idempotency key opts into this bridge.
        coordinator_scope = step_id is not None or delegation_idempotency_key is not None
        reverse_placeholder: dict[str, Any] | None = None
        if coordinator_scope:
            child_id = _clean(step_id)
            expected_task_id = _clean(task_id)
            expected_idempotency_key = _clean(delegation_idempotency_key)
            expected_parent_step_id = _clean(parent_step_id)
            expected_parent_lease_id = _clean(parent_lease_id)
            expected_parent_execution_claim_id = _clean(
                parent_execution_claim_id
            )
            if not all((
                child_id,
                expected_task_id,
                expected_idempotency_key,
                expected_parent_step_id,
                expected_parent_lease_id,
                expected_parent_execution_claim_id,
            )):
                return False

            # Discover ids without taking locks, then reacquire in the shared
            # lease -> parent -> child order and revalidate every value.  The
            # discovery reads grant no authority and are never used after the
            # locked checks below.
            child_hint = await db.get(ExecutionStep, child_id)
            child_hint_params = (
                child_hint.params
                if child_hint is not None and isinstance(child_hint.params, dict)
                else {}
            )
            parent_key = _clean(
                child_hint_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY)
            )
            if child_hint is None or not parent_key:
                return False
            parent_hint = (await db.execute(
                select(ExecutionStep).where(
                    ExecutionStep.plan_id == child_hint.plan_id,
                    ExecutionStep.step_key == parent_key,
                )
            )).scalar_one_or_none()
            if (
                parent_hint is None
                or parent_hint.id != expected_parent_step_id
                or _clean(parent_hint.current_lease_id) != expected_parent_lease_id
            ):
                return False

            lease = (await db.execute(
                select(WorkLease)
                .where(WorkLease.id == expected_parent_lease_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            parent = (await db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.id == parent_hint.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            child = (await db.execute(
                select(ExecutionStep)
                .where(ExecutionStep.id == child_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
            plan = (
                await db.execute(
                    select(ExecutionPlan).where(
                        ExecutionPlan.id == child_hint.plan_id,
                    )
                )
            ).scalar_one_or_none()
            child_params = (
                child.params
                if child is not None and isinstance(child.params, dict)
                else {}
            )
            parent_params = (
                parent.params
                if parent is not None and isinstance(parent.params, dict)
                else {}
            )
            now = datetime.now(timezone.utc)
            if (
                lease is None
                or parent is None
                or child is None
                or plan is None
                or lease.status != WorkLeaseStatus.ACTIVE.value
                or not _clean(lease.execution_claim_id)
                or _clean(lease.execution_claim_id)
                != expected_parent_execution_claim_id
                or lease.lease_until is None
                or lease.lease_until <= now
                or lease.step_id != parent.id
                or lease.plan_id != parent.plan_id
                or lease.entity_id != entity_id
                or lease.workspace_id != workspace_id
                or parent.current_lease_id != lease.id
                or parent.step_status != ExecutionStepStatus.RUNNING.value
                or parent.entity_id != entity_id
                or parent.workspace_id != workspace_id
                or parent.plan_id != plan.id
                or parent.params is None
                or parent_params.get(COORDINATOR_PARAM_KEY) is not True
                or child.step_status != ExecutionStepStatus.RUNNING.value
                or child.entity_id != entity_id
                or child.workspace_id != workspace_id
                or child.plan_id != plan.id
                or child.service_key != sub.service_key
                or child_params.get(COORDINATOR_CHILD_PARAM_KEY) is not True
                or child_params.get(COORDINATOR_PARENT_STEP_PARAM_KEY)
                != parent.step_key
                or not _clean(child_params.get(COORDINATOR_CHILD_ITEM_PARAM_KEY))
                or _clean(
                    child_params.get(COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY)
                )
                != expected_idempotency_key
                or _clean(plan.task_id) != expected_task_id
            ):
                return False
            reverse_placeholder = {
                "version": COORDINATOR_NESTED_APPROVAL_CONTEXT_VERSION,
                "request_id": None,
                "nested_request_id": approval_token,
                "child_step_id": child.id,
                "parent_step_id": parent.id,
                "parent_lease_id": lease.id,
                "parent_execution_claim_id": expected_parent_execution_claim_id,
                "plan_id": plan.id,
                "task_id": expected_task_id,
                "child_step_key": child.step_key,
                "service_key": child.service_key,
                "idempotency_key": expected_idempotency_key,
            }

        request = (await db.execute(
            select(HitlRequest).where(
                HitlRequest.id == approval_token,
                HitlRequest.entity_id == entity_id,
                HitlRequest.workspace_id == workspace_id,
                HitlRequest.origin_kind == ApprovalOriginKind.TOOL_CALL,
                HitlRequest.origin_conversation_id == conversation_id,
            ).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if request is None:
            return False
        context = dict(request.context or {})
        if (
            request.status != ApprovalStatus.PENDING
            or request.consumed_at is not None
            or _clean(context.get("tool")) != tool_name
            or context.get("continuation") != continuation
            or str(context.get("requested_by") or "") != user_id
        ):
            return False
        context["delegation_resume"] = {
            "version": 1,
            "approval_token": approval_token,
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "agent_subscription_id": sub.id,
            "delegation_session_id": delegation_session_id,
            "agent_id": sub.agent_id,
            "service_key": sub.service_key,
            "prompt": prompt,
            "max_rounds": max_rounds,
            "delegation_idempotency_key": delegation_idempotency_key,
            "task_id": task_id,
            "step_id": step_id,
            "tool_name": tool_name,
            "tool_call_id": _clean(tool_call_id) or None,
            "remaining_tool_calls": [
                dict(call)
                for call in (remaining_tool_calls or [])
                if isinstance(call, dict)
            ],
            "disable_followup_tools": bool(disable_followup_tools),
            "tool_continuations": {
                str(key): dict(value)
                for key, value in (tool_continuations or {}).items()
                if isinstance(value, dict)
            },
        }
        if reverse_placeholder is not None:
            existing_reverse = context.get(COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY)
            if existing_reverse not in (None, reverse_placeholder):
                return False
            context[COORDINATOR_OUTER_APPROVAL_CONTEXT_KEY] = reverse_placeholder
        request.context = context
        if reverse_placeholder is not None:
            # Persist the child's waiting state in the same transaction as the
            # hidden reverse placeholder. A crash can now leave either neither
            # object or both objects, never an invisible pending approval
            # attached to a RUNNING child that a successor would replay.
            child.step_status = ExecutionStepStatus.WAITING_HUMAN.value
            child.result = dict(hitl)
            child.error = None
            child.finished_at = None
        await db.commit()
    return True


def _delegation_resume_forced_calls(
    resume: dict[str, Any],
    approval_token: str,
) -> list[dict[str, Any]]:
    """Rebuild the approved call and suffix with Runtime continuations intact."""

    continuation_key = "__manor_tool_continuation"
    continuations = (
        resume.get("tool_continuations")
        if isinstance(resume.get("tool_continuations"), dict)
        else {}
    )
    disable_followup_tools = bool(resume.get("disable_followup_tools"))

    def forced_call(
        *,
        name: str,
        arguments: dict[str, Any],
        prior_call_id: str | None,
    ) -> dict[str, Any]:
        rebuilt_arguments = dict(arguments)
        continuation = continuations.get(str(prior_call_id or ""))
        if isinstance(continuation, dict):
            # AgenticLoop removes this Runtime-only field before model context
            # and binds it to the newly materialized stable call id.
            rebuilt_arguments[continuation_key] = dict(continuation)
        return {
            "name": name,
            "arguments": rebuilt_arguments,
            "disable_followup_tools": disable_followup_tools,
        }

    calls = [
        forced_call(
            name=str(resume["tool_name"]),
            arguments={
                **dict(resume["tool_arguments"]),
                "approval_token": approval_token,
            },
            prior_call_id=_clean(resume.get("tool_call_id")) or None,
        )
    ]
    for raw_call in resume.get("remaining_tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        name = _clean(raw_call.get("name"))
        if not name:
            continue
        arguments = (
            dict(raw_call.get("arguments") or {})
            if isinstance(raw_call.get("arguments"), dict)
            else {}
        )
        calls.append(
            forced_call(
                name=name,
                arguments=arguments,
                prior_call_id=_clean(raw_call.get("id")) or None,
            )
        )
    return calls


async def _workspace_service_options(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    from packages.core.models.workspace import Agent, AgentSubscription

    rows = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
        .order_by(
            AgentSubscription.service_key.asc().nulls_last(),
            Agent.name.asc(),
        )
    )).all()
    return [
        {
            "agent_subscription_id": sub.id,
            "service_key": sub.service_key,
            "agent_id": sub.agent_id,
            "agent_name": agent.name,
        }
        for sub, agent in rows
    ]


async def _resolve_workspace_service_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    raw_params: dict[str, Any],
) -> tuple[Any | None, Any | None, list[dict[str, Any]], str | None]:
    from packages.core.models.workspace import Agent, AgentSubscription

    available = await _workspace_service_options(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    subscription_id = _clean(
        raw_params.get("agent_subscription_id") or raw_params.get("subscription_id")
    )
    service_key = _clean(
        raw_params.get("service_key")
        or raw_params.get("target_service_key")
        or raw_params.get("service")
    )
    agent_id = _clean(raw_params.get("agent_id"))

    filters = [
        AgentSubscription.entity_id == entity_id,
        AgentSubscription.workspace_id == workspace_id,
        AgentSubscription.status == "active",
    ]
    selector = ""
    if subscription_id:
        filters.append(AgentSubscription.id == subscription_id)
        selector = "agent_subscription_id"
    elif service_key:
        filters.append(AgentSubscription.service_key == service_key)
        selector = "service_key"
    elif agent_id:
        filters.append(AgentSubscription.agent_id == agent_id)
        selector = "agent_id"
    else:
        return (
            None,
            None,
            available,
            "service_key, agent_subscription_id, or agent_id is required",
        )

    rows = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            *filters,
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
    )).all()
    if not rows:
        return None, None, available, f"No active workspace service agent matched {selector}."
    if len(rows) > 1:
        return None, None, available, (
            f"{selector} matched multiple active workspace services; pass service_key "
            "or agent_subscription_id to disambiguate."
        )
    sub, agent = rows[0]
    return sub, agent, available, None


DELEGATE_TO_AGENT_TOOL_NAME = "delegate_to_agent"
# Manor -> agent -> one more agent. Deeper chains are refused: each level is a
# blocking nested loop, and a cycle would never return.
MAX_DELEGATION_DEPTH = 2
# One delegate_to_agent call may hand tasks to up to this many agents at once.
MAX_PARALLEL_DELEGATIONS = 20
PARALLEL_DELEGATION_STAGGER_SECONDS = 0.2
_delegation_depth: "ContextVar[int]" = ContextVar("workspace_delegation_depth", default=0)


def _target_label(sub: Any, agent: Any) -> str:
    return _clean(getattr(sub, "name", None)) or _clean(getattr(agent, "name", None)) or "Agent"


async def runtime_workspace_delegation_targets(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    exclude_agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every agent in the Workspace another agent can hand work to.

    Labels are the names people see (deployment name, else agent name), made
    unique with the service key or a short id. Manor itself and the calling
    agent are not targets.
    """
    from packages.core.constants.agents import is_master_agent
    from packages.core.models.workspace import Agent, AgentSubscription

    rows = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
            Agent.status == "active",
            Agent.deleted_at.is_(None),
        )
        .order_by(Agent.name.asc(), AgentSubscription.id.asc())
    )).all()
    rows = [
        (sub, agent) for sub, agent in rows
        if not is_master_agent(agent.id) and agent.id != exclude_agent_id
    ]
    counts: dict[str, int] = {}
    for sub, agent in rows:
        counts[_target_label(sub, agent)] = counts.get(_target_label(sub, agent), 0) + 1
    targets = []
    for sub, agent in rows:
        label = _target_label(sub, agent)
        if counts[label] > 1:
            label = f"{label} ({_clean(sub.service_key) or sub.id[-6:]})"
        summary = " ".join(_clean(getattr(agent, "description", None)).split())
        targets.append({
            "label": label,
            "agent_subscription_id": sub.id,
            "agent_id": agent.id,
            "service_key": sub.service_key,
            "summary": summary[:200],
        })
    return targets


def delegate_to_agent_schema(targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The delegate_to_agent tool; with targets, the agent is an enum of them."""
    description = (
        "Hand tasks to other agents in this Workspace and get their results back. "
        "Each agent works with its own tools, skills and integrations and remembers "
        "earlier tasks you gave it in this chat, but it does not see this chat: "
        "state the goal, the inputs, the constraints and any corrections the user "
        "made. Use it when the request belongs to that agent's role.\n"
        f"Tasks in one call run at the same time (up to {MAX_PARALLEL_DELEGATIONS}, "
        "one per agent). Put independent tasks in one call; when a task needs "
        "another task's result, send it in a later call with that result in the task.\n"
        f"All answers of one call share {DELEGATION_TOOL_RESULT_CHARS:,} characters: one agent "
        f"may answer with about {runtime_delegation_answer_budget(1):,}, twenty with about "
        f"{runtime_delegation_answer_budget(20):,} each. Do not ask for more than that; "
        "ask for longer output to be saved as a file. An answer marked 'Cut here' "
        "continues in storage: read it with read_delegation_result before quoting or "
        "using the rest, never reconstruct it."
    )
    agent_property: dict[str, Any] = {
        "type": "string",
        "description": "Name of the agent, exactly as listed.",
    }
    if targets:
        agent_property["enum"] = [target["label"] for target in targets]
        description += "\n\nAgents in this Workspace:\n" + "\n".join(
            f"- {target['label']}" + (f": {target['summary']}" if target["summary"] else "")
            for target in targets
        )
    return {
        "type": "function",
        "function": {
            "name": DELEGATE_TO_AGENT_TOOL_NAME,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_PARALLEL_DELEGATIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": agent_property,
                                "task": {
                                    "type": "string",
                                    "description": "What the agent should do, with everything it needs to know.",
                                },
                                "continue_interrupted": {
                                    "type": "boolean",
                                    "description": "Continue after you have checked the previous turn's effects; this does not repeat it.",
                                },
                            },
                            "required": ["agent", "task"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    }


async def runtime_specialize_delegate_to_agent(
    db: AsyncSession | None,
    tools: list[dict[str, Any]],
    *,
    entity_id: str | None,
    workspace_id: str | None,
    agent_id: str | None,
) -> list[dict[str, Any]]:
    """Name this Workspace's agents in delegate_to_agent, or drop it when there are none.

    The list changes only when the Workspace's agents change, so it does not
    disturb the cached request prefix from turn to turn.
    """
    def name_of(tool: dict[str, Any]) -> str:
        return str((tool.get("function") or {}).get("name") or "")

    if not any(name_of(tool) == DELEGATE_TO_AGENT_TOOL_NAME for tool in tools):
        return tools
    targets = (
        await runtime_workspace_delegation_targets(
            db, entity_id=entity_id, workspace_id=workspace_id, exclude_agent_id=agent_id,
        )
        if db is not None and entity_id and workspace_id
        else []
    )
    if not targets:
        return [tool for tool in tools if name_of(tool) != DELEGATE_TO_AGENT_TOOL_NAME]
    return [
        delegate_to_agent_schema(targets) if name_of(tool) == DELEGATE_TO_AGENT_TOOL_NAME else tool
        for tool in tools
    ]


def runtime_match_delegation_target(targets: list[dict[str, Any]], requested: str) -> dict[str, Any] | None:
    wanted = _clean(requested)
    for match in (
        lambda target: target["label"] == wanted,
        lambda target: target["label"].casefold() == wanted.casefold(),
        lambda target: wanted in {target["agent_subscription_id"], target["agent_id"], _clean(target["service_key"])},
    ):
        found = [target for target in targets if match(target)]
        if len(found) == 1:
            return found[0]
    return None


# The keys one delegate_to_agent task may carry; everything else in a call is
# the runtime's call context.
_DELEGATION_TASK_KEYS = ("agent", "task", "continue_interrupted")
# Selectors that name a Workspace service directly (delegate_service tasks).
_DELEGATION_TARGET_KEYS = (
    "agent_subscription_id", "subscription_id", "service_key", "target_service_key", "service", "agent_id",
)
# Keys a task names for itself; shared call params never override them.
_DELEGATION_ITEM_KEYS = frozenset({
    "tasks", "prompt", "instructions", "instruction", "task", "message", "request",
    "agent_subscription_id", "subscription_id", "service_key", "target_service_key",
    "service", "agent_id", "session_id", "continue_interrupted", "approval_token",
    "delegation_idempotency_key", "answer_max_chars",
})


@dataclass(frozen=True)
class RuntimeDelegationBatch:
    """One delegation call's tasks, checked before anything runs.

    ``items`` are ``{"agent": label, "params": {...}}`` in input order (once
    pinned, also ``agent_id`` and ``service_key`` of the subscription in
    ``params["agent_subscription_id"]``); ``shared`` is the call context
    every task runs with. ``error`` is the
    serialized refusal when the call is rejected: then nothing ran and the
    model can correct the call in place.
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    shared: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def runtime_delegation_tool_arguments(arguments: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """delegate_to_agent's arguments as ``(tasks, shared)``.

    The single-task form from before tasks lists becomes a one-task list,
    each task keeps only its own keys, and ``shared`` carries only the
    runtime call context.
    """
    from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS

    tasks = arguments.get("tasks")
    if not isinstance(tasks, list):
        # The single-task form from before tasks lists.
        tasks = [{key: arguments[key] for key in _DELEGATION_TASK_KEYS if key in arguments}]
    tasks = [
        {key: task[key] for key in _DELEGATION_TASK_KEYS if key in task}
        if isinstance(task, dict) else task
        for task in tasks
    ]
    shared = {key: value for key, value in arguments.items() if key in RUNTIME_TOOL_CONTEXT_KEYS}
    return tasks, shared


async def _runtime_pin_delegation_items(
    *,
    entity_id: str,
    workspace_id: str,
    items: list[dict[str, Any]],
    shared_params: dict[str, Any] | None,
) -> RuntimeDelegationBatch:
    """Pin every item to the exact subscription it names, or refuse the batch.

    A bad name, a repeated agent or an empty task rejects the whole batch
    with no side effects. Each item is told its share of the one tool result
    all answers share.
    """
    from packages.core.database import async_session

    # Each item names its own agent and instructions; shared params carry only
    # the call context.
    shared = {key: value for key, value in (shared_params or {}).items() if key not in _DELEGATION_ITEM_KEYS}
    items = [{**item, "params": dict(item["params"])} for item in items]
    seen: dict[str, str] = {}
    async with async_session() as db:
        for item in items:
            params = {**shared, **item["params"]}
            sub, _agent, available, error = await _resolve_workspace_service_agent(
                db, entity_id=entity_id, workspace_id=workspace_id, raw_params=params,
            )
            if error:
                return RuntimeDelegationBatch(error=_dumps({
                    "error": "workspace_service_agent_not_found",
                    "message": f"{item['agent']}: {error}",
                    "available_services": available,
                    "action_executed": False,
                }))
            if sub.id in seen:
                return RuntimeDelegationBatch(error=_dumps({
                    "error": "duplicate_delegation_target",
                    "message": (
                        f"{item['agent']} appears more than once. Give each agent one task "
                        "per call: combine its work into one task, or send the next one "
                        "after this call returns."
                    ),
                    "action_executed": False,
                }))
            seen[sub.id] = item["agent"]
            # Run exactly the agent checked here, whatever selector named it,
            # and tell it its share of the one tool result all answers share.
            item["params"] = {
                **item["params"], "agent_subscription_id": sub.id,
                "answer_max_chars": runtime_delegation_answer_budget(len(items)),
            }
            # The pinned identity, for hosts that record the child before it
            # runs (the child's own params stay as the caller gave them).
            item["agent_id"] = getattr(sub, "agent_id", None)
            item["service_key"] = getattr(sub, "service_key", None)
            if not _delegation_prompt(params):
                return RuntimeDelegationBatch(error=_dumps({
                    "error": "prompt is required",
                    "message": f"{item['agent']}: the task is empty.",
                    "action_executed": False,
                }))
    return RuntimeDelegationBatch(items=items, shared=shared)


async def runtime_delegation_batch(
    *,
    entity_id: str,
    workspace_id: str,
    tasks: list[Any],
    shared: dict[str, Any],
    caller_agent_id: str | None = None,
    pin_targets: bool = True,
) -> RuntimeDelegationBatch:
    """Check one delegation call's tasks before any agent runs.

    A task names its agent as listed (``agent``) or by subscription id,
    service key or agent id, and carries its instructions in ``task`` or
    ``prompt``. Listed names are matched against the Workspace's agents
    (never Manor, never the caller). With ``pin_targets`` every task is then
    resolved to one active subscription, duplicates and empty tasks are
    refused, and each task gets its answer budget; chat defers that step to
    its parallel runner so a lone task keeps its own resolution path.
    """

    if not workspace_id:
        return RuntimeDelegationBatch(error=_dumps({"error": "Delegation works only inside a Workspace."}))
    if not tasks or len(tasks) > MAX_PARALLEL_DELEGATIONS or not all(isinstance(t, dict) for t in tasks):
        return RuntimeDelegationBatch(error=_dumps({
            "error": "invalid_tasks",
            "message": f"tasks must list between 1 and {MAX_PARALLEL_DELEGATIONS} tasks.",
            "action_executed": False,
        }))
    if caller_agent_id is None:
        caller_agent_id = _clean(shared.get("_agent_id_from_context")) or None
    targets: list[dict[str, Any]] | None = None
    items: list[dict[str, Any]] = []
    for task in tasks:
        prompt = str(task.get("task") or task.get("prompt") or "").strip()
        if not prompt:
            return RuntimeDelegationBatch(error=_dumps({
                "error": "task is required", "message": "Every task needs instructions.", "action_executed": False,
            }))
        params: dict[str, Any] = {"prompt": prompt}
        if task.get("continue_interrupted") is True:
            params["continue_interrupted"] = True
        explicit = {key: task[key] for key in _DELEGATION_TARGET_KEYS if str(task.get(key) or "").strip()}
        requested = str(task.get("agent") or "").strip()
        if requested and not explicit:
            if targets is None:
                from packages.core.database import async_session

                async with async_session() as db:
                    targets = await runtime_workspace_delegation_targets(
                        db, entity_id=entity_id, workspace_id=workspace_id, exclude_agent_id=caller_agent_id,
                    )
            target = runtime_match_delegation_target(targets, requested)
            if target is None:
                return RuntimeDelegationBatch(error=_dumps({
                    "error": "unknown_agent",
                    "message": f"No agent in this Workspace is named {requested!r}.",
                    "available_agents": [item["label"] for item in targets],
                    "action_executed": False,
                }))
            params["agent_subscription_id"] = target["agent_subscription_id"]
            label = target["label"]
        else:
            params.update(explicit)
            label = requested or str(next(iter(explicit.values()), "") or "")
        items.append({"agent": label, "params": params})
    if not pin_targets:
        return RuntimeDelegationBatch(items=items, shared=dict(shared))
    return await _runtime_pin_delegation_items(
        entity_id=entity_id, workspace_id=workspace_id, items=items, shared_params=shared,
    )


def _service_prompt_appendix(sub: Any, agent: Any, caller: str = "Manor AI") -> str:
    service_key = _clean(getattr(sub, "service_key", None))
    service_label = (
        service_key
        or _clean(getattr(sub, "name", None))
        or _clean(getattr(agent, "name", None))
    )
    parts = [
        (
            f"You are being called by {caller} as a delegated workspace service "
            f"agent for `{service_label}`. Complete the delegated request using "
            "only your own visible tools, MCP integrations, skills, and workspace "
            f"context. Return a concise result for {caller} to relay to the user. "
            "If a required credential, approval, or user decision is missing, "
            "state exactly what is needed. Preserve the labels and units of facts; "
            "do not reinterpret an unlabeled number as a price, cost, or discount. "
            "Ask for clarification when those meanings are ambiguous. Apply the "
            "latest explicit corrections and limits to your recommendations. "
            "Report confirmed success, explicit failure, and unknown effects separately."
        )
    ]
    custom_prompt = _clean(getattr(sub, "custom_prompt", None))
    if custom_prompt:
        parts.append(f"Workspace service-specific instructions:\n{custom_prompt}")
    return "\n\n".join(parts)


async def runtime_workspace_delegate_service_action(**kwargs: Any) -> str:
    """Delegate a workspace chat request to a service-bound sub-agent.

    Refuses beyond MAX_DELEGATION_DEPTH nested delegations.
    """
    depth = _delegation_depth.get()
    if depth >= MAX_DELEGATION_DEPTH:
        return _dumps({
            "error": "delegation_depth_limit",
            "message": (
                "This agent was itself delegated to by another agent and cannot delegate "
                "further. Do the work with your own tools or report what is missing."
            ),
            "action_executed": False,
        })
    token = _delegation_depth.set(depth + 1)
    try:
        return await _runtime_workspace_delegate_service_action(**kwargs)
    finally:
        _delegation_depth.reset(token)


# Delegated answers come back in one tool result, which transcript chats
# show the parent model up to 40,000 characters of (conversation_transcript
# TRANSCRIPT_TOOL_RESULT_MAX_CHARS, kept equal by a test). Each agent is told
# its share before it starts, so answers are written to fit rather than cut.
DELEGATION_TOOL_RESULT_CHARS = 40_000
_DELEGATION_RESULT_ENVELOPE_CHARS = 1_000
_DELEGATION_ENTRY_FIELDS_CHARS = 700
_DELEGATION_RESULT_MARGIN_CHARS = 200
DELEGATION_ANSWER_MIN_CHARS = 800
DELEGATION_READ_PAGE_CHARS = 20_000


def runtime_delegation_answer_budget(agent_count: int) -> int:
    """Characters each of ``agent_count`` agents may answer with in one call."""
    share = (DELEGATION_TOOL_RESULT_CHARS - _DELEGATION_RESULT_ENVELOPE_CHARS) // max(1, agent_count)
    return max(DELEGATION_ANSWER_MIN_CHARS, share - _DELEGATION_ENTRY_FIELDS_CHARS)


class DelegatedAnswerLengthContract:
    """One chance for a delegated agent to fit its answer to its share.

    An answer over the limit goes back to the agent once, with its tools, to
    shorten it or save the long part as a file. A second overrun is accepted
    as is (the parent result is fitted then) rather than lost.
    """

    instructions = ""
    response_format = None

    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self.revised = False

    async def validate(self, content: str):
        from packages.core.ai.runtime.final_response import FinalResponseContext

        return await self.validate_with_context(content, FinalResponseContext())

    async def validate_with_context(self, content: str, context: Any):
        from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseDecision
        from packages.core.ai.runtime.tool_error_contract import tool_error_payload

        if len(content) <= self.max_chars or self.revised or not context.allow_execution:
            return FinalResponseDecision(FinalResponseAction.ACCEPT, content=content)
        self.revised = True
        return FinalResponseDecision(FinalResponseAction.CONTINUE_EXECUTION, diagnostic=tool_error_payload(
            tool_name="final_response", code="answer_over_limit",
            message=(
                f"Your answer is {len(content)} characters; the limit is {self.max_chars}. "
                f"Reply again within {self.max_chars} characters. If the full text matters, "
                "save it as a file and give its path with a short summary."
            ),
            details={"answer_chars": len(content), "max_chars": self.max_chars},
        ))


def _answer_budget(raw_params: dict[str, Any]) -> int | None:
    budget = raw_params.get("answer_max_chars")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        return None
    return budget


def _answer_budget_note(raw_params: dict[str, Any]) -> str | None:
    """The agent's answer limit for this call: turn context, never stored in
    its private history, where a past call's limit would linger."""
    budget = _answer_budget(raw_params)
    if budget is None:
        return None
    return (
        f"Your final answer this turn is limited to {budget} characters (about "
        f"{budget // 6} English words or {budget} Chinese characters); text beyond that "
        "is cut off. Fit the answer to this limit even if the task asks for more, and "
        "save anything longer (full drafts, tables, data) as a file and give its path."
    )


# Per-run bookkeeping the parent model does not act on; usage is already
# recorded by the child run itself. tool_calls_made stays: it tells the
# parent which tools the agent actually ran.
_MODEL_DROPPED_KEYS = frozenset({
    "usage", "tool_outcomes", "rounds", "idempotency_key",
})


def runtime_delegation_result_for_model(payload: dict[str, Any]) -> dict[str, Any]:
    """The parent model's copy of one finished child run: no bookkeeping and
    no second copy of the answer."""
    out = {key: value for key, value in payload.items() if key not in _MODEL_DROPPED_KEYS}
    if isinstance(out.get("tool_calls_made"), list):
        # Which tools ran, once each: repeats spend the shared budget.
        out["tool_calls_made"] = list(dict.fromkeys(map(str, out["tool_calls_made"])))[:12]
    content = out.get("content") if isinstance(out.get("content"), str) else ""
    if out.get("result") == {"text": content}:
        # A chat run without an output schema repeats the whole answer here.
        out.pop("result")
    return out


def _cut_marker(entry: dict[str, Any], shown: int, total: int) -> str:
    # The rest is readable by run_id when the run's full answer was kept: in
    # its chat session, or stored with the run itself (result_stored).
    if entry.get("run_id") and (entry.get("session_id") or entry.get("result_stored") is True):
        return (
            f"\n[Cut here: {shown} of {total} characters shown. The rest is stored: read it "
            f"with workspace_agent action read_delegation_result, run_id {entry['run_id']}, "
            f"offset {shown}, before quoting or using it.]"
        )
    return f"\n[Cut here: {shown} of {total} characters shown; the rest was not kept.]"


def _fit_delegation_payload(payload: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    """Serialize within the tool-result budget, shortening only the longest
    answers and only by what does not fit. Agents that stayed within their
    stated share are normally untouched."""
    limit = DELEGATION_TOOL_RESULT_CHARS - _DELEGATION_RESULT_MARGIN_CHARS
    text = _dumps(payload)
    if len(text) <= limit:
        return text
    originals = [entry.get("content") if isinstance(entry.get("content"), str) else "" for entry in entries]

    def render(cap: int) -> str:
        for entry, original in zip(entries, originals):
            if len(original) > cap:
                entry["content"] = original[:cap] + _cut_marker(entry, cap, len(original))
                entry["content_truncated"] = {"shown_chars": cap, "total_chars": len(original)}
            else:
                entry["content"] = original
                entry.pop("content_truncated", None)
        return _dumps(payload)

    low, high = 0, max(map(len, originals), default=0)
    while low < high:
        cap = (low + high + 1) // 2
        if len(render(cap)) <= limit:
            low = cap
        else:
            high = cap - 1
    return render(low)


def runtime_delegation_tool_result(raw: str) -> str:
    """A single delegation's tool result within the budget; pauses pass through."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if (not isinstance(payload, dict) or not payload.get("delegated")
            or payload.get("stop_parent") or _delegated_hitl_result(payload) is not None):
        return raw
    out = runtime_delegation_result_for_model(payload)
    return _fit_delegation_payload(out, [out])


async def runtime_read_delegation_result_action(
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    conversation_id: str | None,
    params: dict[str, Any] | None = None,
) -> str:
    """Read a delegated run's full answer by run_id, a page at a time."""
    from packages.core.database import async_session
    from packages.core.services.agent_delegation_session import (
        DELEGATION_RESULTS_KEPT,
        find_delegation_result,
    )

    raw_params = dict(params or {})
    run_id = _clean(raw_params.get("run_id"))
    if not run_id:
        return _dumps({"error": "run_id is required", "action_executed": False})
    if not (entity_id and workspace_id and user_id and conversation_id):
        return _dumps({
            "error": "delegation_result_unavailable",
            "message": "Delegated results are readable only in the Workspace chat that ran them.",
        })
    try:
        offset = max(0, int(raw_params.get("offset") or 0))
        limit = min(DELEGATION_READ_PAGE_CHARS, max(1, int(raw_params.get("limit") or DELEGATION_READ_PAGE_CHARS)))
    except (TypeError, ValueError):
        return _dumps({"error": "offset and limit must be integers", "action_executed": False})
    async with async_session() as db:
        found = await find_delegation_result(
            db, entity_id=entity_id, workspace_id=workspace_id,
            conversation_id=conversation_id, user_id=user_id, run_id=run_id,
        )
    if found is None:
        return _dumps({
            "error": "delegation_result_not_found",
            "message": (
                f"No delegated run {run_id} in this chat. The latest {DELEGATION_RESULTS_KEPT} "
                "results of each agent here are kept."
            ),
        })
    session, item = found
    content = str(item.get("content") or "")
    page = content[offset:offset + limit]
    end = offset + len(page)
    return _dumps({
        "run_id": run_id,
        "session_id": session.id,
        "agent": item.get("agent_name"),
        "status": item.get("status"),
        "error": item.get("error"),
        "content": page,
        "offset": offset,
        # A paged-read shape: a caller with a smaller tool-result budget cuts
        # the page and rewrites next_offset to where its visible text ends.
        "returned_chars": len(page),
        "total_chars": item.get("total_chars", len(content)),
        "next_offset": end if end < len(content) else None,
    })


def _parallel_child_payload(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = {"content": raw}
    return parsed if isinstance(parsed, dict) else {"content": parsed}


def _parallel_entry(item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    hitl = _delegated_hitl_result(payload)
    if hitl is not None:
        prompt = hitl.get("hitl", {}).get("prompt") if isinstance(hitl.get("hitl"), dict) else None
        return {"agent": item["agent"], "delegation_status": "awaiting_approval",
                **({"approval_prompt": prompt} if prompt else {})}
    if payload.get("stop_parent"):
        status = "waiting_for_input"
        entry = {key: payload.get(key) for key in ("run_id", "session_id", "content") if payload.get(key)}
    else:
        status = ("failed" if payload.get("error") and not payload.get("delegated")
                  else _clean(payload.get("status")) or "completed")
        entry = runtime_delegation_result_for_model(payload)
        entry.pop("delegated", None)
    return {"agent": item["agent"], "delegation_status": status, **entry}


async def runtime_workspace_delegate_parallel(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
    conversation_id: str | None,
    items: list[dict[str, Any]],
    shared_params: dict[str, Any] | None = None,
) -> str:
    """Run several delegations to different Workspace agents at the same time.

    ``items`` are ``{"agent": label, "params": {...delegate_service params}}``.
    Every target is resolved before anything runs, so a bad name or a repeated
    agent rejects the whole batch with no side effects. Results come back in
    input order. A child that pauses for approval or for the user's input
    pauses the parent with that child's envelope; the other children's
    results ride along in ``parallel_results``.
    """
    if not items or len(items) > MAX_PARALLEL_DELEGATIONS:
        return _dumps({
            "error": "invalid_parallel_delegation",
            "message": f"Give between 1 and {MAX_PARALLEL_DELEGATIONS} tasks.",
            "action_executed": False,
        })
    if _delegation_depth.get() >= MAX_DELEGATION_DEPTH:
        return await runtime_workspace_delegate_service_action(
            entity_id=entity_id, workspace_id=workspace_id, params={},
        )
    batch = await _runtime_pin_delegation_items(
        entity_id=entity_id, workspace_id=workspace_id, items=items, shared_params=shared_params,
    )
    if batch.error is not None:
        return batch.error
    items, shared = batch.items, batch.shared

    async def run(index: int, item: dict[str, Any]) -> str:
        # A short stagger spreads the children's first LLM and DB calls.
        await asyncio.sleep(index * PARALLEL_DELEGATION_STAGGER_SECONDS)
        return await runtime_workspace_delegate_service_action(
            entity_id=entity_id,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            params={**shared, **item["params"]},
        )

    # The children's database work queues as one caller: twenty runs must
    # not fill the chat process's admission queue and time each other (and
    # everyone else on the process) out. Their model calls still overlap.
    from packages.core.database_runtime import DatabaseAdmissionGroup, database_admission_group

    group_token = database_admission_group.set(DatabaseAdmissionGroup(limit=1))
    try:
        raws = await asyncio.gather(
            *(run(index, item) for index, item in enumerate(items)), return_exceptions=True,
        )
    finally:
        database_admission_group.reset(group_token)
    for result in raws:
        if isinstance(result, asyncio.CancelledError):
            raise result
    raws = [
        _dumps({"error": "workspace_service_delegation_failed", "message": str(result)})
        if isinstance(result, BaseException) else result
        for result in raws
    ]
    return runtime_delegation_batch_result(items, raws, parallel=True)


def runtime_delegation_batch_result(
    items: list[dict[str, Any]],
    results: list[str | dict[str, Any]],
    *,
    parallel: bool | None = None,
) -> str:
    """One delegation call's tool result from its children's results.

    ``items`` are the batch's ``{"agent": label, ...}`` items, ``results``
    each child's raw result (or payload) in the same order; a batch has at
    least one. A lone child's result is fitted to the budget as is (a pause,
    an approval envelope or its host carrier passes through untouched);
    ``items`` is not read on that path. Several come back in input order in
    one envelope within the shared budget, labelled with each item's
    ``agent``; a child waiting for approval or for the user's input pauses
    the parent with its own envelope, and the other children's entries ride
    along in ``parallel_results``. ``parallel`` defaults to more than one
    child.
    """
    if not results:
        raise ValueError("A delegation batch result needs at least one child result.")
    raws = [result if isinstance(result, str) else _dumps(result) for result in results]
    if parallel is None:
        parallel = len(raws) > 1
    if not parallel:
        return runtime_delegation_tool_result(raws[0])
    payloads = [_parallel_child_payload(raw) for raw in raws]
    entries = [_parallel_entry(item, payload) for item, payload in zip(items, payloads)]
    for payload in payloads:
        hitl = _delegated_hitl_result(payload)
        if hitl is not None:
            return _fit_delegation_payload({**hitl, "parallel_results": entries}, entries)
    for payload in payloads:
        if payload.get("stop_parent"):
            return _fit_delegation_payload({**payload, "parallel_results": entries}, entries)
    return _fit_delegation_payload({
        "delegated": True,
        "parallel": True,
        "completed": sum(1 for entry in entries if entry["delegation_status"] == "completed"),
        "total": len(entries),
        "results": entries,
    }, entries)


async def _delegation_caller_label(db: AsyncSession, caller_agent_id: str | None) -> str:
    from packages.core.constants.agents import is_master_agent
    from packages.core.models.workspace import Agent

    if not caller_agent_id or is_master_agent(caller_agent_id):
        return "Manor AI"
    name = (await db.execute(select(Agent.name).where(Agent.id == caller_agent_id))).scalar_one_or_none()
    return f"the Workspace agent {name}" if name else "another Workspace agent"


@dataclass(frozen=True)
class RuntimeDelegatedChildSetup:
    """Who a delegated child is and what it runs with.

    ``error`` is the serialized refusal when the child cannot start; then
    nothing ran.
    """

    sub: Any = None
    agent: Any = None
    ctx: Any = None
    # The child's tools: its own context's tools plus request_input.
    tools: list[dict[str, Any]] = field(default_factory=list)
    allowed_tool_names: list[str] | None = None
    max_rounds: int = 0
    # Skills the delegation requires the child to run first.
    skill_refs: list[str] = field(default_factory=list)
    error: str | None = None


async def runtime_delegated_child_setup(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None,
    conversation_id: str | None,
    task_id: str | None,
    step_id: str | None,
    raw_params: dict[str, Any],
    prompt: str,
    delegation_resume: dict[str, Any] | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    caller_agent_id: str | None = None,
    on_identity: Callable[[Any, Any], Awaitable[str | None] | str | None] | None = None,
) -> RuntimeDelegatedChildSetup:
    """Resolve a delegated child's identity and build what it runs with.

    The service is resolved from ``raw_params`` and must still match every
    identity the call pinned (subscription, agent, service key, and an
    approval resume's service). ``on_identity`` runs once the identity is
    pinned and before any context construction or provider I/O; a string it
    returns refuses the child with that result. The child then gets its own
    Workspace-chat context (no history: the prompt carries the task), the
    round budget for its kind of run, and request_input in its tools.
    """
    from packages.core.ai.agentic_loop import DEFAULT_MAX_ROUNDS
    from packages.core.ai.context import build_agent_context
    from packages.core.ai.runtime.human_input import (
        REQUEST_INPUT_TOOL_NAME,
        runtime_request_input_tool_schema,
    )
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.turn_policy import runtime_turn_max_rounds

    sub, agent, available_services, error = await _resolve_workspace_service_agent(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        raw_params=raw_params,
    )
    if error:
        return RuntimeDelegatedChildSetup(error=_dumps({
            "error": "workspace_service_agent_not_found",
            "message": error,
            "available_services": available_services,
        }))
    expected_subscription_id = _clean(
        raw_params.get("agent_subscription_id")
    )
    expected_agent_id = _clean(raw_params.get("agent_id"))
    expected_service_key = _clean(raw_params.get("service_key"))
    if (
        (expected_subscription_id and expected_subscription_id != sub.id)
        or (expected_agent_id and expected_agent_id != sub.agent_id)
        or (
            expected_service_key
            and expected_service_key != _clean(sub.service_key)
        )
    ):
        return RuntimeDelegatedChildSetup(error=_dumps({
            "error": "workspace_service_identity_changed",
            "message": (
                "The delegated workspace service no longer matches the "
                "subscription and agent pinned to this run."
            ),
        }))
    if delegation_resume is not None and (
        _clean(delegation_resume.get("agent_subscription_id")) != sub.id
        or _clean(delegation_resume.get("agent_id")) != sub.agent_id
        or _clean(delegation_resume.get("service_key")) != _clean(sub.service_key)
    ):
        return RuntimeDelegatedChildSetup(error=_dumps({
            "error": "delegated_approval_resume_rejected",
            "message": "Delegated approval service identity has changed.",
        }))

    # Pin the exact subscription/agent before context construction or
    # provider I/O. A successor must never continue an existing child
    # transcript through whichever subscription happens to be active later.
    if on_identity is not None:
        refusal = on_identity(sub, agent)
        if isawaitable(refusal):
            refusal = await refusal
        if refusal is not None:
            return RuntimeDelegatedChildSetup(sub=sub, agent=agent, error=refusal)

    skill_refs = [
        _clean(ref)
        for ref in (raw_params.get("skill_refs") or [])
        if _clean(ref)
    ]
    if caller_agent_id is None:
        caller_agent_id = _clean(raw_params.get("_agent_id_from_context")) or None
    ctx = await build_agent_context(
        db,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=sub.agent_id,
        agent_subscription_id=sub.id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        task_id=task_id,
        active_user_message=prompt,
        model_role="primary",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
        extra_system_prompt=_service_prompt_appendix(
            sub, agent, await _delegation_caller_label(db, caller_agent_id),
        ),
        runtime_metadata=runtime_metadata,
        load_history=False,
        skill_refs=[{"kind": "slug", "value": ref} for ref in skill_refs],
    )

    # Interactive Chat delegation keeps its deliberately tight budget.
    # A coordinator child is a materialized Plan step, however: silently
    # clamping a requested 60-round worker to 20 made the nested loop less
    # capable than the worker adapter that owns the parent Task. Keep both
    # on the shared Plan-step cap without widening ordinary Chat turns.
    max_rounds = _delegation_max_rounds(
        raw_params.get("max_rounds"),
        task_id=task_id,
        step_id=step_id,
    )
    if not (task_id and step_id):
        chat_max_rounds = runtime_turn_max_rounds(ctx) or DEFAULT_MAX_ROUNDS
        max_rounds = _bounded_max_rounds(
            raw_params.get("max_rounds"), default=chat_max_rounds, upper=chat_max_rounds,
        )

    tools = list(ctx.tools)
    if not any(
        _clean((tool.get("function") or {}).get("name"))
        == REQUEST_INPUT_TOOL_NAME
        for tool in tools
        if isinstance(tool, dict)
    ):
        tools.append(runtime_request_input_tool_schema())
    allowed_tool_names = ctx.allowed_tool_names
    if allowed_tool_names is not None:
        allowed_tool_names = [
            *allowed_tool_names,
            REQUEST_INPUT_TOOL_NAME,
        ]
    return RuntimeDelegatedChildSetup(
        sub=sub,
        agent=agent,
        ctx=ctx,
        tools=tools,
        allowed_tool_names=allowed_tool_names,
        max_rounds=max_rounds,
        skill_refs=skill_refs,
    )


@contextmanager
def runtime_delegated_child_billing(
    *,
    run_id: str,
    invocation_id: str,
    entity_id: str,
    user_id: str | None,
    agent_id: str,
    workspace_id: str,
    conversation_id: str | None,
    fallback_model: str | None,
    byok: bool,
) -> Iterator[tuple[Callable[..., Any], Callable[..., Any]]]:
    """Per-call durable billing for one invocation of a delegated child.

    Yields the ``(reserve, settle)`` callbacks for the child loop and holds a
    suppressed billing context around it, so every provider request is
    settled by those callbacks and nothing is charged twice. ``run_id``
    keeps attribution stable across a resumed child; ``invocation_id`` is a
    fresh nonce per invocation so that real provider work repeated after a
    crash is not hidden by the settlement idempotency fence of an earlier
    process.
    """
    from packages.core.ai.runtime.billing import (
        runtime_release_billing_context,
        runtime_set_suppressed_billing_context,
    )
    from packages.core.services.usage_service import (
        build_durable_llm_billing_callbacks,
    )

    reserve_llm_call, settle_llm_call = build_durable_llm_billing_callbacks(
        source_prefix=f"delegation:{run_id}:{invocation_id}",
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        fallback_model=fallback_model,
        byok=byok,
    )
    billing_handle = runtime_set_suppressed_billing_context(
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        durable_per_call=True,
    )
    try:
        yield reserve_llm_call, settle_llm_call
    finally:
        runtime_release_billing_context(billing_handle)


def runtime_delegated_tool_outcome(name: str, raw_result: Any) -> dict[str, Any]:
    """One tool result of a delegated child, as its outcome counts it.

    Only the classified status is kept, not a second unbounded copy of tool
    output that compaction has already removed from the active window.
    """
    from packages.core.services.runtime_learning import chat_tool_outcome

    return {"name": name, "result": {"status": chat_tool_outcome({"result": raw_result})}}


def runtime_delegated_child_content(result: Any) -> str:
    """The child's final answer as the parent may see it."""
    from packages.core.ai.runtime.output_policy import (
        runtime_is_internal_failure_detail,
        runtime_sanitize_assistant_content_after_loop,
    )

    internal_failure_seen = any(
        isinstance(runtime_message, dict)
        and runtime_message.get("role") == "tool"
        and runtime_is_internal_failure_detail(runtime_message.get("content"))
        for runtime_message in (result.messages or [])
    )
    return runtime_sanitize_assistant_content_after_loop(
        result.content or "",
        internal_failure_seen=internal_failure_seen,
    )


def _delegated_service(sub: Any, agent: Any) -> dict[str, Any]:
    return {
        "agent_subscription_id": sub.id,
        "service_key": sub.service_key,
        "agent_id": sub.agent_id,
        "agent_name": agent.name,
    }


def _interrupted_fields(replay_fields: dict[str, Any]) -> dict[str, Any]:
    """What an interrupted child adds to its payload: it may not continue
    until the parent has checked its last action."""
    return {
        **replay_fields,
        "session_status": "interrupted",
        "next_step": (
            "Check whether the child's last action took effect before delegating again; "
            "then pass continue_interrupted=true."
        ),
    }


@dataclass(frozen=True)
class RuntimeDelegatedChildResult:
    """A finished delegated child: the payload the parent gets, and the
    pieces its host records."""

    payload: dict[str, Any]
    status: str
    content: str
    error: str | None
    replay_fields: dict[str, Any]
    # The child left an unconfirmed effect behind: it may not continue until
    # the parent has checked it and passes continue_interrupted.
    interrupted: bool


def runtime_delegated_child_result(
    result: Any,
    *,
    run_id: str,
    session_id: str | None,
    idempotency_key: str | None,
    sub: Any,
    agent: Any,
    entity_id: str,
    workspace_id: str,
    task_id: str | None,
    tool_results: list[dict[str, Any]],
    output_schema: dict[str, Any] | None = None,
    content: str | None = None,
    forced_error: str | None = None,
    parks_when_unconfirmed: bool | None = None,
    result_stored: bool = False,
) -> RuntimeDelegatedChildResult:
    """The result payload of one finished delegated child loop.

    Pure: no session I/O and no event stream, so every host (nested chat,
    durable child run) returns the same payload for the same loop result.
    ``tool_results`` are this invocation's ``runtime_delegated_tool_outcome``
    records; ``forced_error`` fails the child with that error when the host
    could not keep its result (e.g. an approval resume it could not store).
    A child that left an unconfirmed effect behind is ``interrupted`` when
    its host parks it (by default: when it has a session). With
    ``result_stored`` the full answer is readable by ``run_id`` even without
    a session.
    """
    from packages.core.ai.runtime.outcome import replay_fields_from_control, replay_fields_from_result
    from packages.core.services.runtime_learning import chat_learning_outcome

    if content is None:
        content = runtime_delegated_child_content(result)
    typed_result = _typed_delegation_result(content, output_schema)
    schema_error = _delegation_schema_error(typed_result, output_schema)
    run_error = result.error or (
        "output_schema_validation_failed" if schema_error is not None else None
    )
    run_error_detail = getattr(result, "error_detail", None)
    if schema_error is not None and not isinstance(run_error_detail, dict):
        run_error_detail = {"validation_error": schema_error}
    if result.stop_reason in {"cancelled", "canceled"}:
        run_status = "cancelled"
    elif run_error or result.stop_reason == "error":
        run_status = "failed"
    elif result.stop_reason in {"blocked", "hitl_required", "needs_hitl"}:
        run_status = "blocked"
    else:
        run_status = "completed"
    if forced_error is not None:
        run_error = run_error or forced_error
        run_status = "failed"
    outcome, tool_outcomes = chat_learning_outcome(
        stop_reason=result.stop_reason,
        has_content=bool(content.strip()),
        error=run_error,
        tools=list(result.tool_calls_made or []),
        tool_results=tool_results,
    )
    if run_status == "completed" and outcome != "succeeded":
        run_status = outcome

    # The ledger floor is sticky: a child that recovered and completed keeps
    # only its control's fields. A failed or partial child that left an
    # unconfirmed call behind parks until the parent checks it.
    replay_fields = (
        replay_fields_from_control(getattr(result, "control", None))
        if run_status == "completed"
        else replay_fields_from_result(result)
    )
    if parks_when_unconfirmed is None:
        parks_when_unconfirmed = bool(session_id)
    interrupted = parks_when_unconfirmed and replay_fields.get("do_not_replay") is True
    evidence_refs, artifacts, knowledge_refs = _delegation_receipts(
        [message for message in (result.messages or []) if isinstance(message, dict)],
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    payload = {
        "delegated": True,
        "run_id": run_id,
        "session_id": session_id,
        **({"result_stored": True} if result_stored else {}),
        "idempotency_key": idempotency_key or None,
        "status": run_status,
        "tool_outcomes": tool_outcomes,
        "service": _delegated_service(sub, agent),
        "content": content,
        "result": typed_result,
        "evidence_refs": evidence_refs,
        "artifacts": artifacts,
        "knowledge_refs": knowledge_refs,
        "rounds": result.rounds,
        "tool_calls_made": list(result.tool_calls_made or []),
        "usage": result.usage or {},
        "stop_reason": result.stop_reason,
        "error": run_error,
        "error_detail": run_error_detail,
        **(_interrupted_fields(replay_fields) if interrupted else {}),
    }
    return RuntimeDelegatedChildResult(
        payload=payload,
        status=run_status,
        content=content,
        error=run_error,
        replay_fields=replay_fields,
        interrupted=interrupted,
    )


_HOST_FAILURE_STATUSES = frozenset({"failed", "cancelled"})


def runtime_delegated_child_failure(
    *,
    run_id: str,
    sub: Any,
    agent: Any,
    status: str,
    error: str,
    message: str = "",
    session_id: str | None = None,
    idempotency_key: str | None = None,
    rounds: int = 0,
    tool_calls_made: list[str] | None = None,
    usage: dict[str, Any] | None = None,
    error_detail: dict[str, Any] | None = None,
    replay_fields: dict[str, Any] | None = None,
    result_stored: bool = False,
) -> dict[str, Any]:
    """The payload of a delegated child its host ended without a loop result.

    For host-side endings (the pinned identity changed, crash repair was
    exhausted, the child was cancelled): the same delegated shape as
    ``runtime_delegated_child_result``, so the entry still carries the
    child's ``run_id`` and the parent reads one shape. ``replay_fields``
    (e.g. ``do_not_replay`` when the child's last round is unknown) mark the
    child interrupted exactly as a finished child would be.
    """
    if status not in _HOST_FAILURE_STATUSES:
        raise ValueError(f"A host-ended delegated child is failed or cancelled, not {status!r}.")
    replay_fields = dict(replay_fields or {})
    return {
        "delegated": True,
        "run_id": run_id,
        "session_id": session_id,
        **({"result_stored": True} if result_stored else {}),
        "idempotency_key": idempotency_key or None,
        "status": status,
        "tool_outcomes": {},
        "service": _delegated_service(sub, agent),
        "content": message,
        "result": None,
        "evidence_refs": [],
        "artifacts": [],
        "knowledge_refs": [],
        "rounds": rounds,
        "tool_calls_made": list(tool_calls_made or []),
        "usage": usage or {},
        "stop_reason": "cancelled" if status == "cancelled" else "error",
        "error": error,
        "error_detail": error_detail,
        **(_interrupted_fields(replay_fields) if replay_fields.get("do_not_replay") is True else {}),
    }


def _delegated_input_pause_payload(
    result: Any,
    *,
    run_id: str,
    idempotency_key: str | None,
    sub: Any,
    agent: Any,
) -> dict[str, Any]:
    """A nested child that asked the user (request_input): it stops its parent."""
    from packages.core.ai.pending_action import PendingAction

    control = dict(result.control or {})
    input_payload = (
        dict(control.get("payload") or {})
        if isinstance(control.get("payload"), dict)
        else {}
    )
    question = _clean(input_payload.get("question"))
    questions: list[dict[str, Any]] = []
    fields = input_payload.get("fields")
    options = input_payload.get("options")
    if isinstance(fields, list):
        questions.extend(
            dict(entry)
            for entry in fields
            if isinstance(entry, dict) and _clean(entry.get("name"))
        )
    if not questions:
        questions.append({
            "name": "response",
            "label": question or "Response",
            "type": "select" if isinstance(options, list) and options else "textarea",
            "required": True,
            **(
                {"options": [str(item) for item in options]}
                if isinstance(options, list) and options
                else {}
            ),
        })
    pending_action = PendingAction.needs_input(
        questions=questions,
        title=question or "Input required",
        context_summary=_clean(input_payload.get("context")) or None,
    ).to_dict()
    return {
        "delegated": True,
        "run_id": run_id,
        "idempotency_key": idempotency_key or None,
        "status": "blocked",
        "service": _delegated_service(sub, agent),
        "content": question,
        "result": {"question": question},
        "evidence_refs": [],
        "artifacts": [],
        "knowledge_refs": [],
        "rounds": result.rounds,
        "tool_calls_made": list(result.tool_calls_made or []),
        "usage": result.usage or {},
        "stop_reason": "waiting_resource",
        "blocker_kind": "request_input",
        "human_input": input_payload,
        "_pending_action": pending_action,
        "stop_parent": True,
        "error": None,
    }


async def _runtime_workspace_delegate_service_action(
    *,
    entity_id: str,
    workspace_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
    task_id: str | None = None,
    step_id: str | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
    initial_usage: dict[str, Any] | None = None,
    initial_rounds: int = 0,
    initial_tool_calls_made: list[str] | None = None,
    runtime_checkpoint: dict[str, Any] | None = None,
    on_round_end: Callable[..., Awaitable[None] | None] | None = None,
    on_round_end_strict: bool = False,
    on_tool_effect_intent: (
        Callable[
            [
                str,
                dict[str, Any],
                str | None,
                "RuntimeToolClassification | None",
                dict[str, Any] | None,
            ],
            Any,
        ]
        | None
    ) = None,
    output_schema: dict[str, Any] | None = None,
    on_human_input_suspension: (
        Callable[[dict[str, Any], dict[str, Any]], Awaitable[None] | None] | None
    ) = None,
    on_service_resolved: (
        Callable[[str, str], Awaitable[None] | None] | None
    ) = None,
    record_nested_usage: bool = True,
    parent_step_id: str | None = None,
    parent_lease_id: str | None = None,
    parent_execution_claim_id: str | None = None,
) -> str:
    """Delegate a workspace chat request to a service-bound sub-agent."""

    if not workspace_id:
        return _dumps({
            "error": "workspace_id is required; use this tool only inside workspace chat",
        })
    if not entity_id:
        return _dumps({"error": "entity_id is required"})
    if not (task_id and step_id) and (not user_id or not conversation_id):
        return _dumps({"error": "Delegation requires a user and conversation."})
    raw_params = dict(params or {})
    approval_token = _clean(raw_params.get("approval_token"))

    run_id = ""
    session_id = ""
    delegation_idempotency_key = ""
    billing_invocation_id = ""
    prior_messages: list[dict[str, Any]] = []
    event_sink = None
    event_base: dict[str, Any] = {}
    # The child loop's result, once it returned: a later failure still hands
    # over the ledger of what the child already did.
    result = None
    try:
        from packages.core.ai.agentic_loop import LOOP_COMPACT_RATIO
        from packages.core.services.model_context_window import effective_context_window_tokens
        from packages.core.ai.runtime.context_window import runtime_context_window_usage, context_tool_manifest
        from packages.core.ai.runtime.harness import runtime_execute_chat_agent_loop
        from packages.core.ai.runtime.streams import (
            runtime_tool_arguments_for_chat,
            runtime_tool_status_for_chat,
            runtime_tool_stream_sink_var,
        )
        from packages.core.database import async_session
        from packages.core.ai.runtime.agentic_llm import RUNTIME_AGENTIC_MAX_TOKENS
        from packages.core.services.agent_delegation_session import (
            DelegationSessionBusy,
            DelegationSessionInterrupted,
            claim_delegation_session,
            finish_delegation_session,
            renew_delegation_session,
            archive_delegation_context,
        )

        async with async_session() as db:
            delegation_resume = None
            if approval_token:
                expected_idempotency_key = _clean(
                    raw_params.get("delegation_idempotency_key")
                ) or None
                expected_service_key = _clean(raw_params.get("service_key")) or None
                expected_subscription_id = _clean(
                    raw_params.get("agent_subscription_id")
                ) or None
                delegation_resume, resume_error = await _load_delegation_resume(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    approval_token=approval_token,
                    expected_task_id=task_id,
                    expected_step_id=step_id,
                    expected_idempotency_key=expected_idempotency_key,
                    expected_service_key=expected_service_key,
                    expected_subscription_id=expected_subscription_id,
                )
                if resume_error:
                    return _dumps({
                        "error": "delegated_approval_resume_rejected",
                        "message": resume_error,
                    })
                raw_params = {
                    "agent_subscription_id": delegation_resume["agent_subscription_id"],
                    "session_id": delegation_resume.get("delegation_session_id"),
                    "prompt": delegation_resume["prompt"],
                    "max_rounds": delegation_resume.get("max_rounds"),
                    "delegation_idempotency_key": delegation_resume.get(
                        "delegation_idempotency_key"
                    ),
                }
                task_id = _clean(delegation_resume.get("task_id")) or task_id
                step_id = _clean(delegation_resume.get("step_id")) or step_id

            prompt = _delegation_prompt(raw_params)
            if not prompt:
                return _dumps({"error": "prompt is required"})
            answer_budget = _answer_budget(raw_params)
            answer_budget_note = _answer_budget_note(raw_params)
            publication_resume = None
            if delegation_resume is not None:
                from packages.core.ai.runtime.approval_messages import (
                    is_chrome_publication_call, runtime_approval_runtime_metadata,
                )

                if is_chrome_publication_call(delegation_resume["tool_name"], delegation_resume["tool_arguments"]):
                    publication_resume = runtime_approval_runtime_metadata({
                        "tool": delegation_resume["tool_name"],
                        "retry_args": delegation_resume["tool_arguments"],
                    }, approval_token)
                    prompt += "\n\n" + publication_resume["approval_resume_guidance"]

            async def pin_identity(sub: Any, agent: Any) -> str | None:
                nonlocal run_id, session_id, prior_messages, event_sink, event_base
                nonlocal delegation_idempotency_key, billing_invocation_id
                if on_service_resolved is not None:
                    callback_result = on_service_resolved(
                        str(sub.id),
                        str(sub.agent_id),
                    )
                    if isawaitable(callback_result):
                        await callback_result
                delegation_idempotency_key = _clean(
                    raw_params.get("delegation_idempotency_key")
                )
                run_id = delegation_idempotency_key or generate_ulid()
                # A successor may genuinely repeat an LLM request after a crash.
                # Keep the child run prefix stable for attribution, but mint a new
                # invocation nonce so that real provider work is not hidden by the
                # settlement idempotency fence from an earlier process.
                billing_invocation_id = generate_ulid()
                if not (task_id and step_id):
                    try:
                        session, run_id, prior_messages = await claim_delegation_session(
                            db,
                            entity_id=entity_id,
                            workspace_id=workspace_id,
                            conversation_id=conversation_id,
                            user_id=user_id,
                            subscription=sub,
                            session_id=_clean(raw_params.get("session_id")) or None,
                            continue_interrupted=raw_params.get("continue_interrupted") is True,
                        )
                    except DelegationSessionInterrupted as exc:
                        return _dumps({"error": "delegation_session_interrupted", "message": str(exc)})
                    except (DelegationSessionBusy, ValueError) as exc:
                        return _dumps({"error": "delegation_session_unavailable", "message": str(exc)})
                    session_id = session.id
                event_sink = runtime_tool_stream_sink_var.get(None)
                event_base = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "agent_id": sub.agent_id,
                    "agent_subscription_id": sub.id,
                    "agent_name": agent.name,
                    "service_key": sub.service_key,
                    "objective": prompt[:500],
                }
                if event_sink is not None and event_sink.active:
                    event_sink.emit_sub_agent_event({
                        **event_base,
                        "event_type": "started",
                        "status": "running",
                        "updated_at": _event_time(),
                    })
                return None

            setup = await runtime_delegated_child_setup(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                task_id=task_id,
                step_id=step_id,
                raw_params=raw_params,
                prompt=prompt,
                delegation_resume=delegation_resume,
                runtime_metadata=publication_resume,
                on_identity=pin_identity,
            )
            if setup.error is not None:
                return setup.error
            sub, agent, ctx = setup.sub, setup.agent, setup.ctx
            required_skill_refs = setup.skill_refs
            max_rounds = setup.max_rounds

        tool_seq = [0]
        pending_tool_seqs: dict[str, list[int]] = {}
        nested_hitl: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        current_tool_results: list[dict[str, Any]] = []

        def on_tool_start(name: str, args: dict[str, Any]) -> None:
            tool_seq[0] += 1
            seq = tool_seq[0]
            signature = _tool_event_signature(name, args)
            pending_tool_seqs.setdefault(signature, []).append(seq)
            if event_sink is not None and event_sink.active:
                safe_arguments = runtime_tool_arguments_for_chat(name, args)
                tool = {
                    "seq": seq,
                    "name": name,
                    "status": "running",
                }
                if safe_arguments is not None:
                    tool["arguments"] = safe_arguments
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "tool_started",
                    "status": "running",
                    "updated_at": _event_time(),
                    "tool": tool,
                })

        def on_tool_end(
            name: str,
            result: str,
            duration_ms: float = 0,
            args: dict[str, Any] | None = None,
        ) -> None:
            # Observe only this turn's receipts. Historical failed attempts in
            # the private session must not poison a later successful turn.
            current_tool_results.append(runtime_delegated_tool_outcome(name, result))
            parsed_hitl = _delegated_hitl_result(result)
            if parsed_hitl is not None and isinstance(args, dict):
                nested_hitl.append((name, dict(args), parsed_hitl))
            signature = _tool_event_signature(name, args)
            pending = pending_tool_seqs.get(signature) or []
            seq = pending.pop(0) if pending else None
            if not pending:
                pending_tool_seqs.pop(signature, None)
            if event_sink is not None and event_sink.active:
                safe_arguments = runtime_tool_arguments_for_chat(name, args)
                tool = {
                    "seq": seq,
                    "name": name,
                    "status": (
                        "error"
                        if runtime_tool_status_for_chat(result) == "error"
                        else "completed"
                    ),
                    "duration_ms": int(duration_ms),
                }
                if safe_arguments is not None:
                    tool["arguments"] = safe_arguments
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "tool_completed",
                    "status": "running",
                    "updated_at": _event_time(),
                    "tool": tool,
                })

        async def on_llm_call_before(*args: Any, **kwargs: Any) -> Any:
            async with async_session() as db:
                await renew_delegation_session(db, session_id=session_id, run_id=run_id)
            return await reserve_llm_call(*args, **kwargs)

        async def on_context_compacted(messages: list[dict]) -> str:
            async with async_session() as db:
                return await archive_delegation_context(
                    db, session_id=session_id, run_id=run_id, messages=messages,
                )

        async def on_context_checkpoint(messages: list[dict]) -> None:
            from packages.core.services.agent_delegation_session import checkpoint_delegation_context
            async with async_session() as db:
                await checkpoint_delegation_context(db, session_id=session_id, run_id=run_id, messages=messages)

        async def read_context_archive(args: dict) -> str:
            from packages.core.services.agent_delegation_session import read_delegation_context_archive
            from packages.core.services.runtime_context_archive import archive_page
            async with async_session() as db:
                archive = await read_delegation_context_archive(
                    db, archive_id=str(args.get("archive_id") or ""), session_id=session_id,
                    entity_id=entity_id, workspace_id=workspace_id, conversation_id=conversation_id,
                    user_id=user_id, agent_subscription_id=sub.id,
                )
                return archive_page(archive, args)

        loop_user_message = (
            "Continue the same delegated checklist item from the durable transcript. "
            "Do not repeat tool work whose successful result is already present. "
            "Finish the original deliverable."
            if initial_messages
            else prompt
        )
        from packages.core.ai.runtime.tool_effect_intents import (
            runtime_tool_effect_intent_scope,
        )

        with runtime_delegated_child_billing(
            run_id=run_id,
            invocation_id=billing_invocation_id,
            entity_id=entity_id,
            user_id=user_id,
            agent_id=sub.agent_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            fallback_model=ctx.model,
            byok=bool(getattr(ctx, "byok", False)),
        ) as (reserve_llm_call, settle_llm_call):
            with runtime_tool_effect_intent_scope(on_tool_effect_intent):
                result = await runtime_execute_chat_agent_loop(
                    runtime_envelope=ctx.runtime_envelope,
                    prompt_context=getattr(ctx, "prompt_context", None),
                    system_prompt=ctx.system_prompt,
                    user_message=loop_user_message,
                    tools=setup.tools,
                    entity_id=entity_id,
                    user_id=user_id,
                    agent_id=sub.agent_id,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    task_id=task_id or ctx.task_id,
                    step_id=step_id,
                    active_user_message=prompt,
                    tool_profile=ctx.tool_profile,
                    allowed_tool_names=setup.allowed_tool_names,
                    model=ctx.model,
                    temperature=getattr(ctx, "temperature", None),
                    max_tokens=getattr(ctx, "max_tokens", None),
                    max_rounds=max_rounds,
                    hard_max_rounds=max_rounds,
                    initial_messages=initial_messages if task_id and step_id else prior_messages or None,
                    initial_usage=initial_usage,
                    initial_rounds=initial_rounds,
                    initial_tool_calls_made=initial_tool_calls_made,
                    runtime_checkpoint=runtime_checkpoint,
                    human_input_suspension=True,
                    metadata=getattr(ctx, "llm_metadata", None),
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    on_llm_call_before=on_llm_call_before if session_id else reserve_llm_call,
                    on_llm_usage_settled=settle_llm_call,
                    on_context_compacted=on_context_compacted if session_id else None,
                    on_context_checkpoint=on_context_checkpoint if session_id else None,
                    context_archive_reader=read_context_archive if session_id else None,
                    on_round_end=on_round_end,
                    on_round_end_strict=on_round_end_strict,
                    output_schema=output_schema,
                    **({"final_response_contract": DelegatedAnswerLengthContract(answer_budget),
                        "turn_context": answer_budget_note}
                       if answer_budget is not None and output_schema is None else {}),
                    runtime_run_id=delegation_idempotency_key or None,
                    forced_tool_calls=(
                        _delegation_resume_forced_calls(
                            delegation_resume,
                            approval_token,
                        )
                        if delegation_resume is not None and publication_resume is None
                        else [
                            {
                                "name": "invoke_skill",
                                "arguments": {
                                    "skill_id": ref,
                                    "input": prompt,
                                },
                            }
                            for ref in required_skill_refs
                        ] or None
                    ),
                )
        # Each provider request above is durably settled. This collector is
        # only attribution for the parent checkpoint/result; record the rounds
        # produced by this invocation so resumed history is not merged twice.
        from packages.core.ai.runtime.nested_usage import runtime_record_nested_usage

        if record_nested_usage:
            runtime_record_nested_usage(
                _delegation_usage_delta(result.usage, initial_usage)
            )
        from packages.core.ai.runtime.control import (
            is_human_input_suspension_control,
        )

        if (
            result.stop_reason == "waiting_resource"
            and is_human_input_suspension_control(result.control)
        ):
            pause_payload = _delegated_input_pause_payload(
                result,
                run_id=run_id,
                idempotency_key=delegation_idempotency_key,
                sub=sub,
                agent=agent,
            )
            question = pause_payload["content"]
            # Files the child wrote before asking stay in the parent ledger.
            published = _publish_delegated_child_ledger(run_id, result)
            if on_human_input_suspension is not None:
                callback_result = on_human_input_suspension(dict(result.control or {}), pause_payload)
                if isawaitable(callback_result):
                    await callback_result
            if session_id:
                async with async_session() as db:
                    await finish_delegation_session(
                        db, session_id=session_id, run_id=run_id,
                        messages=result.messages, context_usage=result.usage or {},
                    )
            if event_sink is not None and event_sink.active:
                event_sink.emit_sub_agent_event({
                    **event_base,
                    "event_type": "blocked",
                    "status": "blocked",
                    "updated_at": _event_time(),
                    "content": question[:1200],
                    "rounds": result.rounds,
                    "tool_calls_made": list(result.tool_calls_made or [])[:20],
                    "stop_reason": "waiting_resource",
                    "error": None,
                })
            # The pause is no effect of its own only with the child's ledger
            # merged into the parent's; otherwise it stays unconfirmed.
            return _delegated_pause_result(pause_payload, published=published)
        content = runtime_delegated_child_content(result)
        transcript = list(result.messages or [])
        if not transcript:
            transcript = [
                *prior_messages,
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": content},
            ]
        context_usage = (result.usage or {}).get("context_window_last") or runtime_context_window_usage(
            system_prompt=next((m["content"] for m in transcript if m.get("role") == "system"), ctx.system_prompt),
            tools=ctx.tools,
            messages=[m for m in transcript if m.get("role") != "system"],
            context_window_tokens=effective_context_window_tokens(getattr(ctx, "model", None)),
            output_reserve_tokens=int(getattr(ctx, "max_tokens", None) or RUNTIME_AGENTIC_MAX_TOKENS),
            compaction_ratio=LOOP_COMPACT_RATIO,
        )
        from packages.core.ai.runtime.context_window import context_source_manifest
        current_sections = getattr(getattr(ctx, "prompt_context", None), "runtime_context_sections", None)
        context_sources = [
            *(context_source_manifest(current_sections) if current_sections is not None else getattr(ctx, "context_sources", [])),
            *context_tool_manifest(ctx.tools, set(ctx.allowed_tool_names or [])),
        ]
        forced_error = None
        if result.stop_reason in {"hitl_required", "needs_hitl"} and nested_hitl:
            tool_name, tool_arguments, hitl = nested_hitl[-1]
            persisted = await _persist_delegation_resume(
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                sub=sub,
                delegation_session_id=session_id,
                prompt=prompt,
                max_rounds=max_rounds,
                delegation_idempotency_key=delegation_idempotency_key or None,
                task_id=task_id or ctx.task_id,
                step_id=step_id,
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                hitl=hitl,
                tool_call_id=(
                    str(result.control.get("tool_call_id") or "") or None
                    if isinstance(result.control, dict)
                    else None
                ),
                remaining_tool_calls=(
                    list(result.control.get("remaining_tool_calls") or [])
                    if isinstance(result.control, dict)
                    else []
                ),
                disable_followup_tools=(
                    bool(result.control.get("disable_followup_tools"))
                    if isinstance(result.control, dict)
                    else False
                ),
                tool_continuations=(
                    dict(result.control.get("tool_continuations") or {})
                    if isinstance(result.control, dict)
                    else {}
                ),
                parent_step_id=parent_step_id,
                parent_lease_id=parent_lease_id,
                parent_execution_claim_id=parent_execution_claim_id,
            )
            if persisted:
                # Files the child wrote before the approval stop stay in the parent ledger.
                _publish_delegated_child_ledger(run_id, result)
                if session_id:
                    async with async_session() as db:
                        await finish_delegation_session(
                            db, session_id=session_id, run_id=run_id,
                            messages=transcript, context_usage=context_usage,
                            context_sources=context_sources,
                        )
                if event_sink is not None and event_sink.active:
                    event_sink.emit_sub_agent_event({
                        **event_base,
                        "event_type": "blocked",
                        "status": "blocked",
                        "updated_at": _event_time(),
                        "content": content[:1200],
                        "rounds": result.rounds,
                        "tool_calls_made": list(result.tool_calls_made or [])[:20],
                        "stop_reason": result.stop_reason,
                        "error": result.error,
                    })
                return _dumps(hitl)
            forced_error = "delegated_approval_resume_unavailable"
        child = runtime_delegated_child_result(
            result,
            run_id=run_id,
            session_id=session_id,
            idempotency_key=delegation_idempotency_key,
            sub=sub,
            agent=agent,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id or ctx.task_id,
            tool_results=current_tool_results,
            output_schema=output_schema,
            content=content,
            forced_error=forced_error,
        )
        if session_id:
            # An interrupted child parks its session until the parent checks it.
            async with async_session() as db:
                await finish_delegation_session(
                    db, session_id=session_id, run_id=run_id,
                    messages=transcript, context_usage=context_usage,
                    context_sources=context_sources,
                    run_result={
                        "status": child.status,
                        "content": content,
                        "agent_subscription_id": sub.id,
                        "agent_name": agent.name,
                        "error": child.error,
                        **child.replay_fields,
                    },
                )
        if event_sink is not None and event_sink.active:
            event_sink.emit_sub_agent_event({
                **event_base,
                "event_type": "completed" if child.status == "completed" else child.status,
                "status": child.status,
                "updated_at": _event_time(),
                "content": content[:1200],
                "rounds": result.rounds,
                "tool_calls_made": list(result.tool_calls_made or [])[:20],
                "stop_reason": result.stop_reason,
                "error": child.error,
            })
        _publish_delegated_child_ledger(run_id, result)
        return _dumps(child.payload)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
        if result is not None and getattr(result, "tool_evidence", None):
            _publish_delegated_child_ledger(run_id, result)
        if session_id and run_id:
            try:
                from packages.core.database import async_session
                from packages.core.services.agent_delegation_session import interrupt_delegation_session

                async with async_session() as db:
                    await interrupt_delegation_session(
                        db, session_id=session_id, run_id=run_id,
                    )
            except Exception:
                logger.warning("Failed to mark delegated session interrupted", exc_info=True)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if event_sink is not None and event_sink.active and event_base:
            event_sink.emit_sub_agent_event({
                **event_base,
                "event_type": "failed",
                "status": "failed",
                "updated_at": _event_time(),
                "error": str(exc),
            })
        logger.warning(
            "workspace service delegation failed: workspace=%s",
            workspace_id,
            exc_info=True,
        )
        return _dumps({
            "error": "workspace_service_delegation_failed",
            "message": str(exc),
        })
