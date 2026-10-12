"""Server-rendered Strategist execution facts remain distinct from strategy."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.strategist import service
from packages.core.strategist.execution_presentation import (
    execution_review_metadata,
    format_execution_notes,
)
from packages.core.strategist.proposal import ExecutionReference, ExecutionReview, Proposal

pytestmark = pytest.mark.oss_regression


def _proposal(*references: ExecutionReference, notes: str | None = "Try a smaller batch.") -> Proposal:
    return Proposal(
        review_id="review-1", summary="Next steps", tasks=[], notes=notes,
        execution_review=ExecutionReview(revision="snapshot-1", references=list(references)),
    )


def _snapshot(*facts: dict) -> dict:
    return {
        "status": "available", "observed_at": "2026-09-25T12:00:00+00:00",
        "revision": "snapshot-1", "tasks": list(facts),
    }


def _fact(task_id: str, *, state: str = "blocked", blocker: dict | None = None) -> dict:
    return {
        "task_id": task_id, "title": "Prepare evidence", "task_status": "blocked",
        "observed_state": state, "plan_id": "plan-1", "plan_status": "running",
        "step_counts": {"done": 2, "pending": 1}, "current_blocker": blocker,
        "latest_error": {"ref": "task_log:log-1", "code": "earlier_failure",
                         "message": "A prior attempt failed."},
        "revision": "task-revision-1", "owner_service_key": "writer",
    }


@pytest.mark.parametrize("scope,label", [
    ("still_missing", "required capability still missing"),
    ("configuration_changed", "tool scope now covers previous gap; retry/recheck needed"),
    ("unavailable", "current capability scope unverified"),
])
def test_execution_notes_are_frozen_host_facts_not_acceptance_or_current_error(scope, label):
    blocker = {"ref": "capability:task-1:hash", "source_task_id": "task-1",
               "scope_status": scope, "current_missing_capabilities": ["web.safe_search"],
               "affected_agents": [{"service_key": "research"}]}
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="plan-1", observed_state="blocked",
        blocker_ref=blocker["ref"],
    ))
    snapshot = _snapshot(_fact("task-1", blocker=blocker))

    rendered = format_execution_notes(proposal, snapshot)
    metadata = execution_review_metadata(proposal, snapshot)

    assert "observed at 2026-09-25T12:00:00+00:00; not live status" in rendered
    assert "- Prepare evidence: blocked" in rendered
    assert "2 step(s) done (not Task acceptance)" in rendered
    assert label in rendered
    if scope == "still_missing":
        assert "web.safe_search for research" in rendered
    assert "past error earlier_failure (not current cause)" in rendered
    assert proposal.notes == "Try a smaller batch."
    assert metadata["source"] == "server_observation"
    assert metadata["strategy_notes"] == proposal.notes
    assert metadata["facts"][0]["current_blocker"] == blocker
    assert metadata["facts"][0]["latest_error"]["message"] == "A prior attempt failed."
    assert metadata["facts"][0]["owner_service_key"] == "writer"


def test_delivered_and_waiting_coordinator_do_not_confuse_step_progress_with_review():
    delivered = _fact("task-delivered", state="delivered")
    waiting = _fact("task-waiting", state="awaiting_coordinator")
    proposal = _proposal(
        ExecutionReference(task_id="task-delivered", plan_id="plan-1", observed_state="delivered"),
        ExecutionReference(task_id="task-waiting", plan_id="plan-1", observed_state="awaiting_coordinator"),
    )
    rendered = format_execution_notes(proposal, _snapshot(delivered, waiting))
    assert "- Prepare evidence: delivered" in rendered
    assert "- Prepare evidence: steps recorded; waiting for next execution update" in rendered
    assert rendered.count("not Task acceptance") == 1
    assert "awaiting Task review" not in rendered


def test_task_title_is_bounded_for_existing_plaintext_card():
    fact = _fact("task-title")
    fact["title"] = "[Untrusted] " + "X" * 100
    proposal = _proposal(ExecutionReference(
        task_id="task-title", plan_id="plan-1", observed_state="blocked",
    ))
    rendered = format_execution_notes(proposal, _snapshot(fact))
    assert "- [Untrusted] " in rendered
    assert "X" * 71 not in rendered
    assert "task-title" not in rendered


def test_execution_presentation_rejects_mismatched_or_missing_citations():
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="wrong-plan", observed_state="blocked",
    ))
    snapshot = _snapshot(_fact("task-1"))
    assert execution_review_metadata(proposal, snapshot) is None
    assert format_execution_notes(proposal, snapshot) is None

    proposal.execution_review.references[0].plan_id = "plan-1"
    proposal.execution_review.references[0].observed_state = "delivered"
    assert execution_review_metadata(proposal, snapshot) is None

    proposal.execution_review.references[0].observed_state = "blocked"
    proposal.execution_review.revision = "stale"
    assert execution_review_metadata(proposal, snapshot) is None

    historical = Proposal(review_id="old", summary="Old proposal", tasks=[])
    assert execution_review_metadata(historical, snapshot) is None
    assert format_execution_notes(historical, snapshot) is None


def test_empty_citations_keep_revision_auditable_without_an_execution_note():
    proposal = _proposal()
    metadata = execution_review_metadata(proposal, _snapshot(_fact("task-1")))
    assert metadata["revision"] == "snapshot-1"
    assert metadata["references"] == [] and metadata["facts"] == []
    assert format_execution_notes(proposal, _snapshot(_fact("task-1"))) is None


@pytest.mark.asyncio
async def test_proposal_chat_receipt_keeps_execution_metadata_and_strategy_notes(monkeypatch):
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="plan-1", observed_state="blocked",
    ))
    metadata = execution_review_metadata(proposal, _snapshot(_fact("task-1")))
    proposal.notes = format_execution_notes(proposal, _snapshot(_fact("task-1"))) + "\n\nStrategy notes:\nTry a smaller batch."
    post = AsyncMock(return_value=SimpleNamespace(id="message-1"))
    monkeypatch.setattr(service.chat_service, "post_message", post)
    monkeypatch.setattr(service, "_proposal_basis_reports", AsyncMock(return_value=[]))
    workspace = SimpleNamespace(id="workspace-1", entity_id="entity-1")

    await service._post_proposal_chat(
        workspace, proposal, [], db=object(), execution_review=metadata,
    )

    payload = post.await_args.kwargs
    assert payload["message_kind"] == "proposal"
    assert payload["meta"]["proposal"]["execution_review"] == metadata
    assert payload["meta"]["proposal"]["notes"] == proposal.notes
    assert "Execution facts" in payload["body"]
    assert "Strategy notes" in payload["body"]
    assert payload["meta"]["proposal"]["summary"] == "Next steps"
