"""A run started with a person's confirmed answer completes only the matching approval wait."""

import pytest
from pydantic import ValidationError

from apps.api.routers.scheduler import RunNowRequest
from packages.core.ai.workflow_runner import preapproved_wait_decision

LATE_WAIT = {
    "wait_type": "approval",
    "response_variable": "late_publication_decision",
    "options": ["publish_late", "skip"],
    "approval_values": ["publish_late"],
}


def _variables(responses, user_id="person-1"):
    return {"__preapproved": {"responses": responses, "user_id": user_id}}


def test_the_matching_approval_completes_its_wait_for_the_person_who_confirmed():
    decided = preapproved_wait_decision(
        LATE_WAIT, step_id="approve_late",
        variables=_variables({"late_publication_decision": "publish_late"}),
    )
    assert decided["approved"] is True
    assert decided["decision"] == "publish_late"
    assert decided["approved_by"] == "person-1"


@pytest.mark.parametrize("variables", [
    {},
    _variables({"late_publication_decision": "skip"}),
    _variables({"other_decision": "publish_late"}),
    _variables({"late_publication_decision": "publish_later"}),
    _variables({"late_publication_decision": "publish_late"}, user_id=""),
])
def test_anything_else_leaves_the_wait_for_a_person(variables):
    assert preapproved_wait_decision(LATE_WAIT, step_id="approve_late", variables=variables) is None


def test_run_now_accepts_a_short_named_response_and_refuses_bad_ones():
    accepted = RunNowRequest(responses={"late_publication_decision": "publish_late"})
    assert accepted.responses == {"late_publication_decision": "publish_late"}
    with pytest.raises(ValidationError):
        RunNowRequest(responses={"Bad-Name": "publish_late"})
    with pytest.raises(ValidationError):
        RunNowRequest(responses={f"decision_{index}": "publish_late" for index in range(6)})
    with pytest.raises(ValidationError):
        RunNowRequest(responses={"late_publication_decision": ""})
