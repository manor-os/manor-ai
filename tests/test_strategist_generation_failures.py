"""Proposal generation failures are diagnostic, never a successful empty cohort."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError, LLMStreamProviderError
from packages.core.budget.enforcement import BudgetExceeded
from packages.core.services.credit_reservations import CreditReservationError
from packages.core.strategist import prompt
from packages.core.strategist.context import StrategistContext


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(prompt, "_load_skill_preamble", AsyncMock(return_value=None))
    monkeypatch.setattr(prompt, "runtime_strategist_system_prompt", lambda *_a, **_kw: "system")
    monkeypatch.setattr(prompt, "runtime_strategist_user_prompt", lambda *_a, **_kw: "review")
    monkeypatch.setattr(prompt, "runtime_strategist_user_prompt_v2", lambda *_a, **_kw: "review briefing")
    return StrategistContext(workspace=SimpleNamespace(id="workspace", entity_id="entity", name="Workspace"))


def _completion(value):
    return SimpleNamespace(content=value if isinstance(value, str) or value is None else json.dumps(value))


def _repair_feedback(completion):
    second_prompt = completion.await_args_list[1].args[1]
    return json.loads(second_prompt.split("Your previous response failed validation. ", 1)[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("briefing", [None, "Evidence briefing"])
async def test_invalid_json_feedback_reaches_one_repair_with_actor_scope(context, monkeypatch, briefing):
    completion = AsyncMock(side_effect=[
        _completion('{"summary":'),
        _completion({"summary": "No business work justified", "tasks": []}),
    ])
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    proposal = await prompt.generate_proposal(context, review_id="review", user_id="actor", briefing_markdown=briefing)
    assert proposal.summary == "No business work justified"
    assert proposal.tasks == []
    assert proposal.review_id == "review"
    assert completion.await_count == 2
    feedback = _repair_feedback(completion)
    assert feedback["contract"] == "tool_error"
    assert feedback["error"] == "proposal_invalid_output"
    assert feedback["details"]["validation_errors"] == [{
        "path": [], "type": "invalid_json", "message": "Expecting value",
        "line": 1, "column": 12, "position": 11,
    }]
    for call in completion.await_args_list:
        assert call.kwargs == {"entity_id": "entity", "workspace_id": "workspace", "user_id": "actor"}


@pytest.mark.asyncio
async def test_schema_diagnostics_identify_missing_field_without_echoing_model_input(context, monkeypatch):
    secret = "opaque-private-summary-that-is-not-a-diagnostic"
    invalid = {"summary": secret, "tasks": [{"title": "Write report", "owner_service_key": "writer"}]}
    completion = AsyncMock(side_effect=[_completion(invalid), _completion({"summary": "Done", "tasks": []})])
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    await prompt.generate_proposal(context, review_id="review")
    feedback = _repair_feedback(completion)
    assert feedback["details"]["validation_errors"] == [{
        "path": ["tasks", 0, "deliverables"], "type": "missing", "message": "Field required",
    }]
    assert secret not in json.dumps(feedback)
    assert "input" not in feedback["details"]["validation_errors"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["invalid proposal", "", "  ", None, '{"tasks": [{}]}'])
async def test_exhausted_validation_raises_instead_of_manufacturing_empty_proposal(context, monkeypatch, content):
    completion = AsyncMock(return_value=_completion(content))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        await prompt.generate_proposal(context, review_id="review")
    assert caught.value.code == "proposal_invalid_output"
    assert caught.value.details["attempts"] == 2
    assert caught.value.details["review_id"] == "review"
    assert caught.value.details["validation_errors"]
    assert completion.await_count == 2
    assert "fallback" not in str(caught.value).lower()


@pytest.mark.asyncio
async def test_a_failed_model_call_reports_why_instead_of_no_content(context, monkeypatch):
    """Staging 2026-11-04: a provider error surfaced as "returned no proposal content"."""
    completion = AsyncMock(return_value=SimpleNamespace(content="", usage={"error": "402 no credits"}))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        await prompt.generate_proposal(context, review_id="review")
    assert caught.value.code == "proposal_llm_unavailable"
    assert "402 no credits" in caught.value.details["cause"]
    assert completion.await_count == 1


@pytest.mark.asyncio
async def test_unavailable_provider_fails_once_with_redacted_diagnostics(context, monkeypatch, caplog):
    secret = "sk-provider-secret-123456789"
    completion = AsyncMock(side_effect=RuntimeError(f"No model available; api_key={secret}"))
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        await prompt.generate_proposal(context, review_id="review")
    assert caught.value.code == "proposal_llm_unavailable"
    assert caught.value.details["exception_type"] == "RuntimeError"
    assert "No model available" in caught.value.details["cause"]
    assert secret not in json.dumps(caught.value.details)
    assert secret not in caplog.text
    assert "<redacted>" in caught.value.details["cause"]
    completion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TimeoutError("stream timeout"), LLMStreamProviderError("stream failed"),
    CreditExhaustedError("credits exhausted"), CreditCheckUnavailableError("credit check unavailable"),
    CreditReservationError("reservation unavailable"), BudgetExceeded("budget exhausted"),
])
async def test_transport_and_billing_errors_retain_their_existing_type(context, monkeypatch, error):
    completion = AsyncMock(side_effect=error)
    monkeypatch.setattr(prompt, "runtime_execute_strategist_completion", completion)
    with pytest.raises(type(error)) as caught:
        await prompt.generate_proposal(context, review_id="review")
    assert caught.value is error
    completion.assert_awaited_once()


def test_validation_diagnostics_are_bounded_and_do_not_include_pydantic_inputs():
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        prompt._parse_proposal(json.dumps({"summary": "Empty tasks", "tasks": [{}] * 8}), review_id="review")
    details = caught.value.details
    assert len(details["validation_errors"]) == 20
    assert details["omitted_errors"] == 4
    assert all(set(error) == {"path", "type", "message"} for error in details["validation_errors"])


def test_custom_validation_message_redacts_credential_shaped_values():
    with pytest.raises(prompt.StrategistProposalGenerationError) as caught:
        prompt._parse_proposal(json.dumps({"summary": "Report", "tasks": [{
            "title": "Report", "owner_service_key": "writer", "deliverables": [{
                "name": "report", "kind": "value", "shape": "api_key=sk-sensitive-provider-secret",
                "acceptance": "Evidence verified", "usage": "Read the report",
            }],
        }]}), review_id="review")
    assert "sk-sensitive-provider-secret" not in json.dumps(caught.value.details)
    assert caught.value.details["validation_errors"][0]["path"] == ["tasks", 0, "deliverables", 0, "shape"]
