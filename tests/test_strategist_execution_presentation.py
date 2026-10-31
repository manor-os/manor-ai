"""Server-recorded Strategist execution facts remain distinct from strategy."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.strategist import service
from packages.core.strategist.execution_presentation import execution_review_metadata
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


def test_execution_metadata_keeps_exact_cited_facts_apart_from_strategy():
    blocker = {"ref": "capability:task-1:hash", "source_task_id": "task-1",
               "scope_status": "still_missing", "current_missing_capabilities": ["web.safe_search"],
               "affected_agents": [{"service_key": "research"}]}
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="plan-1", observed_state="blocked",
        blocker_ref=blocker["ref"],
    ))

    metadata = execution_review_metadata(proposal, _snapshot(_fact("task-1", blocker=blocker)))

    assert proposal.notes == "Try a smaller batch."
    assert metadata["source"] == "server_observation"
    assert metadata["observed_at"] == "2026-09-25T12:00:00+00:00"
    assert metadata["strategy_notes"] == proposal.notes
    assert metadata["facts"][0]["current_blocker"] == blocker
    assert metadata["facts"][0]["latest_error"]["message"] == "A prior attempt failed."
    assert metadata["facts"][0]["owner_service_key"] == "writer"


def test_execution_presentation_rejects_mismatched_or_missing_citations():
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="wrong-plan", observed_state="blocked",
    ))
    snapshot = _snapshot(_fact("task-1"))
    assert execution_review_metadata(proposal, snapshot) is None

    proposal.execution_review.references[0].plan_id = "plan-1"
    proposal.execution_review.references[0].observed_state = "delivered"
    assert execution_review_metadata(proposal, snapshot) is None

    proposal.execution_review.references[0].observed_state = "blocked"
    proposal.execution_review.revision = "stale"
    assert execution_review_metadata(proposal, snapshot) is None

    historical = Proposal(review_id="old", summary="Old proposal", tasks=[])
    assert execution_review_metadata(historical, snapshot) is None


def test_empty_citations_keep_revision_auditable():
    metadata = execution_review_metadata(_proposal(), _snapshot(_fact("task-1")))
    assert metadata["revision"] == "snapshot-1"
    assert metadata["references"] == [] and metadata["facts"] == []


@pytest.mark.asyncio
async def test_owner_card_shows_manor_ai_summary_not_execution_facts_or_private_notes(monkeypatch):
    proposal = _proposal(ExecutionReference(
        task_id="task-1", plan_id="plan-1", observed_state="blocked",
    ))
    proposal.summary = "Everything is on track; nothing needs you right now."
    metadata = execution_review_metadata(proposal, _snapshot(_fact("task-1")))
    post = AsyncMock(return_value=SimpleNamespace(id="message-1"))
    monkeypatch.setattr(service.chat_service, "post_message", post)
    monkeypatch.setattr(service, "_proposal_basis_reports", AsyncMock(return_value=[]))
    workspace = SimpleNamespace(id="workspace-1", entity_id="entity-1")

    await service._post_proposal_chat(
        workspace, proposal, [], db=object(), execution_review=metadata,
    )

    payload = post.await_args.kwargs
    card = payload["meta"]["proposal"]
    assert payload["message_kind"] == "proposal"
    assert payload["body"] == proposal.summary
    assert card["summary"] == proposal.summary
    assert "notes" not in card and card["approval"] is None
    # Facts and reasoning stay in the review record, off the owner's card.
    assert card["execution_review"] == metadata
