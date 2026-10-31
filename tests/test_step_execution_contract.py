"""A Step's execution contract is a closed declaration a human resume cannot widen."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.core.contracts.step_execution import (
    STEP_EXECUTION_CONTRACT_PARAM,
    StepExecutionContract,
)


pytestmark = pytest.mark.unit
INLINE = {"delivery": "inline", "effects": "read_only"}


@pytest.mark.parametrize("value", [
    {"delivery": "inline"}, {"delivery": "inline", "effects": "anything"},
    {**INLINE, "allowed_tools": ["generate_file"]},
])
def test_execution_contract_is_a_closed_explicit_declaration(value):
    with pytest.raises(ValidationError):
        StepExecutionContract.model_validate(value)


@pytest.mark.parametrize("replacement", [None, {}, {"delivery": "inline", "effects": "authorized"}])
def test_human_resume_cannot_remove_or_widen_the_materialized_contract(replacement):
    from packages.core.services.step_resume import apply_step_resume

    step = SimpleNamespace(params={STEP_EXECUTION_CONTRACT_PARAM: INLINE}, step_status="waiting_human")
    with pytest.raises(ValueError, match="server-owned"):
        apply_step_resume(step, params_update={STEP_EXECUTION_CONTRACT_PARAM: replacement})
    assert step.params == {STEP_EXECUTION_CONTRACT_PARAM: INLINE}
    assert step.step_status == "waiting_human"
    apply_step_resume(step, params_update={"answers": {"color": "blue"}})
    assert step.params[STEP_EXECUTION_CONTRACT_PARAM] == INLINE
    assert step.params["answers"] == {"color": "blue"}
