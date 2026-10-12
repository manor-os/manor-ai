"""Present cited Strategist execution observations without model-authored status prose.

These are frozen review-time facts, not a live Task status endpoint or an
approval/dispatch decision. The Proposal's own notes remain strategy.
"""
from __future__ import annotations

from typing import Any

from packages.core.strategist.proposal import Proposal


_STATE_LABELS = {
    "not_started": "not started",
    "accepted": "queued",
    "assigned": "assigned",
    "planning": "Agent coordinating task",
    "awaiting_coordinator": "steps recorded; waiting for next execution update",
    "running": "running",
    "waiting": "parked; waiting for a scheduled time or media jobs",
    "waiting_input": "waiting for input",
    "blocked": "blocked",
    "delivered": "delivered",
    "completed_unverified": "marked complete; delivery unverified",
    "failed": "failed",
    "cancelled": "cancelled",
}
_BLOCKER_LABELS = {
    "still_missing": "required capability still missing",
    "configuration_changed": "tool scope now covers previous gap; retry/recheck needed",
    "unavailable": "current capability scope unverified",
}


def execution_review_metadata(proposal: Proposal, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Select only exact, already-validated Task citations from one observation."""
    review = getattr(proposal, "execution_review", None)
    if (
        not review or snapshot.get("status") != "available"
        or review.revision != snapshot.get("revision")
        or not isinstance(snapshot.get("observed_at"), str)
        or not snapshot["observed_at"]
    ):
        return None
    facts_by_id = {
        fact["task_id"]: fact for fact in snapshot.get("tasks", [])
        if isinstance(fact, dict) and isinstance(fact.get("task_id"), str)
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for reference in review.references:
        fact = facts_by_id.get(reference.task_id)
        if (
            fact is None or reference.task_id in seen
            or reference.plan_id != fact.get("plan_id")
            or reference.observed_state != fact.get("observed_state")
        ):
            return None
        blocker = fact.get("current_blocker")
        blocker_ref = blocker.get("ref") if isinstance(blocker, dict) else None
        if reference.blocker_ref is not None and reference.blocker_ref != blocker_ref:
            return None
        seen.add(reference.task_id)
        selected.append({
            "task_id": fact["task_id"],
            "title": fact.get("title"),
            "task_status": fact.get("task_status"),
            "observed_state": fact.get("observed_state"),
            "plan_id": fact.get("plan_id"),
            "plan_status": fact.get("plan_status"),
            "owner_service_key": fact.get("owner_service_key"),
            "owner_subscription_id": fact.get("owner_subscription_id"),
            "coordinator": fact.get("coordinator"),
            "step_counts": fact.get("step_counts") or {},
            "current_blocker": blocker,
            "latest_error": fact.get("latest_error"),
            "revision": fact.get("revision"),
        })
    return {
        "source": "server_observation",
        "observed_at": snapshot["observed_at"],
        "revision": snapshot["revision"],
        "references": [reference.model_dump(mode="json") for reference in review.references],
        "facts": selected,
        "strategy_notes": proposal.notes,
    }


def format_execution_notes(proposal: Proposal, snapshot: dict[str, Any]) -> str | None:
    """A compact card note; the full cited facts remain in structured metadata."""
    evidence = execution_review_metadata(proposal, snapshot)
    if evidence is None or not evidence["facts"]:
        return None
    lines = [f"Execution facts (observed at {evidence['observed_at']}; not live status):"]
    for index, fact in enumerate(evidence["facts"]):
        task_id = fact["task_id"]
        state = _STATE_LABELS.get(fact["observed_state"], "state unavailable")
        title = " ".join(str(fact.get("title") or f"Task {task_id}").split())[:70]
        line = f"- {title}: {state}"
        done = (fact.get("step_counts") or {}).get("done")
        if isinstance(done, int) and done > 0 and fact["observed_state"] != "delivered":
            line += f"; {done} step(s) done (not Task acceptance)"
        blocker = fact.get("current_blocker")
        scope = blocker.get("scope_status") if isinstance(blocker, dict) else None
        if scope in _BLOCKER_LABELS:
            line += f"; {_BLOCKER_LABELS[scope]}"
            if scope == "still_missing":
                missing = blocker.get("current_missing_capabilities") or []
                services = [owner.get("service_key") for owner in blocker.get("affected_agents") or []
                            if isinstance(owner, dict) and owner.get("service_key")]
                if missing:
                    line += ": " + ", ".join(str(item)[:80] for item in missing[:4])
                    if len(missing) > 4:
                        line += f" (+{len(missing) - 4} more)"
                if services:
                    names = list(dict.fromkeys(str(service)[:50] for service in services))
                    line += " for " + ", ".join(names[:2])
                    if len(names) > 2:
                        line += f" (+{len(names) - 2} more)"
        error = fact.get("latest_error")
        if isinstance(error, dict) and error.get("code"):
            code = "".join(char for char in str(error["code"])
                           if char.isalnum() or char in "._-")[:60]
            if code:
                line += f"; past error {code} (not current cause)"
        if len("\n".join([*lines, line])) > 900:
            lines.append(f"- {len(evidence['facts']) - index} additional cited Task(s) retained in the review record.")
            break
        lines.append(line)
    return "\n".join(lines)
