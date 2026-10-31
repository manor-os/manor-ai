"""Record cited Strategist execution observations without model-authored status prose.

These are frozen review-time facts for the review record, not a live Task
status endpoint, an approval/dispatch decision, or text for the owner's card.
The Proposal's own notes remain strategy.
"""
from __future__ import annotations

from typing import Any

from packages.core.strategist.proposal import Proposal


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
