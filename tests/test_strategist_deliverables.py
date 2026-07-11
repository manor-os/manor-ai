import pytest
from pydantic import ValidationError
from packages.core.strategist.proposal import Deliverable, ProposedTask


def _valid(**over):
    base = dict(
        name="drafts", kind="value", shape="DraftPack", acceptance="3 distinct drafts", usage="handed to publish step"
    )
    base.update(over)
    return base


def test_valid_deliverable_and_task():
    d = Deliverable(**_valid())
    assert d.shape == "DraftPack"
    t = ProposedTask(title="Draft posts", owner_service_key="content_creation", deliverables=[d])
    assert t.deliverables[0].kind == "value"


def test_unknown_shape_rejected():
    with pytest.raises(ValidationError):
        Deliverable(**_valid(shape="NotAShape"))


def test_bad_kind_rejected():
    with pytest.raises(ValidationError):
        Deliverable(**_valid(kind="artifact"))


def test_empty_acceptance_or_usage_rejected():
    with pytest.raises(ValidationError):
        Deliverable(**_valid(acceptance=""))
    with pytest.raises(ValidationError):
        Deliverable(**_valid(usage="  "))


def test_proposed_task_requires_at_least_one_deliverable():
    with pytest.raises(ValidationError):
        ProposedTask(title="x", owner_service_key="content_creation", deliverables=[])


def test_proposed_task_requires_deliverables_when_omitted():
    # The real path: the LLM omits `deliverables` entirely. A default_factory
    # would silently pass an empty list — the field must be genuinely required.
    with pytest.raises(ValidationError):
        ProposedTask(title="x", owner_service_key="content_creation")


def test_prompt_hint_documents_deliverables_and_shapes():
    from packages.core.contracts.shapes import shape_names
    from packages.core.ai.runtime.strategist import RUNTIME_STRATEGIST_PROPOSAL_JSON_HINT as HINT

    ex = HINT["tasks"][0]
    assert "deliverables" in ex
    d = ex["deliverables"][0]
    assert set(d) >= {"name", "kind", "shape", "acceptance", "usage"}
    assert any(name in d["shape"] for name in shape_names())


def test_task_expected_output_carries_deliverables():
    from packages.core.strategist.service import _task_expected_output_from_proposed
    from packages.core.strategist.proposal import ProposedTask, Deliverable

    pt = ProposedTask(
        title="t",
        owner_service_key="content_creation",
        deliverables=[
            Deliverable(name="drafts", kind="value", shape="DraftPack", acceptance="3 drafts", usage="publish step")
        ],
    )
    out = _task_expected_output_from_proposed(pt)
    assert out["deliverables"][0]["shape"] == "DraftPack"
    assert out["deliverables"][0]["name"] == "drafts"


def test_task_expected_output_preserves_explicit_schema():
    from packages.core.strategist.service import _task_expected_output_from_proposed
    from packages.core.strategist.proposal import ProposedTask, Deliverable

    pt = ProposedTask(
        title="t",
        owner_service_key="content_creation",
        expected_output={
            "type": "object",
            "properties": {"brief": {"type": "string"}},
            "required": ["brief"],
        },
        deliverables=[
            Deliverable(name="brief", kind="value", shape="TextResult", acceptance="brief", usage="operator review")
        ],
    )

    out = _task_expected_output_from_proposed(pt)

    assert out["required"] == ["brief"]
    assert out["properties"]["brief"]["type"] == "string"
    assert out["deliverables"][0]["name"] == "brief"
