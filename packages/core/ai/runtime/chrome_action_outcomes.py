"""Run-scoped execution receipts and explicit, read-only condition verification.

These receipts certify execution/observation, never the user's whole business
goal. They do not grant authority or replay browser actions.
"""

from __future__ import annotations

import json
from secrets import token_urlsafe
from typing import Any

from packages.core.ai.chrome_results import normalize_chrome_result
from packages.core.ai.runtime.chrome_observation_conditions import (
    action_scope, condition_for_action, observation_fields, observed_condition, scoped_feedback,
)
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult

CONTRACT = "chrome_action_v1"
_MAX_RECEIPTS = 64
_MAX_PAGE_TEXT = 32_000


def pending_action_receipt(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    receipt = payload.get("action_receipt")
    return (
        isinstance(receipt, dict) and receipt.get("contract") == CONTRACT
        and (receipt.get("execution"), receipt.get("verification")) in {
            ("executed", "pending"), ("acknowledged", "pending"), ("unknown", "unverified"),
        }
        and bool(receipt.get("action_id"))
        and payload.get("error") in (None, "", "tool_error")
    )


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _page(payload: dict[str, Any]) -> dict[str, Any]:
    parts = [_text(payload.get(key)) for key in ("page_text", "pageContent", "visible_text", "page_text_sample")]
    for key in ("page_feedback", "visible_text_blocks"):
        items = payload.get(key)
        if isinstance(items, list):
            parts.extend(_text(item.get("text")) for item in items if isinstance(item, dict))
    text = "\n".join(part for part in parts if part)
    return {
        "snapshot_id": payload.get("snapshot_id"), "url": payload.get("url"),
        "text": text[:_MAX_PAGE_TEXT], "truncated": len(text) > _MAX_PAGE_TEXT,
        **observation_fields(payload),
    }


def _verify(state: dict[str, Any], payload: dict[str, Any], request: dict[str, Any], *, automatic: bool = False) -> dict[str, Any]:
    action_id = request.get("action_id") or state.get("latest_action_by_tab", {}).get(str(payload.get("tabId")))
    record = state.get("action_receipts", {}).get(action_id) if isinstance(action_id, str) else None
    result = {"contract": CONTRACT, "action_id": action_id, "status": "unverified"}
    if not isinstance(record, dict):
        return {**result, "reason": "action_receipt_not_found"}
    tab_id = payload.get("tabId")
    before, after = record.get("before") or {}, _page(payload)
    expected = _text(request.get("text"))
    condition_evidence = None
    reason = None
    feedback = scoped_feedback(record, after, expected) if expected else []
    negative = bool(feedback) and (request.get("outcome") == "failed" or any(
        item.get("severity") == "error" for item in feedback
    ))
    if payload.get("ok") is not True or payload.get("error"):
        reason = "observation_failed"
    elif tab_id != record["tabId"] or tab_id is None:
        reason = "different_tab"
    elif state.get("latest_action_by_tab", {}).get(str(tab_id)) != action_id:
        reason = "intervening_action"
    elif not before.get("snapshot_id"):
        reason = "baseline_unavailable"
    elif not before.get("url") or before["url"] != after.get("url"):
        reason = "page_changed"
    elif record["receipt"]["verification"] == "verified" and (
        not expected or expected == record.get("evidence", {}).get("text")
    ):
        return {**record["evidence"], "status": "already_verified"}
    elif record["receipt"]["verification"] != "pending":
        reason = "action_not_awaiting_verification"
    elif not after.get("snapshot_id") or after["snapshot_id"] == before["snapshot_id"]:
        reason = "fresh_snapshot_required"
    elif record.get("condition") and not negative and request.get("outcome") != "failed":
        condition_evidence = observed_condition(record["condition"], after, payload)
        if condition_evidence is None:
            reason = "condition_not_observed"
    elif not expected or len(expected) > 500:
        reason = "bounded_expected_text_required"
    elif before.get("truncated"):
        reason = "baseline_unavailable"
    elif expected in before["text"]:
        reason = "condition_predates_action"
    elif not feedback:
        reason = "scoped_feedback_required"
    if reason:
        return {**result, "reason": reason}
    proof = {**result, "status": "failed" if negative else "verified", "tabId": tab_id,
             "snapshot_id": after["snapshot_id"],
             **(condition_evidence or {"text": expected, **(record.get("scope") or {})})}
    record["receipt"] = {**record["receipt"], "verification": proof["status"]}
    record["evidence"] = proof
    return proof


def record_action_outcome(*, tool_name: str, arguments: dict[str, Any], result: Any,
                          state: dict[str, Any]) -> Any:
    """Enrich actual Chrome results before streaming, persistence or compaction."""
    from packages.core.ai.runtime.chrome_routing import _chrome_tool_mutates_page

    mutates_page = _chrome_tool_mutates_page(tool_name, arguments)
    if tool_name != "mcp__chrome__read_page" and not mutates_page:
        return result
    # MCP display text cannot override the authoritative execution payload.
    source = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
    try:
        payload = json.loads(source) if isinstance(source, str) else source
    except (ValueError, TypeError):
        return result
    if not isinstance(payload, dict):
        return result
    payload = normalize_chrome_result(payload)
    # A provider/page cannot mint Runtime verification relationships.
    payload = {key: value for key, value in payload.items()
               if key not in {"action_receipt", "action_verification", "pending_verification"}}
    if tool_name == "mcp__chrome__read_page":
        request = arguments.get("verification")
        if isinstance(request, dict):
            payload["action_verification"] = _verify(state, payload, request)
        else:
            latest = state.get("latest_action_by_tab", {}).get(str(payload.get("tabId")))
            record = state.get("action_receipts", {}).get(latest, {})
            if record.get("condition") and record.get("receipt", {}).get("verification") == "pending":
                payload["action_verification"] = _verify(state, payload, {}, automatic=True)
        latest = state.get("latest_action_by_tab", {}).get(str(payload.get("tabId")))
        record = state.get("action_receipts", {}).get(latest, {})
        if record.get("receipt", {}).get("verification") == "pending":
            payload["pending_verification"] = {"action_id": latest, "tool": record["tool"],
                "condition": record.get("condition", {}).get("kind", "explicit_page_evidence"),
                "next_step": "Read again when ready; for new page_feedback from the same editor, pass verification.text without an action_id. Text cannot replace typed preview/field evidence. Do not replay the action."}
        if payload.get("ok") is True and not payload.get("error") and payload.get("tabId") is not None:
            pages = state.setdefault("action_observations", {})
            pages[str(payload["tabId"])] = _page(payload)
            while len(pages) > 16:
                pages.pop(next(iter(pages)))
    elif mutates_page:
        executed = payload.get("action_executed")
        # Legacy successful replies acknowledge the tool call without an
        # explicit physical-execution fact. Preserve that distinction; only an
        # explicit unknown execution requires reconciliation instead of success.
        execution = ("executed" if executed is True else "not_executed" if executed is False
                     else "acknowledged" if executed is None and payload.get("ok") is True else "unknown")
        verification = payload.get("verification")
        tab_id = payload.get("tabId", arguments.get("tabId"))
        before = state.get("action_observations", {}).get(str(tab_id)) or {}
        condition = condition_for_action(tool_name, arguments, payload, before)
        pending = (execution in {"executed", "acknowledged"} and payload.get("ok") is True
                   and not payload.get("error") and (condition is not None or (
                       isinstance(verification, dict) and verification.get("status") == "pending")))
        failed = payload.get("ok") is False or bool(payload.get("error"))
        receipt = {"contract": CONTRACT, "action_id": f"action-{token_urlsafe(12)}",
                   "execution": execution,
                   "verification": ("pending" if pending else "failed" if failed
                                    else "unverified" if execution == "unknown" else "not_requested")}
        payload["action_receipt"] = receipt
        records = state.setdefault("action_receipts", {})
        records[receipt["action_id"]] = {
            "tool": tool_name, "tabId": tab_id, "receipt": receipt.copy(),
            "before": before if pending else None,
            "scope": action_scope(payload),
            **({"condition": condition} if condition else {}),
        }
        if execution != "not_executed" and tab_id is not None:
            state.setdefault("latest_action_by_tab", {})[str(tab_id)] = receipt["action_id"]
        while len(records) > _MAX_RECEIPTS:
            records.pop(next(iter(records)))
        # A verified post-action snapshot is the baseline for the next action,
        # never evidence that this action's business outcome was achieved.
        post = payload.get("post_action_page_state")
        if tab_id is not None and isinstance(post, dict) and post.get("state_verified") is True:
            if pending and condition is not None:
                proof = _verify(state, post, {"action_id": receipt["action_id"]}, automatic=True)
                if proof["status"] == "verified":
                    payload["action_verification"] = proof
                    payload["action_receipt"] = records[receipt["action_id"]]["receipt"].copy()
            state.setdefault("action_observations", {})[str(tab_id)] = _page(post)
    return json.dumps(payload, ensure_ascii=False)
