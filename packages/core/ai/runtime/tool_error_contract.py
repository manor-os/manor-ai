"""Shared diagnostic construction and model projection; never retry authority.

Handlers can describe failures with the same constructor. The model projection
runs after runtime controls and never rewrites host proofs or durable receipts.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from packages.core.ai.runtime.streams import runtime_tool_call_error
from packages.core.ai.runtime.streams import runtime_tool_path_memory_outcome
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult
from packages.core.services.sensitive_data import (
    sanitize_approval_credentials,
    sanitize_sensitive_payload,
)

TOOL_ERROR_CONTRACT = "tool_error"
TOOL_ERROR_MAX_CHARS = 12000


def tool_error_payload(
    *, tool_name: str, code: str, message: str, details: dict[str, Any] | None = None,
    internal_error: bool = False,
) -> dict[str, Any]:
    """Describe a failure, without selecting repairs or granting retry authority.

    Who can resolve it: by default the agent (another call, another path).
    ``internal_error=True`` declares a Manor platform fault that no person can
    resolve, read back by ``runtime_platform_error``: the run records it, and
    ``request_input`` refuses to park on a person until it is resolved.
    """
    details = dict(details or {})
    if internal_error:
        details.update(internal_error=True, requires_human=False, retryable=False)
    return {
        "contract": TOOL_ERROR_CONTRACT,
        "status": "error",
        "tool": tool_name,
        "error": code,
        "message": message,
        "details": details,
    }

# Under context pressure keep actionable diagnostics ahead of bulky output.
# This is ordering, not a whitelist: every original field survives when it fits.
_DIAGNOSTIC_KEYS = (
    # Preserve execution/verification evidence before bulky locator or output
    # diagnostics. These remain observations, never host retry authority.
    "ok", "status", "internal_error", "action_executed", "retryable", "do_not_replay", "verified",
    "website_upload_confirmed", "next_required_tool", "event_receipt",
    "action_receipt", "action_verification", "pending_verification",
    "contract", "action_id", "execution", "verification",
    "path", "schema_rule", "line", "column", "expected", "actual_type",
    "code", "message", "reason", "cause", "detail", "error",
    "field_paths", "source_saved", "contract_valid", "violations", "contract_violations",
    "validation_errors", "issues", "diagnostics",
    "file", "rule", "pointer", "repair_hint", "manor_hint", "fix_hint", "hint", "recovery_hint",
    "retry_policy", "next_step", "repair_strategy", "stderr", "exit_code",
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _bounded(value: Any, budget: int, *, key: str = "") -> Any:
    """Bound diagnostics while retaining JSON structure and explicit omissions."""
    if len(_dump(value)) <= budget:
        return value
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda pair: (
            _DIAGNOSTIC_KEYS.index(pair[0]) if pair[0] in _DIAGNOSTIC_KEYS else len(_DIAGNOSTIC_KEYS),
        ))
        kept: dict[str, Any] = {}
        for key, child in items:
            # Reserve space for an honest omitted-field count. Bound each
            # large child so one provider blob cannot consume all diagnostics.
            remaining = budget - len(_dump(kept)) - len(_dump(key)) - 32
            if remaining < 24:
                break
            child_budget = remaining if len(items) == 1 else max(24, int(remaining * .65))
            candidate = {**kept, key: _bounded(child, child_budget, key=key)}
            if len(_dump(candidate)) + 24 > budget:
                break
            kept = candidate
        if len(kept) < len(items):
            kept["_omitted_fields"] = len(items) - len(kept)
        return kept
    if isinstance(value, list):
        kept_items = []
        for child in value:
            remaining = budget - len(_dump(kept_items)) - 32
            if remaining < 24:
                break
            candidate = [*kept_items, _bounded(child, remaining, key=key)]
            if len(_dump(candidate)) + 24 > budget:
                break
            kept_items = candidate
        if len(kept_items) < len(value):
            kept_items.append({"_omitted_items": len(value) - len(kept_items)})
        return kept_items
    if isinstance(value, str):
        # Never present a partial source/template as executable complete code.
        if key in {"code", "html", "source", "template", "minimal_example"} or key.endswith((".html", ".js", ".py")):
            return "[code omitted]"
        marker = "… [truncated]"
        keep = max(0, budget - len(_dump(marker)))
        result = value[:keep] + marker
        while len(_dump(result)) > budget and keep:
            keep //= 2
            result = value[:keep] + marker
        return result
    return value


def model_tool_error_result(tool_name: str, result: Any, *, max_chars: int) -> str | None:
    """Project execution failures only; gates/success keep their own contracts.

    No arguments are rewritten, no tools invoked, and no host proof is minted.
    Reprojection (including history compaction) is idempotent.
    """
    error_message = runtime_tool_call_error(result)
    if error_message is None:
        return None
    payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("contract"), str)
        and isinstance(payload.get("status"), str)
        and isinstance(payload.get("error"), str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", payload["error"])
        and isinstance(payload.get("message"), str)
        and isinstance(payload.get("details"), dict)
    ):
        # A provider can supply the same shape. The marker is not authority:
        # reconstruct only defined fields; never trust arbitrary metadata.
        envelope = tool_error_payload(
            tool_name=tool_name, code=payload["error"], message=payload["message"],
            details=payload["details"],
        )
        details = envelope.pop("details")
        if payload["status"] != "error":
            details["source_status"] = payload["status"]
        truncation = payload.get("truncation")
        if isinstance(truncation, dict) and type(truncation.get("original_chars")) is int:
            envelope["truncation"] = {"original_chars": min(max(0, truncation["original_chars"]), 10**12)}
        envelope["details"] = details
    else:
        details = dict(payload) if isinstance(payload, dict) else {"raw_result": str(result)}
        nested_error = details.get("error")
        nested_error = nested_error if isinstance(nested_error, dict) else {}
        code = details.get("code") or nested_error.get("code") or details.get("error")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", code):
            code = "tool_execution_failed"
        message = details.get("message") or nested_error.get("message") or details.get("reason") or details.get("detail") or error_message
        envelope = tool_error_payload(
            tool_name=tool_name, code=code, message=str(message), details=details,
        )
    envelope = sanitize_sensitive_payload(sanitize_approval_credentials(envelope))
    serialized = _dump(envelope)
    if len(serialized) <= max_chars:
        return serialized

    # Keep the envelope valid even when an old round is compacted a second
    # time. Never degrade an error to an unstructured prefix/preview.
    compacted = {key: envelope[key] for key in ("contract", "status", "tool", "error", "message")}
    compacted["tool"] = _bounded(compacted["tool"], 100)
    compacted["message"] = _bounded(compacted["message"], max(100, min(900, max_chars // 4)))
    compacted["truncation"] = envelope.get("truncation") or {"original_chars": len(serialized)}
    budget = max(64, max_chars - len(_dump(compacted)) - 14)
    compacted["details"] = _bounded(envelope.get("details", {}), budget)
    return _dump(compacted)


PLATFORM_ERROR_DETAILS_MAX_CHARS = 4000
_ERROR_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")


def tool_error_summary(result: Any) -> str | None:
    """One line naming a failed tool result: ``code: message`` for this
    contract (its ``error`` field is only the code), else the decoder's detail."""
    detail = runtime_tool_call_error(result)
    if detail is None:
        return None
    payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return detail
    if not isinstance(payload, dict) or payload.get("contract") != TOOL_ERROR_CONTRACT:
        return detail
    message = str(payload.get("message") or "").strip()
    return f"{detail}: {message}" if message and message != detail else detail


def runtime_platform_error(tool_name: str, result: Any) -> dict[str, Any] | None:
    """A Manor platform fault a failed tool result declares, or None.

    Only Manor's own handlers mark ``internal_error: true``: an invariant broke
    on the platform side, so no grant, approval or repeated call repairs it.
    ``retryable: false`` alone is not this; setup and governance blockers the
    user can resolve use it too. An external MCP provider cannot declare a
    Manor fault. Returns the error code and the bounded, redacted payload.
    """
    if runtime_tool_call_error(result) is None:
        return None
    if tool_name.startswith("mcp__") and not tool_name.startswith("mcp__manor_"):
        return None
    payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    parts = [part for part in (payload, payload.get("details"), payload.get("error")) if isinstance(part, dict)]
    marked = next((part for part in parts if part.get("internal_error") is True), None)
    if marked is None or any(part.get("retryable") is True for part in parts):
        return None
    candidates = [part.get(key) for part in parts for key in ("error", "code")]
    code = next(
        (value for value in candidates if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value)),
        "internal_error",
    )
    details = sanitize_sensitive_payload(sanitize_approval_credentials(payload))
    return {"code": code, "details": _bounded(details, PLATFORM_ERROR_DETAILS_MAX_CHARS)}


def model_tool_diagnostic_result(tool_name: str, result: Any, *, max_chars: int) -> str | None:
    """Preserve failed validation on a successful action without erasing success.

    For example, a draft can be saved successfully but fail its output contract.
    Validation diagnostics are observations, not failure/no-effect receipts.
    """
    if runtime_tool_call_error(result) is not None or runtime_tool_path_memory_outcome(result) is None:
        return None
    payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict) or payload.get("contract_valid") is not False:
        return None
    if not any(payload.get(key) for key in ("contract_violations", "violations", "diagnostics")):
        return None
    payload = sanitize_sensitive_payload(sanitize_approval_credentials(payload))
    serialized = _dump(payload)
    if len(serialized) <= max_chars:
        return serialized
    # State and resource identity remain separate from bounded diagnostics;
    # do not replace successful status with the tool_error envelope.
    compacted = {
        key: payload[key]
        for key in ("status", "source_saved", "contract_valid")
        if key in payload
    }
    omitted_identity_fields: list[str] = []
    for key in ("session_id", "project_path", "source_revision"):
        if key not in payload:
            continue
        if len(_dump(payload[key])) > 180:
            omitted_identity_fields.append(key)
        else:
            compacted[key] = payload[key]
    if omitted_identity_fields:
        compacted["omitted_identity_fields"] = omitted_identity_fields
    compacted["truncation"] = {"original_chars": len(serialized)}
    remainder = {
        key: value for key, value in payload.items()
        if key not in compacted and key not in omitted_identity_fields
    }
    compacted.update(_bounded(remainder, max(64, max_chars - len(_dump(compacted)) - 4)))
    return _dump(compacted)


def _focus_child_observation(value: Any, *, key: str = "") -> Any:
    """Omit long authoring text before budgeting each child tool observation."""
    if isinstance(value, dict):
        return {
            child_key: _focus_child_observation(child, key=str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_focus_child_observation(child, key=key) for child in value]
    if isinstance(value, str) and len(value) > 240:
        if key in {"warnings", "warning", "error", "message", "reason"} or key.endswith(
            ("_id", "_path", "_url", "_policy", "_intent")
        ):
            return _bounded(value, 600, key=key)
        return f"[omitted {len(value)} chars of {key or 'text'} from model context]"
    return value


def _bounded_child_metadata(metadata: dict[str, Any], budget: int) -> dict[str, Any]:
    """Keep recent, citable facts whole; never manufacture partial call IDs.

    These are model-visible references only. The host still validates claims
    against the independent execution_evidence carrier, never this projection.
    """
    evidence = metadata.get("evidence")
    if not isinstance(evidence, list):
        return _bounded(metadata, budget)
    kept: dict[str, Any] = {}
    headers = {"delivery_assessment", "stop_reason", "goal_status", "evidence_ref_prefix", "evidence_ref_format"}
    for key, value in metadata.items():
        if key in headers and len(_dump({**kept, key: value})) + 48 <= budget:
            kept[key] = value

    remainder = {key: value for key, value in metadata.items()
                 if key not in headers | {"evidence", "evidence_omitted"}}
    available = max(0, budget - len(_dump(kept)) - 48)
    evidence_budget = int(available * .7) if remainder else available
    required = ("call_id", "operation", "stage", "result", "semantic_success_verified")
    facts = []
    sources = []
    for item in reversed(evidence):
        if not isinstance(item, dict) or not all(key in item for key in required):
            continue
        fact = {key: item[key] for key in required}
        if len(_dump([*facts, fact])) <= evidence_budget:
            facts.append(fact)
            sources.append(item)
    # Establish identities/outcomes before spending space on diagnostics. A
    # long failure message must not hide the latest failed call behind older
    # successes. Optional fields are added whole only when they still fit.
    for key in ("error", "parent_call_id", "failure_scope", "external_attempt",
                "authorization_blocked", "unavailable_provider_keys", "capability_availability", "message"):
        for fact, source in zip(facts, sources):
            if source.get(key):
                fact[key] = source[key]
                if len(_dump(facts)) > evidence_budget:
                    del fact[key]
    # Preserve chronological ordering so a second projection retains the same
    # latest facts. Omission counts are metadata, never fake evidence entries.
    previous_omitted = metadata.get("evidence_omitted")
    omitted = len(evidence) - len(facts) + (previous_omitted if type(previous_omitted) is int else 0)
    evidence_fields = {"evidence": list(reversed(facts)), "evidence_omitted": omitted}
    if len(_dump({**kept, **evidence_fields})) <= budget:
        kept.update(evidence_fields)
    remaining = max(0, budget - len(_dump(kept)) - 4)
    kept.update(_bounded(remainder, remaining))
    return kept


def model_tool_execution_result(tool_name: str, result: Any, *, max_chars: int) -> str | None:
    """Bound a child invocation for model context without changing its receipt.

    This is a projection, not an execution verdict. Keep the child execution
    scope and observations ahead of potentially huge business output so the
    parent can distinguish a completed child call from delivered work.
    """
    payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    metadata_key = next(
        (
            key for key in ("execution", "skill_execution")
            if isinstance(payload.get(key), dict)
            and payload[key].get("scope") == "skill_invocation"
        ),
        None,
    )
    if metadata_key is None:
        return None

    original = result if isinstance(result, str) else _dump(payload)
    if len(original) <= max_chars:
        return None
    sanitized = sanitize_sensitive_payload(sanitize_approval_credentials(payload))
    if not isinstance(sanitized, dict):
        return None
    metadata = sanitized.get(metadata_key)
    if not isinstance(metadata, dict):
        return None

    prior_truncation = sanitized.get("_tool_result_truncated")
    prior_truncation = prior_truncation if isinstance(prior_truncation, dict) else {}
    original_chars = prior_truncation.get("original_chars")
    if type(original_chars) is not int or original_chars < len(original):
        original_chars = len(original)
    prior_digest = prior_truncation.get("sha256")
    digest = (
        prior_digest if isinstance(prior_digest, str) and re.fullmatch(r"[a-f0-9]{64}", prior_digest)
        else hashlib.sha256(original.encode("utf-8")).hexdigest()
    )
    truncation = {
        "tool": str(tool_name)[:120],
        "original_chars": original_chars,
        "sha256": digest,
        "strategy": "skill_invocation",
        "scope": "model_context_only",
    }
    fixed: dict[str, Any] = {}
    for key in (
        "status", "stop_reason", "skill", "terminal_failure", "stop_parent",
        "retryable", "requires_human", "action_executed", "do_not_replay",
    ):
        if key in sanitized:
            fixed[key] = _bounded(sanitized[key], 120, key=key)
    fixed[metadata_key] = {"scope": "skill_invocation"}
    fixed["_tool_result_truncated"] = truncation

    preferred_metadata = (
        "delivery_assessment", "stop_reason", "goal_status", "evidence_ref_prefix", "evidence_ref_format", "evidence",
        "rounds", "tool_observations",
        "tools_used",
    )
    metadata_remainder = {
        key: metadata[key]
        for key in (*preferred_metadata, *metadata.keys())
        if key not in {"scope", "_omitted_fields"} and key in metadata
    }
    if "tool_observations" in metadata_remainder:
        metadata_remainder["tool_observations"] = _focus_child_observation(
            metadata_remainder["tool_observations"]
        )
    previous_metadata_omitted = metadata.get("_omitted_fields")
    previous_metadata_omitted = (
        previous_metadata_omitted
        if type(previous_metadata_omitted) is int and previous_metadata_omitted > 0 else 0
    )

    def business_priority(key: str) -> int:
        if key in {"id", "path", "url", "uri", "reference", "document_id", "artifact_id"} or key.endswith(
            ("_id", "_path", "_url", "_ref")
        ):
            return 0
        if key in {"references", "documents", "artifacts", "files"} or key.endswith(
            ("_ids", "_paths", "_urls", "_refs")
        ):
            return 1
        if key in {"summary", "content"}:
            return 2
        if key in {"result", "report", "transcript", "body"}:
            return 3
        return 2

    business = {
        key: sanitized[key]
        for key in sorted(
            (key for key in sanitized if key not in fixed and key != "_omitted_fields"),
            key=business_priority,
        )
    }
    identity_fields = {key: value for key, value in business.items() if business_priority(key) == 0}

    def bound_business(values: dict[str, Any], budget: int) -> dict[str, Any]:
        # A truncated path/URL/ID is unusable as a locator. Prefer complete
        # resource identities, and explicitly omit ones too large to carry.
        kept: dict[str, Any] = {}
        previous = values.get("_omitted_identity_fields")
        omitted = [key for key in previous if isinstance(key, str)] if isinstance(previous, list) else []
        for key, value in values.items():
            if key not in identity_fields:
                continue
            if len(_dump({**kept, key: value})) + 64 <= budget:
                kept[key] = value
            else:
                omitted.append(key)
        if omitted:
            kept["_omitted_identity_fields"] = list(dict.fromkeys(omitted))
        remainder = {key: value for key, value in values.items()
                     if key not in identity_fields and key != "_omitted_identity_fields"}
        kept.update(_bounded(remainder, max(0, budget - len(_dump(kept)) - 4)))
        return kept

    previous_top_omitted = sanitized.get("_omitted_fields")
    previous_top_omitted = (
        previous_top_omitted if type(previous_top_omitted) is int and previous_top_omitted > 0 else 0
    )

    # Reserve a useful child summary before spending the rest on diagnostics.
    # Rebuild at decreasing budgets rather than falling back to a raw prefix:
    # every returned projection remains parseable and states its omissions.
    for scale in (1.0, 0.75, 0.5, 0.25, 0.0):
        compacted = dict(fixed)
        variable_budget = max(0, max_chars - len(_dump(compacted)) - 24)
        summaries = {key: business[key] for key in ("summary", "content")
                     if isinstance(business.get(key), str)}
        if summaries and len(_dump(summaries)) <= int(variable_budget * .75 * scale):
            compacted.update(summaries)
        remaining_business = {key: value for key, value in business.items() if key not in compacted}
        variable_budget = max(0, max_chars - len(_dump(compacted)) - 24)
        business_reserve = min(len(_dump(remaining_business)), int(variable_budget * (
            .2 if any(key in compacted for key in summaries) else .6
        )))
        eligible_identities = {key: value for key, value in identity_fields.items()
                               if len(_dump({key: value})) + 64 <= int(variable_budget * .6)}
        if eligible_identities:
            business_reserve = max(business_reserve, min(
                len(_dump(eligible_identities)) + 64, int(variable_budget * .6),
            ))
        metadata_budget = max(0, int((variable_budget - business_reserve) * scale))
        bounded_metadata = _bounded_child_metadata(metadata_remainder, metadata_budget)
        if isinstance(bounded_metadata, dict):
            if previous_metadata_omitted:
                bounded_metadata["_omitted_fields"] = (
                    int(bounded_metadata.get("_omitted_fields") or 0)
                    + previous_metadata_omitted
                )
            compacted[metadata_key] = {
                "scope": "skill_invocation", **bounded_metadata,
            }
        remaining = max(0, max_chars - len(_dump(compacted)) - 20)
        bounded_business = bound_business(remaining_business, int(remaining * scale))
        if isinstance(bounded_business, dict):
            if previous_top_omitted:
                bounded_business["_omitted_fields"] = (
                    int(bounded_business.get("_omitted_fields") or 0)
                    + previous_top_omitted
                )
            compacted.update(bounded_business)
        serialized = _dump(compacted)
        if len(serialized) <= max_chars:
            return serialized

    # An exceptionally small history budget still gets structural state and
    # the child marker, never a non-JSON preview of the original result.
    minimal = {
        key: fixed[key] for key in ("status", "stop_reason") if key in fixed
    }
    minimal[metadata_key] = {"scope": "skill_invocation", "_omitted_fields": len(metadata_remainder)}
    minimal["_tool_result_truncated"] = {"original_chars": original_chars, "sha256": digest}
    if business or previous_top_omitted:
        minimal["_omitted_fields"] = len(business) + previous_top_omitted
    serialized = _dump(minimal)
    if len(serialized) <= max_chars:
        return serialized
    # Caller-supplied tiny budgets cannot carry every proof field. Shed
    # projection metadata before the child scope or execution status.
    minimal["_tool_result_truncated"].pop("sha256")
    for key in ("_tool_result_truncated", "_omitted_fields"):
        if len(_dump(minimal)) <= max_chars:
            break
        minimal.pop(key, None)
    if len(_dump(minimal)) > max_chars:
        minimal[metadata_key].pop("_omitted_fields", None)
    for key in ("stop_reason", "status", metadata_key):
        if len(_dump(minimal)) <= max_chars:
            break
        minimal.pop(key, None)
    return _dump(minimal) if len(_dump(minimal)) <= max_chars else "{}"
