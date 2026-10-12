"""Chat reply outcome derived from the host tool ledger, never model prose.

The process title of a chat reply (answered / completed / partial / blocked /
not_attempted) is read from what this turn's calls actually returned. The
model only writes the answer text; it cannot raise the status above what the
ledger shows.

Per operation the last attempt decides: an operation that failed and then
succeeded is recovered (the web timeline shows "processed after retry"), one
whose last attempt failed is unfinished. Discovery calls (search_tools,
acquire_capabilities, manor search) look capabilities up; they never complete
or fail the user's request on their own.
"""
from __future__ import annotations

from typing import Any, Iterable

_ATTEMPT_STAGES = frozenset({"execution", "inventory", "delegation", "authorization"})
_MAX_LIMITATIONS = 16
_MAX_REFS = 32


def _attempts(observations: Iterable[dict]) -> dict[str, list[dict]]:
    """Attempt observations grouped by operation, in first-seen order."""
    by_operation: dict[str, list[dict]] = {}
    for item in observations:
        if not isinstance(item, dict) or item.get("stage") not in _ATTEMPT_STAGES:
            continue
        operation = str(item.get("operation") or item.get("tool") or "").strip()
        if not operation:
            continue
        by_operation.setdefault(operation, []).append(item)
    return by_operation


def _succeeded(item: dict) -> bool:
    return item.get("result") == "returned" and not item.get("authorization_blocked")


def _limitation(operation: str, item: dict) -> str:
    if item.get("authorization_blocked"):
        detail = str(item.get("error") or "permission required")
        return f"{operation}: {detail}"[:2000]
    detail = str(item.get("message") or item.get("error") or "failed").strip()
    return f"{operation}: {detail}"[:2000]


def derive_chat_outcome(
    observations: Iterable[dict],
    *,
    pending_handoffs: Iterable[dict] = (),
    unconfirmed_calls: Iterable[dict] = (),
    stop_reason: str | None = None,
    gate_limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Return ``{status, evidence_refs, limitations, claims}`` for one turn."""
    attempts = _attempts(observations)
    claims: list[dict[str, Any]] = []
    limitations: list[str] = []
    refs: list[str] = []
    succeeded = failed = 0
    for operation, items in attempts.items():
        last = items[-1]
        call_ids = [str(item.get("call_id")) for item in items if item.get("call_id")]
        refs.extend(call_ids)
        if _succeeded(last):
            succeeded += 1
            claims.append({"operation": operation, "status": "completed", "evidence_refs": call_ids})
        else:
            failed += 1
            claims.append({"operation": operation, "status": "blocked", "evidence_refs": call_ids})
            limitations.append(_limitation(operation, last))

    # An unconfirmed effect may or may not have happened. When its operation
    # later succeeded the request is still served; say so instead of hiding it.
    recovered_ops = {claim["operation"] for claim in claims if claim["status"] == "completed"}
    for call in unconfirmed_calls:
        if isinstance(call, dict) and call.get("operation") in recovered_ops:
            limitations.append(f"{call['operation']}: an earlier attempt's effect is unconfirmed")
            break

    open_handoffs = [handoff for handoff in pending_handoffs if isinstance(handoff, dict)]
    for handoff in open_handoffs:
        skill = str(handoff.get("skill") or "skill")
        stage = str(handoff.get("stage") or "")
        limitations.append(
            f"{skill}: stopped at stage '{stage}' before the requested result"
            if handoff.get("kind") == "skill_continuation"
            else f"{skill}: the requested deliverable was not saved"
        )
    gate_notes = [str(note) for note in gate_limitations if str(note or "").strip()]
    limitations.extend(gate_notes)

    if not attempts:
        status = "not_attempted" if stop_reason == "max_rounds" else "answered"
    elif failed and succeeded:
        status = "partial"
    elif failed:
        status = "blocked"
    else:
        status = "completed"
    # Unfinished delivery, an exhausted round budget or a gate the model never
    # satisfied cap a completed turn at partial; they never raise a status.
    if status == "completed" and (open_handoffs or gate_notes or stop_reason == "max_rounds"):
        status = "partial"
    if status == "answered" and gate_notes:
        status = "not_attempted"

    deduped = list(dict.fromkeys(limitations))[:_MAX_LIMITATIONS]
    return {
        "status": status,
        "evidence_refs": list(dict.fromkeys(refs))[-_MAX_REFS:],
        "limitations": deduped,
        "claims": claims[:16],
    }


def derive_chat_outcome_from_ledger(
    ledger: Any, *, stop_reason: str | None = None, gate_limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """``derive_chat_outcome`` over a live ``RuntimeToolEvidence``."""
    return derive_chat_outcome(
        list(getattr(ledger, "observations", {}).values()),
        pending_handoffs=ledger.pending_sandbox_handoffs() if hasattr(ledger, "pending_sandbox_handoffs") else (),
        unconfirmed_calls=list(getattr(ledger, "unconfirmed_calls", []) or []),
        stop_reason=stop_reason,
        gate_limitations=gate_limitations,
    )
